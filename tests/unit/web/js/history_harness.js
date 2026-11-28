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

const at = (t) => ({ key: 'when', label: 'When', value: t.replace('T', ' ').replace('Z', ' UTC'), at: t, empty: false });
const whose = (i) => {
  const kind = !i.actor || /login.failed|opted_out/.test(i.action) ? 'system' : i.avatar ? 'account' : 'former';
  return { kind, name: kind === 'system' ? 'mediaman' : i.actor, value: kind === 'former' ? '~former:' + i.actor : kind === 'system' ? '~mediaman' : i.actor };
};
const item = (id, more) => { const i = raw(id, more); return { who: whose(i), ...i }; };
const raw = (id, more) => ({
  id, action: 'downloaded', action_label: 'downloaded', line_title: 'Film ' + id, title: 'Film ' + id, summary: 'Added to downloads',
  is_security: false, created_at: '2026-11-24T20:15:03Z', actor: 'ana', avatar: '/a/1', ip: null,
  fields: [at('2026-11-24T20:15:03Z'), { key: 'who', label: 'Who', value: 'ana', empty: false }, { key: 'space', label: 'Space freed', value: 'Not recorded', empty: true },
    { key: 'record', label: 'Stored record', value: '<i>', empty: false }], ...more,
});
const PEOPLE = [{ value: 'ana', username: 'ana', kind: 'account', avatar: '/a/1' }, { value: 'bob', username: 'bob', kind: 'account', avatar: '/a/2' },
  { value: '~former:bob', username: 'bob', kind: 'former', avatar: null }, { value: '~mediaman', username: 'mediaman', kind: 'system', avatar: null }];
const page = (ids, next, total, n) => ({ page: n || 1, items: ids.map((i) => item(i)), next_cursor: next, total: total === undefined ? ids.length : total });

