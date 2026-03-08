"""Cider (Apple Music) controller – polls local Cider API for playback state.
When a track is playing, searches Spotify for the same track and uses
Spotify Canvas as the background visual."""

import re
import threading
import time
import traceback

import requests

from album_cache import cache_art, get_dominant_color
from scrobbler import update as scrobbler_update, reset as scrobbler_reset

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
    "dominant_color": "#1a1a2e",
    "progress_ms": 0,
    "duration_ms": 0,
    "is_playing": False,
    "volume": 0,
    "device": "Cider",
    "track_id": "",
    "canvas_url": None,
    "visual_type": "image",
    "server_time": 0,
    "track_changed_at": 0,
}
_lock = threading.Lock()
_previous_track_id = None
_polling_active = False

# Spotify client reference (set via set_spotify_client)
_sp_ref = None

# ── Spotify Canvas cross-search cache ────────────────────
LOOKUP_CACHE_TTL = 6 * 3600  # 6 hours
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
    """Quick check – is Cider currently playing something?"""
    try:
        resp = requests.get(
            CIDER_BASE + "/api/v1/playback/is-playing",
            headers=_headers(),
            timeout=2,
        )
        if resp.status_code == 200:
            return resp.json().get("is_playing", False)
    except Exception:
        pass
    return False


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
    from spotify_controller import fetch_canvas_for_external

    def on_done(track_id, proxy_url):
        with _lock:
            if _current_data.get("track_id") != cider_track_id:
                return
            if proxy_url:
                _current_data["canvas_url"] = proxy_url
                _current_data["visual_type"] = "canvas_video"
                print(f"[Cider->Spotify] Canvas set: {proxy_url}")
            else:
                _current_data["canvas_url"] = None
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

def _do_poll():
    global _previous_track_id
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

        is_playing = bool(info.get("status") is None or progress_ms > 0)
        remaining_ms = info.get("remainingTime", None)
        if remaining_ms is not None:
            is_playing = remaining_ms > 0 and progress_ms > 0

        track_changed = track_id != _previous_track_id

        if track_changed:
            print(f"[Cider] Track changed: {artist} - {track_name}", flush=True)
            scrobbler_reset()
            _previous_track_id = track_id
            local_art = cache_art(art_url) if art_url else ""
            color = get_dominant_color(art_url) if art_url else "#1a1a2e"
            track_changed_at = now
            with _lock:
                _current_data["canvas_url"] = None
                _current_data["visual_type"] = "image"

            if track_name and artist:
                _search_spotify_canvas(track_name, artist, track_id)
        else:
            with _lock:
                prev_local = _current_data.get("album_art_local", "")
                prev_color = _current_data.get("dominant_color", "#1a1a2e")
                track_changed_at = _current_data.get("track_changed_at", 0)
            if prev_local:
                local_art = prev_local.replace("/art/", "")
            else:
                local_art = cache_art(art_url) if art_url else ""
            color = prev_color if prev_color != "#1a1a2e" else (
                get_dominant_color(art_url) if art_url else "#1a1a2e"
            )

        with _lock:
            _current_data.update({
                "artist": artist,
                "track": track_name,
                "album": album_name,
                "album_art_url": art_url,
                "album_art_local": ("/art/" + local_art) if local_art else "",
                "dominant_color": color,
                "progress_ms": progress_ms,
                "duration_ms": duration_ms,
                "is_playing": is_playing,
                "volume": 0,
                "device": "Cider",
                "track_id": track_id,
                "server_time": now,
                "track_changed_at": track_changed_at,
            })

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
        return True
    except Exception as e:
        print(f"Cider volume failed: {e}")
        return False


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
