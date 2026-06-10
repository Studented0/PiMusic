import threading
import time
import traceback
from curl_cffi import requests as cffi_requests
from spotipy.exceptions import SpotifyException
from album_cache import cache_art, get_cached_bg, get_dominant_color
from scrobbler import update as scrobbler_update, reset as scrobbler_reset
from spotify_auth import get_web_player_tokens, start_wp_token_refresh


def _scrobbler_active():
    """Only the active source's poller may drive the scrobbler, otherwise the
    idle poller resets/advances the other source's scrobble progress."""
    import source_manager  # late import -- source_manager imports this module
    return source_manager.get_active_source() == "spotify"

# Rate limit: when 429 received, back off completely. Retry-After can be 76k+ seconds.
_rate_limited_until = 0.0
_rate_limit_lock = threading.Lock()
RATE_LIMIT_MIN_BACKOFF = 300  # seconds if Retry-After missing (5 min - be conservative)

_current_data = {
    "artist": "",
    "track": "",
    "album": "",
    "album_art_url": "",
    "album_art_local": "",
    "bg_art_local": "",
    "dominant_color": "#1a1a2e",
    "progress_ms": 0,
    "duration_ms": 0,
    "is_playing": False,
    "volume": 0,
    "device": "",
    "track_id": "",
    "canvas_url": None,
    "shuffle_state": False,
    "repeat_state": "off",
    "is_saved": False,
    "server_time": 0,
    "track_changed_at": 0,
}
_lock = threading.Lock()
_previous_track_id = None
_canvas_cache = {}
_canvas_lock = threading.Lock()
_CANVAS_CACHE_MAX_ENTRIES = 256


_canvas_prefetch_hook = None


def set_canvas_prefetch_hook(fn):
    """Register a callable(track_id) invoked whenever a canvas CDN URL is
    resolved, so the server can pre-download the MP4 bytes in the background
    instead of blocking the Pi's first /api/canvas request."""
    global _canvas_prefetch_hook
    _canvas_prefetch_hook = fn


def _canvas_cache_store(track_id: str, cdn_url):
    """Bounded FIFO-style insert (dict preserves insertion order in Py3.7+)."""
    with _canvas_lock:
        if len(_canvas_cache) >= _CANVAS_CACHE_MAX_ENTRIES and track_id not in _canvas_cache:
            _canvas_cache.pop(next(iter(_canvas_cache)))
        _canvas_cache[track_id] = cdn_url
    if cdn_url and _canvas_prefetch_hook:
        try:
            _canvas_prefetch_hook(track_id)
        except Exception as e:
            print(f"Canvas prefetch hook error: {e}")
_active_device_id = None
_sp_ref = None

_art_inflight = set()
_art_inflight_lock = threading.Lock()

_saved_inflight = set()
_saved_inflight_lock = threading.Lock()

_last_play_cmd = 0.0
_last_pause_cmd = 0.0
_last_skip_cmd = 0.0
_CMD_COOLDOWN = 1.0
_SKIP_COOLDOWN = 0.5

POLL_INTERVAL = 5  # Reduced from 1 to avoid rate limits (was 60 req/min, now 12)
POLL_INTERVAL_NEAR_END = 1  # Fast poll inside the last NEAR_END_MS of a track
NEAR_END_MS = 15_000
_poll_counter = 0
_force_poll_timer = None
_force_poll_timer_lock = threading.Lock()

CANVAS_HASH = "575138ab27cd5c1b3e54da54d0a7cc8d85485402de26340c2145f0f6bb5e7a9f"
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"

# Idle screensaver canvases — played as the background when no music is active.
# Add more track IDs here to rotate through them later.
IDLE_CANVAS_TRACK_IDS = [
    "20fAoPjfYltmd3K3bO7gbt",  # Stick Talk - Future
]
_idle_prewarm_lock = threading.Lock()
_idle_prewarm_in_progress = False


def _check_rate_limited():
    """True if we should not make any Spotify API calls."""
    with _rate_limit_lock:
        return time.time() < _rate_limited_until


def check_rate_limited():
    """Public wrapper for other modules (library endpoints)."""
    return _check_rate_limited()


