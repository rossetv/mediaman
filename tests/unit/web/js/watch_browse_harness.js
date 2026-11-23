// Runs the Watch page's browse scripts (core, keep, browse, watch-rows) against the fake DOM and
// prints the outcome of one scenario as JSON. Usage: node watch_browse_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build, click, settle } = require('./fake_dom');

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
  const out = { ok: true, enabled: opts.enabled !== false, batch: opts.batch || batches[0].id,
    batches: opts.enabled === false ? [] : batches,
    items: opts.items || [card('rec-7', { rec_id: 7, reason: 'Because' }), card('rec-8', { rec_id: 8 })] };
  if (opts.enabled === false) out.items = [];
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
  const skelRow = ['div', { class: 'watch-row watch-row--skel' },
    ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ['div', { class: 'watch-card--skel' }]]]];
  const leaving = (id, title) => ['article', { class: 'watch-card', 'data-keep-id': id },
    ['a', { class: 'watch-card__link', href: '/watch/movie/' + id, 'data-title-ref': 'movie-' + id }, ['span', { class: 'watch-card__poster' }, title]],
    ['button', { class: 'watch-keep', 'data-watch-keep': id, 'aria-label': 'Keep ' + title }, 'Keep']];
  doc.body.appendChild(build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['input', { id: 'watch-search' }],
    ['div', { class: 'watch-rows' },
      ['section', { class: 'watch-row', 'data-watch-leaving-row': '' },
        ['div', { class: 'watch-row__viewport' },
          ['div', { class: 'watch-row__scroll' }, leaving('1100', 'Leaving Film'), leaving('1101', 'Northbound')]]],
      ['div', { class: 'watch-discover-slot', id: 'watch-discover-slot', 'aria-busy': 'true' },
        ['div', { class: 'watch-discover-skel', 'aria-hidden': 'true' },
          ['p', { class: 'watch-group-label' }, 'Discover'], skelRow, skelRow, skelRow]]],
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', hidden: '' },
      ['button', { class: 'watch-keep-menu__item', 'data-days': '30' }, 'Keep 30 days']]]));
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' },
    JSON.stringify({ is_admin: !!opts.admin, tmdb_configured: opts.tmdb !== false })]));
}

/* *routes* maps a URL to a function returning a promise; calls are recorded. */
function load(opts = {}) {
  const doc = makeDocument();
  page(doc, opts);
  const toasts = [];
  const timers = [];
  const calls = [];
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
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, WeakSet, Date, Error,
    parseInt, parseFloat, encodeURIComponent,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingLeft: '0', paddingRight: '0' }),
    matchMedia: () => ({ matches: true }), // reduced motion: swaps are immediate
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} },
    innerHeight: opts.innerHeight || 900,
    innerWidth: opts.innerWidth || 1280,
    location: { pathname: '/watch', href: '/watch' },
    UIFeedback: { toast: (message, o) => toasts.push({ message, opts: o }) },
    MM: { api: { get: call('GET'), post: call('POST'), messageOf: (e, f) => (e && e.data && e.data.message) || f } },
    addEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['watch/core.js', 'watch/keep.js', 'watch/browse.js', 'watch-rows.js']
    .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));
  return { ctx, doc, toasts, timers, calls, routes };
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

