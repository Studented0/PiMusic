#!/usr/bin/env python3
"""PiMusic – Spotify Now-Playing display server."""

import os
import dotenv
dotenv.load_dotenv()
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
import requests as http_requests

from spotify_auth import get_spotify_client, SP_DC, start_wp_token_refresh
from spotify_controller import (
    force_poll,
    get_canvas_cdn_url,
    get_current_data,
    next_track,
    pause,
    play,
    previous_track,
    seek_track,
    set_volume,
    start_polling,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, "art_cache")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


sp = get_spotify_client()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
@app.route("/api/current")
def api_state():
    return jsonify(get_current_data())


@app.route("/api/play", methods=["POST"])
def api_play():
    return jsonify({"ok": play(sp)})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    return jsonify({"ok": pause(sp)})


@app.route("/api/next", methods=["POST"])
def api_next():
    return jsonify({"ok": next_track(sp)})


@app.route("/api/previous", methods=["POST"])
def api_previous():
    return jsonify({"ok": previous_track(sp)})


@app.route("/api/seek", methods=["POST"])
def api_seek():
    data = request.get_json(silent=True) or {}
    pos = data.get("position_ms", 0)
    return jsonify({"ok": seek_track(sp, int(pos))})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    data = request.get_json(silent=True) or {}
    vol = data.get("volume", 50)
    return jsonify({"ok": set_volume(sp, int(vol))})


@app.route("/api/force-poll", methods=["POST"])
def api_force_poll():
    force_poll()
    return jsonify({"ok": True})


@app.route("/art/<path:filename>")
def serve_art(filename):
    return send_from_directory(ART_DIR, filename)


_canvas_bytes_cache = {}


@app.route("/api/canvas/<path:filename>")
def serve_canvas_proxy(filename):
    """Stream canvas MP4 from RAM cache, fetching from CDN on first request."""
    track_id = filename.replace(".mp4", "")
    if track_id in _canvas_bytes_cache:
        print("Canvas proxy: serving " + track_id + " from RAM cache")
        return Response(
            _canvas_bytes_cache[track_id],
            mimetype="video/mp4",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )

    cdn_url = get_canvas_cdn_url(track_id)
    if not cdn_url:
        print("Canvas proxy: no CDN URL for " + track_id)
        return Response("not found", status=404)

    print("Canvas proxy: fetching " + track_id + " from CDN...")
    try:
        resp = http_requests.get(cdn_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print("Canvas proxy: CDN fetch failed for " + track_id + ": " + str(e))
        return Response("fetch failed", status=502)

    _canvas_bytes_cache[track_id] = resp.content
    size_mb = len(resp.content) / (1024 * 1024)
    print("Canvas proxy: cached " + track_id + " -- " + str(round(size_mb, 2)) + " MB (" + str(len(resp.content)) + " bytes)")
    if size_mb < 0.1:
        print("Canvas proxy: WARNING -- file is tiny, may not be a real video!")

    return Response(
        resp.content,
        mimetype="video/mp4",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400",
        },
    )


if __name__ == "__main__":
    if SP_DC:
        print("SP_DC loaded (" + SP_DC[:8] + "...)")
        print("Capturing web player token (Chromium will flash briefly)...")
        start_wp_token_refresh()
    else:
        print("WARNING: SP_DC not set in .env -- Canvas will not work")
    print("Starting Spotify poller ...")
    start_polling(sp)
    print("PiMusic server running on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
