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

# demo_state.py lives in server/, but the playlist + canvas/art bundles
# live at <repo-root>/static/demo/. Walk one level up from this file to
# reach the repo root. Without this, _load_playlist silently falls back
# to _DEFAULT_PLAYLIST and bundled MP4s/JPGs are never found.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEMO_DIR = os.path.join(_REPO_ROOT, "static", "demo")
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
# canvas_url is left empty when a track has neither a bundled MP4 nor a
# CDN URL; the frontend treats empty as "no canvas, show artwork only".
for _t in _PLAYLIST:
    _t.setdefault("canvas_cdn_url", "")
    _t.setdefault("canvas_local", "")
    _t.setdefault("art_local", "")
    _t.setdefault("dominant_color", "#1a1a2e")
    _t["canvas_url"] = _resolve_canvas(_t["canvas_local"], _t.get("canvas_cdn_url") or "")
    _t["album_art_local"] = _resolve_art(_t["art_local"], _t.get("album_art_url", ""))


# For the idle screensaver, prefer any track with a real canvas; fall back
# to Stick Talk so the screensaver always has something to show even if
# every track in the playlist is canvas-less.
_IDLE_TRACK = next((t for t in _PLAYLIST if t["canvas_url"]), None)


# How long the demo can sit paused before we surface it as "no active
# device" to the frontend, which is what triggers the idle screensaver.
IDLE_AFTER_PAUSE_S = 15

_lock = threading.Lock()
_state = {
    "index": 0,
    "started_at": time.time(),
    "is_playing": True,
    "paused_at": 0.0,         # wall-clock time pause began (0 while playing)
    "pause_progress_ms": 0,   # frozen progress while paused
    "volume": 65,
    "source": "spotify",
    "visual_mode": "canvas_card",
}


def _current_track():
    return _PLAYLIST[_state["index"] % len(_PLAYLIST)]


def _progress_ms_unlocked():
    t = _current_track()
    if not _state["is_playing"]:
        return _state["pause_progress_ms"]
    elapsed = (time.time() - _state["started_at"]) * 1000.0
    return int(elapsed) % t["duration_ms"]


def _is_idle_paused_unlocked():
    return (
        not _state["is_playing"]
        and _state["paused_at"] > 0
        and (time.time() - _state["paused_at"]) > IDLE_AFTER_PAUSE_S
    )


def _maybe_advance_unlocked():
    """If the current track has finished playing, jump to the next one
    and shift started_at to the moment that track began. Idempotent and
    handles long gaps between polls (advances multiple tracks at once
    if needed)."""
    if not _state["is_playing"]:
        return
    while True:
        t = _current_track()
        elapsed_ms = (time.time() - _state["started_at"]) * 1000.0
        if elapsed_ms < t["duration_ms"]:
            return
        _state["started_at"] += t["duration_ms"] / 1000.0
        _state["index"] = (_state["index"] + 1) % len(_PLAYLIST)


def get_state():
    """Return a payload matching the real /api/state schema exactly."""
    with _lock:
        _maybe_advance_unlocked()
        t = _current_track()
        progress = _progress_ms_unlocked()
        is_playing = _state["is_playing"]
        idle_paused = _is_idle_paused_unlocked()
        has_canvas = bool(t["canvas_url"])
        idle_t = _IDLE_TRACK or t
        # When paused long enough, drop track_id so the frontend kicks
        # into idle screensaver mode (matches what real Spotify does
        # once the device falls inactive).
        return {
            "track_id": "" if idle_paused else t["track_id"],
            "track": "" if idle_paused else t["track"],
            "artist": "" if idle_paused else t["artist"],
            "album": "" if idle_paused else t["album"],
            "album_art_url": "" if idle_paused else t["album_art_url"],
            "album_art_local": "" if idle_paused else t["album_art_local"],
            "canvas_url": "" if idle_paused else t["canvas_url"],
            "canvas_cdn_url": "" if idle_paused else (t.get("canvas_cdn_url") or ""),
            "duration_ms": 0 if idle_paused else t["duration_ms"],
            "progress_ms": 0 if idle_paused else progress,
            "is_playing": is_playing,
            "volume": _state["volume"],
            "device": "PiMusic Demo",
            "source": _state["source"],
            "server_time": time.time(),
            "track_changed_at": _state["started_at"],
            "visual_mode": _state["visual_mode"],
            "visual_type": "canvas_video" if (has_canvas and not idle_paused) else "image",
            "cpu_throttled": False,
            "rate_limited_until": 0,
            "dominant_color": t["dominant_color"],
            "idle_canvas_track_id": idle_t["track_id"],
            "idle_canvas_url": idle_t["canvas_url"] or _STICK_TALK_CDN,
            "idle_canvas_cdn_url": _STICK_TALK_CDN,
        }


def _on_track_change_unlocked():
    """Called whenever the current track or progress is reset by user
    interaction. Restarts the idle-pause clock so user activity dismisses
    the screensaver (the frontend already dismisses on tap, this keeps
    the backend in sync)."""
    _state["pause_progress_ms"] = 0
    if not _state["is_playing"]:
        _state["paused_at"] = time.time()


def next_track():
    with _lock:
        _state["index"] = (_state["index"] + 1) % len(_PLAYLIST)
        _state["started_at"] = time.time()
        _on_track_change_unlocked()
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
        _on_track_change_unlocked()
    return True


def seek(position_ms):
    with _lock:
        t = _current_track()
        pos = max(0, min(int(position_ms), t["duration_ms"] - 1))
        if _state["is_playing"]:
            _state["started_at"] = time.time() - (pos / 1000.0)
        else:
            _state["pause_progress_ms"] = pos
            _state["paused_at"] = time.time()
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


def set_playing(playing):
    """Toggle play/pause. Pausing freezes progress where it is; resuming
    picks up from there. Pause also starts the idle-pause clock — after
    IDLE_AFTER_PAUSE_S of paused silence, get_state() drops the track
    and the frontend's idle screensaver takes over."""
    with _lock:
        playing = bool(playing)
        if playing == _state["is_playing"]:
            return True
        if playing:
            _state["started_at"] = time.time() - (_state["pause_progress_ms"] / 1000.0)
            _state["is_playing"] = True
            _state["paused_at"] = 0.0
        else:
            _state["pause_progress_ms"] = _progress_ms_unlocked()
            _state["is_playing"] = False
            _state["paused_at"] = time.time()
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
    """Return the public CDN URL for a track_id, or None if the track has no
    canvas. The Vercel /api/canvas proxy turns None into a 404, which the
    frontend treats as "give up canvas, show artwork only"."""
    for t in _PLAYLIST:
        if t["track_id"] == track_id:
            return t.get("canvas_cdn_url") or None
    return None
