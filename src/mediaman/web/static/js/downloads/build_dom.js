/**
 * downloads/build_dom.js — DOM helpers and placeholder builders.
 *
 * DOM construction, no fetches. Other download modules consume these to
 * either build a fresh card or look up an existing one to patch in place,
 * and to open or close a card, section or list without the page jumping.
 *
 * Thin aliases q, setText, and findByDlId delegate to MM.dom to avoid
 * duplicating logic already provided by core/dom.js.
 *
 * Exposes:
 *   MM.downloads.buildDom.q(sel, ctx)              → MM.dom.q
 *   MM.downloads.buildDom.setText(el, txt)         → MM.dom.setText
 *   MM.downloads.buildDom.findByDlId(c, dlId)      → MM.dom.findByAttr(c, 'data-dl-id', dlId)
 *   MM.downloads.buildDom.buildHero(item)          shared hero card builder (item.hero_backdrop fills its background)
 *   MM.downloads.buildDom.syncArt(card, item)       a hero's poster and background
 *   MM.downloads.buildDom.syncPoster(box, url)
 *   MM.downloads.buildDom.syncTypePill(info, mediaType)  the Movie or TV pill above a hero's title
 *   MM.downloads.buildDom.buildHeroPlaceholder(id) thin wrapper around buildHero
 *   MM.downloads.buildDom.buildRecentItem(r)
 *   MM.downloads.buildDom.recentWords(r)            the line under a recent item's title
 *   MM.downloads.buildDom.buildEmptyState(outage, setUp, refused)
 *   MM.downloads.buildDom.dropAbsent(rows, present)  close the rows the payload no longer lists
 *   MM.downloads.buildDom.buildUpcomingRow(item)
 *   MM.downloads.buildDom.buildCompactRow(item)    queue row for an item the page has not seen
 *   MM.downloads.buildDom.setProgress(fill, percent)  a bar's width and its spoken value
 *   MM.downloads.buildDom.buildEpisodeRow(ep)
 *   MM.downloads.buildDom.syncEpisodes(card, item)  add, refresh or remove the episodes button and list
 *   MM.downloads.buildDom.closeEpisodes(card)
 *   MM.downloads.buildDom.easeHeight(card)          a function that eases the card from this height to its next
 *   MM.downloads.buildDom.fadeOver(card)            the hero as it is fades out over what it becomes
 *   MM.downloads.buildDom.syncAbandon(card, item, kind)  show, hide or refresh the abandon button
 *   MM.downloads.buildDom.fillAbandonButton(btn, item)   what an abandon button says it is of
 *   MM.downloads.buildDom.focusHeading()            the top of the page, when what had the focus is gone
 *   MM.downloads.buildDom.passFocus(el)             move focus on from an element that is about to go
 *   MM.downloads.buildDom.reveal(el)               grow an element in from nothing
 *   MM.downloads.buildDom.collapse(el, done)       shrink it away, then call done
 *   MM.downloads.buildDom.collapseAway(el)          shrink it away, then remove it
 *   MM.downloads.buildDom.isCollapsing(el)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function focusHeading() {
    var h1 = document.querySelector('#dl-root h1');
    if (!h1) return;
    h1.setAttribute('tabindex', '-1');
    h1.focus({ preventScroll: true });
  }

  /* Focus inside what is about to go moves on: from an abandon button to the one
     of the next row or the one before, else to the top of the page. */
  function passFocus(node) {
    var active = document.activeElement;
    if (!active || !node.contains(active)) return;
    var row = active.closest('[data-abandon-trigger]') && node.closest('.dl-compact, .dl-upcoming-row');
    var dirs = ['nextElementSibling', 'previousElementSibling'];
    for (var i = 0; row && i < dirs.length; i++) {
      for (var at = row[dirs[i]]; at; at = at[dirs[i]]) {
        var btn = !isCollapsing(at) && at.querySelector('[data-abandon-trigger]');
        if (btn) { btn.focus(); return; }
      }
    }
    focusHeading();
  }

  var FOLD_PROPS = ['opacity', 'paddingTop', 'paddingBottom', 'marginTop', 'marginBottom'];

  /* Open or close an element in place: its height, padding and margins run
     to their new size so nothing below it jumps. A fold that starts while
     another runs picks up from where that one is. `done` runs once the
     height has closed, and never if a later fold took over. */
  function fold(el, open, done) {
    var running = el._dlFold;
    if (running && running.open === open) return;
    var token = { open: open };
    el._dlFold = token;
    if (!open) passFocus(el);

    function finish() {
      if (el._dlFold !== token) return;
      el._dlFold = null;
      el.removeEventListener('transitionend', onEnd);
      el.classList.remove('dl-fold');
      el.classList.remove('is-folded');
      el.style.height = '';
      if (!open) passFocus(el);
      if (done) done();
    }
    function onEnd(e) {
      if (e.target === el && e.propertyName === 'height') finish();
    }
    function now() {
      var cs = window.getComputedStyle(el);
      var s = { height: el.offsetHeight + 'px' };
      FOLD_PROPS.forEach(function (p) { s[p] = cs[p]; });
      return s;
    }

    if (reducedMotion()) { finish(); return; }

    /* Read a running fold before anything stops it. */
    var from = running ? now() : null;
    el.style.transition = 'none';
    el.classList.add('dl-fold');
    el.classList.remove('is-folded');
    el.style.height = '';
    var natural = el.offsetHeight;
    if (!from) {
      if (open) { el.classList.add('is-folded'); el.style.height = '0px'; }
      from = now();
    }
    if (open) el.classList.remove('is-folded');
    else el.classList.add('is-folded');
    el.style.height = from.height;
    FOLD_PROPS.forEach(function (p) { el.style[p] = from[p]; });
    void el.offsetHeight;
    el.style.transition = '';
    FOLD_PROPS.forEach(function (p) { el.style[p] = ''; });
    el.style.height = open ? natural + 'px' : '0px';

    el.addEventListener('transitionend', onEnd);
    /* transitionend never comes when the height did not change or the
       element was hidden mid-way. */
    setTimeout(finish, 400);
  }

  /* State labels come from the server (item.state_label). The canonical map
     lives in services/downloads/download_format/_types.py. */

  function buildBar(cls, label) {
    var bar = document.createElement('div');
    bar.className = cls;
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-label', label || '');
    return bar;
  }

  /* Fill a bar to `percent`, and say so to a screen reader. */
  function setProgress(fill, percent) {
    if (!fill) return;
    fill.style.width = (percent || 0) + '%';
    fill.parentNode.setAttribute('aria-valuenow', String(percent || 0));
  }

  function buildPoster(url) {
    var img = document.createElement('img');
    img.alt = '';
    img.setAttribute('data-broken-on-error', '');
    img.src = url;
    return img;
  }

  /* An item's poster in `box`, and under it the mark that stands for one while there is
     none or it does not load. */
  function fillPoster(box, url) {
    if (url) box.appendChild(buildPoster(url));
    var mark = document.createElement('span');
    mark.className = 'dl-poster-mark';
    mark.appendChild(MM.dom.icon('i-film'));
    box.appendChild(mark);
  }

  /* Show an item's poster in `box`. */
  function syncPoster(box, url) {
    if (!box) return;
    var img = box.querySelector('img');
    if ((img ? img.getAttribute('src') : '') === (url || '') && box.firstChild) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    fillPoster(box, url);
  }

  /* The hero's poster and blurred background are its item's, or none. */
  function syncArt(card, item) {
    var url = item.poster_url || '';
    syncPoster(card.querySelector('.dl-hero-poster'), url);
    var bg = card.querySelector('.dl-hero-bg');
    if (!bg || (bg.getAttribute('data-bg-url') || '') === url) return;
    bg.setAttribute('data-bg-url', url);
    if (window.DLPoster) window.DLPoster.apply(bg);
  }

  var RECENT_WORDS = { part: 'Some episodes ready', unknown: 'Added' };

  function recentWords(r) { return RECENT_WORDS[r.readiness] || 'Ready to watch'; }

  /* Build a recent item element safely. */
  function buildRecentItem(r) {
    var item = document.createElement('div');
    item.className = 'dl-recent-item';

    var poster = document.createElement('div');
    poster.className = 'dl-recent-poster';
    if (r.poster_url) poster.appendChild(buildPoster(r.poster_url));
    if (!RECENT_WORDS[r.readiness]) {
      var badge = document.createElement('div');
      badge.className = 'dl-recent-badge';
      badge.appendChild(MM.dom.icon('i-check'));
      poster.appendChild(badge);
    }
    item.appendChild(poster);

    var title = document.createElement('div');
    title.className = 'dl-recent-title';
    title.textContent = r.title;
    item.appendChild(title);

    var sub = document.createElement('div');
    sub.className = 'dl-recent-sub' + (RECENT_WORDS[r.readiness] ? ' dl-recent-sub--plain' : '');
    sub.textContent = recentWords(r);
    item.appendChild(sub);

    return item;
  }

  /* Build the "nothing downloading" empty state.

     Mirrors the c.empty() macro markup (.empty > .empty-ico + h3 + p) and
     the download-tray glyph defined in downloads.html, so the poll-built
     card is identical to the server-rendered one. The #dl-empty-container
     owns the id — this builder returns only the inner card. */
  /* Mirrors the cards in downloads.html: nothing downloading, a service that cannot be
     reached (which the subtitle names), one whose key was refused (which an admin is sent to
     Settings for), or neither Radarr nor Sonarr set up. */
  function buildEmptyState(outage, setUp, refused) {
    var unset = !!setUp && !setUp.movies && !setUp.shows;
    var shut = canAbandonHere() && outage && refused && refused.length ? refused : null;
    var el = document.createElement('div');
    el.className = 'empty';

    var icon = document.createElement('div');
    icon.className = 'empty-ico';
    icon.appendChild(MM.dom.icon(outage && !unset ? 'i-triangle-exclamation' : 'i-download'));
    el.appendChild(icon);

    var h3 = document.createElement('h3');
    h3.textContent = unset ? 'Downloads aren’t set up'
      : outage ? 'Downloads can\'t be checked right now' : 'All caught up';
    el.appendChild(h3);

    var p = document.createElement('p');
    if (unset || shut) {
      if (unset && !canAbandonHere()) {
        p.textContent = 'Ask whoever runs mediaman to set it up.';
      } else {
        var names = shut && shut.length > 1
          ? shut.slice(0, -1).join(', ') + ' and ' + shut[shut.length - 1] : shut && shut[0];
        p.appendChild(document.createTextNode(shut ? 'Check the details for ' + names + ' in '
          : 'Connect Radarr or Sonarr in '));
        var link = document.createElement('a');
        link.href = '/settings#setg-integrations';
        link.textContent = 'Settings';
        p.appendChild(link);
        p.appendChild(document.createTextNode('.'));
      }
    } else {
      p.textContent = outage ? 'This page keeps trying.' : 'Nothing downloading right now.';
    }
    el.appendChild(p);

    return el;
  }

  function buildEtaStat(text) {
    var wrap = document.createElement('span');
    var eta = document.createElement('b');
    eta.setAttribute('data-v', 'eta');
    eta.textContent = text;
    wrap.appendChild(eta);
    return wrap;
  }

  /* The ETA stat exists only while there is an ETA, so it is added, updated
   * and removed here. */
  function updateEtaStat(card, text) {
    var eta = card.querySelector('[data-v="eta"]');
    if (eta && text) {
      eta.textContent = text;
    } else if (eta) {
      eta.parentNode.parentNode.removeChild(eta.parentNode);
    } else if (text) {
      var stats = card.querySelector('.dl-hero-details');
      if (stats) stats.appendChild(buildEtaStat(text));
    }
  }

  function buildTypeRow() {
    var row = el('div', 'pill-row u-mb-12');
    row.appendChild(el('span'));
    return row;
  }

  function showType(row, mediaType) {
    var isMovie = mediaType === 'movie';
    var pill = row.children[0];
    pill.className = 'pill pill--' + (isMovie ? 'movie' : 'tv');
    MM.dom.setText(pill, isMovie ? 'Movie' : 'TV');
  }

  function syncTypePill(info, mediaType) {
    if (!info) return;
    var row = info.querySelector('.pill-row');
    if (!row) row = info.insertBefore(buildTypeRow(), info.firstChild);
    showType(row, mediaType);
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
   *   Shape: { id, title, media_type, poster_url, state, state_label, progress, eta }
   * @returns {HTMLElement}  The .dl-hero div — not yet attached to the DOM.
   */
  function buildHero(item) {
    item = item || {};
    var bgUrl = item.poster_url || '';
    /* The card's blurred background is the wide backdrop when there is one, as in the
       server-rendered card; the poster stays the poster. */
    var heroBg = item.hero_backdrop || bgUrl;
    var state  = item.state || '';

    /* Root .dl-hero container */
    var card = document.createElement('div');
    card.className = 'dl-hero';
    if (item.id) card.setAttribute('data-dl-id', item.id);

    /* Background — set via DLPoster.apply (avoids CSS string injection). */
    var bg = document.createElement('div');
    bg.className = 'dl-hero-bg';
    if (heroBg) bg.setAttribute('data-bg-url', heroBg);
    card.appendChild(bg);
    if (heroBg && window.DLPoster) window.DLPoster.apply(bg);

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
    fillPoster(posterWrap, bgUrl);
    content.appendChild(posterWrap);

    /* Info section */
    var info = document.createElement('div');
    info.className = 'dl-hero-info';

    if (item.media_type) {
      var typeRow = buildTypeRow();
      showType(typeRow, item.media_type);
      info.appendChild(typeRow);
    }

    var titleEl = document.createElement('h2');
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
    searchHint.hidden = true;
    info.appendChild(searchHint);

    /* Progress wrap (hidden entirely while searching) */
    var progressWrap = document.createElement('div');
    progressWrap.className = 'dl-hero-progress';
    progressWrap.setAttribute('data-v', 'progress-wrap');
    if (state === 'searching') progressWrap.hidden = true;

    var bar = buildBar('dl-hero-bar dl-bar', item.title);
    var fill = document.createElement('div');
    fill.className = 'dl-hero-fill dl-bar-fill' + (state === 'almost_ready' ? ' green' : '');
    fill.setAttribute('data-v', 'fill');
    bar.appendChild(fill);
    setProgress(fill, item.progress);
    progressWrap.appendChild(bar);

    /* Same stats line as _dl_hero_card.html: "N% complete", "done of total", the time left. */
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
    btn.setAttribute('data-arr-id', String(item.arr_id || 0));
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
    btn.appendChild(MM.dom.icon('i-xmark'));
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
      if (wrap) {
        passFocus(wrap);
        wrap.parentNode.removeChild(wrap);
      }
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

  /* Mirrors the episode rows in _dl_hero_card.html and _dl_compact_row.html. */
  function buildEpisodeRow(ep) {
    var row = el('div', 'dl-ep-row' + (ep.is_pack_episode ? ' dl-ep-row-pack' : ''));
    row.setAttribute('data-ep', ep.label);
    row.appendChild(el('span', 'dl-ep-number', ep.label));
    row.appendChild(el('span', 'dl-ep-title', ep.title || ''));

    if (ep.is_pack_episode) {
      row.appendChild(el('span', 'dl-ep-status-pill downloading', 'In pack'));
      return row;
    }

    var miniBar = buildBar('dl-ep-mini-bar', ep.label);
    var miniFill = el('div', 'dl-ep-mini-fill');
    miniFill.setAttribute('data-v', 'ep-fill');
    miniBar.appendChild(miniFill);
    row.appendChild(miniBar);
    row.appendChild(el('span', 'dl-ep-status-pill'));
    patchEpisodeRow(row, ep);
    return row;
  }

  function patchEpisodeRow(row, ep) {
    MM.dom.setText(row.querySelector('.dl-ep-title'), ep.title || '');
    if (ep.is_pack_episode) return;
    var state = ep.state || 'searching';
    var fill = row.querySelector('[data-v="ep-fill"]');
    setProgress(fill, ep.progress);
    fill.className = 'dl-ep-mini-fill' + (state === 'ready' ? ' green' : '');
    var pill = row.querySelector('.dl-ep-status-pill');
    pill.className = 'dl-ep-status-pill ' + state;
    MM.dom.setText(pill, state === 'ready' ? 'Ready'
      : state === 'downloading' ? (ep.progress || 0) + '%'
      : state === 'queued' ? 'Queued' : 'Searching');
  }

  function episodeCount(episodes) {
    return episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '');
  }

  function closeEpisodes(card) {
    var toggle = card.querySelector('[data-v="ep-toggle"]');
    if (!toggle) return;
    toggle.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
    card.querySelector('[data-v="ep-list"]').hidden = true;
  }

  /* A copy of the card as it is, drawn over it to fade out while the card turns into
     another title's. It is a picture alone, and beside the card, so no poll that looks for
     a part of the card finds one of the copy's. */
  function fadeOver(card) {
    var box = card.parentNode;
    var old = box.querySelector('.dl-hero-ghost');
    if (old) box.removeChild(old);
    if (reducedMotion()) return;
    var ghost = el('div', 'dl-hero-ghost');
    ghost.setAttribute('aria-hidden', 'true');
    ghost.setAttribute('inert', '');
    var copy = el('div', 'dl-hero-ghost__card');
    Array.prototype.forEach.call(card.children, function (part) { copy.appendChild(part.cloneNode(true)); });
    ghost.appendChild(copy);
    ['id', 'data-v', 'data-ep', 'data-dl-id', 'data-abandon-trigger'].forEach(function (hook) {
      Array.prototype.forEach.call(ghost.querySelectorAll('[' + hook + ']'), function (part) { part.removeAttribute(hook); });
    });
    box.appendChild(ghost);
    /* The new content waits for the old to be gone, so lines that differ never overlap. */
    var run = card._dlSwap = {};
    card.classList.remove('is-swapping');
    void card.offsetWidth;
    card.classList.add('is-swapping');
    /* Longer than the fade, which may never end in a tab that is hidden. */
    setTimeout(function () {
      if (ghost.parentNode) box.removeChild(ghost);
      if (card._dlSwap === run) card.classList.remove('is-swapping');
    }, 400);
  }

  /* Read a card's height before it turns into another title's; the function returned is
     called after, and brings it to its new one from there. A swap while one still moves
     starts from where the card is. */
  function easeHeight(card) {
    if (reducedMotion()) return function () {};
    var from = card.getBoundingClientRect().height;
    return function () {
      var run = card._dlEase = {};
      card.style.transition = 'none';
      card.style.height = '';
      var to = card.getBoundingClientRect().height;
      if (Math.abs(to - from) >= 1) {
        card.style.height = from + 'px';
        void card.offsetHeight;
        setTimeout(function () { if (card._dlEase === run) card.style.height = ''; }, 400);
      }
      card.style.transition = '';
      if (card.style.height) card.style.height = to + 'px';
    };
  }

  /* The episodes button and list of a hero or queue row exist only while the
     item has episodes. The list is rebuilt when their set has changed and
     patched otherwise, so it stays open as the person left it. */
  function syncEpisodes(card, item) {
    var episodes = item.episodes || [];
    var toggle = card.querySelector('[data-v="ep-toggle"]');
    var list = card.querySelector('[data-v="ep-list"]');
    if (!episodes.length) {
      if (toggle) {
        passFocus(toggle);
        toggle.parentNode.removeChild(toggle);
      }
      if (list) list.parentNode.removeChild(list);
      return;
    }

    if (!toggle) {
      var info = card.querySelector('.dl-hero-info');
      var prefix = info ? 'dl-hero' : 'dl-compact';
      var home = info || card.querySelector('.dl-compact-row') || card;
      toggle = el('button', prefix + '-episodes-toggle');
      toggle.type = 'button';
      toggle.setAttribute('data-v', 'ep-toggle');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.appendChild(MM.dom.icon('i-chevron-right'));
      toggle.appendChild(el('span'));
      list = el('div', prefix + '-episodes');
      list.setAttribute('data-v', 'ep-list');
      list.hidden = true;
      home.appendChild(toggle);
      home.appendChild(list);
    }
    MM.dom.setText(toggle.querySelector('span'),
      episodeCount(episodes) + (item.has_pack ? ' · pack download' : ''));

    var rows = list.querySelectorAll('[data-ep]');
    var same = rows.length === episodes.length && episodes.every(function (ep, i) {
      return rows[i].getAttribute('data-ep') === ep.label &&
        rows[i].classList.contains('dl-ep-row-pack') === !!ep.is_pack_episode;
    });
    if (same) {
      episodes.forEach(function (ep, i) { patchEpisodeRow(rows[i], ep); });
      return;
    }
    while (list.firstChild) list.removeChild(list.firstChild);
    episodes.forEach(function (ep) { list.appendChild(buildEpisodeRow(ep)); });
  }

  /* Build a queue row, mirroring _dl_compact_row.html. */
  function buildCompactRow(item) {
    var card = el('div', 'dl-compact');
    card.setAttribute('data-dl-id', item.id);
    var row = el('div', 'dl-compact-row dl-row');
    card.appendChild(row);

    var poster = el('div', 'dl-compact-poster dl-row-poster');
    fillPoster(poster, item.poster_url);
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
    if (item.state === 'searching') sizeInfo.hidden = true;
    var done = el('span', '', item.size_done || '');
    done.setAttribute('data-v', 'size-done');
    var total = el('span', '', item.size_total || '');
    total.setAttribute('data-v', 'size-total');
    sizeInfo.appendChild(done);
    sizeInfo.appendChild(document.createTextNode(' of '));
    sizeInfo.appendChild(total);
    meta.appendChild(sizeInfo);
    info.appendChild(meta);

    var hint = el('div', 'dl-search-hint');
    hint.setAttribute('data-v', 'search-hint');
    hint.hidden = true;
    info.appendChild(hint);

    var bar = buildBar('dl-compact-bar dl-row-bar', item.title);
    var fill = el('div', 'dl-compact-fill dl-row-bar-fill' +
      (item.state === 'almost_ready' ? ' green' : ''));
    fill.setAttribute('data-v', 'fill');
    bar.appendChild(fill);
    setProgress(fill, item.progress);
    info.appendChild(bar);
    row.appendChild(info);

    var right = el('div', 'dl-compact-right');
    var pct = el('div', 'dl-compact-pct dl-row-pct' + (item.progress > 0 ? '' : ' dl-compact-pct--muted'),
      item.progress > 0 ? item.progress + '%' : '—');
    pct.setAttribute('data-v', 'pct');
    right.appendChild(pct);
    var eta = el('div', 'dl-compact-eta', item.eta || '');
    eta.setAttribute('data-v', 'eta');
    right.appendChild(eta);
    row.appendChild(right);

    syncAbandon(card, item, 'row');

    syncEpisodes(card, item);
    updateHint(card, item);
    return card;
  }

  function updateHint(card, item) {
    var hero = MM.downloads.renderHero;
    if (hero && hero.updateSearchHint) hero.updateSearchHint(card, item);
  }

  /* Close the rows whose item is gone. `present` maps the ids the payload has to true, or
     to false for one shown elsewhere on the page. With none left to show, the rows are not
     closed one by one, since the section closes with them. */
  function dropAbsent(rows, present) {
    var gone = [];
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var id = rows[i].getAttribute('data-dl-id');
      if (!id || isCollapsing(rows[i])) continue;
      if (present[id]) shown += 1;
      else gone.push(rows[i]);
    }
    if (shown) gone.forEach(collapseAway);
  }

  /* Build upcoming row element. */
  function buildUpcomingRow(item) {
    /* The server marks the page root; viewers get no stop-tracking button. */
    var root = document.getElementById('dl-root');
    var canAbandon = !root || root.getAttribute('data-can-abandon') !== 'false';
    var row = document.createElement('div');
    row.className = 'dl-compact-row dl-row dl-upcoming-row' +
      (canAbandon ? ' dl-row--has-abandon' : '');
    row.setAttribute('data-dl-id', item.id);

    var poster = document.createElement('div');
    poster.className = 'dl-compact-poster dl-row-poster';
    fillPoster(poster, item.poster_url);
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
    fillAbandonButton(btn, item);
    btn.setAttribute('aria-label', 'Stop tracking');
    btn.setAttribute('title', 'Stop tracking');
    btn.appendChild(MM.dom.icon('i-xmark'));
    abandonWrap.appendChild(btn);
    row.appendChild(abandonWrap);

    return row;
  }

  function isCollapsing(el) { return !!(el._dlFold && !el._dlFold.open); }

  function collapseAway(el) {
    fold(el, false, function () { if (el.parentNode) el.parentNode.removeChild(el); });
  }

  MM.downloads.buildDom = {
    q:                    function (sel, ctx)        { return MM.dom.q(sel, ctx); },
    setText:              function (el, txt)         { return MM.dom.setText(el, txt); },
    findByDlId:           function (container, dlId) { return MM.dom.findByAttr(container, 'data-dl-id', dlId); },
    buildHero:            buildHero,
    syncArt:              syncArt,
    syncPoster:           syncPoster,
    syncTypePill:         syncTypePill,
    buildEtaStat:         buildEtaStat,
    updateEtaStat:        updateEtaStat,
    buildHeroPlaceholder: buildHeroPlaceholder,
    buildRecentItem:      buildRecentItem,
    recentWords:          recentWords,
    buildEmptyState:      buildEmptyState,
    canAbandonHere:       canAbandonHere,
    reducedMotion:        reducedMotion,
    dropAbsent:           dropAbsent,
    buildUpcomingRow:     buildUpcomingRow,
    buildCompactRow:      buildCompactRow,
    setProgress:          setProgress,
    buildEpisodeRow:      buildEpisodeRow,
    syncEpisodes:         syncEpisodes,
    closeEpisodes:        closeEpisodes,
    fadeOver:             fadeOver,
    easeHeight:           easeHeight,
    syncAbandon:          syncAbandon,
    fillAbandonButton:    fillAbandonButton,
    focusHeading:         focusHeading,
    passFocus:            passFocus,
    reveal:               function (el)              { fold(el, true); },
    collapse:             function (el, done)        { fold(el, false, done); },
    collapseAway:         collapseAway,
    isCollapsing:         isCollapsing,
  };
})();