const scenarios = {
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
      badges: ['movie-11', 'tmdb-movie-2', 'tmdb-tv-3', 'tmdb-movie-4'].map((r) => badgeOf(p.doc, r)),
      hostileTitle: trending.querySelectorAll('.watch-card__title')[3].childNodes.map((n) => n.nodeType),
      arrows: p.doc.querySelectorAll('[data-discover-row] .watch-row__viewport').map((v) => v.querySelectorAll('.watch-row__arrow').length),
    };
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

  // A batch with no picks.
  async noPicks() {
    const viewer = load({ items: [] });
    const admin = load({ items: [], admin: true });
    await settle();
    return { viewer: text(rec(viewer.doc).querySelector('.watch-row__note')), admin: text(rec(admin.doc).querySelector('.watch-row__note')) };
  },

  // Viewers can switch batches but get no Refresh; admins get both.
  async tools() {
    const viewer = load();
    const admin = load({ admin: true });
    await settle();
    const names = (doc) => rec(doc).querySelectorAll('.row-tool').map((b) => b.hasAttribute('data-picks-menu') ? 'picks' : b.hasAttribute('data-picks-refresh') ? 'refresh' : '?');
    return { viewer: names(viewer.doc), admin: names(admin.doc), refresh: text(rec(admin.doc).querySelector('[data-picks-refresh]')) };
  },

  // The earlier-picks menu: the Keep menu's component, and choosing a batch swaps the row in place.
  async batch() {
    const p = load({ routes: { 'GET /api/watch/recommended?batch=2026-11-14': () => Promise.resolve(recommended({ batch: '2026-11-14', items: [card('rec-9')] })) } });
    await settle();
    const section = rec(p.doc);
    const pill = section.querySelector('[data-picks-menu]');
    click(p.doc, pill);
    const menu = p.doc.getElementById('watch-picks-menu');
    const open = {
      expanded: pill.getAttribute('aria-expanded'),
      keepMenuLook: menu.classList.contains('watch-keep-menu'),
      items: menu.querySelectorAll('.watch-keep-menu__item').map((i) => [text(i), i.getAttribute('aria-checked')]),
      focus: p.doc.activeElement === menu.querySelectorAll('.watch-keep-menu__item')[0],
    };
    click(p.doc, menu.querySelectorAll('.watch-keep-menu__item')[1]);
    await settle();
    return {
      open,
      sameRow: rec(p.doc) === section,
      cards: section.querySelectorAll('[data-title-ref]').map((a) => a.getAttribute('data-title-ref')),
      pill: text(section.querySelector('[data-picks-menu]')),
      checked: menu.querySelectorAll('[aria-checked="true"]').map(text),
    };
  },

  // Refresh while cooling down: the server's 429 shows the time left, not an error.
  async refreshCooldown() {
    const next = new Date(Date.now() + 14 * HOUR - 60000).toISOString();
    const p = load({ admin: true, routes: {
      'POST /api/recommended/refresh': () => Promise.reject(apiError(429, { message: 'Already refreshed', next_available_at: next })),
    } });
    await settle();
    click(p.doc, p.doc.querySelector('[data-picks-refresh]'));
    await settle();
    const button = p.doc.querySelector('[data-picks-refresh]');
    return {
      label: text(button),
      disabled: button.getAttribute('aria-disabled'),
      error: !!p.doc.querySelector('.watch-row__error'),
      note: text(p.doc.querySelector('.rec-menu__note')),
    };
  },

  // Refresh that the server refuses for another reason: an inline error, and Refresh is back.
  async refreshFailed() {
    const p = load({ admin: true, routes: {
      'POST /api/recommended/refresh': () => Promise.reject(apiError(200, { message: 'Plex not configured' })),
    } });
    await settle();
    click(p.doc, p.doc.querySelector('[data-picks-refresh]'));
    await settle();
    const button = p.doc.querySelector('[data-picks-refresh]');
    return { error: text(p.doc.querySelector('.watch-row__error')), label: text(button), disabled: button.getAttribute('aria-disabled') };
  },

  // A session that ran out mid-refresh goes to the login page.
  async refreshLoggedOut() {
    const p = load({ admin: true, routes: {
      'POST /api/recommended/refresh': () => Promise.resolve({ status: 'started' }),
      'GET /api/recommended/refresh/status': () => Promise.reject(apiError(401, {})),
    } });
    await settle();
    click(p.doc, p.doc.querySelector('[data-picks-refresh]'));
    await settle();
    await tick(p);
    return { href: p.ctx.location.href };
  },

  // Refresh runs, shows it is busy, and when done the row's picks change in place.
  async refreshDone() {
    let status = 'running';
    let fresh = false;
    const next = new Date(Date.now() + 24 * HOUR - 60000).toISOString();
    const p = load({ admin: true, routes: {
      'POST /api/recommended/refresh': () => Promise.resolve({ status: 'started' }),
      'GET /api/recommended/refresh/status': () => Promise.resolve(status === 'done' ? { status, result: { ok: true } } : { status }),
      'GET /api/watch/recommended': () => Promise.resolve(fresh
        ? recommended({ admin: true, items: [card('rec-20')], refresh: { available: false, next_available_at: next, running: false } })
        : recommended({ admin: true })),
    } });
    await settle();
    const section = rec(p.doc);
    click(p.doc, p.doc.querySelector('[data-picks-refresh]'));
    await settle();
    const busy = { label: text(p.doc.querySelector('[data-picks-refresh]')), spinning: p.doc.querySelector('[data-picks-refresh]').classList.contains('is-busy') };
    await tick(p);
    status = 'done';
    fresh = true;
    await tick(p);
    return {
      busy,
      sameRow: rec(p.doc) === section,
      cards: section.querySelectorAll('[data-title-ref]').map((a) => a.getAttribute('data-title-ref')),
      after: text(p.doc.querySelector('[data-picks-refresh]')),
      toast: p.toasts.map((t) => t.message),
    };
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
    menu.querySelectorAll('.watch-keep-menu__item').forEach((item, i) => {
      item.getBoundingClientRect = () => {
        const top = box().top + PAD + i * ITEM - menu.scrollTop;
        return { top, bottom: top + ITEM - 2, left: 0, right: 200, width: 200, height: ITEM - 2 };
      };
    });
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
