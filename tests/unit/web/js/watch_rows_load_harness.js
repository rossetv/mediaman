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
  const seen = { near: true, failed: 0, range: null, took: [] };
  const ctx = {
    document: doc, console, Array, Object, String, Math, Promise, WeakMap, Set, Map,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    UIFeedback: { toast: (m) => toasts.push(m) },
    addEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['watch/core.js', 'watch/rows-load.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const card = (n) => build(doc, ['article', { class: 'watch-card' },
    ['a', { class: 'watch-card__link', 'data-title-ref': 'movie-' + n },
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
  return { doc, grid, row, timers, toasts, asked, seen, failedAt };
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
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
