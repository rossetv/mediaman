// Runs Downloading on Watch (watch/dlgrid.js) with the row it grows out of, the address and, for
// an admin, the abandon dialog, against the fake DOM with a fake history and a stepped clock, and
// prints every scenario's outcome as JSON.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const SOURCES = ['core/dom.js', 'core/api.js', 'core/modal.js', 'modal-a11y.js', 'downloading.js', 'watch/core.js', 'watch/address.js',
  'watch/downloading.js', 'watch/dlgrid.js'].map((name) => [name, new vm.Script(fs.readFileSync(path.join(JS, name), 'utf8'), { filename: name })]);
const ABANDON = new vm.Script(fs.readFileSync(path.join(JS, 'dl-abandon.js'), 'utf8'), { filename: 'dl-abandon.js' });

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', !!on); },
});
Object.defineProperty(Node.prototype, 'dataset', {
  get() {
    const el = this;
    const attr = (k) => 'data-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
    return new Proxy({}, {
      get: (_t, k) => { const v = el.getAttribute(attr(k)); return v === null ? undefined : v; },
      set: (_t, k, v) => { el.setAttribute(attr(k), v); return true; },
    });
  },
});
/* What CSS hides takes no room and no focus: [hidden], and the rows while the grid has the page. */
Node.prototype.getClientRects = function () {
  if (!this.ownerDocument.body.contains(this)) return [];
  for (let a = this; a && a.nodeType === 1; a = a.parentNode) {
    if (a.hasAttribute('hidden')) return [];
    const up = a.parentNode;
    if (up && up.id === 'watch-browse' && up.classList.contains('has-dl') && !a.classList.contains('dlg')) return [];
  }
  return [this.getBoundingClientRect()];
};
Node.prototype.focus = function () { if (this.getClientRects().length) this.ownerDocument.activeElement = this; };
Node.prototype.remove = (function (remove) {
  return function () {
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    remove.call(this);
  };
})(Node.prototype.remove);
Node.prototype.toggleAttribute = function (k, on) {
  const want = on === undefined ? !this.hasAttribute(k) : !!on;
  if (want) this.setAttribute(k, ''); else this.removeAttribute(k);
  return want;
};
/* What takes no room has no scroll of its own to read. */
Object.defineProperty(Node.prototype, 'scrollLeft', {
  get() { return this.getClientRects().length ? this.scrolled || 0 : 0; },
  set(v) { this.scrolled = v; },
});
Object.defineProperty(Node.prototype, 'lastElementChild', { get() { return this.children[this.children.length - 1] || null; } });
Node.prototype.replaceChildren = function () { [...this.childNodes].forEach((c) => c.remove()); };

const TOKENS = { '--dur-fast': '.15s', '--dur': '.2s', '--dur-slow': '.25s', '--ease-out': 'cubic-bezier(.2,.8,.2,1)' };

/* The strip the row is told. */
const row = (title, over) => ({
  key: 'radarr:' + title, title, kind: 'movie', state: 'downloading', percent: 40, minutes_left: 14, line: '', held: false,
  ready_seconds: null, ref: 'tmdb-movie-' + title.length, href: '/watch#title=tmdb-movie-' + title.length,
  poster_url: '/api/poster/1', art_url: '/api/poster/1?kind=art', ...over,
});
const strip = (items) => ({ ok: true, outage: '', items, summary: { total: items.length, active: items.length, ready: 0, waiting: 0, percent: 40 } });
/* An item of /api/downloads, as the server sends it. */
const dl = (title, over) => ({
  id: 'radarr:' + title, title, media_type: 'movie', poster_url: '/api/watch/img/tmdb/w342/' + title + '.jpg', state: 'downloading',
  state_label: 'Downloading', progress: 40, eta: '', size_done: '', size_total: '', episodes: null, episode_summary: '',
  release_label: '', has_pack: false, search_hint: '', arr_link: '', arr_source: '', abandon_visible: false, stuck_seasons: [],
  arr_id: 7, kind: 'movie', label: '', attention: '', attention_hint: '', ...over,
});
const all = (queue, over) => ({
  hero: null, queue, upcoming: [], recent: [], subtitle: '', unreachable: [], refused: [], outage: '',
  downloads_set_up: { movies: true, shows: true }, strip: null, ...over,
});

const MODAL = ['div', { class: 'modal-backdrop', id: 'abandon-modal', hidden: '', role: 'dialog', 'aria-modal': 'true', 'aria-hidden': 'true', tabindex: '-1' },
  ['div', { class: 'modal-sheet' }, ['div', { class: 'abandon-modal-body' }, ['h2', { id: 'abandon-modal-title' }, 'Abandon search?'],
    ['p', { id: 'abandon-modal-copy' }], ['div', { id: 'abandon-season-list', hidden: '' }],
    ['div', { class: 'modal-actions' }, ['button', { type: 'button', class: 'btn', 'data-abandon-cancel': '' }, 'Cancel'],
      ['button', { type: 'button', class: 'btn', 'data-abandon-confirm': '' }, ['span', { 'data-confirm-label': '' }, 'Abandon']]]]]];

/* A Watch page at *url*. opts: admin, strip (the row's answers), all (/api/downloads' answers: a
   number is that status, 'hang' one that has not come until p.answer(), or until given up),
   canDownload, reduced, grid (a stand-in for the grid of a row, at ?all=), title (one for a
   title's page over it, at ?t=). */