function open(opts) {
  opts = { search: '', items: [item(5), item(6)], cursor: null, total: 2, admin: true, filters: {}, shown: true, ...opts };
  const doc = makeDocument();
  doc.readyState = 'complete';
  const timers = [];
  const toasts = [];
  const copied = [];
  const steps = [];
  const own = [];
  const asked = [];
  const menus = [];
  const kinds = [];
  const sizes = [];
  const listeners = {};
  const win = {
    location: { href: 'http://mm.test/?you=history' },
    innerWidth: opts.width || 1280, innerHeight: 900, scrollY: 0, isSecureContext: true,
    matchMedia: () => ({ matches: !!opts.reduced }),
    setTimeout: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; },
    clearTimeout: (t) => { if (t) t.fn = () => {}; },
    requestAnimationFrame: (fn) => timers.push({ fn, ms: 16 }),
    getComputedStyle: (n) => ({ visibility: n.matches('.hx-x') && !n.parentNode.matches('.has-q') ? 'hidden' : 'visible', animationDuration: '0.2s', animationTimingFunction: 'ease', top: '52px' }),
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    scrollTo() {}, AbortController,
    ResizeObserver: class { constructor(fn) { sizes.push(fn); } observe(node) { this.node = node; } },
    UIFeedback: { toast: (m) => toasts.push(m), error: (m) => toasts.push('error: ' + m) },
  };
  /* The pane's place in the address is the shell's: the page writes none of its own. */
  win.history = {
    pushState: (_s, _t, url) => own.push('push ' + url),
    replaceState: (_s, _t, url) => own.push('replace ' + url),
  };
  win.window = win;
  const ctx = {
    window: win, document: doc, URL, URLSearchParams, Date: FixedDate, Node,
    navigator: { clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
  };
  vm.createContext(ctx);
  const group = doc.body.appendChild(build(doc, ['div', { 'data-you-group': 'history' }, ['div', { class: 'you-view', 'data-view': 'history', 'data-title': 'History' }]]));
  const view = group.firstElementChild;
  const root = view.appendChild(build(doc, ['div', { 'data-hist': '' },
    ['div', { class: 'hx-tools' }, ['form', { class: 'hx-line' },
      ['div', { class: 'hx-search' }, ['input', {}], ['button', { class: 'hx-x' }]]],
      ['div', { class: 'hx-groups' }],
      ['div', { class: 'hx-filters' }, ['div', { class: 'hx-chips' }],
        ['div', { class: 'hx-sum' }, ['span', {}], ['button', { 'data-hist-clear': '' }]]]],
    ['div', { class: 'hx-list', tabindex: '-1' }], ['div', { class: 'hx-more' }], ['span', { 'data-hist-said': '' }]]));
  view.appendChild(build(doc, ['script', { id: 'history-data' }, JSON.stringify({
    admin: opts.admin, me: 'ana', people: PEOPLE,
    actions: [['kept', 'Kept'], ['plays', 'Plays']], filters: opts.filters, filterLabel: '',
    items: opts.items, cursor: opts.cursor, total: opts.total, page: opts.page || 1, openId: null,
  })]));
  group.appendChild(build(doc, ['div', { class: 'hx-veil' }]));
  group.appendChild(build(doc, ['div', { class: 'hx-menu' }]));
  /* The end of the list is far down, so nothing more is asked for until a test scrolls. */
  const more = root.querySelector('.hx-more');
  let far = true;
  more.getBoundingClientRect = () => ({ top: far ? 5000 : 500 });
  const scroller = doc.createElement('div');
  scroller.scrollTop = 0;
  scroller.getBoundingClientRect = () => ({ top: 60, bottom: 900 });
  scroller.scrollTo = () => {};
  let shown = opts.shown;
  let h = opts.search.replace(/^\?/, '');
  const address = {
    read: () => new URLSearchParams(h),
    write: (params, how) => {
      h = new URLSearchParams(params).toString();
      steps.push(how + ' /history' + (h ? '?' + h : ''));
    },
  };
  const url = () => 'http://mm.test/history' + (h ? '?' + h : '');
  vm.runInContext(fs.readFileSync(path.join(JS, 'core/dom.js'), 'utf8'), ctx);
  win.MM.api = {
    get: (url, o) => new Promise((resolve, reject) => (/menu=action/.test(url) ? kinds : url.startsWith('/api/history/menu') ? menus : asked).push({ url, resolve, reject, signal: o && o.signal })),
  };
  win.MM.you = { shown: (g) => shown && g === 'history', scroller: (v) => (v === 'history' ? scroller : null) };
  win.MM.history = { address };
  const run = () => vm.runInContext(fs.readFileSync(path.join(JS, 'history.js'), 'utf8'), ctx);
  run();
  const q = (s) => doc.querySelector(s);
  const input = root.querySelector('input');
  const fire = (el, type, e) => (el.listeners[type] || []).forEach((fn) => fn({ target: el, preventDefault() {}, ...e }));
  return {
    doc, win, root, view, sizes, more, toasts, copied, steps, own, asked, menus, kinds, timers, q, input, fire, scroller, listeners, url, run,
    show: (on) => { shown = on; fire(doc, on ? 'mm:you-shown' : 'mm:you-hidden', { detail: { group: 'history', view: 'history' } }); },
    rows: () => root.querySelectorAll('.hist'),
    ids: () => root.querySelectorAll('.hist').map((r) => r.getAttribute('data-event')),
    tick: (max) => timers.splice(0).filter((t) => (t.ms <= (max || 1e9) ? (t.fn(), false) : true)).forEach((t) => timers.push(t)),
    type: (text) => { input.value = text; fire(input, 'input'); },
    resize: () => listeners.resize.forEach((fn) => fn()),
    scroll: () => { far = false; fire(scroller, 'scroll'); far = true; },
    back: (search) => { h = search.split('#')[0].replace(/^\?/, ''); listeners.popstate.forEach((fn) => fn()); },
    pick: (k, v) => {
      if (k === 'action') { click(doc, root.querySelector(`[data-group="${v}"]`)); return; }
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


/* The tools as a phone draws them: 196px high, of which the pills and chips under the search take 100. */
function sized(h) {
  const tools = h.q('.hx-tools');
  const def = (node, props) => Object.entries(props).forEach(([k, v]) => Object.defineProperty(node, k, { get: () => v }));
  def(tools, { offsetHeight: 196 });
  def(h.q('.hx-line'), { offsetTop: 4, offsetHeight: 44 });
  def(h.q('.hx-filters'), { offsetTop: 102, offsetHeight: 46 });
  h.sizes.forEach((fn) => fn());
}

/* Whether the tools are tucked after each scroll, through a walk down, up a little, up more and back to the top. */
function tucking(h) {
  sized(h);
  const tools = h.q('.hx-tools');
  return [50, 150, 140, 100, 300, 40].map((top) => {
    h.scroller.scrollTop = top;
    h.scroll();
    return tools.classList.contains('is-tucked');
  }).concat(h.view.style['--hx-fold'] || null);
}

const scenarios = {
  close: (h = open({ search: '?action=kept&event=5', filters: { action: 'kept' } })) => {
    const moves = watchMoves(h);
    const first = h.rows()[0];
    click(h.doc, first.querySelector('.hist-sum'));
    const out = { afterClick: state(first), url: h.url(), moving: moves.map((m) => ({ ...m })) };
    h.tick();
    return { ...out, moves, settled: state(first), urlAfter: h.url() };
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
    const url = h.url();
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
    return { chips: h.root.querySelectorAll('[data-menu]').map((b) => b.getAttribute('data-menu')), groups: h.root.querySelectorAll('[data-group]').length, asked: h.asked[0].url };
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
      sec('user.password_reset', 'ana', '/a/1', { summary: 'eve' }),
      play(), play({ finished: false, media_type: 'movie', episodes: [], line_title: 'Film' }),
      play({ episodes: [{ label: 'S1E2', finished: true }, { label: 'S1E3', finished: true }] }),
      item(1, { action: 'never_heard_of', action_label: 'never heard of' }),
    ].map((i) => open({ items: [i] }).line(0));
  },
  newest: async () => {
    const h = open({ search: '?event=5', page: 4 });
    const link = () => !!h.q('.hx-newest');
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
    h.asked[0].resolve(page([8], null));
    await settle();
    const hidden = !!h.q('.hx-newest');
    const d = open({ search: '?date=week&event=5' });
    d.asked[0].resolve(page([5], null, 60, 2));
    await settle();
    d.press('.hx-newest');
    return { hidden, steps: d.steps, asked: d.asked[d.asked.length - 1].url.replace(/\?.*/, '') + (/event|page/.test(d.asked[d.asked.length - 1].url) ? ' deep' : ''), own: d.own };
  },
  since: async () => {
    const h = open({ search: '?since=2026-11-01&until=2026-11-03&q=x', filters: { q: 'x', since: '2026-11-01', until: '2026-11-03' } });
    const out = { asked: h.asked.map((a) => a.url), ids: h.ids(), chip: h.q('[data-menu="date"]').textContent };
    h.pick('user', 'bob');
    out.next = h.asked[h.asked.length - 1].url;
    out.url = h.url();
    h.press('[data-clear="date"]');
    out.cleared = h.asked[h.asked.length - 1].url;
    const bad = open({ search: '?since=soon', filters: { since: 'soon' } });
    return { ...out, bad: bad.q('[data-menu="date"]').textContent };
  },
  search_focus: async () => {
    const h = open({});
    const asked = [];
    h.input.focus = (o) => { asked.push(o && o.preventScroll === true); };
    h.fire(h.doc, 'keydown', { key: '/', target: h.doc.body });
    h.input.value = 'x';
    click(h.doc, h.q('.hx-search .hx-x'));
    return { asked };
  },
  focus: async () => {
    const out = {};
    const only = async (key, sel) => {
      const h = open({});
      const b = h.rows()[0].querySelector(sel);
      b.focus();
      click(h.doc, b);
      h.asked[0].resolve(page([8], null));
      await settle();
      out[key] = h.doc.activeElement.isConnected && h.doc.activeElement.getAttribute('data-menu');
    };
    await only('title', '[data-title]');
    await only('user', '[data-user]');
    const h = open({ search: '?date=week' });
    const at = () => (h.doc.activeElement.isConnected ? h.doc.activeElement : h.doc.body);
    const retry = () => { const b = h.q('[data-hist-retry]'); b.focus(); click(h.doc, b); };
    h.asked[0].reject(new Error('no'));
    await settle();
    retry();
    out.waiting = at() === h.q('.hx-list');
    h.asked[1].reject(new Error('no'));
    await settle();
    out.again = at().hasAttribute('data-hist-retry');
    retry();
    h.asked[2].resolve(page([3], 'c', 30));
    await settle();
    out.loaded = at().closest('.hist') && at().closest('.hist').getAttribute('data-event');
    h.scroll();
    h.asked[h.asked.length - 1].reject(new Error('no'));
    await settle();
    const more = () => { const b = h.q('.hx-fail [data-hist-more]'); b.focus(); click(h.doc, b); };
    more();
    out.moreWaiting = h.more.contains(at());
    h.asked[h.asked.length - 1].reject(new Error('no'));
    await settle();
    out.moreAgain = h.more.contains(at());
    more();
    h.asked[h.asked.length - 1].resolve(page([], null));
    await settle();
    out.moreNone = at().closest('.hist') && at().closest('.hist').getAttribute('data-event');
    return out;
  },
  hash: () => {
    const h = open({ cursor: 'c1', total: 40 });
    h.back('#main-content');
    const same = h.asked.length;
    h.back('');
    const back = h.asked.length;
    h.pick('user', 'bob');
    h.back('');
    return { same, back, left: h.asked.length };
  },
  oddName: () => {
    const h = open({ search: '?user=constructor', filters: { user: 'constructor' }, items: [item(1, { action: 'sec:login.failed', is_security: true, actor: 'constructor', avatar: null })] });
    return { imgs: h.q('.hx-chips').querySelectorAll('img').length, line: h.line(0) };
  },
  titlesMore: async () => {
    const h = open({});
    const titled = (...t) => ({ options: t.map((v, i) => ({ value: v, label: v, count: 9 - i })), more: false });
    const menu = h.q('.hx-menu');
    const shown = () => menu.querySelectorAll('.hx-opt').filter((o) => !o.hidden).map((o) => o.getAttribute('data-v'));
    h.press('[data-menu="title"]');
    h.menus[0].resolve(titled('Low Tide', 'Aces', 'Tide Two'));
    await settle();
    const find = menu.querySelector('input');
    const type = (t) => { find.value = t; h.fire(find, 'input'); };
    type('aces');
    h.tick(200);
    h.menus[1].resolve(titled('Aces', 'Two Aces'));
    await settle();
    type('tide');
    const other = shown();
    type('');
    const cleared = shown();
    type('ace');
    const again = shown();
    const cross = menu.querySelector('.hx-x');
    click(h.doc, cross);
    const key = (k, target) => h.fire(menu, 'keydown', { key: k, target: target || h.doc.activeElement });
    const out = { other, cleared, again, crossed: find.value, kept: h.doc.activeElement === find };
    key('End');
    out.inField = h.doc.activeElement === find;
    key('ArrowDown');
    key('End');
    out.end = h.doc.activeElement.getAttribute('data-v');
    key('Home');
    out.home = h.doc.activeElement.getAttribute('data-v');
    return out;
  },
  menuKeysScroll: async () => {
    const h = open({});
    const menu = h.q('.hx-menu');
    h.press('[data-menu="title"]');
    h.menus[0].resolve({ options: ['A', 'B', 'C'].map((v) => ({ value: v, label: v, count: 1 })), more: false });
    await settle();
    const asked = [];
    menu.querySelectorAll('.hx-opt').forEach((o) => {
      const real = o.focus.bind(o);
      o.focus = (opts) => { asked.push(!!(opts && opts.preventScroll)); real(opts); };
    });
    const key = (k) => h.fire(menu, 'keydown', { key: k, target: h.doc.activeElement });
    key('ArrowDown');
    key('ArrowDown');
    key('End');
    key('Home');
    return { asked };
  },
  titlesRetry: async () => {
    const h = open({});
    const menu = h.q('.hx-menu');
    h.press('[data-menu="title"]');
    const fail = async () => { h.menus[h.menus.length - 1].reject(new Error('no')); await settle(); };
    await fail();
    const b = menu.querySelector('[data-titles-retry]');
    click(h.doc, b);
    const waiting = menu.contains(h.doc.activeElement);
    await fail();
    const again = h.doc.activeElement.hasAttribute('data-titles-retry');
    click(h.doc, h.doc.activeElement);
    h.menus[h.menus.length - 1].resolve({ options: [{ value: 'Aces', label: 'Aces', count: 1 }], more: false });
    await settle();
    return { waiting, again, asked: h.menus.length, focus: h.doc.activeElement.getAttribute('data-v'), left: !!menu.querySelector('[data-titles-retry]') };
  },
  lowScreen: () => {
    const h = open({});
    h.win.innerHeight = 390;
    h.press('[data-menu="user"]');
    const low = h.q('.hx-menu').style.maxHeight;
    h.press('[data-menu="user"]');
    h.win.innerHeight = 900;
    h.press('[data-menu="user"]');
    return { low, high: h.q('.hx-menu').style.maxHeight };
  },
  tabInEmpty: async () => {
    const h = open({});
    h.press('[data-menu="title"]');
    const menu = h.q('.hx-menu');
    const find = menu.querySelector('input');
    const tab = () => {
      find.focus();
      let held = false;
      h.fire(menu, 'keydown', { key: 'Tab', preventDefault() { held = true; } });
      return held && menu.contains(h.doc.activeElement);
    };
    const loading = tab();
    h.menus[0].resolve({ options: [], more: false });
    await settle();
    return { loading, empty: tab(), says: menu.querySelector('.hx-none').textContent };
  },
  hiddenName: async () => {
    const hidden = (id, n) => item(id, { actor: n, avatar: null, who: { value: '~former:' + n, name: '[hidden]', kind: 'former' } });
    const p = (n) => ({ value: '~former:' + n, username: '[hidden]', kind: 'former', avatar: null });
    PEOPLE.push(p('a@x.org'), p('b@x.org'));
    const h = open({ items: [hidden(1, 'a@x.org')] });
    const line = h.line(0);
    const acts = h.rows()[0].querySelectorAll('button').map((b) => b.textContent);
    h.press('[data-menu="user"]');
    h.menus[0].resolve({ options: [{ value: '~former:a@x.org', count: 3 }, { value: '~former:b@x.org', count: 8 }] });
    await settle();
    const rows = h.q('.hx-menu').querySelectorAll('.hx-opt').slice(-2).map((o) => o.querySelector('.n').textContent + ' ' + o.querySelector('small').textContent);
    click(h.doc, h.q('.hx-menu').querySelector('[data-v="~former:b@x.org"]'));
    const chip = h.q('[data-menu="user"]').children[1].textContent;
    h.back('?user=~former:gone@x.org');
    return { line, acts, rows, chip, typed: h.q('[data-menu="user"]').children[1].textContent };
  },
  outerSpace: () => {
    const h = open({});
    h.back('?title=%20%09Film%20');
    return h.asked[0].url;
  },
  resized: () => {
    const h = open({});
    h.press('[data-menu="user"]');
    const before = h.q('.hx-menu').style.maxHeight;
    h.win.innerHeight = 390;
    h.resize();
    const low = [h.q('.hx-menu').style.maxHeight, h.q('.hx-menu').classList.contains('on')];
    h.win.innerHeight = 900;
    h.resize();
    return { before, low, on: h.q('.hx-menu').classList.contains('on') };
  },
  titlesUnseen: async () => {
    const h = open({});
    const menu = h.q('.hx-menu');
    h.press('[data-menu="title"]');
    h.menus[0].resolve({ options: [{ value: 'Aces', label: 'Aces', count: 1 }], more: false });
    await settle();
    const find = menu.querySelector('input');
    const type = (t) => { find.value = t; h.fire(find, 'input'); };
    const rows = () => menu.querySelectorAll('.hx-opt-skel').length;
    type('ac');
    const matched = rows();
    type('tide');
    const waiting = rows();
    h.tick(200);
    h.menus[1].resolve({ options: [{ value: 'Low Tide', label: 'Low Tide', count: 1 }], more: false });
    await settle();
    const answered = rows();
    type('zz');
    h.tick(200);
    h.menus[2].reject(new Error('no'));
    await settle();
    return { matched, waiting, answered, failed: rows() };
  },
  tabEach: async () => {
    const h = open({});
    h.press('[data-menu="title"]');
    h.menus[0].resolve({ options: ['Aces', 'Low Tide'].map((t) => ({ value: t, label: t, count: 1 })), more: false });
    await settle();
    const menu = h.q('.hx-menu');
    const at = () => h.doc.activeElement.getAttribute('data-v') || h.doc.activeElement.tagName + (h.doc.activeElement.getAttribute('aria-label') || '');
    const walk = (shiftKey) => [1, 2, 3, 4].map(() => {
      let held = false;
      h.fire(menu, 'keydown', { key: 'Tab', shiftKey, preventDefault() { held = true; } });
      return held ? at() : 'left';
    });
    return { from: at(), on: walk(false), back: walk(true) };
  },
  plays: () => {
    const tv = (more) => item(1, { action: 'play', media_type: 'tv', line_title: 'Show', episodes: [{ label: 'S1E2' }], summary: 'S1E2', year: 2020, ...more });
    const film = (more) => item(1, { action: 'play', media_type: 'movie', line_title: 'Film', episodes: [], summary: '', year: 2023, ...more });
    const part = { finished: false, summary: 'stopped at 1 h of 2 h', progress: { fraction: 0.5 }, fields: [{ key: 'progress', label: 'Progress', value: '1 h of 2 h' }] };
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
    return { text, tools: h.q('.hx-tools').hidden, url: h.url(), asked: h.asked[h.asked.length - 1].url };
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
  details: () => {
    const h = open({});
    const fields = h.rows()[0].querySelectorAll('.hist-field').map((f) => f.querySelector('dt').textContent + ': ' + f.querySelector('dd').textContent);
    return { fields, utc: h.rows()[0].querySelector('.hist-field dd').getAttribute('title') };
  },
  unreadableTime: () => {
    const h = open({ items: [item(1, { fields: [{ ...at('not a time'), value: 'stored as sent' }] })] });
    const dd = h.rows()[0].querySelector('.hist-field dd');
    return { text: dd.textContent, title: dd.getAttribute('title') };
  },
  who: () => {
    const h = open({ items: [item(1, { actor: 'bob', avatar: null }), item(2, { actor: null, avatar: null })] });
    const only = h.rows().map((r) => { const b = r.querySelector('[data-user]'); return b && b.getAttribute('data-user'); });
    h.press('[data-user]');
    const chip = h.q('[data-menu="user"]');
    const out = { only, url: h.url(), chip: chip.children[1].textContent, gone: !!chip.querySelector('.is-gone') };
    h.pick('user', '~mediaman');
    const m = h.q('[data-menu="user"]');
    return { ...out, mediaman: m.children[1].textContent, gear: !!m.querySelector('.is-mm') };
  },
  counts: async () => {
    const h = open({ search: '?q=x&action=kept', filters: { q: 'x', action: 'kept' } });
    h.press('[data-menu="user"]');
    const opts = () => h.q('.hx-menu').querySelectorAll('.hx-opt');
    const before = opts();
    const names = before.map((o) => o.querySelector('.n').textContent);
    before[2].focus();
    h.menus[0].resolve({ options: PEOPLE.map((p, i) => ({ ...p, count: 1200 - i })) });
    await settle();
    const same = opts().every((o, i) => o === before[i]);
    const out = { asked: h.menus[0].url, names, same, counts: opts().map((o) => o.querySelector('small').textContent), focus: h.doc.activeElement === before[2] };
    h.press('[data-menu="user"]');
    h.press('[data-menu="user"]');
    h.press('[data-menu="user"]');
    h.press('[data-menu="user"]');
    h.menus[2].resolve({ options: [{ value: 'bob', count: 7 }] });
    await settle();
    h.menus[1].resolve({ options: [{ value: 'bob', count: 1 }] });
    await settle();
    return { ...out, late: opts().map((o) => o.querySelector('small').textContent) };
  },
  titles: async () => {
    const h = open({ search: '?user=bob', filters: { user: 'bob' } });
    const titled = (o) => ({ options: Object.keys(o).map((t) => ({ value: t, label: t, count: o[t] })), more: false });
    const menu = h.q('.hx-menu');
    const shown = () => menu.querySelectorAll('.hx-opt').filter((o) => !o.hidden).map((o) => o.getAttribute('data-v'));
    h.press('[data-menu="title"]');
    const waiting = menu.querySelectorAll('.hx-opt-skel').length;
    h.menus[0].resolve(titled({ 'Low Tide': 9, 'Aces': 4, 'Tide Two': 1 }));
    await settle();
    const all = shown();
    const find = menu.querySelector('input');
    find.value = 'tide';
    h.fire(find, 'input');
    const narrowed = shown();
    h.tick(200);
    find.value = 'zz';
    h.fire(find, 'input');
    const early = menu.querySelector('.hx-none').hidden;
    h.tick(200);
    h.menus[2].resolve(titled({}));
    await settle();
    h.menus[1].resolve(titled({ 'Low Tide': 9 }));
    await settle();
    return { waiting, all, narrowed, early, asked: h.menus.map((a) => a.url), last: shown(), none: !menu.querySelector('.hx-none').hidden };
  },
  titlesFail: async () => {
    const h = open({});
    h.press('[data-menu="title"]');
    h.menus[0].reject(new Error('no'));
    await settle();
    return h.q('.hx-menu').querySelector('.hx-none').textContent;
  },
  roundTrip: async () => {
    const h = open({});
    h.pick('user', 'bob');
    h.pick('action', 'plays');
    const pressed = () => h.root.querySelectorAll('[data-group]').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-group'));
    const out = { steps: h.steps.slice(), pressed: pressed() };
    h.back('?title=Low+Tide&date=week');
    out.askedBack = h.asked[h.asked.length - 1].url.replace(/&since=.*/, '');
    out.chips = h.root.querySelectorAll('.is-set').map((c) => c.querySelector('[data-menu]').getAttribute('data-menu'));
    out.pressedBack = pressed();
    out.own = h.own;
    return out;
  },
  eventRow: async () => {
    const listed = open({ search: '?event=6' });
    const shown = { asked: listed.asked.length, open: listed.rows().filter((r) => r.open).map((r) => r.getAttribute('data-event')) };
    const h = open({ search: '?event=7' });
    const asked = h.asked.map((a) => a.url);
    h.asked[0].resolve(page([6, 7, 8], null, 30, 2));
    await settle();
    return { shown, asked, open: h.rows().filter((r) => r.open).map((r) => r.getAttribute('data-event')), url: h.url(), own: h.own };
  },
  scroller: async () => {
    const h = open({ cursor: 'c1', total: 4 });
    const windowScroll = (h.listeners.scroll || []).length;
    h.scroller.scrollTop = 120;
    h.scroll();
    return { windowScroll, asked: h.asked.map((a) => a.url), stuck: h.q('.hx-tools').classList.contains('is-stuck') };
  },
  slashShown: () => {
    const h = open({ shown: false });
    const focused = [];
    h.input.focus = () => focused.push(h.win.MM.you.shown('history'));
    h.fire(h.doc, 'keydown', { key: '/', target: h.doc.body });
    h.show(true);
    h.fire(h.doc, 'keydown', { key: '/', target: h.doc.body });
    h.show(false);
    h.fire(h.doc, 'keydown', { key: '/', target: h.doc.body });
    return focused;
  },
  twice: () => {
    const h = open({ cursor: 'c1', total: 4 });
    h.run();
    h.scroll();
    return { asked: h.asked.length, rows: h.rows().length };
  },
  emptyTools: () => {
    const h = open({ items: [], total: 0 });
    const out = { tools: h.q('.hx-tools').hidden };
    h.back('?q=x');
    h.asked[0].resolve(page([], null, 0));
    return settle().then(() => ({ ...out, filtered: h.q('.hx-tools').hidden }));
  },
  kindCounts: async () => {
    const h = open({ search: '?q=x&user=bob', filters: { q: 'x', user: 'bob' } });
    const pills = () => h.root.querySelectorAll('[data-group]').map((b) => b.textContent);
    const first = h.kinds.map((k) => k.url);
    const counted = (o) => ({ options: Object.keys(o).map((v) => ({ value: v, label: v === 'kept' ? 'Kept' : 'Plays', count: o[v] })) });
    h.pick('action', 'plays');
    h.asked[h.asked.length - 1].resolve(page([8], null));
    await settle();
    const next = h.kinds[1].url;
    h.kinds[1].resolve(counted({ kept: 1200, plays: 3 }));
    await settle();
    h.kinds[0].resolve(counted({ kept: 1, plays: 1 }));
    await settle();
    return { first, next, pills: pills(), label: h.q('[data-group="kept"]').getAttribute('aria-label') };
  },
  kindZero: async () => {
    const h = open({});
    const pills = () => h.root.querySelectorAll('[data-group]').map((b) => [b.textContent, b.getAttribute('aria-label')]);
    const counted = (o) => ({ options: Object.keys(o).map((v) => ({ value: v, label: v === 'kept' ? 'Kept' : 'Plays', count: o[v] })) });
    h.kinds[0].resolve(counted({ kept: 4, plays: 0 }));
    await settle();
    const first = pills();
    h.pick('action', 'plays');
    h.asked[h.asked.length - 1].resolve(page([8], null));
    await settle();
    h.kinds[1].resolve(counted({ kept: 0, plays: 2 }));
    await settle();
    return { first, then: pills() };
  },
  tuck: () => tucking(open({ width: 402 })),
  tuckWide: () => tucking(open({})),
  tuckFocus: () => {
    const h = open({ width: 402 });
    sized(h);
    const tools = h.q('.hx-tools');
    [150, 300].forEach((top) => { h.scroller.scrollTop = top; h.scroll(); });
    const before = tools.classList.contains('is-tucked');
    h.fire(tools, 'focusin', { target: h.q('.hx-group') });
    return [before, tools.classList.contains('is-tucked')];
  },
  dayStuck: () => {
    const h = open({ items: [item(1, { fields: [at('2026-11-25T01:00:00Z')] }), item(2, { fields: [at('2026-11-24T10:00:00Z')] }), item(3, { fields: [at('2026-11-20T10:00:00Z')] })] });
    const heads = h.root.querySelectorAll('.hx-day__top');
    const look = (tops) => {
      heads.forEach((d, i) => { d.getBoundingClientRect = () => ({ top: tops[i], bottom: tops[i] + 30 }); });
      h.scroller.scrollTop = 500;
      h.scroll();
      h.tick();
      return { stuck: heads.map((d) => d.classList.contains('is-stuck')), pushed: heads.map((d) => d.classList.contains('is-pushed')), tools: h.q('.hx-tools').classList.contains('has-day') };
    };
    // The scroller's top is at 60 and the heads stick 52px below it; the last, the next day's head
    // pushing the stuck one up.
    return [look([-40, 112, 400]), look([200, 400, 800]), look([-40, 90, 130])];
  },
  sumPlace: async () => {
    const h = open({});
    const where = () => (h.q('.hx-sum').parentNode.matches('.hx-day__top') ? 'day' : h.q('.hx-sum').parentNode.className);
    const out = [where()];
    h.back('?q=x');
    h.tick(600);
    out.push(where(), h.q('.hx-sum').classList.contains('is-waiting'));
    h.asked[0].resolve(page([8, 9], null, 2));
    await settle();
    out.push(where(), h.q('.hx-day__top').querySelectorAll('.hx-sum').length);
    h.back('?q=y');
    h.asked[h.asked.length - 1].resolve(page([], null, 0));
    await settle();
    out.push(where());
    return out;
  },
  record: () => {
    const h = open({});
    const dds = h.rows()[0].querySelectorAll('.hist-field dd');
    return dds.map((dd) => (dd.firstElementChild ? dd.firstElementChild.tagName + '.' + dd.firstElementChild.className : null));
  },
  dateButton: () => {
    const h = open({});
    h.press('[data-menu="date"]');
    const form = h.q('.hx-menu form');
    const [a] = form.querySelectorAll('input');
    const go = form.querySelector('button');
    const out = [go.className, a.className, !!go.disabled];
    a.value = '2026-11-05';
    h.fire(a, 'input');
    out.push(!!go.disabled);
    a.value = '';
    h.fire(a, 'input');
    out.push(!!go.disabled);
    return out;
  },
  toolsHeight: () => {
    const h = open({});
    const tools = h.q('.hx-tools');
    Object.defineProperty(tools, 'offsetHeight', { get: () => 196 });
    h.sizes.forEach((fn) => fn());
    return h.view.style['--hx-tools'] || null;
  },
  /* The row of kinds wider than the pane, then scrolled to its end, then narrow enough to fit. */
  kindArrows: (reduced) => {
    const h = open({ reduced });
    const g = h.q('.hx-groups');
    let wide = 1300;
    Object.defineProperty(g, 'clientWidth', { get: () => 1000 });
    Object.defineProperty(g, 'scrollWidth', { get: () => wide });
    Object.defineProperty(g, 'offsetTop', { get: () => 58 });
    g.scrollLeft = 0;
    const scrolled = [];
    g.scrollBy = (o) => scrolled.push(o);
    const ends = () => ['has-prev', 'has-next'].filter((c) => g.classList.contains(c));
    const arrows = h.root.querySelectorAll('.hx-kinds-arrow');
    h.sizes.forEach((fn) => fn());
    const out = { start: ends(), top: h.q('.hx-tools').style['--hx-kinds'] || null };
    out.arrows = arrows.map((b) => [b.className, b.getAttribute('aria-hidden'), b.getAttribute('tabindex'), b.previousElementSibling === g || b.previousElementSibling.matches('.hx-kinds-arrow')]);
    click(h.doc, arrows[1]);
    out.scrolled = scrolled.splice(0);
    g.scrollLeft = 300;
    h.fire(g, 'scroll');
    out.end = ends();
    click(h.doc, arrows[0]);
    out.back = scrolled.splice(0);
    wide = 1000;
    g.scrollLeft = 0;
    h.sizes.forEach((fn) => fn());
    out.fits = ends();
    return out;
  },
  kindArrowsReduced: () => scenarios.kindArrows(true),
  handTyped: () => ['?since=20261110', '?since=2026-11-12%2010:00', '?since=2026-02-30', '?until=2026-02-30T10:00', '?since=2026-11-01T24:00', '?since=2026-11-01'].map((search) => {
    const h = open({ search });
    return [h.q('[data-menu="date"]').textContent, h.asked.map((a) => a.url), h.url().split('/history')[1]];
  }),
};

Promise.resolve(scenarios[process.argv[2]]()).then((o) => console.log(JSON.stringify(o)));
