# PiMusic

A physical desk display for Spotify and Apple Music. Built around a Raspberry Pi 3B+ and a HyperPixel 4.0 touchscreen. Shows what's playing, plays Spotify Canvas videos as animated backgrounds, and lets you control playback with a rotary encoder knob.

**v2.1** — Python + Flask + Vanilla JS

---

## Screenshots

<img width="946" height="965" alt="Card mode" src="https://github.com/user-attachments/assets/76271508-90b1-4d4c-8391-4987c300cea9" />
<img width="939" height="974" alt="Canvas behind artwork" src="https://github.com/user-attachments/assets/e98f7299-f699-494a-8890-7e4b21579528" />
<img width="1005" height="927" alt="Cinematic fullscreen" src="https://github.com/user-attachments/assets/d6a9b810-50e6-4a7c-a87e-24a7d04cb654" />

---

## Why I built this

I use Apple Music. Every similar project I could find, Car Thing, Pixoo, Tidbyt only supports Spotify. I wanted a physical display on my desk that actually worked with what I use.

---

## The build

The display is a HyperPixel 4.0, which slots directly onto the Pi's 40-pin GPIO header, no extra wiring. It runs at 800×480 over a DPI interface.

The server doesn't run on the Pi. I tried that first and the 3B+ can't handle Python, Playwright, Spotify polling, and a browser at the same time without performance issues. The Flask server runs on a Windows PC on the same local network and the Pi runs Chromium in kiosk mode pointed at it. The Pi just renders HTML and CSS, all the dirty work happens on the PC.

The rotary encoder is an EC11 wired to a SparkFun Pro Micro (ATmega32U4). The Pro Micro shows up as a USB HID keyboard so the browser picks up keydown events directly. The encoder firmware decodes A/B pulses using a 16-entry gray-code lookup table on `(prevAB << 2) | currentAB` to tell apart CW, CCW, and invalid states. Multi-press logic runs in the browser with a ~400ms window. The firmware just sends spacebar.

The case is a 17° wedge stand, prints in one piece with no supports. Theres a separate enclosure for the encoder in `CAD/`.

---

## Hardware

- Raspberry Pi 3B+
- HyperPixel 4.0 Touch (PIM369) — 800×480, DPI interface
- 5.1V 2.5A micro USB PSU
- 16GB+ microSD card
- SparkFun Pro Micro (ATmega32U4)
- EC11 rotary encoder with push button
- Micro USB cable (Pro Micro to Pi)
- Windows PC on the same local network (runs the server)

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Server | Python 3, Flask |
| Spotify API | Spotipy, Playwright (token capture) |
| Apple Music | Cider local API |
| Canvas fetch | GraphQL via curl_cffi (Chrome TLS impersonation) |
| Frontend | Vanilla JS, CSS, HTML |
| Scrobbler | timer with 2s drift threshold |
| Monitoring | psutil for CPU usage |
| Encoder firmware | Arduino C++ on Pro Micro (ATmega32U4) |

---

## Firmware

Sketch is in `firmware/pimusic_encoder/pimusic_encoder.ino`. Flash it from Arduino IDE with the board set to **SparkFun Pro Micro** or **Arduino Leonardo**.

### Encoder Wiring

| Encoder pin | Pro Micro pin |
|-------------|---------------|
| A           | 2             |
| B           | 3             |
| Button      | 4             |
| Common / GND | GND          |

All three on internal pull-ups, so no external resistors are needed.

### Controls

| Action | How |
|--------|-----|
| Volume down | Turn CW |
| Volume up | Turn CCW |
| Play / pause | 1 press |
| Next track | 2 press |
| Previous / restart | 3 press |
| Open settings | 4 presses |

---

## How Canvas works

Spotify Canvas is a short looping video attached to some tracks. There's no public API for it. PiMusic hits Spotify's internal GraphQL Pathfinder endpoint directly, which is the same thing as using the Web Player. It needs auth tokens from an active Spotify session, Playwright captures those by launching a real Chrome window and scraping the auth headers. Tokens refresh every 50 mins on its own.

The CDN (`canvaz.scdn.co`) fingerprints TLS clients. Python's `requests` library gets blocked. The app uses `curl_cffi` with `impersonate="chrome131"` to get around it.

When playing from Apple Music via Cider, PiMusic searches Spotify for the same track and pulls its Canvas. It tries 3 different searches: track + artist first, then track name only, then artist only as a last resort. Before searching it strips `(feat. ...)`, `(ft. ...)`, `(... remix)` suffixes because Apple and Spotify don't always agree on those.

---

## Visual Modes

| Mode | Description |
|------|-------------|
| **Canvas in Artwork Box** | Canvas video plays inside the album art square. Click art to go fullscreen. |
| **Canvas Behind Artwork** | Canvas video fills the screen behind the centered card UI. |
| **Album Artwork Only** | No video. Blurred album art background. |

Change the mode from the Settings page (`/settings`)

When nothing is playing the display shows a pre-fetched Canvas fullscreen as a screensaver instead of a dead screen. The idle canvas is pre-warmed at startup so it appears immediately. Currently set to DS2 by Future. Change it by editing `IDLE_CANVAS_TRACK_IDS` at the top of `spotify_controller.py`.

---

## Installation

```bash
git clone https://github.com/Studented0/PiMusic.git
cd PiMusic
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Fill in `.env`:

```env
SPOTIPY_CLIENT_ID=your_client_id
SPOTIPY_CLIENT_SECRET=your_client_secret
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8080
SP_DC=your_sp_dc_cookie
```

`SP_DC` is your Spotify `sp_dc` browser cookie, needed for Canvas. Client ID and Secret come from a developer app at [developer.spotify.com](https://developer.spotify.com/dashboard). On first run Spotipy opens a browser for OAuth — paste the redirect URL back into the terminal to finish.

Start the server:

```bash
python spotify_server.py
```

On the Pi:

```bash
DISPLAY=:0 chromium --kiosk --noerrdialogs --disable-infobars --disable-smooth-scrolling http://<your-pc-ip>:5000
```

**Windows autostart (optional):** `start-pimusic-hidden.vbs` launches the server silently at login. `debug-pimusic.bat` kills it and opens a visible console. `view-log.bat` tails the log.

---

## Headaches/things to fix

1. App is slow to update sometimes (not a dealbreaker only a couple seconds)
2. Scrobbler goes to 0 when you pause then updates to the right timestamp
3. Volume is janky to update and does not update in realtime

## Project Structure

```
PiMusic/
├── spotify_server.py        # Flask server, routes, canvas proxy
├── spotify_controller.py    # Spotify polling, Canvas GraphQL fetch, idle screensaver
├── spotify_auth.py          # Spotipy OAuth, Playwright token capture
├── cider_controller.py      # Apple Music via Cider, Canvas cross-lookup
├── source_manager.py        # Source switching
├── scrobbler.py             # Scrobble logging
├── resource_monitor.py      # CPU monitoring
├── album_cache.py           # Album art download, atomic writes, quota pruning
├── requirements.txt
├── BOM.csv
├── .env.example
├── start-pimusic-hidden.vbs
├── debug-pimusic.bat
├── view-log.bat
├── static/
│   ├── app.js               # Polling, rendering, controls, encoder keydown handling
│   ├── style.css
│   └── settings.js
├── templates/
│   ├── index.html
│   └── settings.html
├── firmware/
│   └── pimusic_encoder/
├── canvas-finder/
├── CAD/
└── renders/
```

---

## License

[MIT](LICENSE)