def _set_rate_limited(retry_after_sec=None):
    """Enter backoff. Respect Retry-After or use minimum."""
    global _rate_limited_until
    with _rate_limit_lock:
        sec = RATE_LIMIT_MIN_BACKOFF
        if retry_after_sec is not None:
            try:
                sec = max(int(retry_after_sec), 60)
            except (ValueError, TypeError):
                pass
        _rate_limited_until = time.time() + sec
        hrs = sec / 3600
        print(f"RATE LIMITED: backing off for {sec}s ({hrs:.1f}h). No API calls until then.")


def _handle_429(e):
    """If 429, extract Retry-After and set backoff. Return True if handled."""
    if isinstance(e, SpotifyException) and getattr(e, "http_status", None) == 429:
        retry_after = None
        if hasattr(e, "headers") and e.headers:
            for key in ("retry-after", "Retry-After"):
                if key in e.headers:
                    try:
                        retry_after = int(e.headers[key])
                        break
                    except (ValueError, TypeError):
                        pass
        _set_rate_limited(retry_after)
        return True
    if "429" in str(e):
        _set_rate_limited(None)
        return True
    return False


def handle_429(e):
    """Public wrapper for other modules (library endpoints)."""
    return _handle_429(e)


# After a command, Spotify's API can return pre-command state for a second
# or two. Polls inside this grace window must not overwrite the optimistic
# fields, or the UI flickers back to the old state ("revert then correct").
_optimistic_until = 0.0
_OPTIMISTIC_GRACE_SEC = 2.5
_OPTIMISTIC_FIELDS = (
    "is_playing", "progress_ms", "volume",
    "shuffle_state", "repeat_state", "server_time",
)


def _mark_optimistic():
    global _optimistic_until
    _optimistic_until = time.time() + _OPTIMISTIC_GRACE_SEC


def _apply_local_playback(is_playing=None, progress_ms=None, volume=None):
    """Optimistically update local state right after a successful command so
    /api/state reflects it immediately instead of waiting for the next poll
    (the main cause of 'volume is janky' and 'slow to update')."""
    now = time.time()
    _mark_optimistic()
    with _lock:
        if _current_data.get("track_id"):
            cur = _current_data.get("progress_ms", 0) or 0
            sampled = _current_data.get("server_time") or now
            if _current_data.get("is_playing"):
                elapsed = max(0.0, now - sampled) * 1000.0
                cur = cur + elapsed
                dur = _current_data.get("duration_ms", 0) or 0
                if dur:
                    cur = min(cur, dur)
            _current_data["progress_ms"] = int(progress_ms if progress_ms is not None else cur)
            _current_data["server_time"] = now
            if is_playing is not None:
                _current_data["is_playing"] = is_playing
        if volume is not None:
            _current_data["volume"] = max(0, min(100, int(volume)))


def _canvas_proxy_url(track_id):
    """Single source of truth: convert track_id to the proxy path the frontend uses."""
    return "/api/canvas/" + track_id + ".mp4"


def _canvas_graphql_request(track_id, bearer, client_token):
    """Single GraphQL request for canvas. Returns (status_code, cdn_url_or_None)."""
    resp = cffi_requests.post(PATHFINDER_URL, json={
        "operationName": "canvas",
        "variables": {"trackUri": "spotify:track:" + track_id},
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": CANVAS_HASH}
        },
    }, headers={
        "Authorization": "Bearer " + bearer,
        "client-token": client_token,
        "Content-Type": "application/json",
    }, impersonate="chrome131", timeout=10)

    if resp.status_code == 200:
        data = resp.json() or {}
        track_union = (data.get("data") or {}).get("trackUnion") or {}
        canvas = track_union.get("canvas") or {}
        return resp.status_code, canvas.get("url", "") or None
    return resp.status_code, None


