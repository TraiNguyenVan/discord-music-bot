import asyncio
import math
import os
import random
import time
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from .youtube import FFMPEG_BEFORE, FFMPEG_OPTIONS, get_ydl_opts


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


def _is_url(s: str) -> bool:
    s = s.strip().lower()
    return s.startswith("http") or "youtube.com/" in s or "youtu.be/" in s


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
    queue: list[Track] = field(default_factory=list)
    current: Track | None = None
    loop_mode: str = "off"  # off | track | queue
    volume: float = 0.5  # 0.0 - 2.0
    started_at: float = 0.0
    paused: bool = False
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


def _event(guild_id: int | str, msg: str):
    print(f"[event] guild={guild_id} {msg}", flush=True)


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
            st.queue.extend(tracks)
            _event(inter.guild.id, f"panel-add playlist n={len(tracks)} by={inter.user}")  # type: ignore
            if was_idle:
                await self.cog._play_next(inter.guild)  # type: ignore
            await inter.followup.send(f"📃 Added **{len(tracks)} tracks**.", ephemeral=True)
        else:
            await self.cog._queue_track(inter, tracks[0], quiet=True)
        await self.cog._update_panel(inter.guild.id)  # type: ignore


class MusicPanelView(discord.ui.View):
    """Persistent shared control panel. timeout=None + fixed custom_ids
    so buttons survive bot restarts (re-registered in setup)."""

    def __init__(self, cog: "Music"):
        super().__init__(timeout=None)
        self.cog = cog

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
        random.shuffle(st.queue)
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(f"🔀 Shuffled {len(st.queue)} tracks.", ephemeral=True)

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
        for i, t in enumerate(st.queue[:10], start=1):
            lines.append(f"`{i}.` {t.title} (`{fmt_duration(t.duration)}`) — {t.requester}")
        if len(st.queue) > 10:
            lines.append(f"…and {len(st.queue) - 10} more (use `/queue` for pages)")
        em = discord.Embed(title="📜 Queue", description="\n".join(lines) or "Queue is empty.",
                            colour=discord.Colour.blurple())
        await inter.followup.send(embed=em, ephemeral=True)

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
        st.queue.clear()
        st.current = None
        st.paused = False
        st._force_next_skip = False
        st._skip_stop = False
        st.self_disconnect = False
        st.fast_fails = 0
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

    async def _extract(self, query: str, playlist: bool = False, search_n: int = 0, flat: bool = False) -> list[dict]:
        opts = get_ydl_opts(playlist=playlist, search_n=search_n, flat=flat)
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
            # no cap: full playlist / full search results
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
        """Re-resolve a fresh stream URL (old ones expire). Falls back to cached."""
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
            if st.loop_mode == "queue" and st.current:
                st.queue.append(st.current)
            nxt = st.queue.pop(0) if st.queue else None
        st.current = nxt
        if nxt is None:
            st.started_at = 0
            await self._update_panel(guild.id)
            # schedule auto-leave on empty queue
            if self.inactivity_timeout > 0:
                if st.leave_task and not st.leave_task.done():
                    st.leave_task.cancel()
                st.leave_task = self.bot.loop.create_task(self._idle_leave(guild.id))
            return
        if st.leave_task and not st.leave_task.done():
            st.leave_task.cancel()
        url = await self._refresh_stream(nxt)
        src = self._source(url, st.volume)
        st.started_at = time.time()
        st.paused = False
        _event(guild.id, f"now-playing title={nxt.title!r} by={nxt.requester} left={len(st.queue)} loop={st.loop_mode} force_skip={force}")

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
                st.queue.insert(0, nxt)
                if st.loop_mode == "queue" and prev is not None and st.queue and st.queue[-1] is prev:
                    st.queue.pop()  # undo the rotation above
            st.current = prev
            return
        await self._update_panel(guild.id)

    async def _idle_leave(self, guild_id: int):
        await asyncio.sleep(self.inactivity_timeout)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        st = self.state(guild_id)
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and not st.queue and not st.current:
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
        em.add_field(name="Up next", value=str(len(st.queue)))
        em.add_field(name="Loop", value=st.loop_mode)
        em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
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
        st.queue.append(track)
        _event(inter.guild.id, f"queued title={track.title!r} by={track.requester} pos={len(st.queue)}")  # type: ignore
        if vc.is_playing() or vc.is_paused():
            await inter.followup.send(f"➕ Queued **{track.title}** (`{fmt_duration(track.duration)}`) — #{len(st.queue)}",
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
        Returns (kind, tracks). kind is 'playlist' | 'url' | 'search'.
        - playlist link (has list=) -> full playlist, uncapped (deep)
        - video link -> single resolve (deep)
        - text -> flat listing top-5 for a fast pick list; stream URL
          lazy-resolved only after the user picks a number
        """
        q = query.strip()
        if "list=" in q and _is_url(q):
            infos = await self._extract(q, playlist=True)
            return ("playlist", [t for t in (self._to_track(d, user) for d in infos) if t])
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
            if not st.queue:
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

    def _do_toggle(self, guild: discord.Guild) -> str:
        vc = guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self.state(guild.id).paused = True
            return "⏸ Paused."
        if vc and vc.is_paused():
            vc.resume()
            self.state(guild.id).paused = False
            return "▶ Resumed."
        return "Nothing playing."

    def _do_loop_cycle(self, guild: discord.Guild) -> str:
        st = self.state(guild.id)
        order = ["off", "track", "queue"]
        st.loop_mode = order[(order.index(st.loop_mode) + 1) % 3]
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
            em.add_field(name="Up next", value=str(len(st.queue)))
            em.add_field(name="Loop", value=st.loop_mode)
            em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
            return em
        elapsed = (time.time() - st.started_at) if st.started_at else 0
        icon = "⏸" if st.paused else "▶"
        em = discord.Embed(title="🎧 Music Panel", description=f"{icon} **{t.title}**", colour=discord.Colour.blurple())
        em.add_field(name="Progress", value=f"`{progress_bar(elapsed, t.duration)}` `{fmt_duration(t.duration)}`", inline=False)
        em.add_field(name="Requested by", value=t.requester)
        em.add_field(name="Up next", value=str(len(st.queue)))
        em.add_field(name="Loop", value=st.loop_mode)
        em.add_field(name="Volume", value=f"{int(st.volume * 100)}%")
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
            await msg.edit(embed=self._panel_embed(guild_id), view=MusicPanelView(self))
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
            st.queue.extend(tracks)
            _event(inter.guild.id, f"play-playlist n={len(tracks)} by={inter.user}")  # type: ignore
            if was_idle:
                await self._play_next(inter.guild)  # type: ignore
                await inter.followup.send(f"📃 Queued playlist: **{len(tracks)} tracks**.", embed=self._now_playing_embed(st))
            else:
                await inter.followup.send(f"📃 Added **{len(tracks)} tracks** to queue.")
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

    @app_commands.command(name="playlist", description="Queue a full YouTube playlist (no limit)")
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
            infos = await self._extract(url, playlist=True)
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id, f"playlist FAILED url={url!r} err={e}")  # type: ignore
            await inter.followup.send(f"❌ Playlist failed: `{e}`")
            return
        tracks = [t for t in (self._to_track(d, inter.user) for d in infos) if t]
        if not tracks:
            await inter.followup.send("❌ No playable entries.")
            return
        vc = await self._ensure_voice(inter)
        if vc is None:
            return
        st = self.state(inter.guild.id)  # type: ignore
        was_idle = not vc.is_playing() and not vc.is_paused() and st.current is None
        st.queue.extend(tracks)
        if was_idle:
            await self._play_next(inter.guild)  # type: ignore
            await inter.followup.send(f"📃 Queued playlist: **{len(tracks)} tracks**.", embed=self._now_playing_embed(st))
        else:
            await inter.followup.send(f"📃 Added **{len(tracks)} tracks** to queue.")

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
            vc.pause()
            self.state(inter.guild.id).paused = True  # type: ignore
            await self._update_panel(inter.guild.id)  # type: ignore
            await inter.response.send_message("⏸ Paused.")
        else:
            await inter.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc and vc.is_paused():
            vc.resume()
            self.state(inter.guild.id).paused = False  # type: ignore
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
        start = (max(page, 1) - 1) * 10
        for i, t in enumerate(st.queue[start:start + 10], start=start + 1):
            lines.append(f"`{i}.` {t.title} (`{fmt_duration(t.duration)}`) — {t.requester}")
        total_pages = max(1, math.ceil(len(st.queue) / 10)) if st.queue else 1
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
        if 1 <= index <= len(st.queue):
            t = st.queue.pop(index - 1)
            await inter.response.send_message(f"🗑 Removed **{t.title}**.")
            await self._update_panel(inter.guild.id)  # type: ignore
        else:
            await inter.response.send_message("Invalid index.", ephemeral=True)

    @app_commands.command(name="clear", description="Clear the queue")
    async def clear(self, inter: discord.Interaction):
        self.state(inter.guild.id).queue.clear()  # type: ignore
        await inter.response.send_message("🧹 Queue cleared.")
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        random.shuffle(st.queue)
        await inter.response.send_message(f"🔀 Shuffled {len(st.queue)} tracks.")
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

    @app_commands.command(name="music", description="Open the interactive music panel (no typing needed)")
    async def music(self, inter: discord.Interaction):
        if inter.guild is None:
            await inter.response.send_message("Use /music inside a server.", ephemeral=True)
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
        msg = await inter.followup.send(embed=self._panel_embed(inter.guild.id), view=MusicPanelView(self))
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
                            description="**/music** — button panel, no typing needed\n/play /search /playlist /queue /nowplaying\n/skip /pause /resume /stop\n/remove /clear /shuffle /loop /volume\n/join /leave")
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
                    if self.inactivity_timeout > 0 and not vc.is_playing() and not st.queue and not st.current:
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
        if (st.current is not None or st.queue) and before.channel is not None:
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
            st.queue.insert(0, st.current)
            st.current = None
            st.started_at = 0
            await self._play_next(guild)
            if was_paused:
                nv = guild.voice_client
                if nv is not None and nv.is_playing():
                    nv.pause()
                    st.paused = True
        await self._update_panel(guild_id)


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    bot.add_view(MusicPanelView(cog))
