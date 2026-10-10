"""Sidecar web UI: picker + player control panel.

The browser frontend (frontend/, React + TypeScript) is built to
frontend/dist and served here at /pick; it talks to the /api/* routes
below. The bot only receives {videoId + metadata} via POST /api/pick and
does a single deep yt-dlp resolve for voice. This keeps search/listing
load off the bot's egress IP.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from aiohttp import web

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VID_IN_URL = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")
WEB_DIR = Path(__file__).parent
FRONTEND_INDEX = WEB_DIR.parent / "frontend" / "dist" / "index.html"
FRONTEND_ASSETS = WEB_DIR.parent / "frontend" / "dist" / "assets"


def _yt_fallback_on() -> bool:
    return (os.getenv("SEARCH_YT_FALLBACK", "true").strip().lower() in ("1", "true", "yes"))


def _session_valid(cog, token: str):
    # No fixed expiry: a session lives while it's used (every /api call is a
    # heartbeat). Stale sessions are swept by the cog's idle-tunnel monitor
    # (_prune_stale_web_sessions) once the tunnel idle-kill has fired.
    sess = cog.web_sessions.get(token)
    if not sess:
        return None
    sess["last_heartbeat"] = time.time()
    return sess


def build_app(cog) -> web.Application:
    app = web.Application()

    async def health(_req):
        return web.json_response({"ok": True})

    async def pick_page(req):
        # Serves the built React picker (frontend/dist). Token is used by JS,
        # not validated here so the page can show errors itself.
        if FRONTEND_INDEX.is_file():
            # no-cache: the index references hashed assets that vanish on rebuild
            return web.FileResponse(FRONTEND_INDEX, headers={"Cache-Control": "no-cache"})
        return web.Response(
            status=503,
            text="Music picker UI is not built yet. Run: cd frontend && npm ci && npm run build",
            content_type="text/plain",
        )

    async def session_info(req):
        token = req.query.get("token", "")
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Invalid or expired link. Run /music mode:web again."}, status=403)
        guild = cog.bot.get_guild(sess["guild_id"])
        return web.json_response({
            "ok": True,
            "guild": guild.name if guild else str(sess["guild_id"]),
            "user": sess.get("user_name", ""),
            "queue_len": cog.state(sess["guild_id"]).qtotal,
            # null → no fixed expiry; the page hides the countdown
            "expires_in": None,
        })

    async def search_config(_req):
        from .search import backends, instances
        return web.json_response({
            "ok": True,
            "searchBackends": backends(),      # [{url, kind}] — raced in parallel
            "searchInstances": instances(),   # compat: bare urls
        })

    async def search_proxy(req):
        import aiohttp as _aiohttp

        from .search import server_search
        q = (req.query.get("q", "") or "").strip()
        if not q:
            return web.json_response({"ok": False, "error": "Empty query."}, status=400)
        async with _aiohttp.ClientSession() as session:
            results, via = await server_search(q, session)
        if not results and _yt_fallback_on():
            # every public backend is down — last resort: the bot's own
            # throttled yt-dlp search (same pacing + retry path as /play).
            try:
                kind, tracks = await cog._resolve_input(cog.bot.user, q)
            except Exception as e:  # noqa: BLE001
                print(f"[web-search] yt-dlp fallback failed: {type(e).__name__}: {e}", flush=True)
                kind, tracks = "error", []
            out = []
            if kind == "search":
                for t in tracks:
                    m = _VID_IN_URL.search(t.webpage_url or "")
                    if not m:
                        continue
                    out.append({"videoId": m.group(1), "title": t.title,
                                "uploader": t.uploader or "?", "duration": t.duration or 0,
                                "thumbnail": t.thumbnail or ""})
                if out:
                    results, via = out, "yt-dlp"
                    print(f"[web-search] yt-dlp fallback ok n={len(out)} q={q!r}", flush=True)
        if not results:
            return web.json_response(
                {"ok": False, "error": "No results — every search backend is unreachable right now. Try pasting a link below."},
                status=502,
            )
        return web.json_response({"ok": True, "results": results, "via": via})

    async def now_state(req):
        token = req.query.get("token", "")
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Link expired."}, status=403)
        return web.json_response({"ok": True, "now": cog.web_now(sess["guild_id"])})

    async def control(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Bad JSON."}, status=400)
        token = str(data.get("token", ""))
        action = str(data.get("action", ""))
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Link expired. Run /music mode:web again."}, status=403)
        allowed = {"toggle", "pause", "resume", "skip", "stop", "leave", "clear",
                   "shuffle", "loop", "loop_set", "volume_set", "volume_delta",
                   "autoplay", "autoplay_set", "remove", "jump", "join",
                   "playlist_more", "mix_more"}
        if action not in allowed:
            return web.json_response({"ok": False, "error": "Unknown action."}, status=400)
        params = {k: v for k, v in data.items() if k not in ("token", "action")}
        ok, msg = await cog.web_control(sess["guild_id"], sess["user_id"], action, params)
        status = 200 if ok else 400
        out: dict = {"ok": ok, "message": msg}
        if ok and action in ("loop", "loop_set", "volume_set", "volume_delta",
                             "autoplay", "autoplay_set", "jump"):
            # piggyback fresh state so buttons relabel without an extra poll
            try:
                out["now"] = cog.web_now(sess["guild_id"])
            except Exception:
                pass
        return web.json_response(out, status=status)

    async def queue_page(req):
        token = req.query.get("token", "")
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Link expired."}, status=403)
        try:
            page = max(1, int(req.query.get("page", "1")))
        except ValueError:
            page = 1
        return web.json_response({"ok": True, **cog.web_queue_page(sess["guild_id"], page)})

    async def play_query(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Bad JSON."}, status=400)
        token = str(data.get("token", ""))
        query = str(data.get("query", "") or "")
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Link expired. Run /music mode:web again."}, status=403)
        if not query.strip():
            return web.json_response({"ok": False, "error": "Empty query."}, status=400)
        ok, msg, payload = await cog.web_play(sess["guild_id"], sess["user_id"], query)
        return web.json_response({"ok": ok, "message": msg, **payload}, status=200 if ok else 400)

    async def related(req):
        import aiohttp as _aiohttp

        from .search import server_related
        vid = (req.query.get("videoId", "") or "").strip()
        if not VIDEO_ID_RE.match(vid):
            return web.json_response({"ok": False, "error": "Bad videoId."}, status=400)
        async with _aiohttp.ClientSession() as session:
            results, via = await server_related(vid, session)
        if not results and _yt_fallback_on():
            # public backends down — cap the radio-mix listing at 12 so this
            # stays as cheap as the autoplay top-up the bot already does.
            try:
                infos = await cog._extract(
                    f"https://www.youtube.com/watch?v={vid}&list=RD{vid}",
                    playlist=True, flat=True, playlist_start=2, playlist_end=13,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[web-search] yt-dlp related fallback failed: {type(e).__name__}", flush=True)
                infos = []
            out = []
            for d in infos or []:
                m = _VID_IN_URL.search(str(d.get("url") or d.get("webpage_url") or ""))
                if not m:
                    continue
                v = m.group(1)
                out.append({"videoId": v, "title": str(d.get("title") or "Unknown title")[:200],
                            "uploader": str(d.get("uploader") or d.get("channel") or "?")[:200],
                            "duration": int(d.get("duration") or 0),
                            "thumbnail": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg"})
            if out:
                results, via = out, "yt-dlp"
                print(f"[web-search] yt-dlp related fallback ok n={len(out)} vid={vid}", flush=True)
        return web.json_response({"ok": True, "results": results, "via": via})

    async def suggest(req):
        import aiohttp as _aiohttp

        from .search import server_suggest
        q = (req.query.get("q", "") or "").strip()
        async with _aiohttp.ClientSession() as session:
            out = await server_suggest(q, session)
        return web.json_response({"ok": True, "suggestions": out})

    async def submit_pick(req):
        try:
            data = await req.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Bad JSON."}, status=400)
        token = str(data.get("token", ""))
        video_id = str(data.get("videoId", "")).strip()
        sess = _session_valid(cog, token)
        if not sess:
            return web.json_response({"ok": False, "error": "Link expired. Run /music mode:web again."}, status=403)
        if not VIDEO_ID_RE.match(video_id):
            return web.json_response({"ok": False, "error": "Invalid videoId."}, status=400)
        ok, msg = await cog.queue_client_pick(
            sess["guild_id"],
            sess["user_id"],
            {
                "video_id": video_id,
                "title": str(data.get("title", "") or "Unknown title")[:200],
                "uploader": str(data.get("uploader", "") or "?")[:200],
                "duration": int(data.get("duration", 0) or 0),
                "thumbnail": str(data.get("thumbnail", "") or "")[:500],
            },
        )
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)

    app.router.add_get("/healthz", health)
    app.router.add_get("/pick", pick_page)
    if FRONTEND_ASSETS.is_dir():
        app.router.add_static("/assets/", FRONTEND_ASSETS)
    app.router.add_get("/api/session", session_info)
    app.router.add_get("/api/config", search_config)
    app.router.add_get("/api/search", search_proxy)
    app.router.add_get("/api/now", now_state)
    app.router.add_get("/api/related", related)
    app.router.add_get("/api/suggest", suggest)
    app.router.add_post("/api/control", control)
    app.router.add_get("/api/queue", queue_page)
    app.router.add_post("/api/play", play_query)
    app.router.add_post("/api/pick", submit_pick)
    return app


async def start_web_server(cog) -> None:
    """Run the picker web server in this event loop. Never raises."""
    import asyncio as _asyncio

    port = int(os.getenv("WEB_PORT", "8765"))
    host = os.getenv("WEB_HOST", "0.0.0.0")
    try:
        runner = web.AppRunner(build_app(cog))
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        cog.web_runner = runner
        print(f"[web] picker up on {host}:{port} (/pick)", flush=True)
    except OSError as e:
        print(f"[web] could not bind {host}:{port}: {e} (picker links will fail)", flush=True)
        return
    # Zero-config public link: quick tunnel in the background (non-blocking).
    try:
        from .tunnel import ensure_tunnel

        async def _tunnel_bg():
            url = await ensure_tunnel(port)
            if url:
                cog.web_public_url = url

        _asyncio.get_running_loop().create_task(_tunnel_bg())
    except Exception as e:  # noqa: BLE001 — tunnel is optional
        print(f"[web] tunnel disabled: {e}", flush=True)