def _fetch_canvas_graphql(track_id):
    """Fetch canvas CDN URL via Spotify's internal GraphQL Pathfinder API.
    On 403, triggers a background token refresh and retries once."""
    def _work():
        if not track_id or _check_rate_limited():
            return
        with _canvas_lock:
            cached_hit = track_id in _canvas_cache
        if cached_hit:
            _apply_canvas(track_id)
            return

        cdn_url = None
        try:
            bearer, client_token = get_web_player_tokens()
            if not bearer:
                print("Canvas skip: no bearer token yet")
                return

            status, cdn_url = _canvas_graphql_request(track_id, bearer, client_token)

            if status == 200:
                if cdn_url:
                    print("Canvas found for " + track_id + ": " + cdn_url[:60] + "...")
                else:
                    print("Canvas: track " + track_id + " has no canvas")
            elif status in (401, 403):
                print("Canvas " + str(status) + " for " + track_id + " – forcing token refresh and retrying...")
                start_wp_token_refresh()
                time.sleep(12)
                bearer2, client_token2 = get_web_player_tokens()
                if bearer2 and bearer2 != bearer:
                    status2, cdn_url = _canvas_graphql_request(track_id, bearer2, client_token2)
                    if status2 == 200 and cdn_url:
                        print("Canvas retry succeeded for " + track_id)
                    elif status2 == 200:
                        print("Canvas: track " + track_id + " has no canvas (after retry)")
                    else:
                        print("Canvas retry still " + str(status2) + " for " + track_id)
                else:
                    print("Canvas: token refresh did not yield new token")
            else:
                print("Canvas GraphQL " + str(status) + " for " + track_id)

        except Exception as e:
            print("Canvas error for " + track_id + ": " + str(e))

        # Always cache the result (even if the active track moved on, future
        # plays of this track and the idle screensaver still need it).
        _canvas_cache_store(track_id, cdn_url)
        with _lock:
            still_current = _current_data.get("track_id") == track_id
        if still_current:
            _apply_canvas(track_id)

    t = threading.Thread(target=_work, daemon=True)
    t.start()


def _apply_canvas(track_id):
    """Write the proxy URL (or None) into _current_data. Only writer for canvas_url."""
    with _canvas_lock:
        cdn_url = _canvas_cache.get(track_id)
    with _lock:
        if _current_data.get("track_id") == track_id:
            if cdn_url:
                proxy = _canvas_proxy_url(track_id)
                _current_data["canvas_url"] = proxy
                print("Canvas state set for " + track_id + " -> " + proxy)
            else:
                _current_data["canvas_url"] = None


def get_canvas_cdn_url(track_id):
    """Return the raw CDN URL for a track's canvas, or None."""
    with _canvas_lock:
        return _canvas_cache.get(track_id)


def prewarm_idle_canvas():
    """Pre-fetch idle screensaver canvas(es) so they're ready when needed.
    Safe to call repeatedly; debounced internally."""
    global _idle_prewarm_in_progress
    with _idle_prewarm_lock:
        if _idle_prewarm_in_progress:
            return
        _idle_prewarm_in_progress = True

    def _work():
        global _idle_prewarm_in_progress
        try:
            # Give token capture a chance to settle before first fetch.
            time.sleep(8)
            for tid in IDLE_CANVAS_TRACK_IDS:
                if not tid:
                    continue
                with _canvas_lock:
                    already = tid in _canvas_cache
                if already:
                    continue
                _fetch_canvas_graphql(tid)
                time.sleep(3)
        finally:
            with _idle_prewarm_lock:
                _idle_prewarm_in_progress = False

    threading.Thread(target=_work, daemon=True).start()


def get_idle_canvas():
    """Return (track_id, cdn_url) for the idle screensaver canvas, or (None, None).
    Currently returns the first cached entry; later we can rotate. If nothing is
    cached, kicks off a prewarm so it'll be available on a subsequent call."""
    for tid in IDLE_CANVAS_TRACK_IDS:
        if not tid:
            continue
        with _canvas_lock:
            cdn = _canvas_cache.get(tid)
        if cdn:
            return tid, cdn
    prewarm_idle_canvas()
    return None, None


