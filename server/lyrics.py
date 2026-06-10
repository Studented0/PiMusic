"""Lyrics fetching for PiMusic.

Primary source: LRCLIB (https://lrclib.net) -- free, no auth, time-synced
LRC lyrics, matched by artist/title/album/duration so it works for both
Spotify and Apple Music (Cider) tracks.

Fallback: Spotify's internal color-lyrics endpoint, using the same
Playwright-captured web player tokens the Canvas fetcher uses.

All results (including misses) are cached in a small in-memory LRU.
"""

import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from curl_cffi import requests as cffi_requests
from spotify_auth import get_web_player_tokens

LRCLIB_GET_URL = "https://lrclib.net/api/get"
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LRCLIB_HEADERS = {"Lrclib-Client": "PiMusic (https://github.com/pimusic)"}
SPOTIFY_LYRICS_URL = "https://spclient.wg.spotify.com/color-lyrics/v2/track/{}?format=json&vocalRemoval=false&market=from_token"

_CACHE_MAX = 60
_cache = OrderedDict()  # key -> result dict (including {"found": False} misses)
_cache_lock = threading.Lock()

# [mm:ss.xx] or [mm:ss] line tags; <mm:ss.xx> enhanced (word-level) tags
_LRC_TAG = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_ELRC_TAG = re.compile(r"<(\d+):(\d{1,2}(?:[.:]\d{1,3})?)>")


def _cache_key(artist, track, duration_ms):
    return "{}|{}|{}".format(
        (artist or "").strip().lower(),
        (track or "").strip().lower(),
        int(round((duration_ms or 0) / 1000.0)),
    )


def _cache_get(key):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_put(key, value):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
        elif len(_cache) >= _CACHE_MAX:
            _cache.popitem(last=False)
        _cache[key] = value


def _lrc_timestamp_to_ms(minutes, seconds_str):
    seconds = float(seconds_str.replace(":", "."))
    return int((int(minutes) * 60 + seconds) * 1000)


def _parse_enhanced_words(content):
    """Parse Enhanced LRC word tags into [{"time_ms", "text"}, ...] or None."""
    tags = list(_ELRC_TAG.finditer(content or ""))
    if not tags:
        return None
    words = []
    for i, m in enumerate(tags):
        start = m.end()
        end = tags[i + 1].start() if i + 1 < len(tags) else len(content)
        text = content[start:end].strip()
        if not text:
            continue
        try:
            t_ms = _lrc_timestamp_to_ms(m.group(1), m.group(2))
        except ValueError:
            return None
        words.append({"time_ms": t_ms, "text": text})
    return words if words else None


def _synced_line(time_ms, text, word_timings=None):
    entry = {"time_ms": time_ms, "text": text}
    if word_timings:
        entry["words"] = word_timings
    return entry


def parse_lrc(text):
    """Parse LRC into [{"time_ms", "text", "words"?}, ...] sorted by time.

    Standard LRC yields line-level entries only. Enhanced LRC (angle-bracket
    word tags) adds a ``words`` array when real per-word timestamps are present.
    """
    lines = []
    for raw in (text or "").splitlines():
        tags = list(_LRC_TAG.finditer(raw))
        if not tags:
            continue
        content = raw[tags[-1].end():].strip()
        word_timings = _parse_enhanced_words(content)
        display = (
            " ".join(w["text"] for w in word_timings)
            if word_timings
            else content
        )
        for m in tags:
            try:
                t_ms = _lrc_timestamp_to_ms(m.group(1), m.group(2))
            except ValueError:
                continue
            lines.append(_synced_line(t_ms, display, word_timings))
    lines.sort(key=lambda x: x["time_ms"])
    return lines


def _parse_spotify_word_timings(entries):
    """Return word timings from Spotify syllables when every entry is timed."""
    words = []
    for item in entries or []:
        if not isinstance(item, dict):
            return None
        raw_ms = item.get("startTimeMs", item.get("start_time_ms"))
        try:
            t_ms = int(raw_ms)
        except (TypeError, ValueError):
            return None
        text = (
            item.get("text")
            or item.get("words")
            or item.get("syllable")
            or ""
        ).strip()
        if not text:
            return None
        words.append({"time_ms": t_ms, "text": text})
    return words if words else None


def _word_sync_score(result):
    synced = (result or {}).get("synced") or []
    return sum(1 for ln in synced if ln.get("words"))


def _result(synced=None, plain=None, instrumental=False, source=""):
    return {
        "found": bool(synced or plain or instrumental),
        "synced": synced or None,
        "plain": plain or None,
        "instrumental": bool(instrumental),
        "source": source,
    }


def _normalize_lrclib_record(rec):
    if not rec or rec.get("statusCode") == 404:
        return None
    if rec.get("instrumental"):
        return _result(instrumental=True, source="lrclib")
    synced_raw = rec.get("syncedLyrics") or ""
    plain = rec.get("plainLyrics") or ""
    synced = parse_lrc(synced_raw) if synced_raw else None
    if synced or plain:
        return _result(synced=synced, plain=plain, source="lrclib")
    return None


