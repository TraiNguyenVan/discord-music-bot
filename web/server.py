"""Sidecar web UI: client-side YouTube picker with a real embedded player.

The browser does all YouTube discovery (search/browse/preview via the
YouTube IFrame Player API). The bot only receives {videoId + metadata}
via POST /api/pick and does a single deep yt-dlp resolve for voice.
This keeps search/listing load off the bot's egress IP.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from aiohttp import web

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WEB_DIR = Path(__file__).parent


def _session_valid(cog, token: str):
    sess = cog.web_sessions.get(token)
    if not sess:
        return None
    if time.time() > sess.get("expires_at", 0):
        cog.web_sessions.pop(token, None)
        return None
    return sess


def build_app(cog) -> web.Application:
    app = web.Application()

    async def health(_req):
        return web.json_response({"ok": True})

    async def pick_page(req):
        # token is used by JS, not validated here so the page can show errors itself
        return web.FileResponse(WEB_DIR / "pick.html")

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
            "queue_len": len(cog.state(sess["guild_id"]).queue),
            "expires_in": max(0, int(sess["expires_at"] - time.time())),
        })

    async def search_config(_req):
        from .search import instances
        return web.json_response({"ok": True, "searchInstances": instances()})

    async def search_proxy(req):
        import aiohttp as _aiohttp

        from .search import instances, server_search
        q = (req.query.get("q", "") or "").strip()
        if not q:
            return web.json_response({"ok": False, "error": "Empty query."}, status=400)
        async with _aiohttp.ClientSession() as session:
            results, via = await server_search(q, session)
        if not results:
            return web.json_response(
                {"ok": False, "error": f"No results (tried {len(instances())} search backends). Try pasting a link below."},
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
        if action not in ("toggle", "skip"):
            return web.json_response({"ok": False, "error": "Unknown action."}, status=400)
        ok, msg = await cog.web_control(sess["guild_id"], sess["user_id"], action)
        return web.json_response({"ok": ok, "message": msg}, status=200 if ok else 400)

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
    app.router.add_get("/api/session", session_info)
    app.router.add_get("/api/config", search_config)
    app.router.add_get("/api/search", search_proxy)
    app.router.add_get("/api/now", now_state)
    app.router.add_post("/api/control", control)
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
