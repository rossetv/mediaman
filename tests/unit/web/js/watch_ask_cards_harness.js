// Runs watch/ask.js and watch/search.js against the fake DOM and prints one scenario's outcome as
// JSON: what the AI's answer draws (reasons, order of the groups, titles so far).
// Usage: node watch_ask_cards_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/watch');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

Node.prototype.scrollIntoView = function () {};

const ASSIST = { account: 'a1', available: true, tonight: false, history: true, records: true, recent: [], personal: true, phrases: [], min_words: 2, left: 20, auto: true };
const Q = 'comedy movies from 2026';
const card = (n, owned, over) => ({
  ref: 'tmdb-movie-' + n, library_ref: owned ? 'movie-' + n : null, in_library: owned, kind: 'movie', title: 'Film ' + n,
  year: 2026, minutes: 90, reason: 'It is funny.', href: owned ? '/watch/movie/' + n : null, play_href: owned ? '/watch/play/' + n : null,
  poster_url: '/api/poster/' + n, download_state: null, ...over,
});

function load() {
  const doc = makeDocument();
  const timers = [];
  const calls = [];
  const on = {};
  const tab = { entries: [{ url: '/', state: null }], index: 0 };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).forEach((fn) => fn(e));
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Error, Infinity, URLSearchParams, parseFloat,
    setTimeout: (fn, ms) => { if (!ms) { setImmediate(fn); return -1; } timers.push({ fn, ms, on: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].on = false; },
    setInterval: () => 0,
    clearInterval: () => {},
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    Image: class {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1' }),
    matchMedia: (q) => ({ matches: !/coarse/.test(q), addEventListener() {} }),
    innerWidth: 1280,
    innerHeight: 800,
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} disconnect() {} },
    ResizeObserver: class { observe() {} disconnect() {} },
    scrollY: 0,
    scrollTo() {},
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    Date,
    location: {
      get pathname() { return here().pathname; },
      get search() { return here().search; },
      get hash() { return here().hash; },
      replace() {},
    },
    history: {
      get state() { return tab.entries[tab.index].state; },
      pushState(state, _t, url) { tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state }); tab.index++; },
      replaceState(state, _t, url = tab.entries[tab.index].url) { tab.entries[tab.index] = { url, state }; },
      go() {},
    },
    addEventListener: (t, fn) => { (on[t] = on[t] || []).push(fn); },
    removeEventListener() {},
    MM: {
      api: {
        get: (url) => (url === '/api/watch/explore' ? Promise.resolve({ tmdb_configured: true, genres: [], decades: [] })
          : url === '/api/search/assist' ? Promise.resolve({ ...ASSIST })
          : new Promise((resolve, reject) => { calls.push({ url, resolve, reject }); })),
        post: (url, body) => new Promise((resolve, reject) => { calls.push({ method: 'POST', url, body, resolve, reject }); }),
        delete: (url) => new Promise((resolve, reject) => { calls.push({ method: 'DELETE', url, resolve, reject }); }),
        messageOf: (_e, fallback) => fallback,
      },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  doc.body.appendChild(build(doc, ['header', { class: 'nav-glass wbar' },
    ['a', { class: 'brand', id: 'brand', href: '/' }],
    ['div', { class: 'watch-search' },
      ['span', { id: 'watch-search-token', hidden: '' }],
      ['span', { class: 'watch-search__field' }, ['span', { id: 'watch-search-hint' }, 'Search'],
        ['input', { id: 'watch-search', class: 'watch-search__input', value: '', role: 'combobox' }]],
      ['button', { id: 'watch-search-clear' }]],
    ['div', { id: 'watch-sheet' }],
    ['span', { id: 'watch-search-status' }],
    ['div', { class: 'nav-end', id: 'end' }, ['button', { class: 'space-meter', id: 'meter' }], ['button', { class: 'profile-btn', id: 'profile' }]]]));
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' },
    ['div', { id: 'watch-veil' }],
    ['div', { id: 'watch-browse' }, ['div', { id: 'watch-downloading' }]],
    ['section', { id: 'watch-results' }],
    ['script', { id: 'watch-page-data' }, JSON.stringify({ is_admin: true, view: 'rows', tmdb_configured: true })]]));
  const input = doc.getElementById('watch-search');
  input.select = () => {};
  input.value = '';
  doc.getElementById('watch-results').hidden = true;
  ['../core/dom.js', '../modal-a11y.js', 'core.js', 'address.js', 'keep.js', 'search.js', 'ask.js', 'explore.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.MMWatch.grid = { list: (host) => ({ tail: host.appendChild(doc.createElement('div')), set() {}, busy() {}, stop() {} }) };
  ctx.MMWatch.search.init();
  ctx.MMWatch.address.start();
  const p = { ctx, doc, input, calls, timers };
  p.reply = async (method, urlPart, payload) => {
    calls.filter((c) => (c.method || 'GET') === method && c.url.includes(urlPart) && !c.done).forEach((c) => { c.done = true; c.resolve(payload); });
    await settle();
  };
  p.poll = async (payload) => {
    timers.filter((t) => t.on && t.ms <= 1200).forEach((t) => { t.on = false; t.fn(); });
    await settle();
    await p.reply('GET', '/runs/', payload);
  };
  p.ask = async (q) => {
    input.value = q;
    input.listeners.input.forEach((fn) => fn({}));
    const e = { key: 'Enter', target: input, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    input.listeners.keydown.forEach((fn) => fn(e));
    await settle();
  };
  p.cards = () => doc.querySelectorAll('.ask .watch-card__link').map((n) => n.querySelector('.watch-card__title').textContent);
  p.whys = () => doc.querySelectorAll('.ask .watch-card__why').map((n) => ({ text: n.textContent, bold: n.children.map((c) => c.tagName + ':' + c.textContent), kids: n.children.length }));
  p.outlines = () => doc.querySelectorAll('.ask .ask__skel').length;
  p.start = async () => {
    await settle();
    await p.ask(Q);
    await p.reply('POST', '/ask', { status: 'running', run_id: 7 });
  };
  return p;
}

const scenarios = {
  // A reason is plain text under the card; "Because you liked X" leads it in bold.
  async reasons() {
    const p = load();
    await p.start();
    await p.poll({ status: 'done', owned: [
      card(1, true, { reason: 'Because you liked Nine Locks: the same dry humour.', reason_lead: 'Because you liked Nine Locks' }),
      card(2, true, { reason: '<img src=x onerror=alert(1)>' }),
      card(3, true, { reason: 'Because you watched Orchard: <b>sharp</b> and warm.', reason_lead: 'Because you watched Orchard' }),
      card(4, true, { reason: '' }),
    ], others: [], chips: [], instead: [], lead: 'owned' });
    const imgs = p.doc.querySelectorAll('.ask .watch-card__why img').length;
    return { whys: p.whys(), imgs };
  },
  // The lead the server names is bolded whole, whatever it holds; without one the reason is plain.
  async leads() {
    const p = load();
    await p.start();
    await p.poll({ status: 'done', owned: [
      card(1, true, { reason: 'Because you liked Star Wars: Episode IV: a space saga.', reason_lead: 'Because you liked Star Wars: Episode IV' }),
      card(2, true, { reason: 'Because you liked Nine Locks: the same dry humour.', reason_lead: null }),
      card(3, true, { reason: 'Because you liked Nine Locks: the same dry humour.' }),
      card(4, true, { reason: 'A heist done right.', reason_lead: 'Because you liked Nine Locks' }),
    ], others: [], chips: [], instead: [], lead: 'owned' });
    return { whys: p.whys() };
  },
  async leadOwned() {
    const p = load();
    await p.start();
    await p.poll({ status: 'done', owned: [card(1, true)], others: [card(2, false)], chips: ['Funnier'], instead: [], lead: 'owned' });
    return { cards: p.cards() };
  },
  async leadHolds() {
    const p = load();
    await p.start();
    await p.poll({ status: 'done', owned: [card(1, true)], others: [card(2, false)], chips: ['Funnier'], instead: [], lead: 'others' });
    const out = { before: p.cards() };
    const chip = p.doc.querySelectorAll('.ask .refine .chip')[0];
    click(p.doc, chip);
    await settle();
    out.sent = p.calls.filter((c) => c.method === 'POST' && !c.done).map((c) => c.body.refine || c.body.chip || c.body);
    await p.reply('POST', '/ask', { status: 'running', run_id: 8 });
    await p.poll({ status: 'running', run_id: 8, stage: 'checking', owned: [card(1, true)], others: [], lead: 'owned' });
    out.during = p.cards();
    await p.poll({ status: 'done', owned: [card(1, true)], others: [card(2, false)], chips: ['Funnier'], instead: [], lead: 'owned' });
    out.after = p.cards();
    return out;
  },
  // A running poll draws the titles checked so far with outlines after them.
  async partial() {
    const p = load();
    await p.start();
    const out = { none: [p.cards(), p.outlines()] };
    await p.poll({ status: 'running', run_id: 7, stage: 'checking', owned: [], others: [card(2, false)], lead: 'others' });
    out.one = [p.cards(), p.outlines(), p.whys().map((w) => w.text)];
    await p.poll({ status: 'running', run_id: 7, stage: 'checking', owned: [card(1, true)], others: [card(2, false), card(3, false)], lead: 'others' });
    out.three = [p.cards(), p.outlines()];
    await p.poll({ status: 'done', owned: [card(1, true)], others: [card(2, false), card(3, false)], chips: [], instead: [], lead: 'others' });
    out.done = [p.cards(), p.outlines()];
    return out;
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
