// Runs the Watch page's browse scripts (core, address, keep, search, rows-load, grid, expand, browse, watch-rows) against the fake DOM and
// prints the outcome of one scenario as JSON. Usage: node watch_browse_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const HOUR = 3600 * 1000;

function card(ref, extra = {}) {
  return { ref, kind: 'movie', title: 'Title ' + ref, year: 2024, poster_url: '/api/watch/img/tmdb/w342/a.jpg',
    rating: 7.1, in_library: false, library_ref: null, href: null, download_state: null, download_pct: null, ...extra };
}

const DISCOVER = {
  ok: true, tmdb_configured: true, stale: false,
  rows: [
    { id: 'trending', title: 'Trending this week', items: [
      card('tmdb-movie-1', { in_library: true, library_ref: 'movie-11', ref: 'movie-11', href: '/watch/movie/11' }),
      card('tmdb-movie-2', { download_state: 'queued' }),
      card('tmdb-tv-3', { kind: 'show', download_state: 'downloading', download_pct: 42 }),
      card('tmdb-movie-4', { title: '<img src=x onerror=alert(1)>' }),
      card('javascript:alert(1)'),
    ] },
    { id: 'popular_movies', title: 'Popular movies', items: [card('tmdb-movie-5')] },
    { id: 'popular_tv', title: 'Popular TV', items: [card('tmdb-tv-6', { kind: 'show' })] },
  ],
};

const BATCHES = [
  { id: '2026-11-21', label: 'Latest picks', date_label: '21 Nov' },
  { id: '2026-11-14', label: 'Last week', date_label: '14 Nov' },
];

function recommended(opts = {}) {
  const batches = opts.batches || BATCHES;
  const out = { ok: true, enabled: opts.enabled !== false, batch: opts.batch || (batches.length ? batches[0].id : null),
    batches: opts.enabled === false ? [] : batches,
    items: opts.items || [card('rec-7', { rec_id: 7, reason: 'Because' }), card('rec-8', { rec_id: 8 })] };
  if (opts.enabled === false) out.items = [];
  out.my_refresh = opts.mine || { available: false, reason: 'not_enough_plays', next_available_at: null, running: false };
  if (opts.admin) out.refresh = opts.refresh || { available: true, next_available_at: null, running: false };
  return out;
}

function apiError(status, data) {
  const err = new Error('http ' + status);
  err.status = status;
  err.data = data;
  return err;
}

/* The Watch page around the Discover slot, as the server sends it. */
function page(doc, opts) {
  const libraryRow = opts.library ? [['section', { class: 'watch-row' },
    ['div', { class: 'watch-row__viewport', 'data-row-kind': 'tv', 'data-row-next': opts.library.next },
      ['div', { class: 'watch-row__scroll', 'aria-label': 'TV' }, ...opts.library.keys.map((k) => ['article', { class: 'watch-card', 'data-mark': 'show/' + k },
        ['a', { class: 'watch-card__link', href: '/watch/show/' + k, 'data-title-ref': 'show-' + k },
          ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, 'Show ' + k]], ['span', { class: 'watch-card__sub' }, 'TV']]])]]]] : [];
  const skelRow = ['div', { class: 'watch-row watch-row--skel' },
    ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ['div', { class: 'watch-card--skel' }]]]];
  const leaving = (id, title) => ['article', { class: 'watch-card', 'data-keep-id': id },
    ['a', { class: 'watch-card__link', href: '/watch/movie/' + id, 'data-title-ref': 'movie-' + id }, ['span', { class: 'watch-card__poster' }, title]],
    ['button', { class: 'watch-keep', 'data-watch-keep': id, 'aria-label': 'Keep ' + title }, 'Keep']];
  doc.body.appendChild(build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['input', { id: 'watch-search' }],
    ['div', { id: 'watch-browse' }, ['div', { class: 'watch-rows' },
      ['section', { class: 'watch-row', 'data-watch-leaving-row': '' },
        ['div', { class: 'watch-row__viewport' },
          ['div', { class: 'watch-row__scroll' }, leaving('1100', 'Leaving Film'), leaving('1101', 'Northbound')]]],
      ['div', { class: 'watch-discover-slot', id: 'watch-discover-slot', 'aria-busy': 'true' },
        ['div', { class: 'watch-discover-skel', 'aria-hidden': 'true' },
          ['p', { class: 'watch-group-label' }, 'Discover'], skelRow, skelRow, skelRow]],
      ...libraryRow]],
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', hidden: '' },
      ['button', { class: 'watch-keep-menu__item', 'data-days': '30' }, 'Keep 30 days']]]));
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' },
    JSON.stringify({ is_admin: !!opts.admin, tmdb_configured: opts.tmdb !== false, grids: opts.grids })]));
}

/* *routes* maps a URL to a function returning a promise; calls are recorded. */
function load(opts = {}) {
  const doc = makeDocument();
  page(doc, opts);
  const toasts = [];
  const timers = [];
  const calls = [];
  const replaced = [];
  const pushed = [];
  const moveTo = (url) => {
    const [, pathname, search, hash] = /^([^?#]*)(\?[^#]*)?(#.*)?$/.exec(url);
    Object.assign(ctx.location, { pathname, search: search || '', hash: hash || '' });
  };
  const scrolls = [];
  const routes = Object.assign({
    'GET /api/watch/discover': () => Promise.resolve(DISCOVER),
    'GET /api/watch/recommended': () => Promise.resolve(recommended(opts)),
  }, opts.routes || {});
  const call = (method) => (url) => {
    calls.push(method + ' ' + url);
    const fn = routes[method + ' ' + url];
    return fn ? fn() : Promise.reject(apiError(404, {}));
  };
  const ctx = {
    document: Object.assign(doc, { dispatchEvent: () => {} }), CustomEvent: class {}, console, JSON, Array, Object, String, Math, Promise, WeakMap, WeakSet, Date, Error,
    parseInt, parseFloat, encodeURIComponent,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingLeft: '0', paddingRight: '0' }),
    // Reduced motion unless a scenario asks for motion: swaps are immediate.
    matchMedia: (q) => ({ matches: !(opts.motion && /reduce/.test(q)) }),
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} disconnect() {} },
    innerHeight: opts.innerHeight || 900,
    innerWidth: opts.innerWidth || 1280,
    location: { pathname: '/watch', href: '/watch', search: opts.search || '', hash: opts.hash || '' },
    history: { state: null, replaceState(state, _t, url) {
      this.state = state;
      if (url !== ctx.location.pathname + ctx.location.search + ctx.location.hash) replaced.push(url);
      moveTo(url);
    }, pushState(state, _t, url) { this.state = state; pushed.push(url); moveTo(url); } },
    URLSearchParams, Map, Set,
    scrollY: opts.scrollY || 0,
    scrollBy(o) { scrolls.push(o); },
    UIFeedback: { toast: (message, o) => toasts.push({ message, opts: o }) },
    MM: { api: { get: call('GET'), post: call('POST'), messageOf: (e, f) => (e && e.data && e.data.message) || f } },
    addEventListener() {},
    removeEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/search.js', 'watch/rows-load.js', 'watch/grid.js', 'watch/expand.js', 'watch/browse.js', 'watch-rows.js']
    .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));
  return { ctx, doc, toasts, timers, calls, routes, replaced, pushed, scrolls };
}

