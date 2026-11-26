// Runs watch/address.js and preview.js against the fake DOM, a fake clock and a fake history: what
// Back does to a preview a finger opened. Prints every scenario's outcome as one JSON object.
// Usage: node watch_preview_back_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.click = function () { click(this.ownerDocument, this, { detail: 0 }); };

const FILM = {
  ref: 'movie-1', kind: 'movie', title: 'Fieldwork', in_library: true, library_ref: 'movie-1', year: 2024,
  runtime_min: 116, genres: ['Drama'], ratings: {}, overview: 'A field.', trailer_key: 'AbCdEfGhIjK',
  leaving: null, keep_allowed: false, seasons: [], download: null,
  play: { label: 'Play', href: '/watch/play/1', progress_pct: null, minutes_left: null },
};

function card(id, ref) {
  return ['article', { class: 'watch-card', id },
    ['a', { class: 'watch-card__link', href: '/watch#title=' + ref, 'data-title-ref': ref },
      ['span', { class: 'watch-card__poster' }, ['img', { src: '/api/poster/1' }], ['span', { class: 'watch-card__title' }, 'Fieldwork']],
      ['span', { class: 'watch-card__sub' }, 'Movie']],
    ['div', { class: 'card-tools' }, ['button', { class: 'card-tool' }]]];
}

/* A phone's page at rest. *opts*: width, height, motionThrows, fail (the reply fails). */
function load(opts = {}) {
  const doc = makeDocument();
  doc.createElement = ((make) => (tag) => { const n = make(tag); n.style.setProperty = (name, value) => { n.style[name] = value; }; return n; })(doc.createElement);
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['div', { class: 'watch-row__scroll' }, card('film', 'movie-1'), card('other', 'movie-2')], ['p', { id: 'elsewhere' }, 'Words']]));
  const ttl = doc.body.appendChild(build(doc, ['div', { id: 'ttl', hidden: '' }, ['div', { class: 'ttl__hero' }]]));
  ttl.style.removeProperty = () => {};
  doc.documentElement.clientWidth = opts.width || 390;
  doc.hidden = false;
  doc.dispatchEvent = () => {};
  // A listener of the capture phase is kept apart: it has the event first.
  doc.addEventListener = ((on) => (type, fn, how) => on(how === true || (how && how.capture) ? type + ' capture' : type, fn))(doc.addEventListener);

  const winListeners = {};
  let clock = 1000;
  let timers = [];
  let ids = 0;
  const at = (ms, fn) => { timers.push({ id: ++ids, at: clock + ms, fn }); return ids; };
  const drop = (id) => { timers = timers.filter((t) => t.id !== id); };
  const gets = [];
  const opened = []; // the cards a click reached, as a tap would open them
  const steps = [];
  const tab = { entries: [{ url: '/watch', state: null }], index: 0, went: 0 };
  doc.body.addEventListener('click', (e) => { const link = e.target.closest('.watch-card__link'); if (link) opened.push(link.closest('.watch-card').id); });

  const ctx = {
    document: doc, console, JSON, URL, URLSearchParams, Infinity, Array, Object, String, Math, Promise, Map, Set, Error, AbortController, encodeURIComponent,
    MutationObserver: class { observe() {} disconnect() {} },
    Date: { now: () => clock },
    performance: { now: () => clock },
    setTimeout: (fn, ms) => at(ms || 0, fn),
    clearTimeout: drop,
    setInterval: (fn, ms) => at(ms, fn),
    clearInterval: drop,
    requestAnimationFrame: (fn) => at(16, () => fn(clock)),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: (q) => ({ matches: !!opts.mouse && /hover: hover/.test(q) }),
    navigator: {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    innerWidth: opts.width || 390, innerHeight: opts.height || 844, scrollY: 0,
    location: {
      origin: 'https://mm.example',
      get pathname() { return new URL(tab.entries[tab.index].url, 'http://mm').pathname; },
      get search() { return new URL(tab.entries[tab.index].url, 'http://mm').search; },
      get hash() { return new URL(tab.entries[tab.index].url, 'http://mm').hash; },
    },
    history: {
      scrollRestoration: 'auto',
      get state() { const st = tab.entries[tab.index].state; return st === null ? null : JSON.parse(JSON.stringify(st)); },
      pushState(state, _t, url) { tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url, state: JSON.parse(JSON.stringify(state)) }); tab.index++; },
      replaceState(state, _t, url) { tab.entries[tab.index] = { url, state: JSON.parse(JSON.stringify(state)) }; },
      go(by) { tab.went++; setImmediate(() => { if (tab.index + by < 0 || tab.index + by >= tab.entries.length) return; tab.index += by; (winListeners.popstate || []).forEach((fn) => fn({})); }); },
    },
    sessionStorage: { getItem: () => null, setItem() {} },
    scrollTo() {},
    addEventListener: (type, fn) => { (winListeners[type] = winListeners[type] || []).push(fn); },
    UIFeedback: { toast() {} },
    MM: {
      api: {
        get(url, o) {
          const ref = url.replace('/api/watch/preview/', '');
          gets.push(ref);
          return new Promise((resolve, reject) => {
            o.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
            if (opts.fail) reject(Object.assign(new Error('http'), { status: 500 }));
            else resolve({ ...FILM, ref, library_ref: ref });
          });
        },
      },
    },
    MMWatch: {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/carry.js', 'watch/preview.js'].forEach((f) => {
    vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
    if (f === 'watch/core.js') {
      const html = doc.documentElement.classList;
      ctx.MMWatch.detail = {
        open: () => html.add('has-ttl'),
        ahead() {},
        draw: (ref) => { steps.push('draw ' + ref); html.add('has-ttl'); return true; },
        openDrawn() {},
        undraw: () => { steps.push('undraw'); html.remove('has-ttl'); },
      };
    }
    if (f === 'watch/carry.js') {
      ctx.MMWatch.carry.stage = () => ({ draw() {}, end: (to) => steps.push('end ' + to), heroFoot: 500 });
      if (opts.motionThrows) ctx.MMWatch.carry.motion = () => { throw new Error('moving'); };
    }
  });

  const listeners = (type) => [...(doc.listeners[type + ' capture'] || []), ...(doc.listeners[type] || [])];
  const fire = (type, target, e) => {
    const ev = { type, target, pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: 50, clientY: 50, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; }, ...e };
    listeners(type).forEach((fn) => fn(ev));
    return ev;
  };
  const h = {
    doc, tab, ctx, gets, opened, steps, winListeners, listeners, now: () => clock,
    poster: (id) => doc.querySelector('#' + id + ' .watch-card__poster'),
    async pass(ms) {
      const end = clock + ms;
      await settle();
      for (;;) {
        const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        clock = Math.max(clock, due.at);
        drop(due.id);
        due.fn();
        await settle();
      }
      clock = Math.max(clock, end);
    },
    down: (target, e) => fire('pointerdown', target, e),
    move: (target, e) => fire('pointermove', target, e),
    up: (target, e) => fire('pointerup', target, e),
    cancel: (target) => fire('pointercancel', target),
    scroll: () => fire('scroll', doc.querySelector('.watch-row__scroll')),
    menu: (target) => fire('contextmenu', target).defaultPrevented,
    /* What the browser does with a tap ended on *target*: a click with a count, through the capture listeners first. */
    tap(target) {
      const e = { target, button: 0, detail: 1, defaultPrevented: false, stopped: false, preventDefault() { e.defaultPrevented = true; }, stopPropagation() { e.stopped = true; } };
      (doc.listeners['click capture'] || []).forEach((fn) => fn(e));
      if (e.stopped) return;
      for (let a = target; a; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e));
      (doc.listeners.click || []).forEach((fn) => fn(e));
    },
    pv: () => doc.querySelectorAll('.pv').filter((n) => n.classList.contains('is-open') && !n.classList.contains('is-fading'))[0] || null,
    leave: () => (doc.documentElement.listeners.pointerleave || []).forEach((fn) => fn({ pointerType: 'touch' })),
  };
  ctx.MMWatch.address.start();
  return h;
}

