// Runs the Watch page's row loader (watch/rows-load.js) on cards that stand in lines, through its `view`, against the
// fake DOM and prints the outcome of one scenario as JSON. Usage: node watch_rows_load_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, settle } = require('./fake_dom');

const PLACEHOLDERS = 5;

Object.defineProperty(Node.prototype, 'clientWidth', { configurable: true, get: () => 800 });

/* Cards by number, the first page's *first*, and *pages*: what each fetch answers in turn. */
function load(first, pages) {
  const doc = makeDocument();
  const timers = [];
  const toasts = [];
  const asked = [];
  const resizes = new Set();
  const seen = { near: true, failed: 0, range: null, took: [] };
  const ctx = {
    document: doc, console, Array, Object, String, Math, Promise, WeakMap, Set, Map,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    UIFeedback: { toast: (m) => toasts.push(m) },
    addEventListener: (t, fn) => resizes.add(fn),
    removeEventListener: (t, fn) => resizes.delete(fn),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['watch/core.js', 'watch/rows-load.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  // A number is a film; 'show mark' a card of that show with that mark.
  // '# text' is a heading among the cards; '@key' a card of a title that has no page, '@' one with no key either.
  const card = (n) => /^#/.test(n) ? build(doc, ['h3', {}, n.slice(2)]) : /^@/.test(n) ? build(doc, ['article', { class: 'watch-card', ...(n.length > 1 ? { 'data-key': n.slice(1) } : {}) },
    ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-card__title' }, 'Film']]]) : build(doc, ['article', { class: 'watch-card', ...(/ /.test(n) ? { 'data-mark': n.split(' ')[1] } : {}) },
    ['a', { class: 'watch-card__link', 'data-title-ref': / /.test(n) ? n.split(' ')[0] : 'movie-' + n },
      ['span', { class: 'watch-card__poster' }, ['img', { src: '/p/' + n }], ['span', { class: 'watch-card__title' }, 'Film ' + n]]]]);
  const host = doc.body.appendChild(build(doc, ['div', {}, ['div', { class: 'watch-grid', tabindex: '-1' }]]));
  const grid = host.children[0];
  first.forEach((n) => grid.appendChild(card(n)));
  const failedAt = doc.body.appendChild(build(doc, ['div', { id: 'failed' }]));
  const row = ctx.MMWatch.rowsLoad.start(host, {
    title: 'Films',
    next: 'p0',
    fetchPage: (next) => {
      asked.push(next);
      const page = pages.shift();
      return page.status ? Promise.reject(Object.assign(new Error('no'), page)) : Promise.resolve({ cards: page.cards.map(card), next: page.next });
    },
    view: {
      scroller: grid,
      near: () => seen.near,
      placeholders: () => PLACEHOLDERS,
      range: (cards) => seen.range || [0, cards.length],
      failed: (button) => { seen.failed++; failedAt.appendChild(button); },
      took: (fresh, done) => { seen.took.push([fresh.length, done]); failedAt.textContent = ''; if (seen.stopAfter) seen.near = false; },
    },
  });
  return { doc, grid, row, timers, toasts, asked, seen, failedAt, resizes };
}

const refs = (p) => p.grid.querySelectorAll('[data-title-ref]').map((a) => Number(a.getAttribute('data-title-ref').slice(6)));
const placeholders = (p) => p.grid.querySelectorAll('.watch-card--skel').length;
const press = (button) => button.listeners.click.forEach((fn) => fn({}));

