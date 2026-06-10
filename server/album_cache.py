import os
import hashlib
import requests

# Art lives at the repo root -- the same directory spotify_server.py serves
# from (/art/...) and clears via /api/clear-cache. It used to be written to
# server/art_cache/ which the server never served (hence the /art 404s).
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
ART_CACHE_DIR = os.path.join(os.path.dirname(_SERVER_DIR), "art_cache")
_LEGACY_ART_DIR = os.path.join(_SERVER_DIR, "art_cache")
ART_CACHE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB default quota

# Pre-blurred background variant: small + already gaussian-blurred so the Pi
# doesn't have to run a full-screen CSS blur() on every composited frame.
BG_WIDTH = 160
BG_BLUR_RADIUS = 3  # on a 160px-wide image upscaled to 800px ~ blur(15px) on screen


def _migrate_legacy_cache():
    """One-time move of art cached under server/art_cache/ (old path bug)
    into the repo-root art_cache/ directory the server actually serves."""
    if not os.path.isdir(_LEGACY_ART_DIR):
        return
    try:
        os.makedirs(ART_CACHE_DIR, exist_ok=True)
        moved = 0
        for name in os.listdir(_LEGACY_ART_DIR):
            if not name.endswith(".jpg"):
                continue
            src = os.path.join(_LEGACY_ART_DIR, name)
            dst = os.path.join(ART_CACHE_DIR, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                try:
                    os.replace(src, dst)
                    moved += 1
                except OSError:
                    pass
        if moved:
            print(f"Art cache: migrated {moved} file(s) from server/art_cache/ to art_cache/")
        try:
            if not os.listdir(_LEGACY_ART_DIR):
                os.rmdir(_LEGACY_ART_DIR)
        except OSError:
            pass
    except Exception as e:
        print(f"Art cache migration error: {e}")


_migrate_legacy_cache()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _bg_filename(url: str) -> str:
    return _url_hash(url) + "_bg.jpg"


def _make_bg_variant(src_path: str, bg_path: str):
    """Generate the small pre-blurred background JPEG from cached art."""
    from PIL import Image, ImageFilter  # Pillow ships with colorthief

    tmp_path = bg_path + ".tmp"
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        h = max(1, round(im.height * (BG_WIDTH / im.width)))
        im = im.resize((BG_WIDTH, h), Image.LANCZOS)
        im = im.filter(ImageFilter.GaussianBlur(radius=BG_BLUR_RADIUS))
        im.save(tmp_path, "JPEG", quality=70)
    os.replace(tmp_path, bg_path)


def _ensure_bg_variant(image_url: str, art_path: str):
    try:
        bg_path = os.path.join(ART_CACHE_DIR, _bg_filename(image_url))
        if not os.path.isfile(bg_path) and os.path.isfile(art_path):
            _make_bg_variant(art_path, bg_path)
    except Exception:
        pass


def get_cached_art(image_url: str) -> str | None:
    """Return the local filename if the image is already cached, else None."""
    if not image_url:
        return None
    filename = _url_hash(image_url) + ".jpg"
    path = os.path.join(ART_CACHE_DIR, filename)
    if os.path.isfile(path):
        return filename
    return None


def get_cached_bg(image_url: str) -> str | None:
    """Return the local filename of the pre-blurred bg variant, if present."""
    if not image_url:
        return None
    filename = _bg_filename(image_url)
    if os.path.isfile(os.path.join(ART_CACHE_DIR, filename)):
        return filename
    return None


def cache_art(image_url: str) -> str | None:
    """Download and cache album art (plus a pre-blurred bg variant).
    Returns the local filename of the full-size art."""
    if not image_url:
        return None
    filename = _url_hash(image_url) + ".jpg"
    path = os.path.join(ART_CACHE_DIR, filename)
    if os.path.isfile(path):
        _ensure_bg_variant(image_url, path)
        return filename
    os.makedirs(ART_CACHE_DIR, exist_ok=True)
    tmp_path = path + ".tmp"
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            f.write(resp.content)
        os.replace(tmp_path, path)
        _ensure_bg_variant(image_url, path)
        return filename
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None


def prune_art_cache(max_bytes: int = ART_CACHE_MAX_BYTES) -> int:
    """Delete oldest art until the folder is under max_bytes. Full art and
    its _bg.jpg variant are pruned together so neither is orphaned.
    Returns count of files removed."""
    if not os.path.isdir(ART_CACHE_DIR):
        return 0
    entries = []  # (mtime, [paths], total_size) grouped by url hash
    groups = {}
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
        key = name[:-7] if name.endswith("_bg.jpg") else name[:-4]
        g = groups.setdefault(key, [0.0, [], 0])
        g[0] = max(g[0], st.st_mtime)
        g[1].append(path)
        g[2] += st.st_size
        total += st.st_size
    if total <= max_bytes:
        return 0
    entries = sorted(groups.values(), key=lambda g: g[0])
    removed = 0
    for _mtime, paths, size in entries:
        if total <= max_bytes:
            break
        for path in paths:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
        total -= size
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
