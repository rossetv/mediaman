// Runs the rows that grow into grids (watch/expand.js and browse.js, on the real address.js, grid.js and rows-load.js)
// against the fake DOM and a fake history, with reduced motion, and prints the outcome of one scenario as JSON.
// Usage: node watch_expand_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const CARD_PX = 160;
const VIEW_PX = 800;

function shown(node) {
  for (let a = node; a; a = a.parentNode) if (a.hidden) return false;
  return node.ownerDocument.body.contains(node);
}

const define = (name, get) => Object.defineProperty(Node.prototype, name, { configurable: true, get });
define('clientWidth', function () { return shown(this) ? VIEW_PX : 0; });
define('scrollWidth', function () { return this.children.length * CARD_PX; });
define('offsetLeft', function () { return this.parentNode.children.indexOf(this) * CARD_PX; });
Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.scrollLeft = 0;
Node.prototype.scrollTo = function (o) { this.scrollLeft = o.left; };
Node.prototype.getClientRects = function () { return shown(this) ? [this.getBoundingClientRect()] : []; };
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };
Node.prototype.removeEventListener = () => {};

const range = (from, to) => Array.from({ length: to - from }, (_, i) => from + i);

function libraryCard(n, over = {}) {
  return ['article', { class: 'watch-card', 'data-keep-id': over.keep || '', ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', href: over.dismiss ? '/watch/play/' + n : '/watch/movie/' + n, ...(over.dismiss ? {} : { 'data-title-ref': 'movie-' + n }) },
      ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, over.title || 'Film ' + n]], ['span', { class: 'watch-card__sub' }, 'Movie']],
    ...(over.keep ? [['button', { 'data-watch-keep': over.keep, 'aria-label': 'Keep Film ' + n }]] : []),
    ['button', { class: 'watch-more', 'data-watch-more': '' }]];
}

function serverRow(id, title, cards, total, next) {
  const viewport = { class: 'watch-row__viewport', ...(next ? { 'data-row-kind': id, 'data-row-next': next } : {}) };
  return ['section', { class: 'watch-row', 'data-row-id': id, 'data-row-total': String(total), ...(id === 'leaving' ? { 'data-watch-leaving-row': '' } : {}) },
    ['div', { class: 'watch-row__head' }, ['h2', { class: 'watch-row__title' },
      ['a', { class: 'row-open', href: '/watch?all=' + id, 'data-row-open': id }, ['span', { 'data-row-title': '' }, title],
        ['span', { class: 'row-open__cue', 'data-grid-fade': '' }, 'See all']]]],
    ['div', viewport, ['div', { class: 'watch-row__scroll', 'aria-label': title }, ...cards]]];
}

const GRIDS = { 'continue': 'Continue watching', leaving: 'Leaving soon', movies: 'Movies', tv: 'TV',
  recommended: 'Recommended for you', trending: 'Trending this week', popular_movies: 'Popular movies' };

const tmdb = (n, kind = 'movie') => ({ ref: 'tmdb-' + (kind === 'movie' ? 'movie' : 'tv') + '-' + n, kind, title: 'T' + n, year: 2026 });
const pick = (n) => ({ ref: 'pick-' + n, pick_ref: 'pick-' + n, kind: 'movie', title: 'P' + n, year: 2026, ai_pick: true, own: true });
const BATCHES = [{ id: 'b3', label: 'Latest picks', date_label: '23 Nov' }, { id: 'b2', label: 'Earlier', date_label: '9 Nov' }, { id: 'b1', label: 'Earlier', date_label: '26 Oct' }];
const recommended = (batch, items, next) => ({ ok: true, enabled: true, batch, batches: BATCHES, items, next_batch: next,
  my_refresh: { available: false, reason: 'not_enough_plays', next_available_at: null, running: false } });
const film = (n, title) => ({ kind: 'movie', title: title || 'Film ' + n, subtitle: 'Movie', href: '/watch/movie/' + n, mark_kind: 'movie', mark_key: String(n) });

