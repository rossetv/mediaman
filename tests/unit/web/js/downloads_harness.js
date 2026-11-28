// Runs the real abandon dialog, shared modal and re-authenticate scripts against a tiny
// fake DOM and prints the outcome of one scenario as JSON. Usage: node downloads_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = process.env.DL_JS_DIR || path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = [];
    this.parentNode = null; this.attrs = {}; this.style = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.id = ''; this.dataset = {};
  }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(' '); },
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(c) : !!force;
        if (on) el.classList.add(c); else el.classList.remove(c);
        return on;
      },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) {
    if (k === 'id') return this.id || null;
    if (k.startsWith('data-') && !(k in this.attrs)) {
      const key = k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());
      return key in this.dataset ? this.dataset[key] : null;
    }
    return k in this.attrs ? this.attrs[k] : null;
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set src(v) { this.attrs.src = v; }
  get src() { return this.attrs.src; }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() {}
  contains(n) { for (let a = n; a; a = a.parentNode) if (a === this) return true; return false; }
  cloneNode() {
    const copy = new El(this.tagName, this.doc);
    copy.attrs = { ...this.attrs }; copy.className = this.className; copy._text = this._text; copy.hidden = this.hidden;
    this.children.forEach((c) => copy.appendChild(c.cloneNode()));
    return copy;
  }
  replaceChildren() { this.children = []; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  after(c) { const p = this.parentNode; const i = p.children.indexOf(this); c.parentNode = p; p.children.splice(i + 1, 0, c); }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  // what the browser sends when a transition or animation on this element ends
  emit(t, extra) { (this.listeners[t] || []).slice().forEach((f) => f({ target: this, ...extra })); }
  // a layout read: a transform set before it is where a slide starts, and a height set before it
  // is where a fold eases from
  get offsetHeight() { if (this.style.transform) this.slideStart = this.style.transform; this.laidHeight = this.style.height; return this.hidden ? 0 : this.style.height ? parseFloat(this.style.height) : 50; }
  // A queue row stands 100px tall, and 43px more while its search line shows, or where a fold
  // under way starts from; rows stack.
  get rowHeight() {
    if (this.classList.contains('dl-fold') && this.laidHeight) return parseFloat(this.laidHeight);
    return 100 + (this.querySelectorAll('[data-v="search-hint"]').some((h) => !h.hidden) ? 43 : 0); }
  getBoundingClientRect() {
    const p = this.parentNode;
    if (!p || p.id !== 'dl-queue-list') return { height: this.offsetHeight, top: 0 };
    const above = p.children.slice(0, p.children.indexOf(this));
    const shift = /translateY\((-?[\d.]+)px\)/.exec(this.style.transform || '');
    return { height: this.rowHeight, top: above.reduce((sum, r) => sum + r.rowHeight, 0) + (shift ? Number(shift[1]) : 0) };
  }
  get nextElementSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  closest(sel) { let e = this; while (e && e.matches) { if (e.matches(sel)) return e; e = e.parentNode; } return null; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(sel) {
    let m;
    if ((m = sel.match(/^\.([\w-]+)$/))) return this.classList.contains(m[1]);
    if ((m = sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/))) {
      const v = this.getAttribute(m[1]);
      return v !== null && (m[2] === undefined || v === m[2]);
    }
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function build(pollGet) {
  const listeners = {};
  const doc = {
    hidden: false, documentElement: { clientWidth: 1280, classList: { add() {}, remove() {} } },
    createElement: (t) => new El(t, doc),
    createElementNS: (ns, t) => new El(t, doc),
    createTextNode: (t) => { const e = new El('#text', doc); e._text = t; return e; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    getElementById: (id) => root.all().find((e) => e.id === id) || null,
  };
  const root = new El('div', doc);
  root.setAttribute('id', 'dl-root');
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, AbortController,
    setTimeout: (fn) => { fn.tid = ++ctx.timerSeq; ctx.timers.push(fn); return fn.tid; },
    clearTimeout: (id) => { ctx.timers = ctx.timers.filter((f) => f.tid !== id); }, timerSeq: 0, CustomEvent: class {},
    timers: [], MM: {}, MM_API: pollGet, reduce: false,
    matchMedia: () => ({ matches: ctx.reduce }),
    getComputedStyle: (e) => ({ opacity: e.style.opacity || '1', paddingTop: '0px', paddingBottom: '0px', marginTop: '0px', marginBottom: '0px' }),
  };
  ctx.window = ctx; ctx.MM.api = { get: pollGet };
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  doc.getElementById = (id) => (id === 'dl-root' ? root : root.all().find((e) => e.id === id) || null);
  return { ctx, doc, root, load, listeners, fire: (t, e) => (listeners[t] || []).forEach((f) => f({ preventDefault() {}, ...e })) };
}

const scenarios = {};

// The abandon dialog on fake parts: `post` hands back each request so a test
// settles it when it likes, `closes` counts the times the dialog shut itself.
function abandonRig(named) {
  const h = build(() => Promise.resolve({}));
  const mk = (cls) => { const e = new El('div', h.doc); e.className = cls || ''; return e; };
  const modal = mk(); modal.setAttribute('id', 'abandon-modal');
  const title = mk(); const copy = mk(); const list = mk();
  // *named*: the dialog names what it is of, with its poster.
  const who = mk('abandon-who');
  ['abandon-who__pic', 'abandon-who__name', 'abandon-who__fact'].forEach((cls) => who.appendChild(mk(cls)));
  const confirm = mk(); confirm.setAttribute('data-abandon-confirm', ''); confirm.setAttribute('aria-disabled', 'false');
  const label = mk(); label.setAttribute('data-confirm-label', ''); confirm.appendChild(label);
  modal.appendChild(confirm);
  const byId = { 'abandon-modal': modal, 'abandon-modal-title': title, 'abandon-modal-copy': copy, 'abandon-season-list': list };
  if (named) byId['abandon-modal-who'] = who;
  h.doc.getElementById = (id) => byId[id] || null;
  h.ctx.MM.dom = { q: (sel, c) => c.querySelector(sel), holdPage: () => () => {} };
  let dismissFn;
  const rig = { h, who, label, confirm, list, copy, posts: [], closes: 0, refreshes: 0, errors: 0, toasts: [] };
  h.ctx.MM.modal = {
    setupDetail: (_m, opts) => (dismissFn = () => opts.onClose(), {
      open() {},
      close() { rig.closes += 1; opts.onClose(); },
      // what the modal does on Escape or the backdrop
      dismiss() { opts.onClose(); },
    }),
  };
  h.ctx.MM.api.post = (url, body) => new Promise((resolve, reject) => { rig.posts.push({ url, body, resolve, reject }); });
  h.ctx.MM.api.messageOf = () => 'nope';
  h.ctx.UIFeedback = { error() { rig.errors += 1; }, toast(text, o) { rig.toasts.push([text, o.variant]); } };
  h.ctx.CustomEvent = class { constructor(t) { this.type = t; } };
  h.doc.dispatchEvent = (e) => { if (e.type === 'mediaman:downloads:refresh') rig.refreshes += 1; };
  h.load('dl-abandon.js');
  rig.openItem = (dlId, kind, stuck, title, arrId, upcoming, poster) => {
    const t = mk(); t.dataset = { dlId, kind, title: title || 'T' + dlId, arrId, stuckSeasons: JSON.stringify(stuck || []), poster };
    if (upcoming) t.dataset.abandonUpcoming = '1';
    t.closest = () => t;
    h.fire('click', { target: t });
  };
  rig.dismiss = () => dismissFn();
  rig.confirmClick = () => confirm.listeners.click[0]();
  rig.togs = () => list.querySelectorAll('.tog');
  rig.flip = (i) => rig.togs()[i].listeners.click[0]();
  rig.settle = () => new Promise((r) => setImmediate(r));
  return rig;
}
const SEASONS = [{ number: 1, missing_episodes: 2 }, { number: 2, missing_episodes: 3 }];
const status = (r) => ({ label: r.label.textContent, disabled: r.confirm.getAttribute('aria-disabled') });

scenarios.abandon = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  const idle = r.label.textContent;
  r.confirmClick();
  const working = r.label.textContent;
  const disabled = r.confirm.getAttribute('aria-disabled');
  r.posts[0].reject(new Error('x'));
  await r.settle();
  return { idle, working, disabled, after: r.label.textContent, reenabled: r.confirm.getAttribute('aria-disabled') };
};

// the poster the dialog shows is only ever one of mediaman's own
scenarios.abandonPoster = async function () {
  const r = abandonRig(true);
  const shown = (poster) => {
    r.openItem('m1', 'movie', [], 'Film', 1, false, poster);
    const img = r.who.querySelector('img');
    return img ? img.getAttribute('src') : null;
  };
  return Object.fromEntries(['/p/1.jpg', '//host/x.jpg', '/\\host/x.jpg', 'https://host/x.jpg', '/a b.jpg'].map((p) => [p, shown(p)]));
};

// the season picker is shown for a multi-season show only
scenarios.abandonList = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  const movie = !!r.list.hidden;
  r.openItem('s1', 'series', SEASONS);
  const picker = !!r.list.hidden;
  r.openItem('m2', 'movie');
  return { movie, picker, movieAgain: !!r.list.hidden };
};

