# PiMusic

A raspberry Pi desk display for Spotify and Apple Music. Shows whats playing, lets you skip tracks, and plays Spotify Canvas videos in the background.

**v2.0** | Python + Flask | Vanilla JS

---

## Screenshots

[Card mode with Canvas in artwork box]<img width="946" height="965" alt="Screenshot 2026-03-08 032807" src="https://github.com/user-attachments/assets/76271508-90b1-4d4c-8391-4987c300cea9" />


[Canvas Behind Artwork mode].<img width="939" height="974" alt="Screenshot 2026-03-08 040108" src="https://github.com/user-attachments/assets/e98f7299-f699-494a-8890-7e4b21579528" />


[Cinematic fullscreen mode]<img width="1005" height="927" alt="Screenshot 2026-03-12 070609" src="https://github.com/user-attachments/assets/d6a9b810-50e6-4a7c-a87e-24a7d04cb654" />



---



## Why I built this

I kept seeing things like carthing and other now-playing displays but they were all missing things, like Apple Music. I use Apple Music almost exclusively, and tabbing out to skip a song is not feasible long-term. The software side took longer than expected, I kind of went in blind and learned as I went. Spotify Canvas has no public API so I had to reverse engineer the GraphQL endpoint and capture tokens with Playwright. There were a lot of bugs. The Apple Music side was cleaner since Cider exposes a local API, but Apple Music side only works with Cider currently.

---

## Current Status

Software is complete and working. Case is designed, BOM is finalized, and Hardware is ready to assemble. Will likely need to tweak and optimize preformance for a Pi.

## Features

- **Dual-source playback** — Spotify and Apple Music (Cider) with automatic source detection and manual switching
- **Spotify Canvas** — Animated background videos from Spotify, including cross-source lookup for Apple Music tracks
- **Three visual modes** — Canvas in artwork box, canvas fullscreen behind card, or album artwork only
- **Cinematic mode** — Click album art to enter fullscreen; click anywhere to exit
- **Predictive progress bar** — Local timer with smooth drift correction
- **Optimistic controls** — Play/pause/skip update the UI instantly, before the API responds
- **Scrobble logging** — Timer-based scrobbler shared across both sources
- **Settings page** — Web UI to configure credentials, visual mode, CPU threshold, and more
- **Pi-safe** — CPU monitoring with automatic video disable when usage exceeds threshold

---

## Visual Modes

| Mode | Description |
|------|-------------|
| **Canvas in Artwork Box** | Canvas video plays inside the album art square. Click art to go fullscreen. |
| **Canvas Behind Artwork** | Canvas video fills the screen behind the centered Artwork UI. |
| **Album Artwork Only** | No video. Blurred album art background. |

Change the mode from the Settings page (`/settings`)

---

## How Canvas Works

Spotify Canvas is a short looping video that Spotify attaches to select tracks. PiMusic uses Canvas as its universal visual system across both Spotify and Apple Music.

### Native Spotify Tracks

When playing from Spotify, Canvas is fetched directly via Spotify's internal GraphQL Pathfinder API. Authentication uses an `sp_dc` cookie token, automatically captured and refreshed by Playwright in the background.

### Cross-Source Lookup for Apple Music

When playing from Apple Music via Cider, PiMusic searches Spotify for the same track and displays its Canvas. This means Apple Music tracks get animated backgrounds too — the key differentiator of PiMusic.

The search uses a three-tier fallback pipeline:

1. **Track + Artist** — `track:{name} artist:{artist}` (best match)
2. **Track only** — `track:{name}` (handles artist name differences)
3. **Artist only** — `artist:{artist}` (last resort, still gets a Canvas from the same artist)

Before searching, track names are normalized — stripping `(feat. ...)`, `(ft. ...)`, `(with ...)`, `(... version)`, and `(... remix)` suffixes that often differ between Apple Music and Spotify metadata.




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
- **Cider**  — [cider.sh](https://cider.sh) for Apple Music support

---

## Hardware 

 - Raspberry Pi 3B+
 - HyperPixel 4.0 Touch
 - Micro USB PSU 5.1V 2.5A (For Pi)
 - microSD card (32gb but anything at or above 16 works)

---

## Wiring 
The HyperPixel 4.0 connects directly to the Pi's 40-pin GPIO. No additional wiring needed.

---

## Case
Case for the Pi and display with a 17° wedge stand. Prints as one piece, no supports needed.
## Installation

```bash
git clone https://github.com/Studented0/PiMusic.git
cd PiMusic
pip install -r requirements.txt
python -m playwright install chromium
```

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Then edit `.env` with your values:

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
| `SP_DC` | No(only for canvas) | Spotify `sp_dc` cookie for Canvas/web player token |

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
├── BOM.csv                  # Bill of materials with links
├── .env                     # Environment variables (not tracked)
├── .gitattributes
├── static/
│   ├── app.js               # Frontend: polling, rendering, controls, cinematic toggle
│   ├── style.css            # All visual styling and layout modes
│   └── settings.js          # Settings page logic
├── templates/
│   ├── index.html           # Main player page
│   └── settings.html        # Settings page
├── canvas-finder/           # Canvas URL lookup tooling
├── cad/                     # SolidWorks files, STEP
└── renders/                 # Assembly renders
```



---

## License

This project is licensed under the [MIT License](LICENSE).
