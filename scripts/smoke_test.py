"""Quick smoke test: boots the Flask app in DEMO_MODE with a test client
(no port binding, safe to run while the real server is up) and exercises
the key routes plus the lyrics module against live LRCLIB.

Run:  python scripts/smoke_test.py
"""

import os
import sys

os.environ["DEMO_MODE"] = "1"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import spotify_server  # noqa: E402

app = spotify_server.app
client = app.test_client()
failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({extra})" if extra else ""))
    if not cond:
        failures.append(name)


# ── Pages ────────────────────────────────────────────────
r = client.get("/")
check("GET / renders", r.status_code == 200)
html = r.get_data(as_text=True)
check("index has library button", 'id="library-btn"' in html)
check("index has lyrics button", 'id="lyrics-btn"' in html)
check("index has library overlay", 'id="library-overlay"' in html)
check("index has lyrics overlay", 'id="lyrics-overlay"' in html)
check("index marks demo mode", 'data-demo="1"' in html)

# ── State ────────────────────────────────────────────────
r = client.get("/api/state")
check("GET /api/state", r.status_code == 200)
state = r.get_json()
check("state has track", bool(state.get("track")), state.get("track", ""))
check("state has progress", isinstance(state.get("progress_ms"), int))

# ── Demo playlist assets resolve (path fix) ──────────────
import demo_state  # noqa: E402
check(
    "demo dir points at repo static/demo",
    demo_state._DEMO_DIR.endswith(os.path.join("PiMusic", "static", "demo")),
    demo_state._DEMO_DIR,
)
check("demo playlist loaded", len(demo_state._PLAYLIST) > 0, f"{len(demo_state._PLAYLIST)} tracks")
local_art = [t for t in demo_state._PLAYLIST if str(t.get("album_art_local", "")).startswith("/static/demo/")]
check("demo art resolves to local bundle", len(local_art) > 0, f"{len(local_art)} local")

# ── Library endpoints (demo: empty but valid JSON) ───────
r = client.get("/api/library/search?q=test")
check("GET /api/library/search", r.status_code == 200 and r.get_json() == {"tracks": [], "albums": [], "playlists": []})
r = client.get("/api/library/playlists")
check("GET /api/library/playlists", r.status_code == 200 and r.get_json().get("items") == [])
r = client.get("/api/library/liked")
check("GET /api/library/liked", r.status_code == 200)
r = client.post("/api/library/play", json={"uri": "spotify:track:x"})
check("POST /api/library/play (demo refuses)", r.status_code == 200 and r.get_json().get("ok") is False)

# ── Playback routes still fine ───────────────────────────
r = client.post("/api/next")
check("POST /api/next", r.status_code == 200 and r.get_json().get("ok") is True)
r = client.post("/api/volume", json={"volume": 40})
check("POST /api/volume", r.status_code == 200 and r.get_json().get("ok") is True)

# ── Shuffle / repeat / like / queue ──────────────────────
r = client.post("/api/shuffle", json={"state": True})
check("POST /api/shuffle", r.status_code == 200 and r.get_json().get("ok") is True)
r = client.get("/api/state")
check("state reflects shuffle", r.get_json().get("shuffle_state") is True)
r = client.post("/api/repeat", json={"state": "track"})
check("POST /api/repeat", r.status_code == 200 and r.get_json().get("ok") is True)
r = client.get("/api/state")
check("state reflects repeat", r.get_json().get("repeat_state") == "track")
r = client.post("/api/repeat", json={"state": "bogus"})
check("repeat rejects bad state", r.get_json().get("ok") is False)
r = client.post("/api/like")
check("POST /api/like toggles on", r.status_code == 200 and r.get_json().get("is_saved") is True)
r = client.get("/api/state")
check("state reflects is_saved", r.get_json().get("is_saved") is True)
r = client.post("/api/like")
check("POST /api/like toggles off", r.get_json().get("is_saved") is False)
r = client.get("/api/queue")
check("GET /api/queue (demo empty)", r.status_code == 200 and r.get_json().get("items") == [])

# ── Settings round-trip (lock paths) ─────────────────────
r = client.get("/api/settings")
check("GET /api/settings", r.status_code == 200 and "visual_mode" in r.get_json())

# ── Lyrics module: LRC parser ────────────────────────────
import lyrics as lyrics_mod  # noqa: E402

parsed = lyrics_mod.parse_lrc("[00:12.34]Hello world\n[01:02]Second line\n[00:50.1][02:00.5]Repeated")
check("parse_lrc count", len(parsed) == 4, str(len(parsed)))
check("parse_lrc sorted", parsed[0]["time_ms"] == 12340 and parsed[-1]["time_ms"] == 120500)
check("parse_lrc text", parsed[0]["text"] == "Hello world")
check("parse_lrc no guessed words", "words" not in parsed[0])

elrc = lyrics_mod.parse_lrc("[00:12.34]<00:12.50>Hello <00:12.89>world")
check("parse_lrc enhanced words", len(elrc) == 1 and len(elrc[0].get("words") or []) == 2)
check("parse_lrc enhanced text", elrc[0]["text"] == "Hello world")
check("parse_lrc enhanced times", elrc[0]["words"][0]["time_ms"] == 12500)

syl = lyrics_mod._parse_spotify_word_timings([
    {"startTimeMs": "100", "text": "Hello"},
    {"startTimeMs": "450", "words": "world"},
])
check("spotify syllables parse", syl and len(syl) == 2 and syl[1]["text"] == "world")
check("spotify syllables reject partial", lyrics_mod._parse_spotify_word_timings([
    {"startTimeMs": "100", "text": "Hi"},
    {"text": "missing time"},
]) is None)

# ── Lyrics route against live LRCLIB ─────────────────────
r = client.get(
    "/api/lyrics?artist=Tame%20Impala&track=Let%20It%20Happen&album=Currents&duration_ms=467000&source=demo"
)
check("GET /api/lyrics", r.status_code == 200)
ly = r.get_json()
check("lyrics found (live LRCLIB)", ly.get("found") is True, "source=" + str(ly.get("source")))
check("lyrics synced lines", bool(ly.get("synced")) and len(ly["synced"]) > 10,
      f"{len(ly.get('synced') or [])} lines")

# cached second hit
r = client.get(
    "/api/lyrics?artist=Tame%20Impala&track=Let%20It%20Happen&album=Currents&duration_ms=467000&source=demo"
)
check("lyrics cache hit", r.status_code == 200 and r.get_json().get("found") is True)

# negative lookup
r = client.get("/api/lyrics?artist=zzqx%20nobody&track=qqzzz%20nonexistent&duration_ms=123000")
check("lyrics miss handled", r.status_code == 200 and r.get_json().get("found") is False)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
