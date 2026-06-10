#!/usr/bin/env python3
"""PiMusic – hybrid Spotify + Cider music visualization server."""

import json
import os
import threading
import time

import dotenv
dotenv.load_dotenv()
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
import requests as http_requests
from curl_cffi import requests as cffi_requests

DEMO_MODE = os.getenv("DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")

from spotify_auth import (
    get_spotify_client, SP_DC, CLIENT_ID, REDIRECT_URI,
    start_wp_token_refresh, force_reauth, get_account_info,
    wait_for_wp_tokens,
)
import spotify_controller
from spotify_controller import (
    force_poll,
    get_canvas_cdn_url,
    get_idle_canvas,
    prewarm_idle_canvas,
    set_canvas_prefetch_hook,
    start_polling,
)
from album_cache import prune_art_cache
import cider_controller
import source_manager
import resource_monitor
import demo_state
import spotify_library
import lyrics as lyrics_provider

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (server/ -> ..)
ART_DIR = os.path.join(BASE_DIR, "art_cache")
DEMO_DIR = os.path.join(BASE_DIR, "static", "demo")
SETTINGS_PATH = os.path.expanduser("~/pimusic/settings.json")

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True


sp = None if DEMO_MODE else get_spotify_client()


# ── Settings persistence ─────────────────────────────────

_default_settings = {
    "spotify_sp_dc": SP_DC,
    "spotify_client_id": CLIENT_ID,
    "spotify_client_secret": "",
    "spotify_redirect_uri": REDIRECT_URI,
    "cider_token": "",
    "cider_host": "http://127.0.0.1:10767",
    "cider_storefront": "us",
    "cpu_threshold": 75,
    "scanline_overlay": True,
    "cinematic_auto": False,
    "visual_mode": "canvas_card",
    "lyrics_bg": "media",  # "media" (canvas/art behind lyrics) | "dark"
}


def _load_settings():
    settings = dict(_default_settings)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            settings.update(stored)
        except Exception as e:
            print(f"Settings load error: {e}")
    if not settings.get("spotify_sp_dc") and SP_DC:
        settings["spotify_sp_dc"] = SP_DC
    return settings


def _save_settings(settings):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


_settings = _load_settings()
_settings_lock = threading.Lock()


def _get_setting(key, default=None):
    with _settings_lock:
        return _settings.get(key, default)


def _apply_settings():
    """Push current settings into subsystems."""
    with _settings_lock:
        cider_token = _settings.get("cider_token", "")
        cider_storefront = _settings.get("cider_storefront", "us")
        cider_host = _settings.get("cider_host", "")
        cpu_threshold = _settings.get("cpu_threshold", 75)
    cider_controller.configure(
        token=cider_token,
        storefront=cider_storefront,
        base_url=cider_host,
    )
    resource_monitor.set_threshold(cpu_threshold)


# ── Page routes ──────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", demo_mode=DEMO_MODE)


@app.route("/settings")
def settings_page():
    return render_template("settings.html", demo_mode=DEMO_MODE)


# ── Unified state API ────────────────────────────────────

# Changes on every server start. The frontend reloads itself when this
# changes, so the Pi kiosk always picks up new code after a server restart.
_SERVER_BOOT_ID = str(int(time.time()))


