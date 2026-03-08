(function () {
  "use strict";

  var $ = function (s) { return document.querySelector(s); };

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
    visual_mode:          $("#visual_mode")
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

  /* Boot */
  loadSettings();
  pollCpu();
  pollSource();
  setInterval(pollCpu, 5000);
  setInterval(pollSource, 5000);
})();
