PiMusic
A desk display for Spotify and Apple Music. Built with a Raspberry Pi 3B+ and a HyperPixel 4.0 touchscreen. Shows what's playing, Spotify Canvas videos as backgrounds, and lets you control playback with a rotary encoder.

---


## Screenshots


<img width="946" height="965" alt="Card mode" src="https://github.com/user-attachments/assets/76271508-90b1-4d4c-8391-4987c300cea9" />
<img width="939" height="974" alt="Canvas behind artwork" src="https://github.com/user-attachments/assets/e98f7299-f699-494a-8890-7e4b21579528" />
<img width="1005" height="927" alt="Cinematic fullscreen" src="https://github.com/user-attachments/assets/d6a9b810-50e6-4a7c-a87e-24a7d04cb654" />


---
## Hardware

Hyperpixel 4.0 rectangular slots directly on the raspberry pi’s 40 pin GPIO interface, no extra wiring is needed. Resolution is 800x480 via DPI. Software compositor is is smoother than GL driver on the Pi 3B+

Server is running on PC to optimize performance for the Pi. 3B+ struggles with Python, Playwright, and chromium even in kiosk at the same time. Flask server runs on my personal PC over the same network and the Pi just has Chromium in Kiosk mode pointed at the server, PC does all the heavy work and Pi just renders and looks pretty.
## Rotary Encoder
An EC11 encoder is wired to a SparkFun Pro Micro ATmega32U4, the Pro Micro shows up as a HID keyboard and the browser just reads the keypresses, no drivers needed. All three of the pins are on internal pull-ups which means that resistors are not needed. Single press play/pause, double skip, triple previous, four presses opens settings. The firmware just sends spacebars and the browser counts how many in a row.

---
## Case/Enclosure

The case is a simple Pi 3B+ case with a 17 degree wedge stand, made in SOLIDWORKS. All of it prints in one piece. Separate enclosure for the encoder and pro micro also made in SOLIDWORKS.
<img width="453" height="357" alt="image" src="https://github.com/user-attachments/assets/c657c5ea-9ae9-4fb8-a104-cd9228317261" />

---

## How Canvas works
Canvas does not have a public API, so with reverse engineering of Spotify’s internal GraphQL endpoint we get our own API. Playwright captures auth tokens from a Spotify web player by opening a real chromium window in the background. Tokens are cycled around every 50 mins. Spotify’s CDN fingerprints TLS clients, so Python requests get blocked. To bypass this curl_cffi with Chrome131 impersonation gets past it. When on Apple Music, same system is used but with looking up the Apple Music track name and pulling the Canvas if track has Canvas. 
---
## Software

Flask server on PC handles Spotify polling, Canvas fetching, Apple Music by using Cider (3rd party Apple Music desktop app), Album art caching, and just gives everything to the Pi over the network.
---
## Headaches/things to fix

1. App is slow to update sometimes (not a dealbreaker only a couple seconds)
2. Scrobbler goes to 0 when you pause then updates to the right timestamp
3. Volume is janky to update and does not update in realtime
---
## Project Structure

```
PiMusic/
├── server/                  # Flask server + all backend modules
│   ├── spotify_server.py    # Flask routes, canvas proxy, demo-mode wiring
│   ├── spotify_controller.py # Spotify polling, Canvas GraphQL fetch, idle screensaver
│   ├── spotify_auth.py      # Spotipy OAuth, Playwright token capture
│   ├── cider_controller.py  # Apple Music via Cider, Canvas cross-lookup
│   ├── source_manager.py    # Source switching
│   ├── scrobbler.py         # Scrobble logging
│   ├── resource_monitor.py  # CPU monitoring
│   ├── album_cache.py       # Album art download, atomic writes, quota pruning
│   └── demo_state.py        # Hardcoded playlist for DEMO_MODE / Vercel
├── api/                     # Vercel serverless entry (lean Flask, no heavy deps)
│   ├── index.py
│   └── requirements.txt
├── static/
│   ├── app.js               # Polling, rendering, controls, encoder keydown handling
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
│   └── build_demo_playlist.py # Build static/demo/playlist.json from Spotify URLs
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

## License

[MIT](LICENSE)
