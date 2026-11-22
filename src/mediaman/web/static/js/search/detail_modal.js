/**
 * search/detail_modal.js — detail modal open/close and content rendering
 * for the /search page.
 *
 * Owns: resetModal, openDetail, renderDetail, renderSeasonPicker,
 * refreshSeasonUI, updateSeasonSummary, renderPrimaryButton, submitDownload,
 * closeModal.
 *
 * Cross-module dependencies:
 *   MM.modal.setupDetail       — display/aria/body-overflow + a11y lifecycle
 *   MM.api.get / MM.api.post   — API helpers
 *   MM.api.APIError            — typed error for download failures
 *   MM.search.shelves          — escapeHtml/escapeAttr helpers + refreshCurrentView
 *                                (injected via init)
 *
 * Exposes:
 *   MM.search.detail.init({ refreshCurrentView })
 *   MM.search.detail.openDetail(mediaType, tmdbId)
 */
(() => {
  'use strict';

  window.MM = window.MM || {};
  MM.search = MM.search || {};

  const modal   = document.getElementById("detail-modal");
  const modalEl = {
    hero:     document.getElementById("modal-hero"),
    quick:    document.getElementById("modal-quick"),
    ratings:  document.getElementById("modal-ratings"),
    tagline:  document.getElementById("modal-tagline"),
    desc:     document.getElementById("modal-desc"),
    reason:   document.getElementById("modal-reason"),
    trailerL: document.getElementById("modal-trailer-label"),
    trailer:  document.getElementById("modal-trailer"),
    trailerF: document.getElementById("modal-trailer-fallback"),
    castL:    document.getElementById("modal-cast-label"),
    cast:     document.getElementById("modal-cast"),
    seasonsL: document.getElementById("detail-modal-seasons-label"),
    seasons:  document.getElementById("detail-modal-seasons"),
    actions:  document.getElementById("modal-actions"),
  };

  let currentDetail   = null;
  let seasonTickState = new Map();
  let lockedSeasons   = new Set();
  /* Bumped on every open and close so a delayed close, or a detail reply
     that arrives late, can tell whether the modal it was started in is
     still showing. */
  let openCount       = 0;
  /* Titles with an Add in flight, by media type and TMDB id. The request
     outlives the modal, so a reopened title must still show it as busy. */
  const adding        = new Set();
  /* Titles added successfully during this page session, same keys. A detail
     reply that was already in flight when the Add finished carries the state
     from before it, so the button cannot trust the reply alone. */
  const added         = new Set();

  /* Injected at init time. */
  let _refreshCurrentView = null;

  /* Modal lifecycle via MM.modal.setupDetail.
     setupDetail owns display:flex/none + aria-hidden + body overflow +
     ModalA11y registration. The close-bar button uses [data-close-modal]
     which is wired automatically. */
  const detailModal = MM.modal.setupDetail(modal, {
    /* Esc and backdrop close without going through closeModal. */
    onClose: function() { openCount += 1; currentDetail = null; },
  });
  function closeModal() { detailModal.close(); }

  function escapeHtml(v) { return MM.search.shelves.escapeHtml(v); }
  function escapeAttr(v) { return MM.search.shelves.escapeAttr(v); }

  function resetModal() {
    modalEl.hero.innerHTML     = "";
    modalEl.quick.innerHTML    = "";
    modalEl.ratings.innerHTML  = "";
    modalEl.tagline.textContent = "";
    modalEl.desc.textContent   = "";
    if (modalEl.reason) modalEl.reason.style.display = "none";
    modalEl.trailerL.style.display = "none";
    modalEl.trailer.style.display  = "none";
    modalEl.trailer.innerHTML  = "";
    if (modalEl.trailerF) modalEl.trailerF.innerHTML = "";
    modalEl.castL.style.display  = "none";
    modalEl.cast.innerHTML       = "";
    modalEl.seasonsL.style.display = "none";
    modalEl.seasons.style.display  = "none";
    modalEl.seasons.innerHTML      = "";
    modalEl.actions.innerHTML      = "";
  }

  /* The dialog is named by the hero title, so it needs one while there is
     no detail to draw: loading, and after a failed load. */
  function setHeroName(text) {
    const h = document.createElement("h2");
    h.id = "detail-modal-title";
    h.className = "sr-only";
    h.textContent = text;
    modalEl.hero.replaceChildren(h);
  }

  async function openDetail(mediaType, tmdbId) {
    const mine = ++openCount;
    currentDetail = null;
    detailModal.open();
    resetModal();
    setHeroName("Loading…");
    const loading = document.createElement("div");
    loading.className = "modal-loading";
    loading.textContent = "Loading...";
    modalEl.actions.appendChild(loading);

    let data;
    try {
      data = await MM.api.get(
        "/api/search/detail/" + encodeURIComponent(mediaType) + "/" + encodeURIComponent(tmdbId),
      );
    } catch (e) {
      if (mine !== openCount) return;
      setHeroName("Couldn't load details.");
      modalEl.actions.replaceChildren();
      const err = document.createElement("div");
      err.className = "modal-error";
      err.textContent = "Couldn't load details. ";
      const closeBtn = document.createElement("button");
      closeBtn.textContent = "Close";
      closeBtn.className = "modal-err-close";
      closeBtn.addEventListener("click", closeModal);
      err.appendChild(closeBtn);
      modalEl.actions.appendChild(err);
      return;
    }
    /* A reply for a modal that has since been closed or reopened on
       another title must not draw over it. */
    if (mine !== openCount) return;
    currentDetail = data;
    renderDetail(data);
  }

  function addKey(mediaType, tmdbId) { return mediaType + ":" + tmdbId; }

  /* SECURITY: hero, quick, ratings, and cast blocks use innerHTML
     with template literals. This is intentional — the DOM-construction path
     would not materially reduce risk because the same data is flowing through.
     EVERY server-supplied value MUST go through escapeHtml() or escapeAttr()
     before interpolation. If you add a new field here, escape it — no
     exceptions. Do NOT pass d.* values bare into innerHTML. */
  function renderDetail(d) {
    const img = d.backdrop_url || d.poster_url;
    /* innerHTML is intentional here: escapeAttr/escapeHtml sanitise every
       interpolated value. The title is drawn with or without artwork: the
       dialog is named by it. */
    modalEl.hero.innerHTML =
      (img ? '<img src="' + escapeAttr(img) + '" alt="">' +
             '<div class="detail-modal-hero-gradient"></div>' : '') +
      '<div class="detail-modal-hero-info">' +
      '<h2 id="detail-modal-title" class="detail-modal-hero-title">' + escapeHtml(d.title) +
      (d.year ? ' <span class="year">' + escapeHtml(d.year) + '</span>' : '') +
      '</h2></div>';

    const runtime = d.runtime ? d.runtime + " min" : "";
    const quickBits = [
      '<span class="type-pill' + (d.media_type === "movie" ? " type-pill--movie" : "") + '">' + (d.media_type === "movie" ? "Movie" : "TV Series") + '</span>',
      (d.genres || []).map(function(g) {
        return '<span class="meta-text">' + escapeHtml(g) + '</span>';
      }).join(""),
      runtime ? '<span class="meta-text">' + escapeHtml(runtime) + '</span>' : "",
      d.director ? '<span class="meta-text">' + escapeHtml(d.director) + '</span>' : "",
    ].filter(Boolean).join("");
    modalEl.quick.innerHTML = quickBits;

    const STAR   = '<span class="rating-icon" aria-hidden="true">★</span>';
    const TOMATO = '<span class="rating-icon" aria-hidden="true">🍅</span>';
    const ratings = [];
    if (d.rating_tmdb)      ratings.push('<span class="rating-pill r-tmdb">' + STAR + 'TMDB ' + escapeHtml(d.rating_tmdb) + '</span>');
    if (d.rating_imdb)      ratings.push('<span class="rating-pill r-imdb">' + STAR + 'IMDb ' + escapeHtml(d.rating_imdb) + '</span>');
    if (d.rating_rt)        ratings.push('<span class="rating-pill r-rt">' + TOMATO + 'RT ' + escapeHtml(d.rating_rt) + '</span>');
    if (d.rating_metascore) ratings.push('<span class="rating-pill r-meta">' + STAR + 'Metascore ' + escapeHtml(d.rating_metascore) + '</span>');
    modalEl.ratings.innerHTML = ratings.join("");

    modalEl.tagline.textContent = d.tagline ? '"' + d.tagline + '"' : "";
    modalEl.desc.textContent    = d.description || "";

    /* Validate key is exactly 11 URL-safe base64 chars before
       building the iframe to prevent injection via a crafted trailer_key. */
    if (d.trailer_key && /^[A-Za-z0-9_-]{11}$/.test(d.trailer_key)) {
      modalEl.trailerL.style.display = "";
      /* Build iframe via DOM to avoid any innerHTML injection. */
      const iframe = document.createElement("iframe");
      iframe.src = "https://www.youtube.com/embed/" + d.trailer_key + "?rel=0";
      iframe.setAttribute("allowfullscreen", "");
      iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-presentation");
      iframe.setAttribute("allow", "autoplay; encrypted-media; fullscreen");
      iframe.setAttribute("referrerpolicy", "strict-origin");
      iframe.setAttribute("loading", "lazy");
      while (modalEl.trailer.firstChild) modalEl.trailer.removeChild(modalEl.trailer.firstChild);
      modalEl.trailer.appendChild(iframe);
      modalEl.trailer.style.display = "";
    }

    if (d.cast && d.cast.length) {
      modalEl.castL.style.display = "";
      /* Every server-supplied field (profile_url, name, character, initials)
         is passed through escapeAttr or escapeHtml — no bare interpolation. */
      modalEl.cast.innerHTML = d.cast.map(function(c) {
        const initials = (c.name || "?").split(" ").map(function(x) { return x[0]; }).slice(0, 2).join("");
        const avatar = c.profile_url
          ? '<div class="cast-avatar"><img src="' + escapeAttr(c.profile_url) + '" alt=""></div>'
          : '<div class="cast-avatar">' + escapeHtml(initials) + '</div>';
        return '<div class="cast-item">' + avatar +
          '<div class="cast-name">' + escapeHtml(c.name || "") + '</div>' +
          '<div class="cast-char">' + escapeHtml(c.character || "") + '</div></div>';
      }).join("");
    }

    if (d.media_type === "tv" && d.seasons) {
      renderSeasonPicker(d.seasons);
    }

    renderPrimaryButton(d);
  }

  function renderSeasonPicker(seasons) {
    modalEl.seasonsL.style.display = "";
    modalEl.seasons.style.display  = "";
    seasonTickState = new Map();
    lockedSeasons   = new Set();
    for (const s of seasons) {
      if (s.in_library) lockedSeasons.add(s.season_number);
      else seasonTickState.set(s.season_number, true);
    }

    const gridRows = seasons.map(function(s) {
      const locked  = s.in_library;
      const subBits = [(s.episode_count || 0) + " ep"];
      if (locked) subBits.push("In library");
      else if (s.year) subBits.push(String(s.year));
      return '<div class="detail-modal-season ' + (locked ? "already" : "checked") +
        '" data-season="' + escapeAttr(s.season_number) + '">' +
        '<div class="detail-modal-season-check"></div><div>' +
        '<div class="detail-modal-season-name">' + escapeHtml(s.name || "Season " + s.season_number) + '</div>' +
        '<div class="detail-modal-season-sub">' + escapeHtml(subBits.join(" · ")) + '</div>' +
        '</div></div>';
    }).join("");

    modalEl.seasons.innerHTML =
      '<div class="detail-modal-seasons-head">' +
        '<div class="detail-modal-seasons-title">Seasons — pick which to download</div>' +
        '<div>' +
          '<button type="button" class="detail-modal-seasons-link" data-action="all">Select all</button>' +
          '<button type="button" class="detail-modal-seasons-link detail-modal-seasons-link--clear" data-action="clear">Clear</button>' +
        '</div>' +
      '</div>' +
      '<div class="detail-modal-seasons-grid">' + gridRows + '</div>' +
      '<div class="detail-modal-season-summary" id="detail-season-summary"></div>';

    modalEl.seasons.querySelectorAll(".detail-modal-season").forEach(function(el) {
      const num = Number(el.dataset.season);
      if (lockedSeasons.has(num)) return;
      el.addEventListener("click", function() {
        const current = seasonTickState.get(num);
        seasonTickState.set(num, !current);
        el.classList.toggle("checked", !current);
        updateSeasonSummary();
        renderPrimaryButton(currentDetail);
      });
    });
    modalEl.seasons.querySelector('[data-action="all"]').addEventListener("click", function() {
      for (const k of seasonTickState.keys()) seasonTickState.set(k, true);
      refreshSeasonUI();
    });
    modalEl.seasons.querySelector('[data-action="clear"]').addEventListener("click", function() {
      for (const k of seasonTickState.keys()) seasonTickState.set(k, false);
      refreshSeasonUI();
    });
    updateSeasonSummary();
  }

  function refreshSeasonUI() {
    modalEl.seasons.querySelectorAll(".detail-modal-season").forEach(function(el) {
      const num = Number(el.dataset.season);
      if (lockedSeasons.has(num)) return;
      el.classList.toggle("checked", !!seasonTickState.get(num));
    });
    updateSeasonSummary();
    renderPrimaryButton(currentDetail);
  }

  function updateSeasonSummary() {
    const ticked  = [...seasonTickState.entries()].filter(function([, v]) { return v; }).length;
    const already = lockedSeasons.size;
    const summary = document.getElementById("detail-season-summary");
    if (!summary) return;
    summary.textContent = "";
    if (ticked) {
      const s1 = document.createElement("strong");
      s1.textContent = ticked + " " + (ticked === 1 ? "season" : "seasons");
      summary.appendChild(s1);
      summary.appendChild(document.createTextNode(" will be added and searched"));
    }
    if (already) {
      if (ticked) summary.appendChild(document.createTextNode(" · "));
      const s2 = document.createElement("strong");
      s2.textContent = already + " " + (already === 1 ? "season" : "seasons");
      summary.appendChild(s2);
      summary.appendChild(document.createTextNode(" already in library"));
    }
  }

  function renderPrimaryButton(d) {
    const tickedSeasons = [...seasonTickState.entries()].filter(function([, v]) { return v; }).map(function([k]) { return k; });
    let label    = "Download";
    let variant  = "";
    let disabled = false;
    let click    = null;

    if (added.has(addKey(d.media_type, d.tmdb_id))) {
      label = "Added ✓"; variant = "is-added"; disabled = true;
    } else if (adding.has(addKey(d.media_type, d.tmdb_id))) {
      label = "Adding…"; disabled = true;
    } else if (d.media_type === "movie") {
      if (d.download_state === "in_library")      { label = "In Library ✓"; variant = "ready";  disabled = true; }
      else if (d.download_state === "downloading") { label = "Downloading"; variant = "queued"; disabled = true; }
      else if (d.download_state === "queued")      { label = "Queued";      variant = "queued"; disabled = true; }
      else click = function() { submitDownload({ media_type: "movie", tmdb_id: d.tmdb_id, title: d.title }); };
    } else {
      const allLocked = lockedSeasons.size > 0 && seasonTickState.size === 0;
      if (d.sonarr_tracked)           { label = "Already tracked — manage in Library"; variant = "ready"; disabled = true; }
      else if (allLocked)             { label = "All seasons in library ✓"; variant = "ready"; disabled = true; }
      else if (tickedSeasons.length === 0) { label = "Pick a season"; disabled = true; }
      else {
        label = "Download " + tickedSeasons.length + " " + (tickedSeasons.length === 1 ? "season" : "seasons");
        click = function() {
          submitDownload({
            media_type: "tv",
            tmdb_id: d.tmdb_id,
            title: d.title,
            monitored_seasons: [...tickedSeasons, ...lockedSeasons],
            search_seasons: tickedSeasons,
          });
        };
      }
    }

    modalEl.actions.innerHTML = "";
    const primary = document.createElement("button");
    primary.className   = variant ? "btn-download " + variant : "btn-download";
    primary.id          = "modal-primary-btn";
    primary.textContent = label;
    if (disabled) primary.disabled = true;
    if (click && !disabled) primary.addEventListener("click", click);
    const secondary = document.createElement("button");
    secondary.className   = "btn-share";
    secondary.id          = "modal-secondary-btn";
    secondary.textContent = "Close";
    secondary.addEventListener("click", closeModal);
    modalEl.actions.appendChild(primary);
    modalEl.actions.appendChild(secondary);
  }

  async function submitDownload(payload) {
    const key = addKey(payload.media_type, payload.tmdb_id);
    if (adding.has(key)) return;
    adding.add(key);
    const btn = document.getElementById("modal-primary-btn");
    btn.disabled    = true;
    btn.textContent = "Adding…";
    /* The modal may be closed, or reopened, before the reply: act on the
       button that is on screen now, and only if it is this title's. */
    const showing = function() {
      return !!currentDetail && addKey(currentDetail.media_type, currentDetail.tmdb_id) === key;
    };
    try {
      await MM.api.post("/api/search/download", payload);
      adding.delete(key);
      added.add(key);
      const shown = showing();
      const shownIn = openCount;
      if (shown) {
        const cur = document.getElementById("modal-primary-btn");
        cur.textContent = "Added ✓";
        cur.classList.add("is-added");
      }
      setTimeout(function() {
        if (shown && shownIn === openCount) closeModal();
        if (_refreshCurrentView) _refreshCurrentView();
      }, 1200);
    } catch (err) {
      adding.delete(key);
      if (showing()) {
        /* Re-drawing the buttons drops focus from either of them. */
        const wasOnClose = document.activeElement === document.getElementById("modal-secondary-btn");
        /* A click on something that cannot take focus sends it behind the dialog. */
        const refocus = MM.dom.focusStillIn(document.getElementById("modal-primary-btn")) ||
          !modal.contains(document.activeElement);
        renderPrimaryButton(currentDetail);
        if (refocus) document.getElementById("modal-primary-btn").focus();
        else if (wasOnClose) document.getElementById("modal-secondary-btn").focus();
      }
      const reason = MM.api.messageOf(err, "Something went wrong. Try again.");
      if (window.UIFeedback) {
        window.UIFeedback.error(("Could not add " + payload.title + ": " + reason).slice(0, 200));
      }
    }
  }

  function init({ refreshCurrentView }) {
    _refreshCurrentView = refreshCurrentView;
  }

  MM.search.detail = {
    init,
    openDetail,
  };
})();
