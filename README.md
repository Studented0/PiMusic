# PiMusic

A cinematic music display for Spotify and Apple Music (via Cider), built to run on a Raspberry Pi.

**v2.0** | Python + Flask | Vanilla JS

---

## Screenshots

![Card mode with Canvas in artwork box](screenshots/canvas_card.png)

![Canvas Behind Artwork mode](screenshots/canvas_bg.png)

![Cinematic fullscreen mode](screenshots/cinematic.png)

> Replace the images above with your own screenshots. Create a `screenshots/` folder in the project root and add `canvas_card.png`, `canvas_bg.png`, and `cinematic.png`.

---

## Features

- **Dual-source playback** — Spotify and Apple Music (Cider) with automatic source detection and manual switching
- **Spotify Canvas** — Animated background videos from Spotify, including cross-source lookup for Apple Music tracks
- **Three visual modes** — Canvas in artwork box, canvas fullscreen behind card, or album artwork only
- **Cinematic mode** — Click album art to enter fullscreen; click anywhere to exit
- **Predictive progress bar** — Local timer with smooth drift correction, no jitter
- **Optimistic controls** — Play/pause/skip update the UI instantly, before the API responds
- **Scrobble logging** — Timer-based scrobbler shared across both sources, logs to `~/pimusic/scrobbles.log`
- **Settings page** — Web UI to configure credentials, visual mode, CPU threshold, and more
- **Pi-safe** — CPU monitoring with automatic video disable when usage exceeds threshold
- **ESP32 HID support** — HTTP endpoint for external hardware controls

---

## Visual Modes

| Mode | Description |
|------|-------------|
| **Canvas in Artwork Box** | Canvas video plays inside the album art square. Click art to go fullscreen. |
| **Canvas Behind Artwork** | Canvas video fills the screen behind the centered card UI. |
| **Album Artwork Only** | No video. Blurred album art background. |

Change the mode from the Settings page (`/settings`) or via the API.

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Server | Python 3, Flask |
| Spotify API | Spotipy, Playwright (token capture) |
| Apple Music | Cider local API |
| Canvas fetch | GraphQL via curl_cffi |
| Frontend | Vanilla JS, CSS, HTML |
| Scrobbler | Monotonic timer with 2s drift threshold |
| Monitoring | psutil for CPU usage |

---

## Prerequisites

