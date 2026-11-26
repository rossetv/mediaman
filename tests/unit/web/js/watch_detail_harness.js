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
Node.prototype.scrollIntoView = function () {};
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
    play: null, leaving: null, keep_allowed: false, leaving_seasons: [], seasons: [], episodes: [], download: null, tmdb_configured: true,
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
  keep_allowed: true, leaving: { days: 3, keep_id: '3010', season: 1 }, leaving_seasons: [{ days: 3, keep_id: '3010', season: 1 }],
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
  const aheads = [];
  const posts = [];
  const answers = []; // to the ratings sent, not answered yet
  const events = [];
  const history = [];
  const routes = Object.assign({}, opts.routes);
  const posted = opts.posted || {};
  const loc = { origin: 'https://mediaman.test', pathname: '/', search: '', hash: opts.hash || '' };
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
    ['section', { class: 'watch-row' }, ['div', { class: 'watch-row__scroll' }, ...(opts.films || []).map((id) => card('movie-' + id, 'Film ' + id))]],
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
    // A wide, tall screen unless the scenario says it is narrow, or short as a phone on its side.
    matchMedia: (q) => ({ matches: q.split(',').some((either) => either.split(' and ').every((f) =>
      /min-width/.test(f) ? !opts.narrow : /max-width/.test(f) ? !!opts.narrow : /min-height/.test(f) ? !opts.short
        : /max-height/.test(f) ? !!opts.short : /reduced-motion/.test(f) ? !!opts.still : false)) }),
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
      get: (url, o) => {
        // Asked ahead or not, a title is the same title.
        if (/\?ahead=1$/.test(url)) aheads.push(url);
        url = url.replace(/\?ahead=1$/, '');
        gets.push(url);
        const r = routes[url];
        if (r instanceof Error) return Promise.reject(r);
        if (typeof r === 'function') return r(o);
        return r ? Promise.resolve(JSON.parse(JSON.stringify(r))) : Promise.reject(new APIError(404, 'not_found'));
      },
      put: (url, body) => {
        posts.push({ url, body });
        return new Promise((resolve) => { answers.push(resolve); });
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
    ['core/dom.js', 'modal-a11y.js'].concat(opts.thumbs ? 'ratings.js' : [], 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/cast.js', opts.collection ? 'watch/collection.js' : [], opts.playOn ? 'watch/play-on.js' : [], 'watch/detail.js')
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
    (doc.listeners.keydown || []).concat(winListeners.keydown || []).forEach((fn) => fn(e));
    return e;
  };
  const fireWindow = (type) => (winListeners[type] || []).slice().forEach((fn) => fn({}));
  const popstate = () => (winListeners.popstate || []).forEach((fn) => fn({}));
  const message = (e) => (winListeners.message || []).forEach((fn) => fn(e));
  return { answers, message, ctx, doc, toasts, timers, gets, aheads, posts, events, history, entries, explored, loc, routes, clock, run, ttl, q, text, tick, key, popstate, fireWindow };
}

function primary(p) {
  const n = p.q('[data-ttl-part="actions"] [data-ttl-act="primary"]');
  return n ? { text: n.textContent.trim(), cls: n.className, disabled: n.getAttribute('aria-disabled') === 'true' } : null;
}

async function openCard(p, ref) {
  click(p.doc, p.doc.querySelector('[data-title-ref="' + ref + '"]'));
  await settle();
}

const ASK = { url: '/api/search/download', body: { media_type: 'tv', tmdb_id: 90, title: 'Hollow Pines' } };

/* A season only TMDB knows, in a state. */
function one(number, status, over) {
  return Object.assign({ number, name: 'Season ' + number, episode_count: 8, in_library: false, selected: false, key: null, watched: false,
    status, asked: false, askable: false, refusal: null }, over);
}

/* A show Sonarr has, with seasons in every state. */
function asked(ref, download) {
  return Object.assign({}, TV_DL, { ref, sonarr_tracked: true, seasons_request: ASK, seasons_pending: false, more_seasons: [],
    download: Object.assign({}, TV_DL.download, download),
    seasons: [one(1, 'downloading', { asked: true }), one(2, 'queued', { asked: true }), one(3, 'partly_held', { askable: true }), one(4, 'not_asked', { askable: true })] });
}

/* Each season's number, its state's words, and whether it can be ticked. */
function chipsOf(p) {
  return p.ttl().querySelectorAll('.season-chip').map((c) => [c.getAttribute('data-season'), c.querySelector('.season-chip__sub').textContent, c.getAttribute('aria-disabled') !== 'true']);
}

/* What a preview says of *data*. */
function previewOf(data, over) {
  const keys = ['ref', 'kind', 'in_library', 'library_ref', 'title', 'year', 'runtime_min', 'season_count', 'genres', 'certificate', 'tagline', 'overview',
    'backdrop_url', 'ratings', 'trailer_key', 'reason', 'play', 'leaving', 'leaving_seasons', 'keep_allowed', 'download'];
  return Object.assign(Object.fromEntries(keys.map((k) => [k, data[k]])), { seasons: [] }, over);
}

/* A film on the way from its card: the page, its card's link, and what it has been drawn with. */
function carried(opts = {}) {
  const data = opts.data || LIB_MOVIE;
  const p = load({ films: ['120'], clock: true, ...opts, routes: { ['/api/watch/title/' + data.ref]: data, ...opts.routes } });
  p.run();
  p.detail = p.ctx.MMWatch.detail;
  p.link = p.doc.querySelector('[data-title-ref="' + data.ref + '"]');
  p.draw = () => p.detail.draw(data.ref, { origin: p.link, title: data.title, kind: data.kind, preview: previewOf(data, opts.preview), poster: null, watched: false });
  p.look = () => ({ hidden: p.ttl().hidden, locked: p.doc.documentElement.classList.contains('has-ttl'), hash: p.loc.hash, pushes: p.history.length,
    focus: p.doc.activeElement === p.q('#ttl-title'), inert: p.doc.getElementById('watch-search').inert, title: p.text('#ttl-title'), words: p.text('.ttl__overview') });
  return p;
}

