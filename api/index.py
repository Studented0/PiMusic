"""Vercel serverless entry point for PiMusic.

This is intentionally a lean Flask app that does not import anything from
the real PiMusic backend (spotify_server.py, spotify_controller.py,
spotify_auth.py, cider_controller.py, album_cache.py, source_manager.py,
resource_monitor.py). Those pull in Playwright, spotipy, curl_cffi,
colorthief, and psutil, which would blow past Vercel's serverless function
size limits and none of which can run in a read-only /tmp-only sandbox.

Instead, it wraps the shared demo_state module and serves the same
templates + static assets the real server uses, so the frontend looks and
feels identical.
"""

import os
import sys

# Add server/ to sys.path so we can import demo_state, regardless of
# where Vercel drops the working directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SERVER = os.path.join(_ROOT, "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from flask import (
    Flask, Response, jsonify, redirect, render_template,
    request, send_from_directory,
)

import demo_state

app = Flask(
    __name__,
    static_folder=os.path.join(_ROOT, "static"),
    template_folder=os.path.join(_ROOT, "templates"),
)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


# ── Pages ────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", demo_mode=True)


@app.route("/settings")
def settings_page():
    return render_template("settings.html", demo_mode=True)


# ── State & transport ────────────────────────────────────

@app.route("/api/state")
@app.route("/api/current")
def api_state():
    return jsonify(demo_state.get_state())


@app.route("/api/play", methods=["POST"])
def api_play():
    return jsonify({"ok": demo_state.set_playing(True)})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    return jsonify({"ok": demo_state.set_playing(False)})


@app.route("/api/next", methods=["POST"])
def api_next():
    return jsonify({"ok": demo_state.next_track()})


@app.route("/api/previous", methods=["POST"])
def api_previous():
    return jsonify({"ok": demo_state.previous_track()})


@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.get_json(silent=True) or {}
    return jsonify({"ok": demo_state.seek(int(data.get("position_ms", 0)))})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json(silent=True) or {}
    return jsonify({"ok": demo_state.set_volume(int(data.get("volume", 50)))})


@app.route("/api/force-poll", methods=["POST"])
def api_force_poll():
    return jsonify({"ok": True})


# ── Canvas proxy ─────────────────────────────────────────

@app.route("/api/canvas/<path:filename>")
def serve_canvas_proxy(filename):
    track_id = filename.replace(".mp4", "")
    local = demo_state.get_canvas_file(track_id)
    if local:
        return send_from_directory(
            os.path.dirname(local), os.path.basename(local),
            mimetype="video/mp4",
        )
    cdn = demo_state.get_canvas_cdn(track_id)
    if cdn:
        return redirect(cdn, code=302)
    return Response("not found", status=404)


# ── Album art fallback (real server serves from art_cache/) ─────

@app.route("/art/<path:filename>")
def serve_art(_filename):
    return Response("not found", status=404)


# ── Source management ────────────────────────────────────

@app.route("/api/source", methods=["GET"])
def api_get_source():
    return jsonify({"source": demo_state.get_state()["source"]})


@app.route("/api/source", methods=["POST"])
def api_set_source():
    data = request.get_json(silent=True) or {}
    ok = demo_state.set_source(data.get("source", ""))
    return jsonify({"ok": ok, "source": demo_state.get_state()["source"]})


# ── HID input passthrough (encoder) ──────────────────────

@app.route("/api/hid/input", methods=["POST"])
def api_hid_input():
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
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


# ── Settings stubs (read-only demo) ──────────────────────

_FAKE_SETTINGS = {
    "spotify_sp_dc": "***",
    "spotify_client_id": "demo",
    "spotify_client_secret": "********",
    "spotify_redirect_uri": "http://127.0.0.1:8080",
    "cider_token": "",
    "cider_host": "http://127.0.0.1:10767",
    "cider_storefront": "us",
    "cpu_threshold": 75,
    "scanline_overlay": True,
    "cinematic_auto": False,
    "visual_mode": "canvas_card",
}


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(_FAKE_SETTINGS)


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    # Accept and acknowledge, but don't persist — read-only FS on Vercel.
    data = request.get_json(silent=True) or {}
    if "visual_mode" in data and data["visual_mode"] in ("canvas_card", "canvas_bg", "artwork"):
        demo_state.set_source(demo_state.get_state()["source"])  # no-op placeholder
    return jsonify({"ok": True})


@app.route("/api/force-reauth", methods=["POST"])
def api_force_reauth():
    return jsonify({"ok": True, "message": "Demo mode — no auth to refresh"})


@app.route("/api/spotify/reauth", methods=["POST"])
def api_spotify_reauth():
    return jsonify({"ok": True, "account": "demo@pimusic.local"})


@app.route("/api/clear-cache", methods=["POST"])
def api_clear_cache():
    return jsonify({"ok": True, "removed": 0})


# ── System / CPU ─────────────────────────────────────────

@app.route("/api/system/cpu", methods=["GET"])
def api_cpu():
    return jsonify({"cpu_percent": 12.0, "video_disabled": False, "threshold": 75})


# Vercel imports `app` as the WSGI handler. Running this file directly is
# also useful for local smoke tests: `python api/index.py`
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
