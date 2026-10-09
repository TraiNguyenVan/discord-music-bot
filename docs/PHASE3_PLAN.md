# Phase 3 — Smarter autoplay: learning, ranking, UX

> Status: **planned** (not started). Phases 1–2 are deployed — see "Current
> architecture" below for the state this plan builds on.
>
> Regression harness: `tests/queue_sim.py` (see "How to test" at the bottom).
> Every Phase 3 item must land with a new scenario in that harness.

## Current architecture (phases 1–2, shipped)

- **Two-segment queue** (`cogs/music.py`): `GuildState.user_queue` +
  `auto_queue`; `pop_head()` = user first, `peek_head()`, `qtotal`.
- **Autoplay engine**: `_autoplay_check(reason)` is the single brain, called
  from every mutation (advance/drain/add/remove/clear/toggle/loop change).
  Fills slice to `AUTO_BUFFER_TARGET = 8` when the buffer drops below
  `AUTO_TOPUP_AT = 3`; hard cap `AUTO_MAX = 16` (oldest dropped first).
- **Seeding** (`_pick_seed`): fresh user pick (30-min `USER_SEED_WINDOW`) →
  current track → last played → stale user pick; `dry_seeds` are skipped and
  rotate after `AUTO_DRY_RETRY_DELAY`.
- **Fetches**: `_get_mix_entries(seed)` caches the RD-mix listing per seed
  (`MIX_CACHE_TTL` 30 min, `MIX_CACHE_MAX` 32). Failures retry on
  `AUTO_RETRY_DELAYS = (30, 60, 120)` via `auto_retry_task`.
- **Dedupe**: `_filter_auto_candidates()` is the only gate for auto-path
  additions (drops seed/queued/played/intra-batch dupes).
- **Safety**: fills re-validate idleness at insertion time (never hijack a
  playing/paused player); `auto_status` (`ok|finding|retrying|dry`) is shown
  on the panel; `last_played` survives `/stop` for bootstrap.
- Loops own the queue: `loop=track` and `loop=queue` suspend autoplay;
  `loop=queue` rotates within the track's own segment.

## P4 — skip-weighted uploader filtering (do this first, biggest win/effort ratio)

The room votes with skips. Give uploaders a session reputation and let it
steer suggestions.

1. **Track the stats** — new `GuildState` field (session-only, cleared in
   `_reset_state`):
   - `uploader_stats: dict[str, tuple[int, int]]` → `(plays, skips)`
2. **Feed it**:
   - Natural finish: in `_play_next_inner`'s `_after` callback, the clean-
     finish branch (`else: st.fast_fails = 0`) → `plays += 1` for
     `nxt.uploader`. NOTE: `_after` runs in a worker thread — dict updates
     of ints are fine, no lock needed.
   - Skip: in `_do_skip`, both the playing branch and the idle-next branch,
     right after the debounce accept → `skips += 1` for `st.current.uploader`.
   - Optional: `/remove` of an autoplay track → count as a skip.
3. **Use it** — in `_filter_auto_candidates`, after the dedupe pass, **sort**
   survivors (do not hard-filter) by uploader score before the caller slices
   to `needed`:
   - `score = skips / (plays + skips)` with a minimum sample
     (e.g. ignore until `plays + skips >= 2`, then demote if `score > 0.6`).
   - Demoted tracks go to the back of the survivor list, so they only play
     when nothing better exists. Hard-drop only when `score > 0.9` and
     `skips >= 3`.
   - `_autoplay_fill` already slices `survivors[:needed]` — sorting inside
     the filter is enough; no other call site changes.
4. **Log it**: `[event] autoplay-demoted uploader=X score=0.75 n=3` so the
   behavior is observable.

## P5 — explore/exploit seeding

Today `_pick_seed` always exploits the freshest taste signal, which narrows
over time (recs of recs). Add a small exploration factor:

1. `GuildState`: `fill_count: int = 0` (bump on every successful fill) and
   `user_seed_history: collections.deque[str]` (`maxlen=12`, append the vid
   in `_note_user_seed`, dedupe consecutive repeats).