async function load(opts = {}) {
  const doc = makeDocument();
  const tab = { entries: [{ url: opts.url || '/watch', state: null }], index: 0 };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  const listeners = {};
  const calls = [];
  const toasts = [];
  const routes = {
    '/api/watch/discover': () => ({ rows: [{ id: 'trending', items: [tmdb(1), tmdb(2, 'show'), tmdb(3)], next_page: 2 }, { id: 'popular_movies', items: range(10, 30).map((n) => tmdb(n)), next_page: null }] }),
    '/api/watch/discover?row=trending&page=2': () => ({ items: [tmdb(4, 'show'), tmdb(5)], next_page: null }),
    '/api/watch/recommended': () => recommended('b3', [pick(1), pick(2)], 'b2'),
    '/api/watch/recommended?batch=b2&start=b3': () => recommended('b2', [pick(3)], 'b1'),
    '/api/watch/recommended?batch=b1&start=b3': () => recommended('b1', [pick(4)], null),
    '/api/watch/library/movies/row?after=c30': () => ({ items: [film(31)], next: null, total: 32 }),
    '/api/watch/library/movies/row?after=c30&limit=60': () => ({ items: [film(31), film(32)], next: null, total: 32 }),
    '/api/watch/library/movies/row?after=&limit=60&sort=az': () => ({ items: [film(9), film(1)], next: 'z', total: 32 }),
    '/api/watch/library/movies/row?after=&limit=60': () => ({ items: [film(1), film(2)], next: null, total: 32 }),
    '/api/watch/library/movies/row?after=z&limit=60&sort=az': () => ({ items: [], next: null, total: 32 }),
    ...opts.routes,
  };
  const server = opts.server ? [['section', { class: 'watch-all', id: 'watch-all' },
    ['div', { class: 'watch-all__head' }, ['a', { class: 'watch-all__back', href: '/watch' }], ['h1', { class: 'watch-all__title', id: 'watch-all-title' }],
      ['span', { class: 'watch-all__count' }], ['div', { class: 'watch-all__tools' }]],
    ['div', { class: 'watch-all__body' }, ['div', { class: 'watch-grid' }, ...opts.server.map((n) => libraryCard(n))], ['div', { class: 'grid-tail' }]]]] : [];
  doc.body.appendChild(build(doc, ['div', { class: 'watch-page', id: 'watch-page' }, ['input', { id: 'watch-search' }],
    ['nav', { id: 'watch-viewtoggle' }, ['a', { href: '/watch', 'data-view': 'rows' }, 'Browse']],
    ['div', { id: 'watch-browse', ...(opts.server ? { hidden: '' } : {}) }, ['div', { class: 'watch-rows' },
      serverRow('continue', 'Continue watching', [['The Zebra', 41], ['Orchard', 42], ['An Apple', 43], ['Éclair', 44], ['Fig', 45], ['Gnu', 46]]
        .map(([title, n]) => libraryCard(n, { title, dismiss: 'movie-' + n })), 6),
      serverRow('leaving', 'Leaving soon', [51, 52].map((n) => libraryCard(n, { keep: String(n) })), 2),
      ['div', { id: 'watch-discover-slot' }],
      serverRow('movies', 'Movies', range(1, 31).map((n) => libraryCard(n)), 32, 'c30')]],
    ...server,
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', hidden: '' }, ['button', { class: 'watch-keep-menu__item', 'data-days': '7' }, '7 days']]]));
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' }, JSON.stringify({ is_admin: false, tmdb_configured: true, grids: opts.grids || GRIDS, grid: opts.grid || null })]));
  doc.createRange = () => ({ selectNodeContents() {}, getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, width: 0, height: 0 }) });
  const get = (url) => {
    calls.push(url);
    const found = routes[url];
    return found ? Promise.resolve().then(found) : Promise.reject(Object.assign(new Error('404 ' + url), { status: 404 }));
  };
  const fire = (type, e) => (listeners[type] || []).slice().forEach((fn) => fn(e || {}));
  const go = (by) => setImmediate(() => { tab.index += by; fire('popstate'); });
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, WeakMap, WeakSet, Map, Set, Date, Error, Infinity,
    URLSearchParams, parseInt, parseFloat, encodeURIComponent,
    setTimeout: (fn) => { setImmediate(fn); return 1; }, clearTimeout: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '', visibility: 'visible', display: 'block', paddingLeft: '0', paddingRight: '0', gridTemplateColumns: '1px 1px 1px' }),
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} },
    innerHeight: 900, innerWidth: 1280, scrollY: 0, scrollTo() {}, scrollBy() {},
    sessionStorage: { getItem: () => null, setItem() {} },
    history: {
      get state() { return tab.entries[tab.index].state; },
      pushState(state, _t, url) { tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state }); tab.index++; },
      replaceState(state, _t, url) { tab.entries[tab.index] = { url, state }; },
      go,
    },
    location: { get pathname() { return here().pathname; }, get search() { return here().search; }, get hash() { return here().hash; } },
    UIFeedback: { toast: (m, o) => toasts.push(o) },
    MM: { api: { get, post: () => Promise.resolve({}), messageOf: (_e, f) => f } },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/search.js', 'watch/rows-load.js', 'watch/grid.js', 'watch/expand.js', 'watch/browse.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f }));
  if (opts.beforeStart) await settle();
  ctx.MMWatch.address.start();
  if (!opts.early) await settle();
  const p = { doc, ctx, calls, tab, fire, toasts };
  p.at = () => tab.entries[tab.index].url;
  p.row = (id) => doc.querySelectorAll('#watch-browse [data-row-id]').find((s) => s.getAttribute('data-row-id') === id);
  p.open = async (id) => { const link = p.row(id).querySelector('.row-open'); link.focus(); const e = press(link); await settle(); fire('scroll'); await p.settle(); return e; };
  const press = (node, keys) => {
    const e = { target: node, button: 0, ...keys, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (let a = node; a; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e));
    (doc.listeners.click || []).forEach((fn) => fn(e));
    return e.defaultPrevented;
  };
  p.press = press;
  p.step = async (by) => { go(by); await settle(); fire('scroll'); await p.settle(); };
  p.settle = async () => { await settle(); await settle(); };
  p.grid = () => {
    const all = doc.getElementById('watch-all');
    const text = (s) => { const n = all.querySelector(s); return n ? n.textContent : null; };
    return all && !all.hidden ? {
      title: text('.watch-all__title'), count: text('.watch-all__count'), sort: text('.watch-all__value'),
      things: all.querySelector('.watch-grid').children.map((n) => (n.classList.contains('watch-card') ? n.querySelector('.watch-card__title').textContent
        : n.classList.contains('grid-group') ? '[' + n.textContent + ']' : '(skel)')),
      tail: text('.grid-tail'),
    } : null;
  };
  p.choose = async (value) => {
    const button = doc.getElementById('watch-all-sort');
    click(doc, button);
    const items = doc.getElementById('watch-all-menu').children;
    const labels = items.map((i) => i.textContent);
    click(doc, items.find((i) => i.getAttribute('data-value') === value));
    await p.settle();
    return labels;
  };
  p.scroll = async (id, left) => {
    const scroller = p.row(id).querySelector('.watch-row__scroll');
    scroller.scrollLeft = left;
    doc.listeners.scroll.forEach((fn) => fn({ target: scroller }));
    await settle();
  };
  p.offer = (id) => p.row(id).querySelector('.row-offer').classList.contains('is-on');
  return p;
}

