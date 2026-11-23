// Runs the real dashboard script against a tiny fake DOM and prints the
// outcome of one scenario as JSON. Usage: node dashboard_harness.js <scenario>
//
// The server's page is described as JSON ({"tiles": ["b", "c"]}); the fake
// DOMParser builds the fragments from it, so each scenario says what the
// server answers and when.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = process.env.DASH_JS_DIR || path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

const camelToData = (k) => 'data-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());

class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = [];
    this.parentNode = null; this.attrs = {}; this.style = {}; this._text = '';
    const el = this;
    this.dataset = new Proxy({}, {
      get: (_, k) => (typeof k === 'string' ? el.attrs[camelToData(k)] : undefined),
      set: (_, k, v) => { el.attrs[camelToData(k)] = String(v); return true; },
      deleteProperty: (_, k) => { delete el.attrs[camelToData(k)]; return true; },
    });
  }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (...cs) => { el.className = [...new Set(list().concat(cs))].join(' '); },
      remove: (...cs) => { el.className = list().filter((x) => !cs.includes(x)).join(' '); },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c); c.parentNode = null;
    if (c.contains(this.doc.activeElement)) this.doc.activeElement = this.doc.body;
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  before(n) {
    if (n.parentNode) n.parentNode.removeChild(n);
    const p = this.parentNode; n.parentNode = p; p.children.splice(p.children.indexOf(this), 0, n);
  }
  replaceWith(n) { const p = this.parentNode; this.before(n); p.removeChild(this); }
  get nextElementSibling() { const s = this.parentNode.children; return s[s.indexOf(this) + 1] || null; }
  get previousElementSibling() { const s = this.parentNode.children; return s[s.indexOf(this) - 1] || null; }
  get isConnected() { let e = this; while (e.parentNode) e = e.parentNode; return e === this.doc.body; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  contains(o) { for (let e = o; e; e = e.parentNode) if (e === this) return true; return false; }
  matchesOne(sel) {
    const parts = sel.match(/[.#]?[\w-]+|\[[^\]]+\]/g) || [];
    return parts.every((p) => {
      if (p[0] === '.') return this.classList.contains(p.slice(1));
      if (p[0] === '#') return this.id === p.slice(1);
      if (p[0] === '[') {
        const m = p.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
        const v = this.getAttribute(m[1]);
        return v !== null && (m[2] === undefined || v === m[2]);
      }
      return this.tagName === p.toUpperCase();
    });
  }
  matches(sel) { return sel.split(',').some((s) => this.matchesOne(s.trim())); }
  closest(sel) { for (let e = this; e && e.tagName; e = e.parentNode) if (e.matches(sel)) return e; return null; }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  isEqualNode(o) { return o && this.serialize() === o.serialize(); }
  serialize() {
    const a = Object.keys(this.attrs).sort().map((k) => k + '=' + this.attrs[k]).join(' ');
    return '<' + this.tagName + ' ' + a + '>' + this._text + this.children.map((c) => c.serialize()).join('') + '</>';
  }
  /* A control inside a closed menu is hidden, so it cannot take focus. */
  focus() {
    const menu = this.closest('.snooze-dropdown');
    if (!this.isConnected || (menu && !menu.parentNode.classList.contains('is-open'))) return;
    this.doc.activeElement = this;
  }
}

function makeDoc() {
  const listeners = {};
  const doc = { addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); } };
  doc.createElement = (t) => new El(t, doc);
  doc.createTextNode = (t) => { const e = new El('#text', doc); e._text = t; return e; };
  doc.body = new El('body', doc);
  doc.activeElement = doc.body;
  doc.querySelectorAll = (s) => doc.body.querySelectorAll(s);
  doc.querySelector = (s) => doc.body.querySelector(s);
  doc.getElementById = (id) => doc.body.all().find((e) => e.id === id) || null;
  doc.importNode = (n) => { [n, ...n.all()].forEach((e) => { e.doc = doc; }); return n; };
  doc.fire = (t, e) => (listeners[t] || []).forEach((f) => f(e));
  return doc;
}

