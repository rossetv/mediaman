// Runs the real tv.js against a fake TV browser, with a clock it steps, and prints one scenario's
// outcome as JSON. Usage: node --expose-internals tv_harness.js <scenario> [argument]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../../../src/mediaman/web/static');
const SOURCE = fs.readFileSync(path.join(WEB, 'js/tv.js'), 'utf8');
const template = (name) => fs.readFileSync(path.join(WEB, '../templates', name), 'utf8');
// What the stage holds as the page is opened.
const STAGE = template('tv.html').match(/<main id="s">(.*?)<\/main>/s)[1]
  .replace(/\{% include "([^"]+)" %\}/g, (all, name) => template(name)).replace(/\{#.*?#\}/gs, '');

// `full`: the browser has the Fullscreen API and goes full screen a moment after it is asked;
// 'refuse' is one that rejects the request, 'webkit' one with only the prefixed API.
function tv({ kept = null, plays = true, full = false } = {}) {
  const order = [];
  let time = 1e12;
  let timers = [];
  let ids = 0;
  const later = (fn, ms, every) => { timers.push({ id: ids += 1, at: time + (ms || 0), fn, every }); return ids; };
  const store = kept ? { 'mm-tv': kept } : {};
  const errors = [];
  const sent = [];
  const answers = {};
  const nodes = {};
  const element = () => ({
    innerHTML: '', className: '', style: {}, attrs: {}, children: [], on: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; if (k === 'id') this.id = ''; if (k === 'src') this.gone = true; },
    addEventListener(k, fn) { (this.on[k] = this.on[k] || []).push(fn); },
    appendChild(n) { this.children.push(n); return n; },
    getElementsByTagName: () => [],
  });
  const video = Object.assign(element(), {
    paused: true, ended: false, volume: 1, muted: false, seeking: false, duration: 2880, calls: [], refuse: false,
    canPlayType: () => (plays ? 'maybe' : ''),
    load() { this.paused = true; this.time = 0; },
    play() { this.calls.push('play'); order.push('play'); if (this.refuse) return Promise.reject(Object.assign(new Error('no'), { name: 'NotAllowedError' })); this.paused = false; return undefined; },
    pause() { this.calls.push('pause'); this.paused = true; },
    fire(type) { (this.on[type] || []).forEach((fn) => fn({})); },
  });
  // A seek lands `land` seconds off, and the event that tells of it comes a moment later.
  video.sets = [];
  video.time = 0;
  Object.defineProperty(video, 'currentTime', {
    get() { return this.time; },
    set(v) { this.sets.push(v); if (this.inert) return; if (this.slow) { this.seeking = true; later(() => { this.seeking = false; this.time = v + this.slow; }, 300); return; } this.time = v + (this.land || 0); if (this.land) later(() => this.fire('seeked'), 3); },
  });
  Object.defineProperty(video, 'src', { get() { return this.attrs.src; }, set(v) { this.attrs.src = v; } });
  nodes.v = video;
  nodes.s = element();
  nodes.f = element();
  // The page before: what was drawn on the stage, moved with the nodes it had.
  nodes.o = Object.assign(element(), {
    held: [],
    appendChild(page) { this.innerHTML = page.html; this.held = page.nodes; drawn(''); },
    getElementsByTagName() { return this.held; },
  });
  let html = '';
  const drawn = (text) => {
    html = text;
    Object.keys(nodes).forEach((id) => { if (!FIXED.includes(id)) delete nodes[id]; });
  };
  Object.defineProperty(nodes.s, 'innerHTML', { get: () => html, set: drawn });
  Object.defineProperty(nodes.s, 'firstChild', {
    get: () => (html ? { html, nodes: Object.keys(nodes).filter((id) => !FIXED.includes(id)).map((id) => nodes[id]) } : null),
  });
  const body = Object.assign(element(), { attrs: { 'data-base': 'https://mediaman.example', 'data-hls': '/hls.js' } });
  // What is drawn on the page is found by its id while it is drawn.
  const document = {
    body, createElement: element, on: {},
    addEventListener(k, fn) { (this.on[k] = this.on[k] || []).push(fn); },
    querySelector(selector) {
      if (selector === '.mid') return { outerHTML: STAGE };
      if (selector === '.brand') return { outerHTML: STAGE.match(/<div class="brand">.*?<\/div>/s)[0] };
      return null;
    },
    getElementById(id) {
      if (FIXED.includes(id)) return nodes[id];
      if (!nodes.s.innerHTML.includes('id="' + id + '"')) return null;
      return (nodes[id] = nodes[id] || Object.assign(element(), { id, nodeType: 1, focus() { if (document.activeElement === this) return; document.activeElement = this; (document.on.focus || []).forEach((fn) => fn({ target: this })); } }));
    },
  };
  const fullAsks = [];
  const prefixed = full === 'webkit';
  const changed = () => (document.on[prefixed ? 'webkitfullscreenchange' : 'fullscreenchange'] || []).forEach((fn) => fn({}));
  const inFull = prefixed ? 'webkitFullscreenElement' : 'fullscreenElement';
  document.documentElement = element();
  if (full) {
    document.fullscreenEnabled = true;
    document[inFull] = null;
    document.documentElement[prefixed ? 'webkitRequestFullscreen' : 'requestFullscreen'] = function () {
      fullAsks.push(this === document.documentElement ? 'root' : 'other');
      order.push('full');
      if (full === 'refuse') return { then(ok, no) { later(() => no(new Error('no')), 10); } };
      later(() => { document[inFull] = this; changed(); }, 50);
      return { then() {} };
    };
  }
  class XMLHttpRequest {
    open(method, url, async) { Object.assign(this, { method, url, async, headers: {} }); }
    setRequestHeader(k, v) { this.headers[k] = v; }
    getResponseHeader() { return this.retry || null; }
    abort() {}
    send(text) {
      const call = { path: this.url.replace('/api/tv/device/', ''), token: this.headers['X-TV-Token'], body: JSON.parse(text) };
      sent.push(call);
      const give = answers[call.path];
      const answer = typeof give === 'function' ? give(call) : give;
      if (answer === undefined) return;
      later(() => {
        const [status, data, retry] = Array.isArray(answer) ? answer : [200, answer];
        Object.assign(this, { readyState: 4, status, retry, responseText: data === null ? '' : JSON.stringify(data) });
        this.onreadystatechange();
      }, 1);
    }
  }
  let skew = 0;
  class FixedDate extends Date { static now() { return time + skew; } }
  const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem(k, v) { store[k] = v; }, removeItem(k) { delete store[k]; } };
  const ctx = {
    document, XMLHttpRequest, localStorage, Date: FixedDate, location: { protocol: 'https:', host: 'x' },
    setTimeout: (fn, ms) => later(fn, ms), clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: (fn, ms) => later(fn, ms, ms), unescape, encodeURIComponent, innerWidth: 1920, innerHeight: 1080,
  };
  ctx.window = ctx;
  return {
    sent, answers, video, store, fullAsks, order,
    leaveFull() { document[inFull] = null; changed(); },
    ov: () => (document.getElementById('ov') || {}).className,
    hint: () => nodes.f.className,
    run() { vm.runInContext(SOURCE, vm.createContext(ctx)); return this; },
    qr: (text) => ctx.window.MMTv.qr(text),
    // Steps the clock, and lets what was promised come.
    async step(ms) {
      const until = time + ms;
      for (;;) {
        await Promise.resolve();
        const due = timers.filter((t) => t.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        const from = time;
        time = Math.max(time, due.at);
        if (!video.paused && video.attrs.src && !video.stuck) video.time += (time - from) / 1000;
        if (due.every) due.at = time + due.every; else timers = timers.filter((t) => t !== due);
        // As in a browser, what throws ends itself alone.
        try { due.fn(); } catch (e) { errors.push(e.message); }
      }
      if (!video.paused && video.attrs.src && !video.stuck) video.time += (until - time) / 1000;
      time = until;
      return this;
    },
    jump(ms) { skew += ms; },
    style: (id) => (document.getElementById(id) || {}).style,
    node: (id) => document.getElementById(id),
    held: () => (document.activeElement || {}).id,
    point: (id, x) => document.onmousemove({ target: { nodeType: 3, parentNode: document.getElementById(id) }, clientX: x, clientY: 0 }),
    click: (id) => document.onclick({ target: document.getElementById(id) }),
    left: () => nodes.o.innerHTML,
    leaving: () => nodes.o.held.map((n) => [n.id, n.style]),
    entering: () => nodes.s.className,
    wheel: () => document.onwheel && document.onwheel({}),
    key: (code) => document.onkeydown({ keyCode: code, preventDefault() {} }),
    // A browser clicks the button that has the focus as Space is let go, if its press was not prevented.
    space(repeats = 0) {
      const at = document.activeElement;
      let clicks = false;
      for (let i = 0; i <= repeats; i += 1) document.onkeydown({ keyCode: 32, repeat: i > 0, preventDefault() { clicks = true; } });
      if (!clicks && at) document.onclick({ target: at });
    },
    // A press of a pointer that brings its own move, held for `ms`.
    async tap(id, x, ms = 0) {
      const target = document.getElementById(id);
      document.onmousemove({ target, clientX: x, clientY: 0 });
      if (document.onmousedown) document.onmousedown({ target });
      await this.step(ms);
      document.onclick({ target });
    },
    look: () => body.className,
    html: () => nodes.s.innerHTML,
    says: () => nodes.s.innerHTML.replace(/<svg[^>]* aria-label="([^"]*)"[^>]*>/g, '$1').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
    text: (id) => document.getElementById(id).innerHTML,
    paths: (p) => sent.filter((c) => c.path === p),
    last: (p) => sent.filter((c) => c.path === p).pop(),
  };
}