scenarios.abandonSuccess = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  r.confirmClick();
  r.posts[0].resolve({});
  await r.settle();
  return { url: r.posts[0].url, closes: r.closes, refreshes: r.refreshes, errors: r.errors };
};

// what is sent carries the Radarr or Sonarr id of the button that opened the dialog
scenarios.abandonSendsTheArrId = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie', [], 'Twin', '21');
  r.confirmClick();
  return r.posts[0].body;
};

// another tab abandoned it first
scenarios.abandonGone = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  r.confirmClick();
  r.posts[0].reject(Object.assign(new Error('gone'), { status: 404 }));
  await r.settle();
  return { closes: r.closes, refreshes: r.refreshes, errors: r.errors, toasts: r.toasts };
};

// it started downloading, or stopped searching, after the dialog opened: the server refuses
scenarios.abandonNoLongerStuck = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  r.confirmClick();
  r.posts[0].reject(Object.assign(new Error('refused'), { status: 409 }));
  await r.settle();
  return { closes: r.closes, refreshes: r.refreshes, errors: r.errors, toasts: r.toasts };
};

// what the dialog tells, for a film and a show, tracked or not yet in the library
scenarios.abandonWords = async function () {
  const r = abandonRig();
  const words = {};
  [['movie', false], ['movie', true], ['series', false], ['series', true]].forEach(([kind, upcoming]) => {
    r.openItem('x', kind, [], 'Tx', 1, upcoming);
    words[kind + (upcoming ? ' upcoming' : '')] = r.copy.textContent;
  });
  return words;
};

