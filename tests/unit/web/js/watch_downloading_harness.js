// Runs downloading.js, watch/core.js and watch/downloading.js against the fake DOM, with a clock
// that is stepped, and prints every scenario's outcome as JSON.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const SOURCES = ['core/api.js', 'downloading.js', 'watch/core.js', 'watch/rows-load.js', 'watch/downloading.js'].map((name) =>
  new vm.Script(fs.readFileSync(path.join(JS, name), 'utf8'), { filename: name }));

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', !!on); },
});
const ownRemove = Node.prototype.remove;
Node.prototype.remove = function () {
  if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
  ownRemove.call(this);
};

const item = (title, over) => ({
  key: 'radarr:' + title, title, kind: 'movie', state: 'downloading', percent: 40, minutes_left: 14, line: '', held: false,
  ready_seconds: null, ref: 'tmdb-movie-' + title.length, href: '/watch#title=tmdb-movie-' + title.length,
  poster_url: '/api/poster/1', art_url: '', ...over,
});
const ready = (title, seconds = 60, over) => item(title, { state: 'ready', percent: 100, minutes_left: null, ready_seconds: seconds, ...over });
const answer = (items, outage = '') => {
  const of = (...states) => items.filter((i) => states.includes(i.state)).length;
  const active = of('downloading', 'almost_ready');
  return { ok: true, outage, items, summary: { total: items.length, active, ready: of('ready'), waiting: items.length - active - of('ready'), percent: 0 } };
};

const recentCard = (ref, title) => ['article', { class: 'watch-card' }, ['a', { class: 'watch-card__link', href: '/watch', 'data-title-ref': ref },
  ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, title]]]];

/* *answers*: what the server gives, the last one for good; a number is an error's status, 'never' no answer. */
function load(answers, opts = {}) {
  const doc = makeDocument();
  doc.hidden = false;
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' },
    ['div', { class: 'watch-rows' },
      ['div', { class: 'dlx', id: 'watch-downloading', hidden: '' }],
      ['section', { class: 'watch-row', 'data-row-id': 'recent' }, ['div', { class: 'watch-row__viewport' },
        ['div', { class: 'watch-row__scroll' }, recentCard('movie-1', 'Salt'), recentCard('movie-2', 'Fieldwork')]]]],
    ...(opts.drawn ? [['script', { id: 'watch-page-data' }, JSON.stringify({ downloading: opts.drawn })]] : [])]));
  const bare = doc.createElement;
  const styled = (el) => { if (opts.still === false) el.animate = () => ({}); el.style = { set: {}, setProperty(k, v) { this.set[k] = v; }, removeProperty(k) { delete this.set[k]; } }; return el; };
  doc.createElement = (t) => styled(bare(t));
  (function all(n) { if (n.nodeType === 1) styled(n); n.childNodes.forEach(all); })(doc.body);
  const p = { doc, asks: 0, aborted: 0, answers: answers.slice(), time: 1000000 };
  let timers = [];
  let ids = 0;
  const clock = {
    set(fn, ms) { timers.push({ id: ++ids, fn, at: p.time + (ms || 0) }); return ids; },
    clear(id) { timers = timers.filter((t) => t.id !== id); },
  };
  p.step = async (ms) => {
    const until = p.time + ms;
    await settle();
    for (;;) {
      const next = timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
      if (!next) break;
      p.time = next.at;
      clock.clear(next.id);
      next.fn();
      await settle();
    }
    p.time = until;
  };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Set, Map, Error, parseFloat, AbortController,
    Date: { now: () => p.time },
    setTimeout: clock.set, clearTimeout: clock.clear,
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingBottom: '0', columnGap: '14' }),
    matchMedia: () => ({ matches: opts.still !== false }),
    innerHeight: 900, innerWidth: 1280, scrollBy() {},
    addEventListener() {}, requestAnimationFrame: (fn) => fn(),
    location: { pathname: '/settings', href: 'start' },
    fetch: (url, o) => {
      p.asks += 1;
      const a = p.answers.length > 1 ? p.answers.shift() : p.answers[0];
      if (typeof a === 'number') return Promise.resolve({ ok: false, status: a, json: () => Promise.resolve({}) });
      if (a === 'never') return new Promise((_ok, no) => o.signal.addEventListener('abort', () => { p.aborted += 1; no(new Error('aborted')); }));
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(a) });
    },
    MM: {
      dom: { icon: (name) => build(doc, ['svg', { class: 'ico ' + name }]) },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  SOURCES.forEach((s) => s.runInContext(ctx));
  p.MM = ctx.MM;
  p.watch = ctx.MMWatch;
  p.at = () => ctx.location.href;
  p.tell = (name) => (doc.listeners[name] || []).forEach((fn) => fn({ type: name }));
  p.hide = (on) => { doc.hidden = on; p.tell('visibilitychange'); };
  p.slot = doc.getElementById('watch-downloading');
  p.cards = () => p.slot.querySelectorAll('.dw').map((n) => n.querySelector('.dw__title').textContent);
  p.card = (title) => p.slot.querySelectorAll('.dw').find((n) => n.querySelector('.dw__title').textContent === title);
  p.line = (title) => p.card(title).querySelector('.dw__line').textContent;
  p.recent = () => doc.querySelector('[data-row-id="recent"]').querySelectorAll('.watch-card__title').map((n) => n.textContent);
  p.said = () => doc.querySelector('.sr-only').textContent;
  p.note = () => { const n = p.slot.querySelector('.dlx__note'); return n.hidden ? null : n.textContent; };
  p.heading = () => p.slot.querySelector('.row-open').getAttribute('aria-label');
  p.there = () => !p.slot.hidden && p.slot.querySelectorAll('.dw').length > 0;
  return p;
}

