// Runs static/js/watch/search.js against the fake DOM and prints the outcome of one scenario as JSON.
// Usage: node watch_search_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/watch');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const LIB = { ref: 'movie-1', title: 'Orchard Keepers', href: '/watch/movie/1', subtitle: 'Movie', keep_id: '7', leaving_days: 3 };
const TM = { ref: 'tmdb-movie-9', kind: 'movie', title: 'Orchard Road', year: 2020, href: null, download_state: 'queued' };

const GENRES = [
  { id: 'action', name: 'Action', aliases: ['martial arts'], types: ['movie', 'tv'], poster_urls: ['/api/watch/img/tmdb/a.jpg'] },
  { id: 'science-fiction', name: 'Science Fiction', aliases: ['sci-fi', 'scifi'], types: ['movie', 'tv'], poster_urls: [] },
  { id: 'horror', name: 'Horror', aliases: ['scary'], types: ['movie'], poster_urls: [] },
  { id: 'news', name: 'News', aliases: [], types: ['tv'], poster_urls: [] },
];
const DECADES = ['2020s', '2010s', '1990s'];

Node.prototype.scrollIntoView = function () {};
Node.prototype.click = function () { click(this.ownerDocument, this); };

/* A page with the Watch search shell; every request waits until the scenario answers it. The categories are there at once, unless the scenario answers them too (null). */
function load(data, opened = '', at = null, categories = { tmdb_configured: true, genres: GENRES, decades: DECADES }, { still = true, every = () => 0, stored = {} } = {}) {
  const doc = makeDocument();
  const timers = [];
  const calls = [];
  const events = [];
  const on = {};
  const urls = [];
  const grids = [];
  const tab = { entries: [{ url: at || '/watch' + (opened ? '?q=' + opened : ''), state: null }], index: 0 };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  const fire = (type, e) => (on[type] || []).slice().forEach((fn) => fn(e || {}));
  const go = (by) => {
    if (!tab.entries[tab.index + by]) return;
    tab.index += by;
    fire('popstate');
  };
  doc.dispatchEvent = (e) => events.push({ type: e.type, q: e.detail.q });
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Error, Infinity, URLSearchParams, parseFloat,
    setTimeout: (fn, ms) => { if (!ms) { setImmediate(fn); return -1; } timers.push({ fn, ms, on: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].on = false; },
    setInterval: (fn) => every(fn),
    clearInterval: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() { this.aborted = true; } },
    Image: class {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1' }),
    matchMedia: () => ({ matches: still }),
    innerHeight: 800,
    categories,
    scrollY: 0,
    scrollTo: (o) => { ctx.scrollY = o.top; },
    sessionStorage: { getItem: () => null, setItem() {} },
    localStorage: { getItem: (k) => (k in stored ? stored[k] : null), setItem: (k, v) => { stored[k] = v; }, removeItem: (k) => { delete stored[k]; } },
    location: {
      get pathname() { return here().pathname; },
      get search() { return here().search; },
      get hash() { return here().hash; },
    },
    history: {
      get state() { return tab.entries[tab.index].state; },
      pushState(state, _t, url) {
        tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state });
        tab.index++;
      },
      replaceState(state, _t, url = tab.entries[tab.index].url) { tab.entries[tab.index] = { url, state }; urls.push(url); },
      go: (by) => setImmediate(() => go(by)),
    },
    addEventListener: (t, fn) => { (on[t] = on[t] || []).push(fn); },
    removeEventListener() {},
    MM: {
      api: {
        get: (url) => (url === '/api/watch/explore' && ctx.categories ? Promise.resolve(ctx.categories)
          : new Promise((resolve, reject) => { calls.push({ url, resolve, reject }); })),
        messageOf: (_e, fallback) => fallback,
      },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' },
    ['div', { class: 'watch-search' },
      ['span', { id: 'watch-search-token', hidden: '' }],
      ['span', { class: 'watch-search__field' }, ['span', { id: 'watch-search-hint' }, 'Search films and shows'],
        ['input', { id: 'watch-search', class: 'watch-search__input', value: opened, role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'false', 'aria-controls': 'watch-sheet' }]],
      ['button', { id: 'watch-search-clear', 'aria-label': 'Clear search' }]],
    ['nav', { id: 'watch-viewtoggle' }, ['a', { href: '/watch', 'data-view': 'rows' }, 'Browse']],
    ['div', { id: 'watch-sheet' }],
    ['span', { id: 'watch-search-status' }],
    ['div', { id: 'watch-veil' }],
    ['div', { id: 'watch-browse' }],
    ['section', { id: 'watch-results' }],
    ['script', { id: 'watch-page-data' }, JSON.stringify(data)]]));
  const $ = (id) => doc.getElementById(id);
  // The server opens on the results when the address has a search; otherwise the rows are on view.
  $('watch-results').hidden = !opened && !/genre=|person=/.test(at || '');
  $('watch-browse').hidden = !$('watch-results').hidden;
  $('watch-search-token').hidden = true;
  const input = $('watch-search');
  input.value = opened;
  ['focus', 'blur'].forEach((type) => {
    const plain = Node.prototype[type];
    input[type] = () => {
      const was = doc.activeElement === input;
      plain.call(input);
      if (was !== (type === 'focus')) (input.listeners[type] || []).forEach((fn) => fn({}));
    };
  });
  input.select = () => {};
  ['../core/dom.js', 'core.js', 'address.js', 'keep.js', 'search.js', 'explore.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  // Stands in for the grid: it keeps what it was given, and asks for its first page as the real one does.
  ctx.MMWatch.grid = {
    list(host, opts) {
      const g = { opts, set: [], stopped: false, busy: 0, titles: [], tail: doc.createElement('div') };
      const cards = host.appendChild(doc.createElement('div'));
      const add = (got) => {
        g.titles = got.cards.map((c) => c.textContent);
        g.next = got.next;
        g.tail.textContent = cards.textContent = '';
        got.cards.forEach((c) => cards.appendChild(c));
      };
      // As the grid's loader does: it shows a failure and its Try again, but one for being asked too much only a placeholder.
      const load = () => opts.fetchPage(1).then(add, (err) => {
        g.failed = err.status || true;
        if (err.status === 429 || g.tail.children.length) return;
        const again = doc.createElement('button');
        again.className = 'grid-fail__retry';
        again.addEventListener('click', load);
        g.tail.appendChild(build(doc, ['div', { class: 'grid-fail' }, ['span', {}, 'Couldn’t load the titles.']])).appendChild(again);
      });
      host.appendChild(g.tail);
      load();
      grids.push(g);
      return {
        tail: g.tail,
        set: (content, how) => {
          g.set.push([content.cards.length, content.next, how.move]);
          if (content.cards.length) { add(content); return; }
          g.tail.textContent = '';
          opts = { ...opts, fetchPage: content.fetchPage };
          load();
        },
        busy: () => { g.busy++; },
        stop: () => { g.stopped = true; },
      };
    },
  };
  ctx.MMWatch.search.init();
  ctx.MMWatch.address.start();
  return {
    ctx, doc, input, calls, events, urls, timers, tab, grids, $, fire,
    results: $('watch-results'), browse: $('watch-browse'), status: $('watch-search-status'), sheet: $('watch-sheet'),
    url: () => tab.entries[tab.index].url,
    shown: () => [$('watch-browse'), $('watch-results')].filter((n) => !n.hidden).map((n) => n.id.slice(6)).join(),
    type(value) {
      input.value = value;
      input.listeners.input.forEach((fn) => fn({}));
    },
    // Type and let the debounce fire.
    async search(value) {
      this.type(value);
      this.wait();
      await settle();
    },
    // What is still waiting for its time.
    wait() { timers.filter((t) => t.on).forEach((t) => { t.on = false; t.fn(); }); },
    async back() { go(-1); await settle(); },
    key(key, more) {
      const e = { key, target: input, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...more };
      input.listeners.keydown.forEach((fn) => fn(e));
      return e.defaultPrevented;
    },
    press: (node) => doc.querySelector('.watch-search').listeners.mousedown.forEach((fn) => fn({ target: node || input, preventDefault() {} })),
    async answer(urlPart, payload) {
      calls.filter((c) => c.url.includes(urlPart) && !c.done).forEach((c) => { c.done = true; c.resolve(payload); });
      await settle();
    },
    titles(sel) { return doc.querySelectorAll(sel).map((c) => c.querySelector('.watch-card__title').textContent); },
    heads: () => doc.querySelectorAll('.results-group__title').map((h) => h.textContent),
    suggested: () => doc.querySelectorAll('.watch-suggestion').map((n) => n.querySelector('.watch-suggestion__text').children.map((c) => c.textContent).join(' | ')),
    line: () => { const n = doc.querySelector('.watch-results__line'); return n && n.textContent; },
  };
}

/* A page whose categories are in. */
async function ready(...args) {
  const p = load(...args);
  await settle();
  return p;
}

const MARA = { person_id: 9001, name: 'Mara Ellison', job: 'Actor', profile_url: '/api/watch/img/tmdb/w185/p.jpg', known_for: ['Salt'] };
const ADMIN = { is_admin: true, view: 'rows', tmdb_configured: true, settings_tmdb_href: '/settings#setg-integrations' };
const VIEWER = { is_admin: false, view: 'rows', tmdb_configured: false, settings_tmdb_href: '/settings#setg-integrations' };

