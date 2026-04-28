"""Scrobbler – trusted local timer for consistent scrobble detection.

Maintains its own monotonic clock accumulator instead of relying on
polling-dependent progress_ms. Syncs with the API only when drift > 2s.
"""

import os
import time
import threading
from datetime import datetime

SCROBBLE_LOG = os.path.expanduser("~/pimusic/scrobbles.log")
SCROBBLE_THRESHOLD = 0.50
SCROBBLE_TIME_CAP_MS = 4 * 60 * 1000
SCROBBLE_MIN_PLAY_MS = 30 * 1000

_lock = threading.Lock()

_track_id = ""
_track_name = ""
_artist = ""
_duration_ms = 0
_accumulated_ms = 0.0
_playing = False
_last_tick = 0.0
_scrobbled = False

DRIFT_THRESHOLD_MS = 2000


def update(track_id, track_name, artist, duration_ms, is_playing, api_progress_ms):
    """Called every poll cycle from both Spotify and Cider controllers."""
    global _track_id, _track_name, _artist, _duration_ms
    global _accumulated_ms, _playing, _last_tick, _scrobbled

    now = time.monotonic()

    with _lock:
        if track_id != _track_id:
            _track_id = track_id
            _track_name = track_name
            _artist = artist
            _duration_ms = duration_ms
            _accumulated_ms = float(api_progress_ms) if api_progress_ms else 0.0
            _playing = is_playing
            _last_tick = now
            _scrobbled = False
            return

        _track_name = track_name
        _artist = artist
        _duration_ms = duration_ms

        if _playing and _last_tick > 0:
            elapsed = (now - _last_tick) * 1000.0
            _accumulated_ms += elapsed

        _last_tick = now
        _playing = is_playing

        if api_progress_ms is not None and abs(_accumulated_ms - api_progress_ms) > DRIFT_THRESHOLD_MS:
            _accumulated_ms = float(api_progress_ms)

        if not _track_id or _duration_ms <= 0 or _scrobbled:
            return

        if _accumulated_ms < SCROBBLE_MIN_PLAY_MS:
            return

        hit_pct = _accumulated_ms / _duration_ms >= SCROBBLE_THRESHOLD
        hit_time = _accumulated_ms >= SCROBBLE_TIME_CAP_MS

        if hit_pct or hit_time:
            _scrobbled = True
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{ts} | {_artist} | {_track_name}\n"
            try:
                os.makedirs(os.path.dirname(SCROBBLE_LOG), exist_ok=True)
                with open(SCROBBLE_LOG, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as e:
                print(f"Scrobble log error: {e}")


def reset():
    """Force-reset state (e.g. on source switch)."""
    global _track_id, _scrobbled, _accumulated_ms, _last_tick
    with _lock:
        _track_id = ""
        _scrobbled = False
        _accumulated_ms = 0.0
        _last_tick = 0.0