const ready = (more) => ({ ok: true, state: 'ready', id: 1, name: 'Living room', owner: 'maren', play: null, commands: [], poll_ms: 1000, ...more });
const film = (more) => ({
  play_id: 'p1', state: 'loading', url: '/api/cast/t/index.m3u8', title: 'Glass Orchard', year: 2024, season: 1, episode: 4, episode_title: 'The Knife',
  art_url: '/api/tv/device/art/p1', duration_s: 2880, start_s: 100, played_s: 50, markers: [], chapters: [], next: null, upnext_in_s: null, still_in_s: null, error: null, ...more,
});
const GONE = [404, { ok: false, error: 'not_found' }];
const OK = 13, LEFT = 37, RIGHT = 39, BACK = 10009;
const FIXED = ['v', 's', 'o', 'f'];

async function playing(more) {
  const t = tv({ kept: 'old' });
  t.answers.poll = () => ready({ play: t.now });
  t.now = film(more);
  t.run();
  await t.step(50);
  t.video.fire('loadedmetadata');
  t.video.fire('playing');
  await t.step(600);
  return t;
}

const MINUTE = 60000;

// a page nothing is done at, up to `short` of when it dims
async function left(name, short) {
  const t = name === 'pause' || name === 'ended' || name === 'err' ? await playing() : tv({ kept: ['code', 'conn', 'press'].includes(name) ? null : 'old', plays: name !== 'old' });
  if (name === 'code') {
    t.answers.code = () => ({ ok: true, code: '482916', token: 'fresh', expires_in_s: 300 });
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 290, poll_ms: 2000 };
    t.run();
  } else if (name === 'idle') { t.answers.poll = ready({ poll_ms: 2000 }); t.run(); }
  else if (name === 'unpair') { t.answers.poll = ready({ poll_ms: 2000 }); t.run(); await t.step(100); t.key(RIGHT); t.key(OK); t.key(RIGHT); }
  else if (name === 'conn') {
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.answers.poll = ready({ poll_ms: 2000 });
    t.run();
    await t.step(2500);
  } else if (name === 'press') {
    t.answers.code = () => ({ ok: true, code: '111111', token: 'tok', expires_in_s: 300 });
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 1, poll_ms: 60000 };
    t.run();
    await t.step(100 * MINUTE);
  } else if (name === 'old') t.run();
  else if (name === 'out') { t.answers.poll = GONE; t.run(); }
  else if (name === 'pause') { t.key(OK); t.video.fire('pause'); }
  else { t.now = film({ state: name === 'err' ? 'failed' : 'ended', error: 'tv', url: null }); await t.step(1000); }
  await t.step((name === 'pause' ? 5 : 10) * MINUTE - short);
  return t;
}

