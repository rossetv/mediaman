// Runs watch/core.js, keep.js, ratings.js, card-tools.js and marks.js against the fake DOM and prints one scenario's outcome as
// JSON. Usage: node watch_marks_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

// As in a browser, a node taken off the page loses the focus.
['remove', 'replaceWith'].forEach((name) => {
  const own = Node.prototype[name];
  Node.prototype[name] = function (...args) {
    own.apply(this, args);
    const doc = this.ownerDocument;
    if (this.contains(doc.activeElement) && !doc.body.contains(this)) doc.activeElement = doc.body;
  };
});

// As in a browser, a far card's buttons, hidden, take no focus.
const ownFocus = Node.prototype.focus;
Node.prototype.focus = function (...args) {
  const card = this.closest && this.closest('.watch-card');
  if (!(card && card.classList.contains('is-far') && this.closest('.card-tools'))) ownFocus.apply(this, args);
};

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});

function card(mark, title, sub, over = {}) {
  const at = /^(movie|show)\/(\d+)$/.exec(mark);
  const ref = over.ref || (at && at[1] + '-' + at[2]);
  const tmdb = over.tmdb && 'tmdb-movie-' + over.tmdb;
  const eye = mark || tmdb;
  return ['article', { class: 'watch-card', ...(tmdb ? { 'data-tmdb-ref': tmdb } : {}), ...(over.level ? { 'data-level': over.level } : {}), ...(eye ? { 'data-mark': eye } : {}), ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', ...(ref && !over.dismiss ? { 'data-title-ref': ref } : {}),
      ...(over.pick ? { 'data-pick-ref': over.pick, 'data-kind': 'movie', 'data-tmdb-id': over.tmdb || '' } : {}) },
      ['span', { class: 'watch-card__poster' }, ...(over.img ? [['img', { src: over.img }]] : []), ['span', { class: 'watch-card__title' }, title],
        ...(over.watched ? [['span', { class: 'watch-badge watch-badge--watched' }, 'Watched']] : []),
        ...(over.bar ? [['progress', { class: 'watch-bar' }]] : [])],
      ['span', { class: 'watch-card__sub' }, sub]],
    ['div', { class: 'card-tools' },
      ...(tmdb ? [['button', { class: 'card-tool card-tool--thumb', 'data-card-rate': '' }]] : []),
      ...(eye ? [['button', { class: 'card-tool card-tool--eye', 'data-mark-toggle': eye, 'aria-pressed': over.watched ? 'true' : 'false' }, ['svg', {}]]] : []),
      ...(over.dismiss ? [['button', { class: 'card-tool card-tool--x', 'data-watch-dismiss': over.dismiss, 'data-mark-name': title }, ['svg', {}]]] : [])]];
}

function row(title, cards) {
  return ['section', { class: 'watch-row', id: title }, ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ...cards]]];
}

function banner(ref, title, play) {
  return ['section', { class: 'watch-banner', ...(ref ? { 'data-dismiss-ref': ref } : {}) },
    ['h1', { class: 'watch-banner__title' }, title], ['a', { class: 'watch-btn--play', href: play }, 'Resume'],
    ...(ref ? [['button', { 'data-watch-dismiss': ref, 'data-mark-name': title }, 'Dismiss']] : [])];
}

const fillers = (n) => Array.from({ length: n }, (_, i) => card('movie/' + (1000 + i), 'Filler', 'Movie'));
const FILM = (over) => card('movie/1', 'Fieldwork', 'Movie · 31 min left', { bar: true, ...over });
const CONTINUE = [
  card('movie/1', 'Fieldwork', 'Movie · 31 min left', { dismiss: 'movie-1', bar: true }),
  card('episode/22', 'Brightwater', 'S1 · E2 · 12 min left', { dismiss: 'show-2', bar: true }),
  card('movie/3', 'Salt', 'Movie · 5 min left', { dismiss: 'movie-3', bar: true }),
];

function watchPage(parts) {
  return ['div', { class: 'watch-page', id: 'watch-page' }, ['input', { id: 'watch-search' }],
    ['div', { id: 'watch-browse' }, ...parts]];
}

/* *page*: the page shown; *next*: the page the server sends after a change (it sends *page* after an
   Undo), or a function of the fetch's number giving it, at once or as a promise; *api*: "METHOD url" to
   its answer, or a function giving it. */
function load(page, opts = {}) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, page));
  const calls = [];
  const toasts = [];
  const events = []; // every ref a mark was told by
  const told = [];
  const win = {};
  let nears = null; // what the cards on their way to the screen are told to
  const seers = []; // every IntersectionObserver made
  const fetched = [];
  doc.dispatchEvent = (e) => {
    if (e.type === 'mm:rating') doc.listeners['mm:rating'].forEach((fn) => fn(e));
    else if (e.type === 'mm:watch-continue') continues.push(doc.querySelectorAll('.watch-card').map((c) => c.getAttribute('data-dismiss-ref'))
      .filter((ref) => ref && ctx.MMWatch.marks.continues(ref)));
    else {
      events.push(...e.detail.refs);
      told.push(e.detail);
    }
  };
  const continues = []; // what stands in Continue watching, each time a title has left or joined it
  const gone = [];
  const held = []; // the timers of a rating just given, with opts.hold
  const watchers = new Set();
  const call = (method) => (url) => {
    calls.push(method + ' ' + url);
    let answer = (opts.api || {})[method + ' ' + url];
    if (typeof answer === 'function') answer = answer();
    if (answer instanceof Promise) return answer;
    if (typeof answer === 'number') return Promise.reject(Object.assign(new Error('http'), { status: answer }));
    return Promise.resolve(answer || { ok: true, changed: 1, watched: method === 'POST' || method === 'PUT', ratings: {} });
  };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Error, parseFloat, parseInt,
    setTimeout: (fn, ms) => { if (opts.clock) return opts.clock.set(fn, ms); if (opts.hold && ms === (opts.hold === true ? 650 : opts.hold)) return held.push(fn); if (opts.hold && ms === 0) { Promise.resolve().then(fn); return 0; } fn(); return 0; },
    clearTimeout(id) { if (opts.clock) opts.clock.drop(id); else if (id) held[id - 1] = null; },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingTop: '0', transform: opts.drawn || 'none' }),
    matchMedia: (query) => ({ matches: opts.motion && /reduced-motion/.test(query) ? false : /coarse/.test(query) ? !!opts.touch : !(opts.touch && /hover/.test(query)) }),
    requestAnimationFrame: (fn) => fn(),
    performance: { now: () => 1e6 + (opts.clock ? opts.clock.now : 0) },
    IntersectionObserver: class { constructor(fn, o) { this.fn = fn; this.margin = o && o.rootMargin; this.seen = new Set(); seers.push(this); nears = (cards) => fn(cards.filter((c) => this.seen.has(c)).map((target) => ({ target, isIntersecting: true })), this); } observe(c) { this.seen.add(c); } unobserve(c) { this.seen.delete(c); } disconnect() { this.seen.clear(); } },
    Number,
    Date,
    innerHeight: 900,
    innerWidth: 1280,
    location: { pathname: '/watch/show/2', search: '' },
    DOMParser: class { parseFromString(text) { const d = makeDocument(); d.body.appendChild(build(d, JSON.parse(text))); return d; } },
    fetch: (url) => {
      fetched.push(url);
      if (opts.onFetch) opts.onFetch(doc);
      if (!opts.next) return Promise.reject(new Error('down'));
      const undone = /\?undo=1$|^DELETE .*dismissed/.test(calls.filter((c) => !/^GET /.test(c)).pop());
      const sent = typeof opts.next === 'function' ? opts.next(fetched.length, calls) : undone ? page : opts.next;
      return Promise.resolve(sent).then((spec) => ({ ok: true, redirected: !!opts.redirected, text: () => Promise.resolve(JSON.stringify(spec)) }));
    },
    UIFeedback: {
      toast: (message, o) => { toasts.push({ message, variant: o.variant, undo: o.action && o.action.onClick, dialog: o.dialog ? o.dialog.id : null }); return message; },
      dismiss: (message) => gone.push(message),
    },
    MutationObserver: class { constructor(fn) { this.fn = fn; } observe() { watchers.add(this); } disconnect() { watchers.delete(this); } },
    MM: { api: { get: call('GET'), post: call('POST'), put: (url, body) => call('PUT')(url + (body ? ' ' + body.level : '')), delete: call('DELETE'), messageOf: (_e, f) => f } },
    addEventListener: (t, fn) => { (win[t] = win[t] || []).push(fn); },
    removeEventListener: (t, fn) => { win[t] = (win[t] || []).filter((f) => f !== fn); },
    scrollBy() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/keep.js', 'ratings.js', 'watch/card-tools.js', 'watch/marks.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const cards = (id) => doc.querySelectorAll('.watch-card').filter((c) => !id || c.closest('#' + id));
  const started = [];
  ctx.MMWatch.rows = { init: (viewport) => started.push(viewport.className) };
  const p = {
    gone, pass() { held.splice(0).forEach((fn) => fn && fn()); }, changed() { [...watchers].forEach((w) => w.fn([])); }, added(n) { [...watchers].forEach((w) => w.fn([{ addedNodes: [n], removedNodes: [] }])); },
    removed(n) { [...watchers].forEach((w) => w.fn([{ addedNodes: [], removedNodes: [n] }])); }, seers,
    ctx, win, told, near: (cards) => nears(cards), started, doc, calls, toasts, events, continues, fetched, cards,
    look: (id) => cards(id).map((c) => [c.getAttribute('data-mark'), !!c.querySelector('.watch-badge--watched'),
      c.querySelector('.watch-card__sub').textContent, !!c.querySelector('.watch-bar')]),
    marks: (id) => cards(id).map((c) => c.getAttribute('data-mark')),
    open(c) { const m = c.querySelector('[data-card-rate]'); m.focus(); click(doc, m); },
    async pick(c, act) {
      const b = c.querySelector(act === 'dismiss' ? '.card-tool--x' : '.card-tool--eye');
      b.focus();
      click(doc, b);
      await settle();
    },
    async undo(n = toasts.length - 1) { toasts[n].undo(); await settle(); },
    // Dismissed from somewhere else than its card, as the hover preview does.
    dismissed(ref) { click(doc, doc.body.appendChild(build(doc, ['button', { 'data-watch-dismiss': ref, 'data-mark-name': ref }]))); },
    banner: () => doc.querySelectorAll('.watch-banner__title').map((t) => t.textContent),
    focus: () => { const a = doc.activeElement; const c = a.closest('.watch-card'); return c ? c.getAttribute('data-mark') : a.className || a.tagName; },
  };
  return p;
}

function later() {
  let send;
  const answer = new Promise((r) => { send = r; });
  return { answer, send };
}

/* The Watch page whose Continue watching row holds *cards*, or has no row at all. */
function continuePage(cards, show) {
  return watchPage([['div', { class: 'watch-rows' }, ...(cards.length ? [row('cont', cards)] : []), row('lib', [card('movie/9', 'Other', 'Movie'), ...(show ? [card('show/2', 'Brightwater', 'TV')] : [])])]]);
}
function episodeCard(key) { return card('episode/' + key, 'Brightwater', 'S1 · E' + (key - 20), { dismiss: 'show-2' }); }

/* A show's own page whose episodes *watched* are watched, of 21, 22 and 23. */
function episodesPage(watched, play = '/watch/play/21') {
  return ['div', { id: 'watch-page' }, ['div', { class: 'watch-hero__info' }, ['a', { class: 'watch-btn--play', href: play }, play]],
    ['section', { class: 'watch-episodes' }, ...[21, 22, 23].map((n) => ['button', { 'data-mark-toggle': 'episode/' + n, 'data-mark-name': 'E' + n, 'aria-pressed': String(watched.includes(n)) }])]];
}
async function press(p, n) {
  const b = p.doc.querySelector('[data-mark-toggle="episode/' + n + '"]');
  click(p.doc, b);
  await settle();
  return b;
}
function pressed(p) { return p.doc.querySelectorAll('[data-mark-toggle]').map((b) => b.getAttribute('aria-pressed')); }

// A caption's words, each with the class of the span it is in.
function pieces(node, kind = '') {
  return node.childNodes.flatMap((n) => {
    if (n.nodeType === 3) return [[kind, n.textContent]];
    const own = n.className.replace('watch-card__', '');
    return own === 'unit' ? [[own, n.getAttribute('data-unit')]] : pieces(n, own);
  });
}

