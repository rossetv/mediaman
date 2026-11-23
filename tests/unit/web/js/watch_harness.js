// Runs the real scripts under static/js/watch against a tiny fake DOM and prints the outcome of one
// scenario as JSON. Usage: node watch_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

const { Node, makeDocument, build, click, settle } = require('./fake_dom');


/* ── The show page ── */

function leavingLine(days) {
  return ['p', { class: 'watch-leaving', 'data-watch-leaving': '' },
    ['span', { class: 'watch-leaving__text' }, 'Leaving in ' + days + ' days'],
    ['span', { 'aria-hidden': 'true' }, '·'],
    ['button', { class: 'watch-keep', 'data-watch-keep': '3010', 'aria-label': 'Keep Brightwater' }, ['span', {}, 'Keep']]];
}

function heroInfo(season, line) {
  return ['div', { class: 'watch-hero__info' },
    ['h1', { class: 'watch-hero__title' }, 'Brightwater'],
    ...(line ? [line] : []),
    ['div', { class: 'watch-actions' }, ['a', { class: 'btn' }, 'Resume']],
    ['nav', { class: 'watch-seasons' },
      ['a', { class: 'watch-pill' + (season === 1 ? ' on' : ''), href: '/watch/show/3000?season=1' }, 'Season 1'],
      ['a', { class: 'watch-pill' + (season === 2 ? ' on' : ''), href: '/watch/show/3000?season=2' }, 'Season 2']]];
}

function showPage(doc, season, line) {
  return build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['section', { class: 'watch-hero' }, heroInfo(season, line)],
    ['ol', { class: 'watch-episodes' }, ['li', {}, 'Ep ' + season]],
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', hidden: '' },
      ['button', { class: 'watch-keep-menu__item', 'data-days': '7' }, '7 days']]]);
}

/* On a season that is not leaving, the server names the one that is. */
function otherLeavingLine() {
  return ['p', { class: 'watch-leaving watch-leaving--other', 'data-watch-leaving': '' }, 'Season 1 leaves in 3 days'];
}

/* The page as the server sends it for each season: Season 1 is leaving, Season 2 is not. */
function seasonDoc(season) {
  const doc = makeDocument();
  doc.body.appendChild(showPage(doc, season, season === 1 ? leavingLine(3) : otherLeavingLine()));
  return doc;
}

function load(opts = {}) {
  const doc = makeDocument();
  const toasts = [];
  const timers = [];
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, parseInt, parseFloat, Error,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    AbortController: class { abort() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: () => ({ matches: !opts.motion }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900,
    innerWidth: 1280,
    location: { pathname: '/watch/show/3000', search: '?season=1', reload() {} },
    history: { pushState() {} },
    DOMParser: class { parseFromString(html) { return seasonDoc(Number(html)); } },
    fetch: (href) => Promise.resolve({ ok: true, redirected: false, text: () => Promise.resolve(href.slice(-1)) }),
    UIFeedback: { toast: (message, opts) => toasts.push({ message, opts }) },
    MM: { api: { post: () => Promise.resolve({}), get: () => Promise.resolve({ items: [] }), messageOf: (_e, f) => f } },
    addEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  return { ctx, doc, toasts, timers, run: () => ['watch/core.js', 'watch/keep.js', 'watch/search.js', 'watch/seasons.js', 'watch.js']
    .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx)) };
}

function lineState(doc) {
  const line = doc.querySelector('.watch-hero__info [data-watch-leaving]');
  if (!line) return null;
  return {
    text: line.textContent.trim(),
    kept: line.classList.contains('watch-leaving--kept'),
    other: line.classList.contains('watch-leaving--other'),
    keep: !!line.querySelector('[data-watch-keep]'),
    beforeActions: line.nextElementSibling === doc.querySelector('.watch-hero__info .watch-actions'),
  };
}

function pill(doc, n) { return doc.querySelectorAll('.watch-seasons a.watch-pill')[n - 1]; }