// a failed Stop tracking says so, and keeps the server's words when it sent some
scenarios.abandonFailureWords = async function () {
  const r = abandonRig();
  const said = [];
  r.h.ctx.UIFeedback.error = (m) => said.push(m);
  r.h.ctx.MM.api.messageOf = (err, fallback) => (err.message === 'server' ? 'Radarr is down.' : fallback);
  const fail = async (upcoming, message) => {
    r.openItem('a' + said.length, 'movie', [], 'Tx', 1, upcoming);
    r.confirmClick();
    r.posts[r.posts.length - 1].reject(new Error(message));
    await r.settle();
  };
  await fail(true, 'none');
  await fail(false, 'none');
  await fail(true, 'server');
  return said;
};

// A fails after the dialog moved on to B: B's button is left alone
scenarios.abandonStaleFailure = async function () {
  const r = abandonRig();
  r.openItem('a', 'series', SEASONS);
  r.confirmClick();
  r.dismiss();
  r.openItem('b', 'movie');
  const before = status(r);
  r.posts[0].reject(new Error('x'));
  await r.settle();
  return { before, after: status(r), errors: r.errors };
};

// A succeeds after the dialog moved on to B: B's dialog stays open
scenarios.abandonStaleSuccess = async function () {
  const r = abandonRig();
  r.openItem('a', 'series', SEASONS);
  r.confirmClick();
  r.dismiss();
  r.openItem('b', 'movie');
  r.posts[0].resolve({});
  await r.settle();
  return { closes: r.closes, refreshes: r.refreshes, b: status(r) };
};

