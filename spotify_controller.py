import threading
import time
import traceback
from curl_cffi import requests as cffi_requests
from spotipy.exceptions import SpotifyException
from album_cache import cache_art, get_dominant_color
from scrobbler import update as scrobbler_update, reset as scrobbler_reset
from spotify_auth import get_web_player_tokens, start_wp_token_refresh

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
    "dominant_color": "#1a1a2e",
    "progress_ms": 0,
    "duration_ms": 0,
    "is_playing": False,
    "volume": 0,
    "device": "",
    "track_id": "",
    "canvas_url": None,
    "server_time": 0,
    "track_changed_at": 0,
}
_lock = threading.Lock()
_previous_track_id = None
_canvas_cache = {}
_canvas_lock = threading.Lock()
_active_device_id = None
_sp_ref = None

_last_play_cmd = 0.0
_last_pause_cmd = 0.0
_last_skip_cmd = 0.0
_CMD_COOLDOWN = 1.0
_SKIP_COOLDOWN = 0.5

POLL_INTERVAL = 5  # Reduced from 1 to avoid rate limits (was 60 req/min, now 12)
_poll_counter = 0
_force_poll_timer = None
_force_poll_timer_lock = threading.Lock()

CANVAS_HASH = "575138ab27cd5c1b3e54da54d0a7cc8d85485402de26340c2145f0f6bb5e7a9f"
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"


def _check_rate_limited():
    """True if we should not make any Spotify API calls."""
    with _rate_limit_lock:
        return time.time() < _rate_limited_until


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
            if track_id in _canvas_cache:
                _apply_canvas(track_id)
                return
        with _lock:
            if _previous_track_id != track_id:
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
            elif status == 403:
                print("Canvas 403 for " + track_id + " – forcing token refresh and retrying...")
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

        with _lock:
            if _current_data.get("track_id") != track_id:
                return
        with _canvas_lock:
            _canvas_cache[track_id] = cdn_url
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


def _grab_device(sp):
    global _active_device_id
    if _check_rate_limited():
        return _active_device_id
    try:
        devs = sp.devices().get("devices", [])
        for d in devs:
            if d.get("is_active"):
                _active_device_id = d["id"]
                print("Active device: " + d.get("name", "?") + " (" + _active_device_id[:12] + "...)")
                return _active_device_id
        if devs:
            target = devs[0]["id"]
            sp.transfer_playback(target, force_play=False)
            _active_device_id = target
            print("Transferred playback to: " + devs[0].get("name", target[:12]))
            return _active_device_id
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


def _do_poll(sp):
    global _previous_track_id, _active_device_id, _poll_counter
    if _check_rate_limited():
        return
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

            device = pb.get("device", {})
            device_name = device.get("name", "Unknown")
            volume = device.get("volume_percent")
            if volume is None:
                volume = 0
            if device.get("id"):
                _active_device_id = device["id"]

            track_changed = track_id != _previous_track_id

            if track_changed:
                scrobbler_reset()
                _previous_track_id = track_id
                local_art = cache_art(art_url) if art_url else ""
                color = get_dominant_color(art_url) if art_url else "#1a1a2e"
                track_changed_at = now
                with _lock:
                    _current_data["canvas_url"] = None
                _fetch_canvas_graphql(track_id)
            else:
                with _lock:
                    prev_local = _current_data.get("album_art_local", "")
                    prev_color = _current_data.get("dominant_color", "#1a1a2e")
                    track_changed_at = _current_data.get("track_changed_at", 0)
                if prev_local:
                    local_art = prev_local.replace("/art/", "")
                else:
                    local_art = cache_art(art_url) if art_url else ""
                color = prev_color if prev_color != "#1a1a2e" else (get_dominant_color(art_url) if art_url else "#1a1a2e")

            scrobbler_update(track_id, track_name, artists, duration, is_playing, progress)

            if my_id != _poll_counter:
                return
            with _lock:
                _current_data.update({
                    "artist": artists,
                    "track": track_name,
                    "album": album_name,
                    "album_art_url": art_url,
                    "album_art_local": ("/art/" + local_art) if local_art else "",
                    "dominant_color": color,
                    "progress_ms": progress,
                    "duration_ms": duration,
                    "is_playing": is_playing,
                    "volume": volume,
                    "device": device_name,
                    "track_id": track_id,
                    "server_time": now,
                    "track_changed_at": track_changed_at,
                })
        else:
            if my_id != _poll_counter:
                return
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
    except Exception as e:
        if _handle_429(e):
            return
        traceback.print_exc()


def _poll_loop(sp):
    while True:
        if _check_rate_limited():
            with _rate_limit_lock:
                remaining = _rate_limited_until - time.time()
            wait = min(60, max(1, remaining))
            if wait > 0:
                time.sleep(wait)
            continue
        _do_poll(sp)
        time.sleep(POLL_INTERVAL)


def start_polling(sp):
    global _sp_ref
    _sp_ref = sp
    _grab_device(sp)
    t = threading.Thread(target=_poll_loop, args=(sp,), daemon=True)
    t.start()


def get_current_data():
    with _lock:
        d = dict(_current_data)
    with _rate_limit_lock:
        d["rate_limited_until"] = _rate_limited_until if _rate_limited_until > time.time() else 0
    tid = d.get("track_id", "")
    if tid:
        with _canvas_lock:
            cdn = _canvas_cache.get(tid)
        if cdn:
            d["canvas_url"] = _canvas_proxy_url(tid)
        else:
            d["canvas_url"] = None
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
        return True
    except Exception as e:
        print("Seek failed: " + str(e))
        return False


def set_volume(sp, volume_percent):
    vol = max(0, min(100, volume_percent))
    dev = _active_device_id
    try:
        if dev:
            sp.volume(vol, device_id=dev)
        else:
            sp.volume(vol)
        return True
    except Exception as e:
        print("Volume failed: " + str(e))
        return False


def fetch_canvas_for_external(track_id, callback):
    """Fetch the Spotify Canvas for *any* track_id (used by cider_controller
    for cross-source Canvas).  Runs asynchronously; calls
    callback(track_id, proxy_url_or_None) when done."""
    def _work():
        with _canvas_lock:
            cached = _canvas_cache.get(track_id)
            if cached is not None:
                callback(track_id, _canvas_proxy_url(track_id) if cached else None)
                return
            if track_id in _canvas_cache:
                callback(track_id, None)
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
            elif status == 403:
                print(f"[Canvas External] 403 for {track_id}, refreshing token...")
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

        with _canvas_lock:
            _canvas_cache[track_id] = cdn_url

        proxy = _canvas_proxy_url(track_id) if cdn_url else None
        callback(track_id, proxy)

    threading.Thread(target=_work, daemon=True).start()
