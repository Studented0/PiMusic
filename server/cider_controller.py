"""Cider (Apple Music) controller – polls local Cider API for playback state.
When a track is playing, searches Spotify for the same track and uses
Spotify Canvas as the background visual."""

import re
import threading
import time
import traceback

import requests

from album_cache import cache_art, get_cached_bg, get_dominant_color
from scrobbler import update as scrobbler_update, reset as scrobbler_reset


def _scrobbler_active():
    """Only the active source's poller may drive the scrobbler."""
    import source_manager  # late import -- source_manager imports this module
    return source_manager.get_active_source() == "cider"

CIDER_BASE = "http://127.0.0.1:10767"
CIDER_TOKEN = ""
STOREFRONT = "us"

POLL_INTERVAL = 0.5

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
    "device": "Cider",
    "track_id": "",
    "canvas_url": None,
    "canvas_cdn_url": None,
    "visual_type": "image",
    "shuffle_state": False,
    "repeat_state": "off",
    "is_saved": False,
    "server_time": 0,
    "track_changed_at": 0,
}
_lock = threading.Lock()
_previous_track_id = None
_polling_active = False
_volume_poll_counter = 0
_VOLUME_POLL_EVERY = 4  # fetch real volume every Nth poll (~2s at 0.5s polls)

_art_inflight = set()
_art_inflight_lock = threading.Lock()

# Spotify client reference (set via set_spotify_client)
_sp_ref = None

# ── Spotify Canvas cross-search cache ────────────────────
LOOKUP_CACHE_TTL = 6 * 3600  # 6 hours
_LOOKUP_CACHE_MAX_ENTRIES = 512
_spotify_lookup_cache = {}   # key -> (spotify_track_id_or_None, timestamp)
_spotify_lookup_lock = threading.Lock()
_lookup_in_progress = set()
_lookup_progress_lock = threading.Lock()


def configure(token="", storefront="us", base_url=""):
    global CIDER_TOKEN, STOREFRONT, CIDER_BASE
    if token:
        CIDER_TOKEN = token
    if storefront:
        STOREFRONT = storefront
    if base_url:
        CIDER_BASE = base_url.rstrip("/")


def set_spotify_client(sp):
    global _sp_ref
    _sp_ref = sp


def _headers():
    h = {"Content-Type": "application/json"}
    if CIDER_TOKEN:
        h["apitoken"] = CIDER_TOKEN
    return h


