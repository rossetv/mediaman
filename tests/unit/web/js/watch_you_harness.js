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
// Each animation asked for, kept running until the scenario finishes it.
const animations = [];
Node.prototype.animate = function (frames, opts) {
  const a = { el: this, frames, opts, onfinish: null, cancelled: false, finish() { if (this.onfinish) this.onfinish(); }, cancel() { this.cancelled = true; } };
  animations.push(a);
  return a;
};

// The motion tokens as the stylesheet declares them, read by the pane from the computed style.
const TOKENS = {};
fs.readFileSync(path.resolve(JS, '../css/_tokens.css'), 'utf8').replace(/(--ease-[\w-]+)\s*:\s*([^;]+);/g, (_m, k, v) => { TOKENS[k] = v.trim(); });

// The panel's width as the stylesheet sets it: the screen's on a phone, else 1100 px held or wider (Settings),
// 880 px wide (History), 440 px narrow; a view as wide as the panel it ends in, pinned to its right.
function layout(doc, opts) {
  const root = doc.getElementById('you');
  const has = (c) => root.classList.contains(c);
  const panelW = () => (opts.phone ? 402 : has('is-sizing') || has('is-wider') ? 1100 : has('is-wide') ? 880 : 440);
  Object.defineProperty(doc.getElementById('panel'), 'offsetWidth', { get: panelW });
  doc.getElementById('panel').getBoundingClientRect = () => ({ top: 12, left: 1428 - panelW(), width: panelW(), height: 876 });
  Object.defineProperty(Node.prototype, 'offsetLeft', {
    configurable: true,
    get() {
      if (!this.classList.contains('you-view')) return 0;
      const inner = panelW() - 2;
      return inner - (opts.phone ? inner : this.hasAttribute('data-wider') ? 1098 : this.hasAttribute('data-wide') ? 878 : 438);
    },
  });
}

const view = (name, title, kids, wide) => ['div', Object.assign({ class: 'you-view', 'data-view': name, 'data-title': title }, wide ? { ['data-' + wide]: '' } : {}), ...kids];
const FRAGMENTS = {
  '/account': ['div', { 'data-you-group': 'profile', 'data-styles': '/static/css/password.css?v=1', 'data-scripts': '/static/js/reauth.js?v=1 /static/js/account.js?v=1' },
    view('profile', 'Profile', [['button', { id: 'to-email', 'data-you-go': 'profile/email' }], ['a', { id: 'to-history', href: '/history?action=plays&bogus=1' }], ['button', { id: 'last' }],
      ['div', { id: 'inner', class: 'scroller' }, ['p', { id: 'deep' }]]]),
    view('profile/email', 'Email', [['input', { id: 'email' }], ['a', { id: 'out', href: '/downloads' }],
      ['form', { id: 'loose', method: 'post' }], ['form', { id: 'logout', method: 'post', action: '/api/auth/logout' }], ['div', { class: 'yo-foot' }]]),
    view('profile/subtitles', 'Subtitles', [['button', { id: 'to-sublang', 'data-you-go': 'profile/subtitles/language' }]]),
    view('profile/subtitles/language', 'Subtitle language', [['button', { id: 'lang' }]])],
  '/history': ['div', { 'data-you-group': 'history', 'data-styles': '', 'data-scripts': '/static/js/reauth.js?v=1 /static/js/history.js?v=1' }, view('history', 'History', [], 'wide')],
  '/settings': ['div', { 'data-you-group': 'settings', 'data-styles': '/static/css/password.css?v=1', 'data-scripts': '/static/js/settings.js?v=1' }, view('settings', 'Settings', [['button', { id: 'field' }],
    ['section', { class: 'stx-sec', 'data-sec': 'integrations', 'data-title': 'Integrations' }]], 'wider')],
};

