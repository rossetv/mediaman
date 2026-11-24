// Runs static/js/watch/search.js against the fake DOM and prints the outcome of one scenario as JSON.
// Usage: node watch_search_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/watch');
const { makeDocument, build, click, settle } = require('./fake_dom');

const LIB = { ref: 'movie-1', title: 'Orchard Keepers', href: '/watch/movie/1', subtitle: 'Movie', keep_id: '7', leaving_days: 3 };
const TM = { ref: 'tmdb-movie-9', kind: 'movie', title: 'Orchard Road', year: 2020, href: null, download_state: 'queued' };

/* A page with the Watch search shell; every request waits until the scenario answers it. */
function load(data) {
  const doc = makeDocument();
  const timers = [];
  const calls = [];
  const events = [];
  const viewHandlers = [];
  const urls = [];
  const loc = { pathname: '/watch', search: '', hash: '' };
  doc.dispatchEvent = (e) => events.push({ type: e.type, q: e.detail.q });
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Error, URLSearchParams, parseFloat,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() { this.aborted = true; } },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: () => ({ matches: true }),
    location: loc,
    history: { state: null, replaceState: (_s, _t, url) => urls.push(url) },
    addEventListener: (t, fn) => { if (t === 'mm:watch-view') viewHandlers.push(fn); },
    MM: {
      api: {
        get: (url) => new Promise((resolve, reject) => { calls.push({ url, resolve, reject }); }),
        messageOf: (_e, fallback) => fallback,
      },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' },
    ['div', { class: 'watch-search' },
      ['input', { id: 'watch-search', class: 'watch-search__input' }],
      ['button', { id: 'watch-search-clear', hidden: '' }],
      ['span', { id: 'watch-search-status' }]],
    ['div', { id: 'watch-browse' }],
    ['section', { id: 'watch-results' }, ['p', { id: 'watch-results-msg' }], ['div', { id: 'watch-grid' }]],
    ['script', { id: 'watch-page-data' }, JSON.stringify(data)]]));
  doc.getElementById('watch-results').hidden = true;
  doc.getElementById('watch-search').value = '';
  ['core.js', 'keep.js', 'search.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.MMWatch.search.init();
  const input = doc.getElementById('watch-search');
  const $ = (id) => doc.getElementById(id);
  return {
    ctx, doc, input, calls, events, urls, timers, loc, viewHandlers, $,
    results: $('watch-results'), browse: $('watch-browse'), status: $('watch-search-status'),
    type(value) {
      input.value = value;
      input.listeners.input.forEach((fn) => fn({}));
    },
    // Type and let the debounce fire.
    async search(value) {
      this.type(value);
      this.timers[this.timers.length - 1].fn();
      await settle();
    },
    async answer(urlPart, payload) {
      calls.filter((c) => c.url.includes(urlPart) && !c.done).forEach((c) => { c.done = true; c.resolve(payload); });
      await settle();
    },
    titles(sel) { return doc.querySelectorAll(sel).map((c) => c.querySelector('.watch-card__title').textContent); },
    heads: () => doc.querySelectorAll('.results-group__title').map((h) => h.textContent),
  };
}

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
      libraryRefs: p.doc.querySelectorAll('[data-title-ref]').map((c) => c.getAttribute('data-title-ref')),
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
    const pending = both.doc.getElementById('watch-results-msg').textContent;
    await both.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    const msg = both.doc.getElementById('watch-results-msg');
    const onlyTmdb = load(ADMIN);
    await onlyTmdb.search('orchard');
    await onlyTmdb.answer('/api/watch/search?', { items: [] });
    await onlyTmdb.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
    return {
      whileWaiting: pending,
      line: msg.querySelector('.watch-results__line').textContent,
      status: both.status.textContent,
      tmdbOnly: { msg: onlyTmdb.doc.getElementById('watch-results-msg').textContent, heads: onlyTmdb.heads() },
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
        line: none.doc.querySelector('.watch-results__line').textContent,
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
    const marked = p.doc.querySelectorAll('[data-title-ref]');
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
    out.tmdbOkInGroup = tmdb.doc.querySelectorAll('.results-group')[1].contains(tmdb.doc.activeElement);
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
    const msg = p.$('watch-results-msg');
    const out = {
      text: msg.textContent, alerts: p.doc.querySelectorAll('[role="alert"]').length,
      groups: p.doc.querySelectorAll('.results-group').length, status: p.status.textContent,
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
    const group = p.doc.querySelector('.results-group');
    return {
      waiting: waiting === null,
      status: p.status.textContent,
      line: none && none.textContent,
      above: none !== null && group !== null && p.doc.querySelector('.results-groups').childNodes[0].contains(none),
      heads: p.heads(),
      alerts: p.doc.querySelectorAll('[role="alert"]').length,
    };
  },

  // The rows and the results trade places by fading: out over --dur-fast, then in; reversing mid-fade stays clean.
  async viewFade() {
    const out = {};
    const p = load(ADMIN);
    p.ctx.matchMedia = () => ({ matches: false });
    const anims = [];
    const animate = (node) => (keyframes, opts) => {
      const a = { node, keyframes, opts, cancelled: false, cancel() { this.cancelled = true; } };
      anims.push(a);
      return a;
    };
    p.browse.animate = animate('browse');
    p.results.animate = animate('results');
    await p.search('orchard');
    await p.answer('/api/watch/search?', { items: [LIB] });
    out.during = { browse: p.browse.hidden === true, results: p.results.hidden, anims: anims.map((a) => [a.node, a.opts.duration]) };
    anims[0].onfinish();
    out.after = { browse: p.browse.hidden === true, results: p.results.hidden, fadeIn: p.results.classList.contains('fade-in'), cancelled: anims[0].cancelled };
    // Back to the rows, and reversed before the fade ends.
    p.type('');
    out.clearing = { anims: anims.length, node: anims[1].node, duration: anims[1].opts.duration };
    p.type('orchard');
    p.timers[p.timers.length - 1].fn();
    await p.answer('/api/watch/search?', { items: [LIB] });
    out.reversed = { results: p.results.hidden, browse: p.browse.hidden === true, cancelled: anims[1].cancelled, anims: anims.length };
    anims[1].onfinish && anims[1].onfinish();
    out.afterStale = { results: p.results.hidden, browse: p.browse.hidden === true };

    // Typing then clearing at once never leaves the results up.
    const q = load(ADMIN);
    q.ctx.matchMedia = () => ({ matches: false });
    const more = [];
    q.browse.animate = (k, o) => { const a = { cancel() { this.cancelled = true; } }; more.push(a); return a; };
    q.results.animate = q.browse.animate;
    await q.search('orchard');
    await q.answer('/api/watch/search?', { items: [LIB] });
    q.type('');
    out.quick = { browse: q.browse.hidden === true, results: q.results.hidden, cancelled: more[0].cancelled };
    // Reduced motion: a cut, nothing animated.
    const r = load(ADMIN);
    let ran = 0;
    r.browse.animate = () => { ran++; return {}; };
    await r.search('orchard');
    await r.answer('/api/watch/search?', { items: [LIB] });
    out.reduced = { ran, browse: r.browse.hidden === true, results: r.results.hidden };
    return out;
  },

  // Results replace each other with a cross-fade; whatever order the queries and replies come in, the end is one clean view.
  async settledAfterFade() {
    const live = [];
    // Motion is on once the first results are up: every fade runs until the scenario ends it.
    const animate = function () { const a = { cancel() {} }; live.push(a); return a; };
    const finish = () => live.splice(0).forEach((a) => a.onfinish && a.onfinish());
    const page = async () => {
      const p = load(ADMIN);
      await p.search('orchard');
      await p.answer('/api/watch/search?', { items: [LIB] });
      await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [TM] });
      p.ctx.matchMedia = () => ({ matches: false });
      return p;
    };
    const view = (p) => ({
      heads: p.heads(), titles: p.doc.querySelectorAll('.watch-card__title').map((t) => t.textContent), skeleton: p.doc.querySelectorAll('.watch-card--skel').length,
      stacks: p.results.children.filter((n) => n.style.display === 'grid').length, msg: p.$('watch-results-msg').textContent,
    });
    const out = {};
    const proto = require('./fake_dom').Node.prototype;
    proto.animate = animate;
    try {
      // Nothing matches while the old results are still fading out.
      const a = await page();
      await a.search('zzqx');
      await a.answer('/api/watch/search?', { items: [] });
      await a.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      finish();
      await settle();
      out.nothing = view(a);

      // The next query lands before the last fade is over.
      const b = await page();
      await b.search('tallow');
      await b.answer('/api/watch/search?', { items: [{ ...LIB, title: 'Tallow' }] });
      await b.search('harrow');
      await b.answer('/api/watch/search?', { items: [{ ...LIB, title: 'Harrow' }] });
      await b.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
      finish();
      await settle();
      out.chained = view(b);
    } finally {
      delete proto.animate;
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
    const p = load(ADMIN);
    p.loc.search = '?foo=1';
    p.loc.hash = '#title=movie-2';
    await p.search('orchard');
    const withQ = p.urls[p.urls.length - 1];
    await p.answer('/api/watch/search?', { items: [LIB] });
    await p.answer('/api/watch/search/tmdb', { tmdb_configured: true, items: [] });
    click(p.doc, p.$('watch-search-clear'));
    await settle();
    return {
      withQ, url: p.urls[p.urls.length - 1], value: p.input.value, focus: p.doc.activeElement === p.input,
      browse: p.browse.hidden === false, results: p.results.hidden, status: p.status.textContent,
      groups: p.doc.querySelectorAll('.results-group').length,
    };
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
    r.viewHandlers.forEach((fn) => fn({ detail: { view: 'rows' } }));
    await r.search('lantern');
    out.afterSwitch = { events: r.events.length, calls: r.calls.length, placeholder: r.input.placeholder };
    return out;
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
