# PiMusic

A Raspberry Pi desk display for Spotify and Apple Music. Shows what's playing, lets you skip tracks, and plays Spotify Canvas videos in the background. Ships with a rotary encoder for physical volume and playback control.

**v2.1** | Python + Flask | Vanilla JS

---

## Screenshots

[Card mode with Canvas in artwork box]<img width="946" height="965" alt="Screenshot 2026-03-08 032807" src="https://github.com/user-attachments/assets/76271508-90b1-4d4c-8391-4987c300cea9" />


[Canvas Behind Artwork mode].<img width="939" height="974" alt="Screenshot 2026-03-08 040108" src="https://github.com/user-attachments/assets/e98f7299-f699-494a-8890-7e4b21579528" />


[Cinematic fullscreen mode]<img width="1005" height="927" alt="Screenshot 2026-03-12 070609" src="https://github.com/user-attachments/assets/d6a9b810-50e6-4a7c-a87e-24a7d04cb654" />



---



## Why I built this

I kept seeing things like Car Thing and other now-playing displays but they were all missing things, like Apple Music. I use Apple Music almost exclusively, and tabbing out to skip a song is not feasible long-term. The software side took longer than expected, I kind of went in blind and learned as I went. Spotify Canvas has no public API so I had to reverse engineer the GraphQL endpoint and capture tokens with Playwright. There were a lot of bugs. The Apple Music side was cleaner since Cider exposes a local API, but Apple Music side only works with Cider currently.

---

## Current Status

Software is complete and working on a Pi 3B+ with the HyperPixel 4.0 display. Case is designed, BOM is finalized, and hardware is ready to assemble. The rotary encoder firmware is done and tested with a SparkFun Pro Micro.

## Features

- **Dual-source playback** — Spotify and Apple Music (Cider) with automatic source detection and manual switching
- **Spotify Canvas** — Animated background videos from Spotify, including cross-source lookup for Apple Music tracks
- **Three visual modes** — Canvas in artwork box, canvas fullscreen behind card, or album artwork only
- **Cinematic mode** — Click album art to enter fullscreen; sticks across tracks so it doesn't bail out between songs
- **Idle canvas screensaver** — When nothing's playing, a pre-picked Spotify Canvas (Stick Talk by Future, by default) takes over the background so the display never feels dead
- **Rotary encoder control** — Pro Micro + E11 encoder handles volume, play/pause, skip, previous, and opening settings, all without touching the screen
- **Predictive progress bar** — Local clock with discrete resyncs on pause/seek/skip (no more choppy drift correction)
- **Optimistic controls** — Play/pause/skip update the UI instantly, before the API responds
- **Scrobble logging** — Timer-based scrobbler shared across both sources
- **Settings page** — Web UI to configure credentials, visual mode, CPU threshold, and more. Fully navigable with just the encoder.
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

### Idle Canvas Screensaver

When nothing's playing, PiMusic doesn't just sit on a dead screen. A pre-picked Canvas video takes over as a fullscreen screensaver, with just the source toggle, settings gear, and a clean "No music playing" label on top — nothing else. Default canvas is **Stick Talk by Future**.

**Dismiss and return**

- Tap anywhere on the screen → screensaver fades out, regular idle UI comes back with all the buttons (play/pause, skip, volume, progress bar).
- Tap the album artwork area → screensaver comes right back.
- Don't touch anything for 15 seconds → screensaver comes back on its own.
- Play a track → screensaver exits for good until you stop playing again.

Every touch or encoder press resets the 15-second countdown, so the screensaver won't yank you away while you're actually using the UI.

**Changing the screensaver**

Edit this list at the top of `spotify_controller.py`:

```python
IDLE_CANVAS_TRACK_IDS = [
    "20fAoPjfYltmd3K3bO7gbt",  # Stick Talk - Future
    # Add more track IDs here to rotate through them later
]
```

To grab a track's Spotify ID: open the track in Spotify → right-click → Share → Copy Song Link. The ID is the 22-character string after `/track/` in the URL, before the `?si=...` part.

The server pre-fetches every listed canvas on startup, so the screensaver shows up the instant it's needed. Only tracks with an actual Canvas work — if Spotify returns nothing, PiMusic just skips that ID. Today it uses the first available entry; adding rotation between multiple canvases later is a straightforward swap in `get_idle_canvas()`.

---

## Rotary Encoder

The encoder is an E11 rotary with a push button, wired to a SparkFun Pro Micro (ATmega32U4). The Pro Micro shows up as a USB keyboard, so the Pi kiosk just sees arrow keys and spacebar presses. No driver, no serial protocol, no extra endpoint — the web app listens for keydown events directly.