function open(url, opts = {}) {
  const tab = { entries: [{ url, state: null }], index: 0, calls: [] };
  const doc = makeDocument();
  doc.head = doc.documentElement.insertBefore(doc.createElement('head'), doc.body);
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).slice().forEach((fn) => fn(e));
  const timers = [];
  const idles = [];
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
        ['button', { class: 'you__back is-off', id: 'up', 'data-you-back': '' }, ['span', { class: 'you__back-label', id: 'up-label' }]],
        ['h2', { class: 'you__title', id: 'title', tabindex: '-1' }],
        ['button', { class: 'you__close', id: 'x', 'data-you-close': '' }]],
      ['div', { class: 'you__views', id: 'views' }]]]));
  const $ = (id) => doc.getElementById(id);
  layout(doc, opts);

  const widths = [];
  const media = (q) => ({
    get matches() { return /reduce/.test(q) ? !!opts.calm : /min-width/.test(q) ? !!opts.wide : !!opts.phone; },
    addEventListener: (t, fn) => { if (/min-width/.test(q)) widths.push(fn); },
  });
  // The window made wide or narrow, and what listens to its width told.
  const resize = (wide) => { opts.wide = wide; widths.forEach((fn) => fn({ matches: wide })); };
  const go = (by) => {
    const to = tab.index + by;
    if (to < 0 || to >= tab.entries.length || by === 0) return;
    tab.index = to;
    (winListeners.popstate || []).slice().forEach((fn) => fn({}));
  };
  const server = opts.server || (() => null);
  const fragments = Object.assign({}, FRAGMENTS, opts.fragments);
  // With opts.hold, every fragment waits until release() lets them all through.
  const held = [];
  const release = () => held.splice(0).forEach((fn) => fn());
  const ctx = {
    document: doc, console, JSON, URL, URLSearchParams, Object, Array, Math, Error, Infinity, Promise, WeakMap,
    navigator: { platform: 'Linux' },
    performance: { now: () => clock.now },
    requestAnimationFrame: (fn) => timers.push({ fn, ms: 0, on: true }),
    // The page is idle only when a scenario says so.
    requestIdleCallback: (fn) => { idles.push(fn); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms, on: true }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].on = false; },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener: (t, fn) => { winListeners[t] = (winListeners[t] || []).filter((f) => f !== fn); },
    scrollTo() {}, scrollY: 0, innerHeight: 900, innerWidth: 1200,
    getComputedStyle: (el) => ({ visibility: el.closest('[hidden]') ? 'hidden' : 'visible', transitionDuration: '0s', transitionDelay: '0s', getPropertyValue: (k) => TOKENS[k] || '' }),
    matchMedia: media,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    Date: { now: () => clock.now },
    sessionStorage: { getItem: () => null, setItem() {} },
    fetch: (u) => {
      fetched.push(u);
      const page = u.split('?')[0];
      const said = server(page) || { status: 200 };
      const res = { status: said.status, redirected: !!said.to, url: 'http://mm' + (said.to || u), text: () => Promise.resolve(JSON.stringify(fragments[page])) };
      return opts.hold ? new Promise((r) => held.push(() => r(res))) : Promise.resolve(res);
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
  ['core/api.js', 'core/dom.js', 'modal-a11y.js', 'ui-feedback.js', 'watch/core.js', 'watch/address.js', 'watch/you.js']
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
    doc, tab, $, ctx, fetched, assets, loaded, toasts, told, assigned, timers, clock, winListeners, resize, release, animations,
    idle: async () => { idles.splice(0).forEach((fn) => fn({ didTimeout: false, timeRemaining: () => 50 })); await api.settle(); },
    you: ctx.MM.you,
    at: () => tab.entries[tab.index].url,
    settle: async () => { for (let i = 0; i < 4; i++) { await settle(); late(); } },
    step: async (by) => { go(by); await api.settle(); },
    click: async (id, extra) => { click(doc, $(id), extra); await api.settle(); },
    key: async (key, extra) => { const e = event('keydown', doc.activeElement, { key, ...extra }); await api.settle(); return e; },
    event,
    upHidden: () => $('up').classList.contains('is-off'),
    // The sheet pulled down by its handle, far enough to close it.
    pull: async () => {
      const send = (type, y, t) => ($('grab').listeners[type] || []).forEach((fn) => fn({ type, pointerId: 1, button: 0, clientY: y, timeStamp: t, target: $('grab'), currentTarget: $('grab') }));
      send('pointerdown', 100, 0); send('pointermove', 400, 500); send('pointerup', 400, 600);
      await api.settle();
    },
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
      pull: () => p.pull(),
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
    const at = async (url) => {
      const p = open(url, { server: () => ({ status: 200, to: '/login' }) });
      await p.settle();
      return p.assigned;
    };
    return { plain: await at('/?you=settings/libraries'), hash: await at('/?you=settings/libraries&h=q%3Dx#title=movie-1') };
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
      wide: p.$('you').classList.contains('is-wider'),
      up: [p.upHidden(), p.$('up-label').textContent],
    };
  },

  // A Settings section on a narrow pane, then on a wide one, and a section not in the fragment.
  async sectionTitle() {
    const bar = (p) => [p.title(), p.upHidden(), p.$('up-label').textContent];
    const p = open('/');
    p.you.go('settings/integrations/openai');
    await p.settle();
    const narrow = bar(p);
    p.resize(true);
    const widened = bar(p);
    p.resize(false);
    const narrowed = bar(p);
    p.you.go('settings/general');
    await p.settle();
    return { narrow, widened, narrowed, missing: bar(p) };
  },

  // The pane opened over a title's page closes it: the title's hash is not in the pane's address.
  async overATitle() {
    const p = open('/#title=movie-1');
    p.you.go('profile');
    await p.settle();
    return p.at();
  },
  // A press that starts in the panel and ends on the veil is a drag, not a way out; one on the veil is.
  async veilDrag() {
    const p = open('/');
    p.you.go('profile');
    await p.settle();
    p.event('pointerdown', p.$('title'));
    await p.click('veil');
    const dragged = [p.you.shown('profile'), p.at()];
    p.event('pointerdown', p.$('veil'));
    await p.click('veil');
    return { dragged, pressed: [p.you.shown('profile'), p.at()] };
  },

  // With nothing unsaved, as while a picture is sent, Up and the close button still work.
  // A view a step below another: Up and Escape go back to that one, not to the hub.
  async nestedProfile() {
    const p = open('/?you=profile/subtitles');
    await p.settle();
    await p.click('to-sublang');
    const deep = [p.at(), p.title(), p.upHidden()];
    await p.key('Escape');
    const escaped = [p.at(), p.title()];
    await p.click('to-sublang');
    await p.click('up');
    const upped = [p.at(), p.title()];
    await p.click('up');
    return { deep, escaped, upped, hub: [p.at(), p.title(), p.upHidden()] };
  },

  async stillCloses() {
    const p = open('/');
    p.you.guard('profile', { dirty: () => false, discard() {} });
    p.you.go('profile/email');
    await p.settle();
    const ups = [p.upHidden(), !!p.$('up').disabled];
    await p.click('up');
    const upped = [p.at(), p.title(), !!p.asked()];
    p.you.go('profile/email');
    await p.settle();
    await p.click('x');
    return { ups, upped, closed: [p.at(), p.you.shown('profile'), !!p.asked(), !!p.$('x').disabled] };
  },

  // Closed by Back and opened again by Forward before History came: it is shown once, when it comes.
  async forwardWhileLoading() {
    const p = open('/', { hold: true });
    p.you.go('history');
    await p.settle();
    await p.step(-1);
    await p.step(1);
    const waiting = [p.at(), p.you.shown('history'), p.$('you').classList.contains('is-loading')];
    p.release();
    await p.settle();
    return { waiting, came: [p.title(), p.$('you').classList.contains('is-loading')], told: p.told, fetched: p.fetched };
  },

  // The sheet pulled shut before History came stays shut when it comes.
  async pullWhileLoading() {
    const p = open('/', { hold: true, phone: true });
    p.you.go('history');
    await p.settle();
    await p.pull();
    const pulled = [p.at(), p.you.shown('history')];
    p.release();
    await p.settle();
    const on = p.doc.querySelector('.you-view.is-on');
    return { pulled, after: [p.at(), p.you.shown('history'), p.$('you').hasAttribute('hidden'), !!on], told: p.told };
  },

  // A fragment that names styles or scripts from anywhere but the app's own /static/ loads none of them.
  async foreignAssets() {
    const bad = ['//evil.test/x', 'https://evil.test/x', '/static//evil.test/x', '/staticky/x', 'javascript:x', '/static/\\evil.test'];
    const p = open('/', { fragments: { '/account': ['div', { 'data-you-group': 'profile', 'data-styles': bad.map((u) => u + '.css').join(' ') + ' /static/css/ok.css',
      'data-scripts': '/static/js/ok.js ' + bad.map((u) => u + '.js').join(' ') }, view('profile', 'Profile', [])] } });
    p.you.go('profile');
    await p.settle();
    return { assets: p.assets, toasts: p.toasts };
  },

  // History is asked for with only its own keys from h.
  async historyKeys() {
    const p = open('/?you=history&h=action%3Dplays%26pane%3D0%26evil%3D1');
    await p.settle();
    return p.fetched;
  },

  // Profile is fetched once the page is idle, and History and Settings once the hub is on show; each
  // fetched ahead is used, not fetched again, unless History's filters changed meanwhile.
  async prefetch() {
    const run = async (viewer) => {
      const p = open('/', { viewer });
      await p.settle();
      const before = p.fetched.slice();
      await p.idle();
      const idle = p.fetched.slice();
      p.you.go('profile');
      await p.settle();
      const opened = [p.title(), p.fetched.length];
      await p.idle();
      const hub = p.fetched.slice();
      await p.click('to-history');
      return { before, idle, opened, hub, filtered: p.fetched.slice(hub.length), title: p.title(), toasts: p.toasts };
    };
    return { admin: await run(false), viewer: await run(true) };
  },

  // What was fetched ahead, as the pointer came to the profile button, and did not come says nothing, and is asked for again when it is opened.
  async prefetchFails() {
    let down = true;
    const p = open('/', { server: (page) => (down && page === '/account' ? { status: 500 } : null) });
    p.event('pointerenter', p.$('btn'));
    await p.settle();
    const quiet = [p.toasts.slice(), p.you.shown('profile'), p.at()];
    down = false;
    p.you.go('profile');
    await p.settle();
    return { quiet, opened: [p.title(), p.fetched, p.toasts] };
  },

  // The pointer on the profile button, or a press on it, fetches Profile ahead.
  async prefetchOnButton() {
    const p = open('/');
    p.event('pointerenter', p.$('btn'));
    await p.settle();
    const hovered = p.fetched.slice();
    p.event('pointerdown', p.$('btn'));
    await p.settle();
    return { hovered, pressed: p.fetched };
  },

  // A step to a group still on its way slides the view on show out at once, so the wait shows in its place.
  async stepWhileLoading() {
    const p = open('/', { hold: true });
    p.you.go('profile');
    await p.settle();
    p.release();
    await p.settle();
    p.$('to-email').focus();
    p.you.go('history');
    await p.settle();
    const on = p.doc.querySelectorAll('.you-view.is-on').map((v) => v.getAttribute('data-view'));
    const hub = p.doc.querySelector('.you-view[data-view="profile"]');
    const waiting = { on, loading: p.$('you').classList.contains('is-loading'), focus: p.focused(), hubDir: hub.style['--dir'], hubInert: hub.inert };
    p.release();
    await p.settle();
    const came = p.doc.querySelectorAll('.you-view.is-on').map((v) => v.getAttribute('data-view'));
    return { waiting, came, title: p.title() };
  },

  // On a phone a pull down on the content, scrolled to its top, takes the sheet with it and closes it.
  async pullFromContent() {
    const touch = (p, type, target, y, x = 50) => p.event(type, target, { touches: type === 'touchend' ? [] : [{ clientX: x, clientY: y }], timeStamp: y });
    // *inner*: the finger on what scrolls inside the view, scrolled down from its top before the
    // touch or, with *later*, once the finger is down.
    const run = async (scrolled, way, inner, later) => {
      const p = open('/', { phone: true });
      p.you.go('profile');
      await p.settle();
      const view = p.doc.querySelector('.you-view[data-view="profile"]');
      view.scrollTop = scrolled;
      const at = p.$(inner || 'last');
      if (inner && !later) p.$('inner').scrollTop = 120;
      touch(p, 'touchstart', at, 100);
      if (inner && later) p.$('inner').scrollTop = 30;
      const moves = way === 'up' ? [80, 60] : way === 'side' ? [[110, 200], [120, 300]] : [140, 260, 400];
      const kept = moves.map((m) => (Array.isArray(m) ? touch(p, 'touchmove', at, m[0], m[1]) : touch(p, 'touchmove', at, m)).defaultPrevented);
      const moved = p.$('panel').style.transform || '';
      touch(p, 'touchend', at, 400);
      await p.settle();
      return { kept, moved, at: p.at() };
    };
    return { top: await run(0, 'down'), scrolled: await run(40, 'down'), up: await run(0, 'up'), side: await run(0, 'side'),
      inner: await run(0, 'down', 'deep'), innerStart: await run(0, 'down', 'deep', true) };
  },

  // Pulled up by its handle, the sheet gives a little and comes back.
  async rubberBand() {
    const p = open('/', { phone: true });
    p.you.go('profile');
    await p.settle();
    const send = (type, y) => (p.$('grab').listeners[type] || []).forEach((fn) => fn({ type, pointerId: 1, button: 0, clientY: y, timeStamp: 0, target: p.$('grab'), currentTarget: p.$('grab') }));
    send('pointerdown', 300);
    const up = [10, 100, 300, 3000].map((by) => { send('pointermove', 300 - by); return p.$('panel').style.transform; });
    send('pointerup', 200);
    await p.settle();
    return { up, after: [p.$('panel').style.transform, p.at()] };
  },

  // A step moves the bar's title with the view: the old one leaves as the new one comes, both ways.
  async titleMoves() {
    const run = async (calm) => {
      const p = open('/', { calm });
      p.you.go('profile');
      await p.settle();
      p.animations.length = 0;
      await p.click('to-email');
      const ghost = p.$('bar').querySelector('.you__title.is-old');
      const of = (el) => p.animations.filter((a) => a.el === el).map((a) => a.frames.map((f) => [f.opacity, f.transform || 'none']));
      const when = (el) => p.animations.filter((a) => a.el === el).map((a) => [a.opts.delay || 0, a.opts.duration, a.opts.easing]);
      const step = { title: p.title(), ghost: ghost && [ghost.textContent, ghost.getAttribute('aria-hidden')], out: ghost && of(ghost), in: of(p.$('title')), label: of(p.$('up-label')),
        timing: { out: ghost && when(ghost), in: when(p.$('title')) } };
      p.animations.forEach((a) => a.finish());
      const left = p.$('bar').querySelectorAll('.is-old').length;
      p.animations.length = 0;
      await p.click('up');
      const back = p.$('bar').querySelector('.you__title.is-old');
      return { step, left, back: [p.title(), back && of(back), of(p.$('title'))] };
    };
    return { full: await run(false), calm: await run(true) };
  },
};

