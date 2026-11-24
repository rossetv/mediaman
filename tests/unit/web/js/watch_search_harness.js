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

/* A page with the Watch search shell; every request waits until the scenario answers it. The categories are there at once, unless the scenario answers them too (null). */
function load(data, opened = '', at = null, categories = { tmdb_configured: true, genres: GENRES, decades: DECADES }) {
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
    setInterval: () => 0,
    clearInterval: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() { this.aborted = true; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1' }),
    matchMedia: () => ({ matches: true }),
    innerHeight: 800,
    categories,
    scrollY: 0,
    scrollTo: (o) => { ctx.scrollY = o.top; },
    sessionStorage: { getItem: () => null, setItem() {} },
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
      replaceState(state, _t, url) { tab.entries[tab.index] = { url, state }; urls.push(url); },
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
        ['input', { id: 'watch-search', class: 'watch-search__input', value: opened }]],
      ['button', { id: 'watch-search-clear' }]],
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
      const g = { opts, set: [], stopped: false, busy: 0, titles: [] };
      const add = (got) => { g.titles = got.cards.map((c) => c.textContent); g.next = got.next; };
      opts.fetchPage(opts.next).then(add, () => { g.failed = true; });
      grids.push(g);
      return {
        set: (content, how) => { g.set.push([content.cards.length, content.next, how.move]); if (content.cards.length) add(content); },
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
    await tmdb.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    out.tmdbOk = active(tmdb);
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
      { ...TM, ref: 'tmdb-movie-2', title: 'Held', in_library: true, library_ref: 'movie-2' },
      { ...TM, ref: 'tmdb-movie-3', title: 'Not held', download_state: null },
    ] });
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

  // "/" goes to the field, but not from another field, with a modifier or behind a dialog.
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
    // A failure leaves the asking to the grid, which says so and offers to try again.
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
    // Asked for again, it is in memory.
    p.ctx.MMWatch.explore.person('9001', 1);
    out.calls = p.calls.length;
    return out;
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
    const p = load({ ...ADMIN, view: 'list' });
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
    out.afterSwitch = { events: r.events.length, calls: r.calls.length, placeholder: r.input.placeholder };
    return out;
  },

  // A search that runs before the categories are in does not say there is nothing, and lists the category once they are.
  async lateCategories() {
    const p = load(ADMIN, 'popular action movies', null, null);
    await p.answer('/api/watch/search?', { items: [] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    const out = { waiting: [p.line(), p.doc.querySelector('.watch-search').classList.contains('is-pending')] };
    const all = { tmdb_configured: true, genres: GENRES, decades: DECADES };
    const asked = p.calls.filter((c) => c.url === '/api/watch/explore')[0];
    asked.done = true;
    asked.resolve(all);
    await settle();
    out.asked = p.calls.filter((c) => c.url.includes('explore?')).map((c) => c.url);
    out.suggested = p.suggested()[0];
    await p.answer('/api/watch/explore?', { items: [{ ...TM, title: 'Held' }] });
    out.titles = p.titles('.watch-card');
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
    out.without = r.line();
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