@app.route("/api/state")
@app.route("/api/current")
def api_state():
    if DEMO_MODE:
        data = demo_state.get_state()
        data["server_boot_id"] = _SERVER_BOOT_ID
        data["lyrics_bg"] = "media"
        return jsonify(data)
    cpu_throttle = resource_monitor.should_disable_video()
    data = source_manager.get_unified_state(cpu_override_image=cpu_throttle)

    vm = _get_setting("visual_mode", "canvas_card")
    if vm == "artwork":
        data["visual_type"] = "image"
        data["canvas_url"] = None
    elif vm in ("canvas_card", "canvas_bg"):
        if data.get("visual_type") != "canvas_video":
            data["visual_type"] = "image"
            data["canvas_url"] = None

    data["visual_mode"] = vm

    idle_tid, idle_cdn = get_idle_canvas()
    if idle_tid and idle_cdn:
        data["idle_canvas_track_id"] = idle_tid
        data["idle_canvas_url"] = f"/api/canvas/{idle_tid}.mp4"
        data["idle_canvas_cdn_url"] = idle_cdn
    else:
        data["idle_canvas_track_id"] = None
        data["idle_canvas_url"] = None
        data["idle_canvas_cdn_url"] = None

    data["server_boot_id"] = _SERVER_BOOT_ID
    data["lyrics_bg"] = _get_setting("lyrics_bg", "media")
    return jsonify(data)


# ── Playback commands (routed via source manager) ────────

@app.route("/api/play", methods=["POST"])
def api_play():
    if DEMO_MODE:
        return jsonify({"ok": demo_state.set_playing(True)})
    return jsonify({"ok": source_manager.dispatch_command("play", sp=sp)})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    if DEMO_MODE:
        return jsonify({"ok": demo_state.set_playing(False)})
    return jsonify({"ok": source_manager.dispatch_command("pause", sp=sp)})


@app.route("/api/next", methods=["POST"])
def api_next():
    if DEMO_MODE:
        return jsonify({"ok": demo_state.next_track()})
    # spotify_controller.next_track() force-polls internally.
    return jsonify({"ok": source_manager.dispatch_command("next", sp=sp)})


@app.route("/api/previous", methods=["POST"])
def api_previous():
    if DEMO_MODE:
        return jsonify({"ok": demo_state.previous_track()})
    return jsonify({"ok": source_manager.dispatch_command("previous", sp=sp)})


@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.get_json(silent=True) or {}
    pos = data.get("position_ms", 0)
    if DEMO_MODE:
        return jsonify({"ok": demo_state.seek(int(pos))})
    return jsonify({
        "ok": source_manager.dispatch_command("seek", sp=sp, position_ms=int(pos))
    })


@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json(silent=True) or {}
    vol = data.get("volume", 50)
    if DEMO_MODE:
        return jsonify({"ok": demo_state.set_volume(int(vol))})
    return jsonify({
        "ok": source_manager.dispatch_command("volume", sp=sp, volume=int(vol))
    })


@app.route("/api/shuffle", methods=["POST"])
def api_shuffle():
    data = request.get_json(silent=True) or {}
    state = bool(data.get("state", False))
    if DEMO_MODE:
        return jsonify({"ok": demo_state.set_shuffle(state)})
    return jsonify({"ok": source_manager.dispatch_command("shuffle", sp=sp, state=state)})


@app.route("/api/repeat", methods=["POST"])
def api_repeat():
    data = request.get_json(silent=True) or {}
    state = data.get("state", "off")
    if DEMO_MODE:
        return jsonify({"ok": demo_state.set_repeat(state)})
    return jsonify({"ok": source_manager.dispatch_command("repeat", sp=sp, state=state)})


@app.route("/api/like", methods=["POST"])
def api_like():
    """Toggle the current track in Liked Songs (Spotify only)."""
    if DEMO_MODE:
        ok, saved = demo_state.toggle_like()
        return jsonify({"ok": ok, "is_saved": saved})
    if source_manager.get_active_source() != "spotify":
        return jsonify({"ok": False, "error": "spotify only"})
    ok, saved = spotify_controller.toggle_save(sp)
    return jsonify({"ok": ok, "is_saved": saved})


@app.route("/api/queue")
def api_queue():
    """Current Spotify queue (now playing + up next)."""
    if DEMO_MODE or source_manager.get_active_source() != "spotify":
        return jsonify({"currently_playing": None, "items": []})
    return _library_call(spotify_library.get_queue, sp)


