// Runs the real watch/space.js and watch/keep.js against the fake DOM and a made-up server, and
// prints one scenario's outcome as JSON. Usage: node watch_space_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const GB = 1024 ** 3;

// What the browser reflects or refuses and the fake DOM does not.
Object.defineProperty(Node.prototype, 'inert', {
  get() { return this.hasAttribute('inert'); },
  set(on) { if (on) this.setAttribute('inert', ''); else this.removeAttribute('inert'); },
});
const classes = Object.getOwnPropertyDescriptor(Node.prototype, 'classList').get;
Object.defineProperty(Node.prototype, 'classList', {
  get() { return Object.assign(classes.call(this), { 0: this.className.split(/\s+/)[0] }); },
});
Node.prototype.scrollIntoView = function () {};
Node.prototype.focus = function () {
  if (!this.isConnected || this.closest('[inert]') || this.closest('#space') && this.ownerDocument.getElementById('space').hidden) return;
  const was = this.ownerDocument.activeElement;
  this.ownerDocument.activeElement = this;
  if (was !== this) (this.listeners.focus || []).forEach((fn) => fn({}));
};
Node.prototype.remove = (function (remove) {
  return function () {
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    remove.call(this);
  };
})(Node.prototype.remove);

function storage(over) {
  return {
    used: '6.5 TB', total: '8.0 TB', free: '1.4 TB', total_bytes: 8192 * GB, used_bytes: 6656 * GB, free_bytes: 1434 * GB,
    movies_bytes: 3900 * GB, tv_bytes: 1900 * GB, anime_bytes: 0, other_bytes: 280 * GB,
    movies_label: '3.8 TB', tv_label: '1.9 TB', anime_label: '0 B', other_label: '280 GB',
    movies_pct: 47.6, tv_pct: 23.2, anime_pct: 0, other_pct: 3.4,
    readable: true, counts: { movies: 312, tv: 64, anime: 0 },
    disks: [{ path: '/media', readable: true, total_bytes: 8192 * GB, used_bytes: 6656 * GB, free_bytes: 1434 * GB, total: '8.0 TB', free: '1.4 TB',
      used_pct: 81.3, threshold_pct: 90, threshold_shared: true, over_threshold: false }],
    ...over,
  };
}

function row(id, over) {
  return { media_item_id: id, sa_id: 'sa' + id, title: 'Film ' + id, media_type: 'movie', plex_rating_key: id, type_label: 'MOVIE', ref: 'movie-' + id,
    season_number: null, countdown: 'Deletes in 3 days', days_left: 3, state: 'leaving', added_ago: '330 days ago', file_size: '10.0 GB', file_size_bytes: 10 * GB, ...over };
}

function title(name, bytes, over) {
  return { id: name, ref: 'movie-1', title: name, plex_rating_key: '1', file_size: Math.round(bytes / GB) + ' GB', file_size_bytes: bytes, season_count: null,
    last_watched: '', protected: false, deletion_days: null, ...over };
}

const label = (n) => Math.round(n / GB) + ' GB';

/* Answers as the real routes do; `hold(url)` keeps an answer until it is let go. */
class Server {
  constructor(rows) {
    this.rows = rows;
    this.deleted = [];
    this.storage = storage();
    this.extra = {};
    this.library = { movie: [title('Big', 80 * GB), title('Small', 40 * GB)], tv: [title('Show', 90 * GB, { season_count: 3 })], anime: [] };
    this.total = { movie: 312, tv: 64, anime: 0 };
    this.calls = [];
    this.held = [];
    this.holding = null;
    this.fail = {};
    this.scan = { running: false };
    this.page = null; // what /watch holds
    this.redirected = false;
    this.pages = 0; // how often it was asked for
  }
  stats() {
    const l = this.rows.filter((r) => r.state === 'leaving');
    const sum = (list) => list.reduce((n, r) => n + r.file_size_bytes, 0);
    return { storage: this.storage, reclaimed_total_bytes: 5 * GB, reclaimed_total: '5.0 GB',
      scheduled: { total: this.rows.length, total_bytes: sum(this.rows), total_size: label(sum(this.rows)), leaving: l.length, leaving_bytes: sum(l), leaving_size: label(sum(l)), unsized_count: this.rows.filter((r) => !r.file_size_bytes).length,
        overdue: this.rows.filter((r) => r.state === 'overdue').length, deleting: this.rows.filter((r) => r.state === 'deleting').length },
      unavailable_roots: [], deletions_off: false, plex_configured: true, show_storage: true, ...this.extra };
  }
  answer(url) {
    if (url === '/api/dashboard/stats') return this.stats();
    if (url === '/api/dashboard/scheduled') return { items: this.rows };
    if (url === '/api/dashboard/deleted') return { items: this.deleted };
    if (url === '/api/scan/status') return this.scan;
    const type = /type=(\w+)/.exec(url)[1];
    return { items: this.library[type], total: this.total[type], stats: { no_size: { movies: 0, tv: 0, anime: 0 } } };
  }
  request(method, url, body) {
    this.calls.push([method, url, body]);
    const failing = Object.keys(this.fail).find((k) => url.includes(k));
    const err = failing && Object.assign(new Error(this.fail[failing]), { message: this.fail[failing] });
    const value = err ? null : JSON.parse(JSON.stringify(method === 'GET' ? this.answer(url) : this.post(url, body)));
    const go = () => (err ? Promise.reject(err) : Promise.resolve(value));
    if (!this.holding || !url.includes(this.holding)) return go();
    return new Promise((resolve, reject) => this.held.push({ url, go: () => go().then(resolve, reject) }));
  }
  post(url, body) {
    if (url === '/api/watch/keep') { this.rows = this.rows.filter((r) => r.media_item_id !== body.media_item_id); return { ok: true }; }
    if (url === '/api/watch/unkeep') { this.rows = this.before.slice(); return { ok: true }; }
    const m = /^\/api\/media\/(.+)\/keep$/.exec(url);
    if (m) { this.rows = this.rows.filter((r) => r.media_item_id !== m[1]); return this.keepAnswer || { ok: true }; }
    if (url === '/api/scan/clear-scheduled') { const n = this.rows.length; this.rows = []; return { ok: true, cleared: n, already_deleting: 0 }; }
    return { status: 'started' };
  }
}

const card = (id) => ['article', { class: 'watch-card', 'data-keep-id': id }, ['a', { class: 'watch-card__link' }],
  ['button', { class: 'watch-keep', 'data-watch-keep': id, 'aria-label': 'Keep Film ' + id, id: 'card-keep-' + id }]];
const leavingSection = (...ids) => ['section', { 'data-watch-leaving-row': '', 'data-row-id': 'leaving', class: 'watch-row' }, ['h2', { class: 'watch-row__title' }, 'Leaving soon'],
  ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ...ids.map(card)]]];
const recentSection = () => ['section', { 'data-row-id': 'recent', class: 'watch-row' },
  ['div', { class: 'watch-row__scroll', id: 'recent-scroll' }, ['article', { class: 'watch-card', 'data-keep-id': '1' }, ['span', { class: 'watch-badge watch-badge--leaving' }, '3 days left']]]];