def _grab_device(sp):
    global _active_device_id
    if _check_rate_limited():
        with _lock:
            return _active_device_id
    try:
        devs = sp.devices().get("devices", [])
        for d in devs:
            if d.get("is_active"):
                did = d["id"]
                with _lock:
                    _active_device_id = did
                print("Active device: " + d.get("name", "?") + " (" + did[:12] + "...)")
                return did
        if devs:
            target = devs[0]["id"]
            sp.transfer_playback(target, force_play=False)
            with _lock:
                _active_device_id = target
            print("Transferred playback to: " + devs[0].get("name", target[:12]))
            return target
        print("No Spotify devices found")
    except SpotifyException as e:
        if getattr(e, "http_status", None) == 429:
            _handle_429(e)
        else:
            print("Device grab error: " + str(e))
    except Exception as e:
        if "429" in str(e):
            _set_rate_limited(None)
        else:
            print("Device grab error: " + str(e))
    with _lock:
        return _active_device_id


def force_poll():
    """Schedule a single debounced poll. Rapid skips reset the timer."""
    global _force_poll_timer
    if not _sp_ref or _check_rate_limited():
        return
    with _force_poll_timer_lock:
        if _force_poll_timer:
            _force_poll_timer.cancel()
        def _run():
            global _force_poll_timer
            with _force_poll_timer_lock:
                _force_poll_timer = None
            threading.Thread(target=_do_poll, args=(_sp_ref,), daemon=True).start()
        _force_poll_timer = threading.Timer(0.25, _run)
        _force_poll_timer.start()


def _spawn_art_cache(track_id, art_url):
    """Download art + compute dominant color off the poll thread.
    Only writes back to _current_data if the active track_id is still the same.
    """
    if not track_id or not art_url:
        return
    with _art_inflight_lock:
        if track_id in _art_inflight:
            return
        _art_inflight.add(track_id)

    def _work():
        local = None
        bg_local = None
        color = None
        try:
            local = cache_art(art_url)
            if local:
                bg_local = get_cached_bg(art_url)
                color = get_dominant_color(art_url)
        except Exception:
            pass
        finally:
            with _art_inflight_lock:
                _art_inflight.discard(track_id)
        if not local:
            return
        with _lock:
            if _current_data.get("track_id") != track_id:
                return
            _current_data["album_art_local"] = "/art/" + local
            _current_data["bg_art_local"] = ("/art/" + bg_local) if bg_local else ""
            if color and color != "#1a1a2e":
                _current_data["dominant_color"] = color

    threading.Thread(target=_work, daemon=True).start()


def _spawn_saved_check(sp, track_id):
    """Check whether the track is in the user's Liked Songs (async, once per
    track change). Writes is_saved into _current_data if still current."""
    if not track_id or _check_rate_limited():
        return
    with _saved_inflight_lock:
        if track_id in _saved_inflight:
            return
        _saved_inflight.add(track_id)

    def _work():
        saved = None
        try:
            res = sp.current_user_saved_tracks_contains([track_id])
            if isinstance(res, list) and res:
                saved = bool(res[0])
        except Exception as e:
            if not _handle_429(e):
                print(f"Saved check failed for {track_id}: {e}")
        finally:
            with _saved_inflight_lock:
                _saved_inflight.discard(track_id)
        if saved is None:
            return
        with _lock:
            if _current_data.get("track_id") == track_id:
                _current_data["is_saved"] = saved

    threading.Thread(target=_work, daemon=True).start()


