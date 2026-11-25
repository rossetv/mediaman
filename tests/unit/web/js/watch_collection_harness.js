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
  const win = {};
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
    addEventListener: (t, fn) => { (win[t] = win[t] || []).push(fn); },
    removeEventListener: (t, fn) => { win[t] = (win[t] || []).filter((f) => f !== fn); },
    innerHeight: 900,
    UIFeedback: { toast: (message, o) => toasts.push([message, (o && o.variant) || null]) },
    MM: { api: {
      hasUnsavedChanges: () => false,
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
    ['a', { class: 'watch-card__link', href: f.href || '#' }, ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, f.title], ...(f.download_state ? [['span', { class: 'watch-badge--state' }, f.download_state]] : [])]],
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
  const press = async (node, e) => { clock.now += 1000; click(doc, node, e); await settle(); };
  const go = () => q('.coll-dialog .btn--primary');
  const rows = () => doc.querySelectorAll('.coll-item').map((r) => r.querySelector('.coll-item__name').textContent + ': ' + r.querySelector('.coll-item__sub').textContent);
  const answer = async (i, r) => { if (r instanceof Error) posts[i].reject(r); else posts[i].resolve(r || { ok: true }); await settle(); };
  const tick = async (i, on) => {
    const box = doc.querySelectorAll('.coll-item input')[i];
    box.checked = on;
    box.listeners.change.forEach((fn) => fn({}));
    await settle();
  };
  /* Whether leaving the page would be asked about. */
  const warns = () => {
    const e = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    (win.beforeunload || []).forEach((fn) => fn(e));
    return e.defaultPrevented;
  };
  const told = (n, over) => doc.dispatchEvent({ type: 'mm:title-changed', detail: Object.assign({ ref: 'tmdb-movie-' + n,
    download_state: 'queued', download_pct: null, title: 'Film ' + n, kind: 'movie', year: 2000 + n }, over) });
  return { reply: (r) => { reply = r; }, win, warns, told, ctx, doc, clock, gets, posts, events, toasts, opened, body, into, redraw, q, cards, get, isOpen, press, go, rows, answer, tick };
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
    const red = p.doc.querySelectorAll('.coll-item').map((row) => row.classList.contains('is-failed'));
    const out = { ids: ids(p), rows: p.rows(), red, said: p.q('.coll-dialog__said').textContent };
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

  // Back closes the title with the dialog open, whose button has gone.
  async closedByBack() {
    const p = await asked();
    await p.press(p.go());
    await p.answer(0); await p.answer(1); await p.answer(2);
    const root = p.doc.getElementById('ttl');
    p.ctx.ModalA11y.closeFrom(root);
    root.classList.add('is-leaving');
    p.doc.activeElement = p.doc.body;
    await settle();
    return { open: p.isOpen(), kept: p.doc.activeElement === p.doc.body };
  },

  // The title is closed while films are added, then opened again.
  async reopened() {
    let reply = SAGA();
    const p = load(() => reply);
    await p.into('tmdb-movie-2');
    const first = p.cards();
    await p.press(p.q('.coll__get'));
    await p.press(p.go());
    p.body.textContent = '';
    await p.answer(0);
    await p.into('tmdb-movie-2');
    const fresh = p.cards();
    const badges = fresh.map((c) => !!c.querySelector('.watch-badge--state'));
    // Closed again while a reply is on its way, which lists fewer.
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: {} });
    reply = { name: 'The Saga', films: SAGA().films.slice(0, 3) };
    await p.redraw('tmdb-movie-2');
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: {} });
    reply = SAGA();
    p.body.textContent = '';
    p.ctx.MMWatch.collection.into(p.body, 'tmdb-movie-2', 'movie');
    p.body.textContent = '';
    await settle();
    p.ctx.MMWatch.collection.into(p.body, 'tmdb-movie-2', 'movie');
    await settle();
    return { drawn: fresh.every((c, i) => c !== first[i]), badges, count: p.cards().length };
  },

  async unload() {
    const p = await asked();
    await p.tick(2, false);
    const out = [p.warns()];
    await p.press(p.go());
    out.push(p.warns());
    const said = p.q('.coll-dialog__said').textContent;
    await p.answer(0);
    out.push(p.warns()); // the last one has been asked for
    await p.answer(1);
    out.push(p.warns());
    return { out, said, listening: (p.win.beforeunload || []).length };
  },

  async another() {
    const other = { name: 'Other', films: [film(12, { here: true }), film(13)] };
    const p = load((url) => (/movie-12/.test(url) ? other : SAGA()));
    await p.into('tmdb-movie-2');
    await p.press(p.q('.coll__get'));
    await p.press(p.go());
    await p.press(p.q('.coll-dialog .btn--ghost'));
    await p.redraw('tmdb-movie-12');
    const get = p.get();
    await p.press(p.q('.coll__get'));
    return { get, open: p.isOpen(), toast: p.toasts.length, ids: ids(p) };
  },

  async spinner() {
    const p = await asked();
    const look = (i) => { const row = p.doc.querySelectorAll('.coll-item')[i]; return [!!row.querySelector('.btn-spinner'), row.classList.contains('is-locked'), row.querySelector('input').disabled]; };
    await p.press(p.go());
    const adding = look(0);
    await p.answer(0, new APIError(502, 'Failed to add to Radarr'));
    const failed = look(0);
    await p.answer(1); await p.answer(2);
    return { adding, failed, after: look(0) };
  },

  // Two replies on their way: the older one comes last.
  async older() {
    const gives = [];
    const p = load(() => new Promise((r) => gives.push(r)));
    await p.into('tmdb-movie-2');
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: {} });
    await p.redraw('tmdb-movie-2');
    gives[1]({ name: 'New', films: SAGA().films });
    await settle();
    gives[0]({ name: 'Old', films: SAGA().films });
    await settle();
    return { name: p.q('.coll__name').textContent };
  },

  async doubleClick() {
    const p = load(SAGA());
    await p.into('tmdb-movie-2');
    const press = async (node, detail) => {
      // The fake DOM has no capture: the dialog's own listeners run in order, up to a stop.
      const e = { target: node, detail, button: 0, stopped: false, preventDefault() {}, stopPropagation() { this.stopped = true; } };
      const box = p.q('.coll-dialog');
      if (box && box.contains(node)) box.listeners.click[0](e);
      for (let a = node; a && !e.stopped; a = a.parentNode) for (const fn of (a.listeners.click || []).slice(a === box ? 1 : 0)) fn(e);
      await settle();
    };
    await press(p.q('.coll__get'), 1);
    await press(p.q('.coll-dialog'), 2); // the rest of it, on the backdrop
    const open = p.isOpen();
    await press(p.go(), 1);
    await p.answer(0); await p.answer(1); await p.answer(2);
    p.clock.now += 5000;
    await press(p.go(), 2); // Done stands where Download did
    const stays = p.isOpen();
    await press(p.go(), 1);
    return { open, ids: ids(p), stays, closed: !p.isOpen() };
  },

  async place() {
    const p = load(SAGA());
    await p.into('tmdb-movie-2');
    const row = p.q('.coll__row');
    row.scrollLeft = 340;
    row.listeners.scroll.forEach((fn) => fn({}));
    await p.redraw('tmdb-movie-3');
    const other = p.q('.coll__row').scrollLeft;
    await p.redraw('tmdb-movie-2');
    return { other: other === 340, back: p.q('.coll__row').scrollLeft, fresh: p.q('.coll__row') !== row };
  },

  // Two of them are asked for elsewhere on the page while the first is added.
  async meanwhile() {
    const p = await asked();
    await p.press(p.go());
    p.told(3);
    p.told(4, { download_state: 'in_library' });
    await p.answer(0);
    return { ids: ids(p), rows: p.rows(), said: p.q('.coll-dialog__said').textContent, go: p.go().textContent };
  },

  // Asked for elsewhere, and taken out again, before its turn.
  async takenOut() {
    const p = await asked();
    await p.press(p.go());
    p.told(3);
    p.told(3, { download_state: null });
    await p.answer(0);
    return { ids: ids(p), row: p.rows()[1] };
  },

  async skipped() {
    const p = await asked();
    await p.press(p.go());
    p.told(3);
    await p.answer(0);
    return { ids: ids(p), go: p.go().textContent, get: p.get() };
  },

  // Closed while it runs, and opened again: a failure is still there to be read.
  async failedClosed() {
    const p = await asked();
    await p.press(p.go());
    await p.press(p.q('.coll-dialog .btn--ghost'));
    await p.press(p.q('.coll__get'));
    const spins = !!p.doc.querySelectorAll('.coll-item')[0].querySelector('.btn-spinner');
    await p.press(p.q('.coll-dialog .btn--ghost'));
    await p.answer(0);
    await p.answer(1, new APIError(502, 'Failed to add to Radarr'));
    await p.answer(2);
    await p.press(p.q('.coll__get'));
    const out = { spins, rows: p.rows(), said: p.q('.coll-dialog__said').textContent, go: p.go().textContent };
    await p.press(p.go());
    out.ids = ids(p);
    await p.press(p.q('.coll-dialog .btn--ghost'));
    await p.answer(3, new APIError(502, 'Failed to add to Radarr'));
    // Meanwhile it is got another way, and one of those added is taken out.
    p.told(3);
    p.clock.now += 120000;
    const back = SAGA();
    Object.assign(back.films[2], { can_request: false, why_not: 'requested', download_state: 'queued' });
    Object.assign(back.films[3], { can_request: false, why_not: 'requested', download_state: 'queued' });
    p.reply(back);
    await p.redraw('tmdb-movie-2');
    await p.press(p.q('.coll__get'));
    out.later = [p.rows(), p.go().textContent];
    return out;
  },

  // The session ends while films are added: a request asks for unsaved changes before it goes to sign in.
  async signedOut() {
    const p = await asked();
    await p.press(p.go());
    const before = p.warns();
    const unsaved = p.ctx.MM.api.hasUnsavedChanges();
    const after = p.warns();
    const out = { before, unsaved, after, rows: p.rows(), go: p.go().textContent, disabled: p.go().getAttribute('aria-disabled') };
    await p.press(p.go());
    out.ids = ids(p);
    await p.answer(0); // the one on its way before, answered after all
    out.still = ids(p);
    return out;
  },

  async held() {
    const p = await asked();
    const enter = (repeat) => {
      const e = { key: 'Enter', repeat, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      (p.go().listeners.keydown || []).forEach((fn) => fn(e));
      return e.defaultPrevented;
    };
    const prevented = [enter(false), enter(true)];
    await p.press(p.go());
    await p.answer(0); await p.answer(1); await p.answer(2);
    click(p.doc, p.go());
    const soon = p.isOpen();
    await p.press(p.go());
    return { prevented, soon, later: p.isOpen() };
  },

  // TMDB fails after the row was drawn.
  async outage() {
    let reply = SAGA();
    const p = load(() => reply);
    await p.into('tmdb-movie-2');
    const node = p.q('.coll');
    reply = new APIError(502, 'tmdb_request_failed');
    p.doc.dispatchEvent({ type: 'mm:watch-marked', detail: {} });
    await p.redraw('tmdb-movie-2');
    const kept = p.q('.coll') === node && !p.q('.ttl__collsec').hidden;
    reply = { name: 'Back again', films: SAGA().films };
    await p.redraw('tmdb-movie-2');
    return { kept, gets: p.gets.length, name: p.q('.coll__name').textContent.split(' · ')[0] };
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