/* Run the timers due now (the poll), in order, once each. */
async function tick(p) {
  const due = p.timers.splice(0);
  due.forEach((t) => t.fn());
  await settle();
}

function rowIds(doc) {
  return doc.querySelectorAll('[data-discover-row]').map((s) => s.getAttribute('data-discover-row'));
}

function rec(doc) {
  return doc.querySelector('[data-discover-row="recommended"]');
}

function text(node) { return node ? node.textContent.replace(/\s+/g, ' ').trim() : null; }

function badgeOf(doc, ref) {
  const link = doc.querySelectorAll('[data-title-ref]').find((a) => a.getAttribute('data-title-ref') === ref);
  return text(link && link.querySelector('.watch-badge--state'));
}

const CARD_PX = 160;
const VIEW_PX = 800;

/* Give every row a layout: things 160px apart in an 800px view. */
function layout() {
  const define = (name, get) => Object.defineProperty(Node.prototype, name, { configurable: true, get });
  define('clientWidth', () => VIEW_PX);
  define('scrollWidth', function () { return Math.max(VIEW_PX, this.children.length * CARD_PX); });
  define('offsetLeft', function () { return this.parentNode.children.indexOf(this) * CARD_PX; });
  Node.prototype.scrollLeft = 0;
  Node.prototype.scrollTo = function (o) { this.scrollLeft = o.left; };
}

function scrollerOf(p, id) { return p.doc.querySelector('[data-discover-row="' + id + '"] .watch-row__scroll'); }

async function scroll(scroller, left) {
  scroller.scrollLeft = left;
  scroller.listeners.scroll.forEach((fn) => fn());
  await settle();
}

function refsOf(scroller) { return scroller.querySelectorAll('.watch-card__link').map((a) => a.getAttribute('data-title-ref')); }

function range(from, to) { return Array.from({ length: to - from }, (_, i) => from + i); }

/* The page with one TMDB row, Popular movies, of *first* and the later *pages* by number: each
   {items, next_page}, or a function returning a promise. */
function loadPages(first, pages, extra = {}) {
  layout();
  const routes = { 'GET /api/watch/discover': () => Promise.resolve({ rows: [
    { id: 'popular_movies', title: 'Popular movies', items: first, next_page: 2 }] }) };
  Object.keys(pages).forEach((n) => {
    const page = pages[n];
    routes['GET /api/watch/discover?row=popular_movies&page=' + n] = () => (typeof page === 'function' ? page() : Promise.resolve(page));
  });
  return load({ ...extra, routes: { ...routes, ...extra.routes } });
}

function pageCalls(p) { return p.calls.filter((c) => /page=/.test(c)).map((c) => Number(/page=([0-9]+)/.exec(c)[1])); }

const movies = (...ids) => ids.map((id) => card('tmdb-movie-' + id));

const MINE = {
  ready: { available: true, reason: null, next_available_at: null, running: false },
  wait: { available: false, reason: 'wait', next_available_at: new Date(Date.now() + 6 * 24 * HOUR - 60000).toISOString(), running: false },
  few: { available: false, reason: 'not_enough_plays', next_available_at: null, running: false },
  noKey: { available: false, reason: 'not_configured', next_available_at: null, running: false },
  running: { available: false, reason: 'running', next_available_at: null, running: true },
};

const pick = (ref, over) => card(ref, { ai_pick: true, own: /^pick/.test(ref), pick_ref: ref, ...over });
const OWN_ITEMS = [pick('pick-1', { ref: 'movie-11', in_library: true, library_ref: 'movie-11' }), pick('pick-2'), pick('rec-7')];

function lineOf(p) {
  const line = rec(p.doc).querySelector('.rec-line');
  const button = line.querySelector('[data-own-refresh]');
  return { text: text(line.querySelector('.rec-line__text')), button: text(button), off: button && button.getAttribute('aria-disabled') === 'true' };
}

/* The picks in three batches of five cards (*far*: forty), the older two loaded as the row goes on. */
async function threeBatches(opts = {}) {
  layout();
  Object.defineProperty(Node.prototype, 'offsetWidth', { configurable: true, get: () => CARD_PX - 10 });
  const batches = [...BATCHES, { id: '2026-11-07', label: 'Two weeks ago', date_label: '7 Nov' }];
  const n = opts.far ? 40 : 5;
  const items = (from) => range(from, from + n).map((i) => card('rec-' + (100 + i)));
  const older = (batch) => 'GET /api/watch/recommended?batch=' + batch + '&start=2026-11-21';
  const lost = () => Promise.reject(apiError(opts.lost, {}));
  const answer = (batch, from, next) => Promise.resolve({ ...recommended({ batches, batch, items: items(from) }), next_batch: next });
  const second = () => (opts.slow ? opts.slow() : Promise.resolve()).then(() => answer('2026-11-14', n, '2026-11-07'));
  const p = load({ batches, routes: {
    'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
    'GET /api/watch/recommended': () => answer('2026-11-21', 0, opts.emptied ? '2026-11-07' : '2026-11-14'),
    [older('2026-11-14')]: opts.lost ? lost : second,
    [older('2026-11-07')]: () => answer('2026-11-07', 2 * n, null),
  } });
  await settle();
  const button = () => rec(p.doc).querySelector('[data-picks-menu]');
  const open = () => { click(p.doc, button()); return p.doc.getElementById('watch-picks-menu').querySelectorAll('.watch-keep-menu__item'); };
  return { p,
    pill: () => { const on = button().querySelectorAll('.is-on'); return on.map((o) => [text(o.querySelector('.row-tool__long')), text(o.querySelector('.picks-name__short'))]); },
    menu: () => open().filter((i) => i.getAttribute('aria-checked') === 'true').map(text),
    choose: async (i) => { click(p.doc, open()[i]); await settle(); await settle(); } };
}