### Wiring

| Encoder pin | Pro Micro pin |
|-------------|---------------|
| A           | 2             |
| B           | 3             |
| Button      | 4             |
| Common / GND | GND          |

All three inputs use internal pull-ups, so no external resistors needed.

### Firmware

Sketch lives at `firmware/pimusic_encoder/pimusic_encoder.ino`. Flash it from the Arduino IDE with the board set to **SparkFun Pro Micro** (or Arduino Leonardo). It sends:

- CW rotation → `KEY_DOWN_ARROW` (volume down)
- CCW rotation → `KEY_UP_ARROW` (volume up)
- Button → `Space` (multi-press detection on the web side)

### Encoder actions in the web UI

| Action | How |
|--------|-----|
| Volume down | Turn CW (step of 2) |
| Volume up | Turn CCW (step of 2) |
| Play / pause | Single press |
| Next track | Double press |
| Previous / restart | Triple press |
| Open settings | 4+ presses |

On the settings page, the encoder also works as navigation — turn to move between options, press to activate, and turn again inside a slider to adjust the value. Volume changes are debounced 450 ms so rapid spins don't hit the Spotify rate limit.

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Server | Python 3, Flask |
| Spotify API | Spotipy, Playwright (token capture) |
| Apple Music | Cider local API |
| Canvas fetch | GraphQL via curl_cffi (Chrome TLS impersonation) |
| Frontend | Vanilla JS, CSS, HTML |
| Scrobbler | Monotonic timer with 2s drift threshold |
| Monitoring | psutil for CPU usage |
| Encoder firmware | Arduino C++ on Pro Micro (ATmega32U4) |

---

## Prerequisites

