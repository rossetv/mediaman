// Runs the real download.js and share.js on the page the server draws, given on stdin as
// nested [tag, attrs, ...children] arrays, and prints the outcome as JSON.
// Usage: node download_page_harness.js <scenario> < page.json
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const SPEC = JSON.parse(fs.readFileSync(0, 'utf8'));

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', on); },
});
Node.prototype.replaceChildren = function (...cs) { this.textContent = ''; this.append(...cs); };
Node.prototype.removeChild = function (c) { c.remove(); return c; };
Object.defineProperty(Node.prototype, 'content', { get() { return this; } });
// The busy button's spinner and word are narrower.
Object.defineProperty(Node.prototype, 'offsetWidth', { get() { return this.getAttribute('aria-disabled') === 'true' ? 92 : 132; } });

function page() {
  const doc = makeDocument();
  const bar = doc.body.appendChild(doc.createElement('header'));
  bar.className = 'share-bar';
  SPEC.forEach((s) => doc.body.appendChild(build(doc, s)));
  const inTemplate = (n) => !!n.parentNode && !!n.parentNode.closest('template');
  // A template's parts are not in the page until they are cloned.
  const all = doc.documentElement.querySelectorAll.bind(doc.documentElement);
  doc.querySelectorAll = (s) => all(s).filter((n) => !inTemplate(n));
  doc.querySelector = (s) => doc.querySelectorAll(s)[0] || null;
  doc.getElementById = (id) => doc.querySelector('#' + id);
  const p = {
    doc, payload: {}, posted: 0, asked: [], timers: [],
    answer: () => Promise.resolve({ service: 'radarr', tmdb_id: 7, poll_token: 'p2' }),
  };
  const make = doc.createElement;
  doc.createElement = (tag) => { const n = make(tag); n.style.setProperty = (k, v) => { n.style[k] = v; }; return n; };
  const styles = p.styles = {};
  doc.querySelector('.share-page').style.setProperty = (k, v) => { styles[k] = v; };
  doc.hidden = false;
  class APIError extends Error { constructor(status, data) { super(); this.status = status; this.data = data || {}; } }
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, encodeURIComponent,
    getComputedStyle: () => ({}), matchMedia: () => ({ matches: true }),
    setInterval: (fn) => { p.tick = fn; return 1; }, clearInterval() { p.tick = null; },
    setTimeout: (fn) => { p.timers.push(fn); }, MM: {},
  };
  ctx.window = ctx;
  const heard = {};
  ctx.addEventListener = (type, fn) => { (heard[type] = heard[type] || []).push(fn); };
  p.hear = (type, e) => (heard[type] || []).forEach((fn) => fn(e));
  p.resized = () => p.hear('resize');
  ctx.matchMedia = () => ({ matches: p.still });
  p.still = true;
  ctx.location = { origin: 'https://mediaman.example' };
  // Two entries of one page, as a browser's history has them.
  const entries = [null];
  // Going back is asked for now and done later.
  const asked = [];
  p.went = () => { asked.splice(0).forEach(go); return !!p.left; };
  p.now = 1e6;
  ctx.Date = { now: () => p.now };
  let at = 0;
  const go = (by) => { if (at + by < 0) p.left = true; else if (entries[at + by] !== undefined) { at += by; p.hear('popstate', { state: entries[at] }); } };
  ctx.history = { get state() { return entries[at]; }, back: () => { asked.push(-1); },
    pushState: (s) => { entries.splice(++at, entries.length, s); }, replaceState: (s) => { entries[at] = s; } };
  p.back = () => go(-1); p.forward = () => go(1); p.entries = () => entries.length; p.history = ctx.history;
  Node.prototype.scrollIntoView = function (how) { p.scrolled = [this.className, how.block, how.behavior]; };
  Object.defineProperty(Node.prototype, 'contentWindow', { configurable: true, get() { return this.win = this.win || { postMessage: (m) => { (p.told = p.told || []).push(JSON.parse(m)); } }; } });
  // Where the player ends and the buttons start.
  const rect = Node.prototype.getBoundingClientRect;
  p.place = (playerEnds, buttonsStart) => {
    doc.createElement('div').constructor.prototype.getBoundingClientRect = function () {
      if (this.classList.contains('share-player')) return { ...rect.call(this), bottom: playerEnds };
      if (this.classList.contains('share-act')) return { ...rect.call(this), top: buttonsStart };
      return rect.call(this);
    };
  };
  ctx.MM.api = {
    get: (url) => { p.asked.push(url); return p.get ? p.get() : Promise.resolve(p.payload); },
    post: (url) => { p.posted += 1; p.postedTo = url; return p.answer(); },
    APIError,
    messageOf: (err, fallback) => (err.data && err.data.message) || fallback,
  };
  p.refuse = (status, data) => { p.answer = () => Promise.reject(new APIError(status, data)); };
  p.error = (status) => new APIError(status);
  vm.createContext(ctx);
  ['core/dom.js', 'downloads/build_dom.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  p.start = () => vm.runInContext(fs.readFileSync(path.join(JS, 'download.js'), 'utf8'), ctx);
  p.trailer = () => vm.runInContext(fs.readFileSync(path.join(JS, 'share.js'), 'utf8'), ctx);
  p.one = (s) => doc.querySelector(s);
  p.act = () => p.one('#download-action');
  p.later = () => { p.timers.splice(0).forEach((fn) => fn()); };
  p.poll = async (payload) => { p.payload = payload; p.tick(); await settle(); };
  return p;
}