// reopening A while its request runs shows it busy and sends nothing more
scenarios.abandonReopen = async function () {
  const r = abandonRig();
  r.openItem('a', 'series', SEASONS);
  r.confirmClick();
  r.dismiss();
  r.openItem('a', 'series', SEASONS);
  const reopened = status(r);
  r.confirmClick();
  const switchesLocked = r.togs().map((t) => t.getAttribute('aria-disabled'));
  r.posts[0].resolve({});
  await r.settle();
  return { posts: r.posts.length, reopened, switchesLocked, closes: r.closes };
};

// season switches do nothing while the request runs; a failure shows the current count
scenarios.abandonSwitchesLocked = async function () {
  const r = abandonRig();
  r.openItem('a', 'series', SEASONS);
  const picked = r.label.textContent;
  r.flip(0);
  const oneLeft = r.label.textContent;
  r.confirmClick();
  r.flip(0);
  const during = { label: r.label.textContent, on: r.togs().map((t) => t.classList.contains('on')) };
  r.posts[0].reject(new Error('x'));
  await r.settle();
  return { picked, oneLeft, during, after: status(r) };
};

// what the season picker starts with, and which seasons a confirm sends
// the only season of a show is still to air: it is offered unticked; one with gaps is sent at once
scenarios.abandonOneSeason = async function () {
  const r = abandonRig();
  r.openItem('s1', 'series', [{ number: 2, missing_episodes: 0, aired: false }]);
  const unaired = { listed: !r.list.hidden, on: r.togs().map((t) => t.classList.contains('on')), ...status(r) };
  r.confirmClick();
  const sentUnticked = r.posts.length;
  r.flip(0);
  r.confirmClick();
  r.dismiss();
  r.openItem('s2', 'series', [{ number: 3, missing_episodes: 4, aired: true }]);
  const aired = { listed: !r.list.hidden, ...status(r) };
  r.confirmClick();
  return { unaired, sentUnticked, aired, seasons: r.posts.map((p) => p.body.seasons) };
};

scenarios.abandonSeasons = async function () {
  const r = abandonRig();
  r.openItem('s1', 'series', [
    { number: 0, missing_episodes: 4, aired: true },
    { number: 1, missing_episodes: 1, aired: true },
    { number: 2, missing_episodes: 6, aired: true },
    { number: 3, missing_episodes: 0, aired: true },
    { number: 4, missing_episodes: 0, aired: false },
  ]);
  const rows = r.list.querySelectorAll('.setg-row').map((row) => [
    row.querySelector('.setg-row-lbl').textContent,
    row.querySelector('.setg-row-sub').textContent,
    row.querySelector('.tog').classList.contains('on'),
    row.querySelector('.tog').getAttribute('aria-checked'),
  ]);
  return { rows, ...status(r) };
};

// the shared modal: a backdrop plays its exit before it hides
function modalRig(opts) {
  const h = build(() => Promise.resolve({}));
  h.load('core/modal.js');
  const a11y = [];
  h.ctx.ModalA11y = { register() {}, onOpened: () => a11y.push('opened'), onClosed: () => a11y.push('closed') };
  const backdrop = new El('div', h.doc); backdrop.className = 'modal-backdrop'; backdrop.id = 'm';
  const sheet = new El('div', h.doc); sheet.className = 'modal-sheet'; backdrop.appendChild(sheet);
  backdrop.hidden = true;
  const m = h.ctx.MM.modal.setupDetail(backdrop, { manageBodyOverflow: false, ...opts });
  const look = () => ({ hidden: !!backdrop.hidden, closing: backdrop.classList.contains('is-closing'), aria: backdrop.getAttribute('aria-hidden') });
  return { h, backdrop, sheet, m, look, a11y };
}