def is_available():
    """Check if Cider is running (GET /api/v1/playback/active returns 204)."""
    try:
        resp = requests.get(
            CIDER_BASE + "/api/v1/playback/active",
            headers=_headers(),
            timeout=2,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def is_playing_active():
    """Quick check – is Cider currently playing something?
    Reads the poller's state instead of making an HTTP call, so the source
    auto-detect loop costs nothing (the poller refreshes every 0.5s anyway)."""
    with _lock:
        return _current_data.get("is_playing", False)


def _fetch_volume():
    """Read the real Cider volume (0-100) or None on failure."""
    try:
        resp = requests.get(
            CIDER_BASE + "/api/v1/playback/volume",
            headers=_headers(),
            timeout=2,
        )
        if resp.status_code == 200:
            vol = (resp.json() or {}).get("volume")
            if vol is None:
                return None
            vol = float(vol)
            if vol <= 1.0:
                vol *= 100.0
            return max(0, min(100, int(round(vol))))
    except Exception:
        pass
    return None


# ── Track name normalization for better Spotify search ────

def _normalize_track(name):
    name = re.sub(r"\s*\(feat\.?[^)]*\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\(ft\.?[^)]*\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\(with\s[^)]*\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\([^)]*version[^)]*\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\([^)]*remix[^)]*\)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\[[^\]]*\]", "", name)
    name = re.sub(r"\s*-\s*(feat|ft)\.?\s.*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def _cache_key(track, artist):
    return (track.lower().strip() + "|" + artist.lower().strip())


def _cache_get(key):
    with _spotify_lookup_lock:
        entry = _spotify_lookup_cache.get(key)
        if entry is None:
            return None, False
        spotify_id, ts = entry
        if time.time() - ts > LOOKUP_CACHE_TTL:
            del _spotify_lookup_cache[key]
            return None, False
        return spotify_id, True


def _cache_set(key, spotify_id):
    with _spotify_lookup_lock:
        if len(_spotify_lookup_cache) >= _LOOKUP_CACHE_MAX_ENTRIES and key not in _spotify_lookup_cache:
            _spotify_lookup_cache.pop(next(iter(_spotify_lookup_cache)))
        _spotify_lookup_cache[key] = (spotify_id, time.time())


# ── Spotify Canvas cross-search ──────────────────────────

def _search_spotify_canvas(track_name, artist, cider_track_id):
    """Search Spotify for a matching track and fetch its Canvas.
    Runs in a background thread. Writes result into _current_data."""
    key = _cache_key(track_name, artist)

    with _lookup_progress_lock:
        if key in _lookup_in_progress:
            return
        _lookup_in_progress.add(key)

    print(f"[Cider->Spotify] Starting search for: {track_name} - {artist}", flush=True)

    def _work():
        try:
            cached_id, hit = _cache_get(key)
            if hit:
                print(f"[Cider->Spotify] Cache hit: {cached_id}")
                if cached_id:
                    _apply_external_canvas(cached_id, cider_track_id)
                return

            if not _sp_ref:
                print("[Cider->Spotify] No Spotify client available for search")
                _cache_set(key, None)
                return

            spotify_id = None
            cleaned = _normalize_track(track_name)

            # Search 1: track + artist
            query = f"track:{cleaned} artist:{artist}"
            try:
                result = _sp_ref.search(q=query, type="track", limit=1)
                items = result.get("tracks", {}).get("items", [])
                if items:
                    spotify_id = items[0].get("id")
                    print(f"[Cider->Spotify] Match: {query} -> {spotify_id}")
            except Exception as e:
                print(f"[Cider->Spotify] Search 1 error: {e}")

            # Search 2: track only (fallback if artist name differs)
            if not spotify_id:
                query2 = f"track:{cleaned}"
                try:
                    result = _sp_ref.search(q=query2, type="track", limit=1)
                    items = result.get("tracks", {}).get("items", [])
                    if items:
                        spotify_id = items[0].get("id")
                        print(f"[Cider->Spotify] Match (track-only): {query2} -> {spotify_id}")
                except Exception as e:
                    print(f"[Cider->Spotify] Search 2 error: {e}")

            # Search 3: artist only (last resort for Canvas from same artist)
            if not spotify_id and artist:
                query3 = f"artist:{artist}"
                try:
                    result = _sp_ref.search(q=query3, type="track", limit=1)
                    items = result.get("tracks", {}).get("items", [])
                    if items:
                        spotify_id = items[0].get("id")
                        print(f"[Cider->Spotify] Match (artist-only): {query3} -> {spotify_id}")
                except Exception as e:
                    print(f"[Cider->Spotify] Search 3 error: {e}")

            _cache_set(key, spotify_id)

            if not spotify_id:
                print(f"[Cider->Spotify] No match for: {track_name} - {artist}")
                return

            _apply_external_canvas(spotify_id, cider_track_id)

        except Exception:
            traceback.print_exc()
        finally:
            with _lookup_progress_lock:
                _lookup_in_progress.discard(key)

    t = threading.Thread(target=_work, daemon=True)
    t.start()


def _apply_external_canvas(spotify_track_id, cider_track_id):
    """Fetch the Spotify Canvas for spotify_track_id and write the proxy URL
    into _current_data if the Cider track hasn't changed."""
    from spotify_controller import fetch_canvas_for_external, get_canvas_cdn_url

    def on_done(track_id, proxy_url):
        with _lock:
            if _current_data.get("track_id") != cider_track_id:
                return
            if proxy_url:
                _current_data["canvas_url"] = proxy_url
                _current_data["canvas_cdn_url"] = get_canvas_cdn_url(track_id)
                _current_data["visual_type"] = "canvas_video"
                print(f"[Cider->Spotify] Canvas set: {proxy_url}")
            else:
                _current_data["canvas_url"] = None
                _current_data["canvas_cdn_url"] = None
                _current_data["visual_type"] = "image"

    fetch_canvas_for_external(spotify_track_id, on_done)


# ── Cider artwork URL builder ────────────────────────────

def _extract_art_url(artwork_dict):
    if not artwork_dict:
        return ""
    url = artwork_dict.get("url", "")
    if not url:
        return ""
    w = artwork_dict.get("width", 600)
    h = artwork_dict.get("height", 600)
    return url.replace("{w}", str(w)).replace("{h}", str(h))


# ── Polling ──────────────────────────────────────────────

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


def _do_poll():
    global _previous_track_id, _volume_poll_counter
    try:
        resp = requests.get(
            CIDER_BASE + "/api/v1/playback/now-playing",
            headers=_headers(),
            timeout=2,
        )
        if resp.status_code != 200:
            with _lock:
                _current_data["is_playing"] = False
            return

        body = resp.json()
        info = body.get("info", {})
        if not info:
            with _lock:
                _current_data["is_playing"] = False
                _current_data["track"] = ""
                _current_data["artist"] = ""
                _current_data["track_id"] = ""
                _current_data["canvas_url"] = None
                _current_data["canvas_cdn_url"] = None
                _current_data["visual_type"] = "image"
                _current_data["server_time"] = time.time()
            return

        now = time.time()
        artist = info.get("artistName", "")
        track_name = info.get("name", "")
        album_name = info.get("albumName", "")
        art_url = _extract_art_url(info.get("artwork"))
        duration_ms = info.get("durationInMillis", 0)
        current_time_s = info.get("currentPlaybackTime", 0)
        progress_ms = int(current_time_s * 1000)

        play_params = info.get("playParams", {})
        catalog_id = str(play_params.get("catalogId", ""))
        opaque_id = play_params.get("id", "")
        song_id = catalog_id or opaque_id
        track_id = song_id or info.get("isrc", "")

        # Prefer an explicit playback state string when Cider provides one.
        # Fallbacks must NOT default to "playing" -- the old heuristic was
        # always-true when status was missing, so pause was never detected.
        status = info.get("state") or info.get("status")
        remaining_ms = info.get("remainingTime", None)
        if isinstance(status, str) and status.lower() in ("playing", "paused", "stopped"):
            is_playing = status.lower() == "playing"
        elif remaining_ms is not None:
            is_playing = remaining_ms > 0 and progress_ms > 0
        else:
            # No explicit signal at all: keep the previous value.
            with _lock:
                is_playing = _current_data.get("is_playing", False)

        # Real volume (was hardcoded 0, which reset the UI slider every poll).
        volume = None
        _volume_poll_counter += 1
        if _volume_poll_counter >= _VOLUME_POLL_EVERY:
            _volume_poll_counter = 0
            volume = _fetch_volume()

        # Shuffle/repeat: MusicKit conventions when the fields are present
        # (shuffleMode 0/1, repeatMode 0=off 1=one 2=all). None = unknown,
        # keep previous value.
        shuffle_state = None
        sm = info.get("shuffleMode")
        if sm is not None:
            shuffle_state = bool(sm)
        repeat_state = None
        rm = info.get("repeatMode")
        if rm is not None:
            repeat_state = {0: "off", 1: "track", 2: "context"}.get(rm, "off")

        with _lock:
            prev_tid = _previous_track_id
            prev_local = _current_data.get("album_art_local", "")
            prev_bg = _current_data.get("bg_art_local", "")
            prev_color = _current_data.get("dominant_color", "#1a1a2e")
            prev_changed_at = _current_data.get("track_changed_at", 0)
            prev_volume = _current_data.get("volume", 0)
            prev_shuffle = _current_data.get("shuffle_state", False)
            prev_repeat = _current_data.get("repeat_state", "off")

        track_changed = track_id != prev_tid

        if track_changed:
            print(f"[Cider] Track changed: {artist} - {track_name}", flush=True)
            if _scrobbler_active():
                scrobbler_reset()
            local_art_path = ""
            bg_art_path = ""
            color = "#1a1a2e"
            track_changed_at = now
            with _lock:
                _current_data["canvas_url"] = None
                _current_data["canvas_cdn_url"] = None
                _current_data["visual_type"] = "image"

            if track_name and artist:
                _search_spotify_canvas(track_name, artist, track_id)
        else:
            local_art_path = prev_local
            bg_art_path = prev_bg
            color = prev_color
            track_changed_at = prev_changed_at

        with _lock:
            _current_data.update({
                "artist": artist,
                "track": track_name,
                "album": album_name,
                "album_art_url": art_url,
                "album_art_local": local_art_path,
                "bg_art_local": bg_art_path,
                "dominant_color": color,
                "progress_ms": progress_ms,
                "duration_ms": duration_ms,
                "is_playing": is_playing,
                "volume": volume if volume is not None else prev_volume,
                "device": "Cider",
                "track_id": track_id,
                "shuffle_state": shuffle_state if shuffle_state is not None else prev_shuffle,
                "repeat_state": repeat_state if repeat_state is not None else prev_repeat,
                "server_time": now,
                "track_changed_at": track_changed_at,
            })
            if track_changed:
                _previous_track_id = track_id

        if art_url and (track_changed or not local_art_path):
            _spawn_art_cache(track_id, art_url)

        if _scrobbler_active():
            scrobbler_update(track_id, track_name, artist, duration_ms, is_playing, progress_ms)

    except (requests.ConnectionError, requests.Timeout):
        with _lock:
            _current_data["is_playing"] = False
    except Exception:
        traceback.print_exc()


def _poll_loop():
    while _polling_active:
        _do_poll()
        time.sleep(POLL_INTERVAL)


def start_polling():
    global _polling_active
    if _polling_active:
        return
    _polling_active = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    print("Cider poller started")


def stop_polling():
    global _polling_active
    _polling_active = False


def get_current_data():
    with _lock:
        return dict(_current_data)


def play():
    try:
        requests.post(CIDER_BASE + "/api/v1/playback/play",
                       json={}, headers=_headers(), timeout=5)
        return True
    except Exception as e:
        print(f"Cider play failed: {e}")
        return False


def pause():
    try:
        requests.post(CIDER_BASE + "/api/v1/playback/pause",
                       json={}, headers=_headers(), timeout=5)
        return True
    except Exception as e:
        print(f"Cider pause failed: {e}")
        return False


def next_track():
    try:
        requests.post(CIDER_BASE + "/api/v1/playback/next",
                       json={}, headers=_headers(), timeout=5)
        return True
    except Exception as e:
        print(f"Cider next failed: {e}")
        return False


def previous_track():
    try:
        requests.post(CIDER_BASE + "/api/v1/playback/previous",
                       json={}, headers=_headers(), timeout=5)
        return True
    except Exception as e:
        print(f"Cider previous failed: {e}")
        return False


def seek_track(position_ms):
    try:
        position_s = position_ms / 1000.0
        requests.post(CIDER_BASE + "/api/v1/playback/seek",
                       json={"position": position_s},
                       headers=_headers(), timeout=5)
        return True
    except Exception as e:
        print(f"Cider seek failed: {e}")
        return False


def set_volume(volume_percent):
    try:
        vol = max(0.0, min(1.0, volume_percent / 100.0))
        requests.post(CIDER_BASE + "/api/v1/playback/volume",
                       json={"volume": vol},
                       headers=_headers(), timeout=5)
        with _lock:
            _current_data["volume"] = max(0, min(100, int(volume_percent)))
        return True
    except Exception as e:
        print(f"Cider volume failed: {e}")
        return False


# Cider 2 has no shuffle/repeat *setters* -- only toggle endpoints plus
# state getters (GET shuffle-mode -> 0/1, GET repeat-mode -> 0=off 1=one
# 2=all, each toggle advancing the cycle by one). Setters are therefore
# implemented as read-then-toggle.

def _get_mode(path):
    """Read a 0/1/2 mode value from a Cider state endpoint, or None."""
    try:
        resp = requests.get(CIDER_BASE + path, headers=_headers(), timeout=3)
        if resp.status_code == 200:
            val = (resp.json() or {}).get("value")
            if val is not None:
                return int(val)
    except Exception:
        pass
    return None


def _post_toggle(path):
    try:
        resp = requests.post(CIDER_BASE + path, headers=_headers(), timeout=5)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


def set_shuffle(state):
    """Set shuffle on Cider via read-then-toggle."""
    desired = 1 if state else 0
    current = _get_mode("/api/v1/playback/shuffle-mode")
    if current is None:
        print("Cider shuffle: could not read shuffle-mode")
        return False
    if current != desired and not _post_toggle("/api/v1/playback/toggle-shuffle"):
        print("Cider shuffle: toggle failed")
        return False
    with _lock:
        _current_data["shuffle_state"] = bool(state)
    return True


def set_repeat(state):
    """Set repeat on Cider: 'off' | 'track' | 'context'.
    Repeat-mode enum is 0=off, 1=one, 2=all; each toggle advances the
    cycle by one, so toggle (target - current) % 3 times."""
    target = {"off": 0, "track": 1, "context": 2}.get(state)
    if target is None:
        return False
    current = _get_mode("/api/v1/playback/repeat-mode")
    if current is None:
        print("Cider repeat: could not read repeat-mode")
        return False
    presses = (target - current) % 3
    for _ in range(presses):
        if not _post_toggle("/api/v1/playback/toggle-repeat"):
            print("Cider repeat: toggle failed")
            return False
    with _lock:
        _current_data["repeat_state"] = state
    return True


# ══════════════════════════════════════════════════════════
# DISABLED: Cider editorial video system (kept for future use)
# To re-enable, uncomment these functions and wire back into _do_poll().
# ══════════════════════════════════════════════════════════
#
# from urllib.parse import quote
#
# _editorial_cache = {}
# _editorial_lock = threading.Lock()
#
#
# def _extract_hls_from_attrs(attrs):
#     """Extract HLS URL from editorialVideo in an item's attributes."""
#     ev = attrs.get("editorialVideo", {})
#     if not ev:
#         return None
#     preference = [
#         "motionDetailTall",
#         "motionArtworkFullscreen16x9",
#         "motionDetailSquare",
#         "motionSquareVideo1x1",
#     ]
#     for key in preference:
#         variant = ev.get(key, {})
#         url = variant.get("video", "")
#         if url:
#             return url
#     return None
#
#
# def _fetch_editorial_video(song_id):
#     """Query Apple Music via Cider's run-v3 proxy for editorial motion artwork.
#     First tries the song (with include=albums to discover the album ID).
#     If the song has no editorialVideo, falls back to the album query."""
#     def _work():
#         hls_url = None
#         discovered_album_id = ""
#
#         if song_id:
#             song_path = (
#                 f"/v1/catalog/{STOREFRONT}/songs/{song_id}"
#                 f"?include=albums&extend=editorialArtwork,editorialVideo"
#             )
#             try:
#                 resp = requests.post(
#                     CIDER_BASE + "/api/v1/amapi/run-v3",
#                     json={"path": song_path},
#                     headers=_headers(),
#                     timeout=10,
#                 )
#                 if resp.status_code == 200:
#                     data = resp.json()
#                     items = data.get("data", [])
#                     if items:
#                         attrs = items[0].get("attributes", {})
#                         hls_url = _extract_hls_from_attrs(attrs)
#                         album_rel = (items[0].get("relationships", {})
#                                      .get("albums", {}).get("data", []))
#                         if album_rel:
#                             discovered_album_id = album_rel[0].get("id", "")
#             except Exception as e:
#                 print(f"Editorial video song fetch error: {e}")
#
#         if not hls_url and discovered_album_id:
#             album_path = (
#                 f"/v1/catalog/{STOREFRONT}/albums/{discovered_album_id}"
#                 f"?extend=editorialArtwork,editorialVideo"
#             )
#             try:
#                 resp = requests.post(
#                     CIDER_BASE + "/api/v1/amapi/run-v3",
#                     json={"path": album_path},
#                     headers=_headers(),
#                     timeout=10,
#                 )
#                 if resp.status_code == 200:
#                     data = resp.json()
#                     items = data.get("data", [])
#                     if items:
#                         attrs = items[0].get("attributes", {})
#                         hls_url = _extract_hls_from_attrs(attrs)
#             except Exception as e:
#                 print(f"Editorial video album fetch error: {e}")
#
#         cache_key = discovered_album_id or song_id
#         with _editorial_lock:
#             _editorial_cache[cache_key] = hls_url
#
#         _apply_editorial(cache_key)
#
#     t = threading.Thread(target=_work, daemon=True)
#     t.start()
#
#
# def _apply_editorial(cache_key):
#     """Write the proxy URL (or None) into _current_data."""
#     with _editorial_lock:
#         hls_url = _editorial_cache.get(cache_key)
#     with _lock:
#         if hls_url:
#             proxy_url = "/video_proxy?url=" + quote(hls_url, safe="")
#             _current_data["canvas_url"] = proxy_url
#             _current_data["visual_type"] = "hls_video"
#         else:
#             _current_data["canvas_url"] = None
#             _current_data["visual_type"] = "image"
