/* PiMusic lyrics overlay — synced lyrics with auto-scroll + highlight,
   driven by the player's predictive clock. Tap a line to seek. */
(function () {
  "use strict";

  var btn     = document.getElementById("lyrics-btn");
  var overlay = document.getElementById("lyrics-overlay");
  if (!btn || !overlay) return;

  var demo = document.body.getAttribute("data-demo") === "1";
  if (demo) {
    btn.classList.add("hidden");
    return;
  }

  var closeBtn  = document.getElementById("lyrics-close");
  var searchBtn = document.getElementById("lyrics-search-btn");
  var content  = document.getElementById("lyrics-content");
  var headTitle = document.getElementById("lyrics-track-title");
  var headSub   = document.getElementById("lyrics-track-sub");
  var bgEl     = document.getElementById("lyrics-bg");
  var transportPrev = document.getElementById("lyrics-btn-prev");
  var transportPlay = document.getElementById("lyrics-btn-play");
  var transportNext = document.getElementById("lyrics-btn-next");
  var transportIconPlay  = document.getElementById("lyrics-icon-play");
  var transportIconPause = document.getElementById("lyrics-icon-pause");
  var timeCur = document.getElementById("lyrics-time-current");
  var timeTot = document.getElementById("lyrics-time-total");
  var mainBtnPlay = document.getElementById("btn-play");
  var mainBtnNext = document.getElementById("btn-next");
  var mainBtnPrev = document.getElementById("btn-prev");
  var mainVolSlider = document.getElementById("volume-slider");
  var lyricsVolSlider = document.getElementById("lyrics-volume-slider");

  var P = window.PiMusic || {};
  var mediaBg = false;      // canvas/art behind lyrics (lyrics_bg setting)
  var canvasBorrowed = false;

  var isOpen = false;
  var fetchToken = 0;
  var loadedTrackId = null;
  var syncedLines = null;     // [{time_ms, text, words?}] or null
  var lineEls = [];
  var linesWrap = null;
  var activeIdx = -1;
  var activeWordIdx = -1;
  var wordSyncMode = false;
  var syncTimer = null;
  var chromeTimer = null;
  var SYNC_INTERVAL_MS = 200;
  var WORD_SYNC_INTERVAL_MS = 100;
  var CHROME_INTERVAL_MS = 1000;
  var lastScrollTarget = null;
  var activeWordEl = null;
  var LYRICS_LEAD_MS = 250;   // highlight slightly early to cover latency
  var lyricsCache = Object.create(null);   /* track_id -> API payload */
  var prefetchPromises = Object.create(null);  /* track_id -> Promise */
  var lastQueuePrefetchId = "";
  var QUEUE_PREFETCH_MAX = 2;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  var artEl = document.getElementById("lyrics-art");
  var progressFillEl = document.getElementById("lyrics-progress-fill");
  var appliedArtSrc = null;

  function setHeader() {
    var s = P.getState ? P.getState() : {};
    headTitle.textContent = s.track || "Lyrics";
    headSub.textContent = s.artist || "";
    if (artEl) {
      var src = s.album_art_local || s.album_art_url || "";
      if (src !== appliedArtSrc) {
        appliedArtSrc = src;
        artEl.src = src;
        artEl.style.visibility = src ? "visible" : "hidden";
      }
    }
  }

  var transportPlaying = null;

  var timeCurText = "";
  var timeTotText = "";

  function syncTransport() {
    if (!transportPlay || !transportIconPlay || !transportIconPause) return;
    var playing = !!(P.getState && P.getState().is_playing);
    if (playing === transportPlaying) return;
    transportPlaying = playing;
    transportIconPlay.classList.toggle("hidden", playing);
    transportIconPause.classList.toggle("hidden", !playing);
  }

  function syncTime() {
    if (!timeCur || !timeTot || !P.clockNow || !P.fmt) return;
    var s = P.getState ? P.getState() : {};
    var cur = P.fmt(P.clockNow());
    var tot = P.fmt(s.duration_ms || 0);
    if (cur !== timeCurText) {
      timeCurText = cur;
      timeCur.textContent = cur;
    }
    if (tot !== timeTotText) {
      timeTotText = tot;
      timeTot.textContent = tot;
    }
    if (progressFillEl) {
      var d = s.duration_ms || 0;
      var pct = d ? Math.min(1, Math.max(0, P.clockNow() / d)) : 0;
      progressFillEl.style.transform = "scaleX(" + pct + ")";
    }
  }

  function wireTransport(btn, mainBtn) {
    if (!btn || !mainBtn) return;
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      mainBtn.click();
      syncTransport();
    });
  }

  wireTransport(transportPrev, mainBtnPrev);
  wireTransport(transportPlay, mainBtnPlay);
  wireTransport(transportNext, mainBtnNext);

  var lyricsVolDragging = false;

  function syncVolume() {
    if (!lyricsVolSlider || lyricsVolDragging) return;
    var v = (P.getState && P.getState().volume) || 0;
    if (parseInt(lyricsVolSlider.value, 10) !== v) {
      lyricsVolSlider.value = v;
    }
  }

  if (lyricsVolSlider && mainVolSlider) {
    lyricsVolSlider.addEventListener("pointerdown", function (e) {
      e.stopPropagation();
      lyricsVolDragging = true;
      mainVolSlider.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    });
    lyricsVolSlider.addEventListener("input", function (e) {
      e.stopPropagation();
      mainVolSlider.value = e.target.value;
      mainVolSlider.dispatchEvent(new Event("input"));
    });
    window.addEventListener("pointerup", function () {
      lyricsVolDragging = false;
    });
  }

  /* ── Media background (canvas video / album art behind lyrics) ── */

  var appliedBgArt = null;

  function trackHasCanvas(s) {
    return !!(s && s.visual_type === "canvas_video"
      && (s.canvas_url || s.canvas_cdn_url));
  }

  function releaseBorrowedCanvas() {
    if (!canvasBorrowed || !P.returnCanvas) return;
    P.returnCanvas();
    canvasBorrowed = false;
  }

  function syncMediaBg() {
    if (!mediaBg || !bgEl) return;
    var s = P.getState ? P.getState() : {};
    var wantsCanvas = trackHasCanvas(s);
    var canvasReady = wantsCanvas && !!(P.isCanvasActive && P.isCanvasActive());

    if (wantsCanvas) {
      if (P.borrowCanvas && !canvasBorrowed) {
        P.borrowCanvas(bgEl);
        canvasBorrowed = true;
      }
    } else {
      releaseBorrowedCanvas();
    }

    /* Canvas-only CSS when the video is actually visible — not just metadata. */
    overlay.classList.toggle("lyrics-overlay--canvas", canvasReady);

    /* Canvas: video only — no blurred _bg.jpg underneath (keeps loop sharp).
       Art-only: sharp album art, not the pre-blurred fullscreen variant. */
    if (wantsCanvas) {
      if (canvasReady && appliedBgArt !== "") {
        appliedBgArt = "";
        bgEl.style.backgroundImage = "none";
      }
      return;
    }
    var art = s.album_art_local || s.album_art_url || "";
    if (art === appliedBgArt) return;
    appliedBgArt = art;
    bgEl.style.backgroundImage = art ? 'url("' + art + '")' : "none";
  }

  function enableMediaBg() {
    var s = P.getState ? P.getState() : {};
    mediaBg = (s.lyrics_bg || "media") !== "dark";
    overlay.classList.toggle("lyrics-overlay--media", mediaBg);
    if (!mediaBg) {
      releaseBorrowedCanvas();
      overlay.classList.remove("lyrics-overlay--canvas");
      appliedBgArt = null;
      return;
    }
    syncMediaBg();
  }

  function disableMediaBg() {
    releaseBorrowedCanvas();
    overlay.classList.remove("lyrics-overlay--media");
    overlay.classList.remove("lyrics-overlay--canvas");
    appliedBgArt = null;
    mediaBg = false;
  }

  function showMessage(text) {
    content.innerHTML = "";
    content.classList.remove("lyrics-content--synced");
    content.classList.remove("lyrics-content--word-sync");
    syncedLines = null;
    wordSyncMode = false;
    linesWrap = null;
    content.appendChild(el("div", "lyrics-message", text));
    if (isOpen) startSyncLoop();
  }

  function showSpinner() {
    content.innerHTML = "";
    content.classList.remove("lyrics-content--synced");
    content.appendChild(el("div", "lib-spinner"));
  }

  /* ── Rendering ────────────────────────────────────────── */

  function lineHasWords(line) {
    return !!(line && line.words && line.words.length);
  }

  function detectWordSync(lines) {
    for (var i = 0; i < lines.length; i++) {
      if (lineHasWords(lines[i])) return true;
    }
    return false;
  }

  function renderSynced(lines, source) {
    content.innerHTML = "";
    content.classList.add("lyrics-content--synced");
    syncedLines = lines;
    lineEls = [];
    activeIdx = -1;
    activeWordIdx = -1;
    activeWordEl = null;
    lastScrollTarget = null;
    wordSyncMode = detectWordSync(lines);
    content.classList.toggle("lyrics-content--word-sync", wordSyncMode);

    linesWrap = el("div", "lyrics-lines");
    for (var i = 0; i < lines.length; i++) {
      (function (idx) {
        var row = lines[idx];
        var text = row.text;
        var line = el("div", "lyric-line");
        if (!text) {
          line.textContent = "\u266a";
          line.classList.add("lyric-line--gap");
        } else if (lineHasWords(row)) {
          line.classList.add("lyric-line--karaoke");
          var parts = row.words;
          for (var w = 0; w < parts.length; w++) {
            if (w > 0) line.appendChild(document.createTextNode(" "));
            (function (wordIdx) {
              var span = el("span", "lyric-word", parts[wordIdx].text);
              span.addEventListener("click", function (e) {
                e.stopPropagation();
                if (P.seek) {
                  P.seek(parts[wordIdx].time_ms);
                  updateSync(true);
                }
              });
              line.appendChild(span);
            })(w);
          }
        } else {
          line.textContent = text;
        }
        if (!lineHasWords(row)) {
          line.addEventListener("click", function () {
            if (P.seek) {
              P.seek(row.time_ms);
              updateSync(true);
            }
          });
        } else {
          line.addEventListener("click", function () {
            if (P.seek) {
              P.seek(row.time_ms);
              updateSync(true);
            }
          });
        }
        linesWrap.appendChild(line);
        lineEls.push(line);
      })(i);
    }
    var credit = el("div", "lyrics-credit", "Lyrics via " + (source === "spotify" ? "Spotify" : "LRCLIB"));
    content.appendChild(linesWrap);
    content.appendChild(credit);
    if (isOpen) startSyncLoop();   // switch to the fast highlight tick
    requestAnimationFrame(function () { updateSync(true); });
  }

  function renderPlain(text, source) {
    content.innerHTML = "";
    content.classList.remove("lyrics-content--synced");
    content.classList.remove("lyrics-content--word-sync");
    syncedLines = null;
    wordSyncMode = false;
    var wrap = el("div", "lyrics-plain", text);
    content.appendChild(wrap);
    content.appendChild(el("div", "lyrics-credit", "Lyrics via " + (source === "spotify" ? "Spotify" : "LRCLIB")));
    content.scrollTop = 0;
    if (isOpen) startSyncLoop();   // drop back to the slow bg-only tick
  }

  /* ── Sync loop ────────────────────────────────────────── */

  function currentLineIndex(posMs) {
    if (!syncedLines || !syncedLines.length) return -1;
    var lo = 0;
    var hi = syncedLines.length - 1;
    var idx = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (syncedLines[mid].time_ms <= posMs) {
        idx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return idx;
  }

  function currentWordIndex(words, posMs) {
    var idx = -1;
    for (var i = 0; i < words.length; i++) {
      if (words[i].time_ms <= posMs) idx = i;
      else break;
    }
    return idx;
  }

  function updateSync(force) {
    if (!isOpen || !syncedLines || !linesWrap || !P.clockNow) return;
    var pos = P.clockNow() + LYRICS_LEAD_MS;
    var idx = currentLineIndex(pos);
    var wordIdx = -1;
    if (idx >= 0 && lineHasWords(syncedLines[idx])) {
      wordIdx = currentWordIndex(syncedLines[idx].words, pos);
    }
    if (idx === activeIdx && wordIdx === activeWordIdx && !force) return;

    var lineChanged = idx !== activeIdx;

    if (lineChanged && activeIdx >= 0 && lineEls[activeIdx]) {
      if (activeWordEl) {
        activeWordEl.classList.remove("lyric-word--active");
        activeWordEl = null;
      }
      lineEls[activeIdx].classList.remove("lyric-line--active");
    } else if (!lineChanged && wordIdx !== activeWordIdx && activeWordEl) {
      activeWordEl.classList.remove("lyric-word--active");
      activeWordEl = null;
    }

    activeIdx = idx;
    activeWordIdx = wordIdx;

    if (idx >= 0 && lineEls[idx]) {
      var row = syncedLines[idx];
      lineEls[idx].classList.add("lyric-line--active");
      if (lineHasWords(row) && wordIdx >= 0) {
        var spans = lineEls[idx].querySelectorAll(".lyric-word");
        if (spans[wordIdx]) {
          activeWordEl = spans[wordIdx];
          activeWordEl.classList.add("lyric-word--active");
        }
      }
      if (lineChanged || force) {
        var containerH = content.clientHeight;
        var line = lineEls[idx];
        var target = (containerH * 0.40) - line.offsetTop - (line.offsetHeight / 2);
        if (target > 0) target = 0;
        var rounded = Math.round(target);
        if (rounded !== lastScrollTarget) {
          lastScrollTarget = rounded;
          linesWrap.style.transform = "translateY(" + rounded + "px)";
        }
      }
    }
  }

  function syncLyricsChrome() {
    if (!isOpen) return;
    syncMediaBg();
    syncTransport();
    syncTime();
    syncVolume();
  }

  function startSyncLoop() {
    stopSyncLoop();
    if (syncedLines) {
      var interval = wordSyncMode ? WORD_SYNC_INTERVAL_MS : SYNC_INTERVAL_MS;
      syncTimer = setInterval(function () { updateSync(false); }, interval);
      chromeTimer = setInterval(syncLyricsChrome, CHROME_INTERVAL_MS);
    } else {
      syncTimer = setInterval(syncLyricsChrome, CHROME_INTERVAL_MS);
    }
  }

  function stopSyncLoop() {
    if (syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }
    if (chromeTimer) {
      clearInterval(chromeTimer);
      chromeTimer = null;
    }
  }

  /* ── Fetching ─────────────────────────────────────────── */

  function metaToState(meta, source) {
    return {
      track_id: meta.id || meta.track_id || "",
      track: meta.name || meta.track || "",
      artist: meta.artists || meta.artist || "",
      album: meta.album || "",
      duration_ms: meta.duration_ms || 0,
      source: source || meta.source || "spotify"
    };
  }

  function lyricsParams(s) {
    return "artist=" + encodeURIComponent(s.artist || "") +
      "&track=" + encodeURIComponent(s.track || "") +
      "&album=" + encodeURIComponent(s.album || "") +
      "&duration_ms=" + encodeURIComponent(s.duration_ms || 0) +
      "&track_id=" + encodeURIComponent(s.track_id || "") +
      "&source=" + encodeURIComponent(s.source || "spotify");
  }

  function requestLyrics(s) {
    if (!s.track_id || !s.track) {
      return Promise.reject(new Error("no track"));
    }
    return fetch("/api/lyrics?" + lyricsParams(s)).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      lyricsCache[s.track_id] = data;
      return data;
    });
  }

  function prefetchLyricsForMeta(meta, source) {
    var s = metaToState(meta, source);
    if (!s.track_id || !s.track) return null;
    if (lyricsCache[s.track_id]) return null;
    if (prefetchPromises[s.track_id]) return prefetchPromises[s.track_id];
    prefetchPromises[s.track_id] = requestLyrics(s).catch(function () {
      return null;
    }).then(function (data) {
      delete prefetchPromises[s.track_id];
      if (isOpen && s.track_id === (P.getState ? P.getState().track_id : "")) {
        if (loadedTrackId !== s.track_id) fetchLyrics();
      }
      return data;
    });
    return prefetchPromises[s.track_id];
  }

  function prefetchQueueUpNext(force) {
    var s = P.getState ? P.getState() : {};
    if (!s.track_id || s.source !== "spotify") return;
    if (!force && s.track_id === lastQueuePrefetchId) return;
    lastQueuePrefetchId = s.track_id;
    fetch("/api/queue").then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (data) {
      if (!data) return;
      var items = data.items || [];
      for (var i = 0; i < items.length && i < QUEUE_PREFETCH_MAX; i++) {
        prefetchLyricsForMeta(items[i], "spotify");
      }
    }).catch(function () {});
  }

  function renderLyricsData(data) {
    if (data.instrumental) {
      showMessage("\u266a Instrumental \u266a");
    } else if (data.synced && data.synced.length) {
      renderSynced(data.synced, data.source);
    } else if (data.plain) {
      renderPlain(data.plain, data.source);
    } else {
      showMessage("No lyrics found for this song");
    }
  }

  function prefetchLyrics() {
    var s = P.getState ? P.getState() : {};
    prefetchLyricsForMeta({
      id: s.track_id,
      name: s.track,
      artists: s.artist,
      album: s.album,
      duration_ms: s.duration_ms
    }, s.source);
    prefetchQueueUpNext();
  }

  function fetchLyrics() {
    var s = P.getState ? P.getState() : {};
    setHeader();
    syncTime();

    if (!s.track_id || !s.track) {
      loadedTrackId = null;
      showMessage("Nothing playing");
      return;
    }
    if (s.track_id === loadedTrackId) return;

    var myToken = ++fetchToken;
    var trackId = s.track_id;
    loadedTrackId = trackId;

    if (lyricsCache[trackId]) {
      renderLyricsData(lyricsCache[trackId]);
      return;
    }

    showSpinner();

    var pending = prefetchPromises[trackId];
    var load = pending || requestLyrics(s);
    load.then(function (data) {
      if (myToken !== fetchToken || !isOpen) return;
      if (!data) {
        loadedTrackId = null;
        showMessage("Couldn't load lyrics");
        return;
      }
      renderLyricsData(data);
    }).catch(function () {
      if (myToken !== fetchToken) return;
      loadedTrackId = null;
      showMessage("Couldn't load lyrics");
    });
  }

  /* ── Open / close / track changes ─────────────────────── */

  function open() {
    if (window.PiMusicLibrary && window.PiMusicLibrary.isOpen()) {
      window.PiMusicLibrary.close();
    }
    isOpen = true;
    overlay.classList.remove("hidden");
    transportPlaying = null;
    enableMediaBg();
    loadedTrackId = null;   // always refresh on open
    fetchLyrics();
    syncTransport();
    syncTime();
    syncVolume();
    startSyncLoop();
  }

  function close() {
    isOpen = false;
    overlay.classList.add("hidden");
    disableMediaBg();
    stopSyncLoop();
  }

  function toggle() {
    if (isOpen) close();
    else open();
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    if (isOpen) close();
    else open();
  });
  closeBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    close();
  });
  if (searchBtn) {
    searchBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (window.PiMusicLibrary && window.PiMusicLibrary.openSearch) {
        window.PiMusicLibrary.openSearch(true);
      }
    });
  }
  overlay.addEventListener("click", function (e) { e.stopPropagation(); });

  if (P.onTrackChange) {
    P.onTrackChange(function () {
      lastQueuePrefetchId = "";
      prefetchLyrics();
      if (isOpen) {
        syncMediaBg();
        loadedTrackId = null;
        fetchLyrics();
      }
    });
  }

  /* Warm current + queue lyrics as soon as the player is up. */
  if (document.readyState === "complete") {
    prefetchLyrics();
  } else {
    window.addEventListener("load", prefetchLyrics);
  }
  /* Re-prefetch queue periodically (shuffle/reorder won't fire track change). */
  setInterval(function () {
    if (P.getState && P.getState().track_id) prefetchQueueUpNext(true);
  }, 45000);

  window.PiMusicLyrics = {
    isOpen: function () { return isOpen; },
    close: close,
    open: open,
    toggle: toggle,
    syncMediaBg: syncMediaBg
  };
})();