const scenarios = {
  // The heading opens the row's grid: its cards, then on from where the row stands; Back brings the rows.
  async library() {
    const p = await load();
    const link = p.row('movies').querySelector('.row-open');
    const label = link.getAttribute('aria-label');
    await p.scroll('movies', 0);
    const prevented = await p.open('movies');
    await p.settle();
    const open = p.grid();
    const out = { label, prevented, at: p.at(), title: open.title, count: open.count, sort: open.sort, n: open.things.length, last: open.things.slice(-2), tail: open.tail,
      rows: p.doc.getElementById('watch-browse').hidden, calls: p.calls.filter((c) => /limit=60/.test(c)) };
    p.press(p.doc.querySelector('.watch-all__back'));
    await p.settle();
    return { ...out, back: { at: p.at(), grid: p.grid(), rows: !p.doc.getElementById('watch-browse').hidden, focus: p.doc.activeElement === link } };
  },

  // A to Z is asked of the server; the address has it, and Back and Forward keep it.
  async sorted() {
    const p = await load();
    await p.open('movies');
    const labels = await p.choose('az');
    const sorted = { at: p.at(), grid: p.grid() };
    await p.step(-1);
    const back = { at: p.at(), grid: p.grid() };
    await p.step(1);
    const forward = { at: p.at(), things: p.grid().things, sort: p.grid().sort };
    await p.choose('added');
    return { labels, sorted, back, forward, again: { at: p.at(), n: p.grid().things.length, first: p.grid().things[0] } };
  },

  // A row that is all on the page is put in order there.
  async continued() {
    const p = await load();
    await p.open('continue');
    const first = p.grid();
    await p.choose('az');
    return { first, sorted: p.grid().things, at: p.at(), calls: p.calls.filter((c) => /library|limit/.test(c)) };
  },

  // Only films of a row of both: those on the page, and those of every later page.
  async filtered() {
    const p = await load();
    await p.open('trending');
    const all = p.grid();
    await p.choose('tv');
    return { all: all.things, count: all.count, tail: all.tail, tv: p.grid().things, at: p.at() };
  },

  // Each batch of picks under its date; the pill jumps.
  async picks() {
    const p = await load();
    await p.open('recommended');
    await p.settle();
    const open = p.grid();
    const labels = await p.choose('b1');
    return { things: open.things, sort: open.sort, labels, pill: p.grid().sort, count: open.count };
  },

  // A row that shows whole has a plain heading and opens by its address alone.
  async whole() {
    const p = await load();
    const link = p.row('leaving').querySelector('.row-open');
    const plain = { href: link.getAttribute('href'), label: link.getAttribute('aria-label'), cue: !link.querySelector('.row-open__cue').hidden };
    const prevented = await p.open('leaving');
    return { plain, prevented, at: p.at(), grid: p.grid() };
  },

  // Three screens of one row bring the offer; another row's scroll takes it away and starts the count again.
  async offered() {
    const p = await load();
    const seen = [];
    for (const left of [800, 1600, 2400]) { await p.scroll('movies', left); seen.push(p.offer('movies')); }
    const text = p.row('movies').querySelector('.row-offer__text').textContent;
    const endless = p.row('trending').querySelector('.row-offer__text').textContent;
    await p.scroll('continue', 100);
    const taken = p.offer('movies');
    await p.scroll('movies', 1600);
    const again = p.offer('movies');
    await p.scroll('movies', 0);
    await p.scroll('movies', 800);
    const back = p.offer('movies');
    p.press(p.row('movies').querySelector('.row-offer'));
    await p.settle();
    return { seen, text, endless, taken, again, back, at: p.at(), title: p.grid().title, left: p.offer('movies') };
  },

  // A click that is for a new tab, or for a row nobody knows, is the browser's.
  async otherClicks() {
    const p = await load({ grids: { movies: 'Movies' } });
    const link = p.row('movies').querySelector('.row-open');
    return { tab: p.press(link, { metaKey: true }), unknown: p.press(p.row('continue').querySelector('.row-open')), grid: p.grid(), at: p.at() };
  },

  // A shared link: the server's cards stay the same nodes, and it goes on from its cursor.
  async cold() {
    const p = await load({ url: '/watch?all=movies', server: [1, 2, 3], grid: { id: 'movies', total: 5, next: 'k' },
      routes: { '/api/watch/library/movies/row?after=k&limit=60': () => ({ items: [film(4), film(5)], next: null, total: 5 }) }, early: true });
    const node = p.doc.querySelector('.watch-all .watch-card');
    await p.settle();
    const open = p.grid();
    const same = p.doc.querySelector('.watch-all .watch-card') === node;
    p.press(p.doc.querySelector('.watch-all__back'));
    await p.settle();
    return { open, same, at: p.at(), rows: !p.doc.getElementById('watch-browse').hidden, entries: p.tab.entries.length };
  },

  // A shared link to a row that loads after the page: asked for once.
  async coldLater() {
    const trending = await load({ url: '/watch?all=trending', server: [], grid: { id: 'trending', total: null, next: null } });
    await trending.settle();
    const picks = await load({ url: '/watch?all=recommended', server: [], grid: { id: 'recommended', total: null, next: null } });
    await picks.settle();
    let fail = true;
    const failed = await load({ url: '/watch?all=popular_movies', server: [], grid: { id: 'popular_movies', total: null, next: null },
      routes: { '/api/watch/discover': () => { if (fail) throw Object.assign(new Error('no'), { status: 502 }); return { rows: [{ id: 'popular_movies', items: [tmdb(7)], next_page: null }] }; } } });
    await failed.settle();
    const tail = failed.grid().tail;
    fail = false;
    failed.doc.querySelector('.grid-fail__retry').listeners.click.forEach((fn) => fn());
    await failed.settle();
    return { trending: trending.grid().things, asked: trending.calls.filter((c) => c === '/api/watch/discover').length,
      picks: picks.grid().things, sort: picks.grid().sort, tail, retried: failed.grid().things };
  },

  // What else takes the page closes the grid without showing the rows; Back opens it again.
  async leftFor() {
    const p = await load();
    const address = p.ctx.MMWatch.address;
    await p.open('movies');
    address.push('search', address.url({ q: 'x', all: null, sort: null }));
    const left = { grid: p.grid(), rows: !p.doc.getElementById('watch-browse').hidden };
    await p.step(-1);
    const back = { at: p.at(), title: p.grid().title };
    p.press(p.doc.querySelector('#watch-viewtoggle [data-view="rows"]'));
    await p.settle();
    return { left, back, browse: { at: p.at(), grid: p.grid(), rows: !p.doc.getElementById('watch-browse').hidden } };
  },

  // Both a search and a row in the address: the search has the page. An unknown row or order: none.
  async odd() {
    const at = async (url) => { const p = await load({ url }); return p.grid() && [p.grid().title, p.grid().sort]; };
    return { search: await at('/watch?all=movies&q=x'), list: await at('/watch?view=list&all=movies'), genre: await at('/watch?genre=action&all=movies'),
      unknown: await at('/watch?all=films'), inherited: await at('/watch?all=constructor'), order: await at('/watch?all=continue&sort=newest') };
  },

  // A row changes while the page is inert under a panel; one card is itself on its way out.
  async underAPanel() {
    const p = await load();
    const row = p.row('continue');
    p.doc.getElementById('watch-page').setAttribute('inert', '');
    row.querySelector('.watch-card').setAttribute('inert', '');
    p.ctx.MMWatch.expand.row(row);
    const link = row.querySelector('.row-open');
    return { href: link.getAttribute('href'), label: link.getAttribute('aria-label'), cue: !link.querySelector('.row-open__cue').hidden, offer: row.querySelector('.row-offer__text').textContent };
  },

  // A card kept or dismissed in the grid leaves it, and the row, and comes back to both.
  async taken() {
    const p = await load();
    await p.open('continue');
    const { keep } = p.ctx.MMWatch;
    const cards = () => p.doc.querySelectorAll('.watch-card').filter((c) => c.getAttribute('data-dismiss-ref') === 'movie-42');
    const took = cards().map((c) => keep.fromRow(c, c.closest('.watch-row'), c.getClientRects().length > 0, '[data-watch-more]'));
    const gone = { things: p.grid().things, count: p.grid().count, focus: took[1].focus.closest('.watch-card').querySelector('.watch-card__title').textContent, cards: cards().length };
    took.reverse().forEach((t) => t.undo());
    const back = { things: p.grid().things, count: p.grid().count, cards: cards().length };
    const left = p.doc.querySelector('.watch-all__body').hasAttribute('data-watch-leaving-row');
    const q = await load({ url: '/watch?all=leaving' });
    const all = () => q.doc.querySelectorAll('.watch-all .watch-card');
    const last = all().map((c) => keep && q.ctx.MMWatch.keep.fromRow(c, c.closest('[data-watch-leaving-row]'), true, '[data-watch-keep]'));
    // Taken and filled again since, by a sort and by closing and opening: both ends still meet.
    const refilled = async (id, ref, meanwhile) => {
      const r = await load({ url: '/watch?all=' + id });
      const all = r.doc.querySelectorAll('.watch-card').filter((c) => c.querySelector('.watch-card__link').getAttribute('href').endsWith('/' + ref));
      const taken = all.map((c) => r.ctx.MMWatch.keep.fromRow(c, c.closest('.watch-row'), c.getClientRects().length > 0, '[data-watch-more]'));
      await meanwhile(r);
      const control = taken.reverse().map((t) => t.undo());
      return { things: r.grid().things, count: r.grid().count, focus: control.map((c) => c && r.doc.querySelector('.watch-all').contains(c)) };
    };
    const reopen = async (r) => { r.press(r.doc.querySelector('.watch-all__back')); await r.settle(); r.tab.entries[0].url = '/watch?all=leaving'; r.fire('popstate'); await r.settle(); };
    const sorted = await refilled('continue', 42, async (r) => { await r.choose('az'); });
    const reopened = await refilled('leaving', 51, reopen);
    return { sorted, reopened, gone, back, left, empty: { things: q.grid().things, tail: q.grid().tail, count: q.grid().count, focus: last[1].focus.id, body: q.doc.querySelector('.watch-all__body').hasAttribute('data-watch-leaving-row') } };
  },
};