def _do_poll(sp):
    global _previous_track_id, _active_device_id, _poll_counter
    if _check_rate_limited():
        return
    with _lock:
        _poll_counter += 1
        my_id = _poll_counter
    try:
        pb = sp.current_playback()
        now = time.time()

        if pb and pb.get("item"):
            item = pb["item"]
            track_id = item.get("id", "")
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            track_name = item.get("name", "")
            album_name = item.get("album", {}).get("name", "")
            images = item.get("album", {}).get("images", [])
            art_url = images[0]["url"] if images else ""

            is_playing = pb.get("is_playing", False)
            progress = pb.get("progress_ms", 0)
            duration = item.get("duration_ms", 0)
            shuffle_state = bool(pb.get("shuffle_state", False))
            repeat_state = pb.get("repeat_state") or "off"

            device = pb.get("device", {})
            device_name = device.get("name", "Unknown")
            volume = device.get("volume_percent")
            if volume is None:
                volume = 0
            if device.get("id"):
                with _lock:
                    _active_device_id = device["id"]

            with _lock:
                prev_tid = _previous_track_id
                prev_local = _current_data.get("album_art_local", "")
                prev_bg = _current_data.get("bg_art_local", "")
                prev_color = _current_data.get("dominant_color", "#1a1a2e")
                prev_changed_at = _current_data.get("track_changed_at", 0)

            track_changed = track_id != prev_tid

            if track_changed:
                if _scrobbler_active():
                    scrobbler_reset()
                local_art_path = ""
                bg_art_path = ""
                color = "#1a1a2e"
                track_changed_at = now
                with _lock:
                    _current_data["canvas_url"] = None
                    _current_data["is_saved"] = False
                _fetch_canvas_graphql(track_id)
                _spawn_saved_check(sp, track_id)
            else:
                local_art_path = prev_local
                bg_art_path = prev_bg
                color = prev_color
                track_changed_at = prev_changed_at

            if _scrobbler_active():
                scrobbler_update(track_id, track_name, artists, duration, is_playing, progress)

            update = {
                "artist": artists,
                "track": track_name,
                "album": album_name,
                "album_art_url": art_url,
                "album_art_local": local_art_path,
                "bg_art_local": bg_art_path,
                "dominant_color": color,
                "progress_ms": progress,
                "duration_ms": duration,
                "is_playing": is_playing,
                "volume": volume,
                "device": device_name,
                "track_id": track_id,
                "shuffle_state": shuffle_state,
                "repeat_state": repeat_state,
                "server_time": now,
                "track_changed_at": track_changed_at,
            }
            # Inside the post-command grace window, a poll may carry stale
            # pre-command values -- keep the optimistic fields instead.
            # A track change is always authoritative.
            if not track_changed and time.time() < _optimistic_until:
                for key in _OPTIMISTIC_FIELDS:
                    update.pop(key, None)

            with _lock:
                if my_id != _poll_counter:
                    return
                _current_data.update(update)
                if track_changed:
                    _previous_track_id = track_id

            if art_url and (track_changed or not local_art_path):
                _spawn_art_cache(track_id, art_url)
        else:
            with _lock:
                if my_id != _poll_counter:
                    return
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
    except Exception as e:
        if _handle_429(e):
            return
        traceback.print_exc()


def _poll_loop(sp):
    # Device grab happens here (not in start_polling) so server startup
    # doesn't block on a Spotify API round-trip.
    _grab_device(sp)
    while True:
        if _check_rate_limited():
            with _rate_limit_lock:
                remaining = _rate_limited_until - time.time()
            wait = min(60, max(1, remaining))
            if wait > 0:
                time.sleep(wait)
            continue
        _do_poll(sp)
        with _lock:
            is_playing = _current_data.get("is_playing", False)
            duration = _current_data.get("duration_ms", 0) or 0
            progress = _current_data.get("progress_ms", 0) or 0
        near_end = is_playing and duration > 0 and (duration - progress) < NEAR_END_MS
        time.sleep(POLL_INTERVAL_NEAR_END if near_end else POLL_INTERVAL)


def start_polling(sp):
    global _sp_ref
    _sp_ref = sp
    t = threading.Thread(target=_poll_loop, args=(sp,), daemon=True)
    t.start()


def get_current_data():
    with _lock:
        d = dict(_current_data)
    # Extrapolate progress between polls so /api/state is never up to
    # POLL_INTERVAL seconds stale (root cause of the pause snap-to-0 bug:
    # the frontend resynced to old progress values).
    if d.get("is_playing") and (d.get("duration_ms") or 0) > 0 and d.get("server_time"):
        now = time.time()
        elapsed_ms = (now - d["server_time"]) * 1000.0
        if 0 < elapsed_ms < 60_000:
            d["progress_ms"] = int(min((d.get("progress_ms") or 0) + elapsed_ms, d["duration_ms"]))
        d["server_time"] = now
    with _rate_limit_lock:
        d["rate_limited_until"] = _rate_limited_until if _rate_limited_until > time.time() else 0
    tid = d.get("track_id", "")
    if tid:
        with _canvas_lock:
            cdn = _canvas_cache.get(tid)
        if cdn:
            d["canvas_url"] = _canvas_proxy_url(tid)
            d["canvas_cdn_url"] = cdn
        else:
            d["canvas_url"] = None
            d["canvas_cdn_url"] = None
    else:
        d["canvas_cdn_url"] = None
    d["visual_type"] = "canvas_video" if d.get("canvas_url") else "image"
    return d