- **Python 3.10+**
- **Spotify Premium** account
- **Spotify Developer App** — Create one at [developer.spotify.com](https://developer.spotify.com/dashboard) to get a Client ID, Client Secret, and Redirect URI
- **SP_DC cookie** — Extract from your browser's Spotify cookies (needed for Canvas)
- **Chromium** — Installed automatically by Playwright on first run
- **Cider** — [cider.sh](https://cider.sh) for Apple Music support
- **Arduino IDE** (optional) — Only if you want to flash the encoder firmware

---

## Hardware

- Raspberry Pi 3B+
- HyperPixel 4.0 Touch
- Micro USB PSU 5.1V 2.5A (for Pi)
- microSD card (32gb but anything at or above 16 works)
- SparkFun Pro Micro (ATmega32U4)
- E11 rotary encoder with push button
- Micro USB cable (Pro Micro → Pi)

---

## Wiring

The HyperPixel 4.0 connects directly to the Pi's 40-pin GPIO. No additional wiring needed.

The Pro Micro plugs into the Pi over USB. Encoder A/B/button wire to pins 2/3/4 on the Pro Micro (see the Rotary Encoder section above).

---

## Case

Case for the Pi and display with a 17° wedge stand. Prints as one piece, no supports needed. There's also a separate enclosure for the encoder/volume knob in the `CAD/` folder.

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

### Pi kiosk

Run Chromium in kiosk mode pointed at the server:

```bash
DISPLAY=:0 chromium --kiosk --noerrdialogs --disable-infobars --disable-gpu --disable-smooth-scrolling http://<your-pc-ip>:5000
```

`--disable-gpu` is intentional on the 3B+ — the software compositor is smoother than the shaky GL driver on this hardware.

### Windows autostart (optional)

If you're running the server on a Windows PC, there are a couple of helper scripts in the repo:

- `start-pimusic-hidden.vbs` — launches the server silently in the background
- `debug-pimusic.bat` — kills the background task and runs the server in a visible console for debugging
- `view-log.bat` — tails `server.log`

Drop a shortcut to the VBS into your Startup folder or a Scheduled Task set to "At log on" to have the server come up with the PC.

---

## Configuration

### Environment variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `SPOTIPY_CLIENT_ID` | Yes | Spotify app Client ID |
| `SPOTIPY_CLIENT_SECRET` | Yes | Spotify app Client Secret |
| `SPOTIPY_REDIRECT_URI` | Yes | OAuth redirect URI (default `http://127.0.0.1:8080`) |
| `SP_DC` | No (only for canvas) | Spotify `sp_dc` cookie for Canvas/web player token |

### Settings page

Navigate to `/settings` in your browser (or 4-press the encoder) to configure:

- Spotify credentials (SP_DC, Client ID/Secret, Redirect URI)
- Cider API token and host
- Visual mode (Canvas in Artwork Box / Canvas Behind Artwork / Artwork Only)
- CPU threshold for automatic video disable
- Scanline overlay toggle
- Auto cinematic mode toggle
- Clear album art cache

Settings are persisted to `~/pimusic/settings.json`.

---


### Controls

| Action | How |
|--------|-----|
| Play / Pause | Click the center button, or single-press the encoder |
| Next / Previous | Click the skip buttons, or double/triple-press the encoder |
| Seek | Drag the progress bar |
| Volume | Drag the volume slider, or turn the encoder |
| Switch source | Click the source badge dropdown (Spotify / Apple Music) |
| Fullscreen | Click the album art |
| Exit fullscreen | Click the video, background, or empty space |
| Settings | Click the gear icon, go to `/settings`, or 4-press the encoder |

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
| POST | `/api/clear-cache` | Delete the on-disk album art cache |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system/cpu` | CPU usage and video throttle status |
| POST | `/api/force-reauth` | Clear Spotify token cache and re-authenticate |
| POST | `/api/spotify/reauth` | Trigger a Playwright web-player token refresh |
| POST | `/api/hid/input` | HTTP-based HID input (`{ "action": "next" }`) — legacy, encoder uses USB HID now |

### Static

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/art/<filename>` | Cached album artwork |
| GET | `/api/canvas/<filename>` | Canvas video proxy (streamed from RAM) |

---

## Project Structure

```
PiMusic/
├── spotify_server.py        # Flask server, routes, settings persistence, canvas proxy
├── spotify_controller.py    # Spotify API polling, Canvas GraphQL fetch
├── spotify_auth.py          # Spotipy auth, Playwright token capture
├── cider_controller.py      # Cider (Apple Music) polling, Spotify Canvas cross-lookup
├── source_manager.py        # Active source detection, command dispatch
├── scrobbler.py             # Timer-based scrobble tracker
├── resource_monitor.py      # CPU monitoring, video disable threshold
├── album_cache.py           # Album art download, dominant color extraction, quota pruning
├── requirements.txt         # Python dependencies
├── BOM.csv                  # Bill of materials with links
├── .env.example             # Template for credentials
├── .gitignore
├── .gitattributes
├── start-pimusic-hidden.vbs # Windows: launch server silently at login
├── debug-pimusic.bat        # Windows: run server in visible console
├── view-log.bat             # Windows: tail server.log
├── static/
│   ├── app.js               # Frontend: polling, rendering, controls, cinematic toggle, encoder keydown handling
│   ├── style.css            # All visual styling and layout modes
│   └── settings.js          # Settings page logic, encoder navigation
├── templates/
│   ├── index.html           # Main player page
│   └── settings.html        # Settings page
├── firmware/
│   └── pimusic_encoder/     # Arduino sketch for the Pro Micro encoder
├── canvas-finder/           # Canvas URL lookup tooling
├── CAD/                     # SolidWorks files, STEP, and STL for the case and knob enclosure
└── renders/                 # Assembly renders
```



---

## Recent Changes

A pile of reliability work on top of v2.0:

- **Idle canvas screensaver** — When nothing is playing, the display shows a pre-picked Canvas video fullscreen instead of sitting on a dead "No music playing" screen. Minimal UI (source toggle + settings gear + label, no buttons or progress bar cluttering it). Tap to dismiss back to the normal idle UI, tap the artwork to bring it back, or let 15 seconds of inactivity return it automatically. Configurable list of track IDs, pre-fetched on startup.
- **Rotary encoder support** — Pro Micro firmware and web-side keydown handling for volume, play/pause, next, previous, and opening settings. Settings page is fully navigable with just the encoder.
- **Canvas reliability** — Bounded per-track URL cache, deduplicated in-flight CDN downloads, token refresh on both 401 and 403, and Chrome TLS impersonation via `curl_cffi` for the CDN fetch (Spotify's CDN fingerprints clients).
- **Album art race fixes** — Art caching moved off the poll thread, atomic file writes, client-side token guard so a slow image load can't overwrite newer state, and an on-disk quota that prunes to 200 MB.
- **Smoother scrobbler** — Replaced continuous drift correction with discrete resyncs on pause/unpause/seek/skip. Progress bar uses GPU-composited transforms and throttles to ~30 fps.
- **Thread-safety audit** — `_canvas_lock` is never held across callbacks or `_apply_canvas` anymore (was causing self-deadlocks on cache hits). Playwright token capture is gated by a flag so bursts of 401s can't spawn multiple Chromium instances.
- **Touchscreen polish** — Bigger touch targets on the settings page, custom tile picker for visual mode, and a multi-press encoder shortcut for opening settings since the gear icon is tiny on an 800x480 display.

---

## License

This project is licensed under the [MIT License](LICENSE).
