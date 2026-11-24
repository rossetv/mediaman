/* Watch page: the title detail overlay at #title=<ref>, on window.MMWatch.detail.
 * Opens from any [data-title-ref] and from the address; Back closes it.
 * Needs watch/core.js; uses watch/keep.js for a viewer's Keep and
 * MMWatch.admin, when loaded, for the admin facts' Keep and Delete.
 */
(function () {
  'use strict';

  var core = window.MMWatch && window.MMWatch.core;
  var root = document.getElementById('ttl');
  if (!core || !root) return;

  var el = core.el;
  var toArray = core.toArray;
  var safePath = core.safePath;

  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^rec-[0-9]{1,10}$/;
  var YOUTUBE_KEY_RE = /^[A-Za-z0-9_-]{11}$/;
  var POLL_MS = 5000;
  /* A download that has not moved for this long is polled at POLL_SLOW_MS. */
  var POLL_BACKOFF_AFTER_MS = 120000;
  var POLL_SLOW_MS = 30000;
  /* How often an open title asks again whether a download the Arr no longer
     queues was removed or has not reached the Arr yet. */
  var RECHECK_MS = 60000;
  /* How long after this page's own Download a reply that knows nothing of it
     is the Arr catching up rather than the download being gone. */
  var DOWNLOAD_GRACE_MS = 30000;
  var COPIED_MS = 2000;
  var EXIT_FALLBACK = 80; // past an animation's own length, for one that never ends

  var sheet = root.querySelector('.ttl__sheet');
  var titleEl = root.querySelector('#ttl-title');
  var part = function (name) { return root.querySelector('[data-ttl-part="' + name + '"]'); };
  var heroSkel = part('hero-skel');
  var art = part('art');
  var player = part('player');
  var kindEl = part('kind');
  var taglineEl = part('tagline');
  var metaEl = part('meta');
  var body = part('body');

  var isAdmin = false;
  try {
    var dataNode = document.getElementById('watch-page-data');
    isAdmin = !!(dataNode && JSON.parse(dataNode.textContent).is_admin);
  } catch (e) { isAdmin = false; }

  var cur = null;      // the title on show: { ref, data, facts, ... }
  var isOpen = false;
  var leaving = null;  // the close still animating, finished early by a new open
  var seq = 0;
  var pollTimer = null;
  var shareTimer = null;
  /* What this page knows of a title's download, by ref: it outlives the
     overlay, so a title reopened mid-download still shows its state. */
  var known = {};

  /* ── Small builders ── */

  function icon(name) {
    var i = el('i', 'fa-solid ' + name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  }

  function button(cls, label, iconName) {
    var b = el('button', cls);
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName));
    if (label) b.appendChild(document.createTextNode(label));
    return b;
  }

  function skel(cls, width) {
    var s = el('span', 'skel ' + (cls || ''));
    if (width) s.style.width = width;
    s.setAttribute('aria-hidden', 'true');
    return s;
  }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function dur(minutes) {
    if (minutes < 60) return minutes + ' min';
    return Math.floor(minutes / 60) + ' h' + (minutes % 60 ? ' ' + (minutes % 60) + ' min' : '');
  }

  function day(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function hide(node, on) { node.hidden = !!on; }

  function apiUrl(ref) { return '/api/watch/title/' + encodeURIComponent(ref); }

  /* Cards of the title under another ref (a pick, a result) are found by what
     they print: its title, kind and year, whichever title is open by now. */
  function fire(ref, state, pct) {
    if (typeof CustomEvent !== 'function') return;
    var k = stateOf(ref);
    document.dispatchEvent(new CustomEvent('mm:title-changed', {
      detail: {
        ref: ref, download_state: state, download_pct: pct === undefined ? null : pct,
        title: k.title, kind: k.kind, year: k.year
      }
    }));
  }

  function learn(ref, data) {
    var k = stateOf(ref);
    k.title = data.title;
    k.kind = data.kind;
    k.year = data.year;
  }

  function nowInLibrary(ref) {
    var k = stateOf(ref);
    k.state = 'in_library';
    k.pct = null;
    k.tracked = false;
    fire(ref, 'in_library', null);
  }

  /* ── Address ── */

  function base() { return window.location.pathname + window.location.search; }

  function hashRef() {
    var m = /^#title=(.+)$/.exec(window.location.hash || '');
    if (!m) return null;
    var ref;
    try { ref = decodeURIComponent(m[1]); } catch (e) { return null; }
    return REF_RE.test(ref) ? ref : null;
  }

  /* ── Motion ── */

  function wide() { return window.matchMedia('(min-width: 700px)').matches; }

  /* Run *fn* once *node*'s animation ends, or after *ms* if it never does. */
  function afterAnimation(node, ms, fn) {
    var done = false;
    function go() {
      if (done) return;
      done = true;
      node.removeEventListener('animationend', onEnd);
      fn();
    }
    function onEnd(e) { if (e.target === node) go(); }
    node.addEventListener('animationend', onEnd);
    setTimeout(go, ms + EXIT_FALLBACK);
    return go;
  }

  /* The sheet grows out of the card that opened it (desktop only). */
  function growFrom(origin) {
    sheet.style.transformOrigin = '';
    if (!origin || !document.body.contains(origin) || !wide()) return;
    var from = (origin.querySelector && origin.querySelector('.watch-card__poster')) || origin;
    var r = from.getBoundingClientRect();
    var s = sheet.getBoundingClientRect();
    sheet.style.transformOrigin = Math.round(r.left + r.width / 2 - s.left) + 'px ' +
      Math.round(r.top + r.height / 2 - s.top) + 'px';
  }

  /* ── Opening and closing ── */

  function lockPage(on) {
    var html = document.documentElement;
    if (on) {
      // The scrollbar goes with the page's scroll; its width is held so nothing shifts.
      var bar = window.innerWidth - html.clientWidth;
      if (bar > 0) html.style.paddingRight = bar + 'px';
      html.classList.add('has-ttl');
    } else {
      html.classList.remove('has-ttl');
      html.style.paddingRight = '';
    }
  }

  function show(origin) {
    root.hidden = false;
    root.classList.remove('is-leaving', 'is-trailer');
    lockPage(true);
    isOpen = true;
    growFrom(origin);
    root.classList.add('is-entering');
    var ms = Math.max(core.tokenMs('--dur-slow', 250), core.tokenMs('--dur', 200));
    afterAnimation(sheet, ms, function () { root.classList.remove('is-entering'); });
  }

  /* Open the title *ref*. opts: origin (the card), title (what the card
     already knows), push (add a history entry). */
  function open(ref, opts) {
    opts = opts || {};
    if (!REF_RE.test(ref)) return;
    if (isOpen && cur && cur.ref === ref) return;
    if (leaving) leaving();
    var wasOpen = isOpen;
    stopPolling();
    cur = {
      ref: ref,
      origin: opts.origin || (wasOpen && cur ? cur.origin : null),
      title: opts.title || '',
      kind: kindOf(ref, opts.kind),
      data: null,
      facts: null,
      keepId: null,
      recId: /^rec-/.test(ref) ? ref.slice(4) : null,
      reason: null,
      picks: null,
      season: null,
      shareUrl: null,
      shareMsg: null,
      shareState: null,  // null, 'busy' or 'copied'
      justAdded: false,
      addedNow: false,
      movedAt: 0,
      checkedAt: 0,
      ctrl: null,
      seq: ++seq
    };
    if (opts.push) {
      window.history.pushState({ mmTitle: ref }, '', base() + '#title=' + encodeURIComponent(ref));
    }
    closeTrailer(false);
    renderLoading();
    if (!wasOpen) show(cur.origin);
    // The overlay is the scroller and keeps its offset across closes.
    root.scrollTop = 0;
    titleEl.focus({ preventScroll: true });
    load(cur);
  }

  /* 'movie', 'show' or '' while the title loads: from the ref, else from
     whoever opened it or a card of it on the page (a pick's ref says neither). */
  function kindOf(ref, given) {
    var m = /^(movie|tmdb-movie|show|tmdb-tv)-/.exec(ref);
    if (m) return /movie/.test(m[1]) ? 'movie' : 'show';
    if (given === 'movie' || given === 'show') return given;
    var cards = toArray(document.querySelectorAll('[data-title-ref][data-kind]')).filter(function (n) {
      return n.getAttribute('data-title-ref') === ref;
    });
    var kind = cards.length ? cards[0].getAttribute('data-kind') : '';
    return kind === 'movie' || kind === 'show' ? kind : '';
  }

  /* Close at the person's request: Back undoes the entry the open added;
     a title opened from a link loses its #title= without a new entry. */
  function requestClose() {
    if (!isOpen) return;
    var state = window.history.state;
    if (state && state.mmTitle === cur.ref && hashRef() === cur.ref) {
      window.history.back();
      return;
    }
    if (hashRef()) window.history.replaceState(null, '', base());
    close();
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    stopPolling();
    closeTrailer(false);
    closeAdminMenu(false);
    if (cur && cur.ctrl) cur.ctrl.abort();
    if (window.MMWatch.keep) window.MMWatch.keep.closeMenu(false);
    var origin = cur && cur.origin;
    seq++;
    root.classList.remove('is-entering');
    root.classList.add('is-leaving');
    leaving = afterAnimation(sheet, core.tokenMs('--dur', 200), function () {
      leaving = null;
      if (isOpen) return;
      root.hidden = true;
      root.classList.remove('is-leaving', 'ttl--admin');
      root.removeAttribute('aria-busy');
      body.textContent = '';
      lockPage(false);
    });
    var to = origin && document.body.contains(origin) && !origin.closest('.is-leaving') ? origin : cur && cur.fallback;
    if (to && document.body.contains(to)) to.focus({ preventScroll: true });
    else if (root.contains(document.activeElement)) document.activeElement.blur();
  }

  /* Where focus goes when the list row the overlay opened from is deleted:
     the next row's first action, else the previous row's, else the list. */
  function rowFallback(origin) {
    var row = origin && origin.closest && origin.closest('tr[data-row-key]');
    if (!row) return null;
    var near = row.nextElementSibling || row.previousElementSibling;
    var action = near && near.querySelector('.keep-trigger, .wl-actions .btn');
    if (action) return action;
    var list = row.closest('#watch-list');
    if (list && !list.hasAttribute('tabindex')) list.setAttribute('tabindex', '-1');
    return list;
  }

  /* ── Loading ── */

  function get(url, ctrl) {
    return MM.api.get(url, ctrl ? { signal: ctrl.signal } : undefined);
  }

  function load(c) {
    if (c.ctrl) c.ctrl.abort();
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    c.ctrl = ctrl;
    get(apiUrl(c.ref), ctrl).then(function (data) {
      if (c !== cur || !isOpen) return;
      if (data.library_ref && data.library_ref !== c.ref && REF_RE.test(data.library_ref)) {
        switchTo(c, data);
        return;
      }
      arrive(c, data);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      if (c !== cur || !isOpen) return;
      renderFailed(err);
    });
  }

  /* A TMDB title found in the library opens as the library title. */
  function switchTo(c, data) {
    learn(c.ref, data);
    fire(c.ref, 'in_library', null);
    c.reason = c.reason || data.reason;
    if (hashRef() === c.ref) {
      var state = window.history.state && window.history.state.mmTitle ? { mmTitle: data.library_ref } : null;
      window.history.replaceState(state, '', base() + '#title=' + encodeURIComponent(data.library_ref));
    }
    c.ref = data.library_ref;
    load(c);
  }

  function arrive(c, data) {
    if (!data.reason && c.reason) data.reason = c.reason;
    c.data = data;
    c.title = data.title || c.title;
    if (data.leaving && data.leaving.keep_id) c.keepId = String(data.leaving.keep_id);
    learn(c.ref, data);
    takeDownload(c.ref, data.download, true);
    renderContent(true);
    if (isAdmin && /^(movie|show)-/.test(c.ref)) loadFacts(c);
    if (pollable()) schedulePoll(0);
  }

  /* What a title reply says of its download, into what this page knows. A
     null state counts only when the Arr answered, and not just after this
     page's own Download, which the Arr may not list yet. *opened*: the title
     was just opened, so its cards are brought into line even with no change.
     Says whether the panel has changed. */
  function takeDownload(ref, dl, opened) {
    if (!dl) return false;
    var k = stateOf(ref);
    var was = k.state + ':' + k.pct;
    var wasTracked = k.tracked;
    var st = dl.state || null;
    if (st) {
      k.state = st;
      k.pct = st === 'downloading' && typeof dl.pct === 'number' ? Math.max(0, Math.min(100, Math.round(dl.pct)))
        : st === 'downloading' ? k.pct : null;
      k.tracked = false;
    } else if (dl.answered === true && !inGrace(k)) {
      if (inFlight(k)) {
        k.state = null;
        k.pct = null;
      }
      k.tracked = false;
    }
    var moved = k.state + ':' + k.pct !== was;
    if (moved || (opened && (k.state || dl.answered === true))) fire(ref, k.state, k.pct);
    return moved || k.tracked !== wasTracked;
  }

  /* A later reply of a title asked for again, into the panel's facts and what
     this page knows. Says whether the panel has changed. */
  function takeReply(ref, data) {
    if (data.in_library || data.library_ref) {
      nowInLibrary(ref);
      return true;
    }
    if (cur && cur.ref === ref && cur.data) {
      data.reason = data.reason || cur.reason;
      cur.data = data;
    }
    return takeDownload(ref, data.download);
  }

  /* Load the title again without the skeleton (after a Keep or a finished
     download); what changed is swapped in where it is. */
  function refresh() {
    var c = cur;
    if (!c || !isOpen || !c.data) return;
    get(apiUrl(c.ref)).then(function (data) {
      if (c !== cur || !isOpen) return;
      if (data.library_ref && data.library_ref !== c.ref && REF_RE.test(data.library_ref)) {
        c.justAdded = true;
        c.reason = c.reason || data.reason;
        if (hashRef() === c.ref) {
          var state = window.history.state && window.history.state.mmTitle ? { mmTitle: data.library_ref } : null;
          window.history.replaceState(state, '', base() + '#title=' + encodeURIComponent(data.library_ref));
        }
        c.ref = data.library_ref;
        refresh();
        return;
      }
      if (!data.reason && c.reason) data.reason = c.reason;
      c.data = data;
      if (data.leaving && data.leaving.keep_id) c.keepId = String(data.leaving.keep_id);
      renderContent(false);
      if (isAdmin && /^(movie|show)-/.test(c.ref)) loadFacts(c);
    }).catch(function () {});
  }

  function loadFacts(c) {
    get('/api/watch/admin/title/' + encodeURIComponent(c.ref)).then(function (facts) {
      if (c !== cur || !isOpen) return;
      c.facts = facts;
      renderFacts(true);
    }).catch(function (err) {
      // Deleted from under the overlay: nothing left to show.
      if (err && err.status === 404 && c === cur && c.facts) close();
    });
  }

  /* ── Rendering ── */

  function renderLoading() {
    root.setAttribute('aria-busy', 'true');
    root.classList.remove('ttl--admin');
    hide(heroSkel, false);
    hide(art, true);
    art.removeAttribute('src');
    art.classList.remove('is-loaded');
    kindEl.textContent = cur.kind ? kindLabel({ kind: cur.kind }) : '';
    titleEl.textContent = cur.title;
    if (!cur.title) titleEl.appendChild(skel('skel--line ttl__title-skel'));
    hide(taglineEl, true);
    metaEl.textContent = '';
    metaEl.appendChild(skel('skel--line ttl__meta-skel'));
    player.textContent = '';

    body.textContent = '';
    var lead = el('div', 'ttl__lead');
    var main = el('div', 'ttl__main');
    var actions = el('div', 'ttl__actions');
    actions.appendChild(skel('skel--pill', '150px'));
    actions.appendChild(skel('skel--pill', '120px'));
    main.appendChild(actions);
    var lines = el('div', 'ttl-skel-lines');
    lines.appendChild(skel('skel--line', '220px'));
    lines.appendChild(skel('skel--line'));
    lines.appendChild(skel('skel--line'));
    lines.appendChild(skel('skel--line', '72%'));
    main.appendChild(lines);
    lead.appendChild(main);
    body.appendChild(lead);
    var cast = el('section', 'ttl__castsec');
    cast.setAttribute('aria-hidden', 'true');
    cast.appendChild(skel('skel--line ttl__sec-skel'));
    var grid = el('div', 'ttl-cast');
    for (var i = 0; i < 8; i++) {
      var item = el('div', 'ttl-cast__item');
      item.appendChild(skel('skel--circle ttl-cast__avatar'));
      item.appendChild(skel('skel--line ttl-cast__name-skel'));
      grid.appendChild(item);
    }
    cast.appendChild(grid);
    body.appendChild(cast);
  }

  function renderFailed(err) {
    root.removeAttribute('aria-busy');
    hide(heroSkel, true);
    metaEl.textContent = '';
    if (!cur.title) titleEl.textContent = 'Details';
    body.textContent = '';
    var box = el('div', 'ttl__failed fade-in');
    box.setAttribute('role', 'alert');
    box.appendChild(icon('fa-circle-exclamation'));
    box.appendChild(el('p', 'ttl__failed-line', "Couldn't load details"));
    var why = err && err.status === 404 ? 'This title is not available any more.'
      : err && err.error === 'tmdb_not_configured' ? 'Titles outside the library need TMDB, which is not set up.'
      : err && err.error === 'network_error' ? err.message
      : 'Something went wrong. Try again in a moment.';
    box.appendChild(el('p', 'ttl__failed-why', why));
    var row = el('div', 'ttl__failed-actions');
    var retry = button('btn btn--lg watch-btn--grey', 'Try again');
    retry.addEventListener('click', function () {
      if (!cur) return;
      renderLoading();
      titleEl.focus({ preventScroll: true });
      load(cur);
    });
    var shut = button('btn btn--lg btn--ghost', 'Close');
    shut.addEventListener('click', requestClose);
    row.appendChild(retry);
    row.appendChild(shut);
    box.appendChild(row);
    body.appendChild(box);
  }

  function kindLabel(d) { return d.kind === 'movie' ? 'Movie' : 'TV series'; }

  function metaParts(d) {
    var parts = [];
    if (d.year) parts.push(d.year_end && d.year_end !== d.year ? d.year + '–' + d.year_end : String(d.year));
    if (d.kind === 'movie' && d.runtime_min) parts.push(dur(d.runtime_min));
    if (d.kind !== 'movie' && d.season_count) parts.push(plural(d.season_count, 'season', 'seasons'));
    if (d.genres && d.genres.length) parts.push(d.genres.slice(0, 3).join(', '));
    return parts;
  }

  function renderHead(d, fresh) {
    kindEl.textContent = kindLabel(d);
    titleEl.textContent = d.title || cur.title;
    taglineEl.textContent = d.tagline || '';
    hide(taglineEl, !d.tagline);
    metaEl.textContent = '';
    metaParts(d).forEach(function (p) { metaEl.appendChild(el('span', '', p)); });
    if (d.certificate) {
      var cert = el('span', 'ttl__cert', d.certificate);
      cert.setAttribute('aria-label', 'Rated ' + d.certificate);
      metaEl.appendChild(cert);
    }
    var src = safePath(d.backdrop_url) || safePath(d.poster_url);
    hide(heroSkel, true);
    if (src && art.getAttribute('src') !== src) {
      art.classList.remove('is-loaded');
      art.onload = function () { art.classList.add('is-loaded'); };
      art.onerror = function () { hide(art, true); };
      art.setAttribute('src', src);
    }
    hide(art, !src);
    if (fresh) [kindEl, metaEl, taglineEl].forEach(core.fadeIn);
  }

  /* Re-render the content, keeping focus on the same control when it is
     drawn again (or on the first action when it is gone). */
  function renderContent(fresh) {
    var d = cur.data;
    var key = focusKey();
    root.removeAttribute('aria-busy');
    renderHead(d, fresh);
    body.textContent = '';
    var lead = el('div', 'ttl__lead');
    var main = el('div', 'ttl__main');
    lead.appendChild(main);
    if (d.leaving && cur.keepId) {
      var line = el('p', 'ttl__leaving');
      line.appendChild(icon('fa-clock'));
      line.appendChild(document.createTextNode(leavingText(d.leaving.days) + ' Keep it to stop the deletion.'));
      main.appendChild(line);
    }
    var actions = el('div', 'ttl__actions');
    actions.setAttribute('data-ttl-part', 'actions');
    var under = el('div', 'ttl__under');
    under.setAttribute('data-ttl-part', 'under');
    main.appendChild(actions);
    main.appendChild(under);
    fillActions(actions, under);
    hide(under, !under.children.length);
    var ratings = ratingsRow(d);
    if (ratings) main.appendChild(ratings);
    if (d.overview) main.appendChild(el('p', 'ttl__overview', d.overview));
    if (d.director && d.kind === 'movie') {
      var credit = el('p', 'ttl__credit');
      credit.appendChild(el('span', 'ttl__credit-label', 'Director'));
      credit.appendChild(document.createTextNode(d.director));
      main.appendChild(credit);
    }
    if (d.reason) {
      var reason = el('p', 'ttl__reason');
      reason.appendChild(el('b', '', "Why it's here: "));
      reason.appendChild(document.createTextNode(d.reason));
      main.appendChild(reason);
    }
    body.appendChild(lead);
    if (d.in_library && d.kind !== 'movie' && d.episodes && d.episodes.length) body.appendChild(episodesSection(d));
    if (d.cast && d.cast.length) body.appendChild(castSection(d.cast));
    if (cur.facts) renderFacts(false);
    if (fresh) {
      toArray(main.children).concat(toArray(body.children).slice(1)).forEach(function (n) {
        n.classList.add('is-swapping-in');
      });
    }
    restoreFocus(key);
  }

  function leavingText(days) {
    if (days <= 0) return 'Leaving today.';
    return 'Leaving in ' + plural(days, 'day', 'days') + '.';
  }

  function ratingsRow(d) {
    var r = d.ratings || {};
    var pills = [];
    function pill(cls, glyph, text) {
      var p = el('span', 'ttl-rating ' + cls);
      if (glyph) {
        var g = el('span', 'ttl-rating__icon', glyph);
        g.setAttribute('aria-hidden', 'true');
        p.appendChild(g);
      }
      p.appendChild(document.createTextNode(text));
      pills.push(p);
    }
    if (r.imdb) pill('ttl-rating--imdb', '★', 'IMDb ' + r.imdb);
    if (r.rt) pill('ttl-rating--rt', '🍅', 'RT ' + r.rt);
    if (typeof r.tmdb === 'number' && r.tmdb > 0) pill('ttl-rating--tmdb', '★', 'TMDB ' + r.tmdb.toFixed(1));
    if (r.metascore) pill('ttl-rating--meta', '', 'Metascore ' + String(r.metascore).replace(/\/100$/, ''));
    if (!pills.length) return null;
    var row = el('div', 'ttl__ratings');
    pills.forEach(function (p) { row.appendChild(p); });
    return row;
  }

  function castSection(cast) {
    var sec = el('section', 'ttl__castsec');
    var h = el('h3', 'ttl__sec-title', 'Cast');
    sec.appendChild(h);
    var grid = el('ul', 'ttl-cast');
    grid.setAttribute('aria-label', 'Cast');
    cast.slice(0, 12).forEach(function (c) {
      var item = el('li', 'ttl-cast__item');
      var avatar = el('span', 'ttl-cast__avatar');
      avatar.setAttribute('aria-hidden', 'true');
      var src = safePath(c.profile_url);
      if (src) {
        var img = el('img');
        img.alt = '';
        img.loading = 'lazy';
        img.setAttribute('src', src);
        avatar.appendChild(img);
      } else {
        avatar.textContent = (c.name || '?').split(' ').map(function (w) { return w.charAt(0); }).slice(0, 2).join('');
      }
      item.appendChild(avatar);
      item.appendChild(el('span', 'ttl-cast__name', c.name || ''));
      if (c.character) item.appendChild(el('span', 'ttl-cast__char', c.character));
      grid.appendChild(item);
    });
    sec.appendChild(grid);
    return sec;
  }

  /* ── Focus ── */

  function focusKey() {
    var a = document.activeElement;
    if (!a || !root.contains(a)) return null;
    var keyed = a.closest && a.closest('[data-ttl-act]');
    return keyed ? keyed.getAttribute('data-ttl-act') : null;
  }

  function restoreFocus(key) {
    if (!key) return;
    var target = root.querySelector('[data-ttl-act="' + key + '"]') ||
      root.querySelector('[data-ttl-part="actions"] > *');
    if (target) target.focus({ preventScroll: true });
    else titleEl.focus({ preventScroll: true });
  }

  /* ── Actions ── */

  function fillActions(actions, under) {
    var d = cur.data;
    if (d.in_library && d.play && !cur.justAdded) {
      actions.appendChild(playLink(d.play));
      if (d.play.progress_pct) {
        var progress = el('div', 'ttl__progress');
        var bar = el('progress', 'watch-bar watch-bar--banner');
        bar.max = 100;
        bar.value = d.play.progress_pct;
        bar.setAttribute('aria-hidden', 'true');
        progress.appendChild(bar);
        if (d.play.minutes_left) progress.appendChild(el('span', '', dur(d.play.minutes_left) + ' left'));
        under.appendChild(progress);
      }
    } else if (!d.in_library || cur.justAdded) {
      downloadPart(actions, under);
    }
    var trailer = trailerButton(d);
    if (trailer) actions.appendChild(trailer);
    var keep = keepButton(d);
    if (keep) actions.appendChild(keep);
    var share = shareButton();
    if (share) actions.appendChild(share);
    if (cur.shareMsg) under.appendChild(note(cur.shareMsg));
    if (cur.shareUrl) under.appendChild(shareField(cur.shareUrl));
  }

  function playLink(play) {
    var a = el('a', 'btn btn--lg watch-btn--play');
    a.setAttribute('href', safePath(play.href) || '#');
    a.setAttribute('data-ttl-act', 'primary');
    a.appendChild(icon('fa-play'));
    a.appendChild(document.createTextNode(play.label || 'Play'));
    return a;
  }

  function note(text, iconName) {
    var p = el('p', 'ttl__note');
    if (iconName) p.appendChild(icon(iconName));
    p.appendChild(el('span', '', text));
    return p;
  }

  /* A viewer keeps a leaving title through the shared Keep menu (keep.js);
     an admin keeps from the facts panel. */
  function keepButton(d) {
    var api = window.MMWatch.admin;
    if (!d.keep_allowed || !cur.keepId || (isAdmin && api && api.keep)) return null;
    var b = el('button', 'btn watch-btn--keep');
    b.type = 'button';
    b.setAttribute('data-watch-keep', cur.keepId);
    b.setAttribute('data-ttl-act', 'keep');
    b.setAttribute('aria-haspopup', 'menu');
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-label', 'Keep ' + (d.title || cur.title));
    b.appendChild(el('span', '', 'Keep'));
    b.appendChild(icon('fa-chevron-down watch-keep__chev'));
    return b;
  }

  /* ── Download ── */

  function stateOf(ref) {
    if (!known[ref]) known[ref] = { state: null, pct: null, sizeDone: '', sizeTotal: '', eta: '', busy: false, error: null, sentAt: 0, tracked: false, title: null, kind: null, year: null };
    return known[ref];
  }

  function inFlight(k) { return k.state === 'queued' || k.state === 'downloading'; }

  function inGrace(k) { return !!k.sentAt && Date.now() - k.sentAt < DOWNLOAD_GRACE_MS; }

  function seasonPickable(d) {
    return d.kind !== 'movie' && d.download && d.download.request && d.download.request.body &&
      d.seasons && d.seasons.length;
  }

  function picksOf(d) {
    if (!cur.picks) {
      cur.picks = {};
      (d.seasons || []).forEach(function (s) { if (!s.in_library) cur.picks[s.number] = true; });
    }
    return cur.picks;
  }

  function pickedSeasons(d) {
    var picks = picksOf(d);
    return (d.seasons || []).filter(function (s) { return !s.in_library && picks[s.number]; })
      .map(function (s) { return s.number; });
  }

  function statePill(cls, text, iconName, spinner) {
    var s = el('span', 'dl-state ' + cls);
    s.setAttribute('role', 'status');
    s.setAttribute('tabindex', '-1');
    s.setAttribute('data-ttl-act', 'primary');
    if (spinner) {
      var sp = el('span', 'btn-spinner');
      sp.setAttribute('aria-hidden', 'true');
      s.appendChild(sp);
    } else if (iconName) {
      s.appendChild(icon(iconName));
    }
    s.appendChild(document.createTextNode(text));
    return s;
  }

  function downloadPart(actions, under) {
    var d = cur.data;
    var k = stateOf(cur.ref);
    var rec = /^rec-/.test(cur.ref) || !!cur.recId;
    if (k.state === 'in_library' || cur.justAdded) {
      actions.appendChild(d.play && d.in_library ? playLink(d.play) : statePill('dl-state--done', 'In library', 'fa-circle-check'));
      if (cur.addedNow) under.appendChild(note('In your library. Added just now.', 'fa-circle-check ttl__note-ok'));
      return;
    }
    if (k.state === 'queued') {
      actions.appendChild(statePill('dl-state--queued', 'Queued', 'fa-clock'));
      var q = el('p', 'ttl__note');
      var words = el('span', '', 'Waiting for a download slot. Follow it in ');
      var link = el('a', '', 'Downloads');
      link.setAttribute('href', '/downloads');
      words.appendChild(link);
      words.appendChild(document.createTextNode('.'));
      q.appendChild(words);
      under.appendChild(q);
      if (rec && d.download && d.download.notify === true) under.appendChild(emailHint(d));
      return;
    }
    if (k.state === 'downloading') {
      var pct = k.pct || 0;
      actions.appendChild(statePill('dl-state--downloading', 'Downloading ' + pct + '%', null, true));
      var box = el('div', 'dl-progress');
      var bar = el('progress', 'watch-bar watch-bar--banner');
      bar.max = 100;
      bar.value = pct;
      bar.setAttribute('aria-label', 'Download progress');
      box.appendChild(bar);
      var line = el('div', 'dl-progress__line');
      line.appendChild(el('span', '', k.sizeDone && k.sizeTotal ? k.sizeDone + ' of ' + k.sizeTotal : ''));
      line.appendChild(el('span', '', k.eta ? k.eta.replace(/^~/, 'about ').replace(/ remaining$/, ' left') : ''));
      box.appendChild(line);
      under.appendChild(box);
      if (rec && d.download && d.download.notify === true) under.appendChild(emailHint(d));
      return;
    }
    if (!d.download) return;
    var tv = d.kind !== 'movie';
    if (k.tracked || (tv && d.sonarr_tracked)) {
      actions.appendChild(statePill('dl-state--done', tv ? 'Already in Sonarr' : 'Already in Radarr', 'fa-circle-check'));
      return;
    }
    if (tv && d.seasons && d.seasons.length && d.seasons.every(function (s) { return s.in_library; })) {
      actions.appendChild(statePill('dl-state--done', 'All seasons in library', 'fa-circle-check'));
      return;
    }
    var dl = button('btn btn--lg btn--primary ttl__download', '', 'fa-download');
    dl.setAttribute('data-ttl-act', 'primary');
    dl.appendChild(el('span', 'ttl__download-label'));
    if (k.busy) {
      dl.textContent = '';
      var spin = el('span', 'btn-spinner');
      spin.setAttribute('aria-hidden', 'true');
      dl.appendChild(spin);
      dl.appendChild(el('span', 'ttl__download-label', 'Adding…'));
      dl.setAttribute('aria-disabled', 'true');
    }
    dl.addEventListener('click', submitDownload);
    actions.appendChild(dl);
    if (seasonPickable(d)) under.appendChild(seasonPick(d));
    if (!k.busy) labelDownload(dl, d);
    if (k.error) {
      var err = el('p', 'ttl__error');
      err.setAttribute('role', 'alert');
      err.appendChild(icon('fa-circle-exclamation'));
      err.appendChild(el('span', '', k.error));
      var again = button('ttl__retry', 'Try again');
      again.setAttribute('data-ttl-act', 'retry');
      again.addEventListener('click', submitDownload);
      err.appendChild(again);
      under.appendChild(err);
    }
  }

  function labelDownload(dl, d) {
    var label = dl.querySelector('.ttl__download-label');
    if (!seasonPickable(d)) {
      label.textContent = 'Download';
      return;
    }
    var n = pickedSeasons(d).length;
    label.textContent = n ? 'Download ' + plural(n, 'season', 'seasons') : 'Pick seasons';
    if (n) dl.removeAttribute('aria-disabled'); else dl.setAttribute('aria-disabled', 'true');
  }

  /* Only for a pick being downloaded, and only to a person the server says it
     will email. */
  function emailHint(d) {
    return note("You can close this — you'll get an email when " +
      (d.kind === 'movie' ? 'the movie' : 'the show') + " is ready.", 'fa-envelope');
  }

  function seasonPick(d) {
    var wrap = el('div', 'season-pick');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-labelledby', 'ttl-pick-label');
    var head = el('div', 'season-pick__head');
    var label = el('span', '', 'Seasons to download');
    label.id = 'ttl-pick-label';
    head.appendChild(label);
    var all = button('season-pick__all', 'Select all');
    all.setAttribute('data-ttl-act', 'pick-all');
    var none = button('season-pick__all season-pick__clear', 'Clear');
    none.setAttribute('data-ttl-act', 'pick-none');
    head.appendChild(all);
    head.appendChild(none);
    wrap.appendChild(head);
    var list = el('div', 'season-pick__list');
    var picks = picksOf(d);
    d.seasons.forEach(function (s) {
      var chip = el('button', 'season-chip');
      chip.type = 'button';
      chip.setAttribute('role', 'checkbox');
      chip.setAttribute('data-season', String(s.number));
      chip.setAttribute('data-ttl-act', 'season-' + s.number);
      chip.setAttribute('aria-checked', s.in_library || picks[s.number] ? 'true' : 'false');
      if (s.in_library) chip.setAttribute('aria-disabled', 'true');
      var box = el('span', 'season-chip__box');
      box.setAttribute('aria-hidden', 'true');
      box.appendChild(icon('fa-check'));
      chip.appendChild(box);
      chip.appendChild(el('span', 'season-chip__name', s.name || 'Season ' + s.number));
      var sub = s.in_library ? 'In library'
        : s.episode_count ? plural(s.episode_count, 'episode', 'episodes') : '';
      if (sub) chip.appendChild(el('span', 'season-chip__sub', sub));
      chip.addEventListener('click', function () {
        if (s.in_library || stateOf(cur.ref).busy) return;
        picks[s.number] = !picks[s.number];
        syncPicks(wrap, d);
      });
      list.appendChild(chip);
    });
    wrap.appendChild(list);
    var summary = el('p', 'season-pick__summary');
    summary.setAttribute('aria-live', 'polite');
    wrap.appendChild(summary);
    function setAll(on) {
      if (stateOf(cur.ref).busy) return;
      d.seasons.forEach(function (s) { if (!s.in_library) picks[s.number] = on; });
      syncPicks(wrap, d);
    }
    all.addEventListener('click', function () { setAll(true); });
    none.addEventListener('click', function () { setAll(false); });
    syncPicks(wrap, d, true);
    return wrap;
  }

  /* Picking changes the chips, the summary and the button's count in place. */
  function syncPicks(wrap, d, first) {
    var picks = picksOf(d);
    toArray(wrap.querySelectorAll('.season-chip')).forEach(function (chip) {
      var n = Number(chip.getAttribute('data-season'));
      if (chip.getAttribute('aria-disabled') === 'true') return;
      chip.setAttribute('aria-checked', picks[n] ? 'true' : 'false');
    });
    var ticked = pickedSeasons(d).length;
    var already = d.seasons.filter(function (s) { return s.in_library; }).length;
    var summary = wrap.querySelector('.season-pick__summary');
    summary.textContent = '';
    if (ticked) {
      summary.appendChild(el('strong', '', plural(ticked, 'season', 'seasons')));
      summary.appendChild(document.createTextNode(' will be added and searched'));
    }
    if (already) {
      if (ticked) summary.appendChild(document.createTextNode(' · '));
      summary.appendChild(el('strong', '', plural(already, 'season', 'seasons')));
      summary.appendChild(document.createTextNode(' already in library'));
    }
    if (!ticked && !already) summary.textContent = 'No seasons picked';
    if (first) return;
    var dl = root.querySelector('.ttl__download');
    if (dl) labelDownload(dl, d);
  }

  function submitDownload() {
    var c = cur;
    var d = c && c.data;
    if (!d || !d.download || !d.download.request) return;
    var k = stateOf(c.ref);
    if (k.busy || k.state) return;
    var req = d.download.request;
    var url = safePath(req.url);
    if (!url) return;
    var payload;
    if (req.body) {
      payload = Object.assign({}, req.body);
      if (seasonPickable(d)) {
        var ticked = pickedSeasons(d);
        if (!ticked.length) return;
        var held = d.seasons.filter(function (s) { return s.in_library; }).map(function (s) { return s.number; });
        payload.monitored_seasons = ticked.concat(held);
        payload.search_seasons = ticked;
      }
    }
    var ref = c.ref;
    var title = d.title || c.title;
    k.busy = true;
    k.error = null;
    redrawActions(ref);
    MM.api.post(url, payload).then(function () {
      k.busy = false;
      k.state = 'queued';
      k.pct = null;
      k.sentAt = Date.now();
      fire(ref, 'queued', null);
      core.toast(title + ' is queued.', { variant: 'success' });
      redrawActions(ref);
      if (cur && cur.ref === ref && isOpen) {
        cur.movedAt = Date.now();
        schedulePoll(POLL_MS);
        if (cur.recId) askNotify(cur);
      }
    }).catch(function (err) {
      // 409: the Arr already has it, so trying again can only fail again. The
      // title asked for again says whether it is queued, coming in or in the
      // library. A movie's panel stays busy until it answers; a show is already
      // in Sonarr whatever of it is downloaded, and says so meanwhile.
      if (err && err.status === 409) {
        var movie = d.kind === 'movie';
        core.toast(MM.api.messageOf(err, title + ' is already in ' + (movie ? 'Radarr.' : 'Sonarr.')));
        if (!movie) {
          k.busy = false;
          k.tracked = true;
          redrawActions(ref);
        }
        settleExisting(ref, k);
        return;
      }
      k.busy = false;
      k.error = "Couldn't add " + title + ': ' + MM.api.messageOf(err, 'Something went wrong. Try again.');
      redrawActions(ref);
    });
  }

  /* Whether an accepted download of a pick will be emailed to this person is
     the title's own reply to say; until it does, nothing is promised. */
  function askNotify(c) {
    var ref = c.ref;
    get(apiUrl(ref)).then(function (data) {
      if (c !== cur || !isOpen || c.ref !== ref || !data.download || data.download.notify !== true) return;
      c.data.download.notify = true;
      redrawActions(ref);
    }).catch(function () {});
  }

  /* A title the Arr already has: what its reply says is the title's state, on
     the panel and on every card of it. */
  function settleExisting(ref, k) {
    get(apiUrl(ref)).then(function (data) {
      k.busy = false;
      takeReply(ref, data);
      // The Arr has it yet lists nothing: pressing Download again can only 409 again.
      k.tracked = !k.state;
      redrawActions(ref);
      if (!cur || cur.ref !== ref || !isOpen) return;
      if (data.library_ref) refresh();
      else {
        cur.movedAt = Date.now();
        schedulePoll(POLL_MS);
      }
    }).catch(function () {
      k.busy = false;
      k.tracked = true;
      redrawActions(ref);
      if (cur && cur.ref === ref) schedulePoll(POLL_SLOW_MS);
    });
  }

  /* Swap the actions and the line under them for the title's current state,
     in place: the state takes the button's spot at the button's size. */
  function redrawActions(ref) {
    if (!isOpen || !cur || cur.ref !== ref || !cur.data) return;
    var actions = root.querySelector('[data-ttl-part="actions"]');
    var under = root.querySelector('[data-ttl-part="under"]');
    if (!actions || !under) return;
    var key = focusKey();
    var before = actions.querySelector('[data-ttl-act="primary"]');
    var beforeClass = before ? before.className : '';
    actions.textContent = '';
    under.textContent = '';
    fillActions(actions, under);
    hide(under, !under.children.length);
    var after = actions.querySelector('[data-ttl-act="primary"]');
    if (after && after.className !== beforeClass) core.fadeIn(after);
    restoreFocus(key);
  }

  /* ── Download progress ── */

  function pollable() {
    if (!cur || !cur.data || !cur.data.download || !isOpen) return false;
    var k = stateOf(cur.ref);
    return inFlight(k) || k.tracked;
  }

  function stopPolling() {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(delay) {
    stopPolling();
    if (!pollable() || document.hidden) return;
    if (!cur.movedAt) cur.movedAt = Date.now();
    pollTimer = setTimeout(poll, inFlight(stateOf(cur.ref)) ? delay : POLL_SLOW_MS);
  }

  /* Every 5 s while the download moves, every 30 s once it has sat still
     for two minutes. */
  function nextPoll() {
    return Date.now() - cur.movedAt < POLL_BACKOFF_AFTER_MS ? POLL_MS : POLL_SLOW_MS;
  }

  /* The Arr no longer queues the download: ask the title again, whose reply
     says whether the Arr still holds it. */
  function recheck(c) {
    var k = stateOf(c.ref);
    if (k.state !== 'queued' || inGrace(k)) return;
    if (c.checkedAt && Date.now() - c.checkedAt < RECHECK_MS) return;
    c.checkedAt = Date.now();
    askAgain(c).catch(function () {});
  }

  /* Ask the open title again and show what its reply says. */
  function askAgain(c) {
    var ref = c.ref;
    return get(apiUrl(ref)).then(function (data) {
      if (c !== cur || !isOpen || c.ref !== ref) return;
      if (!takeReply(ref, data)) return;
      c.movedAt = Date.now();
      redrawActions(ref);
      if (data.library_ref) refresh();
    });
  }

  function poll() {
    pollTimer = null;
    if (!pollable()) return;
    var c = cur;
    var ref = c.ref;
    if (!inFlight(stateOf(ref))) {
      // Already in the Arr with no download to follow: the title may yet be
      // queued there, or removed from it.
      askAgain(c).then(function () {
        if (c === cur && c.ref === ref) schedulePoll(POLL_MS);
      }, function (err) { pollFailed(err, c, ref); });
      return;
    }
    var dl = c.data.download;
    var url = '/api/download/status?service=' + encodeURIComponent(dl.service) +
      '&tmdb_id=' + encodeURIComponent(dl.tmdb_id);
    MM.api.get(url).then(function (r) {
      var k = stateOf(ref);
      var was = k.state + ':' + k.pct;
      // 'unknown' is an Arr that did not answer: no news, so nothing changes.
      if (r.state === 'ready') {
        k.state = 'in_library';
        k.pct = null;
      } else if (r.state === 'downloading' || r.state === 'almost_ready') {
        k.state = 'downloading';
        k.pct = Math.max(0, Math.min(100, Math.round(r.progress || 0)));
        k.sizeDone = r.size_done || '';
        k.sizeTotal = r.size_total || '';
        k.eta = r.eta || '';
      } else if (r.state === 'queued' || r.state === 'searching') {
        k.state = 'queued';
        k.pct = null;
      }
      if (k.state + ':' + k.pct !== was) {
        if (c === cur) c.movedAt = Date.now();
        fire(ref, k.state, k.pct);
        if (k.state === 'in_library' && c === cur) {
          c.justAdded = true;
          c.addedNow = !!k.sentAt;
          redrawActions(ref);
          refresh();
        } else {
          redrawActions(ref);
        }
      }
      if (c !== cur || c.ref !== ref) return;
      if (r.state === 'searching') recheck(c);
      schedulePoll(r.state === 'unknown' ? POLL_SLOW_MS : nextPoll());
    }).catch(function (err) { pollFailed(err, c, ref); });
  }

  function pollFailed(err, c, ref) {
    if (err && (err.status === 401 || err.status === 403)) return;
    if (c === cur && c.ref === ref) schedulePoll(POLL_SLOW_MS);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopPolling();
    else if (pollable()) schedulePoll(0);
  });

  /* ── Trailer ── */

  function trailerKey(d) {
    return d && d.trailer_key && YOUTUBE_KEY_RE.test(d.trailer_key) ? d.trailer_key : null;
  }

  function trailerButton(d) {
    if (!trailerKey(d)) return null;
    var on = root.classList.contains('is-trailer');
    var b = button('btn btn--lg watch-btn--grey ttl__trailer', '', on ? 'fa-xmark' : 'fa-film');
    b.setAttribute('data-ttl-act', 'trailer');
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.appendChild(document.createTextNode(on ? 'Close trailer' : 'Trailer'));
    b.addEventListener('click', function () {
      if (root.classList.contains('is-trailer')) closeTrailer(true); else openTrailer();
    });
    return b;
  }

  function setTrailerButton(on) {
    var b = root.querySelector('[data-ttl-act="trailer"]');
    if (!b) return;
    b.textContent = '';
    b.appendChild(icon(on ? 'fa-xmark' : 'fa-film'));
    b.appendChild(document.createTextNode(on ? 'Close trailer' : 'Trailer'));
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  /* The player takes the hero's box, so nothing below it moves. */
  function openTrailer() {
    var key = cur && trailerKey(cur.data);
    if (!key) return;
    player.textContent = '';
    var frame = el('iframe', 'ttl__frame');
    frame.setAttribute('src', 'https://www.youtube-nocookie.com/embed/' + key + '?autoplay=1&rel=0&playsinline=1');
    frame.setAttribute('title', 'Trailer: ' + (cur.data.title || cur.title));
    frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox');
    frame.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
    frame.setAttribute('referrerpolicy', 'strict-origin');
    frame.setAttribute('allowfullscreen', '');
    player.appendChild(frame);
    player.setAttribute('aria-hidden', 'false');
    root.classList.add('is-trailer');
    setTrailerButton(true);
  }

  function closeTrailer(refocus) {
    if (!root.classList.contains('is-trailer')) return;
    root.classList.remove('is-trailer');
    player.setAttribute('aria-hidden', 'true');
    var frame = player.querySelector('iframe');
    // Gone at once stops the sound; the box fades as it empties.
    if (frame) frame.remove();
    setTrailerButton(false);
    var b = root.querySelector('[data-ttl-act="trailer"]');
    if (refocus && b) b.focus({ preventScroll: true });
  }

  /* ── Share (admins, Recommended picks) ── */

  /* The button is drawn from cur.shareState, so a redraw mid-share (a poll)
     keeps it as it was. */
  function shareButton() {
    if (!isAdmin || !cur.recId) return null;
    var st = cur.shareState;
    var b = button('btn btn--lg watch-btn--grey ttl__share');
    b.setAttribute('data-ttl-act', 'share');
    // The longest label, laid out unseen beside the icon, holds the button's width in
    // every state; the icon and the label sit centred together inside it.
    b.setAttribute('data-widest', 'Sharing…');
    var group = el('span', 'ttl__share-group');
    group.appendChild(icon(st === 'copied' ? 'fa-check' : 'fa-share-nodes'));
    group.appendChild(el('span', 'ttl__share-label', st === 'busy' ? 'Sharing…' : st === 'copied' ? 'Copied' : 'Share'));
    b.appendChild(group);
    if (st === 'busy') b.setAttribute('aria-disabled', 'true');
    b.addEventListener('click', share);
    return b;
  }

  function setShare(c, st) {
    c.shareState = st;
    if (c !== cur || !isOpen) return;
    var b = root.querySelector('[data-ttl-act="share"]');
    var fresh = shareButton();
    if (b && fresh) {
      var focused = document.activeElement === b;
      b.replaceWith(fresh);
      if (focused) fresh.focus({ preventScroll: true });
    }
  }

  function share() {
    var c = cur;
    if (!c || c.shareState === 'busy') return;
    clearTimeout(shareTimer);
    c.shareMsg = null;
    c.shareUrl = null;
    setShare(c, 'busy');
    MM.api.post('/api/recommended/' + encodeURIComponent(c.recId) + '/share-token').then(function (data) {
      var url = data && data.share_url;
      if (!url) throw new Error('no link');
      var copy = navigator.clipboard && navigator.clipboard.writeText
        ? navigator.clipboard.writeText(url) : Promise.reject(new Error('no clipboard'));
      return copy.then(function () {
        setShare(c, 'copied');
        shareTimer = setTimeout(function () { setShare(c, null); }, COPIED_MS);
      }, function () {
        c.shareUrl = url;
        setShare(c, null);
        redrawActions(c.ref);
        if (c !== cur) return;
        var field = root.querySelector('.ttl-share__input');
        if (field) { field.focus({ preventScroll: true }); field.select(); }
      });
    }).catch(function (err) {
      var status = err && err.status;
      c.shareMsg = status === 422 ? "This pick can't be shared: it has no TMDB id."
        : status === 503 ? 'Set the public address in Settings before sharing links.'
        : "Couldn't make a share link. Try again.";
      setShare(c, null);
      redrawActions(c.ref);
    });
  }

  function shareField(url) {
    var wrap = el('div', 'ttl-share');
    var label = el('label', 'ttl-share__label', 'Copy this link');
    label.setAttribute('for', 'ttl-share-url');
    var input = el('input', 'inp ttl-share__input');
    input.id = 'ttl-share-url';
    input.type = 'text';
    input.readOnly = true;
    input.value = url;
    input.setAttribute('data-ttl-act', 'share-field');
    input.addEventListener('focus', function () { input.select(); });
    wrap.appendChild(label);
    wrap.appendChild(input);
    return wrap;
  }

  /* ── Seasons and episodes (library shows) ── */

  function episodeRow(e) {
    var li = el('li');
    var a = el('a', 'watch-ep' + (e.state === 'in_progress' ? ' watch-ep--in-progress' : ''));
    a.setAttribute('href', safePath(e.href) || '#');
    a.appendChild(el('span', 'watch-ep__num', String(e.number)));
    var thumb = el('span', 'watch-ep__thumb');
    var src = safePath(e.thumb_url);
    if (src) {
      var img = el('img');
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('src', src);
      img.setAttribute('data-broken-on-error', '');
      thumb.appendChild(img);
    }
    if (e.state === 'in_progress' && e.progress_pct) {
      var bar = el('progress', 'watch-bar');
      bar.max = 100;
      bar.value = e.progress_pct;
      bar.setAttribute('aria-hidden', 'true');
      thumb.appendChild(bar);
    }
    a.appendChild(thumb);
    var text = el('span', 'watch-ep__text');
    text.appendChild(el('span', 'watch-ep__title', e.title || 'Episode ' + e.number));
    var meta = [];
    if (e.minutes) meta.push(dur(e.minutes));
    if (e.state === 'in_progress' && e.minutes_left) meta.push(dur(e.minutes_left) + ' left');
    text.appendChild(el('span', 'watch-ep__meta', meta.join(' · ')));
    a.appendChild(text);
    if (e.state === 'watched') a.appendChild(el('span', 'watch-ep__state', 'Watched'));
    else if (e.state === 'in_progress') a.appendChild(el('span', 'watch-ep__state watch-ep__state--active', 'In progress'));
    li.appendChild(a);
    return li;
  }

  function episodeList(episodes, label) {
    var ol = el('ol', 'watch-episodes');
    ol.setAttribute('aria-label', label);
    episodes.forEach(function (e) { ol.appendChild(episodeRow(e)); });
    return ol;
  }

  function seasonName(s) { return s.name || 'Season ' + s.number; }

  function episodesSection(d) {
    var sec = el('section', 'ttl__seasonsec');
    sec.appendChild(el('h3', 'ttl__sec-title', 'Episodes'));
    var selected = d.seasons.filter(function (s) { return s.selected; })[0] || d.seasons[0];
    if (cur.season === null && selected) cur.season = selected.number;
    if (d.seasons.length > 1) {
      var nav = el('div', 'watch-seasons');
      nav.setAttribute('role', 'group');
      nav.setAttribute('aria-label', 'Seasons');
      d.seasons.forEach(function (s) {
        var pill = button('watch-pill', seasonName(s));
        var on = s.number === cur.season;
        pill.classList.toggle('on', on);
        pill.setAttribute('aria-pressed', on ? 'true' : 'false');
        pill.setAttribute('data-season', String(s.number));
        pill.setAttribute('data-ttl-act', 'pill-' + s.number);
        pill.addEventListener('click', function () { pickSeason(s, sec); });
        nav.appendChild(pill);
      });
      sec.appendChild(nav);
    }
    sec.appendChild(episodeList(d.episodes, (selected ? seasonName(selected) : 'Season') + ' episodes'));
    return sec;
  }

  function markSeason(sec, number) {
    toArray(sec.querySelectorAll('.watch-pill')).forEach(function (p) {
      var on = Number(p.getAttribute('data-season')) === number;
      p.classList.toggle('on', on);
      p.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  /* A season pill swaps the list in place; focus stays on the pill. When the
     season will not load, the one on show stays and offers a retry. */
  function pickSeason(s, sec) {
    var c = cur;
    if (!c || (s.number === c.season && !c.seasonLoading)) return;
    var shown = c.season;
    var token = {};
    c.seasonLoading = token;
    markSeason(sec, s.number);
    var list = sec.querySelector('.watch-episodes');
    var oldErr = sec.querySelector('.ttl__error');
    if (oldErr) oldErr.remove();
    list.setAttribute('aria-busy', 'true');
    get(apiUrl(c.ref) + '/season/' + encodeURIComponent(s.number)).then(function (data) {
      if (c !== cur || c.seasonLoading !== token) return;
      c.seasonLoading = null;
      c.season = s.number;
      var fresh = episodeList(data.episodes || [], seasonName(s) + ' episodes');
      list.replaceWith(fresh);
      core.fadeIn(fresh);
    }).catch(function () {
      if (c !== cur || c.seasonLoading !== token) return;
      c.seasonLoading = null;
      markSeason(sec, shown);
      list.setAttribute('aria-busy', 'false');
      var err = el('p', 'ttl__error');
      err.setAttribute('role', 'alert');
      err.appendChild(el('span', '', 'Could not load ' + seasonName(s) + '.'));
      var again = button('ttl__retry', 'Try again');
      again.addEventListener('click', function () {
        var pill = sec.querySelector('.watch-pill[data-season="' + s.number + '"]');
        pickSeason(s, sec);
        if (pill) pill.focus({ preventScroll: true });
      });
      err.appendChild(again);
      list.before(err);
    });
  }

  /* ── Admin facts ── */

  var adminMenu = null;

  function renderFacts(fresh) {
    var f = cur.facts;
    var lead = body.querySelector('.ttl__lead');
    if (!f || !lead) return;
    var key = focusKey();
    var old = lead.querySelector('.ttl-facts');
    var aside = el('aside', 'ttl-facts');
    aside.setAttribute('aria-label', 'Library facts, admins only');
    var eyebrow = el('p', 'ttl-facts__eyebrow');
    eyebrow.appendChild(icon('fa-lock'));
    eyebrow.appendChild(document.createTextNode('Admin only'));
    aside.appendChild(eyebrow);
    var dl = el('dl', 'ttl-facts__list');
    function row(label, value, cls, sub) {
      dl.appendChild(el('dt', '', label));
      var dd = el('dd', cls || '', value);
      if (sub) dd.appendChild(el('span', 'ttl-facts__sub', sub));
      dl.appendChild(dd);
    }
    row('Size on disk', f.size || '—');
    row('Added', f.added ? day(f.added) : '—');
    row('Last watched', f.last_watched ? day(f.last_watched) : 'Never', f.last_watched ? '' : 'is-warning');
    if (f.deletion) {
      var days = f.deletion.days;
      row('Deletion', day(f.deletion.execute_at), days <= 7 ? 'is-danger' : '',
        days <= 0 ? 'today' : 'in ' + plural(days, 'day', 'days'));
    } else {
      row('Deletion', 'Not scheduled');
    }
    var keep = f.keep || { state: 'none', label: 'Not kept' };
    row('Keep', keep.label, keep.state !== 'none' ? 'is-success' : '');
    aside.appendChild(dl);
    var foot = factsFooter(f);
    if (foot) aside.appendChild(foot);
    if (old) old.replaceWith(aside); else lead.appendChild(aside);
    root.classList.add('ttl--admin');
    if (fresh) aside.classList.add('is-swapping-in');
    restoreFocus(key);
  }

  function factsFooter(f) {
    var api = window.MMWatch.admin;
    var actions = f.actions || {};
    if (!api) return null;
    var foot = el('div', 'ttl-facts__foot');
    var kept = !!(f.keep && f.keep.state !== 'none');
    // What the admin Keep reads: a scheduled title kept for days can be undone.
    var wrapper = actions.keep_wrapper && Object.assign({}, actions.keep_wrapper, {
      title: actions.keep_wrapper.show_title || cur.title, kept: kept, deletion: !!f.deletion
    });
    if (api.keep && actions.keep_wrapper) {
      var wrap = el('div', 'ttl-facts__keep');
      var trigger = button('btn watch-btn--keep ttl-facts__btn', '', null);
      trigger.setAttribute('data-ttl-act', 'admin-keep');
      trigger.setAttribute('aria-haspopup', 'menu');
      trigger.setAttribute('aria-expanded', 'false');
      if (f.keep && f.keep.state !== 'none') trigger.appendChild(icon('fa-check'));
      trigger.appendChild(document.createTextNode(f.keep && f.keep.state !== 'none' ? 'Kept' : 'Keep'));
      trigger.appendChild(icon('fa-chevron-down watch-keep__chev'));
      var menu = el('div', 'ttl-facts__menu');
      menu.setAttribute('role', 'menu');
      menu.setAttribute('aria-label', 'Keep for');
      menu.hidden = true;
      [['7 days', 'Keep 7 days'], ['30 days', 'Keep 30 days'], ['90 days', 'Keep 90 days'], ['forever', 'Keep forever']]
        .forEach(function (opt) {
          var item = button('watch-keep-menu__item', opt[1]);
          item.setAttribute('role', 'menuitem');
          item.setAttribute('tabindex', '-1');
          item.addEventListener('click', function () {
            closeAdminMenu(true);
            api.keep(wrapper, opt[0]);
          });
          menu.appendChild(item);
        });
      if (f.keep && f.keep.state !== 'none' && api.removeKeep) {
        var remove = button('watch-keep-menu__item', 'Remove keep');
        remove.setAttribute('role', 'menuitem');
        remove.setAttribute('tabindex', '-1');
        remove.addEventListener('click', function () {
          closeAdminMenu(true);
          api.removeKeep(wrapper);
        });
        menu.appendChild(remove);
      }
      trigger.addEventListener('click', function () {
        if (adminMenu) closeAdminMenu(false); else openAdminMenu(trigger, menu);
      });
      wrap.appendChild(trigger);
      wrap.appendChild(menu);
      foot.appendChild(wrap);
    }
    var del = actions.delete;
    var canDelete = del && (del.kind === 'movie' ? api.confirmDelete : api.pickSeasonDelete);
    if (canDelete) {
      var delBtn = button('btn btn--danger ttl-facts__btn', 'Delete', 'fa-trash');
      delBtn.setAttribute('data-ttl-act', 'admin-delete');
      if (del.kind !== 'movie') delBtn.setAttribute('aria-haspopup', 'dialog');
      delBtn.addEventListener('click', function () {
        if (del.kind === 'movie') api.confirmDelete(del.id, del.title, { kind: 'movie', size: f.size });
        else api.pickSeasonDelete(del.show_rk, del.title);
      });
      foot.appendChild(delBtn);
    }
    return foot.children.length ? foot : null;
  }

  /* The admin actions announce what they did; one on this title redraws the
     facts and actions, and a deleted movie closes the overlay (a show whose
     last season went closes once its facts are gone). */
  document.addEventListener('mm:watch-admin', function (e) {
    var d = e.detail || {};
    if (!isOpen || !cur || !cur.facts || d.phase !== 'done') return;
    var a = cur.facts.actions || {};
    var w = a.keep_wrapper || {};
    var del = a.delete || {};
    var ids = [w.id, del.id].filter(function (x) { return x != null; }).map(String);
    var showRk = w.show_rk || del.show_rk || '';
    var mine = (d.id && ids.indexOf(String(d.id)) >= 0) || (showRk && d.showRk === showRk);
    if (!mine) return;
    if (d.action === 'delete' && !d.showRk) {
      cur.fallback = rowFallback(cur.origin);
      requestClose();
    }
    else refresh();
  });

  function menuItems(menu) { return toArray(menu.querySelectorAll('[role="menuitem"]')); }

  function openAdminMenu(trigger, menu) {
    adminMenu = { trigger: trigger, menu: menu };
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    void menu.offsetWidth; // start the entrance from the closed look
    menu.classList.add('is-open');
    var first = menuItems(menu)[0];
    if (first) first.focus({ preventScroll: true });
  }

  function closeAdminMenu(refocus) {
    if (!adminMenu) return;
    var m = adminMenu;
    adminMenu = null;
    m.menu.classList.remove('is-open');
    m.menu.hidden = true;
    m.trigger.setAttribute('aria-expanded', 'false');
    if (refocus && document.body.contains(m.trigger)) m.trigger.focus({ preventScroll: true });
  }

  /* ── A viewer's Keep: keep.js marks the title busy while it saves; once it
     is not, the overlay loads the title again to show what changed. ── */

  if (typeof MutationObserver === 'function') {
    new MutationObserver(function (records) {
      if (!isOpen || !cur || !cur.keepId) return;
      var settled = records.some(function (r) {
        var t = r.target;
        return t.getAttribute('data-watch-keep') === cur.keepId &&
          r.oldValue === 'true' && t.getAttribute('aria-busy') === 'false';
      });
      if (settled) refresh();
    }).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['aria-busy'], attributeOldValue: true });
  }

  /* ── Keys and focus ── */

  var FOCUSABLE = 'a[href], button, input, select, textarea, iframe, [tabindex]';

  function focusables() {
    return toArray(root.querySelectorAll(FOCUSABLE)).filter(function (n) {
      if (n.getAttribute('tabindex') === '-1' || n.disabled) return false;
      if (n.closest('[hidden]')) return false;
      return n.getClientRects().length > 0;
    });
  }

  /* Elsewhere focus may go while the overlay is open: menus, dialogs and
     toasts opened over it. */
  function allowedOutside(node) {
    return !!(node && node.closest &&
      node.closest('[role="menu"], [role="dialog"], [role="alertdialog"], .modal-backdrop, .ui-toast, .ui-toast-host'));
  }

  document.addEventListener('keydown', function (e) {
    if (!isOpen) return;
    var target = e.target;
    var inside = root.contains(target) || target === document.body || target === document.documentElement || !target;
    if (e.key === 'Escape') {
      if (e.defaultPrevented || !inside) return;
      e.preventDefault();
      if (adminMenu) closeAdminMenu(true);
      else if (root.classList.contains('is-trailer')) closeTrailer(true);
      else requestClose();
      return;
    }
    if (adminMenu && adminMenu.menu.contains(target)) {
      var items = menuItems(adminMenu.menu);
      var i = items.indexOf(target);
      if (e.key === 'ArrowDown') items[(i + 1) % items.length].focus();
      else if (e.key === 'ArrowUp') items[(i - 1 + items.length) % items.length].focus();
      else if (e.key === 'Home') items[0].focus();
      else if (e.key === 'End') items[items.length - 1].focus();
      else if (e.key === 'Tab') { closeAdminMenu(true); return; }
      else return;
      e.preventDefault();
      return;
    }
    if (e.key !== 'Tab' || !inside) return;
    var list = focusables();
    if (!list.length) { e.preventDefault(); titleEl.focus(); return; }
    var first = list[0];
    var last = list[list.length - 1];
    var active = document.activeElement;
    if (e.shiftKey && (active === first || !root.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !root.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  });

  document.addEventListener('focusin', function (e) {
    if (!isOpen || root.contains(e.target) || allowedOutside(e.target)) return;
    titleEl.focus({ preventScroll: true });
  });

  /* ── Clicks ── */

  root.addEventListener('click', function (e) {
    var t = e.target;
    if (adminMenu && !adminMenu.menu.contains(t) && !adminMenu.trigger.contains(t)) closeAdminMenu(false);
    if (t.closest && t.closest('[data-ttl-close]')) {
      e.preventDefault();
      requestClose();
    }
  });

  function cardTitle(node) {
    var named = node.getAttribute('data-title') || node.getAttribute('aria-label') || '';
    if (named) return named;
    var caption = node.querySelector('.watch-card__title, .watch-card__name');
    return caption ? caption.textContent.trim() : '';
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest || e.defaultPrevented) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var card = t.closest('[data-title-ref]');
    if (!card || root.contains(card)) return;
    // A control inside the card (Keep) does its own thing.
    var control = t.closest('button, input, select, textarea, [data-watch-keep]');
    if (control && control !== card && card.contains(control)) return;
    var ref = card.getAttribute('data-title-ref');
    if (!REF_RE.test(ref)) return;
    e.preventDefault();
    open(ref, { origin: card, title: cardTitle(card), kind: card.getAttribute('data-kind'), push: true });
  });

  /* ── The address ── */

  window.addEventListener('popstate', function () {
    var ref = hashRef();
    if (ref) {
      if (!isOpen || !cur || cur.ref !== ref) open(ref, {});
    } else if (isOpen) {
      close();
    }
  });

  (function openFromAddress() {
    var old = /^#recommendation-([0-9]{1,10})$/.exec(window.location.hash || '');
    if (old) window.history.replaceState(null, '', base() + '#title=rec-' + old[1]);
    var ref = hashRef();
    if (ref) open(ref, {});
  })();

  window.MMWatch.detail = {
    open: function (ref, opts) {
      opts = opts || {};
      open(ref, { origin: opts.origin, title: opts.title, kind: opts.kind, push: opts.push !== false });
    },
    close: requestClose
  };
})();
