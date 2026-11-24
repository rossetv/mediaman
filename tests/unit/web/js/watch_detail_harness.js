// Runs watch/core.js, watch/address.js, watch/keep.js, watch/cast.js and watch/detail.js against the fake DOM, with the real
// _watch_detail.html as the overlay, and prints one scenario's outcome as JSON.
// Usage: node watch_detail_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const v8 = require('v8');

const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const WEB = path.resolve(__dirname, '../../../../src/mediaman/web');
const JS = path.join(WEB, 'static/js');

/* What the fake DOM leaves out and the overlay needs. */
Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.removeEventListener = function (t, fn) {
  this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn);
};
Node.prototype.toggleAttribute = function (k, on) { if (on) this.setAttribute(k, ''); else this.removeAttribute(k); };
Node.prototype.scrollTo = function (o) { this.scrolledTo = o; };
Node.prototype.select = function () { this.selected = true; };
Node.prototype.focus = function (o) { this.ownerDocument.activeElement = this; this.ownerDocument.focusOpts = o; };
Object.defineProperty(Node.prototype, 'id', {
  get() { return this.attrs.id || ''; },
  set(v) { this.attrs.id = String(v); },
});

/* The template's markup as fake-DOM nodes: tags, attributes and text only. */
function parseTemplate(doc, html) {
  html = html.replace(/\{#[\s\S]*?#\}|\{\{.*?\}\}|\{%.*?%\}/g, '');
  const top = doc.createElement('div');
  const stack = [top];
  const re = /<\/([a-z0-9]+)\s*>|<([a-z0-9]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>|([^<]+)/gi;
  let m;
  while ((m = re.exec(html))) {
    if (m[1]) { stack.pop(); continue; }
    if (m[4]) {
      if (m[4].trim()) stack[stack.length - 1].appendChild(doc.createTextNode(m[4].trim()));
      continue;
    }
    const node = doc.createElement(m[2]);
    (m[3].match(/[\w-]+(?:="[^"]*")?/g) || []).forEach((a) => {
      const eq = a.indexOf('=');
      if (eq < 0) node.setAttribute(a, ''); else node.setAttribute(a.slice(0, eq), a.slice(eq + 2, -1));
    });
    stack[stack.length - 1].appendChild(node);
    if (!/^(img|input|br|hr)$/i.test(m[2])) stack.push(node);
  }
  return top.children[0];
}

/* ── Titles the fake API knows ── */

function base(over) {
  return Object.assign({
    ok: true, kind: 'movie', in_library: false, library_ref: null, title: 'Meridian', year: 2026,
    year_end: null, runtime_min: 139, season_count: null, genres: ['Drama'], certificate: '12A',
    tagline: null, overview: 'A survey crew maps a drifting station.', backdrop_url: '/api/watch/img/tmdb/w1280/a.jpg',
    poster_url: null, director: null, ratings: { imdb: '7.9', rt: '88%', tmdb: 7.7, metascore: null },
    cast: [{ name: 'Mara Ellison', character: 'Ines', profile_url: null }], trailer_key: null, reason: null,
    play: null, leaving: null, keep_allowed: false, seasons: [], episodes: [], download: null, tmdb_configured: true,
  }, over);
}

const MOVIE_DL = base({
  ref: 'tmdb-movie-77', trailer_key: 'abcdefghijk',
  download: { state: null, service: 'radarr', tmdb_id: 77,
    request: { url: '/api/search/download', body: { media_type: 'movie', tmdb_id: 77, title: 'Meridian' } } },
});

const REC_DL = base({
  ref: 'rec-5', ai_pick: true, reason: 'You watched two slow mysteries.',
  download: { state: null, service: 'radarr', tmdb_id: 78, request: { url: '/api/recommended/5/download' } },
});

const OWN_PICK = base({
  ref: 'pick-9', ai_pick: true, reason: 'A slow mystery by the sea.', because_of: ['Salt', '<b>Harbour</b>'],
  download: { state: null, service: 'radarr', tmdb_id: 79, notify: false, request: { url: '/api/watch/picks/9/download' } },
});

function tvSeasons(held) {
  return [1, 2, 3].map((n) => ({ number: n, name: 'Season ' + n, episode_count: 8, in_library: held.includes(n), selected: false }));
}

const TV_DL = base({
  ref: 'tmdb-tv-90', kind: 'show', title: 'Hollow Pines', season_count: 3, seasons: tvSeasons([3]),
  download: { state: null, service: 'sonarr', tmdb_id: 90,
    request: { url: '/api/search/download', body: { media_type: 'tv', tmdb_id: 90, title: 'Hollow Pines' } } },
});

const LIB_SHOW = base({
  ref: 'show-300', kind: 'show', in_library: true, library_ref: 'show-300', title: 'Brightwater',
  play: { label: 'Resume S2 · E4', href: '/watch/play/41', progress_pct: 38, minutes_left: 32 },
  seasons: [{ number: 1, name: 'Season 1', episode_count: 2, in_library: true, selected: false },
    { number: 2, name: 'Season 2', episode_count: 2, in_library: true, selected: true }],
  episodes: [{ number: 1, title: 'Two One', minutes: 50, thumb_url: '/api/watch/thumb/21', state: 'watched', progress_pct: null, minutes_left: null, href: '/watch/play/21' }],
  keep_allowed: true, leaving: { days: 3, keep_id: '3010' },
});

const LIB_MOVIE = base({
  ref: 'movie-120', in_library: true, library_ref: 'movie-120', title: 'Glass Harbour',
  play: { label: 'Play', href: '/watch/play/120', progress_pct: null, minutes_left: null },
  keep_allowed: true, leaving: { days: 3, keep_id: '120' },
  director: 'Ada Rimmington', tagline: 'Some secrets are worth keeping.',
  ratings: { imdb: '7.6', rt: '91%', tmdb: 7.4, metascore: '74' },
});

/* A picture the browser finished with; a wide-art image judged unfit is marked first, as
   broken-poster.js does it. */
function settlePicture(img, how) {
  if (how !== 'wide') img.setAttribute('data-broken', '');
  (how === 'failed' ? img.onerror : img.onload)();
}
function hero(p) {
  const look = (name) => { const n = p.q('[data-ttl-part="' + name + '"]'); return n.hidden ? null : n.getAttribute('src'); };
  return { art: look('art'), fill: look('fill'), poster: look('poster'),
    loaded: p.q('[data-ttl-part="art"]').classList.contains('is-loaded') };
}

const FACTS = {
  ok: true, size: '16.2 GB', size_bytes: 1, added: '2026-02-04T10:00:00+00:00', last_watched: null,
  deletion: { execute_at: '2026-11-27T10:00:00+00:00', days: 3 }, keep: { state: 'none', label: 'Not kept' },
  actions: { keep_wrapper: { id: '120', is_tv: false, show_rk: '', has_rk: false, show_title: 'Glass Harbour' },
    delete: { kind: 'movie', id: '120', title: 'Glass Harbour' } },
};

class APIError extends Error {
  constructor(status, error, message) { super(message || error); this.status = status; this.error = error; this.data = message ? { message } : {}; }
}

/* ── The page ── */

function load(opts = {}) {
  const doc = makeDocument();
  const toasts = [];
  const timers = [];
  const gets = [];
  const posts = [];
  const events = [];
  const history = [];
  const routes = Object.assign({}, opts.routes);
  const posted = opts.posted || {};
  const loc = { origin: 'https://mediaman.test', pathname: '/watch', search: '', hash: opts.hash || '' };
  const explored = [];
  const winListeners = {};
  const entries = (opts.before || []).concat({ state: opts.state || null, search: '', hash: loc.hash });
  let at = entries.length - 1;

  function setUrl(url) {
    const u = new URL(url, loc.origin);
    loc.search = u.search;
    loc.hash = u.hash;
  }
  const url = () => loc.pathname + loc.search + loc.hash;
  const entry = (state) => ({ state, search: loc.search, hash: loc.hash });

  const card = (ref, title) => ['article', { class: 'watch-card', 'data-keep-id': '' },
    ['a', { class: 'watch-card__link', href: '/watch/movie/1', 'data-title-ref': ref },
      ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, title]]],
    ['button', { class: 'watch-keep', 'data-watch-keep': '999', 'aria-label': 'Keep ' + title }, 'Keep']];

  const page = build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['input', { id: 'watch-search' }],
    ['div', { class: 'watch-rows' }, card('tmdb-movie-77', 'Meridian'), card('show-300', 'Brightwater')],
    ['section', { class: 'watch-list', id: 'watch-list', 'aria-label': 'Library list' },
      ['table', {}, ['tbody', {}, ...(opts.listRows || []).map((id) => ['tr', { class: 'wl-row', 'data-row-key': 'id:' + id },
        ['td', {}, ['a', { class: 'wl-title', href: '/watch#title=movie-' + id, 'data-title-ref': 'movie-' + id }, 'Film ' + id]],
        ['td', { class: 'wl-actions' }, ['button', { class: 'btn btn--danger btn--sm', id: 'del-' + id }, 'Delete']]])]]],
    ['div', { id: 'watch-detail' }],
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', role: 'menu', hidden: '' },
      ['button', { class: 'watch-keep-menu__item', 'data-days': '7' }, '7 days']]]);
  doc.body.appendChild(page);
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' }, JSON.stringify({ is_admin: !!opts.admin })]));
  doc.getElementById('watch-detail').appendChild(
    parseTemplate(doc, fs.readFileSync(path.join(WEB, 'templates/_watch_detail.html'), 'utf8')));
  /* Pictures load as they are asked for, unless the scenario settles them itself. */
  if (opts.pictures !== 'pending') {
    ['art', 'fill', 'poster'].map((name) => doc.getElementById('ttl').querySelector('[data-ttl-part="' + name + '"]')).forEach((img) => {
      const set = img.setAttribute.bind(img);
      img.setAttribute = (k, v) => { set(k, v); if (k === 'src') settlePicture(img, opts.pictures || 'wide'); };
    });
  }
  doc.dispatchEvent = (e) => {
    if (e.type === 'mm:title-changed') events.push(e.detail);
    (doc.listeners[e.type] || []).forEach((fn) => fn(e));
  };

  /* The page's clock: real unless the scenario steps it. */
  const clock = { now: Date.UTC(2026, 10, 24, 12) };
  class FakeDate extends Date { static now() { return clock.now; } }
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, WeakMap, Error, isNaN,
    Date: opts.clock ? FakeDate : Date,
    parseInt, parseFloat, encodeURIComponent, decodeURIComponent,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    AbortController: class { constructor() { this.signal = {}; } abort() { this.signal.aborted = true; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: (q) => ({ matches: /min-width/.test(q) || (!!opts.still && /reduced-motion/.test(q)) }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900,
    innerWidth: 1280,
    location: loc,
    history: {
      state: opts.state || null,
      pushState(state, _t, to) { history.push(['push', to]); this.state = state; setUrl(to); entries.splice(++at); entries[at] = entry(state); },
      replaceState(state, _t, to) {
        if (to !== undefined && to !== url()) { history.push(['replace', to]); setUrl(to); }
        this.state = state;
        entries[at] = entry(state);
      },
      back() { this.go(-1); },
      go(by) {
        history.push(['go', by]);
        at += by;
        this.state = entries[at].state;
        loc.search = entries[at].search;
        loc.hash = entries[at].hash;
        (winListeners.popstate || []).slice().forEach((fn) => fn({}));
      },
    },
    navigator: { clipboard: opts.clipboard === false ? undefined : { writeText: () => (opts.clipboardFails ? Promise.reject(new Error('denied')) : Promise.resolve()) } },
    prompt: () => { throw new Error('window.prompt must not be used'); },
    UIFeedback: { toast: (message, o) => toasts.push({ message, o }) },
    MM: { api: {
      APIError,
      get: (url) => {
        gets.push(url);
        const r = routes[url];
        if (r instanceof Error) return Promise.reject(r);
        if (typeof r === 'function') return r();
        return r ? Promise.resolve(JSON.parse(JSON.stringify(r))) : Promise.reject(new APIError(404, 'not_found'));
      },
      post: (url, body) => {
        posts.push({ url, body });
        const r = posted[url];
        if (r instanceof Error) return Promise.reject(r);
        if (typeof r === 'function') return r();
        return Promise.resolve(r || { ok: true });
      },
      messageOf: (e, f) => (e && e.data && e.data.message) || f,
    } },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn); },
    URLSearchParams,
    scrollY: 0,
    scrollTo: () => {},
    sessionStorage: { getItem: () => null, setItem: () => {} },
  };
  ctx.window = ctx;
  ctx.MMWatch = {};
  if (opts.adminApi) ctx.MMWatch.admin = opts.adminApi;
  vm.createContext(ctx);
  /* A stand-in for Explore: the pages of a person and of a genre, in the address. */
  const explore = () => {
    const address = ctx.MMWatch.address;
    const open = (params) => {
      address.push('explore', address.url(Object.assign({ person: null, genre: null }, params), ''));
      explored.push(params);
    };
    address.layer({ name: 'explore', depth: 20, params: ['person', 'genre'],
      read: (here) => here.params.get('person') || here.params.get('genre'), open: () => {}, close: () => {} });
    ctx.MMWatch.explore = { openPerson: (who) => open({ person: who.person_id }), openGenre: (id) => open({ genre: id }),
      person: () => new Promise(() => {}) };
  };
  const run = () => {
    ['core/dom.js', 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/cast.js', 'watch/detail.js']
      .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));
    explore();
    ctx.MMWatch.address.start();
  };
  const ttl = () => doc.getElementById('ttl');
  const q = (sel) => ttl().querySelector(sel);
  const text = (sel) => { const n = q(sel); return n ? n.textContent.trim() : null; };
  /* Run the timers due now (the next poll, the Copied reset). */
  const tick = () => { const due = timers.splice(0); due.forEach((t) => t.fn && t.fn()); };
  const key = (k, extra) => {
    const e = Object.assign({ key: k, target: doc.activeElement, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, extra);
    (doc.listeners.keydown || []).forEach((fn) => fn(e));
    return e;
  };
  const popstate = () => (winListeners.popstate || []).forEach((fn) => fn({}));
  const message = (e) => (winListeners.message || []).forEach((fn) => fn(e));
  return { message, ctx, doc, toasts, timers, gets, posts, events, history, entries, explored, loc, routes, clock, run, ttl, q, text, tick, key, popstate };
}

