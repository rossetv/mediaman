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
  }
  stats() {
    const l = this.rows.filter((r) => r.state === 'leaving');
    const sum = (list) => list.reduce((n, r) => n + r.file_size_bytes, 0);
    return { storage: this.storage, reclaimed_total_bytes: 5 * GB, reclaimed_total: '5.0 GB',
      scheduled: { total: this.rows.length, total_bytes: sum(this.rows), total_size: label(sum(this.rows)), leaving: l.length, leaving_bytes: sum(l), leaving_size: label(sum(l)),
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

function markup() {
  const pane = (id) => ['div', { class: 'space-pane', id: 'space-' + id, role: 'tabpanel' }];
  const tab = (id) => ['button', { class: 'space-tab', id: 'space-tab-' + id, 'data-space-tab': id }, id, ['span', { class: 'space-tab__count' }]];
  const card = (id) => ['article', { class: 'watch-card', 'data-keep-id': id }, ['a', { class: 'watch-card__link' }],
    ['button', { class: 'watch-keep', 'data-watch-keep': id, 'aria-label': 'Keep Film ' + id, id: 'card-keep-' + id }]];
  return [
    ['header', { id: 'nav' }, ['button', { class: 'space-meter space-meter--phone', 'data-space-open': 'storage', id: 'phone-meter' }]],
    ['main', { id: 'main' },
      ['button', { class: 'space-meter', 'data-space-open': 'storage', id: 'meter', 'aria-busy': 'true' }],
      ['section', { 'data-watch-leaving-row': '', class: 'watch-row' }, ['h2', { class: 'watch-row__title' }, 'Leaving soon'],
        ['div', { class: 'watch-row__scroll' }, card('1'), card('2')]],
      ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', role: 'menu' },
        ...['7', '30', '90'].map((d) => ['button', { class: 'watch-keep-menu__item', 'data-days': d }, d])]],
    ['div', { class: 'space', id: 'space' },
      ['div', { class: 'space__veil', 'data-space-close': '' }],
      ['section', { class: 'space__panel', role: 'dialog' },
        ['div', { class: 'space__grab' }],
        ['div', { class: 'space__tabs' }, ['span', { class: 'space__ink' }], tab('storage'), tab('scheduled'), tab('deleted')],
        ['button', { class: 'space__close', 'data-space-close': '', id: 'close' }],
        pane('storage'), pane('scheduled'), pane('deleted'),
        ['footer', { class: 'space__foot' }, ['p', { id: 'space-scan-status', class: 'space__scan-status' }],
          ['button', { id: 'space-scan', class: 'btn space__scan' }, ['svg', { class: 'ico' }, ['use', { href: '#i-rotate-right' }]], ['span', { class: 'space__scan-label' }, 'Scan now']]]],
      ['p', { id: 'space-live' }]],
  ];
}

async function page(rows = ['1', '2', '3'].map((id) => row(id)), setup = () => {}) {
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
    AbortController: class { abort() {} },
    CustomEvent: class {},
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', transform: 'none', opacity: '1' }),
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900, innerWidth: 1280,
    scrollBy() {},
    location: { reload() { throw new Error('the page was reloaded'); } },
    UIFeedback: { toast: (message, opts) => toasts.push({ message, undo: !!(opts && opts.action), opts }), error: (message) => toasts.push({ message, error: true }), confirm: () => Promise.resolve(confirmed) },
    addEventListener(type, fn) { if (type === 'load') ready.push(fn); },
  };
  ctx.window = ctx;
  ctx.MM = { api: {
    get: (url) => server.request('GET', url), post: (url, body) => server.request('POST', url, body),
    postForm: (url, body) => server.request('FORM', url, body), messageOf: (e, f) => (e && e.message) || f } };
  doc.readyState = 'interactive';
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js'].forEach(run);
  ctx.MMWatch.address = address;
  ['watch/keep.js', 'watch/space.js'].forEach(run);
  function run(file) { vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx); }
  const before = server.calls.length;
  ready.forEach((fn) => fn());
  await settle();
  const one = (sel) => doc.querySelector(sel);
  const all = (sel) => doc.querySelectorAll(sel);
  const p = {
    doc, server, toasts, pushed, $, one, all, before, ctx,
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
    rows: () => all('#space-scheduled .space-item').filter((li) => !li.inert).map((li) => li.getAttribute('data-id')),
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
    const open = { hidden: p.$('space').hidden, focused: p.focused(), held: p.held(), menuFree: !p.$('watch-keep-menu').inert, expanded: p.$('meter').getAttribute('aria-expanded'), pushed: p.pushed.slice() };
    await p.key('Escape');
    return { open, closed: { inert: p.$('space').inert, focused: p.focused(), held: p.held(), expanded: p.$('meter').getAttribute('aria-expanded') } };
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
    return { values, on: p.all('.space-pane').filter((x) => x.classList.contains('is-on')).map((x) => x.id), tabs: p.focused() };
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
      s.storage = storage({ disks: [{ ...one, path: '/media/movies', over_threshold: true }, { ...one, path: '/media/<b>', readable: false }] });
    });
    await p.click(p.$('meter'));
    const mixed = await page(undefined, (s) => { s.storage = storage({ disks: [{ ...one, threshold_shared: false }] }); });
    await mixed.click(mixed.$('meter'));
    return { list: p.all('.space-disk').map((d) => d.textContent), tick: p.one('.space-tick') !== null, line: p.one('.space-fig__line').textContent, mixedTick: mixed.one('.space-tick') !== null };
  },
  async unread() {
    const p = await page(undefined, (s) => { s.storage = storage({ readable: false }); });
    await p.click(p.$('meter'));
    const was = { text: p.text('storage'), segs: p.all('.space-seg').map((b) => b.getAttribute('data-k')) };
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
  async keepAndUndo() {
    const p = await page();
    await p.open();
    await p.keep('2', '30');
    const kept = { rows: p.rows(), focused: p.focused(), posts: p.posts(), toast: p.last(), meter: p.meter(), link: p.one('.space-row-link').textContent,
      card: p.all('.watch-card').map((c) => c.getAttribute('data-keep-id')) };
    delete kept.toast.opts.action.label;
    p.last().opts.action.onClick();
    await settle();
    return { kept: { ...kept, toast: { message: kept.toast.message, undo: kept.toast.undo } }, undone: { rows: p.rows(), focused: p.focused(), post: p.posts()[1], meter: p.meter() } };
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
  async keepFromTheRow() {
    const p = await page();
    await p.open();
    await p.key('Escape');
    await p.click(p.$('card-keep-1'));
    await p.click(p.one('#watch-keep-menu [data-days="7"]'));
    const meter = p.meter();
    await p.open();
    return { meter, rows: p.rows() };
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
  async clearFails() {
    const p = await page(undefined, (s) => { s.fail = { 'clear-scheduled': 'Slow down' }; });
    await p.open();
    const b = p.one('#space-scheduled .btn--danger');
    b.focus();
    await p.click(b);
    return { rows: p.rows(), toast: p.last().message, enabled: !b.hasAttribute('aria-disabled'), focused: p.doc.activeElement === b };
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
    await p.run(3000); states.push(look());
    p.server.rows = p.server.rows.concat([row('9')]);
    p.server.scan = { running: false };
    await p.run(3000); states.push(look());
    const during = { focused: p.focused(), rows: p.rows(), live: p.$('space-live').textContent.replace(/\d+s/, 'Ns') };
    await p.run(2000); await p.run(5000); states.push(look());
    return { states, during, toasts: p.toasts.length };
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
    const out = { head: p.one('#space-deleted .space-list-head small').textContent, history: p.one('#space-deleted .space-more').href, rows: li.map((n) => n.textContent), buttons: li.map((n) => n.querySelectorAll('button').length) };
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
