// Runs the pane over Watch (watch/you.js) with the real address.js, modal-a11y.js and ui-feedback.js
// against the fake DOM and a fake history, and prints one scenario's outcome as JSON.
// Usage: node watch_you_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

// What is inert is so by its property here, as the attribute reflects it in a browser.
const baseClosest = Node.prototype.closest;
Node.prototype.closest = function (sel) {
  if (sel !== '[inert]') return baseClosest.call(this, sel);
  for (let a = this; a && a.nodeType === 1; a = a.parentNode) if (a.inert) return a;
  return null;
};
Node.prototype.removeChild = function (c) { c.remove(); return c; };
Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', !!on); },
});

const view = (name, title, kids, wide) => ['div', Object.assign({ class: 'you-view', 'data-view': name, 'data-title': title }, wide ? { 'data-wide': '' } : {}), ...kids];
const FRAGMENTS = {
  '/account': ['div', { 'data-you-group': 'profile', 'data-styles': '/s/password.css?v=1', 'data-scripts': '/j/reauth.js?v=1 /j/account.js?v=1' },
    view('profile', 'Profile', [['button', { id: 'to-email', 'data-you-go': 'profile/email' }], ['a', { id: 'to-history', href: '/history?action=plays&bogus=1' }], ['button', { id: 'last' }]]),
    view('profile/email', 'Email', [['input', { id: 'email' }], ['a', { id: 'out', href: '/downloads' }],
      ['form', { id: 'loose', method: 'post' }], ['form', { id: 'logout', method: 'post', action: '/api/auth/logout' }]])],
  '/history': ['div', { 'data-you-group': 'history', 'data-styles': '', 'data-scripts': '/j/reauth.js?v=1 /j/history.js?v=1' }, view('history', 'History', [], true)],
  '/settings': ['div', { 'data-you-group': 'settings', 'data-styles': '/s/password.css?v=1', 'data-scripts': '/j/settings.js?v=1' }, view('settings', 'Settings', [['button', { id: 'field' }]], true)],
};

