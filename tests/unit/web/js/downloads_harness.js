// Runs the real /downloads page scripts against a tiny fake DOM and prints
// the outcome of one scenario as JSON. Usage: node downloads_harness.js <scenario>
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
  get offsetHeight() { return this.style.display === 'none' ? 0 : 50; }
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
    hidden: false,
    createElement: (t) => new El(t, doc),
    createTextNode: (t) => { const e = new El('#text', doc); e._text = t; return e; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    getElementById: (id) => root.all().find((e) => e.id === id) || null,
  };
  const root = new El('div', doc);
  root.setAttribute('id', 'dl-root');
  const add = (id, parent) => { const e = new El('div', doc); e.setAttribute('id', id); (parent || root).appendChild(e); return e; };
  const ph = new El('div', doc); ph.className = 'ph'; root.appendChild(ph);
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise,
    setTimeout: (fn) => { ctx.timers.push(fn); return ctx.timers.length; },
    clearTimeout: () => {}, CustomEvent: class {},
    timers: [], MM: {}, MM_API: pollGet, reduce: false,
    matchMedia: () => ({ matches: ctx.reduce }),
    getComputedStyle: (e) => ({ opacity: e.style.opacity || '1', paddingTop: '0px', paddingBottom: '0px', marginTop: '0px', marginBottom: '0px' }),
  };
  ctx.window = ctx; ctx.MM.api = { get: pollGet };
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  doc.getElementById = (id) => (id === 'dl-root' ? root : root.all().find((e) => e.id === id) || null);
  return { ctx, doc, root, add, load, fire: (t, e) => (listeners[t] || []).forEach((f) => f(e)) };
}

const item = (id, extra) => Object.assign({
  id, title: 'T' + id, media_type: 'movie', state: 'downloading', state_label: 'Downloading',
  progress: 10, abandon_visible: false, stuck_seasons: [], episodes: [],
}, extra || {});

const scenarios = {
  // hero goes away and comes back; queue refills after empty and gets a new item
  async page() {
    const h = build(() => Promise.resolve({}));
    ['core/dom.js', 'downloads/build_dom.js', 'downloads/render_hero.js', 'downloads/render_row.js', 'downloads/render_recent.js'].forEach(h.load);
    h.ctx.MM.downloads.poll = { start: (cb) => { h.ctx.update = cb; } };
    h.add('dl-empty-container'); h.add('dl-recent-container');
    h.root.setAttribute('data-can-abandon', 'true');
    h.load('downloads.js');
    const upd = h.ctx.update;
    const heroCards = () => h.doc.getElementById('dl-hero-container').querySelectorAll('.dl-hero').length;
    const rows = () => (h.doc.getElementById('dl-queue-list') ? h.doc.getElementById('dl-queue-list').querySelectorAll('.dl-compact').map((r) => r.getAttribute('data-dl-id')) : []);
    const base = { subtitle: '', upcoming: [], recent: [] };
    upd({ ...base, hero: item('h1'), queue: [item('q1')] });
    upd({ ...base, hero: null, queue: [] });
    h.ctx.timers.splice(0).forEach((f) => f());   // the page has finished closing
    upd({ ...base, hero: item('h2'), queue: [item('q2')] });
    const afterRefill = { hero: heroCards(), rows: rows() };
    upd({ ...base, hero: item('h2'), queue: [item('q2'), item('q3')] });
    const withNew = rows();
    // abandon button follows the payload on a row and the hero
    upd({ ...base, hero: item('h2', { abandon_visible: true, stuck_seasons: [{ number: 2, missing_episodes: 3 }] }), queue: [item('q2', { abandon_visible: true }), item('q3')] });
    const shown = {
      hero: h.doc.getElementById('dl-hero-container').querySelectorAll('[data-abandon-trigger]').length,
      row: h.doc.getElementById('dl-queue-list').querySelectorAll('[data-abandon-trigger]').length,
      stuck: h.doc.getElementById('dl-hero-container').querySelector('[data-abandon-trigger]').getAttribute('data-stuck-seasons'),
    };
    upd({ ...base, hero: item('h2'), queue: [item('q2'), item('q3')] });
    const hidden = {
      hero: h.doc.getElementById('dl-hero-container').querySelectorAll('[data-abandon-trigger]').length,
      row: h.doc.getElementById('dl-queue-list').querySelectorAll('[data-abandon-trigger]').length,
    };
    // departed row leaves even when no animationend fires
    upd({ ...base, hero: item('h2'), queue: [item('q3')] });
    h.ctx.timers.forEach((f) => f());
    return { afterRefill, withNew, shown, hidden, afterExit: rows() };
  },

  // a poll-now during an in-flight poll is not lost
  async poll() {
    const calls = [];
    let release;
    const h = build(() => { calls.push(1); return calls.length === 1 ? new Promise((r) => { release = r; }) : Promise.resolve({}); });
    h.load('downloads/poll.js');
    h.ctx.MM.downloads.poll.start(() => {});
    h.ctx.timers.shift()();            // first scheduled poll starts
    h.fire('mediaman:poll:now');       // asked for while it is in flight
    release({});
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    return { calls: calls.length };
  },

  // the recent grid keeps its nodes while nothing changed
  async recent() {
    const h = build(() => Promise.resolve({}));
    ['core/dom.js', 'downloads/build_dom.js', 'downloads/render_recent.js'].forEach(h.load);
    const box = new El('div', h.doc);
    const r = h.ctx.MM.downloads.renderRecent;
    const recent = [{ title: 'A', poster_url: '/a.jpg' }, { title: 'B', poster_url: '' }];
    r.updateRecent(box, recent);
    const first = box.querySelector('.dl-recent-item');
    r.updateRecent(box, recent.map((x) => ({ ...x })));
    const kept = box.querySelector('.dl-recent-item') === first;
    r.updateRecent(box, [{ title: 'C', poster_url: '/c.jpg' }].concat(recent));
    return { kept, count: box.querySelectorAll('.dl-recent-item').length };
  },
};