2. In `_pick_seed`: when `fill_count % EXPLORE_EVERY == 0` (suggest
   `EXPLORE_EVERY = 5`) **and** the history has ≥2 non-dry vids that differ
   from the default candidate, pick one at random (seed title = the history
   entry's title — store `(vid, title)` tuples) and log
   `[event] autoplay-explore seed=…` (so exploratory fills are debuggable).
3. Keep exploitation untouched for the other 4 of 5 fills.

## P6 — mix survivor ranking (reuse `_rank_search` heuristics)

Mix listings contain junk (2h compilations, lives). `_filter_auto_candidates`
currently keeps YouTube's order; rank survivors instead:

- Sort key per entry (cheap, pure): demote `live_status is_live/is_upcoming`,
  demote duration > 900s **unless** the room plays long content (see
  heuristic below), prefer channels containing official/topic/vevo, prefer
  60–720s durations. This is `_rank_search`'s scoring adapted to flat mix
  entries — extract the shared parts into a helper so both callers stay in
  sync rather than copying the logic.
- "Room plays long content" heuristic: average duration of `last_played`
  tracks — track `recent_durations: deque[int]` (maxlen 10), append on each
  now-playing; if avg ≥ 600s, stop demoting long entries.
- Fold P4's uploader score into this same sort (one comparator, applied once).

## UX — make the learning visible and actionable

1. **"🚫 Nothing like this" button** on the panel (row 2):
   - New fixed `custom_id="music:notthis"` in `MusicPanelView` (persistent
     view is re-registered in `setup()`; `_update_panel` attaches a fresh view
     on every edit, so the button appears on existing panels after the next
     panel update).
   - Handler: seed-blacklists the current track's uploader for the session
     (`st.uploader_ban: set[str]` checked in `_filter_auto_candidates` as a
     hard drop) **and** performs the same skip as `_do_skip`. Combined with
     P4 this is the user-facing control over the recommender.
   - Mirror as a web action `"notthis"` in `web_control` (add to the allowed
     set in `web/server.py`), and a "Queue" item button in `web_queue_page`
     output if the web UI wants it (check `web/pick.html` queue rendering).
2. **Seed lineage in `/queue`**: `Track.seeded_from: str = ""`, set in
   `_filter_auto_candidates` (needs the seed's title passed in — it already
   receives `seed_id`; pass `seed_title` from callers). Render as
   `… — 🔮 Autoplay (mix of "Song B")` in `/queue`, the panel queue button,
   and `web_queue_page` items (`"seeded_from"` field, additive for the web UI).
3. **Autoplay buffer knob**: `AUTO_MODES = {"chill": 4, "normal": 8, "marathon": 16}`;
   `GuildState.auto_target: int = AUTO_BUFFER_TARGET`. `/autoplay` gains
   choices `chill|normal|marathon` (plus the existing on/off/toggle);
   `_autoplay_fill` uses `st.auto_target` instead of the constant. Panel
   button stays a plain on/off toggle; show the mode in the Autoplay label
   (e.g. `ON — marathon (16)`).

## Non-goals / later ideas

- P3 multi-seed blending (interleave two mixes per fill) — costs an extra
  fetch per fill; revisit only if exploration (P5) doesn't diversify enough.
- Persisting taste/played history across restarts (sqlite/json) — currently
  session-only by design; needs a privacy/dedup story first.
- `/autoplay` per-user profiles — the queue is a shared room, keep it room-wide.

## How to test (required per item)

Harness: `tests/queue_sim.py`. Run:

```sh
python3 -m venv /tmp/simvenv && /tmp/simvenv/bin/pip install "discord.py>=2.4.0" yt-dlp PyNaCl
/tmp/simvenv/bin/python tests/queue_sim.py
```

It drives the real `cogs/music.py` with fakes (voice client, yt-dlp) — 27
scenarios map 1:1 to the v1 bug hunt plus regressions. All must stay green.

New scenarios to add with Phase 3:

- **P4**: mix with two uploaders; skip 3 tracks from uploader X in a row →
  next fill's sliced survivors contain none (or only trailing) X tracks;
  plays with no skips → X stays eligible. Use `Track.uploader` in
  `SimDB._flat` (add an `uploader=` param).
- **P5**: seed history `[A, B, C]`, force `st.fill_count = EXPLORE_EVERY - 1`
  → next fill's fetch URL is NOT the default candidate and is in history;
  `fill_count = 0` → default candidate again.
- **P6**: mix entries `[2h mix, live, 3min song, Topic channel song]` with
  `needed=2` → the two sane tracks are the ones sliced.
- **Not-this button**: 3 autoplay tracks from uploader X queued; press the
  button (call the web action or panel callback path) → current skipped,
  X's remaining tracks gone from the queue, X absent from the next fill.
- **Knob**: set marathon → fill brings the buffer to 16; chill → 4.

## Suggested order

1. P4 (stats + sort in `_filter_auto_candidates`) — smallest diff, biggest
   perceived intelligence gain.
2. P6 ranking (shared helper with `_rank_search`) — composes with P4's sort.
3. UX buttons/status (notthis + lineage + knob) — makes 1–2 visible.
4. P5 exploration — last, since it only matters once P4/P6 make exploitation
   strong enough to be worth escaping occasionally.
