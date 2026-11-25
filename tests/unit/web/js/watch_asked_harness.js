// Runs watch/asked.js against the fake DOM, with stand-ins for the modules it needs, and prints
// one scenario's outcome as JSON. It is noon on 25 November 2026.
// Usage: TZ=Europe/Dublin node watch_asked_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, build, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const NOON = Date.UTC(2026, 10, 25, 12);

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.scrollTo = function (o) { this.scrollLeft = o.left; };

function item(n, state, over) {
  return Object.assign({ key: 'movie-' + n, ref: (state === 'ready' ? 'movie-' : 'tmdb-movie-') + n,
    held_ref: state === 'ready' ? 'movie-' + n : null, kind: 'movie', title: 'Film ' + n,
    subtitle: null, state, arrived_at: null, release_date: null, leaving_days: null, download_state: null }, over);
}

const ready = (n, days, over) => item(n, 'ready', Object.assign({ arrived_at: new Date(NOON - days * 86400000).toISOString() }, over));

function load(answer, opts = {}) {
  const doc = makeDocument();
  const clock = { now: opts.now || NOON, timers: [] };
  const api = { answer, gets: 0 };
  const left = [];
  const standing = new Set(opts.continues); // the refs in Continue watching
  const win = {};
  doc.body.appendChild(build(doc, ['div', { class: 'watch-rows' }, ['div', { id: 'watch-asked', hidden: '' }],
    ...(opts.skeleton ? [['div', { class: 'asked-skel' }]] : []), ['section', { class: 'watch-row', id: 'next' }]]));
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).slice().forEach((fn) => fn(e));
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, isNaN,
    Date: class extends Date { static now() { return clock.now; } },
    setTimeout: (fn, ms) => clock.timers.push({ fn, at: clock.now + (ms || 0) }),
    clearTimeout: (id) => { if (clock.timers[id - 1]) clock.timers[id - 1].fn = () => {}; },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: !!opts.reduced }),
    addEventListener: (t, fn) => { win[t] = fn; },
    MM: { api: { get: () => {
      api.gets += 1;
      if (api.answer instanceof Error) return Promise.reject(api.answer);
      if (api.held) return new Promise((r) => api.held.push(r));
      return Promise.resolve(JSON.parse(JSON.stringify(Object.assign({ complete: true }, api.answer))));
    } } },
    MMWatch: {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js'].forEach(run);
  const watch = ctx.MMWatch;
  let source = null;
  watch.browse = { card: (i) => build(doc, ['article', { class: 'watch-card' }, ['a', { class: 'watch-card__link' },
    ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, i.title]], ['span', { class: 'watch-card__sub' }, 'Movie']]]) };
  watch.expand = {
    head: (id, title) => build(doc, ['div', { class: 'watch-row__head' }, ['h2', {}, title]]),
    row() {}, source: (id, make) => { source = () => make({ id }); },
  };
  watch.marks = { continues: (ref) => standing.has(ref) };
  watch.address = { putRowsBack() {} };
  watch.keep = {
    growIn() {},
    foldAway: (n) => n.remove(),
    fromRow: (card, row) => {
      left.push(card.getAttribute('data-asked'));
      // The last one leaves with its row, which takes a moment to go.
      if (row.querySelectorAll('.watch-card').length > 1) card.remove();
      else { card.classList.add('is-kept'); row.setAttribute('data-going', ''); ctx.setTimeout(() => row.remove(), 20000); }
      return { focus: null, undo() {} };
    },
  };
  run('watch/asked.js');
  const cards = () => doc.querySelectorAll('.asked-row .watch-card').filter((c) => !c.closest('[data-going]'));
  return {
    doc, api, left, standing, win, source: () => source(),
    fire: (type, ref) => doc.dispatchEvent({ type, detail: { ref } }),
    async pass(ms) {
      clock.now += ms;
      clock.timers.filter((t) => t.at <= clock.now).forEach((t) => { const fn = t.fn; t.fn = () => {}; t.at = Infinity; fn(); });
      await settle();
    },
    async answer(next) { api.answer = next; },
    keys: () => cards().map((c) => c.getAttribute('data-asked')),
    states: () => cards().map((c) => c.getAttribute('data-asked-state')),
    lines: () => cards().map((c) => c.querySelector('.asked-state__text').textContent),
    row: () => doc.querySelectorAll('.asked-row').filter((r) => !r.hasAttribute('data-going'))[0] || null,
    q: (sel) => doc.querySelector(sel),
    has: (sel) => cards().map((c) => !!c.querySelector(sel)),
  };
}

