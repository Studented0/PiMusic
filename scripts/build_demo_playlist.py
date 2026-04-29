"""Generate static/demo/playlist.json from a list of Spotify track URLs.

Usage (from the repo root):
    # Edit static/demo/tracks.txt — one Spotify URL per line — then:
    python scripts/build_demo_playlist.py

    # Or pass URLs directly:
    python scripts/build_demo_playlist.py https://open.spotify.com/track/...

It reuses the same credentials the real server uses (.env → SPOTIPY_* and
SP_DC). For each URL it resolves: title, artist, album, duration, 640px
album art URL, and — if a Canvas is available — the canvas CDN URL from
Spotify's GraphQL. It then downloads the MP4 + JPG into static/demo/ so
the Vercel deployment can serve them directly (no CDN round-trip), and
samples the dominant color from the album art.

Commit static/demo/playlist.json plus the downloaded canvas-*.mp4 and
album-*.jpg files; Vercel picks them up on the next push.

No canvas? Not a problem. The track still plays, it just falls back to the
default Stick Talk CDN canvas the same way any of the bundled demos do.
"""

import json
import os
import re
import sys
import time

import dotenv
import requests

dotenv.load_dotenv()

# This script lives in scripts/; the repo root is one directory up.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_REPO_ROOT, "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

try:
    from spotify_auth import get_spotify_client, start_wp_token_refresh, get_web_player_tokens
    from spotify_controller import _canvas_graphql_request
except ImportError as exc:
    print(f"ERROR: could not import server modules ({exc})")
    sys.exit(1)


_DEMO_DIR = os.path.join(_REPO_ROOT, "static", "demo")
OUTPUT_PATH = os.path.join(_DEMO_DIR, "playlist.json")
TRACKS_TXT = os.path.join(_DEMO_DIR, "tracks.txt")


def _slug(text):
    """Lowercase-kebab slug for filenames (no diacritics, no punctuation)."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s or "track"


def _read_tracks_file():
    """Return the list of URLs in static/demo/tracks.txt (one per line),
    skipping blank lines and #-comments. Returns [] if the file is missing."""
    if not os.path.isfile(TRACKS_TXT):
        return []
    urls = []
    with open(TRACKS_TXT, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def _download(url, dest_path):
    """Stream URL → dest_path. Returns True on success."""
    if not url:
        return False
    try:
        r = requests.get(url, timeout=30, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
        return True
    except Exception as exc:
        print(f"  download failed ({url[:60]}...): {exc}")
        return False


def _dominant_color(image_path):
    """Sample a representative hex color from a downloaded album JPG.
    Falls back to the navy default if colorthief is unavailable or errors."""
    try:
        from colorthief import ColorThief
        r, g, b = ColorThief(image_path).get_color(quality=10)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception as exc:
        print(f"  color sampling failed: {exc}")
        return "#1a1a2e"


def _extract_track_id(url_or_id):
    """Accept a full share URL, a spotify URI, or a bare 22-char ID."""
    s = url_or_id.strip()
    m = re.search(r"(?:track[:/])([A-Za-z0-9]{22})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", s):
        return s
    return None


def _wait_for_web_tokens(timeout=40):
    """Wait for the Playwright token grabber to finish so canvas requests
    can hit the private GraphQL endpoint."""
    print("Waiting for web player token...", end="", flush=True)
    start = time.time()
    bearer = None
    while time.time() - start < timeout:
        bearer, _ = get_web_player_tokens()
        if bearer:
            print(" got it.")
            return True
        print(".", end="", flush=True)
        time.sleep(2)
    print(" gave up. Canvas lookups will be skipped.")
    return False


def _fetch_canvas(track_id):
    """Return the canvas CDN URL, or None if the track has no canvas."""
    bearer, client_token = get_web_player_tokens()
    if not bearer:
        return None
    try:
        status, cdn_url = _canvas_graphql_request(track_id, bearer, client_token)
        if status == 200:
            return cdn_url
        if status in (401, 403):
            # One retry after refreshing the token.
            start_wp_token_refresh()
            time.sleep(10)
            bearer2, ct2 = get_web_player_tokens()
            if bearer2:
                status2, cdn_url2 = _canvas_graphql_request(track_id, bearer2, ct2)
                if status2 == 200:
                    return cdn_url2
    except Exception as exc:
        print(f"  canvas fetch failed: {exc}")
    return None


def _fetch_track_meta(sp, track_id):
    """Return (title, artist, album, duration_ms, album_art_url)."""
    t = sp.track(track_id)
    title = t["name"]
    artist = ", ".join(a["name"] for a in t["artists"])
    album = t["album"]["name"]
    duration_ms = t["duration_ms"]
    images = t["album"].get("images", [])
    # Pick the 640px image (index 0 for Spotify's returns).
    art_url = images[0]["url"] if images else ""
    return title, artist, album, duration_ms, art_url


def main(urls):
    if not urls:
        urls = _read_tracks_file()
        if urls:
            print(f"Reading {len(urls)} URL(s) from {TRACKS_TXT}")
        else:
            print(f"No URLs given and {TRACKS_TXT} is empty/missing.")
            print("Usage: python scripts/build_demo_playlist.py [<spotify-url> ...]")
            sys.exit(1)

    track_ids = []
    for raw in urls:
        tid = _extract_track_id(raw)
        if not tid:
            print(f"  skipping (cant parse): {raw}")
            continue
        if tid in track_ids:
            print(f"  skipping duplicate: {tid}")
            continue
        track_ids.append(tid)

    if not track_ids:
        print("No valid Spotify track IDs found.")
        sys.exit(1)

    print(f"Resolving {len(track_ids)} track(s)...")
    sp = get_spotify_client()
    start_wp_token_refresh()
    have_canvas_tokens = _wait_for_web_tokens()

    playlist = []
    for i, tid in enumerate(track_ids, 1):
        print(f"[{i}/{len(track_ids)}] {tid}")
        try:
            title, artist, album, dur_ms, art_url = _fetch_track_meta(sp, tid)
        except Exception as exc:
            print(f"  skipping (metadata failed): {exc}")
            continue
        print(f"  {title} by {artist} ({dur_ms/1000:.0f}s)")

        canvas_cdn = _fetch_canvas(tid) if have_canvas_tokens else None
        if canvas_cdn:
            print(f"  canvas: {canvas_cdn[:60]}...")
        else:
            print("  no canvas (falls back to default idle canvas)")

        slug = _slug(title)
        canvas_local = f"canvas-{slug}.mp4"
        art_local = f"album-{slug}.jpg"

        os.makedirs(_DEMO_DIR, exist_ok=True)

        if canvas_cdn and _download(canvas_cdn, os.path.join(_DEMO_DIR, canvas_local)):
            print(f"  saved → {canvas_local}")

        color = "#1a1a2e"
        art_path = os.path.join(_DEMO_DIR, art_local)
        if art_url and _download(art_url, art_path):
            print(f"  saved → {art_local}")
            color = _dominant_color(art_path)

        playlist.append({
            "track_id": tid,
            "track": title,
            "artist": artist,
            "album": album,
            "duration_ms": dur_ms,
            "album_art_url": art_url,
            "canvas_cdn_url": canvas_cdn or "",
            "canvas_local": canvas_local,
            "art_local": art_local,
            "dominant_color": color,
        })

    if not playlist:
        print("Nothing to write.")
        sys.exit(1)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(playlist, fh, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(playlist)} track(s) to {OUTPUT_PATH}")
    print("Restart spotify_server.py (or redeploy to Vercel) to pick up changes.")


if __name__ == "__main__":
    main(sys.argv[1:])
