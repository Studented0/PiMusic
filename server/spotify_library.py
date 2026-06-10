"""Spotify library/browse backend for the on-device library UI.

Search, user playlists, liked songs, album tracks, and play/queue commands.
Everything respects the global rate-limit backoff in spotify_controller.
"""

import spotify_controller


class RateLimitedError(Exception):
    pass


def _guard():
    if spotify_controller.check_rate_limited():
        raise RateLimitedError("spotify rate limited")


def _art(images, prefer_small=True):
    """Pick a thumbnail URL from a Spotify images array ([large..small])."""
    if not images:
        return ""
    if prefer_small:
        # Smallest is usually 64px -- fine for 1x kiosk thumbnails.
        return images[-1].get("url", "")
    return images[0].get("url", "")


def _norm_track(t, art_override=None):
    if not t or not t.get("id"):
        return None
    album = t.get("album") or {}
    return {
        "id": t.get("id", ""),
        "uri": t.get("uri", ""),
        "name": t.get("name", ""),
        "artists": ", ".join(a.get("name", "") for a in (t.get("artists") or [])),
        "album": album.get("name", ""),
        "art": art_override if art_override is not None else _art(album.get("images")),
        "duration_ms": t.get("duration_ms", 0),
    }


def _norm_album(a):
    if not a or not a.get("id"):
        return None
    return {
        "id": a.get("id", ""),
        "uri": a.get("uri", ""),
        "name": a.get("name", ""),
        "artists": ", ".join(ar.get("name", "") for ar in (a.get("artists") or [])),
        "art": _art(a.get("images")),
        "total_tracks": a.get("total_tracks", 0),
    }


def _playlist_entry_track(entry):
    """Extract a track object from a playlist row.

    Spotify's newer playlist-items responses use ``item`` (with ``type:
    track``) instead of the legacy ``track`` field. In the new shape,
    ``entry["track"]`` is a boolean flag, not the track dict — reading it
    as the track was why playlists showed zero songs.
    """
    if not entry:
        return None
    legacy = entry.get("track")
    if isinstance(legacy, dict):
        return legacy
    item = entry.get("item")
    if isinstance(item, dict) and item.get("type") in (None, "track"):
        return item
    return None


def _norm_playlist(p):
    if not p or not p.get("id"):
        return None
    return {
        "id": p.get("id", ""),
        "uri": p.get("uri", ""),
        "name": p.get("name", ""),
        "owner": (p.get("owner") or {}).get("display_name", ""),
        "art": _art(p.get("images"), prefer_small=False),
        "total": (p.get("tracks") or {}).get("total", 0),
    }


def search(sp, query, limit=10):
    """Search tracks, albums, and playlists in one request."""
    _guard()
    try:
        res = sp.search(q=query, type="track,album,playlist", limit=limit)
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    tracks = [x for x in (
        _norm_track(t) for t in ((res.get("tracks") or {}).get("items") or [])
    ) if x]
    albums = [x for x in (
        _norm_album(a) for a in ((res.get("albums") or {}).get("items") or [])
    ) if x]
    playlists = [x for x in (
        _norm_playlist(p) for p in ((res.get("playlists") or {}).get("items") or [])
    ) if x]
    return {"tracks": tracks, "albums": albums, "playlists": playlists}


def _page_meta(res, offset, items):
    """Pagination metadata. next_offset counts raw API rows (including skipped
    entries) so load-more never re-fetches or skips playlist/liked rows."""
    raw = len(res.get("items") or [])
    total = res.get("total") or 0
    next_off = offset + raw
    return {
        "total": total,
        "offset": offset,
        "next_offset": next_off,
        "has_more": next_off < total,
    }


def get_playlists(sp, limit=50, offset=0):
    _guard()
    try:
        res = sp.current_user_playlists(limit=limit, offset=offset)
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    items = [x for x in (_norm_playlist(p) for p in (res.get("items") or [])) if x]
    meta = _page_meta(res, offset, items)
    return {"items": items, **meta}


def get_playlist_tracks(sp, playlist_id, limit=50, offset=0):
    _guard()
    fallback_art = ""
    try:
        if offset == 0:
            meta = sp.playlist(playlist_id, fields="images")
            fallback_art = _art((meta or {}).get("images"), prefer_small=False)
        res = sp.playlist_items(
            playlist_id, limit=limit, offset=offset, additional_types=("track",)
        )
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    items = []
    for entry in res.get("items") or []:
        raw = _playlist_entry_track(entry)
        if not raw:
            continue
        t = _norm_track(raw)
        if not t:
            continue
        if not t.get("art") and fallback_art:
            t["art"] = fallback_art
        items.append(t)
    meta = _page_meta(res, offset, items)
    return {"items": items, **meta}


def get_liked(sp, limit=50, offset=0):
    _guard()
    try:
        res = sp.current_user_saved_tracks(limit=limit, offset=offset)
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    items = []
    for entry in res.get("items") or []:
        t = _norm_track((entry or {}).get("track"))
        if t:
            items.append(t)
    meta = _page_meta(res, offset, items)
    return {"items": items, **meta}


