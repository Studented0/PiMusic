"""Source manager – orchestrates switching between Spotify and Cider,
auto-detects which player is active, and provides a unified state API."""

import threading
import time

import cider_controller
import spotify_controller

_active_source = "spotify"
_lock = threading.Lock()
_manual_override_until = 0.0
_detection_active = False

DETECT_INTERVAL = 5
MANUAL_LOCK_DURATION = 30


def get_active_source():
    with _lock:
        return _active_source


def set_source(source):
    """Manual override – locks auto-detection for MANUAL_LOCK_DURATION seconds."""
    global _active_source, _manual_override_until
    if source not in ("spotify", "cider"):
        return False
    with _lock:
        _active_source = source
        _manual_override_until = time.time() + MANUAL_LOCK_DURATION
    print(f"Source manually set to: {source} (locked for {MANUAL_LOCK_DURATION}s)")
    return True


def _detect_loop():
    global _active_source
    while _detection_active:
        time.sleep(DETECT_INTERVAL)
        with _lock:
            if time.time() < _manual_override_until:
                continue

        spotify_playing = spotify_controller.is_playing_active()
        cider_playing = cider_controller.is_playing_active()

        with _lock:
            if spotify_playing and cider_playing:
                pass  # sticky – keep current
            elif cider_playing and not spotify_playing:
                if _active_source != "cider":
                    _active_source = "cider"
                    print("Auto-detected source: cider")
            elif spotify_playing and not cider_playing:
                if _active_source != "spotify":
                    _active_source = "spotify"
                    print("Auto-detected source: spotify")
            # neither playing: keep last


def start_detection():
    global _detection_active
    if _detection_active:
        return
    _detection_active = True
    t = threading.Thread(target=_detect_loop, daemon=True)
    t.start()
    print("Source auto-detection started")


def stop_detection():
    global _detection_active
    _detection_active = False


def get_unified_state(cpu_override_image=False):
    """Return the playback state from the active source, enriched with
    source identifier and visual_type.  If cpu_override_image is True,
    downgrade visual_type to 'image' regardless of video availability."""
    source = get_active_source()

    if source == "cider":
        data = cider_controller.get_current_data()
    else:
        data = spotify_controller.get_current_data()

    data["source"] = source

    if "visual_type" not in data:
        if data.get("canvas_url"):
            data["visual_type"] = "canvas_video"
        else:
            data["visual_type"] = "image"

    if cpu_override_image:
        data["visual_type"] = "image"
        data["cpu_throttled"] = True
    else:
        data["cpu_throttled"] = False

    return data


def dispatch_command(action, sp=None, **kwargs):
    """Route a playback command to the active source's controller.
    For Spotify commands, `sp` (the spotipy client) is required."""
    source = get_active_source()

    if source == "cider":
        handlers = {
            "play": cider_controller.play,
            "pause": cider_controller.pause,
            "next": cider_controller.next_track,
            "previous": cider_controller.previous_track,
            "seek": lambda: cider_controller.seek_track(kwargs.get("position_ms", 0)),
            "volume": lambda: cider_controller.set_volume(kwargs.get("volume", 50)),
        }
    else:
        if not sp:
            return False
        handlers = {
            "play": lambda: spotify_controller.play(sp),
            "pause": lambda: spotify_controller.pause(sp),
            "next": lambda: spotify_controller.next_track(sp),
            "previous": lambda: spotify_controller.previous_track(sp),
            "seek": lambda: spotify_controller.seek_track(sp, kwargs.get("position_ms", 0)),
            "volume": lambda: spotify_controller.set_volume(sp, kwargs.get("volume", 50)),
        }

    handler = handlers.get(action)
    if handler:
        return handler()
    return False
