(function () {
  "use strict";

  /* ── DOM refs ──────────────────────────────────────── */

  var $ = function (s) { return document.querySelector(s); };

  var dom = {
    bg:        $("#bg-layer"),
    art:       $("#album-art"),
    canvas:    $("#canvas-video"),
    title:     $("#track-title"),
    artist:    $("#track-artist"),
    timeCur:   $("#time-current"),
    timeTotal: $("#time-total"),
    fill:      $("#progress-fill"),
    thumb:     $("#progress-thumb"),
    bar:       $("#progress-bar"),
    btnPlay:   $("#btn-play"),
    btnNext:   $("#btn-next"),
    btnPrev:   $("#btn-prev"),
    iconPlay:  $("#icon-play"),
    iconPause: $("#icon-pause"),
    volSlider: $("#volume-slider"),
    device:    $("#device-name"),
    player:    $(".player"),
    trackInfo: $(".track-info"),
    srcSpotify: $("#src-spotify"),
    srcCider:   $("#src-cider")
  };

  console.log("[PiMusic] app.js v20 loaded at " + new Date().toISOString());

  /* ── Constants ─────────────────────────────────────── */

  var POLL_MS           = 1000;
  var POLL_TIMEOUT_MS   = 2000;
  var DRIFT_CORRECT_MS  = 1500;
  var TEXT_UPDATE_MS    = 200;
  var SNAPBACK_GUARD_MS = 5000;
  var BTN_COOLDOWN_MS   = 500;
  var INPUT_LOCK_MS     = 5000;
  var STALE_EXTEND_MS   = 2000;

  /* ── State ─────────────────────────────────────────── */

  var state = {
    is_playing: false,
    progress_ms: 0,
    duration_ms: 0,
    album_art_local: "",
    album_art_url: "",
    dominant_color: "#1a1a2e",
    track: "",
    artist: "",
    device: "",
    volume: 50,
    track_id: "",
    canvas_url: null,
    server_time: 0,
    track_changed_at: 0,
    rate_limited_until: 0,
    source: "spotify",
    active_source: "auto"
  };

  /* ── Predictive clock ──────────────────────────────── */

  var clockMs       = 0;
  var clockAnchor   = 0;
  var clockRate     = 1.0;
  var driftTarget   = null;
  var driftStart    = 0;
  var driftDuration = 0;

  var trackChangeLocalTs = 0;

  /* ── Render cache ──────────────────────────────────── */

  var prevRenderedArt     = "";
  var prevRenderedTitle   = "";
  var prevRenderedArtist  = "";
  var prevRenderedDevice  = "";
  var prevRenderedPlaying = null;
  var prevTimeCurText     = "";
  var prevTimeTotalText   = "";
  var prevPct             = -1;

  var activeCanvasSrc = "";
  var canvasMode = false;
  var artworkWrap = $("#artwork-wrap");

  /* ── Input locks ───────────────────────────────────── */

  var volDragging    = false;
  var volTimer       = null;
  var volLockUntil   = 0;
  var volBeforeDrag  = 50;
  var seekDragging   = false;
  var seekTimer      = null;
  var seekLockUntil  = 0;
  var seekTarget     = 0;
  var cooldownUntil  = 0;
  var lastTextUpdate = 0;
  var pendingSkip    = false;
  var pollReqId      = 0;

  /* ── Predictive clock functions ────────────────────── */

  function clockNow() {
    if (!state.is_playing) return clockMs;
    var elapsed = (performance.now() - clockAnchor) * clockRate;
    return Math.min(Math.max(clockMs + elapsed, 0), state.duration_ms || 0);
  }

  function clockSet(ms) {
    clockMs     = ms;
    clockAnchor = performance.now();
    clockRate   = 1.0;
    driftTarget = null;
  }

  function clockApplyDrift(serverMs) {
    var localMs = clockNow();
    var drift   = serverMs - localMs;
    if (Math.abs(drift) < 80) return;
    var sinceTrackChange = performance.now() - trackChangeLocalTs;
    if (sinceTrackChange < SNAPBACK_GUARD_MS && drift < -200) return;
    if (performance.now() < seekLockUntil) return;
    if (Math.abs(drift) > 4000) { clockSet(serverMs); return; }
    if (Math.abs(drift) < 2000) {
      clockMs       = localMs;
      clockAnchor   = performance.now();
      clockRate     = 1.0 + (drift / DRIFT_CORRECT_MS);
      driftTarget   = serverMs;
      driftStart    = performance.now();
      driftDuration = DRIFT_CORRECT_MS;
      return;
    }
    clockSet(serverMs);
  }

  function clockTick() {
    if (driftTarget !== null) {
      if (performance.now() - driftStart >= driftDuration) {
        clockMs     = clockNow();
        clockAnchor = performance.now();
        clockRate   = 1.0;
        driftTarget = null;
      }
    }
  }

  /* ── Helpers ───────────────────────────────────────── */

  function fmt(ms) {
    var total = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    }).catch(function () { return { ok: false }; });
  }

  var emergencyPollTimer = null;
  function emergencyPoll(delayMs) {
    clearTimeout(emergencyPollTimer);
    emergencyPollTimer = setTimeout(function () {
      post("/api/force-poll").then(function () { poll(); });
    }, delayMs);
  }

  /* ── Source switcher ───────────────────────────────── */

  function setSourceUI(src) {
    if (src === "cider") {
      dom.srcCider.classList.add("src-btn--active");
      dom.srcSpotify.classList.remove("src-btn--active");
    } else {
      dom.srcSpotify.classList.add("src-btn--active");
      dom.srcCider.classList.remove("src-btn--active");
    }
  }

  function switchSource(src) {
    post("/api/source", { source: src }).then(function () {
      poll();
    });
  }

  dom.srcSpotify.addEventListener("click", function () {
    setSourceUI("spotify");
    switchSource("spotify");
  });

  dom.srcCider.addEventListener("click", function () {
    setSourceUI("cider");
    switchSource("cider");
  });

  /* ── Canvas / video (event-driven) ─────────────────── */

  function positionCanvasOverArt() {
    if (canvasMode) return;
    var rect = artworkWrap.getBoundingClientRect();
    dom.canvas.style.position = "absolute";
    dom.canvas.style.top      = rect.top + "px";
    dom.canvas.style.left     = rect.left + "px";
    dom.canvas.style.width    = rect.width + "px";
    dom.canvas.style.height   = rect.height + "px";
    dom.canvas.style.borderRadius = "var(--radius)";
  }

  function applyCanvas(url) {
    if (!url) {
      if (activeCanvasSrc) {
        activeCanvasSrc = "";
        exitCinematic();
        dom.canvas.classList.remove("active");
        dom.canvas.removeAttribute("src");
        dom.canvas.load();
        dom.bg.classList.remove("bg-canvas-active");
      }
      return;
    }

    if (url === activeCanvasSrc) return;

    activeCanvasSrc = url;
    dom.canvas.classList.remove("active");

    dom.canvas.oncanplay = function () {
      dom.canvas.classList.add("active");
      dom.bg.classList.add("bg-canvas-active");
      if (!canvasMode) positionCanvasOverArt();
      dom.canvas.play().catch(function () {});
    };

    dom.canvas.onerror = function () {
      activeCanvasSrc = "";
      dom.canvas.classList.remove("active");
      dom.bg.classList.remove("bg-canvas-active");
    };

    dom.canvas.src = url;
    dom.canvas.load();
  }

  /* ── Cinematic mode toggle ─────────────────────────── */

  function enterCinematic() {
    canvasMode = true;
    document.body.classList.add("canvas-cinematic");
    dom.canvas.style.position = "";
    dom.canvas.style.top      = "";
    dom.canvas.style.left     = "";
    dom.canvas.style.width    = "";
    dom.canvas.style.height   = "";
    dom.canvas.style.borderRadius = "";
  }

  function exitCinematic() {
    canvasMode = false;
    document.body.classList.remove("canvas-cinematic");
    if (activeCanvasSrc) positionCanvasOverArt();
  }

  /* ── Render (60fps via rAF, scaleX progress) ───────── */

  function render(timestamp) {
    requestAnimationFrame(render);
    clockTick();

    var p   = clockNow();
    var d   = state.duration_ms || 1;
    var pct = Math.min(1, Math.max(0, p / d));

    if (!seekDragging && performance.now() >= seekLockUntil) {
      var pctRound = Math.round(pct * 10000);
      if (pctRound !== prevPct) {
        prevPct = pctRound;
        dom.fill.style.transform = "scaleX(" + pct + ")";
        dom.thumb.style.left     = (pct * 100) + "%";
      }
    }

    if (timestamp - lastTextUpdate > TEXT_UPDATE_MS) {
      lastTextUpdate = timestamp;

      var curText = fmt(p);
      if (curText !== prevTimeCurText) {
        prevTimeCurText = curText;
        dom.timeCur.textContent = curText;
      }
      var totText = fmt(d);
      if (totText !== prevTimeTotalText) {
        prevTimeTotalText = totText;
        dom.timeTotal.textContent = totText;
      }
      if (state.track !== prevRenderedTitle) {
        prevRenderedTitle = state.track;
        dom.title.textContent = state.track || "No music playing";
      }
      if (state.artist !== prevRenderedArtist) {
        prevRenderedArtist = state.artist;
        dom.artist.textContent = state.artist || "\u2014";
      }

      /* Device / rate-limit display */
      var deviceText = "";
      if (state.rate_limited_until > 0) {
        var secLeft = Math.max(0, Math.ceil(state.rate_limited_until - Date.now() / 1000));
        var minLeft = Math.ceil(secLeft / 60);
        deviceText = "Rate limited \u2013 back in " + (minLeft >= 60 ? Math.ceil(minLeft / 60) + "h" : minLeft + " min");
      } else {
        deviceText = state.device || "No device";
      }
      if (deviceText !== prevRenderedDevice) {
        prevRenderedDevice = deviceText;
        dom.device.textContent = deviceText;
      }
    }

    if (state.is_playing !== prevRenderedPlaying) {
      prevRenderedPlaying = state.is_playing;
      dom.iconPlay.classList.toggle("hidden", state.is_playing);
      dom.iconPause.classList.toggle("hidden", !state.is_playing);
      if (state.is_playing) {
        dom.player.classList.remove("idle");
      } else if (!state.track) {
        dom.player.classList.add("idle");
      }
    }

    if (!volDragging && performance.now() > volLockUntil) {
      dom.volSlider.value = state.volume;
    }

    /* Album art */
    var artSrc = state.album_art_local || state.album_art_url || "";
    if (artSrc && artSrc !== prevRenderedArt) {
      prevRenderedArt = artSrc;
      var img = new Image();
      img.onload = function () {
        dom.art.classList.remove("fresh");
        void dom.art.offsetWidth;
        dom.art.src = artSrc;
        dom.art.classList.add("fresh");
        if (!state.canvas_url) {
          dom.bg.style.backgroundImage = "url(\"" + artSrc + "\")";
        }
        if (activeCanvasSrc && !canvasMode) positionCanvasOverArt();
      };
      img.src = artSrc;
    }
  }

  /* ── API polling (1s, with 2s AbortController timeout) */

  var pollAbortCtrl = null;

  function poll() {
    pollReqId += 1;
    var myReqId = pollReqId;

    /* Abort any in-flight poll that's stuck */
    if (pollAbortCtrl) {
      try { pollAbortCtrl.abort(); } catch (e) { /* ignore */ }
    }
    pollAbortCtrl = new AbortController();
    var signal = pollAbortCtrl.signal;

    /* 2-second hard timeout */
    var timeoutId = setTimeout(function () {
      try { pollAbortCtrl.abort(); } catch (e) { /* ignore */ }
    }, POLL_TIMEOUT_MS);

    return fetch("/api/state", { signal: signal }).then(function (res) {
      clearTimeout(timeoutId);
      if (!res.ok) return;
      return res.json().then(function (data) {
        if (myReqId !== pollReqId) return;
        var trackChanged = data.track_id && data.track_id !== state.track_id;

        if (pendingSkip) {
          pendingSkip = false;
          dom.trackInfo.classList.remove("stale");
        }

        var now = performance.now();

        /* Volume lock */
        if (volDragging || now < volLockUntil) {
          data.volume = state.volume;
        } else if (volLockUntil > 0 && data.volume === volBeforeDrag) {
          volLockUntil = now + STALE_EXTEND_MS;
          data.volume = state.volume;
        }

        /* Seek lock */
        if (seekDragging || (now < seekLockUntil && !trackChanged)) {
          data.progress_ms = undefined;
        } else if (seekLockUntil > 0 && !trackChanged && data.progress_ms !== undefined) {
          if (Math.abs(data.progress_ms - seekTarget) > 3000) {
            seekLockUntil = now + STALE_EXTEND_MS;
            data.progress_ms = undefined;
          }
        }

        var incomingProgress = data.progress_ms;
        var incomingCanvas   = data.canvas_url || null;

        /* Update source switcher UI from server state */
        if (data.active_source) {
          var resolved = data.source || "spotify";
          setSourceUI(resolved);
        }

        /* Merge state */
        var key;
        for (key in data) {
          if (data.hasOwnProperty(key)) {
            state[key] = data[key];
          }
        }
        dom.player.classList.remove("connecting");

        if (trackChanged) {
          trackChangeLocalTs = now;
          clockSet(state.progress_ms || 0);
          prevRenderedArt    = "";
          prevRenderedTitle  = "";
          prevRenderedArtist = "";
        } else if (incomingProgress !== undefined) {
          clockApplyDrift(incomingProgress);
        }

        applyCanvas(incomingCanvas);
      });
    }).catch(function (e) {
      clearTimeout(timeoutId);
      if (e && e.name === "AbortError") {
        console.log("[PiMusic] Poll timed out (2s)");
      } else {
        console.log("[PiMusic] Poll error: " + (e ? e.message : "unknown"));
      }
      dom.player.classList.add("connecting");
      dom.device.textContent = "Reconnecting\u2026";
    });
  }

  /* ── Playback controls ─────────────────────────────── */

  function isCooling() { return performance.now() < cooldownUntil; }
  function setCooldown(ms) { cooldownUntil = performance.now() + ms; }

  dom.btnPlay.addEventListener("click", function () {
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    if (state.is_playing) {
      state.is_playing = false;
      clockMs     = clockNow();
      clockAnchor = performance.now();
      post("/api/pause");
    } else {
      state.is_playing = true;
      clockAnchor = performance.now();
      clockRate   = 1.0;
      driftTarget = null;
      post("/api/play");
    }
  });

  dom.btnNext.addEventListener("click", function () {
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    pendingSkip = true;
    dom.trackInfo.classList.add("stale");
    state.canvas_url = null;
    applyCanvas(null);
    clockSet(0);
    state.is_playing = true;
    trackChangeLocalTs = performance.now();
    post("/api/next");
    emergencyPoll(350);
  });

  dom.btnPrev.addEventListener("click", function () {
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    pendingSkip = true;
    dom.trackInfo.classList.add("stale");
    state.canvas_url = null;
    applyCanvas(null);
    clockSet(0);
    state.is_playing = true;
    trackChangeLocalTs = performance.now();
    post("/api/previous");
    emergencyPoll(350);
  });

  /* ── Volume ────────────────────────────────────────── */

  dom.volSlider.addEventListener("pointerdown", function () {
    volDragging = true;
    volBeforeDrag = state.volume;
  });
  window.addEventListener("pointerup", function () {
    if (volDragging) {
      volDragging = false;
      volLockUntil = performance.now() + INPUT_LOCK_MS;
    }
  });

  dom.volSlider.addEventListener("input", function (e) {
    state.volume = parseInt(e.target.value, 10);
    clearTimeout(volTimer);
    volTimer = setTimeout(function () {
      post("/api/volume", { volume: state.volume });
    }, 200);
  });

  /* ── Progress bar seek (drag-to-scrub) ─────────────── */

  function seekFromEvent(e) {
    var rect = dom.bar.getBoundingClientRect();
    var pct  = Math.max(0, Math.min(1, ((e.clientX || e.pageX) - rect.left) / rect.width));
    var posMs = Math.round(pct * (state.duration_ms || 1));
    clockSet(posMs);
    seekTarget = posMs;
    dom.fill.style.transform = "scaleX(" + pct + ")";
    dom.thumb.style.left     = (pct * 100) + "%";
    dom.timeCur.textContent  = fmt(posMs);
    return posMs;
  }

  dom.bar.addEventListener("pointerdown", function (e) {
    seekDragging = true;
    seekLockUntil = Infinity;
    dom.bar.setPointerCapture(e.pointerId);
    seekFromEvent(e);
  });

  dom.bar.addEventListener("pointermove", function (e) {
    if (!seekDragging) return;
    seekFromEvent(e);
  });

  dom.bar.addEventListener("pointerup", function (e) {
    if (!seekDragging) return;
    seekDragging = false;
    var posMs = seekFromEvent(e);
    seekLockUntil = performance.now() + INPUT_LOCK_MS;
    clearTimeout(seekTimer);
    seekTimer = setTimeout(function () {
      post("/api/seek", { position_ms: posMs });
    }, 150);
  });

  dom.bar.addEventListener("pointercancel", function () {
    seekDragging = false;
    seekLockUntil = performance.now() + INPUT_LOCK_MS;
  });

  /* ── Cinematic mode events ─────────────────────────── */

  artworkWrap.addEventListener("click", function (e) {
    if (!activeCanvasSrc) return;
    e.stopPropagation();
    if (!canvasMode) enterCinematic();
  });

  dom.player.addEventListener("click", function (e) {
    if (canvasMode) e.stopPropagation();
  });

  dom.canvas.addEventListener("click", function () {
    if (canvasMode) exitCinematic();
  });
  document.body.addEventListener("click", function () {
    if (canvasMode) exitCinematic();
  });

  window.addEventListener("resize", function () {
    if (activeCanvasSrc && !canvasMode) positionCanvasOverArt();
  });

  /* ── Boot ──────────────────────────────────────────── */

  console.log("[PiMusic] Boot: starting poll + render loop");
  dom.player.classList.add("connecting");
  poll();
  setInterval(poll, POLL_MS);
  requestAnimationFrame(render);
})();
