import asyncio
import collections
import math
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from .youtube import FFMPEG_BEFORE, FFMPEG_OPTIONS, get_ydl_opts

# Autoplay ("Up next") engine — a small, fresh autoplay buffer instead of one
# giant stale tail. Every fill is seeded from the latest taste signal (the
# last user pick first, then the playing/last-played track), so the queue
# adapts to what the room actually listens to.
MIX_BATCH = 25              # mix listing page size (one request either way)
AUTO_BUFFER_TARGET = 8      # keep ~this many autoplay tracks buffered
AUTO_TOPUP_AT = 3           # top up when the auto segment drops below this
AUTO_MAX = 16               # hard cap on the auto segment (mix-button floods)
USER_SEED_WINDOW = 1800.0   # a user pick steers autoplay seeding for 30 minutes
AUTO_RETRY_DELAYS = (30.0, 60.0, 120.0)  # ladder after failed fetches (capped)
AUTO_DRY_RETRY_DELAY = 45.0 # a dry seed rotates to the next candidate after this
MIX_CACHE_TTL = 1800.0      # cached mix listings live 30 minutes
MIX_CACHE_MAX = 32          # seeds kept in the mix cache
PLAYED_CAP = 300            # rolling session history size (deque maxlen)


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int  # seconds, 0 if unknown/live
    thumbnail: str | None
    uploader: str
    requester: str
    requester_id: int
    needs_resolve: bool = False  # flat listing: stream_url is placeholder until lazy deep resolve
    resolved_at: float = 0.0  # epoch of last successful deep stream resolve
    from_mix: bool = False  # True if queued by autoplay; False if added by a user


def _is_url(s: str) -> bool:
    s = s.strip().lower()
    return s.startswith("http") or "youtube.com/" in s or "youtu.be/" in s


def _get_playlist_start_index(url: str) -> int:
    """Extract 1-based start index from URL if &index=N is present, default to 1."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        idx_strs = qs.get("index", [])
        if idx_strs:
            idx = int(idx_strs[0])
            return max(1, idx)
    except Exception:
        pass
    return 1


def _video_id(url: str) -> str | None:
    """Extract YouTube video ID from watch URL, short URL, or flat ID."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc:
            return parsed.path.strip("/") or None
        qs = parse_qs(parsed.query)
        v = qs.get("v", [])
        if v:
            return v[0]
    except Exception:
        pass
    return None


