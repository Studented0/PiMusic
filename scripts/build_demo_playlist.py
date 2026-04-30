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

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

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


# Title keywords that almost always indicate a wrong-version YouTube hit
# (remixes, hour-long loops, fan covers, etc.). We hard-reject any
# candidate whose title contains one of these — *unless* the original
# Spotify track title also contains the keyword, which catches edge cases
# like "Live Forever" or actual official remixes the user picked.
_BAD_KEYWORDS = (
    "remix", "cover", "live performance", "live at", "live in",
    "1 hour", "10 hour", "loop", "8d", "slowed", "reverb",
    "sped up", "speed up", "instrumental", "karaoke", "reaction",
    "lyrics", "lyric video", "nightcore", "mashup", "tutorial",
)


def _score_youtube_match(entry, track, artist, expected_s):
    """Return a score where higher is better, or None if disqualified.
    Designed so the obviously-correct match (Topic channel, exact title,
    near-identical duration) wins by a wide margin."""
    title = (entry.get("title") or "").lower()
    uploader = (entry.get("uploader") or entry.get("channel") or "").lower()
    duration = entry.get("duration")
    if not duration:
        return None

    track_lower = track.lower()
    for bad in _BAD_KEYWORDS:
        if bad in title and bad not in track_lower:
            return None

    # Reject extreme version mismatches (>120s drift = wrong song or
    # wildly different cut). Within 120s we trust the scoring below.
    diff = abs(duration - expected_s)
    if diff > 120:
        return None

    # Closer duration = higher score. ~1 point per second of drift.
    score = -diff

    # "Artist - Topic" channels are auto-generated official audio — the
    # single strongest signal that this is the right upload.
    if uploader.endswith("- topic"):
        score += 60

    # Exact track title in the video title.
    if track_lower in title:
        score += 25

    # Uploader name overlaps with the artist (handles "Future", "Daft Punk",
    # collabs like "Daft Punk, Pharrell Williams, Nile Rodgers").
    for a in artist.lower().split(","):
        a = a.strip()
        if a and a in uploader:
            score += 15
            break

    # "Official audio" / "official video" uploads.
    if "official" in title:
        score += 5

    return score


def _find_youtube_match(track, artist, expected_duration_ms):
    """Search YouTube for the best audio match of (track, artist).
    Returns the YouTube video ID, or None if nothing scores well enough."""
    if yt_dlp is None:
        return None

    query = f"{track} {artist}"
    expected_s = (expected_duration_ms or 0) / 1000.0

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch10:{query}", download=False)
    except Exception as exc:
        print(f"  search failed: {exc}")
        return None

    candidates = (result or {}).get("entries") or []

    best, best_score = None, float("-inf")
    for c in candidates:
        if not c:
            continue
        score = _score_youtube_match(c, track, artist, expected_s)
        if score is None:
            continue
        if score > best_score:
            best_score = score
            best = c

    if not best:
        return None

    title = best.get("title", "?")
    uploader = best.get("uploader") or best.get("channel") or "?"
    dur = best.get("duration") or 0
    drift = dur - expected_s
    sign = "+" if drift >= 0 else ""
    print(f"  matched: '{title}' by {uploader}  ({dur:.0f}s, {sign}{drift:.0f}s)")
    return best.get("id")


def _download_audio(video_id, dest_dir, slug):
    """Download YouTube audio as m4a (no ffmpeg needed). Returns
    (filename, duration_ms) on success, ('', 0) on failure."""
    if yt_dlp is None or not video_id:
        return "", 0
    outtmpl = os.path.join(dest_dir, f"audio-{slug}.%(ext)s")
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=True
            )
        ext = info.get("ext", "m4a")
        duration_ms = int(round((info.get("duration") or 0) * 1000))
        return f"audio-{slug}.{ext}", duration_ms
    except Exception as exc:
        print(f"  audio download failed: {exc}")
        return "", 0


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

        # Find + grab a matching YouTube audio track. We search by title +
        # artist, score candidates against Spotify's reported duration,
        # and reject anything that smells like a remix/cover/loop.
        audio_local = ""
        audio_duration_ms = 0
        yt_id = _find_youtube_match(title, artist, dur_ms)
        if yt_id:
            audio_local, audio_duration_ms = _download_audio(yt_id, _DEMO_DIR, slug)
            if audio_local:
                print(f"  saved → {audio_local} ({audio_duration_ms / 1000:.0f}s)")

        # Use the YouTube file's actual duration when we got one — that's
        # what's actually playing, so progress + auto-advance should match.
        # Fall back to Spotify's duration only if no audio was downloaded.
        effective_duration = audio_duration_ms if audio_duration_ms > 0 else dur_ms

        playlist.append({
            "track_id": tid,
            "track": title,
            "artist": artist,
            "album": album,
            "duration_ms": effective_duration,
            "album_art_url": art_url,
            "canvas_cdn_url": canvas_cdn or "",
            "canvas_local": canvas_local,
            "art_local": art_local,
            "audio_local": audio_local,
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
