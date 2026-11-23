// Runs watch/core.js, watch/admin.js and watch/list.js against the fake DOM and prints the outcome
// of one scenario as JSON. Usage: node watch_list_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, build, settle } = require('./fake_dom');

/* Bubble a click from *target* up to the document; stopPropagation ends it there. */
function click(doc, target) {
  const e = {
    target, button: 0, defaultPrevented: false, stopped: false,
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
Object.defineProperty(Node.prototype, 'firstElementChild', { get() { return this.children[0] || null; } });

function deferred() {
  let resolve; let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/* ── The list fragment, as /watch/list renders it ── */

function row(r) {
  const key = r.showRk ? 'show:' + r.showRk : 'id:' + r.id;
  const wrapper = ['div', {
    class: 'keep-wrapper', 'data-id': r.id, 'data-tv': String(!!r.showRk), 'data-show-rk': r.showRk || '',
    'data-has-rk': String(!!r.showRk), 'data-show-title': r.title, 'data-title': JSON.stringify(r.title),
    'data-kept': String(!!r.kept), 'data-deletion': String(!!r.deletion),
  },
  ['button', { class: 'keep-trigger' + (r.kept ? ' keep-trigger--kept' : '') }, r.kept ? 'Kept' : 'Keep'],
  ['div', { class: 'snooze-dropdown' },
    ['button', { class: 'snooze-option', 'data-keep-dur': '30 days' }, '30 days'],
    ['button', { class: 'snooze-option', 'data-keep-dur': 'forever' }, 'Forever'],
    ...(r.kept ? [['button', { class: 'snooze-option', 'data-remove-keep-id': JSON.stringify(r.id) }, 'Remove keep']] : [])]];
  const del = r.kept ? [] : r.showRk
    ? [['button', { class: 'btn btn--danger btn--sm', 'data-action': 'pick-season-delete', 'data-show-rk': r.showRk, 'data-title': JSON.stringify(r.title) }, 'Delete']]
    : [['button', { class: 'btn btn--danger btn--sm', 'data-action': 'confirm-delete', 'data-id': r.id, 'data-title': JSON.stringify(r.title), 'data-size': '22.1 GB', 'data-kind': 'movie' }, 'Delete']];
  return ['tr', { class: 'wl-row' + (r.kept ? ' wl-row--kept' : ''), 'data-row-key': key, 'data-id': r.id },
    ['td', { class: 'title-cell' }, r.title],
    ['td', { class: 'wl-act' }, ['div', { class: 'wl-actions' }, wrapper, ...del]]];
}

const ROWS = [
  { id: 'm1', title: 'Copperline', deletion: true },
  { id: 'm2', title: 'Saltmarsh' },
  { id: 's1', title: 'Night Shift', showRk: '77' },
  { id: 'm3', title: 'Glass Harbour', kept: true },
];

function fragment(doc, state, rows) {
  const s = Object.assign({ q: '', type: '', sort: 'added_desc', page: '1', per_page: '20' }, state);
  return build(doc, ['div', { class: 'wl', 'data-list-root': '', 'data-q': s.q, 'data-type': s.type, 'data-sort': s.sort, 'data-page': s.page, 'data-per-page': s.per_page },
    ['div', { 'data-list-part': 'stats' }, 'stats ' + (s.label || '')],
    ['p', { 'data-list-part': 'summary' }, 'summary ' + (s.label || '')],
    ['form', { 'data-list-part': 'toolbar' },
      ['a', { class: 'fpill on', href: '/watch?view=list', 'data-list-nav': '', 'data-type': '' }, 'All'],
      ['a', { class: 'fpill', href: '/watch?view=list&type=kept', 'data-list-nav': '', 'data-type': 'kept' }, 'Kept'],
      ['select', { id: 'watch-list-sort', 'data-list-sort': '' }]],
    ['div', { 'data-list-part': 'results' },
      ['table', {}, ['tbody', {}, ...rows.map(row)]],
      ['nav', { class: 'pagination' },
        ['a', { class: 'page-btn', href: '/watch?view=list&page=2', 'data-list-nav': '' }, '2'],
        ['span', { class: 'page-info' }, 'range ' + (s.label || '')]]]]);
}

function page(doc, listShown) {
  return build(doc, ['div', { class: 'watch-page', id: 'watch-page' },
    ['div', { id: 'watch-top' },
      ['input', { id: 'watch-search', placeholder: 'Search films and shows' }],
      ['button', { id: 'watch-search-clear' }],
      ['nav', { id: 'watch-viewtoggle' },
        ['a', { href: '/watch', 'data-view': 'rows', 'aria-current': 'page' }, 'Browse'],
        ['a', { href: '/watch?view=list', 'data-view': 'list' }, 'List']]],
    ['div', { id: 'watch-browse' }, 'rows'],
    ['section', { id: 'watch-results' }],
    ['section', { id: 'watch-list' }]]);
}

/* The page with the scripts loaded. opts.list: the list already rendered in place. */
function load(opts = {}) {
  const doc = makeDocument();
  doc.dispatchEvent = (ev) => { (doc.listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; };
  doc.body.appendChild(page(doc));
  const list = doc.getElementById('watch-list');
  const browse = doc.getElementById('watch-browse');
  doc.getElementById('watch-results').hidden = true;
  if (opts.list) {
    list.appendChild(fragment(doc, {}, opts.rows || ROWS));
    browse.hidden = true;
  } else {
    list.hidden = true;
  }

  const calls = [];
  const toasts = [];
  const errors = [];
  const confirms = [];
  const fetches = [];
  const history = [];
  const events = [];
  const winListeners = {};
  const replies = {}; // url -> function returning a promise
  const pages = {}; // fetch url -> { state, rows } or a deferred
  let confirmAnswer = true;

  const api = (method) => (url, body) => {
    calls.push([method, url, body === undefined ? null : body]);
    const reply = replies[method + ' ' + url];
    return reply ? reply() : Promise.resolve({ ok: true });
  };

  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, WeakMap, Error, Proxy,
    URLSearchParams, parseInt, parseFloat, encodeURIComponent,
    setTimeout: (fn) => { fn(); return 1; },
    clearTimeout: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
    matchMedia: () => ({ matches: true }),
    innerHeight: 900,
    innerWidth: 1280,
    scrollBy() {},
    location: { pathname: '/watch', search: opts.list ? '?view=list' : '', assign(u) { history.push(['assign', u]); } },
    history: {
      pushState: (_s, _t, url) => history.push(['push', url]),
      replaceState: (_s, _t, url) => history.push(['replace', url]),
    },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; this.bubbles = !!(init && init.bubbles); } },
    DOMParser: class { parseFromString(token) { const d = makeDocument(); d.body.appendChild(pages[token].node(d)); return d; } },
    fetch: (url) => {
      fetches.push(url);
      const entry = pages[url] || pages['*'];
      const answer = () => Promise.resolve({ ok: true, redirected: false, text: () => Promise.resolve(entry === pages[url] ? url : '*') });
      return entry && entry.wait ? entry.wait.promise.then(answer) : answer();
    },
    UIFeedback: {
      toast: (message, o) => toasts.push({ message, undo: !!(o && o.action), action: o && o.action }),
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
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['watch/core.js', 'watch/admin.js', 'watch/list.js']
    .forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));
  // The search field listens on window, so the event must bubble up to it.
  doc.addEventListener('mm:watch-view', (e) => events.push(['view', e.detail.view, e.bubbles]));

  return {
    doc, ctx, calls, toasts, errors, confirms, fetches, history, events, replies,
    /* What GET /watch/list answers for *url*: a list for *state* with *rows*. */
    serve(url, state, rows, wait) { pages[url] = { node: (d) => fragment(d, state, rows), wait }; },
    answerConfirm(v) { confirmAnswer = v; },
    popstate(search) { ctx.location.search = search; (winListeners.popstate || []).forEach((fn) => fn({})); },
    rowKeys() { return doc.querySelectorAll('#watch-list tr[data-row-key]').map((r) => r.getAttribute('data-row-key')); },
    row(key) { return doc.querySelectorAll('#watch-list tr[data-row-key]').find((r) => r.getAttribute('data-row-key') === key) || null; },
    part(name) { return doc.querySelector('#watch-list [data-list-part="' + name + '"]').textContent; },
  };
}

function keepOption(p, key, dur) {
  return p.row(key).querySelectorAll('[data-keep-dur]').find((b) => b.getAttribute('data-keep-dur') === dur);
}

const scenarios = {
  // Keep a movie that was due for deletion: the Watch keep, so Undo restores the date.
  async keepScheduled() {
    const p = load({ list: true });
    const kept = ROWS.map((r) => (r.id === 'm1' ? Object.assign({}, r, { kept: true }) : r));
    p.serve('/watch/list', { label: 'after' }, kept);
    click(p.doc, keepOption(p, 'id:m1', '30 days'));
    await settle();
    const toast = p.toasts[0];
    const pill = p.row('id:m1').querySelector('.keep-trigger');
    const out = {
      call: p.calls[0],
      toast: toast.message,
      undo: toast.undo,
      pill: pill.className,
      summary: p.part('summary'),
      history: p.history,
    };
    toast.action.onClick();
    await settle();
    out.undoCall = p.calls[1];
    out.afterUndo = p.toasts[1].message;
    return out;
  },

  // Keep a movie with no deletion and no keep: Undo removes the keep again.
  async keepUnscheduled() {
    const p = load({ list: true });
    p.serve('/watch/list', {}, ROWS);
    click(p.doc, keepOption(p, 'id:m2', 'forever'));
    await settle();
    p.toasts[0].action.onClick();
    await settle();
    return { calls: p.calls, toast: p.toasts[0].message };
  },

  // Change the keep on a kept movie: there is nothing to undo to.
  async keepAgain() {
    const p = load({ list: true });
    p.serve('/watch/list', {}, ROWS);
    click(p.doc, keepOption(p, 'id:m3', '30 days'));
    await settle();
    return { call: p.calls[0], undo: p.toasts[0].undo };
  },

  // Two quick presses on one keep send one request.
  async keepTwice() {
    const p = load({ list: true });
    const reply = deferred();
    p.replies['post /api/watch/keep'] = () => reply.promise;
    p.serve('/watch/list', {}, ROWS);
    click(p.doc, keepOption(p, 'id:m1', '30 days'));
    click(p.doc, keepOption(p, 'id:m1', '30 days'));
    reply.resolve({ ok: true });
    await settle();
    return { calls: p.calls.length };
  },

  // A keep that fails says so and leaves the row as it was.
  async keepFails() {
    const p = load({ list: true });
    p.replies['post /api/watch/keep'] = () => Promise.reject(new Error('Too many keep operations'));
    click(p.doc, keepOption(p, 'id:m1', '30 days'));
    await settle();
    return { errors: p.errors, fetches: p.fetches, toasts: p.toasts.length };
  },

  // Remove the keep on a kept movie: the row turns back to Keep in place.
  async removeKeep() {
    const p = load({ list: true });
    const unkept = ROWS.map((r) => (r.id === 'm3' ? Object.assign({}, r, { kept: false }) : r));
    p.serve('/watch/list', {}, unkept);
    click(p.doc, p.row('id:m3').querySelector('[data-remove-keep-id]'));
    await settle();
    return {
      call: p.calls[0],
      toast: p.toasts[0].message,
      kept: p.row('id:m3').classList.contains('wl-row--kept'),
      keys: p.rowKeys(),
    };
  },

  // Delete a movie: confirm, busy row, then the row leaves and focus moves on.
  async deleteMovie() {
    const p = load({ list: true });
    const reply = deferred();
    p.replies['post /api/media/m1/delete'] = () => reply.promise;
    p.serve('/watch/list', { label: 'after' }, ROWS.slice(1));
    const row = p.row('id:m1');
    const del = row.querySelector('[data-action="confirm-delete"]');
    del.focus();
    click(p.doc, del);
    await settle();
    const busy = {
      row: row.classList.contains('is-busy'),
      label: del.textContent,
      spinner: !!del.querySelector('.btn-spinner'),
      disabled: del.disabled === true,
    };
    reply.resolve({ ok: true });
    await settle();
    const next = p.row('id:m2').querySelector('.keep-trigger');
    return {
      confirm: p.confirms[0],
      busy,
      keys: p.rowKeys(),
      toast: p.toasts[0].message,
      focus: p.doc.activeElement === next,
      range: p.doc.querySelector('#watch-list .page-info').textContent,
      history: p.history,
    };
  },

  // A delete the server refuses puts the row and its button back.
  async deleteRefused() {
    const p = load({ list: true });
    p.replies['post /api/media/m1/delete'] = () => Promise.reject(new Error('This item is kept'));
    const row = p.row('id:m1');
    const del = row.querySelector('[data-action="confirm-delete"]');
    click(p.doc, del);
    await settle();
    return {
      busy: row.classList.contains('is-busy'),
      label: del.textContent,
      disabled: !!del.disabled,
      keys: p.rowKeys(),
      errors: p.errors,
    };
  },

  // Cancel in the confirm sends nothing.
  async deleteCancelled() {
    const p = load({ list: true });
    p.answerConfirm(false);
    click(p.doc, p.row('id:m1').querySelector('[data-action="confirm-delete"]'));
    await settle();
    return { calls: p.calls, busy: p.row('id:m1').classList.contains('is-busy') };
  },

  // Delete one season from a show's season dialog: only that show's row is read again.
  async seasonDelete() {
    const p = load({ list: true });
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({
      show_title: 'Night Shift', show_kept: false,
      seasons: [{ id: 's1', season_number: 1, file_size: '9 GB', kept: false }, { id: 's2', season_number: 2, kept: true }],
    });
    const fewer = ROWS.map((r) => (r.id === 's1' ? Object.assign({}, r, { title: 'Night Shift (1 season)' }) : r));
    p.serve('/watch/list', {}, fewer);
    click(p.doc, p.row('show:77').querySelector('[data-action="pick-season-delete"]'));
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
      title: p.row('show:77').querySelector('.title-cell').textContent,
      keys: p.rowKeys(),
    };
  },

  // Keep a show from its dialog: the dialog closes and the show's row changes in place.
  async showKeep() {
    const p = load({ list: true });
    p.replies['get /api/show/77/seasons'] = () => Promise.resolve({
      show_title: 'Night Shift', show_kept: false,
      seasons: [{ id: 's1', season_number: 1, kept: false }],
    });
    const kept = ROWS.map((r) => (r.id === 's1' ? Object.assign({}, r, { kept: true }) : r));
    p.serve('/watch/list', {}, kept);
    click(p.doc, keepOption(p, 'show:77', '30 days'));
    await settle();
    const dialog = p.doc.getElementById('keep-dialog-overlay');
    const opened = dialog.classList.contains('active');
    click(p.doc, p.doc.getElementById('keep-dialog-confirm'));
    await settle();
    return {
      opened,
      call: p.calls.find((c) => c[0] === 'post'),
      closed: !dialog.classList.contains('active'),
      kept: p.row('show:77').classList.contains('wl-row--kept'),
      history: p.history,
    };
  },

  // The detail calls the same actions with a plain object, list or no list.
  async detailCalls() {
    const p = load({ list: false });
    const admin = p.ctx.MMWatch.admin;
    await admin.keep({ id: '42', is_tv: false, show_rk: '', has_rk: false, show_title: 'Dune' }, '7 days');
    const deleted = await admin.confirmDelete('42', 'Dune');
    return { calls: p.calls, toasts: p.toasts.map((t) => t.message), deleted, fetches: p.fetches, keys: Object.keys(admin).sort() };
  },

  // Browse to List: fetched, swapped in place, the address and the switch follow; Back returns.
  async switchView() {
    const p = load({ list: false });
    p.doc.getElementById('watch-search').value = 'harbour';
    p.serve('/watch/list?q=harbour', { q: 'harbour' }, ROWS);
    const listLink = p.doc.querySelector('#watch-viewtoggle [data-view="list"]');
    const e = { target: listLink, button: 0, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    (p.doc.listeners.click || []).forEach((fn) => fn(e));
    await settle();
    const list = p.doc.getElementById('watch-list');
    const out = {
      prevented: e.defaultPrevented,
      fetches: p.fetches,
      history: p.history.slice(),
      listShown: !list.hidden,
      browseHidden: p.doc.getElementById('watch-browse').hidden === true,
      current: listLink.getAttribute('aria-current'),
      placeholder: p.doc.getElementById('watch-search').getAttribute('placeholder'),
      rows: p.rowKeys().length,
      events: p.events.slice(),
    };
    p.popstate('');
    out.back = { listHidden: list.hidden === true, browseShown: !p.doc.getElementById('watch-browse').hidden, events: p.events.slice(1) };
    p.popstate('?view=list&q=harbour');
    out.forward = { listShown: !list.hidden, fetches: p.fetches.length };
    return out;
  },

  // A modified click on the switch is left to the browser (new tab).
  async switchModified() {
    const p = load({ list: false });
    const listLink = p.doc.querySelector('#watch-viewtoggle [data-view="list"]');
    const e = { target: listLink, button: 0, metaKey: true, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    (p.doc.listeners.click || []).forEach((fn) => fn(e));
    await settle();
    return { prevented: e.defaultPrevented, fetches: p.fetches };
  },

  // The search field's event filters the list; an older reply that lands last is dropped.
  async search() {
    const p = load({ list: true });
    const slow = deferred();
    p.serve('/watch/list?q=har', { q: 'har', label: 'har' }, ROWS.slice(0, 1), slow);
    p.serve('/watch/list?q=harbour', { q: 'harbour', label: 'harbour' }, ROWS.slice(1, 2));
    const fire = (q) => p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-list-search', { detail: { q } }));
    fire('har');
    fire('harbour');
    await settle();
    slow.resolve();
    await settle();
    return { fetches: p.fetches, history: p.history, keys: p.rowKeys(), summary: p.part('summary') };
  },

  // A pill, a page link and Clear search and filters all change the list in place.
  async navigate() {
    const p = load({ list: true });
    p.serve('/watch/list?type=kept', { type: 'kept' }, ROWS.slice(3));
    p.serve('/watch/list?page=2', { page: '2' }, ROWS.slice(2));
    p.serve('/watch/list', {}, ROWS);
    const kept = p.doc.querySelectorAll('#watch-list .fpill')[1];
    kept.focus();
    click(p.doc, kept);
    await settle();
    const out = { afterPill: p.rowKeys(), focus: p.doc.activeElement.getAttribute('data-type') };
    click(p.doc, p.doc.querySelector('#watch-list .page-btn'));
    await settle();
    out.afterPage = p.rowKeys();
    p.doc.getElementById('watch-search').value = 'lantern';
    const clear = build(p.doc, ['a', { href: '/watch?view=list', 'data-list-clear': '' }, 'Clear']);
    p.doc.querySelector('#watch-list [data-list-part="results"]').appendChild(clear);
    click(p.doc, clear);
    await settle();
    out.cleared = { q: p.doc.getElementById('watch-search').value, clearHidden: p.doc.getElementById('watch-search-clear').hidden === true };
    out.fetches = p.fetches;
    out.history = p.history;
    return out;
  },

  // The sort select reorders in place, from page 1.
  async sort() {
    const p = load({ list: true });
    p.serve('/watch/list?sort=deletion_asc', { sort: 'deletion_asc' }, ROWS);
    p.doc.querySelector('[data-list-root]').setAttribute('data-page', '2');
    const select = p.doc.getElementById('watch-list-sort');
    select.value = 'deletion_asc';
    (p.doc.listeners.change || []).forEach((fn) => fn({ target: select }));
    await settle();
    return { fetches: p.fetches, history: p.history, focus: p.doc.activeElement.id };
  },
};

const name = process.argv[2];
scenarios[name]().then((out) => {
  process.stdout.write(JSON.stringify(out === undefined ? null : out));
}, (err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