function open(url, opts = {}) {
  const tab = { entries: [{ url, state: null }], index: 0, calls: [] };
  const doc = makeDocument();
  doc.head = doc.documentElement.insertBefore(doc.createElement('head'), doc.body);
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).slice().forEach((fn) => fn(e));
  const timers = [];
  const winListeners = {};
  const clock = { now: 1000 };
  const fetched = [];
  const assets = [];
  const toasts = [];
  const told = [];
  const assigned = [];
  const here = () => new URL(tab.entries[tab.index].url, 'http://mm');

  doc.body.appendChild(build(doc, ['header', { class: 'nav-glass', id: 'nav' }, ['button', { class: 'profile-btn', id: 'btn', 'aria-expanded': 'false' }]]));
  doc.body.appendChild(build(doc, ['main', { id: 'main-content' }, ['div', { id: 'watch-browse' },
    ['a', { id: 'open-settings', href: '/settings#setg-general' }], ['a', { id: 'card', href: '/#title=movie-1' }]]]));
  doc.body.appendChild(build(doc, ['div', Object.assign({ class: 'you', id: 'you', hidden: '' }, opts.viewer ? {} : { 'data-admin': '' }),
    ['div', { class: 'you__veil', id: 'veil', 'data-you-close': '' }],
    ['section', { class: 'you__panel', id: 'panel', role: 'dialog', tabindex: '-1' },
      ['div', { class: 'you__grab', id: 'grab' }],
      ['header', { class: 'you__bar', id: 'bar' },
        ['button', { class: 'you__back', id: 'up', 'data-you-back': '', hidden: '' }, ['span', { class: 'you__back-label', id: 'up-label' }]],
        ['h2', { class: 'you__title', id: 'title', tabindex: '-1' }],
        ['button', { class: 'you__close', id: 'x', 'data-you-close': '' }]],
      ['div', { class: 'you__views', id: 'views' }]]]));
  const $ = (id) => doc.getElementById(id);

  const media = (q) => ({ matches: /reduce/.test(q) ? !!opts.calm : /min-width/.test(q) ? !!opts.wide : !!opts.phone, addEventListener() {} });
  const go = (by) => {
    const to = tab.index + by;
    if (to < 0 || to >= tab.entries.length || by === 0) return;
    tab.index = to;
    (winListeners.popstate || []).slice().forEach((fn) => fn({}));
  };
  const server = opts.server || (() => null);
  const ctx = {
    document: doc, console, JSON, URL, URLSearchParams, Object, Array, Math, Error, Infinity, Promise, WeakMap,
    navigator: { platform: 'Linux' },
    performance: { now: () => clock.now },
    requestAnimationFrame: (fn) => timers.push({ fn, ms: 0, on: true }),
    setTimeout: (fn, ms) => { timers.push({ fn, ms, on: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].on = false; },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn); },
    scrollTo() {}, scrollY: 0, innerHeight: 900, innerWidth: 1200,
    getComputedStyle: (el) => ({ visibility: el.closest('[hidden]') ? 'hidden' : 'visible', transitionDuration: '0s', transitionDelay: '0s' }),
    matchMedia: media,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    Date: { now: () => clock.now },
    sessionStorage: { getItem: () => null, setItem() {} },
    fetch: (u) => {
      fetched.push(u);
      const page = u.split('?')[0];
      const said = server(page) || { status: 200 };
      return Promise.resolve({ status: said.status, redirected: !!said.to, url: 'http://mm' + (said.to || u), text: () => Promise.resolve(JSON.stringify(FRAGMENTS[page])) });
    },
    history: {
      scrollRestoration: 'auto',
      get state() { const s = tab.entries[tab.index].state; return s === null ? null : JSON.parse(JSON.stringify(s)); },
      pushState(state, _t, u) {
        tab.calls.push('push ' + u);
        tab.entries = tab.entries.slice(0, tab.index + 1).concat({ url: u, state: JSON.parse(JSON.stringify(state)) });
        tab.index++;
      },
      replaceState(state, _t, u) { tab.entries[tab.index] = { url: u, state: JSON.parse(JSON.stringify(state)) }; },
      go(by) { tab.calls.push('go ' + by); setImmediate(() => go(by)); },
      back() { this.go(-1); },
    },
    location: {
      get pathname() { return here().pathname; },
      get search() { return here().search; },
      get hash() { return here().hash; },
      get href() { return here().href; },
      assign: (u) => assigned.push(u),
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  // Each script added to the page loads a moment later; what was loaded by then is noted.
  const append = doc.head.appendChild.bind(doc.head);
  doc.head.appendChild = (el) => {
    append(el);
    assets.push(el.src || el.href);
    setImmediate(() => { if (el.src) loaded.push(el.src); if (el.onload) el.onload(); });
    return el;
  };
  const loaded = [];
  ['core/dom.js', 'modal-a11y.js', 'ui-feedback.js', 'watch/core.js', 'watch/address.js', 'watch/you.js']
    .forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.UIFeedback.error = (m) => toasts.push(m);
  ctx.MM.you.insert = (html) => {
    const node = build(doc, JSON.parse(html));
    node.querySelectorAll('.you-view').forEach((v) => { v.inert = true; });
    $('views').appendChild(node);
    return node;
  };
  ['mm:you-shown', 'mm:you-hidden'].forEach((t) => doc.addEventListener(t, (e) => told.push(t.slice(7) + ' ' + e.detail.group + ' ' + e.detail.view)));

  const event = (type, target, extra) => {
    const e = { type, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...extra };
    for (let a = target; a; a = a.parentNode) (a.listeners[type] || []).slice().forEach((fn) => fn(e));
    (doc.listeners[type] || []).slice().forEach((fn) => fn(e));
    (winListeners[type] || []).slice().forEach((fn) => fn(e));
    return e;
  };
  const late = () => { for (let n = 0; n < 5; n++) timers.filter((t) => t.on).forEach((t) => { t.on = false; t.fn(); }); };
  const api = {
    doc, tab, $, ctx, fetched, assets, loaded, toasts, told, assigned, timers, clock, winListeners,
    you: ctx.MM.you,
    at: () => tab.entries[tab.index].url,
    settle: async () => { for (let i = 0; i < 4; i++) { await settle(); late(); } },
    step: async (by) => { go(by); await api.settle(); },
    click: async (id, extra) => { click(doc, $(id), extra); await api.settle(); },
    key: async (key, extra) => { const e = event('keydown', doc.activeElement, { key, ...extra }); await api.settle(); return e; },
    event,
    focused: () => { const a = doc.activeElement; return a.id || a.textContent || a.tagName; },
    title: () => $('title').textContent,
    // The question on show, and its answer.
    asked: () => doc.querySelector('.ui-confirm-overlay:not(.is-leaving)'),
    answer: async (yes) => {
      const q = api.asked();
      clock.now += 1000;
      const btns = q.querySelectorAll('button');
      click(doc, btns[yes ? 1 : 0], { detail: 1 });
      await api.settle();
    },
  };
  ctx.MMWatch.address.start();
  return api;
}

