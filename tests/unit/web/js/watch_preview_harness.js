// Runs watch/core.js, keep.js and preview.js against the fake DOM and a fake clock, and prints one
// scenario's outcome as JSON. Usage: node watch_preview_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Object.defineProperty(Node.prototype, 'value', {
  get() { return Number(this.getAttribute('value')); },
  set(v) { this.setAttribute('value', v); },
});
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };
Node.prototype.click = function () { click(this.ownerDocument, this); };
Object.defineProperty(Node.prototype, 'naturalWidth', { get: () => 1280 });
Object.defineProperty(Node.prototype, 'naturalHeight', { get: () => 720 });

const YT = 'https://www.youtube-nocookie.com';
const FILM = {
  ref: 'movie-1', kind: 'movie', title: 'Fieldwork', in_library: true, library_ref: 'movie-1', year: 2024,
  runtime_min: 116, certificate: '15', genres: ['Drama'], ratings: { imdb: '7.4', rt: '88%', tmdb: 7.1 }, overview: 'A field.',
  backdrop_url: '/img/b.jpg', trailer_key: 'AbCdEfGhIjK', reason: null, leaving: null, keep_allowed: false, seasons: [],
  play: { label: 'Play', href: '/watch/play/1', progress_pct: null, minutes_left: null }, download: null,
};
const OUTSIDE = {
  ...FILM, ref: 'tmdb-movie-9', title: 'Salt', in_library: false, library_ref: null, play: null,
  download: { state: null, pct: null, service: 'radarr', tmdb_id: 9, request: { url: '/api/search/download', body: { media_type: 'movie', tmdb_id: 9, title: 'Salt' } } },
};
const season = (number, held) => ({ number, name: 'Season ' + number, in_library: held });
const showOf = (seasons) => ({
  ...OUTSIDE, ref: 'tmdb-tv-7', kind: 'show', title: 'Brightwater', season_count: seasons.length, seasons,
  download: { ...OUTSIDE.download, request: { url: '/api/search/download', body: { media_type: 'tv', tmdb_id: 7, title: 'Brightwater' } } },
});

function card(id, title, over = {}) {
  return ['article', { class: 'watch-card', id, ...(over.mark ? { 'data-mark': over.mark } : {}), ...(over.keep ? { 'data-keep-id': over.keep } : {}), ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', href: over.href || '/watch#title=' + over.ref, ...(over.ref ? { 'data-title-ref': over.ref } : {}), ...(over.pick ? { 'data-pick-ref': over.pick, 'data-kind': 'movie' } : {}) },
      ['span', { class: 'watch-card__poster' }, ['img', { src: '/api/poster/1' }], ['span', { class: 'watch-card__title' }, title],
        ...(over.leaving ? [['span', { class: 'watch-badge watch-badge--leaving' }, ['span', { class: 'watch-badge__long' }, over.leaving]]] : []),
        ...(over.pct ? [['progress', { class: 'watch-bar', value: String(over.pct) }]] : [])],
      ['span', { class: 'watch-card__sub' }, over.sub || 'Movie']],
    ['button', { class: 'watch-more', 'data-watch-more': '' }]];
}

const CARDS = [
  card('film', 'Fieldwork', { ref: 'movie-1', mark: 'movie/1' }),
  card('other', 'Orbit', { ref: 'movie-2', mark: 'movie/2' }),
  card('third', 'Third', { ref: 'movie-3' }),
  card('outside', 'Salt', { ref: 'tmdb-movie-9' }),
  card('show', 'Brightwater', { ref: 'tmdb-tv-7' }),
  card('pick', 'Pick', { ref: 'rec-5' }),
  card('season2', 'The Long Coast', { ref: 'show-2', keep: '202', sub: 'Season 2', leaving: 'Leaving in 9 days' }),
  card('season3', 'The Long Coast', { ref: 'show-2', keep: '203', sub: 'Season 3', leaving: 'Leaving in 3 days' }),
  card('keptSeason', 'The Long Coast', { ref: 'show-2', keep: '202', sub: 'Season 2' }),
  card('wholeShow', 'The Long Coast', { ref: 'show-2', sub: 'TV · 3 seasons' }),
  card('onAnother', 'The Long Coast', { href: '/watch/play/21', dismiss: 'show-2', sub: 'S1 · E4 · 23 min left' }),
  card('held', 'Held', { ref: 'movie-3', pick: 'rec-6' }),
  card('going', 'The Long Coast', { href: '/watch/play/22?resume=1', dismiss: 'show-2', mark: 'episode/22', pct: 40, sub: 'S2 · E4 · 23 min left' }),
];

