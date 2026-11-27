/* Watch: Watch next, the endless grid under the last row (_watch_next.html).
 *
 * Its first page is asked of /api/watch/next once the person scrolls the end of the rows within
 * AHEAD_VIEWS screens, never as the page opens; the next whenever the last card is as near. One
 * request at a time, each with the cursor the last answer gave; each filter (All, Movies, TV) starts
 * again from the head. A card is a discover card (watch/browse.js) with a line under its caption
 * that says why it is there; watch/preview.js says it in full. A title the grid holds already is
 * not added again.
 *
 * Far-off posters are let go, and past SHED_AFTER titles the rows far above are taken out and
 * stand as room, to be built again from the titles kept as they come near. On leaving the page
 * the titles, the filter and the cursor are kept for its entry in the history, so a page loaded
 * again by Back lays them out at once and watch/address.js puts the scroll back over them.
 *
 * Needs watch/core.js, watch/rows-load.js and watch/browse.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch && watch.core;
  var sec = core && core.byId('watch-next');
  if (!sec || !watch.browse || !watch.rowsLoad) return;

  var el = core.el;
  var byId = core.byId;
  var toArray = core.toArray;
  var icon = window.MM.dom.icon;
  var loader = watch.rowsLoad;

  var URL = '/api/watch/next';
  var STORE = 'mm:watch:next';
  var AHEAD_VIEWS = 1.5;    // a page is asked for when the last card is this near
  var AWAKE_VIEWS = 3;      // posters further off than this are let go
  var TOP_AFTER_VIEWS = 2;  // Back to top, this far into the section
  var SHED_AFTER = 600;     // titles given, past which rows far above are taken out
  var SHED_VIEWS = 10;      // rows wholly this far above
  var BACK_VIEWS = 3;       // they are built again once the first card left is this near
  var FAST_PX_MS = 3;       // a flick this fast: cards are simply there
  var STILL_MS = 120;       // a scroll this long ago has stopped
  var COL_MS = 32;
  var ROW_MS = 70;
  var MOST_MS = 360;
  var EMPTY_LIMIT = 3;      // answers in a row with nothing new: the sources have no more
  var BACKOFF_MS = [5000, 10000, 20000, 40000, 60000]; // after a 429; then Try again
  var FILTERS = ['all', 'movie', 'tv'];
  // Each source's icon and its words in the preview.
  var KINDS = {
    ai: ['i-sparkles', 'AI pick'],
    watched: ['i-clock-rotate-left', 'Because you watched'],
    search: ['i-magnifying-glass', 'Your search'],
    trending: ['i-trend', 'Trending'],
    taste: ['i-thumb-up', 'Your taste'],
    popular: ['i-users', 'Popular']
  };

  var grid = byId('watch-next-grid');
  var tail = byId('watch-next-tail');
  var end = byId('watch-next-end');
  var said = byId('watch-next-said');
  var note = sec.querySelector('.wn__note');
  var topButton = byId('watch-next-top');
  var who = sec.getAttribute('data-wn-who') || '';

  var filter = 'all';
  var items = [];        // every title given, in order; the grid holds those from shedCount on
  var shed = [];         // the rows taken out, each { count, height }, the latest last
  var shedCount = 0;
  var shedWidth = 0;     // the grid's width when they were measured
  var cursor = null;
  var state = 'idle';    // idle, loading, waiting (after a 429), failed, done
  var armed = false;     // the person has scrolled: only then is a first page asked for
  var switching = false; // the cards are of the filter before
  var ticket = 0;
  var asking = null;     // the request on its way, to be called off
  var empties = 0;
  var waits = 0;
  var timer = null;
  var isNew = loader.onlyNew();
  var tmdb = true;
  var opened = '';       // the title last opened from the grid
  var speed = 0;
  var lastY = window.scrollY;
  var lastT = 0;
  var arrives = 'IntersectionObserver' in window;

  function text(value) { return typeof value === 'string' ? value : ''; }

  function now() { return window.performance ? window.performance.now() : Date.now(); }

  /* ── Cards ── */

  function card(item) {
    var node = watch.browse.card(Object.assign({}, item, { ai_pick: false }));
    if (!node) return null;
    var why = item.reason || {};
    var kind = Object.prototype.hasOwnProperty.call(KINDS, why.kind) ? why.kind : '';
    var words = text(why.text);
    if (!kind || !words) return node;
    var link = node.querySelector('.watch-card__link');
    var line = el('span', 'wn-why wn-why--' + kind);
    line.appendChild(icon(KINDS[kind][0]));
    line.appendChild(el('span', 'wn-why__t wn-why__long', words));
    line.appendChild(el('span', 'wn-why__t wn-why__short', text(why.short) || words)).setAttribute('aria-hidden', 'true');
    link.appendChild(line);
    // A pick's preview asks for the AI's own words.
    if (kind !== 'ai' && text(why.why)) {
      link.setAttribute('data-why', why.why);
      link.setAttribute('data-why-label', KINDS[kind][1]);
      link.setAttribute('data-why-icon', KINDS[kind][0]);
    }
    return node;
  }

  function cards() { return toArray(grid.children).filter(function (n) { return n.classList.contains('watch-card'); }); }

  function skels() { return toArray(grid.children).filter(function (n) { return n.classList.contains('watch-card--skel'); }); }

  function columns() {
    var t = window.getComputedStyle(grid).gridTemplateColumns;
    return t && t !== 'none' ? t.trim().split(/\s+/).length : 1;
  }

  /* ── Placeholders: the rest of the last row and two more ── */

  function skel() {
    var node = el('div', 'watch-card--skel');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(el('span', 'skel skel--poster'));
    node.appendChild(el('span', 'skel skel--line'));
    node.appendChild(el('span', 'wn-skel-why')).appendChild(el('span', 'skel skel--line'));
    return node;
  }

  function removeSkels() { skels().forEach(function (n) { n.remove(); }); }

  function showSkels() {
    removeSkels();
    var n = columns();
    var count = (n - cards().length % n) % n + 2 * n;
    for (var i = 0; i < count; i++) grid.appendChild(skel());
  }

  /* ── Posters near and far, and the arrival ── */

  function observer(fn, opts) {
    return arrives ? new window.IntersectionObserver(fn, opts) : { observe: function () {}, unobserve: function () {} };
  }

  var awake = observer(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) loader.reload(e.target);
      else loader.unload(e.target);
    });
  }, { rootMargin: AWAKE_VIEWS * 100 + '% 0px' });

  function moving() { return now() - lastT < STILL_MS ? speed : 0; }

  /* A card comes in the first time it is seen, a step later for each column and row after the
     first it came with; on a fast flick it is simply there. */
  var seen = observer(function (entries) {
    var shown = entries.filter(function (e) { return e.isIntersecting; }).map(function (e) { return e.target; });
    if (!shown.length) return;
    var all = cards();
    var n = columns();
    var at = shown.map(function (c) { return all.indexOf(c); });
    var firstRow = Math.floor(Math.min.apply(Math, at) / n);
    var fast = moving() > FAST_PX_MS;
    var reduce = core.reduceMotion();
    shown.forEach(function (c, k) {
      seen.unobserve(c);
      c.classList.remove('is-pending');
      if (fast) return;
      var i = at[k];
      var delay = reduce ? 0 : Math.min(MOST_MS, (i % n) * COL_MS + (Math.floor(i / n) - firstRow) * ROW_MS);
      c.style.setProperty('--wn-delay', delay + 'ms');
      c.classList.add('is-in');
      c.addEventListener('animationend', function done(e) {
        if (e.target !== c) return;
        c.removeEventListener('animationend', done);
        c.classList.remove('is-in');
        c.style.removeProperty('--wn-delay');
      });
    });
  }, { rootMargin: '0px 0px -4% 0px' });

  /* The height an undrawn card keeps: every card has its two caption lines. */
  function measure() {
    var drawn = cards().filter(function (c) { return !c.hasAttribute('data-far'); })[0];
    var h = drawn && drawn.getBoundingClientRect().height;
    if (h) sec.style.setProperty('--wn-h', h + 'px');
  }

  function place(nodes, quiet) {
    nodes.forEach(function (node) {
      if (!quiet && arrives) node.classList.add('is-pending');
      grid.appendChild(node);
      awake.observe(node);
      if (!quiet && arrives) seen.observe(node);
    });
    measure();
  }

  /* ── Rows far above ── */

  function setShed() {
    var h = shed.reduce(function (sum, s) { return sum + s.height; }, 0);
    grid.style.setProperty('--wn-shed', h + 'px');
  }

  function forget(node) {
    awake.unobserve(node);
    seen.unobserve(node);
    node.remove();
  }

  /* Whole rows wholly SHED_VIEWS screens above go, and their height stands as room; the row
     that holds the focus, and what is under it, stay. */
  function shedFar() {
    if (items.length <= SHED_AFTER) return;
    var all = cards();
    var n = columns();
    var line = -SHED_VIEWS * window.innerHeight;
    var focus = document.activeElement;
    var rows = 0;
    while ((rows + 2) * n <= all.length && all[rows * n].getBoundingClientRect().bottom < line) {
      if (all.slice(rows * n, rows * n + n).some(function (c) { return c.contains(focus); })) break;
      rows++;
    }
    if (rows < 2) return;
    var count = rows * n;
    // Measured before anything moves, so the room is what the rows took to the pixel.
    var height = all[count].getBoundingClientRect().top - all[0].getBoundingClientRect().top;
    all.slice(0, count).forEach(forget);
    shed.push({ count: count, height: height });
    shedCount += count;
    shedWidth = grid.getBoundingClientRect().width;
    setShed();
  }

  /* The rows taken out come back, the latest first, before the person reaches them. */
  function buildBack(all) {
    var first = cards()[0] || null;
    while (shed.length && (all || !first || first.getBoundingClientRect().top > -BACK_VIEWS * window.innerHeight)) {
      var chunk = shed.pop();
      shedCount -= chunk.count;
      var nodes = items.slice(shedCount, shedCount + chunk.count).map(card).filter(Boolean);
      nodes.forEach(function (node) {
        grid.insertBefore(node, first);
        awake.observe(node);
      });
      setShed();
      first = nodes[0] || first;
    }
  }

  /* ── Loading ── */

  function shown() { return sec.getClientRects().length > 0; }

  function near() { return shown() && end.getBoundingClientRect().top < window.innerHeight * (1 + AHEAD_VIEWS); }

  function check() {
    if (armed && state === 'idle' && near()) more();
  }

  function retryButton() { return tail.querySelector('.grid-fail__retry'); }

  function more() {
    var my = ++ticket;
    if (asking) asking.abort();
    asking = window.AbortController ? new window.AbortController() : null;
    state = 'loading';
    if (!switching && !retryButton()) showSkels();
    var query = '?type=' + filter + (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
    window.MM.api.get(URL + query, asking ? { signal: asking.signal } : undefined).then(function (data) {
      if (my === ticket) took(data || {});
    }, function (err) {
      if (my === ticket) lost(err);
    });
  }

  /* The cards are of another filter: the grid starts again. */
  function reset() {
    cards().forEach(forget);
    removeSkels();
    items = [];
    shed = [];
    shedCount = 0;
    setShed();
    isNew = loader.onlyNew();
    switching = false;
    sec.classList.remove('is-switching');
  }

  function took(data) {
    asking = null;
    if (switching) reset();
    var first = !items.length;
    var button = retryButton();
    var refocus = !!button && document.activeElement === button;
    tail.textContent = '';
    grid.classList.remove('is-static');
    removeSkels();
    tmdb = data.tmdb_configured !== false;
    if (typeof data.cursor === 'string' && data.cursor) cursor = data.cursor;
    var fresh = [];
    (Array.isArray(data.items) ? data.items : []).forEach(function (item) {
      var node = item && card(item);
      if (node && isNew(node)) {
        items.push(item);
        fresh.push(node);
      }
    });
    if (first) note.toggleAttribute('hidden', data.personal !== false || !fresh.length);
    place(fresh, false);
    empties = fresh.length ? 0 : empties + 1;
    waits = 0;
    if (empties >= (tmdb ? EMPTY_LIMIT : 1)) {
      // Without TMDB only the stored picks are given, and they run out; nothing given at all, the section goes.
      state = 'done';
      if (!items.length) sec.setAttribute('hidden', '');
      return;
    }
    state = 'idle';
    if (fresh.length) said.textContent = fresh.length + ' more titles loaded.';
    if (refocus) (fresh.length ? fresh[0].querySelector('.watch-card__link') : grid).focus({ preventScroll: true });
    later();
  }

  function lost(err) {
    asking = null;
    // The session ran out: the browser is on its way to the login page.
    if (err && err.status === 401) return;
    if (err && err.status === 429 && waits < BACKOFF_MS.length && !retryButton()) {
      state = 'waiting';
      timer = setTimeout(function () {
        timer = null;
        state = 'idle';
        if (armed && near()) more();
        else removeSkels();
      }, BACKOFF_MS[waits++]);
      return;
    }
    waits = 0;
    state = 'failed';
    if (switching) reset();
    var button = retryButton();
    if (button) {
      button.removeAttribute('aria-disabled');
      button.setAttribute('aria-busy', 'false');
      core.toast('Still couldn’t load more. Try again in a moment.', { variant: 'error' });
      return;
    }
    showFailed();
  }

  /* Only the rest of the last row stays held, so the line stands right under the cards. */
  function showFailed() {
    var n = columns();
    var rest = (n - cards().length % n) % n;
    skels().forEach(function (s, i) { s.classList.toggle('is-spare', i >= rest); });
    grid.classList.add('is-static');
    var line = el('div', 'grid-fail');
    line.setAttribute('role', 'alert');
    line.appendChild(el('span', null, 'Couldn’t load more titles.'));
    var button = line.appendChild(el('button', 'grid-fail__retry'));
    button.type = 'button';
    button.setAttribute('data-wn-retry', '');
    button.appendChild(icon('i-rotate-right'));
    button.appendChild(document.createTextNode('Try again'));
    tail.textContent = '';
    tail.appendChild(line);
  }

  function retry() {
    if (state !== 'failed') return;
    var button = retryButton();
    if (button) {
      button.setAttribute('aria-disabled', 'true');
      button.setAttribute('aria-busy', 'true');
    }
    more();
  }

  function setFilter(to) {
    if (to === filter || FILTERS.indexOf(to) < 0) return;
    filter = to;
    showFilter();
    clearTimeout(timer);
    timer = null;
    var top = sec.getBoundingClientRect().top;
    if (top < 0) window.scrollTo({ top: Math.max(0, top + window.scrollY - core.clearArea().top - 8), behavior: 'instant' });
    tail.textContent = '';
    grid.classList.remove('is-static');
    cursor = null;
    empties = 0;
    waits = 0;
    armed = true;
    if (cards().length) {
      // The cards stay, dimmed, until the new ones come.
      switching = true;
      sec.classList.add('is-switching');
    }
    more();
  }

  function showFilter() {
    toArray(sec.querySelectorAll('[data-wn-filter]')).forEach(function (b) {
      var on = b.getAttribute('data-wn-filter') === filter;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  /* ── Back to top ── */

  function showTop() {
    var deep = shown() && window.scrollY > sec.getBoundingClientRect().top + window.scrollY + TOP_AFTER_VIEWS * window.innerHeight;
    if (deep === topButton.classList.contains('is-on')) return;
    topButton.classList.toggle('is-on', deep);
    topButton.tabIndex = deep ? 0 : -1;
    if (deep) topButton.removeAttribute('aria-hidden');
    else topButton.setAttribute('aria-hidden', 'true');
  }

  function toTop() {
    window.scrollTo({ top: 0, behavior: core.reduceMotion() ? 'instant' : 'smooth' });
    var brand = document.querySelector('a.brand');
    if (brand) brand.focus({ preventScroll: true });
  }

  /* ── Watching the page ── */

  var queued = false;
  var resized = false;
  function later() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(function () {
      queued = false;
      frame();
    });
  }

  function frame() {
    if (resized) {
      resized = false;
      if (shed.length && grid.getBoundingClientRect().width !== shedWidth) buildBack(true);
      measure();
    }
    if (shown()) {
      shedFar();
      buildBack(false);
    }
    showTop();
    check();
  }

  window.addEventListener('scroll', function () {
    var t = now();
    var dt = t - lastT;
    speed = dt > 0 && dt < 500 ? Math.abs(window.scrollY - lastY) / dt : 0;
    lastY = window.scrollY;
    lastT = t;
    armed = true;
    later();
  }, { passive: true });
  window.addEventListener('resize', function () { resized = true; later(); });
  window.addEventListener('online', retry);

  /* One listener for the whole section: the filter, Try again, Back to top and the card opened. */
  sec.addEventListener('click', function (e) {
    var t = e.target;
    var pill = t.closest('[data-wn-filter]');
    if (pill) { setFilter(pill.getAttribute('data-wn-filter')); return; }
    if (t.closest('[data-wn-retry]')) { retry(); return; }
    if (t.closest('#watch-next-top')) { toTop(); return; }
    var link = t.closest('.watch-card__link');
    if (link && grid.contains(link)) opened = link.getAttribute('data-title-ref') || '';
  });

  /* A page too short to scroll has its rows' end in reach without a scroll: once the Discover
     rows have come, it is asked for. */
  function settled() {
    var slot = byId('watch-discover-slot');
    return !slot || slot.getAttribute('aria-busy') !== 'true';
  }

  function armIfShort() {
    if (armed || !settled() || document.documentElement.scrollHeight > window.innerHeight + 2) return;
    armed = true;
    check();
  }

  var slot = byId('watch-discover-slot');
  if (slot && window.MutationObserver) {
    new window.MutationObserver(armIfShort).observe(slot, { attributes: true, attributeFilter: ['aria-busy'] });
  }
  window.addEventListener('load', armIfShort);

  /* ── Leaving and coming back ── */

  function entry() {
    var s = window.history.state;
    return s && s.mm && typeof s.mm.id === 'string' ? s.mm.id : null;
  }

  function keep() {
    var active = document.activeElement;
    var link = active && grid.contains(active) && active.closest('.watch-card__link');
    if (link) opened = link.getAttribute('data-title-ref') || opened;
    try {
      if (!items.length || !entry()) { window.sessionStorage.removeItem(STORE); return; }
      window.sessionStorage.setItem(STORE, JSON.stringify({
        id: entry(), who: who, filter: filter, cursor: cursor, items: items,
        opened: opened, tmdb: tmdb, fresh: !note.hasAttribute('hidden')
      }));
    } catch (e) {
      try { window.sessionStorage.removeItem(STORE); } catch (e2) { /* nothing kept */ }
    }
  }

  /* The same entry, loaded again: what it held, laid out before its scroll is put back. */
  function recall() {
    var kept = null;
    try { kept = JSON.parse(window.sessionStorage.getItem(STORE)); } catch (e) { return; }
    if (!kept || kept.id !== entry() || !entry() || kept.who !== who || FILTERS.indexOf(kept.filter) < 0 ||
      !Array.isArray(kept.items) || !kept.items.length) return;
    filter = kept.filter;
    showFilter();
    cursor = typeof kept.cursor === 'string' ? kept.cursor : null;
    tmdb = kept.tmdb !== false;
    note.toggleAttribute('hidden', kept.fresh !== true);
    opened = text(kept.opened);
    var nodes = [];
    kept.items.forEach(function (item) {
      var node = item && card(item);
      if (node && isNew(node)) {
        items.push(item);
        nodes.push(node);
      }
    });
    place(nodes, true);
    armed = true;
    document.addEventListener('DOMContentLoaded', focusOpened);
  }

  /* The card that was opened takes the focus back, unless its title opens again over the page. */
  function focusOpened() {
    if (!opened || /^#title=/.test(window.location.hash)) return;
    var active = document.activeElement;
    if (active && active !== document.body) return;
    var link = toArray(grid.querySelectorAll('.watch-card__link')).filter(function (a) {
      return a.getAttribute('data-title-ref') === opened;
    })[0];
    if (!link) return;
    core.drawTools(link);
    link.focus({ preventScroll: true });
  }

  window.addEventListener('pagehide', keep);
  recall();
  later();
})();