async function press(p) {
  const button = p.one('[data-dl]');
  // A click in Safari puts the focus on the page around the button.
  p.doc.activeElement = p.doc.body;
  click(p.doc, button);
  const during = { label: button.textContent, busy: button.getAttribute('aria-disabled'),
    focused: p.doc.activeElement === button, spinner: !!button.querySelector('.btn-spinner'), minWidth: button.style.minWidth };
  click(p.doc, button);
  await settle();
  return during;
}

const shown = (el) => { for (let a = el; a; a = a.parentNode) if (a.hidden) return false; return true; };
const words = (el) => (el.nodeType === 3 ? el.textContent : shown(el) ? el.childNodes.map(words).join(' ') : '').replace(/\s+/g, ' ').trim();

function seen(p) {
  const act = p.act();
  const steps = act.querySelector('.share-steps');
  const buttons = act.querySelectorAll('.btn');
  return {
    words: words(act), title: p.one('.watch-banner__title').textContent,
    step: steps && words(steps.querySelector('.is-now') || steps.children[3]),
    alert: !!act.querySelector('[role="alert"]'),
    buttons: buttons.map((b) => words(b)), links: buttons.filter((b) => b.tagName === 'A').map((b) => b.getAttribute('href')),
    busy: buttons.length ? buttons[0].getAttribute('aria-disabled') : null,
    focus: p.doc.activeElement === p.doc.body ? null : words(p.doc.activeElement) || p.doc.activeElement.tagName,
    said: p.one('[data-said]').textContent, posted: p.posted,
    bar: !!steps && !act.querySelector('[data-fold]').classList.contains('is-shut'),
    figures: steps && words(act.querySelector('.dl-progress__line')),
    colour: p.styles['--sat'],
  };
}

const RUNNING = { state: 'downloading', state_label: 'Downloading', progress: 34, eta: '5 min',
  episodes: [{ label: 'S01E01', title: 'One', state: 'queued', progress: 0 }] };
const READY = { state: 'ready', state_label: 'Ready', progress: 100 };