function el(doc, tag, attrs, kids, text) {
  const e = new El(tag, doc);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
  (kids || []).forEach((k) => e.appendChild(k));
  if (text) e._text = text;
  return e;
}

/* The fragments the server renders for a list of scheduled tiles. */
function fragments(doc, tiles) {
  const tile = (id) => el(doc, 'div', { class: 'tile' }, [
    el(doc, 'div', { class: 'keep-wrapper tile-actions', 'data-id': id }, [
      el(doc, 'button', { class: 'btn', 'data-action': 'keep-tile', 'data-duration': '30 days' }, [], 'Keep 30 days'),
      el(doc, 'button', { class: 'btn btn-sm-caret', 'aria-expanded': 'false' }),
      el(doc, 'div', { class: 'snooze-dropdown', role: 'menu' }, ['7 days', '90 days', 'forever'].map((d) =>
        el(doc, 'button', { class: 'snooze-option', 'data-action': 'keep-tile', 'data-duration': d }, [], d))),
    ]),
  ]);
  return [
    el(doc, 'div', { 'data-fragment': 'summary' }, [], tiles.length + ' scheduled'),
    el(doc, 'section', { 'data-fragment': 'scheduled' }, [
      el(doc, 'div', { class: 'sec-hd' }, [el(doc, 'h2', {}, [], 'Scheduled for deletion')]),
      tiles.length ? el(doc, 'div', { class: 'grid grid-tiles' }, tiles.map(tile)) : el(doc, 'div', { class: 'empty' }),
    ]),
  ];
}

const flush = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
const deferred = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };

function build(tiles) {
  const doc = makeDoc();
  fragments(doc, tiles).forEach((f) => doc.body.appendChild(f));
  ['scan-btn', 'scan-status', 'scan-live'].forEach((id) => doc.body.appendChild(el(doc, 'span', { id, 'data-action': id === 'scan-btn' ? 'trigger-scan' : '' })));
  const fetches = [];
  const keeps = [];
  const timers = [];
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Date, parseFloat, AbortController,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    location: { pathname: '/', href: '/' },
    matchMedia: () => ({ matches: true }),
    fetch: () => { const d = deferred(); fetches.push(d); return d.p; },
    DOMParser: class {
      parseFromString(html) {
        const page = makeDoc();
        const spec = JSON.parse(html);
        if (spec.tiles) fragments(page, spec.tiles).forEach((f) => page.body.appendChild(f));
        return page;
      }
    },
    UIFeedback: { success() {}, error() {}, toast() {} },
    SnoozeMenu: {
      closeAll() {
        doc.querySelectorAll('.keep-wrapper').forEach((w) => w.classList.remove('is-open'));
        const menu = doc.activeElement.closest && doc.activeElement.closest('.snooze-dropdown');
        if (menu) doc.activeElement = doc.body;
      },
    },
    MM: {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'dashboard.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.MM.api = {
    postForm: () => { const d = deferred(); keeps.push(d); return d.p; },
    post: () => Promise.resolve({}),
    get: () => Promise.resolve({ running: false, status: 'done' }),
    messageOf: (_, f) => f,
  };
  const page = (spec) => ({ ok: true, redirected: false, url: 'http://h/', text: () => Promise.resolve(JSON.stringify(spec)) });
  const wrapper = (id) => doc.querySelector('[data-id="' + id + '"]');
  const keepBtn = (id, d) => wrapper(id).querySelectorAll('[data-duration="' + (d || '30 days') + '"]')[0];
  const click = (t) => doc.fire('click', { target: t });
  const tilesNow = () => doc.querySelectorAll('.keep-wrapper').map((w) => w.dataset.id);
  const focused = () => {
    const a = doc.activeElement;
    const w = a.closest && a.closest('[data-id]');
    return { id: w ? w.dataset.id : null, tag: a.tagName, text: a.textContent, inPage: a.isConnected };
  };
  const runTimers = () => timers.splice(0).forEach((f) => f());
  return { doc, ctx, fetches, keeps, page, wrapper, keepBtn, click, tilesNow, focused, runTimers };
}

