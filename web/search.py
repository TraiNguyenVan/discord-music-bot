"""No-key YouTube search for the picker page (search + scroll, all in-page).

Client tries these Piped instances directly from the browser first (bot
stays out of it). If the browser can't reach any of them, it falls back
to GET /api/search on the bot, which proxies the same call server-side
(plain lightweight REST — not yt-dlp scraping, so no IP-ban concern).

Override with env: SEARCH_INSTANCES=https://a.com,https://b.com
"""

from __future__ import annotations

import os
import re

DEFAULT_INSTANCES = [
    "https://api.piped.private.coffee",  # verified working 2026-10-09
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.reallyaweso.me",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.leptons.xyz",
]

_VIDEO_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


def instances() -> list[str]:
    raw = (os.getenv("SEARCH_INSTANCES") or "").strip()
    if raw:
        return [s.strip().rstrip("/") for s in raw.split(",") if s.strip()]
    return list(DEFAULT_INSTANCES)


def normalize(items: list[dict]) -> list[dict]:
    """Piped /search items -> [{videoId,title,uploader,duration,thumbnail}]."""
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "")
        if "/watch" not in url:
            continue  # skip channels / playlists
        m = _VIDEO_RE.search(url)
        if not m:
            continue
        vid = m.group(1)
        if vid in seen:
            continue
        seen.add(vid)
        try:
            dur = int(it.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        if dur < 0:
            dur = 0
        out.append({
            "videoId": vid,
            "title": str(it.get("title") or "Unknown title")[:200],
            "uploader": str(it.get("uploaderName") or "?")[:200],
            "duration": dur,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return out


async def server_search(query: str, session) -> tuple[list[dict], str]:
    """Try each instance server-side. Returns (results, instance_used)."""
    import aiohttp as _aiohttp

    q = (query or "").strip()
    if not q:
        return [], ""
    for base in instances():
        try:
            async with session.get(
                f"{base}/search",
                params={"q": q, "filter": "videos"},
                timeout=_aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                results = normalize(data.get("items") if isinstance(data, dict) else [])
                if results:
                    return results, base
        except Exception:
            continue
    return [], ""


async def server_related(video_id: str, session) -> tuple[list[dict], str]:
    """YouTube's recommendation graph via /streams/{id}.relatedStreams."""
    import re as _re
    import aiohttp as _aiohttp

    if not _re.match(r"^[A-Za-z0-9_-]{11}$", video_id or ""):
        return [], ""
    for base in instances():
        try:
            async with session.get(
                f"{base}/streams/{video_id}",
                timeout=_aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                results = normalize(data.get("relatedStreams") if isinstance(data, dict) else [])
                if results:
                    return results, base
        except Exception:
            continue
    return [], ""


async def server_suggest(query: str, session) -> list[str]:
    """Autocomplete via YouTube's suggest API (JSONP parsed server-side)."""
    import json as _json

    import aiohttp as _aiohttp

    q = (query or "").strip()
    if not q:
        return []
    try:
        async with session.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "youtube", "ds": "yt", "q": q},
            timeout=_aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                return []
            body = (await resp.text()).strip()
        prefix = "window.google.ac.h("
        if not body.startswith(prefix) or not body.endswith(")"):
            return []
        data = _json.loads(body[len(prefix):-1])
        return [str(s[0]) for s in (data[1] if len(data) > 1 else []) if s][:8]
    except Exception:
        return []
