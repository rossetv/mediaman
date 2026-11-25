// Runs the real history.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node history_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

process.env.TZ = 'Asia/Kolkata';

const { Node, makeDocument, build, click, settle } = require('./fake_dom.js');

Object.defineProperty(Node.prototype, 'lastElementChild', { get() { return this.children[this.children.length - 1] || null; } });
Object.defineProperty(Node.prototype, 'open', { get() { return this.hasAttribute('open'); } });

Node.prototype.value = '';

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const RealDate = Date;
const NOW = RealDate.UTC(2026, 10, 25, 12, 0);
class FixedDate extends RealDate {
  constructor(...a) { if (a.length) super(...a); else super(NOW); }
}

const at = (t) => ({ label: 'Time', value: t.replace('T', ' ').replace('Z', ' UTC'), at: t, empty: false });
const item = (id, more) => ({
  id, action: 'downloaded', action_label: 'downloaded', line_title: 'Film ' + id, title: 'Film ' + id, summary: 'Added to Radarr',
  is_security: false, created_at: '2026-11-24T20:15:03Z', actor: 'ana', avatar: '/a/1', ip: null,
  fields: [at('2026-11-24T20:15:03Z'), { label: 'Who', value: 'ana', empty: false }, { label: 'IP address', value: '1.2.3.4', empty: false }], ...more,
});
const page = (ids, next, total, n) => ({ page: n || 1, items: ids.map((i) => item(i)), next_cursor: next, total: total === undefined ? ids.length : total });

function open(opts) {
  opts = { search: '', items: [item(5), item(6)], cursor: null, total: 2, admin: true, filters: {}, ...opts };
  const doc = makeDocument();
  doc.readyState = 'complete';
  const timers = [];
  const toasts = [];
  const copied = [];
  const steps = [];
  const asked = [];
  const listeners = {};
  const win = {
    location: { href: 'http://mm.test/history' + opts.search },
    innerWidth: 1280, innerHeight: 900, scrollY: 0, isSecureContext: true,
    matchMedia: () => ({ matches: !!opts.reduced }),
    setTimeout: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; },
    clearTimeout: (t) => { if (t) t.fn = () => {}; },
    requestAnimationFrame: (fn) => timers.push({ fn, ms: 16 }),
    getComputedStyle: () => ({ animationDuration: '0.2s', animationTimingFunction: 'ease', top: '52px' }),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    scrollTo() {}, AbortController,
    UIFeedback: { toast: (m) => toasts.push(m), error: (m) => toasts.push('error: ' + m) },
  };
  win.history = {
    pushState: (_s, _t, url) => { win.location.href = 'http://mm.test' + url; steps.push('push ' + url); },
    replaceState: (_s, _t, url) => { win.location.href = 'http://mm.test' + url; steps.push('replace ' + url); },
  };
  win.window = win;
  const ctx = {
    window: win, document: doc, URL, URLSearchParams, Date: FixedDate, Node,
    navigator: { clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
  };
  vm.createContext(ctx);
  const root = doc.body.appendChild(build(doc, ['div', { 'data-hist': '' },
    ['div', { class: 'hx-tools' }, ['form', { class: 'hx-line' },
      ['div', { class: 'hx-search' }, ['input', {}], ['button', { class: 'hx-x' }]], ['div', { class: 'hx-chips' }]]],
    ['div', { class: 'hx-sum' }, ['span', {}], ['button', { 'data-hist-clear': '' }]],
    ['div', { class: 'hx-list' }], ['div', { class: 'hx-more' }], ['span', { 'data-hist-said': '' }]]));
  doc.body.appendChild(build(doc, ['div', { class: 'hx-veil' }]));
  doc.body.appendChild(build(doc, ['div', { class: 'hx-menu' }]));
  doc.body.appendChild(build(doc, ['script', { id: 'history-data' }, JSON.stringify({
    admin: opts.admin, me: 'ana', people: [{ username: 'ana', avatar: '/a/1' }, { username: 'bob', avatar: '/a/2' }],
    actions: [['kept', 'Kept'], ['plays', 'Plays']], filters: opts.filters, filterLabel: '',
    items: opts.items, cursor: opts.cursor, total: opts.total, page: opts.page || 1, openId: null,
  })]));
  /* The end of the list is far down, so nothing more is asked for until a test scrolls. */
  const more = root.querySelector('.hx-more');
  let far = true;
  more.getBoundingClientRect = () => ({ top: far ? 5000 : 500 });
  vm.runInContext(fs.readFileSync(path.join(JS, 'core/dom.js'), 'utf8'), ctx);
  win.MM.api = {
    get: (url, o) => new Promise((resolve, reject) => asked.push({ url, resolve, reject, signal: o && o.signal })),
  };
  vm.runInContext(fs.readFileSync(path.join(JS, 'history.js'), 'utf8'), ctx);
  const q = (s) => doc.querySelector(s);
  const input = root.querySelector('input');
  const fire = (el, type, e) => (el.listeners[type] || []).forEach((fn) => fn({ target: el, preventDefault() {}, ...e }));
  return {
    doc, win, root, more, toasts, copied, steps, asked, timers, q, input, fire,
    rows: () => root.querySelectorAll('.hist'),
    ids: () => root.querySelectorAll('.hist').map((r) => r.getAttribute('data-event')),
    tick: (max) => timers.splice(0).filter((t) => (t.ms <= (max || 1e9) ? (t.fn(), false) : true)).forEach((t) => timers.push(t)),
    type: (text) => { input.value = text; fire(input, 'input'); },
    scroll: () => { far = false; (listeners.scroll || []).forEach((fn) => fn()); far = true; },
    back: (search) => { win.location.href = 'http://mm.test/history' + search; listeners.popstate.forEach((fn) => fn()); },
    pick: (k, v) => {
      click(doc, root.querySelector(`[data-menu="${k}"]`));
      click(doc, q('.hx-menu').querySelector(`[data-v="${v}"]`));
    },
    line: (i) => root.querySelectorAll('.hx-l1')[i].textContent,
    press: (s) => click(doc, root.querySelector(s)),
  };
}

