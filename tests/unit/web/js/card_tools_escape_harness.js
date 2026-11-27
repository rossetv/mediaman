// Runs watch/card-tools.js against the fake DOM, opens a card's levels and presses Escape: once as
// the first to hear it, once after a layer over it has already taken it. Prints, as JSON, whether
// the levels are still open after each press and whether the press was marked as taken.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build, click } = require('./fake_dom');

function load() {
  const doc = makeDocument();
  doc.mouse = true;
  doc.body.appendChild(build(doc, ['article', { class: 'watch-card', 'data-tmdb-ref': 'tmdb-movie-10' },
    ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-card__title' }, 'Fieldwork']],
    ['div', { class: 'card-tools' }, ['button', { class: 'card-tool card-tool--thumb', 'data-card-rate': '' }]]]));
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Error, parseFloat, parseInt, Number, Date,
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', opacity: '1', transform: 'none' }),
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
  return doc;
}

function escape(takenBefore) {
  const doc = load();
  const card = doc.querySelector('.watch-card');
  const thumb = card.querySelector('[data-card-rate]');
  thumb.focus();
  click(doc, thumb);
  const opened = card.classList.contains('is-lifted');
  const e = {
    key: 'Escape', target: doc.activeElement, defaultPrevented: takenBefore,
    preventDefault() { e.defaultPrevented = true; }, stopPropagation() {}, stopImmediatePropagation() {},
  };
  doc.listeners.keydown.forEach((fn) => fn(e));
  return { opened, open: card.classList.contains('is-lifted'), taken: e.defaultPrevented };
}

process.stdout.write(JSON.stringify({ first: escape(false), takenBefore: escape(true) }));