const keepIn = async (p, root, n) => {
  const button = root.querySelectorAll('[data-watch-keep]').find((b) => b.getAttribute('data-watch-keep') === n);
  button.focus();
  click(p.doc, button);
  click(p.doc, p.doc.querySelector('#watch-keep-menu [data-days]'));
  await p.settle();
};
Object.assign(scenarios, {
  // An Undo of a Keep focuses the Keep that is shown: the grid's while it is open, the row's once it is closed.
  async keepUndone() {
    const go = async (meanwhile, preview) => {
      const p = await load({ url: '/watch?all=leaving' });
      const all = p.doc.getElementById('watch-all');
      const pv = p.doc.body.appendChild(build(p.doc, ['div', { class: 'pv' }, ['button', { 'data-watch-keep': '51', 'aria-label': 'Keep Film 51' }]]));
      const node = all.querySelector('.watch-card');
      await keepIn(p, preview ? pv : all, '51');
      pv.remove();
      const focus = p.doc.activeElement.closest('.watch-card');
      if (meanwhile) await meanwhile(p);
      p.doc.activeElement = p.doc.body;
      p.toasts[0].action.onClick();
      await p.settle();
      const on = p.doc.activeElement;
      return { where: all.contains(on) ? 'grid' : p.row('leaving').contains(on) ? 'row' : on.tagName, keep: on.getAttribute('data-watch-keep'),
        shown: shown(on), same: all.querySelector('.watch-card') === node, things: p.grid() && p.grid().things, kept: focus && focus.getAttribute('data-keep-id') };
    };
    const reopen = async (p) => { p.press(p.doc.querySelector('.watch-all__back')); await p.settle(); };
    return { inGrid: await go(), opening: await go((p) => { const b = p.doc.getElementById('watch-browse'); b.hidden = false; b.setAttribute('inert', ''); }, true), fromPreview: await go(null, true), closed: await go(reopen) };
  },
});

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