@app.route("/api/force-poll", methods=["POST"])
def api_force_poll():
    if DEMO_MODE:
        return jsonify({"ok": True})
    force_poll()
    return jsonify({"ok": True})


# ── Art cache ────────────────────────────────────────────

@app.route("/art/<path:filename>")
def serve_art(filename):
    return send_from_directory(ART_DIR, filename)


# ── Spotify canvas MP4 proxy (RAM cache) ─────────────────

_canvas_bytes_cache = {}
_CANVAS_CACHE_MAX = 10
_canvas_ram_lock = threading.Lock()
_canvas_inflight = {}  # track_id -> threading.Event (set when fetch completes)


def _canvas_mp4_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=86400",
    }


def _ensure_canvas_bytes(track_id):
    """Return canvas MP4 bytes for a track, fetching from CDN into the RAM
    cache if needed. Blocking; safe to call concurrently (single fetcher,
    everyone else waits on the in-flight event)."""
    with _canvas_ram_lock:
        if track_id in _canvas_bytes_cache:
            return _canvas_bytes_cache[track_id]

    cdn_url = get_canvas_cdn_url(track_id)
    if not cdn_url:
        return None

    fetcher = False
    with _canvas_ram_lock:
        if track_id in _canvas_bytes_cache:
            return _canvas_bytes_cache[track_id]
        wait_ev = _canvas_inflight.get(track_id)
        if wait_ev is None:
            wait_ev = threading.Event()
            _canvas_inflight[track_id] = wait_ev
            fetcher = True

    if not fetcher:
        wait_ev.wait(timeout=90)
        with _canvas_ram_lock:
            return _canvas_bytes_cache.get(track_id)

    body = None
    try:
        resp = cffi_requests.get(
            cdn_url,
            timeout=15,
            impersonate="chrome131",
            headers={
                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
                "Referer": "https://open.spotify.com/",
            },
        )
        resp.raise_for_status()
        body = resp.content
    except Exception as e:
        print(f"Canvas proxy: CDN fetch failed for {track_id}: {type(e).__name__}")

    with _canvas_ram_lock:
        if body is not None:
            if len(_canvas_bytes_cache) >= _CANVAS_CACHE_MAX and track_id not in _canvas_bytes_cache:
                oldest = next(iter(_canvas_bytes_cache))
                del _canvas_bytes_cache[oldest]
            _canvas_bytes_cache[track_id] = body
        _canvas_inflight.pop(track_id, None)
        wait_ev.set()
    return body


def _prefetch_canvas_async(track_id):
    """Warm the RAM cache in the background as soon as a CDN URL is known,
    so the Pi's /api/canvas request never blocks on a CDN download."""
    threading.Thread(
        target=_ensure_canvas_bytes, args=(track_id,), daemon=True
    ).start()


if not DEMO_MODE:
    set_canvas_prefetch_hook(_prefetch_canvas_async)


@app.route("/api/canvas/<path:filename>")
def serve_canvas_proxy(filename):
    """Stream canvas MP4 from RAM cache, fetching from CDN on first request."""
    track_id = filename.replace(".mp4", "")

    if DEMO_MODE:
        local = demo_state.get_canvas_file(track_id)
        if local:
            return send_from_directory(
                os.path.dirname(local), os.path.basename(local),
                mimetype="video/mp4",
            )
        cdn = demo_state.get_canvas_cdn(track_id)
        if cdn:
            from flask import redirect
            return redirect(cdn, code=302)
        return Response("not found", status=404)

    with _canvas_ram_lock:
        cached = track_id in _canvas_bytes_cache
    if not cached and not get_canvas_cdn_url(track_id):
        return Response("not found", status=404)

    body = _ensure_canvas_bytes(track_id)
    if body is None:
        return Response("fetch failed", status=502)
    return Response(
        body,
        mimetype="video/mp4",
        headers=_canvas_mp4_headers(),
    )