function load(opts) {
  const o = { admin: true, url: '/', strip: [strip([])], all: [all([])], canDownload: { movie: true, show: true }, ...opts };
  const doc = makeDocument();
  doc.hidden = false;
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page', class: 'watch-page' },
    ['div', { id: 'watch-browse' },
      ['section', { class: 'watch-banner' }, 'Banner'],
      ['div', { class: 'watch-rows' },
        ['section', { class: 'watch-row', 'data-row-id': 'continue' }, ['div', { class: 'watch-row__scroll' }, ['a', { href: '/watch/movie/1' }, 'Salt']]],
        ['div', { class: 'dlx', id: 'watch-downloading', hidden: '' }],
        ['section', { class: 'watch-row', 'data-row-id': 'recent' }, ['div', { class: 'watch-row__scroll' }, ['a', { href: '/watch/movie/2' }, 'Fieldwork']]]]]]));
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' }, JSON.stringify({ is_admin: o.admin, can_download: o.canDownload })]));
  doc.body.appendChild(build(doc, ['a', { id: 'elsewhere', href: '/?dl=1', 'data-dl-open': '' }, 'Your library → Downloading']));
  if (o.admin) doc.body.appendChild(build(doc, MODAL));

  const p = { doc, time: 1000000, anims: [], asks: { strip: 0, all: 0 }, log: [], hanging: [], aborted: 0, askedAt: [] };
  let timers = [];
  let ids = 0;
  const clock = {
    set(fn, ms) { timers.push({ id: ++ids, fn, at: p.time + (ms || 0) }); return ids; },
    clear(id) { timers = timers.filter((t) => t.id !== id); },
  };
  p.step = async (ms) => {
    const until = p.time + ms;
    await settle();
    for (;;) {
      const next = timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!next) break;
      p.time = next.at;
      clock.clear(next.id);
      next.fn();
      await settle();
    }
    p.time = until;
    await settle();
  };
  const tab = { entries: [{ url: o.url, state: null }], index: 0 };
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');
  const listeners = {};
  const fire = (type, e) => (listeners[type] || []).slice().forEach((fn) => fn(e || { type }));
  const go = (by) => {
    const to = tab.index + by;
    if (to < 0 || to >= tab.entries.length || !by) return;
    tab.index = to;
    fire('popstate');
  };
  const answers = { strip: o.strip.slice(), all: o.all.slice() };
  const take = (name) => (answers[name].length > 1 ? answers[name].shift() : answers[name][0]);
  const win = { scrollY: 0 };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, WeakMap, WeakSet, Set, Map, Error, Infinity, parseFloat,
    URL, URLSearchParams, AbortController,
    Date: { now: () => p.time },
    setTimeout: clock.set, clearTimeout: clock.clear,
    requestAnimationFrame: (fn) => clock.set(fn, 16),
    getComputedStyle: () => ({ getPropertyValue: (n) => TOKENS[n] || '', fontSize: '19px', display: 'block', columnGap: '14' }),
    matchMedia: (q) => ({ matches: /reduce/.test(q) ? !!o.reduced : false }),
    innerHeight: 900, innerWidth: 1280,
    get scrollY() { return win.scrollY; },
    scrollTo: (a) => { win.scrollY = typeof a === 'object' ? a.top : a; },
    scrollBy() {},
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    sessionStorage: { getItem: () => null, setItem() {} },
    history: {
      scrollRestoration: 'auto',
      get state() { const s = tab.entries[tab.index].state; return s === null ? null : JSON.parse(JSON.stringify(s)); },
      pushState(state, _t, url) {
        tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state: JSON.parse(JSON.stringify(state)) });
        tab.index++;
      },
      replaceState(state, _t, url) { tab.entries[tab.index] = { url, state: JSON.parse(JSON.stringify(state)) }; },
      go(by) { setImmediate(() => go(by)); },
    },
    location: {
      get pathname() { return here().pathname; },
      get search() { return here().search; },
      get hash() { return here().hash; },
      href: '',
    },
    fetch: (url, opts) => {
      const name = url === '/api/downloads' ? 'all' : 'strip';
      p.asks[name] += 1;
      if (name === 'all') p.askedAt.push(p.time);
      const a = take(name);
      if (a === 'hang') {
        return new Promise((resolve, reject) => {
          const signal = opts && opts.signal;
          p.hanging.push((data) => resolve({ ok: true, status: 200, redirected: false, json: () => Promise.resolve(JSON.parse(JSON.stringify(data))) }));
          if (signal) signal.addEventListener('abort', () => { p.aborted += 1; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })); });
        });
      }
      if (typeof a === 'number') return Promise.resolve({ ok: false, status: a, redirected: false, json: () => Promise.resolve({}) });
      return Promise.resolve({ ok: true, status: 200, redirected: false, json: () => Promise.resolve(JSON.parse(JSON.stringify(a))) });
    },
    CustomEvent: class { constructor(t, init) { this.type = t; this.detail = init && init.detail; } },
    UIFeedback: { toast() {}, error() {} },
    MM: {},
  };
  ctx.window = ctx;
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).slice().forEach((f) => f(e));
  Node.prototype.animate = function (frames, opts) {
    p.anims.push({ node: this, frames, opts });
    const run = { cancel() { clock.clear(at); } };
    const at = clock.set(() => { if (run.onfinish) run.onfinish(); }, (opts.duration || 0) + (opts.delay || 0));
    return run;
  };
  vm.createContext(ctx);
  SOURCES.forEach(([, s]) => s.runInContext(ctx));
  if (o.admin) ABANDON.runInContext(ctx);
  const address = ctx.MMWatch.address;
  if (o.grid) {
    address.layer({ name: 'grid', depth: 10, params: ['all'], read: (h) => h.params.get('all'),
      open: (v, how) => { p.log.push('open grid ' + how); }, close: (how) => { p.log.push('close grid ' + how); } });
  }
  if (o.title) {
    address.layer({ name: 'title', depth: 50, params: ['t'], read: (h) => h.params.get('t'),
      open: () => { p.log.push('open title'); }, close: () => { p.log.push('close title'); } });
  }
  address.start();
  p.answer = (data) => p.hanging.splice(0).forEach((fn) => fn(data));
  p.ctx = ctx;
  p.tab = tab;
  p.at = () => tab.entries[tab.index].url;
  p.back = async () => { go(-1); await p.step(400); };
  p.key = (key) => { const e = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, target: doc.activeElement }; (doc.listeners.keydown || []).forEach((f) => f(e)); fire('keydown', e); };
  p.tell = (name) => (doc.listeners[name] || []).slice().forEach((fn) => fn({ type: name }));
  p.hide = (on) => { doc.hidden = on; p.tell('visibilitychange'); };
  p.q = (s) => doc.querySelector(s);
  p.qa = (s) => doc.querySelectorAll(s);
  p.scroll = (y) => { win.scrollY = y; fire('scroll'); };
  p.section = () => doc.getElementById('watch-dl');
  p.open = () => !!p.section() && !p.section().hidden;
  p.rowsAway = () => doc.getElementById('watch-browse').classList.contains('has-dl');
  p.on = () => { const a = doc.activeElement; return a.id || a.getAttribute('class') || a.tagName; };
  p.card = (title) => p.qa('#watch-dl [data-dl-card]').find((n) => n.querySelector('.dw__title').textContent === title);
  p.shown = (n) => !!n && n.getClientRects().length > 0;
  p.text = (n) => (n ? n.textContent.replace(/\s+/g, ' ').trim() : null);
  p.click = (n, extra) => click(doc, n, extra);
  p.durations = (from) => p.anims.slice(from).map((a) => a.opts.duration + (a.opts.delay || 0));
  p.keys = (from) => [...new Set(p.anims.slice(from).flatMap((a) => a.frames.flatMap((f) => Object.keys(f))))].sort();
  return p;
}

