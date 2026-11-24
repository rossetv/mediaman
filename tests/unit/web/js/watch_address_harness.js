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
  const frames = [];
  const win = { scrollY: 0, tallest: opts.tallest === undefined ? 100000 : opts.tallest };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  const fire = (type, event) => (listeners[type] || []).slice().forEach((fn) => fn(event || {}));
  const scroll = (y) => {
    const was = win.scrollY;
    win.scrollY = Math.max(0, Math.min(win.tallest, y));
    if (win.scrollY !== was) fire('scroll');
  };
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
    requestAnimationFrame: (fn) => frames.push(fn),
    innerHeight: 900,
    Date: { now: () => 0 },
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
  // Too short for its scroll, the page moves at once.
  Object.defineProperty(ctx, 'scrollY', { get: () => (win.scrollY = Math.min(win.scrollY, win.tallest)) });
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
    frame: (n) => { for (let i = 0; i < (n || 1); i++) frames.splice(0).forEach((fn) => fn()); },
    late: (ms) => timers.filter((t) => t.on && !(t.ms > ms)).forEach((t) => { t.on = false; t.fn(); }),
  };
  if (!opts.unstarted) address.start();
  return api;
}

const title = (p, ref) => p.address.push('title', p.address.url({}, 'title=' + ref));
const trailer = (p) => p.address.push('trailer', p.address.url({}));
const grid = (p, id) => p.address.push('grid', p.address.url({ all: id, sort: null }));

/* A row of *id* with *width* px of cards, seen through 100. */
function rowOf(p, id, width) {
  const row = p.doc.body.appendChild(build(p.doc, ['section', { 'data-row-id': id }, ['div', { class: 'watch-row__scroll' }]]));
  const s = row.children[0];
  let left = 0;
  s.width = width;
  s.style = {};
  Object.defineProperty(s, 'clientWidth', { get: () => (s.away ? 0 : 100) });
  Object.defineProperty(s, 'scrollLeft', { get: () => left });
  s.scrollTo = (o) => {
    const was = left;
    left = Math.max(0, Math.min(s.width - 100, o.left));
    if (left === was) return;
    (p.doc.listeners.scroll || []).forEach((fn) => fn({ target: s }));
    (s.listeners.scroll || []).forEach((fn) => fn({ target: s }));
  };
  s.drag = (to) => {
    p.doc.listeners.wheel.forEach((fn) => fn({ type: 'wheel', target: s, deltaX: 9, deltaY: 1 }));
    s.scrollTo({ left: to });
  };
  return s;
}

/* A page of three rows left for the player, and loaded again at the same entry. */
function comeBack() {
  const tab = makeTab('/watch');
  let p = open(tab);
  ['movies', 'recent', 'later'].forEach((id) => rowOf(p, id, 2000).drag(1148));
  p.fire('pagehide');
  return open(tab, { unstarted: true });
}

