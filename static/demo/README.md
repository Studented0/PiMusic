# Demo assets

These files power PiMusic's demo mode (`DEMO_MODE=true` or the Vercel
deployment). The frontend reads from `playlist.json` and prefers the
bundled MP4/JPG files in this folder, falling back to public Spotify CDN
URLs only if a local file is missing.

## Adding or changing tracks

1. Edit [`tracks.txt`](tracks.txt) — one Spotify track URL per line.
2. From the repo root, run:
   ```
   python scripts/build_demo_playlist.py
   ```
   This uses your `.env` Spotify credentials (`SPOTIPY_*` and `SP_DC`) to
   fetch each track's title, artist, album, duration, 640px album art,
   and canvas CDN URL — then downloads the MP4 + JPG into this folder
   and samples the dominant color from the art.
3. Commit everything in `static/demo/` and push. Vercel serves the
   bundled MP4s directly with no CDN round-trip.

The script can also take URLs as CLI args if you'd rather not edit
`tracks.txt`:

```
python scripts/build_demo_playlist.py https://open.spotify.com/track/...
```

## Generated files

The script produces:

- `playlist.json` — the resolved metadata Vercel reads at runtime
- `canvas-<slug>.mp4` — one per track that has a canvas
- `album-<slug>.jpg` — 640×640 album art per track

If a canvas MP4 is missing for a track, `demo_state.py` falls back to the
canvas CDN URL stored in `playlist.json` (or the hardcoded Stick Talk
canvas if that's also empty). Album art falls back to `i.scdn.co` the
same way.
