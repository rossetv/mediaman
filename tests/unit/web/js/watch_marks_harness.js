// Runs watch/core.js, keep.js and marks.js against the fake DOM and prints one scenario's outcome as
// JSON. Usage: node watch_marks_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});

function card(mark, title, sub, over = {}) {
  const at = /^(movie|show)\/(\d+)$/.exec(mark);
  const ref = over.ref || (at && at[1] + '-' + at[2]);
  return ['article', { class: 'watch-card', 'data-mark': mark, ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', ...(ref && !over.dismiss ? { 'data-title-ref': ref } : {}) },
      ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, title],
        ...(over.watched ? [['span', { class: 'watch-badge watch-badge--watched' }, 'Watched']] : []),
        ...(over.bar ? [['progress', { class: 'watch-bar' }]] : [])],
      ['span', { class: 'watch-card__sub' }, sub]],
    ['button', { class: 'watch-more', 'data-watch-more': '' }]];
}

function row(title, cards) {
  return ['section', { class: 'watch-row', id: title }, ['div', { class: 'watch-row__scroll' }, ...cards]];
}

function banner(ref, title, play) {
  return ['section', { class: 'watch-banner', ...(ref ? { 'data-dismiss-ref': ref } : {}) },
    ['h1', { class: 'watch-banner__title' }, title], ['a', { class: 'watch-btn--play', href: play }, 'Resume'],
    ...(ref ? [['button', { 'data-watch-dismiss': ref, 'data-mark-name': title }, 'Dismiss']] : [])];
}

const FILM = (over) => card('movie/1', 'Fieldwork', 'Movie · 31 min left', { bar: true, ...over });
const CONTINUE = [
  card('movie/1', 'Fieldwork', 'Movie · 31 min left', { dismiss: 'movie-1', bar: true }),
  card('episode/22', 'Brightwater', 'S1 · E2 · 12 min left', { dismiss: 'show-2', bar: true }),
  card('movie/3', 'Salt', 'Movie · 5 min left', { dismiss: 'movie-3', bar: true }),
];

function watchPage(parts) {
  return ['div', { class: 'watch-page', id: 'watch-page' }, ['input', { id: 'watch-search' }],
    ['div', { id: 'watch-browse' }, ...parts]];
}

/* *page*: the page shown; *next*: the page the server sends after a change (it sends *page* after an
   Undo), or a function of the fetch's number giving it, at once or as a promise; *api*: "METHOD url" to
   its answer, or a function giving it. */