scenarios.modal = async function () {
  const r = modalRig();
  const initial = r.look();
  r.m.open();
  const opened = r.look();
  r.m.close();
  const closing = r.look();
  const a11yAtClose = r.a11y.slice();
  r.sheet.emit('animationend', { target: r.sheet });
  const sheetEnd = r.look();
  r.backdrop.emit('animationend');
  const closed = r.look();
  // the fallback timer ends the exit when no animationend arrives
  r.m.open(); r.m.close();
  r.h.ctx.timers.forEach((f) => f());
  const afterTimer = r.look();
  // a dialog that is not a .modal-backdrop hides at once
  const plain = new El('div', r.h.doc); plain.className = 'delete-modal-overlay';
  const pm = r.h.ctx.MM.modal.setupDetail(plain, { manageBodyOverflow: false, useModalA11y: false, activeClass: 'is-visible' });
  pm.open(); pm.close();
  return { initial, opened, closing, a11yAtClose, sheetEnd, closed, afterTimer, plainShown: plain.classList.contains('is-visible') };
};

// reopening while the exit plays cancels it: shown, no leftover timer, a late animationend is harmless
scenarios.modalReopen = async function () {
  const r = modalRig();
  r.m.open(); r.m.close();
  r.m.open();
  const reopened = { ...r.look(), timers: r.h.ctx.timers.length, listeners: (r.backdrop.listeners.animationend || []).length };
  r.backdrop.emit('animationend');
  const afterLateEnd = r.look();
  // a second close after the reopen still exits normally
  r.m.close();
  const closingAgain = r.look();
  r.backdrop.emit('animationend');
  return { reopened, afterLateEnd, closingAgain, closed: r.look() };
};

// under reduced motion the dialog hides at once, with no exit and no timer
scenarios.modalReduced = async function () {
  const r = modalRig();
  r.h.ctx.reduce = true;
  r.m.open();
  const opened = r.look().hidden;
  r.m.close();
  return { opened, ...r.look(), timers: r.h.ctx.timers.length };
};

// the Watch admin dialogs (.keep-dialog-overlay, shown by the `active` class) leave through the same exit
scenarios.modalKeepOverlay = async function () {
  const mk = () => {
    const h = build(() => Promise.resolve({}));
    h.load('core/modal.js');
    const root = new El('div', h.doc); root.className = 'keep-dialog-overlay wl-dialog';
    const m = h.ctx.MM.modal.setupDetail(root, { manageBodyOverflow: false, useModalA11y: false, activeClass: 'active' });
    const look = () => ({ active: root.classList.contains('active'), closing: root.classList.contains('is-closing') });
    return { h, root, m, look };
  };
  const r = mk();
  r.m.open();
  const opened = r.look();
  r.m.close();
  const closing = r.look();
  r.root.emit('animationend');
  const closed = r.look();
  r.m.open(); r.m.close();
  r.m.open();
  const reopened = { ...r.look(), timers: r.h.ctx.timers.length };
  const q = mk();
  q.h.ctx.reduce = true;
  q.m.open(); q.m.close();
  return { opened, closing, closed, reopened, reduced: { ...q.look(), timers: q.h.ctx.timers.length } };
};

// Escape closes a modal that has no ModalA11y every time it is opened, not just the first
scenarios.modalEscape = async function () {
  const h = build(() => Promise.resolve({}));
  h.load('core/modal.js');
  const backdrop = new El('div', h.doc); backdrop.className = 'modal-backdrop';
  backdrop.hidden = true;
  const m = h.ctx.MM.modal.setupDetail(backdrop, { manageBodyOverflow: false, useModalA11y: false });
  const esc = () => h.fire('keydown', { key: 'Escape' });
  const hidden = () => backdrop.getAttribute('aria-hidden');
  m.open(); m.close();
  h.ctx.reduce = true;
  m.open(); esc();
  const secondOpen = hidden();
  m.open(); esc();
  const thirdOpen = hidden();
  // a closed modal holds no listener, so reopening never stacks them
  m.open(); m.open();
  return { secondOpen, thirdOpen, listeners: (h.listeners.keydown || []).length };
};