const scenarios = {
  async rowsComeBack() {
    const p = comeBack();
    const movies = rowOf(p, 'movies', 2000);
    const recent = rowOf(p, 'recent', 600);
    const hidden = rowOf(p, 'later', 2000);
    hidden.away = true;
    p.address.start();
    const first = [movies, recent, hidden].map((s) => s.scrollLeft);
    p.frame(4);
    recent.width = 2000; // its next pages are in
    hidden.away = false;
    p.observers.forEach((o) => { if (!o.off) o.fn(); });
    const then = [movies, recent, hidden].map((s) => s.scrollLeft);
    movies.scrollTo({ left: 700 }); // Safari's own doing
    p.frame(4);
    const held = movies.scrollLeft;
    p.late(1000);
    movies.scrollTo({ left: 10 });
    p.observers.forEach((o) => { if (!o.off) o.fn(); });
    const other = open(makeTab('/watch'), { unstarted: true });
    const fresh = rowOf(other, 'movies', 2000);
    other.address.start();
    return { first, then, held, own: movies.scrollLeft, fresh: fresh.scrollLeft };
  },

  // A row that loads after the page is put back once it is there; one the person scrolls is theirs.
  async rowsThatComeLate() {
    const p = comeBack();
    p.address.start();
    const movies = rowOf(p, 'movies', 600);
    const recent = rowOf(p, 'recent', 600);
    p.address.putRowsBack(p.doc);
    movies.drag(500);
    p.frame(4);
    movies.width = recent.width = 2000;
    p.observers.forEach((o) => { if (!o.off) o.fn(); });
    return { movies: movies.scrollLeft, recent: recent.scrollLeft };
  },

  // Scroll that is still growing back is waited for as long as the page grows.
  async aSlowPageGrowsBack() {
    const tab = makeTab('/watch');
    let p = open(tab);
    p.scroll(5000);
    p.fire('pagehide');
    p = open(tab, { tallest: 900 });
    const waits = () => p.timers.filter((t) => t.on).length;
    for (const tall of [2000, 4000]) {
      const old = p.timers.filter((t) => t.on)[0];
      p.frame(4);
      p.win.tallest = tall;
      p.observers[0].fn();
      p.frame(4);
      if (old.on && old.ms < 5000) { old.on = false; old.fn(); }
    }
    p.frame(4);
    p.win.tallest = 9000;
    if (!p.observers[0].off) p.observers[0].fn();
    const y = p.win.scrollY;
    p.late();
    return { y, waits: waits() };
  },

  // Safari moves the page as what it waits for takes the place of its skeleton; the person may too.
  async aPageMovedWhileItFills() {
    const come = () => {
      const tab = makeTab('/watch');
      let p = open(tab);
      p.scroll(2400);
      p.fire('pagehide');
      p = open(tab, { unstarted: true });
      const slot = p.doc.body.appendChild(build(p.doc, ['div', { id: 'watch-discover-slot', 'aria-busy': 'true' }]));
      p.address.start();
      return { p, slot };
    };
    const { p, slot } = come();
    p.frame(4);
    p.late(1000);
    p.late(1000);
    p.scroll(1460);
    p.frame(4);
    const swapping = p.win.scrollY;
    slot.removeAttribute('aria-busy');
    p.frame(4);
    p.scroll(1500);
    p.frame(4);
    const settled = p.win.scrollY;
    p.frame(4);
    p.late(1000);
    p.scroll(1600);
    const later = p.win.scrollY;
    const taken = ['wheel', 'touchstart', 'mousedown', 'keydown', 'click'].map((type) => {
      const q = come().p;
      q.fire(type);
      q.scroll(800);
      q.frame(4);
      return q.win.scrollY;
    });
    return { swapping, settled, later, taken, anchor: p.ctx.document.documentElement.style.overflowAnchor };
  },

  // What scrolls frame after frame is the person, whatever they scroll with; a page that never settles is let go.
  async aPageTheyScrollWithoutAnEvent() {
    const come = () => {
      const tab = makeTab('/watch');
      let p = open(tab);
      p.scroll(2400);
      p.fire('pagehide');
      p = open(tab, { unstarted: true });
      p.doc.body.appendChild(build(p.doc, ['div', { id: 'watch-discover-slot', 'aria-busy': 'true' }]));
      p.address.start();
      p.frame(4);
      return p;
    };
    const p = come();
    [2300, 2100, 1800, 1500].forEach((y) => { p.scroll(y); p.frame(); });
    p.frame(4);
    const run = p.win.scrollY;
    p.fire('pagehide');
    const kept = open(p.tab).win.scrollY;
    const q = come();
    q.scroll(2300); q.frame(2); q.scroll(2350); q.frame(4);
    const shaken = q.win.scrollY;
    q.late(5000);
    q.scroll(700);
    q.frame(4);
    return { run, kept, shaken, bounded: q.win.scrollY };
  },

  // Safari scrolls a row nobody touched; a mouse or a finger passing over it on its way down the page is no touch.
  async aRowThatScrollsByItself() {
    const p = open(makeTab('/watch'), { unstarted: true });
    const s = rowOf(p, 'recommended', 2000);
    p.address.start();
    const snap = [s.style.scrollSnapType];
    s.scrollTo({ left: 492 });
    p.frame(4);
    const held = s.scrollLeft;
    p.late(1000);
    snap.push(s.style.scrollSnapType);
    const q = open(makeTab('/watch'), { unstarted: true });
    const other = rowOf(q, 'recommended', 2000);
    q.address.start();
    other.drag(60);
    snap.push(other.style.scrollSnapType);
    const say = (type, e) => (p.doc.listeners[type] || []).forEach((fn) => fn({ type, target: s, ...e }));
    say('wheel', { deltaX: 0, deltaY: 90 });
    say('touchstart', { touches: [{ clientX: 100, clientY: 400 }] });
    say('touchmove', { touches: [{ clientX: 104, clientY: 300 }] });
    s.scrollTo({ left: 492 });
    p.fire('pagehide');
    const back = open(p.tab, { unstarted: true });
    const again = rowOf(back, 'recommended', 2000);
    back.address.start();
    const own = [{ clientX: 40, clientY: 396 }];
    say('touchmove', { touches: own });
    s.scrollTo({ left: 164 });
    p.fire('pagehide');
    const last = open(p.tab, { unstarted: true });
    const theirs = rowOf(last, 'recommended', 2000);
    last.address.start();
    return { held, snap, alone: again.scrollLeft, theirs: theirs.scrollLeft };
  },

  async aRowTheyScrollWithoutAnEvent() {
    const p = comeBack();
    const s = rowOf(p, 'movies', 2000);
    p.address.start();
    [1000, 800, 600].forEach((x) => { s.scrollTo({ left: x }); p.frame(); });
    p.frame(4);
    return { left: s.scrollLeft };
  },

  // Around a swap the page is put back to what the person saw, unless they scroll on.
  async aSwap() {
    const go = (opts, then) => {
      const p = open(makeTab('/watch'));
      const slot = p.doc.body.appendChild(build(p.doc, ['div', { id: 'watch-discover-slot' }, ['section', { 'data-row-id': 'trending' }]]));
      const row = p.doc.body.appendChild(build(p.doc, ['section', { 'data-row-id': 'movies' }]));
      const at = { row: opts.rowAt };
      row.getBoundingClientRect = () => ({ top: at.row - p.win.scrollY, bottom: at.row + 300 - p.win.scrollY, height: 300 });
      p.scroll(2000);
      p.address.hold(slot);
      at.row += opts.shifts || 0;
      p.scroll(1152);
      p.frame(4);
      if (then) then(p);
      return p.win.scrollY;
    };
    const top = open(makeTab('/watch'));
    top.address.hold(top.doc.body);
    top.scroll(90);
    top.frame(4);
    return {
      back: go({ rowAt: 9000 }),
      withTheRow: go({ rowAt: 2200, shifts: -60 }),
      theirs: go({ rowAt: 9000 }, (p) => { p.fire('wheel'); p.scroll(1700); p.frame(4); }),
      fromTheTop: top.win.scrollY,
    };
  },

  // A row off the screen is still waited for while there is more of it to load.
  async aRowOffTheScreen() {
    const p = comeBack();
    const movies = rowOf(p, 'movies', 600);
    movies.parentNode.setAttribute('data-row-more', '');
    p.address.start();
    p.late();
    p.frame(4);
    movies.width = 2000;
    p.observers.forEach((o) => { if (!o.off) o.fn(); });
    return { left: movies.scrollLeft };
  },

  // A title opened from a card under one ref, its address moved to another; the page is loaded again over it.
  async focusAfterTheTitleOfALoadedPage() {
    const go = async (nodes) => {
      const tab = makeTab('/watch?genre=action');
      let p = open(tab);
      title(p, 'tmdb-movie-9');
      p.address.replace('title', p.address.url({}, 'title=movie-7'));
      p.fire('pagehide');
      p = open(tab);
      nodes.forEach((n) => p.doc.body.appendChild(build(p.doc, n)));
      p.address.close('title');
      await settle();
      return [p.at(), p.doc.activeElement.id || null];
    };
    const heading = ['h1', { id: 'heading', class: 'explore-title', tabindex: '-1' }];
    const shared = open(makeTab('/watch?genre=action#title=movie-7'));
    shared.doc.body.appendChild(build(shared.doc, heading));
    shared.address.close('title');
    return {
      card: await go([heading, ['a', { id: 'card', 'data-title-ref': 'tmdb-movie-9' }]]),
      heading: await go([heading, ['a', { id: 'other', 'data-title-ref': 'tmdb-movie-8' }]]),
      rows: await go([]),
      shared: shared.doc.activeElement.id,
    };
  },

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
      p.frame(4);
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
