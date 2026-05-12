/**
 * downloads/build_dom.js — DOM helpers and placeholder builders.
 *
 * Pure DOM construction — no fetches, no event listeners. Other download
 * modules consume these to either build a fresh card or look up an
 * existing one to patch in place.
 *
 * Thin aliases q, setText, and findByDlId delegate to MM.dom to avoid
 * duplicating logic already provided by core/dom.js (CODE_GUIDELINES §1).
 *
 * Exposes:
 *   MM.downloads.buildDom.q(sel, ctx)              → MM.dom.q
 *   MM.downloads.buildDom.setText(el, txt)         → MM.dom.setText
 *   MM.downloads.buildDom.findByDlId(c, dlId)      → MM.dom.findByAttr(c, 'data-dl-id', dlId)
 *   MM.downloads.buildDom.findByEp(container, label)
 *   MM.downloads.buildDom.buildHero(item)          shared hero card builder
 *   MM.downloads.buildDom.buildHeroPlaceholder(id) thin wrapper around buildHero
 *   MM.downloads.buildDom.buildRecentItem(r)
 *   MM.downloads.buildDom.buildEmptyState()
 *   MM.downloads.buildDom.buildUpcomingRow(item)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  /* Find element by data-ep without selector injection. */
  function findByEp(container, label) {
    if (!container) return null;
    var rows = container.querySelectorAll('[data-ep]');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute('data-ep') === label) return rows[i];
    }
    return null;
  }

  /* State labels come from the server (item.state_label). The canonical map
     lives in services/downloads/download_format/_types.py. */

  /* Build a recent item element safely. */
  function buildRecentItem(r) {
    var item = document.createElement('div');
    item.className = 'dl-recent-item';

    var poster = document.createElement('div');
    poster.className = 'dl-recent-poster';
    if (r.poster_url) {
      var img = document.createElement('img');
      img.src = r.poster_url;
      img.alt = '';
      poster.appendChild(img);
    }
    var badge = document.createElement('div');
    badge.className = 'dl-recent-badge';
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', '#fff');
    svg.setAttribute('stroke-width', '3');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    var polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    polyline.setAttribute('points', '20 6 9 17 4 12');
    svg.appendChild(polyline);
    badge.appendChild(svg);
    poster.appendChild(badge);
    item.appendChild(poster);

    var title = document.createElement('div');
    title.className = 'dl-recent-title';
    title.textContent = r.title;
    item.appendChild(title);

    var sub = document.createElement('div');
    sub.className = 'dl-recent-sub';
    sub.textContent = 'Ready to watch';
    item.appendChild(sub);

    return item;
  }

  /* Build empty state element safely. */
  function buildEmptyState() {
    var el = document.createElement('div');
    el.className = 'dl-empty-message';
    el.id = 'dl-empty';

    var icon = document.createElement('div');
    icon.className = 'empty-state__icon';
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '28');
    svg.setAttribute('height', '28');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.5');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4');
    svg.appendChild(path);
    var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    poly.setAttribute('points', '7 10 12 15 17 10');
    svg.appendChild(poly);
    var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', '12');
    line.setAttribute('y1', '15');
    line.setAttribute('x2', '12');
    line.setAttribute('y2', '3');
    svg.appendChild(line);
    icon.appendChild(svg);
    el.appendChild(icon);

    var p = document.createElement('p');
    p.textContent = 'All caught up — nothing downloading right now.';
    el.appendChild(p);

    return el;
  }

  /**
   * Build a hero card DOM element.
   *
   * Shared builder used by both the per-token download page (download.js)
   * and the downloads list page (buildHeroPlaceholder). Produces the full
   * .dl-hero tree with all data-v hooks required by render_hero.js and
   * download.js polling.
   *
   * @param {Object|null} item  Download item. When null/falsy a blank card is
   *   returned (all text empty, progress at 0%, state pill unstyled). Pass a
   *   real item to pre-populate title, poster, state pill, and progress.
   *   Shape: { id, title, poster_url, state, state_label, progress, eta }
   * @returns {HTMLElement}  The .dl-hero div — not yet attached to the DOM.
   */
  function buildHero(item) {
    item = item || {};
    var bgUrl = item.poster_url || '';
    var state  = item.state || '';

    /* Root .dl-hero container */
    var card = document.createElement('div');
    card.className = 'dl-hero dl-card-enter';
    if (item.id) card.setAttribute('data-dl-id', item.id);

    /* Background — set via DLPoster.apply (H67: avoids CSS string injection). */
    var bg = document.createElement('div');
    bg.className = 'dl-hero-bg';
    if (bgUrl) bg.setAttribute('data-bg-url', bgUrl);
    card.appendChild(bg);
    if (bgUrl && window.DLPoster) window.DLPoster.apply(bg);

    /* Overlay */
    var overlay = document.createElement('div');
    overlay.className = 'dl-hero-overlay';
    card.appendChild(overlay);

    /* Content wrapper */
    var content = document.createElement('div');
    content.className = 'dl-hero-content';

    /* Poster */
    var posterWrap = document.createElement('div');
    posterWrap.className = 'dl-hero-poster';
    if (bgUrl) {
      var posterImg = document.createElement('img');
      posterImg.src = bgUrl;
      posterImg.alt = '';
      posterWrap.appendChild(posterImg);
    } else {
      var posterPh = document.createElement('div');
      posterPh.className = 'dl-hero-poster-placeholder';
      posterWrap.appendChild(posterPh);
    }
    content.appendChild(posterWrap);

    /* Info section */
    var info = document.createElement('div');
    info.className = 'dl-hero-info';

    var titleEl = document.createElement('div');
    titleEl.className = 'dl-hero-title';
    if (item.title) titleEl.textContent = item.title;
    info.appendChild(titleEl);

    /* State pill */
    var statusWrap = document.createElement('div');
    statusWrap.className = 'dl-hero-status';
    var pill = document.createElement('span');
    pill.className = 'dl-state-pill' + (state ? ' dl-state-' + state : '');
    pill.setAttribute('data-v', 'pill');
    if (item.state_label || state) pill.textContent = item.state_label || state;
    statusWrap.appendChild(pill);
    info.appendChild(statusWrap);

    /* Search hint — hidden by default; updateSearchHint fills it on each poll. */
    var searchHint = document.createElement('div');
    searchHint.className = 'dl-search-hint';
    searchHint.setAttribute('data-v', 'search-hint');
    searchHint.style.display = 'none';
    info.appendChild(searchHint);

    /* Progress wrap (hidden entirely while searching) */
    var progressWrap = document.createElement('div');
    progressWrap.className = 'dl-hero-progress';
    progressWrap.setAttribute('data-v', 'progress-wrap');
    if (state === 'searching') progressWrap.style.display = 'none';

    var bar = document.createElement('div');
    bar.className = 'dl-hero-bar';
    var fill = document.createElement('div');
    fill.className = 'dl-hero-fill';
    fill.setAttribute('data-v', 'fill');
    fill.style.width = (item.progress || 0) + '%';
    bar.appendChild(fill);
    progressWrap.appendChild(bar);

    var details = document.createElement('div');
    details.className = 'dl-hero-details';
    var pctWrap = document.createElement('span');
    var pct = document.createElement('span');
    pct.className = 'dl-hero-pct';
    pct.setAttribute('data-v', 'pct');
    pct.textContent = (item.progress || 0) + '%';
    pctWrap.appendChild(pct);
    details.appendChild(pctWrap);
    var eta = document.createElement('span');
    eta.setAttribute('data-v', 'eta');
    if (item.eta) eta.textContent = item.eta;
    details.appendChild(eta);
    progressWrap.appendChild(details);
    info.appendChild(progressWrap);

    content.appendChild(info);
    card.appendChild(content);

    return card;
  }

  /* Thin wrapper — builds a blank hero card pre-tagged with the given dl-id.
     The downloads list page uses this to insert a card before the first poll
     returns data; render_hero.js then patches it in place. */
  function buildHeroPlaceholder(id) {
    var card = buildHero(null);
    card.setAttribute('data-dl-id', id);
    return card;
  }

  /* Build upcoming row element. */
  function buildUpcomingRow(item) {
    var row = document.createElement('div');
    row.className = 'dl-compact-row dl-row dl-upcoming-row dl-row--has-abandon dl-card-enter';
    row.setAttribute('data-dl-id', item.id);

    var poster = document.createElement('div');
    poster.className = 'dl-compact-poster dl-row-poster';
    if (item.poster_url) {
      var img = document.createElement('img');
      img.src = item.poster_url;
      img.alt = '';
      poster.appendChild(img);
    }
    row.appendChild(poster);

    var info = document.createElement('div');
    info.className = 'dl-compact-info dl-row-info';

    var title = document.createElement('div');
    title.className = 'dl-compact-title dl-row-title';
    title.textContent = item.title;
    info.appendChild(title);

    var meta = document.createElement('div');
    meta.className = 'dl-compact-meta dl-row-meta';
    var pill = document.createElement('span');
    pill.className = 'dl-state-pill dl-state-pill--xs dl-state-upcoming';
    pill.setAttribute('data-v', 'release-label');
    pill.textContent = item.release_label || '';
    meta.appendChild(pill);
    info.appendChild(meta);

    row.appendChild(info);

    var scheduled = document.createElement('span');
    scheduled.className = 'pill pill--neutral';
    scheduled.textContent = 'Scheduled';
    row.appendChild(scheduled);

    var abandonWrap = document.createElement('div');
    abandonWrap.className = 'dl-row-abandon';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--icon btn--ghost';
    btn.setAttribute('data-abandon-trigger', '');
    btn.setAttribute('data-abandon-upcoming', '1');
    btn.setAttribute('data-dl-id', item.id);
    btn.setAttribute('data-kind', item.media_type === 'movie' ? 'movie' : 'series');
    btn.setAttribute('data-title', item.title || '');
    btn.setAttribute('data-stuck-seasons', '[]');
    btn.setAttribute('aria-label', 'Stop tracking');
    btn.setAttribute('title', 'Stop tracking');
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M6 6l12 12M18 6L6 18');
    svg.appendChild(path);
    btn.appendChild(svg);
    abandonWrap.appendChild(btn);
    row.appendChild(abandonWrap);

    return row;
  }

  MM.downloads.buildDom = {
    q:                    function (sel, ctx)        { return MM.dom.q(sel, ctx); },
    setText:              function (el, txt)         { return MM.dom.setText(el, txt); },
    findByDlId:           function (container, dlId) { return MM.dom.findByAttr(container, 'data-dl-id', dlId); },
    findByEp:             findByEp,
    buildHero:            buildHero,
    buildHeroPlaceholder: buildHeroPlaceholder,
    buildRecentItem:      buildRecentItem,
    buildEmptyState:      buildEmptyState,
    buildUpcomingRow:     buildUpcomingRow,
  };
})();
