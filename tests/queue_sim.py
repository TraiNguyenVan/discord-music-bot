#!/usr/bin/env python3
"""Regression harness for the queue/autoplay architecture.

Runs the REAL cogs/music.py logic with fake Discord voice clients and a
fake yt-dlp, so every user-facing queue scenario is reproducible in
seconds. The scenarios map 1:1 to the bugs found by the v1 simulation run.

Run (only the bot's own deps are needed):

    python3 -m venv /tmp/simvenv
    /tmp/simvenv/bin/pip install "discord.py>=2.4.0" yt-dlp PyNaCl
    /tmp/simvenv/bin/python tests/queue_sim.py

Exit code 0 = all scenarios green. Add a scenario for every queue/autoplay
change (see docs/PHASE3_PLAN.md for the planned ones).
"""
import asyncio
import re
import sys
import threading
import time
from types import SimpleNamespace

from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import discord  # noqa: E402
import cogs.music as M  # noqa: E402
from cogs.music import (  # noqa: E402
    AUTO_BUFFER_TARGET,
    AUTO_MAX,
    AUTO_TOPUP_AT,
    Music,
    Track,
    _video_id,
)

# fast retry ladders in sim (real: 30/60/120s + 45s dry rotation)
M.AUTO_RETRY_DELAYS = (0.05, 0.05, 0.05)
M.AUTO_DRY_RETRY_DELAY = 0.05

RESULTS = []


def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  {'✓ PASS' if ok else '✗ FAIL'} — {name}" + (f" | {detail}" if detail else ""))


def header(txt):
    print("\n" + "=" * 78)
    print(txt)
    print("=" * 78)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeVoiceChannel:
    def __init__(self, guild, cid=1, name="General"):
        self.guild = guild
        self.id = cid
        self.name = name


class FakeMember:
    def __init__(self, guild, name="User", uid=100):
        self.guild = guild
        self.display_name = name
        self.name = name
        self.id = uid
        self.bot = False
        self.voice = SimpleNamespace(channel=FakeVoiceChannel(guild))

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, gid):
        self.id = gid
        self.name = f"guild-{gid}"
        self.voice_client = None
        self._channels = {}

    def get_channel(self, cid):
        return self._channels.get(cid)

    @property
    def me(self):
        return None


class FakeVoiceClient:
    """Mimics discord.py VoiceClient player semantics:
    - play() raises ClientException iff is_playing() (NOT when paused!)
    - play() while paused silently REPLACES the player; the paused track's
      after() never fires (real discord.py behaviour)
    - stop() ends the current player -> after(err) fires once
    """

    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel
        self.source = None
        self._connected = True
        self._playing = False
        self._paused = False
        self._after_cb = None
        self._after_inflight = 0
        guild._channels[channel.id] = channel

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._paused

    def play(self, source, after=None):
        if not self.is_connected():
            raise discord.ClientException("Not connected to voice.")
        if self.is_playing():
            raise discord.ClientException("Already playing audio.")
        self.source = source
        self._after_cb = after
        self._playing = True
        self._paused = False

    def stop(self):
        if self._playing:
            self._playing = False
            self._fire_after(None)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    async def disconnect(self, force=False):
        self._connected = False
        self._playing = False
        self._paused = False

    async def move_to(self, channel):
        self.channel = channel

    def _fire_after(self, err):
        cb, self._after_cb = self._after_cb, None
        if cb is None:
            return
        self._after_inflight += 1

        def run():
            try:
                cb(err)
            finally:
                self._after_inflight -= 1

        threading.Thread(target=run, daemon=True).start()


class FakeBot:
    def __init__(self):
        self.user = SimpleNamespace(id=42, display_name="TestBot", name="TestBot")
        self.loop = None
        self.voice_clients = []
        self._guilds = {}

    def get_guild(self, gid):
        return self._guilds.get(gid)

    def add_guild(self, g):
        self._guilds[g.id] = g


class FakePCM:
    def __init__(self, volume):
        self.volume = volume