/* A page at rest. *opts*: api (ref to its answer: data, a status or nothing yet), reduced, coarse, saveData. */
function load(opts = {}) {
  const doc = makeDocument();
  doc.createElement = ((make) => (tag) => { const n = make(tag); n.style.setProperty = (name, value) => { n.style[name] = value; }; return n; })(doc.createElement);
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['div', { id: 'watch-keep-menu', class: 'watch-keep-menu', hidden: '' }, ['button', { class: 'watch-keep-menu__item', 'data-days': '30' }, 'Keep 30 days']], ['input', { id: 'watch-search' }], ['div', { id: 'watch-browse' }, ['div', { class: 'watch-row__scroll' }, ...CARDS]]]));
  // A listener of the capture phase is kept apart: it has the event first.
  doc.addEventListener = ((on) => (type, fn, how) => on(how === true ? type + ' capture' : type, fn))(doc.addEventListener);
  doc.documentElement.clientWidth = 1280;
  doc.hidden = false;
  const fire = (target, type, e) => ((target.listeners || {})[type] || []).forEach((fn) => fn({ target, preventDefault() {}, ...e }));
  doc.dispatchEvent = (e) => { (doc.listeners[e.type] || []).forEach((fn) => fn(e)); };
  const winListeners = {};

  let clock = 1000;
  let timers = [];
  let ids = 0;
  const at = (ms, fn, every) => { timers.push({ id: ++ids, at: clock + ms, fn, every }); return ids; };
  const drop = (id) => { timers = timers.filter((t) => t.id !== id); };

  const gets = [];
  const aborted = [];
  const posts = [];
  const waiting = {};
  const toasts = [];
  const details = [];
  const origins = [];
  const answers = { 'movie-1': FILM, 'movie-2': { ...FILM, ref: 'movie-2', library_ref: 'movie-2', title: 'Orbit' }, 'movie-3': FILM, 'tmdb-movie-9': OUTSIDE, ...opts.api };
  const store = { ...opts.store };
  const api = {
    get(url, o) {
      const ref = url.replace('/api/watch/preview/', '');
      gets.push(ref);
      const answer = answers[ref];
      return new Promise((resolve, reject) => {
        o.signal.addEventListener('abort', () => { aborted.push(ref); if (!opts.deaf) reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); });
        if (typeof answer === 'number') reject(Object.assign(new Error('http'), { status: answer }));
        else if (answer) resolve(JSON.parse(JSON.stringify(answer)));
        else waiting[ref] = resolve;
      });
    },
    post(url, body) {
      posts.push([url, body]);
      return opts.post ? Promise.reject(Object.assign(new Error('http'), { status: opts.post })) : Promise.resolve({ ok: true });
    },
    messageOf: (_e, fallback) => fallback,
  };
  const watchers = new Set();
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, WeakSet, Map, Set, Error, parseFloat, parseInt, AbortController, encodeURIComponent,
    MutationObserver: class { constructor(fn) { this.fn = fn; } observe() { watchers.add(this); } disconnect() { watchers.delete(this); } },
    Date: { now: () => clock },
    performance: { now: () => clock },
    setTimeout: (fn, ms) => at(ms || 0, fn),
    clearTimeout: drop,
    setInterval: (fn, ms) => at(ms, fn, ms),
    clearInterval: drop,
    requestAnimationFrame: (fn) => at(16, fn),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingTop: '0' }),
    matchMedia: (q) => ({ matches: /reduced-motion/.test(q) ? !!opts.reduced : !opts.coarse }),
    navigator: { connection: { saveData: !!opts.saveData } },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }, removeItem: (k) => { delete store[k]; } },
    innerWidth: 1280, innerHeight: 820, scrollY: 0,
    location: { origin: 'https://mm.example' },
    addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
    UIFeedback: { toast: (m) => toasts.push(m) },
    MM: { api },
    MMWatch: {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/keep.js', ...(opts.grid ? ['watch/rows-load.js', 'watch/grid.js'] : []), 'watch/preview.js'].forEach((f) => {
    vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
    if (f === 'watch/core.js') ctx.MMWatch.detail = { open: (ref, o) => { details.push(ref); origins.push(o); doc.documentElement.classList.add('has-ttl'); } };
  });

  const h = {
    doc, ctx, gets, aborted, posts, waiting, toasts, details, origins, store, fire, answers,
    winFire: (type, e) => (winListeners[type] || []).forEach((fn) => fn(e || {})),
    /* Press *key*, from the document down to *target* and back up to the window. */
    key(key, target, over) {
      const e = { type: 'keydown', key, target, defaultPrevented: false, stopped: false, preventDefault() { e.defaultPrevented = true; }, stopPropagation() { e.stopped = true; }, ...over };
      const up = [];
      for (let a = target; a; a = a.parentNode) up.push(a.listeners.keydown);
      [doc.listeners['keydown capture'], ...up, doc.listeners.keydown, winListeners.keydown].forEach((fns) => { if (!e.stopped) (fns || []).forEach((fn) => fn(e)); });
      return e;
    },
    /* Let *ms* pass, timers and replies in their order. */
    async pass(ms) {
      const end = clock + ms;
      await settle();
      for (;;) {
        const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        clock = Math.max(clock, due.at);
        if (due.every) due.at = clock + due.every; else drop(due.id);
        due.fn();
        await settle();
      }
      clock = end;
    },
    link: (id) => doc.querySelector('#' + id + ' .watch-card__link'),
    x: 0,
    over(target, e) { h.x += 7; fire(doc, 'pointermove', { target, pointerType: 'mouse', clientX: h.x, clientY: 5, buttons: 0, ...e }); },
    /* Two close moves, then stillness: the mouse comes to rest. */
    async rest(id, ms) { h.over(h.link(id)); h.over(h.link(id), { clientX: h.x - 6 }); await h.pass(ms === undefined ? 600 : ms); },
    /* What an observer of the page is told of a change made to it. */
    changed() { [...watchers].forEach((w) => w.fn([])); },
    watchers: () => watchers.size,
    timers: () => timers.length,
    away() { h.over(doc.body); },
    pv: () => doc.querySelectorAll('.pv').filter((n) => n.classList.contains('is-open') && !n.classList.contains('is-fading') && !n.classList.contains('is-handed'))[0],
    title: () => { const n = h.pv(); return n ? n.querySelector('.pv__title').textContent : null; },
    text: (sel) => { const n = h.pv() && h.pv().querySelector(sel); return n ? n.textContent : null; },
    frame: () => doc.querySelector('iframe'),
    message: (data, over) => h.winFire('message', { source: h.frame() && h.frame().contentWindow, origin: YT, data: JSON.stringify(data), ...over }),
  };
  return h;
}

/* A preview of the film with its trailer's frame on the page. */
async function trailer(opts) {
  const h = load(opts);
  await h.rest('film', 600);
  await h.pass(1250);
  const frame = h.frame();
  if (frame) {
    h.said = [];
    frame.contentWindow = { postMessage: (m, to) => h.said.push([JSON.parse(m).func || JSON.parse(m).event, to]) };
    frame.listeners.load.forEach((fn) => fn());
  }
  return h;
}