function markup() {
  const pane = (id) => ['div', { class: 'space-pane', id: 'space-' + id, role: 'tabpanel' }];
  const tab = (id) => ['button', { class: 'space-tab', id: 'space-tab-' + id, 'data-space-tab': id }, id, ['span', { class: 'space-tab__count' }]];
  return [
    ['header', { id: 'nav' }, ['button', { class: 'space-meter space-meter--phone', 'data-space-open': 'storage', id: 'phone-meter' }]],
    ['main', { id: 'main' },
      ['button', { class: 'space-meter', 'data-space-open': 'storage', id: 'meter', 'aria-busy': 'true' }],
      ['div', { class: 'watch-rows' }, leavingSection('1', '2'), recentSection()],
      ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', role: 'menu' },
        ...['7', '30', '90'].map((d) => ['button', { class: 'watch-keep-menu__item', 'data-days': d }, d])]],
    ['div', { class: 'space', id: 'space' },
      ['div', { class: 'space__veil', 'data-space-close': '' }],
      ['section', { class: 'space__panel', role: 'dialog', 'aria-label': 'Storage' },
        ['div', { class: 'space__grab' }],
        ['div', { class: 'space__tabs' }, ['span', { class: 'space__ink' }], tab('storage'), tab('scheduled'), tab('deleted')],
        ['button', { class: 'space__close', 'data-space-close': '', id: 'close' }],
        pane('storage'), pane('scheduled'), pane('deleted'),
        ['footer', { class: 'space__foot' }, ['p', { id: 'space-scan-status', class: 'space__scan-status' }],
          ['button', { id: 'space-scan', class: 'btn space__scan' }, ['svg', { class: 'ico' }, ['use', { href: '#i-rotate-right' }]], ['span', { class: 'space__scan-label' }, 'Scan now']]]],
      ['p', { id: 'space-live' }]],
  ];
}

async function page(rows = ['1', '2', '3'].map((id) => row(id)), setup = () => {}, opts = {}) {
  const doc = makeDocument();
  const create = doc.createElement;
  doc.createElement = (t) => {
    const n = create(t);
    n.style.setProperty = (k, v) => { n.style[k] = v; };
    n.style.getPropertyValue = (k) => n.style[k] || '';
    return n;
  };
  markup().forEach((m) => doc.body.appendChild(build(doc, m)));
  const $ = (id) => doc.getElementById(id);
  $('space').hidden = true;
  const server = new Server(rows);
  server.before = rows.slice();
  setup(server);
  const toasts = [];
  const pushed = [];
  let layer = null;
  let confirmed = true;
  const timers = [];
  const intervals = new Map();
  const ready = [];
  const address = {
    layer(def) { layer = def; },
    url: (_params, hash) => '#' + hash,
    push(_name, url) { pushed.push(['push', url]); },
    replace(_name, url) { pushed.push(['replace', url]); },
    close() { pushed.push(['close']); layer.close('pop'); },
    top: () => 'storage',
  };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Date, parseInt, parseFloat, Error, RegExp, encodeURIComponent,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    setInterval: (fn, ms) => { intervals.set(intervals.size + 1, { fn, ms }); return intervals.size; },
    clearInterval: (id) => intervals.delete(id),
    AbortController: class { abort() {} },
    CustomEvent: class { constructor(type, init) { Object.assign(this, { type, ...init }); } },
    DOMParser: class { parseFromString() { const d = makeDocument(); d.body.appendChild(build(d, server.page)); return d; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', transitionDuration: '0s', transitionDelay: '0s', display: 'block', transform: 'none', opacity: '1' }),
    matchMedia: () => ({ matches: !opts.motion }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900, innerWidth: 1280,
    scrollBy() {},
    location: { pathname: '/watch', reload() { throw new Error('the page was reloaded'); } },
    fetch: (url, init) => {
      // Signed out since the page was loaded: what is asked for next is refused.
      if (opts.signedIn && opts.signedIn.yes) return server.request('GET', url).then((data) => ({ ok: true, status: 200, json: () => Promise.resolve(data) }));
      if (url !== '/watch') return Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) });
      const answer = { ok: !!server.page, status: 200, redirected: server.redirected, text: () => Promise.resolve('') };
      server.pages += 1;
      return server.holding === '/watch' ? new Promise((resolve) => server.held.push({ url, go: () => resolve(answer) })) : Promise.resolve(answer);
    },
    UIFeedback: { toast: (message, opts) => toasts.push({ message, undo: !!(opts && opts.action), opts }), error: (message) => toasts.push({ message, error: true }), confirm: () => Promise.resolve(confirmed), dismissConfirm() {} },
    addEventListener(type, fn) { if (type === 'load') ready.push(fn); },
  };
  ctx.window = ctx;
  const told = [];
  doc.dispatchEvent = (e) => { if (e.type !== 'mm:watch-kept') told.push(e.type); (doc.listeners[e.type] || []).forEach((fn) => fn(e)); };
  ctx.MM = opts.realApi ? {} : { api: {
    get: (url) => server.request('GET', url), post: (url, body) => server.request('POST', url, body),
    postForm: (url, body) => server.request('FORM', url, body), messageOf: (e, f) => (e && e.message) || f } };
  doc.readyState = 'interactive';
  vm.createContext(ctx);
  if (opts.realApi) run('core/api.js');
  if (opts.realConfirm) {
    ctx.navigator = { platform: 'Linux' };
    ['modal-a11y.js', 'ui-feedback.js'].forEach(run);
  }
  ['core/dom.js', 'watch/core.js'].forEach(run);
  ctx.MMWatch.address = address;
  const started = [];
  ctx.MMWatch.rows = { init: (n) => started.push(n.className) };
  ctx.MMWatch.expand = { row: (n) => started.push(n.getAttribute('data-row-id')) };
  ['watch/keep.js', 'watch/space.js'].forEach(run);
  function run(file) { vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx); }
  const before = server.calls.length;
  ready.forEach((fn) => fn());
  await settle();
  const one = (sel) => doc.querySelector(sel);
  const all = (sel) => doc.querySelectorAll(sel);
  const p = {
    doc, server, told, started, toasts, pushed, $, one, all, before, ctx,
    confirm(on) { confirmed = on; },
    layer: () => layer,
    async click(node) { click(doc, node); await settle(); },
    async fire(node, type, extra) { (node.listeners[type] || []).forEach((fn) => fn({ pointerType: 'mouse', ...extra })); await settle(); },
    async key(key) {
      const e = { key, target: doc.activeElement, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} };
      for (let a = doc.activeElement; a; a = a.parentNode) (a.listeners.keydown || []).forEach((fn) => fn(e));
      (doc.listeners.keydown || []).forEach((fn) => fn(e));
      await settle();
    },
    async run(ms) { timers.filter((t) => t.fn && t.ms === ms).forEach((t) => { const fn = t.fn; t.fn = null; fn(); }); await settle(); },
    second() { intervals.forEach((i) => { if (i.ms === 1000) i.fn(); }); },
    async let_go(url) { const mine = server.held.filter((h) => h.url.includes(url)); server.held = server.held.filter((h) => !mine.includes(h)); mine.forEach((h) => h.go()); await settle(); },
    async open(name = 'scheduled') { await p.click($('meter')); await p.click($('space-tab-' + name)); },
    async keep(id, days) {
      const trigger = all('#space-scheduled .watch-keep').find((b) => b.getAttribute('data-watch-keep') === id);
      trigger.focus();
      click(doc, trigger);
      one('#watch-keep-menu [data-days="' + days + '"]').focus();
      click(doc, one('#watch-keep-menu [data-days="' + days + '"]'));
      await settle();
    },
    async refresh() { doc.dispatchEvent({ type: 'mm:watch-admin', detail: { phase: 'done' } }); await settle(); },
    rows: () => all('#space-scheduled .space-item').filter((li) => !li.inert).map((li) => li.getAttribute('data-id')),
    cards: () => all('[data-row-id="leaving"] .watch-card').filter((c) => !c.classList.contains('is-kept')).map((c) => c.getAttribute('data-keep-id')),
    heads: () => all('.space-layer:not([inert]) .space-heavy__row b').map((b) => b.textContent),
    kind: () => (one('.space-layer:not([inert]) .space-cats') ? 'all' : one('.space-layer:not([inert]) .space-heavy') ? 'list' : 'words'),
    focused: () => doc.activeElement.getAttribute('aria-label') || doc.activeElement.id || doc.activeElement.className,
    meter: () => $('meter').textContent,
    text: (id) => $('space-' + id).textContent,
    last: () => toasts[toasts.length - 1],
    posts: () => server.calls.filter((c) => c[0] !== 'GET'),
    held: () => doc.body.children.filter((n) => n.inert).map((n) => n.id),
    seg: (k) => one('.space-seg[data-k="' + k + '"]'),
  };
  return p;
}

