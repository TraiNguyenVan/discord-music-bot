#!/usr/bin/env python3
"""Integration smoke for the web sidecar + the built React frontend.

Starts the REAL web/server.py app (build_app) with a stub cog and asserts
that production serving works: /pick serves frontend/dist/index.html, the
hashed JS/CSS load from /assets, /healthz responds, and an invalid token
gets the real 403 message. No network calls are made.

Run (needs aiohttp + a built frontend):

    pip install aiohttp
    (cd frontend && npm run build)
    python3 tests/web_smoke.py

Exit code 0 = all checks green.
"""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, REPO)

import web.server as web_server  # noqa: E402  (repo's web package, not aiohttp.web)
from web.server import build_app  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    tail = f" — {detail}" if detail and not ok else ""
    print(f"  {'ok ' if ok else 'FAIL'}  {name}{tail}", flush=True)


def stub_cog() -> SimpleNamespace:
    return SimpleNamespace(
        web_sessions={},
        state=lambda guild_id: SimpleNamespace(qtotal=0),
        bot=SimpleNamespace(get_guild=lambda gid: None),
        web_now=lambda guild_id: {"playing": False, "queue_len": 0},
    )


async def main() -> int:
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(build_app(stub_cog())))
    await client.start_server()
    try:
        r = await client.get("/healthz")
        check("/healthz ok", r.status == 200 and (await r.json())["ok"] is True,
              f"status={r.status}")

        r = await client.get("/pick")
        body = await r.text()
        ctype = r.headers.get("Content-Type", "")
        check("/pick serves the built index",
              r.status == 200 and "text/html" in ctype and "/assets/index-" in body,
              f"status={r.status} ctype={ctype}")
        check("/pick sends Cache-Control: no-cache",
              r.headers.get("Cache-Control") == "no-cache")

        m = re.search(r"/assets/index-[A-Za-z0-9_-]+\.(?:js|css)", body)
        if m:
            r = await client.get(m.group(0))
            ctype = r.headers.get("Content-Type", "")
            check("hashed assets served from /assets",
                  r.status == 200 and ("javascript" in ctype or "css" in ctype),
                  f"status={r.status} ctype={ctype}")
        else:
            check("hashed assets served from /assets", False,
                  "no /assets/index-* reference in the built index")

        r = await client.get("/api/session", params={"token": "no-such-token"})
        data = await r.json()
        check("invalid token → 403 with the real message",
              r.status == 403 and data.get("ok") is False
              and "Invalid or expired link" in data.get("error", ""),
              f"status={r.status} body={data}")

        # frontend not built → actionable 503, not a raw 404/500
        real = web_server.FRONTEND_INDEX
        web_server.FRONTEND_INDEX = Path("/nonexistent/dist/index.html")
        try:
            r = await client.get("/pick")
            body = await r.text()
            check("missing dist → actionable 503",
                  r.status == 503 and "npm run build" in body,
                  f"status={r.status} body={body!r}")
        finally:
            web_server.FRONTEND_INDEX = real
    finally:
        await client.close()

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks green", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
