import os
import time
import threading
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

CLIENT_ID = "d5cbeffaa5c1467eb5045d616f6afe29"
CLIENT_SECRET = "d8b9948c0b5d4b578db1937cbe04c715"
REDIRECT_URI = "http://127.0.0.1:8080"
CACHE_PATH = os.path.expanduser("~/pimusic/.spotify_cache")

SCOPES = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state"
)

def _load_sp_dc() -> str:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SP_DC=") and not line.startswith("#"):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val and val != "your_sp_dc_cookie_value_here":
                        return val
    return os.environ.get("SP_DC", "")

SP_DC = _load_sp_dc()

# ── Web player token (for Canvas GraphQL) ────────────────
_wp_bearer = ""
_wp_client_token = ""
_wp_token_ts = 0.0
_wp_lock = threading.Lock()
WP_TOKEN_TTL = 3000  # refresh every 50 min (tokens last ~60 min)

def _capture_tokens_playwright() -> tuple[str, str]:
    """Launch real Chromium, load Spotify, intercept Bearer + client-token."""
    from playwright.sync_api import sync_playwright

    bearer = ""
    client_tok = ""

    def on_request(request):
        nonlocal bearer, client_tok
        if "api-partner.spotify.com" in request.url and "pathfinder" in request.url:
            h = request.headers
            auth = h.get("authorization", "")
            ct = h.get("client-token", "")
            if auth.startswith("Bearer ") and not bearer:
                bearer = auth[7:]
            if ct and not client_tok:
                client_tok = ct

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--window-size=1,1", "--window-position=32000,32000"],
        )
        ctx = browser.new_context()
        ctx.add_cookies([{
            "name": "sp_dc", "value": SP_DC,
            "domain": ".spotify.com", "path": "/",
            "httpOnly": True, "secure": True,
        }])
        page = ctx.new_page()
        page.on("request", on_request)
        try:
            page.goto("https://open.spotify.com/", wait_until="load", timeout=25000)
            for _ in range(40):
                if bearer and client_tok:
                    break
                time.sleep(0.25)
        except Exception as e:
            print(f"Playwright navigation error: {e}")
        browser.close()

    return bearer, client_tok


def _refresh_wp_tokens():
    """Background refresh of web player tokens."""
    global _wp_bearer, _wp_client_token, _wp_token_ts
    if not SP_DC:
        return
    bearer, ct = _capture_tokens_playwright()
    if bearer:
        with _wp_lock:
            _wp_bearer = bearer
            _wp_client_token = ct
            _wp_token_ts = time.time()
        print(f"Web player tokens captured (bearer={bearer[:20]}...)")
    else:
        print("Failed to capture web player tokens")


def get_web_player_tokens() -> tuple[str, str]:
    """Return (bearer_token, client_token) for Canvas GraphQL calls."""
    with _wp_lock:
        if _wp_bearer and (time.time() - _wp_token_ts) < WP_TOKEN_TTL:
            return _wp_bearer, _wp_client_token
    _refresh_wp_tokens()
    with _wp_lock:
        return _wp_bearer, _wp_client_token


def start_wp_token_refresh():
    """Kick off initial token capture in a background thread."""
    if not SP_DC:
        return
    t = threading.Thread(target=_refresh_wp_tokens, daemon=True)
    t.start()


# ── Standard Spotify OAuth (for playback control) ────────

def create_auth_manager():
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        open_browser=False,
        cache_handler=CacheFileHandler(cache_path=CACHE_PATH),
    )


_auth_manager = None


def _get_auth_manager():
    global _auth_manager
    if _auth_manager is None:
        _auth_manager = create_auth_manager()
    return _auth_manager


def get_spotify_client():
    return spotipy.Spotify(
        auth_manager=_get_auth_manager(),
        retries=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503),
    )


def get_access_token() -> str:
    """Return a fresh Spotify access token string for use by external tools."""
    am = _get_auth_manager()
    token_info = am.get_cached_token()
    if not token_info or am.is_token_expired(token_info):
        token_info = am.refresh_access_token(token_info["refresh_token"])
    return token_info["access_token"]
