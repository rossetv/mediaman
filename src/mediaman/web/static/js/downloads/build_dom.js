/**
 * downloads/build_dom.js — DOM helpers and placeholder builders.
 *
 * Pure DOM construction — no fetches, no event listeners. Other download
 * modules consume these to either build a fresh card or look up an
 * existing one to patch in place.
 *
 * Exposes:
 *   MM.downloads.buildDom.q(sel, ctx)
 *   MM.downloads.buildDom.setText(el, txt)
 *   MM.downloads.buildDom.findByDlId(container, dlId)
 *   MM.downloads.buildDom.findByEp(container, label)
 *   MM.downloads.buildDom.stateLabel(state)
 *   MM.downloads.buildDom.buildHeroPlaceholder(id)
 *   MM.downloads.buildDom.buildRecentItem(r)
 *   MM.downloads.buildDom.buildEmptyState()
 *   MM.downloads.buildDom.buildUpcomingRow(item)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  function q(sel, ctx) { return (ctx || document).querySelector(sel); }
  function setText(el, txt) { if (el && el.textContent !== txt) el.textContent = txt; }

  /* Find element by data-dl-id without selector injection. */
  function findByDlId(container, dlId) {
    if (!container) return null;
    var els = container.querySelectorAll('[data-dl-id]');
    for (var i = 0; i < els.length; i++) {
      if (els[i].getAttribute('data-dl-id') === dlId) return els[i];
    }
    return null;
  }

  /* Find element by data-ep without selector injection. */
  function findByEp(container, label) {
    if (!container) return null;
    var rows = container.querySelectorAll('[data-ep]');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute('data-ep') === label) return rows[i];
    }
    return null;
  }

  function stateLabel(state) {
    if (state === 'searching') return 'Looking for the best version';
    if (state === 'downloading') return 'Downloading';
    if (state === 'almost_ready') return 'Almost ready';
    if (state === 'ready') return 'Ready to watch';
    if (state === 'upcoming') return '';
    return '';
  }

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

  /* Build hero placeholder safely. */
  function buildHeroPlaceholder(id) {
    var card = document.createElement('div');
    card.className = 'dl-hero dl-card-enter';
    card.setAttribute('data-dl-id', id);

    var bg = document.createElement('div');
    bg.className = 'dl-hero-bg';
    card.appendChild(bg);

    var overlay = document.createElement('div');
    overlay.className = 'dl-hero-overlay';
    card.appendChild(overlay);

    var content = document.createElement('div');
    content.className = 'dl-hero-content';

    var poster = document.createElement('div');
    poster.className = 'dl-hero-poster';
    content.appendChild(poster);

    var info = document.createElement('div');
    info.className = 'dl-hero-info';

    var title = document.createElement('div');
    title.className = 'dl-hero-title';
    info.appendChild(title);

    var status = document.createElement('div');
    status.className = 'dl-hero-status';
    var pill = document.createElement('span');
    pill.className = 'dl-state-pill';
    status.appendChild(pill);
    info.appendChild(status);

    /* Empty hint container — updateSearchHint fills it in on first poll. */
    var hint = document.createElement('div');
    hint.className = 'dl-search-hint';
    hint.setAttribute('data-v', 'search-hint');
    hint.style.display = 'none';
    info.appendChild(hint);

    var progress = document.createElement('div');
    progress.className = 'dl-hero-progress';
    var bar = document.createElement('div');
    bar.className = 'dl-hero-bar';
    var fill = document.createElement('div');
    fill.className = 'dl-hero-fill';
    fill.setAttribute('data-v', 'fill');
    fill.style.width = '0%';
    bar.appendChild(fill);
    progress.appendChild(bar);

    var details = document.createElement('div');
    details.className = 'dl-hero-details';
    var pctWrap = document.createElement('span');
    var pct = document.createElement('span');
    pct.className = 'dl-hero-pct';
    pct.setAttribute('data-v', 'pct');
    pct.textContent = '0%';
    pctWrap.appendChild(pct);
    details.appendChild(pctWrap);
    var eta = document.createElement('span');
    eta.setAttribute('data-v', 'eta');
    details.appendChild(eta);
    progress.appendChild(details);
    info.appendChild(progress);

    content.appendChild(info);
    card.appendChild(content);

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
    q: q,
    setText: setText,
    findByDlId: findByDlId,
    findByEp: findByEp,
    stateLabel: stateLabel,
    buildHeroPlaceholder: buildHeroPlaceholder,
    buildRecentItem: buildRecentItem,
    buildEmptyState: buildEmptyState,
    buildUpcomingRow: buildUpcomingRow,
  };
})();
