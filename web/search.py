"""No-key YouTube search for the picker page (search + scroll, all in-page).

The browser hits these public API backends directly first (bot stays out
of it). If the browser cannot reach any of them, it falls back to
GET /api/search on the bot, which races the same backends server-side —
and if every public backend is down, the bot's own throttled yt-dlp
resolver is the last resort (same pacing/retry path as /play).

Two API families are supported:
  piped     — /search?q=&filter=videos, /streams/{id} (relatedStreams)
  invidious — /api/v1/search?q=&type=video, /api/v1/videos/{id} (recommendedVideos)

All backends are raced in parallel; the first one returning results wins.

Override with env (comma-separated, each entry "url" or "url|kind"):
  SEARCH_INSTANCES=https://a.com,https://b.com|invidious
"""

from __future__ import annotations

import asyncio
import os

DEFAULT_BACKENDS = [
    # Public instances die constantly (Piped's whole ecosystem is fading) —
    # spread across two ecosystems so one outage doesn't kill search.
    # Re-verified live 2026-10-09: search ok, CORS ok.
    {"url": "https://api.piped.private.coffee", "kind": "piped"},
    {"url": "https://invidious.f5.si", "kind": "invidious"},
    {"url": "https://invidious.flokinet.to", "kind": "invidious"},  # slow (~6s) but alive
]

# old defaults, all dead as of 2026-10-09 — do not resurrect blindly:
#   pipedapi.kavin.rocks (HTTP 525), pipedapi.reallyaweso.me (502),
#   pipedapi.adminforge.de (301 -> homepage), pipedapi.leptons.xyz (502)


def _log(msg: str) -> None:
    print(f"[web-search] {msg}", flush=True)


def backends() -> list[dict]:
    """Configured backends: [{url, kind}]. Env overrides replace defaults."""
    raw = (os.getenv("SEARCH_INSTANCES") or "").strip()
    if not raw:
        return [dict(b) for b in DEFAULT_BACKENDS]
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        url, _, kind = entry.rpartition("|")
        if "/" in url and "." in url:
            kind = (kind or "piped").strip().lower()
        else:  # no "|kind" suffix — the whole entry is the URL
            url, kind = entry, "piped"
        out.append({"url": url.rstrip("/"), "kind": kind})
    return out


def instances() -> list[str]:
    """Backwards-compat: bare URLs of the configured backends."""
    return [b["url"] for b in backends()]


def _host(b: dict) -> str:
    url = b["url"]
    return url.split("//", 1)[-1].split("/", 1)[0]


def normalize(items: list[dict]) -> list[dict]:
    """Piped items -> [{videoId,title,uploader,duration,thumbnail}]."""
    import re as _re

    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "")
        if "/watch" not in url:
            continue  # skip channels / playlists
        m = _re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
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
        out.append({
            "videoId": vid,
            "title": str(it.get("title") or "Unknown title")[:200],
            "uploader": str(it.get("uploaderName") or "?")[:200],
            "duration": max(0, dur),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return out


def normalize_invidious(items: list[dict]) -> list[dict]:
    """Invidious items (search + recommendedVideos share the shape)."""
    out: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        vid = str(it.get("videoId") or "")
        if not vid or vid in seen:
            continue
        if it.get("type") not in (None, "video"):
            continue
        seen.add(vid)
        try:
            dur = int(it.get("lengthSeconds") or 0)
        except (TypeError, ValueError):
            dur = 0
        out.append({
            "videoId": vid,
            "title": str(it.get("title") or "Unknown title")[:200],
            "uploader": str(it.get("author") or "?")[:200],
            "duration": max(0, dur),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return out


async def _race(bases: list[dict], fetch, label: str, extra: str = "") -> tuple[list, str, str]:
    """Race every backend in parallel; first with results wins.

    fetch(base) -> (results, note) — never raises. Returns
    (results, winning_url, failnote) — failnote "" on success, otherwise
    one compact status per backend for the log line.
    """
    if not bases:
        return [], "", "no backends configured"
    tasks = [asyncio.ensure_future(fetch(b)) for b in bases]
    pending, notes = set(tasks), []
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                base, results, note = t.result()
                if results:
                    _log(f"{label} ok via={_host(base)} n={len(results)}{extra}")
                    return results, base["url"], ""
                if note:
                    notes.append(note)
        failnote = ", ".join(notes)
        _log(f"{label} ALL BACKENDS FAILED: {failnote}{extra}")
        return [], "", failnote
    finally:
        for t in tasks:
            t.cancel()


async def _search_one(b: dict, q: str, session):
    import aiohttp as _aiohttp

    try:
        if b["kind"] == "invidious":
            url = f"{b['url']}/api/v1/search"
            params = {"q": q, "type": "video"}
        else:
            url = f"{b['url']}/search"
            params = {"q": q, "filter": "videos"}
        async with session.get(url, params=params, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return b, None, f"{_host(b)}=HTTP{resp.status}"
            data = await resp.json()
        if b["kind"] == "invidious":
            results = normalize_invidious(data if isinstance(data, list) else [])
        else:
            results = normalize(data.get("items") if isinstance(data, dict) else [])
        if not results:
            return b, None, f"{_host(b)}=0 items"
        return b, results, ""
    except Exception as e:  # noqa: BLE001 — one dead backend must not sink the race
        return b, None, f"{_host(b)}={type(e).__name__}"


async def _related_one(b: dict, video_id: str, session):
    import aiohttp as _aiohttp

    try:
        if b["kind"] == "invidious":
            url = f"{b['url']}/api/v1/videos/{video_id}"
            pick = (lambda d: normalize_invidious(d.get("recommendedVideos") if isinstance(d, dict) else []))
        else:
            url = f"{b['url']}/streams/{video_id}"
            pick = (lambda d: normalize(d.get("relatedStreams") if isinstance(d, dict) else []))
        async with session.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return b, None, f"{_host(b)}=HTTP{resp.status}"
            data = await resp.json()
        results = pick(data)
        if not results:
            return b, None, f"{_host(b)}=0 items"
        return b, results, ""
    except Exception as e:  # noqa: BLE001
        return b, None, f"{_host(b)}={type(e).__name__}"


async def server_search(query: str, session) -> tuple[list[dict], str]:
    """Race all backends server-side. Returns (results, winning_url)."""
    q = (query or "").strip()
    if not q:
        return [], ""
    results, via, _ = await _race(
        backends(), lambda b: _search_one(b, q, session), "search", f" q={q!r}")
    return results, via


async def server_related(video_id: str, session) -> tuple[list[dict], str]:
    """Race all backends for the recommendation graph of one video."""
    import re as _re

    if not _re.match(r"^[A-Za-z0-9_-]{11}$", video_id or ""):
        return [], ""
    results, via, _ = await _race(
        backends(), lambda b: _related_one(b, video_id, session), "related", f" vid={video_id}")
    return results, via


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
