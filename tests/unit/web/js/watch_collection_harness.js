// Runs watch/collection.js and the real modal against the fake DOM, with a stand-in for the cards
// of watch/browse.js, and prints one scenario's outcome as JSON.
// Usage: node watch_collection_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.scrollTo = function () {};
Node.prototype.focus = function () { if (!this.hidden) this.ownerDocument.activeElement = this; };

class APIError extends Error {
  constructor(status, message) { super(message); this.status = status; this.data = { message }; }
}

function film(n, over) {
  const why = (over && over.why_not) || null;
  return Object.assign({ ref: 'tmdb-movie-' + n, kind: 'movie', title: 'Film ' + n, year: 2000 + n, poster_url: null,
    in_library: why === 'held', library_ref: why === 'held' ? 'movie-' + (40 + n) : null, download_state: null, download_pct: null,
    release_date: (2000 + n) + '-06-14', here: n === 2, can_request: !why, why_not: why }, over);
}

const SAGA = () => ({ name: 'The Saga', films: [film(1, { why_not: 'held' }), film(2), film(3), film(4), film(5, { why_not: 'requested', download_state: 'queued' }),
  film(6, { why_not: 'unreleased', release_date: '2027-07-12' }), film(7, { why_not: 'undated', release_date: null, year: null })] });

function load(reply) {
  const doc = makeDocument();
  const clock = { now: Date.UTC(2026, 10, 24, 12) };
  const gets = [];
  const posts = [];
  const events = [];
  const toasts = [];
  const opened = [];
  doc.body.appendChild(build(doc, ['div', { id: 'ttl' }, ['div', { class: 'ttl__sheet' }, ['h2', { id: 'ttl-title', tabindex: '-1' }], ['div', { id: 'body' }, ['p', { id: 'before' }]]]]));
  doc.dispatchEvent = (e) => { events.push(e.detail); (doc.listeners[e.type] || []).slice().forEach((fn) => fn(e)); };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise,
    Date: class extends Date { static now() { return clock.now; } },
    setTimeout: (fn, ms) => { if (!ms) fn(); return 1; },
    clearTimeout() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', visibility: 'visible' }),
    matchMedia: () => ({ matches: true }), // reduced motion
    addEventListener() {},
    innerHeight: 900,
    UIFeedback: { toast: (message, o) => toasts.push([message, (o && o.variant) || null]) },
    MM: { api: {
      get: (url) => { gets.push(url); const r = typeof reply === 'function' ? reply(url) : reply; return r instanceof Error ? Promise.reject(r) : Promise.resolve(r).then((got) => JSON.parse(JSON.stringify(got))); },
      post: (url, body) => new Promise((resolve, reject) => posts.push({ url, body, resolve, reject })),
      messageOf: (e, f) => (e && e.data && e.data.message) || f,
    } },
    MMWatch: {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'modal-a11y.js', 'core/modal.js', 'watch/core.js'].forEach(run);
  ctx.MMWatch.browse = { card: (f) => build(doc, ['article', { class: 'watch-card' },
    ['a', { class: 'watch-card__link', href: f.href || '#' }, ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, f.title]]],
    ['button', { class: 'watch-more' }]]) };
  ctx.MMWatch.detail = { open: (ref, o) => opened.push([ref, o && o.title]) };
  run('watch/collection.js');
  const body = doc.getElementById('body');
  const into = async (ref, kind) => { ctx.MMWatch.collection.into(body, ref, kind || 'movie'); await settle(); };
  const redraw = async (ref) => { body.textContent = ''; await into(ref); };
  const q = (sel) => doc.querySelector(sel);
  const cards = () => doc.querySelectorAll('.coll .watch-card');
  const get = () => { const b = q('.coll__get'); return b && !b.hidden ? b.textContent : null; };
  const isOpen = () => !!q('.coll-dialog') && q('.coll-dialog').classList.contains('active');
  const press = async (node, e) => { click(doc, node, e); await settle(); };
  const go = () => q('.coll-dialog .btn--primary');
  const rows = () => doc.querySelectorAll('.coll-item').map((r) => r.querySelector('.coll-item__name').textContent + ': ' + r.querySelector('.coll-item__sub').textContent);
  const answer = async (i, r) => { if (r instanceof Error) posts[i].reject(r); else posts[i].resolve(r || { ok: true }); await settle(); };
  const tick = async (i, on) => {
    const box = doc.querySelectorAll('.coll-item input')[i];
    box.checked = on;
    box.listeners.change.forEach((fn) => fn({}));
    await settle();
  };
  return { ctx, doc, clock, gets, posts, events, toasts, opened, body, into, redraw, q, cards, get, isOpen, press, go, rows, answer, tick };
}

