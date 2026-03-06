import os
import time
from datetime import datetime

SCROBBLE_LOG = os.path.expanduser("~/pimusic/scrobbles.log")
SCROBBLE_THRESHOLD = 0.50
SCROBBLE_TIME_CAP_MS = 4 * 60 * 1000
SCROBBLE_MIN_PLAY_MS = 30 * 1000
_SCROBBLE_CHECK_INTERVAL = 5.0

_last_scrobbled_id = None
_last_check_time = 0.0


def maybe_scrobble(track_id: str, track_name: str, artist: str, progress_ms: int, duration_ms: int):
    """Log once per track when: played >= 30s AND (>= 50% OR >= 4 min). Throttled to every 5s."""
    global _last_scrobbled_id, _last_check_time

    if not track_id or duration_ms <= 0:
        return

    if track_id == _last_scrobbled_id:
        return

    now = time.monotonic()
    if now - _last_check_time < _SCROBBLE_CHECK_INTERVAL:
        return
    _last_check_time = now

    if progress_ms < SCROBBLE_MIN_PLAY_MS:
        return

    hit_pct = progress_ms / duration_ms >= SCROBBLE_THRESHOLD
    hit_time = progress_ms >= SCROBBLE_TIME_CAP_MS

    if hit_pct or hit_time:
        _last_scrobbled_id = track_id
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {artist} | {track_name}\n"
        os.makedirs(os.path.dirname(SCROBBLE_LOG), exist_ok=True)
        with open(SCROBBLE_LOG, "a", encoding="utf-8") as f:
            f.write(line)


def reset_scrobble():
    global _last_scrobbled_id
    _last_scrobbled_id = None
