# Demo assets

Powers the Vercel demo and `DEMO_MODE=true` locally. Every track gets a bundled canvas MP4, album art JPG, and m4a audio file all sitting in this folder, so the demo plays full songs without Spotify auth or hitting the canvas CDN.

## Adding tracks

Edit `tracks.txt` with Spotify URLs (one per line), then from the repo root:

```
python scripts/build_demo_playlist.py
```

The script uses my `.env` Spotify creds to pull track info and the canvas URL, downloads everything, samples a dominant color from the art, then searches YouTube for the audio and grabs that too. Each YouTube match prints the title and uploader so I can sanity check before commit, just to make sure it didn't pull some weird remix or a 1 hour loop.

Commit everything in `static/demo/` and push, Vercel picks it up on redeploy.

## Fallbacks

Missing canvas falls back to album art. Missing audio plays silent. Build script writes empty strings for anything it couldn't find so partial matches don't break the playlist.
