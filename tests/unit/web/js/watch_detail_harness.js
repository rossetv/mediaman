// Runs watch/core.js, watch/keep.js and watch/detail.js against the fake DOM, with the real
// _watch_detail.html as the overlay, and prints one scenario's outcome as JSON.
// Usage: node watch_detail_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
Node.prototype.select = function () { this.selected = true; };
Node.prototype.focus = function (o) { this.ownerDocument.activeElement = this; this.ownerDocument.focusOpts = o; };
Object.defineProperty(Node.prototype, 'id', {
  get() { return this.attrs.id || ''; },
  set(v) { this.attrs.id = String(v); },
});

/* The template's markup as fake-DOM nodes: tags, attributes and text only. */
function parseTemplate(doc, html) {
  html = html.replace(/\{#[\s\S]*?#\}/g, '');
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
  ref: 'rec-5', reason: 'You watched two slow mysteries.',
  download: { state: null, service: 'radarr', tmdb_id: 78, request: { url: '/api/recommended/5/download' } },
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
  const loc = { pathname: '/watch', search: '', hash: opts.hash || '' };
  const winListeners = {};

  function setUrl(url) {
    const i = url.indexOf('#');
    loc.hash = i < 0 ? '' : url.slice(i);
  }

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
    matchMedia: (q) => ({ matches: /min-width/.test(q) }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900,
    innerWidth: 1280,
    location: loc,
    history: {
      state: null,
      pushState(state, _t, url) { history.push(['push', url]); this.state = state; setUrl(url); },
      replaceState(state, _t, url) { history.push(['replace', url]); this.state = state; setUrl(url); },
      back() { history.push(['back']); this.state = null; loc.hash = ''; (winListeners.popstate || []).forEach((fn) => fn({})); },
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
  };
  ctx.window = ctx;
  if (opts.adminApi) {
    ctx.MMWatch = { admin: opts.adminApi };
  }
  vm.createContext(ctx);
  const run = () => ['watch/core.js', 'watch/keep.js', 'watch/detail.js']
    .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));
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
  return { ctx, doc, toasts, timers, gets, posts, events, history, loc, routes, clock, run, ttl, q, text, tick, key, popstate };
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

  async directLink() {
    const p = load({ hash: '#title=movie-120', routes: { '/api/watch/title/movie-120': LIB_MOVIE } });
    p.run();
    await settle();
    const opened = { hidden: p.ttl().hidden, gets: p.gets.slice() };
    p.key('Escape');
    return { opened, history: p.history.slice(), hash: p.loc.hash };
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
    const playing = {
      trailer: p.ttl().classList.contains('is-trailer'),
      button: p.text('[data-ttl-act="trailer"]'),
      src: frame.getAttribute('src'), sandbox: frame.getAttribute('sandbox'),
      allow: frame.getAttribute('allow'), referrer: frame.getAttribute('referrerpolicy'),
    };
    p.key('Escape');
    const afterFirst = { trailer: p.ttl().classList.contains('is-trailer'), frame: !!p.q('.ttl__player iframe'),
      open: !p.ttl().classList.contains('is-leaving'), button: p.text('[data-ttl-act="trailer"]') };
    p.key('Escape');
    return { playing, afterFirst, closedSecond: p.ttl().classList.contains('is-leaving') };
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

  async viewerNoAdmin() {
    const p = load({ routes: { '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS } });
    p.run();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    return { gets: p.gets, facts: !!p.q('.ttl-facts'), keep: !!p.q('[data-watch-keep="120"]') };
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
      const back = p.ctx.history.back;
      p.ctx.history.back = function () { Promise.resolve().then(() => back.call(this)); };
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
        icon: n.parentNode.children[0].className, inButton: n.parentNode.parentNode === b, groups: b.children.length };
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
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