class SimDB:
    def __init__(self):
        self.videos = {}
        self.mixes = {}       # seed -> entries OR Exception
        self.playlists = {}
        self.search = {}
        self.mix_gates = {}   # seed -> asyncio.Event (hold fetch until set)
        self.fail_count = {}  # seed -> remaining #failed fetches
        self.calls = []       # (query, playlist, flat)

    def add_video(self, vid, title, duration=30):
        self.videos[vid] = {
            "id": vid, "title": title, "url": f"http://stream/{vid}",
            "duration": duration, "uploader": "Uploader", "channel": "Uploader",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        }

    def add_mix(self, seed, n=24, include_seed=True, vids=None):
        entries = []
        if include_seed:
            entries.append(self._flat(seed, f"Mix 0 [{seed}] (the seed itself)"))
        source = vids if vids is not None else [f"{seed}m{i:02d}xx" for i in range(n)]
        for i, vid in enumerate(source):
            entries.append(self._flat(vid, f"Mix {i + 1} [{seed}]"))
        self.mixes[seed] = entries

    def add_playlist(self, plid, n=10):
        self.playlists[plid] = [
            self._flat(f"{plid}p{i:02d}xx", f"Pl track {i + 1}") for i in range(n)
        ]

    @staticmethod
    def _flat(vid, title, duration=200):
        return {
            "id": vid, "title": title, "duration": duration,
            "channel": "MixChan", "uploader": "MixChan",
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        }

    def gate(self, seed):
        ev = asyncio.Event()
        self.mix_gates[seed] = ev
        return ev


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class Harness:
    def __init__(self):
        self.bot = FakeBot()
        self.cog = Music(self.bot)
        self.db = SimDB()
        self._gid = 0

        async def _noop_panel(guild_id):
            pass

        async def _noop_gate():
            pass

        self.cog._update_panel = _noop_panel
        self.cog._search_gate = _noop_gate
        self.cog._source = lambda url, volume: FakePCM(volume)
        self.cog._extract = self._fake_extract

    async def _fake_extract(self, query, playlist=False, search_n=0, flat=False,
                            playlist_start=1, playlist_end=25):
        self.db.calls.append((query, playlist, flat))
        q = str(query)

        m = re.search(r"list=RD([A-Za-z0-9_-]+)", q)
        if m:
            seed = m.group(1)
            gate = self.db.mix_gates.get(seed)
            if gate is not None and not gate.is_set():
                await gate.wait()
            if self.db.fail_count.get(seed, 0) > 0:
                self.db.fail_count[seed] -= 1
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            data = self.db.mixes.get(seed, [])
            if isinstance(data, Exception):
                raise data
            return [dict(e) for e in data[playlist_start - 1:playlist_end]]

        if playlist:
            m = re.search(r"list=([A-Za-z0-9_-]+)", q)
            if m:
                data = self.db.playlists.get(m.group(1), [])
                return [dict(e) for e in data[playlist_start - 1:playlist_end]]

        m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]+)", q)
        if m and "list=" not in q:
            vid = m.group(1)
            d = self.db.videos.get(vid) or {
                **SimDB._flat(vid, f"Deep {vid}"), "url": f"http://stream/{vid}"}
            return [dict(d)]

        return [dict(e) for e in self.db.search.get(q, [])]

    # -- world -------------------------------------------------------------
    def fresh(self):
        self._gid += 1
        guild = FakeGuild(2000 + self._gid)
        chan = FakeVoiceChannel(guild, cid=1, name="General")
        guild.voice_client = FakeVoiceClient(guild, chan)
        self.bot.add_guild(guild)
        self.db = SimDB()
        # scenario isolation: a mix listing cached for one scenario's fake
        # video ids must not satisfy another scenario's gated/failing fetch
        self.cog._mix_cache.clear()
        return guild, self.member(guild)

    def member(self, guild, name="User"):
        return FakeMember(guild, name=name)

    def track(self, vid, title, member=None, duration=180, from_mix=False):
        return Track(
            title=title,
            webpage_url=f"https://www.youtube.com/watch?v={vid}",
            stream_url=f"http://stream/{vid}",
            duration=duration, thumbnail=None,
            uploader="Uploader",
            requester=getattr(member, "display_name", "User"),
            requester_id=getattr(member, "id", 100),
            needs_resolve=False,
            from_mix=from_mix,
        )

    # -- mirrors of the NEW command bodies -----------------------------------
    async def play_single(self, guild, member, vid, title, duration=180):
        """/play url -> _queue_track core."""
        cog = self.cog
        st = cog.state(guild.id)
        vc = guild.voice_client
        cog._add_user_track(st, self.track(vid, title, member, duration))
        cog._autoplay_check(guild.id, "add")
        if vc.is_playing() or vc.is_paused():
            return f"queued {title} (user #{len(st.user_queue)})"
        await cog._play_next(guild)
        await self.quiesce(guild)
        return f"started {title}"

    async def add_playlist(self, guild, member, tracks):
        """/play playlist branch: _extend_user_tracks + check + idle play."""
        cog = self.cog
        st = cog.state(guild.id)
        vc = guild.voice_client
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        cog._extend_user_tracks(st, tracks)
        cog._autoplay_check(guild.id, "playlist-add")
        if was_idle:
            await cog._play_next(guild)
        await self.quiesce(guild)

    async def mix_button(self, guild, member, seed):
        """QueueMixView._on_click core: shared filter + capped extend."""
        cog = self.cog
        st = cog.state(guild.id)
        entries = await cog._get_mix_entries(seed)
        tracks = cog._filter_auto_candidates(st, entries, seed)
        for t in tracks:
            t.requester = "🔮 Mix"
        cog._extend_auto_tracks(st, tracks)
        cog._autoplay_check(guild.id, "mix-button")
        await self.quiesce(guild)
        return tracks

    def do_clear(self, guild):
        """/clear."""
        st = self.cog.state(guild.id)
        self.cog._clear_queues(st)
        self.cog._autoplay_check(guild.id, "clear")

    def do_shuffle(self, guild):
        """/shuffle."""
        self.cog._do_shuffle(self.cog.state(guild.id))

    def set_loop(self, guild, mode):
        """/loop <mode>."""
        st = self.cog.state(guild.id)
        st.loop_mode = mode
        self.cog._autoplay_check(guild.id, "loop-set")

    def set_autoplay(self, guild, on):
        """/autoplay on|off."""
        self.cog._set_autoplay(guild.id, on)

    def do_remove(self, guild, index):
        """/remove <index>."""
        st = self.cog.state(guild.id)
        t = self.cog._remove_at(st, index)
        if t is None:
            return None
        vid = _video_id(t.webpage_url)
        if vid:
            st.played_ids.append(vid)
        self.cog._autoplay_check(guild.id, "remove")
        return t

    async def do_pause(self, guild):
        """/pause."""
        st = self.cog.state(guild.id)
        self.cog._set_paused(st, guild.voice_client, True)

    async def do_stop(self, guild):
        """/stop."""
        st = self.cog.state(guild.id)
        vc = guild.voice_client
        self.cog._reset_state(st)
        if vc:
            await self.cog._vc_disconnect(guild.id, vc)
        await self.quiesce(guild)

    # -- playback drivers ----------------------------------------------------
    def age(self, st, secs):
        if st.started_at:
            st.started_at -= secs

    def finish_natural(self, guild, err=None):
        st = self.cog.state(guild.id)
        vc = guild.voice_client
        assert vc and vc.is_playing(), "finish_natural(): nothing playing"
        self.age(st, 250)
        vc._playing = False
        vc._fire_after(err)

    def finish_dead(self, guild):
        vc = guild.voice_client
        assert vc and vc.is_playing(), "finish_dead(): nothing playing"
        vc._playing = False
        vc._fire_after(None)

    async def advance(self, guild, n=1, dead=False):
        for _ in range(n):
            if dead:
                self.finish_dead(guild)
            else:
                self.finish_natural(guild)
            await self.quiesce(guild)

    async def quiesce(self, guild, timeout=10.0):
        st = self.cog.state(guild.id)
        vc = guild.voice_client
        t0 = time.time()
        while True:
            await asyncio.sleep(0.03)
            held = any(not ev.is_set() for ev in self.db.mix_gates.values())
            busy = False
            if vc is not None and vc._after_inflight:
                busy = True
            if st.mix_task and not st.mix_task.done() and not held:
                busy = True
            if st.auto_retry_task and not st.auto_retry_task.done() and not held:
                busy = True
            if st.prewarm_task and not st.prewarm_task.done():
                busy = True
            if not busy:
                return
            if time.time() - t0 > timeout:
                raise TimeoutError("sim did not settle")

    # -- observation ----------------------------------------------------------
    def snap(self, guild, label):
        st = self.cog.state(guild.id)
        cur = st.current.title if st.current else "—"
        u = " | ".join(t.title for t in st.user_queue[:4])
        a = " | ".join(t.title for t in st.auto_queue[:4])
        print(f"    [{label}] now={cur!r} user={len(st.user_queue)} auto={len(st.auto_queue)} "
              f"loop={st.loop_mode} auto_status={st.auto_status} "
              f"seed={st.user_seed_id} dry={len(st.dry_seeds)}")
        if u or a:
            print(f"        user: {u or '-'}")
            print(f"        auto: {a or '-'}")

    def mix_calls(self, seed_substr):
        return [c for c in self.db.calls if "list=RD" in c[0] and seed_substr in c[0]]

    def auto_vids(self, guild):
        st = self.cog.state(guild.id)
        return [v for v in (_video_id(t.webpage_url) for t in st.auto_queue) if v]

    def cleanup(self, guild):
        st = self.cog.state(guild.id)
        self.cog._reset_state(st)
        self.cog.states.pop(guild.id, None)


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