function load(page, opts = {}) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, page));
  const calls = [];
  const toasts = [];
  const events = [];
  const fetched = [];
  doc.dispatchEvent = (e) => { events.push(e.detail.ref); };
  const call = (method) => (url) => {
    calls.push(method + ' ' + url);
    let answer = (opts.api || {})[method + ' ' + url];
    if (typeof answer === 'function') answer = answer();
    if (answer instanceof Promise) return answer;
    if (typeof answer === 'number') return Promise.reject(Object.assign(new Error('http'), { status: answer }));
    return Promise.resolve(answer || { ok: true, changed: 1, watched: method === 'POST' });
  };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, WeakMap, Error, parseFloat, parseInt,
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block', paddingTop: '0' }),
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    innerHeight: 900,
    innerWidth: 1280,
    location: { pathname: '/watch/show/2', search: '' },
    DOMParser: class { parseFromString(text) { const d = makeDocument(); d.body.appendChild(build(d, JSON.parse(text))); return d; } },
    fetch: (url) => {
      fetched.push(url);
      if (opts.onFetch) opts.onFetch(doc);
      if (!opts.next) return Promise.reject(new Error('down'));
      const undone = /\?undo=1$|^DELETE .*dismissed/.test(calls.filter((c) => !/^GET /.test(c)).pop());
      const sent = typeof opts.next === 'function' ? opts.next(fetched.length, calls) : undone ? page : opts.next;
      return Promise.resolve(sent).then((spec) => ({ ok: true, redirected: !!opts.redirected, text: () => Promise.resolve(JSON.stringify(spec)) }));
    },
    UIFeedback: { toast: (message, o) => toasts.push({ message, variant: o.variant, undo: o.action && o.action.onClick }) },
    MM: { api: { get: call('GET'), post: call('POST'), delete: call('DELETE'), messageOf: (_e, f) => f } },
    addEventListener() {},
    scrollBy() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/keep.js', 'watch/marks.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const cards = (id) => doc.querySelectorAll('.watch-card').filter((c) => !id || c.closest('#' + id));
  const p = {
    ctx, doc, calls, toasts, events, fetched, cards,
    look: (id) => cards(id).map((c) => [c.getAttribute('data-mark'), !!c.querySelector('.watch-badge--watched'),
      c.querySelector('.watch-card__sub').textContent, !!c.querySelector('.watch-bar')]),
    marks: (id) => cards(id).map((c) => c.getAttribute('data-mark')),
    menu: () => doc.querySelectorAll('#watch-more-menu .watch-keep-menu__item').map((i) => i.textContent),
    open(c) { const m = c.querySelector('[data-watch-more]'); m.focus(); click(doc, m); },
    async pick(c, act) {
      p.open(c);
      click(doc, doc.querySelector('#watch-more-menu [data-act="' + act + '"]'));
      await settle();
    },
    async undo() { toasts[toasts.length - 1].undo(); await settle(); },
    banner: () => doc.querySelectorAll('.watch-banner__title').map((t) => t.textContent),
    focus: () => { const a = doc.activeElement; const c = a.closest('.watch-card'); return c ? c.getAttribute('data-mark') : a.className || a.tagName; },
  };
  return p;
}

function later() {
  let send;
  const answer = new Promise((r) => { send = r; });
  return { answer, send };
}

/* A show's own page whose episodes *watched* are watched, of 21, 22 and 23. */
function episodesPage(watched, play = '/watch/play/21') {
  return ['div', { id: 'watch-page' }, ['div', { class: 'watch-hero__info' }, ['a', { class: 'watch-btn--play', href: play }, play]],
    ['section', { class: 'watch-episodes' }, ...[21, 22, 23].map((n) => ['button', { 'data-mark-toggle': 'episode/' + n, 'data-mark-name': 'E' + n, 'aria-pressed': String(watched.includes(n)) }])]];
}
async function press(p, n) {
  const b = p.doc.querySelector('[data-mark-toggle="episode/' + n + '"]');
  click(p.doc, b);
  await settle();
  return b;
}
function pressed(p) { return p.doc.querySelectorAll('[data-mark-toggle]').map((b) => b.getAttribute('aria-pressed')); }