def _is_radio_mix(url: str) -> bool:
    """Detect YouTube radio mixes (list=RD... or start_radio=1)."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        lst = qs.get("list", [""])[0]
        return lst.startswith("RD") or "start_radio" in qs
    except Exception:
        return False


def _rank_search(query: str, datas: list[dict]) -> list[dict]:
    """Order raw ytsearch entries so the best song match comes first.

    YouTube's raw order loves 2h compilations + lives. Unless the query
    asks for mix/playlist/live, demote those; prefer title word overlap,
    sane song length (1-12 min), and official/Topic/VEVO channels.
    """
    q = query.lower()
    words = [w for w in q.split() if len(w) > 2]
    wants_long = any(k in q for k in ("mix", "playlist", "live", "hour", "2h", "lofi", "sleep", "compilation"))

    def score(d: dict) -> tuple:
        if not d:
            return (99,)
        if d.get("live_status") in ("is_live", "is_upcoming"):
            return (90,)
        dur = int(d.get("duration") or 0)
        title = (d.get("title") or "").lower()
        chan = (d.get("channel") or d.get("uploader") or "").lower()
        overlap = sum(1 for w in words if w in title)
        official = 1 if any(k in chan or k in title for k in ("official", "topic", "vevo")) else 0
        if wants_long:
            long_ok = 0 if dur >= 600 else 1
            return (0, -overlap, -official, long_ok, abs(dur - 3600))
        # song mode: penalize very long uploads hard
        long_pen = 2 if dur > 900 else (1 if dur > 720 else 0)
        dur_pen = 0 if 60 <= dur <= 720 or dur == 0 else 1
        return (long_pen, dur_pen, -overlap, -official, -dur if dur else 0)

    return sorted([d for d in datas if d], key=score)


@dataclass
class GuildState:
    # Two-segment queue: user intent always outranks the autoplay buffer.
    # Pop order is user_queue first, then auto_queue — steering is structural,
    # so playlists, shuffles and loops can no longer bury user picks.
    user_queue: list[Track] = field(default_factory=list)
    auto_queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    last_played: Track | None = None  # survives /stop: autoplay bootstraps from it when toggled ON while idle
    loop_mode: str = "off"  # off | track | queue
    volume: float = 0.5  # 0.0 - 2.0
    started_at: float = 0.0
    paused: bool = False
    paused_at: float = 0.0  # when pause began; elapsed freezes here until resume
    search_results: list[Track] = field(default_factory=list)
    leave_task: asyncio.Task | None = None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    _force_next_skip: bool = False  # explicit skip bypasses loop=track once
    _skip_stop: bool = False  # our own skip-stop is in flight: its after() is not a failure
    play_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialize _play_next: skip + track-end can fire together
    last_skip_at: float = 0.0  # time.time() of last accepted skip: double-taps inside the window are no-ops
    self_disconnect: bool = False  # True while OUR OWN disconnect() is in flight (vs external drop)
    fast_fails: int = 0  # consecutive instant stream deaths; breaker trips at 3
    last_playlist_url: str | None = None
    last_playlist_page: int = 1
    last_playlist_start_index: int = 1  # &index=N from the original URL, 1-based
    last_playlist_has_more: bool = True
    prewarm_task: asyncio.Task | None = None  # background resolve of the queue head
    autoplay: bool = True  # YouTube "Up next" radio mix — ON by default
    played_ids: collections.deque = field(default_factory=lambda: collections.deque(maxlen=PLAYED_CAP))
    mix_task: asyncio.Task | None = None  # in-flight autoplay fill
    auto_retry_task: asyncio.Task | None = None  # scheduled retry (fetch failure / dry-seed rotation)
    auto_retries: int = 0  # consecutive failed mix fetches (drives the retry ladder)
    auto_status: str = "ok"  # ok | finding | retrying | dry — surfaced on the panel
    dry_seeds: set[str] = field(default_factory=set)  # seeds whose mix yielded nothing new
    user_seed_id: str | None = None  # the latest user pick steers autoplay seeding…
    user_seed_title: str = ""        # …(kept for logs/panel)…
    user_seed_at: float = 0.0        # …while fresh (USER_SEED_WINDOW)
    mix_session_id: int = 0  # incremented on _reset_state to invalidate in-flight fetches

    @property
    def qtotal(self) -> int:
        return len(self.user_queue) + len(self.auto_queue)

    def pop_head(self) -> Track | None:
        """Next track overall: user intent first, autoplay buffer second."""
        if self.user_queue:
            return self.user_queue.pop(0)
        if self.auto_queue:
            return self.auto_queue.pop(0)
        return None

    def peek_head(self) -> Track | None:
        return self.user_queue[0] if self.user_queue else (self.auto_queue[0] if self.auto_queue else None)


def fmt_duration(sec: int) -> str:
    if not sec:
        return "LIVE"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(elapsed: float, total: int, width: int = 15) -> str:
    if not total:
        return "🔴 LIVE"
    ratio = min(max(elapsed / total, 0), 1)
    filled = int(ratio * width)
    return "▬" * filled + "🔘" + "▬" * (width - filled)


def autoplay_label(st: GuildState) -> str:
    """Human-readable autoplay state for embeds — no invisible dead ends:
    ON/OFF plus the live engine status (finding / retrying / ran dry)."""
    if not st.autoplay:
        return "OFF"
    return {
        "ok": "ON",
        "finding": "ON — finding…",
        "retrying": "ON — retrying…",
        "dry": "ON — ran dry",
    }.get(st.auto_status, "ON")


def _event(guild_id: int | str, msg: str):
    print(f"[event] guild={guild_id} {msg}", flush=True)


def now_status(st: GuildState) -> dict:
    """Sync snapshot for the web picker: pause state + frozen-while-paused
    elapsed + the actual queue list (always present, even when idle)."""
    queue = [
        {"title": t.title, "duration": t.duration, "requester": t.requester}
        for t in (*st.user_queue, *st.auto_queue)[:10]
    ]
    base: dict = {
        "playing": False,
        "queue_len": st.qtotal,
        "user_len": len(st.user_queue),
        "auto_len": len(st.auto_queue),
        "queue": queue,
        "loop": st.loop_mode,
        "volume": int(st.volume * 100),
        "autoplay": st.autoplay,
        "auto_status": st.auto_status if st.autoplay else "off",
    }
    if st.current is None or not st.started_at:
        return base
    if st.paused and st.paused_at:
        elapsed = max(0.0, st.paused_at - st.started_at)
    else:
        elapsed = max(0.0, time.time() - st.started_at)
    t = st.current
    base.update({
        "playing": True,
        "title": t.title,
        "uploader": t.uploader,
        "duration": t.duration,
        "thumbnail": t.thumbnail,
        "webpage_url": t.webpage_url,
        "requester": t.requester,
        "paused": st.paused,
        "elapsed": elapsed,
    })
    return base


async def _safe_defer(inter: discord.Interaction, ephemeral: bool = False, retries: int = 1):
    """Defer with bounded fast retry — fail-fast, never stuck.
    One retry (~1s sleep) keeps total <3s Discord window. Caller must handle
    NotFound (expired) vs network blip gracefully."""
    return await _safe_respond(inter, "defer", ephemeral=ephemeral, retries=retries)


async def _safe_respond(inter: discord.Interaction, mode: str = "defer", retries: int = 1, **kw):
    """Ack an interaction with bounded retry on transient DNS/network blips.
    mode: "defer" (kw: ephemeral) or "modal" (kw: modal=<Modal>).
    Bounded: retries<=2, delays 0.8s/1.5s so total never exceeds Discord's 3s window.
    Raises the last error if all attempts fail (caller decides). Never stuck."""
    import aiohttp as _aiohttp

    delays = [0.8, 1.5]  # attempt 0->1, 1->2
    for attempt in range(retries + 1):
        try:
            if mode == "modal":
                await inter.response.send_modal(kw["modal"])
            elif not inter.response.is_done():
                await inter.response.defer(ephemeral=kw.get("ephemeral", False))
            return
        except (discord.NotFound, discord.HTTPException):
            raise  # definitive answer from Discord (10062/400): never worth retrying
        except (_aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            if attempt >= retries:
                print(f"[net-retry] {mode} FAILED after {attempt + 1} tries: {e}", flush=True)
                raise
            delay = delays[attempt] if attempt < len(delays) else 1.0
            print(f"[net-retry] {mode} blip, retrying in {delay}s: {e}", flush=True)
            await asyncio.sleep(delay)


class SearchView(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int, tracks: list[Track], timeout: float = 1800):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.tracks = tracks[:5]
        self.birth = int(time.time())
        for i, t in enumerate(self.tracks):
            btn = discord.ui.Button(
                label=str(i + 1),
                style=discord.ButtonStyle.primary,
                custom_id=f"pick:{self.birth}:{i}",
            )

            async def _cb(inter: discord.Interaction, idx=i):
                # Button presses are new interactions: defer FIRST or Discord
                # shows "didn't respond" while yt-dlp connects + streams.
                try:
                    if not inter.response.is_done():
                        await _safe_defer(inter, ephemeral=True)
                except discord.NotFound:
                    _event(self.guild_id, f"pick-ack-expired idx={idx+1} by={inter.user}")
                    try:
                        await inter.followup.send(
                            "⚠️ That press arrived too late — press the number again.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return  # stale press or ack arrived past the 3s window
                try:
                    picked = self.tracks[idx]
                    _event(self.guild_id, f"search-pick #{idx+1} title={picked.title!r} by={inter.user}")
                    self._lock()  # disable row: press counted, no double-queues
                    if picked.needs_resolve:
                        ok = await self.cog._ensure_stream(picked)
                        if not ok:
                            await inter.followup.send(
                                "❌ That pick expired, try another number.", ephemeral=True)
                            return
                    await self.cog._queue_track(inter, picked)
                except Exception as e:  # noqa: BLE001
                    try:
                        await inter.followup.send(f"❌ Failed to queue that: `{e}`", ephemeral=True)
                    except discord.HTTPException:
                        pass

            btn.callback = _cb
            self.add_item(btn)

    def _lock(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        self._lock()
        _event(self.guild_id, "picker expired (30 min), buttons disabled")
        # best-effort: message may be gone (or never bound for ephemeral); never raise
        try:
            msg = getattr(self, "message", None)
            if msg is not None:
                em = msg.embeds[0] if msg.embeds else None
                if em is not None:
                    em.set_footer(text="⏰ Expired — run /search again for fresh buttons.")
                    await msg.edit(embed=em, view=self)
        except discord.HTTPException:
            pass


class PlaylistNextView(discord.ui.View):
    def __init__(
        self,
        cog: "Music",
        guild_id: int,
        url: str,
        current_page: int = 1,
        start_index: int = 1,
        timeout: float = 1800,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.url = url
        self.current_page = current_page
        self.start_index = start_index  # &index=N from the original URL, 1-based
        self.birth = int(time.time())

        self.btn = discord.ui.Button(
            label=f"📥 Load next 25 (page {self.current_page + 1})",
            style=discord.ButtonStyle.secondary,
            custom_id=f"plnext:{self.birth}:{self.guild_id}",
        )
        self.btn.callback = self._on_click
        self.add_item(self.btn)

    async def _on_click(self, inter: discord.Interaction):
        try:
            if not inter.response.is_done():
                await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            _event(self.guild_id, f"plnext-ack-expired by={inter.user}")
            return

        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return

        st = self.cog.state(self.guild_id)
        # Check that this guild still tracks this playlist
        next_page = self.current_page + 1
        # page 1 covers items [start_index, start_index + 24]; each later page
        # continues from there (page 2 with index=12 -> items 37-61, etc.)
        p_start = self.start_index + (next_page - 1) * 25
        p_end = p_start + 24

        _event(self.guild_id, f"plnext-fetch url={self.url!r} page={next_page} start={p_start} end={p_end} by={inter.user}")

        try:
            infos = await self.cog._extract(
                self.url,
                playlist=True,
                flat=True,
                playlist_start=p_start,
                playlist_end=p_end,
            )
        except Exception as e:  # noqa: BLE001
            _event(self.guild_id, f"plnext FAILED err={e}")
            await inter.followup.send(f"❌ Failed fetching next batch: `{e}`", ephemeral=True)
            return

        tracks = [t for t in (self.cog._to_track(d, inter.user, flat=True) for d in infos) if t]
        if not tracks:
            self.btn.disabled = True
            self.btn.label = "✅ End of playlist"
            self.btn.style = discord.ButtonStyle.secondary
            try:
                if inter.message:
                    await inter.message.edit(view=self)
            except Exception:
                pass
            await inter.followup.send("No more tracks found in playlist.", ephemeral=True)
            return

        vc = await self.cog._ensure_voice(inter)
        if vc is None:
            return

        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        self.cog._extend_user_tracks(st, tracks)  # user intent: lands ahead of the autoplay buffer
        self.cog._autoplay_check(inter.guild.id, "playlist-more")  # type: ignore
        st.last_playlist_url = self.url
        st.last_playlist_page = next_page
        self.current_page = next_page

        if len(tracks) < 25:
            self.btn.disabled = True
            self.btn.label = f"✅ End of playlist ({len(tracks)} added)"
            self.btn.style = discord.ButtonStyle.secondary
        else:
            self.btn.label = f"📥 Load next 25 (page {next_page + 1})"

        try:
            if inter.message:
                await inter.message.edit(view=self)
        except Exception:
            pass

        if was_idle:
            await self.cog._play_next(inter.guild)  # type: ignore
            await inter.followup.send(
                f"📃 Queued **{len(tracks)} more tracks** (page {next_page}).",
                embed=self.cog._now_playing_embed(st),
                ephemeral=True,
            )
        else:
            await inter.followup.send(
                f"📃 Added **{len(tracks)} more tracks** to queue (page {next_page}).",
                ephemeral=True,
            )
        await self.cog._update_panel(self.guild_id)

    def _lock(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        self._lock()
        _event(self.guild_id, "plnext expired, button disabled")
        try:
            msg = getattr(self, "message", None)
            if msg is not None:
                await msg.edit(view=self)
        except Exception:
            pass


class QueueMixView(discord.ui.View):
    """One-shot button to queue the rest of a radio mix (RD...).
    Mirrors PlaylistNextView but for the generated mix URL."""

    def __init__(self, cog: "Music", guild_id: int, mix_url: str, timeout: float = 1800):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.mix_url = mix_url
        self.birth = int(time.time())

        self.btn = discord.ui.Button(
            label="📥 Queue the rest of this mix (25)",
            style=discord.ButtonStyle.secondary,
            custom_id=f"queuemix:{self.birth}:{self.guild_id}",
        )
        self.btn.callback = self._on_click
        self.add_item(self.btn)

    async def _on_click(self, inter: discord.Interaction):
        try:
            if not inter.response.is_done():
                await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            _event(self.guild_id, f"queuemix-ack-expired by={inter.user}")
            return

        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return

        st = self.cog.state(self.guild_id)
        seed = _video_id(self.mix_url)
        _event(self.guild_id, f"queuemix-fetch seed={seed} by={inter.user}")

        try:
            entries = await self.cog._get_mix_entries(seed) if seed else []
        except Exception as e:  # noqa: BLE001
            _event(self.guild_id, f"queuemix FAILED err={e}")
            await inter.followup.send(f"❌ Failed fetching mix: `{e}`", ephemeral=True)
            return

        # shared autoplay filter: the seed itself + already-queued/played
        # tracks never re-enter the queue from a mix path (dupes fixed)
        tracks = self.cog._filter_auto_candidates(st, entries, seed)
        if not tracks:
            self.btn.disabled = True
            self.btn.label = "✅ End of mix"
            self.btn.style = discord.ButtonStyle.secondary
            try:
                if inter.message:
                    await inter.message.edit(view=self)
            except Exception:
                pass
            await inter.followup.send("No more tracks found in mix.", ephemeral=True)
            return

        vc = await self.cog._ensure_voice(inter)
        if vc is None:
            return

        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        for track in tracks:
            track.requester = "🔮 Mix"
        self.cog._extend_auto_tracks(st, tracks)
        self.cog._autoplay_check(self.guild_id, "mix-button")

        if len(tracks) < 25:
            self.btn.disabled = True
            self.btn.label = f"✅ End of mix ({len(tracks)} added)"
            self.btn.style = discord.ButtonStyle.secondary
        else:
            self.btn.label = "📥 Queue the rest of this mix (25)"

        try:
            if inter.message:
                await inter.message.edit(view=self)
        except Exception:
            pass

        if was_idle:
            await self.cog._play_next(inter.guild)  # type: ignore
            await inter.followup.send(
                f"📃 Queued **{len(tracks)} tracks** from mix.",
                embed=self.cog._now_playing_embed(st),
                ephemeral=True,
            )
        else:
            await inter.followup.send(
                f"📃 Added **{len(tracks)} tracks** from mix to queue.",
                ephemeral=True,
            )
        await self.cog._update_panel(self.guild_id)

    def _lock(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        self._lock()
        _event(self.guild_id, "queuemix expired, button disabled")
        try:
            msg = getattr(self, "message", None)
            if msg is not None:
                await msg.edit(view=self)
        except Exception:
            pass


class AddSongModal(discord.ui.Modal, title="Add music"):
    query = discord.ui.TextInput(
        label="Song, video link, or playlist link",
        placeholder="e.g. mot con vit  OR  youtube link  OR  playlist link",
        max_length=500,
    )

    def __init__(self, cog: "Music"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, inter: discord.Interaction):
        try:
            await _safe_defer(inter, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            raise
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id if inter.guild else "DM", f"panel-add submit-ack FAILED err={e}")
            return
        q = str(self.query.value).strip()
        if not q:
            await inter.followup.send("Empty — type something.", ephemeral=True)
            return
        try:
            kind, tracks = await self.cog._resolve_input(inter.user, q)
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id if inter.guild else "DM", f"panel-add FAILED query={q!r} err={e}")
            await inter.followup.send(f"❌ Could not resolve that: `{e}`", ephemeral=True)
            return
        if not tracks:
            await inter.followup.send("❌ No playable results. Try `artist - title` or a direct link.", ephemeral=True)
            return
        if kind == "search":
            # text -> pick list, never autoplay
            await self.cog._send_picker(inter, q, tracks, ephemeral=True)
            return
        vc = await self.cog._ensure_voice(inter)
        if vc is None:
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        if kind == "playlist":
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self.cog._extend_user_tracks(st, tracks)
            self.cog._autoplay_check(inter.guild.id, "panel-playlist")  # type: ignore
            st.last_playlist_url = q
            st.last_playlist_page = 1
            st.last_playlist_start_index = _get_playlist_start_index(q)
            st.last_playlist_has_more = len(tracks) >= 25
            _event(inter.guild.id, f"panel-add playlist n={len(tracks)} by={inter.user}")  # type: ignore

            view = (
                PlaylistNextView(self.cog, inter.guild.id, q, current_page=1, start_index=st.last_playlist_start_index)
                if st.last_playlist_has_more else None
            )

            if was_idle:
                await self.cog._play_next(inter.guild)  # type: ignore
            await inter.followup.send(
                f"📃 Added **{len(tracks)} tracks** to queue (items {st.last_playlist_start_index}-{st.last_playlist_start_index + len(tracks) - 1}).",
                ephemeral=True,
                view=view,
            )
        elif kind == "radio_mix_single":
            single = tracks[0]
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self.cog._add_user_track(st, single)
            self.cog._autoplay_check(inter.guild.id, "panel-mix-single")  # type: ignore
            vid = _video_id(single.webpage_url)
            mix_url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}" if vid else q
            view = QueueMixView(self.cog, inter.guild.id, mix_url)  # type: ignore
            _event(inter.guild.id, f"panel-add radio-mix-single title={single.title!r} by={inter.user}")  # type: ignore
            if was_idle:
                await self.cog._play_next(inter.guild)  # type: ignore
                await inter.followup.send(
                    f"▶ **{single.title}** — tap below to queue the rest of this mix.",
                    embed=self.cog._now_playing_embed(st),
                    view=view,
                    ephemeral=True,
                )
            else:
                await inter.followup.send(
                    f"➕ Queued **{single.title}** — tap below to queue the rest of this mix.",
                    view=view,
                    ephemeral=True,
                )
        else:
            await self.cog._queue_track(inter, tracks[0], quiet=True)
        await self.cog._update_panel(inter.guild.id)  # type: ignore


class MusicPanelView(discord.ui.View):
    """Persistent shared control panel. timeout=None + fixed custom_ids
    so buttons survive bot restarts (re-registered in setup)."""

    def __init__(self, cog: "Music", guild_id: int | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        if guild_id is not None:
            st = cog.state(guild_id)
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == "music:autoplay":
                    child.label = f"🔮 Autoplay: {'ON' if st.autoplay else 'OFF'}"
                    child.style = discord.ButtonStyle.success if st.autoplay else discord.ButtonStyle.secondary

    async def _ack(self, inter: discord.Interaction) -> bool:
        """Defer first (with retry on DNS blips); False if no ack or no voice."""
        try:
            await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            return False
        except Exception as e:  # noqa: BLE001 — DNS still down after retry
            _event(inter.guild.id if inter.guild else "DM", f"panel-ack FAILED err={e}")
            return False
        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, custom_id="music:add", row=0)
    async def add(self, inter: discord.Interaction, _btn: discord.ui.Button):
        try:
            await _safe_respond(inter, "modal", modal=AddSongModal(self.cog))
        except (discord.NotFound, discord.HTTPException):
            raise
        except Exception as e:  # noqa: BLE001 — DNS still down after retry
            _event(inter.guild.id if inter.guild else "DM", f"panel-add modal-ack FAILED err={e}")
            try:
                await inter.followup.send("⚠️ Network hiccup — tap ➕ Add again.", ephemeral=True)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="⏯ Pause", style=discord.ButtonStyle.primary, custom_id="music:toggle", row=0)
    async def toggle(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_toggle(inter.guild)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary, custom_id="music:skip", row=0)
    async def skip(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = await self.cog._do_skip(inter.guild, inter.user)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        vc = inter.guild.voice_client  # type: ignore
        self.cog._reset_state(st)
        if vc:
            await self.cog._vc_disconnect(inter.guild.id, vc)  # type: ignore
        _event(inter.guild.id, f"panel-stop by={inter.user}")  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send("⏹ Stopped.", ephemeral=True)

    @discord.ui.button(label="🔀 Shuffle", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=0)
    async def shuffle(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        n = self.cog._do_shuffle(st)
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(f"🔀 Shuffled {n} tracks.", ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_loop_cycle(inter.guild)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="🔉 Vol-", style=discord.ButtonStyle.secondary, custom_id="music:voldn", row=1)
    async def voldn(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_volume(inter.guild, -10)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="🔊 Vol+", style=discord.ButtonStyle.secondary, custom_id="music:volup", row=1)
    async def volup(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_volume(inter.guild, +10)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="📜 Queue", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=1)
    async def queue(self, inter: discord.Interaction, _btn: discord.ui.Button):
        try:
            await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        lines = []
        if st.current:
            lines.append(f"**Now:** {st.current.title} (`{fmt_duration(st.current.duration)}`)")
        tracks = [*st.user_queue, *st.auto_queue]
        for i, t in enumerate(tracks[:10], start=1):
            lines.append(f"`{i}.` {t.title} (`{fmt_duration(t.duration)}`) — {t.requester}")
        if len(tracks) > 10:
            lines.append(f"…and {len(tracks) - 10} more (use `/queue` for pages)")
        em = discord.Embed(title="📜 Queue", description="\n".join(lines) or "Queue is empty.",
                            colour=discord.Colour.blurple())
        await inter.followup.send(embed=em, ephemeral=True)

    @discord.ui.button(label="🔮 Autoplay", style=discord.ButtonStyle.secondary, custom_id="music:autoplay", row=2)
    async def autoplay(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        self.cog._set_autoplay(inter.guild.id, not st.autoplay, by=str(inter.user))  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(
            f"🔮 Autoplay is now **{'ON' if st.autoplay else 'OFF'}**.", ephemeral=True
        )

    @discord.ui.button(label="🌐 Picker", style=discord.ButtonStyle.success, custom_id="music:picker", row=2)
    async def picker(self, inter: discord.Interaction, _btn: discord.ui.Button):
        # Fresh per-tap link (tokens expire) — no voice needed to open it.
        await self.cog._send_picker_link(inter)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, custom_id="music:refresh", row=1)
    async def refresh(self, inter: discord.Interaction, _btn: discord.ui.Button):
        try:
            await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            return
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send("🔄 Panel refreshed.", ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.inactivity_timeout = int(os.getenv("INACTIVITY_TIMEOUT", "43200"))
        self.rejoin_delay = float(os.getenv("VOICE_REJOIN_DELAY", "5"))
        self.skip_debounce = float(os.getenv("SKIP_DEBOUNCE_SEC", "1.5"))
        self._search_lock = asyncio.Lock()  # global pacing: both guilds share one egress IP
        self._last_search_ts = 0.0
        self._search_min_gap = 3.0
        # Radio-mix listings cached per seed (TTL): re-seeds, dry re-checks and
        # retries after /clear reuse the listing instead of refetching it.
        self._mix_cache: dict[str, tuple[float, list[dict]]] = {}
        self.web_sessions: dict[str, dict] = {}  # token -> {guild_id,user_id,user_name,channel_id,last_heartbeat} — no fixed expiry
        self.web_runner = None  # aiohttp AppRunner for the picker sidecar
        self.web_public_url: str | None = None  # cloudflared quick-tunnel URL, when live
        self.web_heartbeat_timeout = float(os.getenv("WEB_HEARTBEAT_TIMEOUT", "90"))
        self.tunnel_shutdown_delay = float(os.getenv("TUNNEL_SHUTDOWN_DELAY", "300"))
        self.tunnel_shutdown_task: asyncio.Task | None = None
        self.tunnel_monitor_task: asyncio.Task | None = None

    async def cog_load(self):
        try:
            from web.server import start_web_server
            self.bot.loop.create_task(start_web_server(self))
            self.tunnel_monitor_task = self.bot.loop.create_task(self._monitor_tunnel_lifecycle())
        except Exception as e:  # noqa: BLE001 — picker is optional, bot works without it
            print(f"[web] picker disabled: {e}", flush=True)

    def cog_unload(self):
        if self.tunnel_monitor_task and not self.tunnel_monitor_task.done():
            self.tunnel_monitor_task.cancel()
        if self.tunnel_shutdown_task and not self.tunnel_shutdown_task.done():
            self.tunnel_shutdown_task.cancel()
        self.bot.loop.create_task(self._stop_tunnel_on_unload())

    async def _stop_tunnel_on_unload(self):
        try:
            from web.tunnel import stop_tunnel
            await stop_tunnel()
        except Exception:
            pass

    def _prune_stale_web_sessions(self) -> int:
        """Sweep sessions whose heartbeat went stale long enough that the
        idle tunnel-kill has already fired (heartbeat timeout + shutdown
        delay + buffer). With no fixed expiry this is the only way a
        session ends: links live while used, die with the tunnel."""
        default = self.web_heartbeat_timeout + self.tunnel_shutdown_delay + 60.0
        try:
            grace = float(os.getenv("WEB_SESSION_GRACE") or 0) or default
        except ValueError:
            grace = default
        now = time.time()
        stale = [tok for tok, s in self.web_sessions.items()
                 if now - s.get("last_heartbeat", 0) > grace]
        for tok in stale:
            self.web_sessions.pop(tok, None)
        return len(stale)

    def _has_active_web_sessions(self) -> bool:
        """Return whether any picker session has a recent heartbeat."""
        now = time.time()
        for sess in self.web_sessions.values():
            if now - sess.get("last_heartbeat", 0) < self.web_heartbeat_timeout:
                return True
        return False

    async def _monitor_tunnel_lifecycle(self):
        """Stop the global quick tunnel after its last browser user leaves."""
        try:
            while True:
                await asyncio.sleep(10)
                self._prune_stale_web_sessions()
                try:
                    from web.tunnel import get_public_url
                    tunnel_live = bool(get_public_url())
                except Exception:
                    tunnel_live = False
                if not tunnel_live:
                    continue
                if self._has_active_web_sessions():
                    if self.tunnel_shutdown_task and not self.tunnel_shutdown_task.done():
                        self.tunnel_shutdown_task.cancel()
                        self.tunnel_shutdown_task = None
                        print("[web] active picker session reconnected; tunnel shutdown cancelled", flush=True)
                elif self.tunnel_shutdown_task is None or self.tunnel_shutdown_task.done():
                    print(
                        f"[web] no active picker sessions; stopping tunnel in {self.tunnel_shutdown_delay:.0f}s",
                        flush=True,
                    )
                    self.tunnel_shutdown_task = self.bot.loop.create_task(self._shutdown_tunnel_after_delay())
        except asyncio.CancelledError:
            pass

    async def _shutdown_tunnel_after_delay(self):
        try:
            await asyncio.sleep(self.tunnel_shutdown_delay)
            if self._has_active_web_sessions():
                return
            from web.tunnel import stop_tunnel
            if await stop_tunnel():
                self.web_public_url = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[web] tunnel shutdown failed: {e}", flush=True)

    def _tunnel_needed(self) -> bool:
        """True when a picker link would fall back to loopback because the
        quick tunnel is down (idle-stopped or dead) and nothing overrides it.
        Config wins: never respawn when the user disabled the tunnel or set
        their own non-loopback WEB_BASE_URL."""
        explicit = (os.getenv("WEB_BASE_URL") or "").strip()
        if explicit and "127.0.0.1" not in explicit and "localhost" not in explicit:
            return False
        try:
            from web.tunnel import get_public_url, tunnel_enabled
            if not tunnel_enabled():
                return False
            return not get_public_url()
        except Exception:
            return False

    async def _ensure_public_tunnel(self, timeout: float = 12.0) -> str | None:
        """On-demand quick-tunnel (re)start. The tunnel only auto-starts at
        cog load; after an idle-shutdown or a cloudflared crash this respawns
        it so picker taps keep handing out public links. Bounded wait so the
        tap replies fast — a still-starting tunnel finishes in the background
        and the next tap picks up the URL. Never raises."""
        print("[web] on-demand tunnel restart — picker tap found tunnel down", flush=True)
        port = int(os.getenv("WEB_PORT", "8765"))
        try:
            from web.tunnel import ensure_tunnel
            url = await ensure_tunnel(port, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — tunnel is optional
            print(f"[web] on-demand tunnel start failed: {e}", flush=True)
            return None
        if url:
            self.web_public_url = url
        return url

    def web_base_url(self) -> str:
        """Base URL for picker links. Priority:
        1. explicit non-loopback WEB_BASE_URL (user override)
        2. live cloudflared quick-tunnel URL (zero-config, works anywhere)
        3. loopback fallback (host-only)."""
        port = int(os.getenv("WEB_PORT", "8765"))
        explicit = (os.getenv("WEB_BASE_URL") or "").strip().rstrip("/")
        if explicit and "127.0.0.1" not in explicit and "localhost" not in explicit:
            return explicit
        # Live tunnel state is the source of truth: the module global is
        # cleared the instant cloudflared exits, this cached copy is not.
        # Never serve a cached URL while the tunnel is down — it would be a
        # dead link. Mirror live state so the cache can never go stale.
        try:
            from web.tunnel import get_public_url
            tun = get_public_url()
        except Exception:
            tun = None
        self.web_public_url = tun
        if tun:
            return tun
        if explicit:
            return explicit
        return f"http://127.0.0.1:{port}"

    def create_web_session(self, guild_id: int, user, channel_id: int | None) -> tuple[str, str]:
        token = secrets.token_urlsafe(24)
        self.web_sessions[token] = {
            "guild_id": guild_id,
            "user_id": user.id,
            "user_name": getattr(user, "display_name", str(user)),
            "channel_id": channel_id,
            # No fixed expiry: the link lives while it's used (any /api call
            # is a heartbeat) and dies with the idle tunnel-kill.
            "last_heartbeat": time.time(),
        }
        return token, f"{self.web_base_url()}/pick?token={token}"

    async def queue_client_pick(self, guild_id: int, user_id: int, data: dict) -> tuple[bool, str]:
        """Queue a video the CLIENT already resolved in its embedded player.

        Bot does zero search/listing here — only a single deep yt-dlp resolve
        of this one videoId when it reaches the queue head (for voice).
        """
        import re as _re

        vid = str(data.get("video_id", ""))
        if not _re.match(r"^[A-Za-z0-9_-]{11}$", vid):
            return False, "Invalid videoId."
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False, "Server gone."
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return False, "User gone."
        channel = None
        try:
            vs = getattr(member, "voice", None)
            channel = vs.channel if vs else None
        except Exception:
            channel = None
        if channel is None:
            # fall back to the voice channel the user was in when opening the link
            for sess in self.web_sessions.values():
                if sess.get("guild_id") == guild_id and sess.get("user_id") == user_id and sess.get("channel_id"):
                    channel = guild.get_channel(sess["channel_id"])
                    break
        if channel is None:
            return False, "Join a voice channel first, then press Queue again."
        vc = await self._heal_voice(guild, channel)
        if vc is None:
            return False, "Could not join voice — try again."
        if vc.channel != channel:
            try:
                await vc.move_to(channel)
            except Exception:
                return False, "Could not move to your voice channel."
        title = str(data.get("title") or "Unknown title")[:200]
        uploader = str(data.get("uploader") or "?")[:200]
        try:
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        thumb = str(data.get("thumbnail") or "")[:500] or None
        page = f"https://www.youtube.com/watch?v={vid}"
        track = Track(
            title=title,
            webpage_url=page,
            stream_url=page,  # placeholder; deep-resolved on play
            duration=duration,
            thumbnail=thumb,
            uploader=uploader,
            requester=getattr(member, "display_name", str(member)),
            requester_id=user_id,
            needs_resolve=True,
        )
        st = self.state(guild_id)
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        self._add_user_track(st, track)
        self._autoplay_check(guild_id, "web-pick")
        _event(guild_id, f"web-pick title={title!r} vid={vid} by={member} idle={was_idle}")
        if was_idle:
            await self._play_next(guild)
            msg = f"▶ Started **{title}** (picked in browser)."
        else:
            msg = f"➕ Queued **{title}** (picked in browser) — #{len(st.user_queue)}."
        await self._update_panel(guild_id)
        return True, msg

    def web_now(self, guild_id: int) -> dict:
        """Sync snapshot for the picker page (pause state + timestamp)."""
        snap = now_status(self.state(guild_id))
        listeners: list[dict] = []
        try:
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild else None
            snap["connected"] = bool(vc is not None and vc.is_connected())
            snap["voice_channel"] = getattr(getattr(vc, "channel", None), "name", None)
            # who's in the room with us — humans only, bots don't listen
            ch = vc.channel if (vc is not None and vc.is_connected()) else None
            if ch is not None:
                for m in ch.members:
                    if m.bot:
                        continue
                    listeners.append({"id": m.id, "name": m.display_name,
                                      "avatar": str(m.display_avatar.url)})
        except Exception:
            snap["connected"] = False
            snap["voice_channel"] = None
        snap["listeners"] = listeners[:50]
        return snap

    async def _web_member(self, guild_id: int, user_id: int):
        """(guild, member) lookup for web actions. Never raises."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return guild, None
        return guild, member

    def _web_fallback_channel(self, guild, guild_id: int, user_id: int, member):
        """Voice channel for web actions: live member.voice first, then the
        channel stored when the picker link was opened."""
        try:
            vs = getattr(member, "voice", None)
            if vs and vs.channel:
                return vs.channel
        except Exception:
            pass
        for sess in self.web_sessions.values():
            if sess.get("guild_id") == guild_id and sess.get("user_id") == user_id and sess.get("channel_id"):
                ch = guild.get_channel(sess["channel_id"])
                if ch is not None:
                    return ch
        return None

    async def web_control(self, guild_id: int, user_id: int, action: str, params: dict | None = None) -> tuple[bool, str]:
        """Full bot control from the picker page. Same code paths as the
        Discord buttons/slash commands. params carries action args
        (level/delta/mode/index)."""
        params = params or {}
        guild, member = await self._web_member(guild_id, user_id)
        if guild is None:
            return False, "Server gone."
        st = self.state(guild_id)
        vc = guild.voice_client

        if action == "toggle":
            msg = self._do_toggle(guild)
            ok = msg != "Nothing playing."
            await self._update_panel(guild_id)
            _event(guild_id, f"web-control toggle -> {msg} by={user_id}")
            return ok, msg
        if action == "pause":
            if vc and vc.is_playing():
                self._set_paused(st, vc, True)
                await self._update_panel(guild_id)
                return True, "⏸ Paused."
            return False, "Nothing playing."
        if action == "resume":
            if vc and vc.is_paused():
                self._set_paused(st, vc, False)
                await self._update_panel(guild_id)
                return True, "▶ Resumed."
            return False, "Nothing paused."
        if action == "skip":
            if member is None:
                return False, "User gone."
            msg = await self._do_skip(guild, member)  # type: ignore
            await self._update_panel(guild_id)
            _event(guild_id, f"web-control skip -> {msg} by={member}")
            return True, msg
        if action in ("stop", "leave"):
            self._reset_state(st)
            if vc:
                await self._vc_disconnect(guild_id, vc)
            await self._update_panel(guild_id)
            _event(guild_id, f"web-control {action} by={user_id}")
            return True, "⏹ Stopped and left." if action == "leave" else "⏹ Stopped."
        if action == "clear":
            self._clear_queues(st)
            self._autoplay_check(guild_id, "web-clear")
            await self._update_panel(guild_id)
            _event(guild_id, f"web-control clear by={user_id}")
            return True, "🧹 Queue cleared."
        if action == "shuffle":
            n = self._do_shuffle(st)
            await self._update_panel(guild_id)
            return True, f"🔀 Shuffled {n} tracks."
        if action == "loop":
            msg = self._do_loop_cycle(guild)
            await self._update_panel(guild_id)
            return True, msg
        if action == "loop_set":
            mode = str(params.get("mode", "")).strip()
            if mode not in ("off", "track", "queue"):
                return False, "mode must be off/track/queue."
            st.loop_mode = mode
            await self._update_panel(guild_id)
            return True, f"🔁 Loop: **{mode}**."
        if action == "volume_set":
            try:
                level = max(0, min(200, int(params.get("level", 50))))
            except (TypeError, ValueError):
                return False, "level must be 0-200."
            st.volume = level / 100
            if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = st.volume
            await self._update_panel(guild_id)
            return True, f"🔊 Volume: **{level}%**."
        if action == "volume_delta":
            try:
                delta = int(params.get("delta", 10))
            except (TypeError, ValueError):
                return False, "delta must be a number."
            return await self.web_control(guild_id, user_id, "volume_set", {"level": int(st.volume * 100) + delta})
        if action == "autoplay":
            self._set_autoplay(guild_id, not st.autoplay, by=str(user_id))
            await self._update_panel(guild_id)
            return True, f"🔮 Autoplay is now **{'ON' if st.autoplay else 'OFF'}**."
        if action == "autoplay_set":
            mode = str(params.get("mode", "")).strip().lower()
            if mode not in ("on", "off"):
                return False, "mode must be on/off."
            self._set_autoplay(guild_id, mode == "on", by=str(user_id))
            await self._update_panel(guild_id)
            return True, f"🔮 Autoplay is now **{'ON' if st.autoplay else 'OFF'}**."
        if action == "remove":
            try:
                index = int(params.get("index", 0))
            except (TypeError, ValueError):
                return False, "index must be a number."
            t = self._remove_at(st, index)
            if t is None:
                return False, "Invalid index."
            vid = _video_id(t.webpage_url)
            if vid:
                st.played_ids.append(vid)
            self._autoplay_check(guild_id, "web-remove")
            await self._update_panel(guild_id)
            return True, f"🗑 Removed **{t.title}**."
        if action == "jump":
            # play a queued track right now: move it to the head and skip.
            # Reuses _do_skip so debounce/voice-heal/loop semantics stay identical.
            if member is None:
                return False, "User gone."
            try:
                index = int(params.get("index", 0))
            except (TypeError, ValueError):
                return False, "index must be a number."
            track = self._remove_at(st, index)
            if track is None:
                return False, "Invalid index."
            track.from_mix = False  # an explicit jump is user intent now
            st.user_queue.insert(0, track)
            self._note_user_seed(st, track)
            msg = await self._do_skip(guild, member)  # type: ignore
            await self._update_panel(guild_id)
            _event(guild_id, f"web-control jump index={index} title={track.title!r} -> {msg} by={user_id}")
            if "Already skipping" in msg:
                return True, f"⏭ **{track.title}** is up next."
            return True, f"⏭ Playing **{track.title}** now."
        if action == "join":
            if member is None:
                return False, "User gone."
            channel = self._web_fallback_channel(guild, guild_id, user_id, member)
            if channel is None:
                return False, "Join a voice channel first, then retry."
            healed = await self._heal_voice(guild, channel)
            if healed is None:
                return False, "⚠️ Could not join voice — try again."
            if healed.channel != channel:
                try:
                    await healed.move_to(channel)
                except Exception:
                    return False, "⚠️ Could not move to your voice channel."
            _event(guild_id, f"web-control join {channel.name} by={user_id}")
            return True, f"Joined {channel.name}."
        if action == "playlist_more":
            return await self._web_playlist_more(guild, guild_id, user_id, member)
        if action == "mix_more":
            return await self._web_mix_more(guild, guild_id, user_id, member)
        return False, "Unknown action."

    async def _web_playlist_more(self, guild, guild_id: int, user_id: int, member) -> tuple[bool, str]:
        """Load the next 25 of the last playlist (mirrors PlaylistNextView)."""
        st = self.state(guild_id)
        url = st.last_playlist_url
        if not url:
            return False, "No playlist in progress."
        next_page = st.last_playlist_page + 1
        p_start = st.last_playlist_start_index + (next_page - 1) * 25
        p_end = p_start + 24
        try:
            infos = await self._extract(url, playlist=True, flat=True,
                                        playlist_start=p_start, playlist_end=p_end)
        except Exception as e:  # noqa: BLE001
            return False, f"❌ Failed fetching next batch: `{e}`"
        tracks = [t for t in (self._to_track(d, member or self.bot.user, flat=True) for d in infos) if t]
        if not tracks:
            st.last_playlist_has_more = False
            return False, "No more tracks in playlist."
        channel = self._web_fallback_channel(guild, guild_id, user_id, member)
        vc = await self._heal_voice(guild, channel)
        if vc is None:
            return False, "⚠️ Could not join voice — try again."
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        self._extend_user_tracks(st, tracks)
        self._autoplay_check(guild_id, "web-playlist-more")
        st.last_playlist_page = next_page
        st.last_playlist_has_more = len(tracks) >= 25
        if was_idle:
            await self._play_next(guild)
        await self._update_panel(guild_id)
        _event(guild_id, f"web-playlist-more page={next_page} n={len(tracks)} by={user_id}")
        return True, f"📃 Added **{len(tracks)} more tracks** (page {next_page})."

    async def _web_mix_more(self, guild, guild_id: int, user_id: int, member) -> tuple[bool, str]:
        """Queue 25 more from the current track's radio mix (mirrors QueueMixView)."""
        st = self.state(guild_id)
        seed = st.current
        if seed is None:
            return False, "Nothing playing to seed a mix from."
        vid = _video_id(seed.webpage_url)
        if not vid:
            return False, "Current track has no video ID."
        try:
            entries = await self._get_mix_entries(vid)
        except Exception as e:  # noqa: BLE001
            return False, f"❌ Failed fetching mix: `{e}`"
        tracks = self._filter_auto_candidates(st, entries, vid)
        if not tracks:
            return False, "No more tracks found in mix."
        channel = self._web_fallback_channel(guild, guild_id, user_id, member)
        vc = await self._heal_voice(guild, channel)
        if vc is None:
            return False, "⚠️ Could not join voice — try again."
        for t in tracks:
            t.requester = "🔮 Mix"
        self._extend_auto_tracks(st, tracks)
        self._autoplay_check(guild_id, "web-mix-more")
        await self._update_panel(guild_id)
        _event(guild_id, f"web-mix-more n={len(tracks)} seed={vid} by={user_id}")
        return True, f"📃 Added **{len(tracks)} tracks** from mix."

    def web_queue_page(self, guild_id: int, page: int = 1, per: int = 10) -> dict:
        """Paginated queue for the web UI (mirrors /queue). Combined view:
        user segment first, then the autoplay buffer — matches pop order."""
        st = self.state(guild_id)
        page = max(1, page)
        start = (page - 1) * per
        tracks = [*st.user_queue, *st.auto_queue]
        items = [
            {"index": start + i + 1, "title": t.title, "duration": t.duration,
             "requester": t.requester, "uploader": t.uploader, "from_auto": t.from_mix}
            for i, t in enumerate(tracks[start:start + per])
        ]
        total_pages = max(1, math.ceil(len(tracks) / per)) if tracks else 1
        cur = None
        if st.current:
            cur = {"title": st.current.title, "duration": st.current.duration,
                   "requester": st.current.requester, "uploader": st.current.uploader}
        return {"current": cur, "items": items, "page": page,
                "total_pages": total_pages, "total": len(tracks)}

    async def web_play(self, guild_id: int, user_id: int, query: str) -> tuple[bool, str, dict]:
        """Add by text/URL/playlist from the web Add box. Mirrors /play's
        smart router. Search kind returns a pick list (no auto-queue);
        everything else queues directly. Returns (ok, msg, payload)."""
        guild, member = await self._web_member(guild_id, user_id)
        if guild is None:
            return False, "Server gone.", {}
        requester = member or self.bot.user
        q = (query or "").strip()
        if not q:
            return False, "Empty — type something.", {}
        try:
            kind, tracks = await self._resolve_input(requester, q)
        except Exception as e:  # noqa: BLE001
            return False, f"❌ Could not resolve that: `{e}`", {}
        if not tracks:
            return False, "❌ No playable results.", {}
        if kind == "search":
            payload = {"kind": kind, "results": [
                {"videoId": _video_id(t.webpage_url) or "",
                 "title": t.title, "uploader": t.uploader, "duration": t.duration,
                 "thumbnail": t.thumbnail or ""}
                for t in tracks if _video_id(t.webpage_url)
            ]}
            return True, f"🔎 Top {len(payload['results'])} matches — tap ➕ to queue.", payload
        st = self.state(guild_id)
        channel = self._web_fallback_channel(guild, guild_id, user_id, member)
        vc = await self._heal_voice(guild, channel)
        if vc is None:
            return False, "⚠️ Could not join voice — join one and retry.", {"kind": kind}
        if kind == "playlist":
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self._extend_user_tracks(st, tracks)
            self._autoplay_check(guild_id, "web-playlist")
            st.last_playlist_url = q
            st.last_playlist_page = 1
            st.last_playlist_start_index = _get_playlist_start_index(q)
            st.last_playlist_has_more = len(tracks) >= 25
            if was_idle:
                await self._play_next(guild)
            await self._update_panel(guild_id)
            return True, f"📃 Queued playlist: **{len(tracks)} tracks**.", {"kind": kind, "count": len(tracks)}
        if kind == "radio_mix_single":
            single = tracks[0]
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self._add_user_track(st, single)
            self._autoplay_check(guild_id, "web-mix-single")
            if was_idle:
                await self._play_next(guild)
                msg = f"▶ **{single.title}** — use Mix+ to queue the rest."
            else:
                msg = f"➕ Queued **{single.title}** — use Mix+ to queue the rest."
            await self._update_panel(guild_id)
            return True, msg, {"kind": kind}
        # single url
        track = tracks[0]
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        self._add_user_track(st, track)
        self._autoplay_check(guild_id, "web-play")
        if was_idle:
            await self._play_next(guild)
            msg = f"▶ Started **{track.title}**."
        else:
            msg = f"➕ Queued **{track.title}** — #{len(st.user_queue)}."
        await self._update_panel(guild_id)
        return True, msg, {"kind": kind}

    async def _search_gate(self):
        """Enforce ≥3s between YouTube search/listing extractions globally.
        Rapid bursts across guilds trip YouTube's per-IP hour rate-limit."""
        async with self._search_lock:
            now = time.time()
            wait = self._search_min_gap - (now - self._last_search_ts)
            if wait > 0:
                if wait > 0.5:
                    print(f"[throttle] pacing search +{wait:.1f}s", flush=True)
                await asyncio.sleep(wait)
            self._last_search_ts = time.time()

    # ---------- helpers ----------

    def state(self, guild_id: int) -> GuildState:
        return self.states.setdefault(guild_id, GuildState())

    def _reset_state(self, st: GuildState):
        """Full wipe for explicit leave/stop: queue, current, pause flag, idle timer."""
        st.user_queue.clear()
        st.auto_queue.clear()
        st.current = None
        st.paused = False
        st.paused_at = 0.0
        st._force_next_skip = False
        st._skip_stop = False
        st.self_disconnect = False
        st.fast_fails = 0
        st.last_playlist_url = None
        st.last_playlist_page = 1
        st.last_playlist_start_index = 1
        st.last_playlist_has_more = True
        st.auto_status = "ok"
        st.auto_retries = 0
        st.dry_seeds.clear()
        st.user_seed_id = None
        st.user_seed_title = ""
        st.user_seed_at = 0.0
        st.mix_session_id += 1
        st.played_ids.clear()
        # NOTE: st.last_played survives a reset on purpose — /autoplay ON
        # while idle bootstraps the next mix from it.
        if st.prewarm_task and not st.prewarm_task.done():
            st.prewarm_task.cancel()
        st.prewarm_task = None
        if st.mix_task and not st.mix_task.done():
            st.mix_task.cancel()
        st.mix_task = None
        if st.auto_retry_task and not st.auto_retry_task.done():
            st.auto_retry_task.cancel()
        st.auto_retry_task = None
        if st.leave_task and not st.leave_task.done():
            st.leave_task.cancel()
        st.leave_task = None

    async def _vc_disconnect(self, guild_id: int, vc):
        """Intentional voice disconnect: flagged so the self voice-state
        handler does NOT treat it as a drop (no auto-rejoin). Never raises."""
        st = self.state(guild_id)
        st.self_disconnect = True
        try:
            await vc.disconnect(force=True)
        except Exception:
            st.self_disconnect = False

    async def _extract(
        self,
        query: str,
        playlist: bool = False,
        search_n: int = 0,
        flat: bool = False,
        playlist_start: int = 1,
        playlist_end: int = 25,
    ) -> list[dict]:
        opts = get_ydl_opts(
            playlist=playlist,
            search_n=search_n,
            flat=flat,
            playlist_start=playlist_start,
            playlist_end=playlist_end,
        )
        loop = self.bot.loop
        # listings (search/playlist) are the hammering risk: pace them globally.
        # Single-video deep resolves (picks/plays) stay instant.
        if flat or search_n > 0 or playlist:
            await self._search_gate()
        # flat listing ignores default_search routing (falls through to
        # generic extractor), so prefix explicitly: ytsearchN:query
        eff_query = query
        if flat and search_n > 0 and not query.startswith("http"):
            eff_query = f"ytsearch{search_n}:{query}"

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                # gentle pacing: playlists fire many requests fast
                info = ydl.extract_info(eff_query, download=False)
                return info

        t0 = time.time()
        info = await loop.run_in_executor(None, _run)
        dt = time.time() - t0
        n = len(info.get("entries", [])) if isinstance(info, dict) else 1
        print(f"[extract] flat={flat} n={search_n or n} took={dt:.1f}s query={query[:60]!r}", flush=True)
        if not info:
            return []
        if "entries" in info and info["entries"]:
            # playlist capped at 25 (yt-dlp playlistend)
            return [e for e in info["entries"] if e]
        return [info]

    def _to_track(self, data: dict, requester: discord.abc.User, flat: bool = False) -> Track | None:
        vid = data.get("id")
        page = data.get("webpage_url") or data.get("original_url")
        if flat:
            # flat listing: no stream URL yet, only metadata. Lazy-resolve on pick.
            if not page and vid:
                page = f"https://www.youtube.com/watch?v={vid}"
            url = data.get("url")
            if url and not url.startswith("http") and vid:
                page = f"https://www.youtube.com/watch?v={vid}"
            if not page:
                return None
            title = data.get("title") or "Unknown title"
            thumbs = data.get("thumbnails") or []
            thumb = thumbs[-1].get("url") if thumbs else data.get("thumbnail")
            return Track(
                title=title,
                webpage_url=page,
                stream_url=page,  # placeholder until lazy deep resolve
                duration=int(data.get("duration") or 0),
                thumbnail=thumb,
                uploader=str(data.get("channel") or data.get("uploader") or "?"),
                requester=getattr(requester, "display_name", str(requester)),
                requester_id=requester.id,
                needs_resolve=True,
            )
        url = data.get("url")
        if not page:
            page = url
        title = data.get("title") or "Unknown title"
        if not url or not page:
            return None
        thumbs = data.get("thumbnails") or []
        thumb = thumbs[-1].get("url") if thumbs else data.get("thumbnail")
        return Track(
            title=title,
            webpage_url=page,
            stream_url=url,
            duration=int(data.get("duration") or 0),
            thumbnail=thumb,
            uploader=str(data.get("channel") or data.get("uploader") or "?"),
            requester=getattr(requester, "display_name", str(requester)),
            requester_id=requester.id,
        )

    async def _refresh_stream(self, track: Track) -> str:
        """Re-resolve a fresh stream URL (old ones expire). Falls back to cached.
        Flat-listing placeholders (needs_resolve) deep-resolve here on their
        first trip to the queue head."""
        if await self._ensure_stream(track):
            return track.stream_url
        if time.time() - track.resolved_at < 1800 and track.stream_url.startswith("http"):
            return track.stream_url  # just resolved (e.g. lazy pick resolve), reuse it
        try:
            infos = await self._extract(track.webpage_url)
            if infos and infos[0].get("url"):
                track.stream_url = infos[0]["url"]
                track.resolved_at = time.time()
        except Exception:
            pass
        return track.stream_url

    async def _ensure_stream(self, track: Track) -> bool:
        """Lazy deep resolve for flat-listing picks. Returns True on success."""
        if not track.needs_resolve:
            return True
        try:
            infos = await self._extract(track.webpage_url)
            if infos and infos[0].get("url"):
                track.stream_url = infos[0]["url"]
                if infos[0].get("duration"):
                    track.duration = int(infos[0]["duration"])
                track.resolved_at = time.time()
                track.needs_resolve = False
                return True
        except Exception as e:  # noqa: BLE001
            print(f"[extract] lazy resolve FAILED url={track.webpage_url!r} err={e}", flush=True)
        return False

    def _prewarm_next(self, guild_id: int):
        """Resolve queue[0] in the background while the current track plays, so
        the next advance (or a single skip) doesn't pay the 3-5s deep resolve.
        Superseded/replaced on every advance; safe to cancel."""
        st = self.state(guild_id)
        if st.prewarm_task and not st.prewarm_task.done():
            st.prewarm_task.cancel()
        st.prewarm_task = None
        nxt = st.peek_head()
        if nxt is None:
            return
        if not nxt.needs_resolve:
            return

        async def _go():
            try:
                # a skip may pop this track mid-resolve; resolve a copy-identity
                # guard keeps us from stamping a URL onto a different track
                await self._ensure_stream(nxt)
                if st.peek_head() is not nxt:
                    pass  # track moved on; result harmless (needs_resolve=False now)
            except asyncio.CancelledError:
                pass

        st.prewarm_task = self.bot.loop.create_task(_go())

    # ---------- queue mutation helpers (single choke points) ----------

    def _add_user_track(self, st: GuildState, track: Track):
        """User picks land in the user segment — always ahead of autoplay."""
        st.user_queue.append(track)
        self._note_user_seed(st, track)

    def _extend_user_tracks(self, st: GuildState, tracks: list[Track]):
        """Playlists are user intent too: they land ahead of the autoplay
        buffer instead of being buried behind it."""
        if not tracks:
            return
        st.user_queue.extend(tracks)
        self._note_user_seed(st, tracks[0])  # the playlist's opener represents the batch

    def _note_user_seed(self, st: GuildState, track: Track):
        """The latest user pick steers autoplay seeding while fresh."""
        vid = _video_id(track.webpage_url)
        if vid:
            st.user_seed_id = vid
            st.user_seed_title = track.title
            st.user_seed_at = time.time()

    def _do_shuffle(self, st: GuildState) -> int:
        """Shuffle each segment separately — user intent still pops first."""
        random.shuffle(st.user_queue)
        random.shuffle(st.auto_queue)
        return len(st.user_queue) + len(st.auto_queue)

    def _remove_at(self, st: GuildState, index: int) -> Track | None:
        """1-based index over the combined view (user segment first, then
        the autoplay buffer) — matches what /queue and the panel show."""
        if index < 1 or index > st.qtotal:
            return None
        if index <= len(st.user_queue):
            return st.user_queue.pop(index - 1)
        return st.auto_queue.pop(index - len(st.user_queue) - 1)

    def _clear_queues(self, st: GuildState):
        """Clear both segments. Cleared tracks stay 'never re-suggest'
        (played_ids) — the autoplay engine rotates to another seed instead
        of silently dying on this one."""
        for t in (*st.user_queue, *st.auto_queue):
            vid = _video_id(t.webpage_url)
            if vid:
                st.played_ids.append(vid)
        st.user_queue.clear()
        st.auto_queue.clear()

    def _set_autoplay(self, guild_id: int, on: bool, by: str | None = None):
        """Single choke point for every on/off path (command, panel, web)."""
        st = self.state(guild_id)
        st.autoplay = on
        _event(guild_id, f"autoplay set to={on}" + (f" by={by}" if by else ""))
        if on:
            # bootstrap: works even while idle — the seed falls back to the
            # last played track, so "autoplay ON" never means a silent bot.
            self._autoplay_check(guild_id, "autoplay-on")

    # ---------- autoplay engine ----------

    def _autoplay_wanted(self, st: GuildState) -> bool:
        """Loop modes own the queue — autoplay stands down while one is on."""
        return st.loop_mode == "off"

    def _pick_seed(self, st: GuildState) -> tuple[str, str] | None:
        """Seed priority: fresh user pick > currently playing > last played >
        stale user pick. Dry seeds are skipped so a poisoned seed (e.g. its
        whole mix got /clear-ed) can't dead-end the engine."""
        now = time.time()
        cands: list[tuple[str | None, str]] = []
        if st.user_seed_id and (now - st.user_seed_at) <= USER_SEED_WINDOW:
            cands.append((st.user_seed_id, st.user_seed_title or "user pick"))
        if st.current is not None:
            cands.append((_video_id(st.current.webpage_url), st.current.title))
        if st.last_played is not None:
            cands.append((_video_id(st.last_played.webpage_url), st.last_played.title))
        if st.user_seed_id and (now - st.user_seed_at) > USER_SEED_WINDOW:
            cands.append((st.user_seed_id, st.user_seed_title or "user pick"))
        tried: set[str] = set()
        for vid, title in cands:
            if not vid or vid in tried:
                continue
            tried.add(vid)
            if vid in st.dry_seeds:
                continue
            return vid, title
        return None

    def _autoplay_check(self, guild_id: int, reason: str = ""):
        """Central top-up brain — called from every queue mutation
        (advance/drain/add/remove/clear/toggle/loop). Cheap + idempotent."""
        st = self.state(guild_id)
        if not st.autoplay or not self._autoplay_wanted(st):
            return
        # Act when the buffer runs low, or when everything is idle/empty
        # (drain + bootstrap — the two moments autoplay must not miss).
        if len(st.auto_queue) >= AUTO_TOPUP_AT and (st.current is not None or st.user_queue):
            return
        if st.mix_task and not st.mix_task.done():
            return  # fill already in flight
        seed = self._pick_seed(st)
        if seed is None:
            if st.auto_status != "dry":
                _event(guild_id, f"autoplay-dry (no seed candidate) reason={reason}")
            st.auto_status = "dry"
            return
        # a fresh trigger (usually a user action) beats a pending retry timer
        if st.auto_retry_task and not st.auto_retry_task.done():
            st.auto_retry_task.cancel()
            st.auto_retry_task = None
        vid, title = seed
        _event(guild_id, f"autoplay-fill seed={vid} auto_len={len(st.auto_queue)} reason={reason}")
        st.mix_task = self.bot.loop.create_task(self._autoplay_fill(guild_id, vid, title))

    def _retry_delay(self, st: GuildState) -> float:
        i = min(max(st.auto_retries - 1, 0), len(AUTO_RETRY_DELAYS) - 1)
        return AUTO_RETRY_DELAYS[i]

    def _schedule_auto_retry(self, guild_id: int, delay: float, reason: str):
        """Schedule the next fill attempt after a failure or a dry seed. The
        task re-checks autoplay + session before acting; any fresh user
        trigger cancels it and retries immediately."""
        st = self.state(guild_id)
        if st.auto_retry_task and not st.auto_retry_task.done():
            st.auto_retry_task.cancel()
        session = st.mix_session_id

        async def _retry():
            try:
                await asyncio.sleep(delay)
                st2 = self.state(guild_id)
                if st2.mix_session_id != session or not st2.autoplay:
                    return
                st2.auto_retry_task = None
                self._autoplay_check(guild_id, f"retry({reason})")
            except asyncio.CancelledError:
                pass

        st.auto_retry_task = self.bot.loop.create_task(_retry())

    async def _get_mix_entries(self, seed_id: str) -> list[dict]:
        """Radio-mix listing for a seed, cached per seed (TTL) so re-seeding,
        dry re-checks and retries after /clear don't refetch the listing."""
        now = time.time()
        hit = self._mix_cache.get(seed_id)
        if hit and now - hit[0] < MIX_CACHE_TTL:
            return hit[1]
        infos = await self._extract(
            f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}",
            playlist=True, flat=True, playlist_start=1, playlist_end=MIX_BATCH,
        )
        entries = [e for e in infos if e]
        if len(self._mix_cache) >= MIX_CACHE_MAX:
            oldest = min(self._mix_cache, key=lambda k: self._mix_cache[k][0])
            self._mix_cache.pop(oldest, None)
        self._mix_cache[seed_id] = (now, entries)
        return entries

    def _filter_auto_candidates(self, st: GuildState, entries: list[dict], seed_id: str | None) -> list[Track]:
        """The one dedupe gate for every autoplay-path addition: drops the
        seed itself, anything already queued, anything played/skipped/removed
        this session, and intra-batch duplicates."""
        bot_user = self.bot.user
        if bot_user is None:
            return []
        seen: set[str] = set()
        for t in (*st.user_queue, *st.auto_queue):
            vid = _video_id(t.webpage_url)
            if vid:
                seen.add(vid)
        if st.current is not None:
            cur_id = _video_id(st.current.webpage_url)
            if cur_id:
                seen.add(cur_id)
        survivors: list[Track] = []
        for d in entries:
            if not d:
                continue
            vid = d.get("id") or _video_id(d.get("webpage_url") or d.get("url") or "")
            if not vid or vid == seed_id or vid in seen or vid in st.played_ids:
                continue
            track = self._to_track(d, bot_user, flat=True)
            if track:
                track.from_mix = True
                track.requester = "🔮 Autoplay"
                survivors.append(track)
                seen.add(vid)
        return survivors

    def _extend_auto_tracks(self, st: GuildState, tracks: list[Track]) -> int:
        """Append autoplay tracks and enforce the AUTO_MAX cap: drop the
        oldest buffered entries first — they are the cheapest to regenerate."""
        st.auto_queue.extend(tracks)
        dropped = 0
        while len(st.auto_queue) > AUTO_MAX:
            st.auto_queue.pop(0)
            dropped += 1
        return dropped

    async def _autoplay_fill(self, guild_id: int, seed_id: str, seed_title: str):
        """Top the auto segment up to AUTO_BUFFER_TARGET from the seed's radio
        mix, sliced to target (small buffer = the next fill reflects the
        latest taste signal instead of one giant stale tail).
        Post-fetch guards make a Stop/toggle/loop-change mid-flight harmless,
        and idleness is re-validated at insertion time so a fill can never
        hijack a paused or playing track. Background task: never blocks the
        voice-thread after-chain."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        st = self.state(guild_id)
        session_id = st.mix_session_id
        st.auto_status = "finding"
        if st.current is None and not st.user_queue and not st.auto_queue:
            await self._update_panel(guild_id)

        try:
            _event(guild_id, f"autoplay-fetch seed={seed_id} title={seed_title[:40]!r}")
            entries = await self._get_mix_entries(seed_id)
        except asyncio.CancelledError:
            st.auto_status = "ok"
            raise
        except Exception as e:  # noqa: BLE001
            st.auto_retries += 1
            delay = self._retry_delay(st)
            st.auto_status = "retrying"
            _event(guild_id, f"autoplay-fetch FAILED err={e} retries={st.auto_retries} retry_in={delay:.0f}s")
            self._schedule_auto_retry(guild_id, delay, "fetch-failed")
            await self._update_panel(guild_id)
            return

        # Guard: session reset / autoplay off / loop took over while we fetched
        if st.mix_session_id != session_id or not st.autoplay or not self._autoplay_wanted(st):
            st.auto_status = "ok"
            return

        needed = AUTO_BUFFER_TARGET - len(st.auto_queue)
        if needed <= 0:
            st.auto_status = "ok"
            return
        survivors = self._filter_auto_candidates(st, entries, seed_id)
        if not survivors:
            st.dry_seeds.add(seed_id)
            st.auto_status = "dry"
            _event(guild_id, f"autoplay-dry seed={seed_id} — rotating to the next seed candidate")
            self._schedule_auto_retry(guild_id, AUTO_DRY_RETRY_DELAY, "dry-rotate")
            await self._update_panel(guild_id)
            return
        st.auto_retries = 0
        st.auto_status = "ok"
        # R1: re-validate idleness NOW, not at schedule time. A track that
        # started or a pause that began mid-fetch means we only buffer —
        # never force playback over a live or paused player.
        vc = guild.voice_client
        was_idle = (st.current is None and not st.user_queue and not st.auto_queue
                    and (vc is None or (not vc.is_playing() and not vc.is_paused())))
        st.auto_queue.extend(survivors[:needed])
        _event(guild_id, f"autoplay-queued n={min(len(survivors), needed)} auto_len={len(st.auto_queue)} seed={seed_id}")
        await self._update_panel(guild_id)
        if was_idle:
            await self._play_next(guild)

    async def _heal_voice(self, guild: discord.Guild, channel) -> discord.VoiceClient | None:
        """Return a connected voice client, reconnecting zombies via channel.
        Returns None if there is no client and no channel to join."""
        vc = guild.voice_client
        if vc is not None and not vc.is_connected():
            # zombie from a dropped voice gateway (e.g. DNS stall): drop and reconnect
            _event(guild.id, "stale voice client, reconnecting")
            await self._vc_disconnect(guild.id, vc)
            vc = None
        if vc is None and channel is not None:
            try:
                vc = await channel.connect(self_deaf=True)
                _event(guild.id, f"joined voice={channel.name}")
                self.state(guild.id).self_disconnect = False  # fresh client: clear any stale flag
            except Exception as e:  # noqa: BLE001 — voice gateway down/DNS blip
                _event(guild.id, f"voice connect FAILED err={e}")
                return None
        return vc

    async def _ensure_voice(self, inter: discord.Interaction) -> discord.VoiceClient | None:
        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        channel = user.voice.channel
        guild = inter.guild  # type: ignore
        vc = guild.voice_client
        if vc is not None and vc.is_connected() and vc.channel == channel:
            return vc
        # permission check before (re)connect/move: a Forbidden from
        # channel.connect would otherwise surface as a "network hiccup" lie
        me = getattr(guild, "me", None)
        if me is not None:
            try:
                perms = channel.permissions_for(me)
            except Exception:
                perms = None
            if perms is not None and not (perms.connect and perms.speak):
                await inter.followup.send(
                    "⚠️ I need **Connect** + **Speak** permission in that voice channel.", ephemeral=True)
                return None
        vc = await self._heal_voice(guild, channel)
        if vc is None:
            await inter.followup.send("⚠️ Could not join voice (network hiccup) — try again in a few seconds.", ephemeral=True)
            return None
        if vc.channel != channel:
            _event(guild.id, f"moved voice {vc.channel} -> {channel.name} by={user}")
            try:
                await vc.move_to(channel)
            except discord.Forbidden:
                await inter.followup.send(
                    "⚠️ I need **Connect** + **Speak** permission in that voice channel.", ephemeral=True)
                return None
        else:
            _event(guild.id, f"joined voice={channel.name} by={user}")
        return vc

    def _source(self, stream_url: str, volume: float) -> discord.PCMVolumeTransformer:
        audio = discord.FFmpegPCMAudio(
            stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    async def _play_next(self, guild: discord.Guild):
        st = self.state(guild.id)
        async with st.play_lock:
            await self._play_next_inner(guild, st)

    async def _play_next_inner(self, guild: discord.Guild, st: GuildState):
        # self-heal: voice may have dropped between tracks (DNS stall etc.)
        vc = guild.voice_client
        # self-heal: voice may have dropped between tracks (DNS stall etc.)
        vc = await self._heal_voice(guild, vc.channel if vc is not None else None)
        if vc is None:
            return
        # loop track: replay current — unless an explicit skip asked to advance
        force = st._force_next_skip
        st._force_next_skip = False
        prev = st.current
        if not force and st.loop_mode == "track" and st.current:
            nxt = st.current
        else:
            # loop=queue rotates the finished track within ITS OWN segment:
            # user picks cycle together and never strand behind the autoplay
            # buffer. A force-skip drops the skipped track instead of
            # rotating it back in.
            if st.loop_mode == "queue" and prev is not None and not force:
                (st.auto_queue if prev.from_mix else st.user_queue).append(prev)
            nxt = st.pop_head()
        st.current = nxt
        if nxt is not None:
            st.last_played = nxt
            # Mark consumed the moment the track is handed to the player:
            # covers normal plays AND skips (both should never be re-suggested).
            vid = _video_id(nxt.webpage_url)
            if vid:
                st.played_ids.append(vid)
        if nxt is None:
            st.started_at = 0
            await self._update_panel(guild.id)
            # Autoplay: queue drained — the engine decides (and says) what's next
            self._autoplay_check(guild.id, "drain")
            # schedule auto-leave on empty queue
            if self.inactivity_timeout > 0:
                if st.leave_task and not st.leave_task.done():
                    st.leave_task.cancel()
                st.leave_task = self.bot.loop.create_task(self._idle_leave(guild.id))
            return
        # Autoplay: top the buffer up after advancing
        self._autoplay_check(guild.id, "advance")
        if st.leave_task and not st.leave_task.done():
            st.leave_task.cancel()
        url = await self._refresh_stream(nxt)
        src = self._source(url, st.volume)
        prev_started_at = st.started_at
        st.started_at = time.time()
        st.paused = False
        st.paused_at = 0.0
        self._prewarm_next(guild.id)  # resolve the queue head in background: skip stays warm
        _event(guild.id, f"now-playing title={nxt.title!r} by={nxt.requester} user={len(st.user_queue)} auto={len(st.auto_queue)} loop={st.loop_mode} force_skip={force}")

        def _after(err: Exception | None):
            elapsed = time.time() - st.started_at if st.started_at else 999.0
            # A full-length track dying seconds after start = dead stream URL,
            # not a real finish (FFmpeg input failures surface as clean ends,
            # so judge by timing, not err). Breaks loop hot-spins like the
            # IPv6-timeout spin that spawned an FFmpeg every 5s forever.
            # Our own skip-stop() also ends a track in ~0s: exempt it via the
            # flag (each stop produces exactly one after, so 1:1 holds).
            if st._skip_stop:
                st._skip_stop = False
                failed = False
            else:
                failed = err is not None or (nxt.duration > 60 and elapsed < 20)
            if failed:
                st.fast_fails += 1
                print(f"[music] stream FAILED guild={guild.id} title={nxt.title!r} "
                      f"elapsed={elapsed:.0f}s err={err} streak={st.fast_fails}", flush=True)
                if st.fast_fails >= 3:
                    print(f"[music] giving up on title={nxt.title!r}, advancing", flush=True)
                    st.fast_fails = 0
                    st._force_next_skip = True  # move on instead of replaying dead URL
            else:
                st.fast_fails = 0
                _event(guild.id, f"finished title={nxt.title!r}")
            fut = asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[music] play_next failed: {e}")

        try:
            vc.play(src, after=_after)
        except discord.ClientException as e:
            # Shouldn't happen: every advance holds play_lock. If it ever does
            # (player-teardown race, external player), NEVER stop() the other
            # side — that stop() fires its after() instantly, which schedules
            # another advance that collides again: a self-sustaining hot-spin
            # that walks the whole queue (observed: 2248 plays in ~1s on
            # loop=queue from a mere double-/skip). Wait out the teardown and
            # retry once; if it's genuinely busy, re-queue our track, restore
            # state, and stand down: the pending after-chain owns next.
            _event(guild.id, f"vc.play collision err={e}, waiting out teardown")
            await asyncio.sleep(0.5)
            if guild.voice_client is vc and not vc.is_playing() and not vc.is_paused():
                try:
                    vc.play(src, after=_after)
                    await self._update_panel(guild.id)
                    return
                except discord.ClientException as e2:  # noqa: BLE001
                    _event(guild.id, f"vc.play still busy err={e2}, standing down")
            else:
                _event(guild.id, "vc.play winner active, standing down")
            if nxt is not prev:
                (st.auto_queue if nxt.from_mix else st.user_queue).insert(0, nxt)
                # undo the loop=queue rotation of the still-current track
                rot_seg = st.auto_queue if (prev is not None and prev.from_mix) else st.user_queue
                if st.loop_mode == "queue" and not force and rot_seg and rot_seg[-1] is prev:
                    rot_seg.pop()
            st.current = prev
            st.started_at = prev_started_at  # keep the live track's progress clock honest
            return
        await self._update_panel(guild.id)

    async def _idle_leave(self, guild_id: int):
        await asyncio.sleep(self.inactivity_timeout)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        st = self.state(guild_id)
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and st.qtotal == 0 and not st.current:
            _event(guild_id, "auto-leave: idle timeout, disconnecting")
            await self._vc_disconnect(guild_id, vc)
            self._reset_state(st)
            await self._update_panel(guild_id)

    def _now_playing_embed(self, st: GuildState) -> discord.Embed:
        t = st.current
        if not t:
            return discord.Embed(title="Nothing playing", colour=discord.Colour.dark_grey())
        elapsed = (time.time() - st.started_at) if st.started_at else 0
        em = discord.Embed(title="🎶 Now playing", description=f"**{t.title}**", colour=discord.Colour.blurple())
        em.add_field(name="Duration", value=f"`{fmt_duration(t.duration)}`")
        em.add_field(name="Progress", value=f"`{progress_bar(elapsed, t.duration)}`", inline=False)
        em.add_field(name="Requested by", value=t.requester)
        em.add_field(name="Up next", value=f"🎵 {len(st.user_queue)} · 🔮 {len(st.auto_queue)}")
        em.add_field(name="Loop", value=st.loop_mode)
        em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
        em.add_field(name="Autoplay", value=autoplay_label(st))
        if t.thumbnail:
            em.set_thumbnail(url=t.thumbnail)
        if t.webpage_url:
            em.url = t.webpage_url
        return em

    async def _queue_track(self, inter: discord.Interaction, track: Track, quiet: bool = False):
        st = self.state(inter.guild.id)  # type: ignore
        vc = await self._ensure_voice(inter)
        if vc is None:
            return
        self._add_user_track(st, track)
        self._autoplay_check(inter.guild.id, "add")  # type: ignore
        _event(inter.guild.id, f"queued title={track.title!r} by={track.requester} pos={len(st.user_queue)}")  # type: ignore
        if vc.is_playing() or vc.is_paused():
            await inter.followup.send(f"➕ Queued **{track.title}** (`{fmt_duration(track.duration)}`) — #{len(st.user_queue)}",
                                      ephemeral=quiet)
        else:
            await self._play_next(inter.guild)  # type: ignore
            if quiet:
                await inter.followup.send(f"▶ Started **{track.title}**.", ephemeral=True)
            else:
                await inter.followup.send(embed=self._now_playing_embed(st))
        await self._update_panel(inter.guild.id)  # type: ignore

    async def _resolve_input(self, user: discord.abc.User, query: str) -> tuple[str, list[Track]]:
        """Smart router shared by /play and the panel Add box.
        Returns (kind, tracks). kind is 'playlist' | 'url' | 'search' | 'radio_mix_single'.
        - radio mix URL (list=RD... or start_radio=1) -> single video + offer to queue the mix
        - playlist link (has list=) -> flat listing of first 25 (fast reply);
          stream URLs lazy-resolved as each track reaches the queue head
        - video link -> single resolve (deep)
        - text -> flat listing top-5 for a fast pick list; stream URL
          lazy-resolved only after the user picks a number
        """
        q = query.strip()
        if _is_url(q) and ("list=" in q or "start_radio=" in q):
            if _is_radio_mix(q):
                # A radio mix URL is a single copied video plus a generated feed;
                # don't surprise the user by queueing the feed automatically.
                # User pasted a radio mix URL (copied from browser while mix was playing).
                # Play just that one video, then offer a button to queue the rest.
                vid = _video_id(q)
                if vid:
                    single_url = f"https://www.youtube.com/watch?v={vid}"
                    infos = await self._extract(single_url)
                    return ("radio_mix_single", [t for t in (self._to_track(d, user) for d in infos[:1]) if t])
            # Genuine playlist (PL..., OL..., etc.)
            start_index = _get_playlist_start_index(q)
            infos = await self._extract(
                q, playlist=True, flat=True, playlist_start=start_index, playlist_end=start_index + 24
            )
            return ("playlist", [t for t in (self._to_track(d, user, flat=True) for d in infos) if t])
        if _is_url(q):
            infos = await self._extract(q)
            return ("url", [t for t in (self._to_track(d, user) for d in infos[:1]) if t])
        infos = _rank_search(q, await self._extract(q, search_n=10, flat=True))
        return ("search", [t for t in (self._to_track(d, user, flat=True) for d in infos[:5]) if t])

    async def _send_picker(self, inter: discord.Interaction, query: str, tracks: list[Track], ephemeral: bool = False):
        """Post the 1-5 pick list. Caller must have deferred already."""
        st = self.state(inter.guild.id)  # type: ignore
        st.search_results = tracks
        desc = "\n".join(
            f"**{i+1}.** {t.title} (`{fmt_duration(t.duration)}`) — {t.uploader}" for i, t in enumerate(tracks)
        )
        em = discord.Embed(title=f"🔎 Results for: {query}", description=desc, colour=discord.Colour.green())
        em.set_footer(text="Tip: 'artist - title' finds the exact song; plain words favor mixes.")
        await inter.followup.send(
            embed=em, view=SearchView(self, inter.guild.id, tracks, timeout=1800), ephemeral=ephemeral)  # type: ignore

    async def _do_skip(self, guild: discord.Guild, member: discord.Member) -> str:
        """Instant skip for anyone — no voting. Serialized on play_lock so a
        double-tap can't slip two advances past each other, and debounced so
        the second tap inside the window is a no-op instead of a bonus advance.
        Playing -> stop into next. Idle/paused/zombie with a non-empty queue
        -> start the next track. Empty queue -> 'Queue is empty.'"""
        st = self.state(guild.id)
        async with st.play_lock:
            vc = guild.voice_client
            if vc is not None and vc.is_playing():
                if time.time() - st.last_skip_at < self.skip_debounce:
                    _event(guild.id, f"skip debounced by={member}")
                    return "Already skipping — one at a time."
                st.last_skip_at = time.time()
                st._force_next_skip = True  # explicit skip advances even on loop=track
                st.fast_fails = 0  # a deliberate skip is not a stream failure
                st._skip_stop = True  # the stop below ends this track in ~0s: not a failure
                try:
                    vc.stop()
                except Exception:
                    pass
                _event(guild.id, f"skip by={member} loop={st.loop_mode}")
                return "⏭ Skipped."
            if not st.qtotal:
                if st.autoplay and st.loop_mode == "off" and st.current is None:
                    # tell the user what autoplay is doing instead of a bare
                    # "Queue is empty." — no silent dead ends.
                    self._autoplay_check(guild.id, "skip-empty")
                    if st.auto_status == "dry":
                        return "Queue is empty — 🔮 autoplay ran dry. Add any song to re-seed it."
                    if st.auto_status == "retrying":
                        return "Queue is empty — 🔮 autoplay is retrying after a YouTube hiccup…"
                    return "Queue is empty — 🔮 finding up next…"
                return "Queue is empty."
            # idle next: abandon current (paused/drained/stuck) and play the
            # queue head. Runs _play_next_inner directly — we already hold
            # play_lock, so a pending voice-thread after() serializes behind
            # us instead of colliding with us.
            if time.time() - st.last_skip_at < self.skip_debounce:
                _event(guild.id, f"skip debounced by={member}")
                return "Already skipping — one at a time."
            st.paused = False
            if vc is None or not vc.is_connected():
                user_chan = member.voice.channel if getattr(member, "voice", None) else None
                hint = user_chan or (vc.channel if vc is not None else None)
                if hint is None:
                    return "Join a voice channel to start the queue."
                vc = await self._heal_voice(guild, hint)
                if vc is None:
                    return "⚠️ Could not join voice — try again."
            st.last_skip_at = time.time()
            st._force_next_skip = True
            st.fast_fails = 0
            await self._play_next_inner(guild, st)
            _event(guild.id, f"skip idle-next by={member} loop={st.loop_mode}")
            return "⏭ Skipped — playing next."

    def _set_paused(self, st: GuildState, vc, paused: bool):
        """Flip pause state, freezing/shifting started_at so elapsed timestamps
        stay synced (panel progress bar + web picker) across pauses."""
        if paused:
            try:
                vc.pause()
            except Exception:
                pass
            st.paused = True
            st.paused_at = time.time()
        else:
            try:
                vc.resume()
            except Exception:
                pass
            if st.paused_at:
                st.started_at += time.time() - st.paused_at
                st.paused_at = 0.0
            st.paused = False

    def _do_toggle(self, guild: discord.Guild) -> str:
        vc = guild.voice_client
        st = self.state(guild.id)
        if vc and vc.is_playing():
            self._set_paused(st, vc, True)
            return "⏸ Paused."
        if vc and vc.is_paused():
            self._set_paused(st, vc, False)
            return "▶ Resumed."
        return "Nothing playing."

    def _do_loop_cycle(self, guild: discord.Guild) -> str:
        st = self.state(guild.id)
        order = ["off", "track", "queue"]
        st.loop_mode = order[(order.index(st.loop_mode) + 1) % 3]
        self._autoplay_check(guild.id, "loop-cycle")  # loop off → the buffer may need a top-up
        return f"🔁 Loop: **{st.loop_mode}**."

    def _do_volume(self, guild: discord.Guild, delta: int) -> str:
        st = self.state(guild.id)
        level = max(0, min(200, int(st.volume * 100) + delta))
        st.volume = level / 100
        vc = guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume
        return f"🔊 Volume: **{level}%**."

    def _panel_embed(self, guild_id: int) -> discord.Embed:
        st = self.state(guild_id)
        t = st.current
        if not t:
            em = discord.Embed(title="🎧 Music Panel", description="Nothing playing.\nPress **➕ Add** and drop a song name, video link, or playlist link.",
                               colour=discord.Colour.dark_grey())
            em.add_field(name="Up next", value=f"🎵 {len(st.user_queue)} · 🔮 {len(st.auto_queue)}")
            em.add_field(name="Loop", value=st.loop_mode)
            em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
            em.add_field(name="Autoplay", value=autoplay_label(st))
            if st.autoplay and st.auto_status == "finding" and not st.qtotal:
                em.description += "\n🔮 Finding up next…"
            return em
        elapsed = (time.time() - st.started_at) if st.started_at else 0
        icon = "⏸" if st.paused else "▶"
        em = discord.Embed(title="🎧 Music Panel", description=f"{icon} **{t.title}**", colour=discord.Colour.blurple())
        em.add_field(name="Progress", value=f"`{progress_bar(elapsed, t.duration)}` `{fmt_duration(t.duration)}`", inline=False)
        em.add_field(name="Requested by", value=t.requester)
        em.add_field(name="Up next", value=f"🎵 {len(st.user_queue)} · 🔮 {len(st.auto_queue)}")
        em.add_field(name="Loop", value=st.loop_mode)
        em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
        em.add_field(name="Autoplay", value=autoplay_label(st))
        if st.autoplay and st.auto_status == "finding" and not st.qtotal:
            em.description += "\n🔮 Finding up next…"
        if t.thumbnail:
            em.set_thumbnail(url=t.thumbnail)
        if t.webpage_url:
            em.url = t.webpage_url
        return em

    async def _update_panel(self, guild_id: int):
        """Re-render the shared panel message if one is bound."""
        st = self.state(guild_id)
        if not st.panel_channel_id or not st.panel_message_id:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        ch = guild.get_channel(st.panel_channel_id)
        if ch is None:
            try:
                ch = await guild.fetch_channel(st.panel_channel_id)
            except discord.HTTPException:
                st.panel_channel_id = None
                st.panel_message_id = None
                return
        try:
            msg = await ch.fetch_message(st.panel_message_id)  # type: ignore
        except discord.NotFound:
            st.panel_channel_id = None
            st.panel_message_id = None
            return
        except discord.HTTPException:
            return
        try:
            await msg.edit(embed=self._panel_embed(guild_id), view=MusicPanelView(self, guild_id=guild_id))
        except discord.HTTPException:
            pass

    # ---------- commands ----------

    @app_commands.command(name="play", description="Play a song/URL or add it to the queue")
    @app_commands.describe(query="YouTube URL or search text")
    async def play(self, inter: discord.Interaction, query: str):
        try:
            await _safe_defer(inter)
        except discord.NotFound:
            return  # interaction expired (Discord killed it after 3s) — silent, user already saw "thinking"
        except Exception as e:  # noqa: BLE001 — DNS still down after bounded retries
            _event(inter.guild.id if inter.guild else "DM", f"play defer FAILED err={e}")
            try:
                await inter.followup.send("⚠️ Discord hiccup — try again in a few seconds.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            kind, tracks = await self._resolve_input(inter.user, query)
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id, f"play FAILED query={query!r} err={e}")  # type: ignore
            await inter.followup.send(f"❌ Could not resolve that: `{e}`\nTip: update yt-dlp, or add `cookies.txt` if it says 'Sign in to confirm you're not a bot'.")
            return
        if not tracks:
            await inter.followup.send("❌ No results found. Try `artist - title` format for better matches.")
            return
        if kind == "search":
            # text -> pick list, never autoplay
            await self._send_picker(inter, query, tracks)
            return
        if kind == "playlist":
            vc = await self._ensure_voice(inter)
            if vc is None:
                return
            st = self.state(inter.guild.id)  # type: ignore
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self._extend_user_tracks(st, tracks)
            self._autoplay_check(inter.guild.id, "play-playlist")  # type: ignore
            st.last_playlist_url = query
            st.last_playlist_page = 1
            st.last_playlist_start_index = _get_playlist_start_index(query)
            st.last_playlist_has_more = len(tracks) >= 25
            _event(inter.guild.id, f"play-playlist n={len(tracks)} by={inter.user}")  # type: ignore

            view = (
                PlaylistNextView(self, inter.guild.id, query, current_page=1, start_index=st.last_playlist_start_index)
                if st.last_playlist_has_more else None
            )

            if was_idle:
                await self._play_next(inter.guild)  # type: ignore
                await inter.followup.send(
                    f"📃 Queued playlist: **{len(tracks)} tracks** (items {st.last_playlist_start_index}-{st.last_playlist_start_index + len(tracks) - 1}).",
                    embed=self._now_playing_embed(st),
                    view=view,
                )
            else:
                await inter.followup.send(
                    f"📃 Added **{len(tracks)} tracks** to queue (items {st.last_playlist_start_index}-{st.last_playlist_start_index + len(tracks) - 1}).",
                    view=view,
                )
            await self._update_panel(inter.guild.id)  # type: ignore
            return
        if kind == "radio_mix_single":
            vc = await self._ensure_voice(inter)
            if vc is None:
                return
            st = self.state(inter.guild.id)  # type: ignore
            single = tracks[0]
            was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
            self._add_user_track(st, single)
            self._autoplay_check(inter.guild.id, "play-mix-single")  # type: ignore
            vid = _video_id(single.webpage_url)
            mix_url = f"https://www.youtube.com/watch?v={vid}&list=RD{vid}" if vid else query
            view = QueueMixView(self, inter.guild.id, mix_url)  # type: ignore
            _event(inter.guild.id, f"play-radio-mix-single title={single.title!r} by={inter.user}")  # type: ignore
            if was_idle:
                await self._play_next(inter.guild)  # type: ignore
                await inter.followup.send(
                    f"▶ **{single.title}** — tap below to queue the rest of this mix.",
                    embed=self._now_playing_embed(st),
                    view=view,
                )
            else:
                await inter.followup.send(
                    f"➕ Queued **{single.title}** — tap below to queue the rest of this mix.",
                    view=view,
                )
            await self._update_panel(inter.guild.id)  # type: ignore
            return
        await self._queue_track(inter, tracks[0])

    @app_commands.command(name="search", description="Search YouTube and pick from top 5")
    @app_commands.describe(query="What to search for")
    async def search(self, inter: discord.Interaction, query: str):
        try:
            await _safe_defer(inter)
        except discord.NotFound:
            return
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id if inter.guild else "DM", f"search defer FAILED err={e}")
            try:
                await inter.followup.send("⚠️ Discord hiccup — try again in a few seconds.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            # flat listing: metadata only, fast. Stream resolves lazily on pick.
            infos = _rank_search(query, await self._extract(query, search_n=10, flat=True))
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id, f"search FAILED query={query!r} err={e}")  # type: ignore
            await inter.followup.send(f"❌ Search failed: `{e}`")
            return
        tracks = [t for t in (self._to_track(d, inter.user, flat=True) for d in infos[:5]) if t]
        if not tracks:
            await inter.followup.send("❌ No results. Try `artist - title` format.")
            return
        await self._send_picker(inter, query, tracks)

    @app_commands.command(name="playlist", description="Queue a YouTube playlist (first 25 tracks)")
    @app_commands.describe(url="Playlist URL")
    async def playlist(self, inter: discord.Interaction, url: str):
        try:
            await _safe_defer(inter)
        except discord.NotFound:
            return
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id if inter.guild else "DM", f"playlist defer FAILED err={e}")
            try:
                await inter.followup.send("⚠️ Discord hiccup — try again in a few seconds.", ephemeral=True)
            except Exception:
                pass
            return
        try:
            start_index = _get_playlist_start_index(url)
            infos = await self._extract(
                url, playlist=True, flat=True, playlist_start=start_index, playlist_end=start_index + 24
            )
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id, f"playlist FAILED url={url!r} err={e}")  # type: ignore
            await inter.followup.send(f"❌ Playlist failed: `{e}`")
            return
        tracks = [t for t in (self._to_track(d, inter.user, flat=True) for d in infos) if t]
        if not tracks:
            await inter.followup.send("❌ No playable entries.")
            return
        vc = await self._ensure_voice(inter)
        if vc is None:
            return
        st = self.state(inter.guild.id)  # type: ignore
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        self._extend_user_tracks(st, tracks)
        self._autoplay_check(inter.guild.id, "playlist-cmd")  # type: ignore
        st.last_playlist_url = url
        st.last_playlist_page = 1
        st.last_playlist_start_index = _get_playlist_start_index(url)
        st.last_playlist_has_more = len(tracks) >= 25

        view = (
            PlaylistNextView(self, inter.guild.id, url, current_page=1, start_index=st.last_playlist_start_index)
            if st.last_playlist_has_more else None
        )

        if was_idle:
            await self._play_next(inter.guild)  # type: ignore
            await inter.followup.send(
                f"📃 Queued playlist: **{len(tracks)} tracks** (items {st.last_playlist_start_index}-{st.last_playlist_start_index + len(tracks) - 1}).",
                embed=self._now_playing_embed(st),
                view=view,
            )
        else:
            await inter.followup.send(
                f"📃 Added **{len(tracks)} tracks** to queue (items {st.last_playlist_start_index}-{st.last_playlist_start_index + len(tracks) - 1}).",
                view=view,
            )

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, inter: discord.Interaction):
        if not isinstance(inter.user, discord.Member):
            await inter.response.send_message("Use /skip inside a server.", ephemeral=True)
            return
        msg = await self._do_skip(inter.guild, inter.user)  # type: ignore
        ephemeral = not msg.startswith("⏭")
        await inter.response.send_message(msg, ephemeral=ephemeral)
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc and vc.is_playing():
            self._set_paused(self.state(inter.guild.id), vc, True)  # type: ignore
            await self._update_panel(inter.guild.id)  # type: ignore
            await inter.response.send_message("⏸ Paused.")
        else:
            await inter.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc and vc.is_paused():
            self._set_paused(self.state(inter.guild.id), vc, False)  # type: ignore
            await self._update_panel(inter.guild.id)  # type: ignore
            await inter.response.send_message("▶ Resumed.")
        else:
            await inter.response.send_message("Nothing paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop, clear queue and leave")
    async def stop(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        vc = inter.guild.voice_client  # type: ignore
        self._reset_state(st)
        if vc:
            await self._vc_disconnect(inter.guild.id, vc)  # type: ignore
        _event(inter.guild.id, f"stopped by={inter.user}")  # type: ignore
        await self._update_panel(inter.guild.id)  # type: ignore
        await inter.response.send_message("⏹ Stopped and left.")

    @app_commands.command(name="queue", description="Show the queue")
    @app_commands.describe(page="Page number (10 per page)")
    async def queue(self, inter: discord.Interaction, page: int = 1):
        st = self.state(inter.guild.id)  # type: ignore
        lines = []
        if st.current:
            lines.append(f"**Now:** {st.current.title} (`{fmt_duration(st.current.duration)}`)")
        tracks = [*st.user_queue, *st.auto_queue]
        start = (max(page, 1) - 1) * 10
        for i, t in enumerate(tracks[start:start + 10], start=start + 1):
            lines.append(f"`{i}.` {t.title} (`{fmt_duration(t.duration)}`) — {t.requester}")
        total_pages = max(1, math.ceil(len(tracks) / 10)) if tracks else 1
        em = discord.Embed(
            title=f"📜 Queue (page {page}/{total_pages})",
            description="\n".join(lines) or "Queue is empty.",
            colour=discord.Colour.blurple(),
        )
        try:
            await inter.response.send_message(embed=em)
        except discord.NotFound:
            return  # expired — silent
        except Exception as e:  # noqa: BLE001 — DNS blip etc.
            _event(inter.guild.id if inter.guild else "DM", f"queue send FAILED err={e}")
            try:
                await inter.followup.send(embed=em)
            except Exception:
                pass

    @app_commands.command(name="nowplaying", description="Show current track")
    async def nowplaying(self, inter: discord.Interaction):
        await inter.response.send_message(embed=self._now_playing_embed(self.state(inter.guild.id)))  # type: ignore

    @app_commands.command(name="remove", description="Remove a track by position")
    @app_commands.describe(index="Queue position (see /queue, 1-based)")
    async def remove(self, inter: discord.Interaction, index: int):
        st = self.state(inter.guild.id)  # type: ignore
        t = self._remove_at(st, index)
        if t is None:
            await inter.response.send_message("Invalid index.", ephemeral=True)
            return
        vid = _video_id(t.webpage_url)
        if vid:
            st.played_ids.append(vid)
        self._autoplay_check(inter.guild.id, "remove")  # type: ignore
        await inter.response.send_message(f"🗑 Removed **{t.title}**.")
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="clear", description="Clear the queue")
    async def clear(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        self._clear_queues(st)
        self._autoplay_check(inter.guild.id, "clear")  # type: ignore
        await inter.response.send_message("🧹 Queue cleared.")
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        n = self._do_shuffle(st)
        await inter.response.send_message(f"🔀 Shuffled {n} tracks.")
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="loop", description="Set loop mode")
    @app_commands.describe(mode="off, track, queue (empty = cycle)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="track", value="track"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    async def loop(self, inter: discord.Interaction, mode: str | None = None):
        st = self.state(inter.guild.id)  # type: ignore
        if mode is None:
            msg = self._do_loop_cycle(inter.guild)  # type: ignore
        else:
            st.loop_mode = mode
            msg = f"🔁 Loop: **{st.loop_mode}**."
            self._autoplay_check(inter.guild.id, "loop-set")  # type: ignore
        await inter.response.send_message(msg)
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="volume", description="Set volume 0-200")
    @app_commands.describe(level="0 to 200")
    async def volume(self, inter: discord.Interaction, level: int):
        level = max(0, min(200, level))
        st = self.state(inter.guild.id)  # type: ignore
        st.volume = level / 100
        vc = inter.guild.voice_client  # type: ignore
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = st.volume
        await inter.response.send_message(f"🔊 Volume: **{level}%**.")
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="autoplay", description="Toggle or set YouTube Up Next autoplay")
    @app_commands.describe(mode="on or off (leave empty to toggle)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def autoplay(self, inter: discord.Interaction, mode: str | None = None):
        st = self.state(inter.guild.id)  # type: ignore
        self._set_autoplay(inter.guild.id, (not st.autoplay) if mode is None else mode == "on", by=str(inter.user))  # type: ignore
        await self._update_panel(inter.guild.id)  # type: ignore
        await inter.response.send_message(f"🔮 Autoplay is now **{'ON' if st.autoplay else 'OFF'}**.")

    async def _send_picker_link(self, inter: discord.Interaction):
        """Ephemeral fresh picker link (per-tap token). Shared by the panel's
        🌐 Picker button and /music mode:web. No voice required to open the
        link — only to press Queue inside the page."""
        try:
            await _safe_defer(inter, ephemeral=True)
        except discord.NotFound:
            return
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id if inter.guild else "DM", f"picker-link ack FAILED err={e}")
            return
        if inter.guild is None:
            await inter.followup.send("Use this inside a server.", ephemeral=True)
            return
        user = inter.user
        channel_id = None
        if isinstance(user, discord.Member) and user.voice and user.voice.channel:
            channel_id = user.voice.channel.id
        # On-demand tunnel: after an idle-shutdown or a cloudflared crash the
        # tap itself respawns the tunnel, so this link — or at worst the next
        # one — is public again. Bounded wait, already deferred above.
        if self._tunnel_needed():
            await self._ensure_public_tunnel()
        token, url = self.create_web_session(inter.guild.id, user, channel_id)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="🌐 Open YouTube picker", url=url))
        base = self.web_base_url()
        if "trycloudflare.com" in base:
            note = "\n☁️ Public link via Cloudflare — works on any network, no setup needed."
        elif "127.0.0.1" in base or "localhost" in base:
            note = "\n⚠️ Public tunnel still starting or unavailable — this link only works on this machine. Tap 🌐 Picker again in ~10s, or set `WEB_BASE_URL`."
        else:
            note = ""
        if channel_id is None:
            note += "\n Join a voice channel before pressing **Queue on bot**."
        await inter.followup.send(
            f"🌐 Pick songs in your browser (search + real YouTube player — the link stays live while you use it):\n{url}"
            f"\nSearch, tap to preview, **➕ Queue on bot** — search load stays off the bot.{note}",
            view=view,
            ephemeral=True,
        )
        _event(inter.guild.id, f"web-picker link by={user}")

    @app_commands.command(name="music", description="Open the interactive music panel (no typing needed)")
    @app_commands.describe(mode="panel: classic buttons · web: browser YouTube picker")
    @app_commands.choices(mode=[
        app_commands.Choice(name="panel", value="panel"),
        app_commands.Choice(name="web", value="web"),
    ])
    async def music(self, inter: discord.Interaction, mode: str = "panel"):
        if inter.guild is None:
            await inter.response.send_message("Use /music inside a server.", ephemeral=True)
            return
        if mode == "web":
            # Same picker link as the panel's 🌐 Picker button.
            await self._send_picker_link(inter)
            return
        await inter.response.defer()
        st = self.state(inter.guild.id)
        # remove previous panel so there is exactly one per server
        if st.panel_channel_id and st.panel_message_id:
            try:
                old_ch = inter.guild.get_channel(st.panel_channel_id)
                if old_ch is None:
                    old_ch = await inter.guild.fetch_channel(st.panel_channel_id)
                old_msg = await old_ch.fetch_message(st.panel_message_id)  # type: ignore
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
            st.panel_channel_id = None
            st.panel_message_id = None
        msg = await inter.followup.send(embed=self._panel_embed(inter.guild.id), view=MusicPanelView(self, guild_id=inter.guild.id))
        st.panel_channel_id = inter.channel.id  # type: ignore
        st.panel_message_id = msg.id
        _event(inter.guild.id, f"panel opened by={inter.user}")

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, inter: discord.Interaction):
        await inter.response.defer()
        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return
        vc = await self._ensure_voice(inter)
        if vc:
            await inter.followup.send(f"Joined {user.voice.channel.name}.")
        # on failure _ensure_voice already told the user; stay silent here (no double message)

    @app_commands.command(name="leave", description="Leave voice and clear the queue")
    async def leave(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        vc = inter.guild.voice_client  # type: ignore
        self._reset_state(st)
        if vc:
            await self._vc_disconnect(inter.guild.id, vc)  # type: ignore
        _event(inter.guild.id, f"left voice by={inter.user}")  # type: ignore
        await self._update_panel(inter.guild.id)  # type: ignore
        await inter.response.send_message("Left and cleared the queue.")

    @app_commands.command(name="help", description="List commands")
    async def help(self, inter: discord.Interaction):
        em = discord.Embed(title="🎧 Music Bot", colour=discord.Colour.blurple(),
                            description="**/music** — button panel (🌐 Picker = browser YouTube search)\n/play /search /playlist /queue /nowplaying\n/skip /pause /resume /stop\n/remove /clear /shuffle /loop /volume\n/join /leave")
        em.set_footer(text="Tip: 'Sign in to confirm you're not a bot' → add cookies.txt and rebuild.")
        await inter.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="about", description="About this bot")
    async def about(self, inter: discord.Interaction):
        await inter.response.send_message("discord.py + yt-dlp music bot. Audio: YouTube via yt-dlp → FFmpeg → Discord voice.", ephemeral=True)

    # ---------- events ----------

    # NOTE: no empty-channel auto-leave — the bot stays in voice until
    # /leave, /stop, or panel ⏹ Stop. Only the 12h idle safety net
    # (_idle_leave, armed when the queue is drained) disconnects it.
    # A human (re)joining the bot's channel resets that clock.

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        me = self.bot.user.id if self.bot.user else None
        if me is not None and member.id == me:
            await self._self_voice_update(before, after)
            return
        if member.bot:
            return
        for vc in self.bot.voice_clients:
            if after.channel and vc.channel == after.channel and before.channel != after.channel:
                st = self.state(vc.guild.id)
                if st.leave_task and not st.leave_task.done():
                    st.leave_task.cancel()
                    st.leave_task = None
                    if self.inactivity_timeout > 0 and not vc.is_playing() and st.qtotal == 0 and not st.current:
                        st.leave_task = self.bot.loop.create_task(self._idle_leave(vc.guild.id))
                        _event(vc.guild.id, f"idle timer reset by rejoin by={member}")

    async def _self_voice_update(self, before: discord.VoiceState, after: discord.VoiceState):
        """Our OWN voice state changed. A None after.channel with no
        self_disconnect flag means Discord dropped us mid-session
        (signaling may even reconnect, but the UDP audio path is dead):
        do a full fresh rejoin + restart the current track."""
        chan = after.channel or before.channel
        guild = getattr(chan, "guild", None)
        if guild is None:
            return
        st = self.state(guild.id)
        if after.channel is not None:
            if before.channel is None:
                _event(guild.id, f"bot joined voice {after.channel.name} (self)")
            elif before.channel != after.channel:
                _event(guild.id, f"bot moved voice {before.channel.name} -> {after.channel.name}")
            return
        if st.self_disconnect:
            st.self_disconnect = False
            _event(guild.id, "bot left voice (intentional, no rejoin)")
            await self._update_panel(guild.id)
            return
        if (st.current is not None or st.qtotal > 0) and before.channel is not None:
            _event(guild.id, "bot dropped from voice during playback, rejoining")
            self.bot.loop.create_task(self._rejoin_after_drop(guild.id, before.channel.id))
        else:
            _event(guild.id, "bot left voice while idle (no auto-rejoin)")
        await self._update_panel(guild.id)

    async def _rejoin_after_drop(self, guild_id: int, channel_id: int):
        await asyncio.sleep(self.rejoin_delay)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            _event(guild_id, "rejoin skipped: already connected")
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                _event(guild_id, "rejoin FAILED: channel gone")
                return
        try:
            await channel.connect(self_deaf=True)
        except Exception as e:  # noqa: BLE001
            _event(guild_id, f"rejoin FAILED err={e}")
            await self._update_panel(guild_id)
            return
        _event(guild_id, f"rejoined voice={channel.name} after drop")
        st = self.state(guild_id)
        st.self_disconnect = False
        was_paused = st.paused
        if st.current is not None:
            # old player object is bound to the dead socket: restart current track
            # at the head of its own segment, so play order is preserved
            (st.auto_queue if st.current.from_mix else st.user_queue).insert(0, st.current)
            st.current = None
            st.started_at = 0
            await self._play_next(guild)
            if was_paused:
                nv = guild.voice_client
                if nv is not None and nv.is_playing():
                    nv.pause()
                    st.paused = True
                    st.paused_at = time.time()
        await self._update_panel(guild_id)


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    bot.add_view(MusicPanelView(cog))