// Recommended, with the film of the library among the picks, and the same film in the library's own row.
const PICKS = (level) => [card('movie/8', 'Before', 'Movie', { pick: 'rec-8', tmdb: '80' }), card('movie/1', 'Fieldwork', 'Movie', { pick: 'rec-1', tmdb: '10', level }), card('movie/9', 'After', 'Movie', { pick: 'rec-9', tmdb: '90' })];
const RATE = 'PUT /api/watch/ratings/tmdb-movie-10 ';
const FILM_10 = { ok: true, media_type: 'movie', tmdb_id: 10, was: null };
function rated(api, opts) {
  let stored = null;
  const save = (level) => () => { const was = stored; stored = level; return { ...FILM_10, was }; };
  const own = { [RATE + '-2']: save(-2), [RATE + '1']: save(1), 'DELETE /api/watch/ratings/tmdb-movie-10': save(null), ...api };
  // Details names the same title by its library ref.
  Object.keys(own).forEach((k) => { own[k.replace('/tmdb-movie-10', '/movie-1').replace('=tmdb-movie-10', '=movie-1')] = own[k]; });
  return load(watchPage([row('picks', PICKS((opts || {}).level)), row('lib', [FILM({ tmdb: '10', level: (opts || {}).level })])]), { api: own, ...opts });
}

/* A click that goes no further up than where it is stopped. */
function clickThumb(doc, b) {
  let stopped = false;
  const e = { target: b, preventDefault() {}, stopPropagation() { stopped = true; } };
  for (let n = b; n && !stopped; n = n.parentNode) (n.listeners.click || []).forEach((fn) => fn(e));
  if (!stopped) doc.listeners.click.forEach((fn) => fn(e));
}

/* Timers that run as the scenario moves the time on. */
function clock() {
  const c = { now: 0, due: [], n: 0, set: (fn, ms) => { c.due.push({ fn, at: c.now + (ms || 0), id: ++c.n }); return c.n; }, drop: (id) => { c.due = c.due.filter((t) => t.id !== id); },
    async pass(ms) {
      c.now += ms;
      for (let t; (t = c.due.filter((d) => d.at <= c.now).sort((a, b) => a.at - b.at)[0]);) { c.drop(t.id); t.fn(); await settle(); }
    } };
  return c;
}
const send = (fns, e) => (fns || []).slice().forEach((fn) => fn({ preventDefault() {}, stopImmediatePropagation() {}, stopPropagation() {}, ...e }));

/* One film on a touch screen, its levels 50px each from 200px down, pressed by a finger. */
function finger() {
  const c = clock();
  const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })])]), { touch: true, clock: c, api: { [RATE + '2']: FILM_10, [RATE + '1']: FILM_10 } });
  const film = p.cards()[0];
  const button = film.querySelector('[data-card-rate]');
  const own = Node.prototype.getBoundingClientRect;
  Node.prototype.getBoundingClientRect = function () {
    const i = this.classList.contains('rate__b') ? this.parentNode.querySelectorAll('.rate__b').indexOf(this) : -1;
    return i < 0 ? own.call(this) : { top: 200 + i * 50, bottom: 250 + i * 50, left: 0, right: 40, width: 40, height: 50 };
  };
  Node.prototype.click = function () { clickThumb(p.doc, this); };
  const at = (y) => ({ pointerType: 'touch', pointerId: 1, isPrimary: true, clientX: 20, clientY: y, target: button });
  return { p, c, film, button, isOpen: () => film.classList.contains('is-lifted'), down: () => send(p.doc.listeners.pointerdown, at(400)), move: (y) => send(p.win.pointermove, { type: 'pointermove', ...at(y) }),
    up: (type) => send(p.win['pointer' + (type || 'up')], { type: 'pointer' + (type || 'up'), ...at(400) }), tap: () => click(p.doc, button, { detail: 1 }) };
}

/* A level, the worst first. */
async function thumb(p, c, i) {
  p.open(c);
  clickThumb(p.doc, c.querySelectorAll('.card-over .rate__b')[3 - i]);
  await settle();
}