async function keepFromLine(page) {
  click(page.doc, page.doc.querySelector('[data-watch-leaving] [data-watch-keep]'));
  click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
  await settle();
}

/* The Watch page's Leaving soon row, with the shared Keep menu. */
function leavingRowPage(doc) {
  const card = (id, title) => ['article', { class: 'watch-card', 'data-keep-id': id },
    ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-card__poster' }, title]],
    ['button', { class: 'watch-keep', 'data-watch-keep': id, 'aria-label': 'Keep ' + title }, 'Keep']];
  return build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['div', { class: 'watch-rows' },
      ['section', { class: 'watch-row', 'data-watch-leaving-row': '' },
        ['div', { class: 'watch-row__scroll' }, card('1100', 'Leaving Film'), card('1101', 'Northbound')]]],
    ['div', { class: 'watch-keep-menu', id: 'watch-keep-menu', hidden: '' },
      ['button', { class: 'watch-keep-menu__item', 'data-days': '7' }, '7 days']]]);
}

/* The Watch page's search box and the results it swaps in for the rows. */
function searchPage(doc) {
  return build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['div', { class: 'watch-search' },
      ['input', { class: 'watch-search__input', id: 'watch-search' }],
      ['button', { id: 'watch-search-clear', hidden: '' }],
      ['span', { id: 'watch-search-status' }]],
    ['div', { id: 'watch-browse' }],
    ['section', { class: 'watch-results', id: 'watch-results', hidden: '' },
      ['p', { class: 'watch-results__msg', id: 'watch-results-msg' }],
      ['div', { class: 'watch-grid', id: 'watch-grid' }]]]);
}