async def s1_adaptation(h):
    header("S1' — user example: 1 video → autoplay spawns → keep adding songs")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    h.db.add_mix("DDDDDDDDD04", n=24)  # D is the last user pick

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    h.snap(guild, "A started — fill seeded from A")
    fetches_after_start = len(h.mix_calls("RD"))

    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    await h.play_single(guild, member, "CCCCCCCCC03", "Song C")
    await h.play_single(guild, member, "DDDDDDDDD04", "Song D")
    h.snap(guild, "added B, C, D — steering + no refetch while buffer ≥3")
    no_refetch_on_adds = len(h.mix_calls("RD")) == fetches_after_start

    steers = [t.title for t in st.user_queue] == ["Song B", "Song C", "Song D"]

    # play through A,B,C,D and the mix until the buffer drops below AUTO_TOPUP_AT
    d_seeded = False
    for _ in range(12):
        await h.advance(guild)
        if h.mix_calls("DDDDDDDDD04"):
            d_seeded = True
            break
    h.snap(guild, "buffer ran low — refill fired (seeded from…?)")
    capped = len(st.auto_queue) <= AUTO_BUFFER_TARGET
    report("S1a: singles steer ahead of autoplay (B,C,D in order)", steers,
           f"user_queue={[t.title for t in st.user_queue]}")
    report("S1b: no burst of fetches just from adding songs", no_refetch_on_adds,
           f"rd-fetches={len(h.mix_calls('RD'))}")
    report("S1c: refill re-seeds from the LATEST USER PICK (Song D), not a stale mix chain",
           d_seeded, f"fetches for D-seed={len(h.mix_calls('DDDDDDDDD04'))}")
    report("S1d: autoplay buffer stays small (≤ AUTO_BUFFER_TARGET)", capped,
           f"auto_len={len(st.auto_queue)} target={AUTO_BUFFER_TARGET}")
    h.cleanup(guild)