const scenarios = {
  async nothing() {
    const p = load([answer([])]);
    await p.step(10);
    return { hidden: p.slot.hidden, inside: p.slot.childNodes.length };
  },
  async states() {
    const p = load([answer([
      item('Down', { percent: 71, minutes_left: 6 }), item('Hours', { minutes_left: 130 }), item('Unknown', { minutes_left: null }),
      item('Almost', { state: 'almost_ready' }), item('Queued', { state: 'queued' }), item('Looking', { state: 'searching' }),
      item('Stuck', { state: 'stuck', line: 'next attempt in ~4h' }), ready('Ready'), ready('Part', 60, { line: 'Some episodes ready' }),
    ])]);
    await p.step(10);
    return {
      lines: Object.fromEntries(p.cards().map((t) => [t, p.line(t)])),
      fill: p.card('Down').style.set['--p'],
      classes: Object.fromEntries(p.cards().map((t) => [t, p.card(t).className])),
      heading: p.heading(),
    };
  },
  async links() {
    const p = load([answer([
      item('Known'), item('Nzb', { ref: null, href: '/downloads' }),
      item('Bad', { ref: 'javascript:1', href: 'javascript:alert(1)', poster_url: 'http://elsewhere/x', art_url: '//elsewhere/y' }),
    ])]);
    await p.step(10);
    const of = (t) => { const a = p.card(t).querySelector('a'); return [a.getAttribute('href'), a.getAttribute('data-title-ref')]; };
    return { known: of('Known'), nzb: of('Nzb'), bad: of('Bad'), pictures: p.card('Bad').querySelectorAll('img').map((i) => i.getAttribute('src')) };
  },
  async live() {
    const p = load([answer([item('A', { percent: 40 })]), answer([item('A', { percent: 55, minutes_left: 9 })])]);
    await p.step(10);
    const node = p.card('A');
    await p.step(5000);
    return { same: p.card('A') === node, line: p.line('A'), fill: node.style.set['--p'] };
  },
  async sole() {
    const p = load([answer([item('A'), item('B')]), answer([item('A')])]);
    await p.step(10);
    const before = p.card('A').classList.contains('is-sole');
    await p.step(5000);
    return { before, after: p.card('A').classList.contains('is-sole'), cards: p.cards() };
  },
  async readyLands() {
    const p = load([answer([item('A'), item('B')]), answer([item('A'), ready('B', 60)])]);
    await p.step(5010);
    const at = { said: p.said(), cards: p.cards(), recent: p.recent() };
    await p.step(59000);
    const still = p.cards();
    await p.step(1500);
    const link = p.doc.querySelector('[data-row-id="recent"]').querySelector('a');
    return { at, still, cards: p.cards(), recent: p.recent(), ref: link.getAttribute('data-title-ref'), heading: p.heading() };
  },
  async readyOnceOnly() {
    const p = load([answer([ready('B', 2), item('A')])]);
    await p.step(40000); // the server goes on saying it is ready
    return { cards: p.cards(), recent: p.recent() };
  },
  async readyAgain() {
    const p = load([answer([ready('B', 2), item('A')]), answer([item('A'), item('B')])]);
    await p.step(10000);
    return p.cards();
  },
  async alreadyThere() {
    const p = load([answer([ready('Fieldwork', 2, { ref: 'movie-2', href: '/watch/movie/2' }), item('A')])]);
    await p.step(3000);
    return p.recent();
  },
  async readyWithoutTitle() {
    const p = load([answer([ready('Nzb', 2, { ref: null, href: '/downloads' })]), answer([])]);
    await p.step(3000);
    return { hidden: p.slot.hidden, recent: p.recent() };
  },
  async lastOneCloses() {
    const p = load([answer([ready('B', 2)])]);
    await p.step(20);
    const open = p.there();
    await p.step(2500);
    return { open, hidden: p.slot.hidden, inside: p.slot.childNodes.length };
  },
  async closesAndComesBack() {
    const p = load([answer([item('A')]), answer([]), answer([item('C')])]);
    await p.step(10);
    await p.step(5000);
    const gone = p.slot.hidden;
    await p.step(30000);
    return { gone, cards: p.cards(), hidden: p.slot.hidden };
  },
  async backWhileClosing() {
    const p = load([answer([item('A')]), answer([]), answer([item('C')])], { still: false });
    await p.step(5100);
    const shut = !p.slot.classList.contains('is-open') && !p.slot.hidden;
    p.hide(true);
    p.hide(false);
    await p.step(1000);
    return { shut, cards: p.cards(), open: p.slot.classList.contains('is-open'), hidden: p.slot.hidden };
  },
  async abandoned() {
    const p = load([answer([item('A'), item('B')]), answer([item('B')])]);
    await p.step(5010);
    return { cards: p.cards(), recent: p.recent(), said: p.said() };
  },
  async focusMovesOn() {
    const p = load([answer([item('A'), item('B')]), answer([item('B')])]);
    await p.step(10);
    p.card('A').querySelector('a').focus();
    await p.step(5010);
    return p.doc.activeElement === p.card('B').querySelector('a');
  },
  async focusFollowsTheLast() {
    const p = load([answer([ready('B', 2)])]);
    await p.step(20);
    p.card('B').querySelector('a').focus();
    await p.step(3000);
    const row = p.doc.querySelector('[data-row-id="recent"]');
    return p.doc.activeElement === row.querySelector('a');
  },
  async reorder() {
    const p = load([answer([item('A'), item('Q', { state: 'queued' })]), answer([item('New'), item('A'), item('Q', { state: 'queued' })])]);
    await p.step(10);
    p.card('Q').querySelector('a').focus();
    await p.step(5000);
    return { cards: p.cards(), focused: p.doc.activeElement === p.card('Q').querySelector('a') };
  },
  async moved() {
    const p = load([answer([item('A'), item('Q', { state: 'queued' })]), answer([item('Q'), item('A')])]);
    await p.step(10);
    p.card('Q').querySelector('a').focus();
    await p.step(5000);
    return { cards: p.cards(), focused: p.doc.activeElement === p.card('Q').querySelector('a') };
  },
  async goneBeforeItsMinute() {
    const p = load([answer([ready('B', 60), item('A')]), answer([item('A')])]);
    await p.step(5010);
    return { cards: p.cards(), recent: p.recent() };
  },
  async lastIsReplaced() {
    const p = load([answer([item('A')]), answer([item('C')])]);
    await p.step(10);
    p.card('A').querySelector('a').focus();
    await p.step(5010);
    return p.doc.activeElement === p.card('C').querySelector('a');
  },
  async focusSkipsWhatLeaves() {
    const p = load([answer([item('A'), item('B'), item('C')]), answer([item('C')])], { still: false });
    await p.step(10);
    p.card('A').querySelector('a').focus();
    const passed = [];
    p.cards().forEach((t) => { const a = p.card(t).querySelector('a'); const own = a.focus; a.focus = () => { passed.push(t); own.call(a); }; });
    await p.step(5010);
    return { passed, leaving: p.card('B').classList.contains('is-leaving'), onC: p.doc.activeElement === p.card('C').querySelector('a') };
  },
  // Each card that leaves has the focus; what stands in the row while it goes, and which cards were moved.
  async leavesFromItsPlace() {
    const out = {};
    for (const [all, gone] of [['ABC', 'A'], ['ABC', 'B'], ['ABCD', 'A'], ['ABCD', 'C']]) {
      const cards = all.split('').map((t) => (t === gone ? ready(t, 60) : item(t)));
      const p = load([answer(cards), answer(cards.filter((i) => i.title !== gone))], { still: false });
      await p.step(10);
      p.card(gone).querySelector('a').focus();
      const moved = [];
      const scroller = p.slot.querySelector('.watch-row__scroll');
      const own = scroller.insertBefore;
      scroller.insertBefore = (c, ref) => { moved.push(c.querySelector('.dw__title').textContent); return own.call(scroller, c, ref); };
      await p.step(5010);
      out[all + ' without ' + gone] = { row: p.cards().join(''), moved, focus: p.doc.activeElement.querySelector('.dw__title').textContent };
    }
    return out;
  },
  async focusOnSeeAll() {
    const p = load([answer([item('A')]), answer([])]);
    await p.step(10);
    p.slot.querySelector('.row-open').focus();
    await p.step(5010);
    return p.doc.activeElement.getAttribute('data-title-ref');
  },
  async saysBoth() {
    const p = load([answer([item('A'), item('B'), item('C')]), answer([ready('A'), ready('B'), item('C')]),
      answer([ready('A'), ready('B'), ready('C')])]);
    await p.step(5010);
    const both = p.said();
    await p.step(5000);
    return { both, then: p.said() };
  },
  async saidAgain() {
    const p = load([answer([item('A')]), answer([ready('A', 60)]), answer([item('A')]), answer([ready('A', 60)])]);
    await p.step(5010);
    const first = p.said();
    await p.step(9000);
    const between = p.said();
    await p.step(1500);
    return { first, between, again: p.said() };
  },
  async readyAnew() {
    const p = load([answer([ready('B', 2), item('A')]), answer([item('A')]), answer([ready('B', 60), item('A')])]);
    await p.step(5010);
    const gone = p.cards();
    await p.step(5000);
    return { gone, cards: p.cards() };
  },
  async underAnotherRef() {
    const p = load([answer([ready('Fieldwork', 2), ready('Salt', 2, { kind: 'show', ref: 'tmdb-tv-4' }), item('A')])]);
    await p.step(3000);
    return p.doc.querySelector('[data-row-id="recent"]').querySelectorAll('[data-title-ref]').map((n) => n.getAttribute('data-title-ref'));
  },
  async twoFilmsOfOneName() {
    const p = load([answer([ready('B', 2, { key: 'radarr::1', ref: 'tmdb-movie-1' }), ready('B', 2, { key: 'radarr::2', ref: 'tmdb-movie-2' }), item('A')])]);
    await p.step(3000);
    return p.recent();
  },
  async loaderIsTold() {
    const p = load([answer([ready('B', 2, { ref: 'show-7', kind: 'show' }), item('A')])]);
    const viewport = p.doc.querySelector('[data-row-id="recent"]').querySelector('.watch-row__viewport');
    const page = [build(p.doc, recentCard('show-7', 'B')), build(p.doc, recentCard('movie-8', 'Other'))];
    const row = p.watch.rowsLoad.start(viewport, { title: 'Recently added', next: 'p1', fetchPage: () => Promise.resolve({ cards: page, next: null }) });
    await p.step(3000);
    row.more();
    await p.step(10);
    return p.recent();
  },
  async loaderHoldsIt() {
    const p = load([answer([ready('Fieldwork', 2, { ref: 'movie-2' }), item('A')])]);
    const viewport = p.doc.querySelector('[data-row-id="recent"]').querySelector('.watch-row__viewport');
    p.watch.rowsLoad.start(viewport, { title: 'Recently added', next: null });
    viewport.querySelectorAll('.watch-card')[1].remove();
    await p.step(3000);
    return { recent: p.recent(), cards: p.cards() };
  },
  async signedOut() {
    const p = load([answer([item('A')]), 401]);
    await p.step(600000);
    return { asks: p.asks, at: p.at(), cards: p.cards(), note: p.note() };
  },
  async outage() {
    const p = load([answer([item('A', { held: true }), item('B')], "Can't reach Radarr"), answer([item('B')], "Can't reach Radarr"), answer([item('B')])]);
    await p.step(10);
    const first = { note: p.note(), held: p.card('A').classList.contains('is-held'), other: p.card('B').classList.contains('is-held') };
    await p.step(5000);
    const second = p.note();
    await p.step(5000);
    return { first, second, after: p.note() };
  },
  async failed() {
    const p = load([answer([item('A')]), 500, 500, answer([item('A')])]);
    await p.step(5010);
    const once = { cards: p.cards(), note: p.note() };
    await p.step(10000);
    const twice = { cards: p.cards(), note: p.note(), held: p.card('A').classList.contains('is-held') };
    await p.step(20000);
    return { once, twice, after: { note: p.note(), held: p.card('A').classList.contains('is-held') } };
  },
  async failedFirst() {
    const p = load([503, answer([item('A')])]);
    await p.step(10);
    const first = p.slot.hidden;
    await p.step(10000);
    return { first, cards: p.cards() };
  },
  async pace() {
    const asksIn = async (a, ms) => { const p = load([a]); await p.step(10); const n = p.asks; await p.step(ms); return p.asks - n; };
    return {
      busy: await asksIn(answer([item('A')]), 30000), idle: await asksIn(answer([]), 60000),
      stuck: await asksIn(answer([item('A', { state: 'stuck' })]), 60000), refused: await asksIn(401, 300000),
      failing: await asksIn(500, 600000),
    };
  },
  async hidden() {
    const p = load([answer([item('A')])]);
    await p.step(10);
    p.hide(true);
    const before = p.asks;
    await p.step(600000);
    const during = p.asks - before;
    p.hide(false);
    await p.step(10);
    return { during, back: p.asks - before };
  },
  async hiddenWhileAsking() {
    const p = load(['never', answer([item('A')])]);
    await p.step(10);
    p.hide(true);
    p.hide(false);
    await p.step(10);
    return { aborted: p.aborted, cards: p.cards() };
  },
  async hung() {
    const p = load(['never', answer([item('A')])]);
    await p.step(61000);
    const given = p.aborted;
    await p.step(10000);
    return { given, cards: p.cards() };
  },
  async justAsked() {
    const p = load([answer([])]);
    await p.step(10);
    const before = p.asks;
    p.tell('mm:title-changed');
    await p.step(1000);
    const soon = p.asks - before;
    await p.step(20000);
    return { soon, then: p.asks - before };
  },
  async drawn() {
    const p = load([answer([item('Later')])], { drawn: answer([item('A')]) });
    const now = { cards: p.cards(), open: p.slot.classList.contains('is-open'), asks: p.asks };
    await p.step(5000);
    return { now, cards: p.cards() };
  },
  async drawnIdle() {
    const p = load([answer([item('New')])], { drawn: answer([]) });
    const asks = p.asks;
    await p.step(1500);
    return { asks, cards: p.cards() };
  },
  async heldIdle() {
    const p = load([{ ...answer([]), age_ms: 2500 }, answer([item('New')])], { drawn: answer([]) });
    await p.step(3000);
    const fresh = load([answer([])], { drawn: answer([]) });
    await fresh.step(20000);
    const down = load([{ ...answer([], "Can't check downloads"), age_ms: 60000 }], { drawn: answer([]) });
    await down.step(20000);
    return { cards: p.cards(), asks: fresh.asks, down: down.asks };
  },
  async readers() {
    const p = load([answer([item('A')]), answer([item('A'), item('B')])]);
    await p.step(10);
    const got = [];
    const stop = p.MM.downloading.subscribe((s) => got.push(s.summary.total));
    let told = 0;
    p.MM.downloading.subscribe(() => { told += 1; if (told > 1) throw new Error('a reader of its own'); });
    const after = [];
    p.MM.downloading.subscribe((s) => after.push(s.summary.total));
    await p.step(5000);
    stop();
    await p.step(5000);
    return { got, after, asks: p.asks, last: p.MM.downloading.last().summary.total };
  },
};

(async () => {
  const quiet = console.error;
  console.error = () => {};
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  console.error = quiet;
  console.log(JSON.stringify(out));
})();