function primary(p) {
  const n = p.q('[data-ttl-part="actions"] [data-ttl-act="primary"]');
  return n ? { text: n.textContent.trim(), cls: n.className, disabled: n.getAttribute('aria-disabled') === 'true' } : null;
}

async function openCard(p, ref) {
  click(p.doc, p.doc.querySelector('[data-title-ref="' + ref + '"]'));
  await settle();
}

const scenarios = {
  async openFromCard() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    const link = p.doc.querySelector('[data-title-ref="tmdb-movie-77"]');
    const e = { target: link, button: 0, metaKey: true, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    (p.doc.listeners.click || []).forEach((fn) => fn(e));
    const keepClick = p.doc.querySelector('.watch-card .watch-keep');
    click(p.doc, keepClick);
    const afterModifiers = { hidden: p.ttl().hidden, gets: p.gets.length };
    click(p.doc, link);
    const loading = { hidden: p.ttl().hidden, title: p.text('#ttl-title'), busy: p.ttl().getAttribute('aria-busy'),
      focus: p.doc.activeElement === p.q('#ttl-title'), locked: p.doc.documentElement.classList.contains('has-ttl'),
      hash: p.loc.hash, history: p.history.slice() };
    await settle();
    return { afterModifiers, loading, loaded: { busy: p.ttl().getAttribute('aria-busy'), meta: p.text('[data-ttl-part="meta"]'), primary: primary(p) } };
  },

  async backCloses() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('.ttl__close'));
    const link = p.doc.querySelector('[data-title-ref="tmdb-movie-77"]');
    const out = { history: p.history.slice(), leaving: p.ttl().classList.contains('is-leaving'), focusOnCard: p.doc.activeElement === link };
    p.tick();
    out.hidden = p.ttl().hidden;
    out.unlocked = !p.doc.documentElement.classList.contains('has-ttl');
    return out;
  },

  // A title closed, opened again with Forward, then closed with Escape.
  async forwardThenEscape() {
    const p = load({ routes: { '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'show-300');
    p.key('Escape');
    p.tick();
    p.doc.activeElement = p.doc.body;
    p.ctx.history.go(1);
    await settle();
    p.key('Escape');
    return { closed: p.ttl().classList.contains('is-leaving'), focus: p.doc.activeElement.getAttribute('data-title-ref') };
  },

  // Close pressed twice before the browser's popstate comes, then what the second press of a
  // double click on it hits: a card's link, at once and after the sheet has gone.
  async doubleClose() {
    const p = load({ clock: true, routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    const go = p.ctx.history.go;
    let backs = 0;
    p.ctx.history.go = function () { backs++; };
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('.ttl__close'));
    click(p.doc, p.q('.ttl__close'));
    go.call(p.ctx.history, -1);
    const press = (detail) => {
      const e = { target: p.doc.querySelector('[data-title-ref="show-300"]'), button: 0, detail, defaultPrevented: false, stopped: false,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
      (p.doc.listeners.click || []).forEach((fn) => fn(e));
      return { open: p.loc.hash !== '', prevented: e.defaultPrevented, stopped: e.stopped };
    };
    p.clock.now += 250;
    const second = press(2);
    p.clock.now += 2000;
    const later = press(2);
    return { backs, second, later };
  },

  // From a title to a person of it, to one of their titles, to a person of that, and all the way
  // back: what shows, where it stands and what was asked for.
  async peopleAndBack() {
    const cast = (id) => [{ name: 'Mara Ellison', character: 'Ines', profile_url: null, person_id: id }];
    const first = Object.assign({}, MOVIE_DL, { cast: cast(9001) });
    const second = Object.assign({}, LIB_SHOW, { cast: cast(9002) });
    let overview = 'As it was.';
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': () => Promise.resolve(Object.assign({}, first, { overview })),
      '/api/watch/title/show-300': second } });
    p.run();
    const look = () => ({ at: p.loc.search + p.loc.hash, open: !p.ttl().hidden && !/is-(aside|leaving)/.test(p.ttl().className),
      title: p.text('#ttl-title'), scroll: p.ttl().scrollTop, focus: p.doc.activeElement.getAttribute('data-ttl-act'),
      busy: p.ttl().getAttribute('aria-busy'), overview: p.text('.ttl__overview') });
    const press = () => click(p.doc, p.q('.ttl-cast__item'));
    await openCard(p, 'tmdb-movie-77');
    p.ttl().scrollTop = 420;
    press();
    const out = { person: look(), aside: p.ttl().classList.contains('is-aside'),
      unlocked: !p.doc.documentElement.classList.contains('has-ttl'), who: p.explored.slice() };
    p.tick();
    out.hidden = p.ttl().hidden;
    await openCard(p, 'show-300');
    press();
    p.tick();
    p.gets.length = 0;
    overview = 'As it is now.';
    p.ctx.history.back();
    out.second = look();
    p.ctx.history.back();
    p.tick();
    out.personAgain = look();
    p.ctx.history.back();
    out.first = look();
    await settle();
    out.upToDate = look();
    out.gets = p.gets.slice();
    p.key('Escape');
    p.tick();
    out.closed = { open: !p.ttl().hidden, at: p.loc.search + p.loc.hash };
    return out;
  },

  // A title opened from inside another, and Back to that one.
  async titleOnTitle() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL, '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    p.ttl().scrollTop = 420;
    p.ctx.MMWatch.detail.open('show-300');
    await settle();
    const second = { title: p.text('#ttl-title'), scroll: p.ttl().scrollTop };
    p.ctx.history.back();
    return { second, first: { title: p.text('#ttl-title'), scroll: p.ttl().scrollTop, hidden: p.ttl().hidden, play: p.text('.watch-btn--play') } };
  },

  // Back before the sheet has finished stepping aside.
  async backMidway() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, {
      cast: [{ name: 'Mara Ellison', character: null, profile_url: null, person_id: 9001 }] }) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('.ttl-cast__item'));
    p.ctx.history.back();
    p.tick();
    return { hidden: p.ttl().hidden, aside: p.ttl().classList.contains('is-aside'), title: p.text('#ttl-title'),
      cast: p.text('.ttl-cast__name'), locked: p.doc.documentElement.classList.contains('has-ttl') };
  },

  // Genres: those Explore has are links to it, a press with a modifier is the browser's.
  async genreLinks() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, {
      genres: ['Drama', 'Sci-Fi & Fantasy', 'Noir'], genre_refs: [{ name: 'Drama', genre: 'drama' }, { name: 'Sci-Fi & Fantasy', genre: 'science-fiction' }] }) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const links = p.ttl().querySelectorAll('.ttl__genre');
    const e = { target: links[0], button: 0, metaKey: true, preventDefault() { this.defaultPrevented = true; } };
    links[0].listeners.click.forEach((fn) => fn(e));
    const modified = { prevented: !!e.defaultPrevented, explored: p.explored.slice() };
    click(p.doc, links[1]);
    p.tick();
    const gone = p.ttl().hidden;
    p.ctx.history.back();
    return { meta: p.text('[data-ttl-part="meta"]'), hrefs: links.map((a) => a.getAttribute('href')), modified, explored: p.explored, gone,
      focus: p.doc.activeElement.getAttribute('data-ttl-act'), at: p.loc.search + p.loc.hash };
  },

  // Escape with a person's card open.
  async escapeAndTheCard() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, {
      cast: [{ name: 'Mara Ellison', character: null, profile_url: null, person_id: 9001 }] }) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    p.q('.cast-card').style.setProperty = () => {};
    p.ttl().listeners.pointermove.forEach((fn) => fn({ target: p.q('.ttl-cast__item'), pointerType: 'mouse' }));
    p.tick();
    const look = () => ({ card: p.q('.cast-card').classList.contains('is-open'), title: !p.ttl().classList.contains('is-leaving') });
    const out = [look()];
    p.key('Escape');
    out.push(look());
    p.key('Escape');
    out.push(look());
    return out;
  },

  async directLink() {
    const p = load({ hash: '#title=movie-120', routes: { '/api/watch/title/movie-120': LIB_MOVIE } });
    p.run();
    await settle();
    const opened = { hidden: p.ttl().hidden, gets: p.gets.slice() };
    p.key('Escape');
    return { opened, history: p.history.slice(), hash: p.loc.hash };
  },

  // A person's own pick: what it says, its download, and what the admin is offered.
  async ownPick() {
    let asked = 0;
    const emailed = Object.assign({}, OWN_PICK, { download: Object.assign({}, OWN_PICK.download, { state: 'queued', notify: true }) });
    const p = load({ admin: true, hash: '#title=pick-9', routes: { '/api/watch/title/pick-9': () => Promise.resolve(asked++ ? emailed : OWN_PICK) } });
    p.run();
    await settle();
    const out = { label: p.text('.ttl__reason .ai-label'), icon: !!p.q('.ttl__reason .ai-label .ico'), reason: p.text('.ttl__reason'), because: p.text('.ttl__because'),
      share: !!p.q('[data-ttl-act="share"]') };
    click(p.doc, p.q('.ttl__download'));
    p.q('.ttl__download').listeners.click.forEach((fn) => fn({}));
    await settle();
    return Object.assign(out, { posts: p.posts.map((x) => x.url), under: p.text('[data-ttl-part="under"]') });
  },

  // What a pick's box says with less to say: no reason, and one title behind it.
  async pickWithLess() {
    const open = async (over) => {
      const p = load({ hash: '#title=pick-9', routes: { '/api/watch/title/pick-9': Object.assign({}, OWN_PICK, over) } });
      p.run();
      await settle();
      return p.text('.ttl__reason');
    };
    return { label: await open({ reason: null, because_of: [] }), one: await open({ reason: null, because_of: ['Salt'] }) };
  },

  // A pick the library holds, opened from its card under the library's ref: before why is known,
  // once it is, opened again by Forward and by a reload, and when why cannot be had.
  async pickInLibrary() {
    let known;
    const routes = { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/title/pick-9': () => new Promise((resolve, reject) => { known = { resolve, reject }; }) };
    const p = load({ routes });
    p.run();
    const link = p.doc.querySelector('[data-title-ref="show-300"]');
    link.setAttribute('data-title-ref', 'movie-120');
    link.setAttribute('data-pick-ref', 'pick-9');
    click(p.doc, link);
    await settle();
    const held = !!p.q('.ttl__actions .watch-btn--play');
    p.tick();
    await settle();
    const late = { play: !!p.q('.ttl__actions .watch-btn--play'), box: p.text('.ttl__reason'), busy: p.q('.ttl__reason').getAttribute('aria-busy') };
    known.resolve(OWN_PICK);
    await settle();
    const then = { box: p.text('.ttl__reason'), busy: p.q('.ttl__reason').getAttribute('aria-busy') };
    p.key('Escape');
    p.tick();
    p.ctx.history.go(1);
    await settle();
    const forward = p.text('.ttl__because');
    const q = load({ hash: '#title=movie-120', state: p.ctx.history.state, routes: Object.assign({}, routes, { '/api/watch/title/pick-9': OWN_PICK }) });
    q.run();
    await settle();
    const r = load({ routes });
    r.run();
    const plain = r.doc.querySelector('[data-title-ref="show-300"]');
    plain.setAttribute('data-title-ref', 'movie-120');
    click(r.doc, plain);
    await settle();
    const f = load({ hash: '#title=movie-120', state: p.ctx.history.state, routes });
    f.run();
    await settle();
    known.reject(new APIError(404, 'not_found'));
    await settle();
    return { held, late, then, forward, reloaded: q.text('.ttl__because'), refused: f.text('.ttl__reason'), notAPick: r.text('.ttl__reason') };
  },

  async oldRecommendationLink() {
    const p = load({ hash: '#recommendation-5', routes: { '/api/watch/title/rec-5': REC_DL } });
    p.run();
    await settle();
    return { history: p.history.slice(), gets: p.gets.slice(), reason: p.text('.ttl__reason') };
  },

  // A pick's ref says nothing of its kind: the card that opens it does, and a cold link knows none.
  async recKind() {
    const pick = Object.assign({}, REC_DL, { kind: 'show' });
    const p = load({ routes: { '/api/watch/title/rec-5': pick } });
    p.run();
    p.doc.querySelector('.watch-rows').appendChild(build(p.doc,
      ['a', { class: 'watch-card__link', href: '/watch#title=rec-5', 'data-title-ref': 'rec-5', 'data-kind': 'show' }, 'Pick']));
    click(p.doc, p.doc.querySelector('[data-title-ref="rec-5"]'));
    const fromCard = p.text('[data-ttl-part="kind"]');
    await settle();
    const cold = load({ hash: '#title=rec-5', routes: { '/api/watch/title/rec-5': pick } });
    cold.run();
    const fromColdLink = cold.text('[data-ttl-part="kind"]');
    await settle();
    return { fromCard, fromColdLink, loaded: cold.text('[data-ttl-part="kind"]') };
  },

  // Scrolled down inside one title, closed, then another opened (and one opened over another).
  async opensAtTop() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL, '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    p.ttl().scrollTop = 62;
    click(p.doc, p.q('.ttl__close'));
    p.tick();
    await openCard(p, 'show-300');
    const afterClose = p.ttl().scrollTop;
    p.ttl().scrollTop = 140;
    p.ctx.MMWatch.detail.open('tmdb-movie-77', {});
    return { afterClose, overAnother: p.ttl().scrollTop };
  },

  async libraryMatchSwitches() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { in_library: true, library_ref: 'movie-120' }),
      '/api/watch/title/movie-120': LIB_MOVIE,
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    return { gets: p.gets.slice(), hash: p.loc.hash, primary: primary(p) };
  },

  async escapeOrder() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('[data-ttl-act="trailer"]'));
    const frame = p.q('.ttl__player iframe');
    const trailer = p.q('[data-ttl-act="trailer"]');
    const playing = {
      trailer: p.ttl().classList.contains('is-trailer'),
      button: [trailer.textContent.trim(), trailer.hasAttribute('inert')],
      row: p.q('[data-ttl-part="actions"]').children.map((n) => n.textContent.trim()),
      back: p.text('.ttl__back'), focus: p.doc.activeElement === p.q('.ttl__back'), tabbable: !p.q('.ttl__back').hasAttribute('tabindex'),
      src: frame.getAttribute('src'), sandbox: frame.getAttribute('sandbox'),
      allow: frame.getAttribute('allow'), referrer: frame.getAttribute('referrerpolicy'),
    };
    p.key('Escape');
    p.tick();
    const afterFirst = { trailer: p.ttl().classList.contains('is-trailer'), frame: !!p.q('.ttl__player iframe'),
      open: !p.ttl().classList.contains('is-leaving'), button: [trailer.textContent.trim(), trailer.hasAttribute('inert')],
      focus: p.doc.activeElement === trailer, tabbable: !p.q('.ttl__back').hasAttribute('tabindex') };
    p.key('Escape');
    return { playing, afterFirst, closedSecond: p.ttl().classList.contains('is-leaving') };
  },

  // The browser's Back and Forward, the bar's two buttons and a reload while a trailer plays, in
  // a title opened from a card or from a link: what is on show and where the address stands.
  async trailerHistory() {
    const routes = { '/api/watch/title/tmdb-movie-77': MOVIE_DL };
    const look = (p) => ({ open: !p.ttl().hidden && !p.ttl().classList.contains('is-leaving'), trailer: p.ttl().classList.contains('is-trailer'), hash: p.loc.hash });
    const playing = async (opts) => {
      const p = load(Object.assign({ routes }, opts));
      p.run();
      if (opts && opts.hash) await settle(); else await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="trailer"]'));
      return p;
    };
    const out = {};
    let p = await playing();
    p.ctx.history.back();
    out.back = look(p);
    p.ctx.history.go(1);
    out.forward = look(p);
    p.ctx.history.back();
    p.ctx.history.back();
    out.backTwice = look(p);
    p = await playing();
    const go = p.ctx.history.go;
    let backs = 0;
    p.ctx.history.go = () => { backs++; };
    click(p.doc, p.q('.ttl__back'));
    click(p.doc, p.q('.ttl__back'));
    out.button = { asked: backs };
    go.call(p.ctx.history, -1);
    Object.assign(out.button, look(p));
    for (const [name, opts] of Object.entries({ close: {}, closeLink: { hash: '#title=tmdb-movie-77' } })) {
      p = await playing(opts);
      click(p.doc, p.q('.ttl__close'));
      out[name] = look(p);
    }
    const last = (await playing()).entries;
    p = load({ routes, hash: '#title=tmdb-movie-77', state: last[2].state, before: last.slice(0, 2) });
    p.run();
    await settle();
    out.reload = look(p);
    p.ctx.history.back();
    out.reloadBack = look(p);
    return out;
  },

  // What the player's frame says, and from where; the trailer goes back to the artwork only when
  // the frame itself says it has ended, and focus is moved only from what went with it.
  async trailerEnds() {
    const origin = 'https://www.youtube-nocookie.com';
    const run = async (make, focus) => {
      const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="trailer"]'));
      const frame = p.q('.ttl__player iframe');
      const sent = [];
      frame.contentWindow = { postMessage: (data, to) => sent.push([JSON.parse(data).event, JSON.parse(data).func, to]) };
      (frame.listeners.load || []).forEach((fn) => fn({}));
      if (focus) p.q(focus).focus();
      p.message(Object.assign({ origin, source: frame.contentWindow }, make));
      return { playing: p.ttl().classList.contains('is-trailer'), sent, focus: p.doc.activeElement.getAttribute('data-ttl-act') };
    };
    const data = (d) => ({ data: JSON.stringify(d) });
    return {
      ended: await run(data({ event: 'onStateChange', info: 0 })),
      endedInfo: await run(data({ event: 'infoDelivery', info: { playerState: 0 } }), '[data-ttl-act="primary"]'),
      paused: await run(data({ event: 'onStateChange', info: 2 })),
      otherOrigin: await run(Object.assign(data({ event: 'onStateChange', info: 0 }), { origin: 'https://evil.example' })),
      otherFrame: await run(Object.assign(data({ event: 'onStateChange', info: 0 }), { source: {} })),
      junk: await run({ data: '{' }),
    };
  },

  async trailerInView() {
    const run = async (still) => {
      const p = load({ still, routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="trailer"]'));
      return p.ttl().scrolledTo;
    };
    return { moving: await run(false), still: await run(true) };
  },

  // How the head's height eases as its trailer comes and goes: each ease, whether it was cut
  // short, and where the head stood when it was closed halfway through opening.
  async trailerHeight() {
    const out = {};
    for (const [name, steps] of Object.entries({
      openAndClose: ['press', 'end', 'press', 'end'], closedHalfway: ['press', 'press'], anotherTitle: ['press', 'another'],
      still: ['press', 'press'],
    })) {
      const p = load({ still: name === 'still', routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL, '/api/watch/title/movie-120': LIB_MOVIE } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      const eases = [];
      let easing = null;
      const head = p.q('.ttl__hero');
      Object.defineProperty(head, 'offsetHeight', { get: () => (easing ? 400 : p.ttl().classList.contains('is-trailer') ? 552 : 234) });
      head.animate = (frames, o) => {
        const ease = { from: frames[0].minHeight, to: frames[1].minHeight, ms: o.duration, cut: false };
        easing = ease;
        eases.push(ease);
        return { cancel() { if (easing === ease) { easing = null; ease.cut = true; } } };
      };
      steps.forEach((step) => {
        if (step === 'press') click(p.doc, p.q(p.ttl().classList.contains('is-trailer') ? '.ttl__back' : '[data-ttl-act="trailer"]'));
        else if (step === 'end') easing = null;
        else p.ctx.MMWatch.detail.open('movie-120', {});
      });
      out[name] = eases;
    }
    return out;
  },

  // A download ends while its trailer plays: the title goes on under its library ref, and Back, then
  // Close, go out of it one step at a time without loading it again.
  async scannedDuringTrailer() {
    let status = { state: 'queued' };
    let title = Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'queued', answered: true }) });
    const library = Object.assign({}, LIB_MOVIE, { trailer_key: 'abcdefghijk' });
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': () => Promise.resolve(JSON.parse(JSON.stringify(title))),
      '/api/watch/title/movie-120': library,
      '/api/download/status?service=radarr&tmdb_id=77': () => Promise.resolve(status),
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('[data-ttl-act="trailer"]'));
    status = { state: 'ready' };
    title = Object.assign({}, MOVIE_DL, { in_library: true, library_ref: 'movie-120', download: null });
    p.tick();
    await settle();
    const loaded = { hash: p.loc.hash, gets: p.gets.length };
    p.ctx.history.back();
    await settle();
    const afterBack = { hash: p.loc.hash, trailer: p.ttl().classList.contains('is-trailer'), busy: p.ttl().getAttribute('aria-busy'), again: p.gets.length - loaded.gets };
    p.key('Escape');
    return { loaded: loaded.hash, afterBack, closed: p.ttl().classList.contains('is-leaving'), end: p.loc.hash };
  },

  // A library show's own toggles, then what a mark made elsewhere or here brings back.
  async marks() {
    const eps = (n, watched) => [{ number: 1, key: String(n), title: 'Ep ' + n, minutes: 45, state: watched ? 'watched' : 'none', watched, href: '/watch/play/' + n }];
    let seen = false;
    const show = () => Object.assign({}, LIB_SHOW, { watched: false, mark_kind: 'show', mark_key: '300',
      play: { label: seen ? 'Play S1 · E2' : 'Play S1 · E1', href: '/watch/play/1' },
      seasons: LIB_SHOW.seasons.map((s) => Object.assign({}, s, { key: '30' + s.number, watched: seen && s.number === 1 })), episodes: eps(21, false) });
    const p = load({ routes: {
      '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show()))),
      '/api/watch/title/show-300/season/1': () => Promise.resolve({ ok: true, key: '301', watched: seen, episodes: eps(11, seen) }),
      '/api/watch/title/tmdb-movie-77': MOVIE_DL,
    } });
    p.run();
    const painted = [];
    p.ctx.MMWatch.marks = { paint: (mark, watched) => painted.push([mark, watched]) };
    await openCard(p, 'show-300');
    const toggles = () => p.ttl().querySelectorAll('[data-mark-toggle]').map((b) => [b.getAttribute('data-mark-toggle'), b.getAttribute('aria-pressed'), b.getAttribute('data-mark-ref')]);
    const opened = toggles();
    const inLink = p.ttl().querySelectorAll('[data-mark-toggle]').filter((b) => b.closest('a')).length;
    click(p.doc, p.q('[data-season="1"]'));
    await settle();
    const picked = toggles();
    p.q('[data-mark-toggle="episode/11"]').focus();
    seen = true;
    const gets = p.gets.length;
    const say = (ref) => p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref } });
    say('show-9');
    const otherTitle = p.gets.length - gets;
    say('show-300');
    await settle();
    const after = { toggles: toggles(), play: primary(p).text, episode: p.text('.watch-ep__title'), tab: p.q('[data-season="1"]').getAttribute('aria-label'),
      focus: p.doc.activeElement.getAttribute('data-mark-toggle'), painted };
    const q = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    q.run();
    await openCard(q, 'tmdb-movie-77');
    return { opened, inLink, picked, otherTitle, after, outside: q.ttl().querySelectorAll('[data-mark-toggle]').length };
  },

  async noTrailerKey() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { trailer_key: null }) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    return { trailer: !!p.q('[data-ttl-act="trailer"]'), primary: primary(p).text };
  },

  async keepMenuEscapeFirst() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    // keep.js handles Escape for its open menu first and marks the event handled.
    const e = p.key('Escape', { defaultPrevented: true });
    return { open: !p.ttl().hidden && !p.ttl().classList.contains('is-leaving'), handled: e.defaultPrevented };
  },

  async loadFails() {
    let fail = true;
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': () => (fail ? Promise.reject(new APIError(502, 'tmdb_request_failed')) : Promise.resolve(MOVIE_DL)) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const failed = { line: p.text('.ttl__failed-line'), buttons: p.ttl().querySelectorAll('.ttl__failed button').map((b) => b.textContent.trim()) };
    fail = false;
    click(p.doc, p.ttl().querySelectorAll('.ttl__failed button')[0]);
    await settle();
    return { failed, retried: primary(p) };
  },

  async loadNotFound() {
    const p = load({});
    p.run();
    await openCard(p, 'show-300');
    return p.ttl().querySelectorAll('.ttl__failed button').map((b) => b.textContent.trim());
  },

  async downloadMovie() {
    let status = { state: 'downloading', progress: 42, size_done: '3.1 GB', size_total: '7.4 GB', eta: '~6 min remaining' };
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': MOVIE_DL,
      '/api/download/status?service=radarr&tmdb_id=77': () => Promise.resolve(status),
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const before = primary(p);
    p.doc.activeElement = p.q('[data-ttl-act="primary"]');
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    const busy = primary(p);
    await settle();
    const queued = { primary: primary(p), note: p.text('.ttl__note'), focusKept: p.doc.activeElement === p.q('[data-ttl-act="primary"]'),
      email: p.ttl().querySelectorAll('.ttl__note').some((n) => /email/.test(n.textContent)) };
    p.tick();
    await settle();
    const bar = p.q('.dl-progress progress');
    const downloading = { primary: primary(p), bar: bar && bar.value, line: p.text('.dl-progress__line') };
    status = { state: 'ready' };
    p.tick();
    await settle();
    return { before, busy, posts: p.posts, queued, downloading, done: { primary: primary(p), note: p.text('.ttl__note') },
      events: p.events, toasts: p.toasts.map((t) => t.message), reloads: p.gets.filter((g) => g === '/api/watch/title/tmdb-movie-77').length };
  },

  // A download the server once reported as queued is gone by the next reply,
  // which the Arr answered (answered: true) or did not (answered: false).
  async queuedThenGoneOnReopen() {
    async function run(answered) {
      let state = 'queued';
      const p = load({ routes: {
        '/api/watch/title/tmdb-movie-77': () => Promise.resolve(JSON.parse(JSON.stringify(
          Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state, answered: state ? true : answered }) })))),
        '/api/download/status?service=radarr&tmdb_id=77': { state: 'queued' },
      } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      const before = primary(p).text;
      p.ctx.MMWatch.detail.close();
      state = null;
      await openCard(p, 'tmdb-movie-77');
      return { before, after: primary(p).text, events: p.events };
    }
    return { answered: await run(true), unanswered: await run(false) };
  },

  // The status poll answers unknown (the Arr did not answer) for a queued download.
  async queuedThenUnknownOnPoll() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'queued', answered: true }) }),
      '/api/download/status?service=radarr&tmdb_id=77': { state: 'unknown' },
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const before = primary(p).text;
    p.tick();
    await settle();
    return { before, after: primary(p).text, events: p.events, pollsLeft: p.timers.filter((t) => t.fn).map((t) => t.ms) };
  },

  // The status poll answers searching (gone from the Arr's queue) for a title held as queued:
  // the title is asked again, at most once a minute, and its reply decides.
  async searchingRechecks() {
    let state = 'queued';
    const p = load({ clock: true, routes: {
      '/api/watch/title/tmdb-movie-77': () => Promise.resolve(JSON.parse(JSON.stringify(
        Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state, answered: true }) })))),
      '/api/download/status?service=radarr&tmdb_id=77': { state: 'searching' },
    } });
    const titleGets = () => p.gets.filter((g) => g === '/api/watch/title/tmdb-movie-77').length;
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const opened = titleGets();
    p.tick();
    await settle();
    const firstPoll = { gets: titleGets() - opened, primary: primary(p).text };
    p.clock.now += 5000;
    p.tick();
    await settle();
    const soonAfter = { gets: titleGets() - opened, primary: primary(p).text };
    state = null;
    p.clock.now += 60000;
    p.tick();
    await settle();
    const afterMinute = { gets: titleGets() - opened, primary: primary(p).text };
    const statusGets = () => p.gets.filter((g) => g.startsWith('/api/download/status')).length;
    const polled = statusGets();
    p.tick();
    await settle();
    return { firstPoll, soonAfter, afterMinute, events: p.events, pollsLeft: statusGets() - polled };
  },

  // Just after this page's own Download, the Arr not knowing of it yet is not taken as gone.
  async justDownloadedGrace() {
    let title = Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { answered: true }) });
    const p = load({ clock: true, routes: {
      '/api/watch/title/tmdb-movie-77': () => Promise.resolve(JSON.parse(JSON.stringify(title))),
      '/api/download/status?service=radarr&tmdb_id=77': { state: 'searching' },
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    await settle();
    p.clock.now += 5000;
    p.tick();
    await settle();
    const afterPoll = primary(p).text;
    p.ctx.MMWatch.detail.close();
    await openCard(p, 'tmdb-movie-77');
    const afterReopen = primary(p).text;
    p.clock.now += 30000;
    p.tick();
    await settle();
    return { afterPoll, afterReopen, afterGrace: primary(p).text };
  },

  // A title that arrives downloading shows its percent before any poll.
  async arrivesDownloading() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'downloading', pct: 63, answered: true }) }),
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const bar = p.q('.dl-progress progress');
    return { primary: primary(p).text, bar: bar && bar.value };
  },

  // A queued download that never moves is polled every 5 s for two minutes, then every 30 s;
  // closing stops it.
  async pollBacksOff() {
    const p = load({ clock: true, routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'queued', answered: true }) }),
      '/api/download/status?service=radarr&tmdb_id=77': { state: 'queued' },
    } });
    const next = () => p.timers.filter((t) => t.fn).map((t) => t.ms);
    p.run();
    await openCard(p, 'tmdb-movie-77');
    p.tick();
    await settle();
    const early = next();
    p.clock.now += 121000;
    p.tick();
    await settle();
    const late = next();
    p.ctx.MMWatch.detail.close();
    p.tick();
    const statusGets = p.gets.filter((g) => g.startsWith('/api/download/status')).length;
    p.tick();
    await settle();
    return { early, late, primary: primary(p) && primary(p).text,
      pollsAfterClose: p.gets.filter((g) => g.startsWith('/api/download/status')).length - statusGets };
  },

  // Radarr holding the film with its file is not a download this page just finished.
  async heldInLibrary() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'in_library', answered: true }) }),
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    return { primary: primary(p).text, notes: p.ttl().querySelectorAll('.ttl__note').map((n) => n.textContent) };
  },

  // While the title loads, the kicker already says what kind it is.
  async loadingKind() {
    const pending = () => new Promise(() => {});
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': pending, '/api/watch/title/show-300': pending } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-movie-77', { title: 'Meridian' });
    const movie = p.text('[data-ttl-part="kind"]');
    p.ctx.MMWatch.detail.open('show-300', { title: 'Brightwater' });
    return { movie, show: p.text('[data-ttl-part="kind"]') };
  },

  async showDirector() {
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': Object.assign({}, TV_DL, { director: 'Ada Rimmington' }) } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    return { credit: p.text('.ttl__credit') };
  },

  // A poll that redraws the actions mid-share keeps Sharing… and lets no second request go.
  async shareDuringPoll() {
    let release = null;
    let pct = 10;
    const p = load({ admin: true,
      routes: {
        '/api/watch/title/rec-5': Object.assign({}, REC_DL, { download: Object.assign({}, REC_DL.download, { state: 'downloading', pct: 10, answered: true }) }),
        '/api/download/status?service=radarr&tmdb_id=78': () => Promise.resolve({ state: 'downloading', progress: pct }),
      },
      posted: { '/api/recommended/5/share-token': () => new Promise((resolve) => { release = resolve; }) } });
    p.run();
    p.ctx.MMWatch.detail.open('rec-5', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="share"]'));
    pct = 20;
    p.tick();
    await settle();
    const during = { label: p.text('[data-ttl-act="share"]'), busy: p.q('[data-ttl-act="share"]').getAttribute('aria-disabled'),
      primary: primary(p).text };
    click(p.doc, p.q('[data-ttl-act="share"]'));
    const shares = p.posts.filter((x) => x.url === '/api/recommended/5/share-token').length;
    release({ ok: true, share_url: 'https://example.test/download/tok' });
    await settle();
    await settle();
    const copied = p.text('[data-ttl-act="share"]');
    pct = 30;
    p.timers.forEach((t) => { if (t.ms === 2000) t.fn = null; });
    p.tick();
    await settle();
    return { during, shares, copied, copiedAfterPoll: p.text('[data-ttl-act="share"]'), primary: primary(p).text };
  },

  async downloadFails() {
    const p = load({
      routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL },
      posted: { '/api/search/download': new APIError(502, 'arr_failed', 'Radarr did not answer') },
    });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    await settle();
    const err = { text: p.text('.ttl__error'), role: p.q('.ttl__error').getAttribute('role'), primary: primary(p) };
    click(p.doc, p.q('[data-ttl-act="retry"]'));
    await settle();
    return { err, posts: p.posts.length, events: p.events };
  },

  // The Arr says the title is already there: no error, no Try again, busy until the title's own reply
  // answers, and that reply decides the panel and the cards.
  async downloadAlreadyThere() {
    const run = async (again, later) => {
      let gets = 0;
      let answer;
      const p = load({
        routes: { '/api/watch/title/tmdb-movie-77': () => {
          gets += 1;
          if (gets === 1) return Promise.resolve(MOVIE_DL);
          return gets === 2 ? new Promise((resolve, reject) => { answer = () => (again instanceof Error ? reject(again) : resolve(again)); }) : Promise.resolve(later || again);
        }, '/api/watch/title/movie-120': LIB_MOVIE },
        posted: { '/api/search/download': new APIError(409, 'conflict', "'Meridian' is already in Radarr") },
      });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      const shown = () => ({ primary: primary(p), error: !!p.q('.ttl__error'), retry: !!p.q('[data-ttl-act="retry"]') });
      await settle();
      const at409 = shown();
      const eventsAt409 = p.events.length;
      answer();
      await settle();
      const due = (ms) => p.timers.filter((t) => t.fn && t.ms === ms).length;
      const out = { at409, eventsAt409, refetched: shown(), gets: p.gets.slice(), events: p.events.slice(), toasts: p.toasts.map((t) => t.message),
        polls: due(5000), slowPolls: due(30000) };
      if (!later) return out;
      p.tick();
      await settle();
      return Object.assign(out, { later: primary(p).text, laterEvents: p.events.slice(out.events.length), pollsLater: [due(5000), due(30000)] });
    };
    const dl = (state, pct) => Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state, pct, answered: true }) });
    return {
      queued: await run(dl('queued', 0)),
      downloading: await run(dl('downloading', 40)),
      none: await run(dl(null, null), dl(null, null)),
      noneYet: await run(dl(null, null), Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { answered: false }) })),
      failed: await run(new Error('offline'), dl('queued', 0)),
      inLibrary: await run(Object.assign({}, MOVIE_DL, { in_library: true, library_ref: 'movie-120' })),
    };
  },

  // A refused download whose title could not be asked again, then reopened: already there until the
  // Arr answers that it holds nothing, or says what it does with it.
  async trackedReopened() {
    const run = async (first, ref, over) => {
      let offline = false;
      let data = first;
      const p = load({
        routes: {
          ['/api/watch/title/' + ref]: () => (offline ? Promise.reject(new Error('offline')) : Promise.resolve(data)),
          '/api/watch/title/show-300': LIB_SHOW,
        },
        posted: { '/api/search/download': () => { offline = true; return Promise.reject(new APIError(409, 'conflict', 'already there')); } },
      });
      p.run();
      p.ctx.MMWatch.detail.open(ref, {});
      await settle();
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await settle();
      const out = [primary(p).text];
      p.ctx.MMWatch.detail.open('show-300', {});
      await settle();
      offline = false;
      data = Object.assign({}, first, { download: Object.assign({}, first.download, over) });
      p.ctx.MMWatch.detail.open(ref, {});
      await settle();
      return out.concat(primary(p).text);
    };
    const each = async (first, ref) => ({
      gone: await run(first, ref, { state: null, answered: true }),
      noAnswer: await run(first, ref, { state: null, answered: false }),
      queued: await run(first, ref, { state: 'queued', pct: 0, answered: true }),
    });
    return { movie: await each(MOVIE_DL, 'tmdb-movie-77'), show: await each(TV_DL, 'tmdb-tv-90') };
  },

  // A pick accepted, or refused and asked again, while another title is open: the event names its own
  // title, and reopening it tells its cards again.
  async eventAfterAnotherOpened() {
    const run = async (outcome) => {
      let finish;
      let data = REC_DL;
      const p = load({
        routes: { '/api/watch/title/rec-5': () => Promise.resolve(data), '/api/watch/title/show-300': LIB_SHOW },
        posted: { '/api/recommended/5/download': () => new Promise((resolve, reject) => {
          finish = () => (outcome === 'accepted' ? resolve({ ok: true }) : reject(new APIError(409, 'conflict', 'already there')));
        }) },
      });
      p.run();
      p.ctx.MMWatch.detail.open('rec-5', {});
      await settle();
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await settle();
      p.ctx.MMWatch.detail.open('show-300', {});
      await settle();
      data = Object.assign({}, REC_DL, { download: Object.assign({}, REC_DL.download, { state: 'queued', pct: 0, answered: true }) });
      finish();
      await settle();
      const away = p.events.slice();
      p.ctx.MMWatch.detail.open('rec-5', {});
      await settle();
      return { away, reopened: p.events.slice(away.length) };
    };
    return { accepted: await run('accepted'), refused: await run('refused') };
  },

  // Opening a title tells its cards what its reply says, even where this page knew nothing: gone from
  // the Arr, or in the library under another ref; an Arr that did not answer says nothing.
  async openedTellsCards() {
    const open = async (data) => {
      const p = load({ routes: { '/api/watch/title/rec-5': data, '/api/watch/title/movie-120': LIB_MOVIE } });
      p.run();
      p.ctx.MMWatch.detail.open('rec-5', {});
      await settle();
      return p.events.map((e) => [e.ref, e.download_state, e.title]);
    };
    const dl = (answered) => Object.assign({}, REC_DL, { download: Object.assign({}, REC_DL.download, { answered }) });
    return {
      gone: await open(dl(true)),
      noAnswer: await open(dl(false)),
      inLibrary: await open(Object.assign({}, REC_DL, { in_library: true, library_ref: 'movie-120', download: null })),
    };
  },

  // Sonarr already tracks the show: "Already in Sonarr", unless the title asked for again says more, to
  // the panel and to the cards.
  async downloadShowTracked() {
    const run = async (again) => {
      let gets = 0;
      const p = load({
        routes: { '/api/watch/title/tmdb-tv-90': () => {
          gets += 1;
          return Promise.resolve(gets === 1 ? TV_DL : again);
        }, '/api/watch/title/show-300': LIB_SHOW },
        posted: { '/api/search/download': new APIError(409, 'conflict', "'Hollow Pines' is already tracked by Sonarr") },
      });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await Promise.resolve();
      await Promise.resolve();
      const at409 = { primary: primary(p).text, retry: !!p.q('[data-ttl-act="retry"]') };
      await settle();
      const out = { at409, refetched: primary(p).text, retry: !!p.q('[data-ttl-act="retry"]'), events: p.events.slice(), gets: p.gets.slice(),
        polls: p.timers.filter((t) => t.fn && t.ms === 5000).length };
      p.ctx.MMWatch.detail.open('show-300', {});
      await settle();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      return Object.assign(out, { reopened: primary(p).text, reopenedEvents: p.events.slice(out.events.length) });
    };
    const dl = (state, pct) => Object.assign({}, TV_DL, {
      sonarr_tracked: true, download: Object.assign({}, TV_DL.download, { state, pct, answered: true }),
    });
    return {
      tracked: await run(Object.assign({}, TV_DL, { sonarr_tracked: true })),
      queued: await run(dl('queued', 0)),
      downloading: await run(dl('downloading', 40)),
      allThere: await run(dl('in_library', null)),
      inLibrary: await run(Object.assign({}, TV_DL, { in_library: true, library_ref: 'show-300' })),
    };
  },

  // A show's download fails; its picked seasons are then changed (or not), and it is redrawn.
  async downloadErrorPlace() {
    const p = load({
      routes: { '/api/watch/title/tmdb-tv-90': TV_DL },
      posted: { '/api/search/download': new APIError(502, 'sonarr_failed', 'Sonarr did not answer.') },
    });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    await settle();
    return p.q('[data-ttl-part="under"]').children.map((n) => n.className);
  },

  async downloadErrorThenPicks() {
    const run = async (press) => {
      const p = load({
        routes: { '/api/watch/title/tmdb-tv-90': TV_DL, '/api/watch/title/show-300': LIB_SHOW },
        posted: { '/api/search/download': new APIError(422, 'not_listed', "Season 2 isn't in Sonarr's listing.") },
      });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await settle();
      const shown = () => { const e = p.q('[data-ttl-part="under"] .ttl__error'); return e ? (e.hasAttribute('inert') ? 'held' : 'shown') : 'none'; };
      const before = shown();
      const button = p.q(press);
      button.focus();
      click(p.doc, button);
      const out = { before, after: shown(), focusKept: p.doc.activeElement === button, download: primary(p).text };
      p.ctx.MMWatch.detail.open('show-300', {});
      await settle();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      return Object.assign(out, { reopened: shown() });
    };
    return {
      chip: await run('[data-ttl-act="season-2"]'),
      clear: await run('[data-ttl-act="pick-none"]'),
      unchanged: await run('[data-ttl-act="pick-all"]'),
    };
  },

  async recDownload() {
    const emailed = Object.assign({}, REC_DL, { download: Object.assign({}, REC_DL.download, { state: 'queued', notify: true }) });
    let asked = 0;
    const p = load({ routes: { '/api/watch/title/rec-5': () => Promise.resolve(asked++ ? emailed : REC_DL) } });
    p.run();
    p.popstate(); // nothing open, no hash: nothing happens
    p.ctx.MMWatch.detail.open('rec-5', { title: 'Meridian' });
    await settle();
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    await settle();
    return { posts: p.posts, email: p.ttl().querySelectorAll('.ttl__note').some((n) => /email/.test(n.textContent)), share: !!p.q('[data-ttl-act="share"]') };
  },

  // The email is promised only where the title's reply says this person will get one: not to one whose
  // download the Arr refused, not on reopening another person's, and after an accepted one not before
  // the reply, nor at all when it says no.
  async recEmail() {
    const run = async (kind, accepted) => {
      const rec = (state, pct, notify) => Object.assign({}, REC_DL, { kind, download: Object.assign({}, REC_DL.download, {
        state, pct, notify, answered: true, service: kind === 'movie' ? 'radarr' : 'sonarr' }) });
      let reply = () => Promise.resolve(rec(null, null, false));
      let refuse = true;
      const p = load({
        routes: { '/api/watch/title/rec-5': () => reply(), '/api/watch/title/show-300': LIB_SHOW },
        posted: { '/api/recommended/5/download': () => (refuse
          ? Promise.reject(new APIError(409, 'conflict', "'Meridian' is already there")) : Promise.resolve({ ok: true })) },
      });
      p.run();
      const shown = () => ({ primary: primary(p).text, email: p.ttl().querySelectorAll('.ttl__note').some((n) => /email/.test(n.textContent)) });
      const reopen = async (data) => {
        reply = () => Promise.resolve(data);
        p.ctx.MMWatch.detail.open('show-300', {});
        await settle();
        p.ctx.MMWatch.detail.open('rec-5', {});
        await settle();
        return shown();
      };
      const out = {};
      await reopen(rec(null, null, false));
      reply = () => Promise.resolve(rec('queued', 0, false));
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await settle();
      out.refused = shown();
      out.reopened = await reopen(rec('queued', 0, false));
      out.downloading = await reopen(rec('downloading', 40, false));
      out.ownDownloading = await reopen(rec('downloading', 40, true));
      await reopen(rec(null, null, false));
      refuse = false;
      let answer;
      reply = () => new Promise((resolve) => { answer = () => resolve(rec('queued', 0, accepted)); });
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      await settle();
      out.beforeReply = shown();
      if (answer) answer();
      await settle();
      out.accepted = shown();
      return out;
    };
    return { movie: await run('movie', true), show: await run('show', true), noEmail: await run('movie', false) };
  },

  async seasonPicker() {
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': TV_DL } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', { title: 'Hollow Pines' });
    await settle();
    const chips = () => p.ttl().querySelectorAll('.season-chip').map((c) => [c.getAttribute('data-season'), c.getAttribute('aria-checked'), c.getAttribute('aria-disabled')]);
    const initial = { chips: chips(), primary: primary(p), summary: p.text('.season-pick__summary') };
    click(p.doc, p.q('.season-pick__clear'));
    const cleared = { chips: chips(), primary: primary(p) };
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    const postsWhenNone = p.posts.length;
    click(p.doc, p.q('.season-chip[data-season="2"]'));
    const one = { primary: primary(p), summary: p.text('.season-pick__summary') };
    click(p.doc, p.q('[data-ttl-act="primary"]'));
    await settle();
    return { initial, cleared, postsWhenNone, one, posts: p.posts };
  },

  async tvBlocked() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-tv-90': Object.assign({}, TV_DL, { sonarr_tracked: true }),
      '/api/watch/title/tmdb-tv-91': Object.assign({}, TV_DL, { ref: 'tmdb-tv-91', seasons: tvSeasons([1, 2, 3]) }),
    } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    const tracked = { primary: primary(p), button: !!p.q('.ttl__download') };
    p.ctx.MMWatch.detail.open('tmdb-tv-91', {});
    await settle();
    return { tracked, all: { primary: primary(p), button: !!p.q('.ttl__download') } };
  },

  async seasonPills() {
    const p = load({ routes: {
      '/api/watch/title/show-300': LIB_SHOW,
      '/api/watch/title/show-300/season/1': { ok: true, episodes: [{ number: 1, title: 'One One', minutes: 45, thumb_url: '/api/watch/thumb/11', state: 'none', href: '/watch/play/11' }] },
    } });
    p.run();
    await openCard(p, 'show-300');
    const first = { pills: p.ttl().querySelectorAll('.watch-pill').map((b) => [b.textContent, b.getAttribute('aria-pressed')]), eps: p.text('.watch-episodes') };
    const pill = p.q('.watch-pill[data-season="1"]');
    pill.focus();
    const listBefore = p.q('.watch-episodes');
    click(p.doc, pill);
    await settle();
    return { first, eps: p.text('.watch-episodes'), label: p.q('.watch-episodes').getAttribute('aria-label'), replaced: listBefore !== p.q('.watch-episodes'),
      pressed: pill.getAttribute('aria-pressed'), focusOnPill: p.doc.activeElement === pill,
      resume: primary(p), progress: !!p.q('.ttl__progress progress'), keep: p.q('[data-watch-keep]') && p.q('[data-watch-keep]').getAttribute('data-watch-keep') };
  },

  // The title is loaded again after a Keep while another season than the server's own is on
  // show, or is still loading; then the season on show is deleted.
  async seasonPickedThenRefreshed() {
    const eps = (title) => ({ ok: true, episodes: [{ number: 1, title, minutes: 45, thumb_url: null, state: 'none', href: '/watch/play/11' }] });
    const look = (p) => ({ on: p.ttl().querySelectorAll('.watch-pill').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent),
      eps: p.ttl().querySelectorAll('.watch-ep__title').map((n) => n.textContent), label: p.q('.watch-episodes').getAttribute('aria-label') });
    async function run(slow) {
      let show = LIB_SHOW;
      let answer = null;
      const facts = Object.assign({}, FACTS, { actions: { keep_wrapper: { id: '3010', is_tv: true, show_rk: '300' }, delete: null } });
      const p = load({ admin: true, adminApi: {}, routes: {
        '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show))),
        '/api/watch/admin/title/show-300': facts,
        '/api/watch/title/show-300/season/1': () => (slow ? new Promise((resolve) => { answer = () => resolve(eps('One One')); }) : Promise.resolve(eps('One One'))),
      } });
      p.run();
      await openCard(p, 'show-300');
      const announce = async () => {
        p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'keep', id: '3010', showRk: '300' } }));
        await settle();
      };
      click(p.doc, p.q('.watch-pill[data-season="1"]'));
      await settle();
      await announce();
      const during = look(p);
      if (answer) { answer(); await settle(); }
      const kept = look(p);
      show = Object.assign({}, LIB_SHOW, { seasons: LIB_SHOW.seasons.slice(1) });
      await announce();
      return { during, kept, deleted: look(p).eps };
    }
    return { loaded: await run(false), loading: await run(true) };
  },

  // Undo pressed in a toast, which goes with the focus it had.
  async undoInToast() {
    const p = load({ admin: true, adminApi: { keep: () => {} }, routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS } });
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    const toast = build(p.doc, ['div', { class: 'ui-toast' }, ['button', { class: 'ui-toast__action' }, 'Undo']]);
    p.doc.body.appendChild(toast);
    toast.children[0].focus();
    click(p.doc, toast.children[0]);
    const pressed = p.doc.activeElement.getAttribute('data-ttl-act');
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'unkeep', id: '120', showRk: '' } }));
    await settle();
    return { pressed, reloaded: p.doc.activeElement.getAttribute('data-ttl-act') };
  },

  // A season with fewer episodes, picked when the overlay is scrolled so far that the shorter
  // page moves its pills; then one long enough.
  async shorterSeason() {
    const eps = (n) => ({ ok: true, episodes: Array.from({ length: n }, (_, i) => ({ number: i + 1, title: 'E' + i, state: 'none', href: '/watch/play/' + i })) });
    const p = load({ routes: {
      '/api/watch/title/show-300': LIB_SHOW,
      '/api/watch/title/show-300/season/1': eps(2), '/api/watch/title/show-300/season/2': eps(9),
    } });
    p.run();
    await openCard(p, 'show-300');
    p.ttl().scrollTop = 500;
    const body = p.q('[data-ttl-part="body"]');
    let short = true;
    const out = {};
    for (const n of [1, 2]) {
      const pill = p.q('.watch-pill[data-season="' + n + '"]');
      const list = p.q('.watch-episodes');
      pill.getBoundingClientRect = () => ({ top: n === 1 && !p.q('.ttl__seasonsec').contains(list) && short ? 130 : 100 });
      click(p.doc, pill);
      await settle();
      out[n] = { scroll: p.ttl().scrollTop, room: body.style.marginBottom || '' };
      short = false;
    }
    return out;
  },

  // The line of seasons of a library show: where it stands when the title opens on its second
  // season, after a press on one wholly in view and one cut off, and after the title is loaded again.
  async seasonLine() {
    const eps = { ok: true, episodes: [] };
    const started = [];
    const p = load({ admin: true, adminApi: {}, routes: {
      '/api/watch/title/show-300': LIB_SHOW, '/api/watch/title/show-300/season/1': eps, '/api/watch/title/show-300/season/2': eps,
      '/api/watch/admin/title/show-300': Object.assign({}, FACTS, { actions: { keep_wrapper: { id: '3010', is_tv: true, show_rk: '300' }, delete: null } }),
    } });
    p.run();
    const at = { 1: 32, 2: 400 };
    const moves = [];
    // What the fake DOM has no layout for: a 300px line of 100px pills.
    p.ctx.MMWatch.rows = { init: (viewport) => { started.push([viewport.classList.contains('watch-row__viewport'), p.ttl().contains(viewport)]); } };
    Object.defineProperties(Node.prototype, {
      offsetLeft: { configurable: true, get() { return at[this.getAttribute('data-season')] || 0; } },
      offsetWidth: { configurable: true, get() { return 100; } },
      clientWidth: { configurable: true, get() { return 300; } },
    });
    const native = Node.prototype.scrollTo;
    Node.prototype.scrollTo = function (o) {
      if (!this.classList.contains('watch-seasons')) { native.call(this, o); return; }
      this.scrollLeft = o.left;
      moves.push(o);
    };
    await openCard(p, 'show-300');
    const line = () => p.q('.watch-seasons');
    const out = { started: started[0], scroller: line().classList.contains('watch-row__scroll'), opened: moves.splice(0) };
    click(p.doc, p.q('.watch-pill[data-season="2"]'));
    await settle();
    out.inView = moves.splice(0);
    click(p.doc, p.q('.watch-pill[data-season="1"]'));
    await settle();
    out.cutOff = moves.splice(0);
    line().scrollLeft = 77;
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'keep', id: '3010', showRk: '300' } }));
    await settle();
    out.redrawn = { scroll: line().scrollLeft, moves: moves.splice(0), started: started.length };
    return out;
  },

  async viewerNoAdmin() {
    const p = load({ routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS } });
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    return { gets: p.gets, facts: !!p.q('.ttl-facts'), keep: !!p.q('[data-watch-keep="120"]') };
  },

  // The admin's facts arrive after the title: the panel's place is taken from the start. Then they
  // fail, at first, on the retry and after a Keep, or the title is in no scan yet.
  async factsLate() {
    let answer = null;
    const reply = () => new Promise((resolve, reject) => { answer = { resolve, reject }; });
    const open = async () => {
      const p = load({ admin: true, adminApi: { keep: () => {}, confirmDelete: () => {} },
        routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': reply } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      return p;
    };
    const look = (p) => {
      const panel = p.q('.ttl-facts');
      const err = panel && panel.querySelector('.ttl__error');
      return { columns: p.ttl().classList.contains('ttl--admin'), labels: panel ? panel.querySelectorAll('dt').map((n) => n.textContent) : null,
        busy: !!panel && panel.getAttribute('aria-busy') === 'true', buttons: panel ? panel.querySelectorAll('[data-ttl-act]').map((n) => n.getAttribute('data-ttl-act')) : [],
        error: err ? err.getAttribute('role') : null };
    };
    const out = {};
    let p = await open();
    out.pending = look(p);
    answer.resolve(FACTS);
    await settle();
    out.known = look(p);
    p = await open();
    answer.reject(new APIError(500, 'x'));
    await settle();
    out.failed = look(p);
    p.q('[data-ttl-act="facts-retry"]').focus();
    click(p.doc, p.q('[data-ttl-act="facts-retry"]'));
    answer.resolve(FACTS);
    await settle();
    out.retried = look(p);
    out.focus = p.doc.activeElement.getAttribute('data-ttl-act');
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'keep', id: '120', showRk: '' } }));
    await settle();
    answer.reject(new APIError(500, 'x'));
    await settle();
    out.stale = look(p);
    p = await open();
    answer.reject(new APIError(404, 'not_found'));
    await settle();
    out.unknown = look(p);
    return out;
  },

  // What stands where before and after a title arrives.
  async arrival() {
    const run = async (over) => {
      const p = load({ routes: { '/api/watch/title/movie-120': Object.assign({}, LIB_MOVIE, over) } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      const look = () => ({ tagline: p.q('[data-ttl-part="tagline"]').hidden ? 'none' : 'held', first: p.q('.ttl__main').children[0].className.split(' ')[0] });
      const loading = look();
      await settle();
      return { loading, loaded: look(), leaving: p.text('[data-ttl-part="under"] .ttl__leaving') };
    };
    return { tagline: await run({}), without: await run({ tagline: null }) };
  },

  async adminFacts() {
    const calls = [];
    let announce = null;
    const p = load({
      admin: true,
      routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS },
      adminApi: {
        // As the real admin actions: each announces what it did when it is done.
        keep: (w, d) => {
          calls.push(['keep', w.id, d, w.deletion]);
          announce({ phase: 'done', action: 'keep', id: w.id, showRk: '' });
          return Promise.resolve(true);
        },
        confirmDelete: (id, title) => {
          calls.push(['delete', id, title]);
          announce({ phase: 'done', action: 'delete', id, showRk: '' });
          return Promise.resolve(true);
        },
      },
    });
    announce = (detail) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail }));
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    const facts = p.ttl().querySelectorAll('.ttl-facts__list dt, .ttl-facts__list dd').map((n) => n.textContent);
    const rowKeep = !!p.q('.ttl__actions [data-watch-keep]');
    click(p.doc, p.q('[data-ttl-act="admin-keep"]'));
    const menuOpen = !p.q('.ttl-facts__menu').hidden;
    click(p.doc, p.ttl().querySelectorAll('.ttl-facts__menu [role="menuitem"]')[1]);
    await settle();
    const refetched = p.gets.filter((g) => g === '/api/watch/admin/title/movie-120').length;
    const openAfterKeep = !p.ttl().classList.contains('is-leaving');
    click(p.doc, p.q('[data-ttl-act="admin-delete"]'));
    await settle();
    return { gets: p.gets, facts, rowKeep, menuOpen, calls, refetched, openAfterKeep, closedAfterDelete: p.ttl().classList.contains('is-leaving'),
      admin: p.ttl().classList.contains('ttl--admin'), director: p.text('.ttl__credit'), tagline: p.text('[data-ttl-part="tagline"]'),
      ratings: p.text('.ttl__ratings') };
  },

  /* An admin deletes the title from the overlay opened from its list row; the list
     takes the row away (*how*: 'animate' leaves it fading, 'remove' drops it at once)
     before the browser's popstate closes the overlay. */
  async deleteFromListRow() {
    async function run(rows, how) {
      let announce = null;
      const p = load({
        admin: true, listRows: rows,
        routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS },
        adminApi: {
          keep: () => Promise.resolve(true),
          confirmDelete: (id) => { announce({ phase: 'done', action: 'delete', id, showRk: '' }); return Promise.resolve(true); },
        },
      });
      announce = (detail) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail }));
      p.run();
      p.doc.addEventListener('mm:watch-admin', () => {
        const row = p.doc.querySelector('tr[data-row-key="id:120"]');
        if (how === 'animate') row.classList.add('is-leaving'); else row.remove();
      });
      const go = p.ctx.history.go;
      p.ctx.history.go = function (by) { Promise.resolve().then(() => go.call(this, by)); };
      await openCard(p, 'movie-120');
      click(p.doc, p.q('[data-ttl-act="admin-delete"]'));
      await settle();
      const a = p.doc.activeElement;
      return { closed: p.ttl().classList.contains('is-leaving'), focus: a.id || a.tagName, tabindex: a.getAttribute('tabindex'),
        preventScroll: !!(p.doc.focusOpts && p.doc.focusOpts.preventScroll) };
    }
    return {
      next: await run(['119', '120', '121'], 'animate'),
      previous: await run(['119', '120'], 'remove'),
      only: await run(['120'], 'remove'),
    };
  },

  async safePath() {
    const p = load({});
    p.run();
    const inputs = ['/watch/movie/1', '//evil.example', '/\\evil.example', '/\t/evil.example', '/\n/evil.example',
      '/\r/evil.example', '/watch\t/x', 'https://evil.example/'];
    return inputs.map((v) => [v, p.ctx.MMWatch.core.safePath(v)]);
  },

  async adminWithoutApi() {
    const p = load({ admin: true, routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS } });
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    return { facts: !!p.q('.ttl-facts'), foot: !!p.q('.ttl-facts__foot'), keep: !!p.q('[data-watch-keep="120"]') };
  },

  async share() {
    const p = load({ admin: true, routes: { '/api/watch/title/rec-5': REC_DL }, posted: { '/api/recommended/5/share-token': { ok: true, share_url: 'https://example.test/download/tok' } } });
    p.run();
    p.ctx.MMWatch.detail.open('rec-5', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="share"]'));
    await settle();
    const copied = p.text('[data-ttl-act="share"]');
    const timersSet = p.timers.filter((t) => t.fn && t.ms === 2000).length;
    click(p.doc, p.q('[data-ttl-act="share"]'));
    await settle();
    const timersAfterSecond = p.timers.filter((t) => t.fn && t.ms === 2000).length;
    p.tick();
    return { copied, timersSet, timersAfterSecond, after: p.text('[data-ttl-act="share"]') };
  },

  // Share, Sharing… and Copied: what each state shows, and what holds the button's width.
  // What the overlay's status line says of a share, in turn.
  async shareSaid() {
    const run = async (opts) => {
      const p = load(Object.assign({ admin: true, routes: { '/api/watch/title/rec-5': REC_DL } }, opts));
      p.run();
      p.ctx.MMWatch.detail.open('rec-5', {});
      await settle();
      const line = p.q('[role="status"].sr-only');
      const before = line.textContent;
      click(p.doc, p.q('[data-ttl-act="share"]'));
      await settle();
      await settle();
      const said = line.textContent;
      click(p.doc, p.q('[data-ttl-act="share"]'));
      return { before, said, again: line.textContent };
    };
    const url = '/api/recommended/5/share-token';
    return { copied: await run({ posted: { [url]: { ok: true, share_url: 'https://example.test/download/tok' } } }),
      refused: await run({ posted: { [url]: new APIError(503, 'x') } }) };
  },

  async shareWidth() {
    let release = null;
    const p = load({ admin: true, routes: { '/api/watch/title/rec-5': REC_DL },
      posted: { '/api/recommended/5/share-token': () => new Promise((resolve) => { release = resolve; }) } });
    p.run();
    p.ctx.MMWatch.detail.open('rec-5', {});
    await settle();
    const label = () => {
      const b = p.q('[data-ttl-act="share"]');
      const n = p.q('[data-ttl-act="share"] .ttl__share-label');
      return { text: n.textContent, widest: b.getAttribute('data-widest'), group: n.parentNode.className,
        icon: n.parentNode.children[0].querySelector('use').getAttribute('href'), inButton: n.parentNode.parentNode === b, groups: b.children.length };
    };
    const states = [label()];
    click(p.doc, p.q('[data-ttl-act="share"]'));
    states.push(label());
    release({ ok: true, share_url: 'https://example.test/download/tok' });
    await settle();
    await settle();
    states.push(label());
    return states;
  },

  async shareClipboardFails() {
    const p = load({ admin: true, clipboardFails: true, routes: { '/api/watch/title/rec-5': REC_DL }, posted: { '/api/recommended/5/share-token': { ok: true, share_url: 'https://example.test/download/tok' } } });
    p.run();
    p.ctx.MMWatch.detail.open('rec-5', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="share"]'));
    await settle();
    await settle();
    const field = p.q('.ttl-share__input');
    return { value: field && field.value, readOnly: field && field.readOnly, focused: p.doc.activeElement === field, label: p.text('[data-ttl-act="share"]') };
  },

  async pictures() {
    const film = Object.assign({}, LIB_MOVIE, { poster_url: '/api/poster/120' });
    const out = {};
    for (const [name, steps] of Object.entries({
      plex: ['wide'], tmdb: ['failed', 'wide'], poster: ['failed', 'failed'],
      unfit: ['narrow', 'wide'], unfitBoth: ['narrow', 'narrow'],
    })) {
      const p = load({ pictures: 'pending', routes: { '/api/watch/title/movie-120': film } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      const art = p.q('[data-ttl-part="art"]');
      const first = hero(p);
      steps.forEach((how) => settlePicture(art, how));
      out[name] = { first, end: hero(p) };
    }
    return out;
  },

  // When the buttons come and whether the head is short, for a title whose pictures all fail
  // before the wait is over, after it, or show.
  async headHeight() {
    const out = {};
    for (const [name, steps] of Object.entries({
      none: ['failed', 'failed'], slow: ['wait', 'failed', 'failed'], wide: ['wide'],
    })) {
      const p = load({ pictures: 'pending', routes: { '/api/watch/title/movie-120': Object.assign({}, LIB_MOVIE, { poster_url: null }) } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      const look = () => ({ buttons: !!primary(p), short: p.ttl().classList.contains('is-short'), late: p.ttl().classList.contains('is-late') });
      const looks = [look()];
      steps.forEach((how) => {
        if (how === 'wait') p.timers.filter((t) => t.fn && t.ms === 400).forEach((t) => t.fn());
        else settlePicture(p.q('[data-ttl-part="art"]'), how);
        looks.push(look());
      });
      out[name] = looks;
    }
    const gone = load({ routes: {} });
    gone.run();
    gone.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    out.failed = gone.ttl().classList.contains('is-short');
    return out;
  },

  async picturesOutsideTheLibrary() {
    const out = {};
    for (const [name, over] of Object.entries({
      backdrop: {}, posterOnly: { backdrop_url: null, poster_url: '/api/watch/img/tmdb/w500/p.jpg' }, none: { backdrop_url: null },
    })) {
      const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, over) } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      out[name] = hero(p);
    }
    return out;
  },

  async shareRefused() {
    const p = load({ admin: true, routes: { '/api/watch/title/rec-5': REC_DL }, posted: { '/api/recommended/5/share-token': new APIError(503, 'x', 'Base URL not configured') } });
    p.run();
    p.ctx.MMWatch.detail.open('rec-5', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="share"]'));
    await settle();
    return { note: p.ttl().querySelectorAll('.ttl__note').map((n) => n.textContent), label: p.text('[data-ttl-act="share"]') };
  },

  async focusTrap() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const close = p.q('.ttl__close');
    const buttons = p.ttl().querySelectorAll('button').filter((b) => !b.closest('[hidden]'));
    const last = buttons[buttons.length - 1];
    last.focus();
    const fwd = p.key('Tab', { shiftKey: false });
    const wrappedToFirst = p.doc.activeElement === close;
    const back = p.key('Tab', { shiftKey: true });
    const wrappedToLast = p.doc.activeElement === last;
    const outside = p.doc.getElementById('watch-search');
    (p.doc.listeners.focusin || []).forEach((fn) => fn({ target: outside }));
    return { wrappedToFirst, prevented: [fwd.defaultPrevented, back.defaultPrevented], wrappedToLast,
      pulledBack: p.doc.activeElement === p.q('#ttl-title') };
  },

  // Titles opened from cards of pages that are gone by the time the next one is opened.
  async cardsThatGo() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL, '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    const held = [];
    const open = async (ref) => {
      const page = p.doc.body.appendChild(build(p.doc, ['a', { 'data-title-ref': ref }, 'A card']));
      held.push(new WeakRef(page));
      click(p.doc, page);
      await settle();
      click(p.doc, p.q('[data-ttl-close]'));
      p.tick();
      page.remove();
      p.doc.activeElement = p.doc.body;
    };
    for (let i = 0; i < 3; i++) {
      await open('tmdb-movie-77');
      await open('show-300');
    }
    await openCard(p, 'tmdb-movie-77');
    await settle();
    v8.setFlagsFromString('--expose-gc');
    vm.runInNewContext('gc')();
    return { held: held.filter((r) => r.deref()).length };
  },

  // A person's card open in the strip when the title goes: for another, or closed.
  async cardOfAPersonGoes() {
    const cast = [{ name: 'Mara Ellison', character: null, profile_url: null, person_id: 9001 }];
    const out = {};
    for (const how of ['title', 'closed']) {
      const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { cast }), '/api/watch/title/show-300': () => new Promise(() => {}) } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      p.q('.cast-card').style.setProperty = () => {};
      p.ttl().listeners.pointermove.forEach((fn) => fn({ target: p.q('.ttl-cast__item'), pointerType: 'mouse' }));
      p.tick();
      const look = () => p.q('.cast-card').classList.contains('is-open');
      const was = look();
      if (how === 'title') p.ctx.MMWatch.detail.open('show-300');
      else p.ctx.MMWatch.detail.close();
      await settle();
      out[how] = [was, look()];
    }
    return out;
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
