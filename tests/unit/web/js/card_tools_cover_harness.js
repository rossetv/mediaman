// Runs watch/card-tools.js against the fake DOM, opens a card's levels with the mouse and points at
// each level in turn, then off them and shut. Its levels stand 40px apart from y 100, and their
// names are 20px tall and end at x 100; the title's words lie from 200 to 240, the scores' seen
// from 245 to 260. Prints, as JSON, which of the poster's words are covered at each step, and then which
// of three lines beside the name's place are.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build, click } = require('./fake_dom');

const doc = makeDocument();
doc.mouse = true;
doc.body.appendChild(build(doc, ['article', { class: 'watch-card', 'data-tmdb-ref': 'tmdb-movie-10' },
  ['a', { class: 'watch-card__link' }, ['div', { class: 'watch-card__poster' },
    ['span', { class: 'watch-badge' }, '7 days left'],
    ['span', { class: 'watch-card__scores' }, ['span', { class: 'sr-only' }, 'TMDB '], '81%'],
    ['span', { class: 'watch-card__title' }, 'Fieldwork']]],
  ['div', { class: 'card-tools' }, ['button', { class: 'card-tool card-tool--thumb', 'data-card-rate': '' }]]]));
const ctx = {
  document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Error, parseFloat, parseInt, Number, Date,
  setTimeout: () => 0,
  clearTimeout() {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1', transform: 'none', visibility: 'visible' }),
  matchMedia: () => ({ matches: true }),
  requestAnimationFrame: (fn) => fn(),
  performance: { now: () => 1e6 },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  MutationObserver: class { observe() {} disconnect() {} },
  MM: { api: { get: () => Promise.resolve({ ratings: {} }), put: () => Promise.resolve({}) } },
  innerHeight: 900,
  innerWidth: 1280,
  addEventListener() {},
  removeEventListener() {},
};
ctx.window = ctx;
vm.createContext(ctx);
['core/dom.js', 'watch/core.js', 'ratings.js', 'watch/card-tools.js']
  .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));

// The boxes of a text's words, which a range over it gives: where it stands, or as many as it has.
doc.createRange = () => {
  let node = null;
  return { selectNodeContents(n) { node = n; }, getClientRects: () => node.lines || [node.parentNode.getBoundingClientRect()] };
};
const card = doc.querySelector('.watch-card');
const band = (top, bottom) => () => ({ top, bottom, left: 0, right: 150, width: 150, height: bottom - top });
card.querySelector('.watch-card__title').getBoundingClientRect = band(200, 240);
card.querySelector('.watch-card__scores').getBoundingClientRect = band(245, 260);
// Words for a screen reader alone, which a browser still gives a box, here where Not for me's name is.
card.querySelector('.sr-only').getBoundingClientRect = band(195, 205);
card.querySelector('.watch-badge').getBoundingClientRect = band(8, 28);

const thumb = card.querySelector('[data-card-rate]');
thumb.focus();
click(doc, thumb);
const levels = card.querySelectorAll('.rate__b');
levels.forEach((b, i) => { b.getBoundingClientRect = band(100 + i * 40, 140 + i * 40); });
const name = card.querySelector('.rate__name');
name.getBoundingClientRect = () => ({ top: 100, bottom: 260, left: 0, right: 100, width: 100, height: 160 });
Object.defineProperty(name.firstChild, 'offsetWidth', { value: 70 });
const mouse = { pointerType: 'mouse' };
const covered = () => card.querySelectorAll('.is-covered').map((n) => n.className.split(' ')[0]);

const out = { opened: card.classList.contains('is-lifted'), at: [] };
levels.forEach((b) => {
  b.listeners.pointerenter.forEach((fn) => fn(mouse));
  out.at.push(covered());
});
card.querySelector('.rate__set').listeners.pointerleave.forEach((fn) => fn(mouse));
out.off = covered();
levels[3].listeners.pointerenter.forEach((fn) => fn(mouse));
doc.listeners.keydown.forEach((fn) => fn({
  key: 'Escape', target: doc.activeElement, defaultPrevented: false,
  preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {},
}));
out.shut = covered();

// Lines elsewhere, beside Not for me, whose name reaches from 190 to 210 and from x 30 to 100: one
// left of it, one that reaches under it, and one above it.
const lines = [[190, 0, 25], [190, 20, 40], [165, 0, 140]].map(([top, left, right], i) => {
  const n = doc.createElement('p');
  n.id = 'line-' + i;
  // In a box as wide as the name and more, which is not what is covered.
  n.getBoundingClientRect = () => ({ top, bottom: top + 20, left: 0, right: 150 });
  n.appendChild(doc.createTextNode('words')).lines = [{ top, bottom: top + 20, left, right }];
  return n;
});
ctx.MMWatch.cardTools.cover(levels[2], name.firstChild, lines);
out.lines = lines.filter((n) => n.classList.contains('is-covered')).map((n) => n.id);
process.stdout.write(JSON.stringify(out));
