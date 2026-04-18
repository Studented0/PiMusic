(function () {
  "use strict";

  console.log("[PiMusic] settings.js v2 loaded");

  var $  = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  /* ── Visual-mode tile group shim (acts like <select>) ── */
  var visualModeGroup = (function () {
    var root = $("#visual_mode");
    var tiles = $$("#visual_mode .vm-tile");
    var current = "canvas_card";

    function setValue(v) {
      if (v !== "canvas_card" && v !== "canvas_bg" && v !== "artwork") v = "canvas_card";
      current = v;
      tiles.forEach(function (t) {
        var active = t.getAttribute("data-value") === v;
        t.classList.toggle("vm-tile--active", active);
        t.setAttribute("aria-checked", active ? "true" : "false");
      });
    }

    tiles.forEach(function (t) {
      t.addEventListener("click", function () { setValue(t.getAttribute("data-value")); });
    });

    return {
      el: root,
      tiles: tiles,
      get value() { return current; },
      set value(v) { setValue(v); }
    };
  })();

  var fields = {
    spotify_sp_dc:        $("#spotify_sp_dc"),
    spotify_client_id:    $("#spotify_client_id"),
    spotify_client_secret:$("#spotify_client_secret"),
    spotify_redirect_uri: $("#spotify_redirect_uri"),
    cider_token:          $("#cider_token"),
    cider_host:           $("#cider_host"),
    cider_storefront:     $("#cider_storefront"),
    cpu_threshold:        $("#cpu_threshold"),
    scanline_overlay:     $("#scanline_overlay"),
    cinematic_auto:       $("#cinematic_auto"),
    visual_mode:          visualModeGroup
  };

  var statusBar = $("#status-bar");

  function showStatus(msg, isError) {
    statusBar.textContent = msg;
    statusBar.className = "status-bar" + (isError ? " status-error" : " status-ok");
    statusBar.classList.remove("hidden");
    setTimeout(function () { statusBar.classList.add("hidden"); }, 4000);
  }

  function loadSettings() {
    fetch("/api/settings")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (s) {
        if (!s || typeof s !== "object") throw new Error("Bad JSON");
        fields.spotify_sp_dc.value        = s.spotify_sp_dc || "";
        fields.spotify_client_id.value    = s.spotify_client_id || "";
        fields.spotify_client_secret.value= s.spotify_client_secret || "";
        fields.spotify_redirect_uri.value = s.spotify_redirect_uri || "";
        fields.cider_token.value          = s.cider_token || "";
        fields.cider_host.value           = s.cider_host || "http://127.0.0.1:10767";
        fields.cider_storefront.value     = s.cider_storefront || "us";
        fields.cpu_threshold.value        = s.cpu_threshold || 75;
        $("#cpu_threshold_val").textContent = fields.cpu_threshold.value;
        fields.scanline_overlay.checked   = !!s.scanline_overlay;
        fields.cinematic_auto.checked     = !!s.cinematic_auto;
        var vm = s.visual_mode;
        if (vm !== "canvas_card" && vm !== "canvas_bg" && vm !== "artwork") vm = "canvas_card";
        fields.visual_mode.value = vm;
      })
      .catch(function (e) {
        console.error("[Settings] Load error:", e);
        showStatus("Failed to load settings", true);
      });
  }

  function saveSettings() {
    var payload = {
      spotify_sp_dc:        fields.spotify_sp_dc.value,
      spotify_client_id:    fields.spotify_client_id.value,
      spotify_client_secret:fields.spotify_client_secret.value,
      spotify_redirect_uri: fields.spotify_redirect_uri.value,
      cider_token:          fields.cider_token.value,
      cider_host:           fields.cider_host.value,
      cider_storefront:     fields.cider_storefront.value,
      cpu_threshold:        parseInt(fields.cpu_threshold.value, 10),
      scanline_overlay:     fields.scanline_overlay.checked,
      cinematic_auto:       fields.cinematic_auto.checked,
      visual_mode:          fields.visual_mode.value
    };

    fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ok) showStatus("Settings saved");
      else showStatus("Save failed", true);
    })
    .catch(function () { showStatus("Save failed", true); });
  }

  function pollCpu() {
    fetch("/api/system/cpu")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        $("#cpu-usage").textContent = d.cpu_percent.toFixed(1) + "%";
        $("#video-disabled").textContent = d.video_disabled ? "Yes (throttled)" : "No";
      })
      .catch(function () {});
  }

  function pollSource() {
    fetch("/api/source")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var el = $("#active-source");
        el.textContent = d.source === "cider" ? "Apple Music (Cider)" : "Spotify";
        el.className = "source-indicator source-indicator--" + d.source;
      })
      .catch(function () {});
  }

  /* Slider live value */
  fields.cpu_threshold.addEventListener("input", function () {
    $("#cpu_threshold_val").textContent = this.value;
  });

  /* Save */
  $("#btn-save").addEventListener("click", saveSettings);

  /* Force re-auth */
  $("#btn-force-reauth").addEventListener("click", function () {
    fetch("/api/force-reauth", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) { showStatus(d.message || "Re-auth started"); })
      .catch(function () { showStatus("Re-auth failed", true); });
  });

  /* Clear album art cache on disk */
  var btnClearCache = $("#btn-clear-cache");
  if (btnClearCache) {
    btnClearCache.addEventListener("click", function () {
      fetch("/api/clear-cache", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) {
            showStatus("Art cache cleared (" + (d.removed || 0) + " file(s))");
          } else {
            showStatus(d.error || "Clear failed", true);
          }
        })
        .catch(function () { showStatus("Clear failed", true); });
    });
  }

  /* Test Spotify */
  $("#btn-test-spotify").addEventListener("click", function () {
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.track || d.device) {
          showStatus("Spotify connected: " + (d.device || "OK"));
        } else {
          showStatus("Spotify: no active playback", true);
        }
      })
      .catch(function () { showStatus("Spotify connection failed", true); });
  });

  /* Test Cider */
  $("#btn-test-cider").addEventListener("click", function () {
    var host = fields.cider_host.value || "http://127.0.0.1:10767";
    showStatus("Testing Cider at " + host + "...");
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(function () { showStatus("Cider reachable"); })
      .catch(function () { showStatus("Cider not reachable", true); });
  });

  /* Source switch */
  $("#btn-source-spotify").addEventListener("click", function () {
    fetch("/api/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "spotify" })
    }).then(function () { pollSource(); showStatus("Switched to Spotify"); });
  });
  $("#btn-source-cider").addEventListener("click", function () {
    fetch("/api/source", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "cider" })
    }).then(function () { pollSource(); showStatus("Switched to Apple Music"); });
  });

  /* ── Encoder / keyboard navigation ─────────────────── */

  var navList        = [];
  var navIndex       = 0;
  var adjustMode     = false;
  var SLIDER_STEP    = 5;
  var NAV_DETENT_MS  = 110;
  var MULTI_PRESS_MS = 350;
  var lastNavTs      = 0;
  var pressCount     = 0;
  var pressTimer     = null;

  function buildNavList() {
    navList = $$(".settings-page .vm-tile, "
                + ".settings-page .settings-btn, "
                + ".settings-page input[type=checkbox], "
                + ".settings-page input[type=range]");
  }

  function clearFocusRing() {
    navList.forEach(function (el) {
      el.classList.remove("kbd-focus");
      el.classList.remove("kbd-focus--adjust");
    });
  }

  function applyFocusRing() {
    clearFocusRing();
    var el = navList[navIndex];
    if (!el) return;
    el.classList.add("kbd-focus");
    if (adjustMode) el.classList.add("kbd-focus--adjust");
    try { el.focus({ preventScroll: true }); } catch (_) { el.focus(); }
    try { el.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
  }

  function moveNav(delta) {
    if (!navList.length) return;
    navIndex = (navIndex + delta + navList.length) % navList.length;
    applyFocusRing();
  }

  function activateCurrent() {
    var el = navList[navIndex];
    if (!el) return;
    var tag = el.tagName.toLowerCase();
    var type = (el.getAttribute("type") || "").toLowerCase();

    if (el.classList.contains("vm-tile")) {
      el.click();
      return;
    }
    if (tag === "input" && type === "checkbox") {
      el.checked = !el.checked;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    if (tag === "input" && type === "range") {
      adjustMode = !adjustMode;
      applyFocusRing();
      return;
    }
    if (tag === "button") {
      el.click();
      return;
    }
  }

  function adjustSlider(delta) {
    var el = navList[navIndex];
    if (!el || el.tagName.toLowerCase() !== "input" || el.type !== "range") return;
    var min  = parseFloat(el.min || "0");
    var max  = parseFloat(el.max || "100");
    var step = parseFloat(el.step || "1") || 1;
    var amt  = Math.max(step, SLIDER_STEP);
    var v    = parseFloat(el.value) + delta * amt;
    if (v < min) v = min;
    if (v > max) v = max;
    el.value = String(v);
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function encoderButton() {
    pressCount++;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(function () {
      var n = pressCount;
      pressCount = 0;
      if (n >= 3) {
        window.location.href = "/";
      } else if (n === 2) {
        var saveBtn = document.getElementById("btn-save");
        if (saveBtn) saveBtn.click();
      } else {
        activateCurrent();
      }
    }, MULTI_PRESS_MS);
  }

  window.addEventListener("keydown", function (e) {
    if (e.repeat) return;

    /* Never hijack typing in text/password inputs */
    var ae = document.activeElement;
    if (ae && ae.tagName && ae.tagName.toLowerCase() === "input") {
      var t = (ae.getAttribute("type") || "").toLowerCase();
      if (t === "text" || t === "password" || t === "email" || t === "number" || t === "url") {
        return;
      }
    }

    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      /* Coalesce multi-pulse encoder detents into one nav step */
      var now = performance.now();
      if (now - lastNavTs < NAV_DETENT_MS) return;
      lastNavTs = now;

      if (e.key === "ArrowDown") {
        if (adjustMode) adjustSlider(-1);
        else            moveNav(+1);
      } else {
        if (adjustMode) adjustSlider(+1);
        else            moveNav(-1);
      }
    } else if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      encoderButton();
    } else if (e.key === "Escape" && adjustMode) {
      adjustMode = false;
      applyFocusRing();
    }
  });

  /* Rebuild once tiles / buttons exist */
  buildNavList();
  applyFocusRing();

  /* Boot */
  loadSettings();
  pollCpu();
  pollSource();
  setInterval(pollCpu, 5000);
  setInterval(pollSource, 5000);
})();