const scenarios = {
  // The library group is on the page before TMDB answers; TMDB then fills its own place.
  async libraryFirst() {
    const p = load(ADMIN);
    await p.search('orchard');
    const out = { debounce: p.timers[0].ms, urls: p.calls.map((c) => c.url) };
    await p.answer('/api/watch/search?', { items: [LIB] });
    out.afterLibrary = {
      heads: p.heads(),
      libraryRefs: p.doc.querySelectorAll('.watch-card [data-title-ref]').map((c) => c.getAttribute('data-title-ref')),
      keepButtons: p.doc.querySelectorAll('[data-watch-keep]').length,
      skeleton: p.doc.querySelectorAll('.watch-card--skel').length > 0,
      shown: p.results.hidden === false && p.browse.hidden === true,
    };
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    const tm = p.doc.querySelectorAll('.watch-card').filter((c) => c.querySelector('[data-title-ref="tmdb-movie-9"]'))[0];
    out.afterTmdb = {
      heads: p.heads(),
      counts: p.doc.querySelectorAll('.results-group__count').map((c) => c.textContent),
      skeleton: p.doc.querySelectorAll('.watch-card--skel').length > 0,
      href: tm.querySelector('a').href,
      badge: tm.querySelector('.watch-badge--queued') !== null,
      sub: tm.querySelector('.watch-card__sub').textContent,
      status: p.status.textContent,
    };
    return out;
  },

  // More titles match than the answer holds; a category named in full adds its own.
  async someOfMany() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB, { ...LIB, ref: 'movie-2', title: 'Orchard Two' }], total: 297 });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    const count = () => p.doc.querySelector('.results-group__count').textContent;
    const out = { count: count(), status: p.status.textContent };
    await p.search('orchard');
    out.again = count();
    const a = await ready(ADMIN);
    await a.search('action');
    await a.answer('/api/watch/search?', { items: [{ ...LIB, title: 'Action One' }], total: 297 });
    await a.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    await a.answer('/api/watch/explore?', { items: ['Action Two', 'Held'].map((title, i) => ({ ...TM, ref: 'tmdb-movie-' + i, title, in_library: true, library_ref: 'movie-8' + i })) });
    out.category = a.doc.querySelector('.results-group__count').textContent;
    return out;
  },

  // A reply for an older query, or for one shorter than TMDB takes, never reaches the page.
  async staleReply() {
    const p = load(ADMIN);
    await p.search('ab');
    const first = p.calls.slice();
    await p.search('abc');
    const second = p.calls.filter((c) => !first.includes(c));
    // the newer answers first, the older one limps in after
    second.forEach((c) => { c.done = true; c.resolve(c.url.includes('/tmdb') ? { tmdb_configured: true, items: [] } : { items: [{ ...LIB, title: 'New' }] }); });
    await settle();
    first.forEach((c) => c.resolve({ items: [{ ...LIB, title: 'Old' }], tmdb_configured: true }));
    await settle();
    const out = { afterOlder: p.titles('.watch-card') };

    // Typing down to one letter: the two-letter reply that is still out is dropped.
    const q = load(ADMIN);
    await q.search('ab');
    const slow = q.calls.slice();
    await q.search('a');
    out.shortCalls = q.calls.filter((c) => !slow.includes(c)).map((c) => c.url);
    await q.answer('/api/watch/search?q=a', { items: [{ ...LIB, title: 'Short' }] });
    slow.forEach((c) => c.resolve({ items: [{ ...LIB, title: 'Stale' }], tmdb_configured: true }));
    await settle();
    out.afterShort = { titles: q.titles('.watch-card'), heads: q.heads() };
    return out;
  },

  // Only TMDB fails: the library stays, the second group retries on its own.
  async tmdbFailed() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    p.calls.filter((c) => c.url.includes('/tmdb'))[0].reject(new Error('502'));
    await settle();
    const out = { heads: p.heads(), libraryCards: p.titles('.watch-card'), alert: p.doc.querySelectorAll('[role="alert"]').length };
    const before = p.calls.length;
    click(p.doc, p.doc.querySelector('.watch-results__retry'));
    await settle();
    out.retryUrls = p.calls.slice(before).map((c) => c.url);
    out.retrying = p.doc.querySelectorAll('.watch-card--skel').length > 0;
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    out.after = { heads: p.heads(), titles: p.titles('.watch-card') };
    return out;
  },

  // "Nothing found" only when both groups are empty.
  async nothing() {
    const both = load(ADMIN);
    await both.search('zzqx');
    await both.answer('/api/watch/search?', { items: [] });
    const pending = both.line();
    await both.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    const onlyTmdb = load(ADMIN);
    await onlyTmdb.search('orchard');
    await onlyTmdb.answer('/api/watch/search?', { items: [] });
    await onlyTmdb.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    return {
      whileWaiting: pending,
      line: both.line(),
      help: both.doc.querySelector('.watch-results__hint').textContent !== '',
      status: both.status.textContent,
      tmdbOnly: { line: onlyTmdb.line(), heads: onlyTmdb.heads() },
    };
  },

  // TMDB not set up: no request for it, a hint under the library, and the viewer never reads "TMDB".
  async notConfigured() {
    const out = {};
    for (const [name, data] of [['admin', { ...ADMIN, tmdb_configured: false }], ['viewer', VIEWER]]) {
      const p = load(data);
      await p.search('orchard');
      await p.answer('/api/watch/search?', { items: [LIB] });
      const hint = p.doc.querySelector('.results-hint');
      const link = hint.querySelector('a');
      out[name] = { keep: p.doc.querySelectorAll('[data-watch-keep]').length, calls: p.calls.length, text: hint.textContent, href: link && link.href };
    }
    // No match either: the same hint stands under the empty state, so nobody wonders why nothing outside was searched.
    for (const [name, data] of [['admin', { ...ADMIN, tmdb_configured: false }], ['viewer', VIEWER]]) {
      const none = load(data);
      await none.search('zzqx');
      await none.answer('/api/watch/search?', { items: [] });
      const hint = none.doc.querySelector('.results-hint');
      const link = hint && hint.querySelector('a');
      out.empty = out.empty || {};
      out.empty[name] = {
        line: none.line(),
        text: hint && hint.textContent, href: link && link.href,
      };
    }
    // The reply may say it too, even when the page thought it was set up.
    const late = load(ADMIN);
    await late.search('orchard');
    await late.answer('/api/watch/search?', { items: [LIB] });
    await late.answer('/api/watch/search/tmdb', { tmdb_configured: false, items: [] });
    out.fromReply = late.doc.querySelector('.results-hint') !== null;
    return out;
  },

  // Cards from either builder put the ref on the link, which can take focus back when the detail closes.
  async cardLink() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    const marked = p.doc.querySelectorAll('.watch-card [data-title-ref]');
    return {
      tags: marked.map((n) => n.tagName),
      refs: marked.map((n) => n.getAttribute('data-title-ref')),
      onArticle: p.doc.querySelectorAll('article').filter((n) => n.hasAttribute('data-title-ref')).length,
    };
  },

  // A retry rebuilds the group that held the button: focus goes to the new group's heading, or to the new Try again.
  async retryFocus() {
    const out = {};
    const active = (p) => p.doc.activeElement.className + '|' + p.doc.activeElement.textContent;
    const tmdb = load(ADMIN);
    await tmdb.search('orchard');
    await tmdb.answer('/api/watch/search?', { items: [LIB] });
    tmdb.calls.filter((c) => c.url.includes('/tmdb'))[0].reject(new Error('502'));
    await settle();
    const retry = tmdb.doc.querySelector('.watch-results__retry');
    retry.focus();
    click(tmdb.doc, retry);
    await settle();
    out.whileLoading = tmdb.doc.activeElement === retry;
    await tmdb.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    out.tmdbOk = active(tmdb);
    out.tmdbOkInGroup = tmdb.doc.querySelectorAll('.results-group__head')[1].contains(tmdb.doc.activeElement);
    out.headingFocusable = tmdb.doc.activeElement.tabIndex;

    const again = load(ADMIN);
    await again.search('orchard');
    await again.answer('/api/watch/search?', { items: [LIB] });
    again.calls.filter((c) => c.url.includes('/tmdb'))[0].reject(new Error('502'));
    await settle();
    const first = again.doc.querySelector('.watch-results__retry');
    first.focus();
    click(again.doc, first);
    await settle();
    again.calls.filter((c) => c.url.includes('/tmdb')).pop().reject(new Error('502'));
    await settle();
    const second = again.doc.querySelector('.watch-results__retry');
    out.failedAgain = second !== first && again.doc.activeElement === second;

    const lib = load(ADMIN);
    await lib.search('orchard');
    lib.calls.filter((c) => c.url.includes('/tmdb'))[0].resolve({ tmdb_configured: true, items: [TM] });
    lib.calls.filter((c) => !c.url.includes('/tmdb'))[0].reject(new Error('502'));
    await settle();
    const lr = lib.doc.querySelector('.watch-results__retry');
    lr.focus();
    click(lib.doc, lr);
    await settle();
    await lib.answer('/api/watch/search?', { items: [LIB] });
    out.library = active(lib);
    return out;
  },

  // Both searches failing is one line and one Try again that runs both again.
  async bothFailed() {
    const p = load(ADMIN);
    await p.search('orchard');
    p.calls.forEach((c) => c.reject(new Error('502')));
    await settle();
    const msg = p.doc.querySelector('.results-fail');
    const out = {
      text: msg.textContent, alerts: p.doc.querySelectorAll('[role="alert"]').length,
      groups: p.heads().length, status: p.status.textContent,
      buttons: p.doc.querySelectorAll('button').filter((b) => b.textContent === 'Try again').length,
    };
    const before = p.calls.length;
    const retry = msg.querySelector('button');
    retry.focus();
    click(p.doc, retry);
    await settle();
    out.retryUrls = p.calls.slice(before).map((c) => c.url);
    out.focus = p.doc.activeElement === p.input;
    return out;
  },

  // No library match and TMDB down: the status and a line above the TMDB error say the library has nothing.
  async libraryEmptyTmdbFailed() {
    const p = load(ADMIN);
    await p.search('zephyrine');
    await p.answer('/api/watch/search?', { items: [] });
    const waiting = p.doc.querySelector('.results-none');
    p.calls.filter((c) => c.url.includes('/tmdb'))[0].reject(new Error('502'));
    await settle();
    const none = p.doc.querySelector('.results-none');
    const rows = p.doc.querySelector('.results-grid').children;
    return {
      waiting: waiting === null,
      status: p.status.textContent,
      line: none && none.textContent,
      above: rows[0] === none && rows.length === 3,
      heads: p.heads(),
      alerts: p.doc.querySelectorAll('[role="alert"]').length,
    };
  },

  // The rows fade and shrink behind the results, which are there at once; clearing half-way brings them back.
  async viewFade() {
    const out = {};
    const p = await ready(ADMIN);
    const finish = withMotion(p);
    try {
      const state = () => [p.shown(), p.browse.classList.contains('is-off')];
      p.type('orchard');
      out.during = state();
      p.timers.filter((t) => t.on && t.ms === 150).forEach((t) => { t.on = false; t.fn(); });
      out.after = state();
      p.type('');
      out.back = state();
      // Cleared before the rows are gone: nothing hides them later.
      p.type('orchard');
      p.type('');
      p.wait();
      await settle();
      out.quick = state();
      finish();
      await settle();
      out.left = p.doc.querySelectorAll('.results-page').length;
    } finally {
      delete Node.prototype.animate;
    }
    const still = await ready(ADMIN);
    still.type('orchard');
    out.reduced = [still.shown(), still.browse.classList.contains('is-off')];
    return out;
  },

  // Whatever order the queries and replies come in, and however fast, the end is one clean view.
  async settledAfterFade() {
    const page = async () => {
      const p = load(ADMIN);
      await p.search('orchard');
      await p.answer('/api/watch/search?', { items: [LIB] });
      await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
      return p;
    };
    const view = (p) => ({
      heads: p.heads(), titles: p.titles('.watch-card'), skeleton: p.doc.querySelectorAll('.watch-card--skel').length, pages: p.doc.querySelectorAll('.results-page').length,
      leaving: p.doc.querySelectorAll('.results-ghosts').reduce((n, g) => n + g.children.length, 0), line: p.line(),
    });
    const out = {};
    const a = await page();
    const finish = withMotion(a);
    try {
      // Nothing matches while the old results are still fading out.
      await a.search('zzqx');
      await a.answer('/api/watch/search?', { items: [] });
      await a.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      finish();
      await settle();
      out.nothing = view(a);
    } finally {
      delete Node.prototype.animate;
    }
    const b = await page();
    const end = withMotion(b);
    try {
      // The next query lands before the last one's posters are in place.
      await b.search('tallow');
      await b.answer('/api/watch/search?', { items: [{ ...LIB, ref: 'movie-2', title: 'Tallow' }] });
      await b.search('harrow');
      await b.answer('/api/watch/search?', { items: [{ ...LIB, ref: 'movie-3', title: 'Harrow' }] });
      await b.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      out.midway = b.doc.querySelectorAll('.results-ghosts').some((g) => g.querySelectorAll('[data-title-ref], [data-watch-keep]').length > 0);
      end();
      await settle();
      out.chained = view(b);
    } finally {
      delete Node.prototype.animate;
    }
    return out;
  },

  // A query under two letters asks the library only; two letters ask TMDB too.
  async short() {
    const p = load(ADMIN);
    await p.search('a');
    const one = p.calls.map((c) => c.url);
    const q = load(ADMIN);
    await q.search('ab');
    return { one, two: q.calls.map((c) => c.url) };
  },

  // Clearing goes back to the rows, keeps focus in the field and takes ?q= off the address.
  async clear() {
    const p = load(ADMIN, '', '/watch?foo=1#title=movie-2');
    await p.search('orchard');
    const withQ = p.urls[p.urls.length - 1];
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    click(p.doc, p.$('watch-search-clear'));
    await settle();
    return {
      withQ, url: p.urls[p.urls.length - 1], value: p.input.value, focus: p.doc.activeElement === p.input,
      browse: p.browse.hidden === false, results: p.results.hidden, status: p.status.textContent,
      groups: p.heads().length, length: p.tab.entries.length,
    };
  },

  // Enter searches at once, and on a phone puts the keyboard away.
  async enter() {
    const out = {};
    for (const [name, coarse] of [['phone', true], ['desk', false]]) {
      const p = load(ADMIN);
      p.ctx.matchMedia = (q) => ({ matches: q.includes('coarse') ? coarse : false });
      p.input.focus();
      p.type('orchard');
      let prevented = false;
      p.input.listeners.keydown.forEach((fn) => fn({ key: 'Enter', preventDefault() { prevented = true; } }));
      await settle();
      out[name] = { urls: p.calls.map((c) => c.url), prevented, focus: p.doc.activeElement === p.input };
    }
    return out;
  },

  // An Enter that ends a composition, or one held down, does not search again.
  async enterNotAsked() {
    const p = load(ADMIN);
    p.type('orchard');
    for (const held of [{ isComposing: true }, { repeat: true }]) {
      p.input.listeners.keydown.forEach((fn) => fn({ key: 'Enter', preventDefault() {}, ...held }));
    }
    await settle();
    return p.calls.length;
  },

  // Only what leaves today or tomorrow is urgent.
  async leaving() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [0, 1, 2].map((d) => ({ ...LIB, ref: 'movie-1' + d, leaving_days: d })) });
    return p.doc.querySelectorAll('.watch-badge--leaving').map((b) => b.classList.contains('watch-badge--leaving-now'));
  },

  // A library result carries its mark and its More; one outside the library neither.
  async marks() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [{ ...LIB, watched: true, mark_kind: 'movie', mark_key: '1' }] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    return p.doc.querySelectorAll('.watch-card').map((c) => [c.getAttribute('data-mark'), !!c.querySelector('.watch-badge--watched'),
      c.querySelectorAll('[data-watch-more]').length, c.querySelectorAll('[data-watch-keep]').length]);
  },

  // A search in the address opens on the results: the rows never show, and a skeleton holds the place.
  async openedOnResults() {
    const p = load(ADMIN, 'orchard');
    const out = {
      urls: p.calls.map((c) => c.url),
      before: { results: p.results.hidden, browse: p.browse.hidden, skeleton: p.doc.querySelectorAll('.watch-card--skel').length > 0 },
      clear: p.doc.querySelector('.watch-search').classList.contains('has-value'),
    };
    await p.answer('/api/watch/search?', { items: [LIB] });
    out.after = { results: p.results.hidden, browse: p.browse.hidden, titles: p.doc.querySelectorAll('.watch-card__title').map((t) => t.textContent) };
    // Clearing goes back to the rows.
    click(p.doc, p.$('watch-search-clear'));
    await settle();
    out.cleared = { results: p.results.hidden, browse: p.browse.hidden };
    // Nothing found removes the skeleton too.
    const none = load(ADMIN, 'zzqx');
    await none.answer('/api/watch/search?', { items: [] });
    await none.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    out.none = { skeleton: none.doc.querySelectorAll('.watch-card--skel').length, line: none.line() };
    return out;
  },

  // A download's badge says what the percentage is for a reader of the page, and a part-owned show counts as in the library.
  async stateBadges() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [] });
    await p.answer('/api/watch/search/tmdb', {
      tmdb_configured: true,
      items: [
        { ...TM, ref: 'tmdb-movie-1', title: 'One', download_state: 'downloading', download_pct: 95 },
        { ...TM, ref: 'tmdb-movie-2', title: 'Two', download_state: 'downloading' },
        { ...TM, ref: 'tmdb-movie-3', title: 'Three', download_state: 'partial' },
      ],
    });
    const badges = p.doc.querySelectorAll('.watch-badge--state');
    return {
      classes: badges.map((b) => b.className),
      texts: badges.map((b) => b.textContent),
      said: badges.map((b) => b.querySelectorAll('.sr-only').map((n) => n.textContent)),
    };
  },

  // A poster both searches found is the same node before and after, so it never blinks; one whose state changed is drawn anew.
  async kept() {
    const p = load(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    const card = (ref) => p.doc.querySelectorAll('.watch-card').filter((c) => c.querySelector('[data-title-ref="' + ref + '"]'))[0];
    const before = [card('movie-1'), card('tmdb-movie-9')];
    await p.search('orchard r');
    const out = { waiting: card('movie-1') === before[0], dimmed: p.doc.querySelector('.results-grid').getAttribute('aria-busy') };
    await p.answer('/api/watch/search?', { items: [{ ...LIB, ref: 'movie-4', title: 'Orchard Rise' }, LIB] });
    // TMDB has not answered: what it said before and still fits stays.
    out.early = [p.titles('.watch-card'), card('tmdb-movie-9') === before[1]];
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [{ ...TM, download_state: 'downloading', download_pct: 4 }] });
    out.same = card('movie-1') === before[0];
    out.redrawn = card('tmdb-movie-9').querySelector('.watch-badge--state').textContent;
    out.titles = p.titles('.watch-card');
    return out;
  },

  // Categories are suggested on the keystroke itself; people and titles as soon as the server names them.
  async suggestions() {
    const p = await ready(ADMIN);
    p.type('act');
    const out = { atOnce: p.suggested(), calls: p.calls.length, expanded: p.input.getAttribute('aria-expanded') };
    await p.search('mar');
    await p.answer('/api/watch/search?', { items: [{ ...LIB, ref: 'movie-8', title: 'Marrow' }] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [], people: [MARA] });
    out.named = p.suggested();
    out.status = p.status.textContent;
    p.type('mara');
    out.known = p.suggested();
    // A phrase that asks for a kind and an order.
    p.type('best action movies');
    out.phrase = p.suggested();
    // Other words for a category, but not from their first letters.
    p.type('scary');
    out.alias = p.suggested();
    p.type('mart');
    out.begun = p.suggested()[0];
    return out;
  },

  // What is typed begins a word however either is spelled; what stands out is the title's own letters.
  async foldedSuggestions() {
    const p = await ready(ADMIN);
    const typed = async (title, q) => {
      await p.search(q);
      await p.answer('/api/watch/search?', { items: [{ ...LIB, title }] });
      await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      const marks = p.doc.querySelectorAll('.watch-suggestion--title em').map((n) => n.textContent);
      return marks.length ? marks[0] : null;
    };
    const out = [];
    for (const [title, q] of [['Amélie', 'ame'], ['Amelie', 'Amé'], ['The Handmaid’s Tale', 'handmaids'], ['The Amazing Spider-Man', 'spiderman'],
      ['Spiderman Returns', 'spider-man'], ['WALL·E', 'wall-e'], ['Law & Order', 'law and'], ['Law and Order', 'law & o'], ['A & B', 'b'],
      ['S.W.A.T.', 'swat'], ['Se7en!', 'se7en!'], ['…And Then', '…'], ['Anthill', 'hill'], ['O’Hara', 'hara'], ['Salt', '!']]) out.push(await typed(title, q));
    return out;
  },

  // TMDB found what the library holds, which its own search may not have: it stands once, among what is held, with its Keep.
  async heldByTmdb() {
    const p = await ready(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    const held = { ...TM, in_library: true, library_ref: 'movie-1', title: 'Orchard Keepers' };
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true,
      items: [held, { ...held, ref: 'tmdb-movie-10', library_ref: 'movie-2', title: 'L’Orchard', href: '/watch/movie/2', leaving_days: 3, keep_id: '2' }, { ...TM, ref: 'tmdb-movie-11' }] });
    const out = { heads: p.heads(), titles: p.titles('.watch-card'), counts: p.doc.querySelectorAll('.results-group__count').map((n) => n.textContent),
      keeps: p.doc.querySelectorAll('[data-watch-keep]').map((n) => n.getAttribute('data-watch-keep')), status: p.status.textContent };
    const q = await ready(ADMIN);
    await q.search('amelie');
    await q.answer('/api/watch/search?', { items: [], total: 0 });
    await q.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [{ ...held, title: 'Amélie', href: '/watch/movie/1' }] });
    out.alone = [q.heads(), q.line(), q.doc.querySelector('.watch-card__link').href];
    return out;
  },

  // The column beside the grid goes only once nothing is known to come into it, so the grid is not moved and moved back.
  async column() {
    const p = await ready(ADMIN);
    const none = () => p.doc.querySelector('.results-page').classList.contains('has-no-suggestions');
    await p.search('orchard');
    const out = { first: none() };
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    p.type('lantern');
    out.typing = [none(), p.suggested().length];
    await p.search('lantern');
    await p.answer('/api/watch/search?', { items: [{ ...LIB, ref: 'movie-5', title: 'Alight', subtitle: 'Lantern' }] });
    out.early = none();
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    out.settled = none();
    return out;
  },

  // "popular action movies" lists the category's own titles with whatever matched, each once.
  async phraseResults() {
    const p = await ready(ADMIN);
    await p.search('popular action movies');
    const out = { urls: p.calls.map((c) => c.url) };
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    out.waiting = p.doc.querySelector('.watch-search').classList.contains('is-pending');
    await p.answer('/api/watch/explore?', { items: [
      { ...TM, ref: 'tmdb-movie-1', title: 'Orchard Keepers', in_library: true, library_ref: 'movie-1' },
      { ...TM, ref: 'tmdb-movie-2', title: 'Held', in_library: true, library_ref: 'movie-2', keep_id: '8', leaving_days: 3 },
      { ...TM, ref: 'tmdb-movie-4', title: 'Staying', in_library: true, library_ref: 'movie-4', keep_id: null, leaving_days: null },
      { ...TM, ref: 'tmdb-movie-3', title: 'Not held', download_state: null },
    ] });
    out.keeps = p.doc.querySelectorAll('.watch-card').map((c) => [c.querySelector('[data-watch-keep]'), c.querySelector('.watch-badge--leaving')]
      .map((n) => n && (n.getAttribute('data-watch-keep') || n.textContent)).concat(c.getAttribute('data-keep-id')));
    // On the category's own page they have none: it has its own badge.
    out.explore = !!p.ctx.MMWatch.search.tmdbCard({ ...TM, in_library: true, keep_id: '8', leaving_days: 3 }, true).querySelector('[data-watch-keep]');
    out.titles = p.titles('.watch-card');
    out.counts = p.doc.querySelectorAll('.results-group__count').map((c) => c.textContent);
    out.status = p.status.textContent;
    // Nothing matched by name and the list failed: that is a failure, not an empty answer.
    const q = await ready(ADMIN);
    await q.search('action');
    await q.answer('/api/watch/search?', { items: [] });
    await q.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    q.calls.filter((c) => c.url.includes('explore'))[0].reject(new Error('502'));
    await settle();
    out.failed = [q.doc.querySelectorAll('.results-fail').length, q.line()];
    return out;
  },

  // Arrows move one highlight; Esc drops it, then the text, then the sheet; Enter opens what is highlighted.
  async keys() {
    const p = await ready(ADMIN);
    const marked = () => p.doc.querySelectorAll('.is-picked').map((n) => n.id + '=' + n.getAttribute('aria-selected'));
    p.press();
    p.input.focus();
    await settle();
    const out = { open: [p.sheet.classList.contains('is-on'), p.sheet.inert, p.browse.inert, p.input.getAttribute('aria-expanded')] };
    p.key('ArrowDown');
    out.tile = [marked(), p.input.getAttribute('aria-activedescendant')];
    p.key('Escape');
    out.dropped = [marked(), p.sheet.classList.contains('is-on')];
    p.key('Escape');
    out.closed = [p.sheet.classList.contains('is-on'), p.browse.inert, p.doc.activeElement === p.input];
    p.input.focus();
    out.script = p.sheet.classList.contains('is-on'); // focus put here by a script opens nothing
    await p.search('act');
    p.key('ArrowDown');
    out.row = marked();
    p.key('ArrowUp');
    out.none = marked();
    p.key('Escape');
    out.cleared = [p.input.value, p.shown(), p.doc.activeElement === p.input];
    await p.search('act');
    p.key('ArrowDown');
    p.doc.querySelector('.is-picked').click = function () { click(p.doc, this); };
    p.key('Enter');
    await settle();
    out.opened = p.url();
    return out;
  },

  // "/" goes to the field, but not from another field, with a modifier, behind a dialog or while the page is out of reach.
  async slash() {
    const p = await ready(ADMIN);
    const press = (more, from) => {
      p.input.blur();
      if (from) from.focus();
      const e = { key: '/', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...more };
      p.doc.listeners.keydown.forEach((fn) => fn(e));
      return p.doc.activeElement === p.input;
    };
    const other = p.doc.body.appendChild(p.doc.createElement('textarea'));
    const out = { plain: press({}), sheet: p.sheet.classList.contains('is-on'), modified: press({ metaKey: true }), field: press({}, other) };
    const dialog = p.doc.body.appendChild(p.doc.createElement('div'));
    dialog.setAttribute('aria-modal', 'true');
    out.dialog = press({});
    dialog.remove();
    p.$('watch-page').setAttribute('inert', '');
    out.inert = press({});
    return out;
  },

  // Nothing to browse without TMDB: the field opens no sheet and suggests no category.
  async noSheet() {
    const p = await ready({ ...ADMIN, tmdb_configured: false });
    p.press();
    p.input.focus();
    const out = { sheet: p.sheet.classList.contains('is-on') };
    p.type('action');
    out.suggested = p.suggested();
    const l = await ready({ ...ADMIN, view: 'list' });
    l.press();
    out.list = l.sheet.classList.contains('is-on');
    return out;
  },

  // A category opened from a search: Back is the search again, and forward of it nothing is lost.
  async categoryAndBack() {
    const p = await ready(ADMIN);
    await p.search('action');
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    await p.answer('/api/watch/explore?', { items: [] });
    click(p.doc, p.doc.querySelector('.watch-suggestion--category'));
    await settle();
    const token = () => { const t = p.$('watch-search-token'); return t.hidden ? null : t.textContent; };
    const out = {
      url: p.url(), entries: p.tab.entries.length, value: p.input.value, token: token(), shown: p.shown(),
      title: p.doc.querySelector('.explore-title').textContent, cards: p.heads(),
    };
    await p.back();
    out.back = { url: p.url(), value: p.input.value, token: token(), titles: p.titles('.watch-card'), explore: p.doc.querySelector('.explore') !== null, stopped: p.grids[0].stopped };
    return out;
  },

  // Typing on a category's page searches in a new entry, so Back returns to the category; the cross goes to the rows.
  async typeOnCategory() {
    const p = await ready(ADMIN, '', '/watch?genre=action&sort=newest');
    const out = { first: [p.url(), p.grids.length, p.calls.map((c) => c.url), p.shown()] };
    p.type('o');
    out.typed = [p.url(), p.tab.entries.length, p.doc.querySelector('.explore') !== null, p.$('watch-search-token').hidden];
    await p.search('orchard');
    out.more = [p.url(), p.tab.entries.length];
    await p.back();
    out.back = [p.url(), p.input.value, p.doc.querySelector('.explore') !== null, p.$('watch-search-token').hidden];
    click(p.doc, p.$('watch-search-clear'));
    await settle();
    out.cleared = [p.url(), p.shown(), p.$('watch-search-token').hidden];
    return out;
  },

  // The filters change the address in place and ask for the first page in the new order; the last one pressed wins.
  async filters() {
    const p = await ready(ADMIN, '', '/watch?genre=action');
    const said = () => p.doc.querySelector('.explore-facts').textContent;
    const choose = (v) => click(p.doc, p.doc.querySelectorAll('.explore-seg__item').filter((b) => b.getAttribute('data-value') === v)[0]);
    const out = { before: said() };
    const page = (title, total) => ({ tmdb_configured: true, genre: GENRES[0], items: [{ ...TM, title }], total, next_page: 2 });
    await p.answer('explore?genre=action', page('One', 1280));
    out.loaded = [said(), p.grids[0].titles.length, p.grids[0].next];
    choose('movie');
    choose('top_rated');
    out.asked = p.calls.slice(1).map((c) => c.url);
    out.address = [p.url(), p.tab.entries.length];
    const [first, second] = p.calls.slice(1);
    second.resolve(page('Two', 7));
    await settle();
    first.resolve(page('Late', 9));
    await settle();
    out.set = p.grids[0].set;
    out.said = said();
    out.pressed = p.doc.querySelectorAll('.explore-seg__item').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent);
    // A failure is the grid's to show, with its way to try again.
    choose('newest');
    p.calls[3].reject(new Error('502'));
    await settle();
    out.failed = p.grids[0].set[1];
    // The same choice again asks for nothing.
    choose('newest');
    out.again = p.calls.length;
    return out;
  },

  // Only films or only TV: there is no kind to choose, and one in the address that it has not is not asked for.
  async oneKind() {
    const p = await ready(ADMIN);
    p.press();
    await settle();
    click(p.doc, p.$('watch-option-genre-horror'));
    await settle();
    const type = p.doc.querySelectorAll('.explore-seg').filter((n) => n.getAttribute('aria-label') === 'Type')[0];
    const out = { hidden: type.hidden === true, says: p.doc.querySelector('.explore-facts').textContent, url: p.calls[0].url };
    const linked = await ready(ADMIN, '', '/watch?genre=horror&type=tv');
    out.linked = [linked.url(), linked.calls[linked.calls.length - 1].url];
    return out;
  },

  // A person: what the library holds under its own heading, then the rest, whatever page they come on.
  async person() {
    const p = await ready(ADMIN, '', '/watch?person=9001');
    const item = (n, held) => ({ ...TM, ref: 'tmdb-movie-' + n, title: 'T' + n, in_library: held });
    await p.answer('/api/watch/person/9001', { tmdb_configured: true, person: { ...MARA, photo_url: '/api/watch/img/tmdb/w342/p.jpg' }, items: [item(1, true), item(2, true)], total: 5, in_library: 3, next_page: 2 });
    const out = {
      title: p.doc.querySelector('.explore-title').textContent, facts: p.doc.querySelector('.explore-facts').textContent,
      token: p.doc.querySelector('.watch-search__name').textContent, letters: p.doc.querySelector('.explore-photo').children[0].textContent, first: p.grids[0].titles, status: p.status.textContent,
      photo: p.doc.querySelector('.explore-photo img').src,
    };
    const more = p.grids[0].opts.fetchPage(2);
    await p.answer('person/9001?page=2', { tmdb_configured: true, person: MARA, items: [item(3, true), item(4, false), item(5, false)], total: 5, in_library: 3, next_page: null });
    out.second = (await more).cards.map((c) => c.textContent);
    // Who they are is in memory; what they are in is not, for its cards say what is so now.
    const known = await p.ctx.MMWatch.explore.person('9001');
    out.kept = [p.calls.length, Object.keys(known).sort()];
    p.ctx.MMWatch.address.push(null, '/watch');
    p.ctx.MMWatch.explore.openPerson(MARA, null);
    out.again = p.calls.slice(2).map((c) => c.url);
    await p.answer('/api/watch/person/9001', { tmdb_configured: true, person: MARA, items: [{ ...item(1, true), download_state: null }], total: 1, in_library: 1, next_page: null });
    out.now = p.grids[1].titles;
    // Come back to by the address alone, it says who from the start.
    await p.back();
    await p.ctx.history.go(1);
    await settle();
    out.back = p.doc.querySelector('.explore-title').textContent;
    return out;
  },

  // What is said of a person: its room held, then their own, and nothing where there is none.
  async personAbout() {
    const said = (p) => ({
      born: p.doc.querySelector('.explore-born').textContent, bio: p.doc.querySelector('.explore-bio').textContent,
      known: p.doc.querySelectorAll('.explore-known a').map((a) => [a.getAttribute('data-title-ref'), a.getAttribute('aria-label')]),
      hidden: ['born', 'story', 'known'].map((k) => p.doc.querySelector('.explore-' + k).hidden === true),
      skeletons: p.doc.querySelectorAll('.explore-head .skel').length,
    });
    const more = (p) => p.doc.querySelector('.explore-more');
    const told = { ...MARA, birthday: '1950-02-01', deathday: '2019-11-02', age: 69, biography: 'A long life.' };
    const known = [{ ...TM, library_ref: 'movie-1', title: 'Salt' }, { ...TM, title: 'Road' }];
    const reply = (person, known_for) => ({ tmdb_configured: true, person, known_for, items: [TM], total: 1, in_library: 0, next_page: null });
    const out = {};
    const p = await ready(ADMIN, '', '/watch?person=9001');
    out.waiting = said(p);
    const bio = p.doc.querySelector('.explore-bio');
    bio.clientHeight = 69;
    bio.scrollHeight = 140;
    await p.answer('/api/watch/person/9001', reply(told, known));
    out.told = said(p);
    const button = () => [more(p).textContent, more(p).getAttribute('aria-expanded'), more(p).hidden === true, bio.classList.contains('is-whole')];
    out.more = [button()];
    click(p.doc, more(p));
    out.more.push(button());
    click(p.doc, more(p));
    out.more.push(button());
    // Known by now, it is there at once.
    p.ctx.MMWatch.address.push(null, '/watch');
    p.ctx.MMWatch.explore.openPerson(MARA, null);
    out.again = said(p);
    const short = await ready(ADMIN, '', '/watch?person=9001');
    await short.answer('/api/watch/person/9001', reply(told, known));
    out.short = more(short).hidden === true;
    const bare = await ready(ADMIN, '', '/watch?person=9001');
    await bare.answer('/api/watch/person/9001', reply({ ...MARA, birthday: null, deathday: null, age: null, biography: '' }, []));
    out.bare = said(bare);
    const failed = await ready(ADMIN, '', '/watch?person=9001');
    failed.calls[0].reject(new Error('502'));
    await settle();
    out.failed = said(failed);
    click(failed.doc, failed.doc.querySelector('.grid-fail__retry'));
    await failed.answer('/api/watch/person/9001', reply(told, known));
    out.retried = said(failed);
    return out;
  },

  // Nobody's titles are counted where there are none.
  async personBare() {
    const p = await ready(ADMIN, '', '/watch?person=9001');
    await p.answer('/api/watch/person/9001', { tmdb_configured: true, person: MARA, known_for: [], items: [], total: 0, in_library: 0, next_page: null });
    return p.doc.querySelector('.explore-facts').textContent;
  },

  // A name of one long word takes its room from the posters beside it.
  async longName() {
    const p = load(ADMIN, '', null, { tmdb_configured: true, genres: [{ ...GENRES[0], id: 'documentary', name: 'Documentary' }, { ...GENRES[0], id: 'science-fiction', name: 'Science Fiction' }], decades: [], people: [] });
    await settle();
    p.press();
    await settle();
    return p.sheet.querySelectorAll('.watch-tile').map((n) => [n.textContent, n.classList.contains('is-long')]).sort();
  },

  // Less: cut short only once what stands below has closed over the text, unless More is pressed first.
  async storyCloses() {
    const p = await ready(ADMIN, '', '/watch?person=9001');
    const bio = p.doc.querySelector('.explore-bio');
    bio.clientHeight = 69;
    bio.scrollHeight = 140;
    await p.answer('/api/watch/person/9001', { tmdb_configured: true, person: { ...MARA, biography: 'A long life.' }, known_for: [], items: [TM], total: 1, in_library: 0, next_page: null });
    const finish = withMotion(p);
    const press = () => click(p.doc, p.doc.querySelector('.explore-more'));
    const look = () => [bio.classList.contains('is-whole'), bio.classList.contains('is-closing')];
    try {
      press();
      const out = [look()];
      press();
      out.push(look());
      press();
      out.push(look());
      press();
      finish();
      out.push(look());
      return out;
    } finally {
      delete Node.prototype.animate;
    }
  },

  // People and no titles: they lead, and that no title matches is a line of text.
  async peopleLead() {
    const find = async (p, q, people, items) => {
      await p.search(q);
      await p.answer('/api/watch/search?', { items: [] });
      await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items, people });
    };
    const look = (p) => ({ leads: p.doc.querySelector('.results-page').classList.contains('is-people'), empty: p.doc.querySelectorAll('.results-empty').length,
      none: p.doc.querySelectorAll('.results-none').map((n) => n.textContent), kinds: p.doc.querySelectorAll('.watch-suggestion').map((n) => n.className.match(/--(\w+)/)[1]) });
    const p = await ready(ADMIN);
    const out = {};
    await find(p, 'mara', [MARA], []);
    out.person = look(p);
    await find(p, 'ma', [MARA], [{ ...TM, title: 'Man' }]);
    out.both = look(p);
    await find(p, 'hor', [{ ...MARA, name: 'Horace Pell' }], []);
    out.mixed = look(p);
    await find(p, 'sci', [], []);
    out.category = look(p);
    // Typed on, they stay while the answers are on their way, and go for a title.
    await find(p, 'mara', [MARA], []);
    await p.search('mara e');
    out.asked = look(p);
    await p.answer('/api/watch/search?', { items: [] });
    out.half = look(p);
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [], people: [MARA] });
    out.still = look(p);
    await p.search('mara el');
    await p.answer('/api/watch/search?', { items: [{ ...LIB, title: 'Mara Elm' }] });
    out.title = look(p);
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [], people: [] });
    await find(p, 'mara', [MARA], []);
    await p.search('zz');
    await p.answer('/api/watch/search?', { items: [] });
    out.nobody = look(p);
    return out;
  },

  // Switched to the list, what was found fades out over it.
  async listOverResults() {
    const p = await ready(ADMIN);
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    const finish = withMotion(p);
    try {
      p.input.value = '';
      p.fire('mm:watch-view', { detail: { view: 'list' } });
      const left = p.doc.querySelector('.results-page.is-leaving');
      const out = [!!left && left.parentNode === p.$('watch-page'), p.results.hidden];
      finish();
      await settle();
      return out.concat(p.doc.querySelectorAll('.results-page').length);
    } finally {
      delete Node.prototype.animate;
    }
  },

  // A title comes once, a page of repeats leads on to the next, and one found in the library late stands without a heading of its own.
  async personPages() {
    const p = await ready(ADMIN, '', '/watch?person=9001');
    const item = (n, held) => ({ ...TM, ref: 'tmdb-movie-' + n, title: 'T' + n, in_library: held, download_state: null });
    const page = (n, items, next) => p.answer('person/9001' + (n > 1 ? '?page=' + n : ''), { tmdb_configured: true, person: MARA, items, total: 6, in_library: 2, next_page: next });
    await page(1, [item(1, true), item(2, false)], 2);
    const more = p.grids[0].opts.fetchPage(2);
    await page(2, [item(2, false), item(1, true)], 3);
    await page(3, [item(3, false), item(2, false), item(4, true)], 4);
    const got = await more;
    // Kept in memory, pages 2 and 3 are not asked for again; the last is empty and says it is the end.
    const last = p.grids[0].opts.fetchPage(4);
    await page(4, [], null);
    return { first: p.grids[0].titles, cards: got.cards.map((c) => c.textContent), next: got.next, calls: p.calls.length, last: [(await last).cards.length, (await last).next] };
  },

  // Esc and the page's Back leave the way Back does; the Browse half of the switch goes to the rows.
  async leaveCategory() {
    const out = {};
    const esc = (p, prevented) => p.fire('keydown', { key: 'Escape', defaultPrevented: !!prevented, preventDefault() {} });
    const linked = await ready(ADMIN, '', '/watch?genre=action&type=tv');
    esc(linked, true); // a menu's own
    out.menus = linked.url();
    esc(linked);
    await settle();
    out.linked = [linked.url(), linked.tab.entries.length, linked.shown(), linked.grids[0].stopped];
    const opened = await ready(ADMIN);
    opened.press();
    await settle();
    click(opened.doc, opened.$('watch-option-genre-action'));
    await settle();
    const at = [opened.url(), opened.tab.index];
    click(opened.doc, opened.doc.querySelector('.explore-back'));
    await settle();
    out.opened = [at, opened.url(), opened.tab.index, opened.shown()];
    const away = await ready(ADMIN, '', '/watch?person=9001');
    click(away.doc, away.doc.querySelector('[data-view="rows"]'));
    await settle();
    out.away = [away.url(), away.shown(), away.$('watch-search-token').hidden];
    return out;
  },

  // The list view opened over a category, as list.js does: the category is shut, and Back opens it again.
  async listOverCategory() {
    const p = await ready(ADMIN, '', '/watch?genre=action');
    const { address } = p.ctx.MMWatch;
    const say = (view) => p.fire('mm:watch-view', { detail: { view } });
    address.layer({ name: 'list', depth: 5, params: ['view'], read: (h) => h.params.get('view'), open: () => say('list'), close: () => say('rows') });
    address.push('list', '/watch?view=list');
    say('list');
    const out = { list: [p.grids[0].stopped, p.results.hidden, address.value('explore')] };
    // What is typed there stays there when Back leads to what the address opens by itself.
    p.input.value = 'lantern';
    await p.back();
    out.back = [p.url(), p.grids.length, p.results.hidden, p.input.value, p.calls.filter((c) => c.url.includes('search')).length];
    return out;
  },

  // What was typed, read as a category.
  async phrases() {
    const p = await ready(ADMIN);
    const read = (text) => { const r = p.ctx.MMWatch.explore.parse(text); return [r.genres.map((g) => g.id).join(), r.exact, r.type, r.sort, r.decade]; };
    return ['popular action movies', 'sci-fi', 'sci fi shows', 'newest 90s horror', 'action films from the 2010s', 'tv', 'new', 'horror series', 'zzqx', 'a']
      .map(read);
  },

  // In the list view the field only announces what was typed; no search runs.
  async listView() {
    const roles = (page) => ['role', 'aria-autocomplete', 'aria-expanded', 'aria-controls'].map((a) => page.input.getAttribute(a));
    const p = load({ ...ADMIN, view: 'list' });
    const l = { roles: roles(p) };
    p.type('lantern');
    const delay = p.timers[p.timers.length - 1].ms;
    p.timers[p.timers.length - 1].fn();
    await settle();
    const out = { delay, events: p.events.slice(), calls: p.calls.length, resultsHidden: p.results.hidden };
    p.type('');
    out.cleared = p.events[p.events.length - 1];
    // Switching to the rows: the same field searches again.
    const r = load({ ...ADMIN, view: 'list' });
    r.fire('mm:watch-view', { detail: { view: 'rows' } });
    await r.search('lantern');
    out.role = [l.roles, roles(r)];
    out.afterSwitch = { events: r.events.length, calls: r.calls.length, placeholder: r.input.placeholder };
    return out;
  },

  // A search that runs before the categories are in does not say there is nothing, and lists the category once they are.
  async lateCategories() {
    const p = load(ADMIN, 'popular action movies', null, null);
    await p.answer('/api/watch/search?', { items: [] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    const out = { waiting: [p.line(), p.doc.querySelector('.watch-search').classList.contains('is-pending')], status: [p.status.textContent] };
    const all = { tmdb_configured: true, genres: GENRES, decades: DECADES };
    const asked = p.calls.filter((c) => c.url === '/api/watch/explore')[0];
    asked.done = true;
    asked.resolve(all);
    await settle();
    out.asked = p.calls.filter((c) => c.url.includes('explore?')).map((c) => c.url);
    out.suggested = p.suggested()[0];
    out.status.push(p.status.textContent);
    await p.answer('/api/watch/explore?', { items: [{ ...TM, title: 'Held' }] });
    out.titles = p.titles('.watch-card');
    out.status.push(p.status.textContent);
    // Typed and not yet searched for: suggested as they come in.
    const q = load(ADMIN, '', null, null);
    q.type('act');
    out.early = q.suggested();
    q.calls.filter((c) => c.url === '/api/watch/explore')[0].resolve(all);
    await settle();
    out.typed = q.suggested().length;
    // They cannot be had: the search ends without them.
    const r = load(ADMIN, 'action', null, null);
    await r.answer('/api/watch/search?', { items: [] });
    await r.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    r.calls.filter((c) => c.url === '/api/watch/explore').forEach((c) => c.reject(new Error('502')));
    await settle();
    out.without = [r.line(), r.doc.querySelector('.watch-search').classList.contains('is-pending'), r.doc.querySelectorAll('.results-fail').length];
    return out;
  },

  // Links to what is not there, to a decade that is not listed, and to a person who cannot be had just now.
  async badLinks() {
    const error = (status) => Object.assign(new Error(String(status)), { status });
    const open = async (at, status, categories) => {
      const p = await ready(ADMIN, '', at, categories);
      if (status) {
        p.calls.filter((c) => /explore\?|person/.test(c.url)).forEach((c) => c.reject(error(status)));
        await settle();
      }
      return p;
    };
    const look = (p) => ({ title: p.doc.querySelector('.explore-title').textContent, pill: p.$('watch-search-token').textContent,
      stopped: p.grids[0].stopped, filters: p.doc.querySelectorAll('.explore-filters').length, outline: p.doc.querySelector('.explore').classList.contains('is-unknown'),
      back: p.doc.querySelectorAll('.explore .grid-empty__link').length, status: p.status.textContent });
    const nope = await open('/watch?genre=nope', 404);
    const out = { genre: look(nope) };
    click(nope.doc, nope.doc.querySelector('.grid-empty__link'));
    await settle();
    out.away = [nope.url(), nope.shown()];
    out.person = look(await open('/watch?person=999999', 404));
    out.failed = look(await open('/watch?person=9001', 502));
    const broken = await open('/watch?genre=action', 502);
    out.broken = look(broken);
    const decade = await open('/watch?genre=action&decade=1910s');
    out.decade = [decade.url(), decade.calls.map((c) => c.url)];
    const late = load(ADMIN, '', '/watch?genre=action&decade=1910s', null);
    await settle();
    late.calls.filter((c) => c.url === '/api/watch/explore').forEach((c) => c.reject(error(502)));
    await settle();
    late.calls.filter((c) => c.url.includes('explore?'))[0].reject(error(400));
    await settle();
    out.late = [late.url(), late.calls.map((c) => c.url)];
    const listed = await open('/watch?genre=action&decade=1990s');
    out.listed = [listed.url(), listed.calls.length];
    return out;
  },

  // A card's preview does not stay over the sheet.
  async sheetOverPreview() {
    const p = await ready(ADMIN);
    let closed = 0;
    p.ctx.MMWatch.preview = { close() { closed++; } };
    p.doc.listeners.keydown.forEach((fn) => fn({ key: '/', preventDefault() {} }));
    return [closed, p.sheet.classList.contains('is-on')];
  },

  // Keys that belong to a composition: Safari's ends one with keyCode 229 and isComposing false.
  async composing() {
    const out = [];
    for (const own of [{ isComposing: true }, { keyCode: 229 }]) {
      const p = await ready(ADMIN);
      p.input.focus();
      await p.search('act');
      p.calls.length = 0;
      p.key('ArrowDown', own);
      const moved = p.doc.querySelectorAll('.is-picked').length;
      const held = [p.key('Escape', own), p.key('Enter', own)];
      await settle();
      out.push([moved, held, p.input.value, p.calls.length]);
      const c = await ready(ADMIN, '', '/watch?genre=action');
      c.fire('keydown', { key: 'Escape', preventDefault() {}, ...own });
      await settle();
      out.push(c.url());
    }
    return out;
  },

  // Any year chosen before the categories arrive stays chosen once they do.
  async anyYear() {
    const p = load(ADMIN, '', '/watch?genre=action&decade=1990s', null);
    const select = p.doc.querySelector('.explore-years__select');
    select.value = '';
    select.listeners.change.forEach((fn) => fn({}));
    p.calls.filter((c) => c.url === '/api/watch/explore').forEach((c) => c.resolve({ genres: GENRES, decades: DECADES }));
    await settle();
    return [select.value, p.url(), select.children.length];
  },

  // What the field says of its list: whether one shows, and which.
  async combobox() {
    const p = await ready(ADMIN);
    const said = () => [p.input.getAttribute('aria-expanded'), p.input.getAttribute('aria-controls'), !!p.$(p.input.getAttribute('aria-controls'))];
    const out = {};
    p.press();
    out.sheet = said();
    await p.search('act');
    out.suggestions = said();
    click(p.doc, p.doc.querySelector('.watch-suggestion--category'));
    await settle();
    out.category = said();
    await p.search('zzqx');
    await p.answer('/api/watch/search?', { items: [] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    out.nothing = said();
    // Where the titles among the suggestions do not show (the results have them).
    const q = await ready(ADMIN);
    const plain = Node.prototype.getClientRects;
    let narrow = true;
    Node.prototype.getClientRects = function () { return narrow && this.classList.contains('watch-suggestion--title') ? [] : plain.call(this); };
    const at = () => [q.input.getAttribute('aria-expanded'), q.input.getAttribute('aria-controls')];
    try {
      await q.search('orchard');
      await q.answer('/api/watch/search?', { items: [LIB] });
      await q.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      out.titles = at();
      narrow = false;
      q.fire('resize');
      out.wide = at();
    } finally {
      Node.prototype.getClientRects = plain;
    }
    return out;
  },

  // What loads on a category's and a person's page is said, after what the page is.
  async spoken() {
    const p = await ready(ADMIN, '', '/watch?genre=action');
    await p.answer('explore?genre=action', { tmdb_configured: true, genre: GENRES[0], items: [TM], total: 5, next_page: 2 });
    p.grids[0].opts.say('1 loaded.');
    const out = [p.status.textContent];
    p.grids[0].opts.fetchPage(2);
    p.grids[0].opts.say('1 more loaded.');
    out.push(p.status.textContent);
    const q = await ready(ADMIN, '', '/watch?person=9001');
    await q.answer('person/9001', { tmdb_configured: true, person: MARA, items: [TM], total: 1, in_library: 0, next_page: null });
    q.grids[0].opts.say('1 loaded.');
    out.push(q.status.textContent);
    return out;
  },

  // A search typed over an open grid, then emptied: back in the grid's own entry, the bar still in use.
  async emptiedOverGrid() {
    const out = [];
    for (const how of ['cross', 'typed', 'escape']) {
      const p = await ready(ADMIN, '', '/watch?all=movies&sort=az');
      const { address } = p.ctx.MMWatch;
      let shown = 0;
      const title = p.doc.body.appendChild(p.doc.createElement('h1'));
      address.layer({ name: 'grid', depth: 10, params: ['all', 'sort'], read: (h) => (h.params.get('q') || h.params.get('genre') ? null : h.params.get('all')),
        open: () => { shown++; p.browse.hidden = true; title.focus(); }, close: () => {} });
      p.ctx.scrollY = 120;
      p.input.focus();
      await p.search('orchard');
      p.ctx.scrollY = 0;
      p.input.focus();
      if (how === 'typed') p.type('');
      else if (how === 'cross') click(p.doc, p.$('watch-search-clear'));
      else p.key('Escape');
      await settle();
      out.push([p.url(), p.tab.index, shown, p.input.value, p.doc.activeElement === p.input, p.ctx.scrollY, p.sheet.classList.contains('is-on')]);
    }
    return out;
  },

  // The step back from an emptied search is never made: a later Back leaves the focus where it was put since.
  async emptiedStepLost() {
    const p = await ready(ADMIN, '', '/watch?all=movies');
    p.ctx.MMWatch.address.layer({ name: 'grid', depth: 10, params: ['all', 'sort'], read: (h) => (h.params.get('q') ? null : h.params.get('all')), open: () => {}, close: () => {} });
    const other = p.doc.body.appendChild(p.doc.createElement('button'));
    p.input.focus();
    await p.search('orchard');
    const { go } = p.ctx.history;
    p.ctx.history.go = () => {};
    p.type('');
    p.wait();
    p.ctx.history.go = go;
    other.focus();
    await p.back();
    return { url: p.url(), other: p.doc.activeElement === other };
  },

  // The categories come in while the arrows stand on one of the first sheet's options: their place is held, and so is the highlight's.
  async redrawnUnderTheHighlight() {
    const p = load(ADMIN, '', null, null);
    p.press();
    p.input.focus();
    p.key('ArrowDown');
    const held = () => ['.watch-tile--skel', '.watch-person--skel', '.watch-tries'].map((s) => p.sheet.querySelectorAll(s).length);
    const before = [p.input.getAttribute('aria-activedescendant'), p.sheet.querySelectorAll('.watch-sheet__title').map((n) => n.textContent), held()];
    await p.answer('/api/watch/explore', { tmdb_configured: true, genres: GENRES, decades: DECADES });
    const picked = p.doc.querySelector('.is-picked');
    const after = [p.input.getAttribute('aria-activedescendant'), picked && p.sheet.contains(picked), held()];
    p.key('Enter');
    const entered = p.input.value;
    // They could not be had: nothing is held open for them.
    const q = load(ADMIN, '', null, null);
    q.press();
    q.calls.filter((c) => c.url === '/api/watch/explore').forEach((c) => c.reject(new Error('502')));
    await settle();
    return { before, after, entered, failed: q.sheet.children.map((n) => n.className) };
  },

  // What the sheet holds a place for, from one visit to the next of one browser, then what it shows.
  async placeHeldForPeople() {
    const stored = {};
    const visit = async (reply) => {
      const p = load(ADMIN, '', null, null, { stored });
      p.press();
      p.input.focus();
      const titles = () => p.sheet.querySelectorAll('.watch-sheet__title').map((n) => n.textContent).includes('People in your library');
      const waiting = titles();
      if (reply) await p.answer('/api/watch/explore', { tmdb_configured: true, genres: GENRES, decades: DECADES, ...reply });
      else { p.calls.filter((c) => c.url === '/api/watch/explore').forEach((c) => c.reject(new Error('502'))); await settle(); }
      return [waiting, titles()];
    };
    const none = { people: [], library: { ready: true } };
    const some = { people: [{ person_id: 1, name: 'Mara', count: 3 }], library: { ready: true } };
    const out = [];
    for (const reply of [null, { people: [], library: { ready: false } }, none, some, none, none]) out.push(await visit(reply));
    return out;
  },

  // The categories themselves could not be had: what was typed may name one, so it is not said to match nothing.
  async categoriesFailed() {
    const answerBoth = async (p, people) => {
      await p.answer('/api/watch/search?', { items: [] });
      await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [], people });
    };
    const fail = async (p) => {
      p.calls.filter((c) => c.url === '/api/watch/explore' && !c.done).forEach((c) => { c.done = true; c.reject(new Error('502')); });
      await settle();
    };
    const text = (p) => { const n = p.doc.querySelector('.results-fail span'); return n && n.textContent; };
    const p = load(ADMIN, '', null, null);
    await fail(p);
    await p.search('action');
    await answerBoth(p);
    await fail(p);
    const out = { text: text(p), line: p.line(), status: p.status.textContent };
    const retry = p.doc.querySelector('.results-fail button');
    if (!retry) return out;
    click(p.doc, retry);
    await settle();
    await answerBoth(p);
    await p.answer('/api/watch/explore', { tmdb_configured: true, genres: GENRES, decades: DECADES });
    out.asked = p.calls[p.calls.length - 1].url;
    await p.answer('explore?genre=action', { items: [TM] });
    out.titles = p.titles('.watch-card');
    // Somebody it does name stays on the page.
    const who = load(ADMIN, '', null, null);
    await fail(who);
    await who.search('mara');
    await answerBoth(who, [MARA]);
    await fail(who);
    out.person = [text(who), who.suggested().length];
    // One letter names no category.
    const one = load(ADMIN, '', null, null);
    await fail(one);
    await one.search('z');
    await answerBoth(one);
    await fail(one);
    out.one = [text(one), one.line()];
    return out;
  },

  // A link is opened before the categories are in: nothing that could move is on the page until it is known what stands there.
  async drawnOnceKnown() {
    const cats = (ready) => ({ tmdb_configured: true, genres: GENRES, decades: DECADES, library: { ready }, people: [] });
    const reply = (ready) => ({ tmdb_configured: true, genre: GENRES[0], items: [TM], total: 12, next_page: null, library: { ready } });
    const seen = (p) => ['.explore-filters', '.explore-grid'].map((s) => !p.doc.querySelector(s).classList.contains('is-unknown'));
    const note = (p) => { const n = p.doc.querySelector('.explore-gathering'); return n && !n.classList.contains('is-unknown'); };
    const known = async (p, payload) => { p.calls.filter((c) => c.url === '/api/watch/explore')[0].resolve(payload); await settle(); };
    const out = {};
    const p = load(ADMIN, '', '/watch?genre=action', null);
    out.order = p.doc.querySelector('.explore-filters').children.map((n) => n.getAttribute('aria-label') || 'Years');
    out.any = [seen(p)];
    await known(p, cats(false));
    out.any.push(seen(p));

    const facts = p.doc.querySelector('.explore-facts');
    out.room = [facts.textContent, facts.getAttribute('data-room')];
    // Drawn without it, the choice of where does not push the years aside later.
    const where = () => p.doc.querySelectorAll('.explore-seg').filter((n) => n.getAttribute('aria-label') === 'Where')[0];
    out.late = [where().hidden];
    await p.answer('explore?genre=action', reply(true));
    out.late.push(where().hidden);

    const held = load(ADMIN, '', '/watch?genre=action&where=library', null);
    out.library = [seen(held)];
    await known(held, cats(false));
    out.library.push(seen(held), note(held));
    // The first page says otherwise: the line goes and its place stays.
    await held.answer('where=library', reply(true));
    out.library.push(note(held));

    // The first page alone is enough to know; a later word does not push the grid down.
    const page = load(ADMIN, '', '/watch?genre=action&where=library', null);
    await page.answer('where=library', reply(true));
    out.page = [seen(page)];
    await known(page, cats(false));
    out.page.push(note(page), page.doc.querySelectorAll('.explore-seg').filter((n) => n.getAttribute('aria-label') === 'Where')[0].hidden);

    const failed = load(ADMIN, '', '/watch?genre=action&where=library', null);
    failed.calls.forEach((c) => c.reject(new Error('502')));
    await settle();
    out.failed = seen(failed);
    return out;
  },

  // A person who could not be had is asked for anew by the next try.
  async personAgain() {
    const p = await ready(ADMIN, '', '/watch?person=9001');
    p.calls[0].reject(new Error('502'));
    await settle();
    const again = p.grids[0].opts.fetchPage(1).catch(() => ({ cards: [] }));
    await p.answer('person/9001', { tmdb_configured: true, person: MARA, items: [TM], total: 1, in_library: 0, next_page: null });
    return [p.calls.length, (await again).cards.length > 0, p.doc.querySelector('.explore-title').textContent];
  },

  // Anywhere or in the library: in the address like the other choices, offered once the library is known, and what a link to it
  // says before.
  async where() {
    const cats = (ready) => ({ tmdb_configured: true, genres: GENRES, decades: DECADES, library: { ready, known: 40, total: 42 }, people: [] });
    const seg = (p) => p.doc.querySelectorAll('.explore-seg').filter((n) => n.getAttribute('aria-label') === 'Where')[0];
    const choose = (p, v) => click(p.doc, p.doc.querySelectorAll('.explore-seg__item').filter((b) => b.getAttribute('data-value') === v)[0]);
    const reply = (over) => ({ tmdb_configured: true, genre: GENRES[0], items: [TM], total: 12, next_page: null, library: { ready: true, titles: 12 }, ...over });
    const p = await ready(ADMIN, '', '/watch?genre=action', cats(true));
    const out = { shown: !seg(p).hidden && !seg(p).classList.contains('is-unknown') };
    choose(p, 'library');
    out.asked = p.calls[p.calls.length - 1].url;
    out.address = [p.url(), p.tab.entries.length];
    const held = () => p.doc.querySelector('.explore-grid').classList.contains('is-held');
    out.held = [held()];
    await p.answer('where=library', reply({ where: 'library' }));
    out.held.push(held());
    out.said = p.doc.querySelector('.explore-facts').textContent;
    choose(p, 'anywhere');
    out.anywhere = [p.calls[p.calls.length - 1].url, p.url()];
    const linked = await ready(ADMIN, '', '/watch?genre=action&where=library', cats(true));
    out.heldLinked = linked.doc.querySelector('.explore-grid').classList.contains('is-held');
    out.linked = [linked.calls[0].url, seg(linked).querySelectorAll('[aria-pressed="true"]').map((b) => b.textContent)];
    out.bogus = (await ready(ADMIN, '', '/watch?genre=action&where=moon', cats(true))).calls[0].url;
    const unready = await ready(ADMIN, '', '/watch?genre=action', cats(false));
    await unready.answer('explore?genre=action', reply({ library: { ready: false, titles: 0 } }));
    out.hidden = [seg(unready).hidden, unready.doc.querySelectorAll('.explore-gathering').length];
    const early = await ready(ADMIN, '', '/watch?genre=action&where=library', cats(false));
    await early.answer('where=library', reply({ where: 'library', library: { ready: false, titles: 3 } }));
    const note = early.doc.querySelector('.explore-gathering');
    out.early = [seg(early).hidden, !!note];
    click(early.doc, note.querySelector('button'));
    await settle();
    out.out = [early.url(), early.calls[early.calls.length - 1].url, early.doc.querySelector('.explore-gathering') === null || early.doc.querySelector('.explore-gathering').hidden];
    // Another search leaves it behind.
    p.type('o');
    const left = await ready(ADMIN, '', '/watch?genre=action&where=library', cats(true));
    left.type('o');
    out.left = left.url();
    return out;
  },

  // The people the library has most of: in the sheet, and those often in a category beside it when it is suggested alone.
  async libraryPeople() {
    const who = (n, titles) => ({ person_id: 9000 + n, name: 'Person ' + n, profile_url: null, titles });
    const people = [1, 2, 3, 4, 5, 6, 7].map((n) => who(n, 9 - n));
    const genres = [{ ...GENRES[0], actors: [who(1, 4), who(2, 3), who(3, 2)] }, { ...GENRES[1], actors: [] }];
    const p = await ready(ADMIN, '', null, { tmdb_configured: true, genres, decades: DECADES, people, library: { ready: true } });
    p.press();
    await settle();
    const tiles = () => p.sheet.querySelectorAll('.watch-person').map((n) => n.children[1].textContent);
    const out = { sheet: tiles(), heads: p.sheet.querySelectorAll('.watch-sheet__title').map((n) => n.textContent),
      first: p.sheet.querySelector('.watch-person').textContent, option: p.sheet.querySelector('.watch-person').getAttribute('role') };
    click(p.doc, p.$('watch-option-person-9001'));
    await settle();
    out.opened = [p.url(), p.doc.querySelector('.explore-title').textContent];
    const q = await ready(ADMIN, '', null, { tmdb_configured: true, genres, decades: DECADES, people, library: { ready: true } });
    q.type('action');
    out.alone = q.suggested();
    q.type('sci-fi');
    out.none = q.suggested().length;
    q.type('action');
    click(q.doc, q.doc.querySelectorAll('.watch-suggestion--person')[0]);
    await settle();
    out.actor = [q.url(), q.doc.querySelector('.explore-title').textContent];
    const bare = await ready(ADMIN);
    bare.press();
    await settle();
    out.bareTiles = bare.sheet.querySelectorAll('.watch-tiles').map((n) => [n.getAttribute('aria-label'), n.children.map((b) => b.id.slice(19))]);
    out.bare = bare.sheet.querySelectorAll('.watch-sheet__title').map((n) => n.textContent);
    return out;
  },

  // Tab out of the field: the rows take it, though the sheet had put them out of reach.
  async tabOut() {
    const out = [];
    for (const shift of [false, true]) {
      const p = await ready({ ...VIEWER, tmdb_configured: true });
      p.press();
      p.input.focus();
      const before = p.browse.inert;
      const held = p.key('Tab', { shiftKey: shift });
      out.push([before, held, p.browse.inert, p.sheet.classList.contains('is-on')]);
    }
    return out;
  },

  // A category opened while the rows are still fading behind what was typed: they go all the same.
  async pageDuringFade() {
    const p = await ready(ADMIN);
    const finish = withMotion(p);
    try {
      p.type('action');
      click(p.doc, p.doc.querySelector('.watch-suggestion--category'));
      p.timers.filter((t) => t.on && t.ms === 150).forEach((t) => { t.on = false; t.fn(); });
      const out = [p.shown(), p.browse.classList.contains('is-off')];
      finish();
      return out;
    } finally {
      delete Node.prototype.animate;
    }
  },

  // A search typed over a category or a person, then emptied: their page again, the bar still in use.
  async emptiedOverPage() {
    const out = [];
    for (const [at, how] of [['/watch?genre=action&sort=newest', 'cross'], ['/watch?genre=action&sort=newest', 'typed'], ['/watch?person=9001', 'escape']]) {
      const p = await ready(ADMIN, '', at);
      p.input.focus();
      await p.search('orchard');
      if (how === 'typed') p.type('');
      else if (how === 'cross') click(p.doc, p.$('watch-search-clear'));
      else p.key('Escape');
      await settle();
      out.push([p.url(), p.tab.index, p.grids.length, p.doc.querySelector('.explore') !== null, p.doc.activeElement === p.input]);
    }
    return out;
  },

  // Letters on their way to a word are waited for; the word itself is searched for as any other.
  async composition() {
    const step = (p, value, isComposing) => { p.input.value = value; p.input.listeners.input.forEach((fn) => fn({ isComposing })); };
    const waits = (p) => p.timers.filter((t) => t.on).map((t) => t.ms);
    const p = await ready(ADMIN);
    step(p, 'n', true);
    step(p, '日', true);
    const out = { during: waits(p), calls: p.calls.length };
    p.input.listeners.compositionend.forEach((fn) => fn({}));
    out.ended = waits(p);
    p.wait();
    await settle();
    out.asked = p.calls.map((c) => c.url);
    // A phone composes every word: a pause in one searches.
    const q = await ready(ADMIN);
    step(q, 'orch', true);
    q.wait();
    await settle();
    out.paused = q.calls.length;
    // Over once it was searched for, and once a category has taken its place: nothing more.
    q.input.listeners.compositionend.forEach((fn) => fn({}));
    out.searched = waits(q).length;
    step(q, 'action', true);
    click(q.doc, q.doc.querySelector('.watch-suggestion--category'));
    await settle();
    q.input.listeners.compositionend.forEach((fn) => fn({}));
    await settle();
    out.page = [q.url(), q.$('watch-search-token').hidden];
    return out;
  },

  // Behind the open sheet the page holds still, and the cross shuts it: the field is empty, and what it holds stays.
  async sheetHolds() {
    const p = await ready(ADMIN);
    const html = p.doc.documentElement;
    const cross = p.$('watch-search-clear');
    const state = () => [html.classList.contains('has-sheet'), p.sheet.classList.contains('is-on'), cross.getAttribute('aria-label')];
    const out = [state()];
    p.press();
    p.input.focus();
    out.push(state());
    click(p.doc, cross);
    await settle();
    out.push(state().concat(p.doc.activeElement === p.input));
    // Opened again by typing and emptying: as any other time.
    p.press();
    p.input.focus();
    p.type('a');
    out.push(state());
    click(p.doc, cross);
    out.push(state());
    return out;
  },

  // Only words that fit the field stand in it: the label, or one word of it, and the examples that fit.
  async hintFits() {
    const out = {};
    const width = { chars: 18 };
    const page = (data) => {
      let tick = null;
      Object.defineProperties(Node.prototype, {
        clientWidth: { configurable: true, get() { return width.chars; } },
        scrollWidth: { configurable: true, get() { return Math.max(width.chars, this.textContent.length); } },
      });
      const p = load(data, '', null, undefined, { still: false, every: (fn) => { tick = fn; return 1; } });
      return { p, tick: () => tick && tick(), hint: () => p.$('watch-search-hint').textContent };
    };
    try {
      const narrow = page({ ...ADMIN, view: 'list' });
      out.list = [narrow.hint()];
      width.chars = 40;
      narrow.p.fire('resize');
      out.list.push(narrow.hint());
      width.chars = 24;
      const rows = page(ADMIN);
      out.rows = [rows.hint()];
      for (let i = 0; i < 4; i++) { rows.tick(); out.rows.push(rows.hint()); }
    } finally {
      delete Node.prototype.clientWidth;
      delete Node.prototype.scrollWidth;
    }
    return out;
  },

  // TMDB is asked too much: said at once, asked again by itself or by a press, and never twice for one answer.
  async tooMany() {
    const busy = () => Object.assign(new Error('429'), { status: 429 });
    const out = {};
    const says = (p) => { const n = p.doc.querySelector('.grid-fail span'); return n && n.textContent; };
    const waits = (p) => p.timers.filter((t) => t.on).map((t) => t.ms);
    const last = async (p, err) => { p.calls[p.calls.length - 1].reject(err); await settle(); };
    const reply = { tmdb_configured: true, genre: GENRES[0], person: MARA, items: [TM, { ...TM, ref: 'tmdb-movie-10' }], total: 2, in_library: 0, next_page: null };
    for (const at of ['genre=action', 'person=9001']) {
      const p = await ready(ADMIN, '', '/watch?' + at);
      await last(p, busy());
      const got = out[at] = { first: [says(p), waits(p), p.calls.length] };
      p.wait();
      await last(p, busy());
      got.second = [says(p), waits(p), p.calls.length, p.grids.length];
      // Pressed while it waits: asked once.
      const again = p.doc.querySelector('.grid-fail__retry');
      again.focus();
      click(p.doc, again);
      got.pressed = [waits(p), p.calls.length];
      await last(p, busy());
      p.wait();
      p.calls[p.calls.length - 1].resolve(reply);
      await settle();
      got.done = [says(p), waits(p), p.grids[0].set.length, p.doc.querySelectorAll('.is-unknown').length];
      got.focus = p.doc.activeElement.className;
      // Left while it waits: not asked for any more.
      const left = await ready(ADMIN, '', '/watch?' + at);
      await last(left, busy());
      left.type('o');
      got.left = waits(left).filter((ms) => ms !== 200);
    }
    const f = await ready(ADMIN, '', '/watch?genre=action');
    await f.answer('explore?genre=action', reply);
    const choose = (v) => click(f.doc, f.doc.querySelectorAll('.explore-seg__item').filter((b) => b.getAttribute('data-value') === v)[0]);
    choose('movie');
    await last(f, busy());
    out.filter = [says(f), f.calls.length, waits(f)];
    choose('tv');
    out.changed = waits(f);
    await last(f, new Error('502'));
    out.broken = [says(f), f.calls.length, waits(f)];
    // It ends some time.
    const e = await ready(ADMIN, '', '/watch?genre=action');
    for (let i = 0; i < 6; i++) { await last(e, busy()); out.ended = [says(e), waits(e)]; e.wait(); await settle(); }
    out.times = e.calls.length;
    return out;
  },
};

// Motion on: every move runs until finish() ends it.
function withMotion(p) {
  const live = [];
  p.ctx.matchMedia = () => ({ matches: false });
  Node.prototype.animate = function () { const a = { playState: 'running', cancel() {} }; live.push(a); return a; };
  return () => live.splice(0).forEach((a) => a.onfinish && a.onfinish());
}

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
