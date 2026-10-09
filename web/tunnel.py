"""Cloudflare quick-tunnel for the picker web UI (zero-config public link).

Out of the box the bot spawns `cloudflared tunnel --url http://127.0.0.1:<port>`
(a free trycloudflare.com quick tunnel, no account needed) and uses the
resulting https URL for picker links. No LAN IP / port-forwarding needed.

Priority for picker links (see Music.web_base_url):
  1. explicit non-loopback WEB_BASE_URL (user override)
  2. live quick-tunnel URL
  3. loopback fallback (host-only, warns in Discord)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil

TUNNEL_URL_RE = re.compile(r"https://[A-Za-z0-9-]+\.trycloudflare\.com/?")

_lock = asyncio.Lock()
_proc: asyncio.subprocess.Process | None = None
public_url: str | None = None


def parse_tunnel_url(line: str) -> str | None:
    m = TUNNEL_URL_RE.search(line or "")
    return m.group(0).rstrip("/") if m else None


def tunnel_enabled() -> bool:
    return os.getenv("CLOUDFLARE_TUNNEL", "true").lower() in ("1", "true", "yes", "on", "auto")


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared")


def get_public_url() -> str | None:
    return public_url


async def _drain(proc: asyncio.subprocess.Process) -> None:
    """Keep stderr drained so cloudflared never blocks; clear URL on exit."""
    global public_url
    try:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace")
            if not public_url and "trycloudflare.com" in line:
                found = parse_tunnel_url(line)
                if found:
                    public_url = found
                    print(f"[web] public picker URL: {public_url}/pick", flush=True)
    except Exception:
        pass
    finally:
        if proc is _proc_ref():
            public_url = None
            print("[web] tunnel exited, picker links fall back to WEB_BASE_URL", flush=True)


def _proc_ref():
    return _proc


async def ensure_tunnel(port: int, timeout: float = 40.0) -> str | None:
    """Start the quick tunnel if needed; return its public URL (or None).

    Never raises — picker links just fall back. Safe to call repeatedly.
    """
    global _proc, public_url
    if not tunnel_enabled():
        return None
    if public_url:
        return public_url
    async with _lock:
        if public_url:
            return public_url
        if _proc is not None and _proc.returncode is None:
            pass  # already starting/running; fall through to wait below
        else:
            binary = find_cloudflared()
            if not binary:
                print("[web] cloudflared binary not found — picker links use WEB_BASE_URL", flush=True)
                return None
            try:
                _proc = await asyncio.create_subprocess_exec(
                    binary, "tunnel", "--no-autoupdate",
                    "--url", f"http://127.0.0.1:{port}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                asyncio.get_running_loop().create_task(_drain(_proc))
            except Exception as e:  # noqa: BLE001
                print(f"[web] tunnel spawn FAILED: {e}", flush=True)
                _proc = None
                return None
        # wait for the URL to appear in stderr (drained by _drain)
        import time as _time
        t0 = _time.time()
        while public_url is None and _time.time() - t0 < timeout:
            if _proc is not None and _proc.returncode is not None:
                print(f"[web] tunnel died fast (rc={_proc.returncode})", flush=True)
                return None
            await asyncio.sleep(0.5)
        return public_url