async def s2_playlist_steers(h):
    header("S2' — add a PLAYLIST while an autoplay tail exists")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    h.db.add_playlist("PLX")

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    h.snap(guild, "A playing, autoplay buffer present")

    pl_tracks = [h.track(f"PLXp{i:02d}xx", f"Pl track {i + 1}", member, 200)
                 for i in range(10)]
    await h.add_playlist(guild, member, pl_tracks)
    h.snap(guild, "playlist added — lands in the USER segment")

    await h.advance(guild)  # A finishes
    h.snap(guild, "A finished — playlist head plays next, not a mix track")
    report("S2: playlists play BEFORE autoplay (no longer buried)",
           st.current is not None and st.current.title == "Pl track 1",
           f"now={st.current.title if st.current else None!r}")
    h.cleanup(guild)


async def s3_clear_no_deadend(h):
    header("S3' — /clear mid-autoplay, then queue drains")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    first_8 = set(h.auto_vids(guild))
    rd_calls_after_fill = len(h.mix_calls("AAAAAAAAA01"))
    h.snap(guild, "A playing + buffer filled")

    h.do_clear(guild)
    h.snap(guild, "user cleared the queue (A still playing)")
    await h.advance(guild)  # A finishes -> drain -> refill
    h.snap(guild, "A finished — refilled from cache, music never stopped")
    vc = guild.voice_client
    cleared_vids_returned = bool(first_8 & set(h.auto_vids(guild)))
    report("S3a: after /clear the queue refills and KEEPS PLAYING (no silence)",
           vc.is_playing() and len(st.auto_queue) > 0,
           f"playing={vc.is_playing()} auto={len(st.auto_queue)} status={st.auto_status}")
    report("S3b: cleared tracks are never re-suggested", not cleared_vids_returned,
           f"overlap={first_8 & set(h.auto_vids(guild))}")
    report("S3c: refill used the seed cache (no refetch)",
           len(h.mix_calls("AAAAAAAAA01")) == rd_calls_after_fill,
           f"rd_calls={len(h.mix_calls('AAAAAAAAA01'))}")
    h.cleanup(guild)