/* A finger held on the film's poster for *ms*, then lifted where it was, with the browser's click. */
async function hold(h, ms, during) {
  const poster = h.poster('film');
  h.down(poster);
  if (during) await during(h, poster);
  await h.pass(ms);
  h.up(poster);
  h.tap(poster);
  await h.pass(50);
}

const state = (h) => ({ entries: h.tab.entries.length, index: h.tab.index, open: !!h.pv(), url: h.tab.entries[h.tab.index].url });

const scenarios = {
  async opened() {
    const h = load();
    await hold(h, 700);
    return state(h);
  },
  async backClosesIt() {
    const h = load();
    await hold(h, 700);
    h.ctx.history.go(-1);
    await h.pass(600);
    return state(h);
  },
  async tapOutside() {
    const h = load();
    await hold(h, 700);
    h.down(h.doc.querySelector('#elsewhere'));
    await h.pass(600);
    return state(h);
  },
  async escape() {
    const h = load();
    await hold(h, 700);
    const e = { type: 'keydown', key: 'Escape', target: h.doc.body, defaultPrevented: false, preventDefault() {}, stopPropagation() {} };
    h.listeners('keydown').forEach((fn) => fn(e));
    await h.pass(600);
    return state(h);
  },
  async scrolled() {
    const h = load();
    await hold(h, 700);
    h.scroll();
    h.ctx.scrollY = 300;
    h.scroll();
    await h.pass(600);
    return { ...state(h), y: h.ctx.scrollY };
  },
  async openedTheTitle() {
    const h = load();
    await hold(h, 700);
    h.ctx.MMWatch.address.layer({ name: 'title', depth: 50, params: [], hash: true, read: (here) => (/^title=/.test(here.hash) ? here.hash : null), open() {}, close() {} });
    h.ctx.MMWatch.address.push('title', '/watch#title=movie-1');
    await h.pass(600);
    const opened = { entries: h.tab.entries.length, index: h.tab.index };
    h.ctx.MMWatch.address.close('title');
    await h.pass(600);
    return { opened, back: { entries: h.tab.entries.length, index: h.tab.index, url: h.tab.entries[h.tab.index].url } };
  },
  async mouse() {
    const h = load({ width: 1280, mouse: true });
    [1, 2].forEach((x) => h.listeners('pointermove').forEach((fn) => fn({ target: h.poster('film'), pointerType: 'mouse', clientX: x, clientY: 5, buttons: 0 })));
    await h.pass(1100);
    const open = !!h.pv();
    h.listeners('pointermove').forEach((fn) => fn({ target: h.doc.querySelector('#elsewhere'), pointerType: 'mouse', clientX: 900, clientY: 900, buttons: 0 }));
    await h.pass(1000);
    return { open, entries: h.tab.entries.length, went: h.tab.went };
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  process.stdout.write(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