def _from_lrclib(artist, track, album, duration_ms):
    duration_s = int(round((duration_ms or 0) / 1000.0))
    try:
        resp = requests.get(
            LRCLIB_GET_URL,
            params={
                "artist_name": artist,
                "track_name": track,
                "album_name": album or "",
                "duration": duration_s,
            },
            headers=LRCLIB_HEADERS,
            timeout=8,
        )
        if resp.status_code == 200:
            out = _normalize_lrclib_record(resp.json())
            if out:
                return out
    except Exception as e:
        print(f"[Lyrics] LRCLIB get error: {e}")

    # Fuzzy fallback: search by track+artist, pick the closest duration match
    # that has lyrics (prefer synced).
    try:
        resp = requests.get(
            LRCLIB_SEARCH_URL,
            params={"track_name": track, "artist_name": artist},
            headers=LRCLIB_HEADERS,
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        candidates = resp.json() or []
        best = None
        best_score = None
        for rec in candidates:
            if not (rec.get("syncedLyrics") or rec.get("plainLyrics") or rec.get("instrumental")):
                continue
            dur_diff = abs((rec.get("duration") or 0) - duration_s)
            if duration_s and dur_diff > 10:
                continue
            score = (0 if rec.get("syncedLyrics") else 1, dur_diff)
            if best_score is None or score < best_score:
                best_score = score
                best = rec
        return _normalize_lrclib_record(best)
    except Exception as e:
        print(f"[Lyrics] LRCLIB search error: {e}")
    return None


def _from_spotify(track_id):
    """Spotify internal color-lyrics endpoint via web player tokens.
    Unofficial (same risk profile as Canvas). Only works with Spotify IDs."""
    if not track_id:
        return None
    try:
        bearer, client_token = get_web_player_tokens()
        if not bearer:
            return None
        resp = cffi_requests.get(
            SPOTIFY_LYRICS_URL.format(track_id),
            headers={
                "Authorization": "Bearer " + bearer,
                "client-token": client_token,
                "app-platform": "WebPlayer",
                "Accept": "application/json",
            },
            impersonate="chrome131",
            timeout=8,
        )
        if resp.status_code != 200:
            if resp.status_code not in (404,):
                print(f"[Lyrics] Spotify lyrics {resp.status_code} for {track_id}")
            return None
        lyrics = (resp.json() or {}).get("lyrics") or {}
        raw_lines = lyrics.get("lines") or []
        if not raw_lines:
            return None
        sync_type = lyrics.get("syncType", "")
        synced = None
        plain_parts = []
        if sync_type != "UNSYNCED":
            synced = []
            for ln in raw_lines:
                line_text = (ln.get("words") or "").strip()
                try:
                    t_ms = int(ln.get("startTimeMs") or 0)
                except (TypeError, ValueError):
                    t_ms = 0
                word_timings = _parse_spotify_word_timings(ln.get("syllables"))
                synced.append(_synced_line(t_ms, line_text, word_timings))
                plain_parts.append(line_text)
        else:
            for ln in raw_lines:
                plain_parts.append((ln.get("words") or "").strip())
        plain = "\n".join(plain_parts).strip()
        if synced or plain:
            return _result(synced=synced, plain=plain, source="spotify")
    except Exception as e:
        print(f"[Lyrics] Spotify lyrics error for {track_id}: {e}")
    return None


def _has_lyrics(result):
    if not result:
        return False
    return bool(result.get("synced") or result.get("plain") or result.get("instrumental"))


def _pick_best_lyrics(candidates):
    """Prefer lyrics with real word timings, then more synced lines."""

    def rank(res):
        synced = res.get("synced") or []
        return (_word_sync_score(res), len(synced), 1 if res.get("source") == "lrclib" else 0)

    return max(candidates, key=rank)


def _fetch_lyrics_parallel(artist, track, album, duration_ms, spotify_track_id):
    """Query LRCLIB and Spotify concurrently; pick the richest lyrics hit."""
    tasks = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks[pool.submit(_from_lrclib, artist, track, album, duration_ms)] = "lrclib"
        if spotify_track_id:
            tasks[pool.submit(_from_spotify, spotify_track_id)] = "spotify"
        hits = []
        try:
            for fut in as_completed(tasks, timeout=9):
                try:
                    res = fut.result()
                except Exception:
                    continue
                if _has_lyrics(res):
                    hits.append(res)
        except Exception:
            pass
        if hits:
            return _pick_best_lyrics(hits)
        for fut in tasks:
            if fut.done():
                try:
                    res = fut.result()
                    if res:
                        return res
                except Exception:
                    continue
    return None


def get_lyrics(artist, track, album="", duration_ms=0, spotify_track_id=""):
    """Fetch lyrics (LRCLIB + Spotify in parallel when possible). Cached."""
    if not artist or not track:
        return _result()

    key = _cache_key(artist, track, duration_ms)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if spotify_track_id:
        out = _fetch_lyrics_parallel(artist, track, album, duration_ms, spotify_track_id)
    else:
        out = _from_lrclib(artist, track, album, duration_ms)
    if out is None:
        out = _result()

    out["fetched_at"] = time.time()
    _cache_put(key, out)
    return out
