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
  return ['article', { class: 'watch-card', id, ...(over.mark ? { 'data-mark': over.mark } : {}), ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', href: over.href || '/watch#title=' + over.ref, ...(over.ref ? { 'data-title-ref': over.ref } : {}), ...(over.pick ? { 'data-pick-ref': over.pick, 'data-kind': 'movie' } : {}) },
      ['span', { class: 'watch-card__poster' }, ['img', { src: '/api/poster/1' }], ['span', { class: 'watch-card__title' }, title],
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
  card('held', 'Held', { ref: 'movie-3', pick: 'rec-6' }),
  card('going', 'The Long Coast', { href: '/watch/play/22?resume=1', dismiss: 'show-2', mark: 'episode/22', pct: 40, sub: 'S2 · E4 · 23 min left' }),
];

/* A page at rest. *opts*: api (ref to its answer: data, a status or nothing yet), reduced, coarse, saveData. */
function load(opts = {}) {
  const doc = makeDocument();
  doc.createElement = ((make) => (tag) => { const n = make(tag); n.style.setProperty = () => {}; return n; })(doc.createElement);
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['input', { id: 'watch-search' }], ['div', { id: 'watch-browse' }, ['div', { class: 'watch-row__scroll' }, ...CARDS]]]));
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
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, WeakSet, Map, Set, Error, parseFloat, parseInt, AbortController, encodeURIComponent,
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
    doc, ctx, gets, aborted, posts, waiting, toasts, details, origins, store, fire,
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
};

scenarios[process.argv[2]]().then((out) => { process.stdout.write(JSON.stringify(out)); }, (err) => {
  console.error(err);
  process.exit(1);
});