const scenarios = {
  async passing() {
    const h = load();
    for (const id of ['film', 'other', 'third', 'outside']) { h.over(h.link(id)); await h.pass(120); }
    h.away();
    await h.pass(2000);
    return { gets: h.gets, open: h.title() };
  },
  async opens() {
    const h = load();
    await h.rest('film');
    const stops = h.pv().querySelectorAll('a, button').filter((n) => n.getAttribute('tabindex') !== '-1').length;
    return {
      gets: h.gets, title: h.title(), meta: h.pv().querySelectorAll('.pv__meta').map((n) => n.textContent), text: h.text('.pv__text'),
      play: h.pv().querySelector('.pv__first').getAttribute('href'), hiddenFromReaders: h.pv().getAttribute('aria-hidden'), stops,
    };
  },
  async leftBeforeReply() {
    const h = load({ api: { 'movie-1': undefined }, deaf: true });
    await h.rest('film', 450);
    const asked = h.gets.slice();
    h.away();
    await h.pass(100);
    h.waiting['movie-1'](FILM);
    await h.pass(1000);
    const open = h.title();
    await h.rest('film', 900);
    return { asked, aborted: h.aborted.slice(0, 1), open, askedAgain: h.gets.length };
  },
  async slow() {
    const h = load({ api: { 'movie-1': undefined } });
    await h.rest('film', 700);
    const early = { title: h.title(), text: h.text('.pv__text'), skels: h.pv().querySelectorAll('.skel').length > 0 };
    const n = h.pv();
    h.waiting['movie-1'](FILM);
    await h.pass(50);
    return { early, same: h.pv() === n, text: h.text('.pv__text'), skels: h.pv().querySelectorAll('.skel').length };
  },
  async failed() {
    const h = load({ api: { 'movie-1': 502 } });
    await h.rest('film', 900);
    const open = h.title();
    h.away();
    await h.pass(1000);
    await h.rest('film', 900);
    const soon = h.gets.length;
    h.away();
    await h.pass(61000);
    await h.rest('film', 900);
    return { open, soon, later: h.gets.length };
  },
  async slowThenFailed() {
    const h = load({ api: { 'movie-1': undefined } });
    const get = h.ctx.MM.api.get;
    let fail;
    h.ctx.MM.api.get = () => new Promise((_r, reject) => { fail = reject; });
    await h.rest('film', 800);
    const before = h.title();
    fail(new Error('down'));
    await h.pass(1000);
    h.ctx.MM.api.get = get;
    return { before, after: h.title(), layers: h.doc.querySelectorAll('.pv').length };
  },
  async refused() {
    const h = load({ api: { 'movie-1': 429 } });
    await h.rest('film', 900);
    await h.rest('other', 900);
    const soon = h.gets.slice();
    h.away();
    await h.pass(61000);
    await h.rest('other', 900);
    return { soon, later: h.gets.slice() };
  },
  async cached() {
    const h = load();
    const again = async () => { h.away(); await h.pass(1000); await h.rest('film'); return h.gets.length; };
    await h.rest('film');
    const out = { first: h.gets.length, second: await again() };
    h.doc.dispatchEvent(new h.ctx.CustomEvent('mm:watch-marked', { detail: { ref: 'movie-1' } }));
    out.marked = await again();
    h.doc.dispatchEvent(new h.ctx.CustomEvent('mm:title-changed', { detail: { ref: 'movie-1' } }));
    out.changed = await again();
    h.away();
    await h.pass(301000);
    await h.rest('film');
    out.stale = h.gets.length;
    return out;
  },
  async oneAtATime() {
    const h = load({ api: { 'movie-1': undefined } });
    await h.rest('film', 700);
    await h.rest('other', 900);
    h.waiting['movie-1'](FILM);
    await h.pass(100);
    return { gets: h.gets, aborted: h.aborted, title: h.title(), layers: h.doc.querySelectorAll('.pv').length };
  },
  async pressed() {
    const h = load({ api: { 'movie-1': undefined } });
    await h.rest('film', 450);
    h.fire(h.doc, 'pointerdown', { target: h.link('film') });
    h.waiting['movie-1'](FILM);
    await h.pass(1500);
    return { open: h.title(), aborted: h.aborted };
  },
  async touch() {
    const h = load();
    h.over(h.link('film'), { pointerType: 'touch' });
    h.over(h.link('film'), { pointerType: 'touch' });
    await h.pass(2000);
    const coarse = load({ coarse: true });
    await coarse.rest('film', 2000);
    return [h.gets, coarse.gets];
  },
  async scrolled() {
    const h = load();
    await h.rest('film');
    h.fire(h.doc, 'scroll', { target: h.doc });
    const open = h.title();
    h.over(h.link('other'));
    await h.pass(100);
    h.over(h.link('other'));
    await h.pass(1500);
    const tooSoon = h.title();
    await h.rest('other');
    return { open, tooSoon, later: h.title() };
  },
  async stillMouse() {
    const h = load();
    h.over(h.link('film'));
    h.away();
    await h.pass(1000);
    // A card moved under a mouse that has not.
    h.over(h.link('other'), { clientX: h.x });
    await h.pass(1500);
    return h.gets;
  },
  async keys() {
    const out = {};
    for (const key of ['Escape', 'Tab', 'a']) {
      const h = load();
      await h.rest('film');
      h.key(key, h.doc.body);
      out[key] = h.title();
    }
    return out;
  },
  async covered() {
    const h = load();
    const menu = h.doc.body.appendChild(build(h.doc, ['div', { class: 'watch-keep-menu is-open' }]));
    await h.rest('film', 900);
    menu.remove();
    const menuOpen = h.gets.slice();
    h.doc.documentElement.classList.add('has-ttl');
    await h.rest('film', 900);
    h.doc.documentElement.className = 'is-grid-moving';
    await h.rest('film', 900);
    return [menuOpen, h.gets];
  },
  async hidden() {
    const h = await trailer();
    const before = !!h.frame();
    h.doc.hidden = true;
    h.fire(h.doc, 'visibilitychange', {});
    return { before, layers: h.doc.querySelectorAll('.pv').length, frame: !!h.frame() };
  },
  async follows() {
    const h = load({ api: { 'tmdb-movie-9': { ...FILM, ref: 'movie-1', library_ref: 'movie-1' } } });
    await h.rest('outside');
    return { gets: h.gets, play: h.pv().querySelector('.pv__first').getAttribute('href') };
  },
  async hostile() {
    const h = load({ api: { 'movie-1': { ...FILM, backdrop_url: 'https://evil.example/x.jpg', trailer_key: 'x" onload=', play: { label: 'Play', href: 'javascript:alert(1)' } },
      'tmdb-movie-9': { ...OUTSIDE, download: { ...OUTSIDE.download, request: { url: '//evil.example/', body: {} } } } } });
    await h.rest('film');
    await h.pass(3000);
    const out = { first: !!h.pv().querySelector('.pv__first'), trailer: !!h.pv().querySelector('.pv__trailerbtn'),
      pictures: h.pv().querySelectorAll('.pv__art').map((n) => n.getAttribute('src')) };
    const art = h.pv().querySelector('.pv__art');
    art.onerror();
    out.then = h.pv().querySelectorAll('img').map((n) => n.getAttribute('src')).filter((s) => /evil/.test(s));
    await h.rest('outside', 900);
    h.pv().querySelector('.pv__first').click();
    out.posts = h.posts;
    return out;
  },
  async trailerWaits() {
    const h = load();
    await h.rest('film', 600);
    await h.pass(900);
    const early = !!h.frame();
    await h.pass(400);
    return { early, src: h.frame().getAttribute('src'), sandbox: h.frame().getAttribute('sandbox'), shown: h.pv().classList.contains('is-playing') };
  },
  async trailerPlays() {
    const h = await trailer();
    h.message({ event: 'onStateChange', info: 1 }, { origin: 'https://evil.example' });
    h.message({ event: 'onStateChange', info: 1 }, { source: {} });
    const forged = h.pv().classList.contains('is-playing');
    h.message({ event: 'infoDelivery', info: { playerState: 1 } });
    const playing = h.pv().classList.contains('is-playing');
    const label = () => h.pv().querySelector('.pv__trailerbtn').getAttribute('aria-label');
    const before = label();
    h.pv().querySelector('.pv__trailerbtn').click();
    const on = label();
    await h.pass(6000);
    const stillThere = !!h.frame();
    h.message({ event: 'onStateChange', info: 0 });
    return { forged, playing, before, on, said: h.said, stillThere, ended: !h.frame() && !h.pv().classList.contains('is-playing') };
  },
  async trailerLeft() {
    const h = await trailer();
    h.away();
    await h.pass(150);
    const said = h.said.map((s) => s[0]);
    await h.pass(1000);
    return { said, frame: !!h.frame() };
  },
  async trailerBad() {
    const out = {};
    for (const [name, spoil] of [['error', (h) => h.message({ event: 'onError', info: 150 })], ['blocked', (h) => h.message({ event: 'onAutoplayBlocked' })], ['stuck', (h) => h.pass(4100)]]) {
      const h = await trailer();
      await spoil(h);
      const gone = !h.frame() && h.title() === 'Fieldwork';
      h.away();
      await h.pass(1000);
      await h.rest('film', 3000);
      out[name] = [gone, !!h.frame()];
    }
    return out;
  },
  async trailerNot() {
    const out = {};
    for (const [name, opts] of Object.entries({ plain: {}, reduced: { reduced: true }, saving: { saveData: true }, off: { store: { 'mm.watch.hover-trailers': 'off' } },
      none: { api: { 'movie-1': { ...FILM, trailer_key: null } } } })) {
      const h = load(opts);
      await h.rest('film', 600);
      await h.pass(3000);
      out[name] = !!h.frame();
    }
    return out;
  },
  async switchOff() {
    const h = await trailer();
    h.pv().querySelector('[data-preview-more]').click();
    h.doc.querySelector('.pv-menu [data-act="trailers"]').click();
    const off = { frame: !!h.frame(), kept: h.store['mm.watch.hover-trailers'] };
    h.away();
    await h.pass(1000);
    await h.rest('other', 600);
    await h.pass(3000);
    return { ...off, next: !!h.frame(), title: h.title() };
  },
  async download() {
    const h = load();
    const said = [];
    h.doc.addEventListener('mm:title-changed', (e) => said.push([e.detail.ref, e.detail.download_state]));
    await h.rest('outside');
    const go = h.pv().querySelector('.pv__first');
    go.click();
    go.click();
    await h.pass(50);
    return { posts: h.posts, said, now: h.text('.pv__first') };
  },
  async downloadFails() {
    const out = {};
    for (const status of [500, 409]) {
      const h = load({ post: status });
      await h.rest('outside');
      h.pv().querySelector('.pv__first').click();
      await h.pass(50);
      const label = h.text('.pv__first');
      const again = h.pv().querySelector('.pv__first').getAttribute('aria-disabled') !== 'true';
      out[status] = [label, again, h.toasts.length];
    }
    return out;
  },
  async downloadShow() {
    const one = load({ api: { 'tmdb-tv-7': showOf([season(1, true), season(2, false)]) } });
    await one.rest('show');
    one.pv().querySelector('.pv__first').click();
    const many = load({ api: { 'tmdb-tv-7': showOf([season(1, false), season(2, false)]) } });
    await many.rest('show');
    const label = many.text('.pv__first');
    many.pv().querySelector('.pv__first').click();
    return { one: one.posts, label, many: many.posts, opened: many.details };
  },
  async going() {
    const h = load({ api: { 'show-2': undefined } });
    await h.rest('going', 900);
    const first = h.pv().querySelector('.pv__first');
    const out = { gets: h.gets, label: first.textContent, href: first.getAttribute('href') };
    h.waiting['show-2']({ ...FILM, kind: 'show', library_ref: 'show-2', title: 'The Long Coast', season_count: 3, play: { label: 'Resume S2 · E4', href: '/x' } });
    await h.pass(50);
    out.where = h.pv().querySelectorAll('.pv__meta')[1].textContent;
    const asked = [];
    h.doc.addEventListener('click', (e) => { ['data-mark-toggle', 'data-watch-dismiss', 'data-mark-name', 'aria-pressed'].forEach((a) => { if (e.target.hasAttribute(a) && !e.target.hasAttribute('data-preview-more')) asked.push([a, e.target.getAttribute(a)]); }); });
    const menu = () => h.pv().querySelector('[data-preview-more]').click();
    menu();
    out.menu = h.doc.querySelectorAll('.pv-menu .watch-keep-menu__item').map((n) => n.textContent);
    h.away();
    await h.pass(1000);
    out.pinned = h.title();
    h.doc.querySelector('.pv-menu [data-act="mark"]').click();
    menu();
    h.doc.querySelector('.pv-menu [data-act="dismiss"]').click();
    out.asked = asked;
    out.closed = h.title();
    return out;
  },
  async details() {
    const h = load({ api: { 'show-2': { ...FILM, kind: 'show', library_ref: 'show-2' } } });
    await h.rest('going', 900);
    h.pv().querySelectorAll('button').filter((b) => b.textContent === 'Details')[0].click();
    return { opened: h.details, open: h.title() };
  },
  async picture() {
    const h = load();
    await h.rest('film');
    let pressed = 0;
    h.link('film').addEventListener('click', () => { pressed += 1; });
    h.pv().querySelector('.pv__picture').click();
    h.pv().querySelector('.pv__text').click();
    const e = { target: h.pv().querySelector('.pv__picture'), button: 0, metaKey: true, preventDefault() { pressed = 'held'; } };
    h.pv().querySelector('.pv__shell').listeners.click.forEach((fn) => fn(e));
    return pressed;
  },
  async art() {
    const h = load();
    await h.rest('film');
    const srcs = [];
    for (;;) {
      const art = h.pv().querySelector('img.pv__art');
      if (!art) break;
      srcs.push(art.getAttribute('src'));
      art.onerror();
    }
    return { srcs, standIn: !!h.pv().querySelector('.pv__standin') };
  },
  async pick() {
    const reply = { ...OUTSIDE, ref: 'rec-5', reason: 'You finished Fieldwork.' };
    const h = load({ api: { 'rec-5': undefined } });
    await h.rest('pick', 800);
    const held = !!h.pv().querySelector('.pv__reason');
    h.waiting['rec-5'](reply);
    await h.pass(50);
    return { held, reason: h.text('.pv__reason') };
  },
  /* The page moves on, with a preview open and with one about to. */
  async movedOn() {
    const h = load();
    await h.rest('film');
    const before = h.title();
    h.ctx.MMWatch.preview.close();
    const after = [h.title(), h.link('film').classList.contains('is-previewed'), h.doc.querySelector('.pv').classList.contains('is-fading')];
    await h.pass(1000);
    const layers = h.doc.querySelectorAll('.pv').length;
    h.over(h.link('third'));
    await h.pass(100);
    h.ctx.MMWatch.preview.close();
    await h.pass(1500);
    return { before, after, layers, gets: h.gets };
  },
  async heldPick() {
    const h = load({ api: { 'rec-6': { ...FILM, ref: 'movie-3', library_ref: 'movie-3', reason: 'You finished Fieldwork.' } } });
    await h.rest('held');
    const reason = h.text('.pv__reason');
    h.pv().querySelectorAll('button').filter((b) => b.textContent === 'Details')[0].click();
    return { gets: h.gets, reason, opened: h.details, pick: h.origins[0].pick, kind: h.origins[0].kind };
  },
  /* Escape over a grid, from the page, from the search box and with a menu of the preview's open. */
  async escapeInGrid() {
    const out = {};
    for (const from of ['page', 'search', 'menu']) {
      const h = load({ grid: true });
      let backs = 0;
      let cleared = 0;
      const search = h.doc.querySelector('#watch-search');
      search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { cleared++; e.preventDefault(); } });
      h.ctx.scrollTo = h.ctx.scrollBy = () => {};
      h.ctx.MMWatch.grid.open({ id: 'movies', title: 'Movies', cards: [build(h.doc, card('ingrid', 'In grid', { ref: 'movie-1' }))], next: null, instant: true, back: () => { backs++; } });
      h.doc.querySelector('.watch-all__head').getBoundingClientRect = () => ({ bottom: 0 });
      await h.rest('ingrid');
      if (from === 'menu') h.pv().querySelector('[data-preview-more]').click();
      const seen = [h.title()];
      const steps = from === 'menu' ? 3 : 2;
      for (let i = 0; i < steps; i++) {
        const e = h.key('Escape', from === 'search' ? search : h.doc.body);
        seen.push([h.title(), e.defaultPrevented, backs, cleared]);
        await h.pass(100);
      }
      out[from] = seen;
    }
    return out;
  },
  async escapeComposing() {
    const h = load();
    await h.rest('film');
    const e = h.key('Escape', h.doc.body, { isComposing: true });
    return [h.title(), e.defaultPrevented];
  },
  async layer() {
    const h = load();
    await h.rest('film');
    return [h.pv().parentNode.className, h.pv().parentNode.parentNode === h.doc.body];
  },
  /* The focus left in the preview by a menu, with the ways out of it: Escape, a scroll and a tab that is hidden. */
  async focusBack() {
    const out = {};
    for (const how of ['escape', 'scroll', 'hidden', 'menuOpen']) {
      const h = load({ api: { 'movie-1': { ...FILM, keep_allowed: true, leaving: { keep_id: 5 } } } });
      await h.rest('film');
      h.pv().querySelector('.pv__keep').focus();
      if (how === 'menuOpen') h.pv().querySelector('[data-preview-more]').click();
      if (how === 'escape') h.key('Escape', h.doc.body);
      if (how === 'scroll' || how === 'menuOpen') h.fire(h.doc, 'scroll', { target: h.doc });
      if (how === 'hidden') { h.doc.hidden = true; h.fire(h.doc, 'visibilitychange', {}); }
      out[how] = h.doc.activeElement === h.link('film');
    }
    return out;
  },
  async focusElsewhere() {
    const h = load();
    h.doc.querySelector('#watch-search').focus();
    await h.rest('film');
    h.key('Escape', h.doc.body);
    return h.doc.activeElement.id;
  },
  async keptFromMenu() {
    const h = load({ api: { 'movie-1': { ...FILM, keep_allowed: true, leaving: { keep_id: 5 } } } });
    const days = h.doc.querySelector('#watch-keep-menu [data-days]');
    await h.rest('film');
    h.pv().querySelector('.pv__keep').focus();
    days.click();
    return [h.title(), h.doc.activeElement === h.doc.body];
  },
  /* Its card goes: taken out, or hidden with the view it is in. */
  async cardGone() {
    const out = {};
    for (const how of ['removed', 'hidden', 'stays']) {
      const h = await trailer();
      const before = h.title();
      if (how === 'removed') h.doc.querySelector('#film').remove();
      if (how === 'hidden') h.link('film').getClientRects = () => [];
      h.changed();
      const n = h.doc.querySelector('.pv');
      out[how] = [before, h.title(), n.classList.contains('is-fading'), n.classList.contains('is-open')];
      await h.pass(1000);
      out[how].push(!!h.frame(), h.watchers());
    }
    return out;
  },
  /* Closed by a key once its card has gone, before the page's change is told. */
  async escapeAfterGone() {
    const h = load();
    await h.rest('film');
    const set = [];
    h.pv().style.setProperty = (name, value) => set.push(name);
    h.doc.querySelector('#film').remove();
    h.key('Escape', h.doc.body);
    return [h.doc.querySelector('.pv').classList.contains('is-fading'), set];
  },
  /* Each way the page closes it: what is left then, how it goes, and what is left in the end. */
  async waysOut() {
    const out = {};
    const ways = {
      back: (h) => h.winFire('popstate'),
      resize: (h) => h.winFire('resize'),
      typing: (h) => h.fire(h.doc, 'input', { target: h.doc.querySelector('#watch-search') }),
      mouseOut: (h) => h.fire(h.doc.documentElement, 'pointerleave', {}),
      drag: (h) => h.fire(h.doc, 'dragstart', {}),
      pagehide: (h) => h.winFire('pagehide'),
    };
    for (const how of Object.keys(ways)) {
      const h = await trailer();
      const before = h.title();
      ways[how](h);
      const n = h.doc.querySelector('.pv');
      const then = !n ? 'gone' : n.classList.contains('is-fading') ? 'fades' : n.classList.contains('is-open') ? 'stays' : 'folds';
      const lifted = h.link('film').classList.contains('is-previewed');
      await h.pass(400);
      out[how] = [before, then, lifted, h.doc.querySelectorAll('.pv').length, !!h.frame()];
      // Called off too while it is only about to open.
      const armed = load();
      armed.over(armed.link('film'));
      armed.over(armed.link('film'), { clientX: armed.x - 6 });
      await armed.pass(200);
      ways[how](armed);
      await armed.pass(1500);
      out[how].push(armed.gets.length);
    }
    return out;
  },
  /* The mouse leaves for a menu of the preview's that hangs past the window. */
  async mouseOutPinned() {
    const h = load();
    await h.rest('film');
    h.pv().querySelector('[data-preview-more]').click();
    h.fire(h.doc.documentElement, 'pointerleave', {});
    return h.title();
  },
  /* A press that opens the detail: on its own card, on another's. */
  async cardPressed() {
    const out = {};
    for (const id of ['film', 'other']) {
      const h = load();
      await h.rest('film');
      h.link(id).addEventListener('click', () => h.ctx.MMWatch.detail.open('x', {}));
      h.link(id).click();
      const n = h.doc.querySelector('.pv');
      const handed = n.classList.contains('is-handed');
      await h.pass(400);
      out[id] = [handed, h.title(), h.doc.querySelectorAll('.pv').length];
    }
    return out;
  },
  async reopened() {
    const h = load();
    await h.rest('film');
    const n = h.pv();
    h.away();
    await h.pass(150);
    const folding = [h.title(), n.classList.contains('is-open')];
    h.over(h.link('film'));
    const back = [h.pv() === n, h.link('film').classList.contains('is-previewed')];
    await h.pass(1000);
    return { folding, back, later: h.pv() === n && n.classList.contains('is-settled'), layers: h.doc.querySelectorAll('.pv').length, gets: h.gets.length };
  },
  /* Still on one card when the time is up, but crossing it fast; then at rest. */
  async crossingFast() {
    const h = load();
    h.over(h.link('film'));
    await h.pass(300);
    h.over(h.link('film'), { clientX: 100 });
    await h.pass(40);
    h.over(h.link('film'), { clientX: 180 });
    await h.pass(60);
    const fast = h.gets.length;
    await h.pass(1000);
    return { fast, atRest: h.title() };
  },
  async noRoom() {
    const at = (left, top, w, hgt) => ({ left, top, width: w, height: hgt, right: left + w, bottom: top + hgt });
    const out = {};
    const boxes = { whole: at(0, 100, 150, 225), mostlyLeft: at(-80, 100, 150, 225), mostlyRight: at(1210, 100, 150, 225), mostlyUnder: at(0, 720, 150, 225), justOver: at(-70, 100, 150, 225) };
    for (const [name, box] of Object.entries(boxes)) {
      const h = load();
      h.link('film').querySelector('.watch-card__poster').getBoundingClientRect = () => box;
      await h.rest('film', 900);
      out[name] = h.title();
    }
    const h = load();
    h.ctx.innerHeight = 30;
    h.link('film').querySelector('.watch-card__poster').getBoundingClientRect = () => at(0, 8, 10, 10);
    await h.rest('film', 900);
    out.tooTall = [h.title(), h.doc.querySelectorAll('.pv').length];
    return out;
  },
  async dialogWhileArmed() {
    const h = load();
    h.over(h.link('film'));
    h.over(h.link('film'), { clientX: h.x - 6 });
    await h.pass(200);
    h.doc.body.appendChild(build(h.doc, ['div', { class: 'modal-backdrop' }]));
    await h.pass(1500);
    return h.gets;
  },
  async focusKept() {
    const h = load();
    await h.rest('film');
    let held = false;
    h.pv().listeners.mousedown.forEach((fn) => fn({ preventDefault() { held = true; } }));
    return held;
  },
  /* What is left to run after each end of a trailer. */
  async trailerTimers() {
    const ends = {
      left: (h) => h.away(),
      switchedOff: (h) => { h.pv().querySelector('[data-preview-more]').click(); h.doc.querySelector('.pv-menu [data-act="trailers"]').click(); },
      ended: (h) => h.message({ event: 'onStateChange', info: 0 }),
      error: (h) => h.message({ event: 'onError', info: 150 }),
      hidden: (h) => { h.doc.hidden = true; h.fire(h.doc, 'visibilitychange', {}); },
    };
    const out = {};
    for (const [name, end] of Object.entries(ends)) {
      const h = await trailer();
      const running = h.timers();
      end(h);
      await h.pass(1000);
      out[name] = [running, h.timers()];
    }
    const h = load();
    await h.rest('film', 600);
    h.away();
    await h.pass(500);
    out.beforeItStarts = [h.timers(), !!h.frame()];
    return out;
  },
  /* The view changes under a mouse at rest, and the browser says where it is: nothing opens until it moves. */
  async viewChanged() {
    const out = {};
    const views = {
      fresh: async () => {},
      // The mouse was moved over the detail, which closes; or where the page did not see it.
      detail: async (h) => {
        h.doc.documentElement.classList.add('has-ttl');
        h.over(h.doc.body, { clientX: 500 });
        h.doc.documentElement.classList.remove('has-ttl');
      },
      back: async (h) => { h.over(h.doc.body, { clientX: 400 }); h.winFire('popstate'); },
      moved: async (h) => { h.over(h.doc.body, { clientX: 400 }); h.ctx.MMWatch.preview.close(); },
    };
    for (const name of Object.keys(views)) {
      const h = load();
      if (name !== 'fresh') h.over(h.doc.body);
      await views[name](h);
      await h.pass(1000);
      h.over(h.link('film'), { clientX: 500 });
      h.over(h.link('film'), { clientX: 500 });
      await h.pass(1500);
      const still = h.title();
      h.over(h.link('film'), { clientX: 501 });
      await h.pass(1500);
      out[name] = [still, h.title()];
    }
    return out;
  },
  /* Two seasons of one show leaving: each card that shows the show, what its preview offers and keeps. */
  async seasonsLeaving() {
    const reply = { ...FILM, ref: 'show-2', kind: 'show', library_ref: 'show-2', title: 'The Long Coast', keep_allowed: true, leaving: { days: 3, keep_id: '203', season: 3 },
      leaving_seasons: [{ days: 3, keep_id: '203', season: 3 }, { days: 9, keep_id: '202', season: 2 }] };
    const h = load({ api: { 'show-2': reply } });
    const out = {};
    for (const id of ['season2', 'season3', 'wholeShow', 'going', 'onAnother', 'keptSeason']) {
      await h.rest(id, 900);
      const keep = h.pv().querySelector('.pv__keep');
      h.pv().querySelector('[data-preview-more]').click();
      const item = h.doc.querySelector('.pv-menu [data-act="keep"]');
      out[id] = [keep && keep.getAttribute('data-watch-keep'), keep && keep.getAttribute('aria-label'), item && item.textContent, h.text('.pv__leaving')];
      h.key('Escape', h.doc.body);
      h.key('Escape', h.doc.body);
      h.away();
      await h.pass(1000);
    }
    return { ...out, gets: h.gets };
  },
  /* A reply from when nothing was leaving: the card is newer. */
  async leavingSince() {
    const h = load({ api: { 'show-2': { ...FILM, kind: 'show', library_ref: 'show-2', keep_allowed: false, leaving_seasons: [] } } });
    await h.rest('season2', 900);
    return h.pv().querySelector('.pv__keep').getAttribute('data-watch-keep');
  },
  async keptSeasonToast() {
    const h = load({ api: { 'show-2': { ...FILM, kind: 'show', library_ref: 'show-2', keep_allowed: true, leaving: { days: 3, keep_id: '203', season: 3 }, leaving_seasons: [{ days: 3, keep_id: '203', season: 3 }, { days: 9, keep_id: '202', season: 2 }] } } });
    await h.rest('season2', 900);
    h.pv().querySelector('[data-preview-more]').click();
    h.doc.querySelector('.pv-menu [data-act="keep"]').click();
    h.doc.querySelector('#watch-keep-menu [data-days]').click();
    await h.pass(1000);
    const cards = ['season2', 'season3'].map((id) => !!h.doc.querySelector('#' + id + ' .watch-badge--leaving'));
    await h.rest('season3', 900);
    return { posts: h.posts, toasts: h.toasts, cards, asked: h.gets.length };
  },
  /* A Keep and an Undo made anywhere: what a whole show's card says after each, its reply held before it. */
  async keptElsewhere() {
    const three = { days: 3, keep_id: '203', season: 3 };
    const two = { days: 9, keep_id: '202', season: 2 };
    const reply = (seasons) => ({ ...FILM, ref: 'show-2', kind: 'show', library_ref: 'show-2', title: 'The Long Coast', keep_allowed: true, leaving: seasons[0] || null, leaving_seasons: seasons });
    const told = (h, id) => h.doc.dispatchEvent(new h.ctx.CustomEvent('mm:watch-kept', { detail: { id } }));
    const says = (h) => [h.title(), h.text('.pv__leaving'), h.pv() && h.pv().querySelector('.pv__keep') ? h.pv().querySelector('.pv__keep').getAttribute('data-watch-keep') : null];
    const out = {};
    // Held, with no preview open.
    let h = load({ api: { 'show-2': reply([three, two]), 'movie-1': FILM } });
    await h.rest('wholeShow', 900);
    h.away();
    await h.pass(1000);
    h.answers['show-2'] = reply([two]);
    told(h, '203');
    await h.rest('wholeShow', 900);
    out.kept = says(h);
    h.away();
    await h.pass(1000);
    h.answers['show-2'] = reply([three, two]);
    told(h, '203');
    await h.rest('wholeShow', 900);
    out.undone = says(h);
    // Open: what it offered goes at once, what is so now comes with the reply.
    delete h.answers['show-2'];
    told(h, '203');
    out.openAtOnce = says(h);
    h.waiting['show-2'](reply([two]));
    await h.pass(50);
    out.openThen = [...says(h), h.pv().querySelectorAll('.pv__art').length];
    // On its way: the answer is from before.
    h = load({ api: { 'show-2': undefined } });
    await h.rest('wholeShow', 450);
    const before = h.waiting['show-2'];
    h.answers['show-2'] = reply([two]);
    told(h, '203');
    before(reply([three, two]));
    await h.pass(1000);
    out.onItsWay = says(h);
    // A preview of another title is left alone, its menu open.
    h = load({ api: { 'show-2': reply([three, two]) } });
    await h.rest('wholeShow', 900);
    await h.rest('film', 900);
    h.pv().querySelector('[data-preview-more]').click();
    told(h, '203');
    out.other = [h.title(), h.gets.length, h.pv().querySelector('[data-preview-more]').getAttribute('aria-expanded')];
    // An admin's Keep, which names no season.
    h = load({ api: { 'show-2': reply([three, two]) } });
    await h.rest('wholeShow', 900);
    h.answers['show-2'] = reply([]);
    h.doc.dispatchEvent(new h.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', id: '', showRk: '2' } }));
    await h.pass(50);
    out.admin = says(h);
    return out;
  },
  /* Over a grid, whose header stands under the nav: cards clear of it, half under it, and over the rows again. */
  async underGridHead() {
    const at = (top) => ({ left: 400, top, width: 150, height: 225, right: 550, bottom: top + 225 });
    const out = {};
    for (const [name, top] of Object.entries({ clear: 130, justOver: 60, mostlyUnder: -10 })) {
      const h = load({ grid: true });
      h.ctx.scrollTo = h.ctx.scrollBy = () => {};
      h.ctx.MMWatch.grid.open({ id: 'movies', title: 'Movies', cards: [build(h.doc, card('ingrid', 'In grid', { ref: 'movie-1' }))], next: null, instant: true, back: () => {} });
      h.doc.querySelector('.watch-all__head').getBoundingClientRect = () => ({ left: 0, top: 52, width: 1280, height: 64, right: 1280, bottom: 116 });
      h.link('ingrid').querySelector('.watch-card__poster').getBoundingClientRect = () => at(top);
      const size = Object.getOwnPropertyDescriptor(Node.prototype, 'offsetHeight');
      Object.defineProperty(Node.prototype, 'offsetHeight', { configurable: true, get: () => 400 });
      await h.rest('ingrid', 900);
      out[name] = h.pv() ? h.pv().style['--pv-to'] : null;
      Object.defineProperty(Node.prototype, 'offsetHeight', size);
    }
    return out;
  },
  /* What opens over the cards from the keyboard, with a trailer playing in a preview. */
  async keyboardCovers() {
    const out = {};
    const ways = {
      details: (h) => h.ctx.MMWatch.detail.open('movie-9', {}),
      dialog: (h) => h.doc.body.appendChild(build(h.doc, ['div', { class: 'modal-backdrop' }])),
      menu: (h) => h.doc.body.appendChild(build(h.doc, ['div', { class: 'watch-keep-menu is-open' }])),
      ownMenu: (h) => h.pv().querySelector('[data-preview-more]').click(),
      nothing: () => {},
    };
    for (const how of Object.keys(ways)) {
      const h = await trailer();
      ways[how](h);
      h.changed();
      await h.pass(500);
      out[how] = [h.title(), !!h.frame()];
    }
    return out;
  },
  /* A press on the card, told of by the page before the preview has heard it. */
  async pressedThenTold() {
    const h = load();
    await h.rest('film');
    h.link('film').addEventListener('click', () => { h.ctx.MMWatch.detail.open('movie-1', {}); h.changed(); });
    h.link('film').click();
    return h.doc.querySelector('.pv').classList.contains('is-handed');
  },
  /* Deletions scheduled or cleared in the storage panel: the next preview and the open one. */
  async scheduled() {
    const three = { days: 3, keep_id: '203', season: 3 };
    const reply = (seasons) => ({ ...FILM, ref: 'show-2', kind: 'show', library_ref: 'show-2', title: 'The Long Coast', keep_allowed: true, leaving_seasons: seasons });
    const says = (h) => [h.text('.pv__leaving'), !!h.pv().querySelector('.pv__keep')];
    const h = load({ api: { 'show-2': reply([three]) } });
    const told = () => h.doc.dispatchEvent(new h.ctx.CustomEvent('mm:watch-scheduled', {}));
    await h.rest('wholeShow', 900);
    const out = { before: says(h) };
    h.away();
    await h.pass(1000);
    h.answers['show-2'] = reply([]);
    told();
    await h.rest('wholeShow', 900);
    out.cleared = says(h);
    h.answers['show-2'] = reply([three]);
    told();
    await h.pass(50);
    out.open = says(h);
    return out;
  },
};

scenarios[process.argv[2]]().then((out) => { process.stdout.write(JSON.stringify(out)); }, (err) => {
  console.error(err);
  process.exit(1);
});