/* The pane open on Profile's email, with what was typed in it not yet saved. */
async function dirtyEmail(opts) {
  const p = open('/', opts);
  const state = { dirty: true, discarded: 0 };
  p.you.guard('profile', {
    dirty: (from, to) => state.dirty && from === 'profile/email' && to !== 'profile/email',
    discard: () => { state.dirty = false; state.discarded++; },
    title: 'Discard what you typed?',
    body: 'Your email and password stay as they were.',
  });
  p.$('btn').focus();
  p.you.go('profile');
  await p.settle();
  await p.click('to-email');
  return { p, state };
}

const scenarios = {
  // A link to the pane's address, as a reload or a shared link: Back closes it and stays on Watch.
  async deepLinkBack() {
    const p = open('/?you=profile');
    await p.settle();
    const opened = { entries: p.tab.entries.map((e) => e.url), shown: p.you.shown('profile'), title: p.title(), focus: p.focused() };
    await p.step(-1);
    return { opened, at: p.at(), shown: p.you.shown('profile'), hidden: p.$('you').hasAttribute('hidden'), focus: p.focused(), told: p.told };
  },

  async backWhileDirty() {
    const { p, state } = await dirtyEmail();
    await p.step(-1);
    const asked = { at: p.at(), title: p.title(), q: p.asked() && p.asked().querySelector('.ui-confirm-title').textContent, focus: p.focused() };
    await p.answer(false);
    const kept = { at: p.at(), title: p.title(), discarded: state.discarded, focus: p.focused() };
    await p.step(-1);
    await p.answer(true);
    return { asked, kept, discarded: { at: p.at(), title: p.title(), discarded: state.discarded } };
  },

  async everyWayOut() {
    const { p, state } = await dirtyEmail({ phone: true });
    const ways = {
      close: () => p.click('x'),
      veil: () => p.click('veil'),
      escape: () => p.key('Escape'),
      up: () => p.click('up'),
      step: async () => { p.you.go('history'); await p.settle(); },
      link: () => p.click('out'),
      pull: async () => {
        const send = (type, y, t) => (p.$('grab').listeners[type] || []).forEach((fn) => fn({ type, pointerId: 1, button: 0, clientY: y, timeStamp: t, target: p.$('grab'), currentTarget: p.$('grab') }));
        send('pointerdown', 100, 0); send('pointermove', 400, 500); send('pointerup', 400, 600);
        await p.settle();
      },
      back: () => p.step(-1),
    };
    const out = {};
    for (const name of Object.keys(ways)) {
      await ways[name]();
      out[name] = [!!p.asked(), p.at()];
      if (p.asked()) await p.answer(false);
    }
    const leave = p.event('beforeunload', p.doc.body, { returnValue: undefined });
    out.unload = [leave.defaultPrevented, leave.returnValue];
    state.dirty = false;
    out.clean = p.event('beforeunload', p.doc.body).defaultPrevented;
    out.assigned = p.assigned;
    return out;
  },

  async tabWraps() {
    const p = open('/');
    p.you.go('profile');
    await p.settle();
    const opened = p.focused();
    p.$('last').focus();
    await p.key('Tab');
    const after = p.focused();
    await p.key('Tab', { shiftKey: true });
    return { opened, after, back: p.focused(), page: [p.$('nav').inert, p.$('watch-browse').getAttribute('aria-hidden')] };
  },

  async focusReturns() {
    const p = open('/');
    p.$('open-settings').focus();
    await p.click('open-settings');
    const opened = [p.at(), p.title(), p.fetched.slice()];
    await p.key('Escape');
    const list = [p.at(), p.title()];
    await p.key('Escape');
    const fromLink = [p.at(), p.focused()];
    const q = open('/?you=history');
    await q.settle();
    await q.click('x');
    return { opened, list, fromLink, deep: [q.at(), q.focused()] };
  },

  oldHashes() {
    const at = (u, opts) => { const p = open(u, opts); return p.at(); };
    return {
      section: at('/?you=settings#setg-libraries'),
      card: at('/?you=settings#intg-openai'),
      plex: at('/?you=profile#plex'),
      unknown: at('/?you=nowhere&h=q%3Dx'),
      history: at('/?you=history&h=action%3Dplays'),
      viewer: at('/?you=settings&q=dune#setg-users', { viewer: true }),
    };
  },

  async loginRedirect() {
    const p = open('/?you=settings/libraries', { server: () => ({ status: 200, to: '/login' }) });
    await p.settle();
    return p.assigned;
  },

  async didNotLoad() {
    const p = open('/', { server: (page) => (page === '/history' ? { status: 500 } : null) });
    p.you.go('history');
    await p.settle();
    const first = [p.at(), p.title(), p.toasts.slice()];
    p.you.go('history');
    await p.settle();
    return { first, tries: p.fetched.filter((u) => u.startsWith('/history')).length };
  },

  async viewerSettings() {
    const p = open('/?you=settings', { viewer: true });
    await p.settle();
    p.you.go('settings');
    await p.settle();
    await p.click('open-settings');
    return { at: p.at(), fetched: p.fetched, shown: p.you.shown('settings'), entries: p.tab.entries.length };
  },

  async assetsOnce() {
    const p = open('/');
    p.you.go('profile');
    await p.settle();
    await p.click('to-history');
    p.you.go('profile');
    await p.settle();
    p.you.go('history');
    await p.settle();
    return { fetched: p.fetched, assets: p.assets, loaded: p.loaded, at: p.at() };
  },

  async hideTimer() {
    const ms = async (calm) => {
      const p = open('/', { calm });
      p.you.go('profile');
      await p.settle();
      p.you.close();
      await settle();
      return p.timers.filter((t) => t.on && t.ms > 0).map((t) => t.ms);
    };
    return { full: await ms(false), calm: await ms(true) };
  },

  async historyAddress() {
    const p = open('/');
    p.you.go('history');
    await p.settle();
    p.ctx.MM.history.address.write({ q: 'dune 2', action: 'plays', user: '', bogus: 'x' }, 'push');
    const pushed = [p.at(), p.tab.entries.length, p.ctx.MM.history.address.read().get('q')];
    p.ctx.MM.history.address.write(new URLSearchParams('page=2'), 'replace');
    return { pushed, replaced: [p.at(), p.tab.entries.length] };
  },

  async submits() {
    const { p } = await dirtyEmail();
    return { loose: p.event('submit', p.$('loose')).defaultPrevented, logout: p.event('submit', p.$('logout')).defaultPrevented };
  },

  async shownAndScroller() {
    const p = open('/');
    p.you.go('settings/integrations/openai');
    await p.settle();
    return {
      shown: [p.you.shown('settings'), p.you.shown('profile')],
      scroller: p.you.scroller('settings/integrations/openai').getAttribute('data-view'),
      told: p.told,
      wide: p.$('you').classList.contains('is-wide'),
      up: [p.$('up').hasAttribute('hidden'), p.$('up-label').textContent],
    };
  },

  // The pane opened over a title's page closes it: the title's hash is not in the pane's address.
  async overATitle() {
    const p = open('/#title=movie-1');
    p.you.go('profile');
    await p.settle();
    return p.at();
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
Promise.resolve(scenarios[name]()).then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
