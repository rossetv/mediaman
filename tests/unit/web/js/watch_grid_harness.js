// Runs the Watch page's grid (watch/grid.js, with the core, keep and rows-load it needs) against the fake DOM with a
// small layout, and prints the outcome of one scenario as JSON. Usage: node watch_grid_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

/* The layout: a page 1000 by 800 px under a bar of 50. The rows start at 100, 300 px each, their cards 150 px apart;
   the layer starts at 100, its header 64 px; the grid has five columns of 200 px and lines of 300 px. */
const VIEW = { width: 1000, height: 800 };
const BAR = 50;
const win = { y: 0, width: VIEW.width };

function shown(node) {
  for (let a = node; a; a = a.parentNode) if (a.hidden || a.hasAttribute && a.hasAttribute('hidden')) return false;
  return true;
}

function boxOf(node) {
  if (node.classList.contains('nav-glass')) return [0, 0, 1000, BAR];
  if (node.classList.contains('watch-all')) return [0, 100 - win.y, 1000, 2000];
  if (node.classList.contains('watch-all__head')) return [0, Math.max(BAR, 100 - win.y), 1000, 64];
  if (node.classList.contains('watch-all__body') || node.classList.contains('watch-grid')) return [0, 164 - win.y, 1000, 2000];
  const card = node.closest && node.closest('.watch-card, .watch-card--skel, .grid-group');
  if (card && card.parentNode && card.parentNode.classList.contains('watch-grid')) {
    const i = card.parentNode.children.indexOf(card);
    return [(i % 5) * 200, 164 + Math.floor(i / 5) * 300 - win.y, 190, 280];
  }
  const row = node.closest && node.closest('.watch-row');
  if (row) {
    const top = 100 + row.parentNode.children.indexOf(row) * 300 - win.y;
    if (card) {
      const i = card.parentNode.children.indexOf(card);
      return [i * 150 - (card.parentNode.scrollLeft || 0), top + 40, 140, 230];
    }
    return [0, top, 1000, 280];
  }
  return [0, 0, 20, 20];
}

const rect = (b) => ({ left: b[0], top: b[1], width: b[2], height: b[3], right: b[0] + b[2], bottom: b[1] + b[3] });
Node.prototype.getBoundingClientRect = function () { return rect(shown(this) && this.ownerDocument.body.contains(this) ? boxOf(this) : [0, 0, 0, 0]); };
Node.prototype.getClientRects = function () { return shown(this) && this.ownerDocument.body.contains(this) ? [this.getBoundingClientRect()] : []; };
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };
Object.defineProperty(Node.prototype, 'clientWidth', { configurable: true, get() { return shown(this) ? VIEW.width : 0; } });
Object.defineProperty(Node.prototype, 'offsetHeight', { configurable: true, get() { return this.classList.contains('watch-all__head') ? 64 : 20; } });
Object.defineProperty(Node.prototype, 'firstElementChild', { get() { return this.children[0] || null; } });
Object.defineProperty(Node.prototype, 'id', { get() { return this.attrs.id || ''; }, set(v) { this.attrs.id = v; } });

function posterCard(doc, n, extra = {}) {
  return build(doc, ['article', { class: 'watch-card' },
    ['a', { class: 'watch-card__link', href: '/watch/movie/' + n, 'data-title-ref': 'movie-' + n },
      ['span', { class: 'watch-card__poster' }, ['img', { ...(extra.far ? { 'data-far-src': '/p/' + n } : { src: '/p/' + n }), loading: 'lazy' }],
        ['span', { class: 'watch-card__title' }, 'Film ' + n]], ['span', { class: 'watch-card__sub' }, 'Movie']]]);
}

const range = (from, to) => Array.from({ length: to - from }, (_, i) => from + i);