// the re-authenticate dialog leaves through the shared fade, and Cancel still cancels
scenarios.reauth = async function () {
  const h = build(() => Promise.resolve({}));
  const body = new El('div', h.doc);
  h.doc.body = body;
  h.doc.querySelectorAll = (sel) => body.querySelectorAll(sel);
  const orig = h.doc.createElement;
  h.doc.createElement = (t) => { const e = orig(t); e.remove = () => { if (e.parentNode) e.parentNode.removeChild(e); }; e.focus = () => {}; return e; };
  h.doc.querySelectorAll = () => [];
  h.load('core/modal.js');
  h.load('core/dom.js');
  h.load('core/reauth.js');
  let cancelled = 0;
  h.ctx.MM.reauth.openModal({ onSubmit: () => Promise.resolve({ ok: true }), onCancel: () => { cancelled++; } });
  const backdrop = body.children[0];
  const cancel = backdrop.querySelectorAll('button')[0];
  const opened = { hidden: !!backdrop.hidden, closing: backdrop.classList.contains('is-closing') };
  cancel.emit('click');
  const closing = { attached: backdrop.parentNode === body, closing: backdrop.classList.contains('is-closing'), cancelled };
  backdrop.emit('animationend', { animationName: 'modal-backdrop-out' });
  h.ctx.timers.splice(0).forEach((f) => f());
  const gone = body.children.length;
  // Escape closes it too
  h.ctx.MM.reauth.openModal({ onSubmit: () => Promise.resolve({ ok: true }), onCancel: () => { cancelled++; } });
  h.fire('keydown', { key: 'Escape' });
  return { opened, closing, gone, cancelledAfterEsc: cancelled };
};

// run() with the reauth dialog: the guarded request answers 403 reauth_required the first time
function reauthRig() {
  const h = build(() => Promise.resolve({}));
  const body = new El('div', h.doc);
  h.doc.body = body;
  const orig = h.doc.createElement;
  h.doc.createElement = (t) => { const e = orig(t); e.remove = () => { if (e.parentNode) e.parentNode.removeChild(e); }; e.focus = () => {}; return e; };
  h.doc.querySelectorAll = (sel) => body.querySelectorAll(sel);
  const posts = [];
  h.ctx.MM.api = {
    APIError: class { constructor(code) { this.name = 'APIError'; this.code = code; } },
    post: () => new Promise((resolve) => posts.push(resolve)),
    messageOf: () => 'failed',
  };
  h.load('core/modal.js');
  h.load('core/dom.js');
  h.load('core/reauth.js');
  const calls = { n: 0 };
  const guarded = () => {
    calls.n++;
    if (calls.n > 1) return Promise.resolve('done');
    return Promise.reject({ name: 'APIError', status: 403, data: { reauth_required: true } });
  };
  const flush = () => new Promise((r) => setImmediate(r));
  const outcome = (p) => { const o = {}; p.then((v) => { o.value = v; }, (e) => { o.code = e.code; }); return o; };
  const submit = (backdrop) => {
    backdrop.querySelector('input').value = 'pw';
    backdrop.querySelector('input').emit('keydown', { key: 'Enter' });
  };
  return { h, body, posts, calls, guarded, flush, outcome, submit };
}

// closing the dialog while the password is being checked must not run the guarded request
scenarios.reauthCancelDuringCheck = async function () {
  const r = reauthRig();
  const out = r.outcome(r.h.ctx.MM.reauth.run(r.guarded));
  await r.flush();
  r.submit(r.body.children[0]);
  r.h.fire('keydown', { key: 'Escape' });
  r.posts[0]({});
  await r.flush();
  return { calls: r.calls.n, code: out.code ?? null, value: out.value ?? null };
};

// a second reauth closes the first dialog, which counts as its cancel
scenarios.reauthSecond = async function () {
  const r = reauthRig();
  const first = r.outcome(r.h.ctx.MM.reauth.run(r.guarded));
  await r.flush();
  const again = () => Promise.reject({ name: 'APIError', status: 403, data: { reauth_required: true } });
  const second = r.outcome(r.h.ctx.MM.reauth.run(again));
  await r.flush();
  return { first: first.code ?? null, second: second.code ?? null, dialogs: r.body.querySelectorAll('.reauth-backdrop').filter((b) => !b.classList.contains('is-closing')).length };
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