- **Python 3.10+**
- **Spotify Premium** account
- **Spotify Developer App** — Create one at [developer.spotify.com](https://developer.spotify.com/dashboard) to get a Client ID, Client Secret, and Redirect URI
- **SP_DC cookie** — Extract from your browser's Spotify cookies (needed for Canvas)
- **Chromium** — Installed automatically by Playwright on first run
- **Cider** (optional) — [cider.sh](https://cider.sh) for Apple Music support

---

## Installation

```bash
git clone https://github.com/Studented0/PiMusic.git
cd PiMusic
pip install -r requirements.txt
python -m playwright install chromium
```

Create a `.env` file in the project root:

```env
SPOTIPY_CLIENT_ID=your_client_id
SPOTIPY_CLIENT_SECRET=your_client_secret
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8080
SP_DC=your_sp_dc_cookie
```

On first run, Spotipy will open a browser for OAuth authorization. Paste the redirect URL back into the terminal to complete authentication.

---

## Configuration

### Environment variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SPOTIPY_CLIENT_ID` | Yes | Spotify app Client ID |
| `SPOTIPY_CLIENT_SECRET` | Yes | Spotify app Client Secret |
| `SPOTIPY_REDIRECT_URI` | Yes | OAuth redirect URI (default `http://127.0.0.1:8080`) |
| `SP_DC` | Yes | Spotify `sp_dc` cookie for Canvas/web player token |

### Settings page

Navigate to `/settings` in your browser to configure:

- Spotify credentials (SP_DC, Client ID/Secret, Redirect URI)
- Cider API token and host
- Visual mode (Canvas in Artwork Box / Canvas Behind Artwork / Artwork Only)
- CPU threshold for automatic video disable
- Scanline overlay toggle
- Auto cinematic mode toggle

Settings are persisted to `~/pimusic/settings.json`.

---

## Usage

Start the server:

```bash
python spotify_server.py
```

Open in your browser:

```
http://127.0.0.1:5000
```

### Controls

| Action | How |
|--------|-----|
| Play / Pause | Click the center button |
| Next / Previous | Click the skip buttons |
| Seek | Drag the progress bar |
| Volume | Drag the volume slider |
| Switch source | Click the source badge dropdown (Spotify / Apple Music) |
| Fullscreen | Click the album art |
| Exit fullscreen | Click the video, background, or empty space |
| Settings | Click the gear icon or go to `/settings` |

---

## API Endpoints

### Playback state

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/state` | Current track, progress, canvas URL, visual mode |
| POST | `/api/play` | Resume playback |
| POST | `/api/pause` | Pause playback |
| POST | `/api/next` | Skip to next track |
| POST | `/api/previous` | Go to previous track |
| POST | `/api/seek` | Seek to position (`{ "position_ms": 30000 }`) |
| POST | `/api/volume` | Set volume (`{ "volume": 75 }`) |
| POST | `/api/force-poll` | Force immediate state refresh |

### Source management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/source` | Get active source (`spotify` or `cider`) |
| POST | `/api/source` | Set active source (`{ "source": "cider" }`) |

### Settings

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/settings` | Get all settings |
| POST | `/api/settings` | Update settings (partial or full JSON) |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system/cpu` | CPU usage and video throttle status |
| POST | `/api/force-reauth` | Clear Spotify token cache and re-authenticate |
| POST | `/api/hid/input` | ESP32 HID input (`{ "action": "next" }`) |

### Static

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/art/<filename>` | Cached album artwork |
| GET | `/api/canvas/<filename>` | Canvas video proxy (streamed from RAM) |

---

## Project Structure

```
PiMusic/
├── spotify_server.py        # Flask server, routes, settings persistence
├── spotify_controller.py    # Spotify API polling, Canvas GraphQL fetch
├── spotify_auth.py          # Spotipy auth, Playwright token capture
├── cider_controller.py      # Cider (Apple Music) polling, Spotify Canvas cross-lookup
├── source_manager.py        # Active source detection, command dispatch
├── scrobbler.py             # Timer-based scrobble tracker
├── resource_monitor.py      # CPU monitoring, video disable threshold
├── album_cache.py           # Album art download, dominant color extraction
├── requirements.txt         # Python dependencies
├── .env                     # Environment variables (not tracked)
├── static/
│   ├── app.js               # Frontend: polling, rendering, controls, cinematic toggle
│   ├── style.css            # All visual styling and layout modes
│   └── settings.js          # Settings page logic
├── templates/
│   ├── index.html           # Main player page
│   └── settings.html        # Settings page
└── canvas-finder/           # Rust helper for Canvas discovery (optional)
    ├── Cargo.toml
    └── src/main.rs
```

---

## Raspberry Pi Notes

PiMusic is designed to run on a **Raspberry Pi 3 B+** or better.

- **CPU threshold** — Set via Settings. When CPU usage exceeds the threshold, Canvas video is automatically disabled and the UI falls back to album artwork. Default is 75%.
- **Video decoding** — All video rendering happens in the browser. Python only provides the Canvas URL.
- **Canvas proxy** — Canvas MP4s are streamed through RAM, never written to disk, to protect SD card / SSD health.
- **Recommended browser** — Chromium in kiosk mode for the best fullscreen experience.

### Kiosk mode example

```bash
chromium-browser --kiosk --noerrdialogs --disable-infobars http://127.0.0.1:5000
```

---

## License

This project is not currently licensed. Add a `LICENSE` file to specify terms.