// The abandon dialog on fake parts: `post` hands back each request so a test
// settles it when it likes, `closes` counts the times the dialog shut itself.
function abandonRig() {
  const h = build(() => Promise.resolve({}));
  const mk = (cls) => { const e = new El('div', h.doc); e.className = cls || ''; return e; };
  const modal = mk(); modal.setAttribute('id', 'abandon-modal');
  const title = mk(); const copy = mk(); const list = mk();
  const confirm = mk(); confirm.setAttribute('data-abandon-confirm', ''); confirm.setAttribute('aria-disabled', 'false');
  const label = mk(); label.setAttribute('data-confirm-label', ''); confirm.appendChild(label);
  modal.appendChild(confirm);
  const byId = { 'abandon-modal': modal, 'abandon-modal-title': title, 'abandon-modal-copy': copy, 'abandon-season-list': list };
  h.doc.getElementById = (id) => byId[id] || null;
  h.ctx.MM.dom = { q: (sel, c) => c.querySelector(sel) };
  let dismissFn;
  const rig = { h, label, confirm, list, posts: [], closes: 0, refreshes: 0, errors: 0 };
  h.ctx.MM.modal = {
    setupDetail: (_m, opts) => (dismissFn = () => opts.onClose(), {
      open() {},
      close() { rig.closes += 1; opts.onClose(); },
      // what the modal does on Escape or the backdrop
      dismiss() { opts.onClose(); },
    }),
  };
  h.ctx.MM.api.post = (url) => new Promise((resolve, reject) => { rig.posts.push({ url, resolve, reject }); });
  h.ctx.MM.api.messageOf = () => 'nope';
  h.ctx.UIFeedback = { error() { rig.errors += 1; } };
  h.ctx.CustomEvent = class { constructor(t) { this.type = t; } };
  h.doc.dispatchEvent = (e) => { if (e.type === 'mediaman:downloads:refresh') rig.refreshes += 1; };
  h.load('dl-abandon.js');
  rig.openItem = (dlId, kind, stuck, title) => {
    const t = mk(); t.dataset = { dlId, kind, title: title || 'T' + dlId, stuckSeasons: JSON.stringify(stuck || []) };
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

scenarios.abandonSuccess = async function () {
  const r = abandonRig();
  r.openItem('m1', 'movie');
  r.confirmClick();
  r.posts[0].resolve({});
  await r.settle();
  return { url: r.posts[0].url, closes: r.closes, refreshes: r.refreshes, errors: r.errors };
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

scenarios.hero = async function () {
  const h = build(() => Promise.resolve({}));
  ['core/dom.js', 'downloads/build_dom.js'].forEach(h.load);
  const stats = (it) => h.ctx.MM.downloads.buildDom.buildHero(it).querySelector('.dl-hero-details').children.map((c) => c.textContent);
  const full = item('h', { progress: 30, size_done: '1 GB', size_total: '3 GB', eta: '5 min' });
  const noEta = item('h', { progress: 30, size_done: '1 GB', size_total: '3 GB' });
  const noSize = item('h', { progress: 30, eta: '5 min' });
  return { full: stats(full), noEta: stats(noEta), noSize: stats(noSize) };
};

// The /downloads page on fake parts, with `upd` feeding it one poll.
function pageRig(reduce) {
  const h = build(() => Promise.resolve({}));
  h.ctx.reduce = !!reduce;
  ['core/dom.js', 'downloads/build_dom.js', 'downloads/render_hero.js', 'downloads/render_row.js', 'downloads/render_recent.js'].forEach(h.load);
  h.ctx.MM.downloads.poll = { start: (cb) => { h.ctx.update = cb; } };
  h.add('dl-empty-container'); h.add('dl-recent-container');
  h.load('downloads.js');
  const base = { subtitle: '', upcoming: [], recent: [] };
  return {
    h,
    upd: (d) => h.ctx.update({ ...base, ...d }),
    byId: (id) => h.doc.getElementById(id),
    rows: () => h.doc.getElementById('dl-queue-list').querySelectorAll('.dl-compact').map((r) => r.getAttribute('data-dl-id')),
    folding: (e) => ({ cls: e.className, height: e.style.height || '' }),
  };
}

// a departed row stays, closing, until its height has closed
scenarios.rowLeave = async function () {
  const p = pageRig();
  p.upd({ hero: item('h1'), queue: [item('q1'), item('q2')] });
  p.upd({ hero: item('h1'), queue: [item('q2')] });
  const leaving = p.byId('dl-queue-list').querySelectorAll('.dl-compact')[0];
  const during = { rows: p.rows(), q1: p.folding(leaving) };
  leaving.emit('transitionend', { propertyName: 'opacity' });
  const afterFade = p.rows();
  leaving.emit('transitionend', { propertyName: 'height' });
  return { during, afterFade, afterHeight: p.rows() };
};

// a row that comes back while it closes opens again and stays
scenarios.rowReturns = async function () {
  const p = pageRig();
  p.upd({ hero: item('h1'), queue: [item('q1'), item('q2')] });
  p.upd({ hero: item('h1'), queue: [item('q2')] });
  p.upd({ hero: item('h1'), queue: [item('q1'), item('q2')] });
  const row = p.byId('dl-queue-list').querySelectorAll('.dl-compact')[0];
  row.emit('transitionend', { propertyName: 'height' });
  p.h.ctx.timers.splice(0).forEach((f) => f());
  return { rows: p.rows(), q1: p.folding(row) };
};

// with reduced motion a departed row goes at once
scenarios.rowLeaveReduced = async function () {
  const p = pageRig(true);
  p.upd({ hero: item('h1'), queue: [item('q1'), item('q2')] });
  p.upd({ hero: item('h1'), queue: [item('q2')] });
  return { rows: p.rows() };
};

// the hero and queue close in place while the empty state opens
scenarios.emptyState = async function () {
  const p = pageRig();
  p.upd({ hero: item('h1'), queue: [item('q1')] });
  p.upd({ hero: null, queue: [] });
  const hero = p.byId('dl-hero-container'), queue = p.byId('dl-queue-container'), empty = p.byId('dl-empty-container');
  const during = {
    hero: hero.children.length, queue: queue.children.length, empty: empty.children.length,
    heroFold: p.folding(hero), emptyFold: p.folding(empty),
  };
  // the next poll while they move changes nothing
  p.upd({ hero: null, queue: [] });
  [hero, queue, empty].forEach((e) => e.emit('transitionend', { propertyName: 'height' }));
  return {
    during,
    after: { hero: hero.children.length, queue: queue.children.length, empty: empty.children.length, emptyFold: p.folding(empty) },
  };
};

// an episode list opens, and on close stays shown until it has folded away
scenarios.episodes = async function () {
  const p = pageRig();
  const eps = [{ label: 'S01E01', title: 'One', state: 'downloading', progress: 10 }];
  p.upd({ hero: item('h1', { media_type: 'series', episodes: eps }), queue: [] });
  const toggle = p.byId('dl-hero-container').querySelector('[data-v="ep-toggle"]');
  const list = p.byId('dl-hero-container').querySelector('[data-v="ep-list"]');
  const click = () => p.h.fire('click', { target: toggle });
  click();
  const opening = { display: list.style.display || '', ...p.folding(list) };
  list.emit('transitionend', { propertyName: 'height' });
  const open = { display: list.style.display || '', ...p.folding(list) };
  click();
  const closing = list.style.display || '';
  list.emit('transitionend', { propertyName: 'height' });
  return { opening, open, closing, closed: list.style.display };
};

// the shared modal: a backdrop fades out before it hides, and reopening cancels the hide
scenarios.modal = async function () {
  const h = build(() => Promise.resolve({}));
  h.load('core/modal.js');
  const mk = (cls) => { const e = new El('div', h.doc); e.className = cls; return e; };
  const backdrop = mk('modal-backdrop'); const sheet = mk('modal-sheet'); backdrop.appendChild(sheet);
  backdrop.style.display = 'none';
  const opts = { manageBodyOverflow: false, useModalA11y: false };
  const m = h.ctx.MM.modal.setupDetail(backdrop, opts);
  const look = () => ({ display: backdrop.style.display, leaving: backdrop.classList.contains('is-leaving'), hidden: backdrop.getAttribute('aria-hidden') });
  m.open(); m.close();
  const closing = look();
  sheet.parentNode.emit('animationend', { target: sheet, animationName: 'modal-sheet-out' });
  backdrop.emit('animationend', { animationName: 'modal-backdrop-in' });
  const otherEnds = look();
  backdrop.emit('animationend', { animationName: 'modal-backdrop-out' });
  const closed = look();
  m.open(); m.close(); m.open();
  h.ctx.timers.forEach((f) => f());
  const reopened = look();
  // a dialog that is not a .modal-backdrop hides at once
  const plain = mk('delete-modal-overlay');
  const pm = h.ctx.MM.modal.setupDetail(plain, { ...opts, activeClass: 'is-visible' });
  pm.open(); pm.close();
  return { closing, otherEnds, closed, reopened, plainShown: plain.classList.contains('is-visible') };
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
