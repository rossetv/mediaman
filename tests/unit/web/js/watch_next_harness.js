// Runs Watch next (watch/watchnext.js) against the fake DOM, with the page laid out by a few numbers, and prints
// the outcome of one scenario as JSON. Usage: node watch_next_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const VIEW_H = 1000;  // the window's height
const SEC_TOP = 5000; // where the section stands on the page
const GRID_TOP = 5100;
const PITCH = 300;    // a row of cards and the gap under it
const CARD_H = 280;

/* The page's layout: the section's cards in rows of `cols`, under the room --wn-shed holds. */
function layout(doc, ctx) {
  const shedOf = (grid) => parseFloat(grid.style['--wn-shed'] || '0') || 0;
  const rectAt = (top, h) => ({ top: top - ctx.scrollY, bottom: top - ctx.scrollY + h, left: 0, right: 100, width: 100, height: h });
  Object.defineProperty(Node.prototype, 'getBoundingClientRect', {
    configurable: true,
    value() {
      const grid = doc.getElementById('watch-next-grid');
      if (this.id === 'watch-next') return rectAt(SEC_TOP, 1);
      if (this === grid) return rectAt(GRID_TOP, 1);
      if (grid && this.parentNode === grid) {
        const i = grid.children.indexOf(this);
        return rectAt(GRID_TOP + shedOf(grid) + Math.floor(i / ctx.cols) * PITCH, CARD_H);
      }
      if (this.id === 'watch-next-end') {
        const n = grid.children.length;
        return rectAt(GRID_TOP + shedOf(grid) + Math.ceil(n / ctx.cols) * PITCH, 1);
      }
      return rectAt(0, 20);
    },
  });
  // A node under a hidden one has no box.
  Object.defineProperty(Node.prototype, 'getClientRects', {
    configurable: true,
    value() {
      for (let a = this; a; a = a.parentNode) if (a.hasAttribute && a.hasAttribute('hidden')) return [];
      return doc.body.contains(this) ? [this.getBoundingClientRect()] : [];
    },
  });
}

/* The section as _watch_next.html draws it, and watchnext.js run over it. *opts*: who, entry (the
   history entry's id), stored (what sessionStorage holds), cols. */