const scenarios = {
  // Drawn at the first of a scroll, taken back, drawn again and opened, then closed.
  async drawnThenOpened() {
    const p = carried();
    p.draw();
    const drawn = p.look();
    p.detail.undraw();
    const back = { ...p.look(), empty: p.q('[data-ttl-part="body"]').children.length };
    p.key('Escape');
    const untouched = p.history.length;
    p.draw();
    p.detail.openDrawn();
    await settle();
    const open = { ...p.look(), busy: p.ttl().getAttribute('aria-busy') };
    const gets = p.gets.filter((u) => /title\/movie-120$/.test(u)).length;
    p.key('Escape');
    return { drawn, back, untouched, open, gets, closed: [p.ttl().classList.contains('is-leaving'), p.doc.activeElement === p.link] };
  },

  // Its whole page has come before it is drawn: it is drawn with it, and asks for the rest once open.
  async drawnInFull() {
    const full = Object.assign({}, LIB_MOVIE, { ratings_pending: true });
    const p = carried({ data: full });
    p.detail.ahead('movie-120');
    await settle();
    p.draw();
    const drawn = { director: p.text('.ttl__credit'), cast: p.ttl().querySelectorAll('.ttl-cast__item').length, gets: p.gets.length };
    const face = p.q('.ttl-cast__item');
    p.detail.openDrawn();
    await settle();
    return { drawn, kept: p.q('.ttl-cast__item') === face, gets: p.gets };
  },

  // The page has other words than the preview it was read by, and a longer wait: they stay as they were.
  async wordsGoOn() {
    const late = {};
    const data = Object.assign({}, LIB_MOVIE, { overview: 'What Plex says.' });
    const p = carried({ data, preview: { overview: 'What was read.' }, routes: { '/api/watch/title/movie-120': () => new Promise((r) => { late.come = () => r(data); }) } });
    p.draw();
    p.detail.openDrawn();
    await settle();
    const before = { words: p.text('.ttl__overview'), play: primary(p).text, skels: p.ttl().querySelectorAll('.ttl__credit .skel').length, busy: p.ttl().getAttribute('aria-busy') };
    late.come();
    await settle();
    const after = { words: p.text('.ttl__overview'), director: p.text('.ttl__credit'), busy: p.ttl().getAttribute('aria-busy'),
      faded: p.ttl().querySelectorAll('.ttl__main .fade-in').map((n) => n.className.split(' ')[0]), swapped: p.q('.ttl__overview').classList.contains('is-swapping-in') };
    return { before, after };
  },

  // The title's page cannot be had: what its preview said stays, and a second try brings the rest.
  async drawnThenFailed() {
    let down = true;
    const p = carried({ routes: { '/api/watch/title/movie-120': () => (down ? Promise.reject(new APIError(502, 'x')) : Promise.resolve(LIB_MOVIE)) } });
    p.draw();
    p.detail.openDrawn();
    await settle();
    const failed = { line: p.text('.ttl__failed-line'), words: p.text('.ttl__overview'), play: primary(p).text, skels: p.q('.ttl__main').querySelectorAll('.skel').length };
    down = false;
    click(p.doc, p.q('.ttl__failed-actions').children[0]);
    await settle();
    return { failed, then: { line: p.text('.ttl__failed-line'), director: p.text('.ttl__credit') } };
  },

  // Marked, kept or queued elsewhere while it is drawn from what was asked ahead: it opens as the server says now.
  // Its own download state, told to the page as it is drawn, asks nothing again.
  async changedWhileDrawn() {
    const out = {};
    for (const name of ['mm:watch-marked', 'mm:watch-kept', 'mm:title-changed']) {
      let watched = false;
      const p = carried({ routes: { '/api/watch/title/movie-120': () => Promise.resolve(Object.assign({}, LIB_MOVIE, { mark_kind: 'movie', mark_key: '120', watched })) } });
      p.detail.ahead('movie-120');
      await settle();
      p.draw();
      watched = true;
      p.doc.dispatchEvent({ type: name, detail: { ref: 'movie-120', download_state: 'queued' } });
      p.detail.openDrawn();
      await settle();
      out[name] = p.q('[data-ttl-act="mark"]').getAttribute('aria-pressed');
    }
    const p = carried({ data: Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'downloading', pct: 40 }) }) });
    p.detail.ahead('tmdb-movie-77');
    await settle();
    p.draw();
    p.detail.openDrawn();
    await settle();
    out.own = p.gets.filter((u) => /title\/tmdb-movie-77$/.test(u)).length;
    return out;
  },

  // What was asked for ahead is refused (its own limit) after the title has been opened on it: the title
  // is asked for again, plainly, and opens, with no failure shown meanwhile.
  async aheadRefusedOnOpen() {
    const waits = [];
    const p = carried({ routes: { '/api/watch/title/movie-120': () => new Promise((resolve, reject) => { waits.push({ resolve, reject }); }) } });
    p.detail.ahead('movie-120');
    p.detail.open('movie-120', {});
    await settle();
    waits[0].reject(new APIError(429, 'too_many_requests'));
    await settle();
    const meanwhile = { asked: waits.length, failed: !!p.q('.ttl__failed') };
    if (waits[1]) waits[1].resolve(LIB_MOVIE);
    await settle();
    return { meanwhile, director: p.text('.ttl__credit'), failed: !!p.q('.ttl__failed') };
  },

  // Refused what it asked ahead, the page asks ahead no more for a minute; what is opened is still asked for.
  async aheadBacksOff() {
    const p = carried({ routes: { '/api/watch/title/movie-1': new APIError(429, 'too_many_requests'),
      '/api/watch/title/movie-2': Object.assign({}, LIB_MOVIE, { ref: 'movie-2', library_ref: 'movie-2' }) } });
    p.detail.ahead('movie-1');
    await settle();
    p.clock.now += 59000;
    p.detail.ahead('movie-2');
    const meanwhile = p.aheads.length;
    p.draw();
    p.detail.undraw();
    await settle();
    const drawn = p.gets.filter((u) => u === '/api/watch/title/movie-120').length;
    p.clock.now += 2000;
    p.detail.ahead('movie-2');
    return { meanwhile, drawn, after: p.aheads.slice(1) };
  },

  // A title that is not there is not asked for again when it is opened.
  async aheadNotFoundOnOpen() {
    const p = carried({ routes: { '/api/watch/title/movie-120': new APIError(404, 'not_found') } });
    p.detail.ahead('movie-120');
    p.detail.open('movie-120', {});
    await settle();
    return { asked: p.gets.length, failed: !!p.q('.ttl__failed') };
  },

  // Only what a preview asks for on the chance it is opened says so to the server, which limits it apart.
  async aheadMarked() {
    const p = carried();
    p.detail.ahead('movie-120');
    const hovered = p.aheads.slice();
    p.clock.now += 61000;
    p.draw();
    p.detail.undraw();
    await settle();
    return { hovered, drawn: p.aheads.length };
  },

  // A show whose seasons are picked, drawn from its preview: Download waits for the page, through a failed try.
  async drawnDownloadWaits() {
    let down = true;
    const late = {};
    const p = carried({ data: TV_DL, preview: { seasons: TV_DL.seasons }, routes: { '/api/watch/title/tmdb-tv-90': () => (down ? Promise.reject(new APIError(502, 'x')) : new Promise((r) => { late.come = () => r(TV_DL); })) } });
    const look = () => { const b = p.q('[data-ttl-act="primary"]'); return [b.getAttribute('aria-disabled'), b.getAttribute('aria-busy')]; };
    const press = () => { click(p.doc, p.q('[data-ttl-act="primary"]')); return p.posts.length; };
    p.draw();
    const drawn = look();
    p.detail.openDrawn();
    await settle();
    const failed = [...look(), press()];
    down = false;
    click(p.doc, p.q('.ttl__failed-actions').children[0]);
    await settle();
    const again = look();
    late.come();
    await settle();
    return { drawn, failed, again, came: [...look(), press()] };
  },

  // What is asked for before it is opened: once, one at a time, few a minute, and not once it has gone stale.
  async askedAhead() {
    const never = {};
    const routes = {};
    for (let i = 1; i <= 30; i++) routes['/api/watch/title/movie-' + i] = Object.assign({}, LIB_MOVIE, { ref: 'movie-' + i, library_ref: 'movie-' + i });
    routes['/api/watch/title/movie-900'] = () => new Promise(() => {});
    const p = carried({ routes });
    const asked = () => p.gets.filter((u) => /^\/api\/watch\/title\/movie-\d+$/.test(u)).map((u) => u.split('-')[1]);
    p.detail.ahead('movie-900');
    p.detail.ahead('movie-1');
    const oneAtATime = asked();
    p.clock.now += 61000;
    p.gets.length = 0;
    p.detail.ahead('movie-1');
    await settle();
    p.detail.ahead('movie-1');
    p.detail.open('movie-1', {});
    await settle();
    const once = asked();
    p.gets.length = 0;
    p.clock.now += 61000;
    for (let i = 2; i <= 30; i++) { p.detail.ahead('movie-' + i); await settle(); }
    const most = asked().length;
    p.gets.length = 0;
    p.clock.now += 61000;
    p.detail.ahead('movie-2');
    await settle();
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'movie-2', refs: ['movie-2'] } });
    p.detail.ahead('movie-2');
    return { oneAtATime, once, most, changed: asked() };
  },

  // Another part of the page queued the film on show.
  async queuedElsewhere() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const before = primary(p).text;
    p.doc.dispatchEvent({ type: 'mm:title-changed', detail: { ref: 'tmdb-movie-77', download_state: 'queued', download_pct: null } });
    return { before, after: primary(p).text, polls: p.timers.some((t) => t.fn && t.ms === 5000), events: p.events.length };
  },

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

  // A pick's card opens and closes over rows held in the page's own parts: what the document is
  // asked for on the way, and what holds the cards meanwhile.
  async opensOverManyCards() {
    const p = load({ films: [1, 2, 3] });
    const browse = p.doc.createElement('div');
    browse.setAttribute('id', 'watch-browse');
    const page = p.doc.getElementById('watch-page');
    page.insertBefore(browse, page.firstChild);
    page.querySelectorAll('.watch-rows, .watch-row').forEach((n) => browse.appendChild(n));
    const pick = p.doc.querySelector('[data-title-ref="show-300"]');
    pick.setAttribute('data-title-ref', 'pick-5');
    pick.setAttribute('data-kind', 'show');
    p.run();
    const asked = [];
    const all = p.doc.querySelectorAll.bind(p.doc);
    p.doc.querySelectorAll = (sel) => { asked.push(sel); return all(sel); };
    // As an address opens it: no card is its origin, so its kind and where the focus goes are looked for.
    p.ctx.MMWatch.detail.open('pick-5');
    const open = { kind: p.text('[data-ttl-part="kind"]'), inert: browse.inert, hidden: browse.getAttribute('aria-hidden') };
    await settle();
    p.key('Escape');
    const closed = { inert: browse.inert, hidden: browse.getAttribute('aria-hidden') };
    return { open, closed, sweeps: asked.filter((sel) => /\[data-title-ref\](?!=)|\.watch-card(?![_-])/.test(sel)) };
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

  // Closed after the address alone opened it: which of the title's cards has the focus, where the
  // first is on its way out and the next is not shown; and a TMDB title that the library holds.
  async directLinkFocus() {
    const p = load({ hash: '#title=show-300', routes: { '/api/watch/title/show-300': LIB_SHOW } });
    const rows = p.doc.querySelector('.watch-rows');
    const first = rows.querySelector('[data-title-ref="show-300"]');
    first.closest('.watch-card').classList.add('is-leaving');
    const unseen = rows.appendChild(build(p.doc, ['a', { 'data-title-ref': 'show-300', id: 'unseen' }]));
    unseen.getClientRects = () => [];
    rows.appendChild(build(p.doc, ['a', { 'data-title-ref': 'show-300', id: 'shown' }]));
    p.run();
    await settle();
    p.key('Escape');
    const t = load({ hash: '#title=tmdb-movie-77', routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { library_ref: 'movie-120' }), '/api/watch/title/movie-120': LIB_MOVIE } });
    t.run();
    await settle();
    t.key('Escape');
    return { library: p.doc.activeElement.id, scanned: t.doc.activeElement.getAttribute('data-title-ref') };
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
      ['a', { class: 'watch-card__link', href: '/#title=rec-5', 'data-title-ref': 'rec-5', 'data-kind': 'show' }, 'Pick']));
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
    const closeName = () => { const x = p.q('.ttl__close'); return [x.getAttribute('aria-label'), x.getAttribute('title') || null]; };
    const playing = {
      trailer: p.ttl().classList.contains('is-trailer'),
      faces: trailer.children.map((n) => n.textContent.trim()), usable: !trailer.hasAttribute('inert') && !trailer.disabled,
      close: closeName(), focus: p.doc.activeElement === p.q('.ttl__close'),
      src: frame.getAttribute('src'), sandbox: frame.getAttribute('sandbox'),
      allow: frame.getAttribute('allow'), referrer: frame.getAttribute('referrerpolicy'),
    };
    p.key('Escape');
    p.tick();
    const afterFirst = { trailer: p.ttl().classList.contains('is-trailer'), frame: !!p.q('.ttl__player iframe'),
      open: !p.ttl().classList.contains('is-leaving'), close: closeName(),
      focus: p.doc.activeElement === trailer };
    p.key('Escape');
    return { playing, afterFirst, closedSecond: p.ttl().classList.contains('is-leaving') };
  },

  // The browser's Back and Forward, the Trailer button, Close, a press outside and a reload while a trailer plays, in
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
    click(p.doc, p.q('[data-ttl-act="trailer"]'));
    click(p.doc, p.q('[data-ttl-act="trailer"]'));
    out.button = { asked: backs };
    go.call(p.ctx.history, -1);
    Object.assign(out.button, look(p));
    for (const [name, opts] of Object.entries({ close: {}, closeLink: { hash: '#title=tmdb-movie-77' } })) {
      p = await playing(opts);
      out[name] = ['.ttl__close', '.ttl__close'].map((sel) => { click(p.doc, p.q(sel)); return look(p); });
    }
    p = await playing();
    out.outside = [1, 2].map(() => { click(p.doc, p.q('.ttl__scrim')); return look(p); });
    p = await playing();
    out.doubleClick = [1, 2].map((detail) => { click(p.doc, p.q('.ttl__close'), { detail }); return look(p); });
    p = await playing();
    for (const detail of [2, 3]) click(p.doc, p.q('[data-ttl-act="trailer"]'), { detail });
    out.trailerTwice = look(p);
    click(p.doc, p.q('[data-ttl-act="trailer"]'), { detail: 1 });
    click(p.doc, p.q('[data-ttl-act="trailer"]'), { detail: 2 });
    out.stopTwice = look(p);
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
      endedOnTitle: await run(data({ event: 'onStateChange', info: 0 }), '.ttl__title'),
      paused: await run(data({ event: 'onStateChange', info: 2 })),
      otherOrigin: await run(Object.assign(data({ event: 'onStateChange', info: 0 }), { origin: 'https://evil.example' })),
      otherFrame: await run(Object.assign(data({ event: 'onStateChange', info: 0 }), { source: {} })),
      junk: await run({ data: '{' }),
    };
  },

  // A player that never answers, or says it cannot play, goes back to the artwork and says so; one that answers plays on.
  async trailerStuck() {
    const origin = 'https://www.youtube-nocookie.com';
    const run = async (then, how = {}) => {
      const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="trailer"]'));
      const frame = p.q('.ttl__player iframe');
      let listening = 0;
      frame.contentWindow = { postMessage: (m) => { if (JSON.parse(m).event === 'listening') listening++; } };
      const say = (d) => p.message({ origin, source: frame.contentWindow, data: JSON.stringify(d) });
      const due = (ms) => p.timers.filter((t) => t.fn && t.ms === ms).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
      // A slow connection: the player's own wait has passed before its frame has even loaded.
      if (how.slow) due(8000);
      if (how.never) due(12000);
      else (frame.listeners.load || []).forEach((fn) => fn({}));
      const loaded = p.ttl().classList.contains('is-trailer');
      then(p, say);
      due(250);
      const asked = listening;
      due(8000);
      const out = { playing: p.ttl().classList.contains('is-trailer'), told: p.toasts.map((t) => t.message),
        focus: p.doc.activeElement.getAttribute('data-ttl-act'), asked };
      return how.slow ? { loaded, ...out } : out;
    };
    return {
      silent: await run(() => {}),
      heard: await run((p, say) => say({ event: 'onReady' })),
      refused: await run((p, say) => say({ event: 'onError', info: 150 })),
      stopped: await run((p) => p.key('Escape')),
      slowThenHeard: await run((p, say) => say({ event: 'onReady' }), { slow: true }),
      slowThenSilent: await run(() => {}, { slow: true }),
      neverLoads: await run(() => {}, { never: true }),
    };
  },

  // Over the trailer's area while its frame loads and its player has not yet spoken; gone once it plays or gives way.
  async trailerLoadingMark() {
    const origin = 'https://www.youtube-nocookie.com';
    const run = async (then, loads = true) => {
      const p = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
      p.run();
      await openCard(p, 'tmdb-movie-77');
      click(p.doc, p.q('[data-ttl-act="trailer"]'));
      const frame = p.q('.ttl__player iframe');
      frame.contentWindow = { postMessage: () => {} };
      const say = (d) => p.message({ origin, source: frame.contentWindow, data: JSON.stringify(d) });
      const due = (ms) => p.timers.filter((t) => t.fn && t.ms === ms).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
      const mark = () => {
        const m = p.q('.ttl__player .ttl__wait');
        return m ? { text: m.textContent, role: m.getAttribute('role'), spinner: !!p.q('.ttl__wait .btn-spinner') } : null;
      };
      const pressed = mark();
      if (loads) (frame.listeners.load || []).forEach((fn) => fn({}));
      const loaded = mark();
      then({ p, say, due });
      return { pressed, loaded, after: mark(), playing: p.ttl().classList.contains('is-trailer') };
    };
    return {
      heard: await run(({ say }) => say({ event: 'onReady' })),
      refused: await run(({ say }) => say({ event: 'onError', info: 150 })),
      silent: await run(({ due }) => { due(8000); }),
      never: await run(({ due }) => { due(12000); }, false),
      closed: await run(({ p }) => p.key('Escape')),
      waiting: await run(() => {}),
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
        if (step === 'press') click(p.doc, p.q('[data-ttl-act="trailer"]'));
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
    // Its player answers, as one that plays does.
    const frame = p.q('.ttl__player iframe');
    frame.contentWindow = { postMessage: () => {} };
    p.message({ origin: 'https://www.youtube-nocookie.com', source: frame.contentWindow, data: JSON.stringify({ event: 'onReady' }) });
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

  // What a title's page learns of it, watched elsewhere since its card was drawn, its cards are brought up to: not from its preview alone.
  async arrivalPaints() {
    const watched = Object.assign({}, LIB_MOVIE, { watched: true, mark_kind: 'movie', mark_key: '120' });
    const p = carried({ data: watched });
    const painted = [];
    p.ctx.MMWatch.marks = { paint: (mark, w) => painted.push([mark, w]) };
    p.draw();
    const drawn = painted.slice();
    p.detail.openDrawn();
    await settle();
    return { drawn, opened: painted };
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
    click(p.doc, p.q('.watch-pill[data-season="1"]'));
    await settle();
    const picked = toggles();
    p.q('[data-mark-toggle="episode/11"]').focus();
    seen = true;
    const gets = p.gets.length;
    const say = (ref) => p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'tmdb-tv-9', refs: ['tmdb-tv-9', ref] } });
    say('show-9');
    const otherTitle = p.gets.length - gets;
    painted.length = 0;
    say('show-300');
    await settle();
    const after = { toggles: toggles(), play: primary(p).text, episode: p.text('.watch-ep__title'), tab: p.q('.watch-pill[data-season="1"]').getAttribute('aria-label'),
      focus: p.doc.activeElement.getAttribute('data-mark-toggle'), painted };
    const q = load({ routes: { '/api/watch/title/tmdb-movie-77': MOVIE_DL } });
    q.run();
    await openCard(q, 'tmdb-movie-77');
    return { opened, inLink, picked, otherTitle, after, outside: q.ttl().querySelectorAll('[data-mark-toggle]').length };
  },

  // Marking the season on show, which nobody picked, watched: the show's next season is then the
  // one the server opens on, and the progress above the episodes goes.
  async markedSeasonStays() {
    const eps = (n, watched) => [{ number: 1, key: String(n), title: 'Ep ' + n, minutes: 45, state: watched ? 'watched' : 'none', watched, href: '/watch/play/' + n }];
    let seen = false;
    const leaving = { days: 3, keep_id: '3020', season: 2 };
    const show = () => Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300', leaving, leaving_seasons: [leaving],
      play: seen ? { label: 'Play S2 · E1', href: '/watch/play/21' } : { label: 'Resume S1 · E1', href: '/watch/play/11', progress_pct: 40, minutes_left: 20 },
      seasons: LIB_SHOW.seasons.map((x) => Object.assign({}, x, { key: '30' + x.number, watched: seen && x.number === 1, selected: x.number === (seen ? 2 : 1) })),
      episodes: seen ? eps(21, false) : eps(11, false) });
    const p = load({ routes: {
      '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show()))),
      '/api/watch/title/show-300/season/1': () => Promise.resolve({ ok: true, key: '301', watched: seen, episodes: eps(11, seen) }),
    } });
    p.run();
    p.ctx.MMWatch.marks = { paint: () => {} };
    await openCard(p, 'show-300');
    const root = p.ttl();
    root.scrollTop = 300;
    const mark = () => p.q('[data-ttl-act="mark-season"]');
    const faces = mark().querySelector('.watch-mark-btn__faces').children.map((f) => [f.querySelectorAll('use').length, f.textContent]);
    Node.prototype.getBoundingClientRect = function () {
      return { top: 500 + (p.q('.ttl__progress') ? 40 : 0) - root.scrollTop, bottom: 20, left: 0, right: 20, width: 20, height: 20 };
    };
    const top = () => mark().getBoundingClientRect().top;
    const look = () => ({ on: p.ttl().querySelectorAll('.watch-pill').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-season')),
      eps: p.ttl().querySelectorAll('.watch-ep__title').map((n) => n.textContent), play: primary(p).text, leaving: p.text('.ttl__leaving') !== null,
      pressed: mark().getAttribute('aria-pressed') });
    const was = { look: look(), top: top() };
    click(p.doc, mark());
    seen = true;
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
    await settle();
    return { faces, before: was.look, after: look(), moved: top() - was.top, focus: p.doc.activeElement === mark() };
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

  // Opened while it downloads, its status says no other share than the title did: the sizes and time left still come.
  async downloadingSizes() {
    const p = load({ routes: {
      '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'downloading', pct: 42 }) }),
      '/api/download/status?service=radarr&tmdb_id=77': { state: 'downloading', progress: 42, size_done: '3.1 GB', size_total: '7.4 GB', eta: '~6 min remaining' },
    } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const before = p.text('.dl-progress__line');
    p.tick();
    await settle();
    return [before, p.text('.dl-progress__line'), p.events.length];
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
    const downloading = { primary: primary(p), bar: bar && bar.value, line: p.text('.dl-progress__line'), note: p.text('.ttl__note'), to: (p.q('.ttl__note a') || { getAttribute() {} }).getAttribute('href') };
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

  // A first answer before Radarr had: the page asks once more a little later, and draws what that says,
  // unless the person has moved on or pressed Download meanwhile.
  async radarrLate() {
    const ref = 'tmdb-movie-77';
    async function run(how) {
      const answers = [];
      let held = null;
      const p = load({ routes: {
        ['/api/watch/title/' + ref]: () => {
          const first = !answers.length;
          const dl = first && how !== 'answered' ? { state: null, answered: false } : { state: how === 'pressed' ? null : 'queued', answered: true };
          const data = Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, dl) });
          answers.push(dl);
          if (!first && how === 'moved') return new Promise((resolve) => { held = () => resolve(data); });
          return Promise.resolve(JSON.parse(JSON.stringify(data)));
        },
        '/api/watch/title/show-300': LIB_SHOW,
      } });
      p.run();
      await openCard(p, ref);
      p.q('[data-ttl-act="primary"]').focus();
      const later = () => p.timers.filter((t) => t.fn && t.ms === 2000);
      const waits = later().length;
      if (how === 'pressed') {
        click(p.doc, p.q('[data-ttl-act="primary"]'));
        await settle();
      }
      later().forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
      await settle();
      if (how === 'moved') {
        await openCard(p, 'show-300');
        if (held) held();
        await settle();
      }
      later().forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
      await settle();
      const asked = p.gets.filter((g) => g === '/api/watch/title/' + ref).length;
      return { waits, asked, primary: primary(p).text, focus: p.doc.activeElement.getAttribute('data-ttl-act'),
        told: p.events.filter((e) => e.ref === ref && e.download_state === 'queued').length };
    }
    return { late: await run('late'), answered: await run('answered'), moved: await run('moved'), pressed: await run('pressed') };
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

  // A title still looked for: what the row of downloads says of it, and as that changes.
  async lookedFor() {
    const queued = { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { download: Object.assign({}, MOVIE_DL.download, { state: 'queued', answered: true }) }) };
    const p = load({ routes: queued });
    let tell;
    let state = { items: [{ ref: 'tmdb-movie-77', state: 'searching', line: '' }, { ref: 'tmdb-movie-78', state: 'stuck', line: 'x' }] };
    p.ctx.MM.downloading = { last: () => state, subscribe(fn) { tell = fn; } };
    p.ctx.MMWatch.downloading = { line: (item) => 'Nothing found yet · ' + item.line };
    p.run();
    tell();
    await openCard(p, 'tmdb-movie-77');
    const look = () => [primary(p).text, p.text('.ttl__note')];
    const out = { searching: look() };
    const pill = p.q('[data-ttl-act="primary"]');
    tell();
    out.redrawn = p.q('[data-ttl-act="primary"]') !== pill;
    const to = (item) => { state = { items: [Object.assign({ ref: 'tmdb-movie-77' }, item)] }; tell(); return look(); };
    out.stuck = to({ state: 'stuck', line: 'next attempt in ~4h' });
    out.queued = to({ state: 'queued', line: '' });
    out.other = to({ ref: 'tmdb-movie-78', state: 'searching', line: '' });
    return out;
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

  async collectionPlace() {
    const out = {};
    const parts = (p) => p.q('[data-ttl-part="body"]').children.map((n) => n.className.split(' ')[0]);
    for (const [name, title] of [['film', Object.assign({}, MOVIE_DL, { in_collection: true })], ['lone', Object.assign({}, MOVIE_DL, { in_collection: false })], ['show', LIB_SHOW]]) {
      const url = '/api/watch/title/' + title.ref;
      let answer;
      const p = load({ collection: true, routes: { [url]: () => new Promise((resolve) => { answer = resolve; }), [url + '/collection']: () => new Promise(() => {}) } });
      p.run();
      p.ctx.MMWatch.detail.open(title.ref, { title: title.title });
      const loading = parts(p);
      answer(title);
      await settle();
      out[name] = { loading, drawn: parts(p).filter((c) => c !== 'ttl__collsec' || !p.q('.ttl__collsec').hidden) };
    }
    return out;
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
    const run = async (again, later, message) => {
      let gets = 0;
      let answer;
      const p = load({
        routes: { '/api/watch/title/tmdb-movie-77': () => {
          gets += 1;
          if (gets === 1) return Promise.resolve(MOVIE_DL);
          return gets === 2 ? new Promise((resolve, reject) => { answer = () => (again instanceof Error ? reject(again) : resolve(again)); }) : Promise.resolve(later || again);
        }, '/api/watch/title/movie-120': LIB_MOVIE },
        posted: { '/api/search/download': new APIError(409, 'conflict', message === undefined ? "'Meridian' has already been added" : message) },
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
      unsaid: (await run(dl('queued', 0), null, '')).toasts,
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
    const lead = p.q('[data-ttl-part="under"]').children.map((n) => n.className.split(' ')[0]);
    return lead.concat(p.q('.ttl__lead').parentNode.children.map((n) => n.className).filter((c) => /season-pick/.test(c)).map(() => 'season-pick'));
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

  // A show in each of its states lists its seasons: queued, tracked, a pick, in the library.
  async seasonStates() {
    const out = {};
    const run = async (ref, data) => {
      const p = load({ routes: { ['/api/watch/title/' + ref]: data } });
      p.run();
      p.ctx.MMWatch.detail.open(ref, {});
      await settle();
      return chipsOf(p);
    };
    out.tracked = await run('tmdb-tv-90', asked('tmdb-tv-90', {}));
    out.queued = await run('tmdb-tv-90', asked('tmdb-tv-90', { state: 'queued', answered: true }));
    out.pick = await run('pick-9', asked('pick-9', { state: 'downloading', pct: 10 }));
    const all = LIB_SHOW.seasons.map((s) => Object.assign({}, s, { status: 'held', asked: false, askable: false, refusal: null }));
    const whole = load({ routes: { '/api/watch/title/show-300': Object.assign({}, LIB_SHOW, { seasons: all, more_seasons: [], seasons_pending: false, seasons_request: ASK }) } });
    whole.run();
    await openCard(whole, 'show-300');
    out.whole = { list: !!whole.q('.ttl__askseasons'), link: !!whole.q('.ttl__season-more') };
    out.held = await run('show-300', Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: false,
      seasons: LIB_SHOW.seasons.map((s) => Object.assign({}, s, { status: 'held', asked: false, askable: false, refusal: null })),
      more_seasons: [one(4, 'not_out', { asked: true }), one(3, 'not_asked', { askable: true })] }));
    const refused = asked('tmdb-tv-90', {});
    refused.seasons = ['leaving', 'removed', 'not_listed', 'show_unmonitored'].map((r, i) => one(i + 1, 'not_asked', { refusal: r }));
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': refused } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    click(p.doc, p.q('.season-chip[data-season="1"]'));
    out.refused = { chips: chipsOf(p), why: p.text('.season-pick__why'), go: p.q('.season-pick__go').hidden, posts: p.posts.length };
    return out;
  },

  // Three seasons asked for, one of which fails; then that one again.
  async seasonsAsked() {
    let reply = Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: false,
      more_seasons: [3, 4, 5].map((n) => one(n, 'not_asked', { askable: true })) });
    let answer = { ok: true, message: 'Added season 3. Couldn’t add season 4.', also_started: [],
      seasons: [{ number: 3, result: 'asked' }, { number: 4, result: 'failed' }, { number: 5, result: 'show_unmonitored' }] };
    const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(reply))) },
      posted: { '/api/search/download': () => Promise.resolve(answer) } });
    p.run();
    await openCard(p, 'show-300');
    const go = () => p.q('[data-ttl-act="ask-seasons"]');
    const none = { label: go().textContent.trim(), off: go().getAttribute('aria-disabled') };
    click(p.doc, go());
    click(p.doc, p.q('[data-ttl-act="pick-all"]'));
    const label = go().textContent.trim();
    click(p.doc, go());
    const busy = go().textContent.trim();
    click(p.doc, go());
    reply = Object.assign({}, reply, { more_seasons: [one(3, 'asked', { asked: true }), one(4, 'not_asked', { askable: true }), one(5, 'not_asked', { refusal: 'show_unmonitored' })] });
    await settle();
    const half = { chips: chipsOf(p), said: p.text('.season-pick__out'), error: !!p.q('.season-pick__out .ttl__error'), gets: p.gets.filter((u) => u === '/api/watch/title/show-300').length };
    answer = { ok: true, message: 'Added season 4.', seasons: [{ number: 4, result: 'asked' }], also_started: [] };
    const again = p.q('[data-ttl-act="ask-again"]');
    again.focus();
    click(p.doc, again);
    await settle();
    return { none, label, busy, half, said: p.text('.season-pick__out'), error: !!p.q('.season-pick__out .ttl__error'),
      posts: p.posts.map((x) => [x.url, x.body]) };
  },

  // Asking is refused for all, with each season's result, or fails with none.
  async seasonsRefused() {
    const run = async (err) => {
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': err } });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      click(p.doc, p.q('.season-chip[data-season="4"]'));
      click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
      await settle();
      return { said: p.text('.season-pick__out'), retry: !!p.q('[data-ttl-act="ask-again"]') };
    };
    const refused = new APIError(409, 'leaving', 'Season 4 is leaving.');
    refused.data = { message: 'Season 4 is leaving.', seasons: [{ number: 4, result: 'leaving' }] };
    // Refused, and the reading of the title after it fails.
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': refused } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    p.routes['/api/watch/title/tmdb-tv-90'] = new APIError(0, 'network_error');
    click(p.doc, p.q('.season-chip[data-season="4"]'));
    click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
    await settle();
    const unread = chipsOf(p)[3];
    // Sonarr took the season, and what mediaman keeps of it could not be written.
    const l = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': new APIError(500, 'http_500') } });
    l.run();
    l.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    const took = asked('tmdb-tv-90', {});
    took.seasons[3] = one(4, 'asked', { asked: true });
    l.routes['/api/watch/title/tmdb-tv-90'] = took;
    click(l.doc, l.q('.season-chip[data-season="4"]'));
    click(l.doc, l.q('[data-ttl-act="ask-seasons"]'));
    await settle();
    const taken = { chip: chipsOf(l)[3], said: l.text('.season-pick__out'), retry: !!l.q('[data-ttl-act="ask-again"]') };
    return { taken, unread, refused: await run(refused), failed: await run(new APIError(502, 'sonarr_failed', 'Sonarr did not answer.')) };
  },

  // Asking when the answer is lost, when it is a duplicate, and when the server itself could not note it.
  async seasonsUnsure() {
    const run = async (err, reads) => {
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': err } });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      const took = asked('tmdb-tv-90', {});
      if (reads) took.seasons[3] = one(4, 'asked', { asked: true });
      p.routes['/api/watch/title/tmdb-tv-90'] = took;
      click(p.doc, p.q('.season-chip[data-season="4"]'));
      click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
      await settle();
      return { said: p.text('.season-pick__out'), bad: !!p.q('.season-pick__out .ttl__error'), retry: !!p.q('[data-ttl-act="ask-again"]'), chip: chipsOf(p)[3][1] };
    };
    const dup = new APIError(429, 'http_429');
    dup.data = { message: 'Duplicate request', duplicate: true };
    const noted = { ok: false, message: 'Season 4 is asked for, but could not be noted.', seasons: [{ number: 4, result: 'unrecorded' }] };
    const unrecorded = new APIError(502, 'x');
    unrecorded.data = noted;
    const first = load({ routes: { '/api/watch/title/tmdb-tv-90': TV_DL }, posted: { '/api/search/download': dup } });
    first.run();
    first.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    first.routes['/api/watch/title/tmdb-tv-90'] = Object.assign({}, TV_DL, { sonarr_tracked: true });
    click(first.doc, first.q('[data-ttl-act="primary"]'));
    await settle();
    // Refused in part while the title is closed, and whole.
    const closed = async (seasons) => {
      let answer;
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) },
        posted: { '/api/search/download': () => new Promise((resolve) => { answer = () => resolve({ ok: true, message: 'Added.', seasons }); }) } });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      click(p.doc, p.q('[data-ttl-act="pick-all"]'));
      click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
      click(p.doc, p.q('.ttl__close'));
      await settle();
      answer();
      await settle();
      const o = p.toasts[p.toasts.length - 1].o;
      return (o && o.variant) || null;
    };
    // Another request is told of, which takes the season, or comes to nothing.
    const twice = async (taken, fails) => {
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': dup } });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      click(p.doc, p.q('.season-chip[data-season="4"]'));
      click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
      await settle();
      const said = [p.text('.season-pick__out'), p.text('[data-ttl-part="status"]')];
      const now = asked('tmdb-tv-90', {});
      if (taken) now.seasons[3] = one(4, 'asked', { asked: true });
      p.routes['/api/watch/title/tmdb-tv-90'] = fails ? new APIError(0, 'network_error') : now;
      p.timers.filter((t) => t.fn && t.ms === 5000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
      await settle();
      return said.concat(p.text('.season-pick__out'));
    };
    // A Download of a show that was added meanwhile, refused season by season.
    const each = new APIError(409, 'x');
    each.data = { message: 'Season 1 is leaving.', seasons: [{ number: 1, result: 'leaving' }, { number: 2, result: 'failed' }] };
    const added = load({ routes: { '/api/watch/title/tmdb-tv-90': TV_DL }, posted: { '/api/search/download': each } });
    added.run();
    added.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    added.routes['/api/watch/title/tmdb-tv-90'] = Object.assign(asked('tmdb-tv-90', {}), { seasons: [one(1, 'not_asked', { refusal: 'leaving' }), one(2, 'not_asked', { askable: true }), one(3, 'held', { in_library: true })] });
    click(added.doc, added.q('[data-ttl-act="primary"]'));
    await settle();
    return { taken: await twice(true), nothing: await twice(false), unreached: await twice(false, true),
      each: { said: added.text('.season-pick__out'), retry: !!added.q('[data-ttl-act="ask-again"]'), top: added.text('[data-ttl-part="under"] .ttl__error'), primary: primary(added).text, toasts: added.toasts.length, chip: chipsOf(added)[0][1] },
      worded: [await run(new APIError(0, 'network_error', 'Could not reach mediaman. Try again.')), await run(new APIError(500, 'http_500')), await run(new APIError(422, 'invalid'))].map((x) => x.said),
      lost: await run(new APIError(0, 'http_0'), true), unread: await run(new APIError(0, 'http_0'), false),
      dup: await run(dup, true), unrecorded: await run(unrecorded, true),
      first: { primary: primary(first).text, error: !!first.q('.ttl__under .ttl__error') },
      half: await closed([{ number: 3, result: 'asked' }, { number: 4, result: 'failed' }]),
      whole: await closed([{ number: 3, result: 'asked' }, { number: 4, result: 'asked' }]) };
  },

  // "Check again" where Sonarr gave no answer: it still gives none, gives one, cannot be reached, or takes a while.
  async seasonsChecked() {
    const show = (status, pending, over) => Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: pending,
      seasons: LIB_SHOW.seasons.map((s) => Object.assign({}, s, { status: 'held', asked: false, askable: false, refusal: null })),
      more_seasons: [one(3, status, over)] });
    const there = show('not_asked', false, { askable: true });
    const run = async (replies) => {
      const p = load({ clock: true, routes: { '/api/watch/title/show-300': show(null, false) } });
      p.run();
      await openCard(p, 'show-300');
      const go = () => p.q('.season-pick__go').textContent.trim();
      const due = async (ms) => {
        const all = p.timers.filter((t) => t.fn && t.ms === ms);
        all.forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
        await settle();
        return all.length;
      };
      p.routes['/api/watch/title/show-300'] = replies[0];
      click(p.doc, p.q('.season-pick__go'));
      await settle();
      const out = { at: go() };
      if (replies.length > 1) {
        p.routes['/api/watch/title/show-300'] = replies[1];
        p.clock.now += 2000;
        out.during = [go(), p.q('.season-pick__go').getAttribute('aria-disabled') === 'true', (await due(2000)) && 2000];
      } else out.waits = (await due(600)) === 1;
      return Object.assign(out, { go: go(), said: p.text('[data-ttl-part="status"]') });
    };
    // The library has another season meanwhile, while Sonarr is still asked.
    const more = Object.assign(show(null, true), { seasons: show(null, true).seasons.concat(Object.assign({}, LIB_SHOW.seasons[1], { number: 9, status: 'held' })) });
    const grown = load({ clock: true, routes: { '/api/watch/title/show-300': show(null, false) } });
    grown.run();
    await openCard(grown, 'show-300');
    let replies = 0;
    grown.routes['/api/watch/title/show-300'] = () => { replies += 1; return Promise.resolve(replies > 1 ? Object.assign({}, more, there, { seasons: more.seasons }) : more); };
    click(grown.doc, grown.q('.season-pick__go'));
    await settle();
    // Two readings of one check, and something asked for between them.
    const twice = load({ clock: true, routes: { '/api/watch/title/show-300': show(null, false) }, posted: { '/api/search/download': () => new Promise(() => {}) } });
    twice.run();
    await openCard(twice, 'show-300');
    const held = [];
    twice.routes['/api/watch/title/show-300'] = () => new Promise((resolve) => { held.push(() => resolve(there)); });
    click(twice.doc, twice.q('.season-pick__go'));
    // The page is looked at again, and the slow poll comes due.
    twice.doc.dispatchEvent({ type: 'visibilitychange' });
    twice.timers.filter((t) => t.fn && t.ms === 30000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    twice.clock.now += 1000;
    if (held[0]) held[0]();
    await settle();
    click(twice.doc, twice.q('.season-chip[data-season="3"]'));
    click(twice.doc, twice.q('[data-ttl-act="ask-seasons"]'));
    if (held[1]) held[1]();
    await settle();
    click(twice.doc, twice.q('[data-ttl-act="ask-seasons"]'));
    const pick = (o, ...names) => Object.fromEntries(names.map((n) => [n, o[n]]));
    return { grown: grown.q('.season-pick__go').textContent.trim(), between: [held.length, twice.posts.length, twice.q('.season-pick__go').textContent.trim()], unknown: await run([show(null, false)]), back: await run([there]),
      failed: pick(await run([new APIError(0, 'network_error')]), 'go', 'said'), pending: await run([show(null, true), there]) };
  },

  // What is scrolled to: an answer that refuses, with and without motion, one that does not, a Download refused.
  async seasonsRevealed() {
    const out = {};
    const run = async (still, answer, act, then) => {
      const p = load({ still, routes: { '/api/watch/title/tmdb-tv-90': act ? TV_DL : asked('tmdb-tv-90', {}) }, posted: { '/api/search/download': () => answer(p) } });
      const seen = [];
      Node.prototype.scrollIntoView = function (o) { seen.push([this.className.split(' ')[0], o.block, o.behavior]); };
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      if (!act) click(p.doc, p.q('.season-chip[data-season="4"]'));
      click(p.doc, p.q('[data-ttl-act="' + (act || 'ask-seasons') + '"]'));
      await settle();
      const now = seen.slice();
      if (then) await then(p);
      return now;
    };
    const no = () => Promise.reject(new APIError(502, 'x', 'No.'));
    const ok = () => Promise.resolve({ ok: true, message: 'Added.', seasons: [{ number: 4, result: 'asked' }] });
    out.refused = await run(false, (p) => (p.posts.length > 1 ? ok() : no()), null, async (p) => {
      click(p.doc, p.q('[data-ttl-act="ask-again"]'));
      const line = p.q('.season-pick__out').firstElementChild;
      out.again = { lines: p.q('.season-pick__out').children.length, inert: !!line && line.hasAttribute('inert') };
      await settle();
      out.again.after = p.q('.season-pick__out').children.map((n) => n.textContent.trim());
    });
    out.still = await run(true, no);
    out.asked = await run(false, ok);
    out.download = await run(false, no, 'primary');
    const unreached = () => Promise.reject(new APIError(0, 'network_error', 'Could not reach mediaman. Check your connection and try again.'));
    await run(false, unreached, 'primary', async (p) => { out.lost = p.text('[data-ttl-part="under"] .ttl__error'); });
    return out;
  },

  // Sonarr is down, for a show not known to be there.
  async seasonsDown() {
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': Object.assign({}, TV_DL, { seasons_request: ASK, seasons_pending: false, more_seasons: [],
      seasons: [one(1, null, { air_date: '2020-01-01' }), one(2, null), one(3, null, { air_date: '2999-01-31' }), one(4, null, { episode_count: 0 })] }) } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    return { primary: primary(p).text, chips: chipsOf(p) };
  },

  // A show in the library with one season partly here, alone and beside one that is not.
  async seasonsNamed() {
    const run = async (more) => {
      const seasons = [Object.assign({}, LIB_SHOW.seasons[0], { status: 'partly_held', asked: false, askable: true, refusal: null })]
        .concat(Object.assign({}, LIB_SHOW.seasons[1], { status: 'held', asked: false, askable: false, refusal: null }));
      const p = load({ routes: { '/api/watch/title/show-300': Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: false, seasons, more_seasons: more }) } });
      p.run();
      await openCard(p, 'show-300');
      return [p.text('.ttl__season-more'), p.text('#ttl-pick-label')];
    };
    return { partly: await run([]), more: await run([one(3, 'not_asked', { askable: true })]) };
  },

  // What the line of what is picked says.
  async seasonsSummed() {
    const run = async (press, over) => {
      const data = Object.assign(asked('tmdb-tv-90', {}), { seasons: [one(1, 'not_asked', { askable: true }), one(2, 'not_asked', { askable: true }),
        one(3, 'not_asked', { askable: true }), one(4, 'not_out', { askable: true })] }, over);
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': data } });
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      press.forEach((n) => click(p.doc, p.q('.season-chip[data-season="' + n + '"]')));
      return p.text('.season-pick__summary');
    };
    const off = { also_starts: [0, 3].reverse() };
    // Asked for, the show is off no more.
    const l = load({ routes: { '/api/watch/title/tmdb-tv-90': Object.assign(asked('tmdb-tv-90', {}), { seasons: [1, 2, 3].map((n) => one(n, 'not_asked', { askable: true })) }, off) } });
    l.run();
    l.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    l.routes['/api/watch/title/tmdb-tv-90'] = Object.assign(asked('tmdb-tv-90', {}), { also_starts: [], seasons: [one(1, 'asked', { asked: true }), one(2, 'not_asked', { askable: true }), one(3, 'asked', { asked: true })] });
    click(l.doc, l.q('.season-chip[data-season="1"]'));
    click(l.doc, l.q('[data-ttl-act="ask-seasons"]'));
    await settle();
    click(l.doc, l.q('.season-chip[data-season="2"]'));
    return { both: await run([1, 2, 4]), later: await run([4]), starts: await run([1], off), named: await run([1, 3], off), none: await run([], off), on: l.text('.season-pick__summary') };
  },

  // Partly here and leaving, and still to come and not to be asked for.
  async seasonsLocked() {
    const data = Object.assign(asked('tmdb-tv-90', {}), { seasons: [one(1, 'partly_held', { refusal: 'leaving' }), one(2, 'not_out', { refusal: 'not_listed' })] });
    const p = load({ routes: { '/api/watch/title/tmdb-tv-90': data } });
    p.run();
    p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
    await settle();
    return p.ttl().querySelectorAll('.season-chip').map((c) => [c.getAttribute('data-state'), c.getAttribute('data-icon')]);
  },

  // The first Download brings a line in under the actions, in sight and scrolled past.
  async seasonsSlide() {
    const run = async (past) => {
      const p = load({ routes: { '/api/watch/title/tmdb-tv-90': Object.assign({}, TV_DL, { overview: 'By the sea.', seasons_pending: false }) } });
      const moved = [];
      const native = Node.prototype.getBoundingClientRect;
      p.run();
      p.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      const after = p.q('.ttl__askseasons').parentNode.appendChild(p.doc.createElement('section'));
      after.className = 'ttl__after';
      const under = p.q('[data-ttl-part="under"]');
      p.routes['/api/watch/title/tmdb-tv-90'] = Object.assign({}, TV_DL, { overview: 'By the sea.', seasons_pending: false, sonarr_tracked: true, seasons: [1, 2, 3].map((n) => one(n, 'asked', { asked: true })) });
      Node.prototype.getBoundingClientRect = function () {
        const base = this === p.ttl() ? 0 : this.closest('.ttl__actions') ? (past ? -100 : 100) : 300 + (under.hidden ? 0 : 35) +
          // The list grows too.
          (this === after && p.q('.season-chip[data-season="1"]').getAttribute('data-state') === 'queued' ? 10 : 0);
        const top = base - (this === p.ttl() ? 0 : p.ttl().scrollTop);
        return { top, bottom: top + 20, left: 0, right: 20, width: 20, height: 20 };
      };
      Node.prototype.animate = function (frames) { moved.push([this, Number(/, (-?\d+)px/.exec(frames[0].transform)[1])]); };
      try {
        click(p.doc, p.q('[data-ttl-act="primary"]'));
        await settle();
      } finally {
        Node.prototype.getBoundingClientRect = native;
        delete Node.prototype.animate;
      }
      const of = (cls) => moved.filter((m) => m[0].className.split(' ')[0] === cls);
      const by = (cls) => of(cls).map((m) => m[1]);
      return { moved: Object.fromEntries(['ttl__overview', 'season-pick__head', 'season-pick__summary', 'ttl__after'].map((c) => [c, by(c)])),
        chips: p.ttl().querySelectorAll('.season-chip').map((c) => moved.filter((m) => m[0] === c).map((m) => m[1])), n: moved.length, scrolled: p.ttl().scrollTop };
    };
    const past = await run(true);
    return Object.assign(await run(false), { scrolled: past.scrolled, slidThen: past.n });
  },

  // A season joins at the front of two rows: one is pushed to the next, the others stay in theirs.
  async seasonsRows() {
    const show = (there) => Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: !there,
      seasons: LIB_SHOW.seasons.map((s, i) => Object.assign({}, s, { status: there && i ? 'partly_held' : 'held', asked: false, askable: there && !!i, refusal: null })),
      more_seasons: [3, 4, 5].map((n) => one(n, there ? 'not_asked' : null, { askable: there })) });
    const p = load({ routes: { '/api/watch/title/show-300': show(false) } });
    p.run();
    await openCard(p, 'show-300');
    const at = (chip) => p.ttl().querySelectorAll('.season-chip').indexOf(chip);
    const native = Node.prototype.getBoundingClientRect;
    const slid = [];
    Object.defineProperty(Node.prototype, 'offsetTop', { configurable: true, get() { return Math.floor(at(this) / 2) * 60; } });
    Node.prototype.getBoundingClientRect = function () {
      const i = at(this);
      return i < 0 ? native.call(this) : { top: Math.floor(i / 2) * 60, left: (i % 2) * 180, bottom: 0, right: 0, width: 20, height: 20 };
    };
    Node.prototype.animate = function () { slid.push(this.getAttribute('data-season')); };
    const chips = p.ttl().querySelectorAll('.season-chip');
    chips.forEach((c) => c.classList.remove('fade-in'));
    p.routes['/api/watch/title/show-300'] = show(true);
    p.timers.filter((t) => t.fn && t.ms === 2000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    await settle();
    return { slid: slid.filter(Boolean), faded: p.ttl().querySelectorAll('.season-chip').filter((c) => c.classList.contains('fade-in')).map((c) => c.getAttribute('data-season')) };
  },

  // The states come late, and a season is then known to be partly here.
  async seasonsJoined() {
    const show = (there) => Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: !there,
      seasons: LIB_SHOW.seasons.map((s, i) => Object.assign({}, s, { status: there && i ? 'partly_held' : 'held', asked: false, askable: there && !!i, refusal: null })),
      more_seasons: [3, 4].map((n) => one(n, there ? 'not_asked' : null, { askable: there })) });
    const p = load({ routes: { '/api/watch/title/show-300': show(false) } });
    p.run();
    await openCard(p, 'show-300');
    const chips = () => p.ttl().querySelectorAll('.season-chip');
    const was = chips();
    was[0].focus();
    p.routes['/api/watch/title/show-300'] = show(true);
    p.timers.filter((t) => t.fn && t.ms === 2000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    await settle();
    return { numbers: chips().map((c) => c.getAttribute('data-season')), kept: was.map((c) => chips().indexOf(c) >= 0), focus: p.doc.activeElement.getAttribute('data-season') };
  },

  // Sonarr is not set up: a library show with seasons missing.
  async seasonsNoSonarr() {
    const p = load({ routes: { '/api/watch/title/show-300': Object.assign({}, LIB_SHOW, { seasons_request: null, seasons_pending: false, more_seasons: [one(3, null), one(4, 'not_out')] }) } });
    p.run();
    await openCard(p, 'show-300');
    return { chips: chipsOf(p), go: p.q('.season-pick__go').hidden, why: p.text('.season-pick__why'), timers: p.timers.filter((t) => t.fn && t.ms === 30000).length };
  },

  // A reading of the title begun before seasons are asked for lands after they were read again.
  async seasonsOlderReading() {
    const copy = (x) => JSON.parse(JSON.stringify(x));
    const before = Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: false, more_seasons: [one(3, 'not_asked', { askable: true })] });
    const after = Object.assign({}, before, { more_seasons: [one(3, 'asked', { asked: true })] });
    let reply = () => Promise.resolve(copy(before));
    const p = load({ routes: { '/api/watch/title/show-300': () => reply() } });
    p.run();
    await openCard(p, 'show-300');
    let late;
    reply = () => new Promise((resolve) => { late = () => resolve(copy(before)); });
    p.doc.dispatchEvent({ type: 'mm:watch-kept', detail: {} });
    await settle();
    reply = () => Promise.resolve(copy(after));
    click(p.doc, p.q('.season-chip[data-season="3"]'));
    click(p.doc, p.q('[data-ttl-act="ask-seasons"]'));
    await settle();
    late();
    await settle();
    // The same of one that a mark began, and of one that knows no states yet.
    const marked = async (pending) => {
      const m = load({ routes: { '/api/watch/title/show-300': () => reply() } });
      reply = () => Promise.resolve(copy(before));
      m.run();
      await openCard(m, 'show-300');
      const bare = Object.assign(copy(before), { seasons_pending: true, more_seasons: [one(3, null)] });
      const old = [];
      reply = pending ? () => Promise.resolve(bare) : () => new Promise((resolve) => { old.push(() => resolve(copy(before))); });
      m.routes['/api/watch/title/show-300/season/2'] = { ok: true, key: '302', episodes: [] };
      m.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
      await settle();
      if (pending) return chipsOf(m);
      reply = () => Promise.resolve(copy(after));
      click(m.doc, m.q('.season-chip[data-season="3"]'));
      click(m.doc, m.q('[data-ttl-act="ask-seasons"]'));
      await settle();
      old.forEach((f) => f());
      await settle();
      return chipsOf(m);
    };
    const marks = [await marked(false), await marked(true)];
    // The same of a show not in the library, whose title the poll reads, or a download just refused.
    const outside = async (poll) => {
      const old = Object.assign({}, TV_DL, { seasons_request: ASK, seasons_pending: false, more_seasons: [], seasons: [one(1, 'asked', { asked: true }), one(2, 'not_asked', { askable: true })] });
      const tracked = Object.assign({}, old, { sonarr_tracked: true });
      const now = Object.assign({}, tracked, { seasons: [one(1, 'asked', { asked: true }), one(2, 'asked', { asked: true })] });
      let read = () => Promise.resolve(copy(old));
      let gets = null;
      const t = load({ routes: { '/api/watch/title/tmdb-tv-90': () => read() }, posted: { '/api/search/download': () => (t.posts.length === 1
        ? Promise.reject(new APIError(409, 'conflict')) : Promise.resolve({ ok: true, message: 'Added.', seasons: [{ number: 2, result: 'asked' }] })) } });
      t.run();
      t.ctx.MMWatch.detail.open('tmdb-tv-90', {});
      await settle();
      const hold = () => new Promise((resolve) => { gets = () => resolve(copy(tracked)); });
      read = poll ? () => Promise.resolve(copy(tracked)) : hold;
      click(t.doc, t.q('[data-ttl-act="primary"]'));
      await settle();
      if (poll) {
        read = hold;
        t.timers.filter((x) => x.fn && x.ms === 30000).forEach((x) => { const fn = x.fn; x.fn = null; fn(); });
        await settle();
      }
      read = () => Promise.resolve(copy(now));
      click(t.doc, t.q('.season-chip[data-season="2"]'));
      click(t.doc, t.q('[data-ttl-act="ask-seasons"]'));
      await settle();
      const asked2 = chipsOf(t)[1][1];
      gets();
      await settle();
      t.doc.dispatchEvent({ type: 'mm:search-assist' }); // anything that draws it again
      return [asked2, chipsOf(t)[1][1]];
    };
    // Of a pick, read for the email it may come with.
    const pick = Object.assign({}, TV_DL, { ref: 'pick-9', seasons: [1, 2].map((n) => one(n, 'not_asked', { askable: true })),
      seasons_request: { url: '/api/watch/picks/9/download', body: {} },
      download: { state: null, service: 'sonarr', tmdb_id: 90, request: { url: '/api/watch/picks/9/download' } } });
    let readPick = () => Promise.resolve(copy(pick));
    let gotPick;
    const n = load({ routes: { '/api/watch/title/pick-9': () => readPick() } });
    n.run();
    n.ctx.MMWatch.detail.open('pick-9', {});
    await settle();
    click(n.doc, n.q('.season-chip[data-season="2"]'));
    readPick = () => new Promise((resolve) => { gotPick = () => resolve(copy(pick)); });
    click(n.doc, n.q('[data-ttl-act="primary"]'));
    await settle();
    const slow = gotPick;
    readPick = () => new Promise(() => {});
    click(n.doc, n.q('.season-chip[data-season="2"]'));
    click(n.doc, n.q('[data-ttl-act="ask-seasons"]'));
    await settle();
    slow();
    await settle();
    return { marks, notified: chipsOf(n).map((c) => c[1]), library: chipsOf(p), polled: await outside(true), refused: await outside(false) };
  },

  // The states are still to come, then there; or they never come.
  async seasonsPending() {
    const run = async (comes) => {
      let n = 0;
      const p = load({ routes: { '/api/watch/title/show-300': () => {
        n += 1;
        const there = comes && n > 2;
        return Promise.resolve(Object.assign({}, LIB_SHOW, { seasons_request: ASK, seasons_pending: !there,
          seasons: LIB_SHOW.seasons.map((s) => Object.assign({}, s, { status: 'held', asked: false, askable: false, refusal: null })),
          more_seasons: [one(3, there ? 'not_asked' : null, { askable: there })] }));
      } } });
      p.run();
      await openCard(p, 'show-300');
      const first = chipsOf(p)[0];
      const list = p.q('.watch-episodes');
      const waits = [];
      for (let i = 0; i < 8; i += 1) {
        const due = p.timers.filter((t) => t.fn && t.ms === 2000);
        waits.push(due.length);
        due.forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
        await settle();
      }
      const press = {};
      const asks = n;
      if (!comes) {
        press.label = p.q('.season-pick__go').textContent.trim();
        click(p.doc, p.q('.season-pick__go'));
        press.busy = p.q('.season-pick__go').textContent.trim();
        await settle();
        for (let i = 0; i < 6; i += 1) { // read again while it is asked, then shown for a while at least
          p.timers.filter((t) => t.fn && (t.ms === 2000 || (t.ms > 500 && t.ms <= 600))).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
          await settle();
        }
      }
      return { press, first, last: chipsOf(p)[0], asks, go: p.q('.season-pick__go').hidden ? null : p.q('.season-pick__go').textContent.trim(), still: list === p.q('.watch-episodes') };
    };
    return { comes: await run(true), never: await run(false) };
  },

  // A show that is a pick, wanted whole and in part.
  async pickSeasons() {
    const run = async (press, last) => {
      const data = Object.assign({}, TV_DL, { ref: 'pick-9', seasons: [1, 2, 3].map((n) => one(n, n === 3 && last || 'not_asked', { askable: true })),
        seasons_request: { url: '/api/watch/picks/9/download', body: {} },
        download: { state: null, service: 'sonarr', tmdb_id: 90, request: { url: '/api/watch/picks/9/download' } } });
      const p = load({ routes: { '/api/watch/title/pick-9': data } });
      p.run();
      p.ctx.MMWatch.detail.open('pick-9', {});
      await settle();
      if (press) click(p.doc, p.q(press));
      click(p.doc, p.q('[data-ttl-act="primary"]'));
      const go = p.q('.season-pick__go').hidden;
      await settle();
      return { body: p.posts[0].body || null, go, primary: primary(p).text };
    };
    return { whole: await run(null), part: await run('.season-chip[data-season="2"]'),
      out: await run(null, 'not_out'), withComing: await run('.season-chip[data-season="3"]', 'not_out') };
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

  // What a season and its rows say, and how their texts open; another season, slow, then failing;
  // and the one on show once a mark loads the title again.
  async seasonTexts() {
    process.env.TZ = 'America/New_York';
    const ep = (n, over) => Object.assign({ number: n, key: '2' + n, title: 'Ep ' + n, minutes: 50, thumb_url: '/api/watch/thumb/2' + n,
      state: 'none', watched: false, href: '/watch/play/2' + n }, over);
    const show = () => Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300', overview: 'A show.',
      play: { label: 'Resume S2 · E2', href: '/watch/play/22' },
      seasons: [Object.assign({}, LIB_SHOW.seasons[0], { summary: 'A show.', year: 2018 }), Object.assign({}, LIB_SHOW.seasons[1], { summary: 'Its <b>second</b> year.', year: 2019 })],
      episodes: [ep(1, { state: 'watched', watched: true, summary: 'It <i>begins</i>.', aired: '2019-05-04' }),
        ep(2, { state: 'in_progress', progress_pct: 40, minutes_left: 30, summary: 'It goes on.', aired: 'soon' }),
        ep(3, { state: 'in_progress', progress_pct: 10, minutes_left: 45 }), ep(4, { aired: '2999-01-02' })] });
    let answer;
    const slow = () => new Promise((resolve, reject) => { answer = { resolve, reject }; });
    let first = slow;
    const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(show()), '/api/watch/title/show-300/season/1': () => first() } });
    p.run();
    await openCard(p, 'show-300');
    const row = (li) => {
      const a = li.querySelector('.watch-ep');
      const about = li.querySelector('.watch-ep__about');
      return { state: a.querySelector('.watch-ep__state') && a.querySelector('.watch-ep__state').textContent, meta: a.querySelector('.watch-ep__meta').textContent,
        facts: a.querySelector('.watch-ep__meta').querySelectorAll('.watch-ep__fact').map((f) => f.textContent),
        about: about && about.textContent, describes: !!about && a.getAttribute('aria-describedby') === about.id, inName: !!about && about.getAttribute('aria-hidden') !== 'true',
        tick: !!a.querySelector('.watch-ep__tick'), controlsInLink: a.querySelectorAll('button').length, more: li.querySelectorAll('.watch-ep__more').length };
    };
    const season = () => ({ facts: p.text('.ttl__season-facts'), text: p.q('.ttl__season-text') && p.text('.ttl__season-text') });
    const rows = p.ttl().querySelectorAll('.watch-eprow').map(row);
    const opened = season();
    const held = p.q('.ttl__season-about');
    const more = p.q('.ttl__season-about .ttl-more');
    const look = () => [held.classList.contains('is-open'), more.textContent, more.getAttribute('aria-expanded'), more.getAttribute('aria-label')];
    const press = [look()];
    for (let i = 0; i < 2; i++) { click(p.doc, more); press.push(look()); }
    const waits = () => [p.q('.ttl__season').classList.contains('is-waiting'), p.ttl().querySelectorAll('.ttl__season .ttl-epskel').length > 0, p.q('.watch-episodes').getAttribute('aria-busy'), !!p.q('.watch-episodes').inert, !!p.q('.ttl__season-about').inert];
    click(p.doc, p.q('.watch-pill[data-season="1"]'));
    const wait = waits();
    answer.reject(new Error('down'));
    await settle();
    const failed = { waits: waits(), season: season() };
    click(p.doc, p.q('.watch-pill[data-season="1"]'));
    const one = { ok: true, key: '301', watched: false, summary: 'Told by another.', year: 2017, episodes: [ep(9, { summary: 'Nine.' })] };
    answer.resolve(one);
    await settle();
    const picked = season();
    first = () => Promise.resolve(one);
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
    await settle();
    const marked = { season: season(), about: p.text('.watch-ep__about') };
    first = () => Promise.resolve({ ok: true, key: '301', watched: false, episodes: [ep(9, {})] });
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
    await settle();
    return { rows, opened, press, wait, failed, picked, marked, older: season(), bare: p.ttl().querySelectorAll('.watch-ep__about').length };
  },

  // Texts opened by More, and the title drawn again by a mark, then another season and back.
  async openedTexts() {
    const eps = [1, 2].map((n) => ({ number: n, key: '2' + n, title: 'Ep ' + n, minutes: 50, state: 'none', href: '/watch/play/2' + n, summary: 'About ' + n }));
    const show = Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300', episodes: eps,
      seasons: [LIB_SHOW.seasons[0], Object.assign({}, LIB_SHOW.seasons[1], { summary: 'Its second year.' })] });
    const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show))),
      '/api/watch/title/show-300/season/1': { ok: true, key: '301', episodes: [eps[0], eps[1]], summary: 'Its first.' },
      '/api/watch/title/show-300/season/2': { ok: true, key: '302', episodes: eps, summary: 'Its second year.' } } });
    p.run();
    await openCard(p, 'show-300');
    const opened = () => [p.q('.ttl__season-about')].concat(p.ttl().querySelectorAll('.watch-eprow')).map((n) => n.classList.contains('is-open'));
    const more = p.ttl().querySelectorAll('.ttl-more');
    click(p.doc, more[0]);
    click(p.doc, more[2]);
    more[2].focus();
    const out = { pressed: opened() };
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
    await settle();
    const fresh = p.ttl().querySelectorAll('.ttl-more')[2];
    out.marked = [opened(), fresh !== more[2], p.doc.activeElement === fresh, fresh.textContent, fresh.getAttribute('aria-expanded')];
    click(p.doc, fresh);
    out.closed = opened();
    click(p.doc, p.q('.watch-pill[data-season="1"]'));
    await settle();
    out.other = opened();
    return out;
  },

  // What TMDB says of a season once it is on show: of the show's own, of one picked, while something
  // is pressed, and of one left meanwhile.
  async lateWords() {
    const ep = (n, over) => Object.assign({ number: n, key: '2' + n, title: 'Ep ' + n, minutes: 50, state: 'none', href: '/watch/play/2' + n, summary: '' }, over);
    const told = (n) => ({ ok: true, key: '30' + n, summary: 'Words on ' + n, year: 2015 + n, words_pending: false,
      episodes: [ep(1, { summary: 'About one.', aired: '2019-05-04' }), ep(2)] });
    const bare = (n) => ({ ok: true, key: '30' + n, summary: '', year: null, words_pending: true, episodes: [ep(1), ep(2)] });
    const show = Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300', words_pending: true, episodes: [ep(1), ep(2)] });
    const start = async (first) => {
      const asks = [];
      const words = (n) => () => new Promise((resolve, reject) => asks.push({ n, resolve: () => resolve(told(n)), reject }));
      const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show))),
        '/api/watch/title/show-300/season/1': () => Promise.resolve(Object.assign(bare(1), first)), '/api/watch/title/show-300/season/2': () => Promise.resolve(bare(2)),
        '/api/watch/title/show-300/season/1?words=1': words(1), '/api/watch/title/show-300/season/2?words=1': words(2) } });
      p.run();
      await openCard(p, 'show-300');
      return { p, asks };
    };
    const look = (p) => ({ facts: p.text('.ttl__season-facts'), text: p.text('.ttl__season-text'),
      rows: p.ttl().querySelectorAll('.watch-eprow').map((li) => [li.querySelector('.watch-ep__meta').textContent, li.querySelector('.watch-ep__about') && li.querySelector('.watch-ep__about').textContent, li.querySelectorAll('.ttl-more').length]) });
    const out = {};
    {
      const { p, asks } = await start();
      const rows = p.ttl().querySelectorAll('.watch-eprow');
      const link = rows[1].querySelector('.watch-ep');
      link.focus();
      out.before = look(p);
      p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:search-assist', {}));
      asks[0].resolve();
      await settle();
      out.came = look(p);
      out.same = [p.ttl().querySelectorAll('.watch-eprow').every((li, i) => li === rows[i]), p.doc.activeElement === link, asks.length];
      click(p.doc, p.q('.watch-pill[data-season="1"]'));
      await settle();
      out.picked = [look(p).text, asks.map((a) => a.n)];
      asks[1].resolve();
      await settle();
      out.pickedCame = look(p);
    }
    {
      const { p, asks } = await start();
      p.doc.dispatchEvent({ type: 'pointerdown' });
      asks[0].resolve();
      await settle();
      out.held = look(p).text;
      p.doc.dispatchEvent({ type: 'pointerup' });
      p.tick();
      out.letGo = look(p).text;
    }
    {
      const { p, asks } = await start();
      p.doc.dispatchEvent({ type: 'pointerdown' });
      asks[0].resolve();
      await settle();
      for (let i = 0; i < 29; i++) p.tick();
      out.unended = [look(p).text];
      p.tick();
      out.unended.push(look(p).text, p.timers.filter((t) => t.ms === 100).length);
    }
    {
      const { p, asks } = await start({ words_pending: false });
      click(p.doc, p.q('.watch-pill[data-season="1"]'));
      await settle();
      asks[0].resolve();
      await settle();
      out.left = look(p).text;
    }
    {
      const { p, asks } = await start();
      asks[0].reject(new APIError(503, 'x'));
      await settle();
      out.failed = look(p);
    }
    return out;
  },

  // A mark made, and more of an episode seen, while the words of their season were on their way,
  // of the season the title opens on and of one that was *picked*.
  async markBeforeWords() {
    async function run(picked) {
      const n = picked ? 1 : 2;
      let seen = false;
      const eps = (seen, summary) => [
        { number: 1, key: '21', title: 'Ep 1', minutes: 50, state: seen ? 'watched' : 'none', watched: seen, href: '/watch/play/21', summary },
        { number: 2, key: '22', title: 'Ep 2', minutes: 50, state: 'in_progress', minutes_left: seen ? 10 : 40, href: '/watch/play/22', summary }];
      const season = () => ({ ok: true, key: '30' + n, watched: false, summary: '', year: null, words_pending: true, episodes: eps(seen, '') });
      let answer = null;
      const routes = { '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300',
        cast: [{ name: 'Mara Ellison', character: null, profile_url: null, person_id: 9001 }], words_pending: !picked, episodes: eps(seen, '') })))) };
      routes['/api/watch/title/show-300/season/' + n] = () => Promise.resolve(season());
      routes['/api/watch/title/show-300/season/' + n + '?words=1'] = () => new Promise((resolve) => {
        const old = Object.assign(season(), { summary: 'Told.', year: 2017, words_pending: false, episodes: eps(false, 'About it.') });
        answer = () => resolve(old);
      });
      const p = load({ routes });
      p.run();
      p.ctx.MMWatch.marks = { paint: () => {} };
      await openCard(p, 'show-300');
      if (picked) { click(p.doc, p.q('.watch-pill[data-season="1"]')); await settle(); }
      seen = true;
      p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
      await settle();
      answer();
      await settle();
      const look = () => ({ eye: p.q('[data-mark-toggle="episode/21"]').getAttribute('aria-pressed'), about: p.text('.watch-ep__about'),
        season: p.text('.ttl__season-text'), facts: p.text('.ttl__season-facts'),
        left: p.ttl().querySelectorAll('.watch-ep__meta')[1].textContent });
      const came = look();
      // Drawn anew from what is held.
      click(p.doc, p.q('.ttl-cast__item'));
      p.tick();
      p.ctx.history.back();
      return [came, look()];
    }
    return { own: await run(false), picked: await run(true) };
  },

  // What stands for a title while it loads: for an admin the facts card, and how far it was watched as its card says.
  async loadingPlaces() {
    const pending = () => new Promise(() => {});
    const onTrending = (admin, inLibrary, leaving) => {
      const p = load({ admin, adminApi: {}, routes: { '/api/watch/title/tmdb-movie-77': pending } });
      const link = p.doc.querySelector('[data-title-ref="tmdb-movie-77"]');
      const card = link.closest('.watch-card');
      if (inLibrary) link.setAttribute('data-in-library', '');
      if (leaving) card.appendChild(build(p.doc, ['span', { class: 'watch-badge--leaving' }]));
      p.run();
      click(p.doc, link);
      return [p.ttl().querySelectorAll('.ttl-facts__skel').length, p.q('.ttl__actions').children.length,
        p.ttl().querySelectorAll('.ttl__keep-skel').length, p.ttl().querySelectorAll('.ttl__leaving').length];
    };
    const run = (ref, admin, begun) => {
      const p = load({ admin, adminApi: {}, routes: { ['/api/watch/title/' + ref]: pending } });
      const card = p.doc.querySelector('.watch-card');
      if (begun) card.appendChild(build(p.doc, ['progress', { class: 'watch-bar' }]));
      p.run();
      p.ctx.MMWatch.detail.open(ref, { title: 'X', origin: card });
      return [p.ttl().querySelectorAll('.ttl-facts .ttl-facts__skel').length, p.q('.ttl__lead').querySelectorAll('.ttl__progress').length];
    };
    const leavingOf = (ref) => {
      const p = load({ routes: { ['/api/watch/title/' + ref]: pending } });
      const card = p.doc.querySelector('.watch-card');
      card.appendChild(build(p.doc, ['span', { class: 'watch-badge--leaving' }]));
      p.run();
      p.ctx.MMWatch.detail.open(ref, { title: 'X', origin: card });
      const pills = p.q('.ttl__actions').children;
      return [p.text('.ttl__leaving-skel').split(' in ')[0], p.ttl().querySelectorAll('.ttl__keep-skel--show').length, pills[pills.length - 2].style.width];
    };
    return { leaving: { film: leavingOf('movie-120'), show: leavingOf('show-300') }, admin: run('movie-120', true, true), viewer: run('movie-120', false, false), elsewhere: run('tmdb-movie-77', true, true),
      trending: [onTrending(true, true), onTrending(true, false), onTrending(false, true, true), onTrending(true, true, true)] };
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

  // Play on a TV, beside Play: with TVs to choose, with none of mediaman's to ask, and of no such page.
  async playOn() {
    const open = async (title, o) => {
      const ref = title.ref;
      const p = load({ routes: { ['/api/watch/title/' + ref]: title, '/api/tvs': { tvs: [] } }, ...o });
      p.run();
      p.ctx.MMWatch.detail.open(ref, {});
      await settle();
      const b = p.q('[data-ttl-act="play-on"]');
      return { p, says: b && { label: b.getAttribute('aria-label'), hidden: b.hidden, afterPlay: p.q('[data-ttl-act="primary"]').nextElementSibling === b }, wide: p.q('.ttl__actions').classList.contains('has-play-on') };
    };
    const film = { ...LIB_MOVIE, play: { label: 'Play', href: '/watch/play/120', progress_pct: null, minutes_left: null } };
    const shown = await open(film, { playOn: true });
    shown.p.q('[data-ttl-act="play-on"]').focus();
    shown.p.doc.dispatchEvent(new shown.p.ctx.CustomEvent('mm:watch-marked', { detail: { refs: [film.ref] } }));
    shown.p.doc.dispatchEvent({ type: 'mm:title-changed', detail: { ref: film.ref, download_state: null, download_pct: null } });
    await settle();
    const now = shown.p.q('[data-ttl-act="play-on"]');
    return {
      shown: shown.says,
      redrawn: shown.p.doc.activeElement === now,
      wide: [shown.wide, shown.p.q('.ttl__actions').classList.contains('has-play-on'), (await open(film, {})).wide],
      noTvs: (await open(film, { playOn: true, routes: { ['/api/watch/title/' + film.ref]: film } })).says,
      notHeld: (await open(MOVIE_DL, { playOn: true })).says,
      without: (await open(film, {})).says,
    };
  },

  // On a phone an admin's line of when a title leaves takes them to the Keep below; elsewhere it only says so.
  async leavingToKeep() {
    const run = async (opts) => {
      const p = load({ adminApi: { keep: () => {} }, ...opts,
        routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': opts.facts || FACTS } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      const line = p.q('.ttl__leaving');
      const to = line.querySelector('button');
      if (to) click(p.doc, to);
      return { words: line.textContent, link: to ? to.textContent : null, focus: p.doc.activeElement.getAttribute('data-ttl-act') };
    };
    return {
      phone: await run({ admin: true, narrow: true }),
      sideways: await run({ admin: true, short: true }),
      wide: await run({ admin: true }),
      viewer: await run({ narrow: true }),
      waiting: await run({ admin: true, narrow: true, facts: () => new Promise(() => {}) }),
    };
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

  // Undo of a viewer's Keep, pressed in its toast: the page behind puts its own Keep back and focuses
  // it, which the overlay pulls back, before the title is loaded again with its Keep. Then the same
  // where the person has moved on meanwhile.
  async undoKeepFocus() {
    const run = async (movedOn) => {
      let leaving = null;
      const p = load({ routes: { '/api/watch/title/movie-120': () => Promise.resolve(Object.assign({}, LIB_MOVIE, { leaving, trailer_key: 'abcdefghijk' })) } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      const toast = build(p.doc, ['div', { class: 'ui-toast' }, ['button', { class: 'ui-toast__action' }, 'Undo']]);
      p.doc.body.appendChild(toast);
      toast.children[0].listeners.click = [() => {
        toast.remove();
        p.doc.activeElement = p.doc.querySelector('.watch-card .watch-keep');
        p.doc.listeners.focusin.forEach((fn) => fn({ target: p.doc.activeElement }));
      }];
      click(p.doc, toast.children[0]);
      const focused = () => { const n = p.doc.activeElement.closest('[data-ttl-act]'); return n && n.getAttribute('data-ttl-act'); };
      const pressed = focused();
      if (movedOn) p.q('[data-ttl-act="trailer"]').focus();
      leaving = LIB_MOVIE.leaving;
      p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-kept', { detail: { id: '120' } }));
      await settle();
      return [pressed, focused()];
    };
    return { waits: await run(false), movedOn: await run(true) };
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

  // The line of a season that leaves goes and comes back above the pills, pressed with the overlay
  // scrolled less far than the line is tall, then further: how far the pressed pill moved, how far
  // it is scrolled, and whether a title loaded again moves it.
  async leavingLineGoes() {
    const out = {};
    for (const at of [0, 25, 300]) {
      const eps = { ok: true, episodes: [] };
      let keepable = true;
      const leaving = { days: 3, keep_id: '3020', season: 2 };
      const show = () => Object.assign({}, LIB_SHOW, { leaving, leaving_seasons: [leaving], keep_allowed: keepable });
      const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(show()),
        '/api/watch/title/show-300/season/1': eps, '/api/watch/title/show-300/season/2': eps } });
      p.run();
      await openCard(p, 'show-300');
      const root = p.ttl();
      let scroll = at;
      Object.defineProperty(root, 'scrollTop', { get: () => scroll, set: (v) => { scroll = Math.max(0, v); } });
      const gap = () => parseFloat(p.q('.ttl__seasonsec').style.marginTop) || 0;
      const top = () => 400 + (p.q('.ttl__leaving') ? 40 : 0) + gap() - scroll;
      Node.prototype.getBoundingClientRect = function () {
        return { top: this.classList.contains('watch-pill') ? top() : 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 };
      };
      const pick = async (n) => {
        const was = top();
        click(p.doc, p.q('.watch-pill[data-season="' + n + '"]'));
        await settle();
        return { moved: top() - was, line: !!p.q('.ttl__leaving'), scroll };
      };
      out[at] = { gone: await pick(1) };
      keepable = false;
      const was = top();
      p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'show-300', refs: ['show-300'] } });
      await settle();
      out[at].loaded = top() - was;
      out[at].back = await pick(2);
      out[at].room = gap();
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
      const bar = () => p.q('[data-ttl-part="tagline"]').querySelectorAll('.ttl__tagline-skel').length;
      const loading = look();
      const bars = [bar()];
      await settle();
      return { loading, loaded: look(), bars: bars.concat(bar()), leaving: p.text('[data-ttl-part="under"] .ttl__leaving') };
    };
    return { tagline: await run({}), without: await run({ tagline: null }) };
  },

  // Delete on a show's page opens the season list at the size its seasons take.
  async adminSeasonDelete() {
    const calls = [];
    const focused = [];
    const facts = Object.assign({}, FACTS, { actions: { keep_wrapper: { id: '3010', is_tv: true, show_rk: '300', seasons: 5 },
      delete: { kind: 'seasons', show_rk: '300', title: 'Brightwater' } } });
    const note = () => focused.push(p.doc.activeElement.getAttribute('data-ttl-act'));
    const p = load({ admin: true, adminApi: { pickSeasonDelete: (...args) => { calls.push(args); note(); } },
      routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': facts } });
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    click(p.doc, p.q('[data-ttl-act="admin-delete"]'));
    return { calls, focused };
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

  /* Ratings that are still to come: what came with the title keeps its place, and another title's page is let be.
     Their line is held for them, with nothing drawn for a rating the title may not have. */
  async lateRatings() {
    const asked = '/api/watch/title/movie-120/ratings';
    const page = async (ratings) => {
      const answers = [];
      const p = load({ routes: { '/api/watch/title/movie-120': { ...LIB_MOVIE, ratings, ratings_pending: true, cast: [{ name: 'Mara Ellison', character: 'Ines', profile_url: null, person_id: 9001 }] },
        '/api/watch/title/tmdb-movie-77': { ...MOVIE_DL, ratings: { imdb: '5.0', rt: null, tmdb: null, metascore: null }, ratings_pending: false },
        [asked]: (o) => new Promise((resolve, reject) => { answers.push({ resolve, reject, signal: (o && o.signal) || {} }); }) } });
      p.run();
      p.ctx.MMWatch.detail.open('movie-120', {});
      await settle();
      const texts = (nodes) => nodes.map((n) => n.textContent);
      return { p, answers, look: () => ({ shown: texts(p.ttl().querySelectorAll('[data-rating]')), line: !!p.q('[data-ttl-part="ratings"]'),
        held: !!p.q('.ttl__ratings-wait'), waiting: p.q('.ttl__ratings-wait') ? texts(p.q('.ttl__ratings-wait').children) : [] }) };
    };
    const early = { imdb: null, rt: null, tmdb: 7.4, metascore: null };
    const came = await page(early);
    const out = { before: came.look(), asked: came.p.gets.filter((g) => g === asked).length };
    came.answers[0].resolve({ ok: true, ratings: { imdb: '8.3', rt: '91%', tmdb: 7.4, metascore: null } });
    await settle();
    out.after = came.look();
    const none = await page(early);
    none.answers[0].reject(new APIError(502, 'x'));
    await settle();
    out.failed = none.look();
    const empty = await page({ ...early, tmdb: null });
    out.held = empty.look();
    empty.answers[0].resolve({ ok: true, ratings: {} });
    await settle();
    out.empty = empty.look();
    const old = await page({ ...early, imdb: '8.1' });
    old.answers[0].resolve({ ok: true, ratings: { imdb: '8.2', rt: null, tmdb: 7.4, metascore: '70' } });
    await settle();
    out.renewed = old.look().shown;
    const brief = old.p.q('[data-rating="metascore"] .ttl-rating__brief');
    out.brief = [brief.getAttribute('data-brief'), brief.getAttribute('aria-hidden')];
    const moved = await page(early);
    moved.p.ctx.MMWatch.detail.open('tmdb-movie-77', {});
    await settle();
    out.dropped = [moved.answers[0].signal.aborted === true];
    moved.answers[0].resolve({ ok: true, ratings: { imdb: '8.3', rt: null, tmdb: 7.4, metascore: null } });
    await settle();
    out.moved = moved.p.text('.ttl__ratings');
    out.others = moved.p.gets.filter((g) => /ratings$/.test(g));
    const shut = await page(early);
    click(shut.p.doc, shut.p.q('.ttl-cast__item'));
    shut.p.tick();
    out.dropped.push(shut.answers[0].signal.aborted === true);
    shut.answers[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    shut.p.ctx.history.back();
    await settle();
    out.again = [shut.answers.length, shut.look().held];
    return out;
  },

  /* An admin deletes the title from the overlay opened from its card. */
  async deleteFromCard() {
    async function run(films) {
      let announce = null;
      const p = load({
        admin: true, films,
        routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS },
        adminApi: {
          keep: () => Promise.resolve(true),
          confirmDelete: (id) => { announce({ phase: 'done', action: 'delete', id, showRk: '' }); return Promise.resolve(true); },
        },
      });
      announce = (detail) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail }));
      p.run();
      const go = p.ctx.history.go;
      p.ctx.history.go = function (by) { Promise.resolve().then(() => go.call(this, by)); };
      await openCard(p, 'movie-120');
      click(p.doc, p.q('[data-ttl-act="admin-delete"]'));
      await settle();
      const a = p.doc.activeElement;
      return { closed: p.ttl().classList.contains('is-leaving'), focus: a.getAttribute('data-title-ref') || a.id || a.tagName,
        preventScroll: !!(p.doc.focusOpts && p.doc.focusOpts.preventScroll) };
    }
    return {
      next: await run(['119', '120', '121']),
      previous: await run(['119', '120']),
      only: await run(['120']),
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

  // Share pressed again after one that was refused, then one whose link was shown to copy.
  async shareAgain() {
    const run = async (first, opts) => {
      let refused = first;
      const p = load(Object.assign({ admin: true, routes: { '/api/watch/title/rec-5': REC_DL },
        posted: { '/api/recommended/5/share-token': () => (refused ? Promise.reject(refused) : Promise.resolve({ ok: true, share_url: 'https://example.test/download/tok' })) } }, opts));
      p.run();
      p.ctx.MMWatch.detail.open('rec-5', {});
      await settle();
      const press = async () => { p.q('[data-ttl-act="share"]').focus(); click(p.doc, p.q('[data-ttl-act="share"]')); await settle(); await settle(); };
      await press();
      const stale = p.ttl().querySelectorAll('.ttl__note, .ttl-share').length;
      refused = null;
      p.ctx.navigator.clipboard = { writeText: () => Promise.resolve() };
      await press();
      return { stale, label: p.text('[data-ttl-act="share"]'), left: p.ttl().querySelectorAll('.ttl__note, .ttl-share').length,
        focus: p.doc.activeElement === p.q('[data-ttl-act="share"]') };
    };
    return { refused: await run(new APIError(503, 'x')), shown: await run(null, { clipboardFails: true }) };
  },

  // Scrolled until the title's text is about to go under Close's bar, then past it, and back.
  async scrolledPastHead() {
    const p = load({ routes: { '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'show-300');
    const root = p.ttl();
    p.q('.ttl__bar').getBoundingClientRect = () => ({ top: 0, bottom: 56 });
    p.q('.ttl__head').getBoundingClientRect = () => ({ top: 100 - root.scrollTop, bottom: 200 - root.scrollTop });
    return [0, 43, 44, 600, 10].map((at) => {
      root.scrollTop = at;
      (root.listeners.scroll || []).forEach((fn) => fn({ target: root }));
      return root.classList.contains('is-past');
    });
  },

  // The window turned while the sheet stands scrolled: where the title's text is moves with it.
  async turnedPastHead() {
    const p = load({ routes: { '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'show-300');
    let top = 100;
    p.q('.ttl__bar').getBoundingClientRect = () => ({ top: 0, bottom: 56 });
    p.q('.ttl__head').getBoundingClientRect = () => ({ top: top - p.ttl().scrollTop, bottom: 0 });
    p.ttl().scrollTop = 60;
    return [100, 400, 100].map((at) => {
      top = at;
      p.fireWindow('resize');
      return p.ttl().classList.contains('is-past');
    });
  },

  // The window turns while a closed title is on its way out.
  async turnedWhileLeaving() {
    const p = load({ routes: { '/api/watch/title/show-300': LIB_SHOW } });
    p.run();
    await openCard(p, 'show-300');
    p.q('.ttl__bar').getBoundingClientRect = () => ({ top: 0, bottom: 56 });
    p.q('.ttl__head').getBoundingClientRect = () => ({ top: 0, bottom: 0 });
    p.key('Escape');
    p.ttl().scrollTop = 60;
    p.fireWindow('resize');
    return p.ttl().classList.contains('is-past');
  },

  // Opened by a link that names a season: the one on show and its episodes.
  async openOnSeason() {
    const run = async (href, season, seasons) => {
      const p = load({ routes: { '/api/watch/title/show-300': seasons ? { ...LIB_SHOW, seasons } : LIB_SHOW, '/api/watch/title/show-300/season/1': season } });
      const link = p.doc.body.appendChild(build(p.doc, ['a', { 'data-title-ref': 'show-300', 'data-title-at': 'episodes', href }, 'Episodes']));
      p.run();
      click(p.doc, link);
      await settle();
      return { on: p.doc.querySelectorAll('.ttl .watch-pill').filter((n) => n.classList.contains('on')).map((n) => n.textContent),
        first: p.q('.ttl .watch-episodes').textContent.includes('One One'), asked: p.gets.filter((c) => c.includes('/season/')).length };
    };
    const one = { key: '301', episodes: [{ ...LIB_SHOW.episodes[0], title: 'One One' }] };
    return { named: await run('/watch/show/300?season=1', one), own: await run('/watch/show/300?season=2', one),
      none: await run('/watch/show/300', one), gone: await run('/watch/show/300?season=9', one), failed: await run('/watch/show/300?season=1', new APIError(503, 'x')) };
  },

  // What a library title says of itself that a page of its own used to: an episode begun again
  // after it was seen, a watched season's tab, the toggles' names, a film's time left, a title gone.
  async ownPageFacts() {
    const show = Object.assign({}, LIB_SHOW, { mark_kind: 'show', mark_key: '300', watched: false,
      seasons: LIB_SHOW.seasons.map((x) => Object.assign({}, x, { key: '30' + x.number, watched: x.number === 1 })),
      episodes: [{ number: 1, key: '21', title: 'Spring Tide', minutes: 50, thumb_url: '/api/watch/thumb/21', state: 'in_progress', watched: true,
        progress_pct: 40, minutes_left: 30, href: '/watch/play/21?resume=1' }] });
    const p = load({ routes: { '/api/watch/title/show-300': show } });
    p.run();
    await openCard(p, 'show-300');
    const a = p.q('.watch-ep');
    const tab = p.q('.watch-pill[data-season="1"]');
    const shown = { href: a.getAttribute('href'), bar: !!a.querySelector('progress'), name: a.querySelector('.watch-ep__title').textContent,
      state: a.querySelector('.watch-ep__state').textContent, tab: [tab.getAttribute('aria-label'), tab.firstChild.textContent, tab.querySelectorAll('use').length],
      tabsInHero: p.ttl().querySelectorAll('.ttl__hero .watch-pill').length,
      toggles: p.ttl().querySelectorAll('[data-mark-toggle]').map((b) => b.getAttribute('aria-label')) };
    const m = load({ hash: '#title=movie-120', routes: { '/api/watch/title/movie-120': Object.assign({}, LIB_MOVIE, {
      play: { label: 'Resume', href: '/watch/play/120?resume=1', progress_pct: 60, minutes_left: 31 } }) } });
    m.run();
    await settle();
    const g = load({});
    g.run();
    await openCard(g, 'show-300');
    return { show: shown, left: m.text('.ttl__progress'), gone: g.text('.ttl__failed-why') };
  },

  // Opened by an address that names a season, as a show's address of before leads to it.
  async openOnSeasonByAddress() {
    const one = { key: '301', episodes: [{ ...LIB_SHOW.episodes[0], title: 'One One' }] };
    const p = load({ hash: '#title=show-300&season=1', routes: { '/api/watch/title/show-300': LIB_SHOW, '/api/watch/title/show-300/season/1': one } });
    p.run();
    await settle();
    return { open: !p.ttl().hidden, title: p.gets[0],
      on: p.doc.querySelectorAll('.ttl .watch-pill').filter((n) => n.classList.contains('on')).map((n) => n.textContent),
      first: p.q('.ttl .watch-episodes').textContent.includes('One One') };
  },

  // The season a link names is slow: what shows while it is, what it changes when it comes, and
  // what Keep is of; then one that cannot be had.
  async openOnSlowSeason() {
    const look = (p) => ({ busy: p.ttl().hasAttribute('aria-busy'), on: p.ttl().querySelectorAll('.watch-pill').filter((n) => n.getAttribute('aria-pressed') === 'true').map((n) => n.textContent),
      episode: p.text('.watch-ep__title'), keep: p.q('.watch-btn--keep') && p.q('.watch-btn--keep').getAttribute('aria-label') });
    const kept = [];
    const facts = { ...FACTS, actions: { keep_wrapper: { id: '3020', is_tv: true, show_rk: '300', has_rk: true, show_title: 'Brightwater' }, delete: null } };
    const open = async (over, meanwhile, how, admin) => {
      let answer = null;
      const p = load({ ...admin, routes: { '/api/watch/admin/title/show-300': facts, '/api/watch/title/show-300': { ...LIB_SHOW, ...over },
        '/api/watch/title/show-300/season/1': () => new Promise((resolve, reject) => { answer = { resolve, reject }; }) } });
      const link = p.doc.body.appendChild(build(p.doc, ['a', { 'data-title-ref': 'show-300', href: '/watch/show/300?season=1' }, 'Season 1']));
      p.run();
      click(p.doc, link);
      await settle();
      const held = look(p);
      const bound = Math.max(...p.timers.filter((t) => t.fn).map((t) => t.ms));
      p.tick();
      await settle();
      const arrived = look(p);
      if (meanwhile) meanwhile(p);
      const focus = p.doc.activeElement;
      if (how === 'failed') answer.reject(new APIError(503, 'x'));
      else answer.resolve({ key: '301', watched: false, episodes: [{ ...LIB_SHOW.episodes[0], title: 'One One' }] });
      await settle();
      const out = { held, bound, arrived, came: look(p), focusKept: p.doc.activeElement === focus };
      if (admin) {
        const trigger = p.q('[data-ttl-act="admin-keep"]');
        out.menuOpen = trigger.getAttribute('aria-expanded') === 'true';
        if (!out.menuOpen) click(p.doc, trigger);
        click(p.doc, p.q('.ttl-facts__menu').querySelector('[role="menuitem"]'));
        out.kept = kept.pop();
      }
      return out;
    };
    const both = { leaving_seasons: [{ days: 3, keep_id: '3010', season: 1 }, { days: 9, keep_id: '3020', season: 2 }] };
    const none = { leaving: null, leaving_seasons: [] };
    const own = { leaving_seasons: [{ days: 9, keep_id: '3020', season: 2 }, { days: 3, keep_id: '3010', season: 1 }] };
    const admin = { admin: true, adminApi: { keep: (w) => kept.push(w.season_id), removeKeep: () => {} } };
    return { quiet: await open(none), appears: await open({}), otherKeep: await open(both),
      pressed: await open(none, (p) => click(p.doc, p.q('.ttl__overview') || p.q('.ttl__body'))),
      reading: await open(none, (p) => p.q('.watch-ep').focus()), facts: await open(none, (p) => p.q('.ttl__season-about').focus()),
      failed: await open(both, null, 'failed'), admin: await open({}, null, null, admin),
      adminMenu: await open({}, (p) => click(p.doc, p.q('[data-ttl-act="admin-keep"]')), null, admin),
      keepMenu: await open(own, (p) => click(p.doc, p.q('.watch-btn--keep'))) };
  },

  // Opened by a link to its episodes: where it is scrolled and what has the focus once it has come;
  // then where the person scrolled it while it loaded, and a film's, which has none.
  async openAtEpisodes() {
    const run = async (ref, data, scrolled) => {
      let answer = null;
      const p = load({ routes: { ['/api/watch/title/' + ref]: () => new Promise((resolve) => { answer = () => resolve(JSON.parse(JSON.stringify(data))); }) } });
      const link = p.doc.body.appendChild(build(p.doc, ['a', { 'data-title-ref': ref, 'data-title': 'It', 'data-title-at': 'episodes', href: '/watch/show/300' }, 'Episodes']));
      p.run();
      Object.defineProperty(Node.prototype, 'offsetTop', { configurable: true, get() { return this.classList.contains('ttl__seasonsec') ? 900 : this.classList.contains('ttl__sheet') ? 40 : 0; } });
      Object.defineProperty(p.q('.ttl__bar'), 'offsetHeight', { value: 56 });
      click(p.doc, link);
      p.ttl().scrollTop = scrolled;
      answer();
      await settle();
      const a = p.doc.activeElement;
      return { to: p.ttl().scrolledTo ? p.ttl().scrolledTo.top : null, focus: a.textContent, hash: p.loc.hash };
    };
    return { show: await run('show-300', LIB_SHOW, 0), scrolled: await run('show-300', LIB_SHOW, 200), film: await run('movie-120', LIB_MOVIE, 0) };
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
    const behind = () => { for (let n = outside; n; n = n.parentNode) if (n.inert) return true; return false; };
    const held = behind();
    (p.doc.listeners.focusin || []).forEach((fn) => fn({ target: outside }));
    const pulledBack = p.doc.activeElement === p.q('#ttl-title');
    p.key('Escape');
    await settle();
    return { held, freed: !behind(), wrappedToFirst, prevented: [fwd.defaultPrevented, back.defaultPrevented], wrappedToLast, pulledBack };
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

  // Two seasons of a show leave: what Details offers to keep, opened from each one's card, from
  // neither, from a card of one that no longer leaves, and once a season is picked.
  async leavingSeasons() {
    const leaving = [{ days: 2, keep_id: '3030', season: 3 }, { days: 9, keep_id: '3020', season: 2 }];
    const seasons = [1, 2, 3].map((n) => ({ number: n, name: 'Season ' + n, episode_count: 2, in_library: true, selected: n === 1 }));
    const show = Object.assign({}, LIB_SHOW, { leaving: leaving[0], leaving_seasons: leaving, seasons });
    const eps = (n) => ({ ok: true, episodes: [{ number: 1, title: 'E' + n, minutes: 45, thumb_url: null, state: 'none', href: '/watch/play/1' + n }] });
    const open = async (keepId) => {
      const p = load({ routes: { '/api/watch/title/show-300': show, '/api/watch/title/show-300/season/2': eps(2), '/api/watch/title/show-300/season/1': eps(1) } });
      p.run();
      p.doc.querySelectorAll('.watch-card')[1].setAttribute('data-keep-id', keepId);
      await openCard(p, 'show-300');
      return p;
    };
    const look = (p) => {
      const b = p.q('.watch-btn--keep');
      return [b && b.getAttribute('data-watch-keep'), b && b.getAttribute('aria-label'), p.text('.ttl__leaving')];
    };
    const out = { second: look(await open('3020')), third: look(await open('3030')), plain: look(await open('')), gone: look(await open('3010')) };
    const p = await open('3030');
    const pick = async (n) => {
      p.q('.watch-pill[data-season="' + n + '"]').focus();
      click(p.doc, p.q('.watch-pill[data-season="' + n + '"]'));
      await settle();
    };
    await pick(2);
    out.picked = look(p);
    out.focus = p.doc.activeElement.getAttribute('data-ttl-act');
    await pick(1);
    out.pickedOther = look(p);
    const film = load({ routes: { '/api/watch/title/movie-120': LIB_MOVIE } });
    film.run();
    film.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    out.film = look(film);
    const past = load({ routes: { '/api/watch/title/movie-120': { ...LIB_MOVIE, leaving: { ...LIB_MOVIE.leaving, days: -3 } } } });
    past.run();
    past.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    out.past = look(past)[2];
    return out;
  },

  // The admin facts of a film, and of a show's season, past the date.
  async factsPastTheDate() {
    const of = async (ref, title, over) => {
      const deletion = { keep_id: title.leaving.keep_id, execute_at: '2026-11-20T10:00:00+00:00', days: -3, season: null, ...over };
      const p = load({ admin: true, routes: { ['/api/watch/title/' + ref]: { ...title, leaving: { ...title.leaving, days: -3 } },
        ['/api/watch/admin/title/' + ref]: { ...FACTS, deletion, deletions: [deletion] } } });
      p.run();
      p.ctx.MMWatch.detail.open(ref, {});
      await settle();
      const rows = p.ttl().querySelectorAll('.ttl-facts__list dt, .ttl-facts__list dd').map((n) => n.textContent);
      return rows[rows.indexOf('Deletion') + 1];
    };
    return { film: await of('movie-120', LIB_MOVIE, {}), season: await of('show-300', LIB_SHOW, { season: 1 }) };
  },

  // What an admin sees and keeps of a show whose Seasons 3 and 2 leave, opened from a card.
  async leavingSeasonsAdmin() {
    const leaving = [{ days: 2, keep_id: '3020', season: 2 }, { days: 9, keep_id: '3030', season: 3 }];
    const deletions = leaving.map((l) => Object.assign({ execute_at: '2026-12-0' + l.days + 'T10:00:00+00:00' }, l));
    const seasons = [1, 2, 3].map((n) => ({ number: n, name: 'Season ' + n, episode_count: 2, in_library: true, selected: n === 1 }));
    const show = Object.assign({}, LIB_SHOW, { leaving: leaving[0], leaving_seasons: leaving, seasons });
    const facts = (keep) => Object.assign({}, FACTS, { deletion: deletions[0], deletions, keep,
      actions: { keep_wrapper: { id: '3010', is_tv: true, show_rk: '300', has_rk: true, show_title: 'Brightwater' }, delete: null } });
    const eps = { ok: true, episodes: [] };
    const calls = [];
    const open = async (keepId, keep) => {
      const p = load({ admin: true, adminApi: { keep: (w, d) => calls.push(['keep', w.season_id, d]), removeKeep: (w) => calls.push(['removeKeep', w.id]) }, routes: {
        '/api/watch/title/show-300': show, '/api/watch/admin/title/show-300': facts(keep),
        '/api/watch/title/show-300/season/1': eps, '/api/watch/title/show-300/season/2': eps } });
      p.run();
      p.doc.querySelectorAll('.watch-card')[1].setAttribute('data-keep-id', keepId);
      await openCard(p, 'show-300');
      return p;
    };
    const row = (p) => {
      const rows = p.ttl().querySelectorAll('.ttl-facts__list dt, .ttl-facts__list dd').map((n) => n.textContent);
      return rows[rows.indexOf('Deletion') + 1];
    };
    const choose = (p, n) => { click(p.doc, p.q('[data-ttl-act="admin-keep"]')); click(p.doc, p.ttl().querySelectorAll('.ttl-facts__menu [role="menuitem"]')[n]); };
    const pick = async (p, n) => { click(p.doc, p.q('.watch-pill[data-season="' + n + '"]')); await settle(); };
    const p = await open('3030', { state: 'none', label: '1 of 3 seasons kept' });
    const out = { rows: [row(p)] };
    choose(p, 1);
    await pick(p, 2);
    out.rows.push(row(p));
    choose(p, 1);
    const trigger = p.q('[data-ttl-act="admin-keep"]');
    click(p.doc, trigger);
    const first = pick(p, 1);
    click(p.doc, p.q('[data-ttl-act="admin-keep"]'));
    p.ttl().querySelectorAll('.ttl-facts__menu [role="menuitem"]')[0].focus();
    await first;
    out.menuAfter = { open: p.ttl().querySelectorAll('.ttl-facts__menu').some((m) => !m.hidden), focus: p.doc.activeElement.getAttribute('data-ttl-act'),
      live: p.ttl().contains(p.doc.activeElement) };
    out.rows.push(row(p));
    choose(p, 1);
    out.calls = calls.splice(0);
    const kept = await open('3030', { state: 'kept', label: 'Kept forever' });
    choose(kept, 4);
    out.removed = calls;
    return out;
  },

  // The Keep menu open on one season while another, asked for before, comes in.
  async keepMenuAcrossSeasons() {
    const leaving = [{ days: 2, keep_id: '3030', season: 3 }, { days: 9, keep_id: '3020', season: 2 }];
    const seasons = [1, 2, 3].map((n) => ({ number: n, name: 'Season ' + n, episode_count: 2, in_library: true, selected: n === 3 }));
    const show = Object.assign({}, LIB_SHOW, { leaving: leaving[0], leaving_seasons: leaving, seasons });
    const out = {};
    for (const n of [2, 1]) {
      let answer = null;
      const p = load({ routes: { '/api/watch/title/show-300': show,
        ['/api/watch/title/show-300/season/' + n]: () => new Promise((resolve) => { answer = () => resolve({ ok: true, episodes: [] }); }) } });
      p.run();
      await openCard(p, 'show-300');
      click(p.doc, p.q('.watch-pill[data-season="' + n + '"]'));
      click(p.doc, p.q('.watch-btn--keep'));
      const menu = p.doc.getElementById('watch-keep-menu');
      const before = menu.classList.contains('is-open');
      answer();
      await settle();
      click(p.doc, menu.children[0]);
      await settle();
      out[n] = { before, after: menu.classList.contains('is-open'), focus: p.doc.activeElement.getAttribute('data-ttl-act'), live: p.ttl().contains(p.doc.activeElement),
        label: p.doc.activeElement.getAttribute('aria-label'), posts: p.posts };
    }
    return out;
  },

  // A season's Keep undone from a toast raised before the title was opened, and one of another title's.
  async keptElsewhere() {
    let show = LIB_SHOW;
    const p = load({ routes: { '/api/watch/title/show-300': () => Promise.resolve(JSON.parse(JSON.stringify(show))) } });
    p.run();
    show = Object.assign({}, LIB_SHOW, { leaving: null, leaving_seasons: [] });
    await openCard(p, 'show-300');
    const look = () => [p.q('.watch-btn--keep') && p.q('.watch-btn--keep').getAttribute('data-watch-keep'), p.text('.ttl__leaving') !== null];
    const tell = async (id) => {
      p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-kept', { detail: { id } }));
      await settle();
      return look();
    };
    const out = { before: look() };
    show = LIB_SHOW;
    out.undone = await tell('3010');
    const body = p.q('.ttl__lead');
    out.other = await tell('999');
    out.drawnAgain = p.q('.ttl__lead') !== body;
    click(p.doc, p.q('.ttl__close'));
    p.tick();
    const gets = p.gets.length;
    await tell('3010');
    out.askedWhenClosed = p.gets.length - gets;
    return out;
  },

  // The director leads to the page of their titles, and Back finds the focus on them; one TMDB gives no id for is words.
  async directorLink() {
    const p = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { director: 'Ada Rimmington', director_id: 9008 }) } });
    p.run();
    await openCard(p, 'tmdb-movie-77');
    const link = p.q('.ttl__credit a');
    const out = { text: p.text('.ttl__credit'), href: link && link.getAttribute('href') };
    const e = { target: link, button: 0, ctrlKey: true, preventDefault() { this.defaultPrevented = true; } };
    link.listeners.click.forEach((fn) => fn(e));
    out.modified = !!e.defaultPrevented;
    click(p.doc, link);
    out.explored = p.explored.slice();
    p.tick();
    p.ctx.history.back();
    out.focus = p.doc.activeElement.getAttribute('data-ttl-act');
    const q = load({ routes: { '/api/watch/title/tmdb-movie-77': Object.assign({}, MOVIE_DL, { director: 'Ada Rimmington', director_id: null }) } });
    q.run();
    await openCard(q, 'tmdb-movie-77');
    out.plain = [q.text('.ttl__credit'), !!q.q('.ttl__credit a')];
    return out;
  },
};

// The set of thumbs: the request held, then its answer; and what the page is when the answer comes.
scenarios.thumbs = async () => {
  const URL = '/api/watch/ratings?refs=';
  const asked = [];
  const p = load({ thumbs: true, routes: {
    '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/title/show-300': LIB_SHOW,
    [URL + 'movie-120']: () => new Promise((resolve) => asked.push(resolve)),
    [URL + 'show-300']: { ok: true, ratings: { 'show-300': { level: null, rateable: true } } },
    '/api/watch/title/tmdb-movie-77': MOVIE_DL,
  } });
  p.run();
  const open = async (ref) => { p.ctx.MMWatch.detail.open(ref, { push: true }); await settle(); };
  const due = async (ms) => { p.timers.filter((t) => t.fn && t.ms === ms).forEach((t) => { const fn = t.fn; t.fn = null; fn(); }); await settle(); };
  const set = () => p.q('.ttl__main .rate');
  const on = () => set().querySelectorAll('button').map((b) => b.getAttribute('aria-pressed'));
  await open('movie-120');
  const out = { skel: !!p.q('.ttl__rate-skel') };
  await due(0);
  out.waits = !!p.q('.ttl__overview');
  asked[0]({ ok: true, ratings: { 'movie-120': { level: 1, rateable: true } } });
  await settle();
  out.order = p.q('.ttl__main').children.map((n) => n.className.split(' ')[0]);
  out.rated = on();
  const b = set().querySelectorAll('button')[3];
  b.focus();
  click(p.doc, b);
  out.pressed = [on(), p.posts.slice()];
  // Drawn again, as when an episode is marked: the same thumb keeps the focus.
  p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'movie-120', refs: ['movie-120'] } });
  await settle();
  out.redrawn = [set().querySelectorAll('button')[3] !== b, p.doc.activeElement === set().querySelectorAll('button')[3], on()];
  const left = set().querySelectorAll('button');
  await open('show-300');
  await due(0);
  out.next = on();
  click(p.doc, left[0]); // a set no longer on show is not painted
  out.old = left[0].getAttribute('aria-pressed');
  p.ctx.history.back();
  await settle();
  out.back = on();
  const last = set().querySelectorAll('button');
  p.key('Escape');
  p.tick();
  p.ctx.MM.ratings.set('movie-120', 1);
  out.closed = last.map((n) => n.getAttribute('aria-pressed'));
  return out;
};

// The focus goes from a title's page to the bar of what plays on a TV, then behind the page.
scenarios.tvBar = async () => {
  const p = load({ routes: { '/api/watch/title/movie-120': LIB_MOVIE } });
  p.doc.body.append(build(p.doc, ['section', { id: 'tv-mini' }, ['button', { id: 'tv-pause' }, 'Pause']]));
  p.run();
  p.ctx.MMWatch.detail.open('movie-120', { push: true });
  await settle();
  const go = (n) => { n.focus(); (p.doc.listeners.focusin || []).forEach((fn) => fn({ target: n })); };
  go(p.doc.getElementById('tv-pause'));
  const out = { bar: p.doc.activeElement.id };
  go(p.doc.querySelector('[data-title-ref]'));
  out.behind = p.doc.activeElement.id;
  go(p.doc.getElementById('tv-pause'));
  p.key('Escape');
  out.closed = p.ttl().classList.contains('is-leaving');
  return out;
};

// What finds more like a title comes to be known after the title opened; then one that is no one's, and one by its TMDB id.
scenarios.like = async () => {
  let named = LIB_MOVIE.title;
  const p = load({ routes: { '/api/watch/title/movie-120': () => Promise.resolve(Object.assign({}, LIB_MOVIE, { title: named, poster_url: '/api/watch/poster/120' })), '/api/watch/title/rec-3': Object.assign({}, MOVIE_DL, { ref: 'rec-3' }),
    '/api/watch/title/tmdb-movie-77': MOVIE_DL,
    '/api/watch/title/rec-4': Object.assign({}, MOVIE_DL, { ref: 'rec-4', tmdb_ref: 'tmdb-movie-78', download: null }) } });
  p.run();
  const b = () => p.q('[data-ttl-act="like"]');
  const acts = () => p.q('[data-ttl-part="actions"]').children.map((n) => n.getAttribute('data-ttl-act')).slice(0, 2);
  const open = async (ref) => { p.ctx.MMWatch.detail.open(ref, { push: true }); await settle(); };
  await open('movie-120');
  const asked = [];
  const out = { before: !!b() };
  let on = true;
  p.ctx.MMWatch.search = { canLike: (ref) => on && /^(movie|show|tmdb-movie|tmdb-tv)-/.test(ref), like: (...args) => asked.push(args) };
  p.q('[data-ttl-act="primary"]').focus();
  p.doc.listeners['mm:search-assist'].forEach((fn) => fn({}));
  out.acts = acts();
  out.focus = p.doc.activeElement.getAttribute('data-ttl-act');
  // Renamed since it opened: the title it was opened as is not the one asked about.
  named = 'Glass Harbour: Restored';
  p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'movie-120', refs: ['movie-120'] } });
  await settle();
  click(p.doc, b());
  await open('rec-3');
  if (b()) click(p.doc, b());
  await open('tmdb-movie-77');
  click(p.doc, b());
  out.asked = asked.slice();
  // A pick with nothing to download, where Radarr is not set up, is still known by its TMDB id.
  await open('rec-4');
  if (b()) click(p.doc, b());
  out.undownloadable = asked.slice(out.asked.length).map((a) => a[0]);
  on = false;
  p.doc.listeners['mm:search-assist'].forEach((fn) => fn({}));
  out.off = !!b();
  return out;
};

