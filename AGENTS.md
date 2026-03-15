# AGENTS.md

## Cursor Cloud specific instructions

### Overview

PiMusic is a single-service Python/Flask web application (cinematic Spotify + Apple Music display). There is no database, no Docker, no monorepo structure. See `README.md` for full feature list and API reference.

### Running the server

```bash
python3 spotify_server.py
```

The server listens on `http://0.0.0.0:5000`. The main player UI is at `/` and the settings page at `/settings`.

**Spotify credentials are required at startup.** The `SpotifyOAuth` constructor will fail if `SPOTIPY_CLIENT_ID` is not set. Set via a `.env` file or environment variables:

```bash
SPOTIPY_CLIENT_ID=placeholder SPOTIPY_CLIENT_SECRET=placeholder python3 spotify_server.py
```

Without real credentials, the server starts and serves the web UI, but the background Spotify poller logs repeated `EOFError` (it tries interactive OAuth in a non-TTY context). This is harmless — the Flask routes still work.

### Key gotchas

- **No test suite exists.** There are no automated tests, no test framework in `requirements.txt`, and no linting tools configured.
- **Playwright Chromium must be installed** separately after pip install: `python3 -m playwright install chromium`. Without it, Canvas token capture will fail at runtime.
- **First real OAuth requires interactive TTY.** Spotipy prompts the user to paste a redirect URL. This cannot complete in a background process; it requires a terminal session or pre-existing cached token at `~/pimusic/.spotify_cache`.
- **Settings persist to `~/pimusic/settings.json`**, not to the repo directory.
- **`pip install` goes to user site** (`~/.local/`) because system site-packages is not writable. Ensure `~/.local/bin` is on `PATH` (it is by default in most shells).
- **No build step.** Frontend is vanilla JS/CSS/HTML served directly by Flask.