const USUAL = () => [row('Ember Coast'), row('Sundial', { state: 'almost_ready', percent: 100, line: 'Waiting to import' })];
const USUAL_ALL = () => all([dl('Ember Coast', { size_done: '4.1 GB', size_total: '9.6 GB', progress: 43, eta: '~5 min remaining' }),
  dl('Sundial', { state: 'almost_ready', state_label: 'Waiting to import', progress: 100 })], { upcoming: [dl('Lanterns', { release_label: 'Not yet released' })] });

async function race(late) {
  const p = load({ strip: [strip(USUAL())], all: ['hang'] });
  await p.step(1100);
  const from = p.anims.length;
  p.click(p.q('#watch-downloading .row-open'));
  const at = p.at();
  let skels = 0;
  for (let t = 0; t < late; t += 10) {
    await p.step(10);
    skels = Math.max(skels, p.qa('#watch-dl .dlg__grid .dlg-skel').length);
  }
  p.answer(USUAL_ALL());
  await p.step(400);
  const flown = p.anims.slice(from).filter((a) => a.frames[0].clipPath);
  return { at, skels, cards: flown.filter((a) => a.node.matches('[data-dl-card]')).length,
    placeholders: flown.filter((a) => a.node.matches('.dlg-skel')).length, grown: p.open() && p.rowsAway() };
}