def get_album_tracks(sp, album_id, limit=50, offset=0):
    _guard()
    try:
        # Always fetch album metadata: paginated pages need the art/name too
        # (album_tracks items don't carry album info).
        album = sp.album(album_id)
        res = sp.album_tracks(album_id, limit=limit, offset=offset)
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    art = _art((album or {}).get("images")) if album else ""
    items = []
    for t in res.get("items") or []:
        nt = _norm_track(t, art_override=art)
        if nt:
            nt["album"] = (album or {}).get("name", "")
            items.append(nt)
    meta = _page_meta(res, offset, items)
    return {
        "items": items,
        "name": (album or {}).get("name", ""),
        "artists": ", ".join(a.get("name", "") for a in ((album or {}).get("artists") or [])),
        "art": art,
        "uri": (album or {}).get("uri", "") or ("spotify:album:" + album_id),
        **meta,
    }


def get_queue(sp):
    """Current playback queue: now playing + up-next list."""
    _guard()
    try:
        res = sp.queue()
    except Exception as e:
        if spotify_controller.handle_429(e):
            raise RateLimitedError("spotify rate limited")
        raise
    now_playing = _norm_track((res or {}).get("currently_playing"))
    items = [x for x in (
        _norm_track(t) for t in ((res or {}).get("queue") or [])
    ) if x]
    return {"currently_playing": now_playing, "items": items}


def _start_playback(sp, **kwargs):
    """start_playback with no-active-device fallback (same recovery pattern
    as the play/pause controls in spotify_controller)."""
    try:
        sp.start_playback(**kwargs)
        return True
    except Exception as e:
        if spotify_controller.handle_429(e):
            return False
        err = str(e)
        if "NO_ACTIVE_DEVICE" in err or "Not found" in err.lower() or "404" in err:
            dev = spotify_controller._grab_device(sp)
            if dev:
                try:
                    sp.start_playback(device_id=dev, **kwargs)
                    return True
                except Exception as e2:
                    print(f"[Library] Play retry failed: {e2}")
        else:
            print(f"[Library] Play failed: {err}")
        return False


def _apply_playback_modes(sp, shuffle=None, repeat=None):
    """Set shuffle/repeat before starting a context (playlist/album)."""
    if shuffle is None and repeat is None:
        return True
    dev = None
    with spotify_controller._lock:
        dev = spotify_controller._active_device_id
    try:
        if shuffle is not None:
            if dev:
                sp.shuffle(bool(shuffle), device_id=dev)
            else:
                sp.shuffle(bool(shuffle))
            with spotify_controller._lock:
                spotify_controller._current_data["shuffle_state"] = bool(shuffle)
        if repeat is not None and repeat in ("off", "context", "track"):
            if dev:
                sp.repeat(repeat, device_id=dev)
            else:
                sp.repeat(repeat)
            with spotify_controller._lock:
                spotify_controller._current_data["repeat_state"] = repeat
        return True
    except Exception as e:
        if spotify_controller.handle_429(e):
            return False
        print(f"[Library] playback mode failed: {e}")
        return False


def play(
    sp,
    uri=None,
    uris=None,
    context_uri=None,
    offset_uri=None,
    position=None,
    shuffle=None,
    repeat=None,
):
    """Play a track, a list of tracks, or a context (album/playlist),
    optionally starting at a specific track within the context."""
    _guard()
    kwargs = {}
    if context_uri:
        kwargs["context_uri"] = context_uri
        if offset_uri:
            kwargs["offset"] = {"uri": offset_uri}
        elif position is not None:
            kwargs["offset"] = {"position": int(position)}
    elif uris:
        kwargs["uris"] = list(uris)[:50]
    elif uri:
        kwargs["uris"] = [uri]
    else:
        return False

    if shuffle is not None or repeat is not None:
        if not _apply_playback_modes(sp, shuffle=shuffle, repeat=repeat):
            return False

    ok = _start_playback(sp, **kwargs)
    if ok:
        spotify_controller._mark_optimistic()
        spotify_controller._apply_local_playback(is_playing=True)
        spotify_controller.force_poll()
    return ok


def queue(sp, uri):
    """Add a track to the playback queue."""
    _guard()
    if not uri:
        return False
    try:
        sp.add_to_queue(uri)
        return True
    except Exception as e:
        if spotify_controller.handle_429(e):
            return False
        err = str(e)
        if "NO_ACTIVE_DEVICE" in err or "Not found" in err.lower() or "404" in err:
            dev = spotify_controller._grab_device(sp)
            if dev:
                try:
                    sp.add_to_queue(uri, device_id=dev)
                    return True
                except Exception as e2:
                    print(f"[Library] Queue retry failed: {e2}")
        else:
            print(f"[Library] Queue failed: {err}")
        return False
