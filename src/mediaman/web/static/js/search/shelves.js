/**
 * search/shelves.js — shelf rendering, search-result lists, and the
 * discovery/search fetch loop for the /search page.
 *
 * Owns: escapeHtml/escapeAttr helpers, buildShelf, renderEmpty,
 * renderDiscover, renderSearchResults, doFetch, loadDiscover, loadSearch,
 * refreshCurrentView, and the debounced input listener.
 *
 * Cross-module dependencies:
 *   MM.tiles.render      — poster-grid tile rendering
 *   MM.search.detail     — openDetail callback (injected via init)
 *
 * Exposes:
 *   MM.search.shelves.init({ openDetail })
 *   MM.search.shelves.refreshCurrentView()
 */
(() => {
  'use strict';

  window.MM = window.MM || {};
  MM.search = MM.search || {};

  // ===== HTML-escaping helpers =====
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

  // ===== DOM refs =====
  const shelves = document.getElementById("shelves");
  const input   = document.getElementById("search-input");
  const wrap    = document.getElementById("search-wrap");

  // ===== Shelf / grid rendering =====
  function buildShelf(label, items, { showCount = false } = {}, openDetail) {
    /* Poster grid is rendered by MM.tiles.render — DOM-safe by construction
       (no innerHTML, no template-literal escaping). The section header is
       built by hand because tiles.render doesn't own the surrounding container. */
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

  function renderDiscover(data, openDetail) {
    shelves.replaceChildren();
    const rows = [
      ["Trending this week", data.trending || []],
      ["Popular movies",     data.popular_movies || []],
      ["Popular TV",         data.popular_tv || []],
    ];
    let rendered = 0;
    for (const [label, items] of rows) {
      if (!items.length) continue;
      shelves.appendChild(buildShelf(label, items, {}, openDetail));
      rendered += 1;
    }
    if (!rendered) renderEmpty("Couldn't load discovery shelves.");
  }

  function renderSearchResults(query, items, openDetail) {
    if (!items.length) {
      renderEmpty("No results.");
      return;
    }
    shelves.replaceChildren();
    shelves.appendChild(
      buildShelf(`Results for "${query}"`, items, { showCount: true }, openDetail),
    );
  }

  // ===== Fetch controller =====
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

  // These are populated at init time with the injected openDetail callback.
  let _openDetail = null;

  function loadDiscover() {
    doFetch("/api/search/discover", (data) => renderDiscover(data, _openDetail));
  }

  function loadSearch(q) {
    doFetch(`/api/search?q=${encodeURIComponent(q)}`, (data) => {
      renderSearchResults(q, data.results || [], _openDetail);
    });
  }

  function refreshCurrentView() {
    const q = input.value.trim();
    if (q === "") loadDiscover();
    else loadSearch(q);
  }

  // ===== Init =====
  function init({ openDetail }) {
    _openDetail = openDetail;

    input.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      const q = input.value.trim();
      if (q === "") { loadDiscover(); return; }
      if (q.length < 2) return;
      debounceTimer = setTimeout(() => loadSearch(q), 300);
    });

    loadDiscover();
  }

  MM.search.shelves = {
    init,
    refreshCurrentView,
    /* expose escapeHtml for use by detail_modal.js */
    escapeHtml,
    escapeAttr,
  };
})();