const KINDS = [
  ready(1, 0), ready(2, 1), ready(3, 3), ready(4, 14), ready(5, 2, { leaving_days: 4 }), ready(12, 1, { started: true }),
  item(6, 'arriving'), item(7, 'downloading', { download_state: 'downloading' }), item(8, 'queued'),
  item(9, 'coming', { release_date: '2026-12-14' }), item(10, 'coming', { release_date: '2027-03-01' }), item(11, 'coming'),
];

const scenarios = {
  async drawn() {
    const t = load({ items: KINDS }, { skeleton: true });
    await settle();
    const row = t.row();
    return { lines: t.lines(), states: t.states(), news: t.has('.asked-new'), leaves: t.doc.querySelectorAll('.asked-leaf').map((l) => l.textContent),
      skeleton: !!t.q('.asked-skel'), before: row.nextElementSibling.id, title: row.querySelector('.watch-row__head').textContent, line: t.q('.asked-line').textContent };
  },
  async late_evening() {
    // Came at 23:30 yesterday, by the person's own clock; it is 9 in the morning.
    const t = load({ items: [item(1, 'ready', { arrived_at: '2026-11-24T23:30:00+00:00' })] }, { now: NOON - 3 * 3600000 });
    await settle();
    return { lines: t.lines() };
  },
  async text() {
    const t = load({ items: [ready(1, 0, { title: '<img src=x onerror=alert(1)>', release_date: '2026-13-01"><b>' }), item(2, 'coming', { release_date: '2026-99-01' })] });
    await settle();
    return { tags: t.row().querySelectorAll('img, b').length, title: t.q('.watch-card__title').textContent, lines: t.lines() };
  },
  async nothing() {
    const t = load({ items: [] }, { skeleton: true });
    await settle();
    return { row: !!t.row(), skeleton: !!t.q('.asked-skel') };
  },
  async failed() {
    const t = load(new Error('down'), { skeleton: true });
    await settle();
    const at = { row: !!t.row(), skeleton: !!t.q('.asked-skel') };
    await t.answer({ items: [ready(1, 0)] });
    t.fire('visibilitychange');
    await t.pass(6000);
    return { at, keys: t.keys() };
  },
  async watched() {
    const t = load({ items: [ready(1, 0), ready(2, 1)] });
    await settle();
    await t.answer({ items: [ready(2, 1)] });
    t.fire('mm:watch-marked');
    const shown = t.keys();
    await t.pass(6000);
    const one = { shown, left: t.left.slice(), keys: t.keys() };
    await t.answer({ items: [] });
    t.fire('mm:watch-marked');
    await t.pass(6000);
    const none = !!t.row();
    await t.answer({ items: [ready(2, 1)] });
    t.fire('mm:watch-marked');
    await t.pass(6000);
    return { ...one, none, back: t.keys() };
  },
  async arrives() {
    const t = load({ items: [ready(1, 3), item(2, 'downloading')] });
    await settle();
    await t.answer({ items: [ready(2, 0), ready(1, 3)] });
    await t.pass(60000);
    const out = { gets: t.api.gets, keys: t.keys(), lit: t.doc.querySelectorAll('.asked-arriving').map((c) => c.getAttribute('data-asked')),
      said: t.q('[role="status"]').textContent };
    await t.pass(2000);
    out.after = [t.doc.querySelectorAll('.asked-arriving').length, t.doc.querySelectorAll('.asked-sweep').length];
    await t.pass(600000);
    out.later = t.api.gets;
    return out;
  },
  async calm() {
    const t = load({ items: [item(2, 'downloading')] }, { reduced: true });
    await settle();
    await t.answer({ items: [ready(2, 0)] });
    await t.pass(60000);
    return { lines: t.lines(), lit: t.doc.querySelectorAll('.asked-arriving, .asked-sweep').length };
  },
  async hidden() {
    const t = load({ items: [item(2, 'downloading')] });
    await settle();
    t.doc.hidden = true;
    await t.pass(60000);
    await t.pass(60000);
    return { gets: t.api.gets };
  },
  async unanswered() {
    const t = load({ items: [ready(1, 3), ready(3, 4), item(2, 'downloading')] });
    await settle();
    await t.answer({ items: [ready(1, 3)], complete: false });
    t.fire('mm:title-changed');
    await t.pass(6000);
    return { keys: t.keys() };
  },
  async unchanged() {
    const t = load({ items: [ready(1, 3), ready(2, 4)] });
    await settle();
    const first = t.q('.asked-row .watch-card');
    first.querySelector('.watch-card__link').focus();
    await t.answer({ items: [ready(1, 3), ready(2, 4, { leaving_days: 6 }), item(3, 'queued')] });
    t.fire('mm:title-changed');
    t.fire('mm:title-changed');
    await t.pass(1000);
    const early = t.api.gets;
    await t.pass(5000);
    return { same: t.q('.asked-row .watch-card') === first, focused: first.contains(t.doc.activeElement), keys: t.keys(), gets: [early, t.api.gets], lit: t.doc.querySelectorAll('.asked-arriving').length };
  },
  async marked() {
    const t = load({ items: [ready(1, 3)] });
    await settle();
    await t.pass(60000);
    t.fire('mm:watch-marked', 'movie-1');
    await t.pass(300);
    const early = t.api.gets;
    await t.pass(700);
    return { gets: [early, t.api.gets] };
  },
  async many() {
    const t = load({ items: Array.from({ length: 27 }, (_x, n) => ready(n + 1, n)) });
    await settle();
    const grid = t.source();
    const drawn = t.keys().length;
    t.fire('mm:title-changed');
    await t.answer({ items: Array.from({ length: 27 }, (_x, n) => ready(n + 2, n)) });
    await t.pass(6000);
    return { row: [drawn, t.keys().length], more: t.q('.watch-row__viewport').hasAttribute('data-row-more'), said: t.row().getAttribute('data-row-total'),
      grid: grid.cards.length, total: grid.total, next: grid.next };
  },
  async continued() {
    const t = load({ items: [ready(1, 0), ready(2, 1, { started: true }), item(3, 'coming', { held_ref: 'show-3' })] }, { continues: ['movie-2', 'show-3'] });
    await settle();
    const out = { keys: t.keys(), total: t.row().getAttribute('data-row-total'), grid: t.source().total };
    t.standing.delete('movie-2');
    t.fire('mm:watch-continue');
    await t.pass(6000);
    out.dismissed = [t.keys(), t.lines()];
    t.standing.add('movie-2');
    t.fire('mm:watch-continue');
    out.undone = [t.keys(), t.row().getAttribute('data-row-total'), t.api.gets];
    return out;
  },
  async continued_mark() {
    // Marked as watched in Continue watching: the server's answer comes before the row takes it.
    const t = load({ items: [ready(1, 0), ready(2, 1)] }, { continues: ['movie-2'] });
    await settle();
    t.standing.delete('movie-2');
    t.fire('mm:watch-continue');
    const early = t.keys();
    await t.answer({ items: [ready(1, 0)] });
    await t.pass(6000);
    return { early, keys: t.keys() };
  },
  async all_continued() {
    const t = load({ items: [ready(1, 0)] }, { continues: ['movie-1'], skeleton: true });
    await settle();
    const at = { row: !!t.row(), skeleton: !!t.q('.asked-skel'), grid: (await t.source().fetchPage()).cards.length };
    t.standing.clear();
    t.fire('mm:watch-continue');
    await t.pass(6000);
    const alone = t.keys();
    t.standing.add('movie-1');
    t.fire('mm:watch-continue');
    return { ...at, alone, left: t.left };
  },
  async replaced() {
    const t = load({ items: [ready(1, 0)] });
    await settle();
    const row = t.row();
    await t.answer({ items: [ready(2, 0)] });
    t.fire('visibilitychange');
    await t.pass(6000);
    return { keys: t.keys(), same: t.row() === row, left: t.left };
  },
  async own_marks() {
    // Two marked within five seconds, then an undo; something else of the page calls for a look meanwhile.
    const t = load({ items: [ready(1, 0), ready(2, 1), ready(3, 2)] });
    await settle();
    await t.pass(60000);
    const out = [];
    const step = async (ref, items, ms) => {
      await t.answer({ items });
      t.fire('mm:watch-marked', ref);
      t.fire('mm:title-changed');
      await t.pass(ms);
      out.push(t.keys());
    };
    await step('movie-1', [ready(2, 1), ready(3, 2)], 900);
    await step('movie-2', [ready(3, 2)], 900);
    await step('movie-2', [ready(2, 1), ready(3, 2)], 0);
    const gets = t.api.gets;
    t.fire('mm:title-changed');
    await t.pass(4900);
    const early = t.api.gets - gets;
    await t.pass(100);
    return { keys: out, others: [early, t.api.gets - gets] };
  },
  async undone_in_flight() {
    // Marked, and undone while the asking that the mark called for is out; its answer comes
    // *late*, with the undo's own asking waiting for it, or before that is due; or the tab is *shown* meanwhile.
    const run = async (late, shown) => {
      const t = load({ items: [ready(1, 0), ready(2, 1)] });
      await settle();
      await t.pass(60000);
      t.api.held = [];
      t.fire('mm:watch-marked', 'movie-1');
      await t.pass(900);
      t.fire('mm:watch-marked', 'movie-1');
      if (late) await t.pass(1000);
      if (shown) { t.fire('visibilitychange'); await t.pass(5000); }
      const reply = t.api.held[0];
      t.api.held = null;
      reply({ complete: true, items: [ready(2, 1)] });
      await settle();
      const keys = [t.keys()];
      await t.pass(1000);
      return keys.concat([t.keys()], t.left);
    };
    return { early: await run(false), late: await run(true), shown: await run(true, true) };
  },
  async in_flight() {
    // Marked while a slow asking is on its way; its answer, from before the mark, comes after *wait*.
    const run = async (wait) => {
      const t = load({ items: [ready(1, 0), ready(2, 1)] });
      await settle();
      await t.pass(60000);
      t.api.held = [];
      t.fire('visibilitychange');
      await t.pass(0);
      const reply = t.api.held[0];
      t.api.held = null;
      t.fire('mm:watch-marked');
      await t.pass(wait);
      await t.answer({ items: [ready(2, 1)] });
      reply({ complete: true, items: [ready(1, 0), ready(2, 1)] });
      await settle();
      const stale = t.keys();
      await t.pass(6000);
      return { stale, keys: t.keys(), gets: t.api.gets };
    };
    return { soon: await run(1000), late: await run(8000) };
  },
  async grid_failed() {
    const t = load(new Error('down'));
    await settle();
    const grid = t.source();
    const failed = await grid.fetchPage(grid.next).then(() => false, () => true);
    await t.answer({ items: [ready(1, 0)] });
    const page = await grid.fetchPage(grid.next);
    return { failed, then: page.cards.length };
  },
  async grid_first() {
    const t = load({ items: [ready(1, 0), ready(2, 1)] });
    const grid = t.source();
    const page = await grid.fetchPage(grid.next);
    return { first: grid.cards.length, then: page.cards.length, next: page.next, gets: t.api.gets };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
