import os
import time
import threading

import dotenv
dotenv.load_dotenv()

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8080")
SP_DC = os.environ.get("SP_DC", "")
CACHE_PATH = os.path.expanduser("~/pimusic/.spotify_cache")

SCOPES = (
    "user-read-playback-state "
    "user-read-currently-playing "
    "user-modify-playback-state "
    "user-read-email "
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-library-read "
    "user-library-modify"
)

# ── Web player token (for Canvas GraphQL) ────────────────
_wp_bearer = ""
_wp_client_token = ""
_wp_token_ts = 0.0
_wp_lock = threading.Lock()
WP_TOKEN_TTL = 3000  # refresh every 50 min (tokens last ~60 min)

PW_PROFILE_DIR = os.path.expanduser("~/pimusic/pw-profile")

# Resource types / hosts the token-capture browser never needs. The page only
# has to boot the web player JS far enough to fire one api-partner request,
# so images, media, fonts, and analytics are pure network noise (and the big
# reason startup used to hammer DNS/bandwidth).
_PW_BLOCKED_TYPES = {"image", "media", "font"}
_PW_BLOCKED_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "scdn.co/image",
    "i.scdn.co",
    "pixel.spotify.com",
    "sentry.io",
    "branch.io",
    "appsflyer.com",
    "hotjar.com",
)


def _pw_route_filter(route):
    req = route.request
    if req.resource_type in _PW_BLOCKED_TYPES:
        return route.abort()
    url = req.url
    for host in _PW_BLOCKED_HOSTS:
        if host in url:
            return route.abort()
    return route.continue_()


def _capture_tokens_playwright() -> tuple[str, str]:
    """Launch Chromium, load Spotify, intercept Bearer + client-token.
    Uses a persistent profile (cached JS between runs) and blocks
    images/media/fonts/analytics to keep the network burst small."""
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

    launch_args = ["--window-size=1,1", "--window-position=32000,32000"]

    with sync_playwright() as p:
        ctx = None
        browser = None
        try:
            os.makedirs(PW_PROFILE_DIR, exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                PW_PROFILE_DIR,
                headless=False,
                args=launch_args,
                # Service workers would bypass route interception and can
                # serve the app shell out-of-band; keep requests observable.
                service_workers="block",
            )
        except Exception as e:
            # Stale profile lock or corrupt profile -- fall back to a
            # throwaway context so token capture still works.
            print(f"Playwright persistent profile failed ({e}); using fresh context")
            browser = p.chromium.launch(headless=False, args=launch_args)
            ctx = browser.new_context(service_workers="block")

        try:
            ctx.add_cookies([{
                "name": "sp_dc", "value": SP_DC,
                "domain": ".spotify.com", "path": "/",
                "httpOnly": True, "secure": True,
            }])
            ctx.route("**/*", _pw_route_filter)
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
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    return bearer, client_tok


def wait_for_wp_tokens(timeout_sec: float = 90.0) -> bool:
    """Block until web player tokens are captured (or timeout). Used by the
    staggered startup sequence so canvas prewarm runs only once it can work.
    Requires BOTH the bearer and the client token -- GraphQL needs both."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with _wp_lock:
            if _wp_bearer and _wp_client_token:
                return True
        time.sleep(1.0)
    with _wp_lock:
        return bool(_wp_bearer and _wp_client_token)


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


_wp_refreshing = False

def get_web_player_tokens() -> tuple[str, str]:
    """Return (bearer_token, client_token) for Canvas GraphQL calls.
    Never blocks -- returns stale tokens and refreshes in background."""
    global _wp_refreshing
    with _wp_lock:
        expired = not _wp_bearer or (time.time() - _wp_token_ts) >= WP_TOKEN_TTL
        if not expired:
            return _wp_bearer, _wp_client_token
        if not SP_DC or _wp_refreshing:
            return _wp_bearer, _wp_client_token
        _wp_refreshing = True
    threading.Thread(target=_bg_refresh, daemon=True).start()
    with _wp_lock:
        return _wp_bearer, _wp_client_token


def _bg_refresh():
    global _wp_refreshing
    try:
        _refresh_wp_tokens()
    finally:
        with _wp_lock:
            _wp_refreshing = False


def start_wp_token_refresh():
    """Kick off initial token capture in a background thread.
    Coalesces with get_web_player_tokens so bursts of 401s do not spawn
    multiple concurrent Playwright Chromium instances."""
    global _wp_refreshing
    if not SP_DC:
        return
    with _wp_lock:
        if _wp_refreshing:
            return
        _wp_refreshing = True
    threading.Thread(target=_bg_refresh, daemon=True).start()


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
        retries=2,
        backoff_factor=1.0,
        status_forcelist=(500, 502, 503),  # Never retry 429 - we handle it with backoff
    )


def get_access_token() -> str:
    """Return a fresh Spotify access token string for use by external tools."""
    am = _get_auth_manager()
    token_info = am.get_cached_token()
    if not token_info or am.is_token_expired(token_info):
        token_info = am.refresh_access_token(token_info["refresh_token"])
    return token_info["access_token"]


def get_account_info() -> str:
    """Return displayable account string: 'Name (email)' or just name."""
    try:
        sp = spotipy.Spotify(auth_manager=_get_auth_manager())
        me = sp.me()
        name = me.get("display_name") or me.get("id", "unknown")
        email = me.get("email", "")
        return f"{name} ({email})" if email else name
    except Exception:
        return "unknown"


def force_reauth():
    """Delete cached OAuth token and restart web player token capture.
    Called from /api/force-reauth when tokens are stale or broken."""
    global _auth_manager
    print("[Spotify Auth] Clearing cached tokens...")
    try:
        if os.path.isfile(CACHE_PATH):
            os.remove(CACHE_PATH)
            print(f"[Spotify Auth] Deleted token cache: {CACHE_PATH}")
    except Exception as e:
        print(f"[Spotify Auth] Failed to delete token cache: {e}")
    _auth_manager = None
    print("[Spotify Auth] Restarting token capture...")
    start_wp_token_refresh()
    print("[Spotify Auth] Waiting for captcha completion...")


if __name__ == "__main__":
    import sys
    if "--reauth" in sys.argv:
        print("[Spotify Auth] Starting re-authentication...")
        force_reauth()
        try:
            sp = get_spotify_client()
            me = sp.me()
            name = me.get("display_name") or me.get("id", "unknown")
            email = me.get("email", "")
            label = f"{name} ({email})" if email else name
            print(f"[Spotify Auth] Authenticating account: {label}")
            print("[Spotify Auth] Authentication successful.")
        except Exception as e:
            print(f"[Spotify Auth] Auth failed: {e}")
            print("[Spotify Auth] You may need to complete a captcha in the browser.")
            sys.exit(1)
    else:
        print("Usage: python spotify_auth.py --reauth")
