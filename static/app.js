(function () {
  "use strict";

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
    sourceBadge: $("#source-badge"),
    sourceLabel: $("#source-label"),
    sourceIconSpotify: $("#source-icon-spotify"),
    sourceIconCider:   $("#source-icon-cider"),
    audio:     $("#audio-player")
  };

  console.error("[PiMusic] app.js v43 loaded at " + new Date().toISOString());

  var POLL_MS            = 1000;
  var DRIFT_CORRECT_MS   = 1500;
  var TEXT_UPDATE_MS     = 200;
  var SNAPBACK_GUARD_MS  = 5000;
  var BTN_COOLDOWN_MS    = 500;
  var INPUT_LOCK_MS      = 5000;
  var STALE_EXTEND_MS    = 2000;
  var RENDER_MIN_INTERVAL_MS = 33;    // ~30 fps
  var VOL_DEBOUNCE_MS    = 450;

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
    canvas_cdn_url: null,
    visual_type: "image",
    source: "spotify",
    server_time: 0,
    track_changed_at: 0,
    rate_limited_until: 0,
    cpu_throttled: false
  };

  var clockMs       = 0;
  var clockAnchor   = 0;
  var clockRate     = 1.0;
  var driftTarget   = null;
  var driftStart    = 0;
  var driftDuration = 0;

  var trackChangeLocalTs = 0;

  var prevRenderedArtKey  = "";
  var artLoadToken        = 0;
  var prevRenderedTitle   = null;
  var prevRenderedArtist  = null;
  var prevRenderedDevice  = "";
  var prevRenderedPlaying = null;
  var prevTimeCurText     = "";
  var prevTimeTotalText   = "";
  var prevPct             = -1;
  var prevSource          = "";

  var activeCanvasSrc = "";
  var activeVisualType = "image";
  var canvasProxyUrl = "";
  var canvasDirectUrl = "";
  var canvasFallbackTried = false;
  var canvasWatchdogTimer = null;
  var CANVAS_WATCHDOG_MS = 6000;
  var canvasMode = false;
  var visualMode = "canvas_card";
  var artworkWrap = $("#artwork-wrap");
  var idleScreensaverActive = false;
  var idleScreensaverDismissed = false;
  var idleInactivityTimer = null;
  var IDLE_RETURN_MS = 15000;

  var barWidth = dom.bar.offsetWidth;
  if (window.ResizeObserver) {
    new ResizeObserver(function (entries) {
      barWidth = entries[0].contentRect.width;
    }).observe(dom.bar);
  } else {
    window.addEventListener("resize", function () { barWidth = dom.bar.offsetWidth; });
  }

  var volDragging    = false;
  var volTimer       = null;
  var volLockUntil   = 0;
  var volBeforeDrag  = 50;
  var seekDragging   = false;
  var seekTimer      = null;
  var seekLockUntil  = 0;
  var seekTarget     = 0;
  var cooldownUntil      = 0;
  var lastTextUpdate     = 0;
  var lastRenderTs       = 0;
  var pendingSkip        = false;
  var pollReqId          = 0;
  var playbackLockUntil  = 0;
  var pendingServerResync = false;

  /* ── Predictive clock ─────────────────────────────────── */

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

  var trackEndPolled = false;

  function clockTick() {
    if (driftTarget !== null) {
      if (performance.now() - driftStart >= driftDuration) {
        clockMs     = clockNow();
        clockAnchor = performance.now();
        clockRate   = 1.0;
        driftTarget = null;
      }
    }
    if (state.is_playing && state.duration_ms > 0 && !trackEndPolled
        && clockNow() >= state.duration_ms - 500) {
      trackEndPolled = true;
      emergencyPoll(0);
    }
  }

  /* ── Helpers ──────────────────────────────────────────── */

  function fmt(ms) {
    var total = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function realPost(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    }).catch(function () { return { ok: false }; });
  }

  // ── Client-side demo state ────────────────────────────────────
  // When the page is served from Vercel, window.PIMUSIC_DEMO === true
  // and window.PIMUSIC_PLAYLIST holds the resolved playlist. We keep
  // all timing/skipping state in the browser so multi-instance
  // serverless can't make it diverge between polls.

  var demoEnabled = !!window.PIMUSIC_DEMO && !!window.PIMUSIC_PLAYLIST
                    && window.PIMUSIC_PLAYLIST.length > 0;
  var demoPlaylist = demoEnabled ? window.PIMUSIC_PLAYLIST : null;
  var demoFirstCanvasIdx = -1;
  if (demoEnabled) {
    for (var di = 0; di < demoPlaylist.length; di++) {
      if (demoPlaylist[di].canvas_url) { demoFirstCanvasIdx = di; break; }
    }
  }
  var IDLE_AFTER_PAUSE_S = 15;
  var demo = demoEnabled ? {
    index: 0,
    started_at: Date.now(),     // wallclock ms when current track started
    is_playing: true,
    paused_at: 0,
    pause_progress_ms: 0,
    volume: 65,
    source: "spotify",
    visual_mode: "canvas_card"
  } : null;

  function demoCurrent() { return demoPlaylist[demo.index % demoPlaylist.length]; }

  function demoMaybeAdvance() {
    if (!demo.is_playing) return;
    while (true) {
      var t = demoCurrent();
      var elapsed = Date.now() - demo.started_at;
      if (elapsed < t.duration_ms) return;
      demo.started_at += t.duration_ms;
      demo.index = (demo.index + 1) % demoPlaylist.length;
    }
  }

  function demoProgressMs() {
    if (!demo.is_playing) return demo.pause_progress_ms;
    return Math.max(0, Date.now() - demo.started_at);
  }

  function demoIsIdle() {
    return !demo.is_playing
        && demo.paused_at > 0
        && (Date.now() - demo.paused_at) > IDLE_AFTER_PAUSE_S * 1000;
  }

  function synthState() {
    demoMaybeAdvance();
    var t = demoCurrent();
    var idleT = demoFirstCanvasIdx >= 0 ? demoPlaylist[demoFirstCanvasIdx] : t;
    var idle = demoIsIdle();
    var hasCanvas = !!t.canvas_url;
    return {
      track_id:           idle ? "" : t.track_id,
      track:              idle ? "" : t.track,
      artist:             idle ? "" : t.artist,
      album:              idle ? "" : t.album,
      album_art_url:      idle ? "" : t.album_art_url,
      album_art_local:    idle ? "" : t.album_art_local,
      canvas_url:         idle ? "" : t.canvas_url,
      canvas_cdn_url:     idle ? "" : t.canvas_cdn_url,
      audio_url:          idle ? "" : t.audio_url,
      duration_ms:        idle ? 0  : t.duration_ms,
      progress_ms:        idle ? 0  : demoProgressMs(),
      is_playing:         demo.is_playing,
      volume:             demo.volume,
      device:             "PiMusic Demo",
      source:             demo.source,
      server_time:        Date.now() / 1000,
      track_changed_at:   demo.started_at / 1000,
      visual_mode:        demo.visual_mode,
      visual_type:        (hasCanvas && !idle) ? "canvas_video" : "image",
      cpu_throttled:      false,
      rate_limited_until: 0,
      dominant_color:     t.dominant_color,
      idle_canvas_track_id: idleT.track_id,
      idle_canvas_url:    idleT.canvas_url || "",
      idle_canvas_cdn_url: idleT.canvas_cdn_url || ""
    };
  }

  function demoApply(url, body) {
    if (!demo) return;
    body = body || {};
    if (url === "/api/play" || (url === "/api/hid/input" && body.action === "play")) {
      if (!demo.is_playing) {
        demo.started_at = Date.now() - demo.pause_progress_ms;
        demo.is_playing = true;
        demo.paused_at = 0;
      }
    } else if (url === "/api/pause" || (url === "/api/hid/input" && body.action === "pause")) {
      if (demo.is_playing) {
        demo.pause_progress_ms = demoProgressMs();
        demo.is_playing = false;
        demo.paused_at = Date.now();
      }
    } else if (url === "/api/next") {
      demo.index = (demo.index + 1) % demoPlaylist.length;
      demo.started_at = Date.now();
      demo.pause_progress_ms = 0;
      if (!demo.is_playing) demo.paused_at = Date.now();
    } else if (url === "/api/previous") {
      // Match real player semantics: >3s in restarts the track,
      // otherwise jump to the previous one.
      if (demoProgressMs() > 3000) {
        demo.started_at = Date.now();
      } else {
        demo.index = (demo.index - 1 + demoPlaylist.length) % demoPlaylist.length;
        demo.started_at = Date.now();
      }
      demo.pause_progress_ms = 0;
      if (!demo.is_playing) demo.paused_at = Date.now();
    } else if (url === "/api/seek") {
      var t = demoCurrent();
      var pos = Math.max(0, Math.min(parseInt(body.position_ms, 10) || 0, t.duration_ms - 1));
      demo.started_at = Date.now() - pos;
      if (!demo.is_playing) {
        demo.pause_progress_ms = pos;
        demo.paused_at = Date.now();
      }
    } else if (url === "/api/volume") {
      demo.volume = Math.max(0, Math.min(100, parseInt(body.volume, 10) || 0));
    } else if (url === "/api/source") {
      if (body.source === "spotify" || body.source === "cider") demo.source = body.source;
    }
  }

  function post(url, body) {
    if (demoEnabled) {
      demoApply(url, body);
      return Promise.resolve({ ok: true });
    }
    return realPost(url, body);
  }

  var emergencyPollTimer = null;
  function emergencyPoll(delayMs) {
    clearTimeout(emergencyPollTimer);
    emergencyPollTimer = setTimeout(function () {
      post("/api/force-poll").then(function () { poll(); });
    }, delayMs);
  }

  /* ── Canvas video ──────────────────────────────────────── */

  function clearCanvas() {
    dom.canvas.classList.remove("active");
    dom.canvas.removeAttribute("src");
    dom.canvas.load();
    document.body.classList.remove("has-canvas");
  }

  function cancelCanvasWatchdog() {
    if (canvasWatchdogTimer) {
      clearTimeout(canvasWatchdogTimer);
      canvasWatchdogTimer = null;
    }
  }

  function giveUpCanvas() {
    cancelCanvasWatchdog();
    canvasProxyUrl = "";
    canvasDirectUrl = "";
    canvasFallbackTried = false;
    activeCanvasSrc = "";
    activeVisualType = "image";
    clearCanvas();
  }

  function tryCanvasFallback(reason) {
    if (!canvasFallbackTried && canvasDirectUrl) {
      canvasFallbackTried = true;
      console.error("[PiMusic] CANVAS: falling back to direct CDN (" + reason + "): " + canvasDirectUrl);
      loadCanvasSrc(canvasDirectUrl);
    } else {
      console.error("[PiMusic] CANVAS: giving up (" + reason + ")");
      giveUpCanvas();
    }
  }

  function loadCanvasSrc(src) {
    cancelCanvasWatchdog();
    activeCanvasSrc = src;

    dom.canvas.oncanplay = function () {
      console.error("[PiMusic] CANVAS: oncanplay -> play()");
      cancelCanvasWatchdog();
      dom.canvas.classList.add("active");
      document.body.classList.add("has-canvas");
      dom.canvas.play().catch(function () {});
    };

    dom.canvas.onerror = function () {
      tryCanvasFallback("onerror");
    };

    canvasWatchdogTimer = setTimeout(function () {
      canvasWatchdogTimer = null;
      if (!dom.canvas.classList.contains("active")) {
        tryCanvasFallback("watchdog");
      }
    }, CANVAS_WATCHDOG_MS);

    dom.canvas.src = src;
    dom.canvas.load();
  }

  function applyCanvas(proxyUrl, cdnUrl, visualType) {
    syncBackgroundMode();

    if (!proxyUrl || visualType === "image") {
      if (canvasProxyUrl || activeCanvasSrc) {
        console.error("[PiMusic] CANVAS: clearing");
        giveUpCanvas();
      }
      return;
    }

    if (proxyUrl === canvasProxyUrl && visualType === activeVisualType) return;

    console.error("[PiMusic] CANVAS: setting proxy=" + proxyUrl + " cdn=" + (cdnUrl ? "yes" : "no"));
    canvasProxyUrl = proxyUrl;
    canvasDirectUrl = cdnUrl || "";
    canvasFallbackTried = false;
    activeVisualType = visualType;

    dom.canvas.classList.remove("active");
    loadCanvasSrc(proxyUrl);
  }

  /* ── Audio sync (demo mode) ────────────────────────────────
     Drives the hidden <audio> element off /api/state. The server
     auto-advances tracks on time, so the audio just needs to follow:
       - swap src when audio_url changes
       - resync currentTime on next/prev/seek (track_changed_at moves)
       - mirror is_playing (autoplay is browser-blocked until first click)
       - mirror volume                                                    */

  var lastAudioSrc = "";
  var lastAudioPlaying = null;
  var lastTrackChangedAt = null;

  function syncAudio(data) {
    if (!dom.audio) return;
    var desired = data.audio_url || "";

    if (desired !== lastAudioSrc) {
      if (desired) {
        dom.audio.src = desired;
        dom.audio.load();
      } else {
        dom.audio.removeAttribute("src");
        dom.audio.load();
      }
      lastAudioSrc = desired;
      lastAudioPlaying = null;
      lastTrackChangedAt = null;  // force seek-sync below
    }

    if (desired && data.track_changed_at !== lastTrackChangedAt) {
      // data.progress_ms gets nulled by the seek lock right after a
      // scrub — falling back to 0 here was sending audio back to the
      // start every time you let go of the seek bar. Use the local
      // clock instead, which already reflects the user's scrub target.
      var targetMs = (data.progress_ms !== undefined && data.progress_ms !== null)
        ? data.progress_ms
        : clockNow();
      try { dom.audio.currentTime = targetMs / 1000; } catch (e) {}
      lastTrackChangedAt = data.track_changed_at;
    }

    if (typeof data.volume === "number") {
      dom.audio.volume = Math.max(0, Math.min(1, data.volume / 100));
    }

    // Soft drift correction: re-anchor the visual clock to the audio
    // element when they've drifted >250 ms apart. The audio is the
    // canonical playback (it's what's actually playing), so it should
    // win — but skip while the user is mid-scrub or the seek lock is
    // active so we don't fight the user's intent.
    if (desired && data.is_playing && !seekDragging
        && performance.now() >= seekLockUntil
        && !isNaN(dom.audio.currentTime) && dom.audio.currentTime > 0) {
      var audioMs = dom.audio.currentTime * 1000;
      if (Math.abs(audioMs - clockNow()) > 250) {
        clockMs = audioMs;
        clockAnchor = performance.now();
        clockRate = 1.0;
        driftTarget = null;
      }
    }

    if (desired && lastAudioPlaying !== data.is_playing) {
      if (data.is_playing) {
        dom.audio.play().then(function () {
          lastAudioPlaying = true;
        }).catch(function () {
          /* Autoplay blocked until first user gesture. Leave
             lastAudioPlaying unchanged so the next poll retries — once
             the user clicks play (or anything that triggers a poll
             after a gesture), it'll go through. */
        });
      } else {
        dom.audio.pause();
        lastAudioPlaying = false;
      }
    }
  }

  // Audio file is often a few seconds shorter than Spotify's reported
  // duration (album version vs deluxe, etc.). Without this, the track
  // sits in silence until the wallclock catches up. End of audio ->
  // skip to next so the demo flows naturally. Reuses prepareSkip for
  // the same in-flight-poll race protection as a manual skip.
  if (dom.audio) {
    dom.audio.addEventListener("ended", function () {
      if (state.is_playing && state.track_id) {
        prepareSkip();
        post("/api/next");
        emergencyPoll(200);
      }
    });
  }

  /* ── Source badge ──────────────────────────────────────── */

  function updateSourceBadge(source) {
    if (source === prevSource) return;
    prevSource = source;

    if (dom.sourceIconSpotify && dom.sourceIconCider && dom.sourceLabel && dom.sourceBadge) {
      if (source === "cider") {
        dom.sourceIconSpotify.classList.add("hidden");
        dom.sourceIconCider.classList.remove("hidden");
        dom.sourceLabel.textContent = "Apple Music";
        dom.sourceBadge.classList.remove("source-spotify");
        dom.sourceBadge.classList.add("source-cider");
      } else {
        dom.sourceIconCider.classList.add("hidden");
        dom.sourceIconSpotify.classList.remove("hidden");
        dom.sourceLabel.textContent = "Spotify";
        dom.sourceBadge.classList.remove("source-cider");
        dom.sourceBadge.classList.add("source-spotify");
      }
    }
  }

  /* ── Source dropdown ──────────────────────────────────── */

  var sourceDropdown = document.getElementById("source-dropdown");
  var sourceDropdownWrap = dom.sourceBadge ? dom.sourceBadge.parentElement : null;

  if (dom.trackInfo) {
    dom.trackInfo.addEventListener("click", function (e) {
      e.stopPropagation();
    });
  }

  if (dom.sourceBadge && sourceDropdown && sourceDropdownWrap) {
    dom.sourceBadge.addEventListener("click", function (e) {
      e.stopPropagation();
      var isOpen = sourceDropdownWrap.classList.contains("open");
      if (isOpen) {
        sourceDropdown.classList.remove("visible");
        sourceDropdownWrap.classList.remove("open");
      } else {
        sourceDropdown.classList.remove("hidden");
        sourceDropdown.classList.add("visible");
        sourceDropdownWrap.classList.add("open");
      }
    });

    var options = sourceDropdown.querySelectorAll(".source-option");
    for (var i = 0; i < options.length; i++) {
      options[i].addEventListener("click", function (e) {
        e.stopPropagation();
        var src = this.getAttribute("data-source");
        post("/api/source", { source: src });
        sourceDropdown.classList.remove("visible");
        sourceDropdownWrap.classList.remove("open");
      });
    }

    document.addEventListener("click", function () {
      sourceDropdown.classList.remove("visible");
      sourceDropdownWrap.classList.remove("open");
    });
  }

  /* ── Cinematic mode toggle ─────────────────────────────── */

  function enterCinematic() {
    canvasMode = true;
    document.body.classList.remove("canvas-background");
    document.body.insertBefore(dom.canvas, document.body.firstChild);
    document.body.classList.add("canvas-cinematic");
  }

  function exitCinematic() {
    canvasMode = false;
    document.body.classList.remove("canvas-cinematic");
    artworkWrap.appendChild(dom.canvas);
  }

  /* Tap/click during the idle screensaver: dismiss it, kill the canvas,
     and fall back to the normal "No music playing" idle chrome.
     Stays dismissed until either a track plays, the inactivity timer
     fires (15s), or the user taps the artwork to bring it back. */
  function dismissIdleScreensaver() {
    if (!idleScreensaverActive) return;
    idleScreensaverDismissed = true;
    idleScreensaverActive = false;
    document.body.classList.remove("idle-screensaver");
    clearCanvas();
    armIdleInactivity();
  }

  /* Bring the idle screensaver back (tap-on-artwork or 15s inactivity). */
  function reactivateIdleScreensaver() {
    if (state.track_id) return;              // track is playing, skip
    if (!state.idle_canvas_url) return;      // nothing cached yet, nothing to show
    idleScreensaverDismissed = false;
    idleScreensaverActive = true;
    disarmIdleInactivity();
    document.body.classList.add("idle-screensaver");
    if (!canvasMode && visualMode !== "canvas_bg") enterCinematic();
    applyCanvas(state.idle_canvas_url, state.idle_canvas_cdn_url || null, "canvas_video");
  }

  function armIdleInactivity() {
    disarmIdleInactivity();
    idleInactivityTimer = setTimeout(function () {
      idleInactivityTimer = null;
      /* Only re-engage if we're still idle-and-dismissed; a track starting
         or the server losing the idle canvas should cancel the return. */
      if (idleScreensaverDismissed && !state.track_id) {
        reactivateIdleScreensaver();
      }
    }, IDLE_RETURN_MS);
  }

  function disarmIdleInactivity() {
    if (idleInactivityTimer) {
      clearTimeout(idleInactivityTimer);
      idleInactivityTimer = null;
    }
  }

  function syncBackgroundMode() {
    if (canvasMode) return;
    if (visualMode === "canvas_bg") {
      document.body.classList.add("canvas-background");
    } else {
      document.body.classList.remove("canvas-background");
    }
  }

  /* ── Render (60fps via rAF, scaleX progress) ──────────── */

  function render(timestamp) {
    requestAnimationFrame(render);
    if (timestamp - lastRenderTs < RENDER_MIN_INTERVAL_MS) return;
    lastRenderTs = timestamp;
    clockTick();

    var p   = clockNow();
    var d   = state.duration_ms || 1;
    var pct = Math.min(1, Math.max(0, p / d));

    // The seek lock is for ignoring stale server progress, not for
    // freezing the visual — keep drawing the local clock so the bar
    // doesn't sit frozen for the whole 5s after letting go of a scrub.
    if (!seekDragging) {
      var pctRound = Math.round(pct * 10000);
      if (pctRound !== prevPct) {
        prevPct = pctRound;
        dom.fill.style.transform = "scaleX(" + pct + ")";
        dom.thumb.style.setProperty("--thumb-x", (pct * barWidth) + "px");
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
      var deviceText = "";
      if (state.rate_limited_until > 0) {
        var secLeft = Math.max(0, Math.ceil(state.rate_limited_until - Date.now() / 1000));
        var minLeft = Math.ceil(secLeft / 60);
        deviceText = "Rate limited \u2013 back in " + (minLeft >= 60 ? Math.ceil(minLeft / 60) + "h" : minLeft + " min");
      } else {
        deviceText = state.device || "No device";
      }
      if (state.cpu_throttled) {
        deviceText += " \u00b7 CPU throttled";
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

    var primarySrc = state.album_art_local || state.album_art_url || "";
    var artKey = (state.track_id || "") + "|" + primarySrc;
    if (primarySrc && artKey !== prevRenderedArtKey) {
      prevRenderedArtKey = artKey;
      loadAlbumArt(primarySrc, state.album_art_url || "");
    }
  }

  function loadAlbumArt(primarySrc, directUrl) {
    var myToken = ++artLoadToken;
    var sources = [primarySrc];
    if (directUrl && directUrl !== primarySrc) sources.push(directUrl);

    var tryLoad = function (idx) {
      if (myToken !== artLoadToken) return;
      if (idx >= sources.length) {
        console.warn("[PiMusic] ART: all sources failed, keeping previous");
        if (myToken === artLoadToken) prevRenderedArtKey = "";
        return;
      }
      var src = sources[idx];
      var img = new Image();
      img.onload = function () {
        if (myToken !== artLoadToken) return;
        dom.art.classList.remove("fresh");
        void dom.art.offsetWidth;
        dom.art.src = src;
        dom.art.classList.add("fresh");
        dom.bg.style.backgroundImage = 'url("' + src + '")';
        if (idx > 0) {
          console.warn("[PiMusic] ART: recovered via fallback source " + idx);
        }
      };
      img.onerror = function () {
        if (myToken !== artLoadToken) return;
        console.warn("[PiMusic] ART: failed attempt " + (idx + 1) + ": " + src);
        tryLoad(idx + 1);
      };
      img.src = src;
    };
    tryLoad(0);
  }

  /* ── API polling (1s) ─────────────────────────────────── */

  function poll() {
    pollReqId += 1;
    var myReqId = pollReqId;
    var stateP = demoEnabled
      ? Promise.resolve(synthState())
      : fetch("/api/state").then(function (res) { return res.ok ? res.json() : null; });
    return stateP.then(function (data) {
      if (!data) return;
      return Promise.resolve(data).then(function (data) {
        if (myReqId !== pollReqId) return;
        var trackChanged = data.track_id && data.track_id !== state.track_id;

        if (pendingSkip && trackChanged) {
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

        /* Playback lock */
        if (now < playbackLockUntil && !trackChanged) {
          data.is_playing = state.is_playing;
          data.progress_ms = undefined;
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

        var incomingCanvas   = data.canvas_url || null;
        var incomingCdn      = data.canvas_cdn_url || null;
        var incomingVisual   = (data.visual_type === "canvas_video") ? "canvas_video" : "image";
        var incomingSource   = data.source || "spotify";
        state.cpu_throttled  = !!data.cpu_throttled;
        if (data.visual_mode) visualMode = data.visual_mode;
        if (canvasMode && visualMode === "canvas_bg") {
          exitCinematic();
        }

        /* Idle screensaver: no active track + we have an idle canvas cached.
           If a track starts, clear any "dismissed" flag + cancel the
           15s return timer so the screensaver can come back cleanly
           the next idle period. */
        var isIdle = !data.track_id;
        if (!isIdle) {
          idleScreensaverDismissed = false;
          disarmIdleInactivity();
        }
        var wasIdle = idleScreensaverActive;
        idleScreensaverActive = isIdle && !!data.idle_canvas_url && !idleScreensaverDismissed;

        if (idleScreensaverActive) {
          incomingCanvas = data.idle_canvas_url;
          incomingCdn    = data.idle_canvas_cdn_url || null;
          incomingVisual = "canvas_video";
        }

        if (idleScreensaverActive && !wasIdle) {
          document.body.classList.add("idle-screensaver");
          disarmIdleInactivity();
          if (!canvasMode && visualMode !== "canvas_bg") enterCinematic();
        } else if (!idleScreensaverActive && wasIdle) {
          document.body.classList.remove("idle-screensaver");
        }

        var playingTransition = data.is_playing !== undefined
                             && data.is_playing !== state.is_playing;

        Object.assign(state, data);
        dom.player.classList.remove("connecting");

        updateSourceBadge(incomingSource);

        if (data.rate_limited_until > 0) {
          var secLeft = Math.ceil(data.rate_limited_until - Date.now() / 1000);
          var minLeft = Math.ceil(secLeft / 60);
          dom.device.textContent = "Rate limited \u2013 back in " + (minLeft > 60 ? Math.ceil(minLeft / 60) + "h" : minLeft + " min");
        } else if (state.device) {
          dom.device.textContent = state.device || "No device";
        }

        if (trackChanged) {
          trackChangeLocalTs = now;
          clockSet(state.progress_ms || 0);
          trackEndPolled     = false;
          pendingServerResync = false;
          prevRenderedTitle  = "";
          prevRenderedArtist = "";
        } else if (data.progress_ms !== undefined
                   && (playingTransition || pendingServerResync)) {
          clockSet(data.progress_ms);
          pendingServerResync = false;
        }

        applyCanvas(incomingCanvas, incomingCdn, incomingVisual);
        syncAudio(data);
      });
    }).catch(function (e) {
      console.error("[PiMusic] Poll error:", e);
      dom.player.classList.add("connecting");
      dom.device.textContent = "Reconnecting\u2026";
    });
  }

  /* ── Playback controls ────────────────────────────────── */

  function isCooling() { return performance.now() < cooldownUntil; }
  function setCooldown(ms) { cooldownUntil = performance.now() + ms; }

  dom.btnPlay.addEventListener("click", function (e) {
    e.stopPropagation();
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    playbackLockUntil = performance.now() + 1500;
    if (state.is_playing) {
      state.is_playing = false;
      clockMs     = clockNow();
      clockAnchor = performance.now();
      if (dom.audio) dom.audio.pause();
      post("/api/pause");
    } else {
      state.is_playing = true;
      clockAnchor = performance.now();
      clockRate   = 1.0;
      driftTarget = null;
      // Kick off audio inside the user gesture so browsers don't block it.
      if (dom.audio && dom.audio.src) dom.audio.play().catch(function () {});
      post("/api/play");
    }
    pendingServerResync = true;
    emergencyPoll(400);
  });

  // Cleanup that runs on every skip — pause the old audio so it
  // doesn't keep playing for ~400ms until the new src loads, and bump
  // pollReqId so any in-flight regular poll (which carries OLD track
  // data) gets orphaned when its response arrives.
  function prepareSkip() {
    pollReqId += 1;
    if (dom.audio) {
      dom.audio.pause();
      dom.audio.removeAttribute("src");
      dom.audio.load();
    }
    lastAudioSrc = "";
    lastAudioPlaying = null;
    lastTrackChangedAt = null;
    pendingSkip = true;
    dom.trackInfo.classList.add("stale");
    state.canvas_url = null;
    state.canvas_cdn_url = null;
    state.visual_type = "image";
    applyCanvas(null, null, "image");
    clockSet(0);
    // Don't force is_playing=true here — backend next_track preserves
    // play/pause state, so forcing it caused the play icon to flicker
    // (flip to playing on click, then back to paused once the poll
    // returned the real state).
    trackChangeLocalTs = performance.now();
  }

  dom.btnNext.addEventListener("click", function (e) {
    e.stopPropagation();
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    prepareSkip();
    post("/api/next");
    emergencyPoll(350);
  });

  dom.btnPrev.addEventListener("click", function (e) {
    e.stopPropagation();
    if (isCooling()) return;
    setCooldown(BTN_COOLDOWN_MS);
    prepareSkip();
    post("/api/previous");
    emergencyPoll(350);
  });

  /* Volume */
  dom.volSlider.addEventListener("pointerdown", function (e) {
    e.stopPropagation();
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
    e.stopPropagation();
    state.volume = parseInt(e.target.value, 10);
    clearTimeout(volTimer);
    volTimer = setTimeout(function () {
      post("/api/volume", { volume: state.volume });
    }, VOL_DEBOUNCE_MS);
  });

  /* Progress bar seek (drag-to-scrub) */
  function seekFromEvent(e) {
    var rect = dom.bar.getBoundingClientRect();
    var pct  = Math.max(0, Math.min(1, ((e.clientX || e.pageX) - rect.left) / rect.width));
    var posMs = Math.round(pct * (state.duration_ms || 1));
    clockSet(posMs);
    seekTarget = posMs;
    dom.fill.style.transform = "scaleX(" + pct + ")";
    dom.thumb.style.setProperty("--thumb-x", (pct * rect.width) + "px");
    dom.timeCur.textContent  = fmt(posMs);
    return posMs;
  }

  dom.bar.addEventListener("pointerdown", function (e) {
    e.stopPropagation();
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
    // Move audio immediately so it doesn't lag behind the visual
    // until the server round-trip completes (~500ms).
    if (dom.audio && dom.audio.src) {
      try { dom.audio.currentTime = posMs / 1000; } catch (err) {}
    }
    clearTimeout(seekTimer);
    seekTimer = setTimeout(function () {
      post("/api/seek", { position_ms: posMs });
    }, 150);
    pendingServerResync = true;
    emergencyPoll(500);
  });

  dom.bar.addEventListener("pointercancel", function () {
    seekDragging = false;
    seekLockUntil = performance.now() + INPUT_LOCK_MS;
  });

  /* Click artwork:
       - If the idle screensaver is dismissed and we're still idle, bring it back.
       - Otherwise, toggle cinematic mode (skipped in canvas_bg — already fullscreen). */
  artworkWrap.addEventListener("click", function (e) {
    e.stopPropagation();
    if (idleScreensaverDismissed && !state.track_id && state.idle_canvas_url) {
      reactivateIdleScreensaver();
      return;
    }
    if (!canvasMode && visualMode !== "canvas_bg") enterCinematic();
  });

  /* While the screensaver is dismissed-and-idle, any tap or keypress
     resets the 15s inactivity countdown. Capture phase so it runs even
     when inner handlers call stopPropagation(). */
  function onIdleActivity() {
    if (idleScreensaverDismissed && !state.track_id) {
      armIdleInactivity();
    }
  }
  document.addEventListener("pointerdown", onIdleActivity, true);
  document.addEventListener("keydown", onIdleActivity, true);

  /* In cinematic: clicking player background exits, clicking controls doesn't */
  dom.player.addEventListener("click", function (e) {
    if (!canvasMode) return;
    if (e.target === dom.player) {
      dismissIdleScreensaver();
      exitCinematic();
    } else {
      e.stopPropagation();
    }
  });

  /* Click the fullscreen canvas video to exit */
  dom.canvas.addEventListener("click", function (e) {
    if (canvasMode) {
      e.stopPropagation();
      dismissIdleScreensaver();
      exitCinematic();
    }
  });

  /* Click the bg-layer or bg-overlay to exit (artwork-only fullscreen) */
  dom.bg.addEventListener("click", function (e) {
    if (canvasMode) {
      e.stopPropagation();
      dismissIdleScreensaver();
      exitCinematic();
    }
  });
  var bgOverlay = $("#bg-overlay");
  if (bgOverlay) {
    bgOverlay.addEventListener("click", function (e) {
      if (canvasMode) {
        e.stopPropagation();
        dismissIdleScreensaver();
        exitCinematic();
      }
    });
  }

  /* Fallback: click body to exit */
  document.body.addEventListener("click", function () {
    if (canvasMode) {
      dismissIdleScreensaver();
      exitCinematic();
    }
  });

  /* ── Rotary encoder (global keyboard) ──────────────────── */

  var MULTI_PRESS_MS = 350;
  var encoderPressCount = 0;
  var encoderPressTimer = null;

  function encoderButtonPressed() {
    encoderPressCount++;
    clearTimeout(encoderPressTimer);
    encoderPressTimer = setTimeout(function () {
      var n = encoderPressCount;
      encoderPressCount = 0;
      if (n >= 4)       window.location.href = "/settings";
      else if (n === 3) dom.btnPrev.click();
      else if (n === 2) dom.btnNext.click();
      else              dom.btnPlay.click();
    }, MULTI_PRESS_MS);
  }

  window.addEventListener("keydown", function (e) {
    if (e.repeat) return;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      state.volume = Math.max(0, Math.min(100,
        state.volume + (e.key === "ArrowUp" ? 2 : -2)));
      dom.volSlider.value = state.volume;
      volLockUntil = performance.now() + INPUT_LOCK_MS;
      dom.volSlider.dispatchEvent(new Event("input"));
    } else if (e.key === " ") {
      e.preventDefault();
      encoderButtonPressed();
    }
  });

  /* ── Boot ─────────────────────────────────────────────── */

  dom.art.addEventListener("error", function () {
    console.warn("[PiMusic] ART: <img> decode failed, forcing reload");
    prevRenderedArtKey = "";
  });

  console.error("[PiMusic] Boot: starting poll + render loop");
  dom.player.classList.add("connecting");
  poll();
  setInterval(poll, POLL_MS);
  requestAnimationFrame(render);
})();
