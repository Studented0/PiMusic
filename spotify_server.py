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
)
from spotify_controller import (
    force_poll,
    get_canvas_cdn_url,
    get_idle_canvas,
    prewarm_idle_canvas,
    start_polling,
)
from album_cache import prune_art_cache
import cider_controller
import source_manager
import resource_monitor
import demo_state

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, "art_cache")
DEMO_DIR = os.path.join(BASE_DIR, "static", "demo")
SETTINGS_PATH = os.path.expanduser("~/pimusic/settings.json")

app = Flask(__name__, static_folder="static", template_folder="templates")
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


def _apply_settings():
    """Push current settings into subsystems."""
    cider_controller.configure(
        token=_settings.get("cider_token", ""),
        storefront=_settings.get("cider_storefront", "us"),
        base_url=_settings.get("cider_host", ""),
    )
    resource_monitor.set_threshold(_settings.get("cpu_threshold", 75))


# ── Page routes ──────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", demo_mode=DEMO_MODE)


@app.route("/settings")
def settings_page():
    return render_template("settings.html", demo_mode=DEMO_MODE)


# ── Unified state API ────────────────────────────────────

@app.route("/api/state")
@app.route("/api/current")
def api_state():
    if DEMO_MODE:
        return jsonify(demo_state.get_state())
    cpu_throttle = resource_monitor.should_disable_video()
    data = source_manager.get_unified_state(cpu_override_image=cpu_throttle)

    vm = _settings.get("visual_mode", "canvas_card")
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
    ok = source_manager.dispatch_command("next", sp=sp)
    if ok and source_manager.get_active_source() == "spotify":
        force_poll()
    return jsonify({"ok": ok})


@app.route("/api/previous", methods=["POST"])
def api_previous():
    if DEMO_MODE:
        return jsonify({"ok": demo_state.previous_track()})
    ok = source_manager.dispatch_command("previous", sp=sp)
    if ok and source_manager.get_active_source() == "spotify":
        force_poll()
    return jsonify({"ok": ok})


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
        if track_id in _canvas_bytes_cache:
            return Response(
                _canvas_bytes_cache[track_id],
                mimetype="video/mp4",
                headers=_canvas_mp4_headers(),
            )

    cdn_url = get_canvas_cdn_url(track_id)
    if not cdn_url:
        return Response("not found", status=404)

    fetcher = False
    wait_ev = None
    with _canvas_ram_lock:
        if track_id in _canvas_bytes_cache:
            return Response(
                _canvas_bytes_cache[track_id],
                mimetype="video/mp4",
                headers=_canvas_mp4_headers(),
            )
        if track_id in _canvas_inflight:
            wait_ev = _canvas_inflight[track_id]
        else:
            wait_ev = threading.Event()
            _canvas_inflight[track_id] = wait_ev
            fetcher = True

    if not fetcher:
        wait_ev.wait(timeout=90)
        with _canvas_ram_lock:
            data = _canvas_bytes_cache.get(track_id)
        if data is None:
            return Response("fetch failed", status=502)
        return Response(
            data,
            mimetype="video/mp4",
            headers=_canvas_mp4_headers(),
        )

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
            _canvas_inflight.pop(track_id, None)
            wait_ev.set()
        return Response("fetch failed", status=502)

    with _canvas_ram_lock:
        if len(_canvas_bytes_cache) >= _CANVAS_CACHE_MAX:
            oldest = next(iter(_canvas_bytes_cache))
            del _canvas_bytes_cache[oldest]
        _canvas_bytes_cache[track_id] = body
        _canvas_inflight.pop(track_id, None)
        wait_ev.set()

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

    ok = source_manager.dispatch_command(action, sp=sp, **kwargs)
    if ok and action in ("next", "previous") and source_manager.get_active_source() == "spotify":
        force_poll()
    return jsonify({"ok": ok})


# ── Settings API ─────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    safe = dict(_default_settings)
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
    global _settings
    data = request.get_json(silent=True) or {}

    for key in _default_settings:
        if key in data:
            if key == "spotify_sp_dc" and data[key].endswith("..."):
                continue
            if key == "spotify_client_secret" and data[key] == "********":
                continue
            _settings[key] = data[key]

    _save_settings(_settings)
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

        if SP_DC:
            print(f"SP_DC loaded ({SP_DC[:8]}...)")
            print("Capturing web player token (Chromium will flash briefly)...")
            start_wp_token_refresh()
        else:
            print("WARNING: SP_DC not set in .env -- Canvas will not work")

        print("Starting Spotify poller ...")
        start_polling(sp)

        print("Pre-warming idle screensaver canvas...")
        prewarm_idle_canvas()

        try:
            account = get_account_info()
            print(f"[Spotify Auth] Authenticated as: {account}")
        except Exception:
            print("[Spotify Auth] Could not retrieve account info")

        cider_controller.set_spotify_client(sp)

        if _settings.get("cider_token") or cider_controller.is_available():
            print("Starting Cider poller ...")
            cider_controller.start_polling()
        else:
            print("Cider not available at startup – will retry in background")
            def _cider_retry_loop():
                import time as _t
                while True:
                    _t.sleep(10)
                    if cider_controller.is_available():
                        print("Cider became available – starting poller")
                        cider_controller.start_polling()
                        break
            threading.Thread(target=_cider_retry_loop, daemon=True).start()

        source_manager.start_detection()
        resource_monitor.start()

        def _art_prune_daemon():
            time.sleep(3)
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
