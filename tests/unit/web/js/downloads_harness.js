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
  removeAttribute(k) { delete this.attrs[k]; }
  focus() {}
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
    hidden: false, documentElement: { clientWidth: 1280 },
    createElement: (t) => new El(t, doc),
    createElementNS: (ns, t) => new El(t, doc),
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
  return { ctx, doc, root, add, load, listeners, fire: (t, e) => (listeners[t] || []).forEach((f) => f(e)) };
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
    const count = box.querySelectorAll('.dl-recent-item').length;
    const look = () => box.querySelectorAll('.dl-recent-item').map((i) => [i.querySelector('.dl-recent-sub').textContent, !!i.querySelector('.dl-recent-badge')]);
    r.updateRecent(box, [{ title: 'A', poster_url: '/a.jpg', readiness: 'part' }, recent[1]]);
    const part = look();
    r.updateRecent(box, [{ title: 'A', poster_url: '/a.jpg', readiness: 'unknown' }, recent[1]]);
    const unknown = look();
    r.updateRecent(box, recent);
    return { kept, count, part, unknown, whole: look() };
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
  h.ctx.MM.dom = { q: (sel, c) => c.querySelector(sel), holdPage: () => () => {} };
  let dismissFn;
  const rig = { h, label, confirm, list, copy, posts: [], closes: 0, refreshes: 0, errors: 0, toasts: [] };
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
  rig.openItem = (dlId, kind, stuck, title, arrId, upcoming) => {
    const t = mk(); t.dataset = { dlId, kind, title: title || 'T' + dlId, arrId, stuckSeasons: JSON.stringify(stuck || []) };
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

// what is sent names the very title the button was of, as the last poll left it
scenarios.abandonNamesTheTitle = async function () {
  const h = build(() => Promise.resolve({}));
  ['core/dom.js', 'downloads/build_dom.js', 'downloads/render_hero.js', 'downloads/render_row.js', 'downloads/render_recent.js'].forEach(h.load);
  h.ctx.MM.downloads.poll = { start: (cb) => { h.ctx.update = cb; } };
  h.add('dl-empty-container'); h.add('dl-recent-container');
  h.root.setAttribute('data-can-abandon', 'true');
  h.load('downloads.js');
  const stuck = (id, arr_id) => item(id, { state: 'searching', abandon_visible: true, arr_id });
  const buttons = () => h.root.querySelectorAll('[data-abandon-trigger]')
    .map((b) => b.getAttribute('data-dl-id') + ' ' + b.getAttribute('data-arr-id')).sort();
  const poll = (n) => h.ctx.update({
    subtitle: '', recent: [], hero: stuck('h', n), queue: [stuck('q', n + 1)], upcoming: [item('u', { arr_id: n + 2 })],
  });
  poll(10);
  const built = buttons();
  poll(20);
  const r = abandonRig();
  r.openItem('m1', 'movie', [], 'Twin', '21');
  r.confirmClick();
  return { built, updated: buttons(), sent: r.posts[0].body };
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

// the page shows what the server lists: in an outage what it no longer lists goes as any row
// does, and an outage never reads as "All caught up"
scenarios.outage = async function () {
  const down = { hero: null, queue: [], outage: "Can't reach Radarr", unreachable: ['Radarr'] };
  const card = (p) => p.byId('dl-empty-container').querySelectorAll('h3').map((e) => e.textContent);
  const count = (p, id, cls) => { const b = p.byId(id); return b ? b.querySelectorAll(cls).length : 0; };
  const live = pageRig(true);
  live.upd({ hero: item('radarr:h1'), queue: [item('radarr:q1'), item('sonarr:q2')], upcoming: [item('radarr:u1')] });
  live.upd(down);
  const gone = {
    hero: count(live, 'dl-hero-container', '.dl-hero'), queue: count(live, 'dl-queue-container', '.dl-compact'),
    upcoming: count(live, 'dl-upcoming-container', '.dl-upcoming-row'), cards: card(live),
  };
  live.upd({ hero: item('radarr:h1'), queue: [item('radarr:q1')], upcoming: [item('radarr:u1')] });
  const back = { hero: count(live, 'dl-hero-container', '.dl-hero'), queue: count(live, 'dl-queue-container', '.dl-compact'), upcoming: count(live, 'dl-upcoming-container', '.dl-upcoming-row') };
  const idle = pageRig(true);
  idle.upd({ hero: null, queue: [] });
  const cards = [card(idle)];
  idle.upd(down);
  cards.push(card(idle));
  idle.upd({ hero: null, queue: [] });
  cards.push(card(idle));
  return { gone, back, cards };
};

// what an answer lists is shown whether or not it names the service that is down
scenarios.outageRows = async function () {
  const ids = (p, box, cls) => { const b = p.byId(box); return b ? b.querySelectorAll(cls).map((r) => r.getAttribute('data-dl-id')) : []; };
  const look = (p) => ({ hero: ids(p, 'dl-hero-container', '.dl-hero'), queue: ids(p, 'dl-queue-container', '.dl-compact'), upcoming: ids(p, 'dl-upcoming-container', '.dl-upcoming-row') });
  const full = {
    hero: item('sonarr:h1'), queue: [item('radarr:q1'), item('sonarr:q2'), item('sonarr:q3')],
    upcoming: [item('radarr:u1'), item('sonarr:u2'), item('sonarr:u3')],
  };
  const run = (extra) => {
    const p = pageRig(true);
    p.upd(full);
    const down = { outage: "Can't reach Radarr", hero: item('sonarr:h1'), queue: [item('sonarr:q2')], upcoming: [item('sonarr:u2')], ...extra };
    p.upd(down);
    const during = look(p);
    p.upd(full);
    return { during, back: look(p) };
  };
  return { admin: run({ unreachable: ['Radarr'], refused: [] }), viewer: run({ unreachable: [], refused: [] }) };
};

// rows keep their order of state through an outage
scenarios.outageOrder = async function () {
  const p = pageRig(true);
  const searching = { state: 'searching', state_label: 'Looking' };
  p.upd({
    hero: item('radarr:h1', { title: 'One' }),
    queue: [item('sonarr:q1'), item('sonarr:q2', searching), item('radarr:q3', { title: 'Three', ...searching }), item('sonarr:q4', searching)],
  });
  const down = {
    outage: "Can't reach Radarr", hero: item('sonarr:q1'),
    queue: [item('Other.2024', { title: 'Other' }), item('sonarr:q4'), item('sonarr:q2', searching)],
  };
  p.upd(down);
  return { rows: p.rows() };
};

// a service that answers in part: what the payload lists is drawn from it, once
scenarios.outagePart = async function () {
  const p = pageRig(true);
  const ids = (box, cls) => { const b = p.byId(box); return b ? b.querySelectorAll(cls).map((r) => r.getAttribute('data-dl-id')) : []; };
  p.upd({ hero: item('radarr:h1', { progress: 40 }), queue: [item('sonarr:q2')], upcoming: [item('radarr:u1')] });
  const down = { outage: "Can't reach Radarr", hero: item('sonarr:q2', { progress: 60 }), queue: [item('radarr:h1', { progress: 50 }), item('radarr:u1')] };
  p.upd(down);
  const row = p.byId('dl-queue-list').querySelector('.dl-compact');
  return { rows: p.rows(), pct: row.querySelector('[data-v="pct"]').textContent, upcoming: ids('dl-upcoming-container', '.dl-upcoming-row') };
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
  const opening = { hidden: !!list.hidden, ...p.folding(list) };
  list.emit('transitionend', { propertyName: 'height' });
  const open = { hidden: !!list.hidden, ...p.folding(list) };
  click();
  const closing = !!list.hidden;
  list.emit('transitionend', { propertyName: 'height' });
  return { opening, open, closing, closed: !!list.hidden };
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

// the subtitle line folds away and grows back in place
scenarios.subtitle = async function () {
  const p = pageRig();
  const ph = p.h.root.querySelector('.ph');
  p.upd({ subtitle: '2 downloading', hero: item('h1'), queue: [] });
  const line = ph.querySelector('p');
  line.emit('transitionend', { target: line, propertyName: 'height' });
  const shown = { text: line.textContent, ...p.folding(line) };
  p.upd({ subtitle: '', hero: null, queue: [] });
  const folding = { attached: line.parentNode === ph, ...p.folding(line) };
  p.upd({ subtitle: '1 downloading', hero: item('h1'), queue: [] });
  const back = { text: line.textContent, ...p.folding(line) };
  p.upd({ subtitle: '', hero: null, queue: [] });
  line.emit('transitionend', { propertyName: 'height' });
  return { shown, folding, back, removed: ph.querySelector('p') === null };
};

// a poll update swaps the state colour of a pill and keeps its size
scenarios.pillSize = async function () {
  const p = pageRig();
  p.upd({ hero: item('h1'), queue: [item('q1')] });
  const pill = () => p.byId('dl-queue-list').querySelector('.dl-state-pill');
  const before = pill().className;
  p.upd({ hero: item('h1'), queue: [item('q1', { state: 'almost_ready', state_label: 'Almost ready' })] });
  return { before, after: pill().className };
};

// the recent section folds out when it empties, and stays if it refills while closing
scenarios.recentLeaves = async function (reduce) {
  const p = pageRig(reduce);
  const recent = [{ title: 'A', poster_url: '/a.jpg' }];
  p.upd({ hero: item('h1'), queue: [], recent });
  const box = p.byId('dl-recent-container');
  const section = () => box.querySelector('.dl-recent-section');
  p.upd({ hero: item('h1'), queue: [], recent: [] });
  const during = section() ? p.folding(section()) : null;
  p.upd({ hero: item('h1'), queue: [], recent });
  const back = section() ? p.folding(section()) : null;
  p.upd({ hero: item('h1'), queue: [], recent: [] });
  const s = section();
  if (s) s.emit('transitionend', { propertyName: 'height' });
  return { during, back, gone: section() === null, boxEmpty: box.children.length === 0 };
};
scenarios.recentLeavesReduced = () => scenarios.recentLeaves(true);


// the empty card for a page with no Radarr and no Sonarr, as an admin and as a viewer
scenarios.notSetUp = async function () {
  const look = (admin, setUp) => {
    const p = pageRig(true);
    if (!admin) p.h.root.setAttribute('data-can-abandon', 'false');
    p.upd({ hero: null, queue: [], downloads_set_up: setUp });
    const card = p.byId('dl-empty-container');
    const link = card.querySelectorAll('a')[0];
    return { h3: card.querySelectorAll('h3')[0].textContent, p: card.querySelectorAll('p')[0].textContent, link: link ? link.href : null };
  };
  return {
    admin: look(true, { movies: false, shows: false }),
    viewer: look(false, { movies: false, shows: false }),
    films: look(true, { movies: true, shows: false }),
    shows: look(false, { movies: false, shows: true }),
    missing: look(true, undefined),
  };
};

// a card the poll swaps to another title fades out over it in one box with it, and its new
// content comes in only after
scenarios.heroSwap = async function () {
  const swap = (reduce) => {
    const p = pageRig(reduce);
    p.upd({ hero: item('h1'), queue: [] });
    p.upd({ hero: item('h2'), queue: [] });
    const box = p.byId('dl-hero-container');
    return {
      ghosts: box.querySelectorAll('.dl-hero-ghost__card').map((g) => g.style.height || ''),
      swapping: box.querySelectorAll('.dl-hero')[0].classList.contains('is-swapping'),
      later: (p.h.ctx.timers.forEach((t) => t()), box.querySelectorAll('.dl-hero')[0].classList.contains('is-swapping')),
    };
  };
  return { moving: swap(false), reduced: swap(true) };
};

// a row whose state changes, and the rows it passes, slide to their places
scenarios.rowSlide = async function () {
  const run = (reduce) => {
    const p = pageRig(reduce);
    const start = [item('q1'), item('q2', { state: 'searching' }), item('q3')];
    p.upd({ hero: item('h'), queue: start });
    p.byId('dl-queue-list').querySelectorAll('.dl-compact').forEach((r) => r.classList.add('dl-card-enter'));
    p.upd({ hero: item('h'), queue: [item('q1', { state: 'searching' }), item('q2', { state: 'searching' }), item('q3')] });
    const rows = p.byId('dl-queue-list').querySelectorAll('.dl-compact');
    return { order: rows.map((r) => r.getAttribute('data-dl-id')), sliding: rows.map((r) => r.classList.contains('is-sliding')), entering: rows.map((r) => r.classList.contains('dl-card-enter')) };
  };
  return { moving: run(false), reduced: run(true) };
};

// a row that grows without moving: the rows below it slide down instead of jumping
scenarios.rowGrows = async function () {
  const run = (reduce) => {
    const p = pageRig(reduce);
    const searching = { state: 'searching', state_label: 'Looking' };
    p.upd({ hero: item('h'), queue: [item('q1', searching), item('q2', searching)] });
    p.upd({ hero: item('h'), queue: [item('q1', { ...searching, search_hint: 'Searched once' }), item('q2', searching)] });
    const rows = p.byId('dl-queue-list').querySelectorAll('.dl-compact');
    return { sliding: rows.map((r) => r.classList.contains('is-sliding')), from: rows.map((r) => r.slideStart || '') };
  };
  return { moving: run(false), reduced: run(true) };
};

// a row that moves again while it still slides starts from where it is seen
scenarios.slideAgain = async function () {
  const p = pageRig();
  p.upd({ hero: item('h'), queue: [item('q1'), item('q2')] });
  const q2 = p.byId('dl-queue-list').querySelectorAll('.dl-compact')[1];
  q2.classList.add('is-sliding');
  q2.style.transform = 'translateY(-60px)';   // seen 40px down, on its way up
  p.upd({ hero: item('h'), queue: [item('q1', { state: 'searching' }), item('q2')] });
  const rows = p.byId('dl-queue-list').querySelectorAll('.dl-compact');
  return rows.map((r) => [r.getAttribute('data-dl-id'), r.slideStart || '']);
};

// rows that come back take their places in the server's order, in Coming soon and the queue
scenarios.returnOrder = async function () {
  const p = pageRig(true);
  const ids = (box, cls) => p.byId(box).querySelectorAll(cls).map((r) => r.getAttribute('data-dl-id'));
  const full = {
    hero: item('h'), queue: [item('q1'), item('q2'), item('q3')],
    upcoming: [item('u1'), item('u2'), item('u3')],
  };
  const less = { ...full, queue: [item('q1'), item('q3')], upcoming: [item('u1'), item('u3')] };
  p.upd(full);
  p.upd(less);
  p.upd(full);
  return { upcoming: ids('dl-upcoming-container', '.dl-upcoming-row'), queue: ids('dl-queue-container', '.dl-compact') };
};

// a viewer's outage with nothing in progress is said by the card alone
scenarios.viewerOutageLine = async function () {
  const line = (admin, upcoming) => {
    const p = pageRig(true);
    if (!admin) p.h.root.setAttribute('data-can-abandon', 'false');
    const outage = admin ? "Can't reach Radarr" : "Downloads aren't available";
    const coming = upcoming.length ? ' · ' + upcoming.length + ' coming soon' : '';
    p.upd({ hero: null, queue: [], upcoming, outage, subtitle: outage + coming });
    return p.h.root.querySelectorAll('.ph')[0].querySelectorAll('p').map((e) => e.textContent);
  };
  return { viewer: line(false, []), viewerSoon: line(false, [item('u1')]), admin: line(true, []) };
};

// a card with no poster, or one that does not load, holds the mark of one
scenarios.posterMark = async function () {
  const p = pageRig(true);
  p.upd({ hero: item('h', { poster_url: '' }), queue: [item('q1', { poster_url: '' }), item('q2', { poster_url: '/p.jpg' })] });
  const kinds = (sel) => p.h.root.querySelectorAll(sel).map((box) => box.children.map((c) => c.tagName + '.' + c.className));
  p.upd({ hero: item('h', { poster_url: '/h.jpg' }), queue: [item('q1', { poster_url: '' }), item('q2', { poster_url: '/p.jpg' })] });
  return { hero: kinds('.dl-hero-poster'), rows: kinds('.dl-compact-poster') };
};

// the queue's heading is true of the rows under it
scenarios.queueHeading = async function () {
  const p = pageRig(true);
  const heading = () => p.byId('dl-queue-container').querySelectorAll('h2')[0].textContent;
  const out = {};
  p.upd({ hero: item('h'), queue: [item('q1', { state: 'queued' }), item('q2', { state: 'queued' })] });
  out.queued = heading();
  p.upd({ hero: item('h'), queue: [item('q1', { state: 'queued' }), item('q2')] });
  out.mixed = heading();
  return out;
};

// a refused key sends an admin to Settings; a viewer is told only that it is not available
scenarios.refusedKey = async function () {
  const look = (admin, extra) => {
    const p = pageRig(true);
    if (!admin) p.h.root.setAttribute('data-can-abandon', 'false');
    p.upd({ hero: null, queue: [], ...extra });
    const card = p.byId('dl-empty-container');
    const link = card.querySelectorAll('a')[0];
    return { p: card.querySelectorAll('p')[0].textContent, link: link ? link.href : null };
  };
  const refused = { outage: "Can't sign in to Sonarr", unreachable: ['Sonarr'], refused: ['Sonarr'] };
  return {
    admin: look(true, refused),
    two: look(true, { ...refused, refused: ['Radarr', 'Sonarr'] }),
    down: look(true, { outage: "Can't reach Sonarr", unreachable: ['Sonarr'], refused: [] }),
    viewer: look(false, { outage: "Downloads aren't available", unreachable: [], refused: [] }),
  };
};

// a viewer is told that rows may be missing, not which service does not answer
scenarios.viewerLine = async function () {
  const line = (admin) => {
    const p = pageRig(true);
    if (!admin) p.h.root.setAttribute('data-can-abandon', 'false');
    p.upd({
      hero: item('h'), queue: [item('q')], upcoming: [item('u')], outage: admin ? "Can't reach Radarr" : "Downloads aren't available",
      subtitle: 'server words',
    });
    return p.h.root.querySelectorAll('.ph')[0].querySelectorAll('p')[0].textContent;
  };
  return { viewer: line(false), admin: line(true) };
};

// only an admin's rows hold a link into Radarr or Sonarr
scenarios.arrLink = async function () {
  const count = (admin) => {
    const p = pageRig(true);
    if (!admin) p.h.root.setAttribute('data-can-abandon', 'false');
    const searching = { state: 'searching', search_hint: 'next attempt', arr_link: 'http://r/x', arr_source: 'Radarr' };
    p.upd({ hero: item('h', searching), queue: [item('q', searching)] });
    return p.h.root.querySelectorAll('[data-v="arr-link"]').length;
  };
  return { admin: count(true), viewer: count(false) };
};

// a row that arrives mid-list opens from nothing, so the rows below it ease down as it grows
// and never start from a place they were not seen in
scenarios.newRowMidList = async function () {
  const p = pageRig();
  p.upd({ hero: item('h'), queue: [item('q1'), item('q3')] });
  p.upd({ hero: item('h'), queue: [item('q1'), item('q2'), item('q3')] });
  return p.byId('dl-queue-list').querySelectorAll('.dl-compact').map((r) => [
    r.getAttribute('data-dl-id'), r.style.height || '', r.slideStart || '', r.getBoundingClientRect().top,
  ]);
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