const scenarios = {
  // keyboard keep from the main button: focus lands on the next tile's button
  async focusButton() {
    const h = build(['a', 'b', 'c']);
    h.keepBtn('a').focus();
    h.click(h.keepBtn('a'));
    h.keeps[0].resolve({});
    await flush();
    h.fetches[0].resolve(h.page({ tiles: ['b', 'c'] }));
    await flush();
    return { focused: h.focused(), tiles: h.tilesNow() };
  },

  // keep picked from the menu: closing the menu must not lose the place
  async focusMenu() {
    const h = build(['a', 'b', 'c']);
    h.wrapper('a').classList.add('is-open');
    h.keepBtn('a', '7 days').focus();
    h.click(h.keepBtn('a', '7 days'));
    h.keeps[0].resolve({});
    await flush();
    h.fetches[0].resolve(h.page({ tiles: ['b', 'c'] }));
    await flush();
    return { focused: h.focused() };
  },

  // a failed keep leaves focus on the main button; from the menu it goes to the caret
  async failedKeep() {
    const h = build(['a', 'b']);
    h.keepBtn('a').focus();
    h.click(h.keepBtn('a'));
    h.keeps[0].reject(new Error('x'));
    await flush();
    const fromButton = h.focused();
    h.wrapper('b').classList.add('is-open');
    h.keepBtn('b', '90 days').focus();
    h.click(h.keepBtn('b', '90 days'));
    h.keeps[1].reject(new Error('x'));
    await flush();
    const a = h.doc.activeElement;
    return { fromButton, fromMenu: { id: h.focused().id, caret: a.classList.contains('btn-sm-caret') } };
  },

  // a scan finishes while a keep is in flight, then the keep fails
  async scanDuringFailedKeep() {
    const h = build(['a', 'b']);
    h.click(h.keepBtn('a'));
    h.click(h.doc.getElementById('scan-btn'));
    await flush();
    h.runTimers();          // the first status poll says the scan is done
    await flush();
    h.fetches.splice(0).forEach((f) => f.resolve(h.page({ tiles: ['a'] })));
    await flush();
    h.keeps[0].reject(new Error('x'));
    await flush();
    h.fetches.splice(0).forEach((f) => f.resolve(h.page({ tiles: ['a'] })));
    await flush();
    return { tiles: h.tilesNow() };
  },

  // an older refresh answering last does not bring a kept tile back
  async staleRefresh() {
    const h = build(['a', 'b', 'c']);
    h.click(h.keepBtn('a'));
    h.keeps[0].resolve({});
    await flush();
    const older = h.fetches[0];
    h.click(h.keepBtn('b'));
    h.keeps[1].resolve({});
    await flush();
    h.fetches.slice(1).forEach((f) => f.resolve(h.page({ tiles: ['c'] })));
    await flush();
    older.resolve(h.page({ tiles: ['b', 'c'] }));
    await flush();
    h.fetches.slice(2).forEach((f) => f.resolve(h.page({ tiles: ['c'] })));
    await flush();
    return { tiles: h.tilesNow() };
  },

  // the session ended: the refresh lands on the login page
  async expired() {
    const run = async (res) => {
      const h = build(['a', 'b']);
      h.click(h.keepBtn('a'));
      h.keeps[0].resolve({});
      await flush();
      h.fetches[0].resolve(res);
      await flush();
      return h.ctx.location.href;
    };
    const login = { ok: true, text: () => Promise.resolve('{}') };
    return {
      redirected: await run({ ...login, redirected: true, url: 'http://h/login' }),
      noFragments: await run({ ...login, redirected: false, url: 'http://h/' }),
    };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