async def s4_shuffle_keeps_steering(h):
    header("S4' — shuffle, then add a new song")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    h.do_shuffle(guild)
    await h.play_single(guild, member, "CCCCCCCCC03", "Song C")
    h.snap(guild, "shuffled, then added C")
    await h.advance(guild)  # A finishes -> user head (B) plays
    await h.advance(guild)  # B finishes -> C plays
    h.snap(guild, "A and B finished")
    report("S4: after shuffle, user adds still play before ALL autoplay tracks",
           st.current is not None and st.current.title == "Song C"
           and len(st.user_queue) == 0,
           f"now={st.current.title if st.current else None!r} user_left={len(st.user_queue)}")
    h.cleanup(guild)


async def s5_loop_queue_no_starvation(h):
    header("S5' — loop=queue + autoplay ON")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    h.set_loop(guild, "queue")
    fetches_before = len(h.mix_calls("RD"))
    auto_before = len(st.auto_queue)

    played = []
    for _ in range(4):
        await h.advance(guild)
        if st.current:
            played.append(st.current.title)
    h.snap(guild, "4 track-ends under loop=queue")
    ok = played == ["Song B", "Song A", "Song B", "Song A"] \
        and len(st.auto_queue) == auto_before \
        and len(h.mix_calls("RD")) == fetches_before
    report("S5: loop=queue cycles the user segment only — no starvation, no re-fetch",
           ok, f"played={played} auto={len(st.auto_queue)} fetches={len(h.mix_calls('RD'))}")
    h.cleanup(guild)


async def s6_loop_track_no_buildup(h):
    header("S6' — loop=track + autoplay ON")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    h.set_loop(guild, "track")
    fetches_before = len(h.mix_calls("RD"))
    auto_before = len(st.auto_queue)
    for _ in range(3):
        await h.advance(guild)
    h.snap(guild, "3 replays of A under loop=track")
    ok = st.current and st.current.title == "Song A" \
        and len(h.mix_calls("RD")) == fetches_before \
        and len(st.auto_queue) == auto_before
    report("S6: loop=track suspends autoplay (no new fetches, no tail growth)",
           ok, f"fetches={len(h.mix_calls('RD'))} auto={len(st.auto_queue)}")
    h.cleanup(guild)


async def s7_skip_debounce(h):
    header("S7' — skip double-tap (expected: works as designed)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    for vid, t in [("BBBBBBBBB02", "Song B"), ("CCCCCCCCC03", "Song C"),
                   ("DDDDDDDDD04", "Song D")]:
        await h.play_single(guild, member, vid, t)

    m1 = await h.cog._do_skip(guild, member)
    await h.quiesce(guild)
    cur1 = st.current.title
    m2 = await h.cog._do_skip(guild, member)  # double-tap inside window
    await h.quiesce(guild)
    cur2 = st.current.title
    st.last_skip_at = 0
    m3 = await h.cog._do_skip(guild, member)
    await h.quiesce(guild)
    cur3 = st.current.title
    print(f"    skip1={m1!r} -> {cur1!r} | skip2={m2!r} -> {cur2!r} | skip3={m3!r} -> {cur3!r}")
    report("S7: double-tap debounced, one track per skip",
           m2.startswith("Already skipping") and (cur1, cur2, cur3) == ("Song B", "Song B", "Song C"),
           f"({cur1!r}, {cur2!r}, {cur3!r})")
    h.cleanup(guild)