def is_playing_active():
    """Quick check for source manager – is Spotify currently playing?"""
    with _lock:
        return _current_data.get("is_playing", False)


def play(sp):
    global _last_play_cmd
    if _check_rate_limited():
        return False
    now = time.time()
    if now - _last_play_cmd < _CMD_COOLDOWN:
        return True
    if _check_rate_limited():
        return False
    _last_play_cmd = now
    try:
        sp.start_playback()
        _apply_local_playback(is_playing=True)
        force_poll()
        return True
    except Exception as e:
        if _handle_429(e):
            return False
        err = str(e)
        if "403" in err and "Restriction" in err:
            return True
        print("Play failed: " + err)
        if "NO_ACTIVE_DEVICE" in err or "Not found" in err.lower():
            dev = _grab_device(sp)
            if dev:
                try:
                    sp.start_playback(device_id=dev)
                    _apply_local_playback(is_playing=True)
                    force_poll()
                    return True
                except Exception as e2:
                    print("Play retry failed: " + str(e2))
        return False


def pause(sp):
    global _last_pause_cmd
    if _check_rate_limited():
        return False
    now = time.time()
    if now - _last_pause_cmd < _CMD_COOLDOWN:
        return True
    if _check_rate_limited():
        return False
    _last_pause_cmd = now
    try:
        sp.pause_playback()
        _apply_local_playback(is_playing=False)
        force_poll()
        return True
    except Exception as e:
        if _handle_429(e):
            return False
        err = str(e)
        if "403" in err and "Restriction" in err:
            return True
        print("Pause failed: " + err)
        if "NO_ACTIVE_DEVICE" in err or "Not found" in err.lower():
            dev = _grab_device(sp)
            if dev:
                try:
                    sp.pause_playback(device_id=dev)
                    _apply_local_playback(is_playing=False)
                    force_poll()
                    return True
                except Exception as e2:
                    print("Pause retry failed: " + str(e2))
        return False


def next_track(sp):
    global _last_skip_cmd
    if _check_rate_limited():
        return False
    now = time.time()
    if now - _last_skip_cmd < _SKIP_COOLDOWN:
        return False
    if _check_rate_limited():
        return False
    _last_skip_cmd = now
    try:
        sp.next_track()
        ok = True
    except Exception as e:
        if _handle_429(e):
            return False
        err = str(e)
        print("Next failed: " + err)
        ok = False
        if "403" in err or "NO_ACTIVE_DEVICE" in err:
            dev = _grab_device(sp)
            if dev:
                try:
                    sp.next_track(device_id=dev)
                    ok = True
                except Exception as e2:
                    print("Next retry failed: " + str(e2))
    if ok:
        force_poll()
    return ok


def previous_track(sp):
    global _last_skip_cmd
    if _check_rate_limited():
        return False
    now = time.time()
    if now - _last_skip_cmd < _SKIP_COOLDOWN:
        return False
    if _check_rate_limited():
        return False
    _last_skip_cmd = now
    try:
        sp.previous_track()
        ok = True
    except Exception as e:
        if _handle_429(e):
            return False
        err = str(e)
        print("Previous failed: " + err)
        ok = False
        if "403" in err or "NO_ACTIVE_DEVICE" in err:
            dev = _grab_device(sp)
            if dev:
                try:
                    sp.previous_track(device_id=dev)
                    ok = True
                except Exception as e2:
                    print("Previous retry failed: " + str(e2))
    if ok:
        force_poll()
    return ok


def seek_track(sp, position_ms):
    try:
        sp.seek_track(position_ms)
        _apply_local_playback(progress_ms=int(position_ms))
        force_poll()
        return True
    except Exception as e:
        print("Seek failed: " + str(e))
        return False


