// Runs watch-rows.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node watch_rows_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build } = require('./fake_dom');

const PAD = 20;
const GAP = 8;

/* A line of season pills of these widths in a view *view* wide, which snaps to the nearest pill's
   start as a touch screen's does. */
function line(widths, view) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['div', { class: 'watch-row__viewport' },
    ['nav', { class: 'watch-row__scroll' }, ...widths.map((w, i) => ['a', { class: 'watch-pill' }, 'Season ' + (i + 1)])]]]));
  const scroller = doc.querySelector('.watch-row__scroll');
  const pills = doc.querySelectorAll('.watch-pill');
  let at = PAD;
  pills.forEach((p, i) => {
    Object.defineProperty(p, 'offsetLeft', { value: at });
    Object.defineProperty(p, 'offsetWidth', { value: widths[i] });
    at += widths[i] + GAP;
  });
  scroller.clientWidth = view;
  scroller.scrollWidth = at - GAP + PAD;
  scroller.scrollLeft = 0;
  const max = scroller.scrollWidth - view;
  const stops = [...pills.map((p) => Math.min(max, p.offsetLeft - PAD)), max];
  scroller.scrollTo = (o) => {
    scroller.scrollLeft = stops.reduce((best, s) => (Math.abs(s - o.left) < Math.abs(best - o.left) ? s : best));
  };
  const ctx = {
    document: doc, Array, Math, WeakMap, WeakSet, parseFloat,
    getComputedStyle: () => ({ paddingLeft: PAD + 'px', paddingRight: PAD + 'px' }),
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} disconnect() {} },
    addEventListener() {},
    MM: { dom: { icon: () => doc.createElement('svg') } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch-rows.js'), 'utf8'), ctx);
  return {
    max,
    at: () => scroller.scrollLeft,
    page(to) { doc.querySelector('.watch-row__arrow--' + to).listeners.click.forEach((fn) => fn()); },
    whole: () => pills.map((p, i) => [i + 1, p.offsetLeft - scroller.scrollLeft])
      .filter(([n, left]) => left >= PAD && left + widths[n - 1] <= view - PAD).map(([n]) => n),
  };
}

/* The seasons in full view at each stop, to the end and back. */
function thereAndBack(widths, view) {
  const l = line(widths, view);
  const there = [l.whole()];
  for (let i = 0; i < 20 && l.at() < l.max; i++) { l.page('next'); there.push(l.whole()); }
  const back = [l.whole()];
  for (let i = 0; i < 20 && l.at() > 0; i++) { l.page('prev'); back.push(l.whole()); }
  return { there, back };
}

/* A row of *count* cards, three whole in view; each has a link, More and (*keep*) a Keep. The focus
   moves as a browser's does, and says so to the row. */