const scenarios = {
  // With motion, a mark in Continue watching is looked at before its card goes, or moves on where it stands.
  async markSeen() {
    const go = async (i, next) => {
      const c = clock();
      const cards = [CONTINUE[0], card('episode/22', 'Brightwater', 'S1 · E2 · 12 min left', { dismiss: 'show-2', bar: true, img: '/two' }), CONTINUE[2]];
      const p = load(watchPage([row('cont', cards)]), { clock: c, motion: true, next: (n, calls) => watchPage([row('cont', /undo=1|^DELETE/.test(calls[calls.length - 1]) ? cards : next)]) });
      const node = p.cards()[i];
      await p.pick(node, 'mark');
      return { p, c, node, shown: () => [p.marks(), !!node.querySelector('.card-lights'), !!node.querySelector('.card-barend'), !!node.querySelector('.watch-bar')] };
    };
    const film = await go(0, CONTINUE.slice(1));
    const out = { film: [film.shown()] };
    await film.c.pass(599);
    out.film.push(film.p.marks());
    await film.c.pass(1);
    out.film.push(film.p.marks());
    await film.p.undo();
    out.film.push([film.p.marks(), !!film.node.querySelector('.watch-bar')]);

    const early = await go(0, CONTINUE.slice(1));
    await early.c.pass(100);
    await early.p.undo();
    await early.c.pass(2000);
    out.early = [early.p.marks(), !!early.node.querySelector('.watch-bar')];

    const again = await go(0, CONTINUE.slice(1));
    await again.c.pass(100);
    await again.p.pick(again.node, 'mark');
    await again.c.pass(2000);
    out.again = [again.p.calls, again.p.marks()];

    const moved = (over) => [CONTINUE[0], card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2', img: '/two', ...over }), CONTINUE[2]];
    const look = (e) => [e.p.marks()[1], e.p.cards()[1] === e.node, e.node.querySelector('.watch-card__sub').textContent, e.node.querySelector('.card-tool--eye').getAttribute('aria-pressed'), !!e.node.querySelector('.watch-bar')];
    const episode = await go(1, moved());
    const img = episode.node.querySelector('img');
    out.episode = [episode.shown().slice(1)];
    await episode.c.pass(599);
    out.episode.push(look(episode));
    await episode.c.pass(1);
    out.episode.push(look(episode), [episode.node.querySelector('img') === img, episode.p.focus(), episode.node.querySelector('.card-tool--eye') === episode.p.doc.activeElement]);
    await episode.p.undo();
    await episode.c.pass(2000);
    out.episode.push(look(episode));

    const other = await go(1, moved({ img: '/three' }));
    await other.c.pass(600);
    out.other = [other.p.marks()[1], other.p.cards()[1] === other.node];
    return out;
  },

  // A mark whose answer took so long that its eye had shut, and one answered at once.
  async lateEye() {
    const go = async (drawn) => {
      const p = load(watchPage([row('lib', [FILM()])]), { drawn });
      await p.pick(p.cards()[0], 'mark');
      const eye = p.cards()[0].querySelector('.card-tool--eye');
      return [eye.classList.contains('is-blink'), Number(eye.style['--shut']).toFixed(2)];
    };
    return { shut: await go('matrix(1, 0, 0, 0.06, 0, 0)'), open: await go('none'), raised: await go('matrix(1.18, 0, 0, 1.18, 0, 0)') };
  },

  // The levels fold after a love, whose ring and sparks stand past their edge.
  async foldAfterLove() {
    const c = clock();
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })])]), { clock: c, motion: true, api: { [RATE + '2']: FILM_10 } });
    const film = p.cards()[0];
    await thumb(p, film, 3);
    const pops = () => film.querySelectorAll('.card-over .pop').length;
    const before = [pops(), p.doc.getElementById('lib').classList.contains('has-lifted'), film.querySelector('[data-card-rate]').className];
    await c.pass(560);
    return { before, folding: [film.querySelectorAll('.card-over').length, pops(), p.doc.getElementById('lib').classList.contains('has-lifted')] };
  },

  // How long a pick given a strong no stays, with motion and without.
  async noStays() {
    const go = async (motion, at) => {
      const c = clock();
      const p = rated({}, { clock: c, motion });
      await thumb(p, p.cards('picks')[1], 0);
      await c.pass(at - 1);
      const before = p.marks('picks');
      await c.pass(1);
      return [before, p.marks('picks')];
    };
    return { moving: await go(true, 900), still: await go(false, 650) };
  },

  // A card that leaves its row and one that comes back are not cut at their edges; a row that goes is.
  async leavesWhole() {
    const styles = [];
    Node.prototype.animate = function () { styles.push([this.tagName, this.style.overflow || '']); const a = { cancel() {} }; Promise.resolve().then(() => a.onfinish && a.onfinish()); return a; };
    const go = async (cards) => {
      const p = load(watchPage([row('cont', cards)]), { motion: true, next: watchPage([row('cont', cards.slice(1))]) });
      await p.pick(p.cards()[0], 'dismiss');
      await p.undo();
    };
    await go(CONTINUE);
    const card = styles.splice(0);
    await go(CONTINUE.slice(0, 1));
    delete Node.prototype.animate;
    return { card, row: styles.slice(0, 1) };
  },

  // Pressed in a touch screen's panel, with an answer that takes its time, and one that refuses.
  async panelWaits() {
    const go = async (answer) => {
      const first = later();
      const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })])]), { touch: true, api: { 'POST /api/watch/watched/movie/1': () => first.answer } });
      const film = p.cards()[0];
      click(p.doc, film.querySelector('[data-card-rate]'), { detail: 1 });
      const eye = film.querySelector('.card-over__b');
      click(p.doc, eye);
      await settle();
      const waiting = [film.classList.contains('is-lifted'), eye.getAttribute('aria-busy')];
      first.send(answer());
      await settle();
      return [waiting, film.classList.contains('is-lifted'), p.toasts.length];
    };
    return { done: await go(() => ({ ok: true, changed: 1 })), refused: await go(() => Promise.reject(Object.assign(new Error('x'), { status: 503 }))) };
  },

  // An Undo that is refused after its card has left Continue watching.
  async refusedFocus() {
    const go = async (act, cards, api) => {
      const p = load(watchPage([row('cont', cards)]), { next: watchPage([row('cont', cards.slice(1))]), api });
      await p.pick(p.cards()[0], act);
      p.doc.activeElement = p.doc.body;
      await p.undo();
      return [p.marks(), p.focus(), p.toasts[p.toasts.length - 1].variant];
    };
    const mark = { 'DELETE /api/watch/watched/movie/1?undo=1': 409 };
    return { mark: await go('mark', CONTINUE, mark), dismiss: await go('dismiss', CONTINUE, { 'DELETE /api/watch/dismissed/movie-1': 409 }), last: (await go('mark', CONTINUE.slice(0, 1), mark))[1] };
  },

  // A finger on a screen that has a mouse too, on a button that is not in sight, and on one that is.
  async unseenTool() {
    const press = async (opacity) => {
      const p = load(watchPage([row('lib', [FILM()])]));
      p.ctx.getComputedStyle = () => ({ opacity: String(opacity), getPropertyValue: () => '', transform: 'none' });
      const eye = p.cards()[0].querySelector('.card-tool--eye');
      send(p.doc.listeners.pointerdown, { target: eye, pointerType: 'touch' });
      const e = { target: eye, stopped: false, preventDefault() {}, stopImmediatePropagation() { this.stopped = true; } };
      p.doc.listeners.click.forEach((fn) => { if (!e.stopped) fn(e); });
      await settle();
      return p.calls;
    };
    return { hidden: await press(0), shown: await press(1) };
  },

  // Where the screen has a mouse too: a finger, its moves, the mouse's move, a pen; then a touch screen alone.
  async byTouch() {
    const go = (opts) => {
      const p = load(watchPage([row('lib', [FILM()])]), opts);
      const eye = p.cards()[0].querySelector('.card-tool--eye');
      const by = () => p.doc.documentElement.classList.contains('is-by-touch');
      const out = [];
      send(p.doc.listeners.pointerdown, { target: eye, pointerType: 'touch', pointerId: 1 });
      // A press on buttons the mouse had shown keeps them shown, as the mouse's :hover no longer does.
      out.push([by(), p.cards()[0].classList.contains('is-touched')]);
      send(p.doc.listeners.pointermove, { target: eye, pointerType: 'touch', pointerId: 1 });
      out.push(by());
      send(p.doc.listeners.pointermove, { target: p.doc.body, pointerType: 'mouse', pointerId: 2 });
      out.push(by());
      send(p.doc.listeners.pointerdown, { target: eye, pointerType: 'pen', pointerId: 3 });
      return out.concat([by()]);
    };
    return [go(), go({ touch: true })];
  },

  // A finger's drag on a button not in sight, that the browser takes for a scroll, then Enter on it.
  async unseenThenKey() {
    const press = async (between) => {
      const p = load(watchPage([row('lib', [FILM()])]));
      p.ctx.getComputedStyle = () => ({ opacity: '0', getPropertyValue: () => '', transform: 'none' });
      const eye = p.cards()[0].querySelector('.card-tool--eye');
      send(p.doc.listeners.pointerdown, { target: eye, pointerType: 'touch', pointerId: 1 });
      between(p, eye);
      eye.focus();
      const e = { target: eye, detail: 0, stopped: false, preventDefault() {}, stopImmediatePropagation() { this.stopped = true; } };
      p.doc.listeners.click.forEach((fn) => { if (!e.stopped) fn(e); });
      await settle();
      return p.calls;
    };
    return {
      cancelled: await press((p, eye) => { send(p.win.pointercancel, { type: 'pointercancel', target: eye, pointerId: 1 }); send(p.doc.listeners.pointercancel, { type: 'pointercancel', target: eye, pointerId: 1 }); }),
      key: await press((p, eye) => send(p.doc.listeners.keydown, { key: 'Enter', target: eye })),
    };
  },

  // A finger on a screen that has a mouse too, where a touch leaves no hover: its first press on a
  // card's hidden buttons shows them, its next acts, and a press elsewhere or a scroll hides them.
  async touchShows() {
    const go = (opts) => {
      const c = clock();
      const p = load(watchPage([row('lib', [FILM({ tmdb: '10' }), card('movie/9', 'Other', 'Movie')])]), { clock: c, ...opts });
      p.ctx.getComputedStyle = () => ({ opacity: '0', getPropertyValue: () => '', transform: 'none' });
      const press = async (target, pointerType = 'touch') => {
        send(p.doc.listeners.pointerdown, { target, pointerType, pointerId: 1, isPrimary: true, clientY: 0 });
        const e = { target, detail: 1, stopped: false, preventDefault() {}, stopImmediatePropagation() { this.stopped = true; } };
        p.doc.listeners.click.forEach((fn) => { if (!e.stopped) fn(e); });
        await c.pass(0);
      };
      return { p, c, press, cards: p.cards(), shown: () => p.cards().map((n) => n.classList.contains('is-touched')) };
    };
    const { p, c, press, cards: [film, other], shown } = go();
    const eye = film.querySelector('.card-tool--eye');
    const out = [];
    await press(eye);
    out.push([p.calls.slice(), shown()]);
    await press(eye);
    out.push([p.calls.slice(), shown()]);
    await press(p.doc.body, 'mouse');
    out.push(shown());
    // A finger held on a thumb not in sight does not open its levels.
    const thumb = film.querySelector('[data-card-rate]');
    send(p.doc.listeners.pointerdown, { target: thumb, pointerType: 'touch', pointerId: 2, isPrimary: true, clientY: 0 });
    await c.pass(400);
    out.push([film.classList.contains('is-lifted'), shown()]);
    send(p.win.pointerup, { type: 'pointerup', target: thumb, pointerId: 2 });
    await press(other.querySelector('.card-tool--eye'));
    out.push(shown());
    send(p.doc.listeners.scroll, { target: p.doc });
    out.push(shown());
    // A touch screen alone keeps its own way.
    const phone = go({ touch: true });
    await phone.press(phone.cards[1].querySelector('.card-tool--eye'));
    out.push([phone.p.calls.slice(), phone.shown()]);
    return out;
  },

  // What a screen reader is told of the levels.
  async levelsSaid() {
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10', level: 1 })])]));
    const film = p.cards()[0];
    p.open(film);
    return film.querySelectorAll('.card-over .rate__b').map((b) => {
      const by = b.getAttribute('aria-describedby');
      const hint = by && p.doc.getElementById(by);
      return [b.getAttribute('aria-checked'), hint ? [hint.textContent, hint.hidden] : null];
    });
  },

  // Badges that come after the others of their poster.
  async badgeOrder() {
    const names = (poster) => poster.querySelectorAll('.watch-badge').map((b) => b.className.split('--')[1]);
    const go = (kinds) => {
      const p = load(watchPage([row('lib', [FILM({ watched: true })])]));
      const poster = p.cards()[0].querySelector('.watch-card__poster');
      kinds.forEach((kind) => p.added(poster.appendChild(build(p.doc, ['span', { class: 'watch-badge watch-badge--' + kind }]))));
      return names(poster);
    };
    return [go(['state']), go(['leaving']), go(['state', 'leaving']), go(['new']), go(['unwatched'])];
  },

  // What a card says of the keyboard's focus and of the mouse, and no copy of it or other card does.
  async cardStates() {
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10' }), card('movie/9', 'Other', 'Movie')])]));
    const [film, other] = p.cards();
    const has = (name) => [film, other].map((c) => c.classList.contains(name));
    const eye = film.querySelector('.card-tool--eye');
    const out = [];
    eye.focus();
    send(p.doc.listeners.focusin, { target: eye });
    out.push(has('has-focus'));
    send(p.doc.listeners.focusout, { target: eye, relatedTarget: film.querySelector('.watch-card__link') });
    out.push(has('has-focus'));
    const away = other.querySelector('.watch-card__link');
    send(p.doc.listeners.focusout, { target: eye, relatedTarget: away });
    out.push(has('has-focus'));
    away.focus();
    send(p.doc.listeners.focusin, { target: away });
    out.push(has('has-focus'));
    send(p.doc.listeners.pointerover, { target: eye });
    out.push(has('is-on-tools'));
    send(p.doc.listeners.pointerover, { target: film.querySelector('.watch-card__link') });
    out.push(has('is-on-tools'));
    send(p.doc.listeners.pointerover, { target: eye });
    send(p.doc.listeners.pointerout, { target: eye, relatedTarget: null });
    out.push(has('is-on-tools'));
    return out;
  },

  // A rating from a card: a level, then the same one taken back.
  async menuRating() {
    const p = rated();
    const open = () => p.cards().some((c) => c.classList.contains('is-lifted'));
    await thumb(p, p.cards('lib')[0], 2);
    const first = [open(), p.calls.slice(), p.focus()];
    const levels = p.cards().map((c) => c.getAttribute('data-level'));
    await thumb(p, p.cards('lib')[0], 2);
    return { first, levels, back: [open(), p.calls.slice(1), p.cards().map((c) => c.getAttribute('data-level'))] };
  },

  // A pick that is not in the library, and one that is: rated and marked from its card.
  async pickMenu() {
    const p = load(watchPage([row('picks', [card('', 'Wanted', 'Movie · 2024', { ref: 'rec-5', pick: 'rec-5', tmdb: '50' }), ...PICKS().slice(0, 1)]), row('found', [card('', 'Wanted', 'Movie · 2024', { ref: 'tmdb-movie-50', tmdb: '50' })])]), {
      api: { 'PUT /api/watch/ratings/tmdb-movie-50 1': { ok: true, media_type: 'movie', tmdb_id: 50 }, 'PUT /api/watch/seen/tmdb-movie-50': { ok: true, watched: true, changed: 1 } } });
    const [wanted, held, found] = p.cards();
    await thumb(p, wanted, 2);
    await p.pick(wanted, 'mark');
    const marked = [p.calls.slice(), p.look(), found.getAttribute('data-level'), p.fetched.length, p.events.slice()];
    await p.undo();
    const undone = p.look('found');
    const calls = p.calls.slice(2);
    await p.pick(wanted, 'mark');
    await p.pick(wanted, 'mark');
    const pages = p.fetched.length;
    await p.pick(held, 'mark');
    return { marked, undone, calls: calls.concat(p.calls.slice(5)), pages };
  },

  // A mark of a title the library has come to hold, one TMDB has not, and one whose seasons are not known.
  async seenRefused() {
    const go = async (status, error, data) => {
      const p = load(watchPage([row('found', [card('', 'Wanted', 'Movie', { ref: 'tmdb-movie-50', tmdb: '50' }), card('', 'Wanted', 'Movie', { ref: 'rec-5', pick: 'rec-5', tmdb: '50' })])]), {
        api: { 'PUT /api/watch/seen/tmdb-movie-50': () => Promise.reject(Object.assign(new Error('x'), { status, error, data })) } });
      await p.pick(p.cards()[0], 'mark');
      return { calls: p.calls, look: p.look(), toggles: p.doc.querySelectorAll('[data-mark-toggle]').map((b) => [b.getAttribute('data-mark-toggle'), b.hidden]), toasts: p.toasts.map((t) => [t.message, !!t.undo]) };
    };
    return { held: await go(409, 'in_library', { mark_kind: 'movie', mark_key: '5' }), odd: await go(409, 'in_library', { mark_kind: 'movie', mark_key: 'x/../9' }), none: await go(409, 'no_tmdb_id'), seasons: await go(503, 'seasons_unknown') };
  },

  // A card that comes late: drawn from an answer asked for before the mark; and long after it.
  async lateCard() {
    const p = load(watchPage([row('lib', [card('', 'Wanted', 'Movie', { ref: 'tmdb-movie-50', tmdb: '50' })])]), { api: { 'PUT /api/watch/ratings/tmdb-movie-50 2': { ok: true, media_type: 'movie', tmdb_id: 50 } } });
    await thumb(p, p.cards()[0], 3);
    await p.pick(p.cards()[0], 'mark');
    const bring = (after) => {
      const own = p.ctx.Date.now;
      p.ctx.Date = { now: () => Date.now() + after };
      const late = p.doc.querySelector('.watch-row__scroll').appendChild(build(p.doc, card('', 'Wanted', 'Movie', { ref: 'rec-5', pick: 'rec-5', tmdb: '50' })));
      p.added(late);
      p.ctx.Date = { now: own };
      return [late.getAttribute('data-level'), !!late.querySelector('.watch-badge--watched'), late.querySelector('.card-tool--eye').getAttribute('aria-pressed')];
    };
    const soon = bring(1000);
    const long = bring(60000);
    await p.undo();
    return { soon, long, undone: p.look().map((l) => l[1]) };
  },

  // A part of a show is marked after the whole, with its Undo still there; and another title.
  async partAfterWhole() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV'), card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' }), FILM()])]));
    await p.pick(p.cards()[0], 'mark');
    await p.pick(p.cards()[2], 'mark');
    const before = p.gone.slice();
    await p.pick(p.cards()[1], 'mark');
    return { before, after: p.gone };
  },

  // A mark of a title that its refs name in the library and outside it.
  async toldOnce() {
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })]), row('picks', [card('', 'Fieldwork', 'Movie', { ref: 'tmdb-movie-10', tmdb: '10' })])]));
    await p.pick(p.cards('lib')[0], 'mark');
    await p.pick(p.cards('picks')[0], 'mark');
    return p.told;
  },

  // Marked though it was marked so already, as another tab may have done; and for the first time.
  async seenAgain() {
    const go = async (changed) => {
      const p = load(watchPage([row('found', [card('', 'Wanted', 'Movie', { ref: 'tmdb-movie-50', tmdb: '50' })])]), { api: { 'PUT /api/watch/seen/tmdb-movie-50': { ok: true, watched: true, changed } } });
      await p.pick(p.cards()[0], 'mark');
      return !!p.toasts[0].undo;
    };
    return [await go(0), await go(1)];
  },

  // A card drawn from an answer from before an Undo, and one of a show a part of which was then marked.
  async lateAfter() {
    const bring = (p, tmdb, watched) => {
      const late = p.doc.querySelector('.watch-row__scroll').appendChild(build(p.doc, card('', 'Late', 'Movie', { ref: 'rec-5', pick: 'rec-5', tmdb, watched })));
      p.added(late);
      return !!late.querySelector('.watch-badge--watched');
    };
    const film = load(watchPage([row('lib', [card('', 'Wanted', 'Movie', { ref: 'tmdb-movie-50', tmdb: '50' })])]));
    await film.pick(film.cards()[0], 'mark');
    await film.undo();
    const show = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV', { tmdb: '20' }), card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]));
    await show.pick(show.cards()[0], 'mark');
    const whole = bring(show, '20', false);
    await show.pick(show.cards()[1], 'mark');
    return { undone: bring(film, '50', true), whole, part: bring(show, '20', false) };
  },

  // A finger down on the thumb, over the levels and up, then the click a browser sends after it.
  async slides() {
    const f = finger();
    f.down();
    f.move(380);
    const open = f.isOpen();
    f.move(210);
    await f.c.pass(700);
    f.up();
    f.tap();
    const sent = [f.p.calls.slice(), f.isOpen()];
    await f.c.pass(600);
    const given = [f.film.getAttribute('data-level'), f.isOpen()];
    f.tap();
    return { open, sent, given, again: f.isOpen() };
  },

  // The scroller takes the finger over a level.
  async slideTaken() {
    const f = finger();
    f.down();
    f.move(210);
    f.up('cancel');
    return f.p.calls;
  },

  // A finger that stays down; let go, the levels stay to be pressed. And one that goes at once.
  async holds() {
    const f = finger();
    f.down();
    await f.c.pass(210);
    const early = f.isOpen();
    await f.c.pass(500);
    const held = f.isOpen();
    f.up();
    f.tap();
    const short = finger();
    short.down();
    short.up();
    await short.c.pass(500);
    const left = short.isOpen();
    short.tap();
    return { early, held, stays: f.isOpen(), calls: f.p.calls, left, tapped: short.isOpen() };
  },

  // What folds the levels on a mouse screen, where the poster has no other buttons: Tab, and a press off the card.
  async folds() {
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10' }), card('movie/4', 'Other', 'Movie')])]));
    const rects = Node.prototype.getClientRects;
    Node.prototype.getClientRects = function () { return this.closest('.card-over__col') ? [] : rects.call(this); };
    const [film, other] = p.cards();
    const button = film.querySelector('[data-card-rate]');
    const isOpen = () => film.classList.contains('is-lifted');
    click(p.doc, button, { detail: 0 });
    const opened = isOpen();
    send(p.doc.listeners.keydown, { key: 'Tab', target: p.doc.activeElement });
    const tab = [isOpen(), p.doc.activeElement === button];
    p.open(film);
    send(p.doc.listeners.pointerdown, { target: film.querySelector('.card-over__scrim') });
    const on = isOpen();
    send(p.doc.listeners.pointerdown, { target: other });
    return { opened, tab, on, off: isOpen() };
  },

  // The keys in a touch screen's panel: from the levels to the buttons beside them, and back.
  async panelKeys() {
    const p = load(watchPage([row('cont', [card('movie/1', 'Fieldwork', 'Movie', { dismiss: 'movie-1', tmdb: '10' })])]), { touch: true });
    const button = p.doc.querySelector('[data-card-rate]');
    const start = () => { if (!p.cards()[0].classList.contains('is-lifted')) click(p.doc, button, { detail: 0 }); };
    const at = () => { const a = p.doc.activeElement; return a === button ? 'thumb' : a.hasAttribute('data-watch-dismiss') ? 'x' : a.hasAttribute('data-mark-toggle') ? 'eye' : a.getAttribute('aria-label'); };
    const go = (...keys) => keys.map((k) => {
      let own = true;
      send(p.doc.listeners.keydown, { key: k.replace('+', ''), shiftKey: k[0] === '+', target: p.doc.activeElement, preventDefault() { own = false; } });
      return at() + (own ? '' : '!');
    });
    start();
    const arrows = go('ArrowLeft', 'ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowRight', 'ArrowLeft', 'ArrowLeft');
    const tabs = go('Tab', '+Tab', 'Tab', 'Tab', '+Tab', 'Tab', 'Tab');
    start();
    const back = go('+Tab');
    start();
    const eye = go('ArrowLeft')[0];
    click(p.doc, p.doc.activeElement, { detail: 0 });
    await settle();
    return { arrows, tabs, back, eye, calls: p.calls };
  },

  // The More menu stands over a touch screen's panel: Escape is its own before the panel's.
  async panelMenu() {
    const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })])]), { touch: true });
    const film = p.cards()[0];
    p.open(film);
    const inMenu = p.doc.body.appendChild(build(p.doc, ['div', { class: 'watch-keep-menu' }, ['button', {}]])).children[0];
    const out = [inMenu, film.querySelector('.card-over__b')].map((target) => {
      send(p.doc.listeners.keydown, { key: 'Escape', target });
      return film.classList.contains('is-lifted');
    });
    return out;
  },

  // A card whose thumb goes, as its title cannot be rated, then its eye; a card the arrow keys alone reach.
  async toolStop() {
    const p = load(watchPage([row('lib', [card('', 'Wanted', 'Movie', { ref: 'tmdb-movie-10', tmdb: '10' }), card('', 'Wanted', 'Movie', { ref: 'rec-1', pick: 'rec-1', tmdb: '10' })])]),
      { hold: true, api: { 'GET /api/watch/ratings?refs=tmdb-movie-10': () => ({ ratings: { 'tmdb-movie-10': { level: null, rateable: false } } }), [RATE + '1']: 500, 'PUT /api/watch/seen/tmdb-movie-10': () => Promise.reject(Object.assign(new Error('x'), { status: 409, error: 'no_tmdb_id' })) } });
    const tools = p.cards().map((c) => c.querySelector('.card-tools'));
    tools[0].appendChild(build(p.doc, ['button', { class: 'card-tool card-tool--more' }, ['svg', {}]]));
    tools[0].children.slice(1).concat(tools[1].children).forEach((b) => b.setAttribute('tabindex', '-1'));
    const stops = () => tools.map((t) => t.children.filter((b) => b.getAttribute('tabindex') !== '-1').map((b) => b.className.split('--')[1]).join());
    const out = [stops()];
    await thumb(p, p.cards()[0], 2);
    out.push(stops());
    await p.pick(p.cards()[0], 'mark');
    return out.concat([stops()]);
  },

  // Cards a screen or more out of sight, as they come near, leave and are taken off the page.
  async farCards() {
    const p = load(watchPage([['div', { class: 'watch-grid' }, FILM(), card('movie/9', 'Other', 'Movie'), ...fillers(499)], row('lib', [card('movie/8', 'Before', 'Movie')])]));
    const [film, other] = p.cards();
    const inRow = p.cards('lib')[0];
    const seer = p.seers.filter((s) => s.margin)[0];
    const far = () => [film, other].map((c) => c.classList.contains('is-far'));
    const out = [seer.margin, [film, other, inRow].map((c) => seer.seen.has(c))];
    seer.fn([{ target: film, isIntersecting: false }, { target: other, isIntersecting: true }], seer);
    out.push(far());
    seer.fn([{ target: film, isIntersecting: true }], seer);
    out.push(far());
    film.remove();
    p.removed(film);
    out.push([film, other].map((c) => seer.seen.has(c)));
    return out;
  },

  // A grid of 500 cards, where none says it is far, then one more is loaded, then a filter leaves fewer.
  async fewFarCards() {
    const p = load(watchPage([['div', { class: 'watch-grid' }, FILM(), ...fillers(499)]]));
    const film = p.cards()[0];
    const seer = p.seers.filter((s) => s.margin)[0];
    seer.fn([{ target: film, isIntersecting: false }], seer);
    const out = [film.classList.contains('is-far')];
    const grid = p.doc.querySelector('.watch-grid');
    const late = grid.appendChild(build(p.doc, card('movie/9', 'Other', 'Movie')));
    p.added(late);
    out.push(film.classList.contains('is-far'));
    late.remove();
    p.removed(late);
    return out.concat([film.classList.contains('is-far')]);
  },

  // A grid's film marked by the keyboard, scrolled far off, then its toast's Undo pressed.
  async farCardRefocused() {
    const p = load(watchPage([['div', { class: 'watch-grid' }, FILM(), ...fillers(500)]]));
    const film = p.cards()[0];
    const eye = film.querySelector('.card-tool--eye');
    const seer = p.seers.filter((s) => s.margin)[0];
    await p.pick(film, 'mark');
    send(p.doc.listeners.focusin, { target: eye });
    seer.fn([{ target: film, isIntersecting: false }], seer);
    const out = [film.classList.contains('is-far')];
    const toast = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-toast' }, ['button', {}, 'Undo']])).children[0];
    toast.focus();
    send(p.doc.listeners.focusout, { target: eye, relatedTarget: toast });
    send(p.doc.listeners.focusin, { target: toast });
    out.push(film.classList.contains('is-far'));
    await p.undo();
    return out.concat([p.doc.activeElement === film.querySelector('.card-tool--eye')]);
  },

  // The server says a title cannot be rated while its thumb, then one of its levels, has the focus.
  async thumbGoes() {
    const go = async (focus) => {
      const p = load(watchPage([row('lib', [FILM({ tmdb: '10' })])]),
        { hold: true, api: { 'GET /api/watch/ratings?refs=tmdb-movie-10': () => ({ ratings: { 'tmdb-movie-10': { level: null, rateable: false } } }) } });
      const film = p.cards()[0];
      focus(p, film);
      const before = p.doc.activeElement.className;
      // A copy drawn from a newer answer has the title asked about again.
      const copy = film.parentNode.appendChild(build(p.doc, FILM({ tmdb: '10', level: 1 })));
      p.added(copy);
      await settle();
      const a = p.doc.activeElement;
      return [before, film.querySelector('[data-card-rate]').hidden, a === film.querySelector('.card-tool--eye') ? 'eye' : a.className || a.tagName];
    };
    return [
      await go((p, film) => film.querySelector('[data-card-rate]').focus()),
      await go((p, film) => { const t = film.querySelector('[data-card-rate]'); t.focus(); click(p.doc, t, { detail: 0 }); }),
    ];
  },

  // A far card taken off the page, to be put back elsewhere.
  async farCardTaken() {
    const p = load(watchPage([['div', { class: 'watch-grid' }, FILM(), ...fillers(500)]]));
    const film = p.cards()[0];
    const seer = p.seers.filter((s) => s.margin)[0];
    seer.fn([{ target: film, isIntersecting: false }], seer);
    const before = film.classList.contains('is-far');
    film.remove();
    p.removed(film);
    return [before, film.classList.contains('is-far')];
  },

  // A page the browser kept, with a card far off it: its level is read once it nears, as a copy of it made since.
  async keptPage() {
    const reads = [];
    const p = load(watchPage([row('lib', [10, 20, 30].map((n) => card('movie/' + n, 'Film', 'Movie', { tmdb: String(n), level: 1 })))]), { hold: true,
      api: Object.fromEntries([['tmdb-movie-10,tmdb-movie-20', [10, 20]], ['tmdb-movie-30', [30]]].map(([refs, ids]) => ['GET /api/watch/ratings?refs=' + encodeURIComponent(refs),
        () => { reads.push(refs); return { ratings: Object.fromEntries(ids.map((n) => ['tmdb-movie-' + n, { level: -1, rateable: true }])) }; }])) });
    const [near, next, far] = p.cards();
    send(p.win.pageshow, { persisted: false });
    send(p.win.pageshow, { persisted: true });
    await settle();
    const unasked = reads.length;
    let looked = 0;
    const find = p.doc.querySelectorAll;
    p.doc.querySelectorAll = (sel) => { if (sel === '.watch-card[data-tmdb-ref]') looked++; return find(sel); };
    p.near([near, next]);
    await settle();
    p.doc.querySelectorAll = find;
    const first = [reads.slice(), looked, p.cards().map((c) => c.getAttribute('data-level'))];
    const copy = far.parentNode.appendChild(far.cloneNode(true));
    p.added(copy);
    p.near([copy]);
    await settle();
    return { unasked, first, then: [reads.slice(1), p.cards().map((c) => c.getAttribute('data-level'))] };
  },

  // Two strong noes undone in the order they were given.
  async strongNoesUndone() {
    const p = load(watchPage([row('picks', PICKS())]), { api: { 'PUT /api/watch/ratings/tmdb-movie-80 -2': { ok: true, media_type: 'movie', tmdb_id: 80, was: null }, [RATE + '-2']: FILM_10,
      'DELETE /api/watch/ratings/tmdb-movie-80': { ok: true, media_type: 'movie', tmdb_id: 80 }, 'DELETE /api/watch/ratings/tmdb-movie-10': FILM_10 } });
    await thumb(p, p.cards('picks')[1], 0);
    await thumb(p, p.cards('picks')[0], 0);
    const gone = p.marks('picks');
    await p.undo(0);
    await p.undo(1);
    return { gone, back: p.marks('picks') };
  },

  // See all opened without the pick, then its Undo: the grid's own card has the focus, the row stands hidden behind it.
  async strongNoUndoneInGrid() {
    const p = rated();
    const twin = build(p.doc, card('movie/1', 'Fieldwork', 'Movie', { pick: 'rec-1', tmdb: '10' }));
    p.ctx.MMWatch.grid = { isOpen: () => true, take: () => null, bring: () => p.doc.body.appendChild(twin) };
    await thumb(p, p.cards('picks')[1], 0);
    p.doc.activeElement.blur();
    const rects = Node.prototype.getClientRects;
    Node.prototype.getClientRects = function () { return this.closest('#picks') ? [] : rects.call(this); };
    await p.undo(0);
    return p.doc.activeElement === twin.querySelector('[data-card-rate]');
  },

  // The four are one stop, which the arrow keys move in; the focus starts on the level given, else on a like.
  async menuKeys() {
    const said = async (level) => {
      const p = rated({}, { level });
      const thumbButton = p.cards('lib')[0].querySelector('[data-card-rate]');
      click(p.doc, thumbButton, { detail: 0 });
      const group = p.doc.querySelector('.card-over .rate__set');
      const out = [p.doc.activeElement.getAttribute('aria-label')].concat(['ArrowUp', 'ArrowUp', 'ArrowDown', 'End', 'Home', 'ArrowRight'].map((key) => {
        group.listeners.keydown.forEach((fn) => fn({ key, target: p.doc.activeElement, preventDefault() {}, stopPropagation() {} }));
        return p.doc.activeElement.getAttribute('aria-label');
      }));
      p.doc.listeners.keydown.forEach((fn) => fn({ key: 'Escape', target: p.doc.activeElement, preventDefault() {}, stopImmediatePropagation() {} }));
      return out.concat([p.doc.activeElement === thumbButton, thumbButton.getAttribute('aria-expanded')]);
    };
    return [await said(), await said(-1)];
  },

  // A film marked while an admin's change of it is on its way, and undone after it.
  async markWhileWorking() {
    const p = load(watchPage([row('lib', [card('movie/3', 'Salt', 'Movie · 1 GB', { tmdb: '10' })])]));
    const c = p.cards()[0];
    const sub = c.querySelector('.watch-card__sub');
    c.classList.add('is-working');
    sub.textContent = 'Keeping…';
    const spin = sub.appendChild(p.doc.createElement('span'));
    c.querySelector('.card-tools').appendChild(build(p.doc, ['button', { class: 'card-tool', 'data-watch-more': '', 'aria-disabled': 'true' }, ['svg', {}]]));
    p.open(c);
    const panel = c.querySelector('.card-over').querySelector('[data-watch-more]').getAttribute('aria-disabled');
    await p.pick(c);
    const working = [sub.textContent, sub.contains(spin), !!c.querySelector('.watch-badge--watched')];
    c.classList.remove('is-working');
    sub.textContent = 'Movie · 1 GB · Kept';
    await p.undo();
    const undone = [sub.textContent, !!c.querySelector('.watch-badge--watched')];
    await p.pick(c);
    c.classList.add('is-working');
    sub.textContent = 'Deleting…';
    await p.undo();
    return { panel, working, undone, undoneMeanwhile: sub.textContent };
  },

  // Left and right go between a card's buttons, which stand from the right to the left; up to the card.
  async toolKeys() {
    const p = load(watchPage([row('cont', [card('movie/1', 'Fieldwork', 'Movie', { dismiss: 'movie-1', tmdb: '10' })])]));
    p.doc.querySelector('[data-card-rate]').focus();
    return ['ArrowLeft', 'ArrowLeft', 'ArrowLeft', 'ArrowRight', 'ArrowUp'].map((key) => {
      p.doc.listeners.keydown.forEach((fn) => fn({ key, target: p.doc.activeElement, preventDefault() {}, stopImmediatePropagation() {} }));
      return p.doc.activeElement.className;
    });
  },

  // A strong no from the library's card, then Undo.
  async strongNo() {
    const p = rated();
    await thumb(p, p.cards('lib')[0], 2);
    await thumb(p, p.cards('lib')[0], 0);
    const out = { gone: p.marks(), toast: p.toasts[0].message };
    await p.undo();
    return { ...out, back: p.marks('picks'), calls: p.calls };
  },

  // From the pick's own card, with the keyboard; then Undo from its toast.
  async strongNoFocus() {
    const p = rated();
    await thumb(p, p.cards('picks')[1], 0);
    const next = p.focus();
    const toast = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-toast' }, ['button', {}]]));
    toast.querySelector('button').focus();
    await p.undo();
    return { next, back: p.focus() };
  },

  // Named by another ref, in Details, which is then closed.
  async strongNoInDetails() {
    const p = load(watchPage([row('picks', [card('movie/8', 'Before', 'Movie', { pick: 'rec-8', tmdb: '80' }), card('movie/7', 'Pick', 'Movie', { ref: 'rec-7', pick: 'rec-7', tmdb: '70' })])]),
      { api: { 'PUT /api/watch/ratings/tmdb-movie-70 -2': { media_type: 'movie', tmdb_id: 70 } } });
    const details = p.doc.body.appendChild(build(p.doc, ['div', { id: 'ttl' }, ['button', { class: 'inside' }]]));
    p.doc.documentElement.classList.add('has-ttl');
    details.querySelector('button').focus();
    p.ctx.MM.ratings.set('tmdb-movie-70', -2);
    await settle();
    const open = [p.marks(), p.focus(), p.toasts[0].dialog];
    p.changed();
    // Closed over See all, its heading has been given the focus by then.
    if (process.argv[3]) p.doc.body.appendChild(build(p.doc, ['h1', { id: process.argv[3], class: 'title' }])).focus();
    details.classList.add('is-leaving');
    p.changed();
    return { open, closed: p.focus() };
  },

  // Each toast that was taken away by the time of the next change: marked, marked back, another film, and undone.
  async markToasts() {
    const p = load(watchPage([row('lib', [FILM(), card('movie/3', 'Salt', 'Movie')])]));
    const at = [];
    const step = async (go) => { await go(); at.push(p.gone.slice()); };
    await step(() => p.pick(p.cards()[0], 'mark'));
    await step(() => p.pick(p.cards()[1], 'mark'));
    await step(() => p.pick(p.cards()[0], 'mark'));
    await step(() => p.undo(1));
    return at;
  },

  async dismissToasts() {
    const p = load(watchPage([row('cont', CONTINUE)]));
    await p.pick(p.cards()[0], 'dismiss');
    await p.undo();
    p.dismissed('movie-1');
    await settle();
    return p.gone;
  },

  async ratingFails() {
    const p = rated({ [RATE + '-2']: 500 });
    await thumb(p, p.cards('picks')[1], 0);
    return { picks: p.marks('picks'), toasts: p.toasts.map((t) => t.message) };
  },

  // A strong no is saved, and another level pressed before it has left; that is not saved, before its time is up or after,
  // or the server does not say, and is asked.
  async otherLevelFails() {
    const out = [];
    for (const [after, status] of [[false, 409], [true, 409], [true, 500], [false, 500]]) {
      const no = later();
      let level = null;
      const save = (to) => () => { const was = level; level = to; return { ...FILM_10, was }; };
      const p = rated({ [RATE + '2']: () => no.answer.then(() => Promise.reject(Object.assign(new Error('http'), { status }))),
        [RATE + '-2']: save(-2), [RATE + '1']: save(1), 'GET /api/watch/ratings?refs=tmdb-movie-10': () => ({ ratings: { 'tmdb-movie-10': { level, rateable: true } } }) }, { hold: true });
      await thumb(p, p.cards('picks')[1], 2);
      p.pass();
      await thumb(p, p.cards('picks')[1], 0);
      await thumb(p, p.cards('picks')[1], 3);
      if (after) p.pass();
      const before = p.marks('picks').length;
      no.send();
      await settle();
      p.pass();
      const told = p.toasts.map((t) => t.message);
      const gone = p.marks('picks');
      p.toasts.filter((t) => t.undo).forEach((t) => t.undo());
      await settle();
      out.push({ before, told, gone, back: p.marks('picks'), last: p.calls.filter((c) => /^PUT/.test(c)).pop() });
    }
    return out;
  },

  // A save is stored and its answer lost: a strong no's, or that of the Undo of one. The server is asked after each.
  async answerLost() {
    const go = async (lost) => {
      let level = 1;
      const save = (to) => () => {
        const was = level;
        level = to;
        return to === lost ? Promise.reject(Object.assign(new Error('http'), { status: 500 })) : { ...FILM_10, was };
      };
      const p = rated({ [RATE + '-2']: save(-2), [RATE + '1']: save(1), 'GET /api/watch/ratings?refs=tmdb-movie-10': () => ({ ratings: { 'tmdb-movie-10': { level, rateable: true } } }) }, { hold: true, level: 1 });
      await thumb(p, p.cards('picks')[1], 0);
      p.pass();
      await settle();
      const gone = p.marks('picks');
      p.toasts.filter((t) => t.undo).forEach((t) => t.undo());
      await settle();
      return { gone, back: p.marks('picks'), told: p.toasts.map((t) => t.message), dismissed: p.gone };
    };
    return [await go(-2), await go(1)];
  },

  // A save that fails at once, with the levels over the card.
  async menuFails() {
    const p = rated({ [RATE + '1']: 429 }, { hold: true, level: 2 });
    await thumb(p, p.cards('lib')[0], 2);
    return [p.cards().map((c) => c.getAttribute('data-level')), p.toasts.map((t) => t.message)];
  },

  // A film that has no TMDB match, and one whose ref the server then says cannot be rated.
  async cannotRate() {
    const p = load(watchPage([row('lib', [card('movie/4', 'Home video', 'Movie'), FILM({ tmdb: '10' })])]),
      { hold: true, api: { 'GET /api/watch/ratings?refs=tmdb-movie-10': () => ({ ratings: { 'tmdb-movie-10': { level: null, rateable: false } } }), [RATE + '1']: 500 } });
    const has = () => p.cards('lib').map((c) => c.querySelectorAll('.card-tool').filter((b) => !b.hidden).map((b) => b.className.split('--')[1]));
    const before = has();
    await thumb(p, p.cards('lib')[1], 2);
    return [before, has()];
  },

  // Undo of a strong no, pressed in its toast over Details of the same title: a level from before, none, and the
  // title named by another ref; then with no thumbs there, and over another title. And from the title's own heading.
  async undoInDetails() {
    const out = [];
    for (const [level, on, from] of [[1, 'movie-1'], [null, 'movie-1'], [1, 'tmdb-movie-10'], [1, 'rec-1'], [1, 'movie-1', 'bare'], [1, 'movie-9'], [null, 'tmdb-movie-90', 'heading']]) {
      const p = rated();
      const details = p.doc.body.appendChild(build(p.doc, ['div', { id: 'ttl' }, ['h2', { id: 'ttl-title' }], ...(from === 'bare' ? [] : [-2, -1, 1, 2].map((n) => ['button', { 'data-ttl-act': 'rate-' + n }]))]));
      p.ctx.location.hash = '#title=' + on;
      if (level) { p.ctx.MM.ratings.set('movie-1', level); await settle(); }
      p.doc.documentElement.classList.add('has-ttl');
      p.ctx.MM.ratings.set('movie-1', -2);
      await settle();
      const toast = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-toast' }, ['button', {}]]));
      (from === 'heading' ? details.querySelector('#ttl-title') : toast.querySelector('button')).focus();
      await p.undo();
      out.push(p.doc.activeElement.getAttribute('data-ttl-act') || p.doc.activeElement.tagName);
    }
    return out;
  },

  // A strong no whose level before is not known has nothing to go back to.
  async unknownBefore() {
    const p = rated();
    p.ctx.MM.ratings.get = () => -2;
    p.doc.listeners['mm:rating'].forEach((fn) => fn({ detail: { phase: 'saved', ref: 'movie-1', refs: ['movie-1'], level: -2, was: undefined, from: null, mediaType: 'movie', tmdbId: 10 } }));
    return [p.marks('picks'), !!p.toasts[0].undo];
  },

  // The sets that are still there once the levels have folded.
  async menuSets() {
    const p = rated();
    let sets = 0;
    const mount = p.ctx.MM.ratings.mount;
    p.ctx.MM.ratings.mount = (ref, o) => { const set = mount(ref, o); const own = set.destroy; sets += 1; set.destroy = () => { sets -= 1; own(); }; return set; };
    p.open(p.cards('lib')[0]);
    const open = sets;
    p.doc.listeners.keydown.forEach((fn) => fn({ key: 'Escape', target: p.doc.activeElement, preventDefault() {}, stopImmediatePropagation() {} }));
    const folded = sets;
    p.open(p.cards('lib')[0]);
    p.cards('lib')[0].remove();
    p.changed();
    return [open, folded, sets];
  },

  // Another card's levels opened before the time to fold those of the first is up.
  async menuAgain() {
    const p = rated({}, { hold: 560 });
    await thumb(p, p.cards('lib')[0], 2);
    p.open(p.cards('picks')[0]);
    p.pass();
    return p.cards().map((c) => c.classList.contains('is-lifted'));
  },

  async undoFails() {
    const p = rated({ 'DELETE /api/watch/ratings/tmdb-movie-10': 500 });
    await thumb(p, p.cards('picks')[1], 0);
    p.toasts[0].undo();
    const now = p.marks('picks');
    await settle();
    return { now, after: p.marks('picks'), toast: p.toasts[1].message };
  },

  // Rated otherwise elsewhere: it is back, and the toast that says it is gone goes.
  async secondThoughts() {
    const p = rated();
    await thumb(p, p.cards('picks')[1], 0);
    const gone = p.marks('picks');
    await thumb(p, p.cards('lib')[0], 2);
    return { gone, back: p.marks('picks'), dismissed: p.gone.length };
  },

  // The row was filled anew before the Undo, or the last pick took the row along.
  async undoLater() {
    const anew = rated();
    await thumb(anew, anew.cards('picks')[1], 0);
    anew.doc.querySelector('#picks .watch-row__viewport').replaceWith(build(anew.doc, row('fresh', PICKS().slice(0, 1))[2]));
    await anew.undo();
    const only = load(watchPage([row('picks', PICKS().slice(1, 2)), row('lib', [FILM()])]), { api: { [RATE + '-2']: FILM_10, 'DELETE /api/watch/ratings/movie-1': FILM_10 } });
    await thumb(only, only.cards('picks')[0], 0);
    const emptied = !!only.doc.querySelector('#picks');
    await only.undo();
    return { anew: anew.marks('picks'), emptied, back: only.marks('picks') };
  },

  // What the eye of each kind of card marks.
  async menus() {
    const p = load(watchPage([row('lib', [FILM()])]));
    return ['movie/1', 'episode/22', 'season/21', 'show/2', 'tmdb-movie-5', 'tmdb-tv-5'].map((m) => p.ctx.MMWatch.core.markLabel(m));
  },

  // The pieces of a caption.
  async captions() {
    const p = load(watchPage([row('lib', [FILM()])]));
    const sub = p.doc.querySelector('.watch-card__sub');
    return JSON.parse(process.argv[3]).map((text) => {
      p.ctx.MMWatch.core.caption(sub, text);
      return [sub.textContent, pieces(sub)];
    });
  },

  // The captions of the cards a script builds.
  async builtCaption() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2016')])]));
    vm.runInContext(fs.readFileSync(path.join(JS, 'watch/search.js'), 'utf8'), p.ctx);
    const sub = p.ctx.MMWatch.search.card({ title: 'Fieldwork', kind: 'movie', subtitle: 'Movie', minutes_left: 31, href: '/watch/movie/1' }).querySelector('.watch-card__sub');
    const poster = p.doc.createElement('span');
    const found = p.cards('lib')[0];
    p.ctx.MMWatch.core.addMarks(found, poster, { title: 'Brightwater', mark_kind: 'show', mark_key: '2' });
    const line = found.querySelector('.watch-card__sub');
    return { library: [sub.textContent].concat(pieces(sub)), found: [line.textContent].concat(pieces(line)) };
  },

  // A film marked from its card, then Undo.
  async markFilm() {
    const p = load(watchPage([row('lib', [FILM()]), row('rec', [FILM(), card('movie/9', 'Other', 'Movie')])]));
    await p.pick(p.cards('lib')[0], 'mark');
    const marked = p.look();
    const eyes = pressed(p);
    await p.undo();
    return { marked, eyes, undone: p.look(), calls: p.calls, toasts: p.toasts.map((t) => t.message), fetched: p.fetched, events: p.events };
  },

  // Undo refused: the change stands and it says so.
  async undoRefused() {
    const p = load(watchPage([row('lib', [FILM()])]), { api: { 'DELETE /api/watch/watched/movie/1?undo=1': 409 } });
    await p.pick(p.cards()[0], 'mark');
    await p.undo();
    return { look: p.look(), variant: p.toasts[1].variant, busy: p.cards()[0].querySelector('.card-tool--eye').getAttribute('aria-disabled') };
  },

  // A mark that fails, then one that changed nothing.
  async markFails() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · 3 left')])]), { api: { 'POST /api/watch/watched/show/2': 503 } });
    await p.pick(p.cards()[0], 'mark');
    const more = p.cards()[0].querySelector('.card-tool--eye');
    const q = load(watchPage([row('lib', [FILM()])]), { api: { 'POST /api/watch/watched/movie/1': { ok: true, changed: 0, watched: true } } });
    await q.pick(q.cards()[0], 'mark');
    return { look: p.look(), toast: p.toasts[0].message, free: more.getAttribute('aria-disabled'), noChange: { look: q.look(), undo: !!q.toasts[0].undo } };
  },

  // While a change is on its way its buttons say so and a second press does nothing.
  async oneAtATime() {
    let answer;
    const p = load(['div', { id: 'watch-page' }, ['button', { 'data-mark-toggle': 'movie/1', 'data-mark-name': 'Fieldwork', 'aria-pressed': 'false' }]],
      { api: { 'POST /api/watch/watched/movie/1': new Promise((r) => { answer = r; }) } });
    const b = p.doc.querySelector('[data-mark-toggle]');
    click(p.doc, b);
    const during = [b.getAttribute('aria-busy'), b.getAttribute('aria-pressed')];
    click(p.doc, b);
    answer({ ok: true, changed: 1, watched: true });
    await settle();
    return { during, calls: p.calls.length, after: [b.getAttribute('aria-busy'), b.getAttribute('aria-pressed')] };
  },

  // A show's change is its season cards' too; another show's stay.
  async markShow() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · 3 left')]),
      row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' }), card('season/91', 'Else', 'Season 1', { ref: 'show-9' })])]));
    await p.pick(p.cards()[0], 'mark');
    const marked = p.look();
    await p.undo();
    return { marked, undone: p.look() };
  },

  // A season marked: its show's card says what the server says is left.
  async markSeason() {
    const show = (sub) => row('lib', [card('show/2', 'Brightwater', sub)]);
    const p = load(watchPage([show('TV · 2 seasons · 9 left'), row('disc', [card('show/2', 'Brightwater', 'TV · 2016')]), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]),
      { next: watchPage([show('TV · 2 seasons · 4 left'), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2', watched: true })])]) });
    await p.pick(p.cards('leave')[0], 'mark');
    const marked = { look: p.look(), toast: p.toasts[0].message };
    await p.undo();
    return { marked, undone: p.look() };
  },

  // A season marked whose show's card stands past the cards the server's page has.
  async markSeasonPastThePage() {
    const go = async (search, title, over) => {
      const p = load(watchPage([row('lib', [card('show/2', 'Brightwater & Co', 'TV · 2 seasons · 9 left', over)]), row('disc', [card('show/2', 'Brightwater & Co', 'TV · 2016')]),
        row('leave', [card('season/21', 'Brightwater & Co', 'Season 1', { ref: 'show-2' })])]),
        { next: watchPage([row('lib', [card('show/9', 'Other', 'TV')])]), api: { 'GET /api/watch/search?q=Brightwater%20%26%20Co': search, 'GET /api/watch/title/show-2': title } });
      await p.pick(p.cards('leave')[0], 'mark');
      const marked = p.look();
      await p.undo();
      return { marked, undone: p.look() };
    };
    const item = (ref, subtitle, watched) => ({ ref, title: 'Brightwater & Co', subtitle, watched });
    return {
      found: await go({ items: [item('show-7', 'TV · 1 season · 2 left', false), item('show-2', 'TV · 2 seasons · 4 left', false)] }, { watched: false, seasons: [{ key: '21', watched: true }] }),
      finished: await go({ items: [item('show-2', 'TV · 2 seasons', true)] }, { watched: true }),
      unlisted: await go({ items: [item('show-7', 'TV', true)] }, { watched: true }),
      noLonger: await go(500, { watched: false, seasons: [] }, { watched: true }),
      failed: await go(500, 500),
    };
  },

  // Dismissed from its card, in the middle of the row: focus goes on, Undo puts it back.
  async dismiss() {
    const p = load(watchPage([row('cont', CONTINUE)]));
    await p.pick(p.cards()[1], 'dismiss');
    const gone = { marks: p.marks(), focus: p.focus(), calls: p.calls.slice(), toast: p.toasts[0].message };
    p.doc.activeElement = p.doc.body;
    await p.undo();
    return { gone, back: p.marks(), focus: p.focus(), call: p.calls[p.calls.length - 1] };
  },

  // The row opened as a grid: the card is in both, and an Undo brings it back to both.
  // The last card of Continue watching goes; after its row stand an anchor, a placeholder and the next row.
  async dismissLast() {
    const strip = (id, spec) => ['section', { id }, ['div', { class: 'watch-row__scroll', ...spec }]];
    const run = async (after, touch) => {
      const p = load(watchPage([row('continue', [CONTINUE[0]]), ['div', { id: 'watch-asked', hidden: '' }], strip('skel', {}), ...after]));
      if (touch) {
        p.doc.body.appendChild(build(p.doc, ['main', { id: 'main-content', tabindex: '-1' }]));
        p.ctx.matchMedia = () => ({ matches: true });
      }
      await p.pick(p.cards()[0], 'dismiss');
      const at = p.doc.activeElement;
      return at.id || at.closest('section').id;
    };
    return { next: await run([strip('leaving', { tabindex: '0' })]), none: await run([]), touch: await run([], true) };
  },
  async dismissInGrid() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { class: 'watch-grid', id: 'grid' }, ...CONTINUE]]));
    await p.pick(p.cards('grid')[1], 'dismiss');
    const gone = [p.marks('cont'), p.marks('grid')];
    await p.undo();
    return { gone, back: [p.marks('cont'), p.marks('grid')], calls: p.calls };
  },

  // With the rows hidden behind an open grid, an Undo gives the focus to what is shown; once the grid is closed, to the row.
  async undoFocusInGrid() {
    const go = async (act, { at, close, menu = true, toggle, inert, mark }) => {
      const p = load(watchPage([['div', { id: 'rows' }, row('cont', CONTINUE), row('lib', [FILM(), card('movie/9', 'Other', 'Movie')])],
        ['div', { class: 'watch-grid', id: 'grid' }, ...CONTINUE, FILM(), card('movie/9', 'Other', 'Movie')]]),
      { next: (n, calls) => watchPage([row('cont', calls.length > 1 ? CONTINUE : CONTINUE.slice(1))]) });
      const hide = (id, on) => { p.doc.getElementById(id).hidden = on; };
      p.doc.body.descendants().forEach((n) => { n.getClientRects = () => (n.closest('[hidden]') || !p.doc.body.contains(n) ? [] : [{}]); });
      hide('rows', true);
      if (inert) { hide('rows', false); p.doc.getElementById('rows').setAttribute('inert', ''); }
      if (toggle) {
        const b = p.doc.body.appendChild(build(p.doc, ['button', { 'data-mark-toggle': 'movie/9', 'data-mark-name': 'Other', 'aria-pressed': 'false' }]));
        click(p.doc, b);
        await settle();
        b.remove();
      } else if (menu) await p.pick(p.cards(at || 'grid')[mark || 0], act);
      else { p.dismissed('movie-1'); await settle(); }
      if (close) { hide('rows', false); p.doc.getElementById('grid').remove(); }
      p.doc.activeElement = p.doc.body;
      await p.undo();
      const c = p.doc.activeElement.closest('.watch-card');
      return c ? (c.closest('#grid') ? 'grid ' : c.closest('#cont') ? 'row ' : 'lib ') + (c.hasAttribute('data-dismiss-ref') ? 'continue' : 'library') : p.doc.activeElement.tagName;
    };
    return {
      dismissed: await go('dismiss', {}),
      fromPreview: await go('dismiss', { menu: false }),
      watched: await go('mark', {}),
      libraryCard: await go('mark', { mark: 3 }),
      closed: await go('dismiss', { close: true }),
      toggledElsewhere: await go('mark', { toggle: true }),
      opening: await go('dismiss', { inert: true, menu: false }),
      libraryClosed: await go('mark', { mark: 3, close: true }),
    };
  },

  // A card an open grid has let go of fades out on its own: a change that follows finds it no more.
  async ghostInGrid() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { id: 'host' }, CONTINUE[0]]]));
    const ghost = p.cards('host')[0];
    ghost.classList.add('is-gone');
    p.dismissed('movie-1');
    await settle();
    return [p.marks('cont'), p.marks('host'), ghost.classList.contains('is-kept')];
  },

  async continues() {
    const p = load(watchPage([row('cont', CONTINUE)]), { next: (n) => watchPage([row('cont', n === 1 ? CONTINUE : CONTINUE.slice(1))]) });
    await p.pick(p.cards()[2], 'dismiss');
    await p.undo();
    await p.pick(p.cards()[0], 'mark');
    return p.continues;
  },

  async dismissRefused() {
    const p = load(watchPage([row('cont', CONTINUE)]), { api: { 'POST /api/watch/dismissed/movie-1': 500 } });
    await p.pick(p.cards()[0], 'dismiss');
    return { marks: p.marks(), variant: p.toasts[0].variant };
  },

  // Dismissed in the banner: the next one takes its place, and the title's card goes too.
  async dismissBanner() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    const b = p.doc.querySelector('[data-watch-dismiss]');
    b.focus();
    click(p.doc, b);
    await settle();
    const gone = { banner: p.banner(), marks: p.marks(), focus: p.focus(), calls: p.calls.slice() };
    await p.undo();
    return { gone, back: { banner: p.banner(), marks: p.marks() } };
  },

  // Dismissed in the banner by a press that gives its button no focus.
  async dismissBannerUnfocused() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    p.doc.activeElement = p.doc.body;
    click(p.doc, p.doc.querySelector('[data-watch-dismiss]'));
    const focused = [];
    Node.prototype.focus = function () { focused.push(this.className); this.ownerDocument.activeElement = this; };
    await settle();
    return focused;
  },

  // A row scrolled past the place of the card an Undo puts back, or that has it in view.
  async undoOffScreen() {
    const go = async (act, left, opts = {}) => {
      const p = load(watchPage([row('cont', CONTINUE)]), opts);
      const scroller = p.doc.querySelector('.watch-row__scroll');
      const by = [];
      const snaps = [];
      const put = scroller.insertBefore;
      scroller.insertBefore = function (...args) { if (args[0].tagName === 'ARTICLE') snaps.push(scroller.style.scrollSnapType); return put.apply(scroller, args); };
      scroller.getBoundingClientRect = () => ({ top: 0, bottom: 20, left: 0, right: 390, width: 390, height: 20 });
      scroller.scrollBy = (to) => by.push([to.left, to.behavior]);
      const first = p.cards()[0];
      const at = () => ({ top: 0, bottom: 20, left, right: left, width: 0, height: 20 });
      first.getBoundingClientRect = at;
      if (opts.next) Node.prototype.getBoundingClientRect = function () { return this.classList.contains('watch-card') ? at() : { top: 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 }; };
      await p.pick(first, act);
      const before = by.length;
      await p.undo();
      return [p.marks()[0], before, by, snaps, scroller.style.scrollSnapType];
    };
    return {
      before: await go('dismiss', -128),
      after: await go('dismiss', 380),
      inView: await go('dismiss', 20),
      mark: await go('mark', -128, { next: watchPage([row('cont', CONTINUE.slice(1))]) }),
    };
  },

  // An Undo of an older dismissal, when another change has brought the row back without that
  // title: its card comes with the server's page, out of sight at the start of a scrolled row.
  async olderUndoOffScreen() {
    const salt = card('movie/3', 'Salt', 'Movie', { watched: true });
    const page = (cards) => watchPage([['div', { class: 'watch-rows' }, ...(cards.length ? [row('cont', cards)] : []), row('lib', [salt])]]);
    const p = load(page(CONTINUE.slice(0, 1)), { next: (_n, calls) => {
      const sent = calls.filter((c) => !/^GET /.test(c));
      return page(sent.length < 2 ? [] : sent.length < 3 ? CONTINUE.slice(2) : [CONTINUE[0], CONTINUE[2]]);
    } });
    await p.pick(p.cards('cont')[0], 'dismiss');
    await p.pick(p.cards('lib')[0], 'mark');
    const rowNow = p.doc.querySelector('.watch-rows').children[0];
    const brought = p.marks('cont');
    const scroller = rowNow.querySelector('.watch-row__scroll');
    const by = [];
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 20, left: 0, right: 390, width: 390, height: 20 });
    scroller.scrollBy = (to) => by.push([to.left, to.behavior]);
    const box = Node.prototype.getBoundingClientRect;
    Node.prototype.getBoundingClientRect = function () { return this.getAttribute('data-mark') === 'movie/1' ? { top: 0, bottom: 20, left: -128, right: -108, width: 20, height: 20 } : box.call(this); };
    await p.undo(0);
    return { brought, marks: p.marks('cont'), by, snap: scroller.style.scrollSnapType };
  },

  // The last title dismissed: with no banner on the server's page, it folds away and comes back.
  async lastBanner() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE.slice(0, 1))]), { next: watchPage([]) });
    click(p.doc, p.doc.querySelector('[data-watch-dismiss]'));
    await settle();
    const gone = { banner: p.banner(), marks: p.marks() };
    await p.undo();
    return { gone, back: { banner: p.banner(), marks: p.marks() } };
  },

  // A film in Continue watching marked watched leaves it; an episode gives way to the next one, or leaves.
  async markContinue() {
    const next = [card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2' }), CONTINUE[2]];
    const p = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', next)]) });
    await p.pick(p.cards()[0], 'mark');
    const film = p.marks();
    await p.pick(p.cards()[0], 'mark');
    const episode = { marks: p.marks(), focus: p.focus(), toast: p.toasts[1].message };
    await p.undo();
    const undone = p.marks();
    const q = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', [CONTINUE[2]])]) });
    await q.pick(q.cards()[1], 'mark');
    return { film, episode, undone, last: q.marks() };
  },

  // Undone before the server's page arrives: what it says is dropped.
  async undoneFirst() {
    const p = load(watchPage([row('cont', CONTINUE)]), { next: (n) => { if (n === 1) p.toasts[0].undo(); return watchPage([row('cont', n === 1 ? [CONTINUE[2]] : CONTINUE)]); } });
    await p.pick(p.cards()[1], 'mark');
    return p.marks();
  },

  // Focus stays in Details while it is open over the rows.
  async underDetails() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { id: 'ttl' }, ['button', { class: 'in-details', 'data-mark-toggle': 'movie/1', 'data-mark-name': 'Fieldwork', 'aria-pressed': 'false' }]]]));
    p.doc.documentElement.classList.add('has-ttl');
    const b = p.doc.querySelector('.in-details');
    b.focus();
    click(p.doc, b);
    await settle();
    const out = { marks: p.marks(), focus: p.focus(), events: p.events.slice(), dialog: p.toasts[0].dialog };
    p.doc.activeElement = p.doc.body;
    await p.undo();
    const afterUndo = p.focus();
    // Pressed by touch, where a button may not take the focus: the rows beneath do not take it either.
    click(p.doc, b);
    p.doc.activeElement = p.doc.body;
    await settle();
    const touched = p.focus();
    p.doc.documentElement.classList.remove('has-ttl');
    click(p.doc, p.doc.querySelector('[data-mark-toggle]'));
    await settle();
    return { ...out, afterUndo, touched, onPage: p.toasts[p.toasts.length - 1].dialog };
  },

  // The show's own page follows an episode's mark: Play, the tabs and every toggle.
  async showPage() {
    const page = (watched, selected, play) => ['div', { id: 'watch-page' },
      ['div', { class: 'watch-hero__info' },
        ['div', { class: 'watch-actions' }, ['a', { class: 'watch-btn--play', href: play }, play],
          ['button', { 'data-mark-toggle': 'show/2', 'data-mark-name': 'Brightwater', 'aria-pressed': String(watched) }]],
        ['nav', { class: 'watch-seasons' }, ...[1, 2].map((n) => ['a', { class: 'watch-pill' + (n === selected ? ' on' : ''), href: '/s?season=' + n,
          ...(watched && n === 1 ? { 'aria-label': 'Season 1, watched' } : {}) }, 'Season ' + n, ...(watched && n === 1 ? [['svg', { class: 'watch-pill__mark' }]] : [])])]],
      ['section', { class: 'watch-episodes' }, ['ol', {}, ['li', {},
        ['a', { class: 'watch-ep' + (watched ? '' : ' watch-ep--in-progress'), 'data-ep': '21' }, ['span', { class: 'watch-ep__thumb' }, ...(watched ? [] : [['progress', { class: 'watch-bar' }]])],
          ['span', { class: 'watch-ep__text' }, watched ? 'One, watched' : 'One'], ...(watched ? [] : [['span', { class: 'watch-ep__state' }, 'In progress']])],
        ['button', { 'data-mark-toggle': 'episode/21', 'data-mark-name': 'One', 'aria-pressed': String(watched), title: 'Mark as watched' }]]]]];
    const run = async (onFetch) => {
      const p = load(page(false, 1, '/watch/play/21'), { next: page(true, 1, '/watch/play/22'), onFetch });
      const b = p.doc.querySelector('[data-mark-toggle="episode/21"]');
      b.focus();
      click(p.doc, b);
      await settle();
      const pill = p.doc.querySelectorAll('.watch-pill')[0];
      const ep = p.doc.querySelector('.watch-ep');
      return { fetched: p.fetched, play: p.doc.querySelector('.watch-btn--play').getAttribute('href'),
        show: p.doc.querySelector('[data-mark-toggle="show/2"]').getAttribute('aria-pressed'), pill: [pill.getAttribute('aria-label'), pill.textContent, !!pill.querySelector('.watch-pill__mark')],
        episode: [b.getAttribute('aria-pressed'), b.getAttribute('title'), ep.textContent, ep.classList.contains('watch-ep--in-progress'), !!ep.querySelector('.watch-bar')], focus: p.doc.activeElement === b };
    };
    const other = await run((doc) => { const t = doc.querySelectorAll('.watch-pill'); t[0].classList.remove('on'); t[1].classList.add('on'); });
    return { same: await run(), otherSeason: [other.play, other.show] };
  },

  // The show's page asked for after one mark lands after the next mark's.
  async lateTitlePage() {
    const first = later();
    const p = load(episodesPage([]), { next: (n) => (n === 1 ? first.answer : episodesPage([21, 22], '/watch/play/23')) });
    await press(p, 21);
    await press(p, 22);
    first.send(episodesPage([21], '/watch/play/22'));
    await settle();
    return { pressed: pressed(p), play: p.doc.querySelector('.watch-btn--play').getAttribute('href') };
  },

  // The page asked for after a mark lands after its Undo's.
  async lateTitlePageUndone() {
    const first = later();
    const p = load(episodesPage([]), { next: (n) => (n === 1 ? first.answer : episodesPage([])) });
    await press(p, 21);
    await p.undo();
    first.send(episodesPage([21], '/watch/play/22'));
    await settle();
    return { pressed: pressed(p), play: p.doc.querySelector('.watch-btn--play').getAttribute('href') };
  },

  // The Watch page asked for after an episode's mark lands after a film's mark.
  async lateRows() {
    const first = later();
    const now = watchPage([row('cont', [CONTINUE[0], card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2' }), CONTINUE[2]]), row('lib', [card('movie/9', 'Other', 'Movie')])]);
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [card('movie/9', 'Other', 'Movie')])]), { next: (n) => (n === 1 ? first.answer : now) });
    await p.pick(p.cards('cont')[1], 'mark');
    await p.pick(p.cards('lib')[0], 'mark');
    first.send(watchPage([row('cont', CONTINUE)]));
    await settle();
    return p.marks('cont');
  },

  // A card gives way with its levels open: by its mark, by the Undo, and by leaving.
  async menuOfAGoneCard() {
    const first = later();
    let sent = first.answer;
    const rows = [card('movie/1', 'Fieldwork', 'Movie', { dismiss: 'movie-1', tmdb: '1' }), card('episode/22', 'Brightwater', 'S1 · E2', { dismiss: 'show-2', tmdb: '2' }), card('movie/3', 'Salt', 'Movie', { dismiss: 'movie-3', tmdb: '3' })];
    const episode = card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2', tmdb: '2' });
    const p = load(watchPage([row('cont', rows)]), { next: () => sent });
    const open = () => p.doc.querySelectorAll('.card-over').length > 0;
    await p.pick(p.cards()[1], 'mark');
    p.open(p.cards()[1]);
    first.send(watchPage([row('cont', [rows[0], episode, rows[2]])]));
    await settle();
    const replaced = [open(), p.focus()];
    p.open(p.cards()[1]);
    sent = watchPage([row('cont', rows)]);
    await p.undo();
    const undone = open();
    p.open(p.cards()[1]);
    p.dismissed('movie-3');
    p.open(p.cards()[1]);
    await settle();
    const another = [open(), p.focus()];
    const last = later();
    sent = last.answer;
    click(p.doc, p.doc.body);
    await p.pick(p.cards()[1], 'mark');
    p.open(p.cards()[1]);
    last.send(watchPage([]));
    await settle();
    return { replaced, undone, another, left: [open(), p.focus()] };
  },

  // What a season's mark asked of its show lands after the mark is undone.
  async lateShow() {
    const first = later();
    let n = 0;
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV')]), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]),
      { next: watchPage([]), api: { 'GET /api/watch/title/show-2': () => (n++ ? { watched: false } : first.answer) } });
    await p.pick(p.cards('leave')[0], 'mark');
    await p.undo();
    first.send({ watched: true });
    await settle();
    return p.look()[0][1];
  },

  // Two episodes marked one after the other, then undone with the oldest first. The card was
  // replaced twice, or the second mark took it away, or took its row away with it.
  async undoOldestMark() {
    const go = async (others, second) => {
      let keys = [22];
      const now = () => continuePage([...others.slice(0, 1), ...keys.map(episodeCard), ...others.slice(1)]);
      const p = load(now(), { next: now });
      const show = () => p.cards().filter((c) => c.getAttribute('data-dismiss-ref') === 'show-2')[0];
      const state = () => p.marks('cont');
      keys = [23];
      await p.pick(show(), 'mark');
      keys = second;
      await p.pick(show(), 'mark');
      const marked = state();
      keys = [22];
      await p.undo(0);
      const oldest = state();
      await p.undo(1);
      return { marked, oldest, both: state(), rows: p.doc.querySelectorAll('#cont').length, started: p.started.length };
    };
    return { replaced: await go([CONTINUE[0], CONTINUE[2]], [24]), left: await go([CONTINUE[0], CONTINUE[2]], []), rowGone: await go([], []) };
  },

  // The row emptied by two titles; the server's page brings it back with one, then the other is undone.
  async undoIntoANewRow() {
    let sent = [CONTINUE[1], CONTINUE[2]];
    const p = load(continuePage(sent), { next: () => continuePage(sent) });
    sent = [episodeCard(23), CONTINUE[2]];
    await p.pick(p.cards('cont')[0], 'mark');
    sent = [CONTINUE[2]];
    await p.pick(p.cards('cont')[0], 'mark');
    sent = [];
    p.dismissed('movie-3');
    await settle();
    sent = [CONTINUE[1]];
    await p.undo(0);
    const one = p.marks('cont');
    sent = [CONTINUE[1], CONTINUE[2]];
    await p.undo(2);
    return { one, both: p.marks('cont'), rows: p.doc.querySelectorAll('#cont').length };
  },

  // The banner dismissed until none is left, then every dismissal undone, oldest first.
  async undoOldestBanner() {
    const titles = [['movie-1', 'Fieldwork', '/watch/play/1'], ['show-2', 'Brightwater', '/watch/play/22'], ['movie-3', 'Salt', '/watch/play/3']];
    const gone = new Set();
    const now = () => {
      const left = titles.filter((t) => !gone.has(t[0]));
      return watchPage([...(left.length ? [banner(...left[0])] : []), ['div', { class: 'watch-rows' }, ...(left.length ? [row('cont', CONTINUE.filter((c) => !gone.has(c[1]['data-dismiss-ref'])))] : [])]]);
    };
    const p = load(now(), { next: now });
    const states = [];
    for (const [ref] of titles) {
      gone.add(ref);
      click(p.doc, p.doc.querySelector('[data-watch-dismiss="' + ref + '"]'));
      await settle();
    }
    states.push(p.banner());
    for (const [i, [ref]] of titles.entries()) {
      gone.delete(ref);
      await p.undo(i);
      states.push(p.banner());
    }
    return { states, marks: p.marks() };
  },

  // Two seasons marked, the first undone: the show's card says what is left now, not before both.
  async undoOldestSeason() {
    const seasons = { 21: false, 22: false };
    const now = () => watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · ' + ((seasons[21] ? 0 : 5) + (seasons[22] ? 0 : 4)) + ' left')]),
      row('leave', [21, 22].map((k) => card('season/' + k, 'Brightwater', 'Season ' + k, { ref: 'show-2', watched: seasons[k] })))]);
    const p = load(now(), { next: now });
    for (const i of [0, 1]) {
      seasons[21 + i] = true;
      await p.pick(p.cards('leave')[i], 'mark');
    }
    seasons[21] = false;
    await p.undo(0);
    return p.look().map((look) => look.slice(1, 3));
  },

  // A dismissal undone while the server cannot ask Plex, whose page then lacks the card and its banner.
  async undoWithoutPlex() {
    const p = load(watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE)]),
      { next: () => watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), ['p', { class: 'watch-notice' }], row('cont', [CONTINUE[0], CONTINUE[2]])]) });
    p.dismissed('show-2');
    await settle();
    const gone = [p.banner(), p.marks()];
    await p.undo();
    return { gone, back: [p.banner(), p.marks()] };
  },

  // The open grid is told of a card the server's page brought, with its row or into it.
  async entersTheGrid() {
    const go = async (before, after) => {
      const p = load(continuePage(before), { next: () => continuePage(after) });
      const told = [];
      p.ctx.MMWatch.grid = { bring: (c) => told.push([c.classList.contains('watch-card'), c.getAttribute('data-dismiss-ref'), p.doc.body.contains(c), c.closest('.watch-row').id]) };
      click(p.doc, p.doc.body.appendChild(build(p.doc, ['button', { 'data-mark-toggle': 'movie/3', 'data-mark-name': 'Salt', 'aria-pressed': 'true' }])));
      await settle();
      return told;
    };
    return { newRow: await go([], [CONTINUE[0], CONTINUE[2]]), into: await go([CONTINUE[0]], [CONTINUE[0], CONTINUE[2]]) };
  },

  // Undone with the focus in what the Undo takes away: a card, the banner, and a card the server's page brings.
  async undoFocus() {
    const p = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', [CONTINUE[0], episodeCard(23), CONTINUE[2]])]) });
    await p.pick(p.cards()[1], 'mark');
    await p.undo();
    const replaced = p.focus();
    const q = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    const b = q.doc.querySelector('[data-watch-dismiss]');
    b.focus();
    click(q.doc, b);
    await settle();
    await q.undo();
    const inBanner = q.doc.activeElement === b && q.banner();
    let keys = [23];
    const o = load(continuePage([CONTINUE[0], episodeCard(22)], true), { next: () => continuePage([CONTINUE[0], ...keys.map(episodeCard)], true) });
    await o.pick(o.cards()[1], 'mark');
    keys = [];
    await o.pick(o.cards()[1], 'mark');
    keys = [22];
    o.doc.activeElement = o.doc.body;
    await o.undo(0);
    const redrawn = o.focus();
    // Focus elsewhere stays there.
    o.doc.getElementById('watch-search').focus();
    await o.undo(1);
    return { replaced, inBanner, redrawn, elsewhere: o.doc.activeElement.id };
  },

  // Undo pressed while a newer change of the same title is on its way.
  async undoWaits() {
    const change = later();
    const p = load(watchPage([row('lib', [FILM()])]), { api: { 'DELETE /api/watch/watched/movie/1': () => change.answer } });
    await p.pick(p.cards()[0], 'mark');
    await p.pick(p.cards()[0], 'mark');
    await p.undo(0);
    const waiting = p.calls.length;
    change.send({ ok: true, changed: 1, watched: false });
    await settle();
    return { waiting, calls: p.calls };
  },

  // Dismiss pressed twice before the first is in.
  async dismissTwice() {
    const answer = later();
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]), { api: { 'POST /api/watch/dismissed/movie-1': () => answer.answer } });
    p.dismissed('movie-1');
    p.dismissed('movie-1');
    answer.send({ ok: true });
    await settle();
    return [p.calls, p.toasts.length];
  },

  // A film, and a show as a whole, marked from a card of the library, then marked as not watched.
  async notWatchedAgain() {
    const go = async (lib, at) => {
      let cont = CONTINUE;
      const p = load(watchPage([row('cont', cont), row('lib', [lib])]), { next: () => watchPage([row('cont', cont), row('lib', [lib])]) });
      cont = CONTINUE.filter((c, i) => i !== at);
      await p.pick(p.cards('lib')[0], 'mark');
      const marked = p.marks('cont');
      cont = CONTINUE;
      await p.pick(p.cards('lib')[0], 'mark');
      return { marked, again: p.marks('cont') };
    };
    return { film: await go(card('movie/1', 'Fieldwork', 'Movie'), 0), show: await go(card('show/2', 'Brightwater', 'TV'), 1) };
  },

  // An episode marked and its show dismissed before the Watch page answers, or before what it asks of a show past the page does.
  async dismissedMeanwhile() {
    const lib = (sub) => row('lib', [card('show/2', 'Brightwater', sub)]);
    const go = async (next, api, send) => {
      const p = load(watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE), lib('TV · 9 left')]), { next, api });
      await p.pick(p.cards('cont')[1], 'mark');
      p.dismissed('show-2');
      await settle();
      send();
      await settle();
      const sub = () => p.look('lib')[0][2];
      const dismissed = sub();
      await p.undo();
      return [dismissed, sub()];
    };
    const page = later();
    const search = later();
    const cont = [CONTINUE[0], CONTINUE[2]];
    return {
      page: await go((n) => (n === 1 ? page.answer : watchPage([row('cont', cont), lib('TV · 8 left')])), {}, () => page.send(watchPage([row('cont', CONTINUE), lib('TV · 9 left')]))),
      show: await go(() => watchPage([row('cont', cont)]), { 'GET /api/watch/search?q=Brightwater': () => search.answer, 'GET /api/watch/title/show-2': { watched: false } },
        () => search.send({ items: [{ ref: 'show-2', subtitle: 'TV · 8 left' }] })),
    };
  },

  // An episode marked, then its whole show as not watched, before the Watch page answers. The show's card stands past the page's.
  async wholeShowMeanwhile() {
    const first = later();
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [card('show/2', 'Brightwater', 'TV · 1 left', { watched: true })])]),
      { next: (n) => (n === 1 ? first.answer : watchPage([row('cont', CONTINUE)])),
        api: { 'GET /api/watch/search?q=Brightwater': { items: [{ ref: 'show-2', subtitle: 'TV' }] }, 'GET /api/watch/title/show-2': { watched: false } } });
    await p.pick(p.cards('cont')[1], 'mark');
    await p.pick(p.cards('lib')[0], 'mark');
    return p.look('lib')[0].slice(1, 3);
  },

  // What a show's page leaves alone when the server's page arrives.
  async leftAlone() {
    const next = episodesPage([21], '/watch/play/22');
    const go = async (before, opts) => {
      const p = load(episodesPage([22]), { next, ...opts });
      await before(p);
      await press(p, 21);
      return [p.doc.querySelector('.watch-btn--play').getAttribute('href'), pressed(p)];
    };
    const nothing = () => {};
    return {
      busyMark: await go((p) => press(p, 22), { api: { 'DELETE /api/watch/watched/episode/22': () => later().answer } }),
      loadingSeason: await go((p) => p.doc.querySelector('.watch-episodes').setAttribute('aria-busy', 'true'), {}),
      signedOut: await go(nothing, { redirected: true }),
      playFocused: await go((p) => p.doc.querySelector('.watch-btn--play').focus(), {}),
      followed: await go(nothing, {}),
    };
  },
};

// What to find more like each card's title by, where an AI answers, then where none does.
scenarios.like = async () => {
  const p = load(watchPage([row('row', [FILM(), card('episode/22', 'Brightwater', 'S1 · E2', { dismiss: 'show-2' }),
    card('', 'Salt', 'Movie', { ref: 'rec-5', pick: 'pick-9', tmdb: '77' }), card('', 'Reef', 'Movie', { ref: 'rec-6', pick: 'pick-8' })])]));
  p.ctx.MMWatch.search = { canLike: (ref) => /^(movie|show|tmdb-movie)-/.test(ref) };
  const refs = () => p.cards().map((c) => p.ctx.MMWatch.marks.likeRef(c.getAttribute('data-dismiss-ref') || c.querySelector('[data-title-ref]').getAttribute('data-title-ref'), c.querySelector('.watch-card__link')));
  const out = [refs()];
  p.ctx.MMWatch.search.canLike = () => false;
  return out.concat([refs()]);
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });
