"""One-time Spotify re-auth with the new library/playlist scopes.

Opens the default browser at Spotify's authorize page and catches the
redirect on 127.0.0.1:8080 automatically (no URL copy-pasting needed).
The existing token cache is only overwritten after a successful auth.

Run:  python scripts/reauth_browser.py
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import spotify_auth  # noqa: E402  (loads .env)
import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyOAuth  # noqa: E402
from spotipy.cache_handler import CacheFileHandler  # noqa: E402

am = SpotifyOAuth(
    client_id=spotify_auth.CLIENT_ID,
    client_secret=spotify_auth.CLIENT_SECRET,
    redirect_uri=spotify_auth.REDIRECT_URI,
    scope=spotify_auth.SCOPES,
    open_browser=True,  # pop the browser + catch redirect on localhost
    cache_handler=CacheFileHandler(cache_path=spotify_auth.CACHE_PATH),
)

print("Requesting scopes:", spotify_auth.SCOPES, flush=True)
print("BROWSER_OPENING -- approve the Spotify prompt in your browser...", flush=True)

try:
    token_info = am.get_access_token(check_cache=True)
except Exception as e:
    print(f"AUTH_ERROR: {e}", flush=True)
    sys.exit(1)

access = token_info["access_token"] if isinstance(token_info, dict) else token_info
sp = spotipy.Spotify(auth=access)
me = sp.me()
name = me.get("display_name") or me.get("id", "unknown")

with open(spotify_auth.CACHE_PATH, "r", encoding="utf-8") as f:
    cached = json.load(f)

print(f"AUTH_OK account={name}", flush=True)
print(f"CACHED_SCOPES: {cached.get('scope')}", flush=True)

needed = {
    "playlist-read-private", "playlist-read-collaborative",
    "user-library-read", "user-library-modify",
}
have = set((cached.get("scope") or "").split())
missing = needed - have
if missing:
    print(f"SCOPES_MISSING: {missing}", flush=True)
    sys.exit(1)
print("ALL_SCOPES_PRESENT", flush=True)