function row(count, { keep = false, skeleton = false, reduced = true } = {}) {
  const doc = makeDocument();
  const card = (n) => build(doc, ['article', { class: 'watch-card' },
    ['a', { class: 'watch-card__link', href: '/watch/movie/' + n }, 'Film ' + n],
    ['button', { class: 'watch-more' }], ...(keep ? [['button', { class: 'watch-keep' }]] : [])]);
  const attrs = skeleton ? { class: 'watch-row__scroll' } : { class: 'watch-row__scroll', tabindex: '0' };
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' },
    ['div', { class: 'watch-row__viewport' }, ['div', attrs]], ['a', { id: 'after', href: '/' }]]));
  const scroller = doc.querySelector('.watch-row__scroll');
  const STEP = 100 + GAP;
  const place = () => {
    scroller.children.forEach((c, i) => {
      Object.defineProperty(c, 'offsetLeft', { configurable: true, value: PAD + i * STEP });
      Object.defineProperty(c, 'offsetWidth', { configurable: true, value: 100 });
    });
    scroller.scrollWidth = 2 * PAD + scroller.children.length * STEP - GAP;
    scroller.scrollLeft = Math.max(0, Math.min(scroller.scrollLeft, scroller.scrollWidth - scroller.clientWidth));
  };
  const add = (from, to) => { for (let n = from; n <= to; n++) scroller.appendChild(card(n)); place(); };
  scroller.clientWidth = 2 * PAD + 3 * STEP - GAP;
  scroller.scrollLeft = 0;
  add(1, count);
  // A smooth scroll is under way until run() lets it, and the timers, get on.
  let going = null;
  let timers = [];
  scroller.scrollTo = (o) => {
    going = o.behavior === 'smooth' ? o.left : null;
    if (going === null) scroller.scrollLeft = o.left;
  };
  const fire = (node, type, extra) => {
    const e = { target: node, key: '', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (let a = node; a; a = a.parentNode) (a.listeners[type] || []).forEach((fn) => fn(e));
    return e;
  };
  const focusOn = (node) => {
    const was = doc.activeElement;
    if (was === node) return;
    doc.activeElement = node;
    if (was !== doc.body) fire(was, 'focusout', { relatedTarget: node });
    fire(node, 'focusin', { relatedTarget: was });
  };
  let pressed = false; // what has the focus got it from the pointer
  const wire = () => doc.documentElement.querySelectorAll('div, a, button').forEach((n) => {
    n.focus = () => focusOn(n);
    n.matches = (selector) => (selector === ':focus-visible' ? !pressed : Object.getPrototypeOf(n).matches.call(n, selector));
  });
  wire();
  const observers = [];
  const ctx = {
    document: doc, Array, Math, WeakMap, WeakSet, parseFloat,
    clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setTimeout: (fn) => { const id = {}; timers.push({ id, fn }); return id; },
    getComputedStyle: () => ({ paddingLeft: PAD + 'px', paddingRight: PAD + 'px' }),
    matchMedia: () => ({ matches: reduced }),
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { constructor(fn) { this.fn = fn; } observe(node) { if (node === scroller) observers.push(this.fn); } },
    addEventListener() {},
    MM: { dom: { icon: () => doc.createElement('svg') } },
    MMWatch: { core: { clearArea: () => ({ top: 0, bottom: 900 }) } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch-rows.js'), 'utf8'), ctx);
  // Chrome: a row scrolled to its end is snapped to the card with the focus, and follows it as cards come.
  const resnap = (wasAtEnd) => {
    const held = scroller.children.find((c) => c.contains(doc.activeElement));
    if (!wasAtEnd || !held || scroller.style.scrollSnapType === 'none') return;
    scroller.scrollLeft = Math.min(held.offsetLeft - PAD, scroller.scrollWidth - scroller.clientWidth);
  };
  const changed = () => { wire(); place(); observers.forEach((fn) => fn([])); };
  const name = (n) => (n === scroller ? 'row' : n.id || (n.closest('.watch-card') ?
    n.className.replace('watch-card__', '').replace('watch-', '') + ' ' + n.closest('.watch-card').textContent.slice(5) : n.tagName));
  return {
    scroller,
    at: () => name(doc.activeElement),
    active: () => doc.activeElement,
    stops: () => [scroller, ...scroller.descendants()].filter((n) => (n === scroller ? n.hasAttribute('tabindex') : /^(A|BUTTON)$/.test(n.tagName)) &&
      n.getAttribute('tabindex') !== '-1').map(name),
    enter: () => focusOn(scroller),
    leave: () => focusOn(doc.getElementById('after')),
    press: (key, extra) => fire(doc.activeElement, 'keydown', { key, ...extra }).defaultPrevented,
    keys(...keys) { return keys.map((k) => { this.press(k); return this.at(); }); },
    load: (from, to) => {
      const atEnd = scroller.scrollLeft >= scroller.scrollWidth - scroller.clientWidth;
      add(from, to);
      resnap(atEnd);
      changed();
    },
    going: () => going,
    touch: (type) => fire(scroller, type),
    run() {
      if (going !== null) {
        scroller.scrollLeft = going;
        going = null;
        fire(scroller, 'scroll');
      }
      timers.splice(0).forEach((t) => t.fn());
    },
    blur: () => fire(doc.activeElement, 'focusout', { relatedTarget: null }),
    snaps: () => scroller.style.scrollSnapType !== 'none',
    click: (n) => {
      pressed = true;
      focusOn(scroller.children[n - 1].querySelector('a'));
      pressed = false;
    },
    // A browser gives what is taken out no focusout.
    take: (card) => { const held = card.contains(doc.activeElement); card.remove(); if (held) doc.activeElement = doc.body; changed(); },
  };
}

const scenarios = {
  oneStop() {
    const r = row(5);
    const before = r.stops();
    r.enter();
    const inside = { at: r.at(), stops: r.stops(), described: r.active().getAttribute('aria-describedby') };
    r.press('ArrowRight');
    const moved = { at: r.at(), described: r.active().getAttribute('aria-describedby') };
    r.leave();
    const after = r.stops();
    r.enter();
    const back = r.at();
    r.leave();
    r.scroller.scrollLeft = 2 * (100 + GAP);
    r.enter();
    return { before, inside, moved, after, back, scrolled: r.at(), skeleton: row(0, { skeleton: true }).stops() };
  },

  keys() {
    const r = row(6, { keep: true });
    r.enter();
    const along = r.keys('ArrowRight', 'ArrowRight', 'ArrowRight', 'ArrowLeft');
    const left = r.scroller.scrollLeft;
    const ends = r.keys('End', 'ArrowRight', 'Home', 'ArrowLeft');
    const down = r.keys('ArrowDown', 'ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowUp');
    const upTaken = r.press('ArrowUp');
    r.keys('ArrowDown', 'ArrowDown');
    const downTaken = r.press('ArrowDown');
    const tabTaken = r.press('Tab') || r.press('ArrowRight', { altKey: true });
    return { along, left, ends, down, taken: [upTaken, downTaken, tabTaken] };
  },

  comeAndGo() {
    const r = row(4);
    r.enter();
    r.keys('End');
    r.load(5, 8);
    const loaded = { at: r.at(), stops: r.stops(), end: r.keys('End')[0] };
    r.take(r.scroller.children[7]);
    const taken = r.stops();
    r.enter();
    return { loaded, taken, again: r.at() };
  },

  steady() {
    const r = row(8);
    r.enter();
    r.keys('End');
    const before = r.scroller.scrollLeft;
    r.load(9, 14);
    const held = { at: r.at(), moved: r.scroller.scrollLeft - before, snaps: r.snaps() };
    r.leave();
    const left = r.snaps();
    r.enter();
    const back = r.snaps();
    r.take(r.active().closest('.watch-card'));
    const taken = r.snaps();
    r.click(2);
    const pressed = r.snaps();
    r.keys('End');
    return { held, left, back, taken, pressed, keys: r.snaps() };
  },

  letGo() {
    // At the end of 8 cards, off a card's start once more have loaded.
    const at = (reduced) => {
      const r = row(8, { reduced });
      r.enter();
      r.keys('End');
      while (r.going() !== null) r.run();
      r.scroller.scrollLeft -= 40;
      r.load(9, 14);
      return r;
    };
    const look = (r) => ({ left: r.scroller.scrollLeft, to: r.going(), snaps: r.snaps() });
    const calm = at(false);
    calm.leave();
    const out = { leaving: look(calm) };
    calm.run();
    out.settled = look(calm);
    const still = at(true);
    still.leave();
    out.reduced = look(still);
    const back = at(false);
    back.leave();
    back.click(6);
    back.run();
    out.back = look(back);
    const away = at(false);
    away.blur();
    out.away = look(away);
    const finger = at(false);
    finger.leave();
    finger.touch('touchstart');
    finger.run();
    out.held = finger.snaps();
    finger.scroller.scrollLeft = 333;
    finger.touch('touchend');
    finger.run();
    out.lifted = look(finger);
    finger.run();
    out.after = look(finger);
    return out;
  },

  seasons: () => ({
    phone: thereAndBack([92, 94, 94, 94, 94, 94, 94, 94, 94, 102], 320),
    wide: thereAndBack([94, 94, 94, 400, 94, 94], 320),
  }),
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
