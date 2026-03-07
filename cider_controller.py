"""
Cider (Apple Music) controller -- mirrors spotify_controller structure.
No auth. Polls local Cider REST API. Handles offline gracefully.
"""

import threading
import time
import requests
from album_cache import cache_art, get_dominant_color

# Cider REST API port: 10767 (default) or 1010 on some setups
CIDER_BASE_URL = "http://localhost:10767"
POLL_INTERVAL = 1.5
REQUEST_TIMEOUT = 1  # Short timeout so we don't hang if Cider is closed

_current_data = {
    "artist": "",
    "track": "",
    "album": "",
    "album_art_url": "",
    "album_art_local": "",
    "dominant_color": "#1a1a2e",
    "progress_ms": 0,
    "duration_ms": 0,
    "is_playing": False,
    "volume": 0,
    "device": "Cider",
    "track_id": "",
    "canvas_url": None,
    "server_time": 0,
    "track_changed_at": 0,
}
_lock = threading.Lock()
_previous_track_id = None
_available = False
_motion_cache = {}
_motion_lock = threading.Lock()

# Throttle offline log messages (print at most once per 30s when Cider is down)
_last_fail_log = 0
_FAIL_LOG_INTERVAL = 30


def _fix_artwork_url(url):
    """Replace {w}x{h} placeholder with 300x300 for album_cache compatibility."""
    if not url:
        return ""
    return url.replace("{w}", "300").replace("{h}", "300")


def _do_poll():
    global _previous_track_id, _available, _last_fail_log
    url = CIDER_BASE_URL + "/api/v1/playback/now-playing"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not _available:
            print("[Cider] Connected", flush=True)
        _available = True
    except (requests.RequestException, ValueError) as e:
        now = time.time()
        was_available = _available
        _available = False
        if was_available or (now - _last_fail_log > _FAIL_LOG_INTERVAL):
            _last_fail_log = now
            print("[Cider] Offline: " + str(e), flush=True)
        return

    info = data.get("info") or {}
    if not info:
        with _lock:
            _current_data.update({
                "is_playing": False,
                "track": "",
                "artist": "",
                "track_id": "",
                "canvas_url": None,
                "progress_ms": 0,
                "server_time": time.time(),
                "track_changed_at": 0,
            })
        return

    play_params = info.get("playParams") or {}
    track_id = str(play_params.get("id", ""))
    track_name = info.get("name") or info.get("trackName") or ""
    artist_name = info.get("artistName") or ""
    album_name = info.get("albumName", "")
    duration_ms = int(info.get("durationInMillis", 0))
    current_time_sec = float(info.get("currentPlaybackTime", 0))
    progress_ms = int(current_time_sec * 1000)

    artwork = info.get("artwork") or {}
    art_url_raw = artwork.get("url", "")
    art_url = _fix_artwork_url(art_url_raw) if art_url_raw else ""

    is_playing = _fetch_is_playing()

    try:
        vol_resp = requests.get(
            CIDER_BASE_URL + "/api/v1/playback/volume",
            timeout=REQUEST_TIMEOUT,
        )
        if vol_resp.ok:
            vol_data = vol_resp.json()
            vol_float = float(vol_data.get("volume", 0.5))
            volume = int(vol_float * 100)
        else:
            volume = 50
    except Exception:
        volume = _current_data.get("volume", 50)

    now = time.time()
    track_changed = track_id != _previous_track_id

    if track_changed:
        _previous_track_id = track_id
        local_art = cache_art(art_url) if art_url else ""
        color = get_dominant_color(art_url) if art_url else "#1a1a2e"
        track_changed_at = now
        canvas_url = _get_motion_artwork(info, track_id)
        print("[Cider] Now playing: " + artist_name + " - " + track_name, flush=True)
    else:
        with _lock:
            prev_local = _current_data.get("album_art_local", "")
            prev_color = _current_data.get("dominant_color", "#1a1a2e")
            track_changed_at = _current_data.get("track_changed_at", 0)
        local_art = prev_local.replace("/art/", "") if prev_local else (cache_art(art_url) if art_url else "")
        color = prev_color if prev_color != "#1a1a2e" else (get_dominant_color(art_url) if art_url else "#1a1a2e")
        canvas_url = _get_motion_artwork(info, track_id)

    device_label = "Cider" if _available else "Cider (offline)"
    with _lock:
        _current_data.update({
            "artist": artist_name,
            "track": track_name,
            "album": album_name,
            "album_art_url": art_url,
            "album_art_local": ("/art/" + local_art) if local_art else "",
            "dominant_color": color,
            "progress_ms": progress_ms,
            "duration_ms": duration_ms,
            "is_playing": is_playing,
            "volume": volume,
            "device": device_label,
            "track_id": track_id,
            "canvas_url": canvas_url,
            "server_time": now,
            "track_changed_at": track_changed_at,
        })


def _fetch_is_playing():
    try:
        resp = requests.get(
            CIDER_BASE_URL + "/api/v1/playback/is-playing",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            data = resp.json()
            return bool(data.get("is_playing", False))
    except Exception:
        pass
    return False


def _get_motion_artwork(info, track_id):
    """
    Check for Apple Music motion artwork / background video.
    Cider may expose these in extended metadata fields.
    Returns URL if found, else None.
    """
    if not track_id:
        return None
    with _motion_lock:
        if track_id in _motion_cache:
            return _motion_cache[track_id]
    motion_url = None
    for key in ("backgroundVideoUrl", "motionArtwork", "motion_artwork",
                "animatedArtwork", "extendedArtwork"):
        val = info.get(key)
        if isinstance(val, dict) and val.get("url"):
            motion_url = _fix_artwork_url(val["url"])
            break
        if isinstance(val, str) and val.startswith("http"):
            motion_url = val
            break
    if motion_url:
        print("[Cider] Motion artwork found for " + track_id, flush=True)
    with _motion_lock:
        _motion_cache[track_id] = motion_url
    return motion_url


def _poll_loop():
    while True:
        _do_poll()
        time.sleep(POLL_INTERVAL)


def start_polling():
    print("[Cider] Poller starting (interval=%.1fs)" % POLL_INTERVAL, flush=True)
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()


def get_current_data():
    with _lock:
        d = dict(_current_data)
    d["available"] = _available
    return d


def force_poll():
    threading.Thread(target=_do_poll, daemon=True).start()


def is_available():
    return _available


def play():
    try:
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/play",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] play failed: " + str(e))
    return False


def pause():
    try:
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/pause",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] pause failed: " + str(e))
    return False


def next_track():
    try:
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/next",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] next failed: " + str(e))
    return False


def previous_track():
    try:
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/previous",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] previous failed: " + str(e))
    return False


def seek_track(position_ms):
    try:
        seconds = position_ms / 1000.0
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/seek",
            json={"position": seconds},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] seek failed: " + str(e))
    return False


def set_volume(volume_percent):
    vol = max(0, min(100, volume_percent))
    vol_float = vol / 100.0
    try:
        resp = requests.post(
            CIDER_BASE_URL + "/api/v1/playback/volume",
            json={"volume": vol_float},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            force_poll()
            return True
    except Exception as e:
        print("[Cider] volume failed: " + str(e))
    return False