// What holds the thumbs' place while a title loads, of the library and not, for an admin and not.
scenarios.thumbsWait = async () => {
  const look = (admin, ref) => {
    const p = load({ admin, thumbs: true, routes: {} });
    p.run();
    p.ctx.MMWatch.detail.open(ref, {});
    return { columns: p.ttl().classList.contains('ttl--admin'), buttons: p.q('.ttl__actions').children.length };
  };
  return { admin: look(true, 'movie-120'), viewer: look(false, 'movie-120'), tmdb: look(true, 'tmdb-movie-77') };
};

// A title's page asks for its rating each time it loads, so one changed in another tab is the one shown.
scenarios.ratedElsewhere = async () => {
  let level = 1;
  const p = load({ thumbs: true, routes: { '/api/watch/title/movie-120': LIB_MOVIE,
    '/api/watch/ratings?refs=movie-120': () => Promise.resolve({ ok: true, ratings: { 'movie-120': { level, rateable: true } } }) } });
  p.run();
  const due = async () => { p.timers.filter((t) => t.fn && t.ms === 0).forEach((t) => { const fn = t.fn; t.fn = null; fn(); }); await settle(); };
  const asked = () => p.gets.filter((g) => /ratings\?refs=/.test(g)).length;
  const open = async () => { p.ctx.MMWatch.detail.open('movie-120', { push: true }); await settle(); await due(); };
  await open();
  const first = [asked(), p.ctx.MM.ratings.get('movie-120')];
  p.key('Escape');
  p.tick();
  level = -2;
  await open();
  return { first, again: [asked(), p.ctx.MM.ratings.get('movie-120')] };
};

// *rateable* unknown: the answer comes after the wait is over.
scenarios.thumbsNone = async () => {
  const none = { ok: true, ratings: { 'movie-120': { level: null, rateable: false } } };
  const asked = [];
  const p = load({ thumbs: true, routes: { '/api/watch/title/movie-120': LIB_MOVIE,
    '/api/watch/ratings?refs=movie-120': () => new Promise((resolve) => asked.push(resolve)) } });
  p.run();
  const due = async (ms) => { p.timers.filter((t) => t.fn && t.ms === ms).forEach((t) => { const fn = t.fn; t.fn = null; fn(); }); await settle(); };
  const open = async () => { p.ctx.MMWatch.detail.open('movie-120', { push: true }); await settle(); };
  await open();
  await due(0);
  const out = { before: !!p.q('.ttl__overview') };
  await due(400);
  out.late = [!!p.q('.ttl__overview'), !!p.q('.ttl__main .rate')];
  asked[0](none);
  await settle();
  out.told = p.q('.ttl__main .rate').hidden;
  p.key('Escape');
  p.tick();
  await open();
  await due(400);
  out.again = [!!p.q('.ttl__overview'), !!p.q('.ttl__main .rate')];
  return out;
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