const SCENARIOS = {
  async answers() {
    const ANSWERS = {
      unanswered: [0, { message: 'Could not reach mediaman.' }],
      wordless: [403, {}],
      ...JSON.parse(process.argv[3]),
      queued: [409, { poll_token: 'p3', download_state: 'queued' }],
      queuedMailed: [409, { poll_token: 'p3', download_state: 'queued', notify_by_email: true }],
      inLibrary: [409, { poll_token: 'p3', download_state: 'in_library' }],
      tracked: [409, { poll_token: 'p3', download_state: 'tracked' }],
    };
    const out = {};
    for (const name of Object.keys(ANSWERS)) {
      const p = page();
      p.start();
      p.refuse(...ANSWERS[name]);
      await press(p);
      out[name] = { ...seen(p), polls: p.asked };
    }
    return out;
  },
  async retry() {
    const p = page();
    const good = p.answer;
    p.start();
    p.refuse(502);
    const first = await press(p);
    const failed = seen(p);
    p.answer = good;
    p.payload = { state: 'searching' };
    const second = await press(p);
    return { first, failed, second, done: seen(p), to: p.postedTo, polls: p.asked };
  },
  // A press, then the polls of a whole download.
  async run() {
    const p = page();
    p.start();
    p.payload = { state: 'searching' };
    await press(p);
    const out = { searching: seen(p) };
    await p.poll(RUNNING);
    out.downloading = seen(p);
    await p.poll({ ...RUNNING, progress: 80, eta: '' });
    out.later = seen(p);
    await p.poll({ state: 'unknown', progress: 0 });
    out.unanswered = seen(p);
    await p.poll({ state: 'almost_ready', progress: 100 });
    out.almost = seen(p);
    await p.poll(READY);
    out.last = seen(p);
    p.later();
    out.ready = seen(p);
    return out;
  },
  async eta() {
    const p = page();
    p.start();
    await settle();
    const step = async (polled) => { await p.poll({ ...RUNNING, ...polled }); return seen(p).figures; };
    return { start: seen(p).figures, noEtaYet: await step({ eta: '' }), arrives: await step({ eta: '5 min' }),
      changes: await step({ eta: '4 min' }), clears: await step({ eta: '' }) };
  },
  async pictures() {
    const p = page();
    p.start();
    const there = () => p.doc.querySelectorAll('img').map((i) => i.getAttribute('src'));
    const before = there();
    await press(p);
    return { before, after: there() };
  },
  async episodes() {
    const p = page();
    p.start();
    await settle();
    const box = p.one('.share-eps');
    const ep = (label, state) => ({ label, title: label, state, progress: 0 });
    const state = () => ({ shown: !box.hidden, open: box.hasAttribute('open'), summary: words(box.querySelector('summary')),
      rows: box.querySelectorAll('.dl-ep-status-pill').map((e) => e.textContent) });
    const out = { none: box.hidden };
    await p.poll({ ...RUNNING, episodes: [ep('S01E01', 'queued'), ep('S01E02', 'searching')] });
    out.first = state();
    box.setAttribute('open', '');
    await p.poll({ ...RUNNING, episodes: [ep('S01E01', 'ready'), ep('S01E02', 'searching'), ep('S01E03', 'queued')] });
    out.after = state();
    const hero = p.one('.share-hero');
    const summary = box.querySelector('summary');
    const list = box.querySelector('[data-v="ep-list"]');
    const eased = [];
    list.animate = (frames) => { eased.push(frames); return (p.easing = {}); };
    const press = () => { click(p.doc, summary); return { open: box.hasAttribute('open'), held: hero.style.alignItems || '' }; };
    p.still = false;
    press();
    p.easing.onfinish();
    press();
    p.easing.onfinish();
    p.still = true;
    out.eased = eased;
    out.shut = press();
    out.opened = press();
    p.resized();
    out.narrowed = hero.style.alignItems || '';
    p.doc.activeElement = summary;
    await p.poll({ state: 'searching' });
    out.letGo = hero.style.alignItems || '';
    out.gone = { shown: !box.hidden, focus: p.doc.activeElement.tagName };
    await p.poll(RUNNING);
    out.back = state();
    return out;
  },
  // Answers that come after a newer one, or once the title is ready.
  async late() {
    const out = {};
    const start = async () => {
      const p = page();
      p.payload = RUNNING;
      p.start();
      await settle();
      const asked = [];
      p.get = () => new Promise((resolve, reject) => asked.push({ resolve, reject }));
      p.ask = () => { p.tick(); return asked[asked.length - 1]; };
      return p;
    };
    let p = await start();
    let first = p.ask(), second = p.ask(), third = p.ask(), fourth = p.ask();
    first.resolve(READY);
    await settle();
    second.resolve(READY);
    third.resolve(RUNNING);
    fourth.reject(p.error(401));
    await settle();
    out.ready = { ...seen(p), timers: p.timers.length };
    p.later();
    out.after = seen(p);

    p = await start();
    first = p.ask(); second = p.ask(); third = p.ask();
    third.resolve({ ...RUNNING, progress: 60 });
    await settle();
    first.resolve(RUNNING);
    second.reject(p.error(401));
    await settle();
    out.overtaken = seen(p);

    p = await start();
    p.ask().reject(p.error(401));
    await settle();
    out.stopped = { ...seen(p), polling: !!p.tick };
    return out;
  },
  async trailer() {
    const p = page();
    p.start();
    p.trailer();
    const link = p.one('[data-trailer]');
    const frames = () => p.doc.querySelectorAll('iframe').map((f) => ({ ...f.attrs, src: f.src }));
    const now = () => ({ frames: frames(), on: p.one('.share-hero').classList.contains('is-trailer'),
      inert: p.one('.watch-banner__body').inert, focus: p.doc.activeElement.getAttribute('aria-label') || p.doc.activeElement.className });
    const out = { before: now() };
    p.place(700, 400);
    click(p.doc, link, { ctrlKey: true });
    out.newTab = now();
    click(p.doc, link);
    out.on = now();
    p.place(390, 430);
    p.resized();
    out.upright = now();
    p.one('[data-dl]').focus();
    p.place(390, 300);
    p.resized();
    out.sideways = now();
    (p.doc.listeners.keydown || []).forEach((fn) => fn({ key: 'Escape' }));
    p.went();
    out.off = now();
    p.resized();
    out.offResized = now();
    out.scrolled = p.scrolled;
    out.told = p.told.map((m) => m.func || m.event);
    out.entries = p.entries();
    const press = (el) => { p.now += 1000; click(p.doc, el || link); p.went(); };
    press();
    press();
    out.again = now();
    press();
    p.back();
    out.back = now();
    p.forward();
    out.forward = now();
    const ended = (state, source, origin) => p.hear('message', { source, origin: origin || 'https://www.youtube-nocookie.com',
      data: JSON.stringify({ event: 'onStateChange', info: state }) });
    const win = p.one('iframe').contentWindow;
    ended(1, win);
    ended(0, {});
    ended(0, win, 'https://elsewhere.example');
    out.played = now();
    ended(0, win);
    p.went();
    out.ended = now();
    p.still = false;
    press();
    // Safari leaves the focus of a clicked button on the page around it.
    p.one('main').focus();
    press(p.one('.ttl__close'));
    out.fading = now();
    p.later();
    out.faded = now();
    p.still = true;
    const reloaded = page();
    reloaded.history.pushState({ trailer: 'dQw4w9WgXcQ' });
    reloaded.trailer();
    reloaded.hear('load');
    reloaded.went();
    out.reloaded = { state: reloaded.history.state, on: reloaded.one('.share-hero').classList.contains('is-trailer') };
    reloaded.back();
    out.reloaded.left = reloaded.left;
    const returned = page();
    returned.history.scrollRestoration = 'manual';
    returned.trailer();
    out.returned = returned.history.scrollRestoration;
    press();
    p.act().replaceChildren();
    click(p.doc, p.one('.ttl__close'));
    out.gone = { ...now(), tag: p.doc.activeElement.tagName };
    return out;
  },
  // Each way to close the trailer, twice before the browser has gone back once.
  async twice() {
    const key = (p, e) => p.doc.listeners.keydown.forEach((fn) => fn(e));
    const ends = (p, event, info) => p.hear('message', { source: p.one('iframe').contentWindow,
      origin: 'https://www.youtube-nocookie.com', data: JSON.stringify({ event, info }) });
    const WAYS = {
      close: (p, again) => click(p.doc, p.one('.ttl__close'), { detail: again ? 2 : 1 }),
      button: (p, again) => click(p.doc, p.one('[data-trailer]'), { detail: again ? 2 : 1 }),
      escape: (p, again) => key(p, { key: 'Escape', repeat: !!again }),
      end: (p, again) => (again ? ends(p, 'infoDelivery', { playerState: 0 }) : ends(p, 'onStateChange', 0)),
    };
    const out = {};
    for (const way of Object.keys(WAYS)) {
      const p = page();
      p.still = false;
      p.start();
      p.trailer();
      click(p.doc, p.one('[data-trailer]'));
      p.now += 1000;
      WAYS[way](p);
      WAYS[way](p, true);
      const barHeld = p.one('.share-bar').style.pointerEvents;
      const left = p.went();
      WAYS[way](p, true);
      out[way] = { left: left || p.went(), on: p.one('.share-hero').classList.contains('is-trailer'), barHeld };
      p.later();
      out[way].barAfter = p.one('.share-bar').style.pointerEvents;
      p.now += 1000;
      click(p.doc, p.one('[data-trailer]'));
      out[way].opens = p.one('.share-hero').classList.contains('is-trailer');
      p.back();
      out[way].back = [p.one('.share-hero').classList.contains('is-trailer'), !!p.left];
    }
    const p = page();
    p.start();
    p.trailer();
    const held = { key: 'Enter', repeat: true, target: p.one('[data-trailer]'), preventDefault() { this.stopped = true; } };
    key(p, held);
    const typed = { ...held, repeat: false, stopped: false };
    key(p, typed);
    out.enter = { held: !!held.stopped, typed: !!typed.stopped };
    return out;
  },
};

SCENARIOS[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
