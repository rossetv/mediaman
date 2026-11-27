// Runs watch/card-tools.js against the fake DOM, opens a card's levels with the mouse and points at
// each level in turn, then off them, at one while its name is hidden, at one while the page is
// resized, gives and takes back a level, and shuts. Its levels stand 40px apart from y 100, at the
// right of a column from x 110 to 150; their names are 20px tall, 7px wide a letter, and end at
// x 100. The title's words lie in two lines, from 110 to 130 reaching x 20, and from 200 to 240
// reaching x 90; the scores' seen lie from 245 to 260 and reach under the column. Prints, as JSON,
// which of the poster's words are covered at each step, and then which of three lines beside the
// name's place are.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click } = require('./fake_dom');

const doc = makeDocument();
doc.mouse = true;
doc.body.appendChild(build(doc, ['article', { class: 'watch-card', 'data-tmdb-ref': 'tmdb-movie-10' },
  ['a', { class: 'watch-card__link' }, ['div', { class: 'watch-card__poster' },
    ['span', { class: 'watch-badge' }, '7 days left'],
    ['span', { class: 'watch-card__scores' }, ['span', { class: 'sr-only' }, 'TMDB '], '81%'],
    ['span', { class: 'watch-card__title' }, 'Fieldwork']]],
  ['div', { class: 'card-tools' }, ['button', { class: 'card-tool card-tool--thumb', 'data-card-rate': '' }]]]));
doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).forEach((fn) => fn(e));
let hidden = false; // the name's words, as a container query too narrow for them leaves them
const onWindow = {};
const ctx = {
  document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Error, parseFloat, parseInt, Number, Date,
  setTimeout: () => 0,
  clearTimeout() {},
  CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1', transform: 'none', visibility: hidden ? 'hidden' : 'visible' }),
  matchMedia: () => ({ matches: true }),
  requestAnimationFrame: (fn) => fn(),
  performance: { now: () => 1e6 },
  IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
  MutationObserver: class { observe() {} disconnect() {} },
  MM: { api: { get: () => Promise.resolve({ ratings: {} }), put: () => Promise.resolve({}), delete: () => Promise.resolve({}) } },
  innerHeight: 900,
  innerWidth: 1280,
  addEventListener(t, fn) { (onWindow[t] = onWindow[t] || []).push(fn); },
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
// The column of levels, from the moment it is made.
const box = Node.prototype.getBoundingClientRect;
Node.prototype.getBoundingClientRect = function () {
  return this.classList.contains('rate--lift') ? { top: 100, bottom: 260, left: 110, right: 150, width: 40, height: 160 } : box.call(this);
};
const card = doc.querySelector('.watch-card');
const band = (top, bottom, right = 150) => () => ({ top, bottom, left: 0, right, width: right, height: bottom - top });
const titleWords = card.querySelector('.watch-card__title').firstChild;
const wrapped = (second) => { titleWords.lines = [band(110, 130, 20)(), second]; };
wrapped(band(200, 240, 90)());
card.querySelector('.watch-card__scores').getBoundingClientRect = band(245, 260, 130);
// Words for a screen reader alone, which a browser still gives a box, here where Not for me's name is.
card.querySelector('.sr-only').getBoundingClientRect = band(195, 205);
card.querySelector('.watch-badge').getBoundingClientRect = band(8, 28);

const thumb = card.querySelector('[data-card-rate]');
thumb.focus();
click(doc, thumb);
const covered = () => card.querySelectorAll('.is-covered').map((n) => n.className.split(' ')[0]).sort();
const out = { opened: card.classList.contains('is-lifted'), lifted: covered(), at: [] };

const levels = card.querySelectorAll('.rate__b');
levels.forEach((b, i) => { b.getBoundingClientRect = band(100 + i * 40, 140 + i * 40); });
const name = card.querySelector('.rate__name');
name.getBoundingClientRect = () => ({ top: 100, bottom: 260, left: 0, right: 100, width: 100, height: 160 });
const words = name.firstChild;
Object.defineProperty(words, 'offsetWidth', { get: () => words.textContent.length * 7 });
const mouse = { pointerType: 'mouse' };
const point = (i) => levels[i].listeners.pointerenter.forEach((fn) => fn(mouse));

levels.forEach((b, i) => {
  point(i);
  out.at.push(covered());
});
card.querySelector('.rate__set').listeners.pointerleave.forEach((fn) => fn(mouse));
out.off = covered();

hidden = true;
point(2);
out.invisible = covered();
hidden = false;
words.getClientRects = () => [];
point(2);
out.undrawn = covered();
delete words.getClientRects;

point(2);
wrapped(band(215, 240, 90)());
(onWindow.resize || []).forEach((fn) => fn());
out.resized = covered();
wrapped(band(200, 240, 90)());

// Given, then taken back: its name is the wider words that say so, which reach the title's first line.
click(doc, levels[0]);
out.given = covered();
click(doc, levels[0]);
out.removed = covered();
out.removedSays = words.textContent;

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
words.textContent = 'Not for me';
ctx.MMWatch.cardTools.cover(levels[2], words, lines);
out.lines = lines.filter((n) => n.classList.contains('is-covered')).map((n) => n.id);
process.stdout.write(JSON.stringify(out));
