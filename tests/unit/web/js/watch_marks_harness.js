// Runs watch/core.js, keep.js, ratings.js and marks.js against the fake DOM and prints one scenario's outcome as
// JSON. Usage: node watch_marks_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

// As in a browser, a node taken off the page loses the focus.
['remove', 'replaceWith'].forEach((name) => {
  const own = Node.prototype[name];
  Node.prototype[name] = function (...args) {
    own.apply(this, args);
    const doc = this.ownerDocument;
    if (this.contains(doc.activeElement) && !doc.body.contains(this)) doc.activeElement = doc.body;
  };
});

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});

function card(mark, title, sub, over = {}) {
  const at = /^(movie|show)\/(\d+)$/.exec(mark);
  const ref = over.ref || (at && at[1] + '-' + at[2]);
  return ['article', { class: 'watch-card', 'data-mark': mark, ...(over.dismiss ? { 'data-dismiss-ref': over.dismiss } : {}) },
    ['a', { class: 'watch-card__link', ...(ref && !over.dismiss ? { 'data-title-ref': ref } : {}),
      ...(over.pick ? { 'data-pick-ref': over.pick, 'data-kind': 'movie', 'data-tmdb-id': over.tmdb || '' } : {}) },
      ['span', { class: 'watch-card__poster' }, ['span', { class: 'watch-card__title' }, title],
        ...(over.watched ? [['span', { class: 'watch-badge watch-badge--watched' }, 'Watched']] : []),
        ...(over.bar ? [['progress', { class: 'watch-bar' }]] : [])],
      ['span', { class: 'watch-card__sub' }, sub]],
    ['button', { class: 'watch-more', 'data-watch-more': '' }]];
}

