#!/usr/bin/env python3
"""PiMusic – Dual-source (Spotify + Cider) now-playing display server."""

import os
import threading

import dotenv
dotenv.load_dotenv()

from flask import Flask, Response, jsonify, render_template, request, send_from_directory
import requests as http_requests

from spotify_auth import get_spotify_client, SP_DC, start_wp_token_refresh
from spotify_controller import (
    force_poll as spotify_force_poll,
    get_canvas_cdn_url,
    get_current_data as spotify_get_data,
    next_track as spotify_next,
    pause as spotify_pause,
    play as spotify_play,
    previous_track as spotify_prev,
    seek_track as spotify_seek,
    set_volume as spotify_volume,
    start_polling as spotify_start_polling,
)
import cider_controller

# ── Configuration ────────────────────────────────────────
PORT = int(os.getenv("PIMUSIC_PORT", 5000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ART_DIR = os.path.join(BASE_DIR, "art_cache")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ── Source switching ─────────────────────────────────────
# "auto"    – prefer Cider when available, fall back to Spotify
# "spotify" – force Spotify only
# "cider"   – force Cider only
_active_source = "auto"
_source_lock = threading.Lock()


def _get_source():
    with _source_lock:
        return _active_source


def _set_source(src):
    global _active_source
    valid = ("auto", "spotify", "cider")
    if src not in valid:
        return False
    with _source_lock:
        _active_source = src
    return True


def _resolve_source():
    """Determine which source to use right now based on mode + availability."""
    src = _get_source()
    if src == "cider":
        return "cider"
    if src == "spotify":
        return "spotify"
    # auto: prefer Cider when it's available and has a track
    if cider_controller.is_available():
        cdata = cider_controller.get_current_data()
        if cdata.get("track"):
            return "cider"
    return "spotify"


# ── Spotify client (safe init) ───────────────────────────
sp = None
try:
    sp = get_spotify_client()
except Exception as e:
    print("WARNING: Spotify client init failed: " + str(e))
    print("Spotify features will be unavailable. Cider-only mode.")


# ── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
@app.route("/api/current")
def api_state():
    """Return now-playing data from the resolved source.
    Cider emergency override: if Cider is available, skip Spotify entirely
    to avoid any risk of blocking on a 429'd Spotify poller."""
    source = _resolve_source()
    if source == "cider":
        data = cider_controller.get_current_data()
        data["source"] = "cider"
    else:
        data = spotify_get_data()
        data["source"] = "spotify"
    data["active_source"] = _get_source()
    return jsonify(data)


@app.route("/api/source", methods=["GET"])
def api_source_get():
    return jsonify({"source": _get_source(), "cider_available": cider_controller.is_available()})


@app.route("/api/source", methods=["POST"])
def api_source_set():
    body = request.get_json(silent=True) or {}
    src = body.get("source", "auto")
    ok = _set_source(src)
    return jsonify({"ok": ok, "source": _get_source()})


# ── Playback controls (routed through active source) ─────

@app.route("/api/play", methods=["POST"])
def api_play():
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.play()})
    return jsonify({"ok": spotify_play(sp) if sp else False})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.pause()})
    return jsonify({"ok": spotify_pause(sp) if sp else False})


@app.route("/api/next", methods=["POST"])
def api_next():
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.next_track()})
    return jsonify({"ok": spotify_next(sp) if sp else False})


@app.route("/api/previous", methods=["POST"])
def api_previous():
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.previous_track()})
    return jsonify({"ok": spotify_prev(sp) if sp else False})


@app.route("/api/seek", methods=["POST"])
def api_seek():
    body = request.get_json(silent=True) or {}
    pos = int(body.get("position_ms", 0))
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.seek_track(pos)})
    return jsonify({"ok": spotify_seek(sp, pos) if sp else False})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    body = request.get_json(silent=True) or {}
    vol = int(body.get("volume", 50))
    if _resolve_source() == "cider":
        return jsonify({"ok": cider_controller.set_volume(vol)})
    return jsonify({"ok": spotify_volume(sp, vol) if sp else False})


@app.route("/api/force-poll", methods=["POST"])
def api_force_poll():
    if _resolve_source() == "cider":
        cider_controller.force_poll()
    else:
        spotify_force_poll()
    return jsonify({"ok": True})


# ── Static / art / media proxy ───────────────────────────

@app.route("/art/<path:filename>")
def serve_art(filename):
    return send_from_directory(ART_DIR, filename)


# RAM-only video byte cache — never touches disk
_video_bytes_cache = {}
_video_cache_lock = threading.Lock()
# Cap: ~80 MB to prevent runaway on Pi (3B+ has 1 GB RAM)
_VIDEO_CACHE_MAX_BYTES = 80 * 1024 * 1024


def _cache_video(key, content):
    """Store video bytes in RAM cache, evicting oldest if over budget."""
    with _video_cache_lock:
        _video_bytes_cache[key] = content
        total = sum(len(v) for v in _video_bytes_cache.values())
        while total > _VIDEO_CACHE_MAX_BYTES and _video_bytes_cache:
            oldest_key = next(iter(_video_bytes_cache))
            total -= len(_video_bytes_cache.pop(oldest_key))


def _serve_video_response(data):
    return Response(
        data,
        mimetype="video/mp4",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400",
        },
    )


@app.route("/api/canvas/<path:filename>")
def serve_canvas_proxy(filename):
    """Spotify Canvas MP4 — RAM-only proxy."""
    track_id = filename.replace(".mp4", "")
    cache_key = "canvas:" + track_id

    with _video_cache_lock:
        cached = _video_bytes_cache.get(cache_key)
    if cached:
        return _serve_video_response(cached)

    cdn_url = get_canvas_cdn_url(track_id)
    if not cdn_url:
        return Response("not found", status=404)

    try:
        resp = http_requests.get(cdn_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print("Canvas proxy fetch failed for " + track_id + ": " + str(e))
        return Response("fetch failed", status=502)

    _cache_video(cache_key, resp.content)
    size_mb = len(resp.content) / (1024 * 1024)
    print("Canvas cached: " + track_id + " (" + str(round(size_mb, 2)) + " MB)")
    return _serve_video_response(resp.content)


@app.route("/api/cider-video/<path:filename>")
def serve_cider_video_proxy(filename):
    """Cider / Apple Music background video — RAM-only proxy."""
    # filename is a URL-safe key; the real URL comes from query param
    video_url = request.args.get("url", "")
    if not video_url:
        return Response("missing url param", status=400)

    cache_key = "cider:" + filename

    with _video_cache_lock:
        cached = _video_bytes_cache.get(cache_key)
    if cached:
        return _serve_video_response(cached)

    try:
        resp = http_requests.get(video_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print("Cider video proxy fetch failed: " + str(e))
        return Response("fetch failed", status=502)

    _cache_video(cache_key, resp.content)
    size_mb = len(resp.content) / (1024 * 1024)
    print("Cider video cached: " + filename + " (" + str(round(size_mb, 2)) + " MB)")
    return _serve_video_response(resp.content)


# ── Startup ──────────────────────────────────────────────

if __name__ == "__main__":
    # Cider poller — always start (gracefully handles Cider being offline)
    print("Starting Cider poller ...")
    cider_controller.start_polling()

    # Spotify setup — wrapped in try/except so Cider-only mode still works
    if sp:
        if SP_DC:
            print("SP_DC loaded (" + SP_DC[:8] + "...)")
            print("Capturing web player token (Chromium will flash briefly) ...")
            start_wp_token_refresh()
        else:
            print("WARNING: SP_DC not set in .env -- Canvas will not work")

        print("Starting Spotify poller ...")
        try:
            spotify_start_polling(sp)
        except Exception as e:
            print("WARNING: Spotify poller failed to start: " + str(e))
    else:
        print("Spotify unavailable — running in Cider-only mode")

    print("PiMusic server running on http://0.0.0.0:" + str(PORT))
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