async def s8_dry_visible(h):
    header("S8' — mix fully exhausted (all suggestions already played)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    # A's mix = only 2 tracks, both already played earlier in the session
    h.db.add_mix("AAAAAAAAA01", n=0, include_seed=False,
                 vids=["OLDvid00001", "OLDvid00002"])
    st.played_ids.extend(["OLDvid00001", "OLDvid00002"])
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_video("BBBBBBBBB02", "Song B")
    h.db.add_mix("BBBBBBBBB02", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    h.snap(guild, "fill dry at A's start (all suggestions played)")
    dry_visible_early = st.auto_status == "dry"

    await h.advance(guild)  # A finishes -> drain -> no seed -> visible dry
    h.snap(guild, "A finished — drained, dry status visible")
    dry_visible_drained = st.auto_status == "dry"
    msg = await h.cog._do_skip(guild, member)
    print(f"    /skip -> {msg!r}")

    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    await h.advance(guild)  # B finishes -> mix B plays
    h.snap(guild, "user added B — autoplay re-seeded and recovered")
    report("S8a: dry state is VISIBLE on the panel/status (not silent)",
           dry_visible_early and dry_visible_drained,
           f"early={dry_visible_early} drained={dry_visible_drained}")
    report("S8b: /skip explains the situation instead of a bare 'Queue is empty.'",
           "ran dry" in msg, f"msg={msg!r}")
    report("S8c: any user add re-seeds autoplay (recovery)",
           st.current is not None and st.current.title.startswith("Mix")
           and len(st.auto_queue) > 0,
           f"now={st.current.title if st.current else None!r}")
    h.cleanup(guild)


async def _drain_fill_race_setup(h, pause: bool):
    """Common construction: a drain-time autoplay fetch is in flight while
    the user adds B (and optionally pauses it). Returns (guild, member, gate)."""
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    gate = h.db.gate("AAAAAAAAA01")  # hold A's mix fetch

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # fill gated
    await h.advance(guild)  # A ends -> drain -> fill still in flight
    h.snap(guild, "drained, fill in flight (gated), bot idle")
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")  # B starts
    h.snap(guild, "user added B while fetch still in flight")
    if pause:
        await h.do_pause(guild)
        print("    user paused B")
    return guild, member, gate


async def s9_race_playing(h):
    header("S9' — autoplay fill lands while a user track is PLAYING")
    guild, member, gate = await _drain_fill_race_setup(h, pause=False)
    st = h.cog.state(guild.id)
    b_started_at = st.started_at

    gate.set()  # fetch completes now
    await h.quiesce(guild)
    h.snap(guild, "fill completed while B playing")
    vc = guild.voice_client

    ok = (st.current is not None and st.current.title == "Song B"
          and vc.is_playing()
          and st.started_at == b_started_at
          and len(st.auto_queue) > 0)
    report("S9: stale fill only buffers — never hijacks or resets the playing track",
           ok, f"now={st.current.title if st.current else None!r} "
                f"playing={vc.is_playing()} started_at_intact={st.started_at == b_started_at}")
    h.cleanup(guild)


async def s10_race_paused(h):
    header("S10' — autoplay fill lands while a user track is PAUSED")
    guild, member, gate = await _drain_fill_race_setup(h, pause=True)
    st = h.cog.state(guild.id)
    vc = guild.voice_client
    assert vc.is_paused() and not vc.is_playing()

    gate.set()  # fetch completes now — this used to hijack the pause
    await h.quiesce(guild)
    h.snap(guild, "fill completed while B paused")
    ok = (vc.is_paused() and not vc.is_playing()
          and st.current is not None and st.current.title == "Song B")
    report("S10: PAUSED player is never hijacked; the paused track survives",
           ok, f"paused={vc.is_paused()} playing={vc.is_playing()} "
                f"now={st.current.title if st.current else None!r}")
    h.cleanup(guild)


async def s11_hot_spin(h):
    header("S11' — instant stream deaths under loop=track (breaker, expected OK)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A", duration=300)
    await h.play_single(guild, member, "AAAAAAAAA01", "Song A", duration=300)
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    h.set_loop(guild, "track")

    for i in range(3):
        await h.advance(guild, dead=True)
        print(f"    death {i + 1}: fast_fails={st.fast_fails} now={st.current.title!r}")
    report("S11: hot-spin breaker still advances after 3 instant deaths",
           st.current is not None and st.current.title == "Song B",
           f"now={st.current.title if st.current else None!r}")
    h.cleanup(guild)


async def s12_off_midfill(h):
    header("S12' — autoplay toggled OFF while a fill is in flight (expected OK)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    gate = h.db.gate("AAAAAAAAA01")

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # fill gated
    h.set_autoplay(guild, False)  # user flips autoplay OFF mid-fetch
    gate.set()
    await h.quiesce(guild)
    await h.advance(guild)  # A finishes -> drain, autoplay OFF
    h.snap(guild, "fill discarded, drain silent (autoplay off)")
    report("S12: mid-flight toggle OFF discards survivors; nothing queued after drain",
           len(st.auto_queue) == 0 and st.current is None and not st.autoplay,
           f"auto={len(st.auto_queue)} status={st.auto_status}")
    h.cleanup(guild)


async def s13_stop_midfill(h):
    header("S13' — /stop while a fill is in flight (expected OK)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    gate = h.db.gate("AAAAAAAAA01")

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # fill gated
    await h.do_stop(guild)  # cancels mix_task, bumps session
    gate.set()  # release gate after cancellation
    await h.quiesce(guild)
    h.snap(guild, "stop mid-fill: clean")
    report("S13: mid-fill stop cancels cleanly, no resurrected tracks",
           len(st.auto_queue) == 0 and st.current is None
           and (st.mix_task is None or st.mix_task.done())
           and st.auto_status == "ok",
           f"auto={len(st.auto_queue)} status={st.auto_status}")
    h.cleanup(guild)


async def s14_retry_recovery(h):
    header("S14' — failed autoplay fetches: retry ladder + drain auto-recovery")
    # Part A: failures while music is playing, then recovery
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)
    h.db.fail_count["AAAAAAAAA01"] = 2  # first two fetches fail

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    await h.quiesce(guild)  # retries chained at 0.05s until success
    h.snap(guild, "two failures then success — buffer recovered while playing")
    report("S14a: failed fetches retry themselves and recover (no manual action)",
           len(st.auto_queue) == AUTO_BUFFER_TARGET and st.auto_retries == 0
           and st.auto_status == "ok",
           f"auto={len(st.auto_queue)} retries={st.auto_retries} status={st.auto_status}")
    h.cleanup(guild)

    # Part B: outage AT DRAIN — used to be permanent silence (v1 S14 bug).
    # Autoplay OFF so nothing fills while A plays; arm the outage, drain,
    # then toggle ON mid-outage. No quiesce while the retry loop spins.
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    h.set_autoplay(guild, False)
    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # no fill (off)
    h.db.fail_count["AAAAAAAAA01"] = 999  # YouTube goes down…
    await h.advance(guild)  # A ends -> drain with autoplay OFF (nothing fires)
    h.snap(guild, "drained with autoplay OFF — bot idle")

    h.set_autoplay(guild, True)  # toggle ON mid-outage -> fill fails -> retrying
    status_during_outage = None
    for _ in range(60):
        await asyncio.sleep(0.03)
        if st.auto_status == "retrying":
            status_during_outage = "retrying"
        if 999 - h.db.fail_count["AAAAAAAAA01"] >= 2:
            break  # observed the retry LADDER actually cycling (≥2 attempts)
    fails_seen = 999 - h.db.fail_count["AAAAAAAAA01"]
    print(f"    during outage: status={status_during_outage} failed_fetches={fails_seen}")

    h.db.fail_count["AAAAAAAAA01"] = 0  # YouTube recovers
    await h.quiesce(guild)  # next retry succeeds -> was_idle -> _play_next
    h.snap(guild, "YouTube recovered — bot resumed by itself")
    vc = guild.voice_client
    report("S14b: drain-time outage shows 'retrying' (visible), then SELF-RECOVERS",
           status_during_outage == "retrying" and fails_seen >= 2
           and vc.is_playing() and st.current is not None,
           f"outage_status={status_during_outage} fails={fails_seen} "
           f"resumed={vc.is_playing()} now={st.current.title if st.current else None!r}")
    h.cleanup(guild)


async def s15_mix_button(h):
    header("S15' — 'Queue the rest of this mix' button: dedupe + cap")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24, include_seed=True)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    h.snap(guild, "A playing, buffer 8 from its mix")
    await h.mix_button(guild, member, "AAAAAAAAA01")
    vids = h.auto_vids(guild)
    dupes = len(vids) - len(set(vids))
    h.snap(guild, "mix button pressed — capped, no dupes")
    report("S15a: mix button adds no duplicates (seed/queued/played filtered)",
           dupes == 0 and "AAAAAAAAA01" not in vids,
           f"dupes={dupes} seed_in_queue={'AAAAAAAAA01' in vids}")
    report("S15b: auto segment respects the hard cap",
           len(st.auto_queue) <= AUTO_MAX,
           f"auto={len(st.auto_queue)} cap={AUTO_MAX}")
    h.cleanup(guild)


async def s16_remove_readd(h):
    header("S16' — /remove then manual re-add (expected OK)")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")

    t = h.do_remove(guild, 1)
    await h.play_single(guild, member, "BBBBBBBBB02", "Song B")
    h.snap(guild, "removed B, re-added manually")
    report("S16: played_ids only blocks suggestions — manual re-adds still work",
           t is not None and t.title == "Song B" and len(st.user_queue) == 1,
           f"user={len(st.user_queue)}")
    h.cleanup(guild)


async def s17_bootstrap(h):
    header("S17' — autoplay ON while idle bootstraps from the last played track")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    h.set_autoplay(guild, False)
    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # no fill (off)
    await h.advance(guild)  # A ends -> drain with autoplay OFF
    h.snap(guild, "drained with autoplay OFF — idle")

    st.user_seed_id = None  # force the last_played fallback path
    st.user_seed_title = ""
    h.set_autoplay(guild, True)
    await h.quiesce(guild)
    h.snap(guild, "toggled ON while idle — music resumed")
    vc = guild.voice_client
    report("S17: /autoplay ON while idle now bootstraps (was a silent no-op)",
           vc.is_playing() and st.current is not None
           and len(st.auto_queue) > 0,
           f"playing={vc.is_playing()} now={st.current.title if st.current else None!r} "
           f"auto={len(st.auto_queue)}")
    h.cleanup(guild)


async def s18_user_flood(h):
    header("S18' — user adds are never lost/reordered under a full autoplay buffer")
    guild, member = h.fresh()
    st = h.cog.state(guild.id)
    h.db.add_video("AAAAAAAAA01", "Song A")
    h.db.add_mix("AAAAAAAAA01", n=24)

    await h.play_single(guild, member, "AAAAAAAAA01", "Song A")  # buffer fills to 8
    for i in range(5):
        await h.play_single(guild, member, f"UURRRR0{i}xx", f"User song {i}")

    titles = []
    for _ in range(6):
        await h.advance(guild)
        if st.current:
            titles.append(st.current.title)
    h.snap(guild, "6 advances with a full autoplay buffer + 5 user adds")
    expected = [f"User song {i}" for i in range(5)] + ["Mix 1 [AAAAAAAAA01]"]
    report("S18: five user adds play in FIFO order before any autoplay track; buffer stays capped",
           titles == expected and len(st.auto_queue) <= AUTO_BUFFER_TARGET,
           f"played={titles}")
    h.cleanup(guild)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def main():
    h = Harness()
    h.bot.loop = asyncio.get_running_loop()

    scenarios = [
        s1_adaptation,
        s2_playlist_steers,
        s3_clear_no_deadend,
        s4_shuffle_keeps_steering,
        s5_loop_queue_no_starvation,
        s6_loop_track_no_buildup,
        s7_skip_debounce,
        s8_dry_visible,
        s9_race_playing,
        s10_race_paused,
        s11_hot_spin,
        s12_off_midfill,
        s13_stop_midfill,
        s14_retry_recovery,
        s15_mix_button,
        s16_remove_readd,
        s17_bootstrap,
        s18_user_flood,
    ]
    for sc in scenarios:
        try:
            await sc(h)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"\n  !!! HARNESS ERROR in {sc.__name__}: {e}")
            traceback.print_exc()
            RESULTS.append((sc.__name__, False, f"harness error: {e}"))

    print("\n" + "#" * 78)
    print("SUMMARY (v1 finding → v2 status)")
    print("#" * 78)
    failed = 0
    for name, ok, detail in RESULTS:
        if not ok:
            failed += 1
        print(f"  {'✓' if ok else '✗'} {name:<58} {detail[:70] if not ok else ''}")
    print(f"\n  {len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    print(f"  constants: target={AUTO_BUFFER_TARGET} topup_at={AUTO_TOPUP_AT} cap={AUTO_MAX}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