const scenarios = {
  // Keep the first card of the Leaving soon row, with motion on.
  async cardExit() {
    const page = load({ motion: true });
    const anims = [];
    page.doc.body.appendChild(leavingRowPage(page.doc));
    Node.prototype.animate = function (frames, timing) {
      const anim = { node: this, frames, timing, cancel() {} };
      anims.push(anim);
      return anim;
    };
    page.run();
    const card = page.doc.querySelector('.watch-card');
    click(page.doc, card.querySelector('[data-watch-keep]'));
    click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
    await settle();
    const exit = anims.find((a) => a.node === card);
    const at = (prop, test) => exit.frames.filter((f) => prop in f && test(f[prop])).map((f) => f.offset);
    return {
      duration: exit.timing.duration,
      // The last moment the slot is still fully open, and the first the card is gone.
      lastOpen: Math.max(...at('width', (v) => v !== '0px')),
      faded: Math.min(...at('opacity', (v) => v === 0)),
      pinned: card.children.map((c) => c.style.minWidth || ''),
    };
  },

  // A search that finds nothing.
  async searchEmpty() {
    const page = load();
    page.doc.body.appendChild(searchPage(page.doc));
    page.run();
    const input = page.doc.getElementById('watch-search');
    input.value = 'zzqx';
    input.listeners.input.forEach((fn) => fn({}));
    page.timers.pop()();
    await settle();
    const msg = page.doc.getElementById('watch-results-msg');
    return {
      empty: msg.classList.contains('watch-results__msg--empty'),
      parts: msg.children.map((c) => c.textContent),
      status: page.doc.getElementById('watch-search-status').textContent,
    };
  },

  // A search that fails, then its Try again.
  async searchError() {
    const page = load();
    let calls = 0;
    page.ctx.MM.api.get = () => { calls += 1; return Promise.reject(new Error('down')); };
    page.doc.body.appendChild(searchPage(page.doc));
    page.run();
    const input = page.doc.getElementById('watch-search');
    input.value = 'zzqx';
    input.listeners.input.forEach((fn) => fn({}));
    page.timers.pop()();
    await settle();
    const msg = page.doc.getElementById('watch-results-msg');
    const out = {
      error: msg.classList.contains('watch-results__msg--error'),
      alert: msg.getAttribute('role'),
      line: msg.children[0] && msg.children[0].textContent,
      status: page.doc.getElementById('watch-search-status').textContent,
    };
    click(page.doc, msg.querySelector('button'));
    await settle();
    out.retried = calls === 2;
    out.focus = page.doc.activeElement === input;
    return out;
  },

  // The grid-stack fade: both nodes share one cell while it runs, then only the new one is left.
  async swap() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function (frames, timing) {
      const anim = { node: this, frames, timing };
      anims.push(anim);
      return anim;
    };
    page.run();
    const doc = page.doc;
    const host = build(doc, ['div', { id: 'host' }, ['p', { id: 'old' }, 'old']]);
    doc.body.appendChild(host);
    const oldNode = doc.getElementById('old');
    const fresh = build(doc, ['p', { id: 'new' }, 'new']);
    const done = page.ctx.MMWatch.core.swap(oldNode, fresh);
    const stacked = oldNode.parentNode === fresh.parentNode && oldNode.parentNode !== host;
    const cell = [oldNode.style.gridArea, fresh.style.gridArea];
    const faded = anims.map((a) => [a.node.id, a.frames[0].opacity, a.frames[1].opacity]);
    anims.find((a) => a.node === oldNode).onfinish();
    await done;
    return { stacked, cell, faded, after: host.children.map((c) => c.id), oldGone: !host.contains(oldNode) };
  },

  // With reduced motion the swap is immediate and runs no animation.
  async swapReduced() {
    const page = load();
    let animated = false;
    Node.prototype.animate = function () { animated = true; return {}; };
    page.run();
    const doc = page.doc;
    const host = build(doc, ['div', { id: 'host' }, ['p', { id: 'old' }, 'old']]);
    doc.body.appendChild(host);
    await page.ctx.MMWatch.core.swap(doc.getElementById('old'), build(doc, ['p', { id: 'new' }, 'new']));
    return { after: host.children.map((c) => c.id), animated };
  },

  // FLIP: a node that moved slides from its old place; one that stayed is left alone.
  async flip() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function (frames) { anims.push({ node: this, frames }); return {}; };
    page.run();
    const doc = page.doc;
    const a = build(doc, ['i', { id: 'a' }]);
    const b = build(doc, ['i', { id: 'b' }]);
    const at = { a: { left: 0, top: 0 }, b: { left: 50, top: 0 } };
    [a, b].forEach((n) => { n.getBoundingClientRect = () => at[n.id]; });
    const play = page.ctx.MMWatch.core.flip([a, b]);
    at.a = { left: 30, top: 10 };
    play();
    return { moved: anims.map((x) => [x.node.id, x.frames[0].transform, x.frames[1].transform]) };
  },

  // Season 1 is leaving, Season 2 is not: the line's slot stays through the swap and back.
  async season() {
    const page = load();
    page.doc.body.appendChild(showPage(page.doc, 1, leavingLine(3)));
    page.run();
    const first = lineState(page.doc);
    click(page.doc, pill(page.doc, 2));
    await settle();
    const second = lineState(page.doc);
    click(page.doc, pill(page.doc, 1));
    await settle();
    return { first, second, back: lineState(page.doc) };
  },

  // A Keep from the line, then Undo after moving to a season that is not leaving.
  async undoElsewhere() {
    const page = load();
    page.doc.body.appendChild(showPage(page.doc, 1, leavingLine(3)));
    page.run();
    await keepFromLine(page);
    click(page.doc, pill(page.doc, 2));
    await settle();
    page.toasts[0].opts.action.onClick();
    await settle();
    return { second: lineState(page.doc) };
  },

  // Keep from the line, then Undo.
  async kept() {
    const page = load();
    page.doc.body.appendChild(showPage(page.doc, 1, leavingLine(3)));
    page.run();
    await keepFromLine(page);
    const kept = lineState(page.doc);
    page.toasts[0].opts.action.onClick();
    await settle();
    return { kept, undone: lineState(page.doc) };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