def set_volume(sp, volume_percent):
    vol = max(0, min(100, volume_percent))
    with _lock:
        dev = _active_device_id
    try:
        if dev:
            sp.volume(vol, device_id=dev)
        else:
            sp.volume(vol)
        # Reflect immediately -- waiting for the next poll made the slider
        # snap back to the old value ("volume is janky" known issue).
        _apply_local_playback(volume=vol)
        return True
    except Exception as e:
        print("Volume failed: " + str(e))
        return False


def set_shuffle(sp, state):
    """Set shuffle on/off. Optimistic local update + force-poll to confirm."""
    if _check_rate_limited():
        return False
    state = bool(state)
    with _lock:
        dev = _active_device_id
    try:
        if dev:
            sp.shuffle(state, device_id=dev)
        else:
            sp.shuffle(state)
        _mark_optimistic()
        with _lock:
            _current_data["shuffle_state"] = state
        force_poll()
        return True
    except Exception as e:
        if _handle_429(e):
            return False
        print("Shuffle failed: " + str(e))
        return False


def set_repeat(sp, state):
    """Set repeat mode: 'off' | 'context' | 'track'."""
    if _check_rate_limited():
        return False
    if state not in ("off", "context", "track"):
        return False
    with _lock:
        dev = _active_device_id
    try:
        if dev:
            sp.repeat(state, device_id=dev)
        else:
            sp.repeat(state)
        _mark_optimistic()
        with _lock:
            _current_data["repeat_state"] = state
        force_poll()
        return True
    except Exception as e:
        if _handle_429(e):
            return False
        print("Repeat failed: " + str(e))
        return False


def toggle_save(sp):
    """Add/remove the current track from Liked Songs.
    Returns (ok, new_saved_state)."""
    if _check_rate_limited():
        return False, None
    with _lock:
        track_id = _current_data.get("track_id", "")
        saved = _current_data.get("is_saved", False)
    if not track_id:
        return False, None
    try:
        if saved:
            sp.current_user_saved_tracks_delete([track_id])
        else:
            sp.current_user_saved_tracks_add([track_id])
        with _lock:
            if _current_data.get("track_id") == track_id:
                _current_data["is_saved"] = not saved
        return True, (not saved)
    except Exception as e:
        if _handle_429(e):
            return False, None
        print("Like toggle failed: " + str(e))
        return False, None


def fetch_canvas_for_external(track_id, callback):
    """Fetch the Spotify Canvas for *any* track_id (used by cider_controller
    for cross-source Canvas).  Runs asynchronously; calls
    callback(track_id, proxy_url_or_None) when done."""
    def _work():
        with _canvas_lock:
            has_key = track_id in _canvas_cache
            cached = _canvas_cache.get(track_id) if has_key else None
        if has_key:
            callback(track_id, _canvas_proxy_url(track_id) if cached else None)
            return

        if _check_rate_limited() or not track_id:
            callback(track_id, None)
            return

        cdn_url = None
        try:
            bearer, client_token = get_web_player_tokens()
            if not bearer:
                print("[Canvas External] No bearer token")
                callback(track_id, None)
                return

            status, cdn_url = _canvas_graphql_request(track_id, bearer, client_token)

            if status == 200:
                if cdn_url:
                    print(f"[Canvas External] Found for {track_id}: {cdn_url[:60]}...")
                else:
                    print(f"[Canvas External] No canvas for {track_id}")
            elif status in (401, 403):
                print(f"[Canvas External] {status} for {track_id}, refreshing token...")
                start_wp_token_refresh()
                time.sleep(12)
                bearer2, ct2 = get_web_player_tokens()
                if bearer2 and bearer2 != bearer:
                    status2, cdn_url = _canvas_graphql_request(track_id, bearer2, ct2)
                    if status2 == 200 and cdn_url:
                        print(f"[Canvas External] Retry OK for {track_id}")
            else:
                print(f"[Canvas External] Status {status} for {track_id}")

        except Exception as e:
            print(f"[Canvas External] Error for {track_id}: {e}")

        _canvas_cache_store(track_id, cdn_url)

        proxy = _canvas_proxy_url(track_id) if cdn_url else None
        callback(track_id, proxy)

    threading.Thread(target=_work, daemon=True).start()
