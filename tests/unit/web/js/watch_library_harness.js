// Runs watch/core.js, watch/admin.js and watch/library.js against the fake DOM and prints the outcome of one scenario
// as JSON. Usage: node watch_library_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, build, settle } = require('./fake_dom');

/* Bubble a click from *target* up to the document; stopPropagation ends it there. */
function click(doc, target, keys) {
  const e = {
    target, button: 0, ...keys, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  for (let a = target; a && !e.stopped; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e));
  if (!e.stopped) (doc.listeners.click || []).forEach((fn) => fn(e));
  return e;
}

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

/* What the scripts need beyond the shared fake DOM. */
Object.defineProperty(Node.prototype, 'id', {
  get() { return this.attrs.id || ''; },
  set(v) { this.attrs.id = String(v); },
});
Object.defineProperty(Node.prototype, 'dataset', {
  get() {
    const node = this;
    const attr = (k) => 'data-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
    return new Proxy({}, {
      get: (_t, k) => (typeof k === 'string' && node.hasAttribute(attr(k)) ? node.getAttribute(attr(k)) : undefined),
      set: (_t, k, v) => { node.setAttribute(attr(k), v); return true; },
      deleteProperty: (_t, k) => { node.removeAttribute(attr(k)); return true; },
    });
  },
});
Node.prototype.replaceChildren = function (...kids) {
  this.textContent = '';
  kids.forEach((k) => this.appendChild(k));
};
Node.prototype.removeEventListener = function () {};
Object.defineProperty(Node.prototype, 'labels', {
  get() { return this.ownerDocument.querySelectorAll('label').filter((l) => l.htmlFor === this.id); },
});
Object.defineProperty(Node.prototype, 'firstElementChild', { get() { return this.children[0] || null; } });

function deferred() {
  let resolve; let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/* ── What GET /api/watch/library/library/row says of a title ── */

const keyOf = (r) => (r.showRk ? 'show:' + r.showRk : 'id:' + r.id);

function item(r) {
  const several = !!r.showRk;
  return {
    key: keyOf(r), title: r.title, subtitle: r.sub || 'Movie', href: r.unheld ? null : '/watch/movie/' + r.id,
    admin: { id: r.id, is_tv: !!r.showRk, show_rk: r.showRk || '', has_rk: !!r.showRk, show_title: r.title, title: r.title,
      kept: !!r.kept, deletion: !!r.deletion, keep: r.keep || null, seasons: r.seasons || null, has_keeps: !!r.kept,
      delete: r.kept ? null : several ? { show_rk: r.showRk } : { show_rk: '', id: r.id, kind: 'movie', size: '22.1 GB' },
      unkeep: !r.kept ? null : several ? 'dialog' : 'item', facts: '22.1 GB' },
  };
}

/* The keep a save answers it made. */
const MADE = { action: 'snoozed', execute_at: '2026-12-24T12:00:00+00:00', kept_at: '2026-11-24T12:00:00+00:00' };
/* The refusal of a change to a keep that is not the one read. */
const keepChanged = () => Promise.reject(Object.assign(new Error('changed'), { error: 'keep_changed', status: 409 }));
const serverError = () => Promise.reject(Object.assign(new Error('no'), { status: 500 }));

const ROWS = [
  { id: 'm1', title: 'Copperline', deletion: true },
  { id: 'm2', title: 'Saltmarsh' },
  { id: 's1', title: 'Night Shift', showRk: '77' },
  { id: 'm3', title: 'Glass Harbour', kept: true, keep: MADE },
];

/* A card, as watch/search.js builds it. */
function cardOf(doc, it) {
  return build(doc, ['article', { class: 'watch-card', 'data-keep-id': '' },
    ['a', { class: 'watch-card__link', href: it.href || '/watch' },
      ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, it.title]], ['span', { class: 'watch-card__sub' }, it.subtitle]],
    ...(it.href ? [['button', { class: 'watch-more', 'data-watch-more': '' }]] : [])]);
}

/* The page with All titles open on *rows*, the scripts loaded. opts.closed: it is not open. */
function load(opts = {}) {
  const doc = makeDocument();
  doc.dispatchEvent = (ev) => { (doc.listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; };
  Object.assign(doc.documentElement.style, { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } });
  doc.body.appendChild(build(doc, ['div', { class: 'watch-page' }, ['input', { id: 'watch-search' }], ['section', { class: 'watch-all' }]]));
  doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' }, JSON.stringify({ is_admin: !opts.viewer })]));
  const all = doc.querySelector('.watch-all');
  const calls = [];
  const reads = [];
  const toasts = [];
  const errors = [];
  const confirms = [];
  const opened = [];
  const winListeners = {};
  const winCaptures = new Set();
  const docCaptures = new Set();
  const listen = doc.addEventListener;
  doc.addEventListener = (t, fn, o) => { listen(t, fn); if (o === true || (o && o.capture)) docCaptures.add(fn); };
  const replies = {}; // method and url -> function returning a promise
  let served = null;
  let confirmAnswer = true;
  const p = { doc, calls, reads, toasts, errors, confirms, opened, replies, bar: {} };

  const library = (url) => {
    reads.push(url);
    const query = new URLSearchParams(url.slice(url.indexOf('?')));
    const now = served;
    if (!now) return Promise.reject(Object.assign(new Error('no'), { status: 500 }));
    const only = query.get('only');
    const answer = () => ({ items: now.rows.map(item).filter((i) => !only || i.key === only), next: null, total: now.rows.length, type: query.get('type') || '', counts: { '': now.rows.length }, fact: now.fact || null, empty: now.empty });
    return now.wait ? now.wait.promise.then(answer) : Promise.resolve().then(answer);
  };
  const api = (method) => (url, body) => {
    if (/^\/api\/watch\/library\//.test(url)) return library(url);
    calls.push([method, url, body === undefined ? null : body]);
    const reply = replies[method + ' ' + url];
    return reply ? reply() : Promise.resolve(method === 'postForm' ? { ok: true, keep: MADE } : { ok: true });
  };

  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, WeakMap, Error, Proxy,
    URLSearchParams, parseInt, parseFloat, encodeURIComponent, Infinity,
    setTimeout: opts.timers ? (fn) => opts.timers.push(fn) : (fn) => { fn(); return 1; },
    clearTimeout: opts.timers ? (id) => { opts.timers[id - 1] = () => {}; } : () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: (q) => ({ matches: !/coarse/.test(q) }),
    innerHeight: 900, innerWidth: 1280, scrollBy() {}, scrollTo() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    UIFeedback: {
      toast: (message, o) => {
        const entry = { message, dialog: (o && o.dialog && o.dialog.id) || null, undo: !!(o && o.action), action: o && o.action, dismissed: false };
        toasts.push(entry);
        return entry;
      },
      dismiss: (entry) => { entry.dismissed = true; },
      error: (message) => errors.push(message),
      confirm: (o) => { confirms.push(o); return Promise.resolve(confirmAnswer); },
    },
    MM: {
      api: { post: api('post'), postForm: api('postForm'), get: api('get'), messageOf: (e, f) => (e && e.message) || f },
      dom: { focusStillIn: (n) => { const a = doc.activeElement; return !a || a === doc.body || n.contains(a); } },
      modal: {
        setupDetail: (node, o) => ({
          open() { node.classList.add(o.activeClass); },
          close() { node.classList.remove(o.activeClass); if (o.onClose) o.onClose(); },
        }),
      },
    },
    addEventListener: (t, fn, o) => { (winListeners[t] = winListeners[t] || []).push(fn); if (o === true || (o && o.capture)) winCaptures.add(fn); },
    removeEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const run = (file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js', 'watch/admin.js'].forEach(run);
  const cards = () => all.querySelectorAll('.watch-card');
  Object.assign(ctx.MMWatch, {
    search: { card: (it) => cardOf(doc, it) },
    grid: {
      shows: () => (opts.closed ? null : { sort: 'added', filter: opts.filter || '' }),
      setBar: (bar) => { p.bar = bar; },
      // As watch/grid.js: the card goes, and the one after or before it is where the focus goes.
      take: (card, control) => {
        const rest = cards();
        const i = rest.indexOf(card);
        const next = rest[i + 1] || rest[i - 1];
        card.remove();
        return { focus: next ? next.querySelector(control) : null };
      },
    },
    expand: {
      source: (_id, make) => { p.make = make; },
      library: (sort, type, how) => opened.push([sort, type, how.still, how.ready ? how.ready.items.length : null]),
    },
  });
  run('watch/library.js');
  p.source = p.make({ id: 'library', sort: 'added', type: '', ready: null, row: null, drawn: null, cards: () => [] });
  Object.assign(p, {
    ctx,
    /* What the library answers with: *rows*, and the line of the bar. */
    serve(rows, more, wait) { served = { rows, wait, ...more }; },
    failReads() { served = null; },
    answerConfirm(v) { confirmAnswer = v; },
    /* A key pressed where the focus is: down from the window, then up to it. */
    press(key, extra) {
      const target = doc.activeElement;
      let stopped = false;
      const e = { key, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; }, ...extra };
      const on = (fns, want) => { if (!stopped) (fns || []).filter(want).forEach((fn) => fn(e)); };
      on(winListeners.keydown, (fn) => winCaptures.has(fn));
      on(doc.listeners.keydown, (fn) => docCaptures.has(fn));
      for (let n = target; n; n = n.parentNode) on(n.listeners.keydown, () => true);
      on(doc.listeners.keydown, (fn) => !docCaptures.has(fn));
      on(winListeners.keydown, (fn) => !winCaptures.has(fn));
      return e;
    },
    rowKeys() { return cards().map((c) => c.getAttribute('data-key')); },
    row(key) { return cards().find((c) => c.getAttribute('data-key') === key) || null; },
  });
  if (!opts.closed) p.make({ sort: 'added', type: '', ready: { items: (opts.rows || ROWS).map(item), next: null }, cards: () => [] }).cards.forEach((c) => all.appendChild(c));
  return p;
}

const more = (p, key) => p.row(key).querySelector('[data-watch-more]');
const adminOf = (p, key) => JSON.parse(p.row(key).getAttribute('data-admin'));
const keptNow = (p, key) => adminOf(p, key).kept;

/* What a card says while something is done of it. */
function look(p, key) {
  const card = p.row(key);
  return { busy: card.classList.contains('is-working'), label: card.querySelector('.watch-card__sub').textContent,
    held: more(p, key).getAttribute('aria-disabled') };
}

/* Choose of a card's menu, which closes and gives the focus back to its button. */
function press(p, key, what, duration) {
  more(p, key).focus();
  p.ctx.MMWatch.library.act(p.row(key), 'admin-' + what, build(p.doc, ['button', duration ? { 'data-keep-dur': duration } : {}]));
}

/* ── The Keep dialog of a show ── */

const FOREVER = { action: 'protected_forever', execute_at: null, kept_at: '2026-11-20T12:00:00+00:00', days: null, label: 'Kept forever' };
/* Kept forever, at another time. */
const OWN_FOREVER = Object.assign({}, FOREVER, { kept_at: '2026-11-01T12:00:00+00:00' });
const readOf = (k) => k && { action: k.action, execute_at: k.execute_at, kept_at: k.kept_at || null };
const until = (days) => ({ action: 'snoozed', execute_at: new Date(Date.UTC(2026, 10, 24 + days)).toISOString(), days, label: 'Kept for ' + days + ' more days' });
const leaves = (days) => ({ execute_at: until(days).execute_at, days });
/* Seasons numbered from 1: each a keep, what is left of it, or nothing. */
const seasonsOf = (...each) => each.map((s, i) => Object.assign(
  { id: 's' + (i + 1), season_number: i + 1, kept: !!(s && s.keep), keep: null, leaving: null }, s));
/* Season 1 kept forever, Season 2 leaves in 3 days and Season 3 in 5. */
const LEAVING = seasonsOf({ keep: FOREVER }, { leaving: leaves(3) }, { leaving: leaves(5) });

/* The dialog, opened as the title page does (*seasonId*: the season its line names, '' when
   none; undefined opens it from its card). */
async function keepDialog({ seasons, showKept, seasonId, duration }) {
  const p = load();
  let now = seasons;
  p.replies['get /api/show/77/seasons'] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: showKept || null, seasons: now });
  p.serve(ROWS);
  if (seasonId === undefined) press(p, 'show:77', 'keep', duration || '30 days');
  else {
    p.ctx.MMWatch.admin.keep({ id: 's1', is_tv: true, show_rk: '77', has_rk: true, show_title: 'Night Shift',
      kept: false, deletion: true, season_id: seasonId }, duration || '30 days');
  }
  await settle();
  const list = p.doc.getElementById('keep-season-list');
  const phases = [];
  p.doc.addEventListener('mm:watch-admin', (e) => phases.push(e.detail.phase));
  const box = (id) => list.querySelectorAll('input').find((n) => n.id === 'keep-' + (id === 'all' ? 'all-seasons' : 'season-' + id));
  const set = (id) => list.querySelectorAll('[data-keep-set]').find((n) => n.getAttribute('data-keep-set') === id);
  return Object.assign(p, {
    ticked: () => list.querySelectorAll('input').filter((n) => n.checked).map((n) => n.id.replace(/^keep-(season-)?/, '')),
    tick(id, on) { const n = box(id); n.checked = on; (n.listeners.change || []).forEach((fn) => fn({ target: n })); },
    set,
    phases,
    reload(fresh) { now = fresh; },
    async reopen() { press(p, 'show:77', 'keep', '30 days'); await settle(); },
    posts: () => p.calls.filter((c) => c[0] !== 'get').map((c) => [c[1], c[2]]),
    button: (id) => p.doc.getElementById(id),
    state: () => ({ ticked: p.ticked(), focus: p.doc.activeElement.id, gets: p.calls.filter((c) => c[0] === 'get').length,
      open: p.doc.getElementById('keep-dialog-overlay').classList.contains('active') }),
    async confirm() { const b = p.doc.getElementById('keep-dialog-confirm'); b.focus(); click(p.doc, b); await settle(); },
    sent: () => p.calls.filter((c) => c[0] !== 'get').map((c) => [c[1], c[2] && (c[2].duration || c[2].days || ''), c[2] && (c[2].season_ids || c[2].media_item_id || '')]),
    told: () => p.toasts.map((t) => t.message),
  });
}

