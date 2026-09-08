import os

COOKIE_FILE = "cookies.txt"


def _force_ipv4() -> bool:
    return os.getenv("YDL_FORCE_IPV4", "true").lower() in ("1", "true", "yes")


def get_ydl_opts(playlist: bool = False, search_n: int = 0, flat: bool = False) -> dict:
    # NOTE: no extractor_args override. yt-dlp's built-in default clients
    # change every few weeks to dodge YouTube's bot checks — pinning
    # player_client (tv/android/...) goes stale and causes
    # "page needs to be reloaded" / error 152. Rely on stock defaults.
    # flat=True is for search LISTING only: metadata without stream URLs,
    # lean timeouts so gated entries fail fast instead of burning retries.
    opts: dict = {
        "format": "bestaudio/best",
        "noplaylist": not playlist,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "default_search": "ytsearch",
        "extract_flat": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
    }
    if flat:
        opts["extract_flat"] = True
        opts["skip_download"] = True
        opts["socket_timeout"] = 8
        opts["retries"] = 1
        opts["fragment_retries"] = 1
    if _force_ipv4():
        opts["source_address"] = "0.0.0.0"
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    if playlist:
        # full playlist, no track cap; flat listing keeps it fast
        opts["extract_flat"] = "discard_in_playlist"
        # playlists often contain dead/private entries — skip, don't fail all
        opts["ignoreerrors"] = True
    if search_n > 0:
        opts["default_search"] = f"ytsearch{search_n}"
        opts["noplaylist"] = True
        # top-N searches often include 1 dead/region-blocked video —
        # skip it instead of failing the whole search
        opts["ignoreerrors"] = True
    return opts


FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
