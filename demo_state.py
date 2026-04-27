"""Demo mode state — hardcoded playlist with live-advancing progress.

When DEMO_MODE=true, both spotify_server.py and api/index.py (Vercel) route
the frontend through this module instead of hitting Spotify/Cider. The
schema returned by get_state() matches exactly what the real /api/state
returns so the client-side code has no idea the backend is faked.

Thread-safe. No file IO outside a one-time existence check at import.
"""

import json
import os
import threading
import time

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEMO_DIR = os.path.join(_BASE_DIR, "static", "demo")
_PLAYLIST_JSON = os.path.join(_DEMO_DIR, "playlist.json")


def _resolve_canvas(local_name, cdn_fallback):
    """Prefer a bundled MP4 in static/demo/, fall back to a public CDN URL."""
    local_path = os.path.join(_DEMO_DIR, local_name)
    if os.path.isfile(local_path):
        return "/static/demo/" + local_name
    return cdn_fallback


def _resolve_art(local_name, cdn_fallback):
    local_path = os.path.join(_DEMO_DIR, local_name)
    if os.path.isfile(local_path):
        return "/static/demo/" + local_name
    return cdn_fallback


# Stick Talk canvas URL is known-working (it's the idle screensaver default
# and has been cached in the app for months). Used as a universal fallback
# until real canvases are dropped into static/demo/.
_STICK_TALK_CDN = (
    "https://canvaz.scdn.co/upload/artist/1RyvyyTE3xzB2ZywiAwp0i/"
    "video/4c9cbe2ef3554e8a85dcf138409144ed.cnvs.mp4"
)


# Default playlist — used if static/demo/playlist.json is missing. Edit this
# by running `python build_demo_playlist.py <spotify-url> <spotify-url> ...`
# which fetches the canvas CDN URL, title, artist, album, duration, and art
# via the running Spotify credentials and writes the JSON file for you.
_DEFAULT_PLAYLIST = [
    {
        "track_id": "2X485T9Z5Ll0iQDZpX4TZS",
        "track": "Let It Happen",
        "artist": "Tame Impala",
        "album": "Currents",
        "duration_ms": 467000,
        "album_art_url": "https://i.scdn.co/image/ab67616d0000b27379ab99ca5bd2bcd96ed01b53",
        "canvas_cdn_url": _STICK_TALK_CDN,
        "canvas_local": "canvas-let-it-happen.mp4",
        "art_local": "album-let-it-happen.jpg",
        "dominant_color": "#d4b68c",
    },
    {
        "track_id": "20fAoPjfYltmd3K3bO7gbt",
        "track": "Stick Talk",
        "artist": "Future",
        "album": "DS2 (Deluxe)",
        "duration_ms": 224000,
        "album_art_url": "https://i.scdn.co/image/ab67616d0000b273b2592bea12d840fd096ef965",
        "canvas_cdn_url": _STICK_TALK_CDN,
        "canvas_local": "canvas-stick-talk.mp4",
        "art_local": "album-stick-talk.jpg",
        "dominant_color": "#c4bcbe",
    },
    {
        "track_id": "69kOkLUCkxIZYexIgSG8rq",
        "track": "Get Lucky",
        "artist": "Daft Punk, Pharrell Williams, Nile Rodgers",
        "album": "Random Access Memories",
        "duration_ms": 369000,
        "album_art_url": "https://i.scdn.co/image/ab67616d0000b2734c02f31c03f0cb7acd57c1c9",
        "canvas_cdn_url": _STICK_TALK_CDN,
        "canvas_local": "canvas-get-lucky.mp4",
        "art_local": "album-get-lucky.jpg",
        "dominant_color": "#9c6f42",
    },
]