const scenarios = {
  async keepScheduled() {
    const p = load();
    const kept = ROWS.map((r) => (r.id === 'm1' ? Object.assign({}, r, { kept: true }) : r));
    p.serve(kept, { fact: 'after' });
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    const toast = p.toasts[0];
    const out = {
      call: p.calls[0],
      toast: toast.message,
      undo: toast.undo,
      kept: keptNow(p, 'id:m1'),
      fact: p.bar.fact,
    };
    toast.action.onClick();
    await settle();
    out.undoCall = p.calls[1];
    out.afterUndo = p.toasts[1].message;
    return out;
  },

  async keepNotToUndo() {
    const late = () => Promise.resolve({ ok: true, undo: false });
    const film = load();
    film.serve(ROWS);
    film.replies['post /api/watch/keep'] = late;
    press(film, 'id:m1', 'keep', '30 days');
    await settle();
    const season = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    season.replies['post /api/watch/keep'] = late;
    await season.confirm();
    return [film, season].map((p) => ({ told: p.toasts[0].message, undo: !!p.toasts[0].action }));
  },

  async keepUnscheduled() {
    const p = load();
    p.serve(ROWS);
    press(p, 'id:m2', 'keep', 'forever');
    await settle();
    p.toasts[0].action.onClick();
    await settle();
    return { calls: p.calls, toast: p.toasts[0].message };
  },

  async keepAgain() {
    const p = load();
    p.serve(ROWS);
    press(p, 'id:m3', 'keep', '30 days');
    await settle();
    return { call: p.calls[0], undo: p.toasts[0].undo };
  },

  async keepTwice() {
    const p = load();
    const reply = deferred();
    p.replies['post /api/watch/keep'] = () => reply.promise;
    p.serve(ROWS);
    press(p, 'id:m1', 'keep', '30 days');
    press(p, 'id:m1', 'keep', '30 days');
    reply.resolve({ ok: true });
    await settle();
    return { calls: p.calls.length, told: /Still saving/.test(p.toasts[0].message) };
  },

  async keepFails() {
    const p = load();
    p.replies['post /api/watch/keep'] = () => Promise.reject(new Error('Too many keep operations'));
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    return { errors: p.errors, reads: p.reads, toasts: p.toasts.length };
  },

  async removeKeep() {
    const p = load();
    const unkept = ROWS.map((r) => (r.id === 'm3' ? Object.assign({}, r, { kept: false }) : r));
    p.serve(unkept);
    press(p, 'id:m3', 'unkeep');
    await settle();
    return {
      call: p.calls[0],
      toast: p.toasts[0].message,
      kept: keptNow(p, 'id:m3'),
      keys: p.rowKeys(),
    };
  },

  async deleteMovie() {
    const p = load();
    const reply = deferred();
    p.replies['post /api/media/m1/delete'] = () => reply.promise;
    p.serve(ROWS.slice(1), { fact: 'after' });
    const card = p.row('id:m1');
    press(p, 'id:m1', 'delete');
    await settle();
    const busy = { card: card.classList.contains('is-working'), label: card.querySelector('.watch-card__sub').textContent,
      spinner: !!card.querySelector('.btn-spinner'), held: more(p, 'id:m1').getAttribute('aria-disabled') };
    reply.resolve({ ok: true });
    await settle();
    return { confirm: p.confirms[0], busy, keys: p.rowKeys(), toast: p.toasts[0].message, focus: p.doc.activeElement === more(p, 'id:m2'), fact: p.bar.fact };
  },

  async deleteRefused() {
    const p = load();
    p.replies['post /api/media/m1/delete'] = () => Promise.reject(new Error('This item is kept'));
    press(p, 'id:m1', 'delete');
    await settle();
    return { ...look(p, 'id:m1'), keys: p.rowKeys(), errors: p.errors, focus: p.doc.activeElement === more(p, 'id:m1') };
  },

  async deleteCancelled() {
    const p = load();
    p.answerConfirm(false);
    press(p, 'id:m1', 'delete');
    await settle();
    return { calls: p.calls, busy: look(p, 'id:m1').busy, focus: p.doc.activeElement === more(p, 'id:m1') };
  },

  async seasonDelete() {
    const p = load();
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({
      show_title: 'Night Shift', show_kept: false,
      seasons: [{ id: 's1', season_number: 1, file_size: '9 GB', kept: false }, { id: 's2', season_number: 2, kept: true }],
    });
    const fewer = ROWS.map((r) => (r.id === 's1' ? Object.assign({}, r, { title: 'Night Shift (1 season)' }) : r));
    p.serve(fewer);
    press(p, 'show:77', 'delete');
    await settle();
    const dialog = p.doc.getElementById('season-delete-overlay');
    const opened = dialog.classList.contains('active');
    const buttons = dialog.querySelectorAll('#season-delete-list button');
    click(p.doc, buttons[0]);
    await settle();
    return {
      opened,
      deletes: buttons.length,
      closed: !dialog.classList.contains('active'),
      confirm: p.confirms[0].title,
      call: p.calls.find((c) => c[0] === 'post'),
      title: p.row('show:77').querySelector('.watch-card__title').textContent,
      keys: p.rowKeys(),
    };
  },

  async seasonDeleteCancelledAndRetried() {
    const p = load();
    const url = 'get /api/show/77/seasons';
    p.replies[url] = serverError;
    const dialog = () => p.doc.getElementById('season-delete-overlay');
    const list = () => p.doc.getElementById('season-delete-list');
    press(p, 'show:77', 'delete');
    await settle();
    const retry = list().querySelector('button');
    const says = () => p.doc.getElementById('season-delete-list-status').textContent;
    const heard = [says()];
    const reading = deferred();
    p.replies[url] = () => reading.promise;
    click(p.doc, retry);
    heard.push(says(), p.doc.getElementById('season-delete-list').contains(p.doc.getElementById('season-delete-list-status')));
    reading.reject(new Error('no'));
    await settle();
    const out = { heard, failed: /load/.test(list().textContent), retry: retry && retry.textContent, focus: p.doc.activeElement === list().querySelector('button') };
    p.replies[url] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: false, seasons: [{ id: 's1', season_number: 1 }] });
    click(p.doc, list().querySelector('button'));
    await settle();
    heard.push(says());
    const del = list().querySelector('button');
    out.back = [del.textContent, p.doc.activeElement === del];
    del.blur();
    p.answerConfirm(false);
    click(p.doc, del);
    await settle();
    out.refocused = p.doc.activeElement === del;
    out.cancelled = { open: dialog().classList.contains('active'), deletes: list().querySelectorAll('button').length, posts: p.calls.filter((c) => c[0] === 'post').length };
    return out;
  },

  async seasonDeleteOpens() {
    const p = load();
    const url = 'get /api/show/77/seasons';
    let reply = deferred();
    p.replies[url] = () => reply.promise;
    vm.runInContext(fs.readFileSync(path.join(JS, 'modal-a11y.js'), 'utf8'), p.ctx);
    const at = () => { const a = p.doc.activeElement; return a.id || a.textContent; };
    const tab = (shiftKey) => p.press('Tab', { shiftKey });
    const heading = () => p.doc.getElementById('season-delete-title').focus();
    press(p, 'show:77', 'delete');
    p.ctx.ModalA11y.onOpened('season-delete-overlay');
    const out = { opened: at() };
    reply.resolve({ show_title: 'Night Shift', show_kept: false, seasons: [{ id: 's1', season_number: 1 }, { id: 's2', season_number: 2 }] });
    await settle();
    out.loaded = at();
    tab(false);
    out.tab = [at(), p.doc.activeElement === p.doc.getElementById('season-delete-list').querySelector('button')];
    heading();
    tab(true);
    out.shiftTab = at();
    reply = deferred();
    press(p, 'show:77', 'delete');
    p.ctx.ModalA11y.onOpened('season-delete-overlay');
    reply.reject(new Error('no'));
    await settle();
    out.failed = at();
    reply = deferred();
    click(p.doc, p.doc.getElementById('season-delete-retry'));
    out.retrying = at();
    reply.resolve({ show_title: 'Night Shift', show_kept: false, seasons: [{ id: 's1', season_number: 1 }] });
    await settle();
    out.retried = at();
    return out;
  },

  async keepDialogOpens() {
    const p = load({ rows: [...ROWS.filter((r) => !r.showRk),
      { id: 's1', title: 'Night Shift', showRk: '77', seasons: 1 }, { id: 't1', title: 'Harbour Lights', showRk: '88', seasons: 4 }] });
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: null, seasons: seasonsOf(null) });
    const reply = deferred();
    p.replies['get /api/show/88/seasons'] = () => reply.promise;
    const root = p.doc.documentElement;
    root.clientWidth = p.ctx.innerWidth - 15;
    press(p, 'show:77', 'keep', '30 days');
    await settle();
    press(p, 'show:88', 'keep', 'forever');
    await settle();
    vm.runInContext(fs.readFileSync(path.join(JS, 'modal-a11y.js'), 'utf8'), p.ctx);
    p.ctx.ModalA11y.onOpened('keep-dialog-overlay');
    const bar = [root.style['--page-bar']];
    const list = p.doc.getElementById('keep-season-list');
    const look = () => ({ rows: list.querySelectorAll('.keep-season-item--skel').length, busy: list.getAttribute('aria-busy') });
    const out = {
      title: p.doc.getElementById('keep-dialog-title').textContent,
      pressed: p.doc.querySelectorAll('.keep-dur-btn').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent),
      focus: p.doc.activeElement.textContent,
      waiting: look(),
    };
    reply.resolve({ show_title: 'Harbour Lights', show_kept: null, seasons: seasonsOf(null, null, null, null) });
    await settle();
    out.loaded = look();
    p.doc.getElementById('keep-season-s1').checked = true;
    click(p.doc, p.doc.getElementById('keep-dialog-confirm'));
    await settle();
    out.bar = bar.concat(root.style['--page-bar'] || '');
    return out;
  },

  async keepDialogFromATitle() {
    const rows = async (said) => {
      const p = load();
      p.replies['get /api/show/77/seasons'] = () => deferred().promise;
      p.ctx.MMWatch.admin.keep({ id: 's1', is_tv: true, show_rk: '77', has_rk: true, show_title: 'Night Shift', season_id: '', ...said }, '30 days');
      await settle();
      return p.doc.querySelectorAll('#keep-season-list .keep-season-item--skel').length;
    };
    const out = [];
    for (const seasons of [0, 1, 2, 12]) out.push(await rows({ seasons }));
    return { out, missing: await rows({}), none: await rows({ seasons: null }) };
  },

  async showKeep() {
    const p = load();
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({
      show_title: 'Night Shift', show_kept: false,
      seasons: [{ id: 's1', season_number: 1, kept: false }],
    });
    const kept = ROWS.map((r) => (r.id === 's1' ? Object.assign({}, r, { kept: true }) : r));
    p.serve(kept);
    press(p, 'show:77', 'keep', '30 days');
    await settle();
    const dialog = p.doc.getElementById('keep-dialog-overlay');
    const opened = dialog.classList.contains('active');
    click(p.doc, p.doc.getElementById('keep-dialog-confirm'));
    await settle();
    return {
      opened,
      call: p.calls.find((c) => c[0] === 'post'),
      closed: !dialog.classList.contains('active'),
      kept: keptNow(p, 'show:77'),
    };
  },

  async keepLeavingSeason() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    const ticked = p.ticked();
    await p.confirm();
    const out = { ticked, sent: p.sent(), told: p.told() };
    p.toasts[0].action.onClick();
    await settle();
    out.undone = p.sent().slice(1);
    return out;
  },

  async keepNothingPicked() {
    const none = seasonsOf(null, null, { leaving: leaves(5) });
    const out = {};
    for (const [name, opts] of [['other', { seasonId: '', seasons: none }], ['kept', { seasonId: '', seasons: LEAVING }]]) {
      const p = await keepDialog(opts);
      out[name] = { ticked: p.ticked() };
      await p.confirm();
      Object.assign(out[name], { sent: p.sent(), errors: p.errors.length, busy: p.row('show:77').classList.contains('is-working') });
    }
    return out;
  },

  async keepRoom() {
    const go = async (has_keeps, seasons) => {
      const p = load();
      let answer;
      p.replies['get /api/show/77/seasons'] = () => new Promise((r) => { answer = () => r({ show_title: 'Night Shift', show_kept: null, seasons }); });
      p.serve(ROWS);
      p.ctx.MMWatch.admin.keep({ id: 's1', is_tv: true, show_rk: '77', has_rk: true, show_title: 'Night Shift', has_keeps }, '30 days');
      const tall = () => p.doc.getElementById('keep-season-list').classList.contains('has-keeps');
      const waiting = tall();
      answer();
      await settle();
      return [waiting, tall()];
    };
    const kept = seasonsOf({ kept: true, keep: { action: 'protected_forever', label: 'Kept forever' } });
    return { kept: await go(true, kept), none: await go(false, seasonsOf(null, null)), since: await go(false, kept) };
  },

  async keepFromList() {
    const p = await keepDialog({ seasons: LEAVING });
    const leavingOnes = p.doc.getElementById('keep-season-list').querySelectorAll('.keep-season-details').map((n) => /leaves in (\d+) days/.exec(n.textContent)).map((m) => m && m[1]);
    const ticked = p.ticked();
    const several = await keepDialog({ seasons: seasonsOf(null, null) });
    const movedOn = await keepDialog({ seasons: seasonsOf(null, { leaving: leaves(9) }, { leaving: leaves(4) }) });
    const past = await keepDialog({ seasons: seasonsOf({ leaving: leaves(-3) }) });
    return { ticked, leavingOnes, nothingLeaves: several.ticked(), soonest: movedOn.ticked(), past: past.doc.querySelector('.keep-season-details').textContent };
  },

  async keepChanged() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    p.tick('s3', false);
    const only = { s1: !!p.set('s1'), s2: !!p.set('s2'), all: !!p.set('all') };
    click(p.doc, p.doc.querySelectorAll('.keep-dur-btn').find((b) => b.dataset.dur === '90 days'));
    const label = p.set('s1').textContent;
    click(p.doc, p.set('s1'));
    const pressed = p.set('s1').getAttribute('aria-pressed');
    await p.confirm();
    const back = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    click(back.doc, back.set('s1'));
    back.tick('s1', false);
    back.tick('s1', true);
    await back.confirm();
    return { only, label, pressed, sent: p.sent(), told: p.told(), undo: p.toasts[0].undo, afterUntick: back.sent() };
  },

  async keepRemoved() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    p.tick('s1', false);
    await p.confirm();
    const only = await keepDialog({ seasons: seasonsOf({ keep: FOREVER }, { keep: until(5) }), seasonId: '' });
    only.tick('s1', false);
    await only.confirm();
    return { sent: p.sent(), told: p.told(), undo: p.toasts[0].undo, only: [only.sent(), only.told()] };
  },

  async keepWholeShow() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    p.tick('all', true);
    const ticked = p.ticked();
    await p.confirm();
    const kept = await keepDialog({ seasons: seasonsOf({ keep: FOREVER }, { keep: until(60) }), seasonId: '' });
    kept.tick('all', true);
    await kept.confirm();
    return { ticked, sent: p.sent(), told: p.told(), allKept: [kept.sent(), kept.errors.length] };
  },

  async keepShowUnticked() {
    const forever = await keepDialog({ showKept: FOREVER, seasons: seasonsOf({ keep: FOREVER }, { keep: until(40) }, null), seasonId: '', duration: '7 days' });
    const ticked = forever.ticked();
    forever.tick('s3', false);
    await forever.confirm();
    const days = await keepDialog({ showKept: until(12), seasons: seasonsOf({ keep: until(3) }, { keep: until(40) }, { keep: until(12) }, null), seasonId: '', duration: '7 days' });
    days.tick('s4', false);
    await days.confirm();
    return { ticked, forever: [forever.sent(), forever.told()], days: days.sent() };
  },

  async keepShowChanged() {
    const p = await keepDialog({ showKept: until(12), seasons: seasonsOf({ keep: FOREVER }, { keep: until(12) }, null), seasonId: '' });
    await p.confirm();
    const untouched = [p.sent(), p.errors.length];
    const seasonSet = !!p.set('s1') || !!p.set('s2');
    click(p.doc, p.set('all'));
    await p.confirm();
    return { untouched, seasonSet, sent: p.sent(), told: p.told() };
  },

  async keepPartFails() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    p.tick('s2', true);
    p.replies['post /api/watch/keep'] = () => (p.calls[p.calls.length - 1][2].media_item_id === 's2' ? Promise.reject(new Error('no')) : Promise.resolve({ ok: true }));
    p.reload(seasonsOf({ keep: FOREVER }, { leaving: leaves(3) }, { keep: until(30) }));
    await p.confirm();
    const out = { errors: p.errors, ticked: p.ticked(), focus: p.doc.activeElement.id, open: p.doc.getElementById('keep-dialog-overlay').classList.contains('active') };
    const before = p.sent().length;
    delete p.replies['post /api/watch/keep'];
    await p.confirm();
    out.again = p.sent().slice(before);
    return out;
  },

  async keepUndoRetired() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    await p.confirm();
    p.reload(seasonsOf({ keep: FOREVER }, { leaving: leaves(3) }, { keep: until(30) }));
    press(p, 'show:77', 'keep', '30 days');
    await settle();
    p.tick('s2', false);
    p.tick('s3', false);
    await p.confirm();
    const always = await keepDialog({ seasons: LEAVING, seasonId: 's3', duration: 'forever' });
    await always.confirm();
    const plain = await keepDialog({ seasons: seasonsOf(null, null), seasonId: '', duration: 'forever' });
    plain.tick('s2', true);
    await plain.confirm();
    plain.toasts[0].action.onClick();
    await settle();
    return { dismissed: p.toasts[0].dismissed, sent: p.sent(), forever: [always.sent(), always.toasts[0].undo], unscheduled: plain.sent() };
  },

  async keepSaysWhatItRead() {
    const out = {};
    const one = await keepDialog({ seasons: seasonsOf({ keep: FOREVER }, { keep: until(5) }, null), seasonId: '' });
    one.tick('s3', true);
    click(one.doc, one.set('s1'));
    one.tick('s2', false);
    await one.confirm();
    out.seasons = one.posts();
    const all = await keepDialog({ seasons: seasonsOf({ keep: FOREVER }, null), seasonId: '' });
    all.tick('all', true);
    await all.confirm();
    out.all = all.posts();
    const end = await keepDialog({ showKept: until(12), seasons: seasonsOf({ keep: until(12) }, null), seasonId: '' });
    end.tick('s2', false);
    await end.confirm();
    out.end = end.posts();
    const none = await keepDialog({ showKept: until(12), seasons: seasonsOf({ keep: until(12) }, null), seasonId: '' });
    click(none.doc, none.button('keep-dialog-remove'));
    await settle();
    out.removeAll = none.posts();
    const own = await keepDialog({ seasons: seasonsOf({ keep: until(5) }, null), seasonId: '' });
    click(own.doc, own.button('keep-dialog-remove'));
    await settle();
    out.removeOwn = own.posts();
    out.expected = { forever: readOf(FOREVER), five: readOf(until(5)), twelve: readOf(until(12)) };
    return out;
  },

  async keepChangedMeanwhile() {
    const now = seasonsOf({ keep: FOREVER }, null, { leaving: leaves(5) });
    const run = async (opts, url, act) => {
      const p = await keepDialog(opts);
      act(p);
      p.replies[url] = keepChanged;
      p.reload(now);
      await p.confirm();
      return { sent: p.posts().length, errors: p.errors, phases: p.phases, told: p.told(), ...p.state() };
    };
    const out = {
      season: await run({ seasons: seasonsOf({ keep: until(5) }, null, { leaving: leaves(5) }), seasonId: 's2' }, 'postForm /api/media/s2/keep', (p) => p.tick('s1', false)),
      all: await run({ seasons: seasonsOf(null, null, null), seasonId: '' }, 'post /api/show/77/keep', (p) => p.tick('all', true)),
      end: await run({ showKept: FOREVER, seasons: seasonsOf({ keep: FOREVER }, null, null), seasonId: '' }, 'post /api/show/77/remove', (p) => p.tick('s3', false)),
      drop: await run({ seasons: seasonsOf({ keep: until(5) }, { keep: until(5) }, null), seasonId: '' }, 'post /api/media/s1/unprotect', (p) => p.tick('s1', false)),
    };
    for (const status of [404, 409]) {
      const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
      p.replies['post /api/watch/keep'] = () => Promise.reject(Object.assign(new Error('gone'), { status }));
      p.reload(now);
      await p.confirm();
      out['watch' + status] = { errors: p.errors, phases: p.phases, ticked: p.ticked() };
    }
    const rm = await keepDialog({ showKept: FOREVER, seasons: seasonsOf({ keep: FOREVER }, null, null), seasonId: '' });
    rm.replies['post /api/show/77/remove'] = keepChanged;
    rm.reload(now);
    click(rm.doc, rm.button('keep-dialog-remove'));
    await settle();
    out.removeAll = { errors: rm.errors, phases: rm.phases, told: rm.told(), ...rm.state() };
    return out;
  },

  async filmKeepChangedMeanwhile() {
    const p = load();
    const phases = [];
    p.doc.addEventListener('mm:watch-admin', (e) => phases.push(e.detail.phase));
    p.serve(ROWS);
    p.replies['postForm /api/media/m2/keep'] = keepChanged;
    press(p, 'id:m2', 'keep', '30 days');
    await settle();
    delete p.replies['postForm /api/media/m2/keep'];
    press(p, 'id:m2', 'keep', '30 days');
    await settle();
    p.replies['post /api/media/m2/unprotect'] = keepChanged;
    p.toasts[0].action.onClick();
    await settle();
    return { phases, errors: p.errors.length, toasts: p.toasts.length, undo: p.calls[p.calls.length - 1] };
  },

  async dialogsSayAfter() {
    const p = load();
    p.replies['get /api/show/77/seasons'] = () => deferred().promise;
    let timers = [];
    p.ctx.setTimeout = (fn) => timers.push(fn);
    p.ctx.clearTimeout = (id) => { if (id) timers[id - 1] = () => {}; };
    const run = () => { const due = timers; timers = []; due.forEach((fn) => fn()); };
    const out = [];
    for (const [go, id] of [[() => press(p, 'show:77', 'keep', '30 days'), 'keep-season-list-status'],
      [() => press(p, 'show:77', 'delete'), 'season-delete-list-status']]) {
      go();
      const line = p.doc.getElementById(id);
      const before = line.textContent;
      run();
      out.push([line.getAttribute('role'), before, line.textContent]);
    }
    // In before it was said: it is not said after.
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: false, seasons: [] });
    press(p, 'show:77', 'delete');
    await settle();
    run();
    out.push(p.doc.getElementById('season-delete-list-status').textContent);
    return out;
  },

  async unreadRowHoldsItsKeep() {
    const p = load();
    p.failReads();
    const read = (id) => adminOf(p, 'id:' + id).keep;
    press(p, 'id:m2', 'keep', 'forever');
    await settle();
    const out = { kept: read('m2') };
    press(p, 'id:m3', 'unkeep');
    await settle();
    out.removed = read('m3');
    p.toasts[0].action.onClick();
    await settle();
    out.undone = read('m2');
    p.replies['post /api/watch/keep'] = () => Promise.resolve({ ok: true, keep: MADE });
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    out.scheduled = read('m1');
    press(p, 'id:m1', 'keep', 'forever');
    await settle();
    out.next = p.calls[p.calls.length - 1];
    return out;
  },

  async undoTooLate() {
    const late = () => Promise.reject(Object.assign(new Error('late'), { error: 'too_late', status: 409 }));
    const run = async (p) => {
      const phases = [];
      p.doc.addEventListener('mm:watch-admin', (e) => phases.push(e.detail.phase));
      p.replies['post /api/watch/unkeep'] = late;
      p.toasts[0].action.onClick();
      await settle();
      return phases;
    };
    const film = load();
    film.serve(ROWS);
    press(film, 'id:m1', 'keep', '30 days');
    await settle();
    const season = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    await season.confirm();
    return { film: await run(film), season: await run(season) };
  },

  async staleRow() {
    const run = async (id, reply, act, rows, shown) => {
      const p = load({ rows: shown });
      const phases = [];
      p.doc.addEventListener('mm:watch-admin', (e) => phases.push([e.detail.phase, e.detail.action]));
      if (rows) p.serve(rows); else p.failReads();
      p.replies[reply[0]] = reply[1];
      act(p);
      await settle();
      const row = p.row('id:' + id);
      return { call: p.calls[0], phases, errors: p.errors, toasts: p.toasts.length, busy: row.classList.contains('is-working'),
        kept: keptNow(p, 'id:' + id), read: adminOf(p, 'id:' + id).keep };
    };
    const keep = (id) => (p) => press(p, 'id:' + id, 'keep', '30 days');
    const now = ROWS.map((r) => (r.id === 'm2' || r.kept ? r : Object.assign({}, r, { kept: true, deletion: false })))
      .map((r) => (r.kept ? Object.assign({}, r, { keep: readOf(FOREVER) }) : r));
    const gone = () => Promise.reject(Object.assign(new Error('gone'), { status: 404 }));
    return {
      forever: readOf(FOREVER),
      keep: await run('m3', ['postForm /api/media/m3/keep', keepChanged], keep('m3'), now),
      remove: await run('m3', ['post /api/media/m3/unprotect', keepChanged], (p) => press(p, 'id:m3', 'unkeep'), now),
      scheduled: await run('m1', ['post /api/watch/keep', gone], keep('m1'), now),
      keptWithNoKeep: await run('m3', ['post /api/media/m3/unprotect', keepChanged], (p) => press(p, 'id:m3', 'unkeep'), now,
        ROWS.map((r) => Object.assign({}, r, { keep: null }))),
      unread: await run('m2', ['postForm /api/media/m2/keep', keepChanged], keep('m2'), null),
    };
  },

  async seasonUndoChangedMeanwhile() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: '' });
    p.tick('s2', true);
    await p.confirm();
    p.replies['post /api/media/s2/unprotect'] = keepChanged;
    p.toasts[0].action.onClick();
    await settle();
    const unsaid = await keepDialog({ seasons: seasonsOf(null, null), seasonId: '' });
    unsaid.tick('s2', true);
    unsaid.replies['postForm /api/media/s2/keep'] = () => Promise.resolve({ ok: true });
    await unsaid.confirm();
    return { undo: p.posts()[1], reads: p.state().gets, made: MADE, errors: p.errors, phases: p.phases, told: p.told(), unsaid: unsaid.toasts[0].undo };
  },

  async undoRetiredAsASaveStarts() {
    const out = {};
    const kept = seasonsOf({ keep: until(30) }, null, null);
    for (const [name, showKept, act] of [
      ['same', null, (p) => { p.tick('s1', false); p.tick('s2', true); }],
      ['other', null, (p) => p.tick('s2', true)],
      ['all', null, (p) => p.tick('all', true)],
      ['show', FOREVER, (p) => p.tick('s3', false)],
    ]) {
      const p = await keepDialog({ seasons: seasonsOf(null, null, null), seasonId: 's1' });
      await p.confirm();
      p.replies['get /api/show/77/seasons'] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: showKept, seasons: kept });
      const reply = deferred();
      ['post /api/media/s1/unprotect', 'postForm /api/media/s2/keep', 'post /api/show/77/keep', 'post /api/show/77/remove']
        .forEach((url) => { p.replies[url] = () => reply.promise; });
      await p.reopen();
      act(p);
      await p.confirm();
      out[name] = p.toasts[0].dismissed;
    }
    const removeAll = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    await removeAll.confirm();
    removeAll.reload(seasonsOf({ keep: until(30) }, null));
    await removeAll.reopen();
    removeAll.replies['post /api/media/s1/unprotect'] = () => deferred().promise;
    click(removeAll.doc, removeAll.button('keep-dialog-remove'));
    await settle();
    out.removeAll = removeAll.toasts[0].dismissed;
    const film = load();
    film.serve(ROWS.map((r) => (r.id === 'm1' ? Object.assign({}, r, { kept: true }) : r)));
    press(film, 'id:m1', 'keep', '30 days');
    await settle();
    film.replies['post /api/media/m1/unprotect'] = () => deferred().promise;
    press(film, 'id:m1', 'unkeep');
    out.film = film.toasts[0].dismissed;
    const again = load();
    again.serve(ROWS.map((r) => (r.id === 'm1' ? Object.assign({}, r, { kept: true }) : r)));
    press(again, 'id:m1', 'keep', '30 days');
    await settle();
    again.replies['postForm /api/media/m1/keep'] = () => deferred().promise;
    press(again, 'id:m1', 'keep', 'forever');
    out.filmKeptAgain = again.toasts[0].dismissed;
    return out;
  },

  async undoWithTheDialogOpen() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    await p.confirm();
    p.reload(seasonsOf({ keep: until(30) }, null));
    await p.reopen();
    const reply = deferred();
    p.replies['post /api/media/s1/unprotect'] = () => reply.promise;
    const buttons = () => [!!p.button('keep-dialog-confirm').disabled, !!p.button('keep-dialog-remove').disabled];
    const out = { before: buttons(), ticked: p.ticked() };
    p.toasts[0].action.onClick();
    await settle();
    out.during = buttons();
    p.reload(seasonsOf(null, null));
    const gets = p.state().gets;
    reply.resolve({ ok: true });
    await settle();
    out.after = buttons();
    out.reads = p.state().gets - gets;
    out.tickedAfter = p.ticked();
    return out;
  },

  async undoWaitsForASave() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    await p.confirm();
    p.reload(seasonsOf({ keep: until(30) }, null));
    await p.reopen();
    const reply = deferred();
    p.replies['postForm /api/media/s2/keep'] = () => reply.promise;
    p.tick('s2', true);
    await p.confirm();
    p.toasts[0].action.onClick();
    await settle();
    const during = p.posts().map((c) => c[0]);
    reply.resolve({ ok: true, keep: MADE });
    await settle();
    return { during, after: p.posts().map((c) => c[0]).slice(during.length) };
  },

  async removeAllWaitsForAnUndo() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    await p.confirm();
    p.reload(seasonsOf({ keep: until(30) }, { keep: until(30) }));
    await p.reopen();
    const reply = deferred();
    p.replies['post /api/media/s1/unprotect'] = () => reply.promise;
    const asked = deferred();
    p.ctx.UIFeedback.confirm = () => asked.promise;
    click(p.doc, p.button('keep-dialog-remove'));
    p.toasts[0].action.onClick();
    asked.resolve(true);
    await settle();
    const during = p.posts().length;
    reply.resolve({ ok: true });
    await settle();
    return { during, after: p.posts().length };
  },

  async undoAnnouncedAtOnce() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    await p.confirm();
    await p.reopen();
    p.replies['postForm /api/media/s2/keep'] = () => deferred().promise;
    p.tick('s1', false);
    p.tick('s2', true);
    await p.confirm();
    p.phases.length = 0;
    p.toasts[0].action.onClick();
    return p.phases;
  },

  async clickedUnfocused() {
    const p = await keepDialog({ seasons: seasonsOf({ keep: until(30) }, null), seasonId: 's2' });
    p.replies['postForm /api/media/s2/keep'] = () => deferred().promise;
    p.doc.body.focus();
    click(p.doc, p.button('keep-dialog-confirm'));
    const keep = p.doc.activeElement.id;
    const r = await keepDialog({ seasons: seasonsOf({ keep: until(30) }, null), seasonId: 's2' });
    r.doc.body.focus();
    r.answerConfirm(false);
    click(r.doc, r.button('keep-dialog-remove'));
    const asked = r.doc.activeElement.id;
    await settle();
    return { keep, asked, cancelled: r.doc.activeElement.id };
  },

  async seasonsAnnounced() {
    const announced = async (opts, act) => {
      const d = await keepDialog(opts);
      const ids = [];
      d.doc.addEventListener('mm:watch-admin', (e) => { if (e.detail.phase === 'busy') ids.push(e.detail.ids); });
      await act(d);
      await settle();
      return ids;
    };
    const none = { seasons: seasonsOf(null, null), seasonId: '' };
    return {
      some: await announced(none, async (d) => { d.tick('s2', true); await d.confirm(); d.toasts[0].action.onClick(); }),
      show: await announced(none, async (d) => { d.tick('all', true); await d.confirm(); }),
      end: await announced({ showKept: FOREVER, seasons: seasonsOf({ keep: FOREVER }, null), seasonId: '' },
        async (d) => { click(d.doc, d.button('keep-dialog-remove')); }),
    };
  },

  async oneKeepToast() {
    const p = load({ rows: ROWS });
    const up = () => p.toasts.filter((t) => !t.dismissed).map((t) => t.message);
    const film = { id: 'm9', title: 'Copperline', kept: true, keep: MADE };
    const out = [];
    await p.ctx.MMWatch.admin.keep(film, '30 days');
    await p.ctx.MMWatch.admin.keep(film, '7 days');
    out.push(up());
    await p.ctx.MMWatch.admin.removeKeep(film);
    out.push(up());
    await p.ctx.MMWatch.admin.keep({ ...film, kept: false, keep: null }, '90 days');
    out.push(up());
    p.toasts[p.toasts.length - 1].dismissed = true;
    p.toasts[p.toasts.length - 1].action.onClick();
    await settle();
    await p.ctx.MMWatch.admin.keep(film, 'forever');
    out.push(up());
    await p.ctx.MMWatch.admin.keep({ ...film, id: 'm8', title: 'Other' }, 'forever');
    out.push(up());
    return out;
  },

  async deleteRefusedAsKept() {
    const out = {};
    for (const error of ['kept', 'delete_refused']) {
      const p = load();
      p.serve(ROWS.map((r) => (r.id === 'm1' ? { ...r, kept: true } : r)));
      p.replies['post /api/media/m1/delete'] = () => Promise.reject(Object.assign(new Error('no'), { error, status: 409 }));
      press(p, 'id:m1', 'delete');
      await settle();
      out[error] = { reads: p.reads.length, kept: keptNow(p, 'id:m1'),
        busy: p.row('id:m1').classList.contains('is-working'), errors: p.errors.length };
    }
    return out;
  },

  async failedKeepDropsNothing() {
    const p = await keepDialog({ seasons: seasonsOf({ keep: until(5) }, null), seasonId: 's2' });
    p.tick('s1', false);
    p.replies['postForm /api/media/s2/keep'] = serverError;
    await p.confirm();
    const show = await keepDialog({ showKept: FOREVER, seasons: seasonsOf({ keep: FOREVER }, null, null), seasonId: '' });
    show.tick('s3', false);
    show.replies['post /api/show/77/remove'] = serverError;
    await show.confirm();
    const label = show.button('keep-dialog-confirm').textContent;
    return { sent: p.posts().map((c) => c[0]), phases: p.phases,
      show: show.posts().map((c) => c[0]), showPhases: show.phases, label, ticked: show.ticked(), disabled: !!show.button('keep-dialog-confirm').disabled };
  },

  async partSavedIsAnnounced() {
    const p = await keepDialog({ seasons: seasonsOf(null, null), seasonId: 's1' });
    p.tick('s2', true);
    p.replies['postForm /api/media/s2/keep'] = serverError;
    await p.confirm();
    const rm = await keepDialog({ seasons: seasonsOf({ keep: until(5) }, { keep: until(5) }), seasonId: '' });
    rm.replies['post /api/media/s2/unprotect'] = serverError;
    click(rm.doc, rm.button('keep-dialog-remove'));
    await settle();
    const no = await keepDialog({ seasons: seasonsOf({ keep: until(5) }, null), seasonId: '' });
    no.replies['post /api/media/s1/unprotect'] = serverError;
    click(no.doc, no.button('keep-dialog-remove'));
    await settle();
    const of = (d) => ({ phases: d.phases, errors: d.errors, told: d.told() });
    return { kept: of(p), removed: of(rm), nothing: of(no) };
  },

  async keepShowShortened() {
    const seasons = seasonsOf({ keep: FOREVER }, { keep: until(90) }, { keep: OWN_FOREVER }, { keep: until(3) }, null);
    const p = await keepDialog({ showKept: FOREVER, seasons, seasonId: '', duration: '7 days' });
    click(p.doc, p.set('all'));
    await p.confirm();
    const days = await keepDialog({ showKept: until(60), seasons: seasonsOf({ keep: until(60) }, { keep: until(40) }, { keep: until(7) }), seasonId: '', duration: '7 days' });
    click(days.doc, days.set('all'));
    await days.confirm();
    const own = await keepDialog({ showKept: FOREVER, seasons: seasonsOf({ keep: OWN_FOREVER }), seasonId: '', duration: '7 days' });
    click(own.doc, own.set('all'));
    await own.confirm();
    return { forever: p.sent(), told: p.told(), days: days.sent(), own: [own.sent(), own.errors] };
  },

  async keepToastDialog() {
    const p = load();
    p.serve(ROWS);
    p.doc.body.appendChild(build(p.doc, ['div', { id: 'ttl' }]));
    p.doc.documentElement.classList.add('has-ttl');
    await p.ctx.MMWatch.admin.keep({ id: '42', is_tv: false, title: 'Dune' }, '7 days');
    p.doc.documentElement.classList.remove('has-ttl');
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    return p.toasts.map((t) => t.dialog);
  },

  // How many there are, as the page says it: with the grid open, with it closed, and by answers out of order.
  async totals() {
    const start = (opts) => {
      const p = load(opts);
      p.all = p.doc.querySelector('.watch-page').appendChild(build(p.doc, ['a', { class: 'watch-all-link' }, ['span', 'All 4 titles']]));
      p.heard = [];
      p.doc.addEventListener('mm:watch-total', (e) => p.heard.push(e.detail.total));
      return p;
    };
    const link = (p) => [p.all.textContent, p.all.hidden];
    const p = start();
    p.serve(ROWS.slice(1));
    press(p, 'id:m1', 'delete');
    await settle();
    const open = link(p);
    // It opens saying so, before its titles have come; a filter's are not known yet.
    const opens = ['', 'kept'].map((type) => p.make({ sort: 'added', type, ready: null, row: null, drawn: null }).total);
    const closed = start({ closed: true });
    const gone = async (rows) => { closed.serve(rows); await closed.ctx.MMWatch.admin.confirmDelete('42', 'Dune'); await settle(); return link(closed); };
    const one = await gone(ROWS.slice(3));
    const none = await gone([]);
    closed.serve(ROWS);
    closed.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    await settle();
    const scanned = link(closed);
    // One asked for before a change answers after it.
    const late = start({ closed: true });
    const wait = deferred();
    late.serve(ROWS, {}, wait);
    late.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    late.serve(ROWS.slice(2));
    late.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    await settle();
    wait.resolve();
    await settle();
    return { open, opens, one, none, scanned, late: link(late), heard: closed.heard };
  },

  async detailCalls() {
    const p = load({ closed: true });
    const admin = p.ctx.MMWatch.admin;
    await admin.keep({ id: '42', is_tv: false, show_rk: '', has_rk: false, show_title: 'Dune' }, '7 days');
    const deleted = await admin.confirmDelete('42', 'Dune');
    return { calls: p.calls, toasts: p.toasts.map((t) => t.message), deleted, reads: p.reads, keys: Object.keys(admin).sort() };
  },

  async detailKeepAsRead() {
    const p = load({ closed: true });
    const film = { id: '42', is_tv: false, title: 'Dune', kept: true, keep: MADE };
    await p.ctx.MMWatch.admin.keep(film, '7 days');
    await p.ctx.MMWatch.admin.removeKeep(film);
    return p.calls;
  },

  async keepForeverScheduled() {
    const p = load();
    p.serve(ROWS);
    press(p, 'id:m1', 'keep', 'forever');
    await settle();
    return { call: p.calls[0], toast: p.toasts[0].message, undo: p.toasts[0].undo };
  },

  async removeKeepRetiresUndo() {
    const p = load();
    const kept = ROWS.map((r) => (r.id === 'm1' ? Object.assign({}, r, { kept: true }) : r));
    p.serve(kept);
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    const first = p.toasts[0];
    const out = { before: first.dismissed };
    press(p, 'id:m1', 'unkeep');
    await settle();
    out.after = first.dismissed;
    out.messages = p.toasts.map((t) => t.message);
    out.calls = p.calls.map((c) => c[1]);
    return out;
  },

  async removeAllLabel() {
    const rows = ROWS.map((r) => (r.id === 's1' ? Object.assign({}, r, { kept: true }) : r));
    const p = load({ rows });
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({
      show_title: 'Night Shift', show_kept: false,
      seasons: [{ id: 's1', season_number: 1, kept: true }],
    });
    const reply = deferred();
    p.replies['post /api/media/s1/unprotect'] = () => reply.promise;
    p.serve(ROWS);
    press(p, 'show:77', 'keep', '30 days');
    await settle();
    const remove = p.doc.getElementById('keep-dialog-remove');
    click(p.doc, remove);
    await settle();
    const note = p.doc.getElementById('keep-dialog-note');
    const busy = {
      label: remove.textContent,
      disabled: remove.disabled === true,
      note: note.textContent,
      noteInButtons: note.parentNode.classList.contains('keep-dialog-actions'),
      row: p.row('show:77').querySelector('.watch-card__sub').textContent,
    };
    reply.resolve({ ok: true });
    await settle();
    return { busy, after: remove.textContent };
  },

  // A keep while it is saved, and when it fails: the caption says so, the menu is held, and the focus stays.
  async keepBusyThenFails() {
    const p = load();
    const reply = deferred();
    p.replies['post /api/watch/keep'] = () => reply.promise;
    press(p, 'id:m1', 'keep', '30 days');
    const busy = look(p, 'id:m1');
    reply.reject(new Error('Too many keep operations'));
    await settle();
    return { busy, after: look(p, 'id:m1'), errors: p.errors, focus: p.doc.activeElement === more(p, 'id:m1') };
  },

  // Saved, but not yet read again.
  async keepBusyUntilItIsBack() {
    const p = load();
    const slow = deferred();
    p.serve(ROWS.map((r) => (r.id === 'm1' ? { ...r, kept: true } : r)), {}, slow);
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    const whileReading = { ...look(p, 'id:m1'), toast: p.toasts[0].message };
    slow.resolve();
    await settle();
    return { whileReading, ...look(p, 'id:m1'), kept: keptNow(p, 'id:m1'), focus: p.doc.activeElement === more(p, 'id:m1') };
  },

  async keepThenReadFails() {
    const p = load();
    p.failReads();
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    return { errors: p.errors, ...look(p, 'id:m1'), kept: keptNow(p, 'id:m1'), del: adminOf(p, 'id:m1').delete, unkeep: adminOf(p, 'id:m1').unkeep };
  },

  // The read that fails after a keep of some of a show's seasons says nothing of the others.
  async showKeepThenReadFails() {
    const p = await keepDialog({ seasons: LEAVING, seasonId: 's3' });
    p.failReads();
    await p.confirm();
    return { kept: keptNow(p, 'show:77'), del: adminOf(p, 'show:77').delete, busy: look(p, 'show:77').busy };
  },

  async removingIsShown() {
    const p = load();
    p.serve(ROWS);
    const undo = deferred();
    p.replies['post /api/media/m2/unprotect'] = () => undo.promise;
    press(p, 'id:m2', 'keep', '30 days');
    await settle();
    p.toasts[0].action.onClick();
    const out = { undo: look(p, 'id:m2') };
    undo.reject(new Error('No active protection found'));
    await settle();
    out.afterUndo = look(p, 'id:m2');
    const remove = deferred();
    p.replies['post /api/media/m3/unprotect'] = () => remove.promise;
    press(p, 'id:m3', 'unkeep');
    out.remove = look(p, 'id:m3');
    remove.reject(new Error('No active protection found'));
    await settle();
    return { ...out, afterRemove: look(p, 'id:m3'), errors: p.errors };
  },

  // A season deleted of a show that stays, and of one that has none left.
  async seasonDeleteFocus() {
    const out = {};
    for (const left of [false, true]) {
      const p = load();
      p.replies['get /api/show/77/seasons'] = () => Promise.resolve({ show_title: 'Night Shift', show_kept: false, seasons: [{ id: 's1', season_number: 1 }, { id: 's2', season_number: 2 }] });
      p.serve(left ? ROWS.filter((r) => r.id !== 's1') : ROWS);
      press(p, 'show:77', 'delete');
      await settle();
      click(p.doc, p.doc.querySelectorAll('#season-delete-list button')[0]);
      p.doc.body.focus(); // the dialog and the confirm have closed
      await settle();
      out[left ? 'left' : 'stayed'] = { keys: p.rowKeys(), focus: p.doc.activeElement === more(p, left ? 'id:m3' : 'show:77') };
    }
    return out;
  },

  // Kept, it is no longer of the filter on: it stays, changed, so its Undo has a card.
  async keptLeavesTheFilter() {
    const p = load({ filter: 'stale' });
    p.serve(ROWS.map((r) => (r.id === 'm1' ? { ...r, kept: true } : r)), { fact: 'after' });
    press(p, 'id:m1', 'keep', '30 days');
    await settle();
    return { keys: p.rowKeys(), kept: keptNow(p, 'id:m1'), undo: p.toasts[0].undo, fact: p.bar.fact, read: p.reads[0] };
  },

  async busyThroughAnother() {
    const two = load();
    const say = (p, phase, action, id, showRk) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase, action, id, showRk: showRk || '' } }));
    const out = {};
    say(two, 'busy', 'delete', 's1', '77');
    say(two, 'busy', 'delete', 's2', '77');
    say(two, 'failed', 'delete', 's1', '77');
    out.oneLeft = look(two, 'show:77').busy;
    say(two, 'failed', 'delete', 's2', '77');
    out.noneLeft = look(two, 'show:77').busy;
    say(two, 'busy', 'delete', 'm1');
    say(two, 'busy', 'unkeep', 'm1');
    say(two, 'failed', 'delete', 'm1');
    out.unkeepLeft = look(two, 'id:m1').label;
    say(two, 'failed', 'unkeep', 'm1');
    out.bothFailed = look(two, 'id:m1');
    // One of two is saved but cannot be read: still busy with the other.
    const unread = load();
    unread.failReads();
    say(unread, 'busy', 'keep', 'm1');
    say(unread, 'busy', 'delete', 'm1');
    say(unread, 'done', 'keep', 'm1');
    await settle();
    out.unreadStillBusy = look(unread, 'id:m1').busy;
    // Filled anew meanwhile, its card is busy still.
    const again = load();
    say(again, 'busy', 'keep', 'm1');
    again.serve(ROWS);
    const content = await again.source.sort.choose('az', '');
    out.filledAnew = content.cards.find((c) => c.getAttribute('data-key') === 'id:m1').classList.contains('is-working');
    return out;
  },

  // A page asked for before a change was saved, which comes after it.
  async pageFromBeforeAChange() {
    const p = load();
    const wait = deferred();
    p.serve(ROWS, {}, wait);
    const page = p.source.fetchPage('c');
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'busy', action: 'keep', id: 'm1', showRk: '' } }));
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'keep', id: 'm1', showRk: '' } }));
    const before = p.reads.length;
    wait.resolve();
    await page;
    await settle();
    return p.reads.slice(before);
  },

  // As above, of the first page: filled anew, opened with it, and held back while the grid moves.
  async firstPageFromBeforeAChange() {
    const change = (p) => ['busy', 'done'].forEach((phase) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase, action: 'keep', id: 'm1', showRk: '' } })));
    const go = async (asks) => {
      const p = load();
      const wait = deferred();
      p.serve(ROWS, {}, wait);
      return asks(p, wait);
    };
    const since = async (p, act) => { const n = p.reads.length; await act(); await settle(); return p.reads.slice(n); };
    return {
      sorted: await go(async (p, wait) => {
        const asked = p.source.sort.choose('size', '');
        change(p);
        return since(p, async () => { wait.resolve(); (await asked).placed(); });
      }),
      heldBack: await go(async (p, wait) => {
        const asked = p.source.sort.choose('size', '');
        wait.resolve();
        const content = await asked;
        change(p);
        await settle();
        return since(p, async () => content.placed());
      }),
      unchanged: await go(async (p, wait) => {
        change(p);
        wait.resolve();
        await settle();
        return since(p, async () => (await p.source.sort.choose('size', '')).placed());
      }),
      opened: await go(async (p, wait) => {
        let ready = null;
        p.ctx.MMWatch.expand.library = (_sort, _type, how) => { ready = how.ready; };
        p.ctx.setTimeout = () => 1; // the titles are in before it is left without them
        p.ctx.MMWatch.library.open('/watch?all=library', { leave() {}, arrive() {}, fail() {} });
        change(p);
        wait.resolve();
        await settle();
        p.ctx.setTimeout = (fn) => { fn(); return 1; };
        return since(p, async () => p.make({ sort: 'added', type: '', ready, cards: () => [] }));
      }),
    };
  },

  // Each card that stands is read again for a schedule; one changed meanwhile keeps what it says.
  async scheduledBeforeAChange() {
    const p = load();
    const wait = deferred();
    p.serve(ROWS, {}, wait);
    p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-scheduled', {}));
    p.serve(ROWS.map((r) => (r.id === 'm2' ? { ...r, kept: true } : r)));
    press(p, 'id:m2', 'keep', 'forever');
    await settle();
    const before = keptNow(p, 'id:m2');
    wait.resolve();
    await settle();
    return [before, keptNow(p, 'id:m2')];
  },

  // Asked for before what is scheduled changes, in once the cards were read again for it.
  async readBeforeASchedule() {
    const schedule = (p) => p.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    const keptRows = ROWS.map((r) => (r.id === 'm2' ? { ...r, kept: true } : r));
    /* What *asks* starts is answered as it was; *lands* puts it in, and the reads that follow are told. */
    const go = async (asks, lands, opts, now) => {
      const p = load(opts);
      const wait = deferred();
      p.serve(ROWS, {}, wait);
      const asked = asks(p);
      p.serve(now || keptRows);
      schedule(p);
      await settle();
      const n = p.reads.length;
      wait.resolve();
      const got = await asked;
      const draw = (cards) => { const all = p.doc.querySelector('.watch-all'); all.replaceChildren(); cards.forEach((c) => all.appendChild(c)); };
      await lands(p, got, draw);
      await settle();
      return { reads: p.reads.slice(n).map((u) => u.slice(u.indexOf('?'))), kept: p.row('id:m2') && keptNow(p, 'id:m2') };
    };
    const choose = (p) => p.source.sort.choose('size', '');
    const set = (p, got, draw) => { draw(got.cards); got.placed(); };
    const timers = [];
    const fire = () => timers.splice(0).forEach((fn) => fn());
    return {
      sorted: await go(choose, set),
      // One the filter no longer has is read by itself.
      gone: await go(choose, set, {}, keptRows.filter((r) => r.id !== 'm2')),
      page: await go((p) => p.source.fetchPage('c'), (_p, got, draw) => { draw(got.cards); fire(); }, { timers }),
      opened: await go((p) => {
        let ready = null;
        p.ctx.MMWatch.expand.library = (_sort, _type, how) => { ready = how.ready; };
        p.ctx.MMWatch.grid.shows = () => null; // the schedule changes before it opens
        p.ctx.MMWatch.library.open('/watch?all=library', { leave() {}, arrive() {}, fail() {} });
        return { read: () => ready };
      }, async (p, got, draw) => {
        await settle();
        p.ctx.MMWatch.grid.shows = () => ({ sort: 'added', filter: '' });
        draw(p.make({ sort: 'added', type: '', ready: got.read(), cards: () => [] }).cards);
        fire();
      }, { timers }),
      // A title being read by itself then is read once more, and is not left busy.
      alone: await (async () => {
        const p = load();
        const wait = deferred();
        p.serve(ROWS, {}, wait);
        ['busy', 'done'].forEach((phase) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase, action: 'keep', id: 'm2', showRk: '' } })));
        p.serve(keptRows);
        schedule(p);
        await settle();
        wait.resolve();
        await settle();
        return { reads: p.reads.map((u) => u.slice(u.indexOf('?'))), kept: keptNow(p, 'id:m2'), busy: look(p, 'id:m2').busy };
      })(),
      // Each walk of its own schedule, and no more.
      twice: await (async () => {
        const p = load();
        const wait = deferred();
        p.serve(ROWS, { fact: 'old' }, wait);
        schedule(p);
        p.serve(keptRows, { fact: 'new' });
        schedule(p);
        await settle();
        wait.resolve();
        await settle();
        return { reads: p.reads.length, kept: keptNow(p, 'id:m2'), fact: p.bar.fact };
      })(),
    };
  },

  // Two changes of one title, each read again after it; the first read lands last.
  async readsOutOfOrder() {
    const p = load();
    const done = (action) => ['busy', 'done'].forEach((phase) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase, action, id: 'm2', showRk: '' } })));
    const first = deferred();
    p.serve(ROWS, { fact: 'first' }, first);
    done('unkeep');
    p.serve(ROWS.map((r) => (r.id === 'm2' ? { ...r, kept: true } : r)), { fact: 'second' });
    done('keep');
    await settle();
    const second = [keptNow(p, 'id:m2'), p.bar.fact];
    first.resolve();
    await settle();
    return [second, [keptNow(p, 'id:m2'), p.bar.fact]];
  },

  // Kept while its menu, which has Delete, was open.
  async deleteOfOneKeptMeanwhile() {
    const p = load();
    press(p, 'id:m3', 'delete');
    await settle();
    return { calls: p.calls, confirms: p.confirms.length, told: p.toasts.map((t) => t.message) };
  },

  // Done while the grid is closed: nothing is read, and nothing is left busy.
  async doneOutOfSight() {
    const p = load({ closed: true });
    ['busy', 'done'].forEach((phase) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase, action: 'keep', id: 'm1', showRk: '' } })));
    await settle();
    return { reads: p.reads, errors: p.errors };
  },

  // An Undo pressed on its toast: the focus goes to the card it is of, unless something has it.
  async undoFocus() {
    const go = async (before) => {
      const p = load();
      p.serve(ROWS);
      press(p, 'id:m2', 'keep', 'forever');
      await settle();
      const toast = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-toast is-leaving' }, ['button', {}, 'Undo']]));
      toast.querySelector('button').focus();
      if (before) before(p);
      p.toasts[0].action.onClick();
      await settle();
      const a = p.doc.activeElement;
      return a === more(p, 'id:m2') ? 'card' : a.id || a.textContent;
    };
    return {
      plain: await go(),
      typing: await go((p) => p.doc.getElementById('watch-search').focus()),
      details: await go((p) => p.doc.documentElement.classList.add('has-ttl')),
      dialog: await go((p) => p.doc.body.appendChild(build(p.doc, ['div', { class: 'wl-dialog active' }]))),
    };
  },

  // What is scheduled changed somewhere else: every card that stands here follows, in place.
  async scheduledChanged() {
    const p = load();
    more(p, 'id:m1').focus();
    const card = p.row('id:m1');
    p.serve(ROWS.map((r) => ({ ...r, sub: 'now ' + r.id })), { fact: 'fresh' });
    p.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    await settle();
    const q = load({ closed: true });
    q.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    await settle();
    return { subs: p.rowKeys().map((k) => p.row(k).querySelector('.watch-card__sub').textContent), same: p.row('id:m1') === card,
      focus: p.doc.activeElement === more(p, 'id:m1'), fact: p.bar.fact, closed: q.reads.length };
  },

  // A mark of one's own: the counts follow, the cards are left to what draws the mark.
  async marked() {
    const p = load();
    p.serve(ROWS.map((r) => ({ ...r, sub: 'other' })), { fact: 'counted' });
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'movie-1' } });
    await settle();
    return { fact: p.bar.fact, sub: p.row('id:m1').querySelector('.watch-card__sub').textContent };
  },

  // What the menu offers each card, and a card with nothing of an admin's.
  async menus() {
    const p = load({ rows: [...ROWS, { id: 's9', title: 'Kept Show', showRk: '99', kept: true }] });
    const of = (key) => {
      const node = p.doc.createElement('div');
      p.ctx.MMWatch.library.menu(p.row(key), node, (act, _icon, label) => build(p.doc, ['button', { 'data-act': act }, label]));
      return node.children.map((n) => n.textContent);
    };
    const out = { film: of('id:m1'), keptFilm: of('id:m3'), show: of('show:77'), keptShow: of('show:99') };
    p.row('id:m2').removeAttribute('data-admin');
    out.none = of('id:m2');
    const node = p.doc.createElement('div');
    const asked = p.ctx.MMWatch.library.durations(p.row('id:m1'), node, (act, _icon, label) => build(p.doc, ['button', { 'data-act': act }, label]));
    out.lengths = [asked, node.children.map((n) => [n.getAttribute('data-keep-dur'), n.getAttribute('data-act')])];
    out.notThere = p.ctx.MMWatch.library.durations(p.row('id:m2'), node, () => null);
    return out;
  },

  // Remove keep of a kept show opens its dialog, which clears every keep of it; of a film it removes the one.
  async removeKeepOfAShow() {
    const p = load({ rows: [{ id: 's9', title: 'Kept Show', showRk: '99', kept: true }] });
    p.replies['get /api/show/99/seasons'] = () => Promise.resolve({ show_title: 'Kept Show', show_kept: null, seasons: seasonsOf({ keep: FOREVER }) });
    press(p, 'show:99', 'unkeep');
    await settle();
    return { dialog: p.doc.getElementById('keep-dialog-overlay').classList.contains('active'), posts: p.calls.filter((c) => c[0] !== 'get').length };
  },

  // A title Watch cannot open: no link, and a menu all the same.
  async unheldCard() {
    const p = load();
    p.serve([{ id: 'x1', title: 'Forgotten Reel', unheld: true }]);
    const { cards: [card] } = await p.source.fetchPage('');
    return { href: card.querySelector('.watch-card__link').getAttribute('href'), more: !!card.querySelector('[data-watch-more]'), key: card.getAttribute('data-key') };
  },

  // What an empty grid says comes from the server, whichever way it was filled.
  async emptyFilters() {
    const p = load();
    p.serve([], { type: 'kept', empty: { title: 'Nothing is kept', filter: '' } });
    const chosen = (await p.source.sort.choose('added', 'kept')).empty;
    await p.source.fetchPage('');
    return { chosen, loaded: p.source.empty.title };
  },

  // Going to it from Storage: nothing changes until it is in; slow, it is gone to without; failed, it is not gone to.
  async going() {
    const go = async (act, already) => {
      const timers = [];
      const p = load({ timers, closed: !already });
      const wait = deferred();
      p.serve(ROWS, {}, wait);
      const told = [];
      const off = p.ctx.MMWatch.library.open('/watch?all=library&type=movies&sort=size', { leave: () => told.push('leave'), arrive: () => told.push('arrive'), fail: () => told.push('fail') });
      const before = told.length + p.opened.length;
      await act({ p, wait, off, slow: () => timers.splice(0).forEach((fn) => fn()) });
      await settle();
      return { before, told, opened: p.opened, read: p.reads };
    };
    return {
      whenIn: await go(({ wait }) => wait.resolve()),
      slow: await go(async ({ wait, slow }) => { slow(); await settle(); wait.resolve(); }),
      fails: await go(({ wait }) => wait.reject(new Error('no'))),
      slowThenFails: await go(async ({ wait, slow }) => { slow(); await settle(); wait.reject(new Error('no')); }),
      calledOff: await go(({ wait, off, slow }) => { off(); slow(); wait.resolve(); }),
      fromItself: await go(({ wait }) => wait.resolve(), true),
    };
  },

  async shown() {
    const p = load({ closed: true });
    p.ctx.MMWatch.library.show('/watch?all=library&type=tv&sort=size');
    return p.opened;
  },
};

const name = process.argv[2];
scenarios[name]().then((out) => {
  process.stdout.write(JSON.stringify(out === undefined ? null : out));
}, (err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