function page(opts) {
  opts = opts || {};
  const doc = makeDocument();
  const asked = [];
  const timers = [];
  const frames = [];
  const listeners = {};
  const store = new Map(Object.entries(opts.stored || {}));
  const observers = [];
  const scrolls = [];
  const toasts = [];
  const ctx = {
    document: doc, console, cols: opts.cols || 3, scrollY: opts.scrollY || 0, innerHeight: VIEW_H,
    location: { hash: '' },
    history: { state: opts.entry ? { mm: { id: opts.entry, at: 0, stack: [] } } : null },
    performance: { now: () => ctx.clock },
    clock: 1000,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    requestAnimationFrame: (fn) => frames.push(fn),
    matchMedia: () => ({ matches: false }),
    getComputedStyle: (n) => ({ gridTemplateColumns: Array(ctx.cols).fill('100px').join(' '), getPropertyValue: () => '' }),
    scrollTo: (o) => { scrolls.push(o); ctx.scrollY = o.top; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener: () => {},
    sessionStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    IntersectionObserver: class {
      constructor(fn, o) { this.fn = fn; this.margin = o.rootMargin; this.seen = new Set(); observers.push(this); }
      observe(n) { this.seen.add(n); }
      unobserve(n) { this.seen.delete(n); }
    },
    UIFeedback: { toast: (m) => toasts.push(m) },
    MM: {
      dom: { icon: (name) => build(doc, ['svg', { class: 'ico', 'data-icon': name }]) },
      api: {
        get: (url) => new Promise((resolve, reject) => { asked.push({ url, resolve, reject }); }),
      },
    },
  };
  ctx.window = ctx;
  layout(doc, ctx);
  doc.body.appendChild(build(doc, ['a', { class: 'brand', href: '/' }]));
  doc.body.appendChild(build(doc, ['div', { id: 'watch-browse' },
    ['section', { class: 'wn', id: 'watch-next', 'data-wn-who': opts.who || 'ann' },
      ['div', { class: 'watch-group-label wn__label' },
        ['h2', { id: 'watch-next-title' }, 'Watch next'],
        ['div', { class: 'wn__filter' },
          ['button', { class: 'fpill on', 'data-wn-filter': 'all', 'aria-pressed': 'true' }, 'All'],
          ['button', { class: 'fpill', 'data-wn-filter': 'movie', 'aria-pressed': 'false' }, 'Movies'],
          ['button', { class: 'fpill', 'data-wn-filter': 'tv', 'aria-pressed': 'false' }, 'TV']]],
      ['p', { class: 'wn__note', hidden: '' }, 'New here'],
      ['div', { class: 'wn__body watch-grid-host' },
        ['div', { class: 'watch-grid', id: 'watch-next-grid' }],
        ['div', { id: 'watch-next-tail' }],
        ['div', { id: 'watch-next-end' }],
        ['p', { id: 'watch-next-said' }]],
      ['button', { class: 'wn-top', id: 'watch-next-top', 'aria-hidden': 'true' }, 'Back to top']]]));
  vm.createContext(ctx);
  ['watch/core.js', 'watch/rows-load.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  // The discover card, as watch/browse.js makes it, cut to what the section reads.
  ctx.MMWatch.browse = {
    card: (item) => build(doc, ['article', { class: 'watch-card' },
      ['a', { class: 'watch-card__link', 'data-title-ref': item.ref, ...(item.pick_ref ? { 'data-pick-ref': item.pick_ref } : {}) },
        ['span', { class: 'watch-card__poster' }, ['img', { src: '/p/' + item.ref }], ['span', { class: 'watch-card__title' }, item.title]],
        ['span', { class: 'watch-card__sub' }, 'Movie' + (item.ai_pick ? ' AI' : '')]]]),
  };
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch/watchnext.js'), 'utf8'), ctx);

  const p = {
    doc, ctx, asked, timers, store, observers, scrolls, toasts,
    grid: doc.getElementById('watch-next-grid'),
    sec: doc.getElementById('watch-next'),
    tail: doc.getElementById('watch-next-tail'),
    fire: (type, e) => (listeners[type] || []).forEach((fn) => fn(e || {})),
    frames: () => { while (frames.length) frames.shift()(); },
    // The person scrolls to *y*, a while after the last scroll.
    scroll: (y, ms) => { ctx.clock += ms || 1000; ctx.scrollY = y; p.fire('scroll'); p.frames(); },
    refs: () => p.grid.querySelectorAll('.watch-card__link').map((a) => a.getAttribute('data-title-ref')),
    skels: () => p.grid.children.filter((n) => n.classList.contains('watch-card--skel')),
    answer: async (data) => { asked[asked.length - 1].resolve(data); await settle(); p.frames(); },
    params: (i) => new URLSearchParams(asked[i].url.split('?')[1]),
  };
  p.frames();
  return p;
}

/* *n* titles from *from*, each its own TMDB film. */
function titles(from, n, extra) {
  return Array.from({ length: n }, (_, i) => ({
    ref: 'tmdb-movie-' + (from + i), title: 'Film ' + (from + i), kind: 'movie', year: 2000,
    reason: { kind: 'trending', text: 'Trending this week', short: 'Trending', why: 'Most watched this week.' }, ...extra,
  }));
}
const pageOf = (from, n, more) => ({ items: titles(from, n), cursor: 'c' + (from + n), personal: true, tmdb_configured: true, ...more });
// The scroll that brings the end of the section's cards within reach.
const near = (p) => p.scroll(GRID_TOP + p.grid.children.length / p.ctx.cols * PITCH - VIEW_H);

const scenarios = {
  // Nothing is asked as the page opens; a scroll far from the end asks nothing; near it, the head.
  async firstOnlyNear() {
    const p = page();
    const onLoad = p.asked.length;
    p.scroll(1000);
    const far = p.asked.length;
    near(p);
    // Opened with the end of the rows in reach, it still waits for a scroll; a page too short to scroll asks once loaded.
    const reach = page({ scrollY: GRID_TOP - VIEW_H });
    reach.doc.documentElement.scrollHeight = 20000;
    reach.fire('load');
    const short = page({ scrollY: GRID_TOP - VIEW_H });
    short.doc.documentElement.scrollHeight = VIEW_H;
    short.fire('load');
    return {
      onLoad, far, near: p.asked.length, type: p.params(0).get('type'), cursor: p.params(0).get('cursor'), skels: p.skels().length,
      inReach: reach.asked.length, short: short.asked.length,
    };
  },

  // While a page is on its way no other is asked; its answer's cursor asks for the one after.
  async oneAtATime() {
    const p = page();
    near(p);
    p.scroll(p.ctx.scrollY + 50);
    p.scroll(p.ctx.scrollY + 50);
    const during = p.asked.length;
    await p.answer(pageOf(0, 24));
    near(p);
    return { during, after: p.asked.length, cursor: p.params(1).get('cursor'), cards: p.refs().length, skels: p.skels().length };
  },

  // A title the grid holds comes once, from the same page or a later one.
  async noTitleTwice() {
    const p = page();
    near(p);
    const first = pageOf(0, 6);
    first.items.push(titles(2, 1)[0]);
    await p.answer(first);
    near(p);
    await p.answer(pageOf(4, 6));
    return p.refs();
  },

  // A switch of the filter starts again from the head of that kind; what the old one answers late is dropped.
  async filterDropsTheOldAnswer() {
    const p = page();
    near(p);
    await p.answer(pageOf(0, 6));
    near(p);
    const old = p.asked[1];
    click(p.doc, p.doc.querySelector('[data-wn-filter="movie"]'));
    const dimmed = p.sec.classList.contains('is-switching');
    old.resolve(pageOf(100, 6));
    await settle();
    const kept = p.refs();
    await p.answer(pageOf(200, 3));
    return {
      type: p.params(2).get('type'), cursor: p.params(2).get('cursor'), dimmed, kept, refs: p.refs(),
      pressed: p.doc.querySelectorAll('[data-wn-filter]').map((b) => b.getAttribute('aria-pressed')),
      dim: p.sec.classList.contains('is-switching'),
    };
  },

  // A failure holds only the rest of the last row, says so with Try again, and comes back by itself online.
  async failsThenOnline() {
    const p = page({ cols: 5 });
    near(p);
    await p.answer(pageOf(0, 24));
    near(p);
    const loading = p.skels().length;
    p.asked[1].reject(Object.assign(new Error('no'), { status: 503 }));
    await settle();
    const line = p.tail.textContent;
    const held = p.skels().filter((s) => !s.classList.contains('is-spare')).length;
    p.scroll(p.ctx.scrollY + 10);
    const quiet = p.asked.length;
    p.fire('online');
    const again = { asked: p.asked.length, cursor: p.params(2).get('cursor'), busy: p.tail.querySelector('button').getAttribute('aria-busy') };
    await p.answer(pageOf(24, 5));
    return { loading, line, held, quiet, again, after: p.tail.textContent, cards: p.refs().length, static: p.grid.classList.contains('is-static') };
  },

  // An answer that says TMDB failed shows the line with what it brought, asks nothing more by itself, and never ends the section.
  async failedAnswer() {
    const p = page({ cols: 5 });
    near(p);
    await p.answer(pageOf(0, 24));
    near(p);
    await p.answer(pageOf(24, 3, { failed: true }));
    const line = p.tail.textContent;
    const held = p.skels().filter((s) => !s.classList.contains('is-spare')).length;
    const cards = p.refs().length;
    p.scroll(p.ctx.scrollY + 10);
    near(p);
    const quiet = p.asked.length;
    for (let i = 0; i < 4; i++) {
      click(p.doc, p.tail.querySelector('button'));
      await p.answer({ items: [], cursor: 'c27', personal: true, tmdb_configured: true, failed: true });
    }
    const button = p.tail.querySelector('button');
    return {
      line, held, cards, quiet, asked: p.asked.length, toasts: p.toasts.length, gone: p.sec.hasAttribute('hidden'),
      still: p.tail.textContent, busy: button.getAttribute('aria-busy'), cursor: p.params(p.asked.length - 1).get('cursor'),
    };
  },

  // A first page that failed says no titles came, and stays to be tried again.
  async failedFirstPage() {
    const p = page();
    near(p);
    await p.answer({ items: [], cursor: 'c0', personal: true, tmdb_configured: true, failed: true });
    const line = p.tail.textContent;
    p.fire('online');
    const asked = p.asked.length;
    await p.answer(pageOf(0, 6));
    return { line, gone: p.sec.hasAttribute('hidden'), asked, after: p.tail.textContent, cards: p.refs().length };
  },

  // A cursor that started again from the head starts the grid again, at the section's top.
  async restarted() {
    const p = page();
    near(p);
    await p.answer(pageOf(0, 24));
    near(p);
    await p.answer(pageOf(24, 24));
    near(p);
    const deep = p.ctx.scrollY;
    await p.answer(pageOf(0, 24, { cursor: 'r24', restarted: true }));
    const refs = p.refs();
    near(p);
    return { deep, cards: refs.length, first: refs[0], top: p.scrolls[p.scrolls.length - 1], cursor: p.params(p.asked.length - 1).get('cursor') };
  },

  // Too many requests: it waits, says nothing, and asks again.
  async waitsAfterTooMany() {
    const p = page();
    near(p);
    p.asked[0].reject(Object.assign(new Error('busy'), { status: 429 }));
    await settle();
    const waiting = { line: p.tail.textContent, ms: p.timers.map((t) => t.ms) };
    p.timers.find((t) => t.ms === 5000).fn();
    return { waiting, asked: p.asked.length };
  },

  // Answers with nothing new: a few and it stops asking; nothing ever given, the section goes.
  async stopsWhenDry() {
    const p = page();
    for (let i = 0; i < 5; i++) {
      near(p);
      if (p.asked.length > i) await p.answer({ items: [], cursor: 'c', personal: true, tmdb_configured: true });
    }
    const q = page();
    near(q);
    await q.answer({ items: [], cursor: 'c', personal: true, tmdb_configured: false });
    near(q);
    return { asked: p.asked.length, gone: p.sec.hasAttribute('hidden'), noTmdb: { asked: q.asked.length, gone: q.sec.hasAttribute('hidden') } };
  },

  // A hidden section (a search or a grid open over the rows) asks for nothing.
  async hiddenAsksNothing() {
    const p = page();
    p.doc.getElementById('watch-browse').setAttribute('hidden', '');
    near(p);
    return p.asked.length;
  },

  // Each card says why, in the server's words as text; only a reason it knows is drawn, and the preview's words are on its link.
  async reasons() {
    const p = page();
    near(p);
    const evil = '<img src=x onerror=alert(1)>';
    await p.answer({
      items: [
        { ref: 'tmdb-movie-1', title: 'A', reason: { kind: 'search', text: 'Because you searched “' + evil + '”', short: '“' + evil + '”', why: 'You searched ' + evil } },
        { ref: 'rec-2', pick_ref: 'rec-2', ai_pick: true, title: 'B', reason: { kind: 'ai', text: 'AI pick', short: 'AI pick', why: 'Picked for you.' } },
        { ref: 'tmdb-movie-3', title: 'C', reason: { kind: 'made-up', text: 'Whatever' } },
      ],
      cursor: 'c', personal: false, tmdb_configured: true,
    });
    const [a, b, c] = p.grid.querySelectorAll('.watch-card');
    const link = (n) => n.querySelector('.watch-card__link');
    return {
      text: a.querySelector('.wn-why__long').textContent, imgs: a.querySelectorAll('img').length,
      icon: a.querySelector('.wn-why .ico').getAttribute('data-icon'),
      why: [link(a).getAttribute('data-why'), link(a).getAttribute('data-why-label'), link(a).getAttribute('data-why-icon')],
      ai: { line: !!b.querySelector('.wn-why--ai'), why: link(b).getAttribute('data-why'), sub: b.querySelector('.watch-card__sub').textContent },
      unknown: !!c.querySelector('.wn-why'),
      note: !p.doc.querySelector('.wn__note').hasAttribute('hidden'),
    };
  },

  // Cards come in a step later for each column and row; on a fast flick they are simply there.
  async arrival() {
    const p = page();
    near(p);
    await p.answer(pageOf(0, 9));
    const seen = p.observers.find((o) => /-4%/.test(o.margin));
    const cards = p.grid.querySelectorAll('.watch-card');
    const pending = cards.every((c) => c.classList.contains('is-pending'));
    seen.fn(cards.slice(0, 6).map((target) => ({ target, isIntersecting: true })));
    const delays = cards.slice(0, 6).map((c) => c.style['--wn-delay']);
    p.scroll(p.ctx.scrollY + 900, 100); // 9 px a ms
    seen.fn(cards.slice(6).map((target) => ({ target, isIntersecting: true })));
    const flicked = cards.slice(6).map((c) => [c.classList.contains('is-pending'), c.classList.contains('is-in')]);
    return { pending, delays, flicked };
  },

  // What the page held is kept on leaving, for its own entry and person.
  async keptOnLeaving() {
    const p = page({ entry: 'e1', who: 'ann' });
    near(p);
    await p.answer(pageOf(0, 6));
    click(p.doc, p.grid.querySelectorAll('.watch-card__link')[2]);
    p.fire('pagehide');
    const kept = JSON.parse(p.store.get('mm:watch:next'));
    return { id: kept.id, who: kept.who, filter: kept.filter, cursor: kept.cursor, items: kept.items.length, opened: kept.opened };
  },

  // Past KEEP titles only the latest are kept, from the start of a row, each with only what its card is made of,
  // and the rows before them are kept as their height.
  async keptAreCapped() {
    const p = page({ entry: 'e1', cols: 4 });
    for (let i = 0; i < 26; i++) {
      near(p);
      await p.answer({ ...pageOf(i * 24, 24), items: titles(i * 24, 24, { overview: 'x'.repeat(400), cast: ['a', 'b'] }) });
    }
    // Every row in the grid, then deep in with the rows far above taken out.
    p.scroll(GRID_TOP);
    p.fire('pagehide');
    const grid = JSON.parse(p.store.get('mm:watch:next'));
    p.scroll(GRID_TOP + 150 * PITCH);
    const shed = parseFloat(p.grid.style['--wn-shed']);
    p.fire('pagehide');
    const deep = JSON.parse(p.store.get('mm:watch:next'));
    const fields = Object.keys(grid.items[0]).sort();
    return {
      items: grid.items.length, first: grid.items[0].ref, room: grid.room, fields, reason: grid.items[0].reason.text,
      deep: { items: deep.items.length, first: deep.items[0].ref, room: deep.room, shed },
    };
  },

  // Laid out again under the room of the rows not kept, their titles where they stood.
  async putBackUnderRoom() {
    const kept = { id: 'e1', who: 'ann', filter: 'all', cursor: 'c9', items: titles(600, 8), room: 1800, opened: '', tmdb: true, fresh: false };
    const p = page({ entry: 'e1', who: 'ann', cols: 4, stored: { 'mm:watch:next': JSON.stringify(kept) } });
    return { room: p.grid.style['--wn-shed'], top: p.grid.querySelector('.watch-card').getBoundingClientRect().top + p.ctx.scrollY };
  },

  // Loaded again at the same entry, the titles are laid out with nothing asked, and go on from the kept cursor.
  async putBack() {
    const kept = { id: 'e1', who: 'ann', filter: 'tv', cursor: 'c9', items: titles(0, 9), opened: 'tmdb-movie-4', tmdb: true, fresh: false };
    const stored = { 'mm:watch:next': JSON.stringify(kept) };
    const p = page({ entry: 'e1', who: 'ann', stored });
    const laid = { refs: p.refs().length, asked: p.asked.length, pending: p.grid.querySelectorAll('.is-pending').length };
    const pressed = p.doc.querySelector('[data-wn-filter="tv"]').getAttribute('aria-pressed');
    (p.doc.listeners.DOMContentLoaded || []).forEach((fn) => fn());
    const focus = p.doc.activeElement.getAttribute('data-title-ref');
    near(p);
    const other = page({ entry: 'e1', who: 'bob', stored }).refs().length;
    const elsewhere = page({ entry: 'e2', who: 'ann', stored }).refs().length;
    return { laid, pressed, focus, type: p.params(0).get('type'), cursor: p.params(0).get('cursor'), other, elsewhere };
  },

  // Past 600 titles whole rows far above go, standing as room of their height; on the way up they are built again.
  async rowsFarAboveGo() {
    const p = page({ cols: 4 });
    for (let i = 0; i < 26; i++) {
      near(p);
      await p.answer(pageOf(i * 24, 24));
    }
    const all = 26 * 24;
    p.scroll(GRID_TOP + 150 * PITCH);
    const left = p.grid.querySelectorAll('.watch-card').length;
    const room = parseFloat(p.grid.style['--wn-shed']);
    const firstTop = p.grid.querySelector('.watch-card').getBoundingClientRect().top;
    p.scroll(GRID_TOP);
    const back = p.refs();
    return {
      all, shedWhole: (all - left) % 4 === 0 && left < all, roomIsRows: room === (all - left) / 4 * PITCH,
      edge: firstTop > -10 * VIEW_H - PITCH && firstTop < -10 * VIEW_H + PITCH, backAll: back.length === all,
      inOrder: back.every((r, i) => r === 'tmdb-movie-' + i), room: p.grid.style['--wn-shed'],
    };
  },

  // Two screens into the section a button goes back to the top and to the wordmark.
  async backToTop() {
    const p = page();
    p.scroll(SEC_TOP + VIEW_H);
    const short = p.doc.getElementById('watch-next-top').classList.contains('is-on');
    p.scroll(SEC_TOP + 2.5 * VIEW_H);
    const button = p.doc.getElementById('watch-next-top');
    const deep = [button.classList.contains('is-on'), button.getAttribute('aria-hidden')];
    click(p.doc, button);
    return { short, deep, to: p.scrolls[p.scrolls.length - 1], focus: p.doc.activeElement.className };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
