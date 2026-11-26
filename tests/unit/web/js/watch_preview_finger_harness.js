// Runs watch/core.js, keep.js, carry.js and preview.js against the fake DOM and a fake clock, a
// finger's presses on a card's poster, and prints every scenario's outcome as one JSON object.
// Usage: node watch_preview_finger_harness.js
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
  doc.body.addEventListener('click', (e) => { const link = e.target.closest('.watch-card__link'); if (link) opened.push(link.closest('.watch-card').id); });

  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Map, Set, Error, AbortController, encodeURIComponent,
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
    location: { origin: 'https://mm.example' },
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
  ['core/dom.js', 'watch/core.js', 'watch/keep.js', 'watch/carry.js', 'watch/preview.js'].forEach((f) => {
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
    doc, gets, opened, steps, winListeners, listeners, now: () => clock,
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

const scenarios = {
  async held() {
    const h = load();
    await hold(h, 700);
    const pv = h.pv();
    await h.pass(3000);
    return {
      open: !!pv && h.pv() === pv,
      width: pv && pv.style.width,
      finger: !!pv && pv.classList.contains('pv--finger'),
      opened: h.opened,
      trailer: !!h.doc.querySelector('iframe'),
      hint: !!(pv && pv.querySelector('.pv__hint')),
    };
  },
  async heldOnANarrowPhone() {
    const h = load({ width: 320 });
    await hold(h, 700);
    return h.pv() && h.pv().style.width;
  },
  async heldOnAWidePhone() {
    const h = load({ width: 440 });
    await hold(h, 700);
    return h.pv() && h.pv().style.width;
  },
  async tapped() {
    const h = load();
    await hold(h, 150);
    await h.pass(1000);
    return { open: !!h.pv(), opened: h.opened, asked: h.gets };
  },
  async moved() {
    const out = {};
    for (const [name, during] of [
      ['moved', async (h, p) => { await h.pass(100); h.move(p, { clientX: 50, clientY: 62 }); }],
      ['wobbled', async (h, p) => { await h.pass(100); h.move(p, { clientX: 54, clientY: 55 }); }],
      ['cancelled', async (h, p) => { await h.pass(100); h.cancel(p); }],
      ['scrolled', async (h) => { await h.pass(100); h.scroll(); }],
    ]) {
      const h = load();
      h.poster('film');
      const poster = h.poster('film');
      h.down(poster);
      await during(h, poster);
      await h.pass(800);
      out[name] = !!h.pv();
    }
    return out;
  },
  async onAGlidingRow() {
    const h = load();
    h.scroll();
    await h.pass(50);
    await hold(h, 700);
    return !!h.pv();
  },
  async released() {
    const h = load();
    const poster = h.poster('film');
    h.down(poster);
    await h.pass(700);
    h.up(poster);
    h.leave();
    await h.pass(500);
    return !!h.pv();
  },
  async closedByATapOutside() {
    const h = load();
    await hold(h, 700);
    const other = h.poster('other');
    h.down(other);
    h.up(other);
    h.tap(other);
    await h.pass(400);
    // Another tap now opens what it is on.
    h.down(other);
    h.up(other);
    h.tap(other);
    return { open: !!h.pv(), opened: h.opened, layers: h.doc.querySelectorAll('.pv').length };
  },
  async tapInsideStays() {
    const h = load();
    await hold(h, 700);
    const body = h.pv().querySelector('.pv__body');
    h.down(body);
    h.up(body);
    await h.pass(100);
    return !!h.pv();
  },
  async scrolledAway() {
    const h = load();
    await hold(h, 700);
    h.scroll();
    await h.pass(400);
    return !!h.pv();
  },
  async noRoom() {
    // A phone on its side: too short for it, so the title opens as by a tap.
    const h = load({ height: 20 });
    await hold(h, 700);
    return { open: !!h.pv(), opened: h.opened };
  },
  async replyFailed() {
    const h = load({ fail: true });
    await hold(h, 700);
    return { open: !!h.pv(), opened: h.opened };
  },
  async phoneMenu() {
    const h = load();
    const out = { touch: h.menu(h.poster('film')) };
    const byMouse = load();
    out.mouse = (() => {
      const e = { target: byMouse.poster('film'), pointerType: 'mouse', defaultPrevented: false, preventDefault() { e.defaultPrevented = true; } };
      byMouse.listeners('contextmenu').forEach((fn) => fn(e));
      return e.defaultPrevented;
    })();
    return out;
  },
  /* The page drawn and staged under the preview, and the motion that would carry it cannot be made. */
  async motionThrows() {
    const h = load({ motionThrows: true, width: 1280, mouse: true });
    // A mouse comes to rest on the card and opens it; a scroll down over it then picks it up.
    [1, 2].forEach((x) => h.listeners('pointermove').forEach((fn) => fn({ target: h.poster('film'), pointerType: 'mouse', clientX: x, clientY: 5, buttons: 0 })));
    await h.pass(1100);
    const pv = h.pv();
    for (const dy of [45, 45, 45]) {
      const e = { type: 'wheel', target: pv, deltaX: 0, deltaY: dy, deltaMode: 0, timeStamp: h.now(), cancelable: true, preventDefault() {} };
      (h.winListeners.wheel || []).forEach((fn) => fn(e));
      for (let a = pv; a; a = a.parentNode) (a.listeners.wheel || []).slice().forEach((fn) => fn(e));
      await h.pass(12);
    }
    await h.pass(500);
    return h.steps;
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  process.stdout.write(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