def _load_playlist():
    """Load playlist.json if present, otherwise fall back to the defaults."""
    if os.path.isfile(_PLAYLIST_JSON):
        try:
            with open(_PLAYLIST_JSON, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                return data
            print(f"[demo_state] {_PLAYLIST_JSON} is empty or malformed; using defaults")
        except Exception as exc:
            print(f"[demo_state] Failed to read {_PLAYLIST_JSON}: {exc}; using defaults")
    return list(_DEFAULT_PLAYLIST)


_PLAYLIST = _load_playlist()

# Fill in any missing optional fields and resolve local-vs-CDN URLs once
# at import. All of this is cheap — just stat() calls and dict merges.
for _t in _PLAYLIST:
    _t.setdefault("canvas_cdn_url", _STICK_TALK_CDN)
    _t.setdefault("canvas_local", "")
    _t.setdefault("art_local", "")
    _t.setdefault("dominant_color", "#1a1a2e")
    _t["canvas_url"] = _resolve_canvas(_t["canvas_local"], _t.get("canvas_cdn_url") or _STICK_TALK_CDN)
    _t["album_art_local"] = _resolve_art(_t["art_local"], _t.get("album_art_url", ""))


_lock = threading.Lock()
_state = {
    "index": 0,
    "started_at": time.time(),
    "volume": 65,
    "source": "spotify",
    "visual_mode": "canvas_card",
}


def _current_track():
    return _PLAYLIST[_state["index"] % len(_PLAYLIST)]


def _progress_ms_unlocked():
    t = _current_track()
    elapsed = (time.time() - _state["started_at"]) * 1000.0
    return int(elapsed) % t["duration_ms"]


def get_state():
    """Return a payload matching the real /api/state schema exactly."""
    with _lock:
        t = _current_track()
        progress = _progress_ms_unlocked()
        return {
            "track_id": t["track_id"],
            "track": t["track"],
            "artist": t["artist"],
            "album": t["album"],
            "album_art_url": t["album_art_url"],
            "album_art_local": t["album_art_local"],
            "canvas_url": t["canvas_url"],
            "canvas_cdn_url": t.get("canvas_cdn_url", _STICK_TALK_CDN),
            "duration_ms": t["duration_ms"],
            "progress_ms": progress,
            "is_playing": True,
            "volume": _state["volume"],
            "device": "PiMusic Demo",
            "source": _state["source"],
            "server_time": time.time(),
            "track_changed_at": _state["started_at"],
            "visual_mode": _state["visual_mode"],
            "visual_type": "canvas_video",
            "cpu_throttled": False,
            "rate_limited_until": 0,
            "dominant_color": t["dominant_color"],
            "idle_canvas_track_id": _PLAYLIST[0]["track_id"] if len(_PLAYLIST) == 1 else _PLAYLIST[1]["track_id"],
            "idle_canvas_url": _PLAYLIST[0]["canvas_url"] if len(_PLAYLIST) == 1 else _PLAYLIST[1]["canvas_url"],
            "idle_canvas_cdn_url": _STICK_TALK_CDN,
        }


def next_track():
    with _lock:
        _state["index"] = (_state["index"] + 1) % len(_PLAYLIST)
        _state["started_at"] = time.time()
    return True


def previous_track():
    """Match real player semantics: if >3s into the track, restart it;
    otherwise jump to the previous track."""
    with _lock:
        if _progress_ms_unlocked() > 3000:
            _state["started_at"] = time.time()
        else:
            _state["index"] = (_state["index"] - 1) % len(_PLAYLIST)
            _state["started_at"] = time.time()
    return True


def seek(position_ms):
    with _lock:
        t = _current_track()
        pos = max(0, min(int(position_ms), t["duration_ms"] - 1))
        _state["started_at"] = time.time() - (pos / 1000.0)
    return True


def set_volume(volume):
    with _lock:
        _state["volume"] = max(0, min(100, int(volume)))
    return True


def set_source(source):
    with _lock:
        if source in ("spotify", "cider"):
            _state["source"] = source
            return True
        return False


def set_playing(_playing):
    """No-op. is_playing is always true in demo mode per spec."""
    return True


def get_canvas_file(track_id):
    """Return absolute path to a bundled canvas MP4 for a given track_id,
    or None if the file isn't present."""
    for t in _PLAYLIST:
        if t["track_id"] == track_id:
            p = os.path.join(_DEMO_DIR, t["canvas_local"])
            if os.path.isfile(p):
                return p
    return None


def get_canvas_cdn(track_id):
    """Return the public CDN fallback URL for a track_id."""
    for t in _PLAYLIST:
        if t["track_id"] == track_id:
            return t.get("canvas_cdn_url") or _STICK_TALK_CDN
    return None