const scenarios = {
  async toTheEnd() {
    const p = load([1, 2, 2], [{ cards: [2, 3], next: 'p1' }, { cards: [1, 3], next: 'p2' }, { cards: [4], next: null }]);
    const during = placeholders(p);
    await settle();
    return { refs: refs(p), asked: p.asked, during, left: placeholders(p), took: p.seen.took, next: p.row.next(), busy: p.grid.getAttribute('aria-busy') };
  },

  // Two seasons of a show are two cards; one season again, or a show whatever its mark, is a repeat.
  async seasons() {
    const p = load(['show-7 season/71', 'show-7 season/72', 'show-7 season/71', 'show-8 show/8'],
      [{ cards: ['show-7 season/72', 'show-7 season/73', 'show-8 show/8', 'show-8 episode/81', 'show-8 '], next: null }]);
    await settle();
    return p.grid.querySelectorAll('.watch-card').map((c) => c.getAttribute('data-mark'));
  },

  async noPage() {
    const p = load(['@id:5', '@id:4', '@'], [{ cards: ['@id:3', '@id:5', '@', '@id:4'], next: null }]);
    await settle();
    return p.grid.querySelectorAll('.watch-card').map((c) => c.getAttribute('data-key'));
  },

  async waitsAfterTooMany() {
    const p = load([1], [{ status: 429 }, { cards: [2], next: null }]);
    await settle();
    const waiting = { placeholders: placeholders(p), failed: p.seen.failed, ms: p.timers.map((t) => t.ms) };
    p.timers.shift().fn();
    await settle();
    return { waiting, refs: refs(p), asked: p.asked };
  },

  async failsAndTriesAgain() {
    const p = load([1], [{ status: 500 }, { status: 500 }, { cards: [2, 3], next: null }]);
    await settle();
    const button = p.failedAt.querySelector('button');
    const failed = { held: placeholders(p), label: button.getAttribute('aria-label'), inGrid: p.grid.contains(button) };
    button.focus();
    press(button);
    await settle();
    const again = { failed: p.seen.failed, toasts: p.toasts.length, held: placeholders(p), same: p.failedAt.querySelector('button') === button };
    press(button);
    await settle();
    return { failed, again, refs: refs(p), left: placeholders(p), focus: p.doc.activeElement.getAttribute('data-title-ref') };
  },

  // What comes after Try again may start with a heading, or hold nothing to focus.
  async tryAgainPastHeadings() {
    const p = load([1], [{ status: 500 }, { cards: ['# Films'], next: 'p1' }, { cards: [3], next: null }]);
    await settle();
    const button = p.failedAt.querySelector('button');
    button.focus();
    press(button);
    await settle();
    const bare = { refs: refs(p), focusInGrid: p.doc.activeElement === p.grid };
    const q = load([1], [{ status: 500 }, { cards: ['# Films', 2], next: 'p1' }, { cards: [3], next: null }]);
    await settle();
    const again = q.failedAt.querySelector('button');
    again.focus();
    press(again);
    await settle();
    return { bare, led: { refs: refs(q), focus: q.doc.activeElement.getAttribute('data-title-ref'), busy: q.grid.getAttribute('aria-busy') } };
  },

  async loadsOnlyNear() {
    const p = load([1], [{ cards: [2], next: 'p1' }, { cards: [3], next: 'p2' }, { cards: [4], next: null }]);
    p.seen.stopAfter = true;
    await settle();
    const first = refs(p);
    p.row.check();
    await settle();
    const still = refs(p);
    p.row.more();
    await settle();
    const asked = refs(p);
    p.seen.range = [2, 3];
    p.row.check();
    const loaded = p.grid.querySelectorAll('img').map((i) => i.hasAttribute('src'));
    return { first, still, asked, next: p.row.next(), loaded };
  },

  // A card taken out of the row by someone else must not hold a place in the range of posters kept awake.
  async cardsTakenOut() {
    const p = load([1, 2, 3], [{ cards: [], next: null }]);
    p.seen.near = false;
    p.seen.range = [2, 3];
    p.row.check();
    const first = p.grid.children[0];
    first.remove();
    p.seen.range = [0, 1];
    p.row.check();
    const awake = () => p.grid.querySelectorAll('img').map((i) => i.hasAttribute('src'));
    const without = awake();
    // One awake as it is taken out comes back awake; far off then, it goes back to sleep with the next sweep.
    const near = p.grid.children[0];
    near.remove();
    p.row.check();
    p.grid.insertBefore(near, p.grid.children[0]);
    const back = awake();
    p.seen.range = [1, 2];
    p.row.check();
    return { without, back, far: awake() };
  },

  // A row whose cards stand in another order than they came in keeps awake those it shows.
  async cardsInAnotherOrder() {
    const p = load([1, 2, 3], [{ cards: [], next: null }]);
    p.seen.near = false;
    p.grid.appendChild(p.grid.children[0]);
    p.seen.range = [0, 1];
    p.row.check();
    return p.grid.querySelectorAll('img').map((i) => i.hasAttribute('src'));
  },

  async stopped() {
    const p = load([1], [{ cards: [2], next: null }]);
    const listens = () => [p.grid.listeners.scroll.length, p.resizes.size];
    const before = listens();
    p.row.stop();
    await settle();
    return { before, after: listens(), refs: refs(p) };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
