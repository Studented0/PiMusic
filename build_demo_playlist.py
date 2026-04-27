"""Generate static/demo/playlist.json from a list of Spotify track URLs.

Usage (PowerShell):
    python build_demo_playlist.py `
      "https://open.spotify.com/track/2X485T9Z5Ll0iQDZpX4TZS" `
      "https://open.spotify.com/track/20fAoPjfYltmd3K3bO7gbt"

It reuses the same credentials the real server uses (.env → SPOTIPY_* and
SP_DC). For each URL it resolves: title, artist, album, duration, 640px
album art URL, and — if a Canvas is available — the canvas CDN URL from
Spotify's GraphQL. The output file is then picked up by demo_state.py
automatically the next time the server boots.

No canvas? Not a problem. The track still plays, it just falls back to the
default Stick Talk CDN canvas the same way any of the bundled demos do.
"""

import json
import os
import re
import sys
import time

import dotenv

dotenv.load_dotenv()

try:
    from spotify_auth import get_spotify_client, start_wp_token_refresh, get_web_player_tokens
    from spotify_controller import _canvas_graphql_request
except ImportError as exc:
    print(f"ERROR: this script has to run from the PiMusic repo root ({exc})")
    sys.exit(1)


OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static", "demo", "playlist.json",
)


def _slug(text):
    """Lowercase-kebab slug for filenames (no diacritics, no punctuation)."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s or "track"


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
        print("Usage: python build_demo_playlist.py <spotify-url> [<spotify-url> ...]")
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
        playlist.append({
            "track_id": tid,
            "track": title,
            "artist": artist,
            "album": album,
            "duration_ms": dur_ms,
            "album_art_url": art_url,
            "canvas_cdn_url": canvas_cdn or "",
            "canvas_local": f"canvas-{slug}.mp4",
            "art_local": f"album-{slug}.jpg",
            "dominant_color": "#1a1a2e",
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
