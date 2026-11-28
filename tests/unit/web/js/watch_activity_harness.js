// Runs the real watch/space.js and watch/activity.js against the fake DOM and a made-up server,
// and prints one scenario's outcome as JSON. Usage: node watch_activity_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const MB = 1024 * 1024;
const NOW = 1795867200; // noon on a set day

Object.defineProperty(Node.prototype, 'inert', {
  get() { return this.hasAttribute('inert'); },
  set(on) { if (on) this.setAttribute('inert', ''); else this.removeAttribute('inert'); },
});
// Every graph is 300 by 100 pixels, from the left of the page.
Object.defineProperty(Node.prototype, 'clientWidth', { get() { return 300; } });
Object.defineProperty(Node.prototype, 'clientHeight', { get() { return 100; } });
Object.defineProperty(Node.prototype, 'offsetWidth', { get() { return 300; } });
Object.defineProperty(Node.prototype, 'offsetLeft', { get() { return 0; } });
Node.prototype.getBoundingClientRect = function () { return { top: 0, bottom: 100, left: 0, right: 300, width: 300, height: 100 }; };
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };
Node.prototype.scrollIntoView = function () {};
Node.prototype.focus = function () { this.ownerDocument.activeElement = this; };
const classes = Object.getOwnPropertyDescriptor(Node.prototype, 'classList').get;
Object.defineProperty(Node.prototype, 'classList', {
  get() { return Object.assign(classes.call(this), { 0: this.className.split(/\s+/)[0] }); },
});

function point(at, down, cpu) {
  return { at, down, up: 2 * MB, dl: down, watch: 2 * MB, mm: 0.1 * MB, cpu, mem: 8 * 1024 * MB,
    apps: { plex: [10, 1024 * MB], sonarr: [3, 600 * MB], mediaman: [0.5, 140 * MB] } };
}

function live(over) {
  const pts = [];
  for (let i = 0; i < 31; i++) pts.push(point(NOW - 60 + i * 2, 100 * MB, 50));
  return {
    at: NOW, every: 2, sources: { plex: 'ok', nzbget: 'ok', docker: 'ok' }, cores: 10, mem_total: 16 * 1024 * MB,
    point: pts[pts.length - 1], live: pts, since: NOW - 40 * 86400,
    downloads: { rate: 100 * MB, left: 50 * 1024 * MB, count: 4, items: [{ title: 'Iron Petal Academy · Season 1', state: 'downloading', done: 10 * 1024 * MB, size: 40 * 1024 * MB }] },
    sessions: [{ user: 'rosa', device: 'Living room TV', kind: 'tv', title: 'Kestrel Point', transcode: false, local: true, rate: 1.5 * MB }],
    work: { searching: {}, fixing: 0, scanning: false, ai: null },
    ...over,
  };
}