const scenarios = {
  async meter() {
    const p = await page();
    return { askedBeforeLoad: p.before, text: p.meter(), label: p.$('meter').getAttribute('aria-label'), busy: p.$('meter').hasAttribute('aria-busy'),
      phone: p.$('phone-meter').textContent, link: p.one('.space-row-link').textContent, asked: p.server.calls.map((c) => c[1]) };
  },
  async meterStates() {
    const of = async (setup) => {
      const p = await page(undefined, setup);
      return [p.$('meter').className.replace('space-meter', '').trim(), p.$('meter').getAttribute('aria-label')];
    };
    const disk = (over) => ({ ...storage().disks[0], ...over });
    return {
      over: await of((s) => { s.storage = storage({ disks: [disk({ over_threshold: true, threshold_pct: 80 })] }); }),
      full: await of((s) => { s.storage = storage({ used_bytes: 8000 * GB }); }),
      oneOfTwo: await of((s) => { s.storage = storage({ disks: [disk({ path: '/a' }), disk({ path: '/b', over_threshold: true })] }); }),
      twoOfTwo: await of((s) => { s.storage = storage({ disks: [disk({ path: '/a', over_threshold: true }), disk({ path: '/b', over_threshold: true })] }); }),
      fullAndOver: await of((s) => { s.storage = storage({ used_bytes: 8000 * GB, disks: [disk({ path: '/a' }), disk({ path: '/b', over_threshold: true })] }); }),
      unread: await of((s) => { s.storage = storage({ readable: false }); }),
      missing: await of((s) => { s.extra = { unavailable_roots: [{ path: '/media/tv', waiting: 4 }] }; }),
      off: await of((s) => { s.extra = { deletions_off: true }; }),
      failed: await of((s) => { s.fail = { stats: 'down' }; }),
    };
  },
  async openAndClose() {
    const p = await page();
    p.$('meter').focus();
    await p.click(p.$('meter'));
    const open = { hidden: p.$('space').hidden, focused: p.focused(), held: p.held(), menuFree: !p.$('watch-keep-menu').closest('[inert]'), modal: p.one('.space__panel').hasAttribute('aria-modal'), expanded: p.$('meter').getAttribute('aria-expanded'), pushed: p.pushed.slice() };
    await p.key('Escape');
    (p.doc.listeners.click || []).forEach((fn) => fn({ target: p.$('meter'), detail: 1 }));
    const byPointer = p.focused();
    await p.key('Escape');
    return { byPointer, open, closed: { inert: p.$('space').inert, focused: p.focused(), held: p.held(), expanded: p.$('meter').getAttribute('aria-expanded') } };
  },
  async otherOpensOver() {
    const p = await page();
    await p.click(p.$('meter'));
    p.layer().close('push');
    return { focused: p.focused(), held: p.held(), under: p.$('space').classList.contains('is-under') };
  },
  async address() {
    const p = await page();
    const read = (hash) => p.layer().read({ hash });
    const values = ['storage', 'storage-scheduled', 'storage-deleted', 'storage-other', 'title=movie-1', ''].map(read);
    p.layer().open('deleted', 'start');
    await settle();
    const tabs = p.focused();
    p.layer().close('replace'); // how a shared link's own Close comes
    return { closed: p.focused(), values, on: p.all('.space-pane').filter((x) => x.classList.contains('is-on')).map((x) => x.id), tabs };
  },
  async tabs() {
    const p = await page();
    await p.click(p.$('meter'));
    await p.key('ArrowRight');
    const one = [p.focused(), p.pushed[p.pushed.length - 1]];
    await p.key('End');
    return { one, end: p.$('space-tab-deleted').getAttribute('aria-selected'), reach: p.all('.space-pane').map((x) => x.inert) };
  },
  async escOrder() {
    const p = await page();
    await p.click(p.$('meter'));
    await p.click(p.seg('movies'));
    const pinned = { kind: p.kind(), rows: p.heads(), pressed: p.seg('movies').getAttribute('aria-pressed') };
    await p.key('Escape');
    const once = { kind: p.kind(), focused: p.focused(), open: !p.$('space').inert };
    await p.key('Escape');
    return { pinned, once, twice: { open: !p.$('space').inert } };
  },
  async hover() {
    const p = await page();
    await p.click(p.$('meter'));
    const body = p.one('.space-body');
    const trail = [];
    const note = () => trail.push(p.kind() === 'all' ? 'all' : p.heads()[0]);
    await p.fire(p.seg('movies'), 'pointerenter'); note();
    await p.fire(p.seg('movies'), 'pointerleave');
    await p.run(110); note(); // only passed over
    await p.fire(p.seg('movies'), 'pointerenter'); await p.run(110); note();
    await p.fire(p.seg('tv'), 'pointerenter'); await p.run(70); note();
    await p.fire(body, 'pointerleave'); note();
    await p.fire(body, 'pointerenter'); await p.run(260); note(); // came back in time
    await p.fire(body, 'pointerleave'); await p.run(260); note();
    return trail;
  },
  async pinned() {
    const p = await page();
    await p.click(p.$('meter'));
    await p.click(p.seg('movies'));
    await p.fire(p.seg('tv'), 'pointerenter'); await p.run(70); await p.run(110);
    const held = p.heads()[0];
    await p.fire(p.one('.space-body'), 'pointerleave'); await p.run(260);
    const left = p.kind();
    await p.click(p.seg('movies'));
    return { held, left, again: p.kind() };
  },
  async lateList() {
    const p = await page();
    p.server.holding = '/api/library';
    await p.click(p.$('meter'));
    await p.click(p.seg('movies'));
    const waiting = p.one('[aria-busy="true"].space-heavy') !== null;
    const old = p.server.held;
    p.server.held = [];
    p.server.library.movie = [title('Newest', 99 * GB)];
    await p.keep('1', '7').catch(() => {});
    const fresh = p.server.held;
    p.server.held = [];
    fresh.forEach((h) => h.go()); await settle();
    const now = p.heads();
    old.forEach((h) => h.go()); await settle();
    return { waiting, now, after: p.heads() };
  },
  async seeAllMeanwhile() {
    const all = (p) => { const a = p.one('.space-layer:not([inert]) .space-all'); return a && [a.textContent, a.href]; };
    const of = async (setup) => {
      const p = await page(undefined, setup);
      await p.click(p.$('meter'));
      const out = [];
      for (const k of ['movies', 'tv']) { await p.click(p.seg(k)); out.push(p.one('.space-layer:not([inert]) [role="alert"]') !== null, all(p)); }
      return out;
    };
    const few = (s) => { s.storage = storage({ counts: { movies: 312, tv: 8, anime: 0 } }); };
    return { loading: await of((s) => { few(s); s.holding = '/api/library'; }), failed: await of((s) => { few(s); s.fail = { '/api/library': 'down' }; }) };
  },
  async listFails() {
    const p = await page(undefined, (s) => { s.fail = { 'type=movie': 'down' }; });
    await p.click(p.$('meter'));
    await p.click(p.seg('movies'));
    const said = p.one('.space-layer:not([inert]) [role="alert"] b').textContent;
    p.server.fail = {};
    await p.click(p.one('.space-layer:not([inert]) .space-say .btn'));
    return { said, rows: p.heads(), focused: p.focused() };
  },
  async segments() {
    const p = await page(undefined, (s) => { s.storage = storage({ movies_bytes: 9000 * GB, other_bytes: 0 }); });
    await p.click(p.$('meter'));
    const share = (k) => Math.round(p.seg(k).style['--v']);
    const rest = p.one('.space-bar__rest');
    return { names: p.all('.space-seg').map((b) => b.getAttribute('data-k')), used: share('movies') + share('tv'), free: share('free'), rest: Math.round(rest.style['--v']),
      tick: p.one('.space-tick').style['--at'], heavy: p.one('.space-cat[data-k="movies"]').textContent };
  },
  async disks() {
    const one = storage().disks[0];
    const p = await page(undefined, (s) => {
      s.storage = storage({ disks: [{ ...one, path: '/media/movies', over_threshold: true }, { ...one, path: '/media/tv' }, { ...one, path: '/media/<b>', readable: false }] });
    });
    await p.click(p.$('meter'));
    const mixed = await page(undefined, (s) => { s.storage = storage({ disks: [{ ...one, threshold_shared: false }] }); });
    await mixed.click(mixed.$('meter'));
    return { list: p.all('.space-disk').map((d) => d.textContent), tick: p.one('.space-tick') !== null, line: p.one('.space-fig__line').textContent, mixedTick: mixed.one('.space-tick') !== null };
  },
  async unread() {
    const p = await page(undefined, (s) => { s.storage = storage({ readable: false }); });
    await p.click(p.$('meter'));
    const was = { meter: [p.meter(), p.one('#meter .space-meter__word').textContent], text: p.text('storage'), segs: p.all('.space-seg').map((b) => b.getAttribute('data-k')) };
    p.server.storage = storage();
    const again = p.one('#space-storage .space-say .btn');
    again.focus();
    await p.click(again);
    return { was, free: p.one('.space-fig__n').textContent, focused: p.focused() };
  },
  async panelFails() {
    const p = await page(undefined, (s) => { s.fail = { '/api/dashboard': 'down' }; });
    await p.click(p.$('meter'));
    const was = { texts: ['storage', 'scheduled', 'deleted'].map((k) => p.one('#space-' + k + ' [role="alert"] b').textContent), foot: p.one('.space__foot').hidden };
    p.server.fail = {};
    const again = p.one('#space-storage .btn');
    again.focus();
    await p.click(again);
    return { was, rows: p.rows(), foot: p.one('.space__foot').hidden, focused: p.focused() };
  },
  async notices() {
    const p = await page([], (s) => { s.extra = { unavailable_roots: [{ path: '/m/<b>x</b>', waiting: 1 }, { path: '/media/tv', waiting: 3 }], deletions_off: true }; });
    await p.click(p.$('meter'));
    return ['storage', 'scheduled'].map((k) => ({ roots: p.all('#space-' + k + ' [role="alert"] li').map((li) => li.textContent), off: p.one('#space-' + k + ' [role="status"]').textContent }));
  },
  async wouldFree() {
    const of = async (extra) => {
      const p = await page(undefined, (s) => { s.extra = extra; });
      await p.click(p.$('meter'));
      const after = p.one('.space-after').textContent;
      const notices = p.all('#space-storage .space-notice').map((n) => n.textContent);
      await p.click(p.seg('free'));
      return { after, said: p.one('.space-layer:not([inert]) .space-say b').textContent, notices };
    };
    return { on: await of({}), off: await of({ deletions_off: true }), dry: await of({ dry_run: true }) };
  },
  // Turned on while nothing is scheduled, and the pane is on show.
  async dryRunOnEmpty() {
    const p = await page([]);
    await p.open();
    p.server.extra = { dry_run: true };
    await p.refresh();
    return p.all('#space-scheduled .space-notice').map((n) => n.textContent);
  },
  async noSize() {
    const p = await page([row('1', { file_size: '0 B', file_size_bytes: 0 })], (s) => {
      s.deleted = [{ id: 1, title: 'Old', deleted_ago: 'yesterday', reclaimed: '0 B', poster_url: '', redownload: null }];
    });
    await p.click(p.$('meter'));
    const out = { link: p.one('.space-row-link').textContent, linkName: p.one('.space-row-link').getAttribute('aria-label'), after: p.one('.space-after').textContent };
    await p.click(p.seg('free'));
    out.said = p.one('.space-layer:not([inert]) .space-say').textContent;
    await p.click(p.$('space-tab-scheduled'));
    return { ...out, head: p.one('#space-scheduled .space-list-head small').textContent, row: p.one('#space-scheduled .space-item').textContent, deleted: p.one('#space-deleted .space-item').textContent };
  },
  async someSizesNotKnown() {
    const of = async (...rows) => {
      const p = await page(rows);
      const closed = p.one('.space-row-link').textContent;
      await p.click(p.$('meter'));
      const out = { closed, link: p.one('.space-row-link').textContent, after: p.one('.space-after').textContent };
      await p.click(p.seg('free'));
      out.said = p.one('.space-layer:not([inert]) .space-say').textContent;
      await p.click(p.$('space-tab-scheduled'));
      return { ...out, head: p.one('#space-scheduled .space-list-head small').textContent };
    };
    const none = { file_size: '0 B', file_size_bytes: 0 };
    return { leaving: await of(row('1'), row('2', none)), overdue: await of(row('1'), row('2', { ...none, state: 'overdue' })) };
  },
  async scaled() {
    const of = async (over) => {
      const p = await page(undefined, (s) => { s.storage = storage(over); });
      await p.click(p.$('meter'));
      return { rows: p.all('.space-cat__size small').map((n) => n.textContent), names: p.all('.space-seg').map((b) => b.getAttribute('aria-label')) };
    };
    return { fits: await of({}), scaled: await of({ movies_bytes: 6000 * GB, other_bytes: 0 }) };
  },
  async noPlex() {
    const p = await page([], (s) => { s.extra = { plex_configured: false, show_storage: false }; s.storage = storage({ readable: false, counts: { movies: 0, tv: 0, anime: 0 } }); });
    await p.click(p.$('meter'));
    return { storage: p.text('storage'), bar: p.one('.space-bar') !== null, scheduled: p.text('scheduled'), link: p.one('#space-scheduled a').href, foot: p.one('.space__foot').hidden };
  },
  async scheduled() {
    const p = await page([row('1', { state: 'overdue', countdown: 'Deletes today', days_left: 0 }), row('2', { state: 'deleting' }),
      row('3', { media_type: 'tv_season', title: 'Show', season_number: 2, days_left: 1, countdown: 'Deletes tomorrow' }), row('4', { title: '<b>"Hi"</b>', ref: null })]);
    await p.open();
    const li = p.all('#space-scheduled .space-item');
    return { head: p.one('#space-scheduled .space-list-head small').textContent, tab: p.one('#space-tab-scheduled .space-tab__count').textContent, meter: p.meter(),
      rows: li.map((n) => n.textContent), late: li.map((n) => n.querySelector('.watch-keep').hasAttribute('data-keep-late')),
      links: li.map((n) => (n.querySelector('a') ? n.querySelector('a').getAttribute('data-title-ref') : null)), name: li[2].querySelector('.watch-keep').getAttribute('aria-label') };
  },
  async overdue() {
    const of = async (extra) => {
      const p = await page([row('1', { state: 'overdue', waiting_folder: '/media/tv' }), row('2', { state: 'overdue', waiting_folder: null })], (s) => { s.extra = extra; });
      await p.open();
      return p.all('#space-scheduled .space-item__when').map((n) => n.textContent);
    };
    const missing = { unavailable_roots: [{ path: '/media/tv', waiting: 1 }] };
    return { missing: await of(missing), dry: await of({ ...missing, dry_run: true }), off: await of({ ...missing, dry_run: true, deletions_off: true }) };
  },
  async keepAndUndo() {
    const p = await page();
    await p.open();
    await p.keep('2', '30');
    const kept = { rows: p.rows(), focused: p.focused(), posts: p.posts(), toast: p.last(), meter: p.meter(), link: p.one('.space-row-link').textContent,
      card: p.all('[data-row-id="leaving"] .watch-card').map((c) => c.getAttribute('data-keep-id')) };
    const fromPanel = kept.toast.opts.dialog === p.one('.space__panel');
    p.last().opts.action.onClick();
    await settle();
    return { fromPanel, kept: { ...kept, toast: { message: kept.toast.message, undo: kept.toast.undo } }, undone: { rows: p.rows(), focused: p.focused(), post: p.posts()[1], meter: p.meter() } };
  },
  async keepLast() {
    const p = await page([row('1')]);
    await p.open();
    await p.keep('1', '7');
    return { rows: p.rows(), focused: p.doc.activeElement.className, text: p.text('scheduled') };
  },
  async keepForever() {
    const p = await page();
    await p.open();
    await p.keep('1', 'forever');
    return { rows: p.rows(), posts: p.posts(), toast: p.last().message, undo: p.last().undo };
  },
  async keepLate() {
    const p = await page([row('1', { state: 'overdue' }), row('2')], (s) => { s.keepAnswer = { ok: true, deletion_running: true, message: 'Already being deleted.' }; });
    await p.open();
    await p.keep('1', '30');
    return { posts: p.posts(), toast: p.last().message, undo: p.last().undo };
  },
  async keepFails() {
    const p = await page(undefined, (s) => { s.fail = { '/api/watch/keep': 'Not now' }; });
    await p.open();
    await p.keep('2', '7');
    return { rows: p.rows(), focused: p.focused(), toast: p.last().message, busy: p.all('#space-scheduled [aria-busy="true"]').length };
  },
  // The kept row is pinned and fades while the rows under it already slide; the next Keep takes the
  // focus without a jump and is then brought into view.
  async keepMotion() {
    const p = await page(undefined, undefined, { motion: true });
    await p.open();
    const list = p.one('#space-scheduled .space-list');
    const place = (li) => list.children.filter((n) => !n.classList.contains('is-gone')).indexOf(li) * 84;
    Node.prototype.getBoundingClientRect = function () { const top = this.parentNode === list ? place(this) : 0; return { top, bottom: top + 84, left: 0, right: 20, width: 20, height: 84 }; };
    const anims = [];
    Node.prototype.animate = function (frames) {
      const a = { node: this, frames };
      anims.push(a);
      return a;
    };
    const seen = [];
    const focus = Node.prototype.focus;
    Node.prototype.focus = function (o) { if (this.closest('.space-item')) seen.push(['focus', o && o.preventScroll]); focus.call(this); };
    Node.prototype.scrollIntoView = function (o) { seen.push(['reveal', o.block, o.behavior]); };
    const trigger = p.all('#space-scheduled .watch-keep')[0];
    click(p.doc, trigger);
    click(p.doc, p.one('#watch-keep-menu [data-days="7"]'));
    await settle();
    delete Node.prototype.animate;
    const rows = p.all('#space-scheduled .space-item');
    const of = (li) => anims.filter((a) => a.node === li).map((a) => a.frames[0].transform || a.frames[a.frames.length - 1].opacity);
    const gone = rows.find((li) => li.classList.contains('is-gone'));
    return { gone: [gone.getAttribute('data-id'), gone.style.top, of(gone)], slid: rows.filter((li) => li !== gone).map(of), seen: seen.slice(-2), focused: p.focused() };
  },
  async expired() {
    const p = await page(undefined, undefined, { realApi: true });
    return { to: p.ctx.location.href, meter: p.$('meter').getAttribute('aria-busy') };
  },
  async keepFromTheRow() {
    const p = await page();
    await p.open();
    await p.key('Escape');
    await p.click(p.$('card-keep-1'));
    await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    const meter = p.meter();
    const fromPanel = !!p.last().opts.dialog;
    await p.open();
    return { fromPanel, meter, rows: p.rows() };
  },
  // The refresh after a keep is still out when the keep is undone.
  async undoRace() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/dashboard';
    await p.keep('1', '7');
    const meter = p.meter();
    const refresh = p.server.held;
    p.server.held = [];
    p.server.holding = '/api/watch/unkeep';
    p.last().opts.action.onClick();
    await settle();
    refresh.forEach((h) => h.go());
    await settle();
    const during = p.rows();
    await p.let_go('/api/watch/unkeep');
    return { meter, during, after: p.rows() };
  },
  async keepInAGrid() {
    const p = await page();
    const took = [];
    p.ctx.MMWatch.grid = { take: (card, control) => { took.push([card.getAttribute('data-keep-id'), control]); card.remove(); return { undo: () => null, focus: p.$('card-keep-2') }; } };
    await p.click(p.$('card-keep-1'));
    await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    const inGrid = { took, focused: p.focused() };
    p.ctx.MMWatch.grid.take = () => null;
    await p.click(p.$('card-keep-2'));
    await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    return { inGrid, inRow: p.cards().length };
  },
  // Leaving soon's grid is open: its kept card still fades, as watch/grid.js leaves it, when the refresh lands.
  async gridFades() {
    const p = await page(undefined, undefined, { motion: true });
    await p.open();
    await p.key('Escape');
    Node.prototype.animate = function () { return { cancel() {} }; };
    const host = p.$('main').appendChild(build(p.doc, ['div', { 'data-watch-leaving-row': '' }, ['div', { class: 'watch-grid' }, ...['1', '2'].map(card)]]));
    const grid = host.children[0];
    const first = grid.children[0];
    p.ctx.MMWatch.grid = { take: (c, control) => {
      if (!grid.contains(c)) return null;
      const next = c.nextElementSibling;
      host.appendChild(c);
      c.classList.add('is-gone');
      c.inert = true;
      return { focus: null, undo: () => { c.classList.remove('is-gone'); c.inert = false; grid.insertBefore(c, next); return c.querySelector(control); } };
    } };
    const keep = async () => {
      await p.click(first.querySelector('.watch-keep'));
      await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    };
    await keep();
    p.last().opts.action.onClick();
    await settle();
    const undone = { in: first.parentNode === grid, card: first.className, inert: first.inert };
    await keep();
    return { undone, again: grid.contains(first) };
  },
  async secondPress() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/watch/keep';
    await p.keep('1', '7');
    await p.keep('1', '7');
    return p.posts().length;
  },
  async scanDuringFailedKeep() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/watch/keep';
    p.server.fail = { '/api/watch/keep': 'Not now' };
    await p.keep('1', '7');
    await p.click(p.$('space-scan'));
    p.server.rows = p.server.rows.concat([row('9')]);
    await p.run(3000);
    const during = p.rows();
    await p.let_go('/api/watch/keep');
    return { during, after: p.rows() };
  },
  async olderRefresh() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/dashboard/scheduled';
    (p.doc.listeners['mm:watch-admin'] || []).forEach((fn) => fn({ detail: { phase: 'done' } }));
    await settle();
    const older = p.server.held;
    p.server.held = [];
    p.server.holding = null;
    await p.keep('1', '7');
    const kept = p.rows();
    older.forEach((h) => h.go());
    await settle();
    return { kept, after: p.rows() };
  },
  async clear() {
    const p = await page();
    await p.open();
    const b = p.one('#space-scheduled .btn--danger');
    p.confirm(false);
    b.focus();
    await p.click(b);
    const declined = { posts: p.posts().length, rows: p.rows() };
    p.confirm(true);
    await p.click(b);
    return { declined, posts: p.posts(), rows: p.rows(), toast: p.last().message, focused: p.doc.activeElement.className, meter: p.meter() };
  },
  // The page behind follows what is scheduled.
  async pageAfterClear() {
    const p = await page(['1', '2'].map((id) => row(id)));
    await p.open();
    await p.click(p.one('#space-scheduled .btn--danger'));
    return { cards: p.cards(), row: p.one('[data-row-id="leaving"]') !== null, badges: p.all('.watch-badge--leaving').length, told: p.told, asked: p.server.pages, inPanel: !!p.doc.activeElement.closest('#space') };
  },
  async pageAfterScan() {
    const p = await page();
    await p.open();
    const before = p.all('.watch-card');
    p.server.rows = ['9', '1', '3', '2'].map((id) => row(id));
    p.server.page = ['div', { class: 'watch-rows' }, leavingSection('9', '1', '3', '2'), recentSection()];
    await p.click(p.$('space-scan'));
    await p.run(3000);
    return { cards: p.cards(), same: before.every((c) => c.isConnected), told: p.told, asked: p.server.pages, link: p.all('.space-row-link').length };
  },
  async rowUndone() {
    const p = await page(['1', '2'].map((id) => row(id)), undefined, { motion: true });
    const anims = [];
    Node.prototype.animate = function () { const a = { node: this, cancel() {} }; anims.push(a); return a; };
    await p.click(p.$('card-keep-1'));
    await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    anims.splice(0).forEach((a) => a.onfinish && a.onfinish());
    p.last().opts.action.onClick();
    const before = p.started.length;
    anims.filter((a) => a.node.classList.contains('watch-card')).forEach((a) => a.onfinish());
    await settle();
    return { before, after: p.started };
  },
  // What a scan adds is given to the row's grid, once all of it is in the row.
  async gridAfterScan() {
    const p = await page();
    await p.open();
    const given = [];
    p.ctx.MMWatch.grid = { take: () => null, bring: (c) => { given.push([c.getAttribute('data-keep-id'), p.cards().join()]); return null; } };
    const scan = async (...ids) => {
      p.server.rows = ids.map((id) => row(id));
      p.server.page = ['div', { class: 'watch-rows' }, leavingSection(...ids), recentSection()];
      await p.click(p.$('space-scan'));
      await p.run(3000);
      await p.run(2000);
      return given.splice(0);
    };
    const added = await scan('9', '1', '8', '2');
    await p.click(p.one('#space-scheduled .btn--danger'));
    return { added: added.sort(), row: await scan('7') };
  },
  // The page asked for is another: the session ended, or it moved.
  async pageRedirected() {
    const p = await page(['1', '2'].map((id) => row(id)));
    await p.open();
    p.server.redirected = true;
    p.server.rows = ['1', '2', '9'].map((id) => row(id));
    p.server.page = ['div', { class: 'watch-rows' }, leavingSection('1', '2', '9')];
    await p.refresh();
    return { asked: p.server.pages, cards: p.cards(), rows: p.rows() };
  },
  // See all is fitted again once the cards that came have their width.
  async rowGrown() {
    const p = await page(undefined, undefined, { motion: true });
    await p.open();
    p.server.rows = ['1', '2', '9'].map((id) => row(id));
    p.server.page = leavingSection('1', '2', '9');
    const anims = [];
    Node.prototype.animate = function () { const a = { node: this }; anims.push(a); return a; };
    await p.click(p.$('space-scan'));
    await p.run(3000);
    delete Node.prototype.animate;
    const before = p.started.length;
    anims.filter((a) => a.node.classList.contains('watch-card')).forEach((a) => a.onfinish());
    return { before, after: p.started };
  },
  async rowAfterScan() {
    const p = await page(['1', '2'].map((id) => row(id)));
    await p.open();
    await p.click(p.one('#space-scheduled .btn--danger'));
    const gone = p.one('[data-row-id="leaving"]') === null;
    p.server.rows = [row('9')];
    p.server.page = ['div', { class: 'watch-rows' }, leavingSection('9'), recentSection()];
    await p.click(p.$('space-scan'));
    await p.run(3000);
    return { gone, rows: p.all('.watch-rows .watch-row').map((s) => s.getAttribute('data-row-id')), cards: p.cards(), started: p.started, link: p.one('[data-row-id="leaving"] .space-row-link').textContent };
  },
  // Nothing new: the page is not asked for. An answer overtaken by a Keep is dropped.
  async pageAsked() {
    const p = await page(['1', '2'].map((id) => row(id)));
    await p.open();
    const none = p.server.pages;
    p.server.page = ['div', { class: 'watch-rows' }, leavingSection('1', '2', '9')];
    p.server.rows = ['1', '2', '9'].map((id) => row(id));
    p.server.holding = '/watch';
    await p.click(p.$('space-scan'));
    await p.run(3000);
    p.server.holding = '/api/watch/keep';
    const held = p.server.held;
    p.server.held = [];
    await p.keep('9', '7');
    held.forEach((h) => h.go());
    await settle();
    return { none, cards: p.cards() };
  },
  // Two cards go, the first from under the focus, the panel closed.
  async pageFocus() {
    const p = await page();
    await p.open();
    await p.key('Escape');
    p.one('[data-row-id="leaving"] .watch-row__scroll').appendChild(build(p.doc, card('3')));
    p.$('card-keep-1').focus();
    p.server.rows = p.server.rows.slice(2);
    p.doc.dispatchEvent({ type: 'mm:watch-admin', detail: { phase: 'done' } });
    await settle();
    return { cards: p.cards(), focused: p.focused() };
  },
  // The list is told once a keep is in, and not of one that failed.
  async listTold() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/watch/keep';
    await p.keep('1', '7');
    const during = p.told.length;
    await p.let_go('/api/watch/keep');
    p.server.holding = null;
    const kept = p.told.slice();
    p.server.fail = { '/api/watch/keep': 'Not now' };
    await p.keep('2', '7');
    const failed = p.told.length;
    p.server.fail = { '/api/watch/keep': 'Not now' };
    // one saved, one failed after it
    p.server.fail = {};
    p.server.holding = '/api/watch/keep';
    await p.keep('2', '7');
    const saved = p.server.held;
    p.server.held = [];
    p.server.fail = { '/api/watch/keep': 'Not now' };
    await p.keep('3', '7');
    saved.forEach((h) => h.go());
    await settle();
    await p.let_go('/api/watch/keep');
    return { during, kept, failed, mixed: p.told.length };
  },
  async clearBusy() {
    const p = await page();
    await p.open();
    const b = p.one('#space-scheduled .btn--danger');
    p.server.holding = 'clear-scheduled';
    b.focus();
    await p.click(b);
    const busy = { disabled: b.disabled === true, aria: b.getAttribute('aria-disabled'), said: b.getAttribute('aria-busy'), focused: p.doc.activeElement === b };
    await p.click(b);
    const posts = p.posts().length;
    await p.let_go('clear-scheduled');
    return { busy, posts, inPanel: !!p.doc.activeElement.closest('#space') };
  },
  async scanTwice() {
    const p = await page();
    await p.open('storage');
    p.server.scan = { running: true };
    await p.click(p.$('space-scan'));
    await p.click(p.$('space-scan'));
    await p.run(3000);
    await p.click(p.$('space-scan'));
    return { posts: p.posts().length, look: p.$('space-scan').classList.contains('is-busy'), aria: p.$('space-scan').getAttribute('aria-disabled') };
  },
  // What has the keyboard before the panel does.
  async escapeElsewhere() {
    const p = await page();
    await p.open();
    const confirm = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-confirm-overlay' }]));
    await p.key('Escape');
    const underConfirm = !p.$('space').inert;
    confirm.remove();
    const trigger = p.all('#space-scheduled .watch-keep')[0];
    await p.click(trigger);
    await p.key('Escape');
    const menu = { open: !p.$('space').inert, expanded: trigger.getAttribute('aria-expanded') };
    await p.key('Escape');
    return { underConfirm, menu, then: !p.$('space').inert };
  },
  // A refresh changes what the rows, the head and the notices say where they are.
  async refreshed() {
    const p = await page(['1', '2'].map((id) => row(id)));
    await p.open();
    await p.refresh();
    const li = p.all('#space-scheduled .space-item');
    const keep = li[0].querySelector('.watch-keep');
    keep.focus();
    const look = () => ({ rows: p.all('#space-scheduled .space-item__when').map((n) => n.textContent), late: li.map((n) => n.querySelector('.watch-keep').hasAttribute('data-keep-late')),
      head: p.one('#space-scheduled .space-list-head small').textContent, notices: p.all('#space-scheduled .space-notice').map((n) => n.textContent), storage: p.all('#space-storage .space-notice').length });
    const was = look();
    p.server.rows = [row('1', { state: 'overdue', waiting_folder: '/media/tv' }), row('2', { countdown: 'Deletes tomorrow', days_left: 1 })];
    p.server.extra = { unavailable_roots: [{ path: '/media/tv', waiting: 2 }], deletions_off: false };
    await p.refresh();
    const now = look();
    p.server.rows = ['1', '2'].map((id) => row(id));
    p.server.extra = { unavailable_roots: [{ path: '/media/tv', waiting: 1 }], deletions_off: true };
    await p.refresh();
    const later = look();
    p.server.extra = {};
    await p.refresh();
    return { was, now, later, gone: look().notices, same: p.all('#space-scheduled .space-item').every((n, i) => n === li[i]), focused: p.doc.activeElement === keep };
  },
  // Two refreshes, the newer one answered first.
  async refreshOrder() {
    const p = await page();
    await p.open();
    p.server.holding = '/api/dashboard';
    await p.refresh();
    const older = p.server.held;
    p.server.held = [];
    p.server.rows = [row('9')];
    await p.refresh();
    const newer = p.server.held;
    p.server.held = [];
    newer.forEach((h) => h.go()); await settle();
    const now = p.rows();
    older.forEach((h) => h.go()); await settle();
    return { now, after: p.rows(), meter: p.meter() };
  },
  // Before the refresh that follows a keep is in.
  async keepLateCount() {
    const p = await page([row('1', { state: 'overdue' }), row('2')]);
    await p.open();
    p.server.holding = '/api/dashboard';
    await p.keep('1', '30');
    return { rows: p.rows(), meter: p.meter(), tab: p.one('#space-tab-scheduled .space-tab__count').textContent };
  },
  async redoneStays() {
    const p = await page(undefined, (s) => {
      s.deleted = [{ id: 1, title: 'Dune', poster_url: '', deleted_ago: '2 days ago', reclaimed: '1.0 GB', redownload: { title: 'Dune', tmdb_id: 1 } }];
    });
    await p.open('deleted');
    const b = p.one('.space-redo');
    p.server.holding = 'redownload';
    b.focus();
    await p.click(b);
    const redrawn = async () => {
      p.server.deleted = p.server.deleted.concat([{ id: 2 + p.server.deleted.length, title: 'New', poster_url: '', deleted_ago: 'today', reclaimed: '1.0 GB', redownload: null }]);
      await p.refresh();
      const now = p.one('.space-redo');
      return [now.textContent, now.getAttribute('aria-disabled'), p.doc.activeElement === now];
    };
    const adding = await redrawn();
    await p.let_go('redownload');
    const queued = p.one('.space-redo').textContent;
    return { drawnAgain: p.one('.space-redo') !== b, adding, queued, again: await redrawn() };
  },
  // Pulled down by the handle: far, not far, fast, up, and cancelled.
  async drag() {
    const of = async (by, ms, end = 'pointerup') => {
      const p = await page();
      await p.click(p.$('meter'));
      const grab = p.one('.space__grab');
      grab.setPointerCapture = () => {};
      await p.fire(grab, 'pointerdown', { clientY: 100, timeStamp: 0, pointerId: 1 });
      await p.fire(grab, 'pointermove', { clientY: 100 + by });
      const follows = p.one('.space__panel').style.transform;
      await p.fire(grab, end, { type: end, timeStamp: ms });
      return [follows, p.$('space').inert, p.one('.space__panel').style.transform, p.one('.space__panel').classList.contains('is-dragged')];
    };
    return { far: await of(130, 1000), near: await of(60, 1000), fast: await of(60, 50), twitch: await of(20, 5), up: await of(-200, 100), cancelled: await of(300, 100, 'pointercancel') };
  },
  // Back to the panel from what opened over it.
  async backFocus() {
    const p = await page();
    await p.open();
    p.layer().close('push');
    p.doc.activeElement = p.doc.body;
    p.layer().open('scheduled', 'pop');
    await p.run(0);
    const lost = p.focused();
    p.layer().close('push');
    p.layer().open('scheduled', 'pop');
    p.$('space-tab-scheduled').focus(); // as address.js puts it back
    await p.run(0);
    return { lost, kept: p.focused() };
  },
  async segmentFocus() {
    const p = await page();
    await p.click(p.$('meter'));
    p.seg('movies').focus();
    await settle();
    const rows = p.heads();
    await p.key('ArrowRight');
    return { rows, next: p.heads(), focused: p.focused(), pressed: p.seg('tv').getAttribute('aria-pressed') };
  },
  // With reduced motion the next Keep is brought into view at once; a refresh that lands then leaves it be.
  async keepRevealed(motion = false) {
    const p = await page(undefined, undefined, { motion });
    await p.open();
    if (motion) Node.prototype.animate = function () { return { cancel() {} }; };
    const seen = [];
    Node.prototype.scrollIntoView = function (o) { seen.push([this.getAttribute('data-watch-keep'), o.behavior]); };
    p.server.holding = '/api/dashboard';
    await p.keep('1', '7');
    const next = p.doc.activeElement;
    p.server.rows = p.server.rows.concat([row('9')]);
    p.server.holding = null;
    p.server.held.splice(0).forEach((h) => h.go());
    await settle();
    await p.refresh();
    return { seen, rows: p.rows(), same: p.doc.activeElement === next && next.isConnected, focused: p.focused() };
  },
  // The same while the rows still slide.
  keepRevealedMoving: () => scenarios.keepRevealed(true),
  // A refresh takes the row of the focused button away while the rows still slide.
  async focusRevealed() {
    const p = await page(['1', '2', '3', '4'].map((id) => row(id)), undefined, { motion: true });
    await p.open();
    Node.prototype.animate = function () { return { cancel() {} }; };
    p.server.holding = '/api/dashboard';
    await p.keep('1', '7');
    const seen = [];
    Node.prototype.scrollIntoView = function (o) { seen.push([this.getAttribute('data-watch-keep'), o.block, o.behavior]); };
    p.server.rows = p.server.rows.filter((r) => r.media_item_id !== '2');
    p.server.holding = null;
    p.server.held.splice(0).forEach((h) => h.go());
    await settle();
    await p.refresh();
    return { seen, focused: p.focused() };
  },
  // The focus of a failed keep: from the menu, and moved on meanwhile.
  async failedFocus() {
    const p = await page(undefined, (s) => { s.fail = { '/api/watch/keep': 'Not now' }; });
    await p.open();
    await p.keep('1', '7');
    const fromMenu = p.focused();
    p.server.holding = '/api/watch/keep';
    await p.keep('2', '90');
    p.$('space-tab-scheduled').focus();
    await p.let_go('/api/watch/keep');
    return { fromMenu, moved: p.doc.activeElement.id, menu: p.$('watch-keep-menu').classList.contains('is-open') };
  },
  async healthy() {
    const p = await page([]);
    await p.open();
    return { text: p.text('scheduled'), button: p.one('#space-scheduled .btn--danger') !== null };
  },
  // The session ended while the page was open.
  async expiredActions() {
    const of = async (press) => {
      const signedIn = { yes: true };
      const p = await page(undefined, undefined, { realApi: true, signedIn });
      await p.open();
      signedIn.yes = false;
      await press(p);
      return { to: p.ctx.location.href, toasts: p.toasts.length, rows: p.rows().length };
    };
    return { scan: await of((p) => p.click(p.$('space-scan'))), clear: await of((p) => p.click(p.one('#space-scheduled .btn--danger'))),
      poll: await of(async (p) => { p.ctx.MM.api.post = () => Promise.resolve({ status: 'started' }); await p.click(p.$('space-scan')); await p.run(3000); }) };
  },
  async clearFails() {
    const p = await page(undefined, (s) => { s.fail = { 'clear-scheduled': 'Slow down' }; });
    await p.open();
    const b = p.one('#space-scheduled .btn--danger');
    b.focus();
    await p.click(b);
    return { rows: p.rows(), toast: p.last().message, enabled: !b.hasAttribute('aria-disabled'), focused: p.doc.activeElement === b };
  },
  async backOverConfirm() {
    const p = await page(undefined, undefined, { realConfirm: true });
    p.$('meter').focus();
    await p.open();
    const b = p.one('#space-scheduled .btn--danger');
    b.focus();
    await p.click(b);
    await p.run(0); // the confirm takes the focus
    const asked = p.one('.ui-confirm-overlay').contains(p.doc.activeElement);
    p.layer().close('pop');
    await settle();
    return { asked, open: p.one('.ui-confirm-overlay:not(.is-leaving)') !== null, focused: p.focused(), posts: p.posts().length };
  },
  async scan() {
    const p = await page();
    await p.open('storage');
    const b = p.$('space-scan');
    const look = () => [p.one('.space__scan-label').textContent, p.one('#space-scan use').getAttribute('href'), p.$('space-scan-status').textContent.replace(/\d+s/, 'Ns')];
    const states = [look()];
    b.focus();
    p.server.scan = { running: true };
    await p.click(b); states.push(look());
    await p.run(3000); p.second(); states.push(look());
    p.server.rows = p.server.rows.concat([row('9')]);
    p.server.scan = { running: false };
    await p.run(3000); states.push(look());
    const during = { focused: p.focused(), rows: p.rows(), live: p.$('space-live').textContent.replace(/\d+s/, 'Ns') };
    await p.run(2000); await p.run(5000); states.push(look());
    return { states, during, toasts: p.toasts.length };
  },
  // The seconds of a running scan, at *posted*'s answer and between two polls.
  async scanClock() {
    const of = async (posted) => {
      const p = await page();
      let now = 0;
      p.ctx.Date = { now: () => now };
      p.server.scan = { running: true };
      if (posted) p.server.post = () => ({ status: posted });
      await p.open('storage');
      await p.click(p.$('space-scan'));
      const said = () => p.$('space-scan-status').textContent;
      const out = [said()];
      const after = async (ms, poll) => { now += ms; p.second(); if (poll) await p.run(3000); out.push(said()); };
      await after(1000); await after(1000); await after(1000, true); await after(1000);
      const read = p.$('space-live').textContent;
      p.server.scan = { running: false };
      await p.run(3000);
      const ended = said();
      await after(1000);
      return { out, read, stopped: said() === ended };
    };
    return { started: await of(), already: await of('already_running') };
  },
  // The panel is closed before the scan ends.
  async scanEndsClosed() {
    const of = async (scan, fail, late) => {
      const p = await page();
      await p.open('storage');
      p.server.scan = { running: true };
      if (fail) p.server.fail = fail;
      if (fail && fail['scan/trigger']) p.server.holding = 'scan/trigger';
      await p.click(p.$('space-scan'));
      await p.key('Escape');
      const label = () => p.$('meter').getAttribute('aria-label');
      const busy = () => ['meter', 'phone-meter'].map((id) => p.$(id).getAttribute('aria-busy'));
      await p.refresh(); // drawn again meanwhile
      const during = { busy: busy(), says: label().includes('scanning') };
      if (late) p.server.fail = late;
      await p.let_go('scan/trigger');
      p.server.scan = scan;
      for (const ms of [3000, 2000, 4000, 8000, 10000]) await p.run(ms);
      return { during, toasts: p.toasts.map((t) => [t.message.replace(/\d+s/, 'Ns'), (t.opts && t.opts.variant) || '']), busy: busy(), says: label().includes('scanning') };
    };
    return {
      done: await of({ running: false }),
      stale: await of({ running: false }, null, { '/api/dashboard': 'down' }),
      skipped: await of({ running: false, status: 'skipped', reason: 'Plex not configured' }),
      error: await of({ running: false, status: 'error', reason: 'see the server log' }),
      notStarted: await of({}, { 'scan/trigger': 'Too many scan triggers' }),
      lost: await of({}, { 'scan/status': 'down' }),
    };
  },
  async scanEnds() {
    const of = async (scan, fail) => {
      const p = await page();
      await p.open('storage');
      if (fail) p.server.fail = fail;
      p.server.scan = scan;
      const b = p.$('space-scan');
      b.focus();
      await p.click(b);
      for (const ms of [3000, 2000, 4000, 8000, 10000]) await p.run(ms);
      return { label: p.one('.space__scan-label').textContent, icon: p.one('#space-scan use').getAttribute('href'), said: p.$('space-scan-status').textContent, live: p.$('space-live').textContent,
        link: p.one('#space-scan-status a') ? p.one('#space-scan-status a').href : null, toasts: p.toasts.length, enabled: !b.hasAttribute('aria-disabled'), focused: p.doc.activeElement === b };
    };
    return {
      skipped: await of({ running: false, status: 'skipped', reason: 'Plex not configured' }),
      error: await of({ running: false, status: 'error', reason: 'see the server log for details' }),
      notStarted: await of({}, { 'scan/trigger': 'Too many scan triggers' }),
      lost: await of({}, { 'scan/status': 'down' }),
    };
  },
  async deleted() {
    const payload = { title: 'He said "Hi" & <b>', tvdb_id: 371980, season_number: 2 };
    const p = await page(undefined, (s) => {
      s.deleted = [{ id: 1, title: payload.title, poster_url: '/api/poster/5', deleted_ago: '2 days ago', reclaimed: '21.0 GB', redownload: payload },
        { id: 2, title: 'Old', poster_url: '', deleted_ago: 'yesterday', reclaimed: '8.7 GB', redownload: null }];
    });
    await p.open('deleted');
    const li = p.all('#space-deleted .space-item');
    const b = li[0].querySelector('.space-redo');
    const out = { head: p.one('#space-deleted .space-list-head small').textContent, history: p.one('#space-deleted .space-more').href, rows: li.map((n) => n.textContent), buttons: li.map((n) => n.querySelectorAll('button').length),
      posters: p.all('#space-deleted img').map((i) => [i.src, i.alt]) };
    p.server.holding = 'redownload';
    b.focus();
    await p.click(b);
    out.adding = [b.textContent, b.getAttribute('aria-disabled')];
    await p.click(b);
    await p.let_go('redownload');
    out.queued = [b.textContent, b.getAttribute('aria-disabled'), b.querySelector('use').getAttribute('href'), p.doc.activeElement === b, p.$('space-live').textContent];
    out.posts = p.posts();
    return out;
  },
  async redownloadFails() {
    const p = await page(undefined, (s) => {
      s.deleted = [{ id: 1, title: 'Dune', poster_url: '', deleted_ago: '2 days ago', reclaimed: '1.0 GB', redownload: { title: 'Dune', tmdb_id: 1 } }];
      s.fail = { redownload: 'Radarr is not set up' };
    });
    await p.open('deleted');
    const b = p.one('.space-redo');
    await p.click(b);
    return { text: b.textContent, enabled: !b.hasAttribute('aria-disabled'), toast: p.last() };
  },
  async noDeleted() {
    const p = await page();
    await p.open('deleted');
    return p.text('deleted');
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