function watchMoves(h) {
  const moves = [];
  const watch = (name, el) => {
    el.animate = (frames, o) => {
      const prop = Object.keys(frames)[0];
      const m = { name, prop, from: frames[prop][0], to: frames[prop][1], ms: o.duration, cancelled: false };
      moves.push(m);
      return { cancel: () => { m.cancelled = true; } };
    };
  };
  const [a, b] = h.rows();
  watch('second', b);
  watch('more', h.more);
  watch('said', h.more.nextElementSibling);
  watch('card', a.parentNode);
  h.rows().forEach((r) => {
    const body = r.querySelector('.hist-body');
    body.animate = () => {};
    Object.defineProperty(body, 'offsetHeight', { get: () => 300 });
  });
  return moves;
}

const state = (r) => ({ open: r.open, closing: r.classList.contains('is-closing') });

const scenarios = {
  close: (h = open({ search: '?action=kept&event=5', filters: { action: 'kept' } })) => {
    const moves = watchMoves(h);
    const first = h.rows()[0];
    click(h.doc, first.querySelector('.hist-sum'));
    const out = { afterClick: state(first), url: h.win.location.href, moving: moves.map((m) => ({ ...m })) };
    h.tick();
    return { ...out, moves, settled: state(first), urlAfter: h.win.location.href };
  },
  reduced: () => scenarios.close(open({ search: '?event=5', reduced: true })),
  open: () => {
    const h = open({});
    const moves = watchMoves(h);
    const first = h.rows()[0];
    let prevented = false;
    const sum = first.querySelector('.hist-sum');
    const e = { target: sum, preventDefault() { prevented = true; } };
    (h.root.listeners.click || []).forEach((fn) => fn(e));
    first.setAttribute('open', '');
    h.fire(h.q('.hx-list'), 'toggle', { target: first });
    h.tick();
    return { prevented, moves, steps: h.steps };
  },
  copy: async () => {
    const h = open({ search: '?event=5' });
    click(h.doc, h.rows()[0].querySelector('[data-hist-copy]'));
    await settle();
    return { copied: h.copied, toasts: h.toasts, open: h.rows()[0].open };
  },
  days: () => {
    const h = open({ items: [item(1, { fields: [at('2026-11-25T01:00:00Z')] }), item(2, { fields: [at('2026-11-24T20:15:03Z')] }),
      item(3, { fields: [at('2026-11-24T10:00:00Z')] }), item(4, { fields: [at('2025-01-02T10:00:00Z')] }), item(5, { created_at: 'soon', fields: [] })] });
    return { days: h.root.querySelectorAll('.hx-day__h').map((d) => d.firstChild.textContent), sizes: h.root.querySelectorAll('.hx-day').map((d) => d.querySelectorAll('.hist').length) };
  },
  more: async () => {
    const h = open({ cursor: 'c1', total: 4 });
    h.scroll();
    h.scroll();
    h.asked[0].resolve(page([6, 7, 8], null, null));
    await settle();
    h.scroll();
    return { asked: h.asked.map((a) => a.url), ids: h.ids(), end: h.more.textContent, said: h.q('[data-hist-said]').textContent };
  },
  moreFails: async () => {
    const h = open({ cursor: 'c1', total: 4 });
    h.scroll();
    h.asked[0].reject(new Error('no'));
    await settle();
    h.scroll();
    const failed = { text: h.more.textContent, asked: h.asked.length };
    h.press('.hx-fail [data-hist-more]');
    h.asked[1].resolve(page([7], null, null));
    await settle();
    return { failed, again: h.asked[1].url, ids: h.ids(), focus: h.doc.activeElement.closest('.hist').getAttribute('data-event') };
  },
  filterDuringMore: async () => {
    const h = open({ cursor: 'c1', total: 4 });
    h.scroll();
    h.pick('action', 'plays');
    h.scroll();
    h.asked[1].resolve(page([9], null));
    await settle();
    h.asked[0].resolve(page([7, 8], null, null));
    await settle();
    return { asked: h.asked.map((a) => a.url), ids: h.ids() };
  },
  typing: async () => {
    const h = open({});
    ['h', 'ha', 'har'].forEach(h.type);
    const before = h.asked.length;
    h.tick(200);
    const once = h.asked.map((a) => a.url);
    h.type('harbour');
    h.tick(200);
    h.asked[1].resolve(page([8], null));
    await settle();
    h.asked[0].resolve(page([1, 2, 3], null));
    await settle();
    return { before, once, ids: h.ids(), steps: h.steps };
  },
  back: async () => {
    const h = open({});
    h.pick('user', 'bob');
    const url = h.win.location.href;
    h.asked[0].resolve(page([8], null));
    await settle();
    const filtered = h.ids();
    h.back('');
    const askedBack = h.asked[h.asked.length - 1].url;
    h.asked[h.asked.length - 1].resolve(page([5, 6], null));
    await settle();
    return { url, askedFor: h.asked[0].url, filtered, askedBack, ids: h.ids(), chips: h.root.querySelectorAll('.is-set').length };
  },
  viewer: () => {
    const h = open({ admin: false, search: '?user=bob&date=today' });
    return { chips: h.root.querySelectorAll('[data-menu]').map((b) => b.getAttribute('data-menu')), asked: h.asked[0].url };
  },
  dates: () => ['today', 'yesterday', 'week', '2026-11-01..2026-11-03', '2026-02-31..', '..2026-11-03'].map((d) => {
    const h = open({ search: '?date=' + d });
    return h.asked.length ? Object.fromEntries(new URL('http://x' + h.asked[0].url).searchParams) : null;
  }),
  lines: () => {
    const sec = (event, actor, avatar, more) => item(1, { action: 'sec:' + event, is_security: true, actor, avatar, summary: '', line_title: 'Label', ...more });
    const play = (more) => item(1, { action: 'play', media_type: 'tv', summary: 'S1E2', episodes: [{ label: 'S1E2 · Two', finished: true }], finished: true, line_title: 'Show', ...more });
    return [
      item(1), item(1, { action: 'deleted', actor: null, avatar: null }), item(1, { actor: 'old', avatar: null }),
      item(1, { actor: 'old@x.org', avatar: null }), sec('login.failed', 'hunter2', null), sec('login.failed', 'bob', null),
      sec('subscriber.opted_out', 'a@x.org', null), sec('odd.event', 'ana', '/a/1'), sec('user.created', 'ana', '/a/1', { summary: 'eve (viewer)' }),
      play(), play({ finished: false, media_type: 'movie', episodes: [], line_title: 'Film' }),
      play({ episodes: [{ label: 'S1E2', finished: true }, { label: 'S1E3', finished: true }] }),
      item(1, { action: 'never_heard_of', action_label: 'never heard of' }),
    ].map((i) => open({ items: [i] }).line(0));
  },
  newest: async () => {
    const h = open({ search: '?event=5', page: 4 });
    const link = () => { const a = h.q('.hx-newest'); return a && a.getAttribute('href'); };
    const out = { deep: link() };
    h.pick('user', 'bob');
    out.filtered = link();
    h.asked[0].resolve(page([8], null));
    await settle();
    out.answered = link();
    h.back('?event=5');
    h.asked[h.asked.length - 1].resolve(page([5, 6], null, 100, 4));
    await settle();
    return { ...out, back: link() };
  },
  newestOfDates: async () => {
    const h = open({ search: '?date=week&event=5', page: 4 });
    const link = () => { const a = h.q('.hx-newest'); return a && a.getAttribute('href'); };
    h.asked[0].resolve(page([8], null));
    await settle();
    const hidden = link();
    const d = open({ search: '?date=week&event=5' });
    d.asked[0].resolve(page([5], null, 60, 2));
    await settle();
    return { hidden, deep: d.q('.hx-newest').getAttribute('href') };
  },
  plays: () => {
    const tv = (more) => item(1, { action: 'play', media_type: 'tv', line_title: 'Show', episodes: [{ label: 'S1E2' }], summary: 'S1E2', year: 2020, ...more });
    const film = (more) => item(1, { action: 'play', media_type: 'movie', line_title: 'Film', episodes: [], summary: '', year: 2023, ...more });
    const part = { finished: false, summary: 'stopped at 1 h of 2 h', progress: { fraction: 0.5 }, fields: [{ label: 'Stopped at', value: '1 h of 2 h' }] };
    const h = open({ items: [{ ...film(part), id: 'w:1' }, { ...film({ finished: true, runtime_text: '2 h' }), id: 'w:2' }, { ...tv({ finished: true, runtime_text: '46 min' }), id: 'w:3' },
      { ...tv({ finished: true, runtime_text: '46 min', episodes: [{}, {}], summary: 'S1E2 to E3' }), id: 'w:4' }] });
    return { subs: h.root.querySelectorAll('.hx-l2').map((l) => l.textContent), bars: h.root.querySelectorAll('.hx-bar i').map((b) => b.style.width) };
  },
  ownLine: () => open({ admin: false, items: [item(1), item(2, { action: 'sec:login.success', is_security: true, summary: '' })] }).root.querySelectorAll('.hx-l1').map((l) => l.textContent),
  marks: () => {
    const h = open({ search: '?q=FILM', filters: { q: 'FILM' }, items: [item(1, { line_title: '<b>a Film</b> & film' })] });
    return { marks: h.root.querySelectorAll('mark').map((m) => m.textContent), text: h.line(0) };
  },
  empty: () => {
    const h = open({ items: [], total: 0 });
    return { text: h.q('.hx-list').textContent, tools: h.q('.hx-tools').hidden };
  },
  noMatch: async () => {
    const h = open({ search: '?q=zan&action=kept', filters: { q: 'zan', action: 'kept' }, items: [], total: 0 });
    const text = h.q('.hx-list').textContent;
    h.press('.hx-list [data-hist-clear]');
    return { text, tools: h.q('.hx-tools').hidden, url: h.win.location.href, asked: h.asked[h.asked.length - 1].url };
  },
  wrong: async () => {
    const h = open({ search: '?date=week' });
    const waiting = { busy: h.q('.hx-list').getAttribute('aria-busy'), skel: h.root.querySelectorAll('.hx-skel').length, rows: h.rows().length };
    h.asked[0].reject(new Error('no'));
    await settle();
    const text = h.q('.hx-list').textContent;
    h.press('[data-hist-retry]');
    h.asked[1].resolve(page([3], null));
    await settle();
    return { waiting, text, ids: h.ids() };
  },
  slow: async () => {
    const h = open({});
    h.pick('action', 'plays');
    const soon = h.rows().length;
    h.tick(600);
    const later = h.root.querySelectorAll('.hx-skel').length;
    return { soon, later };
  },
  count: async () => {
    const h = open({ search: '?action=kept', filters: { action: 'kept' }, total: 2 });
    h.asked[0].resolve(page([1], null, 1340));
    await settle();
    return h.q('.hx-sum').firstChild.textContent;
  },
  wrongDates: () => {
    const h = open({});
    h.press('[data-menu="date"]');
    const form = h.q('.hx-menu form');
    const [a, b] = form.querySelectorAll('input');
    a.value = '2026-11-05';
    b.value = '2026-11-01';
    h.fire(form, 'submit');
    return { asked: h.asked.length, told: !form.querySelector('.hx-dates__e').hidden };
  },
  titles: async () => {
    const h = open({});
    h.press('[data-menu="title"]');
    h.tick(0);
    h.asked[0].resolve(page([70], null));
    await settle();
    return h.q('.hx-menu').querySelectorAll('.hx-opt').map((o) => o.getAttribute('data-v'));
  },
};

Promise.resolve(scenarios[process.argv[2]]()).then((o) => console.log(JSON.stringify(o)));
