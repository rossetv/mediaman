// Runs the Watch page's address (watch/address.js) against a fake history and prints the outcome of one scenario as
// JSON. Usage: node watch_address_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const v8 = require('v8');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build, settle } = require('./fake_dom');

/* A browser tab: its history and its storage outlive a reload. */
function makeTab(url) {
  return { entries: [{ url, state: null }], index: 0, stored: {}, calls: [] };
}

/* Load the page in *tab* with the layers the Watch page has. */
function open(tab, opts = {}) {
  const doc = makeDocument();
  const listeners = {};
  const timers = [];
  const log = [];
  const observers = [];
  const win = { scrollY: 0, tallest: opts.tallest === undefined ? 100000 : opts.tallest };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  const fire = (type, event) => (listeners[type] || []).slice().forEach((fn) => fn(event || {}));
  const scroll = (y) => { win.scrollY = Math.max(0, Math.min(win.tallest, y)); fire('scroll'); };
  const go = (by) => {
    const to = tab.index + by;
    if (to < 0 || to >= tab.entries.length || by === 0) return;
    tab.index = to;
    fire('popstate');
  };
  const ctx = {
    document: doc, console, JSON, URL, URLSearchParams, Object, Array, Math, Error, Infinity,
    setTimeout: (fn, ms) => { timers.push({ fn, ms, on: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].on = false; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    scrollTo: (o) => scroll(o.top),
    ResizeObserver: class { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() { this.off = true; } },
    sessionStorage: {
      getItem: (k) => { if (opts.noStorage) throw new Error('denied'); return k in tab.stored ? tab.stored[k] : null; },
      setItem: (k, v) => { if (opts.noStorage) throw new Error('denied'); tab.stored[k] = v; },
    },
    history: {
      scrollRestoration: 'auto',
      get state() { const s = tab.entries[tab.index].state; return s === null ? null : JSON.parse(JSON.stringify(s)); },
      pushState(state, _t, url) {
        tab.calls.push('push ' + url);
        tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state: JSON.parse(JSON.stringify(state)) });
        tab.index++;
      },
      replaceState(state, _t, url) { tab.entries[tab.index] = { url, state: JSON.parse(JSON.stringify(state)) }; },
      go(by) { tab.calls.push('go ' + by); if (!tab.lost) setImmediate(() => go(by)); },
    },
    location: {
      get pathname() { return here().pathname; },
      get search() { return here().search; },
      get hash() { return here().hash; },
    },
  };
  Object.defineProperty(ctx, 'scrollY', { get: () => win.scrollY });
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch/address.js'), 'utf8'), ctx);
  const address = ctx.MMWatch.address;
  const keep = (keys) => (h) => { const q = keys.map((k) => h.params.get(k)).filter(Boolean).join(' '); return q || null; };
  const add = (name, depth, params, hash, read) => address.layer({ name, depth, params, hash, read,
    open: (v, how) => { log.push('open ' + name + ' ' + v + ' ' + how); if (opts.fails === name) throw new Error('bad'); if (opts.onOpen) opts.onOpen(name, v, api); return opts.placed === name ? (scroll(opts.placeAt || 0), true) : undefined; },
    close: (how) => { log.push('close ' + name + ' ' + how); if (opts.fails === name) throw new Error('bad'); return opts.placed === name ? (scroll(opts.placeAt || 0), true) : undefined; } });
  const title = (h) => { const m = /^title=([a-z0-9-]+)$/.exec(h.hash); return m ? m[1] : null; };
  // Declared out of order on purpose: the depth decides.
  add('trailer', 60, [], false, (h) => (h.entry === 'trailer' && title(h) ? 'on' : null));
  add('title', 50, [], true, title);
  add('storage', 40, [], true, (h) => { const m = /^storage(-scheduled|-deleted)?$/.exec(h.hash); return m ? h.hash : null; });
  add('search', 30, ['q'], false, (h) => (h.params.get('view') === 'list' ? null : h.params.get('q') || null));
  add('explore', 20, ['genre', 'person', 'type', 'decade'], false, keep(['genre', 'person', 'type', 'decade']));
  add('grid', 10, ['all', 'sort'], false, (h) => (['movies', 'tv'].includes(h.params.get('all')) ? keep(['all', 'sort'])(h) : null));
  add('list', 5, ['view'], false, (h) => (h.params.get('view') === 'list' ? h.params.toString() : null));
  const api = {
    doc, address, log, timers, fire, scroll, win, observers, tab, ctx,
    took: () => log.splice(0),
    at: () => tab.entries[tab.index].url,
    step: async (by) => { go(by); await settle(); },
    settle,
    late: () => timers.filter((t) => t.on).forEach((t) => { t.on = false; t.fn(); }),
  };
  if (!opts.unstarted) address.start();
  return api;
}

