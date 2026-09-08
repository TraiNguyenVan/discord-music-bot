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
    votes: set[int] = field(default_factory=set)
    search_results: list[Track] = field(default_factory=list)
    leave_task: asyncio.Task | None = None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None


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
    """Defer with one retry on transient network/DNS failures.
    Raises the last error if all attempts fail (caller decides)."""
    import aiohttp as _aiohttp

    delay = 2.0
    for attempt in range(retries + 1):
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=ephemeral)
            return
        except (discord.NotFound, discord.HTTPException):
            raise  # definitive answer from Discord: never worth retrying
        except (_aiohttp.ClientError, OSError) as e:
            if attempt >= retries:
                print(f"[net-retry] defer FAILED after {attempt + 1} tries: {e}", flush=True)
                raise
            print(f"[net-retry] defer blip, retrying in {delay}s: {e}", flush=True)
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
        # best-effort: message may be gone; never raise from timeout
        try:
            if self.message is not None:
                em = self.message.embeds[0] if self.message.embeds else None
                if em is not None:
                    em.set_footer(text="⏰ Expired — run /search again for fresh buttons.")
                    await self.message.edit(embed=em, view=self)
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
        await inter.response.defer(ephemeral=True)
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
        """Defer first; returns False if user has no voice to control with."""
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=True)
        except discord.NotFound:
            return False
        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, custom_id="music:add", row=0)
    async def add(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await inter.response.send_modal(AddSongModal(self.cog))

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.primary, custom_id="music:toggle", row=0)
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
        msg = self.cog._do_skip(inter.guild, inter.user)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.danger, custom_id="music:stop", row=0)
    async def stop(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        vc = inter.guild.voice_client  # type: ignore
        st.queue.clear()
        st.current = None
        st.votes.clear()
        if vc:
            await vc.disconnect(force=True)
        _event(inter.guild.id, f"panel-stop by={inter.user}")  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send("⏹ Stopped.", ephemeral=True)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle", row=0)
    async def shuffle(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        st = self.cog.state(inter.guild.id)  # type: ignore
        random.shuffle(st.queue)
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(f"🔀 Shuffled {len(st.queue)} tracks.", ephemeral=True)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop", row=1)
    async def loop(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_loop_cycle(inter.guild)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="🔉", style=discord.ButtonStyle.secondary, custom_id="music:voldn", row=1)
    async def voldn(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_volume(inter.guild, -10)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="🔊", style=discord.ButtonStyle.secondary, custom_id="music:volup", row=1)
    async def volup(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._ack(inter):
            return
        msg = self.cog._do_volume(inter.guild, +10)  # type: ignore
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="📜 Queue", style=discord.ButtonStyle.secondary, custom_id="music:queue", row=1)
    async def queue(self, inter: discord.Interaction, _btn: discord.ui.Button):
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=True)
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

    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, custom_id="music:refresh", row=1)
    async def refresh(self, inter: discord.Interaction, _btn: discord.ui.Button):
        try:
            if not inter.response.is_done():
                await inter.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        await self.cog._update_panel(inter.guild.id)  # type: ignore
        await inter.followup.send("🔄 Panel refreshed.", ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.inactivity_timeout = int(os.getenv("INACTIVITY_TIMEOUT", "180"))
        self.empty_grace = int(os.getenv("EMPTY_CHANNEL_GRACE", "30"))
        self.vote_ratio = float(os.getenv("SKIP_VOTE_RATIO", "0.5"))
        self.admin_skip = os.getenv("ADMIN_INSTANT_SKIP", "true").lower() == "true"
        self.requester_skip = os.getenv("REQUESTER_INSTANT_SKIP", "true").lower() == "true"

    # ---------- helpers ----------

    def state(self, guild_id: int) -> GuildState:
        return self.states.setdefault(guild_id, GuildState())

    async def _extract(self, query: str, playlist: bool = False, search_n: int = 0) -> list[dict]:
        opts = get_ydl_opts(playlist=playlist, search_n=search_n)
        loop = self.bot.loop

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                # gentle pacing: playlists fire many requests fast
                info = ydl.extract_info(query, download=False)
                return info

        info = await loop.run_in_executor(None, _run)
        if not info:
            return []
        if "entries" in info and info["entries"]:
            # no cap: full playlist / full search results
            return [e for e in info["entries"] if e]
        return [info]

    def _to_track(self, data: dict, requester: discord.abc.User) -> Track | None:
        url = data.get("url")
        page = data.get("webpage_url") or data.get("original_url") or url
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
        try:
            infos = await self._extract(track.webpage_url)
            if infos and infos[0].get("url"):
                track.stream_url = infos[0]["url"]
        except Exception:
            pass
        return track.stream_url

    async def _ensure_voice(self, inter: discord.Interaction) -> discord.VoiceClient | None:
        user = inter.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await inter.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        channel = user.voice.channel
        vc = inter.guild.voice_client  # type: ignore
        if vc is None:
            vc = await channel.connect(self_deaf=True)
            _event(inter.guild.id, f"joined voice={channel.name} by={user}")  # type: ignore
        elif vc.channel != channel:
            _event(inter.guild.id, f"moved voice {vc.channel} -> {channel.name} by={user}")  # type: ignore
            await vc.move_to(channel)
        return vc

    def _source(self, stream_url: str, volume: float) -> discord.PCMVolumeTransformer:
        audio = discord.FFmpegPCMAudio(
            stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    async def _play_next(self, guild: discord.Guild):
        st = self.state(guild.id)
        vc = guild.voice_client
        if vc is None:
            return
        # loop track: replay current
        if st.loop_mode == "track" and st.current:
            nxt = st.current
        else:
            if st.loop_mode == "queue" and st.current:
                st.queue.append(st.current)
            nxt = st.queue.pop(0) if st.queue else None
        st.current = nxt
        st.votes.clear()
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
        _event(guild.id, f"now-playing title={nxt.title!r} by={nxt.requester} left={len(st.queue)} loop={st.loop_mode}")

        def _after(err: Exception | None):
            if err:
                print(f"[music] player error guild={guild.id} title={nxt.title!r}: {err}", flush=True)
            else:
                _event(guild.id, f"finished title={nxt.title!r}")
            fut = asyncio.run_coroutine_threadsafe(self._play_next(guild), self.bot.loop)
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[music] play_next failed: {e}")

        vc.play(src, after=_after)
        await self._update_panel(guild.id)

    async def _idle_leave(self, guild_id: int):
        await asyncio.sleep(self.inactivity_timeout)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        st = self.state(guild_id)
        vc = guild.voice_client
        if vc and not vc.is_playing() and not st.queue and not st.current:
            _event(guild_id, "auto-leave: idle timeout, disconnecting")
            await vc.disconnect(force=True)
            st.current = None
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
        - playlist link (has list=) -> full playlist, uncapped
        - video link -> single resolve
        - text -> ranked top-5 candidates for a pick list (over-fetched:
          most entries are gated on datacenter IPs)
        """
        q = query.strip()
        if "list=" in q and _is_url(q):
            infos = await self._extract(q, playlist=True)
            return ("playlist", [t for t in (self._to_track(d, user) for d in infos) if t])
        if _is_url(q):
            infos = await self._extract(q)
            return ("url", [t for t in (self._to_track(d, user) for d in infos[:1]) if t])
        infos = _rank_search(q, await self._extract(q, search_n=25))
        return ("search", [t for t in (self._to_track(d, user) for d in infos[:5]) if t])

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

    def _do_skip(self, guild: discord.Guild, member: discord.Member) -> str:
        st = self.state(guild.id)
        vc = guild.voice_client
        if not vc or not vc.is_playing():
            return "Nothing playing."
        is_admin = member.guild_permissions.administrator or member.guild_permissions.manage_guild
        is_requester = st.current and st.current.requester_id == member.id
        if (is_admin and self.admin_skip) or (is_requester and self.requester_skip):
            vc.stop()
            _event(guild.id, f"skip instant by={member}")
            return "⏭ Skipped."
        st.votes.add(member.id)
        ch = vc.channel
        listeners = [m for m in ch.members if not m.bot] if ch else []
        need = max(1, math.ceil(len(listeners) * self.vote_ratio))
        if len(st.votes) >= need:
            vc.stop()
            _event(guild.id, f"skip vote-passed {len(st.votes)}/{need}")
            return f"⏭ Vote-skip passed ({len(st.votes)}/{need})."
        return f"🗳 Vote added ({len(st.votes)}/{need})."

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
        await inter.response.defer()
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
        await inter.response.defer()
        try:
            # over-fetch: most entries are IP-gated dead weight, filter to playable
            infos = _rank_search(query, await self._extract(query, search_n=25))
        except Exception as e:  # noqa: BLE001
            _event(inter.guild.id, f"search FAILED query={query!r} err={e}")  # type: ignore
            await inter.followup.send(f"❌ Search failed: `{e}`")
            return
        tracks = [t for t in (self._to_track(d, inter.user) for d in infos[:5]) if t]
        if not tracks:
            await inter.followup.send("❌ No results. Try `artist - title` format.")
            return
        await self._send_picker(inter, query, tracks)

    @app_commands.command(name="playlist", description="Queue a full YouTube playlist (no limit)")
    @app_commands.describe(url="Playlist URL")
    async def playlist(self, inter: discord.Interaction, url: str):
        await inter.response.defer()
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

    @app_commands.command(name="skip", description="Skip the current track (vote)")
    async def skip(self, inter: discord.Interaction):
        assert isinstance(inter.user, discord.Member)
        msg = self._do_skip(inter.guild, inter.user)  # type: ignore
        ephemeral = msg in ("Nothing playing.",) or msg.startswith("🗳")
        await inter.response.send_message(msg, ephemeral=ephemeral)
        await self._update_panel(inter.guild.id)  # type: ignore

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc and vc.is_playing():
            vc.pause()
            self.state(inter.guild.id).paused = True  # type: ignore
            await inter.response.send_message("⏸ Paused.")
        else:
            await inter.response.send_message("Nothing playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc and vc.is_paused():
            vc.resume()
            self.state(inter.guild.id).paused = False  # type: ignore
            await inter.response.send_message("▶ Resumed.")
        else:
            await inter.response.send_message("Nothing paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop, clear queue and leave")
    async def stop(self, inter: discord.Interaction):
        st = self.state(inter.guild.id)  # type: ignore
        vc = inter.guild.voice_client  # type: ignore
        st.queue.clear()
        st.current = None
        st.votes.clear()
        if vc:
            await vc.disconnect(force=True)
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
        await inter.response.send_message(embed=em)

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
        vc = await self._ensure_voice(inter)
        await inter.followup.send("Joined." if vc else "Failed to join.")

    @app_commands.command(name="leave", description="Leave voice")
    async def leave(self, inter: discord.Interaction):
        vc = inter.guild.voice_client  # type: ignore
        if vc:
            await vc.disconnect(force=True)
        self.state(inter.guild.id).current = None  # type: ignore
        _event(inter.guild.id, f"left voice by={inter.user}")  # type: ignore
        await self._update_panel(inter.guild.id)  # type: ignore
        await inter.response.send_message("Left.")

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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        for vc in self.bot.voice_clients:
            if before.channel and vc.channel == before.channel and len([m for m in vc.channel.members if not m.bot]) == 0:
                if self.empty_grace <= 0:
                    _event(vc.guild.id, "auto-leave: channel emptied, disconnecting")
                    await vc.disconnect(force=True)
                    self.state(vc.guild.id).current = None
                    await self._update_panel(vc.guild.id)
                else:
                    await asyncio.sleep(self.empty_grace)
                    # re-check after grace
                    if len([m for m in vc.channel.members if not m.bot]) == 0 and vc.is_connected():
                        _event(vc.guild.id, "auto-leave: channel empty after grace, disconnecting")
                        await vc.disconnect(force=True)
                        self.state(vc.guild.id).current = None
                        await self._update_panel(vc.guild.id)


async def setup(bot: commands.Bot):
    cog = Music(bot)
    await bot.add_cog(cog)
    bot.add_view(MusicPanelView(cog))