const scenarios = {
  async rests(name) {
    const t = await left(name, 2000);
    const before = t.look();
    await t.step(4000);
    const looks = [];
    for (let i = 0; i < 5; i += 1) { looks.push(t.look()); await t.step(MINUTE); }
    return { before, looks };
  },
  async neverRests(name) {
    const t = await playing({ duration_s: 9000 });
    t.video.duration = 9000;
    if (name !== 'play') t.now = film({ state: name, url: null });
    const looks = [];
    for (let i = 0; i < 50; i += 1) {
      await t.step(MINUTE / 2);
      if (t.look()) looks.push(t.says());
    }
    return { looks, says: t.says() };
  },
  async wakes(by) {
    const t = await left(by === 'command' || by === 'plays' ? 'pause' : by === 'closed' ? 'ended' : 'idle', -2000);
    const dimmed = t.look();
    if (by === 'key') t.key(RIGHT);
    else if (by === 'pointer') t.point('b1', 5);
    else if (by === 'click') t.click('none');
    else if (by === 'wheel') t.wheel();
    else if (by === 'command') t.answers.poll = () => ready({ play: t.now, commands: [{ id: 8, kind: 'volume', level: 0.5 }] });
    else if (by === 'plays') t.answers.poll = () => ready({ play: t.now, commands: [{ id: 8, kind: 'play' }] });
    else if (by === 'closed') t.now = null;
    else t.answers.poll = () => ready({ play: film({ state: 'starting', url: null }) });
    if (!['key', 'pointer', 'click', 'wheel'].includes(by)) await t.step(2500);
    const woken = t.look();
    if (by === 'film') return { dimmed, woken };
    await t.step(4 * MINUTE);
    return { dimmed, woken, later: t.look() };
  },
  // the words that move bring a pointer that lies still to another place on them
  async pointerLiesStill() {
    const t = await left('idle', 1000);
    t.point('b1', 5);
    await t.step(10 * MINUTE + 1000);
    const dimmed = t.look();
    t.point('b1', 5);
    return [dimmed, t.look()];
  },
  async failsAgain() {
    const t = await left('err', -2000);
    const dimmed = t.look();
    t.now = film({ play_id: 'p2', state: 'failed', error: 'tv', url: null });
    await t.step(1200);
    return [dimmed, t.look()];
  },
  async wakesWith(what) {
    const name = what === 'code' ? 'code' : what === 'pausedClick' ? 'pause' : what;
    const t = await left(name, -2000);
    t.answers.ask = { ok: true };
    t.answers.code = { ok: true, code: '111111', token: 'newer', expires_in_s: 300 };
    const asked = () => [t.paths('code').length, t.paths('ask').length];
    const before = asked();
    t.video.calls.length = 0;
    if (what === 'idle') t.click('b0');
    else if (what === 'pausedClick') t.click('b1');
    else t.key(OK);
    await t.step(100);
    const first = [asked().join() !== before.join(), t.video.calls.slice()];
    t.key(OK);
    await t.step(100);
    return { first, then: asked().join() !== before.join(), awake: t.look() };
  },
  // the same press twice on a dimmed page: what it did the first time, and the second
  async pressesTwice(page, how) {
    const t = await left(page, -2000);
    t.answers.ask = { ok: true };
    t.answers.code = { ok: true, code: '111111', token: 'newer', expires_in_s: 300 };
    const button = page === 'unpair' || page === 'pause' ? 'b1' : 'b0';
    const press = () => (how === 'key' ? t.key(OK) : how === 'space' ? t.space() : how === 'heldSpace' ? t.space(3) : t.tap(button, 5, how === 'heldTap' ? 900 : 0));
    const state = () => [t.sent.filter((c) => c.path !== 'poll').length, page === 'pause' ? '' : t.says().replace(/[0-9:]+/g, '')].join();
    const dimmed = t.look();
    const before = state();
    t.video.calls.length = 0;
    await press();
    const first = state() !== before;
    const calls = t.video.calls.slice();
    const woken = t.look();
    await t.step(50);
    await press();
    return { dimmed, woken, first, calls, then: state() !== before };
  },
  async pointsThenClicks() {
    const t = await left('ended', -2000);
    t.answers.ask = { ok: true };
    t.point('b0', 5);
    await t.step(1500);
    t.click('b0');
    return t.paths('ask').length;
  },
  async pairs() {
    const t = tv();
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 290, poll_ms: 2000 };
    t.run();
    await t.step(3000);
    const shown = t.says();
    const picture = (t.html().match(/<div class="qr"[^>]*><svg[^>]*>/) || [''])[0];
    const left = t.text('cd');
    const before = { ...t.store };
    t.answers.poll = ready({ name: '<b>Den</b>', owner: 'a&b' });
    await t.step(3000);
    return { shown, picture, left, before, asked: t.paths('code').map((c) => c.token), polled: t.last('poll').token, after: t.store, said: t.html() };
  },
  async codesStop() {
    const t = tv();
    t.answers.code = () => ({ ok: true, code: '111111', token: 'tok', expires_in_s: 300 });
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 1, poll_ms: 60000 };
    t.run();
    await t.step(100 * 60000);
    const asked = t.paths('code').length;
    const said = t.says();
    t.key(OK); // for the light
    t.key(OK);
    await t.step(1000);
    return { asked, said, after: t.paths('code').length, now: t.says() };
  },
  async codeRanOut() {
    const t = tv();
    t.answers.code = () => ({ ok: true, code: '111111', token: 'tok', expires_in_s: 300 });
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 1, poll_ms: 2000 };
    t.run();
    await t.step(301500);
    return { asked: t.paths('code').length, left: t.text('cd') };
  },
  async tooManyCodes() {
    const t = tv();
    t.answers.code = [429, { ok: false, error: 'rate_limited' }, '240'];
    t.run();
    await t.step(200000);
    const said = t.says();
    const asked = t.paths('code').length;
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    await t.step(50000);
    return { said, asked, now: t.says() };
  },
  async unpairedElsewhere() {
    const t = await playing();
    t.answers.poll = GONE;
    await t.step(3000);
    return { said: t.says(), src: t.video.attrs.src || null, store: t.store };
  },
  async takenOver() {
    const t = await playing();
    t.now = null;
    await t.step(1500);
    t.answers.poll = (c) => (c.token === 'fresh' ? { ok: true, state: 'pairing', expires_in_s: 200, poll_ms: 2000 } : ready({ play: t.now }));
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.key(OK);
    await t.step(500);
    const shown = t.says();
    t.now = film({ play_id: 'p2' });
    await t.step(4000);
    t.video.fire('playing');
    const over = t.video.attrs.src;
    t.answers.poll = (c) => (c.token === 'fresh' ? ready({ owner: 'tobias', name: 'TV' }) : GONE);
    await t.step(3000);
    const said = t.says();
    const src = t.video.attrs.src || null;
    await t.step(5000);
    return { shown, over, said, src, store: t.store, then: t.says() };
  },
  async unpairedWithACodeShown() {
    const t = tv({ kept: 'old' });
    t.answers.poll = ready();
    t.run();
    await t.step(1500);
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.key(OK);
    await t.step(500);
    t.key(RIGHT); t.key(OK);
    const back = t.says();
    t.answers.poll = (c) => (c.token === 'fresh' ? { ok: true, state: 'pairing', expires_in_s: 200, poll_ms: 2000 } : GONE);
    await t.step(6000);
    const said = t.says();
    t.answers.poll = ready({ owner: 'tobias' });
    await t.step(3000);
    const late = t.says();
    const u = tv({ kept: 'old' });
    u.answers.poll = ready();
    u.run();
    await u.step(1500);
    u.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    u.key(OK);
    await u.step(500);
    u.key(RIGHT); u.key(OK);
    u.answers.poll = GONE;
    await u.step(6000);
    return { back, said, late, gone: u.says(), store: u.store };
  },
  async loads() {
    const t = tv({ kept: 'old' });
    t.answers.poll = () => ready({ play: film() });
    // Nothing moves until it plays.
    t.video.stuck = true;
    t.run();
    await t.step(50);
    const first = t.says();
    t.video.fire('loadedmetadata');
    await t.step(1500);
    const at = t.video.currentTime;
    const loading = t.last('poll').body.report;
    t.video.stuck = false;
    t.video.fire('playing');
    await t.step(4000);
    return { first, at: Math.round(at), src: t.video.src, loading: loading.state, report: t.last('poll').body.report, calls: t.video.calls, polls: t.sent.length };
  },
  async openedAgain() {
    const t = await playing();
    await t.step(20000);
    const at = Math.floor(t.video.currentTime);
    const u = tv({ kept: 'old' });
    Object.assign(u.store, t.store);
    u.answers.poll = () => ready({ play: film() });
    u.run();
    await u.step(50);
    u.video.fire('loadedmetadata');
    const behind = at - u.video.currentTime;
    const other = tv({ kept: 'old' });
    Object.assign(other.store, t.store);
    other.answers.poll = () => ready({ play: film({ play_id: 'p9' }) });
    other.run();
    await other.step(50);
    other.video.fire('loadedmetadata');
    t.answers.poll = GONE;
    await t.step(3000);
    return { behind, other: other.video.currentTime, store: t.store };
  },
  async stopsWhileAway() {
    const t = await playing();
    t.answers.ask = undefined;
    t.answers.poll = undefined;
    t.key(413);
    const at = t.video.currentTime;
    const first = { paused: t.video.paused, said: t.says() };
    await t.step(12000);
    const then = t.says();
    t.key(413);
    const asks = t.paths('ask').map((c) => c.body.what);
    t.key(415);
    await t.step(600);
    return { first, still: t.video.currentTime - at < 1, then, asks, plays: !t.video.paused, last: t.says() };
  },
  async noInlineStyle() {
    const play = film({ art_url: '/api/tv/device/art/a"b', chapters: [{ start_s: 720 }], markers: [{ start_s: 60, end_s: 144 }] });
    const t = await playing({ ...play });
    const over = [t.html(), ['m0', 'm1', 'm2'].map((id) => t.style(id).left)];
    const out = [];
    for (const state of ['starting', 'upnext', 'still', 'ended']) {
      t.now = { ...play, play_id: state, state, url: null };
      await t.step(1200);
      out.push([t.html(), t.style('pic').backgroundImage]);
    }
    t.now = { ...t.now, art_url: '/api/tv/device/art/other' };
    await t.step(1200);
    return { over, out, other: t.style('pic').backgroundImage };
  },
  async landsOff() {
    const t = tv({ kept: 'old' });
    t.video.land = 5;
    t.answers.poll = () => ready({ play: film() });
    t.run();
    await t.step(50);
    t.video.fire('loadedmetadata');
    t.video.fire('playing');
    await t.step(9000);
    const sets = t.video.sets.length;
    return { sets, state: t.last('poll').body.report.state, at: t.last('poll').body.report.position_s > 105 };
  },
  // a browser that ignores a seek until it plays; it starts paused, or plays at once, and may take
  // a moment more after it has begun
  async inertUntilItPlays(how) {
    const t = tv({ kept: 'old' });
    t.video.inert = true;
    t.answers.poll = () => ready({ play: film({ start_s: 1200, paused: how === 'paused' }) });
    t.run();
    await t.step(50);
    t.video.fire('loadedmetadata');
    t.video.fire('canplay');
    await t.step(how === 'paused' ? 12000 : 3000);
    if (how === 'paused') t.key(OK);
    if (how !== 'slow') t.video.inert = false;
    t.video.fire('playing');
    await t.step(1200);
    t.video.inert = false;
    await t.step(3000);
    return { at: t.paths('poll').map((c) => c.body.report).filter(Boolean).map((b) => b.position_s), kept: t.store['mm-tv-place'] || '', time: t.video.currentTime };
  },
  // a browser that takes a while to seek, lands off, and says it plays only meanwhile
  async playsWhileItSeeks(off) {
    const t = tv({ kept: 'old' });
    t.video.slow = Number(off);
    t.answers.poll = () => ready({ play: film({ start_s: 1200 }) });
    t.run();
    await t.step(50);
    t.video.fire('loadedmetadata');
    await t.step(100);
    t.video.fire('playing');
    await t.step(9000);
    return { report: t.last('poll').body.report, said: t.says(), sets: t.video.sets.length, reported: t.paths('poll').map((c) => c.body.report).filter(Boolean).map((b) => b.position_s) };
  },
  // mediaman goes away over a film and answers again: as another, or as the same with nothing on
  async restarts(how) {
    const t = await playing();
    let boot = 'a';
    t.now = film({ title: '<b>Orchard', state: how === 'over' ? 'upnext' : 'playing', url: how === 'over' ? null : film().url });
    t.answers.poll = () => ready({ play: t.now, boot });
    await t.step(1500);
    t.answers.poll = [502, null];
    await t.step(3000);
    t.answers.poll = () => ready({ play: t.now, boot });
    if (how !== 'same') boot = 'b';
    t.now = how === 'goes on' ? film({ play_id: 'p2' }) : null;
    await t.step(2500);
    const said = t.says();
    const src = t.video.attrs.src || null;
    const html = t.html();
    if (how === 'key') t.key(LEFT);
    if (how === 'press') t.click('b0');
    const pressed = t.says();
    const stays = how === 'key' || how === 'press' ? 1000 : 4000;
    await t.step(stays);
    const still = t.says();
    await t.step(8000);
    return { said, src, pressed, still, then: t.says(), raw: html.includes('<b>Orchard') };
  },
  // a page opened after the restart knew of no film, and one that shows nothing has nothing to say
  async restartsIdle() {
    const t = tv({ kept: 'old' });
    let boot = 'a';
    t.answers.poll = () => ready({ boot });
    t.run();
    await t.step(1500);
    boot = 'b';
    await t.step(4000);
    return t.says();
  },
  async pages() {
    const t = await playing();
    const out = { toPlay: [t.left().includes('is starting it') || t.entering(), t.leaving().some((n) => n[0])] };
    await t.step(1000);
    out.cleared = t.left();
    t.key(OK);
    out.paused = [t.left(), t.entering()];
    t.key(OK);
    t.now = film({ state: 'upnext', url: null, upnext_in_s: 9, next: { season: 1, episode: 5, title: 'Windfall' } });
    await t.step(1020);
    const at = t.entering();
    let left = t.left();
    t.now = null;
    for (let i = 0; i < 12 && !t.says().includes('is ready'); i += 1) await t.step(100);
    left = t.left();
    out.idle = [at, t.entering(), left.includes('Windfall'), t.leaving().some((n) => n[0])];
    t.answers.poll = ready({ name: 'Den' });
    await t.step(3500);
    out.renamed = [t.says().includes('Den is ready'), t.left(), t.entering()];
    return out;
  },
  // the controls of a film as its page leaves
  async barLeaves() {
    const t = await playing({ chapters: [{ start_s: 720 }, { start_s: 1440 }] });
    t.now = film({ chapters: t.now.chapters, state: 'upnext', url: null });
    for (let i = 0; i < 12 && !t.left(); i += 1) await t.step(100);
    const styles = t.leaving().map((n) => n[1]);
    return { width: styles.map((x) => x.width).filter(Boolean), left: styles.map((x) => x.left).filter(Boolean), ids: t.leaving().filter((n) => n[0]).length };
  },
  async pointer() {
    const t = tv({ kept: 'old' });
    t.answers.poll = ready();
    t.run();
    await t.step(50);
    const look = () => [t.node('b0').className, t.node('b1').className, t.held()];
    const first = look();
    t.point('b1', 5);
    const over = look();
    // the page moves under a pointer that lies still
    t.key(LEFT);
    t.point('b1', 5);
    const still = look();
    t.point('b1', 6);
    t.click('b1');
    const asks = t.says();
    t.key(RIGHT);
    const keyed = t.held();
    t.node('b0').focus();
    const tabbed = look();
    t.key(OK);
    return { first, over, still, asks, keyed, tabbed, kept: t.says() };
  },
  async usedCodeSeenLate(what) {
    const t = tv();
    const codes = [{ code: '482916', token: 'fresh' }, { code: '111111', token: 'newer' }, { code: '222222', token: 'third' }];
    t.answers.code = () => ({ ok: true, expires_in_s: 300, ...codes.shift() });
    t.answers.poll = { ok: true, state: 'pairing', expires_in_s: 290, poll_ms: 2000 };
    t.run();
    await t.step(1000);
    t.key(OK);
    await t.step(500);
    t.key(OK);
    await t.step(60000);
    const polled = () => ['fresh', 'newer', 'third'].map((k) => t.paths('poll').filter((c) => c.token === k).length);
    const count = polled();
    if (what === 'gone') t.answers.poll = (c) => (c.token === 'third' ? { ok: true, state: 'pairing', poll_ms: 2000 } : GONE);
    else t.answers.poll = (c) => (c.token === 'fresh' ? ready() : { ok: true, state: 'pairing', poll_ms: 2000 });
    await t.step(3000);
    const said = t.says();
    const then = polled();
    await t.step(10000);
    return { count, said, quiet: polled()[0] === then[0] && polled()[1] === then[1], store: t.store['mm-tv'] || null };
  },
  async shortSeek() {
    const t = await playing();
    const at = t.video.currentTime;
    t.answers.poll = () => ready({ play: t.now, commands: [{ id: 8, kind: 'seek', position_s: at - 1.5 }] });
    t.video.sets.length = 0;
    await t.step(1800);
    return [t.video.sets[0] - at, t.last('poll').body.ack];
  },
  async clockBack() {
    const t = await playing();
    t.answers.ask = { ok: true };
    t.jump(-3600e3);
    const polls = t.paths('poll').length;
    t.key(OK);
    await t.step(3000);
    const told = t.paths('poll').length - polls;
    t.key(OK);
    await t.step(600);
    t.video.stuck = true;
    await t.step(4000);
    const state = t.last('poll').body.report.state;
    t.answers.poll = () => ready({ play: film({ state: 'upnext', url: null, upnext_in_s: 2 }) });
    await t.step(1100);
    t.answers.poll = undefined;
    t.jump(-3600e3);
    await t.step(4000);
    return { told, state, asks: t.paths('ask').map((c) => c.body.what) };
  },
  async badCommand() {
    const t = await playing();
    let n = 0;
    Object.defineProperty(t.video, 'volume', { get: () => 1, set() { n += 1; throw new TypeError('no'); } });
    t.answers.poll = () => ready({ play: t.now, commands: [{ id: 8, kind: 'volume' }, { id: 9, kind: 'pause' }] });
    await t.step(1100);
    const polls = t.paths('poll').length;
    await t.step(5000);
    return { more: t.paths('poll').length > polls, times: n, paused: t.video.paused, ack: t.last('poll').body.ack };
  },
  async brokenPlay() {
    const t = await playing();
    t.video.load = () => { throw new Error('no'); };
    t.now = film({ play_id: 'p2' });
    await t.step(1100);
    const polls = t.paths('poll').length;
    await t.step(5000);
    return t.paths('poll').length > polls;
  },
  async usedCodeAndNewCode() {
    const t = tv({ kept: 'old' });
    t.answers.poll = ready();
    t.run();
    await t.step(1500);
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.key(OK);
    await t.step(200);
    // Someone enters the code: the old token is refused before the new one is asked.
    t.answers.poll = (c) => (c.token === 'fresh' ? ready({ owner: 'tobias' }) : GONE);
    t.answers.code = { ok: true, code: '111111', token: 'newer', expires_in_s: 300 };
    t.key(OK);
    await t.step(1500);
    return { said: t.says(), store: t.store, asked: t.paths('code').length };
  },
  async bootWaits() {
    const t = tv({ kept: 'old' });
    t.run();
    const boot = t.html();
    t.answers.poll = ready({ play: film() });
    await t.step(10000);
    return { boot, later: t.html() };
  },
  async bootHasNoButtons() {
    const t = tv({ kept: 'old' });
    t.answers.poll = ready();
    t.run();
    await t.step(1500);
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.key(OK);
    await t.step(500);
    t.key(RIGHT); t.key(OK);
    t.answers.poll = (c) => (c.token === 'fresh' ? undefined : GONE);
    await t.step(1500);
    const said = t.says();
    t.key(OK);
    await t.step(200);
    return { said, asked: t.paths('code').length };
  },
  // a browser that loads nothing, or no more than what it must, before it plays
  async pausedWithNothingLoaded(events) {
    const t = tv({ kept: 'old' });
    t.answers.poll = () => ready({ play: film({ paused: true }) });
    t.run();
    await t.step(50);
    if (events === 'metadata') t.video.fire('loadedmetadata');
    await t.step(events === 'metadata' ? 1500 : 8000);
    const report = t.last('poll').body.report;
    const src = t.video.sets.length;
    let loads = 0;
    const load = t.video.load;
    t.video.load = function () { loads += 1; load.call(this); };
    await t.step(100000);
    const late = t.last('poll').body.report.state;
    t.key(OK);
    await t.step(100);
    return { state: report.state, at: report.position_s, loads, late, plays: !t.video.paused, src };
  },
  async commands() {
    const t = await playing();
    const cs = [{ id: 8, kind: 'pause' }, { id: 9, kind: 'seek', position_s: 900 }, { id: 10, kind: 'volume', level: 0.3 }, { id: 11, kind: 'mute', muted: true }];
    t.answers.poll = () => ready({ play: t.now, commands: cs });
    await t.step(1200);
    const once = t.video.calls.slice();
    const was = { time: t.video.currentTime, volume: t.video.volume, muted: t.video.muted };
    t.video.time = 950;
    await t.step(3000);
    return { once, again: t.video.calls, was, time: t.video.currentTime, ack: t.last('poll').body.ack, state: t.last('poll').body.report.state };
  },
  async pausedStaysPaused() {
    const t = await playing();
    t.key(OK);
    t.video.fire('pause');
    t.now = film({ state: 'paused' });
    await t.step(2500);
    t.video.calls.length = 0;
    t.now = film({ play_id: 'p2', start_s: 120, url: '/api/cast/u/index.m3u8', state: 'loading', paused: true });
    await t.step(2500);
    t.video.fire('loadedmetadata');
    t.video.fire('canplay');
    await t.step(1500);
    const kept = { src: t.video.src, calls: t.video.calls.slice(), state: t.last('poll').body.report.state };
    t.now = film({ play_id: 'p2', start_s: 120, url: '/api/cast/u/index.m3u8', state: 'paused', paused: true });
    await t.step(1500);
    t.now = film({ play_id: 'p3', state: 'loading', paused: false });
    await t.step(2500);
    return { kept, anew: t.video.calls };
  },
  async needsKey() {
    const t = tv({ kept: 'old' });
    t.video.refuse = true;
    t.answers.poll = () => ready({ play: film() });
    t.run();
    await t.step(2500);
    const said = t.says();
    const state = t.last('poll').body.report.state;
    t.video.refuse = false;
    t.key(OK);
    t.video.fire('playing');
    await t.step(1500);
    return { said, state, now: t.last('poll').body.report.state };
  },
  // The first press goes full screen and does nothing else; polls never ask.
  async fullByKey() {
    const t = tv({ kept: 'old', full: true });
    t.answers.poll = () => ready({ poll_ms: 1000, commands: t.commands || [] });
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.run();
    await t.step(100);
    const before = { hint: t.hint(), asks: t.fullAsks.length };
    t.commands = [{ id: 3, kind: 'volume', level: 0.5 }];
    await t.step(5000);
    const polled = t.fullAsks.length;
    t.key(OK);
    const first = { asks: t.fullAsks.slice(), said: t.says() };
    await t.step(100);
    const full = t.hint();
    t.key(OK);
    await t.step(100);
    return { before, polled, first, full, asks: t.fullAsks.length, then: t.says() };
  },
  async fullHint(where) {
    const t = tv({ kept: 'old', full: where !== 'none' });
    t.answers.poll = () => ready({ play: t.now });
    t.now = where === 'idle' || where === 'none' ? null : film();
    t.run();
    await t.step(50);
    if (t.now) { t.video.fire('loadedmetadata'); t.video.fire('playing'); }
    await t.step(600);
    const at = t.hint();
    await t.step(5000);
    const later = t.hint();
    const said = t.says();
    t.key(OK);
    await t.step(100);
    const full = t.hint();
    t.leaveFull();
    const left = t.hint();
    await t.step(5000);
    return { at, later, full, left, said, pressed: t.says(), after: t.hint(), asks: t.fullAsks.length };
  },
  // A refused request swallows one press, and a minute on the page asks again.
  async fullRefused() {
    const t = tv({ kept: 'old', full: 'refuse' });
    t.answers.poll = () => ready({ poll_ms: 2000 });
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.run();
    await t.step(100);
    t.key(OK);
    await t.step(100);
    const refused = { hint: t.hint(), asks: t.fullAsks.length, said: t.says() };
    t.key(OK);
    await t.step(100);
    const next = { asks: t.fullAsks.length, said: t.says() };
    await t.step(30000);
    const soon = { hint: t.hint() };
    t.key(RIGHT);
    soon.asks = t.fullAsks.length;
    await t.step(40000);
    const later = { hint: t.hint() };
    t.key(RIGHT);
    return { refused, next, soon, later, asks: t.fullAsks.length };
  },
  async fullWebkit() {
    const t = tv({ kept: 'old', full: 'webkit' });
    t.answers.poll = () => ready({ poll_ms: 2000 });
    t.run();
    await t.step(100);
    const before = t.hint();
    t.key(OK);
    await t.step(100);
    const full = t.hint();
    t.leaveFull();
    return { before, asks: t.fullAsks.slice(), full, left: t.hint() };
  },
  // An arrow is the first press of a film that has hidden its controls.
  async fullFirstArrow() {
    const t = tv({ kept: 'old', full: true });
    t.answers.poll = () => ready({ play: film() });
    t.run();
    await t.step(50);
    t.video.fire('loadedmetadata');
    t.video.fire('playing');
    await t.step(5000);
    const before = t.ov();
    t.key(RIGHT);
    return { before, ov: t.ov(), asks: t.fullAsks.length };
  },
  // The pages whose one button is harmless: the first OK asks for full screen and acts too.
  async fullThenAct(page) {
    const t = tv({ kept: page === 'conn' ? null : 'old', full: true });
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    if (page === 'conn') t.answers.poll = ready({ poll_ms: 2000 });
    else { t.now = film(); t.answers.poll = () => ready({ play: t.now, boot: t.now ? 'a' : 'b', poll_ms: 1000 }); }
    t.run();
    await t.step(page === 'conn' ? 2500 : 50);
    if (page !== 'conn') {
      t.video.fire('loadedmetadata'); t.video.fire('playing');
      await t.step(600);
      t.now = null;
      await t.step(1500);
    }
    const said = t.says();
    t.key(OK);
    return { said, asks: t.fullAsks.length, then: t.says() };
  },
  async fullAndBack() {
    const t = tv({ kept: 'old', full: true });
    t.answers.poll = () => ready({ poll_ms: 2000 });
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.run();
    await t.step(100);
    t.key(OK);
    await t.step(100);
    t.key(OK);
    await t.step(100);
    const code = t.says();
    t.leaveFull();
    t.key(BACK);
    return { code, back: t.says(), asks: t.fullAsks.length };
  },
  async fullFirstOk(blocked) {
    const t = tv({ kept: 'old', full: true });
    t.answers.poll = () => ready({ play: film() });
    t.video.refuse = Boolean(blocked);
    t.run();
    await t.step(50);
    if (!blocked) { t.video.fire('loadedmetadata'); t.video.fire('playing'); }
    await t.step(600);
    t.video.refuse = false;
    const said = t.says(), hint = t.hint();
    t.video.calls.length = 0;
    t.order.length = 0;
    t.key(OK);
    return { said, hint, order: t.order.slice(), calls: t.video.calls, asks: t.fullAsks.length, then: t.says() };
  },
  async keys() {
    const t = await playing();
    await t.step(5000);
    const hidden = t.text.call(null, 'el') !== null && t.html().includes('id="ov"');
    t.video.calls.length = 0;
    t.key(OK);
    const wakes = t.video.calls.slice();
    t.key(OK);
    const pauses = t.video.calls.slice();
    const at = t.video.currentTime;
    t.key(LEFT);
    t.key(OK);
    const back = Math.round(at - t.video.currentTime);
    t.answers.ask = { ok: true };
    t.key(RIGHT); t.key(RIGHT); t.key(RIGHT); t.key(OK);
    await t.step(2000);
    return { hidden, wakes, pauses, back, ask: t.last('ask').body, active: t.paths('poll').some((c) => c.body.active) };
  },
  async upnext() {
    const t = await playing();
    t.video.ended = true;
    t.video.fire('ended');
    await t.step(1200);
    const reported = t.last('poll').body.report;
    let left = 10;
    t.answers.poll = () => ready({ play: film({ state: 'upnext', url: null, upnext_in_s: (left = Math.max(0, left - 1)), next: { season: 1, episode: 5, title: 'Windfall' } }) });
    await t.step(2200);
    const said = t.says() + '|' + t.text('in');
    t.answers.ask = [409, { ok: false, error: 'no_next' }];
    await t.step(11000);
    return { reported: [reported.state, reported.position_s], said, asks: t.paths('ask').map((c) => c.body) };
  },
  async noCountdown() {
    const t = await playing();
    t.now = film({ state: 'upnext', url: null, next: { season: null, episode: null, title: null } });
    await t.step(30000);
    const said = t.text('in');
    t.answers.ask = { ok: true };
    t.key(OK); t.key(OK);
    await t.step(500);
    return { said, asks: t.paths('ask').map((c) => c.body) };
  },
  async still() {
    const t = await playing();
    t.now = film({ state: 'still', url: null, still_in_s: 540 });
    await t.step(1500);
    const said = t.says();
    t.answers.ask = { ok: true };
    t.key(OK);
    await t.step(100);
    return { said, ask: t.last('ask').body.what };
  },
  async goneTwice() {
    const t = await playing();
    let n = 1;
    t.answers.ask = { ok: true };
    t.answers.poll = () => ready({ play: film({ play_id: 'p' + n, state: 'failed', error: 'gone', url: null }) });
    for (let i = 0; i < 4; i += 1) { await t.step(2500); n += 1; }
    await t.step(2500);
    const byItself = t.paths('ask').length;
    const said = t.says();
    t.key(OK);
    await t.step(100);
    return { byItself, said, ask: t.last('ask').body };
  },
  async goneWhilePaused() {
    const t = await playing();
    t.answers.ask = { ok: true };
    t.key(OK);
    t.video.fire('pause');
    t.now = film({ state: 'failed', error: 'gone', url: null });
    await t.step(2500);
    const byItself = t.last('ask').body;
    t.now = film({ play_id: 'p2', url: '/api/cast/u/index.m3u8', state: 'loading', paused: true });
    t.video.calls.length = 0;
    await t.step(2500);
    t.video.fire('loadedmetadata');
    t.video.fire('canplay');
    await t.step(1500);
    const held = t.video.calls.slice();
    t.key(OK);
    await t.step(100);
    return { byItself, held, pressed: t.video.calls };
  },
  async tries() {
    const t = await playing();
    t.video.fire('error');
    const shown = t.says();
    await t.step(1500);
    t.video.fire('loadedmetadata');
    const at = Math.round(t.video.currentTime);
    const early = t.last('poll').body.report.state;
    t.video.fire('error');
    await t.step(1500);
    t.video.fire('error');
    await t.step(1500);
    const late = t.last('poll').body.report.state;
    t.now = film({ state: 'failed', error: 'tv', url: null });
    await t.step(2500);
    const said = t.says();
    t.answers.ask = [429, { ok: false, error: 'too_many_streams' }];
    t.key(OK);
    await t.step(600);
    return { shown, at, early, late, said, ask: t.last('ask').body.what, then: t.says() };
  },
  async stopsShort() {
    const t = await playing();
    t.video.time = 2879;
    t.video.stuck = true;
    await t.step(8000);
    return t.last('poll').body.report.state;
  },
  async stalls() {
    const t = await playing();
    t.video.stuck = true;
    await t.step(4000);
    const state = t.last('poll').body.report.state;
    const said = t.says();
    t.video.stuck = false;
    await t.step(2500);
    return { state, said, after: t.last('poll').body.report.state };
  },
  async lost() {
    const t = await playing();
    t.answers.poll = [502, null];
    await t.step(15000);
    const whilePlaying = t.says();
    t.video.stuck = true;
    await t.step(4000);
    const stuck = t.says();
    const u = tv({ kept: 'old' });
    u.answers.poll = ready();
    u.run();
    await u.step(1500);
    u.answers.poll = [503, { ok: false }];
    await u.step(15000);
    const idle = u.says();
    u.answers.poll = ready();
    await u.step(4000);
    return { whilePlaying, stuck, idle, kept: u.store, back: u.says() };
  },
  async unpairs() {
    const t = tv({ kept: 'old' });
    t.answers.poll = ready();
    t.answers.unpair = { ok: true };
    t.run();
    await t.step(1500);
    t.key(RIGHT); t.key(OK);
    const asks = t.says();
    const before = t.paths('unpair').length;
    t.key(RIGHT); t.key(OK);
    await t.step(3000);
    const polls = t.paths('poll').length;
    await t.step(10000);
    return { asks, before, sent: t.paths('unpair').map((c) => c.token), said: t.says(), store: t.store, quiet: t.paths('poll').length === polls };
  },
  async escapes() {
    const bad = '<img src=x onerror=a>"';
    const out = [];
    for (const state of ['loading', 'upnext', 'still', 'ended', 'failed']) {
      const t = tv({ kept: 'old' });
      const play = film({ title: bad, episode_title: bad, art_url: '/x");</div>' + bad, state, error: 'tv', url: null, next: { season: 1, episode: 5, title: bad } });
      t.answers.poll = ready({ owner: bad, name: bad, play });
      t.run();
      await t.step(50);
      out.push(t.html());
    }
    const t = tv({ kept: 'old' });
    t.answers.poll = ready({ owner: bad, name: bad });
    t.run();
    await t.step(50);
    out.push(t.html());
    t.key(RIGHT); t.key(OK);
    out.push(t.html());
    return out;
  },
  async cannotPlay() {
    const t = tv({ plays: false });
    t.answers.code = { ok: true, code: '482916', token: 'fresh', expires_in_s: 300 };
    t.run();
    await t.step(1000);
    const said = t.says();
    const sent = t.sent.length;
    t.key(OK);
    await t.step(500);
    return { said, sent, then: t.says() };
  },
  async qr(text) {
    const t = tv({ kept: 'x' }).run();
    const rows = t.qr(text);
    return rows && rows.map((r) => r.join(''));
  },
  // node's own copy of acorn reads each file as ES5
  async es5() {
    const acorn = require('internal/deps/acorn/acorn/dist/acorn');
    const out = {};
    for (const f of ['js/tv.js', 'vendor/hls/hls.min.js']) {
      try { acorn.parse(fs.readFileSync(path.join(WEB, f), 'utf8'), { ecmaVersion: 5 }); out[f] = 'ES5'; } catch (e) { out[f] = e.message; }
    }
    try { acorn.parse('let x = 1', { ecmaVersion: 5 }); out.let = 'ES5'; } catch (e) { out.let = 'refused'; }
    return out;
  },
};

scenarios[process.argv[2]](...process.argv.slice(3)).then((out) => {
  process.stdout.write(JSON.stringify(out));
}, (e) => { console.error(e); process.exit(1); });
