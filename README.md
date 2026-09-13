# Discord Music Bot

A resilient Discord music bot — slash commands + persistent panel, YouTube via `yt-dlp` → `FFmpeg` → voice. Built for the real world: DNS stalls, voice drops, IPv6 blackholes, and users who double-tap skip.

![Python](https://img.shields.io/badge/python-3.13-blue)
![discord.py](https://img.shields.io/badge/discord.py-2.7-5865F2)
![yt-dlp](https://img.shields.io/badge/yt--dlp-latest-red)
![Docker](https://img.shields.io/badge/docker-ready-2496ED)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Slash + panel:** `/music` opens a persistent control panel (Add / Pause / Skip / Stop / Shuffle / Loop / Vol / Queue / Refresh). Also `/play /search /playlist /queue /nowplaying /skip /pause /resume /stop /remove /clear /shuffle /loop /volume /join /leave`.
- **Fast search:** text → ranked top-5 picker (prefers official/short over mixes/lives) with lazy deep resolve; links/playlists resolve directly.
- **Uncapped playlists:** full YouTube playlists, no track cap.
- **Stay-in-voice:** bot stays until explicit leave; 12h idle safety net only (`INACTIVITY_TIMEOUT`).
- **Resilient voice:** auto-rejoin on drops, zombie-client heal, FFmpeg reconnect + 20s stall breaker, hot-spin breaker (3 instant deaths → advance).
- **Skip:** instant for anyone, debounced (`SKIP_DEBOUNCE_SEC=1.5`) so double-taps don't drain the queue. `loop=off|track|queue` respected.
- **Interaction hardening:** `defer` with bounded 0.8s+1.5s retry on DNS blips (`Temporary failure in name resolution` → no more `Unknown interaction (10062)` ghosting).
- **Observability:** `[usage]/[event]/[extract]/[throttle]` logs for every interaction.

## Quick start

### 1) Prerequisites

- Python 3.13 + FFmpeg **or** Docker
- A Discord Bot token with `applications.commands` + `bot` scopes
- (Optional) `cookies.txt` for YouTube age/region gates

### 2) Configure

```sh
cp .env.example .env
# edit .env → set DISCORD_TOKEN
```

`.env.example`:

```
DISCORD_TOKEN=put-your-bot-token-here
SKIP_DEBOUNCE_SEC=1.5
INACTIVITY_TIMEOUT=43200
ACTIVITY_NAME=
LOG_LEVEL=INFO
YDL_FORCE_IPV4=true
```

### 3) Run with Docker (recommended)

```sh
docker compose up --build -d
docker compose logs -f --tail 50
```

Uses host `dnsmasq` cache (`172.19.0.1`) + fallback `1.1.1.1`, v4-only (`filter-AAAA`), to survive stalls.

### 4) Run locally

```sh
pip install -r requirements.txt
python bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/music` | Open the persistent panel (one per server, survives restarts) |
| `/play <query>` | URL → plays; `list=` → full playlist; text → picker |
| `/search <text>` | Top-5 YouTube picks |
| `/playlist <url>` | Queue full playlist |
| `/queue [page]` | Show queue (10/page) |
| `/skip` | Skip one (debounced) |
| `/pause` `/resume` `/stop` | Transport |
| `/shuffle` `/loop` `/volume` `/remove` `/clear` | Queue mgmt |
| `/join` `/leave` `/help` `/about` | Voice + help |

Panel is the intended UX — users rarely need to type slash commands.

## How it works

```
Discord interaction → _safe_defer (0.8s retry) → _resolve_input (playlist/url/search)
→ _ensure_voice (heal + perms check) → _play_next (play_lock) → FFmpeg stream
→ voice thread after() → _play_next chain
```

- **Search:** `ytsearch10` flat listing (8s timeout, 1 retry) → `_rank_search` (demotes lives/mixes, prefers title overlap + official) → picker buttons (30m timeout, one-press lock).
- **Play:** `FFmpegPCMAudio(stream, -reconnect -rw_timeout 20s -timeout 20s)` + volume transformer.
- **Reliability:** `VOICE_REJOIN_DELAY=5`, `dns=172.19.0.1`, `YDL_FORCE_IPV4`, global `3s` search throttle to avoid YouTube IP bans (`-t sleep` equivalent).

## Logs

```sh
# all usage/events
docker compose logs --since 60m --no-color | grep -E "\[usage\]|\[event\]|\[extract\]"

# follow live
docker compose logs -f --tail 20 --no-color
```

Examples:

```
[usage] /search query=tình đầu | guild=... user=...
[event] guild=... now-playing title='...' by=... left=3 loop=queue force_skip=False
[extract] flat=True n=10 took=2.8s query='...'
[throttle] pacing search +1.2s
[music] stream FAILED guild=... elapsed=12s streak=1
```

If a `/playlist` shows `910× Video unavailable` + `rate-limited for up to an hour` — that's a YouTube IP throttle from a huge radio mix (`RD...`), not a bot bug. Avoid `RD` mixes for ~1h, use direct links.

## Troubleshooting

- `ClientConnectorDNSError / Temporary failure in name resolution` → DNS blip; bot now retries defer once (0.8s) and then shows `Discord hiccup — try again`. Check `systemctl status dnsmasq` + `/etc/dnsmasq.conf` (`listen-address=127.0.0.1,172.19.0.1`, `filter-AAAA`, `cache-size=2000`).
- `Already playing audio.` / queue drains on double skip → fixed by serialize on `play_lock` + `SKIP_DEBOUNCE_SEC`.
- `Sign in to confirm you're not a bot` / `429` → export `cookies.txt` from your browser (yt-dlp wiki) and `docker compose up --build -d` again.

## Project structure

```
.
├── bot.py              # startup, login retry, slash sync, interaction logging
├── cogs/
│   ├── music.py        # queue, panel, search, voice heal, play/next, skip
│   └── youtube.py      # yt-dlp opts + FFmpeg flags
├── compose.yaml        # music-bot service + dns
├── Dockerfile          # python:3.13-slim + ffmpeg
└── requirements.txt
```

## Security

- Never commit `.env` or `cookies.txt` (both gitignored).
- Token pasted in chat should be rotated via Discord Developer Portal → Bot → Reset Token.

## License

MIT — see `LICENSE` if present.
