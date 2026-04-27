# Demo assets

These files power PiMusic's demo mode (`DEMO_MODE=true` or the Vercel
deployment). When a bundled file is missing, `demo_state.py` falls back to
a public Spotify CDN URL, so the demo still works with an empty folder —
it just won't be offline-capable.

## Expected files

Drop any of these in to override the CDN fallback:

### Canvas videos (MP4, 9:16, short looping, ~1–3 MB each)
- `canvas-let-it-happen.mp4` — Tame Impala, Let It Happen
- `canvas-stick-talk.mp4` — Future, Stick Talk
- `canvas-get-lucky.mp4` — Daft Punk feat. Pharrell, Get Lucky

### Album art (JPG, 640×640 is fine)
- `album-let-it-happen.jpg`
- `album-stick-talk.jpg`
- `album-get-lucky.jpg`

## Where the fallbacks come from

If a local file is missing:
- Canvas: falls back to the Stick Talk canvas URL already hardcoded in
  `demo_state.py` (proven to serve cleanly from `canvaz.scdn.co`).
- Album art: falls back to the public `i.scdn.co` image URL for each album.

These CDNs are open / unauthenticated, so nothing in the demo requires API
keys or running Playwright.

## Adding another track

Edit `_PLAYLIST` in `demo_state.py` at the repo root. Fields are:

```python
{
  "track_id":       "<any unique string>",
  "track":          "Track Name",
  "artist":         "Artist",
  "album":          "Album",
  "duration_ms":    467000,
  "album_art_url":  "https://i.scdn.co/image/...",
  "canvas_local":   "canvas-slug.mp4",
  "art_local":      "album-slug.jpg",
  "dominant_color": "#hex",
}
```

Then drop the matching `canvas-slug.mp4` / `album-slug.jpg` in this folder
(optional — it'll use the CDN fallback if missing).