function row(title, cards) {
  return ['section', { class: 'watch-row', id: title }, ['div', { class: 'watch-row__viewport' }, ['div', { class: 'watch-row__scroll' }, ...cards]]];
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
  doc.dispatchEvent = (e) => {
    if (e.type === 'mm:rating') doc.listeners['mm:rating'].forEach((fn) => fn(e));
    else events.push(e.detail.ref);
  };
  const gone = [];
  const watchers = new Set();
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
    UIFeedback: {
      toast: (message, o) => { toasts.push({ message, variant: o.variant, undo: o.action && o.action.onClick, dialog: o.dialog ? o.dialog.id : null }); return message; },
      dismiss: (message) => gone.push(message),
    },
    MutationObserver: class { constructor(fn) { this.fn = fn; } observe() { watchers.add(this); } disconnect() { watchers.delete(this); } },
    MM: { api: { get: call('GET'), post: call('POST'), put: (url, body) => call('PUT')(url + ' ' + body.level), delete: call('DELETE'), messageOf: (_e, f) => f } },
    addEventListener() {},
    scrollBy() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/keep.js', 'ratings.js', 'watch/marks.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const cards = (id) => doc.querySelectorAll('.watch-card').filter((c) => !id || c.closest('#' + id));
  const started = [];
  ctx.MMWatch.rows = { init: (viewport) => started.push(viewport.className) };
  const p = {
    gone, changed() { [...watchers].forEach((w) => w.fn([])); },
    ctx, started, doc, calls, toasts, events, fetched, cards,
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
    async undo(n = toasts.length - 1) { toasts[n].undo(); await settle(); },
    // Dismissed from somewhere else than its card, as the hover preview does.
    dismissed(ref) { click(doc, doc.body.appendChild(build(doc, ['button', { 'data-watch-dismiss': ref, 'data-mark-name': ref }]))); },
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

/* The Watch page whose Continue watching row holds *cards*, or has no row at all. */
function continuePage(cards, show) {
  return watchPage([['div', { class: 'watch-rows' }, ...(cards.length ? [row('cont', cards)] : []), row('lib', [card('movie/9', 'Other', 'Movie'), ...(show ? [card('show/2', 'Brightwater', 'TV')] : [])])]]);
}
function episodeCard(key) { return card('episode/' + key, 'Brightwater', 'S1 · E' + (key - 20), { dismiss: 'show-2' }); }

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

// A caption's words, each with the class of the span it is in.
function pieces(node, kind = '') {
  return node.childNodes.flatMap((n) => {
    if (n.nodeType === 3) return [[kind, n.textContent]];
    const own = n.className.replace('watch-card__', '');
    return own === 'unit' ? [[own, n.getAttribute('data-unit')]] : pieces(n, own);
  });
}

// Recommended, with the film of the library among the picks, and the same film in the library's own row.
const PICKS = () => [card('movie/8', 'Before', 'Movie', { pick: 'rec-8', tmdb: '80' }), card('movie/1', 'Fieldwork', 'Movie', { pick: 'rec-1', tmdb: '10' }), card('movie/9', 'After', 'Movie', { pick: 'rec-9', tmdb: '90' })];
const RATE = 'PUT /api/watch/ratings/movie-1 ';
const FILM_10 = { ok: true, media_type: 'movie', tmdb_id: 10 };
function rated(api) {
  return load(watchPage([row('picks', PICKS()), row('lib', [FILM()])]), {
    api: { [RATE + '-2']: FILM_10, [RATE + '1']: FILM_10, 'DELETE /api/watch/ratings/movie-1': FILM_10, ...api },
  });
}
/* A click that goes no further up than where it is stopped. */
function clickThumb(doc, b) {
  let stopped = false;
  const e = { target: b, preventDefault() {}, stopPropagation() { stopped = true; } };
  for (let n = b; n && !stopped; n = n.parentNode) (n.listeners.click || []).forEach((fn) => fn(e));
  if (!stopped) doc.listeners.click.forEach((fn) => fn(e));
}

async function thumb(p, c, i) {
  p.open(c);
  clickThumb(p.doc, p.doc.querySelectorAll('#watch-more-menu .rate__b')[i]);
  await settle();
}

const scenarios = {
  // The rating in a card's More menu: a level, then the same one taken back.
  async menuRating() {
    const p = rated();
    const shut = () => p.doc.querySelector('#watch-more-menu').classList.contains('is-open');
    await thumb(p, p.cards('lib')[0], 2);
    const first = [shut(), p.calls.slice(), p.focus()];
    await thumb(p, p.cards('lib')[0], 2);
    return { first, back: [shut(), p.calls.slice(1)] };
  },

  // Up and down take the four as one stop; left and right go between them.
  async menuKeys() {
    const p = rated();
    p.open(p.cards('lib')[0]);
    const menu = p.doc.querySelector('#watch-more-menu');
    return ['ArrowUp', 'ArrowRight', 'ArrowRight', 'ArrowDown', 'Home', 'ArrowLeft', 'End'].map((key) => {
      menu.listeners.keydown.forEach((fn) => fn({ key, preventDefault() {}, stopPropagation() {} }));
      return p.doc.activeElement.getAttribute('aria-label') || p.doc.activeElement.textContent;
    });
  },

  // A strong no from the library's card, then Undo.
  async strongNo() {
    const p = rated();
    await thumb(p, p.cards('lib')[0], 2);
    await thumb(p, p.cards('lib')[0], 0);
    const out = { gone: p.marks(), toast: p.toasts[0].message };
    await p.undo();
    return { ...out, back: p.marks('picks'), calls: p.calls };
  },

  // From the pick's own card, with the keyboard; then Undo from its toast.
  async strongNoFocus() {
    const p = rated();
    await thumb(p, p.cards('picks')[1], 0);
    const next = p.focus();
    const toast = p.doc.body.appendChild(build(p.doc, ['div', { class: 'ui-toast' }, ['button', {}]]));
    toast.querySelector('button').focus();
    await p.undo();
    return { next, back: p.focus() };
  },

  // Named by another ref, in Details, which is then closed.
  async strongNoInDetails() {
    const p = load(watchPage([row('picks', [card('movie/8', 'Before', 'Movie', { pick: 'rec-8', tmdb: '80' }), card('movie/7', 'Pick', 'Movie', { ref: 'rec-7', pick: 'rec-7', tmdb: '70' })])]),
      { api: { 'PUT /api/watch/ratings/tmdb-movie-70 -2': { media_type: 'movie', tmdb_id: 70 } } });
    const details = p.doc.body.appendChild(build(p.doc, ['div', { id: 'ttl' }, ['button', { class: 'inside' }]]));
    p.doc.documentElement.classList.add('has-ttl');
    details.querySelector('button').focus();
    p.ctx.MM.ratings.set('tmdb-movie-70', -2);
    await settle();
    const open = [p.marks(), p.focus(), p.toasts[0].dialog];
    p.changed();
    details.classList.add('is-leaving');
    p.changed();
    return { open, closed: p.focus() };
  },

  async ratingFails() {
    const p = rated({ [RATE + '-2']: 500 });
    await thumb(p, p.cards('picks')[1], 0);
    return { picks: p.marks('picks'), toasts: p.toasts.map((t) => t.message) };
  },

  async undoFails() {
    const p = rated({ 'DELETE /api/watch/ratings/movie-1': 500 });
    await thumb(p, p.cards('picks')[1], 0);
    p.toasts[0].undo();
    const now = p.marks('picks');
    await settle();
    return { now, after: p.marks('picks'), toast: p.toasts[1].message };
  },

  // Rated otherwise elsewhere: it is back, and the toast that says it is gone goes.
  async secondThoughts() {
    const p = rated();
    await thumb(p, p.cards('picks')[1], 0);
    const gone = p.marks('picks');
    await thumb(p, p.cards('lib')[0], 2);
    return { gone, back: p.marks('picks'), dismissed: p.gone.length };
  },

  // The row was filled anew before the Undo, or the last pick took the row along.
  async undoLater() {
    const anew = rated();
    await thumb(anew, anew.cards('picks')[1], 0);
    anew.doc.querySelector('#picks .watch-row__viewport').replaceWith(build(anew.doc, row('fresh', PICKS().slice(0, 1))[2]));
    await anew.undo();
    const only = load(watchPage([row('picks', PICKS().slice(1, 2)), row('lib', [FILM()])]), { api: { [RATE + '-2']: FILM_10, 'DELETE /api/watch/ratings/movie-1': FILM_10 } });
    await thumb(only, only.cards('picks')[0], 0);
    const emptied = !!only.doc.querySelector('#picks');
    await only.undo();
    return { anew: anew.marks('picks'), emptied, back: only.marks('picks') };
  },

  // What the one menu offers for each kind of card.
  async menus() {
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [FILM(), card('show/2', 'Brightwater', 'TV', { watched: true }),
      card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]));
    return p.cards().map((c) => { p.open(c); return p.menu(); });
  },

  // The pieces of a caption.
  async captions() {
    const p = load(watchPage([row('lib', [FILM()])]));
    const sub = p.doc.querySelector('.watch-card__sub');
    return JSON.parse(process.argv[3]).map((text) => {
      p.ctx.MMWatch.core.caption(sub, text);
      return [sub.textContent, pieces(sub)];
    });
  },

  // The captions of the cards a script builds.
  async builtCaption() {
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2016')])]));
    vm.runInContext(fs.readFileSync(path.join(JS, 'watch/search.js'), 'utf8'), p.ctx);
    const sub = p.ctx.MMWatch.search.card({ title: 'Fieldwork', kind: 'movie', subtitle: 'Movie', minutes_left: 31, href: '/watch/movie/1' }).querySelector('.watch-card__sub');
    const poster = p.doc.createElement('span');
    const found = p.cards('lib')[0];
    p.ctx.MMWatch.core.addMarks(found, poster, { title: 'Brightwater', mark_kind: 'show', mark_key: '2' });
    const line = found.querySelector('.watch-card__sub');
    return { library: [sub.textContent].concat(pieces(sub)), found: [line.textContent].concat(pieces(line)) };
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
    const go = async (search, title, over) => {
      const p = load(watchPage([row('lib', [card('show/2', 'Brightwater & Co', 'TV · 2 seasons · 9 left', over)]), row('disc', [card('show/2', 'Brightwater & Co', 'TV · 2016')]),
        row('leave', [card('season/21', 'Brightwater & Co', 'Season 1', { ref: 'show-2' })])]),
        { next: watchPage([row('lib', [card('show/9', 'Other', 'TV')])]), api: { 'GET /api/watch/search?q=Brightwater%20%26%20Co': search, 'GET /api/watch/title/show-2': title } });
      await p.pick(p.cards('leave')[0], 'mark');
      const marked = p.look();
      await p.undo();
      return { marked, undone: p.look() };
    };
    const item = (ref, subtitle, watched) => ({ ref, title: 'Brightwater & Co', subtitle, watched });
    return {
      found: await go({ items: [item('show-7', 'TV · 1 season · 2 left', false), item('show-2', 'TV · 2 seasons · 4 left', false)] }, { watched: false, seasons: [{ key: '21', watched: true }] }),
      finished: await go({ items: [item('show-2', 'TV · 2 seasons', true)] }, { watched: true }),
      unlisted: await go({ items: [item('show-7', 'TV', true)] }, { watched: true }),
      noLonger: await go(500, { watched: false, seasons: [] }, { watched: true }),
      failed: await go(500, 500),
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

  // The row opened as a grid: the card is in both, and an Undo brings it back to both.
  async dismissInGrid() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { class: 'watch-grid', id: 'grid' }, ...CONTINUE]]));
    await p.pick(p.cards('grid')[1], 'dismiss');
    const gone = [p.marks('cont'), p.marks('grid')];
    await p.undo();
    return { gone, back: [p.marks('cont'), p.marks('grid')], calls: p.calls };
  },

  // With the rows hidden behind an open grid, an Undo gives the focus to what is shown; once the grid is closed, to the row.
  async undoFocusInGrid() {
    const go = async (act, { at, close, menu = true, toggle, inert, mark }) => {
      const p = load(watchPage([['div', { id: 'rows' }, row('cont', CONTINUE), row('lib', [FILM(), card('movie/9', 'Other', 'Movie')])],
        ['div', { class: 'watch-grid', id: 'grid' }, ...CONTINUE, FILM(), card('movie/9', 'Other', 'Movie')]]),
      { next: (n, calls) => watchPage([row('cont', calls.length > 1 ? CONTINUE : CONTINUE.slice(1))]) });
      const hide = (id, on) => { p.doc.getElementById(id).hidden = on; };
      p.doc.body.descendants().forEach((n) => { n.getClientRects = () => (n.closest('[hidden]') || !p.doc.body.contains(n) ? [] : [{}]); });
      hide('rows', true);
      if (inert) { hide('rows', false); p.doc.getElementById('rows').setAttribute('inert', ''); }
      if (toggle) {
        const b = p.doc.body.appendChild(build(p.doc, ['button', { 'data-mark-toggle': 'movie/9', 'data-mark-name': 'Other', 'aria-pressed': 'false' }]));
        click(p.doc, b);
        await settle();
        b.remove();
      } else if (menu) await p.pick(p.cards(at || 'grid')[mark || 0], act);
      else { p.dismissed('movie-1'); await settle(); }
      if (close) { hide('rows', false); p.doc.getElementById('grid').remove(); }
      p.doc.activeElement = p.doc.body;
      await p.undo();
      const c = p.doc.activeElement.closest('.watch-card');
      return c ? (c.closest('#grid') ? 'grid ' : c.closest('#cont') ? 'row ' : 'lib ') + (c.hasAttribute('data-dismiss-ref') ? 'continue' : 'library') : p.doc.activeElement.tagName;
    };
    return {
      dismissed: await go('dismiss', {}),
      fromPreview: await go('dismiss', { menu: false }),
      watched: await go('mark', {}),
      libraryCard: await go('mark', { mark: 3 }),
      closed: await go('dismiss', { close: true }),
      toggledElsewhere: await go('mark', { toggle: true }),
      opening: await go('dismiss', { inert: true, menu: false }),
      libraryClosed: await go('mark', { mark: 3, close: true }),
    };
  },

  // A card an open grid has let go of fades out on its own: a change that follows finds it no more.
  async ghostInGrid() {
    const p = load(watchPage([row('cont', CONTINUE), ['div', { id: 'host' }, CONTINUE[0]]]));
    const ghost = p.cards('host')[0];
    ghost.classList.add('is-gone');
    p.dismissed('movie-1');
    await settle();
    return [p.marks('cont'), p.marks('host'), ghost.classList.contains('is-kept')];
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

  // Dismissed in the banner by a press that gives its button no focus.
  async dismissBannerUnfocused() {
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    p.doc.activeElement = p.doc.body;
    click(p.doc, p.doc.querySelector('[data-watch-dismiss]'));
    const focused = [];
    Node.prototype.focus = function () { focused.push(this.className); this.ownerDocument.activeElement = this; };
    await settle();
    return focused;
  },

  // A row scrolled past the place of the card an Undo puts back, or that has it in view.
  async undoOffScreen() {
    const go = async (act, left, opts = {}) => {
      const p = load(watchPage([row('cont', CONTINUE)]), opts);
      const scroller = p.doc.querySelector('.watch-row__scroll');
      const by = [];
      const snaps = [];
      const put = scroller.insertBefore;
      scroller.insertBefore = function (...args) { snaps.push(scroller.style.scrollSnapType); return put.apply(scroller, args); };
      scroller.getBoundingClientRect = () => ({ top: 0, bottom: 20, left: 0, right: 390, width: 390, height: 20 });
      scroller.scrollBy = (to) => by.push([to.left, to.behavior]);
      const first = p.cards()[0];
      const at = () => ({ top: 0, bottom: 20, left, right: left, width: 0, height: 20 });
      first.getBoundingClientRect = at;
      if (opts.next) Node.prototype.getBoundingClientRect = function () { return this.classList.contains('watch-card') ? at() : { top: 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 }; };
      await p.pick(first, act);
      const before = by.length;
      await p.undo();
      return [p.marks()[0], before, by, snaps, scroller.style.scrollSnapType];
    };
    return {
      before: await go('dismiss', -128),
      after: await go('dismiss', 380),
      inView: await go('dismiss', 20),
      mark: await go('mark', -128, { next: watchPage([row('cont', CONTINUE.slice(1))]) }),
    };
  },

  // An Undo of an older dismissal, when another change has brought the row back without that
  // title: its card comes with the server's page, out of sight at the start of a scrolled row.
  async olderUndoOffScreen() {
    const salt = card('movie/3', 'Salt', 'Movie', { watched: true });
    const page = (cards) => watchPage([['div', { class: 'watch-rows' }, ...(cards.length ? [row('cont', cards)] : []), row('lib', [salt])]]);
    const p = load(page(CONTINUE.slice(0, 1)), { next: (_n, calls) => {
      const sent = calls.filter((c) => !/^GET /.test(c));
      return page(sent.length < 2 ? [] : sent.length < 3 ? CONTINUE.slice(2) : [CONTINUE[0], CONTINUE[2]]);
    } });
    await p.pick(p.cards('cont')[0], 'dismiss');
    await p.pick(p.cards('lib')[0], 'mark');
    const rowNow = p.doc.querySelector('.watch-rows').children[0];
    const brought = p.marks('cont');
    const scroller = rowNow.querySelector('.watch-row__scroll');
    const by = [];
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 20, left: 0, right: 390, width: 390, height: 20 });
    scroller.scrollBy = (to) => by.push([to.left, to.behavior]);
    const box = Node.prototype.getBoundingClientRect;
    Node.prototype.getBoundingClientRect = function () { return this.getAttribute('data-mark') === 'movie/1' ? { top: 0, bottom: 20, left: -128, right: -108, width: 20, height: 20 } : box.call(this); };
    await p.undo(0);
    return { brought, marks: p.marks('cont'), by, snap: scroller.style.scrollSnapType };
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
    const p = load(watchPage([row('cont', CONTINUE)]), { next: (n) => { if (n === 1) p.toasts[0].undo(); return watchPage([row('cont', n === 1 ? [CONTINUE[2]] : CONTINUE)]); } });
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
    const out = { marks: p.marks(), focus: p.focus(), events: p.events.slice(), dialog: p.toasts[0].dialog };
    p.doc.activeElement = p.doc.body;
    await p.undo();
    const afterUndo = p.focus();
    // Pressed by touch, where a button may not take the focus: the rows beneath do not take it either.
    click(p.doc, b);
    p.doc.activeElement = p.doc.body;
    await settle();
    const touched = p.focus();
    p.doc.documentElement.classList.remove('has-ttl');
    click(p.doc, p.doc.querySelector('[data-mark-toggle]'));
    await settle();
    return { ...out, afterUndo, touched, onPage: p.toasts[p.toasts.length - 1].dialog };
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

  // A card gives way with its menu open: by its mark, by the Undo, and by leaving.
  async menuOfAGoneCard() {
    const first = later();
    let sent = first.answer;
    const episode = card('episode/23', 'Brightwater', 'S1 · E3', { dismiss: 'show-2' });
    const p = load(watchPage([row('cont', CONTINUE)]), { next: () => sent });
    const open = () => p.doc.getElementById('watch-more-menu').classList.contains('is-open');
    await p.pick(p.cards()[1], 'mark');
    p.open(p.cards()[1]);
    first.send(watchPage([row('cont', [CONTINUE[0], episode, CONTINUE[2]])]));
    await settle();
    const replaced = [open(), p.focus()];
    p.open(p.cards()[1]);
    sent = watchPage([row('cont', CONTINUE)]);
    await p.undo();
    const undone = open();
    p.open(p.cards()[1]);
    p.dismissed('movie-3');
    p.open(p.cards()[1]);
    await settle();
    const another = [open(), !!p.doc.activeElement.closest('#watch-more-menu')];
    const last = later();
    sent = last.answer;
    click(p.doc, p.doc.body);
    await p.pick(p.cards()[1], 'mark');
    p.open(p.cards()[1]);
    last.send(watchPage([]));
    await settle();
    return { replaced, undone, another, left: [open(), p.focus()] };
  },

  // What a season's mark asked of its show lands after the mark is undone.
  async lateShow() {
    const first = later();
    let n = 0;
    const p = load(watchPage([row('lib', [card('show/2', 'Brightwater', 'TV')]), row('leave', [card('season/21', 'Brightwater', 'Season 1', { ref: 'show-2' })])]),
      { next: watchPage([]), api: { 'GET /api/watch/title/show-2': () => (n++ ? { watched: false } : first.answer) } });
    await p.pick(p.cards('leave')[0], 'mark');
    await p.undo();
    first.send({ watched: true });
    await settle();
    return p.look()[0][1];
  },

  // Two episodes marked one after the other, then undone with the oldest first. The card was
  // replaced twice, or the second mark took it away, or took its row away with it.
  async undoOldestMark() {
    const go = async (others, second) => {
      let keys = [22];
      const now = () => continuePage([...others.slice(0, 1), ...keys.map(episodeCard), ...others.slice(1)]);
      const p = load(now(), { next: now });
      const show = () => p.cards().filter((c) => c.getAttribute('data-dismiss-ref') === 'show-2')[0];
      const state = () => p.marks('cont');
      keys = [23];
      await p.pick(show(), 'mark');
      keys = second;
      await p.pick(show(), 'mark');
      const marked = state();
      keys = [22];
      await p.undo(0);
      const oldest = state();
      await p.undo(1);
      return { marked, oldest, both: state(), rows: p.doc.querySelectorAll('#cont').length, started: p.started.length };
    };
    return { replaced: await go([CONTINUE[0], CONTINUE[2]], [24]), left: await go([CONTINUE[0], CONTINUE[2]], []), rowGone: await go([], []) };
  },

  // The row emptied by two titles; the server's page brings it back with one, then the other is undone.
  async undoIntoANewRow() {
    let sent = [CONTINUE[1], CONTINUE[2]];
    const p = load(continuePage(sent), { next: () => continuePage(sent) });
    sent = [episodeCard(23), CONTINUE[2]];
    await p.pick(p.cards('cont')[0], 'mark');
    sent = [CONTINUE[2]];
    await p.pick(p.cards('cont')[0], 'mark');
    sent = [];
    p.dismissed('movie-3');
    await settle();
    sent = [CONTINUE[1]];
    await p.undo(0);
    const one = p.marks('cont');
    sent = [CONTINUE[1], CONTINUE[2]];
    await p.undo(2);
    return { one, both: p.marks('cont'), rows: p.doc.querySelectorAll('#cont').length };
  },

  // The banner dismissed until none is left, then every dismissal undone, oldest first.
  async undoOldestBanner() {
    const titles = [['movie-1', 'Fieldwork', '/watch/play/1'], ['show-2', 'Brightwater', '/watch/play/22'], ['movie-3', 'Salt', '/watch/play/3']];
    const gone = new Set();
    const now = () => {
      const left = titles.filter((t) => !gone.has(t[0]));
      return watchPage([...(left.length ? [banner(...left[0])] : []), ['div', { class: 'watch-rows' }, ...(left.length ? [row('cont', CONTINUE.filter((c) => !gone.has(c[1]['data-dismiss-ref'])))] : [])]]);
    };
    const p = load(now(), { next: now });
    const states = [];
    for (const [ref] of titles) {
      gone.add(ref);
      click(p.doc, p.doc.querySelector('[data-watch-dismiss="' + ref + '"]'));
      await settle();
    }
    states.push(p.banner());
    for (const [i, [ref]] of titles.entries()) {
      gone.delete(ref);
      await p.undo(i);
      states.push(p.banner());
    }
    return { states, marks: p.marks() };
  },

  // Two seasons marked, the first undone: the show's card says what is left now, not before both.
  async undoOldestSeason() {
    const seasons = { 21: false, 22: false };
    const now = () => watchPage([row('lib', [card('show/2', 'Brightwater', 'TV · 2 seasons · ' + ((seasons[21] ? 0 : 5) + (seasons[22] ? 0 : 4)) + ' left')]),
      row('leave', [21, 22].map((k) => card('season/' + k, 'Brightwater', 'Season ' + k, { ref: 'show-2', watched: seasons[k] })))]);
    const p = load(now(), { next: now });
    for (const i of [0, 1]) {
      seasons[21 + i] = true;
      await p.pick(p.cards('leave')[i], 'mark');
    }
    seasons[21] = false;
    await p.undo(0);
    return p.look().map((look) => look.slice(1, 3));
  },

  // A dismissal undone while the server cannot ask Plex, whose page then lacks the card and its banner.
  async undoWithoutPlex() {
    const p = load(watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE)]),
      { next: () => watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), ['p', { class: 'watch-notice' }], row('cont', [CONTINUE[0], CONTINUE[2]])]) });
    p.dismissed('show-2');
    await settle();
    const gone = [p.banner(), p.marks()];
    await p.undo();
    return { gone, back: [p.banner(), p.marks()] };
  },

  // The open grid is told of a card the server's page brought, with its row or into it.
  async entersTheGrid() {
    const go = async (before, after) => {
      const p = load(continuePage(before), { next: () => continuePage(after) });
      const told = [];
      p.ctx.MMWatch.grid = { bring: (c) => told.push([c.classList.contains('watch-card'), c.getAttribute('data-dismiss-ref'), p.doc.body.contains(c), c.closest('.watch-row').id]) };
      click(p.doc, p.doc.body.appendChild(build(p.doc, ['button', { 'data-mark-toggle': 'movie/3', 'data-mark-name': 'Salt', 'aria-pressed': 'true' }])));
      await settle();
      return told;
    };
    return { newRow: await go([], [CONTINUE[0], CONTINUE[2]]), into: await go([CONTINUE[0]], [CONTINUE[0], CONTINUE[2]]) };
  },

  // Undone with the focus in what the Undo takes away: a card, the banner, and a card the server's page brings.
  async undoFocus() {
    const p = load(watchPage([row('cont', CONTINUE)]), { next: watchPage([row('cont', [CONTINUE[0], episodeCard(23), CONTINUE[2]])]) });
    await p.pick(p.cards()[1], 'mark');
    await p.undo();
    const replaced = p.focus();
    const q = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]),
      { next: watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE.slice(1))]) });
    const b = q.doc.querySelector('[data-watch-dismiss]');
    b.focus();
    click(q.doc, b);
    await settle();
    await q.undo();
    const inBanner = q.doc.activeElement === b && q.banner();
    let keys = [23];
    const o = load(continuePage([CONTINUE[0], episodeCard(22)], true), { next: () => continuePage([CONTINUE[0], ...keys.map(episodeCard)], true) });
    await o.pick(o.cards()[1], 'mark');
    keys = [];
    await o.pick(o.cards()[1], 'mark');
    keys = [22];
    o.doc.activeElement = o.doc.body;
    await o.undo(0);
    const redrawn = o.focus();
    // Focus elsewhere stays there.
    o.doc.getElementById('watch-search').focus();
    await o.undo(1);
    return { replaced, inBanner, redrawn, elsewhere: o.doc.activeElement.id };
  },

  // Undo pressed while a newer change of the same title is on its way.
  async undoWaits() {
    const change = later();
    const p = load(watchPage([row('lib', [FILM()])]), { api: { 'DELETE /api/watch/watched/movie/1': () => change.answer } });
    await p.pick(p.cards()[0], 'mark');
    await p.pick(p.cards()[0], 'mark');
    await p.undo(0);
    const waiting = p.calls.length;
    change.send({ ok: true, changed: 1, watched: false });
    await settle();
    return { waiting, calls: p.calls };
  },

  // Dismiss pressed twice before the first is in.
  async dismissTwice() {
    const answer = later();
    const p = load(watchPage([banner('movie-1', 'Fieldwork', '/watch/play/1'), row('cont', CONTINUE)]), { api: { 'POST /api/watch/dismissed/movie-1': () => answer.answer } });
    p.dismissed('movie-1');
    p.dismissed('movie-1');
    answer.send({ ok: true });
    await settle();
    return [p.calls, p.toasts.length];
  },

  // A film, and a show as a whole, marked from a card of the library, then marked as not watched.
  async notWatchedAgain() {
    const go = async (lib, at) => {
      let cont = CONTINUE;
      const p = load(watchPage([row('cont', cont), row('lib', [lib])]), { next: () => watchPage([row('cont', cont), row('lib', [lib])]) });
      cont = CONTINUE.filter((c, i) => i !== at);
      await p.pick(p.cards('lib')[0], 'mark');
      const marked = p.marks('cont');
      cont = CONTINUE;
      await p.pick(p.cards('lib')[0], 'mark');
      return { marked, again: p.marks('cont') };
    };
    return { film: await go(card('movie/1', 'Fieldwork', 'Movie'), 0), show: await go(card('show/2', 'Brightwater', 'TV'), 1) };
  },

  // An episode marked and its show dismissed before the Watch page answers, or before what it asks of a show past the page does.
  async dismissedMeanwhile() {
    const lib = (sub) => row('lib', [card('show/2', 'Brightwater', sub)]);
    const go = async (next, api, send) => {
      const p = load(watchPage([banner('show-2', 'Brightwater', '/watch/play/22'), row('cont', CONTINUE), lib('TV · 9 left')]), { next, api });
      await p.pick(p.cards('cont')[1], 'mark');
      p.dismissed('show-2');
      await settle();
      send();
      await settle();
      const sub = () => p.look('lib')[0][2];
      const dismissed = sub();
      await p.undo();
      return [dismissed, sub()];
    };
    const page = later();
    const search = later();
    const cont = [CONTINUE[0], CONTINUE[2]];
    return {
      page: await go((n) => (n === 1 ? page.answer : watchPage([row('cont', cont), lib('TV · 8 left')])), {}, () => page.send(watchPage([row('cont', CONTINUE), lib('TV · 9 left')]))),
      show: await go(() => watchPage([row('cont', cont)]), { 'GET /api/watch/search?q=Brightwater': () => search.answer, 'GET /api/watch/title/show-2': { watched: false } },
        () => search.send({ items: [{ ref: 'show-2', subtitle: 'TV · 8 left' }] })),
    };
  },

  // An episode marked, then its whole show as not watched, before the Watch page answers. The show's card stands past the page's.
  async wholeShowMeanwhile() {
    const first = later();
    const p = load(watchPage([row('cont', CONTINUE), row('lib', [card('show/2', 'Brightwater', 'TV · 1 left', { watched: true })])]),
      { next: (n) => (n === 1 ? first.answer : watchPage([row('cont', CONTINUE)])),
        api: { 'GET /api/watch/search?q=Brightwater': { items: [{ ref: 'show-2', subtitle: 'TV' }] }, 'GET /api/watch/title/show-2': { watched: false } } });
    await p.pick(p.cards('cont')[1], 'mark');
    await p.pick(p.cards('lib')[0], 'mark');
    return p.look('lib')[0].slice(1, 3);
  },

  // What a show's page leaves alone when the server's page arrives.
  async leftAlone() {
    const next = episodesPage([21], '/watch/play/22');
    const go = async (before, opts) => {
      const p = load(episodesPage([22]), { next, ...opts });
      await before(p);
      await press(p, 21);
      return [p.doc.querySelector('.watch-btn--play').getAttribute('href'), pressed(p)];
    };
    const nothing = () => {};
    return {
      busyMark: await go((p) => press(p, 22), { api: { 'DELETE /api/watch/watched/episode/22': () => later().answer } }),
      loadingSeason: await go((p) => p.doc.querySelector('.watch-episodes').setAttribute('aria-busy', 'true'), {}),
      signedOut: await go(nothing, { redirected: true }),
      playFocused: await go((p) => p.doc.querySelector('.watch-btn--play').focus(), {}),
      followed: await go(nothing, {}),
    };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });
