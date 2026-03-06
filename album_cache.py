import os
import hashlib
import requests
from io import BytesIO

ART_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "art_cache")


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def get_cached_art(image_url: str) -> str | None:
    """Return the local filename if the image is already cached, else None."""
    if not image_url:
        return None
    filename = _url_hash(image_url) + ".jpg"
    path = os.path.join(ART_CACHE_DIR, filename)
    if os.path.isfile(path):
        return filename
    return None


def cache_art(image_url: str) -> str | None:
    """Download and cache album art. Returns the local filename."""
    if not image_url:
        return None
    filename = _url_hash(image_url) + ".jpg"
    path = os.path.join(ART_CACHE_DIR, filename)
    if os.path.isfile(path):
        return filename
    os.makedirs(ART_CACHE_DIR, exist_ok=True)
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        return filename
    except Exception:
        return None


def get_dominant_color(image_url: str) -> str:
    """Extract the dominant color from an image URL as a hex string."""
    try:
        from colorthief import ColorThief

        filename = _url_hash(image_url) + ".jpg"
        path = os.path.join(ART_CACHE_DIR, filename)
        if not os.path.isfile(path):
            cache_art(image_url)
        if os.path.isfile(path):
            ct = ColorThief(path)
            r, g, b = ct.get_color(quality=5)
            return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        pass
    return "#1a1a2e"