// A step between a narrow view and a wide one, out and back, and what moves while the panel changes width.
async function widthSteps(opts, to = 'history') {
  const p = open('/', opts);
  p.you.go('profile');
  await p.settle();
  p.animations.length = 0;
  if (to === 'history') await p.click('to-history');
  else p.you.go(to);
  await p.idle();
  const grab = (dir) => {
    const root = p.$('you');
    const frames = p.doc.querySelectorAll('.you__frame.is-on');
    const hub = p.doc.querySelector('.you-view[data-view="profile"]');
    const of = (el, prop) => p.animations.filter((a) => a.el === el && !a.cancelled && a.frames[0][prop] !== undefined).map((a) => a.frames.map((f) => f[prop]));
    const ease = p.animations.filter((a) => a.el === hub && a.frames[0].transform).map((a) => a.opts.easing);
    const out = {
      held: root.classList.contains('is-sizing'),
      frames: frames.length,
      edge: frames[0] ? of(frames[0].querySelector('.you__frame-edge'), 'transform') : [],
      fill: frames[0] ? of(frames[0].querySelector('.you__frame-fill'), 'transform') : [],
      shade: frames[0] ? of(frames[0].querySelector('.you__frame-shade'), 'transform') : [],
      fade: frames.map((f) => of(f, 'opacity')),
      hub: of(hub, 'transform'),
      hubShown: of(hub, 'opacity'),
      ease,
      up: of(p.$('up'), 'translate'),
      title: of(p.$('title'), 'translate'),
      old: of(p.$('bar').querySelector('.you__title.is-old'), 'translate'),
    };
    p.animations.forEach((a) => a.finish());
    out.after = [root.classList.contains('is-sizing'), p.animations.filter((a) => a.opts && a.opts.fill === 'forwards' && !a.cancelled && a.el.closest('.you__panel, .you__frame') && !a.el.classList.contains('is-old')).length];
    p.animations.length = 0;
    return out;
  };
  const first = grab();
  await p.step(-1);
  const back = grab();
  await p.step(1);
  const into = grab();
  return { first, back, into, at: p.at(), tokens: TOKENS };
}
scenarios.widthSteps = async () => ({ full: await widthSteps({}), calm: await widthSteps({ calm: true }), phone: await widthSteps({ phone: true }) });
scenarios.settingsSteps = async () => ({ full: await widthSteps({}, 'settings'), calm: await widthSteps({ calm: true }, 'settings') });

// A view whose foot saves marks the page while it shows, so the toasts stand clear of the foot.
scenarios.formFoot = async () => {
  const p = open('/');
  const marked = () => p.doc.documentElement.classList.contains('has-you-form');
  p.you.go('profile');
  await p.settle();
  const hub = marked();
  await p.click('to-email');
  const form = marked();
  p.you.go('profile');
  await p.settle();
  const back = marked();
  await p.click('to-email');
  p.you.close();
  await p.settle();
  return { hub, form, back, closed: marked() };
};

// A group on its way leaves no form on show, nor does one that fails to come.
scenarios.formFootWait = async () => {
  const p = open('/', { hold: true, server: (page) => (page === '/history' ? { status: 500 } : null) });
  const marked = () => p.doc.documentElement.classList.contains('has-you-form');
  p.you.go('profile/email');
  await p.settle();
  p.release();
  await p.settle();
  const form = marked();
  p.you.go('history');
  await p.settle();
  const waiting = marked();
  p.release();
  await p.settle();
  return { form, waiting, failed: marked(), title: p.title() };
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
Promise.resolve(scenarios[name]()).then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