const scenarios = {
  // What the one menu offers for each kind of card.
  async menus() {
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [FILM(), card('show/2', 'Brightwater', 'TV', { watched: true }),
      card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]));
    return p.cards().map((c) => { p.open(c); return p.menu(); });
  },

  // A film marked from its card, then Undo.
  async markFilm() {
    const p = load(watchPage([row('lib', [FILM()]), row('rec', [FILM(), card('movie/9', 'Other', 'Movie')])]));
    await p.pick(p.cards('lib')[0], 'mark');
    const marked = p.look();
    const menuNow = (p.open(p.cards('rec')[0]), p.menu());
    await p.undo();
    return { marked, menuNow, undone: p.look(), calls: p.calls, toasts: p.toasts.map((t) => t.message), fetched: p.fetched, events: p.events };
  },

  // Undo refused: the change stands and it says so.
  async undoRefused() {
    const p = load(watchPage([row('lib', [FILM()])]), { api: { 'DELETE /api/watch/watched/movie/1?undo=1': 409 } });
    await p.pick(p.cards()[0], 'mark');
    await p.undo();
    return { look: p.look(), variant: p.toasts[1].variant, busy: p.cards()[0].querySelector('[data-watch-more]').getAttribute('aria-disabled') };
  },

  // A mark that fails, then one that changed nothing.
  async markFails() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · 3 left')])]), { api: { 'POST /api/watch/watched/show/2': 503 } });
    await p.pick(p.cards()[0], 'mark');
    const more = p.cards()[0].querySelector('[data-watch-more]');
    const q = load(watchPage([row('lib', [FILM()])]), { api: { 'POST /api/watch/watched/movie/1': { ok: true, changed: 0, watched: true } } });
    await q.pick(q.cards()[0], 'mark');
    return { look: p.look(), toast: p.toasts[0].message, free: more.getAttribute('aria-disabled'), noChange: { look: q.look(), undo: !!q.toasts[0].undo } };
  },

  // While a change is on its way its buttons say so and a second press does nothing.
  async oneAtATime() {
    let answer;
    const p = load(['div', { id: 'watch-page' }, ['button', { 'data-mark-toggle': 'movie/1', 'data-mark-name': 'Fieldwork', 'aria-pressed': 'false' }]],
      { api: { 'POST /api/watch/watched/movie/1': new Promise((r) => { answer = r; }) } });
    const b = p.doc.querySelector('[data-mark-toggle]');
    click(p.doc, b);
    const during = [b.getAttribute('aria-busy'), b.getAttribute('aria-pressed')];
    click(p.doc, b);
    answer({ ok: true, changed: 1, watched: true });
    await settle();
    return { during, calls: p.calls.length, after: [b.getAttribute('aria-busy'), b.getAttribute('aria-pressed')] };
  },

  // A show's change is its season cards' too; another show's stay.
  async markShow() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · 3 left')]),
      row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' }), card('season/91', 'Else', 'Season 1', { ref: 'show-9' })])]));
    await p.pick(p.cards()[0], 'mark');
    const marked = p.look();
    await p.undo();
    return { marked, undone: p.look() };
  },

  // A season marked: its show's card says what the server says is left.
  async markSeason() {
    const show = (sub) => row('lib', [card('show/2', 'Brightwater', sub)]);
    const p = load(watchPage([show('TV · 2 seasons · 9 left'), row('disc', [card('show/2', 'Brightwater', 'TV · 2016')]), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]),
      { next: watchPage([show('TV · 2 seasons · 4 left'), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2', watched: true })])]) });
    await p.pick(p.cards('leave')[0], 'mark');
    const marked = { look: p.look(), toast: p.toasts[0].message };
    await p.undo();
    return { marked, undone: p.look() };
  },

  // A season marked whose show's card stands past the cards the server's page has.
  async markSeasonPastThePage() {
    const go = async (search) => {
      const p = load(watchPage([row('lib', [card('show/2', 'Brightwater & Co', 'TV · 2 seasons · 9 left')]), row('disc', [card('show/2', 'Brightwater & Co', 'TV · 2016')]),
        row('leave', [card('season/21', 'Brightwater & Co', 'Season 1', { ref: 'show-2' })])]),
        { next: watchPage([row('lib', [card('show/9', 'Other', 'TV')])]), api: { 'GET /api/watch/search?q=Brightwater%20%26%20Co': search } });
      await p.pick(p.cards('leave')[0], 'mark');
      const marked = p.look();
      await p.undo();
      return { marked, undone: p.look() };
    };
    const item = (ref, subtitle, watched) => ({ ref, title: 'Brightwater & Co', subtitle, watched });
    return {
      found: await go({ items: [item('show-7', 'TV · 1 season · 2 left', false), item('show-2', 'TV · 2 seasons · 4 left', false)] }),
      finished: await go({ items: [item('show-2', 'TV · 2 seasons', true)] }),
      failed: await go(500),
    };
  },

  // Dismissed from its card, in the middle of the row: focus goes on, Undo puts it back.
  async dismiss() {
    const p = load(watchPage([row('cont', CONTINUE)]));
    await p.pick(p.cards()[1], 'dismiss');
    const gone = { marks: p.marks(), focus: p.focus(), calls: p.calls.slice(), toast: p.toasts[0].message };
    p.doc.activeElement = p.doc.body;
    await p.undo();
    return { gone, back: p.marks(), focus: p.focus(), call: p.calls[p.calls.length - 1] };
  },

  async dismissRefused() {
    const p = load(watchPage([row('cont', CONTINUE)]), { api: { 'POST /api/watch/dismissed/movie-1': 500 } });
    await p.pick(p.cards()[0], 'dismiss');
    return { marks: p.marks(), variant: p.toasts[0].variant };
  },

  // Dismissed in the banner: the next one takes its place, and the title's card goes too.
  async dismissBanner() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    const b = p.doc.querySelector('[data-watch-dismiss]');
    b.focus();
    click(p.doc, b);
    await settle();
    const gone = { banner: p.banner(), marks: p.marks(), focus: p.focus(), calls: p.calls.slice() };
    await p.undo();
    return { gone, back: { banner: p.banner(), marks: p.marks() } };
  },

  // The last title dismissed: with no banner on the server's page, it folds away and comes back.
  async lastBanner() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE.slice(0, 1))]), { next: watchPage([]) });
    click(p.doc, p.doc.querySelector('[data-watch-dismiss]'));
    await settle();
    const gone = { banner: p.banner(), marks: p.marks() };
    await p.undo();
    return { gone, back: { banner: p.banner(), marks: p.marks() } };
  },

  // A film in Continue watching marked watched leaves it; an episode gives way to the next one, or leaves.
  async markContinue() {
    const next = [card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2' }), CONTINUE[2]];
    const p = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', next)]) });
    await p.pick(p.cards()[0], 'mark');
    const film = p.marks();
    await p.pick(p.cards()[0], 'mark');
    const episode = { marks: p.marks(), focus: p.focus(), toast: p.toasts[1].message };
    await p.undo();
    const undone = p.marks();
    const q = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', [CONTINUE[2]])]) });
    await q.pick(q.cards()[1], 'mark');
    return { film, episode, undone, last: q.marks() };
  },

  // Undone before the server's page arrives: what it says is dropped.
  async undoneFirst() {
    let p;
    p = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', [CONTINUE[2]])]), onFetch: () => p.toasts[0].undo() });
    await p.pick(p.cards()[1], 'mark');
    return p.marks();
  },

  // Focus stays in Details while it is open over the rows.
  async underDetails() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { id: 'ttl' }, ['button', { class: 'in-details', 'data-mark-toggle': 'movie/1', 'data-mark-name': 'Fieldwork', 'aria-pressed': 'false' }]]]));
    p.doc.documentElement.classList.add('has-ttl');
    const b = p.doc.querySelector('[data-mark-toggle]');
    b.focus();
    click(p.doc, b);
    await settle();
    const out = { marks: p.marks(), focus: p.focus(), events: p.events };
    p.doc.activeElement = p.doc.body;
    await p.undo();
    const afterUndo = p.focus();
    // Pressed by touch, where a button may not take the focus: the rows beneath do not take it either.
    click(p.doc, b);
    p.doc.activeElement = p.doc.body;
    await settle();
    return { ...out, afterUndo, touched: p.focus() };
  },

  // The show's own page follows an episode's mark: Play, the tabs and every toggle.
  async showPage() {
    const page = (watched, selected, play) => ['div', { id: 'watch-page' },
      ['div', { class: 'watch-hero__info' },
        ['div', { class: 'watch-actions' }, ['a', { class: 'watch-btn--play', href: play }, play],
          ['button', { 'data-mark-toggle': 'show/2', 'data-mark-name': 'Brightwater', 'aria-pressed': String(watched) }]],
        ['nav', { class: 'watch-seasons' }, ...[1, 2].map((n) => ['a', { class: 'watch-pill' + (n === selected ? ' on' : ''), href: '/s?season=' + n,
          ...(watched && n === 1 ? { 'aria-label': 'Season 1, watched' } : {}) }, 'Season ' + n, ...(watched && n === 1 ? [['svg', { class: 'watch-pill__mark' }]] : [])])]],
      ['section', { class: 'watch-episodes' }, ['ol', {}, ['li', {},
        ['a', { class: 'watch-ep' + (watched ? '' : ' watch-ep--in-progress'), 'data-ep': '21' }, ['span', { class: 'watch-ep__thumb' }, ...(watched ? [] : [['progress', { class: 'watch-bar' }]])],
          ['span', { class: 'watch-ep__text' }, watched ? 'One, watched' : 'One'], ...(watched ? [] : [['span', { class: 'watch-ep__state' }, 'In progress']])],
        ['button', { 'data-mark-toggle': 'episode/21', 'data-mark-name': 'One', 'aria-pressed': String(watched), title: 'Mark as watched' }]]]]];
    const run = async (onFetch) => {
      const p = load(page(false, 1, '/watch/play/21'), { next: page(true, 1, '/watch/play/22'), onFetch });
      const b = p.doc.querySelector('[data-mark-toggle="episode/21"]');
      b.focus();
      click(p.doc, b);
      await settle();
      const pill = p.doc.querySelectorAll('.watch-pill')[0];
      const ep = p.doc.querySelector('.watch-ep');
      return { fetched: p.fetched, play: p.doc.querySelector('.watch-btn--play').getAttribute('href'),
        show: p.doc.querySelector('[data-mark-toggle="show/2"]').getAttribute('aria-pressed'), pill: [pill.getAttribute('aria-label'), pill.textContent, !!pill.querySelector('.watch-pill__mark')],
        episode: [b.getAttribute('aria-pressed'), b.getAttribute('title'), ep.textContent, ep.classList.contains('watch-ep--in-progress'), !!ep.querySelector('.watch-bar')], focus: p.doc.activeElement === b };
    };
    const other = await run((doc) => { const t = doc.querySelectorAll('.watch-pill'); t[0].classList.remove('on'); t[1].classList.add('on'); });
    return { same: await run(), otherSeason: [other.play, other.show] };
  },

  // The show's page asked for after one mark lands after the next mark's.
  async lateTitlePage() {
    const first = later();
    const p = load(episodesPage([]), { next: (n) => (n === 1 ? first.answer : episodesPage([21, 22], '/watch/play/23')) });
    await press(p, 21);
    await press(p, 22);
    first.send(episodesPage([21], '/watch/play/22'));
    await settle();
    return { pressed: pressed(p), play: p.doc.querySelector('.watch-btn--play').getAttribute('href') };
  },

  // The page asked for after a mark lands after its Undo's.
  async lateTitlePageUndone() {
    const first = later();
    const p = load(episodesPage([]), { next: (n) => (n === 1 ? first.answer : episodesPage([])) });
    await press(p, 21);
    await p.undo();
    first.send(episodesPage([21], '/watch/play/22'));
    await settle();
    return { pressed: pressed(p), play: p.doc.querySelector('.watch-btn--play').getAttribute('href') };
  },

  // The Watch page asked for after an episode's mark lands after a film's mark.
  async lateRows() {
    const first = later();
    const now = watchPage([row('cont', [CONTINUE[0], card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2' }), CONTINUE[2]]), row('lib', [card('movie/9', 'Other', 'Movie')])]);
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [card('movie/9', 'Other', 'Movie')])]), { next: (n) => (n === 1 ? first.answer : now) });
    await p.pick(p.cards('cont')[1], 'mark');
    await p.pick(p.cards('lib')[0], 'mark');
    first.send(watchPage([row('cont', CONTINUE)]));
    await settle();
    return p.marks('cont');
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });
