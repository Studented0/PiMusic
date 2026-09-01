## PiMusic
A desk display for Spotify and Apple Music. Built with a Raspberry Pi 5 and a Waveshare 5-inch DSI touchscreen (800x480 landscape). Shows what's playing, Spotify Canvas videos as backgrounds, and lets you control playback with a rotary encoder.

---


## Screenshots


<img width="946" height="965" alt="Card mode" src="https://github.com/user-attachments/assets/76271508-90b1-4d4c-8391-4987c300cea9" />
<img width="946" height="965" alt="Canvas behind artwork" src="https://github.com/user-attachments/assets/e98f7299-f699-494a-8890-7e4b21579528" />
<img width="946" height="965" alt="Cinematic fullscreen" src="https://github.com/user-attachments/assets/d6a9b810-50e6-4a7c-a87e-24a7d04cb654" />


---
## Finished Product
<img width="4284" height="5712" alt="IMG_3090 (2)" src="https://github.com/user-attachments/assets/30e207dd-a3bd-4d86-9467-1db2b71d1236" />
<img width="3264" height="2448" alt="IMG_0071" src="https://github.com/user-attachments/assets/55e84383-95bd-46a3-99dd-95aab1dfe2b4" />

---

## Hardware

Waveshare 5-inch DSI display, 800x480 landscape. Needs a `dtoverlay` entry in `/boot/firmware/config.txt` for the panel driver. Pi 5 has enough headroom to run Chromium in kiosk smoothly.

Server still runs on PC — Playwright token capture, Canvas GraphQL fetching, and all the Python backend are too heavy to colocate with the kiosk. Flask server runs on my personal PC and the Pi just has Chromium in kiosk mode pointed at it over the network. Pi boots straight into the kiosk via `labwc/autostart` on boot.
## Rotary Encoder
An EC11 encoder is wired to a SparkFun Pro Micro ATmega32U4, the Pro Micro shows up as a HID keyboard and the browser just reads the keypresses, no drivers needed. All three of the pins are on internal pull-ups which means that resistors are not needed. Single press play/pause, double skip, triple previous, four presses opens settings. The firmware just sends spacebars and the browser counts how many in a row.

---
## Case/Enclosure

The case is a simple Pi 3B+ case with a 17 degree wedge stand, made in SOLIDWORKS. All of it prints in one piece. Separate enclosure for the encoder and pro micro also made in SOLIDWORKS.
<img width="453" height="357" alt="image" src="https://github.com/user-attachments/assets/c657c5ea-9ae9-4fb8-a104-cd9228317261" />
<img width="3507" height="2480" alt="image" src="https://github.com/user-attachments/assets/b52acbfe-2471-451b-ab0d-e20a23723682" />


---

## How Canvas works

Canvas does not have a public API, so with reverse engineering of Spotify’s internal GraphQL endpoint we get our own API. Playwright captures auth tokens from a Spotify web player by opening a real chromium window in the background. Tokens are cycled around every 50 mins. Spotify’s CDN fingerprints TLS clients, so Python requests get blocked. To bypass this curl_	
cffi with Chrome131 impersonation gets past it. When on Apple Music, same system is used but with looking up the Apple Music track name and pulling the Canvas if track has Canvas. 

---
## Software

Flask server on PC handles Spotify polling, Canvas fetching, Apple Music by using Cider (3rd party Apple Music desktop app), Album art caching, and just gives everything to the Pi over the network.

---
## Remote access (Tailscale)

Tailscale on both the PC and Pi so I can hit the server from outside the apartment LAN. The Pi kiosk tries the local hostname first (`http://jeanine:5000`) and falls back to Tailscale MagicDNS if the LAN address is unreachable. One-time Pi setup script: `scripts/pi5_kiosk_setup.sh`.

---
## Search, Playlists, and Liked Songs

Tap the magnifier in the top bar to open the library overlay. Search Spotify with the on-screen keyboard (the Pi has no physical keyboard), browse your playlists and liked songs, tap a song to play it, or use the queue button on any row. Playing a track from a playlist/album keeps the rest of the collection queued. Spotify-only for now; requires a one-time re-auth after the playlist scopes were added:

```
python server\spotify_auth.py --reauth
```

## Lyrics

Tap the lyrics icon to open synced lyrics for the current track (works for both Spotify and Apple Music). Album art, track info, transport controls, progress, and volume sit on the left; synced lyrics scroll on the right. Lyrics come from LRCLIB with Spotify's internal lyrics endpoint as a fallback (same captured web-player tokens as Canvas). Synced lines auto-scroll and highlight using the same predictive clock as the progress bar — tap a line to seek there.

---
## Cinematic mode

Fullscreen Canvas video background on the 800x480 touchscreen. Touch-first controls sized for the kiosk. Chrome (title, progress, controls) stays visible while music is playing — only the idle screensaver dims it.

---
## Fixed headaches (previously known issues)

1. ~~App is slow to update sometimes~~ — server now applies play/pause/seek/volume optimistically and force-polls after every command; `/api/state` extrapolates progress between polls.
2. ~~Progress goes to 0 when you pause then corrects itself~~ — frontend no longer wipes `progress_ms` during input locks, and drift correction between polls is actually wired up now.
3. ~~Volume is janky and not realtime~~ — volume reflects instantly server-side (Cider volume was hardcoded to 0; now polled for real), and the UI lock dropped from 5s to 1.8s.
4. Album art 404s — art was cached to `server/art_cache/` but served from `art_cache/`; unified (existing files migrate automatically).
5. Startup no longer chokes the PC's network — heavy tasks are staggered, and the hidden token-capture Chromium uses a persistent profile and blocks images/media/fonts/analytics.
6. Lyrics/progress desync on pause — adaptive polling (2s playing / 5s idle) plus frontend sync while the lyrics overlay is open.
7. Title/controls no longer auto-hide during playback — only the idle screensaver dims chrome.
---
## Project Structure

```
PiMusic/
├── server/                  # Flask server + all backend modules
│   ├── spotify_server.py    # Flask routes, canvas proxy, library/lyrics APIs, demo-mode wiring
│   ├── spotify_controller.py # Spotify polling, Canvas GraphQL fetch, idle screensaver
│   ├── spotify_library.py   # Search, playlists, liked songs, play/queue commands
│   ├── spotify_auth.py      # Spotipy OAuth, Playwright token capture
│   ├── lyrics.py            # LRCLIB + Spotify lyrics fetch, LRC parsing, cache
│   ├── cider_controller.py  # Apple Music via Cider, Canvas cross-lookup
│   ├── source_manager.py    # Source switching
│   ├── scrobbler.py         # Scrobble logging
│   ├── resource_monitor.py  # CPU monitoring
│   ├── album_cache.py       # Album art download, pre-blurred bg variants, quota pruning
│   └── demo_state.py        # Hardcoded playlist for DEMO_MODE / Vercel
├── api/                     # Vercel serverless entry (lean Flask, no heavy deps)
│   ├── index.py
│   └── requirements.txt
├── static/
│   ├── app.js               # Polling, rendering, controls, encoder keydown handling
│   ├── library.js           # Search/playlists/liked overlay + on-screen keyboard
│   ├── lyrics.js            # Synced lyrics overlay
│   ├── style.css
│   ├── settings.js
│   └── demo/                # Demo-mode playlist + assets
├── templates/
│   ├── index.html
│   └── settings.html
├── scripts/                 # Helper scripts (autostart + CLI tooling)
│   ├── start-pimusic-hidden.vbs # Windows autostart (rotates server.log, runs hidden)
│   ├── debug-pimusic.bat    # Stop autostart task, run server in a visible console
│   ├── view-log.bat         # Tail server.log
│   ├── smoke_test.py        # Demo-mode test-client smoke tests (safe while server runs)
│   ├── build_demo_playlist.py # Build static/demo/playlist.json from Spotify URLs
│   └── pi5_kiosk_setup.sh   # Pi 5 Chromium kiosk autostart (labwc + LXDE fallback)
├── firmware/
│   └── pimusic_encoder/
├── CAD/                     # Mechanical CAD, renders, BOM
│   ├── BOM.csv
│   └── renders/
├── vercel.json
├── requirements.txt
└── .env.example
```

---
PLEASE LET ME KNOW IF YOU HAVE ANY SUGGESTIONS TO ADD AND OR BUGS
## License

[MIT](LICENSE)
