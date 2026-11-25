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
    ['h1', { class: 'watch-banner__title' }, 'Brightwater'],
    ...(line ? [line] : []),
    ['div', { class: 'watch-actions' }, ['a', { class: 'btn' }, 'Resume']],
    ['nav', { class: 'watch-seasons' },
      ['a', { class: 'watch-pill' + (season === 1 ? ' on' : ''), href: '/watch/show/3000?season=1' }, 'Season 1'],
      ['a', { class: 'watch-pill' + (season === 2 ? ' on' : ''), href: '/watch/show/3000?season=2' }, 'Season 2']]];
}

function showPage(doc, season, line) {
  return build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['section', { class: 'watch-banner' }, heroInfo(season, line)],
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
    UIFeedback: { toast: (message, opts) => { const t = { message, opts }; toasts.push(t); return t; }, dismiss: (t) => { t.dismissed = true; } },
    MM: { api: { post: () => Promise.resolve({}), get: () => Promise.resolve({ items: [] }), messageOf: (_e, f) => f } },
    addEventListener() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  };
  const told = [];
  doc.dispatchEvent = (e) => told.push([e.type, e.detail.id]);
  ctx.window = ctx;
  vm.createContext(ctx);
  return { ctx, doc, toasts, timers, told, run: () => ['core/dom.js', 'watch/core.js', 'watch/keep.js', 'watch/marks.js', 'watch/search.js', 'watch/seasons.js', 'watch.js']
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

const scenarios = {
  // The card an Undo puts back in a row, and where the row is scrolled for it.
  async undoneCardShown() {
    const go = async (left, grid) => {
      const page = load();
      const body = leavingRowPage(page.doc);
      page.doc.body.appendChild(body);
      if (grid) body.querySelector('.watch-row').className = 'watch-all__body';
      page.run();
      const scroller = page.doc.querySelector('.watch-row__scroll');
      const by = [];
      scroller.getBoundingClientRect = () => ({ top: 0, bottom: 20, left: 0, right: 390, width: 390, height: 20 });
      scroller.scrollBy = (to) => by.push([to.left, to.behavior]);
      const first = page.doc.querySelector('.watch-card');
      first.getBoundingClientRect = () => ({ top: 0, bottom: 20, left, right: left, width: 0, height: 20 });
      page.doc.activeElement = page.doc.body;
      click(page.doc, first.querySelector('[data-watch-keep]'));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      await settle();
      const focused = [];
      first.querySelector('[data-watch-keep]').focus = function (o) { focused.push(!!(o && o.preventScroll)); };
      page.toasts[0].opts.action.onClick();
      const out = { back: scroller.children[0] === first, by, focused, snap: scroller.style.scrollSnapType || '' };
      page.timers.forEach((fn) => fn());
      return { ...out, after: scroller.style.scrollSnapType || '' };
    };
    return { off: await go(-128), inView: await go(20), grid: await go(-128, true) };
  },

  // The badge of a show's card goes with the Keep of its season, and is back with an Undo.
  async seasonKept() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    page.doc.body.appendChild(build(page.doc, ['article', { class: 'watch-card', 'data-leaving-id': '1100', 'data-leaving-seasons': '[{"id":"1100"}]' },
      ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-badge--leaving' }, 'S2']]]));
    page.doc.body.appendChild(build(page.doc, ['article', { class: 'watch-card', 'data-leaving-id': '1100',
      'data-leaving-seasons': JSON.stringify([{ id: '1100', season: 1, days: 2 }, { id: '1101', season: 2, days: 5 }]) },
      ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-badge--leaving' }, 'S1']]]]));
    page.run();
    page.ctx.MMWatch.search = { leavingBadge: (item) => build(page.doc, ['span', { class: 'watch-badge--leaving' }, 'S' + item.leaving_season + ' in ' + item.leaving_days]) };
    const badges = () => page.doc.querySelectorAll('[data-leaving-id] .watch-badge--leaving').map((n) => n.textContent);
    click(page.doc, page.doc.querySelector('.watch-row [data-watch-keep]'));
    click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
    await settle();
    const kept = badges();
    page.toasts[0].opts.action.onClick();
    await settle();
    return [kept, badges()];
  },

  // The badge of a kept season, and the one that takes its place, as they change over.
  async seasonFades() {
    const go = async (still) => {
      const page = load({ motion: !still });
      page.doc.body.appendChild(leavingRowPage(page.doc));
      const runs = [];
      const badge = (text) => {
        const node = build(page.doc, ['span', { class: 'watch-badge--leaving' }, text]);
        node.animate = (frames, opts) => { const run = { frames, opts, cancel() { run.cancelled = true; } }; runs.push(run); return run; };
        return node;
      };
      const card = build(page.doc, ['article', { class: 'watch-card', 'data-leaving-id': '1100',
        'data-leaving-seasons': JSON.stringify([{ id: '1100', season: 1, days: 2 }, { id: '1101', season: 2, days: 5 }]) },
        ['a', { class: 'watch-card__link' }, ['span', { class: 'watch-card__poster' }]]]);
      card.querySelector('.watch-card__poster').appendChild(badge('S1'));
      page.doc.body.appendChild(card);
      page.run();
      page.ctx.MMWatch.search = { leavingBadge: (item) => badge('S' + item.leaving_season) };
      const badges = () => card.querySelectorAll('.watch-badge--leaving').map((n) => [n.textContent, n.hasAttribute('data-going'), n.classList.contains('fade-in')]);
      click(page.doc, page.doc.querySelector('.watch-row [data-watch-keep]'));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      await settle();
      const during = badges();
      if (!runs.length) return { during };
      const out = runs[0];
      page.toasts[0].opts.action.onClick();
      await settle();
      const undone = badges();
      out.onfinish(); // the fade the Undo took back
      const after = badges();
      runs[1].onfinish();
      return { during, frames: out.frames, undone, cancelled: !!out.cancelled, after, end: badges() };
    };
    return { moving: await go(false), still: await go(true) };
  },

  // The dialog a Keep's toast says it came from, so that Ctrl+Z reaches its Undo.
  async toastDialog() {
    const keepOnce = async (where) => {
      const page = load();
      page.doc.body.appendChild(build(page.doc, ['div', { class: 'ttl', id: 'ttl' }, ['p', {},
        ['button', { class: 'watch-keep', 'data-watch-keep': '9', 'aria-label': 'Keep Dune' }, 'Keep']]]));
      page.doc.body.appendChild(leavingRowPage(page.doc));
      if (where !== 'page') page.doc.documentElement.classList.add('has-ttl');
      page.run();
      click(page.doc, page.doc.querySelector(where === 'details' ? '#ttl [data-watch-keep]' : '.watch-card [data-watch-keep]'));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      await settle();
      const d = page.toasts[0].opts.dialog;
      return d ? d.id : null;
    };
    return { details: await keepOnce('details'), page: await keepOnce('page') };
  },

  // A finished Keep, a finished Undo and a Keep refused, as they are announced.
  async keepsTold() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    page.run();
    const keepFirst = async () => {
      click(page.doc, page.doc.querySelector('.watch-card [data-watch-keep]'));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      const early = page.told.length;
      await settle();
      return early;
    };
    const out = { early: await keepFirst(), kept: page.told.slice() };
    page.toasts[0].opts.action.onClick();
    await settle();
    out.undone = page.told.slice();
    page.ctx.MM.api.post = () => Promise.reject(new Error('no'));
    await keepFirst();
    out.refused = page.told.length;
    return out;
  },

  // Keep one of two seasons of a show, its button named as the server names it.
  async keptSeason() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    const card = page.doc.querySelector('.watch-card');
    card.querySelector('.watch-card__link').appendChild(build(page.doc, ['span', { class: 'watch-card__sub' }, 'Season 2']));
    card.querySelector('[data-watch-keep]').setAttribute('aria-label', 'Keep Glass Coast, Season 2');
    page.run();
    click(page.doc, card.querySelector('[data-watch-keep]'));
    click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
    await settle();
    return page.toasts.map((t) => t.message);
  },

  // Keep the first card of the Leaving soon row, with motion on.
  async cardExit() {
    const page = load({ motion: true });
    const tokens = { '--dur-fast': '150ms', '--dur': '200ms', '--dur-slow': '.25s' };
    page.ctx.getComputedStyle = () => ({ getPropertyValue: (n) => tokens[n] || '', display: 'block' });
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

  // What a block taller than nothing by its own rules is at the start and the end of its fold.
  async fold() {
    const page = load({ motion: true });
    page.ctx.getComputedStyle = () => ({ getPropertyValue: () => '', minHeight: '420px', paddingTop: '24px', paddingBottom: '44px', marginTop: '16px', marginBottom: '0px', rowGap: 'normal' });
    let frames = null;
    Node.prototype.animate = function (f) { frames = f; return { cancel() {} }; };
    const banner = page.doc.body.appendChild(build(page.doc, ['section', {}]));
    page.run();
    page.ctx.MMWatch.keep.foldAway(banner);
    const room = (f) => ['height', 'minHeight', 'paddingTop', 'paddingBottom', 'marginTop', 'marginBottom'].map((k) => parseFloat(f[k]));
    return frames.map(room);
  },

  // Where a row in a group that has no box of its own ends its fold, in a column of rows 30px apart.
  async foldInGroup() {
    const page = load({ motion: true });
    page.ctx.getComputedStyle = (n) => ({ getPropertyValue: () => '', ...(n.id === 'rows' ? { display: 'flex', rowGap: '30px' } : { display: n.id === 'group' ? 'contents' : 'block', rowGap: 'normal' }) });
    const ends = [];
    Node.prototype.animate = function (f) { ends.push([f[1].marginTop, f[1].marginBottom]); return { cancel() {} }; };
    page.doc.body.appendChild(build(page.doc, ['div', { id: 'rows' }, ['div', { id: 'group' }, ['section', { id: 'first' }], ['section', { id: 'next' }]]]));
    page.run();
    ['first', 'next'].forEach((id) => page.ctx.MMWatch.keep.foldAway(page.doc.getElementById(id)));
    return ends;
  },

  // The toasts still there: after an undo, after the next keep of that title and one of another, and once each is changed elsewhere.
  async oneToast() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    page.run();
    const left = () => page.toasts.filter((t) => !t.dismissed).map((t) => t.message);
    const out = [];
    const keep = async (id) => {
      click(page.doc, page.doc.querySelectorAll('[data-watch-keep]').find((b) => b.getAttribute('data-watch-keep') === id));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      await settle();
      out.push(left());
    };
    const said = (phase, id) => (page.doc.listeners['mm:watch-admin'] || []).forEach((fn) => fn({ detail: { phase, id } }));
    await keep('1100');
    const undo = page.toasts[0].opts.action.onClick;
    page.toasts[0].dismissed = true; // as a press on its button does
    undo();
    await settle();
    out.push(left());
    await keep('1100');
    await keep('1101');
    said('failed', '1101');
    said('busy', '1100');
    out.push(left());
    return out;
  },

  // The toasts still there after a keep in the admin's menus, and as the card's keep of that title and of another starts.
  async oneToastAcross() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    page.doc.dispatchEvent = (e) => (page.doc.listeners[e.type] || []).forEach((fn) => fn(e));
    page.run();
    vm.runInContext(fs.readFileSync(path.join(JS, 'watch/admin.js'), 'utf8'), page.ctx);
    const left = () => page.toasts.filter((t) => !t.dismissed).map((t) => t.message);
    const out = [];
    const start = (id) => {
      page.ctx.MM.api.post = () => new Promise(() => {});
      click(page.doc, page.doc.querySelectorAll('[data-watch-keep]').find((b) => b.getAttribute('data-watch-keep') === id));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      out.push(left());
    };
    await page.ctx.MMWatch.admin.keep({ id: '1100', title: 'Leaving Film', deletion: true }, '30 days');
    start('1101');
    start('1100');
    return out;
  },

  // A change of a whole show, announced with its seasons, takes away the toast of each.
  async showChanged() {
    const page = load();
    page.doc.body.appendChild(leavingRowPage(page.doc));
    page.run();
    for (const id of ['1100', '1101']) {
      click(page.doc, page.doc.querySelectorAll('[data-watch-keep]').find((b) => b.getAttribute('data-watch-keep') === id));
      click(page.doc, page.doc.querySelector('#watch-keep-menu [data-days]'));
      await settle();
    }
    (page.doc.listeners['mm:watch-admin'] || []).forEach((fn) => fn({ detail: { phase: 'busy', id: '', ids: ['1100', '1101'], showRk: '77' } }));
    return page.toasts.map((t) => !!t.dismissed);
  },

  // The grid-stack fade: both nodes share one cell while it runs, then only the new one is left.
  async swap() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function (frames, timing) {
      const anim = { node: this, frames, timing, cancel() {} };
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

  // Focus put in the new view while it fades in is still there once the fade has ended and it
  // has moved to where the old one was (a browser drops the focus of a node that moves).
  async swapFocus() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function () { const a = { node: this, cancel() {} }; anims.push(a); return a; };
    const remove = Node.prototype.remove;
    Node.prototype.remove = function () {
      if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
      remove.call(this);
    };
    page.run();
    const host = build(page.doc, ['div', { id: 'host' }, ['p', { id: 'old' }, 'old']]);
    page.doc.body.appendChild(host);
    const fresh = build(page.doc, ['p', { id: 'new' }, ['a', { id: 'go' }, 'Go']]);
    const done = page.ctx.MMWatch.core.swap(page.doc.getElementById('old'), fresh);
    page.doc.getElementById('go').focus();
    anims.find((a) => a.node !== fresh).onfinish();
    await done;
    return page.doc.activeElement.id;
  },

  // What has eased in, in the view that goes and in the one that comes, while both are moved into
  // the fade's cell and once the new one is moved out of it: what would ease in again.
  async swapEased() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function () { const a = { node: this, cancel() {} }; anims.push(a); return a; };
    page.run();
    const doc = page.doc;
    const host = build(doc, ['div', {}, ['p', { id: 'old', class: 'fade-in' }, ['b', { class: 'fade-in' }, 'old']]]);
    doc.body.appendChild(host);
    const oldNode = doc.getElementById('old');
    const eased = (n) => [n].concat(n.descendants()).filter((x) => x.classList.contains('fade-in')).length;
    const before = eased(host);
    const fresh = build(doc, ['p', { class: 'fade-in' }, ['b', { class: 'fade-in' }, 'new']]);
    const done = page.ctx.MMWatch.core.swap(oldNode, fresh);
    const going = eased(oldNode);
    anims.find((a) => a.node === oldNode).onfinish();
    await done;
    return { before, going, left: eased(host) };
  },

  // A drop or a newer swap while a fade runs wins: the older fade's end never puts back a dropped
  // view, nor loses the newer one (whichever fade ends first).
  async swapInterrupted() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function (frames) {
      const anim = { node: this, frames, cancel() {} };
      anims.push(anim);
      return anim;
    };
    page.run();
    const doc = page.doc;
    const swap = page.ctx.MMWatch.core.swap;
    const fadeOut = (node) => anims.find((a) => a.node === node && a.frames[0].opacity === 1).onfinish();
    const ids = (host) => host.descendants().filter((n) => /^v/.test(n.id)).map((n) => n.id);
    const stage = () => {
      const host = build(doc, ['div', { id: 'host' }, ['p', { id: 'v0' }, 'v0']]);
      doc.body.appendChild(host);
      return { host, v0: doc.getElementById('v0'), v1: build(doc, ['p', { id: 'v1' }, 'v1']), v2: build(doc, ['p', { id: 'v2' }, 'v2']) };
    };

    let t = stage();
    swap(t.v0, t.v1);
    t.v1.remove();
    fadeOut(t.v0);
    await settle();
    const dropped = ids(t.host);
    t.host.remove();

    const order = async (firstEnds) => {
      t = stage();
      swap(t.v0, t.v1);
      swap(t.v1, t.v2);
      const newerCell = t.v2.style.gridArea;
      const oneStack = t.v2.parentNode === t.v0.parentNode && t.v1.parentNode === t.v0.parentNode;
      const [a, b] = firstEnds === 'older' ? [t.v0, t.v1] : [t.v1, t.v0];
      fadeOut(a);
      await settle();
      const between = ids(t.host);
      fadeOut(b);
      await settle();
      const out = { newerCell, oneStack, between, after: ids(t.host), direct: t.v2.parentNode === t.host, cell: t.v2.style.gridArea || '' };
      t.host.remove();
      return out;
    };
    return { dropped, olderEndsFirst: await order('older'), newerEndsFirst: await order('newer') };
  },

  // A swap given the node still fading out (a caller that found it first) ends with one view, the
  // newest, in any order of fade ends; while it runs only the newest is not inert.
  async swapLeaving() {
    const page = load({ motion: true });
    const anims = [];
    Node.prototype.animate = function (frames) {
      const anim = { node: this, frames, cancel() { this.cancelled = true; } };
      anims.push(anim);
      return anim;
    };
    page.run();
    const doc = page.doc;
    const swap = page.ctx.MMWatch.core.swap;
    const ids = (host) => host.descendants().filter((n) => /^v/.test(n.id)).map((n) => n.id);
    const run = async (endFirst) => {
      anims.length = 0;
      const host = build(doc, ['div', { id: 'host' }, ['p', { id: 'v0' }, 'v0']]);
      doc.body.appendChild(host);
      const v = [doc.getElementById('v0'), build(doc, ['p', { id: 'v1' }, 'v1']), build(doc, ['p', { id: 'v2' }, 'v2']), build(doc, ['p', { id: 'v3' }, 'v3'])];
      let done = swap(v[0], v[1]);
      done = swap(v[0], v[2]);
      swap(v[0], v[3]);
      const during = { shown: v.filter((n) => !n.hasAttribute('inert') && host.contains(n)).map((n) => n.id), all: ids(host) };
      const outs = anims.filter((a) => a.frames[1].opacity === 0 && !a.cancelled);
      (endFirst === 'oldest' ? outs : outs.slice().reverse()).forEach((a) => a.onfinish && a.onfinish());
      await done;
      const out = { during, after: ids(host), direct: v[3].parentNode === host, inert: v.some((n) => n.hasAttribute('inert')) };
      host.remove();
      return out;
    };
    return { oldestEndsFirst: await run('oldest'), newestEndsFirst: await run('newest') };
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
    Node.prototype.animate = function (frames) { anims.push({ node: this, frames }); return { cancel: () => {} }; };
    page.run();
    const doc = page.doc;
    const a = build(doc, ['i', { id: 'a' }]);
    const b = build(doc, ['i', { id: 'b' }]);
    const at = { a: { left: 0, top: 0 }, b: { left: 50, top: 0 } };
    [a, b].forEach((n) => { n.getBoundingClientRect = () => at[n.id]; });
    const play = page.ctx.MMWatch.core.flip([a, b]);
    at.a = { left: 30, top: 10 };
    play();
    const moved = anims.map((x) => [x.node.id, x.frames[0].transform, x.frames[1].transform]);
    // Seen halfway, it is moved again.
    let way = { left: -15, top: -5 };
    const rest = { left: 30, top: 10 };
    Node.prototype.animate = function (frames) { anims.push({ node: this, frames }); return { cancel: () => { way = { left: 0, top: 0 }; } }; };
    a.getBoundingClientRect = () => ({ left: rest.left + way.left, top: rest.top + way.top });
    way = { left: 0, top: 0 };
    const begin = page.ctx.MMWatch.core.flip([a]);
    rest.left = 60;
    begin();
    way = { left: -15, top: -5 };
    const on = page.ctx.MMWatch.core.flip([a]);
    rest.left = 80;
    on();
    return { moved, again: anims[anims.length - 1].frames[0].transform };
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

  // The season tabs as one scrolling line: where it stands at first and after a press on a tab
  // that is cut off, then on one wholly in view.
  async seasonLine() {
    const page = load({ motion: true });
    page.doc.body.appendChild(showPage(page.doc, 1, leavingLine(3)));
    const nav = page.doc.querySelector('.watch-seasons');
    const moves = [];
    nav.scrollLeft = 0;
    nav.scrollTo = (o) => { nav.scrollLeft = o.left; moves.push(o); };
    Object.defineProperty(nav, 'clientWidth', { value: 300 });
    [1, 2].forEach((n) => { pill(page.doc, n).offsetLeft = n === 1 ? 0 : 400; Object.defineProperty(pill(page.doc, n), 'offsetWidth', { value: 100 }); });
    page.run();
    const out = { row: nav.parentNode.classList.contains('watch-row__viewport') && nav.classList.contains('watch-row__scroll'), opened: moves.splice(0) };
    click(page.doc, pill(page.doc, 2));
    await settle();
    out.cutOff = moves.splice(0);
    return out;
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