@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    """Delete all files in art_cache/ (album art JPEGs)."""
    if DEMO_MODE:
        return jsonify({"ok": True, "removed": 0})
    removed = 0
    try:
        if os.path.isdir(ART_DIR):
            for name in os.listdir(ART_DIR):
                path = os.path.join(ART_DIR, name)
                if os.path.isfile(path):
                    os.remove(path)
                    removed += 1
    except Exception as e:
        print(f"Clear art cache error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "removed": removed})


# ── Source management ────────────────────────────────────

@app.route("/api/source", methods=["GET"])
def api_get_source():
    if DEMO_MODE:
        return jsonify({"source": demo_state.get_state()["source"]})
    return jsonify({"source": source_manager.get_active_source()})


@app.route("/api/source", methods=["POST"])
def api_set_source():
    data = request.get_json(silent=True) or {}
    src = data.get("source", "")
    if DEMO_MODE:
        ok = demo_state.set_source(src)
        return jsonify({"ok": ok, "source": demo_state.get_state()["source"]})
    ok = source_manager.set_source(src)
    return jsonify({"ok": ok, "source": source_manager.get_active_source()})


# ── ESP32 HID input ─────────────────────────────────────

@app.route("/api/hid/input", methods=["POST"])
def api_hid_input():
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    if not action:
        return jsonify({"ok": False, "error": "missing action"}), 400

    if DEMO_MODE:
        if action == "play":
            return jsonify({"ok": demo_state.set_playing(True)})
        if action == "pause":
            return jsonify({"ok": demo_state.set_playing(False)})
        if action == "next":
            return jsonify({"ok": demo_state.next_track()})
        if action == "previous":
            return jsonify({"ok": demo_state.previous_track()})
        if action == "volume":
            return jsonify({"ok": demo_state.set_volume(int(data.get("value", 50)))})
        if action == "seek":
            return jsonify({"ok": demo_state.seek(int(data.get("position_ms", 0)))})
        return jsonify({"ok": True})

    kwargs = {}
    if action == "volume":
        kwargs["volume"] = data.get("value", 50)
    elif action == "seek":
        kwargs["position_ms"] = data.get("position_ms", 0)

    # Controller commands force-poll internally where needed.
    ok = source_manager.dispatch_command(action, sp=sp, **kwargs)
    return jsonify({"ok": ok})


# ── Settings API ─────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    safe = dict(_default_settings)
    with _settings_lock:
        safe.update(_settings)

    sp_dc = safe.get("spotify_sp_dc", "")
    if sp_dc and isinstance(sp_dc, str):
        safe["spotify_sp_dc"] = sp_dc[:8] + "..." + sp_dc[-4:] if len(sp_dc) > 12 else "***"
    if safe.get("spotify_client_secret"):
        safe["spotify_client_secret"] = "********"

    vm = safe.get("visual_mode", "canvas_card")
    if vm not in ("canvas_card", "canvas_bg", "artwork"):
        vm = "canvas_card"
    safe["visual_mode"] = vm

    return Response(
        json.dumps(safe, ensure_ascii=False),
        mimetype="application/json",
    )


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    data = request.get_json(silent=True) or {}

    with _settings_lock:
        for key in _default_settings:
            if key in data:
                if key == "spotify_sp_dc" and data[key].endswith("..."):
                    continue
                if key == "spotify_client_secret" and data[key] == "********":
                    continue
                _settings[key] = data[key]
        snapshot = dict(_settings)

    _save_settings(snapshot)
    _apply_settings()
    return jsonify({"ok": True})


# ── Force re-auth ────────────────────────────────────────

@app.route("/api/force-reauth", methods=["POST"])
def api_force_reauth():
    if DEMO_MODE:
        return jsonify({"ok": True, "message": "Demo mode — no auth to refresh"})
    force_reauth()
    return jsonify({"ok": True, "message": "Cache cleared, token refresh started"})


@app.route("/api/spotify/reauth", methods=["POST"])
def api_spotify_reauth():
    if DEMO_MODE:
        return jsonify({"ok": True, "account": "demo@pimusic.local"})
    print("[Spotify Auth] Re-auth triggered via API")
    force_reauth()
    try:
        account = get_account_info()
        print(f"[Spotify Auth] Authenticating account: {account}")
        print("[Spotify Auth] Authentication successful.")
        return jsonify({"ok": True, "account": account})
    except Exception as e:
        print(f"[Spotify Auth] Re-auth error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Library: search / playlists / liked / play / queue ──

_EMPTY_SEARCH = {"tracks": [], "albums": [], "playlists": []}


def _library_call(fn, *args, **kwargs):
    """Run a spotify_library call and translate errors into JSON responses."""
    try:
        return jsonify(fn(*args, **kwargs))
    except spotify_library.RateLimitedError:
        return jsonify({"error": "rate_limited"}), 429
    except Exception as e:
        print(f"[Library] {fn.__name__} error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/api/library/search")
def api_library_search():
    q = (request.args.get("q") or "").strip()
    if DEMO_MODE or not q:
        return jsonify(_EMPTY_SEARCH)
    return _library_call(spotify_library.search, sp, q)


@app.route("/api/library/playlists")
def api_library_playlists():
    if DEMO_MODE:
        return jsonify({"items": [], "total": 0, "offset": 0, "has_more": False})
    offset = request.args.get("offset", 0, type=int)
    return _library_call(spotify_library.get_playlists, sp, offset=offset)


@app.route("/api/library/playlists/<playlist_id>")
def api_library_playlist_tracks(playlist_id):
    if DEMO_MODE:
        return jsonify({"items": [], "total": 0, "offset": 0, "has_more": False})
    offset = request.args.get("offset", 0, type=int)
    return _library_call(
        spotify_library.get_playlist_tracks, sp, playlist_id, offset=offset
    )


@app.route("/api/library/liked")
def api_library_liked():
    if DEMO_MODE:
        return jsonify({"items": [], "total": 0, "offset": 0, "has_more": False})
    offset = request.args.get("offset", 0, type=int)
    return _library_call(spotify_library.get_liked, sp, offset=offset)


@app.route("/api/library/albums/<album_id>")
def api_library_album_tracks(album_id):
    if DEMO_MODE:
        return jsonify({"items": [], "total": 0, "offset": 0, "has_more": False})
    offset = request.args.get("offset", 0, type=int)
    return _library_call(spotify_library.get_album_tracks, sp, album_id, offset=offset)


@app.route("/api/library/play", methods=["POST"])
def api_library_play():
    if DEMO_MODE:
        return jsonify({"ok": False, "error": "demo mode"})
    data = request.get_json(silent=True) or {}
    try:
        repeat = data.get("repeat")
        if repeat not in (None, "off", "context", "track"):
            repeat = None
        shuffle = data.get("shuffle")
        if shuffle is not None:
            shuffle = bool(shuffle)
        ok = spotify_library.play(
            sp,
            uri=data.get("uri"),
            uris=data.get("uris"),
            context_uri=data.get("context_uri"),
            offset_uri=data.get("offset_uri"),
            position=data.get("position"),
            shuffle=shuffle,
            repeat=repeat,
        )
        return jsonify({"ok": ok})
    except spotify_library.RateLimitedError:
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    except Exception as e:
        print(f"[Library] play error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/library/queue", methods=["POST"])
def api_library_queue():
    if DEMO_MODE:
        return jsonify({"ok": False, "error": "demo mode"})
    data = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": spotify_library.queue(sp, data.get("uri"))})
    except spotify_library.RateLimitedError:
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    except Exception as e:
        print(f"[Library] queue error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 502


# ── Lyrics ───────────────────────────────────────────────

@app.route("/api/lyrics")
def api_lyrics():
    artist = (request.args.get("artist") or "").strip()
    track = (request.args.get("track") or "").strip()
    album = (request.args.get("album") or "").strip()
    duration_ms = request.args.get("duration_ms", 0, type=int)
    source = (request.args.get("source") or "spotify").strip()
    track_id = (request.args.get("track_id") or "").strip()

    # Spotify fallback needs a real Spotify track id; Cider ids are Apple's.
    spotify_track_id = track_id if (source == "spotify" and not DEMO_MODE) else ""

    result = lyrics_provider.get_lyrics(
        artist, track, album=album, duration_ms=duration_ms,
        spotify_track_id=spotify_track_id,
    )
    return jsonify(result)


# ── System / CPU ─────────────────────────────────────────

@app.route("/api/system/cpu", methods=["GET"])
def api_cpu():
    if DEMO_MODE:
        return jsonify({"cpu_percent": 12.0, "video_disabled": False, "threshold": 75})
    return jsonify({
        "cpu_percent": resource_monitor.get_cpu_percent(),
        "video_disabled": resource_monitor.should_disable_video(),
        "threshold": resource_monitor.CPU_VIDEO_THRESHOLD,
    })


# ── Startup ──────────────────────────────────────────────

if __name__ == "__main__":
    if DEMO_MODE:
        print("=" * 60)
        print("  PiMusic DEMO MODE — Spotify and Cider are disabled.")
        print("  /api/state returns a hardcoded playlist with live progress.")
        print("  Drop MP4/JPG files in static/demo/ to customize.")
        print("=" * 60)
        print("PiMusic server running on http://0.0.0.0:5000")
        app.run(host="0.0.0.0", port=5000, debug=False)
    else:
        _apply_settings()

        # Lightweight pieces first: the Spotify poller (device grab happens
        # inside its own thread) and the local monitors. Flask starts right
        # away so the Pi can connect immediately.
        print("Starting Spotify poller ...")
        start_polling(sp)

        cider_controller.set_spotify_client(sp)
        source_manager.start_detection()
        resource_monitor.start()

        def _deferred_startup():
            """Stagger network-heavy startup work so launching the server
            doesn't choke the PC with a burst of DNS lookups + downloads
            (Playwright Chromium + poller + prewarm all at t=0 used to
            time each other out)."""
            time.sleep(3)

            if SP_DC:
                print(f"SP_DC loaded ({SP_DC[:8]}...)")
                print("Capturing web player token (hidden Chromium, slimmed)...")
                start_wp_token_refresh()
                # Prewarm only once tokens exist -- it silently failed before
                # ("Canvas skip: no bearer token yet").
                if wait_for_wp_tokens(timeout_sec=90):
                    print("Pre-warming idle screensaver canvas...")
                    prewarm_idle_canvas()
                else:
                    print("Web player tokens not ready; idle canvas will prewarm on demand")
            else:
                print("WARNING: SP_DC not set in .env -- Canvas will not work")

            try:
                account = get_account_info()
                print(f"[Spotify Auth] Authenticated as: {account}")
            except Exception:
                print("[Spotify Auth] Could not retrieve account info")

            if _get_setting("cider_token") or cider_controller.is_available():
                print("Starting Cider poller ...")
                cider_controller.start_polling()
            else:
                print("Cider not available at startup – will retry in background")
                while True:
                    time.sleep(10)
                    if cider_controller.is_available():
                        print("Cider became available – starting poller")
                        cider_controller.start_polling()
                        break

        threading.Thread(target=_deferred_startup, daemon=True).start()

        def _art_prune_daemon():
            time.sleep(30)
            while True:
                try:
                    n = prune_art_cache()
                    if n:
                        print(f"Art cache prune: removed {n} oldest file(s)")
                except Exception as e:
                    print(f"Art cache prune error: {e}")
                time.sleep(3600)

        threading.Thread(target=_art_prune_daemon, daemon=True).start()

        print("PiMusic server running on http://0.0.0.0:5000")
        app.run(host="0.0.0.0", port=5000, debug=False)