const ids = (p) => p.posts.map((x) => x.body.tmdb_id);
const titles = (p) => p.cards().map((c) => c.querySelector('.watch-card__title').textContent);

async function asked(reply) {
  const p = load(reply || SAGA());
  await p.into('tmdb-movie-2');
  await p.press(p.q('.coll__get'));
  return p;
}

const scenarios = {
  async row() {
    const p = load(SAGA());
    p.ctx.MMWatch.collection.into(p.body, 'tmdb-movie-2', 'movie');
    const waiting = p.q('.ttl__collsec').hidden;
    await settle();
    const here = p.cards().filter((c) => c.classList.contains('is-here'));
    return { waiting, gets: p.gets, titles: titles(p), last: p.body.children[p.body.children.length - 1].className.split(' ')[0],
      here: here.map((c) => c.querySelector('.watch-card__title').textContent), current: here[0].querySelector('a').getAttribute('aria-current'),
      name: p.q('.coll__name').textContent, stops: p.cards().map((c) => c.querySelector('a').tabIndex), get: p.get() };
  },

  async nothing() {
    const out = {};
    for (const [name, r] of [['none', null], ['alone', { name: 'X', films: [film(2)] }], ['failed', new APIError(429, 'too_many_requests')]]) {
      const p = load(r);
      await p.into('tmdb-movie-2');
      out[name] = !!p.q('.coll') || !p.q('.ttl__collsec').hidden;
    }
    const show = load(SAGA());
    await show.into('show-3', 'show');
    out.show = show.gets.length;
    return out;
  },

  async open() {
    const p = load(SAGA());
    await p.into('tmdb-movie-2');
    const link = (i) => p.cards()[i].querySelector('a');
    await p.press(link(2));
    const focus = p.doc.activeElement.getAttribute('data-ttl-act');
    await p.press(link(0));
    await p.press(link(1)); // the one on show
    const top = p.doc.activeElement.id;
    await p.press(link(3), { metaKey: true });
    // The next film of it shows the row at once; Back, its focus is found by name.
    p.body.textContent = '';
    p.ctx.MMWatch.collection.into(p.body, 'tmdb-movie-3', 'movie');
    const atOnce = p.cards().filter((c) => c.classList.contains('is-here')).map((c) => c.querySelector('.watch-card__title').textContent);
    await settle();
    p.body.textContent = '';
    p.ctx.MMWatch.collection.into(p.body, 'tmdb-movie-2', 'movie');
    return { opened: p.opened, focus, top, atOnce, back: !!p.q('[data-ttl-act="film-tmdb-movie-3"]') };
  },

  async keys() {
    const p = load(SAGA());
    await p.into('tmdb-movie-2');
    const row = p.q('.coll__row');
    const key = (k) => row.listeners.keydown.forEach((fn) => fn({ key: k, target: p.doc.activeElement, preventDefault() {} }));
    const at = () => p.doc.activeElement.getAttribute('data-ttl-act');
    p.cards()[1].querySelector('a').focus();
    return ['ArrowRight', 'End', 'ArrowRight', 'Home', 'ArrowLeft'].map((k) => { key(k); return at(); });
  },

  // Drawn again for a change of its own: nothing of the row is new.
  async drawnAgain() {
    const p = load(SAGA());
    await p.into('tmdb-movie-2');
    const before = p.cards();
    await p.redraw('tmdb-movie-2');
    const after = p.cards();
    const gets = p.gets.length;
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: { ref: 'movie-41' } });
    await p.redraw('tmdb-movie-2');
    return { same: before.every((c, i) => c === after[i]), gets, then: p.gets.length };
  },

  async noneMissing() {
    const p = load({ name: 'X', films: [film(1, { why_not: 'held' }), film(2, { why_not: 'unavailable' })] });
    await p.into('tmdb-movie-2');
    return { row: !!p.q('.coll'), get: p.get() };
  },

  async confirm() {
    const p = await asked();
    const out = { open: p.isOpen(), rows: p.rows(), boxes: p.doc.querySelectorAll('.coll-item input').map((b) => b.checked),
      out: p.doc.querySelectorAll('.coll-out p').map((n) => n.textContent), go: p.go().textContent, posts: p.posts.length };
    await p.tick(1, false);
    out.fewer = p.go().textContent;
    await p.tick(0, false);
    await p.tick(2, false);
    await p.press(p.go());
    out.empty = [p.go().getAttribute('aria-disabled'), p.posts.length, p.isOpen()];
    return out;
  },

  async oneByOne() {
    const p = await asked();
    await p.tick(1, false);
    await p.press(p.go());
    await p.press(p.go());
    const first = { ids: ids(p), rows: p.rows(), go: p.go().textContent, disabled: p.go().getAttribute('aria-disabled') };
    await p.answer(0);
    const second = ids(p);
    await p.answer(1);
    return { first, second, sent: p.posts.map((x) => [x.url, x.body]), rows: p.rows(), events: p.events, said: p.q('.coll-dialog__said').textContent, left: p.get() };
  },

  async partial() {
    const p = await asked();
    await p.press(p.go());
    await p.answer(0, new APIError(409, "'Film 2' is already in Radarr"));
    await p.answer(1, new APIError(502, 'Failed to add to Radarr'));
    await p.answer(2);
    const out = { rows: p.rows(), said: p.q('.coll-dialog__said').textContent, go: p.go().textContent, left: p.get() };
    await p.press(p.go());
    out.again = ids(p);
    await p.answer(3);
    out.then = [p.go().textContent, p.get()];
    await p.press(p.go());
    out.closed = !p.isOpen();
    out.focus = p.doc.activeElement.id;
    return out;
  },

  async limited() {
    const p = await asked();
    await p.press(p.go());
    await p.answer(0);
    await p.answer(1, new APIError(429, 'Too many download requests — slow down'));
    const out = { ids: ids(p), rows: p.rows() };
    await p.press(p.go());
    out.again = ids(p);
    return out;
  },

  // A repeat of one film's request is that film's trouble alone.
  async duplicate() {
    const p = await asked();
    await p.press(p.go());
    await p.answer(0, new APIError(429, 'Duplicate request — wait a moment before retrying'));
    return { ids: ids(p), row: p.rows()[0] };
  },

  async closed() {
    const p = await asked();
    await p.press(p.go());
    await p.press(p.q('.coll-dialog .btn--ghost'));
    const open = p.isOpen();
    const running = p.get();
    await p.answer(0);
    const second = ids(p);
    await p.press(p.q('.coll__get'));
    const reopened = p.rows();
    await p.press(p.go()); // it runs: pressing again adds nothing
    await p.press(p.q('.coll-dialog .btn--ghost'));
    await p.answer(1);
    await p.answer(2);
    return { open, running, second, reopened, ids: ids(p), toasts: p.toasts, get: p.get() };
  },

  // Radarr takes a moment to list what was added.
  async grace() {
    const p = await asked();
    await p.press(p.go());
    await p.answer(0); await p.answer(1); await p.answer(2);
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: {} });
    await p.redraw('tmdb-movie-2');
    const soon = p.get();
    p.clock.now += 120000;
    await p.redraw('tmdb-movie-2');
    return { soon, later: p.get() };
  },

  // One of them downloaded from its own page.
  async elsewhere() {
    const p = load(SAGA());
    await p.into('rec-9');
    p.doc.dispatchEvent({ type: 'mm:title-changed', detail: { ref: 'rec-9', download_state: 'queued', download_pct: null, title: 'Film 2', kind: 'movie', year: 2002 } });
    return { get: p.get() };
  },

  // The title on show goes by another ref than its card.
  async pick() {
    const p = load(SAGA());
    await p.into('rec-9');
    await p.press(p.q('.coll__get'));
    await p.press(p.go());
    await p.answer(0);
    return { refs: p.events.map((e) => e.ref) };
  },
};

scenarios[process.argv[2]]().then((out) => { process.stdout.write(JSON.stringify(out)); }, (err) => { console.error(err); process.exit(1); });