function history(period) {
  const span = { '1h': 3600, '24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400 }[period];
  const pts = [];
  for (let i = 0; i <= 100; i++) pts.push(point(NOW - span + span * i / 100, (i < 50 ? 10 : 200) * MB, i < 50 ? 20 : 80));
  return { period, now: NOW, since: NOW - 40 * 86400, points: pts };
}

function markup() {
  const pane = (id) => ['div', { class: 'space-pane', id: 'space-' + id, role: 'tabpanel' }];
  const tab = (id) => ['button', { class: 'space-tab', id: 'space-tab-' + id, 'data-space-tab': id }, id, ['span', { class: 'space-tab__count' }]];
  return [
    ['header', { id: 'nav' }],
    ['main', { id: 'main' }, ['button', { class: 'space-meter', 'data-space-open': 'storage', id: 'meter' }]],
    ['div', { class: 'space', id: 'space' },
      ['section', { class: 'space__panel', role: 'dialog' },
        ['div', { class: 'space__grab' }],
        ['div', { class: 'space__head' },
          ['div', { class: 'space__tabs' }, ['span', { class: 'space__ink' }], tab('storage'), tab('scheduled'), tab('deleted'), tab('activity')],
          ['button', { class: 'space__close', 'data-space-close': '', id: 'close' }]],
        ['div', { class: 'space__panes' }, pane('storage'), pane('scheduled'), pane('deleted'), pane('activity')],
        ['footer', { class: 'space__foot' }, ['p', { id: 'space-scan-status', class: 'space__scan-status' }],
          ['button', { id: 'space-scan', class: 'btn space__scan' }],
          ['div', { class: 'act-foot' }, ['p', { class: 'act-foot__live' }, ['span', { class: 'act-foot__say' }, 'Updated live']]]]],
      ['p', { id: 'space-live' }]],
  ];
}

async function page(opts = {}) {
  const doc = makeDocument();
  const create = doc.createElement;
  const patch = (n) => {
    n.style.setProperty = (k, v) => { n.style[k] = v; };
    n.style.getPropertyValue = (k) => n.style[k] || '';
    n.animate = () => ({ set onfinish(fn) { fn(); } });
    return n;
  };
  doc.createElement = (t) => patch(create(t));
  doc.createElementNS = (_ns, t) => patch(create(t));
  doc.createDocumentFragment = () => create('fragment');
  markup().forEach((m) => doc.body.appendChild(build(doc, m)));
  doc.querySelectorAll('*').forEach(patch);
  const $ = (id) => doc.getElementById(id);
  $('space').hidden = true;
  const asked = [];
  let answer = live(opts.live);
  const timers = [];
  let frames = 0;
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Date, Number, isFinite, parseInt, parseFloat, Error, RegExp, encodeURIComponent,
    performance: { now: () => 0 },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    CustomEvent: class { constructor(type, init) { Object.assign(this, { type, ...init }); } },
    getComputedStyle: () => ({ getPropertyValue: () => '', transform: 'none' }),
    matchMedia: (q) => ({ matches: /reduce/.test(q) ? !opts.motion : false }),
    // Each frame is a second on, so what eases in one has eased.
    requestAnimationFrame: (fn) => { frames += 1; fn(1000 * frames); return frames; },
    cancelAnimationFrame() {},
    innerHeight: 900, innerWidth: 1440,
    location: { pathname: '/', search: '' },
    addEventListener() {},
    UIFeedback: { dismissConfirm() {} },
  };
  ctx.window = ctx;
  doc.dispatchEvent = (e) => { (doc.listeners[e.type] || []).forEach((fn) => fn(e)); };
  ctx.MM = { inertPage: { hold() {}, release() {} }, api: { get: (url) => {
    asked.push(url);
    if (url === '/api/activity') return Promise.resolve(JSON.parse(JSON.stringify(answer)));
    const m = /period=(\w+)/.exec(url);
    if (m) return Promise.resolve(history(m[1]));
    return Promise.reject(new Error('not here'));
  } } };
  vm.createContext(ctx);
  const run = (file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js'].forEach(run);
  let layer = null;
  ctx.MMWatch.address = { layer(def) { layer = def; }, url: (_p, h) => '#' + h, push() {}, replace() {}, close() { layer.close('pop'); }, top: () => 'storage' };
  ctx.MMWatch.keep = { closeMenu() {}, joinPanel() {}, isBusy: () => false };
  ['watch/space.js', 'watch/activity.js'].forEach(run);
  await settle();
  const one = (sel) => doc.querySelector(sel);
  const p = {
    doc, ctx, $, one, asked,
    frames: () => frames,
    set(over) { answer = live(over); },
    async click(node) { click(doc, node); await settle(); },
    async open() { await p.click($('meter')); await p.click($('space-tab-activity')); },
    async tick() { timers.filter((t) => t.fn && t.ms === 2000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); }); await settle(); },
    async key(key, target) {
      const e = { key, target: target || doc.activeElement, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      for (let a = e.target; a; a = a.parentNode) (a.listeners.keydown || []).forEach((fn) => fn(e));
      (doc.listeners.keydown || []).forEach((fn) => fn(e));
      await settle();
    },
    async pointer(node, type, x) { (node.listeners[type] || []).forEach((fn) => fn({ type, clientX: x, pointerType: 'mouse' })); await settle(); },
    text: (sel) => (one(sel) ? one(sel).textContent : null),
  };
  return p;
}

