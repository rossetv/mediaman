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
// Times are written in one locale and zone, whatever the machine's.
const LOCALE = 'en-GB';
const ZONE = { timeZone: 'UTC' };

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

// Half the server's memory in use, Plex's share 6.25%: 1 GB of 16.
function point(at, down, cpu) {
  return { at, down, up: 2 * MB, dl: down, watch: 2 * MB, mm: 0.1 * MB, cpu, mem_pct: 50, plex_mem_pct: 6.25,
    apps: { plex: [10, null], sonarr: [3, 600 * MB], mediaman: [0.5, 140 * MB] } };
}

function live(over) {
  const pts = [];
  for (let i = 0; i < 31; i++) pts.push(point(NOW - 60 + i * 2, 100 * MB, 50));
  return {
    at: NOW, every: 2, sources: { plex: 'ok', nzbget: 'ok', docker: 'ok' }, cores: 10, mem_total: 16 * 1024 * MB,
    point: pts[pts.length - 1], live: pts, since: NOW - 40 * 86400,
    downloads: { rate: 100 * MB, left: 50 * 1024 * MB, count: 4, items: [
      { title: 'Iron Petal Academy · Season 1', state: 'downloading', done: 10 * 1024 * MB, size: 40 * 1024 * MB, by: 'sonarr' },
      { title: 'The Long Coast · Season 3', state: 'unpacking', done: 2 * 1024 * MB, size: 2 * 1024 * MB, by: 'sonarr' }] },
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
  const clock = { now: 0 };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Number, isFinite, parseInt, parseFloat, Error, RegExp, encodeURIComponent,
    performance: { now: () => clock.now },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = null; },
    // Today is the made-up server's day.
    Date: class extends Date {
      constructor(...a) { super(...(a.length ? a : [NOW * 1000])); }
      static now() { return NOW * 1000; }
      toLocaleDateString(l, o) { return super.toLocaleDateString(l && l.length ? l : LOCALE, { ...ZONE, ...o }); }
      toLocaleTimeString(l, o) { return super.toLocaleTimeString(l && l.length ? l : LOCALE, { ...ZONE, ...o }); }
      toDateString() { return this.toISOString().slice(0, 10); }
      toLocaleString(l, o) { return super.toLocaleString(l && l.length ? l : LOCALE, { ...ZONE, ...o }); }
    },
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
    clock,
    timers,
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

  // An answer on its way when the tab is hidden does not ask again.
  async hidden() {
    const p = await page();
    await p.open();
    p.timers.filter((t) => t.fn && t.ms === 2000).forEach((t) => { const fn = t.fn; t.fn = null; fn(); });
    p.doc.hidden = true;
    p.doc.dispatchEvent({ type: 'visibilitychange' });
    await settle();
    const at = p.asked.length;
    await p.tick();
    await p.tick();
    return { asked: p.asked.length - at };
  },

  // The crosshair stands on the slot under the pointer, and its tip turns back at the right.
  async slot() {
    const p = await page();
    await p.open();
    await p.click(p.one('#space-activity [data-period="1h"]'));
    const box = p.one('#space-activity .act-net .act-chart');
    const tip = p.one('#space-activity .act-net .act-tip');
    Object.defineProperty(tip, 'offsetWidth', { value: 80 });
    // 160 slots over 300 px: x 149 is slot 79, part way up from 10 to 200 MB/s.
    await p.pointer(box, 'pointermove', 149);
    const mid = { text: p.text('#space-activity .act-net .act-tip'), hair: p.one('#space-activity .act-net .act-hair').style.transform };
    await p.pointer(box, 'pointermove', 0);
    const left = tip.style.transform;
    await p.pointer(box, 'pointermove', 300);
    return { mid, left, right: tip.style.transform };
  },

  // Away from today, a 24 h tooltip says the day as well as the time.
  async day() {
    const p = await page();
    await p.open();
    await p.click(p.one('#space-activity [data-period="24h"]'));
    const box = p.one('#space-activity .act-net .act-chart');
    await p.pointer(box, 'pointermove', 10); // 23 hours before noon: the day before
    const before = p.text('#space-activity .act-net .act-tip__when');
    await p.pointer(box, 'pointermove', 300);
    return { before, today: p.text('#space-activity .act-net .act-tip__when') };
  },

  // A frame that began before the count did shows where it counts from, not past it.
  async early() {
    const p = await page({ motion: true });
    await p.open();
    const seen = [];
    const raf = p.ctx.requestAnimationFrame;
    p.clock.now = 1000 * (p.frames() + 3);
    // Frames run at once, one inside the next: what each shows is read as the next begins.
    p.ctx.requestAnimationFrame = (fn) => raf((t) => { seen.push(p.text('.act-tile__n')); fn(t); });
    p.set({ point: point(NOW + 2, 300 * MB, 90) });
    await p.tick();
    return seen;
  },

  // With the server's size set, memory is in GB and the heading names the server.
  async sized() {
    const p = await page();
    await p.open();
    const card = p.doc.querySelectorAll('#space-activity .act-res')[1];
    const keys = card.querySelectorAll('.act-key').map((k) => [k.querySelector('.act-key__name').textContent, k.querySelector('.act-key__val').textContent]);
    card.querySelector('.act-grow').focus();
    await p.key('Enter');
    const ticks = p.doc.querySelectorAll('.act-big__tick').map((t) => t.textContent);
    return {
      note: p.doc.querySelectorAll('#space-activity .act-h__note')[1].textContent,
      tile: p.doc.querySelectorAll('#space-activity .act-tile__sub')[2].textContent,
      fig: card.querySelector('.act-res__n').textContent,
      keys, ticks,
    };
  },

  // Without it, the host and Plex are shares of the server, and nothing is turned into GB.
  async shares() {
    const p = await page({ live: { mem_total: null, cores: null } });
    await p.open();
    const card = p.doc.querySelectorAll('#space-activity .act-res')[1];
    return {
      note: p.doc.querySelectorAll('#space-activity .act-h__note')[1].textContent,
      tiles: p.doc.querySelectorAll('#space-activity .act-tile__sub').map((t) => t.textContent),
      fig: card.querySelector('.act-res__n').textContent,
      keys: card.querySelectorAll('.act-key').map((k) => [k.querySelector('.act-key__name').textContent, k.querySelector('.act-key__val').textContent]),
    };
  },

  // Plex answers but leaves out the host's CPU and its own, in the newest points.
  async unknown() {
    const pts = [];
    for (let i = 0; i < 31; i++) pts.push(point(NOW - 60 + i * 2, 100 * MB, 50));
    for (let i = 25; i < 31; i++) { pts[i].cpu = null; pts[i].apps.plex = [null, null]; }
    const p = await page({ live: { point: pts[30], live: pts } });
    await p.open();
    const card = p.doc.querySelectorAll('#space-activity .act-res')[0];
    const paths = p.doc.querySelectorAll('#space-activity .act-chart--stack path').map((x) => x.getAttribute('d'));
    return {
      say: p.text('.act-say'),
      tile: p.doc.querySelectorAll('#space-activity .act-tile__n')[1].textContent,
      fig: card.querySelector('.act-res__n').textContent,
      keys: card.querySelectorAll('.act-key').map((k) => [k.querySelector('.act-key__name').textContent, k.querySelector('.act-key__val').textContent]),
      nan: /NaN/.test(p.$('space-activity').textContent) || paths.some((d) => /NaN/.test(d)),
    };
  },

  async unpacking() {
    const p = await page();
    await p.open();
    const row = p.doc.querySelectorAll('.act-row.act-c--dl .act-sub')[1];
    return { sub: row.querySelector('small').textContent, cls: row.querySelector('.act-sub__val').className };
  },

  // The queue's "to go" and each download's time left, from the figures the write-up met.
  async queue() {
    const GIB = 1024 * MB;
    const item = (title, left) => ({ title, state: 'downloading', done: 10 * GIB - left, size: 10 * GIB, by: 'sonarr' });
    const rows = async (over) => {
      const p = await page({ live: over });
      await p.open();
      return {
        head: p.text('.act-row.act-c--dl .act-row__sub'),
        subs: p.doc.querySelectorAll('.act-row.act-c--dl .act-sub small').map((s) => s.textContent),
      };
    };
    // 1 MB/s: 7190 s, 3590 s, 3500 s, 7200 s, none and less than none left.
    const timed = await rows({ downloads: { rate: MB, left: 1992294400, count: 6, items: [
      item('a', 7190 * MB), item('b', 3590 * MB), item('c', 3500 * MB), item('d', 7200 * MB), item('e', 0), item('f', -MB)] } });
    const under = await rows({ downloads: { rate: MB, left: GIB - 1, count: 1, items: [] } });
    return { head: timed.head, subs: timed.subs, under: under.head };
  },

  // The AI row's "Last asked", either side of the hour.
  async asked() {
    const out = {};
    for (const s of [3560, 3580, 5400]) {
      const p = await page({ live: { work: { searching: {}, fixing: 0, scanning: false, ai: { running: false, at: NOW - s } } } });
      await p.open();
      out[s] = (/Last asked [^A-Z]*ago/.exec(p.$('space-activity').textContent) || [null])[0];
    }
    return out;
  },

  // mediaman's CPU key for a small share, and figures that round up to a whole one.
  async smallCpu() {
    const out = {};
    for (const v of [0.02, 0.06, 0.97, 0.5]) {
      const pts = [];
      for (let i = 0; i < 31; i++) { const x = point(NOW - 60 + i * 2, 100 * MB, 50); x.apps.mediaman = [v, 140 * MB]; pts.push(x); }
      const p = await page({ live: { point: pts[30], live: pts } });
      await p.open();
      const card = p.doc.querySelectorAll('#space-activity .act-res')[0];
      out[v] = card.querySelectorAll('.act-key').map((k) => [k.querySelector('.act-key__name').textContent, k.querySelector('.act-key__val').textContent]).find((k) => k[0] === 'mediaman')[1];
    }
    return out;
  },

  // Every scale the network graph can pick, read from the small graph and the grown one.
  async scales() {
    const seen = [];
    const peaks = [0, 0.03, 0.08, 0.2, 0.4, 0.8, 1.5, 4, 9, 20, 40, 80, 120, 160, 210, 260, 350, 420, 600, 850, 1200, 1700, 2500, 5000];
    for (const peak of peaks) {
      const pts = [];
      for (let i = 0; i < 31; i++) { const x = point(NOW - 60 + i * 2, peak * MB, 50); x.up = peak * MB; pts.push(x); }
      const p = await page({ live: { point: pts[30], live: pts } });
      await p.open();
      p.one('#space-activity .act-net .act-grow').focus();
      await p.key('Enter');
      seen.push({ top: p.text('.act-scale--top'), up: p.text('.act-scale--up'), ticks: p.doc.querySelectorAll('.act-big__tick').map((t) => t.textContent) });
    }
    return seen;
  },

  // With Up not known, no Up scale is shown.
  async noUp() {
    const pts = [];
    for (let i = 0; i < 31; i++) { const x = point(NOW - 60 + i * 2, 100 * MB, 50); x.up = null; pts.push(x); }
    const p = await page({ live: { point: pts[30], live: pts } });
    await p.open();
    const small = !!p.one('.act-scale--up').hidden;
    p.one('#space-activity .act-net .act-grow').focus();
    await p.key('Enter');
    return { small, ticks: p.doc.querySelectorAll('.act-big__tick').map((t) => t.textContent) };
  },

  async edge() {
    const p = await page();
    const s = p.ctx.MMWatch.activity.slots;
    // The first point comes after the window starts, on a slope up from 10 to 100.
    return s([{ at: 10, v: 10 }, { at: 20, v: 100 }], 0, 20, 3, (x) => x.v, 40);
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