const scenarios = {
  async opensFromTheRow() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    const link = p.q('#watch-downloading .row-open');
    const drawn = { href: link.getAttribute('href'), opens: link.hasAttribute('data-dl-open') };
    const before = p.anims.length;
    link.focus();
    p.click(link);
    const pushed = p.at();
    await p.step(10);
    const growing = { open: p.open(), rowsStill: !p.rowsAway() };
    await p.step(400);
    const grow = p.durations(before);
    const opened = { open: p.open(), rowsAway: p.rowsAway(), on: p.on(), at: p.at(), entries: p.tab.entries.length };
    const quiet = p.q('.dlg__title').classList.contains('is-quiet');
    const folding = p.anims.length;
    p.key('Escape');
    await p.step(400);
    return { drawn, pushed, growing, opened, quiet, grow: Math.max(...grow), folded: { at: p.at(), open: p.open(), rowsAway: p.rowsAway(), on: p.on() },
      fold: Math.max(...p.durations(folding)) };
  },
  // Its head holds under the bar, with its glass once the grid passes under it, and stays where
  // it stood as the grid folds back.
  async stuckHead() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    const section = p.section();
    const head = section.querySelector('.dlg__head');
    let top = 72;
    section.getBoundingClientRect = () => ({ top, bottom: top + 2000, left: 0, right: 1280, width: 1280, height: 2000 });
    // Held under the bar while the page scrolls; in place with the grid once it is fixed to move.
    head.getBoundingClientRect = () => {
      const at = section.classList.contains('is-moving') ? top + (parseFloat(head.style.top) || 0) : Math.max(top, 72);
      return { top: at, bottom: at + 64, left: 0, right: 1280, width: 1280, height: 64 };
    };
    const stuck = () => head.classList.contains('is-stuck');
    const glass = !!head.querySelector('.dlg__glass');
    const out = { glass, still: stuck() };
    top = -300; p.scroll(372);
    out.under = stuck();
    top = 72; p.scroll(0);
    out.back = stuck();
    top = -300; p.scroll(372);
    p.key('Escape');
    await p.step(10);
    out.folding = { stuck: stuck(), at: head.getBoundingClientRect().top };
    await p.step(400);
    out.folded = { stuck: stuck(), top: head.style.top || '' };
    return out;
  },
  async countOpensToo() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .dlx__count'));
    return { at: p.at() };
  },
  async chevronAndBack() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    p.click(p.q('[data-dl-back]'));
    await p.step(400);
    const chevron = { at: p.at(), open: p.open(), index: p.tab.index };
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    await p.back();
    return { chevron, back: { at: p.at(), open: p.open(), index: p.tab.index } };
  },
  async deepLink() {
    const p = load({ url: '/?dl=1', strip: [strip(USUAL())], all: [USUAL_ALL()] });
    const at = { entries: p.tab.entries.map((e) => e.url), index: p.tab.index, open: p.open(), rowsAway: p.rowsAway(), anims: p.anims.length, on: p.on() };
    const quiet = p.q('.dlg__title').classList.contains('is-quiet');
    await p.step(100);
    const drawn = p.qa('#watch-dl .dwx').length;
    await p.back();
    return { at, quiet, drawn, back: { url: p.at(), index: p.tab.index, open: p.open(), rowsAway: p.rowsAway() } };
  },
  async otherValuesAreTakenOut() {
    const p = load({ url: '/?dl=yes&x=1' });
    return { at: p.at(), open: p.open() };
  },
  async gridClosesFirst() {
    const p = load({ url: '/?all=recent', grid: true });
    p.log.splice(0);
    p.click(p.q('#elsewhere'));
    await p.step(400);
    return { log: p.log, at: p.at(), open: p.open() };
  },
  async groups() {
    const why = 'Import failed, path does not exist';
    const p = load({
      url: '/?dl=1',
      all: [all([
        dl('Ember Coast', { size_done: '4.1 GB', size_total: '9.6 GB', progress: 43, eta: '~5 min remaining' }),
        dl('Honest Forger', { state: 'queued', state_label: 'Queued', progress: 0, size_total: '3.2 GB' }),
        dl('Sundial', { state: 'almost_ready', state_label: 'Waiting to import', progress: 100, size_done: '2 GB', size_total: '2 GB' }),
        dl('Windward', { state: 'needs_attention', attention: why, attention_hint: 'Fix it in Radarr: Activity → Queue', arr_source: 'Radarr', arr_link: 'http://radarr.local/movie/1' }),
        dl('Tallow Hill', { state: 'needs_attention', retrying: true, attention: why }),
        dl('Quiet Engines', { state: 'searching', search_hint: 'Searched 11× · next attempt in ~24h', abandon_visible: true, arr_source: 'Radarr', arr_link: 'https://radarr.local/movie/2' }),
        dl('Cobblestone', { state: 'searching', search_hint: 'Searched once · next attempt in 1m', arr_link: 'javascript:alert(1)' }),
        dl('Finchley', { state: 'finding_copy' }),
      ], { upcoming: [dl('Lanterns', { release_label: 'Not yet released' }), dl('Orchard', { release_label: 'Not yet aired', media_type: 'series' })] })],
    });
    await p.step(100);
    const grid = p.q('#watch-dl .dlg__grid');
    const order = [];
    grid.children.forEach((n) => (n.classList.contains('grid-group') ? order.push([p.text(n)]) : order[order.length - 1].push(p.text(n.querySelector('.dw__title')))));
    const band = (t) => {
      const c = p.card(t);
      const part = (s) => { const n = c.querySelector(s); return n && !n.hidden ? p.text(n) : null; };
      return { line: p.text(c.querySelector('.dw__line')), facts: part('.dwx__facts'), why: part('.dwx__why'), fix: part('.dwx__fix'),
        fixKind: c.querySelector('.dwx__fix') ? ['is-ok', 'is-act'].filter((k) => c.querySelector('.dwx__fix').classList.contains(k)) : [],
        seen: part('.dwx__seen'),
        pills: c.querySelectorAll('.dwx__act').map((b) => [p.text(b), b.getAttribute('aria-label'), b.getAttribute('href'), b.getAttribute('target'), b.getAttribute('rel')]) };
    };
    const out = {};
    ['Ember Coast', 'Honest Forger', 'Sundial', 'Windward', 'Tallow Hill', 'Quiet Engines', 'Cobblestone', 'Finchley', 'Lanterns'].forEach((t) => { out[t] = band(t); });
    const trigger = p.card('Quiet Engines').querySelector('[data-abandon-trigger]');
    const states = {};
    ['Windward', 'Honest Forger', 'Quiet Engines', 'Ember Coast'].forEach((t) => {
      states[t] = ['is-stuck', 'is-looking', 'is-queued'].filter((k) => p.card(t).classList.contains(k));
    });
    const lanterns = p.card('Lanterns');
    return { order, count: p.text(p.q('.dlg__count')), title: p.text(p.q('.dlg__title')), cards: out, states,
      stopLast: lanterns.lastElementChild === lanterns.querySelector('[data-abandon-trigger]'),
      trigger: ['data-dl-id', 'data-arr-id', 'data-kind', 'data-title', 'data-stuck-seasons'].map((k) => trigger.getAttribute(k)),
      stop: p.card('Lanterns').querySelector('[data-abandon-trigger]').getAttribute('data-abandon-upcoming') };
  },
  async viewer() {
    const p = load({
      admin: false, url: '/?dl=1',
      // what the server never sends a viewer, to show the page does not draw it either
      all: [all([
        dl('Windward', { state: 'needs_attention', attention: 'Path fault', attention_hint: 'Fix it in Radarr: Activity → Queue', arr_source: 'Radarr', arr_link: 'http://radarr/1' }),
        dl('Tallow Hill', { state: 'needs_attention', retrying: true }),
        dl('Quiet Engines', { state: 'searching', search_hint: 'Searched 11× · next attempt in ~24h', abandon_visible: true, arr_link: 'http://radarr/2' }),
      ], { upcoming: [dl('Lanterns', { release_label: 'Not yet released' })] })],
    });
    await p.step(100);
    const shown = (s) => p.qa('#watch-dl ' + s).filter((n) => !n.hidden).map((n) => p.text(n));
    return { triggers: p.qa('[data-abandon-trigger]').length, links: p.qa('#watch-dl a.dwx__act').length, why: shown('.dwx__why'),
      fix: shown('.dwx__fix'), seen: shown('.dwx__seen'), modal: !!p.q('#abandon-modal'), stops: p.qa('#watch-dl .dwx--small .dwx__act').length };
  },
  async patched() {
    const ep = (label, state, progress) => ({ label, title: 'Ep ' + label, state, progress, is_pack_episode: false });
    const show = (eps, pct) => dl('Brightwater', { media_type: 'series', progress: pct, episodes: eps, abandon_visible: false });
    const p = load({
      url: '/?dl=1', strip: [strip([row('Brightwater', { kind: 'show' })])],
      all: [
        all([show([ep('S01E01', 'ready', 100), ep('S01E02', 'downloading', 20)], 30), dl('Quiet Engines', { state: 'searching', abandon_visible: true }), dl('Finchley', { state: 'finding_copy', abandon_visible: true })]),
        all([show([ep('S01E01', 'ready', 100), ep('S01E02', 'downloading', 60), ep('S01E03', 'queued', 0)], 55), dl('Quiet Engines', { state: 'searching', abandon_visible: true }), dl('Finchley', { state: 'finding_copy', abandon_visible: true })]),
        all([show([ep('S01E01', 'ready', 100), ep('S01E02', 'downloading', 60), ep('S01E03', 'queued', 0)], 55), dl('Finchley', { state: 'finding_copy', abandon_visible: true })]),
      ],
    });
    await p.step(100);
    const card = p.card('Brightwater');
    const pill = card.querySelector('[data-dl-eps]');
    p.click(pill);
    pill.focus();
    await p.step(2000);
    const after = { same: p.card('Brightwater') === card, expanded: pill.getAttribute('aria-expanded'), listShown: !card.querySelector('.dwx__eps-wrap').hidden,
      rows: card.querySelectorAll('.dwx__ep').map((r) => p.text(r.querySelector('.dwx__ep-s'))), on: p.doc.activeElement === pill,
      line: p.text(card.querySelector('.dw__line')), pill: p.text(pill) };
    p.card('Quiet Engines').querySelector('[data-abandon-trigger]').focus();
    await p.step(2000);
    const a = p.doc.activeElement;
    return { after, gone: !p.card('Quiet Engines'), focusTo: a.getAttribute('data-dl-id') || a.id };
  },
  async escapeFoldsAListFirst() {
    const ep = { label: 'S01E01', title: 'Weir', state: 'ready', progress: 100, is_pack_episode: false };
    const p = load({ strip: [strip([row('Brightwater', { kind: 'show' })])], all: [all([dl('Brightwater', { media_type: 'series', episodes: [ep] })])] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    const pill = p.card('Brightwater').querySelector('[data-dl-eps]');
    p.click(pill);
    pill.focus();
    p.key('Escape');
    await p.step(10);
    const first = { expanded: pill.getAttribute('aria-expanded'), open: p.open(), on: p.doc.activeElement === pill };
    p.key('Escape');
    await p.step(400);
    return { first, then: { open: p.open(), at: p.at() } };
  },
  async asking() {
    const p = load({ url: '/?dl=1', all: [USUAL_ALL()] });
    await p.step(100);
    const n0 = p.asks.all;
    await p.step(6000);
    const open = p.asks.all - n0;
    p.hide(true);
    const n1 = p.asks.all;
    await p.step(6000);
    const hidden = p.asks.all - n1;
    p.hide(false);
    await p.step(10);
    const shown = p.asks.all - n1;
    p.tell('mediaman:downloads:refresh');
    await p.step(10);
    const refreshed = p.asks.all - n1 - shown;
    await p.back();
    const n2 = p.asks.all;
    await p.step(10000);
    return { open, hidden, shown, refreshed, closed: p.asks.all - n2 };
  },
  async slimRow() {
    const soon = load({ strip: [{ ...strip([]), soon: 2 }], all: [all([], { upcoming: [dl('Lanterns'), dl('Orchard')] })] });
    await soon.step(1100);
    const of = (p) => {
      const slot = p.q('#watch-downloading');
      const cards = slot.querySelectorAll('.dw');
      return { hidden: slot.hidden, cards: cards.map((c) => [p.text(c.querySelector('.dw__title')), p.text(c.querySelector('.dw__line'))]),
        href: cards[0] && cards[0].querySelector('a').getAttribute('href'), opens: !!cards[0] && cards[0].querySelector('a').hasAttribute('data-dl-open'),
        heading: slot.querySelector('.row-open') && slot.querySelector('.row-open').getAttribute('aria-label') };
    };
    const withSoon = of(soon);
    soon.click(soon.q('#watch-downloading .dw__link'));
    await soon.step(400);
    const opened = { at: soon.at(), open: soon.open() };
    const none = load({ all: [all([])] });
    await none.step(1100);
    const unset = load({ canDownload: { movie: false, show: false } });
    await unset.step(1100);
    const back = load({ strip: [strip([row('Ember Coast')]), strip([])], all: [all([])] });
    await back.step(1100);
    await back.step(5000);
    const down = load({ strip: [{ ...strip([]), outage: "Can't reach Sonarr", soon: 2 }], all: [all([])] });
    await down.step(1100);
    return { withSoon, opened, none: of(none), unset: of(unset), afterLast: of(back), outage: of(down), asked: none.asks.all };
  },
  // The banner runs up under the bar by its own top margin; with the grid alone in its place that
  // margin is gone, so the grid grows to where it will stand, below the bar.
  async growsWhereItLands() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    const browse = p.q('#watch-browse');
    const banner = p.q('.watch-banner');
    browse.getBoundingClientRect = () => ({ top: -200, bottom: 2000, left: 0, right: 1280, width: 1280, height: 2200 });
    p.ctx.scrollTo(200);
    const style = p.ctx.getComputedStyle;
    p.ctx.getComputedStyle = (n) => ({ ...style(n), marginTop: n === banner ? '-72px' : '0px', paddingTop: '0px', borderTopWidth: '0px' });
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(10);
    return { pinned: p.section().style.top };
  },
  // A tap or a click opens it with no ring on its heading; a key press with one.
  async quietByPointer() {
    const run = async (sel, how) => {
      const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
      await p.step(1100);
      if (how === 'key') p.key('Enter');
      else p.tell('pointerdown');
      p.click(p.q(sel));
      await p.step(400);
      return { open: p.open(), on: p.on(), quiet: p.q('.dlg__title').classList.contains('is-quiet') };
    };
    return { rowTap: await run('#watch-downloading .row-open', 'tap'), linkTap: await run('#elsewhere', 'tap'), linkKey: await run('#elsewhere', 'key') };
  },
  async states() {
    const shot = async (admin, answer) => {
      const p = load({ admin, url: '/?dl=1', all: [answer] });
      await p.step(100);
      const empty = p.q('#watch-dl .dlg__empty');
      const note = p.q('#watch-dl .dlg__note');
      return { empty: empty.hidden ? null : [p.text(empty.querySelector('.grid-empty__title')), p.text(empty.querySelector('.grid-empty__text')),
        empty.querySelector('a') && empty.querySelector('a').getAttribute('href')],
      note: note.hidden ? null : p.text(note), count: p.text(p.q('#watch-dl .dlg__count')), raw: p.q('#watch-dl .dlg__count').textContent,
      cards: p.qa('#watch-dl .dwx').length };
    };
    const unset = all([], { downloads_set_up: { movies: false, shows: false } });
    const down = { outage: "Can't reach Radarr", unreachable: ['Radarr'] };
    return {
      unsetAdmin: await shot(true, unset), unsetViewer: await shot(false, unset),
      outageAdmin: await shot(true, all([dl('Ember Coast')], down)),
      refusedAdmin: await shot(true, all([], { ...down, refused: ['Radarr'] })),
      outageViewer: await shot(false, all([dl('Ember Coast')], { outage: "Downloads aren't available" })),
      outageViewerNothing: await shot(false, all([], { outage: "Downloads aren't available" })),
      outageViewerSoon: await shot(false, all([], { outage: "Downloads aren't available", upcoming: [dl('Lanterns')] })),
      outageAdminNothing: await shot(true, all([], { outage: "Can't reach Radarr, Sonarr and NZBGet", unreachable: ['Radarr', 'Sonarr', 'NZBGet'], upcoming: [dl('Lanterns')] })),
      caughtUp: await shot(true, all([], { upcoming: [dl('Lanterns')] })),
    };
  },
  async reduced() {
    const p = load({ reduced: true, strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    const from = p.anims.length;
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    const open = { durations: [...new Set(p.durations(from))], keys: p.keys(from), shown: p.open() };
    const folding = p.anims.length;
    p.key('Escape');
    await p.step(400);
    return { open, fold: { durations: [...new Set(p.durations(folding))], keys: p.keys(folding), shown: p.open() } };
  },
  async grows() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    const from = p.anims.length;
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(10);
    const anims = p.anims.slice(from);
    const of = (sel) => anims.filter((a) => a.node.matches && a.node.matches(sel));
    const above = of('.watch-banner').concat(of('[data-row-id="continue"]'));
    const below = of('[data-row-id="recent"]');
    const card = of('.dwx')[0];
    return {
      above: above.map((a) => a.frames[1].transform), below: below.map((a) => a.frames[1].transform),
      card: card && { from: card.frames[0].clipPath && card.frames[0].clipPath.startsWith('inset('), to: card.frames[1].transform, ms: card.opts.duration },
      title: of('.dlg__title').map((a) => !!a.frames[0].transform),
    };
  },
  // At little height the bar is in the page's flow: scrolled down to the row, it is out of sight
  // while the grid grows where the page's top will be. The row's chevrons sit over the grid.
  async barAndChevrons() {
    const run = async (position, y) => {
      const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
      await p.step(1100);
      const bar = build(p.doc, ['header', { class: 'nav-glass' }, 'mediaman']);
      p.doc.body.insertBefore(bar, p.doc.body.firstChild);
      bar.getBoundingClientRect = () => ({ top: -y, bottom: 72 - y, left: 0, right: 1280, width: 1280, height: 72 });
      const arrow = build(p.doc, ['button', { class: 'watch-row__arrow watch-row__arrow--next' }]);
      p.q('#watch-downloading .watch-row__viewport').appendChild(arrow);
      const style = p.ctx.getComputedStyle;
      p.ctx.getComputedStyle = (n) => ({ ...style(n), position: n === bar ? position : 'static' });
      p.ctx.scrollTo(y);
      const of = (from, n) => p.anims.slice(from).filter((a) => a.node === n).map((a) => ({ frames: a.frames, fill: a.opts.fill || '' }));
      const growing = p.anims.length;
      p.click(p.q('#watch-downloading .row-open'));
      await p.step(10);
      const grow = { bar: of(growing, bar), arrow: of(growing, arrow) };
      await p.step(400);
      const folding = p.anims.length;
      p.key('Escape');
      await p.step(10);
      const fold = { bar: of(folding, bar), arrow: of(folding, arrow) };
      await p.step(400);
      return { grow, fold, y: p.ctx.scrollY };
    };
    return { away: await run('relative', 600), near: await run('relative', 30), sticky: await run('sticky', 600) };
  },
  async abandonFocus() {
    const stuck = (over) => dl('Quiet Engines', { state: 'searching', abandon_visible: true, ...over });
    const p = load({ url: '/?dl=1', strip: [strip([row('Ember Coast')])], all: [all([stuck(), dl('Ember Coast')]), all([stuck(), dl('Ember Coast')]), all([dl('Ember Coast')])] });
    await p.step(100);
    const trigger = () => p.card('Quiet Engines').querySelector('[data-abandon-trigger]');
    const t = trigger();
    t.focus();
    p.click(t);
    await p.step(10);
    const modalOpen = !p.q('#abandon-modal').hidden;
    p.click(p.q('[data-abandon-cancel]'));
    await p.step(300);
    const back = p.doc.activeElement === t;
    trigger().focus();
    p.click(trigger());
    // the answer that drops the card comes while the dialog is open
    await p.step(4100);
    p.click(p.q('[data-abandon-cancel]'));
    await p.step(300);
    return { modalOpen, back, gone: !p.card('Quiet Engines'), then: p.on() };
  },
  async cardWithoutTitle() {
    const p = load({ strip: [strip([row('Nzb', { ref: null, href: '/downloads' })])], all: [all([dl('Nzb')])] });
    await p.step(1100);
    const a = p.q('#watch-downloading .dw__link');
    const inRow = [a.getAttribute('href'), a.hasAttribute('data-dl-open')];
    p.click(a);
    await p.step(400);
    const top = p.card('Nzb').querySelector('.dw__link');
    return { inRow, inGrid: [top.getAttribute('href'), top.hasAttribute('data-dl-open')], open: p.open() };
  },
  async bannerFirst() {
    const p = load({ strip: [strip(USUAL())], all: [USUAL_ALL()] });
    await p.step(1100);
    const first = () => p.q('#watch-browse').firstElementChild.getAttribute('class');
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    const open = first();
    p.key('Escape');
    await p.step(400);
    return { open, folded: first() };
  },
  async packEpisode() {
    const ep = (label, over) => ({ label, title: 'Weir', state: 'downloading', progress: 30, is_pack_episode: false, ...over });
    const p = load({ url: '/?dl=1', all: [all([dl('Brightwater', { media_type: 'series', has_pack: true, episodes: [ep('S01E01', { is_pack_episode: true }), ep('S01E02')] })])] });
    await p.step(100);
    const c = p.card('Brightwater');
    return { words: c.querySelectorAll('.dwx__ep-s').map((n) => p.text(n)), pack: c.querySelectorAll('.dwx__ep').map((n) => n.classList.contains('is-pack')),
      pill: p.text(c.querySelector('[data-dl-eps]')) };
  },
  async failing() {
    const p = load({ url: '/?dl=1', strip: [strip(USUAL())], all: [USUAL_ALL(), 500, 500, 500, USUAL_ALL()] });
    const note = () => (p.q('#watch-dl .dlg__note').hidden ? null : p.text(p.q('#watch-dl .dlg__note')));
    await p.step(100);
    await p.step(2000);
    const once = { note: note(), cards: p.qa('#watch-dl .dwx').length };
    await p.step(4000);
    const twice = note();
    await p.step(24000);
    const back = note();
    await p.step(2000);
    const at = p.askedAt;
    return { once, twice, back, gaps: at.slice(1).map((t, i) => t - at[i]) };
  },
  async slow() {
    const p = load({ url: '/?dl=1', strip: [strip(USUAL())], all: [USUAL_ALL(), 'hang', USUAL_ALL()] });
    const note = () => (p.q('#watch-dl .dlg__note').hidden ? null : p.text(p.q('#watch-dl .dlg__note')));
    await p.step(2100);
    await p.step(19800);
    const before = note();
    await p.step(200);
    const late = note();
    const asks = p.asks.all;
    await p.step(100000);
    const hung = { aborted: p.aborted, asked: p.asks.all - asks, note: note() };
    await p.step(4000);
    return { before, late, hung, then: { asked: p.asks.all - asks, note: note() } };
  },
  async firstLoad() {
    const waiting = load({ url: '/?dl=1', strip: [strip(USUAL())], all: ['hang'] });
    await waiting.step(100);
    const of = (p) => ({ busy: p.q('#watch-dl .dlg__body').getAttribute('aria-busy'), empty: p.text(p.q('#watch-dl .dlg__empty')) || null,
      skels: p.qa('#watch-dl .dlg__grid .dlg-skel').length,
      note: p.q('#watch-dl .dlg__note').hidden ? null : p.text(p.q('#watch-dl .dlg__note')), count: p.text(p.q('#watch-dl .dlg__count')) });
    const down = load({ url: '/?dl=1', strip: [strip(USUAL())], all: [503] });
    await down.step(100);
    return { waiting: of(waiting), down: of(down) };
  },
  async hiddenTab() {
    const p = load({ url: '/?dl=1', all: [USUAL_ALL()] });
    await p.step(100);
    p.hide(true);
    const n = p.asks.all;
    p.tell('mediaman:downloads:refresh');
    await p.step(6000);
    return { asked: p.asks.all - n };
  },
  async openerUnderATitle() {
    const p = load({ url: '/?dl=1', title: true, all: [USUAL_ALL()] });
    await p.step(100);
    p.ctx.MMWatch.address.push('title', '/?dl=1&t=x');
    await p.step(10);
    p.log.splice(0);
    p.click(p.q('#elsewhere'));
    await p.step(400);
    return { at: p.at(), top: p.ctx.MMWatch.address.top(), on: p.on(), open: p.open(), log: p.log };
  },
  async reopenedBeforeTheAnswer() {
    const p = load({ strip: [strip(USUAL())], all: ['hang'] });
    await p.step(1100);
    const scroller = p.q('[data-row-id="recent"] .watch-row__scroll');
    scroller.scrollLeft = 300;
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(10);
    await p.back();
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(130);
    const again = { open: p.open(), skels: p.qa('#watch-dl .dlg__grid .dlg-skel').length, asks: p.asks.all, aborted: p.aborted };
    p.answer(USUAL_ALL());
    await p.step(400);
    const grown = p.open() && p.rowsAway();
    const filled = { skels: p.qa('#watch-dl .dlg__grid .dlg-skel').length, cards: p.qa('#watch-dl [data-dl-card]').length };
    p.key('Escape');
    await p.step(400);
    return { grown, again, filled, left: scroller.scrollLeft, open: p.open() };
  },
  // With no answer yet, a tap grows the row at once into cards that hold the grid's place.
  async growsBeforeTheAnswer() {
    const p = load({ strip: [strip(USUAL())], all: ['hang'] });
    await p.step(1100);
    const skels = () => p.qa('#watch-dl .dlg__grid .dlg-skel').length;
    p.click(p.q('#watch-downloading .row-open'));
    const at = p.at();
    await p.step(130);
    const tapped = { at, open: p.open(), skels: skels(), asked: p.asks.all,
      busy: p.q('#watch-dl .dlg__body').getAttribute('aria-busy'), empty: p.q('#watch-dl .dlg__empty').hidden };
    const rising = p.anims.filter((a) => a.node.classList.contains('dlg-skel')).length;
    await p.step(400);
    const grown = { rowsAway: p.rowsAway(), skels: skels(), on: p.on() };
    const before = p.anims.length;
    p.answer(USUAL_ALL());
    await p.step(10);
    const fades = p.anims.slice(before).filter((a) => a.node.matches('[data-dl-card]') && a.frames[0].opacity === 0).length;
    const filled = { skels: skels(), cards: p.qa('#watch-dl [data-dl-card]').length, busy: p.q('#watch-dl .dlg__body').getAttribute('aria-busy') };
    return { tapped, rising, grown, fades, filled };
  },
  // A tap with no answer yet races it a moment: in time, the row's own cards fly; late, placeholders.
  // The placeholders take the filled grid's layout: its group heading first, the line under the
  // title held, a card each; the answer fades them out where they stood as its cards fade in.
  async placeholderLayout() {
    const p = load({ strip: [strip(USUAL())], all: ['hang'] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(500);
    const kids = () => p.q('#watch-dl .dlg__grid').children.map((n) => n.getAttribute('class').split(' ').filter((c) => /^(grid-group|dlg-skel\S*|dwx)$/.test(c)).join(' ') + (n.getAttribute('aria-hidden') === 'true' ? ' hidden' : ''));
    const countHeld = () => p.qa('#watch-dl .dlg__count .skel').length;
    const waiting = { kids: kids(), count: countHeld(), countText: p.text(p.q('#watch-dl .dlg__count')) };
    const before = p.anims.length;
    p.answer(USUAL_ALL());
    await p.step(10);
    const going = p.qa('#watch-dl .is-going');
    const outs = p.anims.slice(before).filter((a) => a.node.classList.contains('is-going') && a.frames[0].opacity === 1 && a.frames[1].opacity === 0).length;
    const filling = { going: going.length, outs, count: countHeld(), countText: p.text(p.q('#watch-dl .dlg__count')) };
    await p.step(400);
    const mixed = load({ strip: [strip([row('Ember Coast'), row('Tallow Hill', { kind: 'show' }), row('Finchley', { state: 'needs_attention' })])], all: ['hang'] });
    await mixed.step(1100);
    mixed.click(mixed.q('#watch-downloading .row-open'));
    await mixed.step(500);
    const grouped = mixed.q('#watch-dl .dlg__grid').children.map((n) => (n.classList.contains('grid-group') ? 'head' : n.querySelector('.dlg-skel__pill') ? 'card+buttons' : 'card'));
    return { waiting, filling, gone: p.qa('#watch-dl .is-going').length, kids: kids(), grouped };
  },
  // Each placeholder's band holds what the card's own will: its lines, and its row of buttons
  // where the card has one, by who looks.
  async placeholderBands() {
    const of = async (admin) => {
      const p = load({ admin, strip: [strip([row('Salt Road', { state: 'needs_attention', attention: admin ? 'Not enough articles on the server' : '' }), row('Ember Coast'),
        row('Tallow Hill', { kind: 'show', state: 'queued' }), row('Finchley', { state: 'queued' })])], all: ['hang'] });
      await p.step(1100);
      p.click(p.q('#watch-downloading .row-open'));
      await p.step(500);
      return p.qa('#watch-dl .dlg__grid .dlg-skel').map((card) => card.querySelector('.dlg-skel__more').children
        .map((n) => (n.classList.contains('dlg-skel__why') ? 'why' : n.classList.contains('dlg-skel__acts') ? 'acts' : 'line')).join(' '));
    };
    // What a stuck one's reason and its outcome will say is laid out, unseen, to wrap as they will.
    const said = load({ strip: [strip([row('Salt Road', { state: 'needs_attention', attention: 'Not enough articles', retrying: true })])], all: ['hang'] });
    await said.step(1100);
    said.click(said.q('#watch-downloading .row-open'));
    await said.step(500);
    const words = said.qa('#watch-dl .dlg__grid .dlg-skel-said').map((n) => n.textContent);
    const down = load({ strip: [{ ...strip(USUAL()), outage: 'Can\'t sign in to Sonarr' }], all: ['hang'] });
    await down.step(1100);
    const rowNote = down.text(down.q('#watch-downloading .dlx__note'));
    down.click(down.q('#watch-downloading .row-open'));
    await down.step(500);
    const gridNote = down.q('#watch-dl .dlg__note').hidden ? null : down.text(down.q('#watch-dl .dlg__note'));
    return { admin: await of(true), viewer: await of(false), rowNote, gridNote, words };
  },
  // An answer while the placeholders still fly waits for them to land.
  async answerMidGrow() {
    const p = load({ strip: [strip(USUAL())], all: ['hang'] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(200);
    p.answer(USUAL_ALL());
    await p.step(10);
    const mid = { skels: p.qa('#watch-dl .dlg__grid .dlg-skel').length, cards: p.qa('#watch-dl [data-dl-card]').length };
    await p.step(400);
    return { mid, landed: { skels: p.qa('#watch-dl .dlg__grid .dlg-skel').length, cards: p.qa('#watch-dl [data-dl-card]').length } };
  },
  async raceFast() { return race(60); },
  async raceSlow() { return race(200); },
  async skeletonThenOutage() {
    const skels = (p) => p.qa('#watch-dl .dlg__grid .dlg-skel').length;
    const of = (p) => ({ skels: skels(p), empty: p.text(p.q('#watch-dl .dlg__empty')) || null,
      note: p.q('#watch-dl .dlg__note').hidden ? null : p.text(p.q('#watch-dl .dlg__note')) });
    const down = load({ strip: [strip(USUAL())], all: [503] });
    await down.step(1100);
    down.click(down.q('#watch-downloading .row-open'));
    const first = skels(down);
    await down.step(400);
    const hung = load({ strip: [strip(USUAL())], all: ['hang'] });
    await hung.step(1100);
    hung.click(hung.q('#watch-downloading .row-open'));
    await hung.step(19000);
    const waiting = of(hung);
    await hung.step(1100);
    const late = of(hung);
    await hung.step(100000);
    return { down: { first, then: of(down) }, hung: { waiting, late, aborted: hung.aborted, then: of(hung) } };
  },
  async reducedBeforeTheAnswer() {
    const p = load({ reduced: true, strip: [strip(USUAL())], all: ['hang'] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    const skels = p.qa('#watch-dl .dlg__grid .dlg-skel').length;
    await p.step(400);
    const before = p.anims.length;
    p.answer(USUAL_ALL());
    await p.step(10);
    return { open: p.open(), skels, moved: p.anims.slice(before).length, cards: p.qa('#watch-dl [data-dl-card]').length };
  },
  async artFromTheRow() {
    const p = load({ strip: [strip([row('Ember Coast'), row('Twin'), row('Twin', { key: 'radarr:Twin2' })])], all: [all([dl('Ember Coast'), dl('Twin'), dl('Twin', { id: 'radarr:Twin2' })])] });
    await p.step(1100);
    p.click(p.q('#watch-downloading .row-open'));
    await p.step(400);
    const of = (n) => [n.querySelector('.dw__link').getAttribute('href'), n.querySelector('.dw__art').getAttribute('src')];
    return { ember: of(p.card('Ember Coast')), twins: p.qa('#watch-dl .dwx').filter((n) => p.text(n.querySelector('.dw__title')) === 'Twin').map(of) };
  },
  // A title that is done leads the grid, under a heading of its own, and the grid says it showed it.
  async readyLeads() {
    const done = row('Harbour', { state: 'ready', percent: 100, ready_seconds: 40, playable: true });
    const p = load({ strip: [strip([done, row('Ember Coast')])], all: [all([dl('Ember Coast')])] });
    const told = [];
    p.doc.addEventListener('mm:ready-shown', (e) => told.push(e.detail.keys));
    await p.step(1100);
    const closed = told.length;
    p.click(p.q('#elsewhere'));
    await p.step(400);
    const order = p.qa('#watch-dl .grid-group, #watch-dl [data-dl-card]').map((n) => (n.classList.contains('grid-group') ? '# ' : '') + p.text(n.querySelector(n.classList.contains('grid-group') ? 'span' : '.dw__title')));
    return { closed, order, told: told[told.length - 1], href: p.card('Harbour').querySelector('.dw__link').getAttribute('href') };
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) {
    try { out[name] = await scenarios[name](); } catch (err) { out[name] = { error: String(err && err.stack || err) }; }
  }
  process.stdout.write(JSON.stringify(out));
})();
