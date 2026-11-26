// Runs watch/core.js and carry.js against the fake DOM: a staging that throws part-way must leave the page as it was.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build } = require('./fake_dom');

function load() {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { id: 'ttl' },
    ['div', { class: 'ttl__dim' }],
    ['div', { class: 'ttl__sheet' },
      ['div', { class: 'ttl__hero' }],
      ['h1', { class: 'ttl__title' }, 'Fieldwork One'],
      ['p', { class: 'ttl__meta' }, '2024']]]));
  const card = doc.body.appendChild(build(doc, ['div', { class: 'pv' },
    ['div', { class: 'pv__body' }, ['h3', { class: 'pv__title' }, 'Fieldwork One'], ['p', { class: 'pv__meta' }, '2024']]]));
  const ctx = { document: doc, performance: { now: () => 0 }, getComputedStyle: () => ({ opacity: '1' }), innerHeight: 800, MMWatch: {} };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/carry.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  doc.querySelectorAll('.ttl__sheet, .ttl__dim').forEach((n) => { n.style.removeProperty = (k) => { n.style[k] = ''; }; });
  const body = card.querySelector('.pv__body');
  const p = { body, n: card, hint: null, media: card, shell: card };
  return { doc, ctx, p, card, body };
}

const scenarios = {
  // The throw comes after the page was changed: the words under the title are split, the sheet holds a copy.
  async throwsAfterStaging() {
    const h = load();
    const root = h.doc.getElementById('ttl');
    const title = root.querySelector('.ttl__title');
    h.body.getBoundingClientRect = () => { throw new Error('staged'); };
    let threw = false;
    try { h.ctx.MMWatch.carry.stage(h.p); } catch (e) { threw = e.message === 'staged'; }
    const left = (sel) => root.querySelectorAll(sel).length + h.doc.querySelectorAll(sel).length;
    return {
      threw, carried: root.classList.contains('is-carried'), cardCarried: h.card.classList.contains('is-carried'),
      ghost: left('.cry-ghost'), plate: left('.cry-plate'), shade: left('.cry-shade'), title: title.textContent, words: title.querySelectorAll('.cry-word').length,
    };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
