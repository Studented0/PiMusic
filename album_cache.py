import os
import hashlib
import requests

ART_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "art_cache")
ART_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB default quota


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
    tmp_path = path + ".tmp"
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            f.write(resp.content)
        os.replace(tmp_path, path)
        return filename
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None


def prune_art_cache(max_bytes: int = ART_CACHE_MAX_BYTES) -> int:
    """Delete oldest JPEGs until the art_cache folder is under max_bytes. Returns count removed."""
    if not os.path.isdir(ART_CACHE_DIR):
        return 0
    entries = []
    total = 0
    for name in os.listdir(ART_CACHE_DIR):
        if not name.endswith(".jpg"):
            continue
        path = os.path.join(ART_CACHE_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        entries.append((st.st_mtime, path, st.st_size))
        total += st.st_size
    if total <= max_bytes:
        return 0
    entries.sort(key=lambda x: x[0])
    removed = 0
    for _mtime, path, size in entries:
        if total <= max_bytes:
            break
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


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