/* The page with a row of *cards* called Movies (and another under it). */
function load(opts = {}) {
  win.y = 0;
  win.width = VIEW.width;
  const doc = makeDocument();
  const listeners = {};
  const toasts = [];
  const anims = [];
  const timers = [];
  const row = (title, ns) => ['section', { class: 'watch-row' }, ['h2', { class: 'watch-row__title' }, ['a', { href: '/watch?all=x' }, title]],
    ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll', tabindex: '0' }]]];
  doc.body.appendChild(build(doc, ['div', { class: 'nav-glass' }]));
  const server = opts.server ? [['section', { class: 'watch-all', id: 'watch-all' },
    ['div', { class: 'watch-all__head' }, ['a', { class: 'watch-all__back', href: '/watch' }], ['h1', { class: 'watch-all__title' }], ['span', { class: 'watch-all__count' }], ['div', { class: 'watch-all__tools' }]], ['div', { class: 'watch-all__bar' }],
    ['div', { class: 'watch-all__body' }, ['div', { class: 'watch-grid' }]]]] : [];
  doc.body.appendChild(build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['div', { id: 'watch-browse' }, row('Movies'), row('TV')], ...server]));
  const rows = doc.querySelectorAll('.watch-row');
  (opts.cards || range(1, 13)).forEach((n) => rows[0].querySelector('.watch-row__scroll').appendChild(posterCard(doc, n, { far: opts.far === n })));
  range(101, 105).forEach((n) => rows[1].querySelector('.watch-row__scroll').appendChild(posterCard(doc, n)));
  rows.forEach((r) => { r.querySelector('.watch-row__scroll').scrollLeft = 0; });
  if (opts.server) range(50, 53).forEach((n) => doc.querySelector('.watch-grid').appendChild(posterCard(doc, n)));
  doc.createRange = () => ({ selectNodeContents(n) { this.n = n; }, getBoundingClientRect() { return this.n.getBoundingClientRect(); } });
  if (opts.motion) {
    Node.prototype.animate = function (frames, o) {
      const a = { node: this, frames, opts: o, cancelled: false, onfinish: null, cancel() { this.cancelled = true; if (this.oncancel) this.oncancel(); } };
      anims.push(a);
      return a;
    };
  } else {
    delete Node.prototype.animate;
  }
  const fire = (type, event) => (listeners[type] || []).slice().forEach((fn) => fn(event || {}));
  const scroll = (y) => { win.y = Math.max(0, y); fire('scroll'); };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, WeakSet, Map, Set, Error, isNaN, parseFloat, parseInt,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    getComputedStyle: (n) => ({ getPropertyValue: () => '', display: 'block', visibility: 'visible', paddingLeft: '0', fontSize: '20px', transform: n.mid || 'none', opacity: '0.4',
      gridTemplateColumns: '200px 200px 200px 200px 200px' }),
    matchMedia: (q) => ({ matches: /coarse/.test(q) ? !!opts.touch : !opts.motion }),
    requestAnimationFrame: (fn) => fn(),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    scrollTo: (o) => scroll(o.top),
    scrollBy: (o) => scroll(win.y + o.top),
    innerHeight: VIEW.height,
    UIFeedback: { toast: (m) => toasts.push(m) },
    MM: { dom: { icon: (n) => build(doc, ['svg', { class: n }]) } },
  };
  Object.defineProperty(ctx, 'scrollY', { get: () => win.y });
  Object.defineProperty(ctx, 'innerWidth', { get: () => win.width });
  ctx.window = ctx;
  vm.createContext(ctx);
  ['watch/core.js', 'watch/keep.js', 'watch/rows-load.js', 'watch/grid.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const grid = ctx.MMWatch.grid;
  const backs = [];
  const p = { doc, ctx, grid, rows, toasts, anims, timers, fire, scroll, backs, card: (n) => posterCard(doc, n), heard: (t) => (listeners[t] || []).length };
  p.opts = (more) => ({ id: 'movies', title: 'Movies', total: 12, row: () => rows[0], next: null, back: () => backs.push('back'), ...more });
  p.refs = () => doc.querySelectorAll('.watch-all [data-title-ref]').map((a) => Number(a.getAttribute('data-title-ref').slice(6)));
  p.all = () => doc.querySelector('.watch-all');
  p.browse = doc.querySelector('#watch-browse');
  p.say = () => doc.querySelectorAll('[role="status"]').map((n) => n.textContent).filter(Boolean).join(' / ');
  p.focus = () => { const a = doc.activeElement; return a.getAttribute('data-title-ref') || a.id || a.className || a.tagName; };
  p.key = (k, target) => { const e = { key: k, target: target || doc.activeElement, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (let a = e.target; a; a = a.parentNode) (a.listeners.keydown || []).forEach((fn) => fn(e));
    (doc.listeners.keydown || []).forEach((fn) => fn(e)); fire('keydown', e); return e; };
  p.finish = () => anims.slice().forEach((a) => { if (!a.cancelled && !a.done) { a.done = true; if (a.onfinish) a.onfinish(); } });
  p.leftovers = () => doc.querySelectorAll('.is-track, .is-flyer, .is-flown, .is-under, .is-pinned, .watch-ride, [inert]').length;
  p.state = () => ({ browse: shown(p.browse), all: !!p.all() && shown(p.all()), moving: grid.moving(), open: grid.isOpen() });
  return p;
}

/* A grid alone on the page, as another view holds one. */
function lone(opts, pages) {
  const p = load();
  p.browse.hidden = true;
  const host = p.doc.querySelector('#watch-page').appendChild(build(p.doc, ['div', { class: 'results' }]));
  p.asked = [];
  p.list = p.grid.list(host, {
    title: 'Action', total: null, next: pages ? 'p0' : null,
    fetchPage: (next) => { p.asked.push(next); const page = pages.shift(); return page instanceof Promise ? page : page.status ? Promise.reject(Object.assign(new Error('no'), page)) : Promise.resolve({ cards: page.cards.map(make), next: page.next }); },
    ...opts, cards: (opts.cards || []).map(make),
  });
  function make(n) { return typeof n === 'string' ? p.grid.group(n, 'Picks of ' + n) : p.card(n); }
  p.make = make;
  p.nums = () => host.querySelectorAll('.watch-card__link').map((a) => Number(a.getAttribute('data-title-ref').slice(6)));
  p.skels = () => host.querySelectorAll('.watch-card--skel').length;
  p.tail = () => host.querySelector('.grid-tail').textContent;
  p.host = host;
  return p;
}

const pressSort = (p, id) => {
  click(p.doc, p.doc.querySelector('#watch-all-sort'));
  click(p.doc, p.doc.querySelectorAll('#watch-all-menu [data-value]').find((i) => i.getAttribute('data-value') === id));
};
const sortOf = (choose) => ({ label: 'Sort', value: 'added', options: [{ id: 'added', label: 'Recently added' }, { id: 'az', label: 'A to Z' }, { id: 'year', label: 'Newest release' }], choose });
const label = (p) => p.doc.querySelector('#watch-all-sort').getAttribute('aria-label');

const scenarios = {
  async loadsToTheEnd() {
    let give;
    const p = lone({ cards: range(1, 8) }, [new Promise((ok) => { give = ok; }), { cards: range(40, 42), next: null }]);
    const waiting = { skels: p.skels(), busy: p.list.node.getAttribute('aria-busy') };
    give({ cards: [7, 8, 9].map(p.make), next: 'p1' });
    await settle();
    return { waiting, nums: p.nums(), asked: p.asked, left: p.skels(), tail: p.tail(), say: p.say() };
  },

  async failsAndComesBack() {
    const p = lone({ cards: range(1, 4) }, [{ status: 500 }, { cards: [4], next: null }]);
    await settle();
    const alert = p.host.querySelector('[role="alert"]');
    const failed = { text: alert.textContent, skels: p.skels(), still: p.list.node.classList.contains('is-static') };
    const button = alert.querySelector('button');
    button.focus();
    button.listeners.click.forEach((fn) => fn({}));
    await settle();
    return { failed, nums: p.nums(), skels: p.skels(), still: p.list.node.classList.contains('is-static'), alert: !!p.host.querySelector('[role="alert"]'), focus: p.focus() };
  },

  async ends() {
    const tail = (opts) => lone(opts).tail();
    const empty = lone({ empty: { title: 'Nothing in Action yet', text: 'Try another.', link: 'Back', href: '//evil' } });
    return { many: tail({ cards: [1, 2], total: 2 }), one: tail({ cards: [1], total: 1 }), endless: tail({ cards: [1] }),
      empty: empty.tail(), href: empty.host.querySelector('a').getAttribute('href') };
  },

  async otherContentsDropTheOldPages() {
    let give;
    const p = lone({ cards: [1, 2, 3] }, [new Promise((ok) => { give = ok; })]);
    const kept = p.list.cards()[2];
    p.list.set({ cards: [3, 9, 1].map(p.make), next: null, total: 3 });
    give({ cards: [77].map(p.make), next: 'p1' });
    await settle();
    return { nums: p.nums(), sameNode: p.list.cards()[0] === kept, tail: p.tail(), skels: p.skels() };
  },

  async otherContentsTravel() {
    const p = load({ motion: true });
    const host = p.doc.querySelector('#watch-page').appendChild(build(p.doc, ['div', {}]));
    p.browse.hidden = true;
    const list = p.grid.list(host, { title: 'x', total: 3, cards: [1, 2, 3].map(p.card), next: null });
    list.set({ cards: [3, 1, 4].map(p.card), next: null }, { move: true });
    const said = p.anims.map((a) => [a.node.querySelector('[data-title-ref]').getAttribute('data-title-ref'), a.frames[0].transform, a.frames[0].opacity === undefined ? null : a.frames[0].opacity]);
    const gone = host.querySelectorAll('.is-gone').length;
    p.finish();
    return { said, gone, goneAfter: host.querySelectorAll('.is-gone').length, flyers: host.querySelectorAll('.is-flyer').length };
  },

  async arrowKeys() {
    const p = lone({ cards: range(1, 13) });
    const links = p.list.node.querySelectorAll('.watch-card__link');
    links[1].focus();
    const path = [];
    for (const k of ['ArrowRight', 'ArrowDown', 'ArrowDown', 'ArrowDown', 'ArrowUp', 'ArrowLeft']) { const e = p.key(k); path.push(p.focus().slice(6) + (e.defaultPrevented ? '' : '!')); }
    const tab = p.key('Tab').defaultPrevented;
    // Lines of five, two and five, as dated lines leave them.
    const all = p.list.cards();
    const at = [[0, 0], [1, 0], [2, 0], [3, 0], [4, 0], [0, 1], [1, 1], [0, 2], [1, 2], [2, 2], [3, 2], [4, 2]];
    all.forEach((c, i) => { c.getBoundingClientRect = () => rect([at[i][0] * 200, 164 + at[i][1] * 300, 190, 280]); });
    links[4].focus();
    p.key('ArrowDown');
    return { path, tab, short: p.focus().slice(6) };
  },

  async jumpLoadsUntilThere() {
    const p = lone({ cards: ['a', 1, 2] }, [{ cards: ['b', 3], next: 'p1' }, { cards: ['c', 4], next: 'p2' }, { cards: ['d', 5], next: 'p3' }, { cards: ['e', 6], next: null }]);
    // The end is far off: only the jump asks for more.
    p.list.cards().forEach((c) => { c.getBoundingClientRect = () => rect([0, 5000, 190, 280]); });
    const add = Node.prototype.getBoundingClientRect;
    Node.prototype.getBoundingClientRect = function () { return this.classList.contains('watch-card') ? rect([0, 5000, 190, 280]) : add.call(this); };
    await settle();
    const before = p.asked.length;
    p.list.jump('c');
    for (let i = 0; i < 6; i++) { await settle(); p.timers.splice(0).forEach((t) => t.fn()); }
    const asked = p.asked.length;
    p.list.jump('zz');
    p.fire('wheel');
    for (let i = 0; i < 6; i++) { await settle(); p.timers.splice(0).forEach((t) => t.fn()); }
    Node.prototype.getBoundingClientRect = add;
    return { before, asked, taken: p.asked.length, scrolled: win.y > 0 };
  },

  async opens() {
    const p = load({ far: 12 });
    p.rows[0].querySelectorAll('.watch-card__link')[2].focus();
    p.grid.open(p.opts());
    const copies = p.doc.querySelectorAll('.watch-all .watch-card');
    return { ...p.state(), refs: p.refs(), focus: p.focus(), say: p.say(), count: p.doc.querySelector('.watch-all__count').textContent,
      far: copies[11].querySelector('img').getAttribute('src'), row: p.rows[0].querySelectorAll('.watch-card').length, sort: !!p.doc.querySelector('#watch-all-sort') };
  },

  async keysOfItsOwn() {
    const p = load();
    const links = (root) => root.querySelectorAll('.watch-card__link').map((a) => a.getAttribute('tabindex'));
    p.rows[0].querySelectorAll('.watch-card__link').forEach((a) => a.setAttribute('tabindex', '-1'));
    p.grid.open(p.opts());
    return { row: [...new Set(links(p.rows[0]))], grid: [...new Set(links(p.all()))] };
  },

  async closesWhereItWas() {
    const p = load();
    p.scroll(90);
    const scroller = p.rows[0].querySelector('.watch-row__scroll');
    scroller.scrollLeft = 300;
    const link = p.rows[0].querySelectorAll('.watch-card__link')[3];
    link.focus();
    p.grid.open(p.opts());
    p.scroll(500);
    scroller.scrollLeft = 0;
    p.grid.close();
    const same = { y: win.y, left: scroller.scrollLeft, focus: p.focus(), ...p.state(), say: p.say(), cards: p.refs().length };
    p.grid.open(p.opts());
    p.doc.querySelectorAll('.watch-all .watch-card__link')[4].focus();
    p.grid.close();
    const onCard = p.focus();
    p.grid.open(p.opts());
    p.doc.querySelectorAll('.watch-all .watch-card__link')[11].focus();
    p.grid.close();
    return { same, onCard, offScreen: p.focus() };
  },

  async midRowOpensOnItsLine() {
    const p = load({ cards: range(1, 31) });
    p.rows[0].querySelector('.watch-row__scroll').scrollLeft = 150 * 17;
    p.grid.open(p.opts({ total: 30 }));
    const card = p.doc.querySelectorAll('.watch-all .watch-card')[17].getBoundingClientRect();
    return { top: card.top };
  },

  async fromALink() {
    const p = load();
    p.grid.open(p.opts({ instant: true, row: () => p.rows[1] }));
    const opened = p.state();
    p.grid.close();
    const r = p.rows[1].getBoundingClientRect();
    return { opened, mid: r.top + r.height / 2, focus: p.focus(), ...p.state() };
  },

  /* Forward into a grid left scrolled, then Back to rows this page never saw before the grid. */
  async throughTheHistory() {
    const go = (y, motion) => {
      const p = load({ motion });
      p.scroll(90);
      const step = { y };
      p.ctx.MMWatch.address = { top: () => 'grid', arriving: () => ({ ...step, rows: { movies: 450 } }) };
      const placed = p.grid.open(p.opts());
      return { placed, moving: p.grid.moving(), y: win.y };
    };
    const p = load();
    p.rows[0].setAttribute('data-row-id', 'movies');
    p.grid.open(p.opts({ instant: true }));
    p.ctx.MMWatch.address = { top: () => null, arriving: () => ({ y: 777, rows: { movies: 450 } }) };
    p.grid.close();
    const closed = { y: win.y, left: p.rows[0].querySelector('.watch-row__scroll').scrollLeft };
    return { closed, deep: go(5000, true), shallow: go(60, true), still: go(60, false) };
  },

  async serverDrawn() {
    const p = load({ server: true });
    p.browse.hidden = true;
    p.grid.open(p.opts({ instant: true, total: 3 }));
    const drawn = p.refs();
    p.grid.close();
    p.grid.open(p.opts());
    return { drawn, after: p.refs() };
  },

  async movesAndLands() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    const mid = { ...p.state(), pinned: p.browse.classList.contains('is-pinned'), inert: p.browse.hasAttribute('inert') };
    const first = p.anims.find((a) => a.node === p.doc.querySelectorAll('.watch-all .watch-card')[0]);
    const one = p.anims[0];
    one.done = true;
    one.onfinish();
    const afterOne = p.grid.moving();
    const only = new Set(p.anims.flatMap((a) => a.frames.flatMap((f) => Object.keys(f))));
    p.finish();
    const landed = { ...p.state(), left: p.leftovers() };
    p.anims.length = 0;
    p.grid.close();
    const closing = { ...p.state(), pinned: p.all().classList.contains('is-pinned') };
    p.finish();
    return { mid, afterOne, from: first.frames[0].transform, props: [...only].sort(), landed, closing, closed: { ...p.state(), left: p.leftovers(), cards: p.refs().length } };
  },

  async turnsRoundFromWhereItIs() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    const opening = p.anims.splice(0);
    const card = p.doc.querySelectorAll('.watch-all .watch-card')[0];
    card.mid = 'matrix(0.9, 0, 0, 0.9, 12, 34)';
    p.grid.close();
    const closing = p.anims.splice(0);
    const back = closing.find((a) => a.node === card);
    // What it was doing may still say it is done.
    opening.forEach((a) => { if (a.onfinish) a.onfinish(); });
    const after = p.state();
    p.grid.open(p.opts());
    const again = p.anims.find((a) => a.node === card);
    const cards = p.refs().length;
    p.finish();
    return { from: back.frames[0].transform, delays: [...new Set(closing.map((a) => a.opts.delay))], slowest: Math.max(...closing.map((a) => a.opts.duration)),
      cancelled: opening.every((a) => a.cancelled), after, again: again.frames[1].transform, cards, landed: p.state(), left: p.leftovers() };
  },

  /* Safari: a page laid out too short for its scroll gives way, and says where what is fixed is by the scroll it had. */
  async scrolledAndTurned() {
    const p = load({ motion: true });
    const page = p.doc.querySelector('#watch-page');
    const flat = Node.prototype.getBoundingClientRect;
    const inFlow = (n) => !!n && shown(n) && !n.classList.contains('is-pinned');
    const height = () => Math.max(parseFloat(page.style.minHeight) || 0, inFlow(p.browse) ? 3000 : inFlow(p.all()) ? 900 : 0);
    let late = 0;
    const tall = Object.getOwnPropertyDescriptor(Node.prototype, 'offsetHeight');
    Object.defineProperty(Node.prototype, 'offsetHeight', { configurable: true, get() { return this === page ? height() : tall.get.call(this); } });
    Node.prototype.getBoundingClientRect = function () {
      const most = Math.max(0, height() - VIEW.height);
      if (win.y > most) { late = win.y - most; win.y = most; }
      const r = flat.call(this);
      const layer = this.closest('.is-pinned');
      const by = layer ? parseFloat(layer.style.top) - (100 - win.y) + late : this.classList.contains('nav-glass') ? late : 0;
      return rect([r.left, r.top + by, r.width, r.height]);
    };
    const scrollTo = p.ctx.scrollTo;
    p.ctx.scrollTo = (o) => { late = 0; scrollTo(o); };
    const first = (layer) => layer.querySelector('.watch-card').getBoundingClientRect().top;
    const went = (n) => p.anims.filter((a) => a.node === n).pop().frames;
    const out = {};
    p.scroll(120);
    out.rows = first(p.browse);
    p.grid.open(p.opts());
    const card = p.all().querySelector('.watch-card');
    out.opened = [first(p.browse), win.y, went(card)[0].transform];
    p.finish();
    p.scroll(90);
    out.grid = first(p.all());
    p.grid.close();
    out.closing = [first(p.all()), win.y, went(card)[1].transform, page.style.minHeight];
    Node.prototype.getBoundingClientRect = flat;
    Object.defineProperty(Node.prototype, 'offsetHeight', tall);
    return out;
  },

  async resizedMidway() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    win.width = 700;
    p.fire('resize');
    return { ...p.state(), left: p.leftovers() };
  },

  async reducedMotion() {
    const p = load({ motion: true });
    p.ctx.matchMedia = () => ({ matches: true });
    p.grid.open(p.opts({ sort: sortOf(() => Promise.resolve({ cards: [5, 1].map(p.card), next: null, total: 2 })) }));
    pressSort(p, 'az');
    await settle();
    const open = p.state();
    p.grid.close();
    return { open, closed: p.state(), anims: p.anims.length };
  },

  async escape() {
    const p = load();
    p.grid.open(p.opts());
    let top = 'title';
    p.ctx.MMWatch.address = { top: () => top };
    p.key('Escape');
    const under = p.backs.length;
    top = 'grid';
    p.doc.addEventListener('keydown', p.other = (e) => { if (p.eat) e.preventDefault(); });
    p.eat = true;
    p.key('Escape');
    const eaten = p.backs.length;
    p.eat = false;
    p.key('Escape');
    const own = p.backs.length;
    p.grid.close();
    p.key('Escape');
    return { under, eaten, own, closed: p.backs.length };
  },

  async backLink() {
    const p = load();
    p.grid.open(p.opts());
    const back = p.doc.querySelector('.watch-all__back');
    const press = (more) => { const e = { target: back, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...more };
      for (let a = back; a; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e)); return [p.backs.length, e.defaultPrevented]; };
    return { newTab: press({ metaKey: true }), plain: press({}) };
  },

  async sorts() {
    const p = load();
    const gives = {};
    const chosen = [];
    p.grid.open(p.opts({ sort: { ...sortOf((id) => new Promise((ok, no) => { gives[id] = { ok, no }; })), chosen: (id) => chosen.push(id) } }));
    pressSort(p, 'az');
    const waiting = [label(p), p.doc.querySelector('.watch-all__body').getAttribute('aria-busy')];
    pressSort(p, 'year');
    gives.az.ok({ cards: [8].map(p.card), next: null, total: 1 });
    await settle();
    const late = p.refs().length;
    gives.year.ok({ cards: [6, 2].map(p.card), next: null, total: 2 });
    await settle();
    const done = { refs: p.refs(), label: label(p), count: p.doc.querySelector('.watch-all__count').textContent, busy: p.doc.querySelector('.watch-all__body').getAttribute('aria-busy'), say: p.say() };
    pressSort(p, 'az');
    gives.az.no(new Error('no'));
    await settle();
    return { waiting, late, done, chosen, failed: [label(p), p.refs(), p.toasts.length, p.doc.querySelector('.watch-all__body').getAttribute('aria-busy')] };
  },

  // Another order asked for while the first page is on its way, which comes before that order fails or is in.
  async sortWhileTheFirstPageComes() {
    const go = async (lands, page) => {
      const p = load();
      let first; let order;
      const stood = [];
      p.grid.open(p.opts({
        waiting: true, sort: sortOf(() => new Promise((ok, no) => { order = { ok, no }; })),
        later: () => new Promise((ok, no) => { first = { ok, no }; }),
        fetchPage: () => Promise.resolve({ cards: [40].map(p.card), next: null }),
      }));
      pressSort(p, 'az');
      if (page) first.ok({ cards: range(1, 21).map(p.card), next: 'c', total: 60, placed: () => stood.push(p.refs().length) });
      else first.no(new Error('no'));
      await settle();
      const before = p.refs().length;
      await lands(() => order, p);
      await settle();
      return { before, refs: p.refs().length, label: label(p), stood };
    };
    const fails = (order) => order().no(new Error('no'));
    return {
      fails: await go(fails, true),
      // And a third that fails after it.
      comes: await go(async (order, p) => {
        order().ok({ cards: [9, 8].map(p.card), next: null, total: 2 });
        await settle();
        pressSort(p, 'year');
        order().no(new Error('no'));
      }, true),
      bothFail: await go(fails, false),
    };
  },

  async sortAnswersAfterLeaving() {
    const p = load({ motion: true });
    let give;
    const chosen = [];
    p.grid.open(p.opts({ sort: { ...sortOf(() => new Promise((ok) => { give = ok; })), chosen: (id) => chosen.push(id) } }));
    p.finish();
    pressSort(p, 'az');
    p.grid.close();
    give({ cards: [8].map(p.card), next: null, total: 1 });
    await settle();
    const during = p.refs().length;
    const was = label(p);
    p.finish();
    return { during, label: was, chosen, ...p.state(), left: p.leftovers() };
  },

  async leaves() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    p.grid.leave();
    return { ...p.state(), left: p.leftovers() };
  },

  async nothingToSort() {
    const p = load();
    p.grid.open(p.opts({ cards: [], total: 0, sort: sortOf(() => {}) }));
    return { sort: !!p.doc.querySelector('#watch-all-sort'), text: p.doc.querySelector('.grid-empty').textContent, count: p.doc.querySelector('.watch-all__count').textContent };
  },

  /* The grid already open is asked for again: as it is, in another order, and mid-way back to its rows in another. */
  async opensAgain() {
    const orders = { added: range(1, 13), az: [2, 1, 3] };
    const sort = (value) => ({ ...sortOf((id) => ({ cards: orders[id].map(p.card), next: null, total: orders[id].length })), value });
    let p = load();
    p.grid.open(p.opts({ sort: sort('az') }));
    p.scroll(700);
    const card = p.doc.querySelectorAll('.watch-all .watch-card')[3];
    p.grid.open(p.opts({ sort: sort('az') }));
    const same = [p.doc.querySelectorAll('.watch-all .watch-card')[3] === card, win.y];
    p.grid.open(p.opts({ sort: sort('added') }));
    await settle();
    const other = [label(p), p.refs()];
    p.grid.open(p.opts({ sort: { ...sort('added'), jump: true, value: 'az' } }));
    await settle();
    const jump = label(p);
    p = load({ motion: true });
    p.grid.open(p.opts({ sort: sort('az'), cards: orders.az.map(p.card) }));
    p.finish();
    p.grid.close();
    p.grid.open(p.opts({ sort: sort('added') }));
    p.finish();
    return { same, other, jump, midway: [label(p), p.refs(), p.state().all, p.leftovers()] };
  },

  /* Pages that load as it opens, and one loaded later. */
  async opensAndLoads() {
    const p = load();
    vm.runInContext('Date.now = () => clock', Object.assign(p.ctx, { clock: 50000 }));
    const pages = { a: { cards: [20, 21].map(p.card), next: 'b' }, b: { cards: [22].map(p.card), next: 'c' }, c: { cards: [23].map(p.card), next: null } };
    const gives = {};
    p.grid.open(p.opts({ next: 'a', fetchPage: (n) => new Promise((ok) => { gives[n] = () => ok(pages[n]); }) }));
    const said = [p.say()];
    for (const [page, at] of [['a', 50400], ['b', 52900], ['c', 53100]]) { p.ctx.clock = at; gives[page](); await settle(); said.push(p.say()); }
    return said;
  },

  // Nothing loads while it moves; once it has landed it does, and once it is closed no more.
  async loadsOnlyWhenStill() {
    const p = load({ motion: true });
    const asked = [];
    const heard = p.heard('scroll');
    p.grid.open(p.opts({ next: 'a', fetchPage: (n) => { asked.push(n); return new Promise(() => {}); } }));
    p.fire('scroll');
    const moving = asked.length;
    p.finish();
    const landed = asked.length;
    p.grid.close();
    p.finish();
    p.fire('scroll');
    return { moving, landed, closed: asked.length, heard: p.heard('scroll') - heard };
  },

  // A grid that is stopped listens to nothing more.
  async stopped() {
    const p = load();
    p.listeners = () => ['scroll', 'wheel', 'touchmove', 'keydown', 'resize'].map((t) => p.heard(t));
    const before = p.listeners();
    const host = p.doc.querySelector('#watch-page').appendChild(build(p.doc, ['div', {}]));
    const list = p.grid.list(host, { title: 'x', cards: [1].map(p.card), next: null });
    const during = p.listeners();
    list.stop();
    return { grew: during.map((n, i) => n > before[i]), after: p.listeners().map((n, i) => n - before[i]) };
  },

  // The page scrolled while it closes: the grid on its way out goes with it.
  async scrollsWhileClosing() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    p.finish();
    p.grid.close();
    const top = parseFloat(p.all().style.top);
    p.scroll(win.y + 70);
    return { by: parseFloat(p.all().style.top) - top };
  },

  // Opened while the rows are hidden (over results): it closes to no place of an earlier one.
  async placeIsForgotten() {
    const p = load();
    p.scroll(90);
    p.rows[0].querySelectorAll('.watch-card__link')[3].focus();
    p.grid.open(p.opts());
    p.grid.close();
    p.doc.activeElement.blur();
    p.browse.hidden = true;
    p.scroll(0);
    p.grid.open(p.opts({ row: () => p.rows[1] }));
    p.grid.close();
    return { y: win.y, focus: p.focus() };
  },

  // Left for another view and opened again by Back: it still closes to where the rows were.
  async openedAgain() {
    const p = load();
    p.scroll(90);
    p.rows[0].querySelectorAll('.watch-card__link')[3].focus();
    p.grid.open(p.opts());
    p.grid.leave();
    const left = win.y;
    p.grid.open(p.opts({ instant: true, again: true }));
    p.grid.close();
    return { left, y: win.y, focus: p.focus() };
  },

  // A jump gives up when its page fails, when it takes too long, and for a key that scrolls.
  async jumpGivesUp() {
    const run = async (page, meanwhile) => {
      const p = lone({ cards: ['a', 1] }, [page, { cards: ['c', 4], next: 'p9' }, { cards: ['d', 5], next: 'p10' }, { cards: ['z', 9], next: null }]);
      const add = Node.prototype.getBoundingClientRect;
      Node.prototype.getBoundingClientRect = function () { return this.classList.contains('watch-card') ? rect([0, 5000, 190, 280]) : add.call(this); };
      p.list.jump('z');
      if (meanwhile) meanwhile(p);
      for (let i = 0; i < 4; i++) { await settle(); p.timers.splice(0).forEach((t) => t.fn()); }
      const button = p.host.querySelector('.grid-fail__retry');
      if (button) button.listeners.click.forEach((fn) => fn({}));
      for (let i = 0; i < 6; i++) { await settle(); p.timers.splice(0).forEach((t) => t.fn()); }
      Node.prototype.getBoundingClientRect = add;
      return p.asked.length;
    };
    let give;
    const slow = new Promise((ok) => { give = ok; });
    return {
      there: await run({ cards: ['b', 2], next: 'p1' }),
      failed: await run({ status: 500 }),
      key: await run({ cards: ['b', 2], next: 'p1' }, (p) => p.key('PageDown')),
      typed: await run({ cards: ['b', 2], next: 'p1' }, (p) => p.key('x')),
      slow: await run(slow, (p) => { vm.runInContext('Date.now = function () { return 1e15; };', p.ctx); give({ cards: [p.make(2)], next: 'p1' }); }),
    };
  },

  // A copy is of the card at rest, and opening closes a preview over it.
  async previewGoes() {
    const p = load();
    let closed = 0;
    p.ctx.MMWatch.preview = { close() { closed++; } };
    p.rows[0].querySelectorAll('.watch-card__link')[0].classList.add('is-previewed');
    p.grid.open(p.opts());
    return { closed, copies: p.doc.querySelectorAll('.watch-all .is-previewed').length };
  },

  // A card taken out: the rest close up from where they were, and it comes back to its place.
  async takesOut() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    p.finish();
    p.anims.length = 0;
    const card = p.doc.querySelectorAll('.watch-all .watch-card')[1];
    const row = p.rows[0].querySelectorAll('.watch-card')[1];
    const notHere = p.grid.take(row, '.x');
    const took = p.grid.take(card, '.watch-card__link');
    const moved = p.anims.filter((a) => /translate/.test(a.frames[0].transform)).length;
    const ghost = card.classList.contains('is-gone');
    const gone = { inert: card.hasAttribute('inert'), count: p.doc.querySelector('.watch-all__count').textContent, focus: took.focus.getAttribute('data-title-ref') };
    const back = took.undo();
    p.finish();
    return { notHere, moved, ghost, gone, refs: p.refs().slice(0, 3), count: p.doc.querySelector('.watch-all__count').textContent, back: back.getAttribute('data-title-ref'), left: p.leftovers() + (card.classList.contains('is-gone') ? 1 : 0) };
  },

  // Undone once the card has faded out: nothing it was left with holds it unseen. A card off the page lists no animations.
  async backFromAFade() {
    const p = load({ motion: true });
    Node.prototype.getAnimations = function () { return p.doc.body.contains(this) ? p.anims.filter((a) => a.node === this && !a.cancelled) : []; };
    p.grid.open(p.opts());
    p.finish();
    p.anims.length = 0;
    const card = p.doc.querySelectorAll('.watch-all .watch-card')[1];
    const took = p.grid.take(card, '.watch-card__link');
    const fade = p.anims.find((a) => a.node === card);
    p.finish();
    const off = !p.doc.body.contains(card);
    took.undo();
    return { off, held: !fade.cancelled && fade.opts.fill === 'forwards', back: p.refs()[1] };
  },

  // A card comes back to its row: to the grid too where that was filled without it, once, and to its own place.
  async bringsBack() {
    const inOrder = (p, sort) => { const ns = p.rows[0].querySelectorAll('[data-title-ref]').map((a) => Number(a.getAttribute('data-title-ref').slice(6)));
      if (sort === 'az') ns.reverse(); return { cards: ns.map(p.card), next: null, total: ns.length }; };
    const start = () => { const p = load(); p.filled = 0; p.more = { contents: (s) => { p.filled++; return inOrder(p, s); }, sort: sortOf((s) => inOrder(p, s)), total: 12 }; p.open = () => p.grid.open(p.opts({ ...p.more, ...inOrder(p) })); return p; };
    const told = (p) => ({ refs: p.refs(), count: p.doc.querySelector('.watch-all__count').textContent, sort: !!p.doc.querySelector('#watch-all-sort') });
    const out = {};
    // Taken under the other order, sorted, back.
    const p = start();
    p.open();
    pressSort(p, 'az');
    await settle();
    const inRow = p.rows[0].querySelectorAll('.watch-card')[1];
    const spot = inRow.nextElementSibling;
    inRow.remove();
    const took = p.grid.take(p.doc.querySelectorAll('.watch-all .watch-card')[10], '.watch-card__link');
    pressSort(p, 'added');
    await settle();
    const stale = took.undo();
    spot.before(inRow);
    p.scroll(400);
    const brought = p.grid.bring(inRow);
    out.sorted = { ...told(p), stale, brought: p.all().contains(brought) && p.refs()[1], y: win.y, twice: p.grid.bring(inRow) };
    // Opened again in between.
    const q = start();
    const gone = q.rows[0].querySelectorAll('.watch-card')[3];
    const next = gone.nextElementSibling;
    q.open();
    const late = q.grid.take(q.doc.querySelectorAll('.watch-all .watch-card')[3], '.watch-card__link');
    gone.remove();
    q.grid.close();
    q.open();
    out.lateUndo = late.undo();
    next.before(gone);
    q.grid.bring(gone);
    out.reopened = told(q);
    // Emptied, then back; and another row's card brings nothing.
    const r = start();
    r.rows[0].querySelectorAll('.watch-card').slice(1).forEach((c) => c.remove());
    const one = r.rows[0].querySelector('.watch-card');
    const scroller = one.parentNode;
    one.remove();
    r.open();
    r.grid.bring(r.rows[1].querySelector('.watch-card'));
    const other = r.filled;
    const empty = told(r);
    scroller.appendChild(one);
    r.grid.bring(one);
    out.emptied = { empty, other, back: told(r) };
    return out;
  },

  // Picks under their dates, in the grid alone: the last of a date takes its line along and the first back has it again,
  // each in its place, whatever came after and in whatever order they come back.
  async datedOut() {
    const seq = (p) => p.grid.querySelector('.watch-grid').children.filter((n) => !n.classList.contains('watch-card--skel'))
      .map((n) => (n.getAttribute('data-group') || Number(n.querySelector('[data-title-ref]').getAttribute('data-title-ref').slice(6))));
    const start = (motion) => {
      const p = load({ motion });
      const dates = ['b1', 'b2', 'b3'];
      const list = [p.grid.group('b1', 'Picks of b1'), p.card(1), p.card(2), p.grid.group('b2', 'Picks of b2'), p.card(3)];
      p.grid.open(p.opts({ total: null, cards: list, next: 'n', fetchPage: () => Promise.resolve({ cards: [p.grid.group('b3', 'Picks of b3'), p.card(4)], next: null }),
        sort: { label: 'Jump to', value: 'b1', jump: true, options: dates.map((id) => ({ id, label: id })) } }));
      p.take = (n) => p.grid.take(p.doc.querySelectorAll('.watch-all .watch-card').find((c) => c.querySelector('[data-title-ref="movie-' + n + '"]')), '.watch-card__link');
      p.seq = () => seq({ grid: p.all() });
      p.jumps = () => { click(p.doc, p.doc.querySelector('#watch-all-sort')); const ids = p.doc.querySelectorAll('#watch-all-menu [data-value]').map((i) => i.getAttribute('data-value')); p.key('Escape'); return ids; };
      return p;
    };
    const p = start();
    const took = [p.take(2)];
    const one = p.seq();
    pressSort(p, 'b1'); // named until the date has gone
    took.push(p.take(1));
    const gone = { label: label(p) };
    took.push(p.take(3));
    gone.seq = p.seq();
    await settle();
    const loaded = { seq: p.seq(), jumps: p.jumps() };
    const sort = p.doc.querySelector('#watch-all-sort');
    const alone = (setup) => {
      setup();
      const before = p.doc.activeElement;
      const last = p.take(4);
      const now = { none: p.seq(), hidden: !!sort.hidden, menu: p.doc.querySelector('#watch-all-menu').classList.contains('is-open'), focus: p.focus(), kept: p.doc.activeElement === before };
      last.undo();
      return { ...now, back: !!sort.hidden };
    };
    const out = { one, gone, loaded,
      onButton: alone(() => sort.focus()),
      inMenu: alone(() => { click(p.doc, sort); p.doc.querySelector('#watch-all-menu .watch-keep-menu__item').focus(); }),
      elsewhere: alone(() => p.doc.querySelector('.watch-all__back').focus()) };
    took[2].undo();
    took[1].undo();
    out.first = p.seq();
    took[0].undo();
    out.back = { seq: p.seq(), jumps: p.jumps() };
    const q = start(true);
    q.finish();
    const line = q.doc.querySelector('.grid-group[data-group="b2"]');
    const undo = q.take(3).undo;
    out.fades = line.classList.contains('is-gone') && q.anims.some((a) => a.node === line && a.frames[1].opacity === 0);
    q.finish();
    out.faded = q.doc.body.contains(line);
    undo();
    q.finish();
    out.seen = [q.seq().slice(0, 5), line.hasAttribute('inert'), line.className, line.style.top + line.style.width + line.style.paddingTop];
    return out;
  },

  // A pick that had left its row as See all opened: back, it stands at its place under its date, whatever has been taken out of
  // the grid, and so of the row, since; then that comes back too, each with one line of its date. *more*: a page to come.
  async bringsDated() {
    const go = (row, gridOf, n, takes = [], more = {}) => {
      const p = load({ cards: row.map((r) => r[0]) });
      const scroller = p.rows[0].querySelector('.watch-row__scroll');
      const order = scroller.children;
      const cardIn = (nodes, id) => nodes.find((c) => c.querySelector('[data-title-ref="movie-' + id + '"]'));
      const places = {};
      const tag = (c, id, batch, at) => { c.setAttribute('data-batch', batch); c.setAttribute('data-place', at); };
      row.forEach(([id, batch], i) => { places[id] = row.slice(0, i).filter((r) => r[1] === batch).length; tag(order[i], id, batch, places[id]); });
      const line = (c) => p.grid.group(c.getAttribute('data-batch'), c.getAttribute('data-batch'));
      const make = (g) => { if (typeof g === 'string') return p.grid.group(g, g); const c = p.card(g[0]); tag(c, g[0], g[1], places[g[0]] || 0); return c; };
      const rowBack = (id) => { const c = cardIn(order, id); scroller.insertBefore(c, order.slice(order.indexOf(c)).find((o) => o !== c && o.parentNode === scroller) || null); return c; };
      cardIn(order, n).remove();
      p.grid.open(p.opts({ total: null, cards: gridOf.map(make), line, dates: () => ['a', 'b', 'c', 'd'], fetchPage: () => new Promise(() => {}), ...more }));
      const grid = p.doc.querySelector('.watch-all .watch-grid');
      const seq = () => grid.children.filter((c) => !c.classList.contains('watch-card--skel')).map((c) => c.getAttribute('data-group') || Number(c.querySelector('[data-title-ref]').getAttribute('data-title-ref').slice(6)));
      const taken = takes.map((id) => { const t = p.grid.take(cardIn(grid.children, id), '.watch-card__link'); cardIn(order, id).remove(); return () => { t.undo(); p.grid.bring(rowBack(id)); }; });
      const twin = p.grid.bring(rowBack(n));
      const out = [!!twin && twin === cardIn(grid.children, n), seq()];
      taken.forEach((undo) => undo());
      return out.concat([seq()]);
    };
    const row = [[1, 'a'], [2, 'a'], [3, 'a'], [4, 'b'], [5, 'c']];
    return {
      middle: go(row, ['a', [1, 'a'], [3, 'a'], 'b', [4, 'b'], 'c', [5, 'c'], 'd', [6, 'd']], 2),
      firstOfDate: go(row, ['a', [2, 'a'], [3, 'a'], 'b', [4, 'b']], 1),
      aloneFirst: go([[1, 'a'], [4, 'b']], ['b', [4, 'b']], 1),
      aloneBetween: go(row, ['a', [1, 'a'], [2, 'a'], [3, 'a'], 'c', [5, 'c']], 4),
      aloneLast: go(row, ['a', [1, 'a'], [2, 'a'], [3, 'a'], 'b', [4, 'b']], 5, [4], { next: 'd' }),
      aloneAfterTaken: go(row, ['a', [1, 'a'], [2, 'a'], [3, 'a'], 'c', [5, 'c']], 4, [3, 5]),
      dateTaken: go(row, ['a', [1, 'a'], [3, 'a'], 'b', [4, 'b']], 2, [1, 3]),
      dateTakenBack: go(row, ['a', [1, 'a'], [3, 'a'], 'b', [4, 'b']], 2, [3, 1]),
      onlyMarkOfDate: go([[9, 'a'], [1, 'b'], [2, 'b'], [5, 'c']], ['a', [9, 'a'], 'b', [2, 'b'], 'c', [5, 'c']], 1, [2]),
      afterAMark: go([[1, 'a'], [3, 'a'], [4, 'b'], [5, 'c']], ['a', [1, 'a'], [3, 'a'], 'c', [5, 'c']], 4, [3]),
      allOut: go([[1, 'a'], [2, 'b']], ['b', [2, 'b']], 1, [2]),
      toCome: go(row, ['a', [1, 'a'], [2, 'a'], [3, 'a']], 4, [], { next: 'b' }),
    };
  },

  // Two seasons of one show in the row: each is a card, and comes back as itself.
  async bringsSeasons() {
    const p = load({ cards: [1, 1, 2] });
    p.rows[0].querySelectorAll('.watch-card').forEach((c, i) => c.setAttribute('data-mark', i < 2 ? 'season/' + (i + 1) : 'movie/2'));
    const contents = () => { const cards = p.rows[0].querySelectorAll('.watch-card').map(p.grid.copy); return { cards, next: null, total: cards.length }; };
    const first = p.rows[0].querySelector('.watch-card');
    const next = first.nextElementSibling;
    first.remove();
    p.grid.open(p.opts({ contents, cards: contents().cards, total: 2 }));
    next.before(first);
    p.grid.bring(first);
    return { marks: p.doc.querySelectorAll('.watch-all .watch-card').map((c) => c.getAttribute('data-mark')), count: p.doc.querySelector('.watch-all__count').textContent };
  },

  // A grid on its way out is not filled again; one on its way in lands and is.
  async bringsWhileMoving() {
    const go = (closing) => {
      const p = load({ motion: true });
      const cards = () => p.rows[0].querySelectorAll('.watch-card').map(p.grid.copy);
      Node.prototype.getAnimations = function () { return p.anims.filter((a) => a.node === this && !a.cancelled && !a.done); };
      const card = p.rows[0].querySelectorAll('.watch-card')[1];
      const next = card.nextElementSibling;
      card.remove();
      p.grid.open(p.opts({ contents: () => ({ cards: cards(), next: null, total: 12 }), cards: cards(), total: 11 }));
      if (closing) { p.finish(); p.grid.close(); }
      next.before(card);
      p.grid.bring(card);
      p.finish();
      return { ...p.state(), n: p.refs().length, left: p.leftovers() };
    };
    return { opening: go(false), closing: go(true) };
  },

  // The only card dismissed takes its row along: with no row to go back to, the search has the focus.
  async closesWithNoRow() {
    const p = load();
    p.doc.body.appendChild(build(p.doc, ['input', { id: 'watch-search' }]));
    p.rows[0].querySelector('.watch-card__link').focus();
    p.grid.open(p.opts());
    p.rows[0].remove();
    p.doc.activeElement.blur();
    p.grid.close();
    return p.focus();
  },

  // All titles, opened by its address or from the search, has no row to go back to.
  async closesToTheLink() {
    const end = (opts, how) => {
      const p = load(opts);
      p.doc.body.appendChild(build(p.doc, ['main', { id: 'main-content', tabindex: '-1' }, ['input', { id: 'watch-search' }]]));
      if (opts.link) p.browse.appendChild(build(p.doc, ['p', {}, ['a', { class: 'watch-all-link', href: '/watch?all=library' }, 'All 60 titles']]));
      p.grid.open(p.opts({ id: 'library', row: () => null, ...how }));
      p.doc.activeElement.blur();
      p.grid.close();
      return p.focus();
    };
    const from = { fromSearch: true };
    return { mouse: end({ link: true }, {}), touch: end({ link: true, touch: true }, {}), search: end({ link: true }, from),
      searchTouch: end({ link: true, touch: true }, from), bare: end({}, {}), bareTouch: end({ touch: true }, {}) };
  },

  // With no heading on the screen to come from or go to, the title fades alone.
  async titleAlone() {
    const p = load({ motion: true });
    p.grid.open(p.opts({ id: 'library', title: 'All titles', row: () => null }));
    const title = p.doc.querySelector('.watch-all__title');
    const last = () => { const f = p.anims.filter((a) => a.node === title).pop().frames; return f.map((x) => Number(x.opacity)); };
    const opens = last();
    p.finish();
    p.grid.close();
    return { opens, closes: last() };
  },

  // Nothing to show yet: its place is held and the page asked for at once, and put in once nothing moves.
  async firstPage() {
    const p = load({ motion: true });
    const asked = [];
    let give;
    p.grid.open(p.opts({ id: 'library', row: () => null, cards: [], next: '', total: null, filter: { label: 'Show', value: '', href: () => '/watch', options: [], counts: null },
      sort: { label: 'Sort', value: 'a', options: [{ id: 'a', label: 'A' }] }, fetchPage: (next) => { asked.push(next); return new Promise((go) => { give = go; }); } }));
    const grid = p.doc.querySelector('.watch-all .watch-grid');
    const look = () => [grid.querySelectorAll('.watch-card--skel').length, grid.querySelectorAll('.watch-card').length];
    const count = p.doc.querySelector('.watch-all__count');
    const skel = grid.querySelector('.watch-card--skel');
    const out = { asked: asked.slice(), first: look(), count: count.textContent, lead: p.anims.filter((a) => a.node === skel).map((a) => a.opts.delay) };
    give({ cards: [1, 2, 3].map(p.card), next: null, total: 3 });
    await settle();
    out.moving = look();
    const before = p.anims.length;
    p.finish();
    await settle();
    out.rested = look();
    const card = grid.querySelector('.watch-card');
    const since = p.anims.slice(before).map((a) => a.node);
    out.boxFades = since.indexOf(card) >= 0;
    out.wordsFade = since.indexOf(card.querySelector('.watch-card__sub')) >= 0;
    return out;
  },

  async rowThatIsGone() {
    const p = load({ motion: true });
    p.grid.open(p.opts());
    p.finish();
    p.rows[0].remove();
    p.grid.close();
    p.finish();
    return { ...p.state(), left: p.leftovers(), focusInPage: p.doc.body.contains(p.doc.activeElement) };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