const scenarios = {
  // Asked every two seconds while the tab is on show, and not once it is not.
  async polls() {
    const p = await page();
    const before = p.asked.filter((u) => u === '/api/activity').length;
    await p.open();
    const opened = p.asked.filter((u) => u === '/api/activity').length;
    await p.tick();
    const ticked = p.asked.filter((u) => u === '/api/activity').length;
    await p.click(p.$('space-tab-storage'));
    await p.tick();
    await p.tick();
    const away = p.asked.filter((u) => u === '/api/activity').length;
    const foot = { act: p.one('.act-foot').hidden, scan: p.$('space-scan').hidden };
    return { before, opened, ticked, away, foot, say: p.text('.act-say') };
  },

  async period() {
    const p = await page();
    await p.open();
    await p.click(p.one('#space-activity [data-period="24h"]'));
    return {
      asked: p.asked.filter((u) => u.includes('history')),
      checked: p.doc.querySelectorAll('#space-activity [data-period]').filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.getAttribute('data-period')),
      note: p.text('#space-activity .act-h__note'),
      max: p.text('.act-scale--top'),
    };
  },

  async periodKeys() {
    const p = await page();
    await p.open();
    const live = p.one('#space-activity [data-period="live"]');
    live.focus();
    await p.key('ArrowRight');
    await p.key('ArrowLeft');
    await p.key('ArrowLeft');
    return { on: p.one('#space-activity [aria-checked="true"]').getAttribute('data-period'), focused: p.doc.activeElement.getAttribute('data-period') };
  },

  async grow() {
    const p = await page();
    await p.open();
    const wrap = p.one('#space-activity .act-net .act-grow');
    wrap.focus();
    await p.key('Enter');
    const grown = {
      layer: !!p.one('.act-big'),
      title: p.text('.act-big__title'),
      held: ['space__head', 'space__panes', 'space__foot'].map((c) => p.one('.' + c).inert),
      focused: p.doc.activeElement.className,
    };
    await p.key('Escape');
    return {
      grown,
      after: { layer: !!p.one('.act-big'), held: p.one('.space__panes').inert, open: !p.$('space').hidden && p.$('space').classList.contains('is-open'), focused: p.doc.activeElement === wrap },
    };
  },

  // The crosshair reads the values under the pointer: the history is 10 MB/s, then 200 MB/s.
  async crosshair() {
    const p = await page();
    await p.open();
    await p.click(p.one('#space-activity [data-period="1h"]'));
    const box = p.one('#space-activity .act-net .act-chart');
    await p.pointer(box, 'pointermove', 30);
    const early = { tip: p.one('#space-activity .act-tip').hidden, text: p.text('#space-activity .act-tip') };
    await p.pointer(box, 'pointermove', 290);
    const late = p.text('#space-activity .act-tip');
    await p.pointer(box, 'pointerleave', 290);
    return { early, late, gone: p.one('#space-activity .act-tip').hidden };
  },

  async down() {
    const p = await page({ live: { sources: { plex: 'ok', nzbget: 'down', docker: 'off' }, downloads: null } });
    await p.open();
    return {
      say: p.text('.act-say'),
      warn: p.one('.act-say').classList.contains('act-say--warn'),
      row: p.text('.act-row.act-c--dl .act-row__sub'),
      quiet: p.text('.act-quiet'),
    };
  },

  async reduced() {
    const p = await page();
    await p.open();
    const at = p.frames();
    p.set({ point: point(NOW + 2, 300 * MB, 90) });
    await p.tick();
    return { arriving: p.one('.act-body').classList.contains('is-arriving'), frames: p.frames() - at, down: p.text('.act-tile__n') };
  },

  async motion() {
    const p = await page({ motion: true });
    await p.open();
    const at = p.frames();
    p.set({ point: point(NOW + 2, 300 * MB, 90) });
    await p.tick();
    return { arriving: p.one('.act-body').classList.contains('is-arriving'), frames: p.frames() - at, down: p.text('.act-tile__n') };
  },

  async slots() {
    const p = await page();
    const s = p.ctx.MMWatch.activity.slots;
    const pts = [{ at: 0, v: 0 }, { at: 10, v: 10 }, { at: 100, v: 5 }];
    return s(pts, 0, 100, 11, (x) => x.v, 20);
  },
};

(async () => {
  const name = process.argv[2];
  process.stdout.write(JSON.stringify(await scenarios[name]()));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