const title = (p, ref) => p.address.push('title', p.address.url({}, 'title=' + ref));
const trailer = (p) => p.address.push('trailer', p.address.url({}));
const grid = (p, id) => p.address.push('grid', p.address.url({ all: id, sort: null }));

const scenarios = {
  async playLinks() {
    const follow = (url, href, type) => {
      const p = open(makeTab(url));
      const a = p.doc.body.appendChild(build(p.doc, ['a', { href }]));
      p.doc.listeners[type].forEach((fn) => fn({ target: a }));
      const to = new URL(a.getAttribute('href'), 'http://mm');
      return [to.pathname, to.searchParams.get('resume'), to.searchParams.get('from')];
    };
    const old = '/watch/play/7?resume=1&from=%2Fwatch%23title%3Dmovie-7';
    return [
      follow('/watch?genre=action&decade=1990s#title=movie-7', old, 'click'),
      follow('/watch?q=a%26b', old, 'auxclick'),
      follow('/watch?all=movies#storage', '/watch/play/7', 'contextmenu'),
      follow('/watch?all=movies', '/watch/movie/7', 'click'),
    ];
  },

  // Whatever opens closes a card's preview; a title's details grow out of it.
  async closesThePreview() {
    const p = open(makeTab('/watch'));
    let closed = 0;
    p.ctx.MMWatch.preview = { close() { closed++; } };
    const after = (name, url) => { p.address.push(name, url); return closed; };
    return [after('grid', '/watch?all=movies'), after('title', '/watch?all=movies#title=movie-1'), after(null, '/watch')];
  },

  async nestedBackAndForward() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies'); title(p, 'movie-1'); trailer(p);
    const pushed = p.took();
    const top = p.address.top();
    const back = [];
    for (let i = 0; i < 3; i++) { await p.step(-1); back.push([p.at(), ...p.took()]); }
    const forward = [];
    for (let i = 0; i < 3; i++) { await p.step(1); forward.push([p.at(), ...p.took()]); }
    await p.step(-3);
    return { pushed, top, back, forward, all: p.took() };
  },

  async closeStepsPastWhatStandsOnIt() {
    const p = open(makeTab('/watch?q=x'));
    grid(p, 'movies'); title(p, 'movie-1'); trailer(p);
    p.took(); p.tab.calls.length = 0;
    p.address.close('title');
    await settle();
    return { calls: p.tab.calls, at: p.at(), log: p.took(), top: p.address.top() };
  },

  async sharedLink() {
    const p = open(makeTab('/watch?all=movies&sort=az#title=movie-1'));
    const opened = p.took();
    p.address.close('title');
    const title = [p.at(), ...p.took()];
    p.address.back('grid');
    return { opened, title, grid: [p.at(), ...p.took()], entries: p.tab.entries.length, calls: p.tab.calls };
  },

  async sharedTitleWithATrailerOverIt() {
    const p = open(makeTab('/watch#title=movie-1'));
    trailer(p); p.took();
    p.address.close('title');
    await settle();
    return { calls: p.tab.calls, at: p.at(), index: p.tab.index, log: p.took() };
  },

  async closingWhatWasNotPushedLeavesTheEntryUnder() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies');
    p.address.replace('search', p.address.url({ q: 'amber' }));
    p.took(); p.tab.calls.length = 0;
    p.address.close('search');
    return { at: p.at(), calls: p.tab.calls, log: p.took(), top: p.address.top() };
  },

  async filtersReplaceAndViewsPush() {
    const p = open(makeTab('/watch'));
    p.address.push('explore', p.address.url({ genre: 'action' }));
    p.address.replace('explore', p.address.url({ type: 'movie' }));
    p.address.replace('explore', p.address.url({ decade: '1990' }));
    const entries = p.tab.entries.length;
    p.address.push('explore', p.address.url({ genre: null, type: null, decade: null, person: '7' }));
    const own = p.took();
    p.address.back('explore');
    await settle();
    const one = [p.at(), ...p.took()];
    await p.step(1);
    p.took();
    p.address.close('explore');
    await settle();
    return { entries, own, one, all: [p.at(), ...p.took()] };
  },

  async anotherViewClosesTheGrid() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies');
    p.address.push('list', '/watch?view=list&q=a');
    const list = p.took();
    p.address.push(null, '/watch');
    return { list, rows: p.took() };
  },

  async reloadKeepsTheWayBack() {
    const tab = makeTab('/watch');
    let p = open(tab);
    p.scroll(700);
    grid(p, 'movies');
    p.scroll(1500);
    title(p, 'movie-1');
    p.fire('pagehide');
    p = open(tab);
    const opened = p.took();
    const scroll = p.win.scrollY;
    tab.calls.length = 0;
    p.address.close('grid');
    await settle();
    return { opened, scroll, calls: tab.calls, at: p.at(), log: p.took(), after: p.win.scrollY };
  },

  async scrollAndFocusComeBack() {
    const p = open(makeTab('/watch'));
    const card = p.doc.body.appendChild(build(p.doc, ['a', { id: 'card' }]));
    p.scroll(900);
    card.focus();
    p.address.push('explore', p.address.url({ genre: 'action' }));
    p.doc.body.focus();
    p.scroll(300);
    await p.step(-1);
    const back = [p.win.scrollY, p.doc.activeElement.id];
    await p.step(1);
    return { back, forward: p.win.scrollY, restoration: p.ctx.history.scrollRestoration };
  },

  async focusALayerPutSomewhereStays() {
    const p = open(makeTab('/watch'));
    const [card, other] = ['card', 'other'].map((id) => p.doc.body.appendChild(build(p.doc, ['a', { id }])));
    card.focus();
    p.address.push('explore', p.address.url({ genre: 'action' }));
    other.focus();
    await p.step(-1);
    return { focus: p.doc.activeElement.id };
  },

  async aLayerThatPlacesTheScroll() {
    const p = open(makeTab('/watch'), { placed: 'grid', placeAt: 640 });
    p.scroll(900);
    grid(p, 'movies');
    await p.step(-1);
    return { scroll: p.win.scrollY };
  },

  async aPageThatGrows() {
    const p = open(makeTab('/watch'));
    p.scroll(900);
    p.address.push('explore', p.address.url({ genre: 'action' }));
    const grow = async (then) => {
      p.win.tallest = 200;
      await p.step(-1);
      const short = p.win.scrollY;
      if (then) then();
      p.win.tallest = 5000;
      p.observers.filter((o) => !o.off).forEach((o) => o.fn());
      const at = p.win.scrollY;
      await p.step(1);
      return [short, at];
    };
    const left = await grow();
    const taken = await grow(() => { p.fire('wheel'); p.scroll(50); });
    return { left, taken };
  },

  async aStepThatNeverComes() {
    const p = open(makeTab('/watch'));
    title(p, 'movie-1');
    p.tab.lost = true;
    p.took();
    p.address.close('title');
    const before = p.at();
    p.late();
    return { before, at: p.at(), log: p.took() };
  },

  async twoClosesInARow() {
    const p = open(makeTab('/watch'));
    title(p, 'movie-1'); trailer(p);
    p.took();
    p.address.back('trailer');
    p.address.close('title');
    await settle();
    return { at: p.at(), index: p.tab.index, log: p.took() };
  },

  async twoBacksInARow() {
    const tab = makeTab('/downloads');
    tab.entries.push({ url: '/watch', state: null });
    tab.index = 1;
    const p = open(tab);
    grid(p, 'movies');
    p.address.back('grid');
    p.address.back('grid');
    await settle();
    await settle();
    return { at: p.at(), calls: tab.calls.filter((c) => /^go/.test(c)) };
  },

  async aFollowedLink() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies');
    // The browser's own entry for a link in the page, with no state.
    p.tab.entries = p.tab.entries.slice(0, p.tab.index + 1).concat({ url: '/watch?all=movies#storage-deleted', state: null });
    p.took();
    await p.step(1);
    const opened = p.took();
    p.address.close('storage');
    const closed = [p.at(), ...p.took()];
    return { opened, closed };
  },

  async aLayerThatFails() {
    const p = open(makeTab('/watch?all=movies#title=movie-1'), { fails: 'grid' });
    let thrown = 0;
    p.timers.forEach((t) => { try { t.fn(); } catch (e) { thrown++; } });
    return { log: p.took(), thrown };
  },

  async aTitleUnderANewRef() {
    const p = open(makeTab('/watch'), { onOpen: (name, v, api) => {
      if (v === 'tmdb-movie-5') api.address.replace('title', api.address.url({}, 'title=movie-9'));
    } });
    title(p, 'tmdb-movie-5'); trailer(p);
    p.address.replace('title', p.address.url({}, 'title=movie-9'));
    const under = p.address.top();
    p.took();
    await p.step(-1);
    return { under, at: p.at(), value: p.address.value('title'), log: p.took() };
  },

  async samePushTwice() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies'); grid(p, 'movies');
    return { entries: p.tab.entries.length };
  },

  async todaysLinks() {
    const start = (url) => { const p = open(makeTab(url)); return [p.at(), ...p.took()]; };
    const p = open(makeTab('/watch?row=recommended&q=a b#title=movie-1'));
    return {
      links: ['/watch', '/watch?q=amber', '/watch?view=list&sort=size_desc&q=amber', '/watch#title=movie-1', '/watch?all=nothing', '/watch#storage-scheduled'].map(start),
      url: [p.address.url({ row: null }), p.address.url({ q: null }, ''), p.address.url({}, 'storage')],
    };
  },

  async beforeStart() {
    const p = open(makeTab('/watch#recommendation-7'), { unstarted: true });
    p.address.replace(null, p.address.url({}, 'title=rec-7'));
    const before = p.took();
    p.address.start();
    return { before, after: p.took() };
  },

  async noStorage() {
    const p = open(makeTab('/watch'), { noStorage: true });
    grid(p, 'movies');
    p.fire('pagehide');
    await p.step(-1);
    return { log: p.took() };
  },

  async typing() {
    const p = open(makeTab('/watch'));
    grid(p, 'movies');
    let writes = 0;
    const real = p.ctx.history.replaceState;
    p.ctx.history.replaceState = function (...a) { writes++; return real.apply(this, a); };
    for (const q of ['a', 'am', 'amb']) p.address.replace('search', p.address.url({ q }));
    return { writes, at: p.at() };
  },

  // What held the focus on a page that is drawn anew each time, round after round.
  async pagesThatGo() {
    const p = open(makeTab('/watch'));
    const held = [];
    const page = (params) => {
      const node = p.doc.body.appendChild(build(p.doc, ['a', {}]));
      node.focus();
      p.address.push('explore', p.address.url(params));
      node.remove();
      held.push(new WeakRef(node));
      p.doc.activeElement = p.doc.body;
    };
    for (let i = 0; i < 5; i++) {
      page({ genre: 'action', person: null });
      page({ genre: null, person: String(i) });
      await p.step(-1);
      await p.step(-1);
    }
    await p.step(1);
    await settle();
    v8.setFlagsFromString('--expose-gc');
    vm.runInNewContext('gc')();
    return { held: held.filter((r) => r.deref()).length };
  },

  // The same, on and on and never back: every entry stays, and what it had the focus on is gone.
  async pagesThatGoOn() {
    const p = open(makeTab('/watch'));
    const held = [];
    const page = (person) => {
      const node = p.doc.body.appendChild(build(p.doc, ['a', {}]));
      node.focus();
      p.address.push('explore', p.address.url({ person }));
      node.remove();
      held.push(new WeakRef(node));
      p.doc.activeElement = p.doc.body;
    };
    for (let i = 0; i < 5; i++) page(String(i));
    p.address.push('explore', p.address.url({ person: '9' }));
    await settle();
    v8.setFlagsFromString('--expose-gc');
    vm.runInNewContext('gc')();
    return { held: held.filter((r) => r.deref()).length };
  },

  // Going forward and back again and again leaves what is still under it known after a reload.
  async entriesThatGo() {
    const tab = makeTab('/watch');
    let p = open(tab);
    p.scroll(700);
    grid(p, 'movies');
    for (let i = 0; i < 60; i++) {
      p.address.push('explore', p.address.url({ genre: 'g' + i }));
      p.scroll(10);
      await p.step(-1);
    }
    p.fire('pagehide');
    p = open(tab);
    await p.step(-1);
    return { scroll: p.win.scrollY };
  },

  // Back over a link the browser followed itself: its entry stands after those the page pushed.
  async focusUnderAFollowedLink() {
    const p = open(makeTab('/watch'));
    const card = p.doc.body.appendChild(build(p.doc, ['a', { id: 'card' }]));
    grid(p, 'movies');
    card.focus();
    title(p, 'movie-1');
    p.tab.entries = p.tab.entries.slice(0, p.tab.index + 1).concat({ url: '/watch?all=movies#storage-deleted', state: null });
    await p.step(1);
    p.address.push('explore', '/watch?genre=action');
    p.doc.activeElement = p.doc.body;
    await p.step(-3);
    return { at: p.at(), focus: p.doc.activeElement.id || null };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
