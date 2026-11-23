// Runs the real watch.js against a tiny fake DOM and prints the outcome of one
// scenario as JSON. Usage: node watch_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

/* ── Fake DOM ── */

function parseCompound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /^([a-z]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/gi;
  let m;
  while ((m = re.exec(text))) {
    if (m[1]) out.tag = m[1].toUpperCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else out.attrs.push([m[4], m[5]]);
  }
  return out;
}

function matchesCompound(el, c) {
  if (el.nodeType !== 1) return false;
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  if (!c.classes.every((k) => el.classList.contains(k))) return false;
  return c.attrs.every(([k, v]) => el.hasAttribute(k) && (v === undefined || el.getAttribute(k) === v));
}

function matchesSelector(el, sel) {
  return sel.split(',').some((one) => {
    const parts = one.trim().split(/\s+/).map(parseCompound);
    if (!matchesCompound(el, parts[parts.length - 1])) return false;
    let i = parts.length - 2;
    for (let a = el.parentNode; a && i >= 0; a = a.parentNode) {
      if (matchesCompound(a, parts[i])) i--;
    }
    return i < 0;
  });
}

class Node {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.nodeType = tag.toLowerCase() === '#text' ? 3 : 1;
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this.style = {};
    this._text = '';
    this.inert = false;
  }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (...cs) => { el.className = [...new Set(list().concat(cs))].join(' '); },
      remove: (...cs) => { el.className = list().filter((x) => !cs.includes(x)).join(' '); },
      toggle: (c, on) => {
        const want = on === undefined ? !list().includes(c) : on;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }
  get tabIndex() { return this.hasAttribute('tabindex') ? Number(this.attrs.tabindex) : -1; }
  set tabIndex(v) { this.attrs.tabindex = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get textContent() { return this.nodeType === 3 ? this._text : this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(t) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    if (this.nodeType === 3) { this._text = String(t); return; }
    if (t !== '' && t !== null && t !== undefined) this.appendChild(this.ownerDocument.createTextNode(String(t)));
  }
  sibling(step, elementsOnly) {
    if (!this.parentNode) return null;
    const list = this.parentNode.childNodes;
    for (let i = list.indexOf(this) + step; i >= 0 && i < list.length; i += step) {
      if (!elementsOnly || list[i].nodeType === 1) return list[i];
    }
    return null;
  }
  get nextSibling() { return this.sibling(1, false); }
  get nextElementSibling() { return this.sibling(1, true); }
  get previousElementSibling() { return this.sibling(-1, true); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.remove();
    c.parentNode = this;
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(c); else this.childNodes.splice(i, 0, c);
    return c;
  }
  remove() {
    if (!this.parentNode) return;
    const p = this.parentNode;
    p.childNodes.splice(p.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  before(c) { this.parentNode.insertBefore(c, this); }
  replaceWith(c) { const p = this.parentNode; p.insertBefore(c, this); this.remove(); }
  contains(n) { for (let a = n; a; a = a.parentNode) if (a === this) return true; return false; }
  descendants() { return this.childNodes.flatMap((c) => (c.nodeType === 1 ? [c, ...c.descendants()] : [])); }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) { for (let a = this; a && a.nodeType === 1; a = a.parentNode) if (a.matches(sel)) return a; return null; }
  querySelectorAll(sel) { return this.descendants().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { this.ownerDocument.activeElement = this.ownerDocument.body; }
  getBoundingClientRect() { return { top: 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 }; }
  getClientRects() { return this.ownerDocument.body.contains(this) ? [this.getBoundingClientRect()] : []; }
  get offsetWidth() { return 20; }
  get offsetHeight() { return 20; }
  cloneNode() {
    const copy = this.ownerDocument.createElement(this.tagName);
    copy.attrs = { ...this.attrs };
    copy._text = this._text;
    copy.nodeType = this.nodeType;
    this.childNodes.forEach((c) => copy.appendChild(c.cloneNode(true)));
    return copy;
  }
}

function makeDocument() {
  const doc = {
    listeners: {},
    createElement: (t) => new Node(doc, t),
    createTextNode: (t) => { const n = new Node(doc, '#text'); n._text = t; return n; },
    addEventListener: (t, fn) => { (doc.listeners[t] = doc.listeners[t] || []).push(fn); },
    getElementById: (id) => doc.documentElement.querySelector('#' + id),
    querySelector: (s) => doc.documentElement.querySelector(s),
    querySelectorAll: (s) => doc.documentElement.querySelectorAll(s),
  };
  doc.documentElement = new Node(doc, 'html');
  doc.body = doc.documentElement.appendChild(new Node(doc, 'body'));
  doc.activeElement = doc.body;
  doc.importNode = (n) => {
    const copy = n.cloneNode(true);
    const adopt = (x) => { x.ownerDocument = doc; x.childNodes.forEach(adopt); };
    adopt(copy);
    return copy;
  };
  return doc;
}

/* Build markup from [tag, attrs, ...children] arrays; a string is a text node. */
function build(doc, spec) {
  if (typeof spec === 'string') return doc.createTextNode(spec);
  const [tag, attrs, ...kids] = spec;
  const el = doc.createElement(tag);
  Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
  kids.forEach((k) => el.appendChild(build(doc, k)));
  return el;
}

/* Bubble a click from *target* up to the document. */
function click(doc, target) {
  const e = { target, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (let a = target; a; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e));
  (doc.listeners.click || []).forEach((fn) => fn(e));
}

const flush = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await flush(); }

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
  return { ctx, doc, toasts, timers, run: () => vm.runInContext(fs.readFileSync(path.join(JS, 'watch.js'), 'utf8'), ctx) };
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
      centred: msg.classList.contains('watch-results__msg--failed'),
      icon: msg.children[0].tagName === 'I' && msg.children[0].getAttribute('aria-hidden') === 'true',
      line: msg.querySelector('.watch-results__line') && msg.querySelector('.watch-results__line').textContent,
      status: page.doc.getElementById('watch-search-status').textContent,
    };
    click(page.doc, msg.querySelector('button'));
    await settle();
    out.retried = calls === 2;
    out.focus = page.doc.activeElement === input;
    return out;
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