const scenarios = {
  // Pages shift (a title again), one is all repeats, one title is scanned meanwhile: none twice.
  async pagesToTheEnd() {
    const p = loadPages([...movies(1, 2), card('tmdb-movie-2')], {
      2: { items: movies(2, 3), next_page: 3 },
      3: { items: movies(1, 3), next_page: 4 },
      4: { items: [card('movie-70', { title: 'Title tmdb-movie-3', in_library: true }), ...movies(4),
        card('tmdb-movie-5', { title: 'Title tmdb-movie-4' })], next_page: null },
    });
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    await scroll(scroller, 0);
    await scroll(scroller, 0);
    return { refs: refsOf(scroller), calls: pageCalls(p), things: scroller.children.length,
      next: scroller.parentNode.classList.contains('has-next'), more: scroller.parentNode.hasAttribute('data-row-more') };
  },

  // While a page is on its way: one request, placeholders, and Next stays at the very end.
  async pageOnItsWay() {
    let answer;
    const p = loadPages(movies(...range(0, 10)), { 2: () => new Promise((resolve) => { answer = resolve; }) });
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    const far = { calls: pageCalls(p).length };
    for (const left of [100, 700, 800, 1760]) await scroll(scroller, left);
    const waiting = { calls: pageCalls(p), things: scroller.children.length, busy: scroller.getAttribute('aria-busy'),
      hidden: scroller.querySelectorAll('.watch-card--skel').every((n) => n.getAttribute('aria-hidden') === 'true'),
      next: scroller.parentNode.classList.contains('has-next') };
    const left = scroller.scrollLeft;
    answer({ items: movies(...range(10, 30)), next_page: 3 });
    await settle();
    return { far, waiting, kept: scroller.scrollLeft === left, cards: refsOf(scroller).length, things: scroller.children.length,
      busy: scroller.getAttribute('aria-busy') };
  },

  // A row with plenty left to scroll asks for nothing.
  async pageNotNear() {
    const p = loadPages(movies(...range(0, 40)), { 2: { items: [], next_page: null } });
    await settle();
    await scroll(scrollerOf(p, 'popular_movies'), 0);
    return pageCalls(p);
  },

  // Repeats for ever: the row gives up and is finished.
  async pagesOfRepeats() {
    const pages = {};
    range(2, 40).forEach((n) => { pages[n] = { items: movies(1), next_page: n + 1 }; });
    const p = loadPages(movies(1), pages);
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    await scroll(scroller, 0);
    for (let i = 0; i < 10; i++) await settle();
    return { calls: pageCalls(p).length, things: scroller.children.length, more: scroller.parentNode.hasAttribute('data-row-more') };
  },

  // A page that fails: Try again at the end of the row, by its button and by Next.
  async pageFailed() {
    let fail = true;
    const p = loadPages(movies(1), {
      2: () => (fail ? Promise.reject(apiError(502, {})) : Promise.resolve({ items: movies(1, 2, 3), next_page: 3 })),
      3: () => Promise.reject(apiError(502, {})),
    });
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    await scroll(scroller, 0);
    await scroll(scroller, 10);
    const tile = () => scroller.querySelector('.watch-row__more');
    const failed = { calls: pageCalls(p), things: scroller.children.length, text: text(tile()), toasts: p.toasts.length,
      next: scroller.parentNode.classList.contains('has-next') };
    const button = tile().querySelector('button');
    button.focus();
    button.listeners.click.forEach((fn) => { fn(); fn(); }); // pressed twice
    await settle();
    const still = { calls: pageCalls(p), toasts: p.toasts.map((t) => t.message), busy: button.getAttribute('aria-busy'),
      focus: p.doc.activeElement === button };
    fail = false;
    scroller.scrollLeft = scroller.scrollWidth - VIEW_PX;
    scroller.parentNode.querySelector('.watch-row__arrow--next').listeners.click.forEach((fn) => fn());
    await settle();
    return { failed, still, refs: refsOf(scroller), calls: pageCalls(p),
      focus: p.doc.activeElement.getAttribute('data-title-ref'), tiles: scroller.querySelectorAll('.watch-row__more').length };
  },

  // Asked too often: it waits longer each time, says nothing, and after a while offers Try again.
  async pageTooMany() {
    const p = loadPages(movies(1), { 2: () => Promise.reject(apiError(429, {})) });
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    await scroll(scroller, 0);
    const quiet = { tile: !!scroller.querySelector('.watch-row__more'), skel: scroller.querySelectorAll('.watch-card--skel').length };
    await scroll(scroller, 5);
    const calls = pageCalls(p).length;
    const waits = [];
    while (p.timers.length) {
      waits.push(p.timers[0].ms);
      await tick(p);
    }
    return { quiet, calls, waits, total: pageCalls(p).length, tile: !!scroller.querySelector('.watch-row__more'), toasts: p.toasts.length };
  },

  // Posters far off lose their picture and get it back near; the cards stay.
  async farPosters() {
    const p = loadPages(movies(...range(0, 100)), { 2: { items: [], next_page: null } });
    await settle();
    const scroller = scrollerOf(p, 'popular_movies');
    const src = (i) => scroller.querySelectorAll('.watch-card')[i].querySelector('img').getAttribute('src');
    await scroll(scroller, 0);
    const atStart = [src(0), src(99)];
    await scroll(scroller, 99 * CARD_PX - VIEW_PX);
    const atEnd = [src(0), src(98)];
    await scroll(scroller, 0);
    return { atStart, atEnd, back: src(0), cards: scroller.querySelectorAll('.watch-card').length };
  },

  // The picks go on to older batches from the one the row starts at, past one that is gone.
  async olderPicks() {
    layout();
    const older = (batch, start) => 'GET /api/watch/recommended?batch=' + batch + '&start=' + start;
    const batches = [...BATCHES, { id: '2026-11-07', label: 'Two weeks ago', date_label: '7 Nov' }, { id: '2026-10-31', label: 'Three weeks ago', date_label: '31 Oct' }];
    const p = load({ batches, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended({ batches }), next_batch: '2026-11-14' }),
      [older('2026-11-14', '2026-11-21')]: () => Promise.resolve({ ...recommended({ batch: '2026-11-14', items: [card('rec-9'), card('rec-10', { title: 'Title rec-7' })] }), next_batch: '2026-11-07' }),
      [older('2026-11-07', '2026-11-21')]: () => Promise.reject(apiError(404, {})),
      [older('2026-10-31', '2026-11-21')]: () => Promise.resolve({ ...recommended({ batch: '2026-10-31', items: [card('rec-12'), card('rec-13', { title: 'Title rec-9' })] }), next_batch: null }),
    } });
    await settle();
    await scroll(scrollerOf(p, 'recommended'), 0);
    const first = { refs: refsOf(scrollerOf(p, 'recommended')), more: scrollerOf(p, 'recommended').parentNode.hasAttribute('data-row-more') };
    return first;
  },

  // The batch the row starts at is gone; the one it goes on to is not.
  async goneStart() {
    layout();
    const p = load({ batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended(), next_batch: '2026-11-14' }),
      'GET /api/watch/recommended?batch=2026-11-14': () => Promise.resolve({ ...recommended({ batch: '2026-11-14', items: [card('rec-9')] }), next_batch: '2026-11-07' }),
      'GET /api/watch/recommended?batch=2026-11-07&start=2026-11-14': () => Promise.resolve({ ...recommended({ batch: '2026-11-07', items: [card('rec-10')] }), next_batch: null }),
    } });
    await settle();
    await scroll(scrollerOf(p, 'recommended'), 0);
    return refsOf(scrollerOf(p, 'recommended'));
  },

  async picksPill() {
    const p = load({ batches: BATCHES });
    await settle();
    return rec(p.doc).querySelector('[data-picks-menu]').children.map((n) => n.className);
  },

  // Every batch the row goes on to is gone, and the last is one the menu does not know.
  async goneBatches() {
    layout();
    const p = load({ batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended(), next_batch: '2020-01-01' }),
    } });
    await settle();
    await scroll(scrollerOf(p, 'recommended'), 0);
    return { refs: refsOf(scrollerOf(p, 'recommended')), asked: p.calls.length, more: scrollerOf(p, 'recommended').parentNode.hasAttribute('data-row-more') };
  },

  // The row has no batch to start at, and the first older batch is gone: the same address is not asked twice.
  async goneWithoutStart() {
    layout();
    const p = load({ batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended(), batch: null, next_batch: '2026-11-14' }),
    } });
    await settle();
    await scroll(scrollerOf(p, 'recommended'), 0);
    return p.calls.filter((c) => c.indexOf('batch=2026-11-14') > 0);
  },

  // Picks of one name, kind and year: another TMDB id is another title, the same or an unknown one is not.
  async picksOfOneName() {
    layout();
    const twin = (ref, id, extra) => pick(ref, { title: 'Twin', tmdb_id: id, ...extra });
    const p = load({ batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended({ items: [twin('pick-1', 11), twin('rec-1', 12), twin('rec-2', 11, { title: 'Twin renamed' })] }), next_batch: '2026-11-14' }),
      'GET /api/watch/recommended?batch=2026-11-14&start=2026-11-21': () => Promise.resolve({ ...recommended({ batch: '2026-11-14',
        items: [twin('rec-3', 13), twin('rec-4', 12), twin('rec-5', null), twin('rec-6', 11, { kind: 'show' })] }), next_batch: null }),
    } });
    await settle();
    await scroll(scrollerOf(p, 'recommended'), 0);
    return refsOf(scrollerOf(p, 'recommended'));
  },

  // New picks arrive while an older batch is on its way: the row starts again without it.
  async refreshedWhileLoading() {
    layout();
    let answer;
    let done = false;
    const p = load({ batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve(done ? recommended({ mine: MINE.wait, items: [pick('pick-30')] }) : { ...recommended({ mine: MINE.ready }), next_batch: '2026-11-14' }),
      'GET /api/watch/recommended?batch=2026-11-14&start=2026-11-21': () => new Promise((resolve) => { answer = resolve; }),
      'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
      'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'done' }),
    } });
    await settle();
    const old = scrollerOf(p, 'recommended');
    await scroll(old, 0);
    click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
    await settle();
    done = true;
    await tick(p);
    answer({ ...recommended({ batch: '2026-11-14', items: [card('rec-9')] }), next_batch: null });
    await settle();
    return { old: refsOf(old), now: refsOf(scrollerOf(p, 'recommended')) };
  },

  // Try again, pressed with the keyboard, works: the focus goes to the row it was pressed in. The
  // row it replaces fades out inert, which takes the focus from the button in it.
  async focusAfterRetry() {
    const run = async (id, url, ok) => {
      let fail = true;
      const p = load({ routes: { ['GET ' + url]: () => (fail ? Promise.reject(apiError(502, {})) : Promise.resolve(ok)) } });
      await settle();
      p.ctx.MMWatch.core.swap = (old, node) => {
        if (old.contains(p.doc.activeElement)) p.doc.activeElement.blur();
        old.replaceWith(node);
        return Promise.resolve();
      };
      const button = p.doc.querySelector('[data-discover-row="' + id + '"] [data-discover-retry]');
      button.focus();
      fail = false;
      click(p.doc, button);
      await settle();
      const row = p.doc.activeElement.closest('[data-discover-row]');
      return row && row.getAttribute('data-discover-row');
    };
    return { discover: await run('popular_tv', '/api/watch/discover', DISCOVER),
      recommended: await run('recommended', '/api/watch/recommended', recommended()) };
  },

  // Discover and Recommended arrive: the skeleton is replaced by real rows with badges.
  async loaded() {
    const p = load({ admin: true });
    await settle();
    // A row started twice (by watch-rows.js and by browse.js) still has one pair of arrows.
    p.doc.querySelectorAll('[data-discover-row] .watch-row__viewport').forEach(p.ctx.MMWatch.rows.init);
    const slot = p.doc.getElementById('watch-discover-slot');
    const trending = p.doc.querySelector('[data-discover-row="trending"]');
    return {
      rows: rowIds(p.doc),
      skeleton: !!slot.querySelector('.watch-discover-skel'),
      busy: slot.getAttribute('aria-busy'),
      label: text(slot.querySelector('.watch-group-label')),
      refs: trending.querySelectorAll('[data-title-ref]').map((a) => a.getAttribute('data-title-ref')),
      hrefs: trending.querySelectorAll('[data-title-ref]').map((a) => a.getAttribute('href')),
      kinds: p.doc.querySelectorAll('[data-discover-row] [data-title-ref]').map((a) => [a.getAttribute('data-title-ref'), a.getAttribute('data-kind')]),
      badges: ['movie-11', 'tmdb-movie-2', 'tmdb-tv-3', 'tmdb-movie-4'].map((r) => badgeOf(p.doc, r)),
      hostileTitle: trending.querySelectorAll('.watch-card__title')[3].childNodes.map((n) => n.nodeType),
      arrows: p.doc.querySelectorAll('[data-discover-row] .watch-row__viewport').map((v) => v.querySelectorAll('.watch-row__arrow').length),
    };
  },

  // /recommended lands with ?row=recommended: the marker leaves the address, keeping an old
  // #recommendation-<id>, and the Recommended row (970px down) comes up under the top bar.
  async landOnRecommended() {
    const rect = Node.prototype.getBoundingClientRect;
    Node.prototype.getBoundingClientRect = function () {
      return this.getAttribute('data-discover-row') === 'recommended'
        ? { top: 970, bottom: 1200, left: 0, right: 1280, width: 1280, height: 230 } : rect.call(this);
    };
    const run = async (opts) => {
      const p = load(opts);
      const replacedAtOnce = p.replaced.slice();
      await settle();
      return { replacedAtOnce, scrolls: p.scrolls };
    };
    return {
      landed: await run({ search: '?row=recommended', hash: '#recommendation-5' }),
      withMotion: await run({ search: '?view=rows&row=recommended', motion: true }),
      plain: await run({}),
      picksOff: await run({ search: '?row=recommended', enabled: false }),
      alreadyScrolled: await run({ search: '?row=recommended', scrollY: 300 }),
    };
  },

  // A held title that is leaving says when, beside what else its badges say, and a Keep of it is known by its card.
  async leavingCards() {
    const items = [card('movie-11', { in_library: true, href: '/watch/movie/11', leaving_days: 2, keep_id: '11' }), card('tmdb-movie-2', { leaving_days: null, keep_id: null })];
    const p = load({ items, routes: { 'GET /api/watch/discover': () => Promise.resolve({ ...DISCOVER, rows: [{ ...DISCOVER.rows[0], items }] }) } });
    await settle();
    return ['recommended', 'trending'].map((id) => scrollerOf(p, id).querySelectorAll('.watch-card').map((c) => [
      c.getAttribute('data-keep-id'), text(c.querySelector('.watch-badge--leaving .watch-badge__short')), c.querySelectorAll('.watch-badge--leaving').length]));
  },

  // A detail reports a new download state: every card of that title changes its badge.
  async titleChanged() {
    const p = load();
    await settle();
    const send = (detail) => (p.doc.listeners['mm:title-changed'] || []).forEach((fn) => fn({ detail }));
    send({ ref: 'tmdb-movie-5', download_state: 'queued', download_pct: null });
    const queued = badgeOf(p.doc, 'tmdb-movie-5');
    send({ ref: 'tmdb-movie-5', download_state: 'downloading', download_pct: 42 });
    const downloading = badgeOf(p.doc, 'tmdb-movie-5');
    send({ ref: 'tmdb-movie-5', download_state: 'in_library', download_pct: null });
    return { queued, downloading, inLibrary: badgeOf(p.doc, 'tmdb-movie-5') };
  },

  // A detail opened from a result reports its state: the pick of the same title, kind and year shows it too.
  async titleChangedOnAnotherRef() {
    const same = { title: 'Title tmdb-movie-5' };
    const p = load({ items: [card('rec-9', same), card('rec-10', { ...same, year: 1999 }), card('rec-11', { ...same, kind: 'show' })] });
    await settle();
    const send = (detail) => (p.doc.listeners['mm:title-changed'] || []).forEach((fn) => fn({ detail }));
    send({ ref: 'tmdb-movie-5', download_state: 'queued', download_pct: null, title: same.title, kind: 'movie', year: 2024 });
    const at = () => ['tmdb-movie-5', 'rec-9', 'rec-10', 'rec-11'].map((ref) => badgeOf(p.doc, ref));
    const queued = at();
    send({ ref: 'tmdb-movie-5', download_state: 'downloading', download_pct: 42, title: same.title, kind: 'movie', year: 2024 });
    return { queued, downloading: at() };
  },

  // TMDB fails: each discover row says so with Try again, which loads them in place.
  async discoverFailed() {
    let fail = true;
    const p = load({ routes: { 'GET /api/watch/discover': () => (fail ? Promise.reject(apiError(502, {})) : Promise.resolve(DISCOVER)) } });
    await settle();
    const notes = p.doc.querySelectorAll('.watch-row__note--failed').map(text);
    fail = false;
    click(p.doc, p.doc.querySelector('[data-discover-retry="discover"]'));
    await settle();
    return {
      notes,
      rec: !!rec(p.doc),
      after: rowIds(p.doc),
      failedLeft: p.doc.querySelectorAll('.watch-row__note--failed').length,
      cards: p.doc.querySelectorAll('[data-discover-row="trending"] .watch-card').length,
    };
  },

  // No TMDB and no picks for a viewer: the skeleton goes and nothing takes its place.
  async nothing() {
    const p = load({ tmdb: false, enabled: false });
    await settle();
    const slot = p.doc.getElementById('watch-discover-slot');
    return { children: slot.children.length, calls: p.calls };
  },

  // Recommendations switched off.
  async recsOff() {
    const viewer = load({ enabled: false });
    const admin = load({ enabled: false, admin: true });
    await settle();
    const note = rec(admin.doc) && rec(admin.doc).querySelector('.watch-row__note');
    return {
      viewerRow: !!rec(viewer.doc),
      viewerRows: rowIds(viewer.doc),
      adminNote: text(note),
      adminLink: note && note.querySelector('a') && note.querySelector('a').getAttribute('href'),
      adminTools: rec(admin.doc).querySelectorAll('.row-tool').length,
    };
  },

  // A row scrolled while the skeleton fades out is moved to its place and stays where it was.
  async rowsMovedIn() {
    const append = Node.prototype.appendChild;
    const p = load({ motion: true });
    await settle();
    const scroller = scrollerOf(p, 'trending');
    let left = 300;
    Object.defineProperty(scroller, 'scrollLeft', { get: () => left, set: (v) => { left = v; } });
    scroller.scrollTo = (o) => { left = o.left; };
    Node.prototype.appendChild = function (n) {
      if (n.contains && n.contains(scroller)) left = 0;
      return append.call(this, n);
    };
    const before = p.doc.querySelector('.watch-discover-layer') !== null;
    await tick(p);
    Node.prototype.appendChild = append;
    return { before, after: p.doc.querySelector('.watch-discover-layer') !== null, left };
  },

  // The page is held before each of the two swaps.
  async heldAroundTheSwaps() {
    const p = load({ motion: true });
    const slot = p.doc.getElementById('watch-discover-slot');
    const held = [];
    p.ctx.MMWatch.address.hold = (n) => held.push([n === slot, !!slot.querySelector('.watch-discover-layer'), !!slot.querySelector('.watch-discover-skel')]);
    await settle();
    await tick(p);
    return held;
  },

  // No picks at all: what the first ones are, and for an admin the way to make them.
  async noPicks() {
    const at = async (opts) => {
      const p = load({ items: [], batches: [], ...opts });
      await settle();
      const note = rec(p.doc).querySelector('.watch-row__note');
      const link = note.querySelector('a');
      return [text(note.children[0]), link && link.getAttribute('href')];
    };
    return { viewer: await at({}), admin: await at({ admin: true }), noKey: await at({ admin: true, mine: MINE.noKey }) };
  },

  // The tools of the row and every Refresh in it.
  async tools() {
    const at = async (opts) => {
      const p = load({ mine: MINE.ready, ...opts });
      await settle();
      return [rec(p.doc).querySelector('.watch-row__tools').children.map((b) => b.hasAttribute('data-picks-menu')), rec(p.doc).querySelectorAll('.row-tool--refresh').map(text),
        p.calls.filter((c) => /recommended\/refresh/.test(c))];
    };
    const running = { available: false, next_available_at: null, running: true };
    return [await at({}), await at({ admin: true }), await at({ admin: true, refresh: running })];
  },

  // A row of three batches, the last two loaded as it is scrolled; what the button says and the calls made.
  async follows() {
    const { p, pill, menu } = await threeBatches();
    const s = scrollerOf(p, 'recommended');
    const out = { start: pill(), sizers: rec(p.doc).querySelectorAll('.picks-name__sizer .row-tool__long').map((n) => n.textContent) };
    const at = async (left) => { await scroll(s, left); return pill(); };
    await at(0);
    const calls = p.calls.length;
    out.refs = refsOf(s).length;
    out.halfway = await at(3.4 * CARD_PX);
    out.over = await at(3.6 * CARD_PX);
    out.fading = rec(p.doc).querySelectorAll('.picks-name__one').length;
    await at(3.7 * CARD_PX);
    out.still = rec(p.doc).querySelectorAll('.picks-name__one').length;
    p.timers.splice(0).filter((t) => t.ms < 1000).forEach((t) => t.fn());
    out.faded = rec(p.doc).querySelectorAll('.picks-name__one').length;
    out.last = await at(13 * CARD_PX);
    out.label = rec(p.doc).querySelector('[data-picks-menu]').getAttribute('aria-label');
    out.live = rec(p.doc).querySelector('[data-picks-menu]').closest('[aria-live], [role="status"]') !== null;
    out.checked = menu();
    out.back = await at(0);
    out.asked = p.calls.length - calls;
    return out;
  },

  // Choosing a date scrolls the same row to it, loading on when it is not there yet; the scroll it
  // makes does not choose again, and the person's own does.
  async chooses() {
    const { p, pill, choose } = await threeBatches({ far: true });
    const section = rec(p.doc);
    const s = scrollerOf(p, 'recommended');
    const out = { before: refsOf(s).length };
    await choose(2);
    out.loaded = refsOf(s).length;
    out.unique = new Set(refsOf(s)).size;
    out.at = s.scrollLeft / CARD_PX;
    out.name = pill();
    await scroll(s, 4 * CARD_PX); // on the way there
    out.onTheWay = pill();
    out.busy = s.parentNode.getAttribute('aria-busy');
    s.parentNode.listeners.wheel.forEach((fn) => fn());
    await scroll(s, 6 * CARD_PX);
    out.theirs = pill();
    const calls = p.calls.length;
    await choose(0);
    out.latest = [s.scrollLeft, pill(), p.calls.length - calls, scrollerOf(p, 'recommended') === s, rec(p.doc) === section];
    return out;
  },

  // The date chosen cannot be loaded: the row stays, says so, and its name is still true.
  async choosesLost() {
    const at = async (status) => {
      const { p, pill, choose } = await threeBatches({ far: true, lost: status });
      const s = scrollerOf(p, 'recommended');
      await choose(2);
      return { name: pill(), left: s.scrollLeft, busy: s.parentNode.getAttribute('aria-busy'), toasts: p.toasts.map((t) => t.message) };
    };
    return [await at(502), await at(429)];
  },

  // A date with nothing left in the row: to the next older one with some.
  async choosesEmptied() {
    const { p, pill, choose } = await threeBatches({ far: true, emptied: true });
    await choose(1);
    const s = scrollerOf(p, 'recommended');
    return { name: pill(), at: refsOf(s)[s.scrollLeft / CARD_PX] };
  },

  // The person scrolls on their own while the date they chose is on its way: the row is theirs.
  async choosesLeft() {
    let answer;
    const { p, pill, choose } = await threeBatches({ far: true, slow: () => new Promise((resolve) => { answer = resolve; }) });
    const s = scrollerOf(p, 'recommended');
    await choose(2);
    const busy = s.parentNode.getAttribute('aria-busy');
    s.parentNode.listeners.wheel.forEach((fn) => fn());
    s.scrollLeft = 5;
    answer();
    await settle();
    return { busy, after: s.parentNode.getAttribute('aria-busy'), left: s.scrollLeft, name: pill() };
  },

  // A first batch of picks from two days has no date: the pill and menu say Latest alone.
  async undatedFirstBatch() {
    const p = load({ batches: [{ id: '2026-11-21', label: 'Latest picks', date_label: '' }, ...BATCHES.slice(1)] });
    await settle();
    const button = rec(p.doc).querySelector('[data-picks-menu]');
    click(p.doc, button);
    const on = button.querySelector('.is-on');
    const out = { pill: text(on.querySelector('.row-tool__long')), phone: text(on.querySelector('.picks-name__short')) };
    return { ...out, label: button.getAttribute('aria-label'), items: p.doc.getElementById('watch-picks-menu').querySelectorAll('.watch-keep-menu__item').map(text) };
  },

  // A person's own picks, one of them in the library, before a shared one; then an older batch
  // that has one of the titles again under another number.
  async ownRow() {
    layout();
    const p = load({ mine: MINE.ready, batches: BATCHES, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      'GET /api/watch/recommended': () => Promise.resolve({ ...recommended({ mine: MINE.ready, items: OWN_ITEMS }), next_batch: '2026-11-14' }),
      'GET /api/watch/recommended?batch=2026-11-14&start=2026-11-21': () => Promise.resolve({ ...recommended({ batch: '2026-11-14',
        items: [pick('pick-30', { title: 'Title pick-2' }), pick('pick-31'), pick('javascript:1')] }), next_batch: null }),
    } });
    await settle();
    const scroller = scrollerOf(p, 'recommended');
    await scroll(scroller, 0);
    return { refs: refsOf(scroller), picks: scroller.querySelectorAll('.watch-card__link').map((a) => a.getAttribute('data-pick-ref')),
      marked: scroller.querySelectorAll('.watch-card__ai').map(text), line: lineOf(p), chip: text(rec(p.doc).querySelector('.ai-label')),
      updated: (p.doc.listeners['mm:title-changed'].forEach((fn) => fn({ detail: { ref: 'x', title: 'Title pick-2', kind: 'movie', year: 2024, download_state: 'queued' } })), badgeOf(p.doc, 'pick-2')) };
  },

  // What the line under the heading says, and whether there is a Refresh, in each state.
  async ownStates() {
    const at = async (mine, opts = {}) => {
      const p = load({ mine, ...opts });
      await settle();
      const button = rec(p.doc).querySelector('[data-own-refresh]');
      if (button) click(p.doc, button);
      return { ...lineOf(p), toasts: p.toasts.map((t) => t.message), posts: p.calls.filter((c) => /^POST/.test(c)).length };
    };
    const off = load({ enabled: false, admin: true });
    await settle();
    return {
      wait: await at(MINE.wait, { items: OWN_ITEMS }),
      limit: await at({ ...MINE.wait, reason: 'daily_limit', next_available_at: new Date(Date.now() + 5 * HOUR - 60000).toISOString() }),
      few: await at(MINE.few),
      noKeyViewer: await at(MINE.noKey),
      noKeyAdmin: await at(MINE.noKey, { admin: true }),
      off: !!rec(off.doc).querySelector('.rec-line'),
    };
  },

  // A person has theirs made: busy, then the new picks in place and a week's wait.
  async ownRefreshDone() {
    let done = false;
    const p = load({ routes: {
      'GET /api/watch/recommended': () => Promise.resolve(done ? recommended({ mine: MINE.wait, items: OWN_ITEMS }) : recommended({ mine: MINE.ready })),
      'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
      'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: done ? 'done' : 'running' }),
    } });
    await settle();
    const section = rec(p.doc);
    const before = lineOf(p);
    const button = section.querySelector('[data-own-refresh]');
    button.focus();
    click(p.doc, button);
    click(p.doc, button);
    await settle();
    const busy = { ...lineOf(p), spinning: button.classList.contains('is-busy') };
    await tick(p);
    done = true;
    await tick(p);
    return { before, busy, after: lineOf(p), sameRow: rec(p.doc) === section, focus: p.doc.activeElement === section.querySelector('[data-own-refresh]'),
      refs: section.querySelectorAll('[data-title-ref]').map((a) => a.getAttribute('data-title-ref')),
      posts: p.calls.filter((c) => /^POST/.test(c)), toasts: p.toasts.map((t) => t.message) };
  },

  // It made none: the row stays as it was, says so, and can be tried again.
  async ownRefreshFailed() {
    const p = load({ mine: MINE.ready, routes: {
      'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
      'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'failed' }),
    } });
    await settle();
    const scroller = rec(p.doc).querySelector('.watch-row__scroll');
    click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
    await settle();
    await tick(p);
    const failed = { ...lineOf(p), variant: p.toasts[0].opts.variant };
    click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
    await settle();
    return { failed, sameCards: rec(p.doc).querySelector('.watch-row__scroll') === scroller, again: lineOf(p) };
  },

  // The server refuses: what it says takes the button's place, or the button waits.
  async ownRefused() {
    const at = async (error, opts) => {
      const p = load({ mine: MINE.ready, routes: {
        'POST /api/watch/picks/refresh': () => Promise.reject(error),
        'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'running' }),
      } });
      await settle();
      const button = rec(p.doc).querySelector('[data-own-refresh]');
      button.focus();
      click(p.doc, button);
      await settle();
      return { ...lineOf(p), focus: p.doc.activeElement.className.split(' ')[0], toasts: p.toasts.length, href: p.ctx.location.href };
    };
    return {
      wait: await at(apiError(429, { error: 'wait', next_available_at: MINE.wait.next_available_at })),
      few: await at(apiError(409, { error: 'not_enough_plays', next_available_at: null })),
      running: await at(apiError(409, { error: 'running', next_available_at: null })),
      broken: await at(apiError(500, {})),
      out: await at(apiError(401, {})),
    };
  },

  // Found being made when the page loads, by the scan or an earlier press: the end is followed, and
  // an old failure is not told as this one's when there are new picks.
  async ownRunningAtLoad() {
    const at = async (status, items, mine) => {
      let done = false;
      const p = load({ routes: {
        'GET /api/watch/recommended': () => Promise.resolve(done ? recommended({ mine, items }) : recommended({ mine: MINE.running, items: OWN_ITEMS })),
        'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status }),
      } });
      await settle();
      const busy = lineOf(p);
      done = true;
      await tick(p);
      return { busy: busy.text, after: lineOf(p).text, toasts: p.toasts.map((t) => t.message) };
    };
    return {
      made: await at('failed', [pick('pick-40')], MINE.ready),
      none: await at('idle', OWN_ITEMS, MINE.ready),
      failed: await at('failed', OWN_ITEMS, MINE.ready),
    };
  },

  // The refresh followed ends with another, another tab's or a scan's, already running: that is followed too.
  async ownRunningElsewhere() {
    let mine = MINE.ready;
    let items = OWN_ITEMS;
    let status = 'running';
    const statuses = (p) => p.calls.filter((c) => c === 'GET /api/watch/picks/refresh').length;
    const p = load({ routes: {
      'GET /api/watch/recommended': () => Promise.resolve(recommended({ mine, items })),
      'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
      'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status }),
    } });
    await settle();
    mine = MINE.running;
    click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
    await settle();
    const busy = lineOf(p);
    status = 'failed';
    await tick(p);
    const still = { ...lineOf(p), toasts: p.toasts.length };
    mine = MINE.wait;
    items = [pick('pick-40')];
    await tick(p);
    return { busy, still, polls: statuses(p), after: lineOf(p), toasts: p.toasts.map((t) => t.message) };
  },

  // Nobody can tell how it goes, or the new picks do not load: it can be tried again.
  async ownLost() {
    const at = async (lose) => {
      let lost = false;
      const p = load({ routes: {
        'GET /api/watch/recommended': () => (lost ? Promise.reject(apiError(502, {})) : Promise.resolve(recommended({ mine: MINE.running, items: OWN_ITEMS }))),
        'GET /api/watch/picks/refresh': () => (lose === 'status' ? Promise.reject(new Error('offline')) : Promise.resolve({ ok: true, status: 'done' })),
      } });
      await settle();
      lost = true;
      for (let i = 0; i < 20 && !p.toasts.length; i += 1) await tick(p);
      return { ...lineOf(p), toasts: p.toasts.length, timers: p.timers.length };
    };
    return { status: await at('status'), picks: await at('picks') };
  },

  // It made none, and meanwhile a newer batch of only shared picks is there.
  async ownFailedNewerBatch() {
    let done = false;
    const batches = [{ id: '2026-11-22', label: 'Latest picks', date_label: '22 Nov' }, ...BATCHES];
    const at = async (shared) => {
      done = false;
      const p = load({ routes: {
        'GET /api/watch/recommended': () => Promise.resolve(done ? recommended({ mine: MINE.ready, batches, items: shared })
          : recommended({ mine: MINE.ready, items: OWN_ITEMS.filter((i) => i.own === (shared[0].pick_ref === 'rec-50')) })),
        'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
        'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'failed' }),
      } });
      await settle();
      click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
      await settle();
      done = true;
      await tick(p);
      return { ...lineOf(p), refs: refsOf(scrollerOf(p, 'recommended')), toasts: p.toasts.map((t) => t.message) };
    };
    return { hadOwn: await at([pick('rec-50')]), hadNone: await at([pick('rec-51')]) };
  },

  // It made none, and that was the day's last try; or the plays it needs are gone.
  async ownFailedLast() {
    const at = async (mine) => {
      let done = false;
      const p = load({ routes: {
        'GET /api/watch/recommended': () => Promise.resolve(recommended({ mine: done ? mine : MINE.ready, items: OWN_ITEMS })),
        'POST /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'started' }),
        'GET /api/watch/picks/refresh': () => Promise.resolve({ ok: true, status: 'failed' }),
      } });
      await settle();
      click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
      await settle();
      done = true;
      await tick(p);
      return { ...lineOf(p), toasts: p.toasts.map((t) => t.message) };
    };
    return {
      limit: await at({ ...MINE.wait, reason: 'daily_limit' }),
      few: await at(MINE.few),
      noKey: await at(MINE.noKey),
    };
  },

  // The day's limit passes while the page is open: the clock of the button takes its line away too.
  async ownLimitPasses() {
    const p = load({ items: OWN_ITEMS, mine: { ...MINE.wait, reason: 'daily_limit', next_available_at: new Date(Date.now() + 60000).toISOString() } });
    await settle();
    const before = lineOf(p);
    const now = Date.now();
    p.ctx.Date.now = () => now + 61000;
    await tick(p);
    const out = { before, after: lineOf(p) };
    click(p.doc, rec(p.doc).querySelector('[data-own-refresh]'));
    await settle();
    return { ...out, posts: p.calls.filter((c) => /^POST/.test(c)).length };
  },

  // On a phone Refresh has fewer words.
  async ownNarrow() {
    const at = async (mine) => {
      const p = load({ mine });
      await settle();
      return p.doc.querySelector('[data-own-refresh] .row-tool__face').getAttribute('data-narrow');
    };
    return { ready: await at(MINE.ready), wait: await at(MINE.wait), busy: await at(MINE.running) };
  },

  // The session ran out before the rows loaded: off to the login page, not a "Couldn't load".
  async loggedOutOnLoad() {
    const p = load({ routes: {
      'GET /api/watch/discover': () => Promise.reject(apiError(401, {})),
      'GET /api/watch/recommended': () => Promise.reject(apiError(401, {})),
    } });
    await settle();
    return { href: p.ctx.location.href, failed: p.doc.querySelectorAll('.watch-row__note--failed').length };
  },

  // Seventeen batches on a 390x844 phone, the pill low on the screen: the menu stays on the
  // screen, scrolls inside, and End brings the last batch into view.
  async longPicksMenu() {
    const ITEM = 46;
    const PAD = 7;
    const batches = Array.from({ length: 17 }, (_, i) => ({ id: 'b' + i, label: 'Picks', date_label: (i + 1) + ' Oct' }));
    const p = load({ batches, innerHeight: 844, innerWidth: 390 });
    await settle();
    const pill = rec(p.doc).querySelector('[data-picks-menu]');
    pill.getBoundingClientRect = () => ({ top: 500, bottom: 534, left: 20, right: 120, width: 100, height: 34 });
    const menu = p.doc.getElementById('watch-picks-menu');
    const full = batches.length * ITEM + 2 * PAD;
    menu.scrollTop = 0;
    Object.defineProperty(menu, 'offsetHeight', { get: () => (menu.style.maxHeight ? parseFloat(menu.style.maxHeight) : full) });
    const box = () => {
      const top = parseFloat(menu.style.top);
      return { top, bottom: top + menu.offsetHeight };
    };
    menu.getBoundingClientRect = () => ({ ...box(), left: 0, right: 200, width: 200, height: menu.offsetHeight });
    const rect = Node.prototype.getBoundingClientRect;
    Node.prototype.getBoundingClientRect = function () {
      if (this.parentNode !== menu) return rect.call(this);
      const top = box().top + PAD + menu.children.indexOf(this) * ITEM - menu.scrollTop;
      return { top, bottom: top + ITEM - 2, left: 0, right: 200, width: 200, height: ITEM - 2 };
    };
    click(p.doc, pill);
    const opened = box();
    const items = menu.querySelectorAll('.watch-keep-menu__item');
    (p.doc.listeners.keydown || []).forEach((fn) => fn({ key: 'End', preventDefault() {} }));
    const last = items[items.length - 1];
    return {
      full,
      top: opened.top,
      bottom: opened.bottom,
      capped: menu.style.maxHeight,
      lastFocused: p.doc.activeElement === last,
      lastTop: last.getBoundingClientRect().top,
      lastBottom: last.getBoundingClientRect().bottom,
      menuTop: box().top,
      menuBottom: box().bottom,
    };
  },

  // A season line of pills 100px apart: the keys and the arrow page it like a poster row.
  async seasonLine() {
    const p = load();
    await settle();
    const pills = Array.from({ length: 12 }, (_, i) => ['button', { class: 'watch-pill' }, 'Season ' + (i + 1)]);
    const viewport = build(p.doc, ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ...pills]]);
    p.doc.body.appendChild(viewport);
    const scroller = viewport.querySelector('.watch-row__scroll');
    scroller.children.forEach((pill, i) => { pill.offsetLeft = i * 100; });
    Object.assign(scroller, { clientWidth: 400, scrollWidth: 1200, scrollLeft: 0, scrollTo(o) { this.scrollLeft = o.left; } });
    p.ctx.MMWatch.rows.init(viewport);
    const next = viewport.querySelector('.watch-row__arrow--next');
    const key = (k) => scroller.listeners.keydown.forEach((fn) => fn({ key: k, target: scroller, preventDefault() {} }));
    const out = { hasNext: viewport.classList.contains('has-next') };
    key('ArrowRight');
    out.afterKey = scroller.scrollLeft;
    next.listeners.click.forEach((fn) => fn());
    out.afterArrow = scroller.scrollLeft;
    return out;
  },

  // Pills of different widths, 700px of line: the arrow goes to the first not wholly in view, and
  // back to end at the one cut off; the keys go a pill at a time.
  async unevenSeasonLine() {
    const p = load();
    await settle();
    const widths = [100, 300, 100, 260, 100, 100, 400, 100];
    const viewport = build(p.doc, ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ...widths.map((w, i) => ['button', { class: 'watch-pill' }, 'Season ' + i])]]);
    p.doc.body.appendChild(viewport);
    const scroller = viewport.querySelector('.watch-row__scroll');
    let at = 0;
    scroller.children.forEach((pill, i) => {
      pill.offsetLeft = at;
      Object.defineProperty(pill, 'offsetWidth', { value: widths[i] });
      at += widths[i];
    });
    Object.assign(scroller, { clientWidth: 700, scrollWidth: at, scrollLeft: 0, scrollTo(o) { this.scrollLeft = o.left; } });
    p.ctx.MMWatch.rows.init(viewport);
    const press = (dir) => { viewport.querySelector('.watch-row__arrow--' + dir).listeners.click.forEach((fn) => fn()); return scroller.scrollLeft; };
    const key = (k) => { scroller.listeners.keydown.forEach((fn) => fn({ key: k, target: scroller, preventDefault() {} })); return scroller.scrollLeft; };
    return { arrows: [press('next'), press('next'), press('next'), press('prev'), press('prev')], keys: [key('ArrowRight'), key('ArrowRight'), key('ArrowLeft')] };
  },

  // A library row goes on from where the page's own cards end.
  async libraryRow() {
    layout();
    const item = (key, over) => ({ kind: 'show', title: 'Show ' + key, subtitle: 'TV · 2 seasons', poster_url: '/api/poster/' + key, href: '/watch/show/' + key,
      watched: false, mark_kind: 'show', mark_key: String(key), leaving_days: null, keep_id: null, ...over });
    const url = (after) => 'GET /api/watch/library/tv/row?after=' + after;
    const p = load({ library: { next: 'a=1', keys: [1, 2] }, routes: {
      'GET /api/watch/discover': () => Promise.resolve({ rows: [] }),
      [url('a%3D1')]: () => Promise.resolve({ items: [item(2), item(3, { watched: true, keep_id: 'k', leaving_days: 2 })], next: 'b2' }),
      [url('b2')]: () => Promise.resolve({ items: [item(4)], next: null }),
    } });
    await settle();
    const scroller = p.doc.querySelector('[data-row-kind] .watch-row__scroll');
    await scroll(scroller, 0);
    await scroll(scroller, 0);
    const third = scroller.querySelectorAll('.watch-card')[2];
    return { refs: refsOf(scroller), calls: p.calls.filter((c) => /library/.test(c)), more: scroller.parentNode.hasAttribute('data-row-more'),
      third: [third.getAttribute('data-mark'), !!third.querySelector('.watch-badge--watched'), !!third.querySelector('[data-watch-more]'), !!third.querySelector('[data-watch-keep]')] };
  },

  // A row whose rest the server has goes on from there as a grid too, in the grid's pages and orders.
  async libraryGrids() {
    const out = {};
    for (const id of ['movies', 'tv', 'anime', 'recent']) {
      const p = load({ grids: { [id]: 'Row' }, routes: { [`GET /api/watch/library/${id}/row?after=k&limit=60`]: () => Promise.resolve({ items: [], next: null, total: 9 }),
        [`GET /api/watch/library/${id}/row?after=&limit=60&sort=az`]: () => Promise.resolve({ items: [], next: 'z', total: 9 }) } });
      await settle();
      let made = null;
      p.ctx.MMWatch.grid.open = (o) => { made = o; };
      p.doc.body.appendChild(build(p.doc, ['div', {}, ['div', { class: 'watch-all__body' }], ['a', { href: '/watch?all=' + id, 'data-row-open': id }, 'Row']]));
      p.doc.querySelector('.watch-all__body').toggleAttribute = () => {};
      click(p.doc, p.doc.querySelector('[data-row-open="' + id + '"]'));
      out[id] = made.fetchPage ? [(await made.fetchPage('k')).total, (await made.sort.choose('az')).next] : null;
    }
    return out;
  },

  // Discover cards carry a mark and More only for a title the library holds, whatever the server sends.
  async discoverMarks() {
    const p = load({ routes: { 'GET /api/watch/discover': () => Promise.resolve({ rows: [{ id: 'trending', items: [
      card('movie-11', { in_library: true, mark_kind: 'movie', mark_key: '11', watched: true }),
      card('tmdb-movie-2', { mark_kind: null, mark_key: null, watched: false }),
      card('tmdb-movie-3', { mark_kind: 'movie', mark_key: '1/../x', watched: true })] }] }) } });
    await settle();
    return scrollerOf(p, 'trending').querySelectorAll('.watch-card').map((c) => [c.getAttribute('data-mark'), !!c.querySelector('.watch-badge--watched'),
      c.querySelectorAll('[data-watch-more]').length, !!c.querySelector('.watch-card__link [data-watch-more]')]);
  },

  // Keep from Leaving soon: a toast with Undo, and focus moves to the next card's Keep.
  async keepFromRow() {
    const p = load({ routes: { 'POST /api/watch/keep': () => Promise.resolve({ ok: true }) } });
    await settle();
    const first = p.doc.querySelector('[data-watch-keep="1100"]');
    first.focus();
    click(p.doc, first);
    click(p.doc, p.doc.querySelector('#watch-keep-menu [data-days]'));
    await settle();
    const toast = p.toasts[0];
    return {
      gone: !p.doc.body.contains(first.closest('.watch-card')),
      focus: p.doc.activeElement.getAttribute('data-watch-keep'),
      toast: toast.message,
      undo: toast.opts.action.label,
    };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
