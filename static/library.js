/* PiMusic library overlay — Spotify search, playlists, liked songs.
   Touch-first UI for the 800x480 kiosk with an on-screen keyboard. */
(function () {
  "use strict";

  var btn      = document.getElementById("library-btn");
  var overlay  = document.getElementById("library-overlay");
  if (!btn || !overlay) return;

  var demo = document.body.getAttribute("data-demo") === "1";
  if (demo) {
    btn.classList.add("hidden");
    return;
  }

  var closeBtn  = document.getElementById("library-close");
  var content   = document.getElementById("lib-content");
  var keyboard  = document.getElementById("lib-keyboard");
  var queryEl   = document.getElementById("lib-query");
  var searchbar = document.getElementById("lib-searchbar");
  var tabBtns   = overlay.querySelectorAll(".lib-tab");

  var P = window.PiMusic || {};
  var toast = P.toast || function () {};

  var activeTab   = "search";
  var query       = "";
  var searchTimer = null;
  var fetchToken  = 0;
  var SEARCH_DEBOUNCE_MS = 450;
  var PAGE = 50;
  var AUTO_LOAD_DELAY_MS = 100;
  var ENC_HOLD_MS = 550;
  var collectionCache = Object.create(null);  /* url?offset=N -> JSON */
  var autoLoadChainId = 0;
  var detailTrackMode = false;
  var activeCollectionView = null;
  var encHoldTimer = null;
  var encHoldHandled = false;

  /* ── Net helpers ──────────────────────────────────────── */

  function cacheKey(url, offset) {
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "offset=" + (offset || 0);
  }

  function getJson(url, offset) {
    var key = offset !== undefined ? cacheKey(url.split("?")[0], offset) : null;
    if (key && collectionCache[key]) {
      return Promise.resolve(collectionCache[key]);
    }
    var fullUrl = url;
    if (offset !== undefined && fullUrl.indexOf("offset=") < 0) {
      fullUrl += (fullUrl.indexOf("?") >= 0 ? "&" : "?") + "offset=" + offset;
    }
    return fetch(fullUrl).then(function (r) {
      if (!r.ok) {
        return r.json().then(
          function (j) { throw new Error(j.error || ("HTTP " + r.status)); },
          function ()  { throw new Error("HTTP " + r.status); }
        );
      }
      return r.json();
    }).then(function (data) {
      if (key) collectionCache[key] = data;
      return data;
    });
  }

  function prefetchCollection(url, offset) {
    var base = url.split("?")[0];
    var key = cacheKey(base, offset);
    if (collectionCache[key]) return;
    getJson(base, offset).then(function (data) {
      if (data && data.has_more && data.next_offset != null) {
        prefetchCollection(base, data.next_offset);
      }
    }).catch(function () {});
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false }; });
    }).catch(function () {
      return { ok: false };
    });
  }

  /* ── DOM builders (textContent only -- track names are untrusted) ── */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function thumb(src, square) {
    var wrap = el("div", "lib-thumb" + (square ? "" : " lib-thumb--round"));
    if (src) {
      var img = document.createElement("img");
      img.loading = "lazy";
      img.src = src;
      img.alt = "";
      wrap.appendChild(img);
    } else {
      wrap.classList.add("lib-thumb--empty");
    }
    return wrap;
  }

  function spinner() {
    return el("div", "lib-spinner");
  }

  function message(text) {
    return el("div", "lib-message", text);
  }

  function trackRow(t, onPlay, withQueue) {
    var row = el("div", "lib-row");
    if (t.uri) row.dataset.uri = t.uri;
    if (t.name) row.dataset.name = t.name;
    row._libPlay = onPlay;
    row.appendChild(thumb(t.art, true));
    var main = el("div", "lib-row-main");
    main.appendChild(el("div", "lib-row-title", t.name));
    var sub = t.artists + (t.album ? " \u00b7 " + t.album : "");
    main.appendChild(el("div", "lib-row-sub", sub));
    row.appendChild(main);
    if (withQueue !== false) {
      var qBtn = el("button", "lib-row-action", "");
      qBtn.setAttribute("aria-label", "Add to queue");
      qBtn.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M3 15h8v-2H3v2zm0-4h8V9H3v2zm0-6v2h12V5H3zm13 10.18V9h5v2h-3v6a3 3 0 1 1-2-2.82z"/></svg>';
      qBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        queueTrack(t.uri);
      });
      row.appendChild(qBtn);
    }
    row.addEventListener("click", function () { onPlay(t); });
    return row;
  }

  function mediaRow(item, subText, onOpen) {
    var row = el("div", "lib-row");
    row.appendChild(thumb(item.art, true));
    var main = el("div", "lib-row-main");
    main.appendChild(el("div", "lib-row-title", item.name));
    main.appendChild(el("div", "lib-row-sub", subText));
    row.appendChild(main);
    var chev = el("div", "lib-row-chevron", "");
    chev.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/></svg>';
    row.appendChild(chev);
    row.addEventListener("click", function () { onOpen(item); });
    return row;
  }

  function sectionTitle(text) {
    return el("div", "lib-section-title", text);
  }

  function detailHeader(title, subText, onBack, playModes) {
    var head = el("div", "lib-detail-header");
    var back = el("button", "lib-back-btn", "");
    back.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>';
    back.addEventListener("click", onBack);
    head.appendChild(back);
    var main = el("div", "lib-detail-main");
    main.appendChild(el("div", "lib-detail-title", title));
    if (subText) main.appendChild(el("div", "lib-detail-sub", subText));
    head.appendChild(main);
    if (playModes) {
      var modes = el("div", "lib-playmodes");
      var defs = [
        { cls: "lib-playmode-btn", label: "Play", title: "Play in order",
          icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
          onClick: playModes.normal },
        { cls: "lib-playmode-btn", label: "Shuffle", title: "Shuffle play",
          icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41-1.41 1.41 3.13 3.14L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg>',
          onClick: playModes.shuffle },
        { cls: "lib-playmode-btn", label: "Repeat", title: "Repeat playlist",
          icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/></svg>',
          onClick: playModes.repeat }
      ];
      defs.forEach(function (d) {
        var b = el("button", d.cls, "");
        b.setAttribute("title", d.title);
        b.innerHTML = d.icon + "<span>" + d.label + "</span>";
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          d.onClick();
        });
        modes.appendChild(b);
      });
      head.appendChild(modes);
    }
    return head;
  }

  function ensureLoadingStatus() {
    var st = content.querySelector(".lib-loading-status");
    if (!st) {
      st = el("div", "lib-loading-status");
      content.appendChild(st);
    }
    return st;
  }

  function removeLoadingStatus() {
    var st = content.querySelector(".lib-loading-status");
    if (st) st.remove();
  }

  function autoLoadCollection(opts) {
    var chainId = ++autoLoadChainId;
    var baseUrl = opts.url.split("?")[0];
    var offset = opts.offset;
    var myToken = opts.myToken;
    var totalLoaded = opts.initialCount || 0;
    var total = opts.total;

    function updateStatus() {
      if (myToken !== fetchToken) return;
      var st = ensureLoadingStatus();
      if (total && total > 0) {
        st.textContent = "Loading\u2026 " + totalLoaded + "/" + total;
      } else {
        st.textContent = "Loading\u2026 " + totalLoaded + " songs";
      }
    }

    function loadNext() {
      if (chainId !== autoLoadChainId || myToken !== fetchToken) return;
      if (offset == null) {
        removeLoadingStatus();
        if (opts.onDone) opts.onDone(totalLoaded);
        return;
      }
      getJson(baseUrl, offset).then(function (data) {
        if (chainId !== autoLoadChainId || myToken !== fetchToken) return;
        var items = data.items || [];
        totalLoaded += items.length;
        if (data.total != null) total = data.total;
        if (items.length && opts.onPage) opts.onPage(items, data);
        if (data.has_more && data.next_offset != null) {
          offset = data.next_offset;
          updateStatus();
          setTimeout(loadNext, AUTO_LOAD_DELAY_MS);
        } else {
          removeLoadingStatus();
          if (opts.onDone) opts.onDone(totalLoaded);
        }
      }).catch(function () {
        if (chainId !== autoLoadChainId || myToken !== fetchToken) return;
        removeLoadingStatus();
        toast("Couldn't load more");
        if (opts.onError) opts.onError();
      });
    }

    updateStatus();
    setTimeout(loadNext, AUTO_LOAD_DELAY_MS);
  }

  function setContent() {
    detailTrackMode = false;
    activeCollectionView = null;
    autoLoadChainId++;
    clearEncoderFocus();
    content.innerHTML = "";
    for (var i = 0; i < arguments.length; i++) {
      if (arguments[i]) content.appendChild(arguments[i]);
    }
    content.scrollTop = 0;
  }

  /* ── Playback actions ─────────────────────────────────── */

  function playBody(body, label) {
    postJson("/api/library/play", body).then(function (res) {
      if (res && res.ok) {
        toast("Playing " + (label || "\u2026"));
        close();
        if (P.emergencyPoll) P.emergencyPoll(450);
      } else {
        toast(res && res.error === "rate_limited"
          ? "Spotify rate limited \u2013 try later"
          : "Couldn't start playback \u2013 is Spotify open?");
      }
    });
  }

  function queueTrack(uri) {
    postJson("/api/library/queue", { uri: uri }).then(function (res) {
      toast(res && res.ok ? "Added to queue" : "Couldn't add to queue");
    });
  }

  /* ── Search tab ───────────────────────────────────────── */

  function renderQuery() {
    queryEl.innerHTML = "";
    if (query) {
      queryEl.appendChild(el("span", "lib-query-text", query));
      queryEl.appendChild(el("span", "lib-query-caret", ""));
    } else {
      queryEl.appendChild(el("span", "lib-query-placeholder", "Search Spotify\u2026"));
    }
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, SEARCH_DEBOUNCE_MS);
  }

  function doSearch() {
    var q = query.trim();
    if (q.length < 2) {
      setContent(message("Type to search songs, albums, and playlists"));
      return;
    }
    var myToken = ++fetchToken;
    setContent(spinner());
    getJson("/api/library/search?q=" + encodeURIComponent(q)).then(function (data) {
      if (myToken !== fetchToken || activeTab !== "search") return;
      renderSearchResults(data);
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Search failed: " + e.message));
    });
  }

  function renderSearchResults(data) {
    var frag = document.createDocumentFragment();
    var tracks = data.tracks || [];
    var albums = data.albums || [];
    var playlists = data.playlists || [];

    if (!tracks.length && !albums.length && !playlists.length) {
      setContent(message("No results for \u201c" + query.trim() + "\u201d"));
      return;
    }

    if (tracks.length) {
      frag.appendChild(sectionTitle("Songs"));
      tracks.forEach(function (t) {
        frag.appendChild(trackRow(t, function () {
          playBody({ uri: t.uri }, t.name);
        }, true));
      });
    }
    if (albums.length) {
      frag.appendChild(sectionTitle("Albums"));
      albums.forEach(function (a) {
        frag.appendChild(mediaRow(a, a.artists, function () { openAlbum(a); }));
      });
    }
    if (playlists.length) {
      frag.appendChild(sectionTitle("Playlists"));
      playlists.forEach(function (p) {
        frag.appendChild(mediaRow(
          p,
          (p.owner ? "by " + p.owner : "Playlist") + (p.total ? " \u00b7 " + p.total + " songs" : ""),
          function () { openPlaylist(p); }
        ));
      });
    }
    content.innerHTML = "";
    content.appendChild(frag);
    content.scrollTop = 0;
  }

  /* ── On-screen keyboard ───────────────────────────────── */

  var KEY_ROWS = [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
    ["a", "s", "d", "f", "g", "h", "j", "k", "l", "'"],
    ["z", "x", "c", "v", "b", "n", "m", "-", "&", "\u232b"]
  ];

  function buildKeyboard() {
    if (keyboard.childNodes.length) return;
    KEY_ROWS.forEach(function (rowKeys) {
      var row = el("div", "kb-row");
      rowKeys.forEach(function (k) {
        var key = el("button", "kb-key", k);
        if (k === "\u232b") key.classList.add("kb-key--wide");
        key.addEventListener("click", function () { pressKey(k); });
        row.appendChild(key);
      });
      keyboard.appendChild(row);
    });
    var bottom = el("div", "kb-row");
    var clear = el("button", "kb-key kb-key--clear", "Clear");
    clear.addEventListener("click", function () { pressKey("__clear"); });
    var space = el("button", "kb-key kb-key--space", "Space");
    space.addEventListener("click", function () { pressKey(" "); });
    bottom.appendChild(clear);
    bottom.appendChild(space);
    keyboard.appendChild(bottom);
  }

  function pressKey(k) {
    if (k === "\u232b") {
      query = query.slice(0, -1);
    } else if (k === "__clear") {
      query = "";
    } else if (k === " ") {
      if (query && !/\s$/.test(query)) query += " ";
    } else {
      query += k;
    }
    renderQuery();
    scheduleSearch();
  }

  /* ── Playlists tab ────────────────────────────────────── */

  function renderPlaylists(reset) {
    var myToken = ++fetchToken;
    setContent(spinner());
    getJson("/api/library/playlists", 0).then(function (data) {
      if (myToken !== fetchToken || activeTab !== "playlists") return;
      var frag = document.createDocumentFragment();
      var items = data.items || [];
      if (!items.length) {
        setContent(message("No playlists found"));
        return;
      }
      items.forEach(function (p) { frag.appendChild(playlistRow(p)); });
      content.innerHTML = "";
      content.appendChild(frag);
      if (data.has_more && data.next_offset != null) {
        autoLoadCollection({
          url: "/api/library/playlists",
          offset: data.next_offset,
          myToken: myToken,
          initialCount: items.length,
          total: data.total,
          onPage: function (newItems) {
            newItems.forEach(function (p) {
              content.insertBefore(playlistRow(p), ensureLoadingStatus());
            });
          }
        });
      }
      content.scrollTop = 0;
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Couldn't load playlists: " + e.message));
    });
  }

  function playlistRow(p) {
    return mediaRow(
      p,
      (p.owner ? "by " + p.owner : "Playlist") + (p.total ? " \u00b7 " + p.total + " songs" : ""),
      function () { openPlaylist(p); }
    );
  }

  function openPlaylist(p) {
    var myToken = ++fetchToken;
    var contextUri = p.uri || ("spotify:playlist:" + p.id);
    var moreUrl = "/api/library/playlists/" + encodeURIComponent(p.id);
    setContent(spinner());
    getJson(moreUrl, 0).then(function (data) {
      if (myToken !== fetchToken) return;
      renderTrackCollection({
        title: p.name,
        sub: (p.owner ? "by " + p.owner : ""),
        contextUri: contextUri,
        items: data.items || [],
        hasMore: data.has_more,
        nextOffset: data.next_offset,
        total: data.total,
        moreUrl: moreUrl,
        isPlaylist: true,
        onBack: function () { setTab("playlists"); }
      }, myToken);
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Couldn't load playlist: " + e.message));
    });
  }

  /* ── Albums (from search) ─────────────────────────────── */

  function openAlbum(a) {
    var myToken = ++fetchToken;
    var contextUri = a.uri || ("spotify:album:" + a.id);
    var moreUrl = "/api/library/albums/" + encodeURIComponent(a.id);
    setContent(spinner());
    getJson(moreUrl, 0).then(function (data) {
      if (myToken !== fetchToken) return;
      renderTrackCollection({
        title: data.name || a.name,
        sub: data.artists || a.artists,
        contextUri: contextUri,
        items: data.items || [],
        hasMore: data.has_more,
        nextOffset: data.next_offset,
        total: data.total,
        moreUrl: moreUrl,
        isPlaylist: false,
        onBack: function () { setTab("search", true); }
      }, myToken);
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Couldn't load album: " + e.message));
    });
  }

  function collectionPlayModes(view) {
    if (!view.isPlaylist) {
      return {
        normal: function () {
          playBody({ context_uri: view.contextUri }, view.title);
        }
      };
    }
    return {
      normal: function () {
        playBody({
          context_uri: view.contextUri,
          shuffle: false,
          repeat: "off"
        }, view.title);
      },
      shuffle: function () {
        playBody({
          context_uri: view.contextUri,
          shuffle: true,
          repeat: "off"
        }, view.title + " (shuffle)");
      },
      repeat: function () {
        playBody({
          context_uri: view.contextUri,
          shuffle: false,
          repeat: "context"
        }, view.title + " (repeat)");
      }
    };
  }

  /* Shared playlist/album track list view. Tapping a track plays it inside
     its context (so the queue continues with the rest of the collection). */
  function appendCollectionTrackRow(view, t) {
    return trackRow(t, function () {
      playBody({ context_uri: view.contextUri, offset_uri: t.uri }, t.name);
    }, true);
  }

  function renderTrackCollection(view, myToken) {
    detailTrackMode = view.items.length > 0;
    activeCollectionView = view;
    var frag = document.createDocumentFragment();
    var modes = collectionPlayModes(view);
    frag.appendChild(detailHeader(
      view.title,
      view.sub,
      view.onBack,
      view.isPlaylist ? modes : null
    ));
    if (!view.isPlaylist && modes.normal) {
      var playRow = el("button", "lib-playall-btn lib-playall-btn--solo", "");
      playRow.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg><span>Play album</span>';
      playRow.addEventListener("click", modes.normal);
      frag.appendChild(playRow);
    }
    if (!view.items.length) {
      detailTrackMode = false;
      frag.appendChild(message("No playable songs in this playlist"));
    }
    view.items.forEach(function (t) {
      frag.appendChild(appendCollectionTrackRow(view, t));
    });
    content.innerHTML = "";
    content.appendChild(frag);
    if (view.hasMore && view.nextOffset != null) {
      autoLoadCollection({
        url: view.moreUrl,
        offset: view.nextOffset,
        myToken: myToken,
        initialCount: view.items.length,
        total: view.total,
        onPage: function (items) {
          items.forEach(function (t) {
            view.items.push(t);
            content.insertBefore(
              appendCollectionTrackRow(view, t),
              ensureLoadingStatus()
            );
          });
        },
        onDone: function () { focusFirstTrackRowIfNone(); }
      });
    } else if (detailTrackMode) {
      focusFirstTrackRow();
    }
    content.scrollTop = 0;
  }

  /* ── Queue tab ────────────────────────────────────────── */

  function renderQueue() {
    var myToken = ++fetchToken;
    setContent(spinner());
    getJson("/api/queue").then(function (data) {
      if (myToken !== fetchToken || activeTab !== "queue") return;
      var frag = document.createDocumentFragment();
      var now = data.currently_playing;
      var items = data.items || [];
      if (!now && !items.length) {
        setContent(message("Queue is empty"));
        return;
      }
      if (now) {
        frag.appendChild(sectionTitle("Now playing"));
        var nowRow = trackRow(now, function () {}, false);
        nowRow.classList.add("lib-row--current");
        frag.appendChild(nowRow);
      }
      if (items.length) {
        frag.appendChild(sectionTitle("Next up"));
        items.forEach(function (t) {
          frag.appendChild(trackRow(t, function () {
            playBody({ uri: t.uri }, t.name);
          }, false));
        });
      }
      content.innerHTML = "";
      content.appendChild(frag);
      content.scrollTop = 0;
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Couldn't load queue: " + e.message));
    });
  }

  /* ── Liked tab ────────────────────────────────────────── */

  var likedItems = [];

  function renderLiked() {
    var myToken = ++fetchToken;
    likedItems = [];
    setContent(spinner());
    getJson("/api/library/liked", 0).then(function (data) {
      if (myToken !== fetchToken || activeTab !== "liked") return;
      likedItems = data.items || [];
      if (!likedItems.length) {
        setContent(message("No liked songs found"));
        return;
      }
      var frag = document.createDocumentFragment();
      detailTrackMode = likedItems.length > 0;
      likedItems.forEach(function (t, i) { frag.appendChild(likedRow(t, i)); });
      content.innerHTML = "";
      content.appendChild(frag);
      if (data.has_more && data.next_offset != null) {
        autoLoadCollection({
          url: "/api/library/liked",
          offset: data.next_offset,
          myToken: myToken,
          initialCount: likedItems.length,
          total: data.total,
          onPage: function (items) {
            var start = likedItems.length;
            items.forEach(function (t, i) {
              likedItems.push(t);
              content.insertBefore(
                likedRow(t, start + i),
                ensureLoadingStatus()
              );
            });
          },
          onDone: function () { focusFirstTrackRowIfNone(); }
        });
      } else if (detailTrackMode) {
        focusFirstTrackRow();
      }
      content.scrollTop = 0;
    }).catch(function (e) {
      if (myToken !== fetchToken) return;
      setContent(message("Couldn't load liked songs: " + e.message));
    });
  }

  function likedRow(t, index) {
    return trackRow(t, function () {
      /* Play from this song onward (next 50 loaded liked songs). */
      var uris = [];
      for (var i = index; i < likedItems.length && uris.length < 50; i++) {
        uris.push(likedItems[i].uri);
      }
      playBody({ uris: uris }, t.name);
    }, true);
  }

  /* ── Tabs / open / close ──────────────────────────────── */

  function setTab(name, keepResults) {
    activeTab = name;
    for (var i = 0; i < tabBtns.length; i++) {
      tabBtns[i].classList.toggle("lib-tab--active", tabBtns[i].getAttribute("data-tab") === name);
    }
    var isSearch = name === "search";
    keyboard.classList.toggle("hidden", !isSearch);
    searchbar.classList.toggle("hidden", !isSearch);

    fetchToken++;
    if (isSearch) {
      renderQuery();
      if (query.trim().length >= 2) doSearch();
      else setContent(message("Type to search songs, albums, and playlists"));
    } else if (name === "playlists") {
      renderPlaylists(true);
    } else if (name === "liked") {
      renderLiked();
    } else if (name === "queue") {
      renderQueue();
    }
  }

  var isOpen = false;

  function open(opts) {
    opts = opts || {};
    var keepLyrics = !!opts.keepLyrics;
    if (!keepLyrics && window.PiMusicLyrics && window.PiMusicLyrics.isOpen()) {
      window.PiMusicLyrics.close();
    }
    if (typeof opts.tab === "string") activeTab = opts.tab;
    isOpen = true;
    overlay.classList.remove("hidden");
    overlay.classList.toggle("library-overlay--over-lyrics",
      keepLyrics && window.PiMusicLyrics && window.PiMusicLyrics.isOpen());
    window.PiMusicOverlayOpen = true;
    buildKeyboard();
    setTab(activeTab);
  }

  function openSearch(keepLyrics) {
    open({ tab: "search", keepLyrics: keepLyrics !== false });
  }

  function close() {
    isOpen = false;
    overlay.classList.add("hidden");
    overlay.classList.remove("library-overlay--over-lyrics");
    window.PiMusicOverlayOpen = false;
    clearTimeout(searchTimer);
    clearTimeout(encHoldTimer);
    encHoldHandled = false;
    detailTrackMode = false;
    activeCollectionView = null;
    autoLoadChainId++;
    clearEncoderFocus();
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
  for (var i = 0; i < tabBtns.length; i++) {
    (function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        setTab(b.getAttribute("data-tab"));
      });
    })(tabBtns[i]);
  }
  /* Keep taps inside the overlay from reaching the cinematic-exit handlers. */
  overlay.addEventListener("click", function (e) { e.stopPropagation(); });

  /* ── Rotary encoder support ───────────────────────────────
     Rotation moves focus. In track lists, only .lib-row tracks are focused.
     Short press on a track row = play; hold ~0.5s = add to queue.
     2 quick presses (non-track focus) = back; 3+ = close library. */

  var ENC_PRESS_BASE_MS   = 520;
  var ENC_PRESS_EXTEND_MS = 240;
  var ENC_PRESS_MAX_MS    = 1600;
  var encFocusEl = null;
  var encPressCount = 0;
  var encPressTimer = null;

  function encPressWaitMs(count) {
    return Math.min(
      ENC_PRESS_BASE_MS + (count - 1) * ENC_PRESS_EXTEND_MS,
      ENC_PRESS_MAX_MS
    );
  }

  function focusables() {
    var sel = ".lib-tab, .lib-back-btn, .lib-playall-btn, .lib-playmode-btn, .lib-row, #library-close";
    var nodes = overlay.querySelectorAll(sel);
    var out = [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].offsetParent !== null) out.push(nodes[i]);
    }
    return out;
  }

  function trackRows() {
    var nodes = content.querySelectorAll(".lib-row");
    var out = [];
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].offsetParent !== null && nodes[i].dataset.uri) {
        out.push(nodes[i]);
      }
    }
    return out;
  }

  function isTrackFocusRow(el) {
    return el && el.classList.contains("lib-row") && el.dataset.uri;
  }

  function clearEncoderFocus() {
    if (encFocusEl) {
      encFocusEl.classList.remove("kbd-focus");
      encFocusEl = null;
    }
  }

  function focusFirstTrackRow() {
    var rows = trackRows();
    if (!rows.length) return;
    clearEncoderFocus();
    encFocusEl = rows[0];
    encFocusEl.classList.add("kbd-focus");
    if (encFocusEl.scrollIntoView) {
      encFocusEl.scrollIntoView({ block: "nearest" });
    }
  }

  function focusFirstTrackRowIfNone() {
    if (!encFocusEl || !content.contains(encFocusEl) || !isTrackFocusRow(encFocusEl)) {
      focusFirstTrackRow();
    }
  }

  function moveEncoderFocus(dir) {
    var list = detailTrackMode ? trackRows() : focusables();
    if (!list.length) return;
    var idx = encFocusEl ? list.indexOf(encFocusEl) : -1;
    idx = idx + dir;
    if (idx < 0) idx = 0;
    if (idx >= list.length) idx = list.length - 1;
    if (encFocusEl) encFocusEl.classList.remove("kbd-focus");
    encFocusEl = list[idx];
    encFocusEl.classList.add("kbd-focus");
    if (encFocusEl.scrollIntoView) {
      encFocusEl.scrollIntoView({ block: "nearest" });
    }
  }

  function encoderPress() {
    encPressCount++;
    clearTimeout(encPressTimer);
    encPressTimer = setTimeout(function () {
      var n = encPressCount;
      encPressCount = 0;
      if (n >= 3) {
        close();
      } else if (n === 2) {
        var back = overlay.querySelector(".lib-back-btn");
        if (back && back.offsetParent !== null) back.click();
        else close();
      } else if (encFocusEl && encFocusEl.offsetParent !== null) {
        encFocusEl.click();
      }
    }, encPressWaitMs(encPressCount));
  }

  window.addEventListener("keydown", function (e) {
    if (!isOpen || e.repeat) return;
    var up   = e.key === "ArrowUp"   || e.key === "AudioVolumeUp"   || e.key === "VolumeUp";
    var down = e.key === "ArrowDown" || e.key === "AudioVolumeDown" || e.key === "VolumeDown";
    if (up || down) {
      e.preventDefault();
      e.stopPropagation();
      moveEncoderFocus(down ? 1 : -1);
    } else if (e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      encHoldHandled = false;
      clearTimeout(encHoldTimer);
      if (isTrackFocusRow(encFocusEl)) {
        encHoldTimer = setTimeout(function () {
          encHoldHandled = true;
          queueTrack(encFocusEl.dataset.uri);
        }, ENC_HOLD_MS);
      }
    }
  }, true);

  window.addEventListener("keyup", function (e) {
    if (!isOpen || e.key !== " ") return;
    e.preventDefault();
    e.stopPropagation();
    clearTimeout(encHoldTimer);
    if (encHoldHandled) {
      encHoldHandled = false;
      return;
    }
    if (isTrackFocusRow(encFocusEl)) {
      if (encFocusEl._libPlay) encFocusEl._libPlay();
      return;
    }
    encoderPress();
  }, true);

  window.PiMusicLibrary = {
    isOpen: function () { return isOpen; },
    close: close,
    open: open,
    openSearch: openSearch
  };
})();
