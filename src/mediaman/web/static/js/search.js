/**
 * search.js — discovery shelves, search debounce, and the title-detail modal
 * for the /search page.
 *
 * Extracted from search.html so the page no longer relies on an inline
 * <script> block, allowing CSP to drop 'unsafe-inline' for scripts.
 *
 * The hand-rolled escapeHtml() and DOM-via-template-literals construction
 * pattern below are preserved as-is from the inline original; refactoring to
 * pure DOM construction is a separate piece of work.
 */
(() => {
  // ===== Helpers =====
  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function escapeAttr(value) { return escapeHtml(value); }

  const shelves = document.getElementById("shelves");
  const input = document.getElementById("search-input");
  const wrap = document.getElementById("search-wrap");
  const modal = document.getElementById("detail-modal");

  // ===== GridRenderer =====
  function metaLine(item) {
    const parts = [];
    if (item.year) parts.push(item.year);
    parts.push(item.media_type === "movie" ? "Movie" : "TV");
    return parts.join(" · ");
  }

  function buildShelf(label, items, { showCount = false } = {}) {
    /* Poster grid is now rendered by MM.tiles.render — DOM-safe by
       construction (no innerHTML, no template-literal escaping). The
       section header is built by hand because tiles.render doesn't own
       the surrounding container. */
    const section = document.createElement("section");
    section.className = "shelf";

    const labelWrap = document.createElement("div");
    labelWrap.className = "section-label";
    const h = document.createElement("h2");
    h.textContent = label;
    labelWrap.appendChild(h);
    if (showCount) {
      const countText = `${items.length} ${items.length === 1 ? "title" : "titles"}`;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = countText;
      labelWrap.appendChild(count);
    }
    section.appendChild(labelWrap);

    const grid = document.createElement("div");
    grid.className = "poster-grid";
    section.appendChild(grid);

    MM.tiles.render(grid, items, {
      onClick: (item) => openDetail(item.media_type, Number(item.tmdb_id)),
    });
    return section;
  }

  function renderEmpty(message) {
    shelves.replaceChildren();
    const msg = document.createElement("div");
    msg.className = "search-empty";
    msg.textContent = message;
    shelves.appendChild(msg);
  }

  function renderDiscover(data) {
    shelves.replaceChildren();
    const rows = [
      ["Trending this week", data.trending || []],
      ["Popular movies", data.popular_movies || []],
      ["Popular TV", data.popular_tv || []],
    ];
    let rendered = 0;
    for (const [label, items] of rows) {
      if (!items.length) continue;
      shelves.appendChild(buildShelf(label, items));
      rendered += 1;
    }
    if (!rendered) renderEmpty("Couldn't load discovery shelves.");
  }

  function renderSearchResults(query, items) {
    if (!items.length) {
      renderEmpty("No results.");
      return;
    }
    shelves.replaceChildren();
    shelves.appendChild(buildShelf(`Results for "${query}"`, items, { showCount: true }));
  }

  // ===== SearchController =====
  let reqGen = 0;
  let abort = null;
  let debounceTimer = null;

  async function doFetch(url, onData) {
    const myGen = ++reqGen;
    if (abort) abort.abort();
    abort = new AbortController();
    wrap.classList.add("loading");
    try {
      const data = await MM.api.get(url, { signal: abort.signal });
      if (myGen !== reqGen) return;
      onData(data);
    } catch (e) {
      /* Native fetch throws AbortError when the signal aborts; MM.api
         lets it propagate so we can distinguish "user typed another key"
         from "actually failed". */
      if (e.name === "AbortError") return;
      renderEmpty("Couldn't load results.");
    } finally {
      if (myGen === reqGen) wrap.classList.remove("loading");
    }
  }

  function loadDiscover() {
    doFetch("/api/search/discover", renderDiscover);
  }

  function loadSearch(q) {
    doFetch(`/api/search?q=${encodeURIComponent(q)}`, (data) => {
      renderSearchResults(q, data.results || []);
    });
  }

  function refreshCurrentView() {
    const q = input.value.trim();
    if (q === "") loadDiscover();
    else loadSearch(q);
  }

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q === "") { loadDiscover(); return; }
    if (q.length < 2) return;
    debounceTimer = setTimeout(() => loadSearch(q), 300);
  });

  // ===== DetailModal =====
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

  let currentDetail = null;
  let seasonTickState = new Map();
  let lockedSeasons = new Set();

  /* ── Detail-modal lifecycle (MM.modal.setupDetail).
       setupDetail owns the display:flex/none + aria-hidden + body
       overflow + ModalA11y registration. The close-bar button uses
       [data-close-modal] which is wired automatically. */
  const detailModal = MM.modal.setupDetail(modal);
  function closeModal() { detailModal.close(); }

  function resetModal() {
    modalEl.hero.innerHTML = "";
    modalEl.quick.innerHTML = "";
    modalEl.ratings.innerHTML = "";
    modalEl.tagline.textContent = "";
    modalEl.desc.textContent = "";
    if (modalEl.reason) modalEl.reason.style.display = "none";
    modalEl.trailerL.style.display = "none";
    modalEl.trailer.innerHTML = "";
    if (modalEl.trailerF) modalEl.trailerF.innerHTML = "";
    modalEl.castL.style.display = "none";
    modalEl.cast.innerHTML = "";
    modalEl.seasonsL.style.display = "none";
    modalEl.seasons.style.display = "none";
    modalEl.seasons.innerHTML = "";
    modalEl.actions.innerHTML = "";
  }

  async function openDetail(mediaType, tmdbId) {
    detailModal.open();
    resetModal();
    const loading = document.createElement("div");
    loading.className = "modal-loading";
    loading.textContent = "Loading…";
    modalEl.actions.appendChild(loading);

    let data;
    try {
      data = await MM.api.get(`/api/search/detail/${encodeURIComponent(mediaType)}/${encodeURIComponent(tmdbId)}`);
    } catch (e) {
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
    currentDetail = data;
    renderDetail(data);
  }

  function renderDetail(d) {
    if (d.backdrop_url || d.poster_url) {
      const img = d.backdrop_url || d.poster_url;
      modalEl.hero.innerHTML = `
        <img src="${escapeAttr(img)}" alt="">
        <div class="detail-modal-hero-gradient"></div>
        <div class="detail-modal-hero-info">
          <h2 id="detail-modal-title" class="detail-modal-hero-title">${escapeHtml(d.title)}${d.year ? ` <span class="year">${escapeHtml(d.year)}</span>` : ""}</h2>
        </div>
      `;
    }

    const genres = (d.genres || []).join(" · ");
    const runtime = d.runtime ? `${d.runtime} min` : "";
    const quickBits = [
      `<span class="type-pill">${d.media_type === "movie" ? "Movie" : "TV Series"}</span>`,
      genres ? `<span class="meta-text">${escapeHtml(genres)}</span>` : "",
      runtime ? `<span class="meta-dot">·</span><span class="meta-text">${escapeHtml(runtime)}</span>` : "",
      d.director ? `<span class="meta-dot">·</span><span class="meta-text">${escapeHtml(d.director)}</span>` : "",
    ].filter(Boolean).join("");
    modalEl.quick.innerHTML = quickBits;

    const STAR = '<span class="rating-icon" aria-hidden="true">★</span>';
    const TOMATO = '<span class="rating-icon" aria-hidden="true">🍅</span>';
    const ratings = [];
    if (d.rating_tmdb)      ratings.push(`<span class="rating-pill r-tmdb">${STAR}TMDB ${escapeHtml(d.rating_tmdb)}</span>`);
    if (d.rating_imdb)      ratings.push(`<span class="rating-pill r-imdb">${STAR}IMDb ${escapeHtml(d.rating_imdb)}</span>`);
    if (d.rating_rt)        ratings.push(`<span class="rating-pill r-rt">${TOMATO}RT ${escapeHtml(d.rating_rt)}</span>`);
    if (d.rating_metascore) ratings.push(`<span class="rating-pill r-meta">${STAR}Metascore ${escapeHtml(d.rating_metascore)}</span>`);
    modalEl.ratings.innerHTML = ratings.join("");

    modalEl.tagline.textContent = d.tagline ? `"${d.tagline}"` : "";
    modalEl.desc.textContent = d.description || "";

    /* Finding 19: validate key is exactly 11 URL-safe base64 chars before building iframe. */
    if (d.trailer_key && /^[A-Za-z0-9_-]{11}$/.test(d.trailer_key)) {
      modalEl.trailerL.style.display = "";
      /* Build iframe via DOM to avoid any innerHTML injection. */
      const iframe = document.createElement('iframe');
      iframe.src = 'https://www.youtube.com/embed/' + d.trailer_key + '?rel=0';
      iframe.setAttribute('allowfullscreen', '');
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
      iframe.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
      iframe.setAttribute('referrerpolicy', 'strict-origin');
      iframe.setAttribute('loading', 'lazy');
      while (modalEl.trailer.firstChild) modalEl.trailer.removeChild(modalEl.trailer.firstChild);
      modalEl.trailer.appendChild(iframe);
      modalEl.trailer.style.display = "";
    }

    if (d.cast && d.cast.length) {
      modalEl.castL.style.display = "";
      modalEl.cast.innerHTML = d.cast.map(c => {
        const initials = (c.name || "?").split(" ").map(x => x[0]).slice(0, 2).join("");
        const avatar = c.profile_url
          ? `<div class="cast-avatar"><img src="${escapeAttr(c.profile_url)}" alt=""></div>`
          : `<div class="cast-avatar">${escapeHtml(initials)}</div>`;
        return `<div class="cast-item">${avatar}<div class="cast-name">${escapeHtml(c.name || "")}</div><div class="cast-char">${escapeHtml(c.character || "")}</div></div>`;
      }).join("");
    }

    if (d.media_type === "tv" && d.seasons) {
      renderSeasonPicker(d.seasons);
    }

    renderPrimaryButton(d);
  }

  function renderSeasonPicker(seasons) {
    modalEl.seasonsL.style.display = "";
    modalEl.seasons.style.display = "";
    seasonTickState = new Map();
    lockedSeasons = new Set();
    for (const s of seasons) {
      if (s.in_library) lockedSeasons.add(s.season_number);
      else seasonTickState.set(s.season_number, true);
    }
    modalEl.seasons.innerHTML = `
      <div class="detail-modal-seasons-head">
        <div class="detail-modal-seasons-title">Seasons — pick which to download</div>
        <div>
          <button type="button" class="detail-modal-seasons-link" data-action="all">Select all</button>
          <button type="button" class="detail-modal-seasons-link detail-modal-seasons-link--clear" data-action="clear">Clear</button>
        </div>
      </div>
      <div class="detail-modal-seasons-grid">
        ${seasons.map(s => {
          const locked = s.in_library;
          const subBits = [`${s.episode_count || 0} ep`];
          if (locked) subBits.push("In library");
          else if (s.year) subBits.push(String(s.year));
          return `
            <div class="detail-modal-season ${locked ? "already" : "checked"}" data-season="${escapeAttr(s.season_number)}">
              <div class="detail-modal-season-check"></div>
              <div>
                <div class="detail-modal-season-name">${escapeHtml(s.name || "Season " + s.season_number)}</div>
                <div class="detail-modal-season-sub">${escapeHtml(subBits.join(" · "))}</div>
              </div>
            </div>
          `;
        }).join("")}
      </div>
      <div class="detail-modal-season-summary" id="detail-season-summary"></div>
    `;
    modalEl.seasons.querySelectorAll(".detail-modal-season").forEach(el => {
      const num = Number(el.dataset.season);
      if (lockedSeasons.has(num)) return;
      el.addEventListener("click", () => {
        const current = seasonTickState.get(num);
        seasonTickState.set(num, !current);
        el.classList.toggle("checked", !current);
        updateSeasonSummary();
        renderPrimaryButton(currentDetail);
      });
    });
    modalEl.seasons.querySelector('[data-action="all"]').addEventListener("click", () => {
      for (const k of seasonTickState.keys()) seasonTickState.set(k, true);
      refreshSeasonUI();
    });
    modalEl.seasons.querySelector('[data-action="clear"]').addEventListener("click", () => {
      for (const k of seasonTickState.keys()) seasonTickState.set(k, false);
      refreshSeasonUI();
    });
    updateSeasonSummary();
  }

  function refreshSeasonUI() {
    modalEl.seasons.querySelectorAll(".detail-modal-season").forEach(el => {
      const num = Number(el.dataset.season);
      if (lockedSeasons.has(num)) return;
      el.classList.toggle("checked", !!seasonTickState.get(num));
    });
    updateSeasonSummary();
    renderPrimaryButton(currentDetail);
  }

  function updateSeasonSummary() {
    const ticked = [...seasonTickState.entries()].filter(([, v]) => v).length;
    const already = lockedSeasons.size;
    const summary = document.getElementById("detail-season-summary");
    if (!summary) return;
    summary.textContent = "";
    if (ticked) {
      const s1 = document.createElement("strong");
      s1.textContent = `${ticked} ${ticked === 1 ? "season" : "seasons"}`;
      summary.appendChild(s1);
      summary.appendChild(document.createTextNode(" will be added and searched"));
    }
    if (already) {
      if (ticked) summary.appendChild(document.createTextNode(" · "));
      const s2 = document.createElement("strong");
      s2.textContent = `${already} ${already === 1 ? "season" : "seasons"}`;
      summary.appendChild(s2);
      summary.appendChild(document.createTextNode(" already in library"));
    }
  }

  function renderPrimaryButton(d) {
    const tickedSeasons = [...seasonTickState.entries()].filter(([, v]) => v).map(([k]) => k);
    let label = "Download";
    let variant = "";
    let disabled = false;
    let click = null;

    if (d.media_type === "movie") {
      if (d.download_state === "in_library") { label = "In Library ✓"; variant = "ready"; disabled = true; }
      else if (d.download_state === "downloading") { label = "Downloading"; variant = "queued"; disabled = true; }
      else if (d.download_state === "queued") { label = "Queued"; variant = "queued"; disabled = true; }
      else click = () => submitDownload({media_type: "movie", tmdb_id: d.tmdb_id, title: d.title});
    } else {
      const allLocked = lockedSeasons.size > 0 && seasonTickState.size === 0;
      if (d.sonarr_tracked) { label = "Already tracked — manage in Library"; variant = "ready"; disabled = true; }
      else if (allLocked) { label = "All seasons in library ✓"; variant = "ready"; disabled = true; }
      else if (tickedSeasons.length === 0) { label = "Pick a season"; disabled = true; }
      else {
        label = `Download ${tickedSeasons.length} ${tickedSeasons.length === 1 ? "season" : "seasons"}`;
        click = () => submitDownload({
          media_type: "tv", tmdb_id: d.tmdb_id, title: d.title,
          monitored_seasons: [...tickedSeasons, ...lockedSeasons],
          search_seasons: tickedSeasons,
        });
      }
    }

    modalEl.actions.innerHTML = "";
    const primary = document.createElement("button");
    primary.className = variant ? `btn-download ${variant}` : "btn-download";
    primary.id = "modal-primary-btn";
    primary.textContent = label;
    if (disabled) primary.disabled = true;
    if (click && !disabled) primary.addEventListener("click", click);
    const secondary = document.createElement("button");
    secondary.className = "btn-share";
    secondary.id = "modal-secondary-btn";
    secondary.textContent = "Close";
    secondary.addEventListener("click", closeModal);
    modalEl.actions.appendChild(primary);
    modalEl.actions.appendChild(secondary);
  }

  async function submitDownload(payload) {
    const btn = document.getElementById("modal-primary-btn");
    btn.disabled = true;
    btn.textContent = "Adding…";
    try {
      await MM.api.post("/api/search/download", payload);
      btn.textContent = "Added ✓";
      btn.style.background = "rgba(48,209,88,0.2)";
      btn.style.color = "#30d158";
      setTimeout(() => { closeModal(); refreshCurrentView(); }, 1200);
    } catch (err) {
      if (err instanceof MM.api.APIError) {
        btn.textContent = err.message || "Failed";
      } else {
        btn.textContent = "Network error";
      }
      btn.style.background = "rgba(255,69,58,0.2)";
      btn.style.color = "#ff453a";
    }
  }

  loadDiscover();
})();
