/**
 * downloads/build_dom.js — DOM helpers and placeholder builders.
 *
 * Pure DOM construction — no fetches, no event listeners. Other download
 * modules consume these to either build a fresh card or look up an
 * existing one to patch in place.
 *
 * Thin aliases q, setText, and findByDlId delegate to MM.dom to avoid
 * duplicating logic already provided by core/dom.js.
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
 *   MM.downloads.buildDom.buildCompactRow(item)    queue row for an item the page has not seen
 *   MM.downloads.buildDom.syncAbandon(card, item, kind)  show, hide or refresh the abandon button
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
    var badgeIcon = document.createElement('i');
    badgeIcon.className = 'fa-solid fa-check';
    badgeIcon.setAttribute('aria-hidden', 'true');
    badge.appendChild(badgeIcon);
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

  /* Build the "nothing downloading" empty state.

     Mirrors the c.empty() macro markup (.empty > .empty-ico + h3 + p) and
     the download-tray glyph defined in downloads.html, so the poll-built
     card is identical to the server-rendered one. The #dl-empty-container
     owns the id — this builder returns only the inner card. */
  function buildEmptyState() {
    var el = document.createElement('div');
    el.className = 'empty';

    var icon = document.createElement('div');
    icon.className = 'empty-ico';
    var iconEl = document.createElement('i');
    iconEl.className = 'fa-solid fa-download';
    iconEl.setAttribute('aria-hidden', 'true');
    icon.appendChild(iconEl);
    el.appendChild(icon);

    var h3 = document.createElement('h3');
    h3.textContent = 'All caught up';
    el.appendChild(h3);

    var p = document.createElement('p');
    p.textContent = 'Nothing downloading right now.';
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
  function buildEtaStat(text) {
    var wrap = document.createElement('span');
    wrap.appendChild(document.createTextNode('ETA '));
    var eta = document.createElement('b');
    eta.setAttribute('data-v', 'eta');
    eta.textContent = text;
    wrap.appendChild(eta);
    return wrap;
  }

  function buildHero(item) {
    item = item || {};
    var bgUrl = item.poster_url || '';
    var state  = item.state || '';

    /* Root .dl-hero container */
    var card = document.createElement('div');
    card.className = 'dl-hero dl-card-enter';
    if (item.id) card.setAttribute('data-dl-id', item.id);

    /* Background — set via DLPoster.apply (avoids CSS string injection). */
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
    bar.className = 'dl-hero-bar dl-bar';
    var fill = document.createElement('div');
    fill.className = 'dl-hero-fill dl-bar-fill' + (state === 'almost_ready' ? ' green' : '');
    fill.setAttribute('data-v', 'fill');
    fill.style.width = (item.progress || 0) + '%';
    bar.appendChild(fill);
    progressWrap.appendChild(bar);

    /* Same stats line as _dl_hero_card.html: "N% complete", "done of total", "ETA x". */
    var details = document.createElement('div');
    details.className = 'dl-hero-details dl-stats';
    var pctWrap = document.createElement('span');
    var pct = document.createElement('b');
    pct.className = 'dl-hero-pct';
    pct.setAttribute('data-v', 'pct');
    pct.textContent = (item.progress || 0) + '%';
    pctWrap.appendChild(pct);
    pctWrap.appendChild(document.createTextNode(' complete'));
    details.appendChild(pctWrap);
    var sizeWrap = document.createElement('span');
    var done = document.createElement('b');
    done.setAttribute('data-v', 'size-done');
    done.textContent = item.size_done || '';
    var total = document.createElement('span');
    total.setAttribute('data-v', 'size-total');
    total.textContent = item.size_total || '';
    sizeWrap.appendChild(done);
    sizeWrap.appendChild(document.createTextNode(' of '));
    sizeWrap.appendChild(total);
    details.appendChild(sizeWrap);
    if (item.eta) details.appendChild(buildEtaStat(item.eta));
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

  function canAbandonHere() {
    var root = document.getElementById('dl-root');
    return !root || root.getAttribute('data-can-abandon') !== 'false';
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* Mirrors the abandon_btn macro in _components.html. */
  function fillAbandonButton(btn, item) {
    btn.setAttribute('data-dl-id', item.id);
    btn.setAttribute('data-kind', item.media_type === 'movie' ? 'movie' : 'series');
    btn.setAttribute('data-title', item.title || '');
    btn.setAttribute('data-stuck-seasons', JSON.stringify(item.stuck_seasons || []));
  }

  function buildAbandonButton(item) {
    var btn = el('button', 'btn btn--icon btn--ghost');
    btn.type = 'button';
    btn.setAttribute('data-abandon-trigger', '');
    btn.setAttribute('aria-label', 'Abandon search');
    btn.setAttribute('title', 'Abandon search');
    fillAbandonButton(btn, item);
    var icon = el('i', 'fa-solid fa-xmark');
    icon.setAttribute('aria-hidden', 'true');
    btn.appendChild(icon);
    return btn;
  }

  /* The server decides per poll whether an item may be abandoned and which
     seasons are stuck (abandon_visible, stuck_seasons); keep the button in
     step with it. kind is 'hero' or 'row'. */
  function syncAbandon(card, item, kind) {
    var isHero = kind === 'hero';
    var wrapClass = isHero ? 'dl-hero-abandon' : 'dl-row-abandon';
    var host = isHero ? card : card.querySelector('.dl-compact-row');
    if (!host) return;
    var wrap = host.querySelector('.' + wrapClass);
    var want = canAbandonHere() && !!item.abandon_visible;
    if (!want) {
      if (wrap) wrap.parentNode.removeChild(wrap);
      if (!isHero) host.classList.remove('dl-row--has-abandon');
      return;
    }
    if (wrap) {
      var existing = wrap.querySelector('[data-abandon-trigger]');
      if (existing) fillAbandonButton(existing, item);
      return;
    }
    wrap = el('div', wrapClass);
    wrap.appendChild(buildAbandonButton(item));
    if (isHero) {
      var content = card.querySelector('.dl-hero-content');
      card.insertBefore(wrap, content);
    } else {
      host.classList.add('dl-row--has-abandon');
      host.appendChild(wrap);
    }
  }

  /* Build a queue row, mirroring _dl_compact_row.html. */
  function buildCompactRow(item) {
    var card = el('div', 'dl-compact dl-card-enter');
    card.setAttribute('data-dl-id', item.id);
    var row = el('div', 'dl-compact-row dl-row');
    card.appendChild(row);

    var poster = el('div', 'dl-compact-poster dl-row-poster');
    if (item.poster_url) {
      var img = el('img');
      img.src = item.poster_url;
      img.alt = '';
      poster.appendChild(img);
    }
    row.appendChild(poster);

    var info = el('div', 'dl-compact-info dl-row-info');
    info.appendChild(el('div', 'dl-compact-title dl-row-title', item.title));

    var meta = el('div', 'dl-compact-meta dl-row-meta');
    var pill = el('span', 'dl-state-pill dl-state-pill--xs dl-state-' + item.state,
      item.state_label || item.state);
    pill.setAttribute('data-v', 'pill');
    meta.appendChild(pill);
    var sizeInfo = el('span');
    sizeInfo.setAttribute('data-v', 'size-info');
    if (item.state === 'searching') sizeInfo.style.display = 'none';
    var done = el('span', '', item.size_done || '');
    done.setAttribute('data-v', 'size-done');
    var total = el('span', '', item.size_total || '');
    total.setAttribute('data-v', 'size-total');
    sizeInfo.appendChild(done);
    sizeInfo.appendChild(document.createTextNode(' of '));
    sizeInfo.appendChild(total);
    meta.appendChild(sizeInfo);
    var episodes = item.episodes || [];
    if (episodes.length) {
      meta.appendChild(el('span', '',
        episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '')));
    }
    info.appendChild(meta);

    var hint = el('div', 'dl-search-hint');
    hint.setAttribute('data-v', 'search-hint');
    hint.style.display = 'none';
    info.appendChild(hint);

    var bar = el('div', 'dl-compact-bar dl-row-bar');
    var fill = el('div', 'dl-compact-fill dl-row-bar-fill' +
      (item.state === 'almost_ready' ? ' green' : ''));
    fill.setAttribute('data-v', 'fill');
    fill.style.width = (item.progress || 0) + '%';
    bar.appendChild(fill);
    info.appendChild(bar);
    row.appendChild(info);

    var right = el('div', 'dl-compact-right');
    var pct = el('div', 'dl-compact-pct dl-row-pct', item.progress > 0 ? item.progress + '%' : '—');
    pct.setAttribute('data-v', 'pct');
    right.appendChild(pct);
    var eta = el('div', 'dl-compact-eta', item.eta || '');
    eta.setAttribute('data-v', 'eta');
    right.appendChild(eta);
    row.appendChild(right);

    syncAbandon(card, item, 'row');

    if (episodes.length) {
      var toggle = el('button', 'dl-compact-episodes-toggle');
      toggle.type = 'button';
      toggle.setAttribute('data-v', 'ep-toggle');
      toggle.setAttribute('aria-expanded', 'false');
      var chev = el('i', 'fa-solid fa-chevron-right');
      chev.setAttribute('aria-hidden', 'true');
      toggle.appendChild(chev);
      toggle.appendChild(el('span', '', episodes.length + ' episode' +
        (episodes.length !== 1 ? 's' : '') + (item.has_pack ? ' · pack download' : '')));
      card.appendChild(toggle);
      var list = el('div', 'dl-compact-episodes');
      list.setAttribute('data-v', 'ep-list');
      list.style.display = 'none';
      for (var i = 0; i < episodes.length; i++) {
        list.appendChild(MM.downloads.renderHero.buildEpisodeRow(episodes[i]));
      }
      card.appendChild(list);
    }

    updateHint(card, item);
    return card;
  }

  function updateHint(card, item) {
    var hero = MM.downloads.renderHero;
    if (hero && hero.updateSearchHint) hero.updateSearchHint(card, item);
  }

  /* Build upcoming row element. */
  function buildUpcomingRow(item) {
    /* The server marks the page root; viewers get no stop-tracking button. */
    var root = document.getElementById('dl-root');
    var canAbandon = !root || root.getAttribute('data-can-abandon') !== 'false';
    var row = document.createElement('div');
    row.className = 'dl-compact-row dl-row dl-upcoming-row dl-card-enter' +
      (canAbandon ? ' dl-row--has-abandon' : '');
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

    if (!canAbandon) return row;

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
    var xIcon = document.createElement('i');
    xIcon.className = 'fa-solid fa-xmark';
    xIcon.setAttribute('aria-hidden', 'true');
    btn.appendChild(xIcon);
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
    buildEtaStat:         buildEtaStat,
    buildHeroPlaceholder: buildHeroPlaceholder,
    buildRecentItem:      buildRecentItem,
    buildEmptyState:      buildEmptyState,
    buildUpcomingRow:     buildUpcomingRow,
    buildCompactRow:      buildCompactRow,
    syncAbandon:          syncAbandon,
  };
})();
