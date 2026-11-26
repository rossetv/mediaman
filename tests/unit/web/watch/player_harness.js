/* Runs player.js against a small fake DOM built from the rendered player
   markup, with fake hls.js, API, clock and video element.

   Usage: node player_harness.js <markup.html> <scenario> [reduced]
          node player_harness.js <markup.html> <scenario> '<saved settings as JSON>' [<quality the server plays>]
   PLAYER_PAGES in the environment names the file of each other page, by address.
   A scenario named away_... has the Play on sheet, AirPlay and the API of the TVs too.
   Prints the scenario's observations as one JSON object. */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const VOID = { input: 1, br: 1, img: 1, meta: 1, link: 1, source: 1 };

/* ---- fake DOM ------------------------------------------------------------ */

let doc = null;

function decode(s) {
  return s.replace(/&#(\d+);/g, (m, n) => String.fromCharCode(+n))
    .replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}

class Node {
  constructor() { this.parentNode = null; this.childNodes = []; this.listeners = {}; }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (child.isFragment) { child.childNodes.slice().forEach((c) => this.appendChild(c)); return child; }
    if (child.parentNode) child.parentNode.childNodes.splice(child.parentNode.childNodes.indexOf(child), 1);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, at) {
    this.appendChild(child);
    this.childNodes.pop();
    this.childNodes.splice(at ? this.childNodes.indexOf(at) : this.childNodes.length, 0, child);
    return child;
  }
  get nextSibling() { const all = this.parentNode.childNodes; return all[all.indexOf(this) + 1] || null; }
  remove() { if (this.parentNode) { this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1); this.parentNode = null; } }
  get isConnected() { return doc.contains(this); }
  get children() { return this.childNodes.filter((c) => c instanceof Element); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const l = this.listeners[type] || [];
    if (l.indexOf(fn) >= 0) l.splice(l.indexOf(fn), 1);
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    let node = this;
    while (node) {
      ev.currentTarget = node;
      (node.listeners[ev.type] || []).slice().forEach((fn) => fn.call(node, ev));
      if (!ev.bubbles || ev.stopped) break;
      node = node.parentNode || (node === doc.documentElement ? doc : null);
    }
  }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(v) { this.childNodes.forEach((c) => { c.parentNode = null; }); this.childNodes = []; if (v) this.appendChild(new Text(String(v))); }
  contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
  elements() {
    const out = [];
    const walk = (n) => n.childNodes.forEach((c) => { if (c instanceof Element) { out.push(c); walk(c); } });
    walk(this);
    return out;
  }
  querySelectorAll(sel) { return this.elements().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

class Text extends Node {
  constructor(t) { super(); this.data = t; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = v; }
}

class Element extends Node {
  constructor(tag, attrs) {
    super();
    this.tagName = tag.toUpperCase();
    this.attrs = Object.assign({}, attrs || {});
    this.style = { setProperty(n, v) { this[n] = v; } };
    const el = this;
    this.classList = {
      list() { return (el.attrs.class || '').split(/\s+/).filter(Boolean); },
      contains(c) { return this.list().indexOf(c) >= 0; },
      add(c) {
        if (c === 'is-leaving' && !this.contains(c)) el.dropping = true;
        if (!this.contains(c)) el.attrs.class = this.list().concat(c).join(' ');
      },
      remove(c) {
        if (c === 'is-leaving') el.cutShort();
        el.attrs.class = this.list().filter((x) => x !== c).join(' ');
      },
      toggle(c, on) {
        if (on === undefined) on = !this.contains(c);
        if (on) this.add(c); else this.remove(c);
        return on;
      },
    };
  }
  /* Like the browser: an exit that stops before its end says so a moment later. */
  cutShort() {
    if (!this.dropping) return;
    this.dropping = false;
    Promise.resolve().then(() => fire(this, 'animationcancel', { bubbles: true, animationName: 'player-drop' }));
  }
  /* The exit plays to its end. */
  dropped() {
    this.dropping = false;
    fire(this, 'animationend', { bubbles: true, animationName: 'player-drop' });
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  hasAttribute(n) { return n in this.attrs; }
  toggleAttribute(n, on) { if (on) this.attrs[n] = ''; else delete this.attrs[n]; }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = v; }
  set type(v) { this.attrs.type = v; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) { this.attrs.hidden = ''; this.cutShort(); } else delete this.attrs.hidden; }
  matches(sel) {
    /* Like Chrome: any key pressed after the mouse makes the focus visible. */
    if (sel === ':focus-visible') return doc.activeElement === this && doc.lastInput === 'key';
    return sel.split(',').some((one) => {
      const re = /([a-z][a-z0-9]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/iy;
      const s = one.trim();
      let m;
      let pos = 0;
      while (pos < s.length && (m = re.exec(s)) !== null) {
        pos = re.lastIndex;
        if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
        if (m[2] && this.id !== m[2]) return false;
        if (m[3] && !this.classList.contains(m[3])) return false;
        if (m[4] && !(m[4] in this.attrs)) return false;
        if (m[4] && m[5] !== undefined && this.attrs[m[4]] !== m[5]) return false;
      }
      if (pos !== s.length) throw new Error('unsupported selector ' + sel);
      return true;
    });
  }
  closest(sel) { let n = this; while (n instanceof Element) { if (n.matches(sel)) return n; n = n.parentNode; } return null; }
  focus() { doc.activeElement = this; fire(this, 'focusin', { bubbles: true }); }
  blur() { if (doc.activeElement === this) doc.activeElement = doc.body; }
  get offsetWidth() { return 200; }
  getClientRects() { return this.closest('[hidden]') ? [] : [{}]; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 44 }; }
  setPointerCapture() {}
}

class Video extends Element {
  constructor(tag, attrs) {
    super(tag, attrs);
    this.paused = true; this.ended = false; this.seeking = false;
    this.currentTime = 0; this.readyState = 0; this.duration = NaN;
    this.videoWidth = 0; this.videoHeight = 0;
    this.muted = false; this.volume = 1;
    this.buffered = { length: 0 };
    this.plays = 0;
    this.frameCallbacks = [];
  }
  requestVideoFrameCallback(fn) { this.frameCallbacks.push(fn); }
  play() {
    this.plays++;
    this.paused = false;
    fire(this, 'play');
    return Promise.resolve();
  }
  pause() { if (!this.paused) { this.paused = true; fire(this, 'pause'); } }
}

class Canvas extends Element {
  constructor(tag, attrs) {
    super(tag, attrs);
    this.width = 300; this.height = 150;
    this.drawn = [];
    const c = this;
    this.ctx = { drawImage(src) { c.drawn.push({ src: src, w: c.width, h: c.height }); } };
  }
  getContext() { return this.ctx; }
}

class Dialog extends Element {
  get open() { return 'open' in this.attrs; }
  showModal() { this.before = doc.activeElement; this.attrs.open = ''; this.querySelector('button').focus(); }
  get offsetWidth() { return 372; }
  /* Like the browser: the close event comes after what closed it. */
  close(value) {
    delete this.attrs.open;
    doc.activeElement = this.before || doc.body;
    if (value !== undefined) this.returnValue = value;
    Promise.resolve().then(() => fire(this, 'close'));
  }
}

function make(tag, attrs) {
  if (tag === 'dialog') return new Dialog(tag, attrs);
  if (tag === 'video') return new Video(tag, attrs);
  if (tag === 'canvas') return new Canvas(tag, attrs);
  return new Element(tag, attrs);
}

function parse(html, into) {
  const tagRe = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>/g;
  const attrRe = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  const stack = [into];
  let last = 0;
  let m;
  while ((m = tagRe.exec(html)) !== null) {
    const text = html.slice(last, m.index);
    if (text) stack[stack.length - 1].appendChild(new Text(decode(text)));
    last = tagRe.lastIndex;
    if (!m[2]) continue;
    const tag = m[2].toLowerCase();
    if (m[1]) {
      while (stack.length > 1 && stack.pop().tagName !== tag.toUpperCase()) { /* close up to it */ }
      continue;
    }
    const attrs = {};
    let a;
    while ((a = attrRe.exec(m[3])) !== null) {
      const v = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
      attrs[a[1]] = decode(v);
    }
    const el = make(tag, attrs);
    stack[stack.length - 1].appendChild(el);
    if (!VOID[tag]) stack.push(el);
  }
}

function fire(target, type, extra) {
  const ev = Object.assign({ type: type, bubbles: false, isTrusted: true, preventDefault() {}, stopPropagation() { this.stopped = true; } }, extra || {});
  target.dispatchEvent(ev);
  return ev;
}

function click(el) { fire(el, 'click', { bubbles: true, button: 0 }); }

/* A press of the mouse or a finger: what it lands on takes the focus. */
function press(el, pointerType, count) {
  doc.lastInput = 'pointer';
  fire(el, 'pointerdown', { bubbles: true, button: 0, pointerType: pointerType || 'mouse', pointerId: 1, clientX: 0 });
  fire(el, 'mousedown', { bubbles: true, button: 0, detail: count || 1 });
  if (/^(BUTTON|A|INPUT)$/.test(el.tagName)) el.focus();
  else doc.activeElement = doc.body;
  fire(el, 'click', { bubbles: true, button: 0, detail: count || 1 });
}

function doubleClick(el) {
  press(el, 'mouse', 1);
  press(el, 'mouse', 2);
  fire(el, 'dblclick', { bubbles: true });
}

/* A key, sent to what has the focus; Space and Enter press a button, unless the page says no.
   A key held down `repeat`s; Space presses a button once, as it is let go. */
function key(k, repeat, modifiers) {
  doc.lastInput = 'key';
  const target = doc.activeElement;
  let prevented = false;
  fire(target, 'keydown', { bubbles: true, key: k, repeat: !!repeat, preventDefault() { prevented = true; }, ...modifiers });
  const presses = k === 'Enter' || (k === ' ' && !repeat);
  if (presses && !prevented && target.tagName === 'BUTTON' && doc.activeElement === target) click(target);
}

/* Focus moved by Tab. */
function tabTo(el, modifiers) { key('Tab', false, modifiers); el.focus(); }

/* What the pill under the spinner says. */
function noticeOf(p) { return gone(p.q('[data-lost]')) ? null : p.q('[data-lost]').textContent; }

/* Time passes in steps, each request answered as it would be. */
async function pass(p, ms) {
  for (let i = 0; i < ms; i += 500) { p.tick(500); await settle(); }
}

/* ---- the page ------------------------------------------------------------ */

/* *came*: how the page was opened: { referrer, length (of the history), state }. */
function boot(html, reduced, saved, serverPlays, came, elsewhere) {
  doc = new Node();
  doc.documentElement = make('html');
  doc.appendChild(doc.documentElement);
  doc.body = make('body');
  doc.documentElement.appendChild(doc.body);
  parse(html, doc.body);
  doc.activeElement = doc.body;
  doc.visibilityState = 'visible';
  doc.referrer = (came && came.referrer) || '';
  doc.elementFromPoint = () => doc.under || null;
  doc.getElementById = (id) => doc.querySelector('#' + id);
  doc.createElement = (tag) => make(tag.toLowerCase());
  doc.createElementNS = (ns, tag) => make(tag.toLowerCase());
  doc.createTextNode = (t) => new Text(t);
  doc.createDocumentFragment = () => { const f = new Node(); f.isFragment = true; return f; };

  /* Timers run only when the scenario moves the clock. */
  const clock = { now: 0, timers: [], seq: 0 };
  function addTimer(fn, ms, every) {
    const t = { id: ++clock.seq, fn: fn, at: clock.now + (ms || 0), every: every ? Math.max(ms, 1) : 0 };
    clock.timers.push(t);
    return t.id;
  }
  function clearTimer(id) { clock.timers = clock.timers.filter((t) => t.id !== id); }
  function tick(ms) {
    const end = clock.now + ms;
    for (;;) {
      const due = clock.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      clock.now = Math.max(clock.now, due.at);
      if (due.every) due.at += due.every; else clearTimer(due.id);
      due.fn();
    }
    clock.now = end;
  }
  /* Time passes while no timer runs. */
  function sleep(ms) { clock.now += ms; }

  const calls = [];
  const starts = () => calls.filter((c) => /\/api\/watch\/stream$/.test(c.url)).map((c) => c.body.quality);
  /* The page starts its first stream as soon as it loads; a scenario may still change the reply. */
  const replies = [{ ok: { stream_id: 's1', playlist: '/p1', quality: 'best' } }];
  /* `stop` set to [] leaves stops unanswered; `answer` answers the starts no reply is set for;
     `light` is what becomes of light requests: 'answered', 'gateway' by a proxy, or 'silent';
     `lights` is what was asked with. */
  const held = { stop: null, answer: null, light: 'answered', lights: [] };
  const api = {
    post(url, body, opts) {
      const call = { url: url, body: body };
      calls.push(call);
      if (held.stopFails && /\/stop$/.test(url)) return Promise.reject({ status: 0 });
      if (held.stop && /\/stop$/.test(url)) return new Promise((r) => held.stop.push(r));
      if (held.save && /\/progress$/.test(url)) return new Promise((r) => held.save.push(r));
      if (/\/ratings\/ask\//.test(url)) {
        if (Array.isArray(held.rate)) return new Promise((r) => held.rate.push(r));
        if (held.rate === 'silent') return new Promise(() => {});
        return held.rate === 'fails' ? Promise.reject({ status: 500 }) : Promise.resolve(held.rate || { ask: false });
      }
      if (held.progressFails && /\/progress$/.test(url)) return Promise.reject(held.progressFails);
      if (held.keep && /\/keep$/.test(url)) return Promise.resolve(held.keep);
      if (elsewhere && /\/api\/(tvs|cast)\//.test(url)) return elsewhere.post(url, body, opts);
      if (!/\/api\/watch\/stream$/.test(url)) return Promise.resolve({});
      call.open = true;
      return new Promise((resolve, reject) => {
        const settled = (how) => (value) => { call.open = false; how(value); };
        if (opts && opts.signal) {
          opts.signal.addEventListener('abort', () => {
            call.aborted = call.open;
            settled(reject)(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          });
        }
        Promise.resolve().then(() => {
          const reply = replies.shift() || (held.answer && held.answer(body));
          if (!reply) return new Promise(() => {});
          return reply.ok ? reply.ok : Promise.reject(reply.err);
        }).then(settled(resolve), settled(reject));
      });
    },
    get: (url) => (elsewhere && /\/api\/tvs/.test(url)) ? elsewhere.get(url) : Promise.resolve({ ratings: { 'movie-501': { level: null, rateable: !held.unrateable }, 'show-77': { level: null, rateable: true } } }),
    put(url, body) {
      calls.push({ url: url, body: body });
      return held.put === 'silent' ? new Promise(() => {}) : Promise.resolve({ media_type: 'movie', tmdb_id: 5 });
    },
    delete(url) {
      calls.push({ url: url, gone: true });
      return held.put === 'silent' ? new Promise(() => {}) : Promise.resolve({ media_type: 'movie', tmdb_id: 5 });
    },
    messageOf(err, fallback) { return fallback; },
  };

  /* The other pages: what each address answers, or 'hangs'. */
  const pages = {};
  function fetchLight(opts) {
    held.lights.push(opts);
    if (held.light !== 'silent') return Promise.resolve({ status: held.light === 'gateway' ? 504 : 200 });
    return new Promise((r, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
  }
  function fetchPage(url, opts) {
    if (url === '/healthz') return fetchLight(opts);
    if (/\/api\/watch\/ratings\//.test(url)) {
      held.kept = { url: url, method: opts.method, body: opts.body, keepalive: opts.keepalive };
      return Promise.resolve({});
    }
    calls.push({ url: url, page: true });
    const answer = pages[url] || { status: 404, html: '' };
    if (answer === 'offline') return Promise.reject(new TypeError('Failed to fetch'));
    if (answer === 'hangs') {
      return new Promise((r, reject) => opts.signal.addEventListener('abort', () => reject(new Error('aborted'))));
    }
    return Promise.resolve({
      ok: !answer.status, status: answer.status || 200, redirected: !!answer.redirected,
      text: () => Promise.resolve(answer.html),
    });
  }
  class DOMParser {
    parseFromString(html) {
      const page = new Node();
      parse(html, page);
      page.getElementById = (id) => page.querySelector('#' + id);
      const title = page.querySelector('title');
      page.title = title ? title.textContent : '';
      return page;
    }
  }

  /* Frame previews: the scenario answers each request. */
  const frames = { asked: [], answer: null, made: 0, freed: 0 };
  function fetchFrame(url) {
    frames.asked.push(+url.split('/').pop());
    return new Promise((resolve) => {
      frames.answer = (status) => {
        frames.answer = null;
        resolve({ ok: status === 200, status: status, blob: () => Promise.resolve({ url: url }) });
      };
    });
  }

  const hlsList = [];
  class Hls {
    constructor(cfg) { this.config = cfg; this.startsAt = null; this.loads = []; this.stopped = false; this.handlers = {}; this.destroyed = false; hlsList.push(this); }
    static isSupported() { return true; }
    on(name, fn) { this.handlers[name] = fn; }
    emit(name, data) {
      /* Like hls.js: loading starts at the configured place, once the levels are known. */
      if (name === 'hlsManifestParsed') this.startsAt = this.config.startPosition;
      if (this.handlers[name]) this.handlers[name](name, data || {});
    }
    loadSource(src) { this.src = src; }
    attachMedia(v) { this.media = v; }
    stopLoad() { this.stopped = true; }
    startLoad(t) { this.stopped = false; this.loads.push(t); if (this.startsAt !== null) this.startsAt = t; }
    /* Like hls.js: the media is let go of and taken again, empty and paused, with nothing
       said of its place. */
    recoverMediaError() {
      const v = this.media;
      v.readyState = 0; v.videoWidth = 0; v.videoHeight = 0; v.currentTime = 0;
      if (!v.paused) { v.paused = true; fire(v, 'pause'); }
      fire(v, 'emptied');
      fire(v, 'timeupdate');
    }
    /* Like hls.js: detaching the media leaves an empty, paused video. */
    destroy() {
      this.destroyed = true;
      const v = this.media;
      if (!v) return;
      v.readyState = 0; v.videoWidth = 0; v.videoHeight = 0;
      if (!v.paused) { v.paused = true; fire(v, 'pause'); }
      fire(v, 'emptied');
    }
  }
  Hls.Events = { ERROR: 'hlsError', MANIFEST_PARSED: 'hlsManifestParsed', FRAG_LOADING: 'hlsFragLoading', FRAG_LOADED: 'hlsFragLoaded' };
  Hls.ErrorDetails = { FRAG_LOAD_TIMEOUT: 'fragLoadTimeOut' };
  Hls.ErrorTypes = { MEDIA_ERROR: 'mediaError', NETWORK_ERROR: 'networkError' };

  const toasts = [];
  const beacons = [];
  const storage = Object.assign({}, saved);
  const session = { 'mediaman.watch.sitting': storage['mediaman.watch.sitting'] };
  delete storage['mediaman.watch.sitting'];
  const storeOf = (held) => ({
    getItem: (k) => (k in held ? held[k] : null),
    setItem: (k, v) => { held[k] = String(v); },
    removeItem: (k) => { delete held[k]; },
  });
  const win = {
    document: doc,
    serverPlays: serverPlays,
    Hls: Hls,
    MM: { api: api },
    CustomEvent: function CustomEvent(type, init) { Object.assign(this, { type: type }, init); },
    UIFeedback: {
      error(text) { toasts.push({ kind: 'error', text: text }); },
      toast(text, opts) { toasts.push({ kind: (opts && opts.variant) || 'toast', text: text, action: opts && opts.action }); },
    },
    localStorage: storeOf(storage),
    sessionStorage: storeOf(session),
    Date: Object.assign(function (ms) { return new Date(ms); }, { now: () => clock.now }),
    AbortController: AbortController,
    TextDecoder: TextDecoder,
    crypto: require('crypto').webcrypto,
    DOMParser: DOMParser,
    history: {
      urls: [], backs: 0, length: came ? came.length : 1, state: (came && came.state) || null,
      replaceState(state, title, url) { this.state = state; if (url !== '/watch/play/1') this.urls.push(url); },
      back() { this.backs++; },
    },
    MediaMetadata: function MediaMetadata(init) { Object.assign(this, init); },
    location: { href: '/watch/play/1', origin: 'http://mm', host: 'mm' },
    navigator: { mediaSession: {}, sendBeacon(url, blob) { beacons.push({ url: url, body: blob && JSON.parse(blob.parts[0]) }); return true; } },
    matchMedia: (q) => ({ get matches() { return /prefers-reduced-motion:\s*reduce/.test(q) ? !!reduced : /max-height: 599/.test(q) && !!held.tight; } }),
    setTimeout: (fn, ms) => addTimer(fn, ms, false),
    clearTimeout: clearTimer,
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearInterval: clearTimer,
    requestAnimationFrame: (fn) => addTimer(fn, 16, false),
    /* Nothing is laid out here: a scenario calls what watches an element as it grows. */
    sized: [],
    ResizeObserver: function ResizeObserver(fn) { this.observe = (el) => { win.sized.push({ el: el, fn: fn }); }; },
    Blob: function Blob(parts) { this.parts = parts; },
    fetch: (url, opts) => (/\/preview\//.test(url) ? fetchFrame(url) : fetchPage(url, opts)),
    URL: class extends URL {
      static createObjectURL(blob) { frames.made++; return 'blob:' + blob.url; }
      static revokeObjectURL() { frames.freed++; }
    },
    innerWidth: 1000,
    innerHeight: 800,
    console: console,
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    removeEventListener() {},
  };
  win.window = win;
  win.navigator = win.navigator;
  const ctx = vm.createContext(win);
  if (elsewhere) elsewhere.setup(win);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f });
  ['core/dom.js', 'ratings.js'].concat(elsewhere ? elsewhere.scripts : []).forEach(run);
  if (elsewhere && elsewhere.ready) elsewhere.ready(win);
  run('player.js');

  const root = doc.getElementById('player');
  return {
    root: root,
    video: root.querySelector('video'),
    hold: root.querySelector('[data-hold]'),
    hlsList: hlsList,
    replies: replies,
    calls: calls,
    starts: starts,
    frames: frames,
    toasts: toasts,
    beacons: beacons,
    pages: pages,
    held: held,
    sleep: sleep,
    leave() { win.listeners.pagehide.forEach((fn) => fn({})); },
    online() { (win.listeners.online || []).forEach((fn) => fn({})); },
    comeBack() { win.listeners.pageshow.forEach((fn) => fn({ persisted: true })); },
    storage: storage,
    sitting: () => JSON.parse(session['mediaman.watch.sitting']),
    win: win,
    tick: tick,
    q: (sel) => root.querySelector(sel),
  };
}

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

/* The newest stream loads and plays its first frame. */
async function streamPlays(p, pos) {
  await settle();
  const h = p.hlsList[p.hlsList.length - 1];
  h.emit('hlsManifestParsed');
  await settle();
  const v = p.video;
  v.ended = false;
  v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.currentTime = pos || 0;
  fire(v, 'playing');
  present(v);
}

/* A frame of the video reaches the screen. */
function present(v) { v.frameCallbacks.splice(0).forEach((fn) => fn()); }

/* Away for the person: hidden, or only its exit left to play. */
function gone(el) { return el.hidden || el.classList.contains('is-leaving'); }

function playState(p) {
  const btn = p.q('[data-play]');
  const icon = btn.querySelector('.ico');
  return {
    label: btn.getAttribute('aria-label'),
    busy: btn.getAttribute('aria-busy'),
    icon: icon.hasAttribute('hidden') ? null : icon.querySelector('use').getAttribute('href'),
    ring: !btn.querySelector('.player-btn-ring').hidden,
  };
}

/* Every place the video is sent to from now on, as the page sets it. */
function countSeeks(video) {
  const seeks = [];
  let at = video.currentTime;
  Object.defineProperty(video, 'currentTime', { configurable: true, get: () => at, set(t) { at = t; seeks.push(t); } });
  return seeks;
}

function barState(p) {
  return { time: p.q('[data-elapsed]').textContent, now: p.q('[data-seek]').getAttribute('aria-valuenow'), left: p.q('[data-played]').style.width };
}

/* A failed request, as hls.js tells of it, a segment's as no fatal one at first; *body* is what the server answered it with. */
function loadError(details, code, body, headers) {
  const text = body ? JSON.stringify(body) : '';
  const segment = /^frag/.test(details);
  return {
    fatal: !segment, type: 'networkError', details: details, response: { code: code },
    networkDetails: {
      responseType: segment ? 'arraybuffer' : '',
      response: segment ? new TextEncoder().encode(text).buffer : text,
      get responseText() { if (segment) throw new Error('InvalidStateError'); return text; },
      getResponseHeader: (name) => (headers || {})[name.toLowerCase()] || null,
    },
  };
}

/* The start requests, and the name this page gives itself in them. */
function startCalls(p) { return p.calls.filter((c) => /\/api\/watch\/stream$/.test(c.url)); }
function pageOf(p) { return startCalls(p)[0].body.page; }

function messageState(p) {
  if (gone(p.q('[data-message]'))) return null;
  const acts = p.q('[data-message-acts]').querySelectorAll('button').map((b) => b.textContent);
  return Object.assign({ text: p.q('[data-message-text]').textContent, button: p.q('[data-retry]').hidden ? null : p.q('[data-retry]').textContent },
    acts.length ? { acts: acts, line: p.q('[data-message-line]').textContent } : {});
}

/* The connection fails again as often as the page tries, for this long. */
async function failFor(p, ms, fail) {
  for (let i = 0; i < ms; i += 500) {
    p.tick(500);
    fail();
    await settle();
  }
}

/* The video has loaded from its start to *end*. */
function loadedTo(p, end) { p.video.buffered = { length: 1, start: () => 0, end: () => end }; }

/* A segment's request is answered, its body still to come. */
function accept(h) {
  const xhr = { readyState: 2, status: 200, listeners: [], addEventListener(type, fn) { this.listeners.push(fn); } };
  h.config.xhrSetup(xhr, 'http://mm/api/watch/stream/s1/seg/9.ts');
  xhr.listeners.forEach((fn) => fn());
}

/* Every time *loads* grew, the try fails. */
function failingLoads(h, error) {
  let seen = h.loads.length;
  return () => {
    if (h.loads.length === seen) return;
    seen = h.loads.length;
    h.emit('hlsError', error());
  };
}

const NET = { err: { error: 'network_error', status: 0 } };

function holdState(p) {
  return {
    shown: !gone(p.hold),
    drewVideo: !!p.hold && p.hold.drawn.some((d) => d.src === p.video && d.w === 1280 && d.h === 720),
  };
}

/* The video plays on by itself, a second at a time. */
function playOn(p, seconds) {
  for (let i = 0; i < seconds; i++) {
    p.video.currentTime += 1;
    fire(p.video, 'timeupdate');
  }
}

const ROWS = ['1080p-10000', '1080p-8000', '720p-4000', '480p-1500'].map((id) => {
  const [height, kbps] = id.split('p-').map(Number);
  return { id: id, kind: 'convert', label: height + 'p', detail: kbps / 1000 + ' Mbps', height: height, bitrate_kbps: kbps };
});
const AUTO = { id: 'auto', kind: 'auto', label: 'Automatic', detail: '', height: null, bitrate_kbps: null };
const ORIGINAL = { id: 'original', kind: 'original', label: 'Original (1080p)', detail: '11 Mbps', height: 1080, bitrate_kbps: 11000 };
const FULL = [AUTO, ORIGINAL].concat(ROWS);

let streams = 0;
function answer(quality, extra) {
  streams++;
  return { ok: Object.assign({ stream_id: 'a' + streams, playlist: '/a' + streams, quality: quality, qualities: FULL }, extra) };
}

function menuState(p) {
  const items = p.q('[data-quality-menu]').querySelectorAll('[data-quality]');
  return {
    ids: items.map((b) => b.getAttribute('data-quality')),
    ticked: items.filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.getAttribute('data-quality')),
    pill: p.q('[data-quality-btn]').textContent.trim(),
    auto: items.filter((b) => b.getAttribute('data-quality') === 'auto').map((b) => b.querySelector('.player-menu-side').textContent)[0],
    saved: p.storage['mediaman.watch.quality'] || null,
  };
}

const CHAPTERS = [['Cold open', 0], ['Arrival', 300], ['<b>Chase</b>', 312], ['Home', 1500], ['End', 2990]].map((c) => ({ title: c[0], start_s: c[1], end_s: c[1] + 10 }));
const MARKERS = [
  { kind: 'intro', start_s: 60, end_s: 150, final: false },
  { kind: 'credits', start_s: 2400, end_s: 2450, final: false },
  { kind: 'credits', start_s: 2700, end_s: 3000, final: true },
];
const pick = (p, cls, prop) => p.q('[data-marks]').querySelectorAll('.' + cls).map((m) => m.style[prop]);

/* The pointer over the seek bar, 1000 px wide: one pixel is three seconds of the episode. */
function point(p, type, x, pointerType) {
  fire(p.q('[data-seek]'), type, { bubbles: true, button: 0, pointerId: 1, pointerType: pointerType || 'mouse', clientX: x });
}

const WITH_FRAMES = { previews: { interval_s: 5, count: 600 } };

/* The video plays to its end. */
function endsNow(p) {
  p.video.paused = true; p.video.ended = true; p.video.currentTime = 2997;
  fire(p.video, 'ended');
}

async function bubbleState(p) {
  await settle();
  const img = p.q('[data-frame-img]');
  return {
    shown: !gone(p.q('[data-bubble]')),
    time: p.q('[data-bubble-time]').textContent,
    frame: p.q('[data-frame]').hidden || img.hidden ? null : +String(img.src).split('/').pop(),
    stale: p.q('[data-frame]').classList.contains('is-stale'),
  };
}

/* The controls and the preview while the mouse rests: paused, playing, and moved again. */
async function restedOnBar(p, move) {
  const look = async () => ({ idle: p.root.classList.contains('is-idle'), bubble: (await bubbleState(p)).shown });
  const out = {};
  p.video.paused = true;
  move();
  p.tick(5000);
  out.paused = await look();
  p.video.paused = false;
  move();
  p.tick(2900);
  out.atFirst = await look();
  p.tick(200);
  out.later = await look();
  move();
  out.moved = await look();
  return out;
}

/* ---- elsewhere ----------------------------------------------------------- */

const LIVING = { id: 5, kind: 'page', name: 'Living room', on: true, last_seen_at: null, play: null, last: null };
const BEDROOM = { id: 6, kind: 'page', name: 'Bedroom TV', on: false, last_seen_at: null, play: null, last: null };
const KITCHEN = { id: 7, kind: 'older', name: 'Kitchen TV', on: true, busy: true, last_seen_at: null, play: null, last: null };
const SUBS = [{ id: '31', label: 'English', selected: true }, { id: '32', label: 'Polski', selected: false }];

/* The TVs, streams for TVs, AirPlay and a way such as Google Cast, as *mode* has them. */
function elsewhereOf(mode) {
  const e = {
    scripts: mode === 'nosheet' ? [] : ['watch/play-on.js', 'watch/airplay.js'],
    tvs: [LIVING, BEDROOM, KITCHEN],
    tv: null,                  // what one TV answers
    pairs: [],                 // answers to codes
    plays: [],                 // answers to starts on a TV; taken when there are none left
    sent: [],
    casts: 0,
    castFails: [],
    wireless: false,
    picked: 0,
    refuse: (status, error, data) => Promise.reject({ status: status, error: error, data: data || null }),
    gets: [],                  // answers to looks for TVs; the TVs when there are none left
    get(url) {
      e.sent.push({ get: url });
      if (/tvs$/.test(url) && e.gets.length) return e.gets.shift()();
      if (/tvs$/.test(url)) return mode === 'absent' || mode === 'airOnly' ? e.refuse(404, 'http_404') : mode === 'offline' ? e.refuse(0, 'network_error') : Promise.resolve({ tvs: e.tvs });
      return e.tv ? e.tv() : Promise.resolve({ tv: Object.assign({}, LIVING, { play: { rating_key: '501', state: 'playing', position_s: 40 } }) });
    },
    post(url, body, opts) {
      e.sent.push({ post: url, body: body });
      if (/tvs\/pair$/.test(url)) return e.pairs.shift()();
      if (/cast\/stream$/.test(url)) {
        if (e.castFails.length) return e.castFails.shift()(opts);
        e.casts++;
        return Promise.resolve({ stream_id: 'c' + e.casts, url: '/api/cast/T' + e.casts + '/index.m3u8', duration_s: 3000, quality: body.quality, subtitle: body.subtitle === 'off' ? null : body.subtitle || '31', subtitle_tracks: SUBS });
      }
      if (/\/play$/.test(url)) return e.plays.length ? e.plays.shift()(opts) : Promise.resolve({ tv: Object.assign({}, LIVING, { play: { rating_key: body.rating_key, state: 'starting' } }) });
      if (/\/commands$/.test(url)) return Promise.resolve({ tv: Object.assign({}, LIVING, { last: { reason: 'stopped', rating_key: '501', position_s: 77 } }) });
      return Promise.resolve({});
    },
    setup(win) {
      e.win = win;
      Video.prototype.load = function () {};
      delete Video.prototype.webkitShowPlaybackTargetPicker;
      if (mode === 'plain' || mode === 'absent') return;
      win.WebKitPlaybackTargetAvailabilityEvent = function () {};
      Video.prototype.webkitShowPlaybackTargetPicker = function () { e.picked++; };
      Object.defineProperty(Video.prototype, 'webkitCurrentPlaybackTargetIsWireless', { configurable: true, get() { return e.wireless && this.classList.contains('player-air'); } });
    },
    starts: () => e.sent.filter((c) => /cast\/stream$/.test(c.post || '')).map((c) => c.body),
    air: () => doc.querySelector('.player-air'),
    nearby() { fire(e.air(), 'webkitplaybacktargetavailabilitychanged', { availability: 'available' }); },
    onTv(on) { e.wireless = on; fire(e.air(), 'webkitcurrentplaybacktargetiswirelesschanged'); },
  };
  return e;
}

/* A way such as Google Cast; what it plays is followed as a video is. */
function castWay(carriesOn) {
  const way = {
    id: 'cast', label: 'Chromecast', sub: '', icon: 'play-on-cast', available: () => true,
    loads: [], stops: 0, left: 0, kept: null, what: null, gone: null, media: make('video'), counts: [], watched: (key) => way.seen.push(key), seen: [],
  };
  way.play = {
    name: 'Den', carriesOn: carriesOn, media: way.media,
    load(stream) { way.loads.push(stream); way.media.readyState = 1; return Promise.resolve(); },
    stop() { way.stops++; return Promise.resolve(); },
    leave() { way.left++; },
    keep(kept) { way.kept = kept; },
    counts(until) { way.counts.push(until); },
    onGone(fn) { way.gone = fn; },
  };
  way.start = (what) => { way.what = what; return what.open().then((stream) => { way.stream = stream; return way.play; }); };
  return way;
}

/* The newest: one that was closed may still sink away. */
const sheet = () => doc.querySelectorAll('.play-on').pop();
/* A choice of the message's own, by what it says. */
const act = (p, name) => p.q('[data-message-acts]').querySelectorAll('button').filter((b) => b.textContent === name)[0];
const rowOf = (key) => sheet().querySelector('[data-key="' + key + '"]');
const said = () => { const s = sheet() && sheet().querySelector('.play-on__say'); return s ? s.textContent : null; };

async function openSheet(p) {
  await settle();
  click(p.q('.player-controls').querySelector('[data-play-on]'));
  await settle();
}

async function pickRow(p, key) { click(rowOf(key)); await settle(); }

function awayState(p) {
  return {
    away: p.root.classList.contains('is-away'),
    says: p.q('[data-away]').hidden ? null : p.q('[data-away-on]').textContent,
    hint: p.q('[data-away-hint]').textContent,
    message: messageState(p),
  };
}

/* What plays elsewhere plays, from *pos*. */
function mediaPlays(media, pos) {
  media.readyState = 4; media.currentTime = pos;
  fire(media, 'playing');
}

/* AirPlay is picked, its stream loads and plays. */
async function airPlays(p, pos) {
  const air = p.elsewhere.air();
  p.elsewhere.nearby();
  await openSheet(p);
  await pickRow(p, 'airplay');
  p.elsewhere.onTv(true);
  await settle();
  air.readyState = 1;
  fire(air, 'loadedmetadata');
  await settle();
  const at = air.currentTime;
  mediaPlays(air, pos === undefined ? at : pos);
  await settle();
  return { air: air, at: at };
}

async function castPlays(p, carriesOn) {
  const way = castWay(carriesOn);
  p.win.MM.playOn.register(way);
  await openSheet(p);
  await pickRow(p, 'cast');
  mediaPlays(way.media, way.stream.position_s);
  await settle();
  return way;
}

/* ---- scenarios ----------------------------------------------------------- */

const scenarios = {
  /* Switch quality while playing; the new stream starts and plays. */
  async quality_switch(p) {
    await streamPlays(p, 30);
    p.replies.push({ ok: { stream_id: 's2', playlist: '/p2' } });
    click(p.q('[data-quality="medium"]'));
    await settle();
    const whileLoading = { hold: holdState(p), play: playState(p), loading: p.root.classList.contains('is-loading'), held: p.root.classList.contains('is-held') };
    p.hlsList[p.hlsList.length - 1].emit('hlsManifestParsed');
    await settle();
    const afterManifest = { hold: holdState(p), held: p.root.classList.contains('is-held') };
    const v = p.video;
    v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720;
    fire(v, 'playing');
    present(v);
    return { whileLoading: whileLoading, afterManifest: afterManifest, afterPlaying: { hold: holdState(p), play: playState(p), held: p.root.classList.contains('is-held') } };
  },

  /* The new stream says it plays, while it still seeks to its place, before any frame of it is
     on screen. By `serverPlays` the browser tells of frames, does not, or never shows one. */
  async hold_released(p) {
    const v = p.video;
    const held = () => holdState(p).shown;
    if (p.win.serverPlays === 'untold') v.requestVideoFrameCallback = undefined;
    await streamPlays(p, 30);
    p.tick(100);
    p.replies.push(answer('720p-4000'));
    click(p.q('[data-quality="medium"]'));
    await settle();
    p.hlsList[1].emit('hlsManifestParsed');
    v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720;
    v.seeking = true;
    fire(v, 'playing');
    present(v);
    p.tick(100);
    const out = { seeking: held() };
    v.seeking = false;
    fire(v, 'seeked');
    out.seeked = held();
    if (p.win.serverPlays === 'told') present(v);
    else p.tick(p.win.serverPlays === 'untold' ? 40 : 1000);
    out.seen = held();
    out.fading = p.hold.classList.contains('is-leaving');
    p.hold.dropped();
    out.after = p.hold.hidden;
    /* Another quality before the frame of this one is seen: what was held stays. */
    p.replies.push(answer('480p-1500'));
    click(p.q('[data-quality="low"], [data-quality="480p-1500"]'));
    await settle();
    fire(v, 'playing');
    p.replies.push(answer('720p-4000'));
    click(p.q('[data-quality="720p-4000"]'));
    present(v);
    p.tick(1500);
    out.again = held();
    return out;
  },

  /* The menu follows each answer, whatever the saved choice is. */
  async quality_menu(p) {
    p.replies[0] = answer(p.win.serverPlays);
    await streamPlays(p, 30);
    return Object.assign(menuState(p), { starts: p.starts(), errors: p.toasts.length });
  },

  /* A saved choice the server has never heard of. */
  async quality_unknown(p) {
    p.replies[0] = { err: { status: 422 } };
    p.replies.push(answer('1080p-8000'));
    await streamPlays(p, 30);
    return Object.assign(menuState(p), { starts: p.starts(), message: !gone(p.q('[data-message]')) });
  },

  /* The choice that Automatic already plays is made the person's. */
  async quality_same(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    click(p.q('[data-quality="1080p-8000"]'));
    await settle();
    return Object.assign(menuState(p), { starts: p.starts() });
  },

  /* Plex repackages the source, but this browser cannot play it. */
  async original_unplayable(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    p.replies.push(answer('original'), answer('1080p-10000'));
    click(p.q('[data-quality="original"]'));
    await settle();
    p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'mediaError' });
    await streamPlays(p, 30);
    return Object.assign(menuState(p), { starts: p.starts(), toasts: p.toasts });
  },

  /* Automatic on a connection that cannot keep up, then one that can. */
  async auto_steps(p) {
    const out = {};
    const last = () => p.hlsList[p.hlsList.length - 1];
    const stall = () => fire(p.video, 'waiting');
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    playOn(p, 10);
    key('ArrowRight');
    fire(p.video, 'seeking');
    stall();
    playOn(p, 5);
    fire(p.video, 'seeking');
    stall();
    out.afterSeeks = p.starts();
    fire(p.video, 'playing');
    playOn(p, 5);
    stall();
    out.afterOne = p.starts();
    fire(p.video, 'playing');
    playOn(p, 100);
    stall();
    out.afterFarApart = p.starts();
    fire(p.video, 'playing');
    playOn(p, 20);
    p.replies.push(answer('720p-4000'));
    stall();
    await streamPlays(p, 200);
    out.afterTwo = Object.assign(menuState(p), { starts: p.starts() });
    playOn(p, 5);
    p.replies.push(answer('480p-1500'));
    stall();
    p.tick(9000);
    out.shortWait = p.starts();
    p.tick(2000);
    await streamPlays(p, 200);
    out.longWait = p.starts();
    playOn(p, 3);
    stall();
    fire(p.video, 'playing');
    playOn(p, 3);
    stall();
    await settle();
    out.atTheBottom = p.starts();
    fire(p.video, 'playing');
    playOn(p, 280);
    out.notYet = p.starts();
    p.replies.push(answer('720p-4000'));
    last().bandwidthEstimate = 3e6;
    playOn(p, 30);
    out.slowLine = p.starts();
    last().bandwidthEstimate = 50e6;
    playOn(p, 1);
    await streamPlays(p, 600);
    out.up = p.starts();
    playOn(p, 5);
    p.replies.push(answer('480p-1500'));
    last().bandwidthEstimate = 0;
    stall();
    fire(p.video, 'playing');
    playOn(p, 5);
    stall();
    await streamPlays(p, 600);
    out.downAgain = p.starts();
    playOn(p, 310);
    out.damped = p.starts();
    p.replies.push(answer('720p-4000'));
    playOn(p, 300);
    out.upAgain = p.starts();
    return out;
  },

  /* A connection measured far slower than the next step. */
  async auto_jumps(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    p.hlsList[0].bandwidthEstimate = 2e6;
    p.replies.push(answer('480p-1500'));
    for (let i = 0; i < 2; i++) { playOn(p, 5); fire(p.video, 'waiting'); fire(p.video, 'playing'); }
    await settle();
    return p.starts();
  },

  /* The line is too slow for the first picture to come. */
  async auto_never_starts(p) {
    const slow = () => p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: false, details: 'fragLoadTimeOut' });
    p.replies[0] = answer(p.win.serverPlays);
    p.replies.push(answer('720p-4000'));
    await settle();
    /* What hls.js says before it has measured anything. */
    p.hlsList[0].bandwidthEstimate = 5e5;
    slow();
    await settle();
    const once = p.starts();
    slow();
    await settle();
    return { once: once, twice: p.starts() };
  },

  /* Every stream starts and none of them can be loaded. */
  async never_loads(p) {
    p.replies.length = 0;
    p.held.answer = (body) => {
      if (p.win.serverPlays === 'refused' && body.quality !== 'auto') return { err: { status: 503 } };
      return answer(body.quality === 'auto' ? '1080p-8000' : body.quality);
    };
    while (p.starts().length < 40 && gone(p.q('[data-message]'))) {
      await settle();
      const h = p.hlsList[p.hlsList.length - 1];
      if (h.destroyed) continue;
      h.emit('hlsManifestParsed');
      const error = { type: 'networkError', details: 'fragLoadTimeOut' };
      h.emit('hlsError', Object.assign({ fatal: false }, error));
      h.emit('hlsError', Object.assign({ fatal: false }, error));
      h.emit('hlsError', Object.assign({ fatal: true }, error));
      await settle();
    }
    return { starts: p.starts(), message: !gone(p.q('[data-message]')), retry: !p.q('[data-retry]').hidden };
  },

  /* Automatic plays a little and is slow; each step down from it fails at once. */
  async step_fails(p) {
    p.replies.length = 0;
    p.held.answer = (body) => answer(body.quality === 'auto' ? '1080p-8000' : body.quality);
    const error = { type: 'networkError', details: 'fragLoadTimeOut' };
    await streamPlays(p, 0);
    playOn(p, 100);
    while (p.starts().length < 40 && gone(p.q('[data-message]'))) {
      if (p.starts().pop() === 'auto') {
        await streamPlays(p, 100);
        playOn(p, 3);
        for (let i = 0; i < 2; i++) p.hlsList[p.hlsList.length - 1].emit('hlsError', Object.assign({ fatal: false }, error));
      } else {
        p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError', details: 'fragLoadError' });
      }
      await settle();
    }
    return { starts: p.starts(), message: !gone(p.q('[data-message]')) };
  },

  /* The original was left, then the person's own pick is refused. */
  async original_left(p) {
    p.replies[0] = answer('original');
    await streamPlays(p, 0);
    playOn(p, 5);
    for (let i = 0; i < 2; i++) {
      p.replies.push(answer(i ? '1080p-10000' : 'original'));
      p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
      await settle();
    }
    p.replies.push({ err: { status: 503 } }, answer('1080p-10000'));
    click(p.q('[data-quality="720p-4000"]'));
    await streamPlays(p, 0);
    return Object.assign(menuState(p), { starts: p.starts(), toasts: p.toasts });
  },

  /* An original that played is started anew, and that stream fails before its first frame. */
  async original_restarts(p) {
    const last = () => p.hlsList[p.hlsList.length - 1];
    p.replies[0] = answer('original');
    await streamPlays(p, 0);
    playOn(p, 5);
    p.held.answer = (body) => answer(body.quality);
    last().emit('hlsError', { response: { code: 410 } });
    await settle();
    last().emit('hlsError', { fatal: true, type: p.win.serverPlays });
    await settle();
    const once = p.starts();
    last().emit('hlsError', { fatal: true, type: p.win.serverPlays });
    await settle();
    return { once: once, twice: p.starts() };
  },

  /* The quality that last played is picked under the message of its failure. */
  async pick_after_failure(p) {
    p.held.answer = () => answer('1080p-8000');
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 0);
    while (gone(p.q('[data-message]'))) {
      p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
      await settle();
    }
    const before = p.starts().length;
    click(p.q('[data-quality="1080p-8000"]'));
    await settle();
    return { started: p.starts().slice(before), message: !gone(p.q('[data-message]')) };
  },

  /* The quality of the menu's tick is picked under the message that it failed. */
  async pick_ticked(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 0);
    while (gone(p.q('[data-message]'))) {
      p.replies.push(answer('1080p-8000'));
      p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
      await settle();
    }
    const before = p.starts().length;
    const ticked = p.q('[data-quality][aria-checked="true"]');
    p.replies.length = 0;
    p.replies.push(answer('1080p-8000'));
    click(ticked);
    await settle();
    const out = { ticked: ticked.getAttribute('data-quality'), started: p.starts().slice(before), message: !gone(p.q('[data-message]')) };
    /* One that no new try can mend stays. */
    p.replies.push({ err: { status: 404, error: 'not_found' } });
    p.hlsList[p.hlsList.length - 1].emit('hlsError', { response: { code: 410 } });
    await settle();
    click(p.q('[data-quality][aria-checked="true"]'));
    await settle();
    out.final = [p.starts().length - before, !gone(p.q('[data-message]'))];
    return out;
  },

  /* Automatic is picked over a saved choice the server does not know. */
  async pick_over_unknown(p) {
    p.replies[0] = { err: { status: 422 } };
    p.replies.push(answer('1080p-8000'));
    await streamPlays(p, 0);
    const before = p.storage['mediaman.watch.quality'];
    click(p.q('[data-quality="auto"]'));
    return { before: before, saved: p.storage['mediaman.watch.quality'], starts: p.starts().length };
  },

  /* What Automatic waits for before a step up, and what it makes of one refused. */
  async up_waits(p) {
    const out = {};
    const count = () => p.starts().length;
    const last = () => p.hlsList[p.hlsList.length - 1];
    const stall = () => { playOn(p, 5); fire(p.video, 'waiting'); };
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 310);
    out.atItsTop = count();
    stall();
    p.tick(5000);
    fire(p.video, 'playing');
    p.tick(20000);
    out.afterAShortStall = count();
    stall();
    p.replies.push(answer('720p-4000'));
    await streamPlays(p, 100);
    out.down = count();
    playOn(p, 280);
    p.video.currentTime = 2900;
    playOn(p, 40);
    out.nearTheEnd = count();
    p.video.currentTime = 500;
    press(p.q('[data-quality-btn]'));
    playOn(p, 10);
    out.menuOpen = count();
    key('Escape');
    p.replies.push({ err: { status: 404, error: 'unreachable' } }, answer('720p-4000'));
    playOn(p, 2);
    await streamPlays(p, 500);
    out.refused = p.starts().slice(-2);
    const from = count();
    playOn(p, 400);
    out.waitsLonger = count() - from;
    p.replies.push(answer('1080p-8000'));
    playOn(p, 300);
    await settle();
    out.again = p.starts().slice(from);
    return out;
  },

  /* Frames: a stream's refusal that comes late, an image that cannot be shown, and
     the wait after a refusal that follows a frame. */
  async frame_trouble(p) {
    const out = {};
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 0);
    point(p, 'pointermove', 100);
    p.tick(100);
    p.replies.push(answer('720p-4000', WITH_FRAMES));
    click(p.q('[data-quality="720p-4000"]'));
    const late = p.frames.answer;
    await streamPlays(p, 0);
    late(404);
    await settle();
    out.newStream = !p.q('[data-frame]').hidden;
    const ask = async (x, status) => {
      point(p, 'pointermove', x);
      p.tick(100);
      if (p.frames.answer) p.frames.answer(status);
      await settle();
    };
    await ask(200, 503);
    p.tick(2000);
    p.frames.answer(200);
    await settle();
    await ask(300, 503);
    const asked = p.frames.asked.length;
    p.tick(2000);
    out.waitedAgain = p.frames.asked.length - asked;
    p.frames.answer(200);
    await settle();
    out.shown = !p.q('[data-frame]').hidden;
    fire(p.q('[data-frame-img]'), 'error');
    out.afterError = !p.q('[data-frame]').hidden;
    return out;
  },

  /* An arrow held down, then the buttons pressed in quick succession. */
  async hold_arrow(p) {
    await streamPlays(p, 100);
    const seeks = countSeeks(p.video);
    const out = {};
    for (let i = 0; i < 5; i++) { key('ArrowRight', i > 0); p.tick(40); }
    /* The video plays on meanwhile. */
    fire(p.video, 'timeupdate');
    out.held = Object.assign(barState(p), { seeks: seeks.slice() });
    p.tick(150);
    out.early = seeks.slice();
    p.tick(100);
    out.landed = seeks.slice();
    p.tick(5000);
    out.later = seeks.length;
    for (const type of ['mouse', 'touch']) {
      p.video.currentTime = 600;
      seeks.length = 0;
      press(p.q('[data-back10]'), type);
      p.tick(100);
      press(p.q('[data-fwd10]'), type);
      p.tick(100);
      press(p.q('[data-fwd10]'), type);
      press(p.q('[data-fwd10]'), type);
      out[type] = { now: barState(p).now };
      p.tick(300);
      out[type].seeks = seeks.slice();
    }
    tabTo(p.q('[data-seek]'));
    for (const k of ['PageUp', 'ArrowUp', 'Home', 'End']) {
      p.video.currentTime = 600;
      seeks.length = 0;
      for (let i = 0; i < 4; i++) { key(k, i > 0); p.tick(40); }
      p.tick(300);
      out[k] = seeks.slice();
    }
    return out;
  },

  /* Presses that lead past either end. */
  async hold_ends(p) {
    await streamPlays(p, 25);
    const seeks = countSeeks(p.video);
    const out = {};
    for (let i = 0; i < 6; i++) key('ArrowLeft', i > 0);
    out.start = barState(p).now;
    p.tick(300);
    key('ArrowRight');
    key('ArrowLeft', true);
    key('ArrowLeft', true);
    key('ArrowRight');
    p.tick(300);
    p.video.currentTime = 2970;
    for (let i = 0; i < 6; i++) key('ArrowRight', i > 0);
    out.end = barState(p).now;
    p.tick(300);
    out.seeks = seeks;
    return out;
  },

  /* The video ends while presses wait to be sent: ones that lead back, or on to the end. */
  async ends_under_press(p) {
    await streamPlays(p, 2995);
    const seeks = countSeeks(p.video);
    key(p.win.serverPlays);
    const keep = p.video.currentTime;
    p.video.paused = true; p.video.ended = true;
    fire(p.video, 'ended');
    const card = !gone(p.q('[data-upnext]'));
    p.tick(300);
    return { card: card, seeks: seeks, from: keep, after: !gone(p.q('[data-upnext]')) };
  },

  /* Presses wait to be sent as the page is left, as a new quality starts, as a marker is skipped. */
  async press_not_lost(p) {
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 100);
    playOn(p, 3);
    const seeks = countSeeks(p.video);
    const out = {};
    key('ArrowRight');
    key('ArrowRight');
    p.replies.push(answer('480p-1500'));
    click(p.q('[data-quality="480p-1500"]'));
    out.saved = p.beacons[p.beacons.length - 1].body.position_s;
    await settle();
    out.startsAt = p.hlsList[p.hlsList.length - 1].config.startPosition;
    await streamPlays(p, 123);
    seeks.length = 0;
    p.tick(1000);
    out.again = seeks.slice();
    p.video.currentTime = MARKERS[0].start_s - 5;
    key('ArrowRight');
    out.offered = p.q('[data-skip-text]').textContent;
    seeks.length = 0;
    key('s');
    p.tick(1000);
    out.skipped = seeks.map((t) => t - MARKERS[0].end_s);
    key('ArrowRight');
    p.leave();
    out.left = p.beacons[p.beacons.length - 1].body.position_s - MARKERS[0].end_s;
    return out;
  },

  /* Presses while the video waits for more of itself, and Space straight after one. */
  async press_while_buffering(p) {
    await streamPlays(p, 100);
    const seeks = countSeeks(p.video);
    fire(p.video, 'waiting');
    key('ArrowRight');
    key('ArrowRight', true);
    const now = barState(p).now;
    p.tick(300);
    const landed = seeks.slice();
    key('ArrowRight');
    key(' ');
    const bySpace = seeks.slice();
    p.tick(1000);
    return { now: now, landed: landed, bySpace: bySpace, all: seeks, paused: p.video.paused };
  },

  /* A seek to a place that has loaded, and to one that has not. */
  async seek_holds(p) {
    await streamPlays(p, 100);
    const held = () => holdState(p).shown;
    p.video.buffered = { length: 1, start: () => 90, end: () => 160 };
    key('ArrowRight');
    p.tick(300);
    const out = { loaded: held() };
    fire(p.video, 'seeked');
    key('ArrowLeft');
    key('ArrowLeft');
    key('ArrowLeft');
    p.tick(300);
    out.notLoaded = held();
    fire(p.video, 'seeked');
    out.seeked = held();
    present(p.video);
    out.seen = held();
    return out;
  },

  /* The server refuses a segment, or the playlist, for now. */
  async busy(p) {
    const details = p.win.serverPlays;
    await streamPlays(p, 100);
    const h = p.hlsList[0];
    h.emit('hlsError', loadError(details, 429, { error: 'busy' }, { 'retry-after': '3' }));
    const stopped = h.stopped;
    p.tick(2900);
    const early = [h.loads.length, p.hlsList.length];
    p.tick(200);
    await settle();
    const last = p.hlsList[p.hlsList.length - 1];
    const loading = { loads: h.loads.slice(), streams: p.hlsList.length, src: last.src, startPosition: last.config.startPosition };
    for (let i = 0; i < 10; i++) {
      p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError(details, 429, null, {}));
      p.tick(5000);
      await settle();
    }
    return { early: early, stopped: stopped, after: loading, starts: p.starts().length, stops: p.beacons.length, message: messageState(p), notice: !gone(p.q('[data-lost]')) };
  },

  /* A short drop in the middle: the segments fail, and then come again. */
  async dropped(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const error = () => loadError('fragLoadError', p.win.serverPlays === 'gateway' ? 502 : 0);
    const notice = () => !gone(p.q('[data-lost]'));
    const out = { role: p.q('[data-lost]').parentNode.getAttribute('role') };
    const at = [];
    h.emit('hlsError', error());
    out.stopped = h.stopped;
    out.whilePlaying = notice();
    playOn(p, 2);
    fire(p.video, 'waiting');
    p.tick(200);
    out.atFirst = notice();
    p.tick(200);
    out.waiting = notice();
    out.text = noticeOf(p);
    p.tick(100);
    const fail = failingLoads(h, error);
    let before = 0;
    for (let i = 1; i < 80; i++) {
      p.tick(500);
      if (h.loads.length !== before) { at.push((i + 1) / 2); before = h.loads.length; }
      fail();
    }
    out.triedAt = at;
    out.whileTrying = { message: messageState(p), destroyed: h.destroyed, notice: notice(), loading: p.root.classList.contains('is-loading') };
    h.emit('hlsFragLoaded');
    out.back = { notice: notice(), message: messageState(p) };
    fire(p.video, 'playing');
    out.starts = p.starts().length;
    p.tick(120000);
    out.later = messageState(p);
    return out;
  },

  /* A drop that lasts, then Try again, then one the browser says is over. */
  async dropped_long(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const error = () => loadError('fragLoadError', 0);
    p.video.webkitPresentationMode = 'fullscreen';
    p.video.webkitSetPresentationMode = (m) => { p.video.webkitPresentationMode = m; };
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    h.emit('hlsError', error());
    const fail = failingLoads(h, error);
    p.held.light = 'gateway';
    const out = {};
    await failFor(p, 55000, fail);
    out.before = messageState(p);
    await failFor(p, 10000, fail);
    out.message = messageState(p);
    out.mode = p.video.webkitPresentationMode;
    out.notice = !gone(p.q('[data-lost]'));
    out.destroyed = h.destroyed;
    let loads = h.loads.length;
    await failFor(p, 60000, fail);
    out.triesUnderMessage = h.loads.length - loads;
    press(p.q('[data-retry]'));
    out.again = { loads: h.loads.length - loads, message: messageState(p) };
    fail();
    p.tick(400);
    out.again.notice = !!noticeOf(p);
    await failFor(p, 65000, fail);
    out.messageAgain = messageState(p);
    loads = h.loads.length;
    p.online();
    out.online = { loads: h.loads.length - loads, message: messageState(p) };
    h.emit('hlsFragLoaded');
    fire(p.video, 'playing');
    out.back = messageState(p);
    out.starts = p.starts().length;
    return out;
  },

  /* A drop that lasts while enough was loaded to play on. */
  async dropped_playing_on(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const error = () => loadError('fragLoadError', 0);
    h.emit('hlsError', error());
    const fail = failingLoads(h, error);
    for (let i = 0; i < 90; i++) { playOn(p, 1); p.tick(1000); fail(); }
    const playing = messageState(p);
    fire(p.video, 'waiting');
    await failFor(p, 50000, fail);
    const waiting = messageState(p);
    await failFor(p, 15000, fail);
    return { playing: playing, waiting: waiting, late: !!messageState(p), starts: p.starts().length };
  },

  /* The person leaves while the picture plays on from what is buffered, the connection gone. */
  async dropped_left(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 0));
    playOn(p, 20);
    p.beacons.length = 0;
    p.leave();
    return p.beacons.map((b) => b.body && b.body.position_s);
  },

  /* The browser comes online between two tries, and while the person has paused. */
  async dropped_online(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    p.video.pause();
    const plays = p.video.plays;
    h.emit('hlsError', loadError('fragLoadError', 0));
    const fail = failingLoads(h, () => loadError('fragLoadError', 0));
    await failFor(p, 20000, fail);
    const loads = h.loads.length;
    p.online();
    const atOnce = h.loads.length - loads;
    fail();
    await failFor(p, 60000, fail);
    const message = !!messageState(p);
    p.online();
    h.emit('hlsFragLoaded');
    await settle();
    return { atOnce: atOnce, message: message, after: messageState(p), plays: p.video.plays - plays, paused: p.video.paused };
  },

  /* The start of a stream does not reach mediaman: for a while, or for good. */
  async start_dropped(p) {
    const out = {};
    await streamPlays(p, 100);
    playOn(p, 5);
    const down = { err: p.win.serverPlays === 'gateway' ? { error: 'http_502', status: 502 } : NET.err };
    p.held.answer = () => down;
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    p.tick(400);
    out.first = { message: messageState(p), held: holdState(p).shown, notice: !gone(p.q('[data-lost]')) };
    for (let i = 0; i < 60; i++) { p.tick(500); await settle(); }
    out.tries = p.starts().length - 1;
    out.waiting = messageState(p);
    p.held.answer = () => answer('1080p-8000');
    for (let i = 0; i < 20; i++) { p.tick(500); await settle(); }
    out.at = p.hlsList[p.hlsList.length - 1].config.startPosition;
    await streamPlays(p, 105);
    out.back = { message: messageState(p), notice: !gone(p.q('[data-lost]')), quality: p.storage['mediaman.watch.quality'] || null, toasts: p.toasts.length };
    playOn(p, 5);
    p.held.answer = () => down;
    p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    for (let i = 0; i < 140; i++) { await settle(); p.tick(500); }
    await settle();
    out.late = messageState(p);
    out.frame = holdState(p).shown;
    const starts = p.starts().length;
    p.held.answer = () => answer('1080p-8000');
    p.online();
    await settle();
    out.online = { starts: p.starts().length - starts, message: messageState(p), notice: !gone(p.q('[data-lost]')) };
    return out;
  },

  /* The connection is back only after hours: under the message, after the computer slept through
     the tries, or with a request under way all that time. */
  async dropped_hours(p) {
    const HOURS = 3 * 60 * 60 * 1000;
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    h.emit('hlsError', loadError('fragLoadError', 0));
    const fail = failingLoads(h, () => loadError('fragLoadError', 0));
    const way = p.win.serverPlays;
    if (way === 'message') await failFor(p, 65000, fail);
    if (way === 'under way') p.tick(1000);
    p.sleep(HOURS);
    const loads = h.loads.length;
    const plays = p.video.plays;
    if (way === 'asleep') p.tick(1);
    else if (way !== 'under way') p.online();
    if (way === 'under way') { p.video.readyState = 4; h.emit('hlsFragLoaded'); }
    await settle();
    const out = { tried: h.loads.length - loads, message: messageState(p), paused: p.video.paused, play: playState(p).label };
    if (way === 'under way') return out;
    press(p.q('[data-retry]'));
    out.byPress = { tried: h.loads.length - loads, message: messageState(p), paused: p.video.paused };
    fail();
    p.tick(1000);
    out.triedOn = h.loads.length - loads;
    return Object.assign(out, { plays: p.video.plays - plays });
  },

  /* The same as the next episode is on its way. */
  async next_dropped_hours(p) {
    const good = p.pages['/watch/play/502'];
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = 'offline';
    click(p.q('[data-next-btn]'));
    for (let i = 0; i < 130; i++) { await settle(); p.tick(500); }
    p.sleep(3 * 60 * 60 * 1000);
    p.pages['/watch/play/502'] = good;
    p.held.answer = () => answer('1080p-8000');
    p.online();
    for (let i = 0; i < 4; i++) { await settle(); p.tick(500); }
    const out = { starts: p.starts().length, message: messageState(p) };
    press(p.q('[data-retry]'));
    await settle();
    return Object.assign(out, { byPress: p.starts().length });
  },

  /* The next episode's page does not come for a while, or for longer than the page waits. */
  async next_dropped(p) {
    const good = p.pages['/watch/play/502'];
    const asked = () => p.calls.filter((c) => c.url === '/watch/play/502').length;
    const out = {};
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = 'offline';
    click(p.q('[data-next-btn]'));
    await settle();
    p.tick(400);
    out.first = { message: messageState(p), notice: !gone(p.q('[data-lost]')), held: holdState(p).shown };
    if (p.win.serverPlays === 'long') {
      for (let i = 0; i < 130; i++) { p.tick(500); await settle(); }
      out.late = messageState(p);
      out.subtitle = p.q('.player-subtitle').textContent;
    }
    out.asked = asked();
    p.pages['/watch/play/502'] = good;
    p.replies.push(answer('1080p-8000'));
    if (p.win.serverPlays === 'long') p.online();
    for (let i = 0; i < 4; i++) { await settle(); p.tick(500); }
    await streamPlays(p, 0);
    out.went = { key: p.calls.filter((c) => /\/stream$/.test(c.url)).pop().body.rating_key, message: messageState(p), notice: !gone(p.q('[data-lost]')) };
    return out;
  },

  /* What follows cannot be asked, for want of a connection, at the end of the video. */
  async next_unknown_dropped(p) {
    const found = p.pages['/found'];
    p.pages['/watch/play/1'] = 'offline';
    await streamPlays(p, 0);
    endsNow(p);
    await settle();
    p.tick(400);
    const out = { first: messageState(p), notice: !gone(p.q('[data-lost]')) };
    for (let i = 0; i < 130; i++) { p.tick(500); await settle(); }
    out.late = messageState(p);
    p.pages['/watch/play/1'] = found;
    p.online();
    await settle();
    out.found = { message: messageState(p), card: !gone(p.q('[data-upnext]')) };
    return out;
  },

  /* What follows is asked again as the browser comes online under a slow try, which then fails. */
  async next_unknown_twice(p) {
    const found = p.pages['/found'].html;
    p.pages['/watch/play/1'] = 'offline';
    await streamPlays(p, 0);
    endsNow(p);
    await settle();
    const tries = [];
    p.pages['/watch/play/1'] = { get html() { return new Promise((r, fail) => tries.push({ r, fail })); } };
    p.tick(1000);
    await settle();
    p.online();
    await settle();
    tries[1].r(found);
    await settle();
    const card = !gone(p.q('[data-upnext]'));
    tries[0].fail(new TypeError('Failed to fetch'));
    await settle();
    p.tick(4000);
    await settle();
    return { card: card, tries: tries.length, left: p.q('[data-countdown]').textContent };
  },

  /* The person moves back while what follows is still being asked. */
  async next_unknown_left(p) {
    p.pages['/watch/play/1'] = 'offline';
    await streamPlays(p, 0);
    endsNow(p);
    await settle();
    p.video.ended = false; p.video.paused = false; p.video.currentTime = 2000;
    fire(p.video, 'seeking');
    fire(p.video, 'playing');
    playOn(p, 3);
    for (let i = 0; i < 200; i++) { p.tick(500); await settle(); }
    return { message: messageState(p), notice: !gone(p.q('[data-lost]')), asked: p.calls.filter((c) => c.page).length };
  },

  /* The segments go unanswered, and so does every light request: a network that drops and says
     nothing. Then the browser comes online. */
  async silent(p) {
    const timeOut = () => Object.assign(loadError('fragLoadTimeOut'), { response: undefined });
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    p.held.light = p.win.serverPlays;
    for (let i = 0; i < 2; i++) { h.emit('hlsError', timeOut()); await settle(); }
    await pass(p, 5000);
    const out = { whilePlaying: noticeOf(p), starts: p.starts().length };
    const saves = () => p.calls.filter((c) => /\/progress$/.test(c.url)).length;
    const saved = saves();
    for (let i = 0; i < 15; i++) { playOn(p, 1); p.tick(1000); await settle(); }
    out.saves = saves() - saved;
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    p.tick(500);
    out.waiting = noticeOf(p);
    /* Each try goes unanswered for ten seconds, as the first did. */
    let seen = h.loads.length;
    for (let t = 0; t < 120 && !messageState(p); t++) {
      if (h.loads.length !== seen) {
        seen = h.loads.length;
        await pass(p, 10000);
        t += 10;
        h.emit('hlsError', timeOut());
      }
      await pass(p, 1000);
      out.at = t;
    }
    out.message = messageState(p);
    out.destroyed = h.destroyed;
    out.play = playState(p).label;
    out.startsAtEnd = p.starts().length;
    seen = h.loads.length;
    p.held.light = 'answered';
    p.online();
    out.online = { tried: h.loads.length - seen, message: messageState(p) };
    h.emit('hlsFragLoaded');
    await settle();
    out.back = messageState(p);
    return out;
  },

  /* In a drop the server answers light requests again, the segments still slow to come. */
  async silent_then_slow(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const timeOut = () => Object.assign(loadError('fragLoadTimeOut'), { response: undefined });
    p.held.light = 'silent';
    h.emit('hlsError', timeOut());
    await pass(p, 5000);
    fire(p.video, 'waiting');
    p.tick(1500);
    const out = { lost: noticeOf(p) };
    p.held.light = 'answered';
    h.emit('hlsError', timeOut());
    await settle();
    out.slow = noticeOf(p);
    p.held.answer = (body) => answer(body.quality);
    h.emit('hlsError', timeOut());
    await settle();
    return Object.assign(out, { starts: p.starts().length });
  },

  /* The video runs dry and stays so, or waits after a seek, with nothing said by hls.js; light
     requests are answered at first, for as long as told, and then as told. */
  async stalled_silent(p) {
    const [light, bytes, after, seek] = p.win.serverPlays.split(' ');
    p.replies[0] = answer(after > 0 && !seek ? '480p-1500' : '1080p-8000');
    p.held.answer = (body) => answer(body.quality);
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    if (seek) {
      p.video.currentTime = 2000;
      fire(p.video, 'seeking');
    }
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    if (seek === 'seeks') fire(p.video, 'seeking');
    await pass(p, +after);
    p.held.light = light;
    await pass(p, 12000);
    if (bytes === 'bytes') accept(h);
    await pass(p, 4000);
    return { starts: p.starts().length, destroyed: h.destroyed, notice: noticeOf(p) };
  },

  /* The video plays and runs dry before it has moved on at all; then nothing answers. */
  async waits_unmoved(p) {
    await streamPlays(p, 100);
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    p.held.light = 'silent';
    await pass(p, 16000);
    return { notice: noticeOf(p), starts: p.starts().length };
  },

  async light_request(p) {
    await streamPlays(p, 100);
    p.hlsList[0].emit('hlsError', Object.assign(loadError('fragLoadTimeOut'), { response: undefined }));
    await settle();
    return p.held.lights.map((o) => o.credentials);
  },

  /* A wait that outlasts a refusal, or a drop, after a seek: nothing more comes, and nothing is said. */
  async seek_then_silent(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    p.video.currentTime = 2000;
    fire(p.video, 'seeking');
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    if (p.win.serverPlays === 'refused') {
      h.emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
      await pass(p, 2500);
      accept(h);
    } else {
      h.emit('hlsError', loadError('fragLoadError', 0));
      await pass(p, 1500);
      h.emit('hlsFragLoaded');
    }
    const out = { before: noticeOf(p) };
    p.held.light = 'silent';
    await pass(p, 16000);
    return Object.assign(out, { notice: noticeOf(p), starts: p.starts().length });
  },

  /* The message of a lost connection is up, and the server comes back with no word from the
     browser; before that, for a while, the page is not looked at, or is left. */
  async back_unsaid(p) {
    const way = p.win.serverPlays;
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const error = () => loadError('fragLoadError', 0);
    p.held.light = 'gateway';
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    h.emit('hlsError', error());
    await failFor(p, 65000, failingLoads(h, error));
    const out = { message: messageState(p) };
    let asked = p.held.lights.length;
    await pass(p, 91000);
    out.asked = p.held.lights.length - asked;
    const loads = h.loads.length;
    if (way === 'hidden') doc.visibilityState = 'hidden';
    if (way === 'left') p.leave();
    if (way === 'again') press(p.q('[data-retry]'));
    if (way === 'asleep') p.sleep(3 * 3600000);
    p.held.light = 'answered';
    asked = p.held.lights.length;
    await pass(p, 40000);
    out.then = { tried: h.loads.length - loads, message: messageState(p) };
    out.askedThen = p.held.lights.length - asked;
    if (way !== 'hidden') return out;
    doc.visibilityState = 'visible';
    await pass(p, 40000);
    out.seen = { tried: h.loads.length - loads, message: messageState(p) };
    return out;
  },

  /* Under the message the server answers light requests, and every segment still fails. */
  async back_but_failing(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const error = () => loadError('fragLoadError', 0);
    const fail = failingLoads(h, error);
    p.held.light = 'gateway';
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    h.emit('hlsError', error());
    await failFor(p, 65000, fail);
    p.held.light = 'answered';
    /* How often the message comes up anew in this long. */
    const comesUp = async (ms) => {
      let times = 0;
      let up = !!messageState(p);
      for (let i = 0; i < ms; i += 500) {
        p.tick(500);
        fail();
        await settle();
        if (!up && messageState(p)) times++;
        up = !!messageState(p);
      }
      return times;
    };
    const out = {};
    if (p.win.serverPlays === 'plays between') {
      out.before = await comesUp(100000);
      p.online();
      h.emit('hlsFragLoaded');
      playOn(p, 5);
      out.played = messageState(p);
      h.emit('hlsError', error());
      fire(p.video, 'waiting');
      await failFor(p, 65000, fail);
      out.dropped = messageState(p);
    }
    Object.assign(out, { rounds: await comesUp(600000), message: messageState(p) });
    const asked = p.held.lights.length;
    const loads = h.loads.length;
    out.later = [await comesUp(300000), p.held.lights.length - asked, h.loads.length - loads];
    press(p.q('[data-retry]'));
    await settle();
    out.pressed = { tried: h.loads.length - loads, message: messageState(p) };
    out.after = await comesUp(600000);
    return out;
  },

  /* The start, or its playlist, is not answered: by a slow server, or where nothing is. */
  async hung(p) {
    const [what, light] = p.win.serverPlays.split(' ');
    const first = {};
    await streamPlays(p, 100);
    playOn(p, 5);
    p.replies.push(what === 'start' ? { ok: new Promise((r) => { first.reply = r; }) } : answer('1080p-8000'));
    p.held.light = light;
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await pass(p, 7500);
    const out = { early: noticeOf(p) };
    await pass(p, 1000);
    out.slow = noticeOf(p);
    await pass(p, 5500);
    out.later = noticeOf(p);
    const tries = () => p.starts().length + p.hlsList.length;
    const before = tries();
    for (let t = 14; t < 110 && !messageState(p); t++) { await pass(p, 1000); out.at = t; }
    out.message = messageState(p);
    out.unattended = what === 'start' ? tries() - before : 0;
    if (!out.message) {
      if (what === 'start') first.reply(answer('1080p-8000').ok);
      await streamPlays(p, 105);
      return Object.assign(out, { played: { notice: noticeOf(p), message: messageState(p), starts: p.starts().length } });
    }
    const seen = tries();
    p.held.light = 'answered';
    p.replies.push(answer('1080p-8000'));
    p.online();
    out.online = { tried: tries() > seen, message: messageState(p) };
    await streamPlays(p, 105);
    out.back = messageState(p);
    return out;
  },

  /* A start that hung in a drop: the server is there again, and answers it or does not. */
  async hung_back(p) {
    const first = {};
    await streamPlays(p, 100);
    playOn(p, 5);
    p.replies.push({ ok: new Promise((r) => { first.reply = r; }) }, answer('720p-4000'));
    p.held.light = 'silent';
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await pass(p, 14000);
    const out = { lost: noticeOf(p) };
    p.held.light = 'answered';
    if (p.win.serverPlays === 'answers') first.reply(answer('1080p-8000').ok);
    await settle();
    await pass(p, 9000);
    out.starts = p.starts().length;
    await streamPlays(p, 105);
    out.stream = p.hlsList[p.hlsList.length - 1].src !== '/p1';
    if (p.win.serverPlays !== 'answers') first.reply({ stream_id: 'late', playlist: '/late', quality: 'best' });
    await pass(p, 70000);
    return Object.assign(out, { message: messageState(p), ended: p.calls.filter((c) => c.aborted).length });
  },

  /* A move in a drop, between two tries. */
  async seek_in_drop(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const lose = () => h.emit('hlsError', loadError('fragLoadError', 0));
    lose();
    await failFor(p, 20000, failingLoads(h, lose));
    const loads = h.loads.length;
    p.video.currentTime = 2000;
    fire(p.video, 'seeking');
    const atOnce = h.loads.length - loads;
    lose();
    p.tick(1000);
    return { atOnce: atOnce, next: h.loads.length - loads - atOnce };
  },

  /* The same where it is the start that does not come through. */
  async seek_in_lost_start(p) {
    p.replies[0] = NET;
    await settle();
    p.held.answer = () => NET;
    await pass(p, 20000);
    const starts = p.starts().length;
    p.held.answer = () => answer('1080p-8000');
    key('ArrowRight');
    p.tick(300);
    await settle();
    return { atOnce: p.starts().length - starts, at: p.hlsList.map((h) => h.config.startPosition) };
  },

  /* The server refuses for a while, the picture waiting, and then takes a request. */
  async busy_said(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const refuse = async (ms) => {
      for (let i = 0; i < ms; i += 500) {
        if (i % 2000 === 0) h.emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
        p.tick(500);
        await settle();
      }
    };
    loadedTo(p, 400);
    await refuse(20000);
    const out = { playing: noticeOf(p) };
    loadedTo(p, p.video.currentTime);
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    await refuse(7000);
    out.early = noticeOf(p);
    await refuse(2000);
    out.busy = noticeOf(p);
    out.role = p.q('[data-lost]').parentNode.getAttribute('role');
    accept(h);
    return Object.assign(out, { taken: noticeOf(p), message: messageState(p) });
  },

  /* A quality picked under "Playing somewhere else", and another as its place is asked for; or
     Play here, and a key then. */
  async replaced_pick(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { ok: false, error: 'replaced' }));
    await settle();
    p.pages['/watch/play/1'] = p.pages['/elsewhere'];
    p.held.answer = (body) => answer(body.quality);
    const pick = (id) => click(p.q('[data-quality="' + id + '"]'));
    const way = p.win.serverPlays;
    if (way === 'pressed') { press(p.q('[data-retry]')); doc.activeElement = doc.body; key(' '); }
    else pick(way === 'same' ? 'best' : 'medium');
    if (way === 'twice') pick('low');
    await settle();
    return { starts: p.starts().slice(1), at: p.hlsList[p.hlsList.length - 1].config.startPosition, message: messageState(p) };
  },

  /* The same title was started somewhere else: this one learns of it by a segment, the playlist or a save. */
  async replaced(p) {
    const way = p.win.serverPlays;
    await streamPlays(p, 100);
    playOn(p, 5);
    const stops = () => p.calls.filter((c) => /\/(stop|progress)$/.test(c.url)).length + p.beacons.length;
    if (way === 'save') {
      p.held.progressFails = { error: 'replaced', status: 410 };
      p.tick(10000);
      await settle();
    }
    const said = stops();
    if (way !== 'save') p.hlsList[0].emit('hlsError', loadError(way, 410, { ok: false, error: 'replaced' }));
    await settle();
    const out = { message: messageState(p), role: p.q('[data-message]').getAttribute('role'), focus: doc.activeElement === p.q('[data-retry]'), frame: holdState(p).shown, playing: !p.video.paused, destroyed: p.hlsList[0].destroyed };
    p.online();
    p.tick(600000);
    await settle();
    p.leave();
    out.told = stops() - said;
    p.comeBack();
    await settle();
    p.tick(60000);
    out.byItself = p.starts().length - 1;
    out.still = messageState(p);
    /* The other one played on: here goes on from there. */
    p.pages['/watch/play/1'] = p.pages['/elsewhere'] || 'offline';
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    await settle();
    out.here = { starts: p.starts().length - 1, at: p.hlsList[p.hlsList.length - 1].config.startPosition, message: messageState(p) };
    await streamPlays(p, 900);
    /* A later error has its own words on the button again. */
    p.held.answer = () => ({ err: { status: 429, error: 'rate_limited' } });
    p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    out.button = messageState(p).button;
    return out;
  },

  /* Playing somewhere else, after saves here; then, if told, the same again from Play here. */
  async replaced_after_save(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    p.tick(10000);
    await settle();
    if (p.win.serverPlays === 'beacon') {
      playOn(p, 5);
      doc.visibilityState = 'hidden';
      fire(doc, 'visibilitychange');
      doc.visibilityState = 'visible';
    }
    playOn(p, 5);
    const replace = () => p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'replaced' }));
    p.held.answer = (body) => answer(body.quality);
    p.pages['/watch/play/1'] = p.pages['/elsewhere'];
    replace();
    await settle();
    press(p.q('[data-retry]'));
    await settle();
    const at = () => p.hlsList[p.hlsList.length - 1].config.startPosition;
    const out = [at()];
    if (p.win.serverPlays !== 'twice') return out;
    await streamPlays(p, out[0]);
    playOn(p, 5);
    replace();
    await settle();
    press(p.q('[data-retry]'));
    await settle();
    return out.concat(at());
  },

  /* A stream this page itself replaced says so: by a save under way as the quality changes, or by
     a start the page gave up that reaches the server last. */
  async replaced_by_itself(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    const hold = (into) => ({ ok: new Promise((r) => { into.reply = r; }) });
    if (p.win.serverPlays === 'save') {
      p.held.progressFails = { error: 'replaced', status: 410 };
      p.tick(10000);
      p.replies.push(answer('480p-1500'));
      click(p.q('[data-quality="480p-1500"]'));
      await streamPlays(p, 105);
      return { message: messageState(p), starts: p.starts().length };
    }
    const first = {};
    const second = {};
    p.replies.push(hold(first), hold(second));
    click(p.q('[data-quality="480p-1500"]'));
    await settle();
    click(p.q('[data-quality="720p-4000"]'));
    await settle();
    second.reply(answer('720p-4000').ok);
    await streamPlays(p, 105);
    first.reply(answer('480p-1500').ok);
    await settle();
    p.replies.push(answer('720p-4000'), answer('720p-4000'));
    const replacedBy = (by) => p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { ok: false, error: 'replaced', by: by }));
    replacedBy(pageOf(p));
    await streamPlays(p, 105);
    const restarted = { message: messageState(p), starts: p.starts().length };
    /* However long after: the server says whose start it was. */
    p.tick(300000);
    playOn(p, 60);
    replacedBy(pageOf(p));
    await streamPlays(p, 165);
    restarted.muchLater = { message: messageState(p), starts: p.starts().length };
    replacedBy('another-page');
    await settle();
    return Object.assign(restarted, { later: messageState(p).text, after: p.starts().length });
  },

  /* Two qualities are picked quickly, so that the first start reaches the server last or is never
     answered. Much later the title starts somewhere else. */
  async replaced_long_after(p) {
    const way = p.win.serverPlays;
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    const first = {};
    p.replies.push({ ok: new Promise((r) => { first.reply = r; }) }, answer('720p-4000'));
    click(p.q('[data-quality="480p-1500"]'));
    await settle();
    click(p.q('[data-quality="720p-4000"]'));
    await streamPlays(p, 105);
    const h = p.hlsList[p.hlsList.length - 1];
    h.emit('hlsFragLoading');
    if (way !== 'never') first.reply(answer('480p-1500').ok);
    await settle();
    const replaced = () => h.emit('hlsError', loadError('fragLoadError', 410, { ok: false, error: 'replaced', by: 'another-page' }));
    if (way === 'asked before') {
      /* Asked for while this page's other start was still open. */
      h.emit('hlsFragLoaded');
    } else if (way === 'save') {
      playOn(p, 5);
      p.video.pause();
      await settle();
    } else {
      if (way === 'never') p.tick(120000);
      h.emit('hlsFragLoaded');
      h.emit('hlsFragLoading');
      h.emit('hlsFragLoaded');
    }
    const starts = p.starts().length;
    p.replies.push(answer('720p-4000'));
    replaced();
    await settle();
    return { message: messageState(p), starts: p.starts().length - starts };
  },

  /* What this page starts by itself the server refuses, the person having asked another page
     since: after the stream went for idleness, or as a start left by a drop gets through. */
  async start_refused(p) {
    const way = p.win.serverPlays;
    const refusal = { err: { status: 410, error: 'replaced', data: { error: 'replaced', by: 'another-page' } } };
    await streamPlays(p, 100);
    playOn(p, 5);
    p.tick(20000);
    if (way === 'drop') p.replies.push(NET, NET);
    p.replies.push(refusal);
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    if (way === 'drop') {
      p.tick(1000);
      await settle();
      p.online();
      await settle();
    }
    const sent = startCalls(p).map((c) => c.body);
    const out = {
      message: messageState(p), frame: holdState(p).shown, loading: p.root.classList.contains('is-loading'),
      pages: sent.map((b) => b.page === sent[0].page && /^[A-Za-z0-9_-]{8,64}$/.test(b.page)),
      asked: sent.map((b) => b.asked_s),
    };
    p.tick(600000);
    p.online();
    await settle();
    out.byItself = startCalls(p).length - sent.length;
    p.pages['/watch/play/1'] = 'offline';
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    await streamPlays(p, 105);
    out.here = { asked: startCalls(p).pop().body.asked_s, at: p.hlsList[p.hlsList.length - 1].config.startPosition, message: messageState(p) };
    return out;
  },

  /* Much later the person, or the page by itself, does something; the next start then says how
     long ago the page was asked. With it come the seconds since the last press before that. */
  async asked_after(p) {
    const gone = () => p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    const segmentLost = () => loadError('fragLoadError', 0);
    const nextPage = p.pages['/watch/play/502'];
    const refuse = (by) => ({ err: { status: 410, error: 'replaced', data: { error: 'replaced', by: by } } });
    let pressedAt = 0;
    const later = async () => { p.tick(20000); await settle(); };
    const replaced = async () => {
      p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'replaced', by: 'another-page' }));
      await settle();
    };
    const unreached = async () => {
      p.held.light = 'gateway';
      p.pages['/watch/play/502'] = 'offline';
      endsNow(p);
      p.tick(10000);
      await settle();
    };
    /* The start fails, and nothing is heard of it until the browser comes online. */
    const startLost = async () => { p.replies.push(NET, NET, NET); gone(); await settle(); };
    const acts = {
      async quality() { await later(); click(p.q('[data-quality="720p-4000"]')); },
      async 'try again after a drop'() {
        const h = p.hlsList[0];
        p.held.light = 'gateway';
        fire(p.video, 'waiting');
        h.emit('hlsError', segmentLost());
        await failFor(p, 65000, failingLoads(h, segmentLost));
        press(p.q('[data-retry]'));
        gone();
      },
      async 'play here'() { await replaced(); await later(); press(p.q('[data-retry]')); },
      async 'play here, its place 15 s away'() {
        await replaced();
        await later();
        p.pages['/watch/play/1'] = 'hangs';
        press(p.q('[data-retry]'));
        await settle();
        p.tick(15000);
      },
      async 'try again after a refusal'() {
        p.replies.push({ err: { status: 429, error: 'rate_limited' } });
        gone();
        await later();
        press(p.q('[data-retry]'));
      },
      async 'play with no stream'() {
        p.sleep(3 * 3600000);
        endsNow(p);
        p.q('[data-ask]').close();
        await later();
        press(p.q('[data-play]'));
      },
      async 'play on a paused video'() { key(' '); await later(); key(' '); gone(); },
      async 'pause'() { await later(); key(' '); gone(); },
      async 'play as a start is on its way'() { await startLost(); key(' '); await later(); key(' '); p.online(); },
      async 'pause as a start is on its way'() { await startLost(); await later(); key(' '); p.online(); },
      async 'seek by a key'() { await later(); key('ArrowRight'); gone(); },
      async 'seek by a key, long landed'() { await later(); key('ArrowRight'); pressedAt = p.win.Date.now(); await later(); gone(); },
      async 'seek on the bar'() { await later(); point(p, 'pointerdown', 500); point(p, 'pointerup', 500); gone(); },
      async next() { await later(); press(p.q('[data-next-btn]')); },
      async 'next as it is on its way by itself'() {
        p.pages['/watch/play/502'] = 'offline';
        endsNow(p);
        p.tick(10000);
        await settle();
        await later();
        p.pages['/watch/play/502'] = nextPage;
        press(p.q('[data-next-btn]'));
        p.online();
      },
      async 'next after it was out of reach'() {
        p.pages['/watch/play/502'] = { html: '<div id="watch-page" data-plex-unavailable></div>' };
        press(p.q('[data-next-btn]'));
        await later();
        p.pages['/watch/play/502'] = nextPage;
        press(p.q('[data-retry]'));
      },

      async 'a stream that went'() { await later(); gone(); },
      async 'a start tried again by itself'() { await later(); p.replies.push(NET); gone(); await settle(); p.tick(1000); },
      async 'online as a start is lost'() { await startLost(); await later(); p.online(); },
      async 'next by itself'() { endsNow(p); await later(); },
      async 'next by itself, online after a drop'() {
        await unreached();
        await failFor(p, 70000, () => {});
        /* Found out of reach a second time, it is still one episode that began by itself. */
        p.online();
        await failFor(p, 70000, () => {});
        if (p.sitting().count !== 1 || !messageState(p)) throw new Error('counted ' + p.sitting().count);
        p.pages['/watch/play/502'] = nextPage;
        p.online();
      },
      async 'next by a press, online after a drop'() {
        await later();
        p.held.light = 'gateway';
        p.pages['/watch/play/502'] = 'offline';
        press(p.q('[data-next-btn]'));
        pressedAt = p.win.Date.now();
        await failFor(p, 70000, () => {});
        p.pages['/watch/play/502'] = nextPage;
        p.online();
      },
      async 'back from the page cache'() { p.leave(); await later(); p.comeBack(); },
      async 'back from the page cache, refused'() { p.leave(); await later(); p.replies.unshift(refuse('another-page')); p.comeBack(); },
    };
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    p.held.answer = (body) => answer(body.quality);
    p.pages['/watch/play/1'] = 'offline';
    const starts = p.starts().length;
    await acts[p.win.serverPlays]();
    await settle();
    if (p.starts().length === starts) throw new Error('nothing started');
    const now = p.win.Date.now();
    return { asked: startCalls(p).pop().body.asked_s, since: (now - pressedAt) / 1000, message: messageState(p) };
  },

  /* How long ago the first start of the page says it was asked. */
  async asked_at_first(p) {
    await settle();
    return startCalls(p)[0].body.asked_s;
  },

  /* A start is never answered; Try again is, and then the first answer comes after all. */
  async start_unanswered(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const first = {};
    p.replies.push({ ok: new Promise((r) => { first.reply = r; }) }, answer('1080p-8000'));
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    p.tick(115000);
    await settle();
    const out = { early: messageState(p) };
    p.tick(6000);
    await settle();
    out.message = messageState(p);
    out.loading = p.root.classList.contains('is-loading');
    press(p.q('[data-retry]'));
    await streamPlays(p, 105);
    const streams = p.hlsList.length;
    first.reply({ stream_id: 'late', playlist: '/late', quality: 'best' });
    await settle();
    out.after = { streams: p.hlsList.length - streams, message: messageState(p), stopped: p.calls.some((c) => c.url === '/api/watch/stream/late/stop') };
    return out;
  },

  /* One refusal, and then something else goes on for long: by `serverPlays` the segment comes, but
     slowly; the connection is lost; or the person moves back to what has loaded, paused. */
  async busy_once(p) {
    const way = p.win.serverPlays;
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const refuse = () => h.emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
    const out = {};
    if (way === 'back') {
      p.video.pause();
      refuse();
      loadedTo(p, 105);
      key('ArrowLeft');
      p.tick(300);
      fire(p.video, 'seeking');
      for (let i = 0; i < 100; i++) { p.tick(2000); await settle(); }
      loadedTo(p, 0);
      p.video.currentTime = 2000;
      refuse();
      await settle();
      return { message: messageState(p), loads: h.loads.length, stops: p.beacons.length };
    }
    refuse();
    fire(p.video, 'waiting');
    p.tick(2000);
    if (way === 'lost for a while') {
      h.emit('hlsError', loadError('fragLoadError', 0));
      await failFor(p, 50000, failingLoads(h, () => loadError('fragLoadError', 0)));
      for (let i = 0; i < 10; i++) { refuse(); p.tick(2000); await settle(); }
      return { message: messageState(p), starts: p.starts().length };
    }
    if (way === 'lost for long') {
      h.emit('hlsError', loadError('fragLoadError', 0));
      await failFor(p, 130000, failingLoads(h, () => loadError('fragLoadError', 0)));
      press(p.q('[data-retry]'));
      refuse();
      await settle();
      return { message: messageState(p), starts: p.starts().length };
    }
    if (way === 'lost') {
      p.hlsList[0].emit('hlsError', loadError('fragLoadError', 0));
      const fail = failingLoads(h, () => loadError('fragLoadError', 0));
      await failFor(p, 65000, fail);
      out.message = messageState(p);
      const loads = h.loads.length;
      p.online();
      return Object.assign(out, { tried: h.loads.length - loads });
    }
    accept(h);
    if (way === 'again') {
      p.tick(5000);
      refuse();
      p.tick(8000);
      await settle();
      return { starts: p.starts().length };
    }
    p.held.answer = (body) => answer(body.quality);
    p.tick(9000);
    await settle();
    out.early = p.starts().length;
    p.tick(2000);
    await settle();
    out.stepped = p.starts().length;
    p.tick(120000);
    out.message = messageState(p);
    out.destroyed = h.destroyed;
    return out;
  },

  /* The connection drops, or a proxy answers, and the server refuses, by turns, every 3 s: `first`
     comes at the start and every other time. */
  async busy_and_lost(p) {
    const [first, status] = p.win.serverPlays.split(' ');
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const errors = {
      refused: () => loadError('fragLoadError', 429, null, { 'retry-after': '1' }),
      lost: () => loadError('fragLoadError', +status),
    };
    const order = first === 'refused' ? ['refused', 'lost'] : ['lost', 'refused'];
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    const out = { at: null };
    let n = 0;
    for (let t = 0; t < 300 && !messageState(p); t += 3) {
      if (!h.stopped || !t) h.emit('hlsError', errors[order[n++ % 2]]());
      if (messageState(p)) out.at = t;
      else p.tick(3000);
      await settle();
    }
    out.message = messageState(p);
    out.frame = p.video.readyState === 2 || holdState(p).shown;
    /* Under the message nothing goes on by itself; a press starts the count over. */
    const asked = h.loads.length + p.starts().length;
    p.tick(60000);
    await settle();
    out.byItself = h.loads.length + p.starts().length - asked;
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    await settle();
    const last = p.hlsList[p.hlsList.length - 1];
    last.emit('hlsError', errors.lost());
    out.again = messageState(p);
    return out;
  },

  /* Refused with much loaded, then a drop while that plays, and refused again as it runs dry. */
  async busy_then_dry(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const refuse = () => h.emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
    loadedTo(p, 240);
    refuse();
    p.tick(2000);
    const lose = () => h.emit('hlsError', loadError('fragLoadError', 0));
    lose();
    const fail = failingLoads(h, lose);
    for (let i = 0; i < 130; i++) { playOn(p, 1); p.tick(1000); fail(); }
    loadedTo(p, p.video.currentTime);
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    p.tick(3000);
    refuse();
    await settle();
    const out = { dry: messageState(p), starts: p.starts().length };
    for (let i = 0; i < 70 && !messageState(p); i++) { p.tick(2000); refuse(); await settle(); }
    out.late = messageState(p) && messageState(p).text;
    return out;
  },

  /* In a drop after a refusal a try is taken, and its body is lost too. */
  async taken_then_lost(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    playOn(p, 5);
    const h = p.hlsList[0];
    const lose = () => h.emit('hlsError', loadError('fragLoadError', 0));
    h.emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
    p.video.readyState = 2;
    fire(p.video, 'waiting');
    p.tick(2000);
    lose();
    p.tick(1000);
    accept(h);
    lose();
    const fail = failingLoads(h, lose);
    await failFor(p, 50000, fail);
    const out = { starts: p.starts().length, early: messageState(p) };
    await failFor(p, 10000, fail);
    return Object.assign(out, { message: messageState(p) });
  },

  /* Refused for long, paused with enough loaded ahead. */
  async busy_paused(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    loadedTo(p, 140);
    p.video.pause();
    for (let i = 0; i < 80; i++) {
      p.hlsList[0].emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
      p.tick(2000);
      await settle();
    }
    return { message: messageState(p), destroyed: p.hlsList[0].destroyed };
  },

  /* A server that is too busy for everything, for good. */
  async busy_for_good(p) {
    const details = p.win.serverPlays;
    const plays = details === 'fragLoadError';
    if (plays) await streamPlays(p, 100);
    else await settle();
    const refuse = () => p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError(details, 429, null, { 'retry-after': '2' }));
    const out = {};
    refuse();
    /* Enough was loaded for more than a minute. */
    loadedTo(p, 400);
    for (let i = 0; plays && i < 40; i++) { playOn(p, 2); p.tick(2000); await settle(); refuse(); }
    out.playing = messageState(p);
    loadedTo(p, p.video.currentTime);
    if (plays) fire(p.video, 'waiting');
    for (let i = 0; i < 58; i++) { p.tick(2000); await settle(); refuse(); }
    out.early = messageState(p);
    for (let i = 0; i < 4; i++) { p.tick(2000); await settle(); if (!messageState(p)) refuse(); }
    out.message = messageState(p);
    const streams = p.hlsList.length;
    p.tick(60000);
    await settle();
    out.after = [p.hlsList.length - streams, p.starts().length];
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    await settle();
    refuse();
    out.again = { message: messageState(p), starts: p.starts().length, at: p.hlsList[p.hlsList.length - 1].config.startPosition };
    return out;
  },

  /* In a drop one try is never answered; the next, if there is one by Try again, is, and then the
     first after all. */
  async start_unanswered_in_drop(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const first = {};
    p.held.light = 'silent';
    p.replies.push(NET, { ok: new Promise((r) => { first.reply = r; }) });
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    for (let i = 0; i < 250; i++) { p.tick(500); await settle(); }
    const out = { message: messageState(p) };
    if (p.win.serverPlays !== 'unpressed') {
      p.replies.push(answer('1080p-8000'));
      press(p.q('[data-retry]'));
      await streamPlays(p, 105);
    }
    const streams = p.hlsList.length;
    first.reply({ stream_id: 'late', playlist: '/late', quality: 'best' });
    await settle();
    out.ended = p.calls.filter((c) => c.aborted).length;
    out.after = { streams: p.hlsList.length - streams, stopped: p.calls.some((c) => c.url === '/api/watch/stream/late/stop') };
    return out;
  },

  /* In a drop a try hangs; before or under the message the person, or the browser coming online,
     has it tried afresh. Then the hung one is answered or fails after all. */
  async start_hangs_in_drop(p) {
    const [way, then] = p.win.serverPlays.split(' ');
    await streamPlays(p, 100);
    playOn(p, 5);
    const hung = {};
    p.held.light = 'silent';
    p.replies.push(NET, { ok: new Promise((r, j) => { hung.reply = r; hung.fail = j; }) });
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    for (let i = 0; i < (way === 'early' ? 20 : 130); i++) { p.tick(500); await settle(); }
    const out = { message: messageState(p) };
    const starts = p.starts().length;
    p.replies.push(answer('1080p-8000'));
    if (way === 'press') press(p.q('[data-retry]'));
    else p.online();
    out.asked = p.starts().length - starts;
    await streamPlays(p, 105);
    const streams = p.hlsList.length;
    const ended = p.calls.filter((c) => c.aborted).length;
    if (then === 'answered') hung.reply({ stream_id: 'late', playlist: '/late', quality: 'best' });
    else hung.fail(NET.err);
    await settle();
    p.tick(130000);
    await settle();
    out.after = { streams: p.hlsList.length - streams, starts: p.starts().length - starts, ended: ended, message: messageState(p) };
    return out;
  },

  /* Tries that hang in a drop, one after another by a press, a move or a quality picked; how many
     are open at once. Then one is answered, and the server comes to one that was ended. */
  async starts_hang(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    p.held.light = 'silent';
    p.replies.push(NET);
    p.hlsList[0].emit('hlsError', loadError('fragLoadError', 410, { error: 'gone' }));
    await settle();
    await pass(p, 2000);
    const open = () => p.calls.filter((c) => c.open).length;
    for (let i = 0; i < 3; i++) {
      key('ArrowRight');
      p.tick(300);
      await settle();
    }
    const out = { pressed: open() };
    for (const id of ['medium', 'low']) {
      click(p.q('[data-quality="' + id + '"]'));
      await pass(p, 14500);
    }
    out.picked = open();
    out.notice = noticeOf(p);
    p.held.light = 'answered';
    p.held.answer = (body) => answer(body.quality);
    p.online();
    await streamPlays(p, 105);
    const starts = p.starts().length;
    p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'replaced', by: pageOf(p) }));
    await settle();
    return Object.assign(out, { replaced: messageState(p), starts: p.starts().length - starts });
  },

  /* A server too busy for a while lets the video run dry, twice. */
  async busy_stalls(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    for (let i = 0; i < 2; i++) {
      playOn(p, 5);
      p.hlsList[0].emit('hlsError', loadError('fragLoadError', 429, null, { 'retry-after': '2' }));
      fire(p.video, 'waiting');
      p.tick(11000);
      p.hlsList[0].emit('hlsFragLoaded');
      fire(p.video, 'playing');
    }
    await settle();
    const starts = p.starts().length;
    p.held.answer = (body) => answer(body.quality);
    for (let i = 0; i < 2; i++) { playOn(p, 5); fire(p.video, 'waiting'); fire(p.video, 'playing'); }
    await settle();
    return { busy: starts, slow: p.starts().length };
  },

  /* The title starts somewhere else as this one ends and counts down to the next episode. */
  async replaced_at_end(p) {
    p.held.progressFails = { error: 'replaced', status: 410 };
    await streamPlays(p, 0);
    playOn(p, 5);
    endsNow(p);
    await settle();
    p.tick(60000);
    await settle();
    return { message: messageState(p).text, card: !gone(p.q('[data-upnext]')), went: p.calls.some((c) => c.page) };
  },

  /* F and the arrows with the volume slider in focus: by the mouse, then by Tab. */
  async slider_keys(p) {
    let asked = 0;
    p.root.requestFullscreen = () => { asked++; return Promise.resolve(); };
    await streamPlays(p, 100);
    const look = () => {
      const from = p.video.currentTime;
      const full = asked;
      key('ArrowRight');
      p.tick(300);
      key('f');
      return { moved: p.video.currentTime - from, full: asked - full };
    };
    press(p.q('[data-volume]'));
    const byMouse = look();
    tabTo(p.q('[data-volume]'));
    return { byMouse: byMouse, byTab: look() };
  },

  /* The slider dragged to nothing, and the sound put back on, here and by a later page. */
  async drag_to_silence(p) {
    const slider = p.q('[data-volume]');
    const drag = (levels) => {
      levels.forEach((v) => { slider.value = String(v); fire(slider, 'input', { bubbles: true }); fire(p.video, 'volumechange'); });
      fire(slider, 'change', { bubbles: true });
    };
    const look = () => ({ volume: p.video.volume, muted: p.video.muted, slider: +slider.value, name: p.q('[data-mute]').getAttribute('aria-label') });
    await streamPlays(p, 100);
    drag([0.9, 0.7]);
    drag([0.5, 0.2, 0.05, 0]);
    const out = { silent: look(), saved: Object.assign({}, p.storage) };
    press(p.q('[data-mute]'));
    fire(p.video, 'volumechange');
    out.back = look();
    drag([0.3, 0, 0.4]);
    out.up = look();
    return out;
  },

  /* A browser that leaves the page cancels its requests first, and only then says so. */
  async cancelled(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    const cancel = () => p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError', response: { code: 0 } });
    cancel();
    p.tick(500);
    await settle();
    const starts = p.starts().length;
    p.leave();
    p.tick(5000);
    await settle();
    const out = { starts: starts, after: p.starts().length, left: p.beacons.slice() };
    p.beacons.length = 0;
    /* A page that stays: the connection really failed. */
    p.replies.push(answer('1080p-8000'));
    p.comeBack();
    await streamPlays(p, 105);
    playOn(p, 5);
    cancel();
    p.replies.push(answer('1080p-8000'));
    p.tick(1000);
    await settle();
    out.restarted = p.starts().length - out.after;
    out.stopped = p.beacons;
    return out;
  },

  /* The person tries one quality after another and all are refused. */
  async picks_refused(p) {
    await streamPlays(p, 0);
    for (const id of ['medium', 'low', 'medium']) {
      p.replies.push({ err: { status: 503 } }, answer('1080p-8000'));
      click(p.q('[data-quality="' + (p.q('[data-quality="' + id + '"]') ? id : '480p-1500') + '"]'));
      await streamPlays(p, 0);
    }
    return { message: !gone(p.q('[data-message]')), starts: p.starts().length };
  },

  /* A quality the person picked stays, however it stalls. */
  async fixed_stalls(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    for (let i = 0; i < 4; i++) { playOn(p, 5); fire(p.video, 'waiting'); p.tick(12000); fire(p.video, 'playing'); }
    await settle();
    return p.starts();
  },

  /* A quality is picked while a long stall of Automatic's is being waited out. */
  async picked_in_stall(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    playOn(p, 5);
    fire(p.video, 'waiting');
    click(p.q('[data-quality="1080p-8000"]'));
    p.tick(12000);
    await settle();
    return { starts: p.starts(), saved: p.storage['mediaman.watch.quality'] };
  },

  /* The server refuses a step Automatic takes. */
  async auto_refused(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 30);
    p.replies.push({ err: { status: 429, error: 'rate_limited' } }, answer('1080p-8000'));
    for (let i = 0; i < 2; i++) { playOn(p, 5); fire(p.video, 'waiting'); }
    await streamPlays(p, 40);
    return Object.assign(menuState(p), { starts: p.starts(), toasts: p.toasts });
  },

  /* The mouse moves along the bar, rests, and moves back. */
  async frame_hover(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    fire(p.video, 'timeupdate');
    const now = () => p.q('[data-seek]').getAttribute('aria-valuenow');
    const out = { now: now() };
    const answered = async () => { if (p.frames.answer) p.frames.answer(200); await settle(); };
    point(p, 'pointermove', 100);
    out.atOnce = p.frames.asked.slice();
    await answered();
    out.first = await bubbleState(p);
    /* On the move: never a moment's rest. */
    for (let x = 101; x <= 140; x++) { point(p, 'pointermove', x); p.tick(20); await answered(); }
    out.moving = p.frames.asked.slice();
    out.near = await bubbleState(p);
    p.tick(400);
    out.trailing = p.frames.asked[p.frames.asked.length - 1];
    await answered();
    out.rested = await bubbleState(p);
    /* Moved on while a request is out: only the last place is asked for after it. */
    const from = p.frames.asked.length;
    for (const x of [200, 300, 400]) { point(p, 'pointermove', x); p.tick(350); }
    out.oneInFlight = p.frames.asked.slice(from);
    await answered();
    p.tick(400);
    out.latest = p.frames.asked.slice(from);
    await answered();
    out.arrived = await bubbleState(p);
    point(p, 'pointermove', 142);
    out.nearest = await bubbleState(p);
    point(p, 'pointermove', 140);
    out.kept = await bubbleState(p);
    const asked = p.frames.asked.length;
    p.tick(500);
    out.keptAsked = p.frames.asked.length - asked;
    out.nowAfter = now();
    out.position = p.video.currentTime;
    out.restedOnBar = await restedOnBar(p, () => point(p, 'pointermove', 141));
    point(p, 'pointerleave', 140);
    out.left = await bubbleState(p);
    p.tick(5000);
    out.idleAfter = p.root.classList.contains('is-idle');
    return out;
  },

  /* A click on the bar with the mouse, which then stays where it is. */
  async bar_clicked(p) {
    p.replies[0] = answer('1080p-8000', Object.assign({ markers: MARKERS }, WITH_FRAMES));
    await streamPlays(p, 30);
    point(p, 'pointermove', 25);
    point(p, 'pointerdown', 25);
    point(p, 'pointerup', 25);
    fire(p.video, 'timeupdate');
    const out = await restedOnBar(p, () => point(p, 'pointermove', 26));
    out.skip = !gone(p.q('[data-skip]'));
    return out;
  },

  /* WebKit says the pointer left as it lets go of the bar, with what it is over unchanged. */
  async bar_left(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    const bar = p.q('[data-played]');
    const out = {};
    const clickOn = async (under, type) => {
      point(p, 'pointermove', 25);
      point(p, 'pointerdown', 25, type);
      doc.under = under;
      point(p, 'pointerup', 25, type);
      point(p, 'pointerleave', 25, type);
      return (await bubbleState(p)).shown;
    };
    out.mouse = await clickOn(bar, 'mouse');
    doc.under = p.video;
    point(p, 'pointerleave', 25);
    out.left = (await bubbleState(p)).shown;
    out.droppedOff = await clickOn(p.video, 'mouse');
    out.touch = await clickOn(bar, 'touch');
    out.pen = await clickOn(bar, 'pen');
    return out;
  },

  /* A finger drags along the bar and lets go. */
  async frame_drag(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    point(p, 'pointermove', 100, 'touch');
    const before = await bubbleState(p);
    point(p, 'pointerdown', 100, 'touch');
    point(p, 'pointermove', 200, 'touch');
    const during = await bubbleState(p);
    const during_s = p.video.currentTime;
    p.frames.answer(200);
    await settle();
    p.tick(300);
    point(p, 'pointerup', 200, 'touch');
    return { before: before.shown, during: during, duringAt: during_s, asked: p.frames.asked, after: (await bubbleState(p)).shown, at: p.video.currentTime };
  },

  /* A long look around: only the newest frames are kept. */
  async frame_many(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    for (let i = 0; i < 65; i++) {
      point(p, 'pointermove', 5 * i);
      p.tick(300);
      p.frames.answer(200);
      await settle();
    }
    return { kept: p.frames.made - p.frames.freed };
  },

  /* What each refusal does, and what a new stream changes. */
  async frame_refused(p) {
    const status = +p.win.serverPlays;
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    /* Paused, the preview stays under a mouse that rests. */
    p.video.paused = true;
    const go = (x) => { point(p, 'pointermove', x); p.tick(300); };
    go(100);
    p.frames.answer(status);
    const out = { first: await bubbleState(p) };
    go(200);
    out.atOnce = p.frames.asked.length;
    p.tick(2500);
    out.after = p.frames.asked.length;
    if (p.frames.answer) p.frames.answer(status);
    await settle();
    p.tick(2500);
    out.longer = p.frames.asked.length;
    p.tick(2000);
    out.longerAfter = p.frames.asked.length;
    out.starts = p.starts().length;
    p.replies.push(answer('720p-4000', WITH_FRAMES));
    if (p.frames.answer) { p.frames.answer(200); await settle(); }
    const had = p.frames.asked.length;
    click(p.q('[data-quality="720p-4000"]'));
    await streamPlays(p, 30);
    go(500);
    out.newStream = p.frames.asked.length - had;
    return out;
  },

  /* Frames that are not there: by `serverPlays`, one, two, two with one that came between, or the
     last of the video. Which were asked for, and whether previews are still on. */
  async frame_missing(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 30);
    p.video.paused = true;
    /* Each place, and what is answered for it. */
    const places = {
      one: [[100, 404], [100, 200], [200, 200]],
      two: [[100, 404], [200, 404], [300, 200]],
      between: [[100, 404], [200, 200], [300, 404], [400, 200]],
      last: [[999, 404], [997, 404], [994, 404], [400, 200]],
    }[p.win.serverPlays];
    for (const [x, status] of places) {
      point(p, 'pointermove', x);
      p.tick(300);
      if (p.frames.answer) p.frames.answer(status);
      await settle();
    }
    return { asked: p.frames.asked, on: !p.q('[data-frame]').hidden, shown: (await bubbleState(p)).frame };
  },

  /* A stream without previews. */
  async frame_none(p) {
    p.replies[0] = answer('1080p-8000', { previews: null });
    await streamPlays(p, 30);
    point(p, 'pointermove', 100);
    p.tick(150);
    return Object.assign(await bubbleState(p), { asked: p.frames.asked });
  },

  /* Chapters and markers come with the stream, and a later one has none. */
  async chapters(p) {
    p.replies[0] = answer('1080p-8000', { chapters: CHAPTERS, markers: MARKERS });
    await streamPlays(p, 30);
    const menu = p.q('[data-chapter-menu]');
    const items = () => menu.querySelectorAll('[data-chapter]');
    const look = () => ({
      ticked: items().filter((b) => b.getAttribute('aria-checked') === 'true').map((b) => b.getAttribute('data-chapter')),
      name: p.q('[data-chapter-btn]').getAttribute('aria-label'),
      spoken: p.q('[data-seek]').getAttribute('aria-valuetext'),
    });
    const out = { hidden: p.q('[data-chapters]').hidden, rows: items().map((b) => b.textContent.trim()) };
    out.gaps = pick(p, 'player-gap', 'left');
    out.bands = pick(p, 'player-band', 'left').map((l, i) => [l, pick(p, 'player-band', 'width')[i]]);
    out.uncut = ['is-first', 'is-last'].map((c) => pick(p, 'player-band', 'left').filter((l, i) => p.q('[data-marks]').querySelectorAll('.player-band')[i].classList.contains(c)));
    fire(p.video, 'timeupdate');
    out.start = look();
    press(p.q('[data-chapter-btn]'));
    out.focus = doc.activeElement.getAttribute('data-chapter');
    press(items()[3]);
    out.at = p.video.currentTime;
    out.menuOpen = p.q('[data-chapter-btn]').getAttribute('aria-expanded');
    out.after = look();
    point(p, 'pointermove', 30);
    out.bubble = [p.q('[data-bubble-title]').textContent, p.q('[data-bubble-time]').textContent];
    out.stillSpoken = look().spoken;
    p.replies.push(answer('720p-4000', { chapters: [], markers: [] }));
    click(p.q('[data-quality="720p-4000"]'));
    await streamPlays(p, 1500);
    out.kept = [items().length, pick(p, 'player-band', 'left').length];
    return out;
  },

  /* A single chapter is no choice. */
  async chapters_one(p) {
    p.replies[0] = answer('1080p-8000', { chapters: CHAPTERS.slice(0, 1), markers: [] });
    await streamPlays(p, 30);
    point(p, 'pointermove', 30);
    return { hidden: p.q('[data-chapters]').hidden, gaps: pick(p, 'player-gap', 'left').length, title: p.q('[data-bubble-title]').hidden };
  },

  /* The skip button as the video plays into the intro, the credits and the last credits. */
  async skip(p) {
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 30);
    p.video.paused = false;
    p.q('[data-mute]').focus();
    const at = (t) => { p.video.currentTime = t; fire(p.video, 'timeupdate'); };
    const look = () => (gone(p.q('[data-skip]')) ? null : p.q('[data-skip-text]').textContent);
    const out = {};
    at(59);
    out.before = look();
    key('s');
    out.sBefore = p.video.currentTime;
    at(61);
    out.intro = look();
    out.focusKept = doc.activeElement === p.q('[data-mute]');
    p.tick(9000);
    out.quietEarly = p.q('[data-skip]').classList.contains('is-quiet');
    p.tick(2000);
    out.quiet = p.q('[data-skip]').classList.contains('is-quiet');
    out.plays = p.video.currentTime;
    key('s');
    out.sIntro = p.video.currentTime;
    out.introGone = look();
    at(149.5);
    out.introLastSecond = look();
    at(2410);
    out.credits = look();
    tabTo(p.q('[data-skip]'));
    key('Enter');
    out.creditsTo = p.video.currentTime;
    out.focusAfter = doc.activeElement === p.q('[data-play]');
    out.href = p.win.location.href;
    at(2710);
    out.last = look();
    press(p.q('[data-quality-btn]'));
    key('s');
    out.sInMenu = p.win.location.href;
    key('Escape');
    p.pages['/watch/play/502'] = { html: '<p>Not here</p>' };
    press(p.q('[data-skip]'));
    await settle();
    out.hrefAfter = p.win.location.href;
    p.leave();
    out.saved = p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.body.position_s);
    out.beacons = p.beacons.length;
    p.replies.push(answer('1080p-8000'));
    p.comeBack();
    await settle();
    out.backAt = p.hlsList[p.hlsList.length - 1].config.startPosition;
    return out;
  },

  /* The last credits of a video that nothing follows. */
  async skip_last(p) {
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 2710);
    fire(p.video, 'timeupdate');
    const label = p.q('[data-skip-text]').textContent;
    press(p.q('[data-skip]'));
    return { label: label, at: p.video.currentTime, hidden: gone(p.q('[data-skip]')), href: p.win.location.href };
  },

  /* An error over the intro. */
  async skip_error(p) {
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 70);
    fire(p.video, 'timeupdate');
    p.replies.push({ err: { status: 404, error: 'not_found' } });
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    const shown = !gone(p.q('[data-skip]'));
    await settle();
    return { shown: shown, underError: !gone(p.q('[data-skip]')) };
  },

  /* The buffer runs dry with no frame held. */
  async stall(p) {
    await streamPlays(p, 30);
    fire(p.video, 'waiting');
    return { loading: p.root.classList.contains('is-loading'), held: p.root.classList.contains('is-held') };
  },

  /* The server refuses the new quality: the one that played comes back. */
  async quality_refused(p) {
    await streamPlays(p, 30);
    p.replies.push({ err: { status: 502 } }, { err: { status: 502 } }, { ok: { stream_id: 's3', playlist: '/p3' } });
    click(p.q('[data-quality="low"]'));
    await settle();
    const beforePlaying = { hold: holdState(p), qualities: p.calls.filter((c) => /stream$/.test(c.url)).map((c) => c.body.quality) };
    await streamPlays(p, 30);
    return {
      beforePlaying: beforePlaying,
      afterPlaying: { hold: holdState(p) },
      checked: p.q('[data-quality][aria-checked="true"]').getAttribute('data-quality'),
      saved: p.storage['mediaman.watch.quality'],
      toasts: p.toasts,
    };
  },

  /* The episode ends, the next-episode card counts down, Cancel dismisses it. */
  async upnext_cancel(p) {
    await streamPlays(p, 0);
    p.video.paused = true; p.video.ended = true;
    fire(p.video, 'ended');
    const card = p.q('[data-upnext]');
    const fill = p.q('[data-upnext-fill]');
    const shown = { hidden: card.hidden, fill: fill ? fill.style.animationDuration || null : null };
    p.q('[data-upnext-cancel]').focus();
    click(p.q('[data-upnext-cancel]'));
    const afterClick = { hidden: card.hidden, leaving: card.classList.contains('is-leaving') };
    card.dropped();
    const afterExit = { hidden: card.hidden, leaving: card.classList.contains('is-leaving'), focus: doc.activeElement.getAttribute('data-play') !== null };
    p.tick(20000);
    return { shown: shown, afterClick: afterClick, afterExit: afterExit, href: p.win.location.href };
  },

  /* A menu, the skip button, the frame preview and the error card as they go. */
  async exits(p) {
    const out = {};
    const going = (name, sel, at) => {
      const el = p.q(sel);
      const look = () => ({ hidden: el.hidden, leaving: el.classList.contains('is-leaving'), inert: !!el.inert });
      out[name] = { before: look() };
      at();
      out[name].going = look();
      /* An animation of its own that the exit cut short says nothing. */
      fire(el, 'animationcancel', { bubbles: true, animationName: 'player-pop' });
      out[name].still = !el.hidden;
      el.dropped();
      out[name].after = look();
    };
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 70);
    fire(p.video, 'timeupdate');
    going('skip', '[data-skip]', () => key('s'));
    point(p, 'pointermove', 100);
    going('bubble', '[data-bubble]', () => point(p, 'pointerleave', 100));
    press(p.q('[data-quality-btn]'));
    going('menu', '[data-quality-menu]', () => key('Escape'));
    out.focus = doc.activeElement === p.q('[data-quality-btn]');
    press(p.q('[data-quality-btn]'));
    key('Escape');
    press(p.q('[data-quality-btn]'));
    out.back = { hidden: p.q('[data-quality-menu]').hidden, leaving: p.q('[data-quality-menu]').classList.contains('is-leaving'), inert: p.q('[data-quality-menu]').inert };
    key('Escape');
    p.replies.push({ err: { status: 429, error: 'rate_limited' } });
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    await settle();
    p.replies.push(answer('1080p-8000'));
    going('message', '[data-message]', () => press(p.q('[data-retry]')));
    return out;
  },

  /* What goes is shown again before its exit has played. */
  async shown_again(p) {
    const out = {};
    const look = (sel) => { const el = p.q(sel); return { hidden: el.hidden, leaving: el.classList.contains('is-leaving'), inert: !!el.inert }; };
    const again = async (name, sel, leave, show) => {
      leave();
      const leaving = look(sel).leaving;
      show();
      await settle();
      out[name] = Object.assign({ left: leaving }, look(sel));
    };
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 70);
    const at = (t) => () => { p.video.currentTime = t; fire(p.video, 'timeupdate'); };
    at(70)();
    await again('skip', '[data-skip]', at(200), at(71));
    point(p, 'pointermove', 100);
    await again('bubble', '[data-bubble]', () => point(p, 'pointerleave', 100), () => point(p, 'pointermove', 110));
    press(p.q('[data-quality-btn]'));
    await again('menu', '[data-quality-menu]', () => key('Escape'), () => press(p.q('[data-quality-btn]')));
    key('Escape');
    endsNow(p);
    await again('card', '[data-upnext]', () => press(p.q('[data-upnext-cancel]')), () => endsNow(p));
    press(p.q('[data-upnext-cancel]'));
    const refused = { err: { status: 429, error: 'rate_limited' } };
    p.replies.push(refused);
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    await settle();
    p.replies.push(refused);
    await again('message', '[data-message]', () => press(p.q('[data-retry]')), () => {});
    return out;
  },

  /* The next episode takes over: the title says it as it is asked for, and not again. */
  async next_said(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 100);
    const slot = p.q('[data-slot="titles"]');
    const said = [];
    const add = slot.appendChild;
    const sub = p.q('.player-subtitle');
    slot.appendChild = function (n) { said.push(n.textContent.trim()); return add.call(this, n); };
    sub.appendChild = function (n) { said.push(n.textContent); return Node.prototype.appendChild.call(this, n); };
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await settle();
    await streamPlays(p, 0);
    return { said: said.filter(Boolean), item: p.calls.filter((c) => /\/stream$/.test(c.url)).pop().body.rating_key, shown: slot.textContent };
  },

  /* What each way of saying "stop" does while the next-episode card counts down. */
  async upnext_stops(p) {
    const ways = {
      click: () => press(p.video),
      touch: () => press(p.video, 'touch'),
      space: () => key(' '),
      escape: () => key('Escape'),
      button: () => press(p.q('[data-play]')),
    };
    await streamPlays(p, 0);
    const out = {};
    for (const way of Object.keys(ways)) {
      p.video.paused = true; p.video.ended = true;
      fire(p.video, 'ended');
      const counting = playState(p).label;
      const plays = p.video.plays;
      ways[way]();
      out[way] = {
        counting: counting,
        replayed: p.video.plays - plays,
        leaving: p.q('[data-upnext]').classList.contains('is-leaving'),
        after: playState(p).label,
      };
      doc.activeElement = doc.body;
    }
    p.tick(20000);
    out.href = p.win.location.href;
    /* The countdown is over: Play plays again, the picture does not. */
    const plays = p.video.plays;
    press(p.video);
    out.pictureReplays = p.video.plays - plays;
    press(p.q('[data-play]'));
    out.buttonReplays = p.video.plays - plays;
    return out;
  },

  /* Space after the mouse pressed a control, and after Tab reached one. */
  async space(p) {
    await streamPlays(p, 30);
    press(p.q('[data-fwd10]'));
    p.tick(300);
    const skipped = p.video.currentTime;
    key(' ');
    const afterMouse = { skippedOnce: p.video.currentTime === skipped, paused: p.video.paused };
    p.video.play();
    press(p.q('[data-quality-btn]'));
    press(p.q('[data-quality="best"]'));
    key(' ');
    const afterMenu = { paused: p.video.paused, menuClosed: p.q('[data-quality-btn]').getAttribute('aria-expanded') === 'false' };
    p.video.play();
    press(p.q('[data-volume]'));
    key(' ');
    const afterSlider = { paused: p.video.paused };
    p.video.play();
    tabTo(p.q('[data-fwd10]'));
    const before = p.video.currentTime;
    key(' ');
    p.tick(300);
    return { afterMouse: afterMouse, afterMenu: afterMenu, afterSlider: afterSlider, afterTab: { skipped: p.video.currentTime - before, paused: p.video.paused } };
  },

  /* Do the controls hide: after a mouse press and a key, and after Tab reached one. */
  async idle(p) {
    const idle = () => p.root.classList.contains('is-idle');
    await streamPlays(p, 30);
    press(p.q('[data-mute]'));
    key('ArrowRight');
    p.tick(4000);
    const afterMouse = idle();
    tabTo(p.q('[data-mute]'));
    p.tick(4000);
    const afterTab = idle();
    press(p.q('[data-mute]'));
    tabTo(p.q('[data-back10]'), { altKey: true });
    p.tick(4000);
    return { afterMouse: afterMouse, afterTab: afterTab, afterOptionTab: idle() };
  },

  /* A seek before the playlist has come, and one after it but before any video. */
  async early_seek(p) {
    await settle();
    const h = p.hlsList[0];
    key('ArrowRight');
    key('ArrowRight');
    h.emit('hlsManifestParsed');
    const whilePlaylistLoads = h.startsAt;
    key('ArrowRight');
    p.tick(300);
    return { whilePlaylistLoads: whilePlaylistLoads, beforeVideo: h.startsAt };
  },

  /* A press on the picture while a menu is open, then another. */
  async click_away(p) {
    const open = () => p.q('[data-quality-btn]').getAttribute('aria-expanded') === 'true';
    await streamPlays(p, 30);
    p.video.paused = false;
    const out = {};
    for (const type of ['mouse', 'touch']) {
      press(p.q('[data-quality-btn]'));
      const wasOpen = open();
      press(p.video, type);
      out[type] = { wasOpen: wasOpen, open: open(), paused: p.video.paused, idle: p.root.classList.contains('is-idle') };
    }
    press(p.video);
    out.next = { paused: p.video.paused };
    return out;
  },

  /* Play under an error that a new try can mend. */
  async error_retry(p) {
    p.hlsList.length = 0;
    await streamPlays(p, 30);
    const starts = () => p.calls.filter((c) => /stream$/.test(c.url)).length;
    const breakIt = () => {
      const last = () => p.hlsList[p.hlsList.length - 1];
      last().emit('hlsError', { fatal: true, type: 'networkError' });
    };
    const fail = async () => {
      breakIt();
      await settle();
      breakIt();
      await settle();
    };
    p.replies.push({ ok: { stream_id: 's2', playlist: '/p2' } });
    await fail();
    const shown = !gone(p.q('[data-message]'));
    const before = starts();
    p.replies.push({ ok: { stream_id: 's3', playlist: '/p3' } });
    press(p.q('[data-play]'));
    await settle();
    const afterPlay = { started: starts() - before, shown: !gone(p.q('[data-message]')), busy: playState(p).busy };
    p.replies.push({ ok: { stream_id: 's4', playlist: '/p4' } });
    await streamPlays(p, 30);
    await fail();
    const retryFocused = doc.activeElement === p.q('[data-retry]');
    p.replies.push({ ok: { stream_id: 's5', playlist: '/p5' } });
    key('Enter');
    await settle();
    return { shown: shown, afterPlay: afterPlay, retryFocused: retryFocused, focusOnPlay: doc.activeElement === p.q('[data-play]') };
  },

  /* An error no new try can mend, seen on a saved quality so nothing reverts. */
  async error_final(p) {
    p.replies.push({ err: { status: 404, error: 'not_found' } });
    await streamPlays(p, 30);
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    await settle();
    return { text: p.q('[data-message-text]').textContent, retry: !p.q('[data-retry]').hidden, playDisabled: p.q('[data-play]').disabled === true, focusOnBack: doc.activeElement === p.q('[data-message]').querySelector('a') };
  },

  /* The volume the page starts with, and what is kept when it changes. */
  async volume(p) {
    const start = { volume: p.video.volume, muted: p.video.muted, mute: p.q('[data-mute]').getAttribute('aria-label') };
    const fill = () => p.q('[data-volume]').style['--fill'];
    start.fill = fill();
    const startText = p.q('[data-volume]').getAttribute('aria-valuetext');
    p.video.muted = false; p.video.volume = 0.25;
    fire(p.video, 'volumechange');
    const unmutedText = p.q('[data-volume]').getAttribute('aria-valuetext');
    const unmuted = Object.assign({}, p.storage);
    const unmutedFill = fill();
    press(p.q('[data-mute]'));
    fire(p.video, 'volumechange');
    const text = () => p.q('[data-volume]').getAttribute('aria-valuetext');
    return { start: start, unmuted: unmuted, unmutedFill: unmutedFill, muted: Object.assign({}, p.storage), mutedFill: fill(), startText: startText, unmutedText: unmutedText, mutedText: text() };
  },

  /* The clock and the bar when the file is shorter than the length Plex gave (3000 s). */
  async ended_clock(p) {
    await streamPlays(p, 100);
    p.video.duration = 2990;
    p.video.currentTime = 2990;
    const look = () => ({ remaining: p.q('[data-remaining]').textContent, left: p.q('[data-played]').style.width });
    fire(p.video, 'timeupdate');
    const playing = look();
    p.video.paused = true; p.video.ended = true;
    fire(p.video, 'ended');
    return { playing: playing, ended: look() };
  },

  /* The keys of a slider on the seek bar. */
  async seek_keys(p) {
    await streamPlays(p, 600);
    tabTo(p.q('[data-seek]'));
    const out = {};
    for (const k of ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown']) {
      p.video.currentTime = 600;
      key(k);
      p.tick(300);
      out[k] = p.video.currentTime - 600;
    }
    return out;
  },

  /* A key on the seek bar shows for a moment where it led; one elsewhere does not. */
  async seek_key_bubble(p) {
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 600);
    const out = {};
    key('ArrowRight');
    out.elsewhere = (await bubbleState(p)).shown;
    tabTo(p.q('[data-seek]'));
    for (const k of ['ArrowRight', 'PageUp']) {
      key(k);
      p.frames.answer(200);
      out[k] = await bubbleState(p);
      p.tick(900);
      out[k + 'Still'] = (await bubbleState(p)).shown;
    }
    out.marker = !p.q('[data-seek-hover]').hidden;
    p.tick(200);
    out.gone = !(await bubbleState(p)).shown;
    /* The mouse that comes to the bar keeps it. */
    key('Home');
    point(p, 'pointermove', 500);
    p.tick(1100);
    out.underMouse = (await bubbleState(p)).time;
    return out;
  },

  /* A double click on the picture: while it plays, with a menu open, and under an error. */
  async double_click(p) {
    p.root.requestFullscreen = () => Promise.resolve();
    await streamPlays(p, 30);
    p.video.paused = false;
    const out = {};
    doubleClick(p.video);
    out.playing = p.video.paused;
    press(p.q('[data-quality-btn]'));
    doubleClick(p.video);
    out.menu = { open: p.q('[data-quality-btn]').getAttribute('aria-expanded'), paused: p.video.paused };
    press(p.video);
    out.paused = p.video.paused;
    /* The first press on a button that hides, the second on the picture where it was. */
    press(p.q('[data-skip]'), 'mouse', 1);
    press(p.video, 'mouse', 2);
    out.stillPaused = p.video.paused;
    press(p.video);
    p.replies.push({ err: { status: 429, error: 'rate_limited' } });
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    await settle();
    out.error = !gone(p.q('[data-message]'));
    const starts = p.starts().length;
    press(p.video);
    doubleClick(p.video);
    await settle();
    out.retries = p.starts().length - starts;
    p.replies.push(answer('best'));
    press(p.q('[data-retry]'));
    out.wantsToPlay = playState(p);
    return out;
  },

  /* A seek while a new quality starts, which the server then refuses. */
  async seek_then_refused(p) {
    await streamPlays(p, 30);
    p.replies.push({ err: { status: 429, error: 'rate_limited' } }, answer('best'));
    click(p.q('[data-quality="low"]'));
    key('ArrowRight');
    key('ArrowRight');
    await settle();
    return { starts: p.starts(), at: p.hlsList[p.hlsList.length - 1].config.startPosition };
  },

  /* The end of an episode leads, after the countdown, to the next in this page. */
  async next_in_place(p) {
    p.replies[0] = answer('1080p-8000', { markers: MARKERS, chapters: CHAPTERS });
    await streamPlays(p, 2900);
    const video = p.video;
    video.volume = 0.4;
    press(p.q('[data-chapter-btn]'));
    playOn(p, 20);
    p.tick(1000);
    endsNow(p);
    let reply;
    p.replies.push({ ok: new Promise((r) => { reply = r; }) });
    const from = p.calls.length;
    const gone = p.hlsList.length;
    p.tick(9000);
    const early = p.calls.length - from;
    p.tick(1000);
    const held = holdState(p);
    await settle();
    const order = p.calls.slice(from).map((c) => c.url);
    const before = { chapters: p.q('[data-chapters]').hidden, marks: p.q('[data-marks]').childNodes.length, menus: p.root.querySelectorAll('[aria-expanded="true"]').length };
    const stop = p.calls.slice(from).filter((c) => /\/stop$/.test(c.url))[0];
    const start = p.calls.filter((c) => /\/stream$/.test(c.url)).pop();
    reply(answer('1080p-8000').ok);
    await streamPlays(p, 0);
    playOn(p, 3);
    p.tick(20000);
    const saves = p.calls.slice(from).filter((c) => /\/progress$/.test(c.url)).map((c) => c.body);
    return {
      early: early, order: order, stop: stop.body, start: start.body, held: held,
      href: p.win.location.href, urls: p.win.history.urls, title: doc.title,
      heading: p.q('.player-title').textContent, subtitle: p.q('.player-subtitle').textContent,
      media: p.win.navigator.mediaSession.metadata,
      nextHref: p.q('[data-next-btn]').getAttribute('href'),
      max: p.q('[data-seek]').getAttribute('aria-valuemax'),
      back: p.q('.player-back').getAttribute('href'),
      before: before,
      card: p.q('[data-upnext]').hidden, keep: !!p.q('[data-keep]'),
      oldDestroyed: p.hlsList[gone - 1].destroyed, sameVideo: p.q('video') === video, volume: video.volume,
      saves: saves, left: holdState(p).shown,
    };
  },

  /* The other ways on: the Next link and the card's button take the same path. */
  async next_by_hand(p) {
    const out = {};
    await streamPlays(p, 100);
    const link = p.q('[data-next-btn]');
    fire(link, 'click', { bubbles: true, metaKey: true, preventDefault() { out.newTabKept = false; } });
    out.newTabKept = out.newTabKept !== false && !p.calls.some((c) => c.page);
    tabTo(link);
    fire(link, 'click', { bubbles: true, preventDefault() { out.stays = true; } });
    click(link);
    p.replies.push(answer('1080p-8000'));
    await settle();
    out.first = { pages: p.calls.filter((c) => c.page).map((c) => c.url), focus: doc.activeElement.getAttribute('href'), urls: p.win.history.urls.slice() };
    await streamPlays(p, 0);
    endsNow(p);
    press(p.q('[data-upnext-play]'));
    out.cardLeaves = p.q('[data-upnext]').classList.contains('is-leaving');
    p.replies.push(answer('1080p-8000'));
    await settle();
    out.urls = p.win.history.urls;
    out.keep = !!p.q('[data-keep]');
    press(p.q('[data-keep]').querySelector('[data-menu-trigger]'));
    press(p.q('[data-keep-days="30"]'));
    await settle();
    out.kept = p.calls.filter((c) => /\/keep$/.test(c.url)).map((c) => c.body);
    return out;
  },

  /* The page says while Keep's menu is open, for the toasts to make way, and not for another menu. */
  async keep_menu_marks_page(p) {
    p.held.keep = JSON.parse(p.win.serverPlays);
    await streamPlays(p, 0);
    const marked = () => doc.documentElement.classList.contains('has-player-keep-open');
    const keep = p.q('[data-keep]').querySelector('[data-menu-trigger]');
    press(keep);
    const open = marked();
    press(p.q('[data-quality-btn]'));
    const otherMenu = marked();
    press(keep);
    const reopened = marked();
    press(keep);
    return { open, otherMenu, reopened, closed: marked() };
  },

  /* What the toast of a keep offers, by what the server says of undoing it. */
  async keep_offers(p) {
    p.held.keep = JSON.parse(p.win.serverPlays);
    await streamPlays(p, 0);
    press(p.q('[data-keep]').querySelector('[data-menu-trigger]'));
    press(p.q('[data-keep-days="7"]'));
    await settle();
    const toast = p.toasts[0];
    return { pill: p.q('[data-keep]').textContent.trim(), said: toast.text.split(' ').slice(0, 2).join(' '), undo: !!toast.action };
  },

  /* A title is kept, and the keep undone: here, or (`serverPlays`) once the next episode took over. */
  async keep_undone(p) {
    p.pages['/watch/play/502'] = p.pages['/watch/play/503'];
    await streamPlays(p, 0);
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await settle();
    await streamPlays(p, 0);
    const trigger = p.q('[data-keep]').querySelector('[data-menu-trigger]');
    press(trigger);
    press(p.q('[data-keep-days="7"]'));
    await settle();
    const said = [p.q('[data-keep]').textContent.trim(), p.toasts[0].text];
    if (p.win.serverPlays === 'next') {
      p.replies.push(answer('1080p-8000'));
      p.pages['/watch/play/504'] = p.pages['/plain'];
      click(p.q('[data-next-btn]'));
      await settle();
      await streamPlays(p, 0);
    }
    /* The toast, and the focus with it, goes as its button is pressed. */
    doc.activeElement = doc.body;
    p.toasts.filter((t) => t.action)[0].action.onClick();
    await settle();
    return {
      said: said,
      focus: doc.activeElement === trigger ? 'keep' : doc.activeElement === p.q('[data-play]') ? 'play' : 'nothing',
      undone: p.calls.filter((c) => /\/unkeep$/.test(c.url)).map((c) => c.body.media_item_id),
      toasts: p.toasts.map((t) => t.text),
    };
  },

  /* The browser cannot go on with the picture, and hls.js gets over it; by `serverPlays` the person
     had paused, or moves on meanwhile. */
  async media_error(p) {
    const v = p.video;
    await streamPlays(p, 100);
    playOn(p, 20);
    if (p.win.serverPlays === 'paused') v.pause();
    await settle();
    const from = p.calls.length;
    p.hlsList[0].emit('hlsError', { fatal: true, type: 'mediaError' });
    const out = { held: holdState(p).shown };
    p.tick(20000);
    if (p.win.serverPlays === 'seeks') key('ArrowRight');
    out.saved = p.calls.slice(from).filter((c) => /\/progress$/.test(c.url)).map((c) => c.body.position_s);
    v.readyState = 1;
    fire(v, 'loadedmetadata');
    out.at = v.currentTime;
    out.paused = v.paused;
    v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720;
    fire(v, v.paused ? 'canplay' : 'playing');
    present(v);
    out.heldAfter = holdState(p).shown;
    out.starts = p.starts().length;
    p.leave();
    out.left = p.beacons.map((b) => b.body && b.body.position_s);
    return out;
  },

  /* The picture of the next episode: by `serverPlays` its still, the backdrop in its place, or none. */
  async still(p) {
    const img = p.q('[data-upnext]').querySelector('[data-still]');
    const look = (el) => (el.hidden ? null : el.getAttribute('src'));
    const out = { before: look(img) };
    await streamPlays(p, 0);
    if (p.win.serverPlays !== 'still') {
      fire(img, 'error');
      out.asked = img.getAttribute('src');
      out.between = look(img);
    }
    fire(img, p.win.serverPlays === 'none' ? 'error' : 'load');
    out.card = look(img);
    p.sleep(7200000);
    endsNow(p);
    p.sleep(11000);
    p.tick(250);
    out.ask = look(p.q('[data-ask]').querySelector('[data-still]'));
    return out;
  },

  /* A page that cannot take over here is opened instead. */
  async next_falls_back(p) {
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = { redirect: { html: '<div id="player"></div>', redirected: true }, other: { html: '<p>Not here</p>' } }[p.win.serverPlays];
    click(p.q('[data-next-btn]'));
    await settle();
    const starts = p.starts().length;
    p.leave();
    return { href: p.win.location.href, starts: starts, stops: p.calls.filter((c) => /\/stop$/.test(c.url)).length, beacons: p.beacons.length };
  },

  /* The next episode's page cannot be had, and then can; or its page was opened and never came. */
  async next_unreached(p) {
    const good = p.pages['/watch/play/502'];
    const message = () => (gone(p.q('[data-message]')) ? null : { retry: !p.q('[data-retry]').hidden, focus: doc.activeElement === p.q('[data-retry]') });
    const out = {};
    await streamPlays(p, 0);
    p.pages['/watch/play/502'] = { offline: 'offline', hangs: 'hangs', refused: { status: 502, html: '<p>Bad gateway</p>' }, unopened: { html: '<p>Not here</p>' }, plex_down: { html: '<div id="watch-page" data-plex-unavailable></div>' } }[p.win.serverPlays];
    p.video.webkitPresentationMode = 'fullscreen';
    p.video.webkitSetPresentationMode = (m) => { p.video.webkitPresentationMode = m; };
    endsNow(p);
    p.tick(10000);
    for (let i = 0; i < 200 && !message(); i++) { await settle(); p.tick(500); }
    await settle();
    out.message = message();
    out.text = p.q('[data-message-text]').textContent;
    out.mode = p.video.webkitPresentationMode;
    out.loading = p.root.classList.contains('is-loading');
    out.href = p.win.location.href;
    out.goesOn = p.sitting().goes_on;
    p.pages['/watch/play/502'] = good;
    const starts = p.starts().length;
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    out.trying = message();
    await settle();
    await streamPlays(p, 0);
    out.went = { starts: p.starts().length - starts, urls: p.win.history.urls, key: p.calls.filter((c) => /\/stream$/.test(c.url)).pop().body.rating_key, message: message() };
    return out;
  },

  /* The page opened in place of the next episode's never comes; then another quality of this one. */
  async next_never_opens(p) {
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = { html: '<p>Not here</p>' };
    press(p.q('[data-next-btn]'));
    await settle();
    p.tick(15000);
    press(p.q('[data-quality-btn]'));
    press(p.q('[data-quality="low"]'));
    await settle();
    return { start: p.calls.filter((c) => /\/stream$/.test(c.url)).pop().body };
  },

  /* What says at once that the next episode was asked for, and what is left of it when it cannot
     be had, the page is left for it, or another quality is picked. */
  async next_at_once(p) {
    const look = () => ({ subtitle: p.q('.player-subtitle').textContent, switching: p.root.classList.contains('is-switching'), held: holdState(p).shown });
    const out = {};
    await streamPlays(p, 100);
    const went = p.pages['/watch/play/502'];
    p.pages['/watch/play/502'] = 'offline';
    click(p.q('[data-next-btn]'));
    out.asked = look();
    for (let i = 0; i < 130; i++) { await settle(); p.tick(500); }
    out.unreached = look();
    p.pages['/watch/play/502'] = went;
    click(p.q('[data-next-btn]'));
    p.leave();
    out.left = look().subtitle;
    p.replies.push(answer('1080p-8000'));
    p.comeBack();
    await streamPlays(p, 100);
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await streamPlays(p, 0);
    out.there = look();
    p.replies.push(answer('480p-1500'));
    click(p.q('[data-quality="480p-1500"]'));
    out.quality = look();
    return out;
  },

  /* The next episode is no longer there. */
  async next_gone(p) {
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = { status: 404, html: '' };
    press(p.q('[data-next-btn]'));
    await settle();
    return { message: !gone(p.q('[data-message]')), retry: !p.q('[data-retry]').hidden, back: doc.activeElement.hasAttribute('data-back') };
  },

  /* The connection is gone as the next episode is asked for, and back for the try again. */
  async next_unsaved(p) {
    const out = {};
    const stops = () => p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => [c.url, c.body && c.body.position_s]);
    const went = p.pages['/watch/play/502'];
    await streamPlays(p, 100);
    playOn(p, 20);
    p.held.stopFails = true;
    p.pages['/watch/play/502'] = 'offline';
    click(p.q('[data-next-btn]'));
    await settle();
    out.failed = stops();
    for (let i = 0; i < 130 && p.win.serverPlays !== 'leaves'; i++) { await settle(); p.tick(500); }
    if (p.win.serverPlays === 'leaves') {
      p.leave();
      return { failed: out.failed, left: p.beacons.map((b) => [b.url, b.body && b.body.position_s]) };
    }
    p.held.stopFails = false;
    p.pages['/watch/play/502'] = went;
    const from = p.calls.length;
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-retry]'));
    await settle();
    out.retried = p.calls.slice(from).map((c) => c.url);
    out.body = stops().pop();
    await streamPlays(p, 0);
    const had = stops().length;
    p.leave();
    out.left = p.beacons.map((b) => b.url);
    out.again = stops().length - had;
    return out;
  },

  /* The stop of a stream that failed is not taken at first; by the try again it is. */
  async fail_resent(p) {
    await streamPlays(p, 100);
    playOn(p, 20);
    p.held.stopFails = true;
    p.held.answer = () => answer('1080p-8000');
    while (gone(p.q('[data-message]'))) {
      p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
      await settle();
    }
    const missed = p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.url);
    p.held.stopFails = false;
    const from = p.calls.length;
    press(p.q('[data-retry]'));
    await settle();
    const tried = p.calls.slice(from).map((c) => c.url);
    await streamPlays(p, 120);
    p.leave();
    return { missed: missed, tried: tried, left: p.beacons.map((b) => b.url) };
  },

  /* The stop of a stream that failed is never taken; the video is tried again and plays on. */
  async fail_unsaved(p) {
    await streamPlays(p, 100);
    playOn(p, 20);
    p.held.stopFails = true;
    p.held.answer = () => answer('1080p-8000');
    while (gone(p.q('[data-message]'))) {
      p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
      await settle();
    }
    press(p.q('[data-retry]'));
    await settle();
    await streamPlays(p, 120);
    playOn(p, 300);
    const from = p.beacons.length;
    p.leave();
    return p.beacons.slice(from).map((b) => b.body && b.body.position_s);
  },

  /* The old stream counts against the limit until the server has stopped it. */
  async next_waits(p) {
    await streamPlays(p, 100);
    p.held.stop = [];
    click(p.q('[data-next-btn]'));
    await settle();
    const before = p.starts().length;
    key(' ');
    const paused = playState(p).label;
    press(p.q('[data-quality-btn]'));
    press(p.q('[data-quality="low"]'));
    await settle();
    const whileWaiting = p.starts().length;
    p.replies.push(answer('480p-1500'));
    p.held.stop.forEach((r) => r({}));
    await settle();
    const after = p.starts();
    await streamPlays(p, 0);
    click(p.q('[data-next-btn]'));
    await settle();
    p.tick(5000);
    await settle();
    return { before: before, paused: paused, whileWaiting: whileWaiting, after: after, plays: p.video.plays, last: p.starts().length };
  },

  /* The pointer is on the bar, or holds it, as the next episode takes over. */
  async next_over_bar(p) {
    const out = {};
    const go = async () => {
      p.replies.push(answer('1080p-8000', WITH_FRAMES));
      click(p.q('[data-next-btn]'));
      await settle();
    };
    p.replies[0] = answer('1080p-8000', WITH_FRAMES);
    await streamPlays(p, 0);
    point(p, 'pointermove', 2);
    p.tick(100);
    const late = p.frames.answer;
    await go();
    out.bubble = gone(p.q('[data-bubble]'));
    late(200);
    await settle();
    await streamPlays(p, 0);
    point(p, 'pointermove', 2);
    p.tick(300);
    out.asked = p.frames.asked.length;
    point(p, 'pointerleave', 100);
    point(p, 'pointerdown', 500);
    await go();
    point(p, 'pointerup', 500);
    const h = p.hlsList[p.hlsList.length - 1];
    out.startsAt = [h.config.startPosition, h.startsAt];
    return out;
  },

  /* Automatic has stepped down by the time the next episode takes over. */
  async next_stepped(p) {
    p.replies[0] = answer('1080p-8000');
    await streamPlays(p, 0);
    p.replies.push(answer('720p-4000'));
    playOn(p, 5);
    fire(p.video, 'waiting');
    fire(p.video, 'playing');
    playOn(p, 5);
    fire(p.video, 'waiting');
    await settle();
    await streamPlays(p, 10);
    click(p.q('[data-next-btn]'));
    p.replies.push(answer('720p-4000'));
    await settle();
    /* A failure restarts the same, not what Automatic would begin with. */
    p.replies.push(answer('720p-4000'));
    p.hlsList[p.hlsList.length - 1].emit('hlsError', { fatal: true, type: 'networkError' });
    await streamPlays(p, 0);
    const menu = menuState(p);
    p.replies.push(answer('1080p-8000'));
    playOn(p, 310);
    await settle();
    return { starts: p.starts(), ticked: menu.ticked, pill: menu.pill };
  },

  /* The original of one episode cannot play here; the next may. */
  async next_original(p) {
    p.replies[0] = answer('original');
    await settle();
    p.replies.push(answer('1080p-10000'));
    p.hlsList[0].emit('hlsError', { fatal: true, type: 'mediaError' });
    await streamPlays(p, 0);
    click(p.q('[data-next-btn]'));
    await settle();
    return { starts: p.starts() };
  },

  /* On to the next episode from an error. */
  async next_after_error(p) {
    p.replies[0] = { err: { status: 404, error: 'not_found' } };
    await settle();
    const error = !gone(p.q('[data-message]'));
    click(p.q('[data-next-btn]'));
    return { error: error, after: !gone(p.q('[data-message]')) };
  },

  /* The page is left while the next episode is on its way. */
  async next_left(p) {
    await streamPlays(p, 100);
    p.pages['/watch/play/502'] = { redirected: true, html: '' };
    click(p.q('[data-next-btn]'));
    p.leave();
    await settle();
    const left = p.win.location.href;
    delete p.pages['/watch/play/502'];
    const starts = p.starts().length;
    p.comeBack();
    await settle();
    const back = p.calls.filter((c) => /\/stream$/.test(c.url)).pop().body.rating_key;
    click(p.q('[data-next-btn]'));
    await settle();
    return { left: left, starts: starts, back: back, again: p.calls.filter((c) => c.page).length, href: p.win.location.href };
  },

  /* Timers run late in a hidden tab: the countdown goes by the clock. */
  async countdown_clock(p) {
    await streamPlays(p, 100);
    endsNow(p);
    p.sleep(9000);
    p.tick(250);
    const left = p.q('[data-countdown]').textContent;
    p.sleep(5000);
    p.tick(250);
    await settle();
    return { left: left, went: p.calls.some((c) => c.page) };
  },

  /* Episode after episode with nobody there. */
  async ask(p) {
    const out = { asked: [] };
    const ask = p.q('[data-ask]');
    const auto = async (hook) => {
      await streamPlays(p, 0);
      if (hook) hook();
      endsNow(p);
      p.replies.push(answer('1080p-8000', { markers: MARKERS }));
      p.tick(10000);
      await settle();
      out.asked.push(ask.open);
    };
    const modes = [];
    let fullscreens = 0;
    p.root.requestFullscreen = () => { fullscreens++; return Promise.resolve(); };
    p.video.webkitSetPresentationMode = (m) => { p.video.webkitPresentationMode = m; modes.push(m + (ask.open ? ' late' : '')); };
    const signs = [
      () => key('Shift'),
      () => press(p.q('[data-elapsed]')),
      () => p.video.pause(),
      () => { p.video.paused = true; p.video.play(); },
      () => fire(p.video, 'volumechange'),
      () => { playOn(p, 2); p.video.currentTime = 500; fire(p.video, 'seeking'); },
    ];
    for (const sign of signs) {
      await auto(sign);
      await auto();
      await auto();
    }
    p.video.webkitPresentationMode = 'picture-in-picture';
    const stops = p.calls.filter((c) => /\/stop$/.test(c.url)).length;
    const starts = p.starts().length;
    await auto();
    /* The video is gone and no longer ended; its place is still in the last credits. */
    p.video.ended = false;
    fire(p.video, 'progress');
    out.skip = gone(p.q('[data-skip]'));
    out.urls = p.win.history.urls.length;
    out.stopped = p.calls.filter((c) => /\/stop$/.test(c.url)).length - stops;
    out.started = p.starts().length - starts;
    out.modes = modes;
    out.line = p.q('[data-ask-line]').textContent;
    key('ArrowRight');
    key('f');
    out.fullscreens = fullscreens;
    out.focus = doc.activeElement.hasAttribute('data-ask-continue');
    out.tabs = [1, 2, 3].map(() => {
      key('Tab');
      const at = doc.activeElement;
      return at.hasAttribute('data-ask-back') ? 'back' : at.hasAttribute('data-ask-continue') ? 'continue' : at.tagName;
    });
    out.heldBehind = holdState(p).shown;
    p.leave();
    out.beacons = p.beacons.length;
    p.comeBack();
    await settle();
    out.startedByBack = p.starts().length - starts - out.started;
    press(p.q('[data-ask-continue]'));
    await settle();
    out.open = ask.open;
    out.cardBehind = !p.q('[data-upnext]').hidden && !p.q('[data-upnext]').classList.contains('is-leaving');
    out.urlsAfter = p.win.history.urls.length;
    await auto();
    return out;
  },

  /* Nobody there for two hours, in one episode. */
  async ask_hours(p) {
    const out = {};
    await streamPlays(p, 0);
    p.sleep(7190000);
    endsNow(p);
    out.atEnd = p.q('[data-ask]').open;
    p.sleep(11000);
    p.tick(250);
    out.afterCountdown = p.q('[data-ask]').open;
    let prevented = 0;
    fire(p.q('[data-ask]'), 'cancel', { preventDefault() { prevented++; } });
    out.href = p.win.location.href;
    return out;
  },

  /* Nobody there for two hours, with a stream that dropped and came back, or playback that moved
     by `serverPlays` seconds: does the question come? */
  async ask_restarted(p) {
    await streamPlays(p, 0);
    playOn(p, 1);
    p.video.currentTime = 1200;
    fire(p.video, 'timeupdate');
    p.sleep(7190000);
    const asks = () => { endsNow(p); p.sleep(11000); p.tick(250); return p.q('[data-ask]').open; };
    if (p.win.serverPlays !== 'restart') {
      p.video.currentTime += +p.win.serverPlays;
      fire(p.video, 'seeking');
      return { asked: asks() };
    }
    /* Emptied of its stream the video stands at 0 and says so, at whatever point of the start. */
    const stands = () => { p.video.currentTime = 0; fire(p.video, 'timeupdate'); };
    p.hlsList[0].emit('hlsError', { response: { code: 410 } });
    stands();
    p.replies.push(answer('1080p-8000'));
    await settle();
    stands();
    p.hlsList[1].emit('hlsManifestParsed');
    p.video.readyState = 1;
    stands();
    p.video.currentTime = p.hlsList[1].startsAt;
    const at = p.video.currentTime;
    fire(p.video, 'seeking');
    fire(p.video, 'playing');
    return { at: at, asked: asks() };
  },

  /* The question closed by the browser, as a second Escape does, with the details never opening. */
  async ask_closed(p) {
    const ask = p.q('[data-ask]');
    const card = () => ({ shown: !p.q('[data-upnext]').hidden, counts: !p.q('[data-upnext-in]').hidden });
    const out = {};
    p.replies[0] = answer('1080p-8000', { markers: MARKERS });
    await streamPlays(p, 0);
    p.sleep(7200000);
    endsNow(p);
    p.sleep(11000);
    p.tick(250);
    p.video.ended = false;
    fire(ask, 'cancel');
    out.staysOpen = ask.open;
    ask.close();
    await settle();
    out.card = card();
    fire(p.video, 'progress');
    out.skip = !gone(p.q('[data-skip]'));
    p.tick(30000);
    await settle();
    out.wentByItself = p.calls.some((c) => c.page);
    key('Escape');
    const starts = p.starts().length;
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-play]'));
    await settle();
    out.replays = p.starts().length - starts;
    click(p.q('[data-next-btn]'));
    await settle();
    out.went = p.win.history.urls;
    return out;
  },

  /* The card waits: by `serverPlays`, with Autoplay off, or after the question was closed. */
  async upnext_waits(p) {
    await streamPlays(p, 0);
    if (p.win.serverPlays === 'question') {
      p.sleep(7200000);
      endsNow(p);
      p.sleep(11000);
      p.tick(250);
      p.video.ended = false;
      p.video.currentTime = 0;
      p.q('[data-ask]').close();
      await settle();
    } else {
      click(p.q('[data-autoplay]'));
      endsNow(p);
    }
    const did = () => [p.video.plays - plays, p.starts().length - starts];
    const plays = p.video.plays;
    const starts = p.starts().length;
    const out = { card: !p.q('[data-upnext]').hidden };
    key(' ');
    press(p.video);
    await settle();
    out.space = did();
    out.still = !p.q('[data-upnext]').classList.contains('is-leaving');
    p.replies.push(answer('1080p-8000'));
    press(p.q('[data-play]'));
    await settle();
    out.button = did();
    return out;
  },

  /* The second press of a double one, or the repeats of a held key, land on what the first left there. */
  async next_pressed_twice(p) {
    let fullscreens = 0;
    p.root.requestFullscreen = () => { fullscreens++; return Promise.resolve(); };
    const out = {};
    const wants = () => playState(p).busy === 'true';
    const taken = async () => {
      p.replies.push(answer('1080p-8000'));
      await settle();
      await streamPlays(p, 0);
      return p.video.paused;
    };
    await streamPlays(p, 0);
    /* The last press on the picture, long ago, paused. */
    press(p.video);
    endsNow(p);
    press(p.q('[data-upnext-play]'), 'mouse', 1);
    p.video.ended = false;
    press(p.video, 'mouse', 2);
    fire(p.video, 'dblclick', { bubbles: true });
    out.double = { plays: wants(), fullscreens: fullscreens };
    out.doublePaused = await taken();
    doubleClick(p.video);
    out.own = fullscreens;
    p.video.paused = false;
    /* A press on the picture and one that comes after the next episode was asked for. */
    press(p.video, 'mouse', 1);
    click(p.q('[data-next-btn]'));
    press(p.video, 'mouse', 2);
    out.across = wants();
    await taken();
    p.sleep(7200000);
    tabTo(p.q('[data-play]'));
    p.sleep(7200000);
    endsNow(p);
    p.sleep(11000);
    p.tick(250);
    out.asked = p.q('[data-ask]').open;
    /* The browser presses Continue as Enter goes down, and gives Play the focus back. */
    key('Enter');
    out.focus = doc.activeElement === p.q('[data-play]');
    key('Enter', true);
    out.held = wants();
    key('Enter');
    out.enter = wants();
    key('Enter');
    doc.activeElement = doc.body;
    key(' ');
    key(' ', true);
    out.space = wants();
    return out;
  },

  /* Enter held on a control, on Play, and on Continue with the focus then back on Mute. */
  async key_held(p) {
    await streamPlays(p, 0);
    const seek = p.video.currentTime;
    tabTo(p.q('[data-fwd10]'));
    key('Enter');
    key('Enter', true);
    p.tick(300);
    const skipped = p.video.currentTime - seek;
    tabTo(p.q('[data-play]'));
    key('Enter');
    key('Enter', true);
    const paused = p.video.paused;
    tabTo(p.q('[data-mute]'));
    p.sleep(7200000);
    endsNow(p);
    p.sleep(11000);
    p.tick(250);
    key('Enter');
    const back = doc.activeElement === p.q('[data-mute]');
    key('Enter', true);
    await settle();
    return { skipped: skipped, paused: paused, back: back, muted: p.video.muted, card: !p.q('[data-upnext]').hidden };
  },

  /* The episode ends; is the question there? */
  async ask_carried(p) {
    const stored = p.sitting();
    /* The saved volume, set as the page opens. */
    fire(p.video, 'volumechange');
    await streamPlays(p, 0);
    endsNow(p);
    return { asked: p.q('[data-ask]').open, goesOn: stored.goes_on };
  },

  /* A page opened because this one could not go on by itself knows how long nobody was there. */
  async ask_handed_on(p) {
    p.pages['/watch/play/502'] = { redirected: true, html: '' };
    await streamPlays(p, 0);
    endsNow(p);
    p.tick(10000);
    await settle();
    const byItself = p.sitting();
    p.leave();
    p.comeBack();
    click(p.q('[data-next-btn]'));
    await settle();
    return { href: p.win.location.href, byItself: [byItself.count, byItself.goes_on], pressed: byItself.pressed, byHand: p.sitting().goes_on };
  },

  /* Autoplay is a choice of the quality menu. */
  async autoplay_off(p) {
    const row = () => p.q('[data-autoplay]');
    const out = { start: row().getAttribute('aria-checked') };
    await streamPlays(p, 0);
    press(p.q('[data-quality-btn]'));
    click(row());
    out.off = row().getAttribute('aria-checked');
    out.saved = p.storage['mediaman.watch.autoplay'];
    out.menuOpen = p.q('[data-quality-btn]').getAttribute('aria-expanded');
    endsNow(p);
    const card = () => ({ hidden: p.q('[data-upnext]').hidden, counts: !p.q('[data-upnext-in]').hidden, play: playState(p).label });
    out.card = card();
    p.tick(30000);
    out.went = p.calls.some((c) => c.page);
    click(row());
    out.on = card();
    click(row());
    out.offAgain = card();
    p.tick(30000);
    out.wentLater = p.calls.some((c) => c.page);
    return out;
  },

  /* Full screen is entered, and then left, as the card counts down: Escape never reaches the page there. */
  async countdown_fullscreen(p) {
    const change = (el) => { doc.fullscreenElement = el; fire(doc, 'fullscreenchange'); };
    const card = () => ({ counts: playState(p).label === 'Pause', leaving: p.q('[data-upnext]').classList.contains('is-leaving') });
    await streamPlays(p, 0);
    endsNow(p);
    change(p.root);
    const entered = card();
    change(null);
    const left = card();
    p.tick(20000);
    await settle();
    return { entered: entered, left: left, went: p.calls.some((c) => c.page) };
  },

  /* What a screen reader hears of the next-episode card. */
  async upnext_spoken(p) {
    const status = p.q('[data-upnext-status]');
    const out = { polite: status.getAttribute('role'), before: status.textContent };
    await streamPlays(p, 0);
    endsNow(p);
    out.counting = status.textContent;
    p.tick(5000);
    out.later = status.textContent;
    key('Escape');
    out.cancelled = status.textContent;
    endsNow(p);
    out.again = status.textContent;
    press(p.q('[data-quality-btn]'));
    click(p.q('[data-autoplay]'));
    out.still = status.textContent;
    return out;
  },

  /* What of the quality menu scrolls, and which of its ends have more. */
  async menu_parts(p) {
    const menu = p.q('[data-quality-menu]');
    const list = menu.querySelector('.player-menu-list');
    const more = () => ['above', 'below'].filter((end) => list.classList.contains('has-more-' + end));
    const out = {
      heading: menu.querySelector('.player-menu-title').textContent,
      group: list.getAttribute('aria-label'),
      scrolls: list.querySelectorAll('.player-menu-item').length,
      autoplayScrolls: list.contains(p.q('[data-autoplay]')),
      fits: more(),
    };
    Object.assign(list, { scrollTop: 0, clientHeight: 100, scrollHeight: 260 });
    press(p.q('[data-quality-btn]'));
    out.top = more();
    list.scrollTop = 80;
    fire(list, 'scroll');
    out.middle = more();
    list.scrollTop = 160;
    fire(list, 'scroll');
    out.end = more();
    return out;
  },

  async autoplay_saved(p) {
    return { checked: p.q('[data-autoplay]').getAttribute('aria-checked') };
  },

  async autoplay_film(p) {
    return { row: !!p.q('[data-autoplay]') };
  },

  /* Plex could not say what follows; it is asked again at the end, and by Try again. */
  async next_unknown(p) {
    const found = p.pages['/found'];
    const out = { link: !!p.q('[data-next-btn]') };
    await streamPlays(p, 0);
    endsNow(p);
    await settle();
    out.text = p.q('[data-message-text]').textContent;
    out.message = !gone(p.q('[data-message]'));
    out.retry = !p.q('[data-retry]').hidden;
    out.streams = p.calls.filter((c) => /\/stop$/.test(c.url)).length;
    p.video.ended = false; p.video.currentTime = 2000;
    fire(p.video, 'seeking');
    out.afterSeek = !gone(p.q('[data-message]'));
    endsNow(p);
    await settle();
    p.pages['/watch/play/1'] = found;
    press(p.q('[data-retry]'));
    await settle();
    out.found = { message: !gone(p.q('[data-message]')), card: p.q('[data-upnext]').hidden, link: p.q('[data-next-btn]').getAttribute('href'), starts: p.starts().length, href: p.win.location.href };
    return out;
  },

  /* A place the server has is not sent again, whatever is marked meanwhile. */
  async saved_once(p) {
    await streamPlays(p, 100);
    playOn(p, 5);
    p.video.pause();
    await settle();
    const hide = () => { doc.visibilityState = 'hidden'; fire(doc, 'visibilitychange'); };
    hide();
    const afterPause = p.beacons.length;
    p.video.play();
    playOn(p, 2);
    hide();
    const afterMore = p.beacons.length;
    p.video.pause();
    await settle();
    p.leave();
    const last = p.beacons[p.beacons.length - 1];
    /* The next episode at the very same place is another matter. */
    p.comeBack();
    await settle();
    const saves = p.calls.filter((c) => /\/progress$/.test(c.url)).length;
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await settle();
    await streamPlays(p, 100);
    playOn(p, 7);
    p.video.pause();
    const places = p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => [c.body.position_s, c.body.played_s]);
    return { afterPause: afterPause, afterMore: afterMore, stop: last.url, body: last.body, after: places.length - saves, same: String(places[saves]) === String(places[saves - 1]) };
  },

  /* The full screen button as the state changes, by whatever route. */
  async fullscreen(p) {
    const btn = p.q('[data-fullscreen]');
    const look = () => ({ icon: btn.querySelector('use').getAttribute('href'), label: btn.getAttribute('aria-label'), title: btn.getAttribute('title'), text: btn.textContent.trim() });
    let asked = 0;
    p.root.requestFullscreen = () => { asked++; return Promise.resolve(); };
    const out = { start: look() };
    doc.fullscreenElement = p.root;
    fire(doc, 'fullscreenchange');
    out.entered = look();
    doc.fullscreenElement = null;
    fire(doc, 'fullscreenchange');
    out.left = look();
    doc.webkitFullscreenElement = p.root;
    fire(doc, 'webkitfullscreenchange');
    out.enteredWebkit = look();
    doc.webkitFullscreenElement = null;
    click(btn);
    key('f');
    fire(p.video, 'dblclick', { bubbles: true });
    out.asked = asked;
    press(p.video, 'touch');
    fire(p.video, 'dblclick', { bubbles: true });
    out.askedAfterTouch = asked;
    return out;
  },

  /* The Back arrow, by how the page was opened. */
  async back() {
    const html = fs.readFileSync(process.argv[2], 'utf8');
    const press = (came, more) => {
      const p = boot(html, false, null, undefined, came);
      const a = p.root.querySelector('.player-back');
      let kept = false;
      fire(a, 'click', Object.assign({ bubbles: true, button: 0, preventDefault() { kept = true; } }, more));
      const out = { through: p.win.history.backs, link: !kept };
      if (more && more.twice) {
        p.tick(more.twice);
        if (more.leaves) p.win.listeners.beforeunload.forEach((fn) => fn({}));
        fire(a, 'click', { bubbles: true, button: 0 });
        out.through = p.win.history.backs;
        out.second = p.win.location.href;
      }
      if (more && more.leaves) p.win.listeners.beforeunload.forEach((fn) => fn({}));
      p.tick(more && more.waits || 60000);
      return Object.assign(out, { late: p.win.location.href });
    };
    const reloaded = boot(html, false, null, undefined, { referrer: 'http://mm/?genre=action', length: 3 }).win.history.state;
    return {
      fromWatch: press({ referrer: 'http://mm/?genre=action', length: 3 }),
      reload: press({ length: 3, state: reloaded }),
      newTab: press({ referrer: 'http://mm/', length: 1 }),
      deep: press({ length: 2 }),
      otherSite: press({ referrer: 'http://evil/watch', length: 3 }),
      otherPage: press({ referrer: 'http://mm/watch/play/9', length: 3 }),
      slow: press({ referrer: 'http://mm/', length: 3 }, { waits: 10000 }),
      slower: press({ referrer: 'http://mm/', length: 3 }, { leaves: true }),
      twice: press({ referrer: 'http://mm/', length: 3 }, { twice: 500 }),
      again: press({ referrer: 'http://mm/', length: 3 }, { twice: 2500 }),
      onItsWay: press({ referrer: 'http://mm/', length: 3 }, { twice: 2500, leaves: true }),
      newTabPress: press({ referrer: 'http://mm/', length: 3 }, { metaKey: true }),
    };
  },

  /* The film ends on a page that came by a link. */
  async endOfDeepLink() {
    const p = boot(fs.readFileSync(process.argv[2], 'utf8'), false, null, undefined, { length: 3 });
    await settle();
    fire(p.video, 'ended');
    await settle();
    return { through: p.win.history.backs, at: p.win.location.href };
  },

  /* The title ends, and the server says whether to ask for a rating: what then comes, by *what*. */
  async rating() {
    const what = process.argv[5];
    const p = boot(fs.readFileSync(process.argv[2], 'utf8'));
    const card = p.q('[data-rate]');
    const asks = () => p.calls.filter((c) => /ratings\/ask/.test(c.url)).length;
    const look = () => ({ card: !gone(card), upnext: !!p.q('[data-upnext]') && !gone(p.q('[data-upnext]')), at: p.win.location.href, asks: asks() });
    const thumb = () => card.querySelectorAll('.rate__b')[3];
    p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null };
    if (what === 'unrateable') p.held.unrateable = true;
    if (what === 'season') p.held.rate = { ask: true, ref: 'show-77', title: 'The Long Coast', season: 2 };
    if (what === 'refused') p.held.rate = { ask: false };
    if (what === 'fails' || what === 'silent') p.held.rate = what;
    if (what === 'unsaved') p.held.progressFails = { status: 500 };
    if (/^tight/.test(what)) p.held.tight = true;
    await streamPlays(p, 100);
    playOn(p, 5);
    if (what === 'late') p.held.save = [];
    if (what === 'saving' || what === 'backWhileAsked') p.held.save = [];
    if (what === 'saving') { p.video.paused = true; fire(p.video, 'pause'); p.video.paused = false; }
    if (/^hidden/.test(what)) doc.visibilityState = 'hidden';
    if (what === 'hiddenAnswer') { doc.visibilityState = 'visible'; p.held.rate = []; }
    if (what === 'native') { p.video.webkitPresentationMode = 'fullscreen'; p.video.webkitSetPresentationMode = (m) => { p.video.webkitPresentationMode = m; }; }
    if (what === 'picture') doc.pictureInPictureElement = p.video;
    const out = { before: look() };
    endsNow(p);
    if (p.held.save) {
      await settle();
      out.waiting = look();
      out.saves = p.held.save.length;
      if (what === 'late') { p.tick(3000); await settle(); }
      if (what === 'backWhileAsked') { fire(p.video, 'seeking'); }
      p.held.save.pop()({});
      await settle();
      out.oneLeft = look();
      p.held.save.forEach((r) => r({}));
    }
    await settle();
    p.tick(1);
    await settle();
    out.ended = Object.assign(look(), {
      title: p.q('[data-rate-title]').textContent, said: p.q('[data-rate-status]').textContent,
      thumbs: card.querySelectorAll('.rate__b').length, focusTaken: card.contains(doc.activeElement),
    });
    if (what === 'native') out.mode = p.video.webkitPresentationMode;
    if (/^hidden/.test(what)) {
      const returns = async () => { doc.visibilityState = 'visible'; fire(doc, 'visibilitychange'); await settle(); };
      if (what === 'hiddenAnswer') { doc.visibilityState = 'hidden'; p.held.rate[0]({ ask: true, ref: 'movie-501', title: 'Low Water', season: null }); await settle(); }
      p.tick(60000); await settle();
      out.hidden = look();
      if (what === 'hiddenReplay') { p.video.ended = false; p.video.currentTime = 100; fire(p.video, 'seeking'); fire(p.video, 'play'); }
      await returns();
      out.back = look();
    }
    if (what === 'silent') { p.tick(4000); await settle(); }
    if (what === 'replay') { p.video.ended = false; fire(p.video, 'play'); }
    if (what === 'back') click(p.root.querySelector('.player-back'));
    if (what === 'close') { p.q('[data-rate-close]').focus(); click(p.q('[data-rate-close]')); out.focus = doc.activeElement.getAttribute('data-play') !== null; }
    if (what === 'escape') key('Escape');
    if (what === 'escapeInCard') { thumb().focus(); key('Escape'); }
    if (what === 'leave') { p.held.put = 'silent'; click(thumb()); p.leave(); out.kept = p.held.kept; }
    if (what === 'leaveTakenBack') { click(thumb()); await settle(); p.held.put = 'silent'; click(thumb()); p.leave(); out.kept = p.held.kept; }
    if (what === 'leaveSaved') { click(thumb()); await settle(); p.leave(); out.kept = p.held.kept || null; }
    if (what === 'takenBack') { click(thumb()); await settle(); click(thumb()); await settle(); p.tick(3000); await settle(); out.stays = look(); }
    if (what === 'clicked') { p.held.put = 'silent'; press(thumb()); p.tick(29000); await settle(); out.early = look(); p.tick(2000); }
    if (what === 'clickedTakenBack') { press(thumb()); await settle(); press(thumb()); await settle(); p.tick(29000); out.early = look(); p.tick(2000); }
    if (what === 'keyed') { p.held.put = 'silent'; tabTo(thumb()); key('Enter'); p.tick(29000); out.early = look(); p.tick(2000); }
    if (what === 'tabbed') { tabTo(thumb()); p.tick(70000); await settle(); out.stays = look(); }
    if (what === 'message') {
      p.hlsList[p.hlsList.length - 1].emit('hlsError', loadError('fragLoadError', 410, { error: 'replaced' }));
      await settle();
      out.message = messageState(p);
    }
    if (what === 'rated') { click(thumb()); await settle(); p.tick(1000); await settle(); out.early = look(); p.tick(1000); }
    if (what === 'ignored') { p.tick(29000); await settle(); fire(p.root, 'pointermove', { bubbles: true, pointerType: 'mouse' }); p.tick(29000); await settle(); out.early = look(); p.tick(2000); }
    if (what === 'tightCancel') { p.held.tight = true; click(p.q('[data-upnext-cancel]')); await settle(); p.tick(1); await settle(); out.cancelled = look(); }
    if (what === 'tightCountdown') { p.tick(11000); await settle(); out.cancelled = look(); }
    if (what === 'tightSeek') { p.video.ended = false; p.video.currentTime = 100; fire(p.video, 'seeking'); p.tick(10); await settle(); out.cancelled = look(); }
    if (what === 'next') { click(p.q('[data-upnext-play]')); out.pressed = look(); }
    if (what === 'countdown') { p.tick(9000); await settle(); out.early = look(); p.tick(2000); }
    /* Nobody has been there for the two hours that end while it counts down. */
    if (what === 'stillWatching') { p.sleep(2 * 60 * 60 * 1000); p.tick(1000); out.dialog = p.q('[data-ask]').open; }
    if (what === 'countdown' || what === 'next') { await settle(); out.went = p.calls.some((c) => c.page); }
    await settle();
    out.after = look();
    out.status = p.q('[data-rate-status]').textContent;
    p.tick(40000);
    await settle();
    out.late = p.win.location.href;
    return out;
  },

  /* The last credits begin at 2700 of 3000 s, and the server has a question: what comes of it, by *what*. */
  async credits() {
    const what = process.argv[5];
    const p = boot(fs.readFileSync(process.argv[2], 'utf8'), false, what === 'noAutoplay' ? { 'mediaman.watch.autoplay': 'false' } : null);
    const card = p.q('[data-rate]');
    const asks = () => p.calls.filter((c) => /ratings\/ask/.test(c.url)).length;
    const saves = () => p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => c.body.position_s);
    const look = () => ({
      card: !gone(card), idle: (card.querySelector('.rate__line') || card).textContent, asks: asks(), at: p.win.location.href, said: p.q('[data-rate-status]').textContent,
      paused: p.video.paused, upnext: !!p.q('[data-upnext]') && !gone(p.q('[data-upnext]')),
    });
    const thumb = () => card.querySelectorAll('.rate__b')[3];
    const on = async (s) => { for (let i = 0; i < s; i++) { playOn(p, 1); await settle(); } };
    const seek = (t) => { p.video.seeking = true; p.video.currentTime = t; fire(p.video, 'seeking'); p.video.seeking = false; fire(p.video, 'timeupdate'); };
    p.replies[0].ok.markers = MARKERS;
    if (what === 'early') p.replies[0].ok.markers = [{ kind: 'credits', start_s: 2090, end_s: 3000, final: true }];
    if (what === 'notFinal') p.replies[0].ok.markers = [{ kind: 'credits', start_s: 2700, end_s: 3000, final: false }];
    if (/^scene/.test(what)) p.replies[0].ok.markers = [{ kind: 'credits', start_s: 2700, end_s: 2720, final: true }];
    if (/^hidden/.test(what)) doc.visibilityState = 'hidden';
    if (what === 'fraction') p.replies[0].ok.markers = [{ kind: 'credits', start_s: 2700.92, end_s: 3000, final: true }];
    p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null };
    if (what === 'unrateable') p.held.unrateable = true;
    if (what === 'askFails') p.held.rate = 'fails';
    if (what === 'refused') p.held.rate = { ask: false };
    if (what === 'saveFails') p.held.progressFails = { status: 500 };
    if (what === 'native') p.video.webkitPresentationMode = 'fullscreen';
    if (what === 'picture') { doc.pictureInPictureElement = p.video; doc.exitPictureInPicture = () => Promise.resolve(); }
    const start = { early: 2080, resumed: 2750, endCard: 2985, scrubbed: 100 }[what] || 2690;
    await streamPlays(p, start);
    p.video.paused = false;
    p.video.currentTime = start;
    fire(p.video, 'timeupdate');
    const out = {};
    if (what === 'fraction') { p.video.currentTime = 2699.93; fire(p.video, 'timeupdate'); await on(1); }
    if (what === 'scrubbed') { await on(2); seek(2800); await settle(); await on(3); out.passed = look(); seek(200); await on(1); out.back = look(); seek(2690); fire(p.video, 'timeupdate'); await on(11); out.after = look(); return out; }
    if (what === 'endCard') { await on(5); endsNow(p); await settle(); out.end = look(); seek(2950); await settle(); out.sought = look(); p.tick(60000); out.late = look(); return out; }
    if (what === 'resumed') { await on(5); out.passed = look(); await on(6); out.after = look(); return out; }
    await on(9);
    if (what !== 'fraction') out.before = look();
    const QUESTION = p.held.rate;
    if (what === 'seekWhileAsked') p.held.rate = [];
    if (what === 'seekWhileSaved') p.held.save = [];
    if (what === 'endWhileAsked') p.held.save = [];
    p.root.classList.add('is-idle');
    const focus = doc.activeElement;
    await on(2);
    out.in = Object.assign(look(), {
      saves: saves(), focus: doc.activeElement === focus, quiet: p.root.classList.contains('is-idle'),
      beside: card.classList.contains('player-rate--beside'), plays: p.video.plays,
    });
    if (what === 'hidden') { await on(12); out.hidden = look(); doc.visibilityState = 'visible'; await on(1); }
    if (what === 'scene' || what === 'sceneRated') { if (what === 'sceneRated') click(thumb()); await on(22); out.scene = look(); }
    if (what === 'sceneSkip') { click(p.q('[data-skip]')); p.video.currentTime = 2720; fire(p.video, 'timeupdate'); await on(8); out.scene = look(); }
    if (what === 'hiddenBack') { seek(2000); await on(1); doc.visibilityState = 'visible'; seek(2800); await on(3); }
    if (/^idle/.test(what)) {
      if (what === 'idleFocus') tabTo(thumb());
      if (what === 'idleHover') fire(card, 'pointerenter', { pointerType: 'mouse' });
      if (what === 'idleHidden') doc.visibilityState = 'hidden';
      if (what === 'idleTouch') { await on(25); fire(card, 'pointerdown', { bubbles: true, pointerType: 'touch' }); }
      if (what === 'idlePaused') { await on(25); p.video.pause(); p.tick(60000); await settle(); out.paused = look(); p.video.paused = false; }
      await on(what === 'idle' ? 27 : /Focus|Hover|Hidden/.test(what) ? 40 : 29);
      out.nearly = look();
      await on(what === 'idle' ? 4 : 0);
      if (what === 'idleHover') { fire(card, 'pointerleave', { pointerType: 'mouse' }); await on(31); }
      if (what === 'idleHidden') { doc.visibilityState = 'visible'; }
      if (what === 'idle') { out.gone = look(); await on(40); }
    }
    if (what === 'arrowOnThumb') { tabTo(thumb()); const from = p.video.currentTime; key('ArrowLeft'); out.left = doc.activeElement === card.querySelectorAll('.rate__b')[2]; key('ArrowRight'); key('ArrowRight'); out.right = doc.activeElement === card.querySelectorAll('.rate__b')[0]; p.tick(300); await settle(); out.moved = p.video.currentTime - from; }
    if (what === 'unrateable') { p.tick(1); await settle(); }
    if (what === 'rebuffer') { fire(p.video, 'waiting'); fire(p.video, 'play'); fire(p.video, 'playing'); await on(1); }
    if (what === 'seekOn') { seek(2900); await on(1); }
    if (what === 'seekBack') { seek(2000); await settle(); out.back = look(); await on(1); seek(2698); await on(15); }
    if (what === 'arrowBack') { key('ArrowLeft'); key('ArrowLeft'); p.tick(300); fire(p.video, 'timeupdate'); await settle(); }
    if (what === 'seekWhileAsked') { seek(2000); p.held.rate[0](QUESTION); await settle(); out.back = look(); seek(2690); fire(p.video, 'timeupdate'); await on(12); }
    if (what === 'seekWhileSaved') { seek(2000); p.held.save.splice(0).forEach((r) => r({})); p.held.save = null; await settle(); out.back = look(); seek(2690); fire(p.video, 'timeupdate'); await on(12); }
    if (what === 'rated') { click(thumb()); await settle(); p.tick(2000); await settle(); out.rated = look(); }
    if (what === 'levels') { const name = () => p.q('[data-rate-level]').textContent; click(thumb()); out.name = name(); await settle(); click(thumb()); await settle(); out.none = name(); out.rated = card.classList.contains('is-rated'); }
    if (/^meets/.test(what)) { const skip = p.q('[data-skip]'); const rect = (l) => () => ({ left: l, right: l + 100, top: 10, bottom: 60 }); card.getBoundingClientRect = rect(0); skip.getBoundingClientRect = rect(90); p.win.listeners.resize.forEach((fn) => fn()); out.above = card.classList.contains('player-rate--above'); skip.getBoundingClientRect = rect(130); p.win.listeners.resize.forEach((fn) => fn()); out.beside = card.classList.contains('player-rate--above'); }
    if (what === 'spaceAfterClick') { press(thumb()); await settle(); key(' '); await settle(); out.level = p.calls.filter((c) => c.gone).length; }
    if (what === 'close') { click(p.q('[data-rate-close]')); await settle(); }
    if (what === 'escape') key('Escape');
    if (what === 'alone') { p.tick(60000); await settle(); }
    if (what === 'native') { out.native = look(); p.video.webkitPresentationMode = 'inline'; await on(12); }
    out.after = look();
    if (what === 'askFails') { p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null }; }
    if (what === 'refused') { p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null }; }
    if (what === 'saveFails') { p.held.progressFails = null; }
    if (what === 'skip' || what === 'stillOpen') { click(p.q('[data-skip]')); p.video.currentTime = 2999; await settle(); out.skipped = look(); }
    if (what === 'stillWatching') p.sleep(3 * 60 * 60 * 1000);
    endsNow(p);
    await settle();
    if (what === 'endWhileAsked') { p.held.save.splice(0).forEach((r) => r({})); p.held.save = null; await settle(); }
    await settle();
    out.end = Object.assign(look(), { dialog: !!p.q('[data-ask]') && p.q('[data-ask]').open });
    if (what === 'endSeek') { seek(2950); await on(1); out.sought = look(); p.tick(60000); await settle(); out.soughtLate = look(); return out; }
    p.tick(9000); await settle();
    out.nine = look();
    p.tick(22000); await settle();
    out.late = look();
    out.went = p.calls.some((c) => c.page);
    return out;
  },

  /* A next episode played in place: the one at /watch/play/502, whose Back leads where the page says. */
  async backAfterNext() {
    const p = boot(fs.readFileSync(process.argv[2], 'utf8'), false, null, undefined, { referrer: 'http://mm/watch/show/77', length: 3 });
    Object.entries(JSON.parse(process.env.PLAYER_PAGES)).forEach(([href, f]) => { p.pages[href] = { html: fs.readFileSync(f, 'utf8') }; });
    await settle();
    click(p.root.querySelector('[data-next-btn]'));
    await settle();
    const a = p.root.querySelector('.player-back');
    fire(a, 'click', { bubbles: true, button: 0 });
    return { at: p.win.history.urls, through: p.win.history.backs, href: a.getAttribute('href'), state: p.win.history.state };
  },

  /* Whether the button is there, and what the sheet lists. */
  async away_sheet(p) {
    await streamPlays(p, 30);
    if (p.win.WebKitPlaybackTargetAvailabilityEvent) p.elsewhere.nearby();
    await settle();
    const shown = !p.q('.player-controls').querySelector('[data-play-on]').hidden;
    if (!shown) return { shown: shown };
    await openSheet(p);
    const rows = sheet().querySelectorAll('[data-key]').map((r) => r.getAttribute('data-key'));
    const out = { shown: shown, rows: rows, says: said(), text: sheet().textContent };
    key('Escape');
    sheet().close();
    await settle();
    p.tick(1000);
    out.left = !sheet();
    return out;
  },

  /* A TV is picked; `serverPlays` is what then happens here while it plays there. */
  async away_tv(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'tv-5');
    const play = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || ''))[0];
    const out = {
      stopped: p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.body),
      play: { url: play.post, key: play.body.rating_key, at: play.body.position_s, quality: play.body.quality },
      sheet: !!sheet() && sheet().open,
    };
    out.starts = p.starts().length;
    key(' ');
    key('ArrowRight');
    click(p.q('[data-play]'));
    click(p.q('[data-next-btn]'));
    p.online();
    p.comeBack();
    p.tick(60000);
    await settle();
    out.startsThere = p.starts().length + p.elsewhere.casts;
    out.state = awayState(p);
    click(p.q('[data-bring]'));
    await settle();
    out.startsHere = p.starts().length;
    out.from = p.hlsList[p.hlsList.length - 1].config.startPosition;
    out.after = awayState(p);
    return out;
  },

  /* Stopped on the TV by its own remote, unpaired, or lost. */
  async away_tv_over(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'tv-5');
    const starts = p.starts().length;
    const why = p.win.serverPlays;
    p.elsewhere.tv = () => (why === 'unpaired' ? p.elsewhere.refuse(410, 'unpaired', { last: { rating_key: '501', position_s: 88 } })
      : Promise.resolve({ tv: Object.assign({}, LIVING, { last: { reason: why, rating_key: '501', position_s: 88 } }) }));
    p.tick(6000);
    await settle();
    const out = Object.assign(awayState(p), { started: p.starts().length - starts, time: p.q('[data-elapsed]').textContent });
    click(p.q('[data-play]'));
    await settle();
    out.from = p.hlsList[p.hlsList.length - 1].config.startPosition;
    return out;
  },

  /* The TV does not start it. */
  async away_tv_refused(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    await openSheet(p);
    p.elsewhere.plays.push(() => p.elsewhere.refuse(409, how === 'limit' ? 'too_many_streams' : 'tv_off'));
    let answer;
    if (how === 'closed') p.elsewhere.plays[0] = () => new Promise((r, no) => { answer = () => no({ status: 409, error: 'tv_off' }); });
    click(rowOf('tv-5'));
    await settle();
    const out = { starting: how === 'closed' ? awayState(p).says : null };
    if (how === 'closed') { sheet().close(); await settle(); answer(); await settle(); }
    out.says = how === 'closed' ? null : sheet().querySelector('.play-on__say').textContent;
    out.state = awayState(p);
    out.from = p.hlsList[p.hlsList.length - 1].config.startPosition;
    out.toasts = p.toasts.map((t) => t.text);
    await settle();
    if (how === 'again') {
      await pickRow(p, 'again');
      out.plays = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || '')).length;
      out.second = awayState(p).says;
    }
    return out;
  },

  /* The button of a title on a page without a player. */
  async away_title(p) {
    await settle();
    const b = p.win.MM.playOn.button('/watch/play/501?resume=1', 'btn');
    doc.body.appendChild(b);
    const out = { hidden: b.hidden };
    if (out.hidden) return out;
    p.elsewhere.nearby();
    click(b);
    await settle();
    out.rows = sheet().querySelectorAll('[data-key]').map((r) => r.getAttribute('data-key'));
    await pickRow(p, 'tv-5');
    out.play = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || '')).map((c) => c.body);
    out.opened = p.opened;
    p.tick(1000);
    p.elsewhere.tvs = [Object.assign({}, LIVING, { play: { rating_key: '501', state: 'playing', title: 'The Long Coast' } })];
    click(b);
    await settle();
    out.on = rowOf('tv-5').getAttribute('aria-current');
    await pickRow(p, 'here');
    out.went = p.win.location.href;
    return out;
  },

  /* A TV is paired from the sheet: a wrong code, somebody else's, then a name. */
  async away_pair(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'pair');
    const e = p.elsewhere;
    const type = async (code) => { const input = rowOf('code'); input.value = code; fire(input, 'input'); await settle(); };
    const why = () => sheet().querySelector('.play-on__why').textContent;
    const out = { focus: doc.activeElement === rowOf('code'), steps: sheet().textContent };
    e.pairs.push(() => e.refuse(404, 'wrong_code'), () => e.refuse(409, 'in_use', { playing: true }), () => Promise.resolve({ tv: { id: 9, name: 'TV' } }));
    await type('12 34');
    out.early = e.sent.filter((c) => /pair$/.test(c.post || '')).length;
    await type('123456');
    out.wrong = [why(), rowOf('code').value];
    await type('482 916');
    out.others = [why(), !!rowOf('take')];
    await pickRow(p, 'take');
    out.sent = e.sent.filter((c) => /pair$/.test(c.post || '')).map((c) => [c.body.code, c.body.confirm || false]);
    out.name = rowOf('name').value;
    p.win.MM.api.patch = (url, body) => { e.sent.push({ patch: url, body: body }); return Promise.resolve({}); };
    e.tvs = [Object.assign({}, LIVING, { id: 9, name: 'Den' })];
    rowOf('name').value = 'Den';
    fire(rowOf('name'), 'input');
    await pickRow(p, 'play');
    out.named = e.sent.filter((c) => c.patch).map((c) => [c.patch, c.body.name]);
    out.plays = e.sent.filter((c) => /\/play$/.test(c.post || '')).map((c) => c.post);
    return out;
  },

  /* A TV that is not open, or that somebody watches on. */
  async away_tv_asks(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    const plays = () => p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || '')).map((c) => c.body.confirm || false);
    await pickRow(p, 'tv-6');
    const out = { off: said(), untouched: plays().length === 0 && !p.q('[data-away-on]').textContent && !p.hlsList[0].destroyed };
    await pickRow(p, 'tv-7');
    out.busy = said();
    await pickRow(p, 'unsay');
    out.cancelled = said();
    out.before = plays();
    await pickRow(p, 'tv-7');
    await pickRow(p, 'take');
    out.after = plays();
    out.says = awayState(p).says;
    return out;
  },

  /* Nobody was known to watch on it, but somebody does. */
  async away_tv_in_use(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    p.elsewhere.plays.push(() => p.elsewhere.refuse(409, 'in_use'));
    await pickRow(p, 'tv-5');
    const out = { asks: said(), from: p.hlsList[p.hlsList.length - 1].config.startPosition };
    await pickRow(p, 'take');
    out.plays = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || '')).map((c) => c.body.confirm || false);
    return out;
  },

  /* Brought here again before the TV has begun. */
  async away_tv_called_back(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    let answer;
    p.elsewhere.plays.push(() => new Promise((r) => { answer = () => r({ tv: Object.assign({}, LIVING, { play: { play_id: 'p7', rating_key: '501', state: 'starting' } }) }); }));
    await pickRow(p, 'tv-5');
    const starts = p.starts().length;
    click(p.q('[data-bring]'));
    await settle();
    const out = { here: p.starts().length - starts, state: awayState(p).away };
    answer();
    await settle();
    out.stopped = p.elsewhere.sent.some((c) => /\/commands$/.test(c.post || '') && c.body.kind === 'stop');
    out.after = awayState(p).away;
    return out;
  },

  /* AirPlay: until a TV is picked nothing changes. */
  async away_airplay(p) {
    await streamPlays(p, 30);
    const e = p.elsewhere;
    e.nearby();
    await openSheet(p);
    await pickRow(p, 'airplay');
    const out = {
      picked: e.picked,
      chooser: said(),
      waits: { casts: e.starts().length, destroyed: p.hlsList[0].destroyed, away: awayState(p).away, clip: e.air().src },
    };
    p.video.currentTime = 34;
    e.onTv(true);
    await settle();
    out.order = p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.body.position_s).concat(e.starts().map((b) => b.rating_key));
    out.starting = awayState(p).says;
    out.src = e.air().src;
    out.sheet = said();
    e.air().readyState = 1;
    fire(e.air(), 'loadedmetadata');
    await settle();
    out.at = e.air().currentTime;
    mediaPlays(e.air(), 34);
    await settle();
    out.playing = awayState(p);
    out.local = p.starts().length;
    /* The place is saved as ever, by the stream of the TV's. */
    for (let i = 0; i < 12; i++) { e.air().currentTime += 1; fire(e.air(), 'timeupdate'); p.tick(1000); }
    await settle();
    out.saved = p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => c.body).pop();
    /* The picture comes back to this device: Safari pauses it, then says so. */
    e.wireless = false;
    e.air().pause();
    e.onTv(false);
    p.tick(1600);
    await settle();
    out.back = { away: awayState(p).away, from: p.hlsList[p.hlsList.length - 1].config.startPosition, starts: p.starts().length, clip: e.air().src };
    out.stopped = p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.url + ' ' + c.body.position_s).pop();
    await streamPlays(p, 46);
    out.playsOn = p.video.plays;
    return out;
  },

  /* A TV chosen long after the list was asked for, or after a TV's own page was. */
  async away_airplay_dropped(p) {
    await streamPlays(p, 30);
    p.elsewhere.nearby();
    await openSheet(p);
    await pickRow(p, 'airplay');
    if (p.win.serverPlays === 'late') p.tick(95000);
    else await pickRow(p, 'tv-5');
    await settle();
    p.elsewhere.onTv(true);
    await settle();
    return { casts: p.elsewhere.starts().length, says: awayState(p).says, started: p.starts().length };
  },

  /* The server refuses the stream for the TV that was picked. */
  async away_airplay_refused(p) {
    await streamPlays(p, 30);
    p.elsewhere.nearby();
    p.elsewhere.castFails.push(() => p.elsewhere.refuse(429, 'too_many_streams'));
    await openSheet(p);
    await pickRow(p, 'airplay');
    p.elsewhere.onTv(true);
    await settle();
    return { says: said(), state: awayState(p).away, from: p.hlsList[p.hlsList.length - 1].config.startPosition, clip: p.elsewhere.air().src };
  },

  /* A new quality or other subtitles restart the picture on the TV, not here. */
  async away_choices(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 200;
    const out = { qualities: menuState(p).ids, first: p.elsewhere.starts().map((b) => [b.quality, b.subtitle]) };
    click(p.q('[data-quality="low"]'));
    await settle();
    out.quality = way.loads.map((s) => [s.position_s, s.paused]);
    mediaPlays(way.media, 200);
    click(p.q('[data-tracks]').querySelector('[data-subtitle="off"]'));
    await settle();
    out.asked = p.elsewhere.starts().map((b) => [b.quality, b.subtitle]);
    mediaPlays(way.media, 200);
    p.elsewhere.castFails.push(() => p.elsewhere.refuse(409, 'track_unavailable'));
    click(p.q('[data-tracks]').querySelector('[data-subtitle="32"]'));
    await settle();
    out.refused = p.elsewhere.starts().slice(3).map((b) => b.subtitle);
    out.saved = p.storage['mediaman.watch.quality'] || null;
    out.local = p.starts().length;
    click(p.q('[data-bring]'));
    await settle();
    out.here = { qualities: menuState(p).ids, start: p.starts().pop(), tracks: p.q('[data-tracks]').hidden, stops: way.stops };
    return out;
  },

  /* The next episode goes on where this one played. */
  async away_next(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    click(p.q('[data-next-btn]'));
    await settle();
    return {
      title: p.q('.player-subtitle').textContent, loads: way.loads.map((s) => s.title.rating_key),
      casts: p.elsewhere.starts().map((b) => b.rating_key), local: p.starts().length, away: awayState(p).away,
      qualities: menuState(p).ids,
    };
  },

  /* The credits, the end, and what follows it, as at home. */
  async away_end(p) {
    p.replies[0] = answer('best', { markers: MARKERS });
    p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null };
    await streamPlays(p, 30);
    const { air } = await airPlays(p);
    air.currentTime = 2699;
    for (let i = 0; i < 13; i++) { air.currentTime += 1; fire(air, 'timeupdate'); await settle(); }
    const out = { rate: !gone(p.q('[data-rate]')) };
    air.paused = true; air.ended = true; air.currentTime = 2997;
    fire(air, 'ended');
    await settle();
    out.saved = p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => [c.body.stream_id, c.body.position_s]).pop();
    out.upnext = !!p.q('[data-upnext]') && !gone(p.q('[data-upnext]'));
    out.says = awayState(p).says;
    out.ended = p.root.classList.contains('is-ended');
    out.dim = p.root.classList.contains('is-away-paused');
    click(p.q('[data-rate-close]'));
    await settle();
    out.leaves = p.win.location.href;
    out.buttons = [p.q('[data-bring]').textContent, p.q('[data-stop-away]').textContent];
    return out;
  },

  /* Leaving the page: AirPlay ends with it, and says so first; another way carries on. */
  async away_leave(p) {
    await streamPlays(p, 30);
    const cast = p.win.serverPlays === 'cast';
    const way = cast ? await castPlays(p, true) : await airPlays(p);
    const media = cast ? way.media : way.air;
    media.currentTime = 60;
    for (let i = 0; i < 2; i++) { media.currentTime += 1; fire(media, 'timeupdate'); }
    let kept = false;
    fire(p.q('.player-back'), 'click', { bubbles: true, button: 0, preventDefault() { kept = true; } });
    const out = { asked: p.q('[data-leave-card]').open, kept: kept };
    if (out.asked) click(p.q('[data-leave-stay]'));
    p.leave();
    out.beacons = p.beacons.map((b) => [b.url.replace(/.*\/api/, ''), b.body && b.body.position_s]);
    out.stops = cast ? way.stops : null;
    const starts = p.starts().length;
    p.comeBack();
    await settle();
    out.started = p.starts().length - starts;
    out.casts = p.elsewhere.casts;
    return out;
  },

  async away_leave_and_stop(p) {
    await streamPlays(p, 30);
    const { air } = await airPlays(p, 60);
    fire(p.q('.player-back'), 'click', { bubbles: true, button: 0 });
    click(p.q('[data-leave-stop]'));
    return { beacons: p.beacons.map((b) => [b.url, b.body.position_s]), went: p.win.location.href, clip: air.src };
  },

  /* The stream ends under the TV: twice another one, then it says so. */
  async away_error(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 90;
    for (let i = 0; i < 3; i++) {
      way.media.error = {};
      fire(way.media, 'error');
      await settle();
    }
    return { loads: way.loads.map((s) => s.position_s), casts: p.elsewhere.casts, local: p.starts().length, message: messageState(p) };
  },

  /* It ended elsewhere by itself: this page waits for a press. */
  async away_gone(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 120;
    way.gone(p.win.serverPlays);
    await settle();
    p.tick(60000);
    await settle();
    key(' ');
    await settle();
    const out = Object.assign(awayState(p), { starts: p.starts().length, stopped: p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => c.body.position_s).pop() });
    click(p.q('[data-retry]'));
    await settle();
    out.from = p.hlsList[p.hlsList.length - 1].config.startPosition;
    return out;
  },

  /* Paused for long: the stream is kept, and nothing is saved anew. */
  async away_paused(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 50;
    way.media.paused = false;
    way.media.pause();
    await settle();
    const saves = () => p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => [c.body.stream_id, c.body.position_s, c.body.played_s]);
    const first = saves();
    p.tick(30 * 60 * 1000);
    await settle();
    const all = saves();
    const out = { first: first, more: all.length - first.length, same: all.every((s) => JSON.stringify(s) === JSON.stringify(first[0])) };
    click(p.q('[data-bring]'));
    await settle();
    const here = saves().length;
    p.tick(10 * 60 * 1000);
    await settle();
    out.here = saves().length - here;
    return out;
  },

  /* The TV of a way does not answer, and does again. */
  async away_lost_way(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 120;
    way.play.lost = true;
    fire(way.media, 'waiting');
    await settle();
    const out = { lost: awayState(p), inert: p.q('[data-play]').inert, starts: p.starts().length };
    out.endInert = [p.q('[data-away-end]').inert, p.q('[data-next-btn]').inert];
    out.focus = doc.activeElement === p.q('[data-message]');
    if (how === 'keys') {
      const at = way.media.currentTime;
      key(' '); key('ArrowRight'); p.tick(2000);
      click(p.q('[data-next-btn]'));
      await settle();
      out.keys = { stops: way.stops, moved: way.media.currentTime - at, pages: p.calls.filter((c) => c.page && /502/.test(c.url)).length };
    }
    if (how === 'quiet') {
      // Back, with nothing said of what it plays; then gone again.
      way.play.quiet = true;
      fire(way.media, 'waiting');
      await settle();
      out.quiet = [messageState(p), doc.activeElement === p.q('[data-message]')];
      way.play.quiet = false;
      fire(way.media, 'waiting');
      await settle();
      out.lostAgain = messageState(p);
      // Nothing new to say: the alert's words are left alone, so they are not read out again.
      const line = p.q('[data-message-line]');
      let text = line.textContent;
      out.rewrites = 0;
      Object.defineProperty(line, 'textContent', { configurable: true, get: () => text, set: (v) => { out.rewrites++; text = v; } });
      fire(way.media, 'waiting');
      await settle();
    }
    if (how === 'found') {
      let reached = null;
      p.q('[data-play]').focus = function () { reached = this.inert; doc.activeElement = this; };
      way.play.lost = false; fire(way.media, 'canplay'); await settle();
      out.back = [doc.activeElement === p.q('[data-play]'), reached];
    }
    if (how === 'here') { click(act(p, 'Bring it here')); await settle(); out.from = p.hlsList[p.hlsList.length - 1].config.startPosition; }
    if (how === 'stop') { click(act(p, 'Stop')); await settle(); }
    out.after = awayState(p);
    out.inertAfter = p.q('[data-play]').inert;
    out.stops = way.stops;
    out.startsAfter = p.starts().length;
    return out;
  },

  /* A paired TV does not answer; `serverPlays` says what comes of it. */
  async away_lost_tv(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'tv-5');
    const play = { play_id: 'p1', rating_key: '501', state: 'playing', position_s: 40 };
    let say = { tv: Object.assign({}, LIVING, { on: false, lost_s: 12, play: play, kind: /older/.test(how) ? 'older' : 'page' }) };
    p.elsewhere.tv = () => Promise.resolve(say);
    p.tick(6000);
    await settle();
    const out = { lost: awayState(p) };
    const gets = () => p.elsewhere.sent.filter((c) => /tvs\/5$/.test(c.get || '')).length;
    if (how === 'again') { const n = gets(); click(act(p, 'Try again')); await settle(); out.asked = gets() - n; }
    if (how === 'back') { say = { tv: Object.assign({}, LIVING, { play: play }) }; p.tick(6000); await settle(); out.focus = doc.activeElement === p.q('[data-open-remote]'); }
    if (/over/.test(how)) {
      say = { tv: Object.assign({}, LIVING, { play: null, last: { reason: how === 'overTaken' ? 'taken' : 'lost', rating_key: '501', position_s: 88 } }) };
      p.tick(6000); await settle();
      out.over = awayState(p);
      out.starts = p.starts().length;
      if (how === 'overRefused') p.elsewhere.plays.push(() => p.elsewhere.refuse(409, 'tv_off'));
      if (how === 'over' || how === 'overRefused') {
        say = { tv: Object.assign({}, LIVING, { play: play }) };
        click(act(p, 'Try again')); await settle();
        out.plays = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || '')).map((c) => c.body.position_s);
        out.toasts = p.toasts.map((t) => t.text);
      }
      if (how === 'overTaken') { click(act(p, 'Close')); await settle(); }
    }
    out.after = awayState(p);
    out.startsAfter = p.starts().length;
    return out;
  },

  /* A TV that could not play it, as the page says it under the remote. */
  async away_failed_tv(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'tv-5');
    p.elsewhere.tv = () => Promise.resolve({ tv: Object.assign({}, LIVING, { kind: p.win.serverPlays, play: { rating_key: '501', state: 'failed', error: 'tv' } }) });
    p.tick(6000);
    await settle();
    return awayState(p);
  },

  /* An older TV says only as it begins whether it can play it. */
  async away_older(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    p.elsewhere.tvs = [Object.assign({}, LIVING, { id: 7, kind: 'older', name: 'Kitchen TV' })];
    p.tick(1); await openSheet(p);
    const said7 = (state, error) => ({ tv: Object.assign({}, LIVING, { id: 7, kind: 'older', name: 'Kitchen TV', play: { play_id: 'p7', rating_key: '501', state: state, error: error || null } }) });
    p.elsewhere.plays.push(() => Promise.resolve(said7('starting')));
    let next = said7('loading');
    p.elsewhere.tv = () => Promise.resolve(next);
    await pickRow(p, 'tv-7');
    p.tick(1000); await settle();
    const out = { waits: rowOf('tv-7').getAttribute('aria-busy') };
    next = how === 'plays' ? said7('playing') : said7('failed', how);
    p.tick(1000); await settle();
    out.open = sheet().open;
    out.says = said();
    out.state = awayState(p);
    out.stops = p.elsewhere.sent.filter((c) => /\/commands$/.test(c.post || '')).map((c) => c.body.play_id);
    return out;
  },

  /* Played on at once, a TV that was just paired has not been heard of yet. */
  async away_fresh(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'pair');
    const e = p.elsewhere;
    e.pairs.push(() => Promise.resolve({ tv: { id: 9, name: 'Living room' } }));
    rowOf('code').value = '123456'; fire(rowOf('code'), 'input'); await settle();
    e.tvs = [Object.assign({}, LIVING, { id: 9 })];
    const off = () => e.refuse(409, 'tv_off');
    e.plays = how === 'soon' ? [off, off] : Array.from({ length: 30 }, () => off);
    await pickRow(p, 'play');
    const out = { waits: said() };
    for (let i = 0; i < 12; i++) { p.tick(800); await settle(); }
    out.plays = e.sent.filter((c) => /\/play$/.test(c.post || '')).length;
    out.says = said();
    out.state = awayState(p).says;
    return out;
  },

  /* Full screen is left as it begins elsewhere: its button is not there then. */
  async away_fullscreen(p) {
    await streamPlays(p, 30);
    let exits = 0;
    doc.fullscreenElement = p.root;
    doc.exitFullscreen = () => { exits++; doc.fullscreenElement = null; return Promise.resolve(); };
    await castPlays(p, true);
    return exits;
  },

  /* What is told to stop on a TV is what it was last known to play. */
  async away_stop_id(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    p.elsewhere.plays.push(() => Promise.resolve({ tv: Object.assign({}, LIVING, { play: { play_id: 'p1', rating_key: '501', state: 'playing' } }) }));
    await openSheet(p);
    await pickRow(p, 'tv-5');
    const post = p.elsewhere.post;
    if (how === 'stale') {
      p.elsewhere.tv = () => Promise.resolve({ tv: Object.assign({}, LIVING, { play: { play_id: 'p2', rating_key: '999', state: 'playing', title: 'Another' } }) });
      p.elsewhere.post = (url, body, opts) => { const r = post(url, body, opts); return /commands$/.test(url) ? p.elsewhere.refuse(409, 'stale') : r; };
    }
    click(p.q('[data-bring]'));
    await settle();
    const ids = p.elsewhere.sent.filter((c) => /\/commands$/.test(c.post || '')).map((c) => c.body.play_id);
    return { ids: ids, starts: p.starts().length, says: awayState(p).says, hint: awayState(p).hint, toasts: p.toasts.map((t) => t.text) };
  },

  /* The place of a paused play that is found again, the names of its volume, and how it looks. */
  async away_paused_found(p) {
    const media = make('video');
    media.readyState = 4; media.paused = true; media.currentTime = 1800; media.muted = true;
    const way = castWay(true);
    way.play.media = media;
    const opened = p.reopen((win) => {
      way.again = () => Promise.resolve({ play: way.play, stream: { stream_id: 'k1', position_s: 1800, paused: true, rating_key: '501', keep: null } });
      win.MM.playOn.register(way);
    });
    await settle();
    const names = () => [opened.q('[data-mute]').getAttribute('aria-label'), opened.q('[data-volume]').getAttribute('aria-label')];
    const out = { time: opened.q('[data-elapsed]').textContent, names: names(), dim: opened.root.classList.contains('is-away-paused') };
    click(opened.q('[data-bring]'));
    await settle();
    out.here = names();
    out.dimHere = opened.root.classList.contains('is-away-paused');
    return out;
  },

  /* The page under a TV's own remote looks as the TV plays, not as nothing plays here. */
  async away_tv_look(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    await pickRow(p, 'tv-5');
    const look = async (state) => {
      p.elsewhere.tv = () => Promise.resolve({ tv: Object.assign({}, LIVING, { play: { rating_key: '501', state: state } }) });
      p.tick(6000); await settle();
      return ['is-away-paused', 'is-ended'].filter((c) => p.root.classList.contains(c));
    };
    return { playing: await look('playing'), paused: await look('paused') };
  },

  /* A row stays where it is while the sheet is open, whatever is heard meanwhile. */
  async away_rows(p) {
    const way = castWay(true);
    let state = { available: false, looking: true, trouble: null };
    let looks = 0;
    way.available = () => state.available;
    way.looking = () => state.looking;
    way.trouble = () => state.trouble;
    way.look = () => { looks++; };
    await streamPlays(p, 30);
    p.win.MM.playOn.register(way);
    const thrower = { id: 'bad', label: 'Bad', available() { throw new Error('no'); }, look() { throw new Error('no'); } };
    p.win.MM.playOn.register(thrower);
    await openSheet(p);
    const rows = () => sheet().querySelectorAll('.play-on__row').map((r) => [r.getAttribute('data-key'), (r.querySelector('small') || { textContent: '' }).textContent]);
    const out = { first: rows() };
    state = { available: true, looking: false, trouble: null };
    p.win.MM.playOn.changed(); await settle();
    out.found = rows().filter((r) => r[0] === 'cast');
    out.held = sheet().style.minHeight;
    p.elsewhere.tvs = [Object.assign({}, BEDROOM, { on: true }), Object.assign({}, LIVING, { id: 11, name: 'New TV' })];
    state = { available: false, looking: false, trouble: null };
    p.tick(5000); await settle();
    out.later = rows();
    state = { available: false, looking: false, trouble: { title: 'Chromecast isn’t answering', line: 'Google’s script didn’t load.' } };
    p.win.MM.playOn.changed(); await settle();
    await pickRow(p, 'cast');
    out.trouble = said();
    out.freed = sheet().style.minHeight;
    out.looks = looks;
    return out;
  },

  /* The sheet opens before the TVs were ever heard of, and the pointer may be on a row as they come. */
  async away_first(p) {
    const how = p.win.serverPlays;
    let hear;
    const opened = p.reopen((win) => win.MM.playOn.register(castWay(true)),
      { gets: [() => new Promise((yes) => { hear = () => yes({ tvs: p.elsewhere.tvs }); })] });
    await settle();
    const b = opened.win.MM.playOn.button('/watch/play/501?resume=1', 'btn');
    doc.body.appendChild(b);
    click(b);
    await settle();
    const rows = () => sheet().querySelectorAll('.play-on__row').map((r) => r.getAttribute('data-key'));
    const out = { first: rows() };
    if (how === 'slow') { opened.tick(3100); await settle(); out.slow = rows(); }
    if (how === 'reopened') {
      hear();
      await settle();
      fire(sheet(), 'pointerleave', { pointerType: 'touch' });
      opened.win.MM.playOn.close();
      opened.tick(100);
      click(b);
      fire(rowOf('pair'), 'pointerover', { bubbles: true });
      opened.tick(250);
      opened.win.MM.playOn.register(Object.assign(castWay(true), { id: 'late' }));
      await settle();
      return rows().includes('late');
    }
    if (how !== 'plain' && how !== 'slow') fire(rowOf('pair'), 'pointerover', { bubbles: true });
    hear();
    await settle();
    out.heard = rows();
    out.style = [sheet().style.minHeight, sheet().style.height];
    fire(sheet(), 'pointerleave', { pointerType: how === 'finger' ? 'touch' : 'mouse' });
    await settle();
    out.left = rows();
    opened.tick(400);
    await settle();
    out.later = rows();
    return out;
  },

  /* Where the browser shows no list of its own, none is announced. */
  async away_asks(p) {
    const way = castWay(true);
    way.chooser = { title: 'Chrome shows its own list now', line: 'Choose the TV there.' };
    way.asks = () => p.win.serverPlays === 'asks';
    way.start = () => new Promise(() => {});
    await streamPlays(p, 30);
    p.win.MM.playOn.register(way);
    await openSheet(p);
    await pickRow(p, 'cast');
    return said();
  },

  /* The sheet keeps its place where its button is gone. */
  async away_place(p) {
    await streamPlays(p, 30);
    const way = castWay(true);
    way.start = () => new Promise(() => {});
    p.win.MM.playOn.register(way);
    const btn = p.q('.player-controls').querySelector('[data-play-on]');
    btn.getBoundingClientRect = () => ({ left: 900, right: 944, top: 750, bottom: 794, width: 44, height: 44 });
    await openSheet(p);
    const out = { before: [sheet().style.left, sheet().style.bottom] };
    btn.getClientRects = () => [];
    btn.getBoundingClientRect = () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 });
    await pickRow(p, 'cast');
    out.after = [sheet().style.left, sheet().style.bottom];
    return out;
  },

  /* A title's own button, with a Cast and AirPlay. */
  async away_title_ways(p) {
    const how = p.win.serverPlays;
    const way = castWay(true);
    const steps = [];
    let now = null;
    p.win.MM.castSender = { now: () => now, holds: (key) => (how === 'otherTab' || how === 'here') && key === '501', stop: () => { steps.push('castStop'); return Promise.resolve({}); } };
    p.win.MM.tvRemote = { open(id) { steps.push('remote ' + id); }, plays: (key) => how === 'playing' && key === '501' };
    way.start = (what) => { steps.push('start'); way.what = what; return what.open().then((stream) => { way.stream = stream; return way.play; }); };
    way.play.leave = () => { steps.push('leave'); way.left++; };
    way.play.keep = (kept) => { steps.push('keep'); way.kept = kept; };
    if (how === 'airplay') p.win.MM.playOn.register({ id: 'airplay', label: 'AirPlay', icon: 'x', leads: true, available: () => true });
    else p.win.MM.playOn.register(way);
    p.pages['/watch/play/501?resume=1'] = { html: '<div id="player" data-item=\'{"title":"The Long Coast","subtitle":"S2 · E4","duration_s":3000}\' data-start-s="640" data-show-href="/watch/show/77" data-next=\'{"href":"/watch/play/502","label":"S2 · E5"}\'></div>' };
    const b = p.win.MM.playOn.button('/watch/play/501?resume=1', 'btn');
    doc.body.appendChild(b);
    const out = { key: b.getAttribute('data-play-on-key'), label: b.getAttribute('aria-label'), isOn: b.classList.contains('is-on') };
    if (how === 'refused') p.elsewhere.castFails.push(() => p.elsewhere.refuse(429, 'too_many_streams'));
    if (how === 'noTake') { const open = way.start; way.start = (what) => open(what).then(() => { throw Object.assign(new Error(), { code: 'failed', say: { title: 'The TV didn’t start', line: 'Choose it again.' } }); }); }
    p.tick(5000);
    await settle();
    if (how === 'here' || how === 'otherTab') now = how === 'here' ? { rating_key: '501' } : null;
    /* It ended there, or cannot be found: nothing plays it. */
    if (how === 'waits') now = { rating_key: '501', state: 'upnext' };
    let take;
    if (how === 'calledBack') { const open = way.start; way.start = (what) => open(what).then((play) => new Promise((yes) => { take = () => yes(play); })); }
    click(b);
    await settle();
    const rows = () => sheet().querySelectorAll('[data-key]').map((r) => r.getAttribute('data-key') + (r.getAttribute('aria-current') ? '*' : ''));
    out.rows = rows();
    if (how === 'here' || how === 'otherTab') {
      out.hereSays = rowOf('here').textContent;
      await pickRow(p, 'here');
      out.steps = steps; out.went = p.win.location.href;
      return out;
    }
    await pickRow(p, how === 'airplay' ? 'airplay' : 'cast');
    if (take) { await pickRow(p, 'here'); take(); await settle(); }
    out.stopped = way.stops;
    p.tick(1); await settle();
    out.steps = steps;
    out.went = p.win.location.href;
    if (how === 'airplay') return out;
    const asked = p.elsewhere.starts()[0];
    out.asked = { key: asked.rating_key, quality: asked.quality, asked_s: asked.asked_s };
    out.stream = way.stream && { at: way.stream.position_s, url: way.stream.url, title: way.stream.title.title };
    out.kept = way.kept && { next: way.kept.next, tracks: way.kept.given.subtitle_tracks.length };
    out.says = said();
    out.ended = p.calls.filter((c) => /\/api\/watch\/stream\/c1\/stop$/.test(c.url)).length;
    return out;
  },

  /* Led to the player for a way that needs it. */
  async away_led(p) {
    const opened = p.reopen((win) => { win.location.hash = '#play-on'; win.location.pathname = '/watch/play/501'; win.location.search = '?resume=1'; });
    await settle();
    return { open: !!sheet() && sheet().open, url: opened.win.history.urls.pop() };
  },

  /* Another page plays the title since. */
  async away_replaced(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 50;
    p.held.progressFails = { status: 410, error: 'replaced', data: { by: 'other' } };
    way.media.paused = false;
    for (let i = 0; i < 12; i++) { way.media.currentTime += 1; fire(way.media, 'timeupdate'); p.tick(1000); }
    await settle();
    return Object.assign(awayState(p), { stops: way.stops, starts: p.starts().length + p.elsewhere.casts });
  },

  /* The sheet opens another stream's way while one plays already. */
  async away_moves(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 150;
    click(p.q('.player-top').querySelector('[data-play-on]'));
    await settle();
    await pickRow(p, 'tv-5');
    const play = p.elsewhere.sent.filter((c) => /\/play$/.test(c.post || ''))[0];
    return { stops: way.stops, at: play.body.position_s, says: awayState(p).says, starts: p.starts().length };
  },

  /* Where the cards and the words of a TV's page go, by how its parts are laid out. */
  async away_cards(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    const lay = (sel, top, bottom) => { p.q(sel).getBoundingClientRect = () => ({ left: 0, right: 900, top: top, bottom: bottom, width: 900, height: bottom - top }); };
    const grown = (sel) => p.win.sized.filter((s) => s.el === p.q(sel)).forEach((s) => s.fn());
    const px = (name) => parseInt(p.root.style[name], 10);
    const wordless = () => p.root.classList.contains('is-wordless');
    const out = {};
    lay('.player-top', 0, 64); lay('.player-away-say', 300, 350); lay('.player-bottom', 400, 800);
    lay('.player-seek-row', 700, 744); lay('[data-away-end]', 750, 786);
    grown('.player-bottom');
    out.row = [px('--away-row')];
    lay('.player-seek-row', 650, 694);
    grown('.player-bottom');
    out.row.push(px('--away-row'));

    const rate = p.q('[data-rate]');
    rate.hidden = false; rate.scrollHeight = 210;
    grown('[data-rate-set]');
    out.rateFits = [wordless(), px('--away-foot')];
    rate.scrollHeight = 230;
    grown('[data-rate-set]');
    out.rateDoesNot = [wordless(), px('--away-foot')];
    rate.hidden = true;
    grown('[data-rate-set]');
    out.rateGone = wordless();

    const message = p.q('[data-message]');
    message.hidden = false;
    lay('[data-message]', 320, 480);
    grown('.player-bottom');
    out.messageMeets = wordless();
    lay('[data-message]', 370, 480);
    grown('.player-bottom');
    out.messageClear = wordless();
    message.hidden = true;

    const card = p.q('[data-upnext]');
    card.scrollHeight = 140;
    way.media.paused = true; way.media.ended = true; way.media.currentTime = 2997;
    fire(way.media, 'ended');
    await settle();
    out.under = [card.classList.contains('is-under'), px('--away-under'), wordless()];
    lay('[data-away-end]', 520, 560);
    p.win.listeners.resize.forEach((fn) => fn());
    out.noRoom = [card.classList.contains('is-under'), wordless()];
    return out;
  },

  /* Closed, the sheet sinks away over the page, which is there again at once. */
  async away_sheet_sinks(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    const was = sheet();
    const out = {};
    was.showPopover = () => { out.sinks = { open: !!was.open, inert: was.inert, kind: was.popover }; };
    was.scrollTop = 120;
    fire(was, 'scroll');
    const close = was.close;
    was.close = () => { close.call(was); was.scrollTop = 0; fire(was, 'scroll'); };
    key('Escape');
    out.top = was.scrollTop;
    const button = p.q('.player-controls').querySelector('[data-play-on]');
    out.focus = doc.activeElement === button;
    await openSheet(p);
    out.again = sheet() !== was && sheet().open && !sheet().inert;
    p.tick(1000);
    out.left = doc.querySelectorAll('.play-on').length;
    return out;
  },

  /* Played to its end on a Chromecast, which then holds nothing, and asked for once more. */
  async away_play_again(p) {
    const how = p.win.serverPlays;
    if (how === 'film') p.held.rate = { ask: true, ref: 'movie-501', title: 'Low Water', season: null };
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    const media = way.media;
    /* As the sender's: what the TV holds no more cannot be played, and a new stream is no end. */
    media.play = () => (media.ended ? Promise.reject(new Error('failed')) : make('video').play.call(media));
    const load = way.play.load;
    way.play.load = (stream) => { media.ended = false; media.paused = stream.paused; return load(stream); };
    media.paused = true; media.ended = true; media.currentTime = 2997;
    fire(media, 'ended');
    await settle();
    const card = () => !!p.q('[data-upnext]') && !gone(p.q('[data-upnext]'));
    const out = { counts: card(), rate: !gone(p.q('[data-rate]')), button: p.q('[data-bring]').textContent };
    if (how === 'cancelled' || how === 'space') { click(p.q('[data-upnext-cancel]')); await settle(); }
    const before = p.beacons.length;
    if (how === 'space') { doc.activeElement.blur(); key(' '); } else fire(p.q('[data-bring]'), 'click', { bubbles: true, detail: 1 });
    if (how === 'twice') fire(p.q('[data-bring]'), 'click', { bubbles: true, detail: 2 });
    out.card = card() || !gone(p.q('[data-rate]'));
    out.bar = [p.q('[data-elapsed]').textContent, p.q('[data-played]').style.width];
    out.starting = awayState(p).says;
    if (how === 'stop') click(p.q('[data-stop-away]'));
    await settle();
    out.stopped = p.beacons.slice(before).map((b) => b.url);
    out.loads = way.loads.map((s) => [s.stream_id, s.position_s, s.paused]);
    out.casts = p.elsewhere.casts;
    out.local = p.starts().length;
    out.saved = p.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => c.body.position_s);
    out.href = p.win.location.href;
    out.stops = p.calls.filter((c) => /\/stop$/.test(c.url)).map((c) => [c.url, c.body && c.body.position_s]);
    if (how === 'stop') { out.after = awayState(p).away; return out; }
    mediaPlays(media, 0);
    p.tick(30000);
    await settle();
    out.says = awayState(p).says;
    out.next = p.calls.some((c) => c.page && /502/.test(c.url));
    return out;
  },

  /* Played again from its start on a Chromecast, which never shows it. */
  async away_anew(p) {
    const how = p.win.serverPlays;
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    const media = way.media;
    const load = way.play.load;
    const loaded = (stream) => { media.ended = false; media.paused = stream.paused; return load(stream); };
    way.play.load = loaded;
    media.currentTime = 2990;
    for (let i = 0; i < 2; i++) { media.currentTime += 1; fire(media, 'timeupdate'); }
    media.paused = true; media.ended = true; media.currentTime = 2997;
    fire(media, 'ended');
    await settle();
    click(p.q('[data-upnext-cancel]'));
    const out = {};
    if (how === 'goneAtEnd') {
      way.gone('lost');
      await settle();
      click(act(p, 'Bring it here'));
    } else if (how === 'deviceAtEnd') {
      click(p.q('.player-top').querySelector('[data-play-on]'));
      await settle();
      await pickRow(p, 'here');
    } else if (how === 'tvAtEnd') {
      const other = castWay(true);
      other.id = 'other';
      p.win.MM.playOn.register(other);
      click(p.q('.player-top').querySelector('[data-play-on]'));
      await settle();
      await pickRow(p, 'other');
      out.other = awayState(p).says;
    } else {
      if (how === 'refused') way.play.load = (stream) => { loaded(stream); return Promise.reject(new Error('failed')); };
      if (how === 'unanswered') for (let i = 0; i < 2; i++) p.elsewhere.castFails.push(() => p.elsewhere.refuse(404, 'not_found'));
      click(p.q('[data-bring]'));
    }
    await settle();
    media.currentTime = 0;
    out.saved_s = way.loads.map((s) => s.saved_s);
    if (how === 'stop') click(p.q('[data-stop-away]'));
    if (how === 'here') click(p.q('[data-bring]'));
    if (how === 'paused') { media.paused = true; fire(media, 'pause'); p.tick(30 * 60 * 1000); }
    if (how === 'gone') way.gone('lost');
    await settle();
    out.says = awayState(p).says;
    out.hint = awayState(p).hint;
    out.begun = p.root.classList.contains('is-begun');
    out.message = !gone(p.q('[data-message]'));
    if (how === 'plays') {
      mediaPlays(media, 12);
      media.paused = false;
      media.pause();
      await settle();
    }
    p.leave();
    await settle();
    out.saved = p.calls.concat(p.beacons).filter((c) => /\/(progress|stop)$/.test(c.url) && c.body && 'position_s' in c.body && c.body.stream_id !== 's1' && !/s1/.test(c.url)).map((c) => c.body.position_s);
    out.toasts = p.toasts.map((t) => t.text);
    return out;
  },

  /* A page opened as a Chromecast has yet to play it from its start again, and left again. */
  async away_anew_opened(p) {
    const way = castWay(true);
    way.again = () => Promise.resolve({ play: way.play, stream: { stream_id: 'k1', position_s: 0, saved_s: 2997, paused: false, rating_key: '501', keep: null } });
    p.win.MM.playOn.register(way);
    p.leave();
    p.comeBack();
    await settle();
    const says = awayState(p).says;
    p.leave();
    return { says: says, saved: p.beacons.filter((b) => b.body).map((b) => b.body.position_s) };
  },

  /* What becomes of the next-episode countdown where the title comes back meanwhile. */
  async away_countdown(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, p.win.serverPlays !== 'cancelStays');
    way.media.paused = true; way.media.ended = true; way.media.currentTime = 2997;
    fire(way.media, 'ended');
    await settle();
    const out = { counts: !gone(p.q('[data-upnext]')) };
    if (/^cancel/.test(p.win.serverPlays)) { out.hint = awayState(p).hint; p.q('[data-upnext-cancel]').focus(); click(p.q('[data-upnext-cancel]')); await settle(); return Object.assign(out, { after: awayState(p).hint, focus: doc.activeElement === p.q('[data-bring]') }); }
    const how = p.win.serverPlays;
    if (how === 'stop') click(p.q('[data-stop-away]'));
    else if (how === 'here') { click(p.q('.player-top').querySelector('[data-play-on]')); await settle(); await pickRow(p, 'here'); }
    else way.gone(how);
    await settle();
    p.tick(30000);
    await settle();
    out.next = p.calls.some((c) => c.page && /502/.test(c.url));
    out.card = !gone(p.q('[data-upnext]'));
    return out;
  },

  /* The page is left while the next episode counts down, or not, and what its way is told of it. */
  async away_count_left(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.paused = true; way.media.ended = true; way.media.currentTime = 2997;
    fire(way.media, 'ended');
    await settle();
    if (p.win.serverPlays === 'cancel') { click(p.q('[data-upnext-cancel]')); await settle(); }
    const out = { counts: way.counts.map((until) => until && Math.round((until - p.win.Date.now()) / 1000)) };
    p.leave();
    out.beacons = p.beacons.map((b) => b.url.replace(/.*\/api/, ''));
    return out;
  },

  /* A title that a way holds as the one to come is watched here. */
  async away_watched(p) {
    const how = p.win.serverPlays;
    p.replies[0] = answer('best', { markers: MARKERS });
    await streamPlays(p, 30);
    const way = castWay(true);
    p.win.MM.playOn.register(way);
    if (how === 'there') { await openSheet(p); await pickRow(p, 'cast'); mediaPlays(way.media, 2990); await settle(); }
    const media = how === 'there' ? way.media : p.video;
    if (how === 'skipped') {
      media.currentTime = 2710; fire(media, 'timeupdate');
      click(p.q('[data-skip]'));
    } else if (/Left$/.test(how)) {
      media.currentTime = how === 'creditsLeft' ? 2710 : 1000; fire(media, 'timeupdate');
      p.leave();
    } else if (how !== 'left') {
      media.paused = true; media.ended = true; media.currentTime = 2999;
      fire(media, 'ended');
    }
    await settle();
    return way.seen;
  },

  /* The next episode of what ended on a TV is not reached, and is tried again. */
  async away_next_tried(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.paused = true; way.media.ended = true; way.media.currentTime = 2997;
    fire(way.media, 'ended');
    await settle();
    p.pages['/watch/play/502'] = { status: 500 };
    click(p.q('[data-upnext-play]'));
    await settle();
    const out = { message: messageState(p), ended: p.root.classList.contains('is-ended') };
    let hidden = null;
    p.q('[data-play]').focus = function () { hidden = p.root.classList.contains('is-ended'); doc.activeElement = this; };
    press(p.q('[data-retry]'));
    return Object.assign(out, { hidden: hidden });
  },

  /* The page opens, or comes back from the page cache, while a TV plays a title. */
  async away_opened_on_tv(p) {
    const how = p.win.serverPlays;
    const on = [Object.assign({}, LIVING, { play: { rating_key: how === 'other' ? '999' : '501', state: how === 'ended' ? 'ended' : 'paused', position_s: 400 } })];
    if (how === 'cache') {
      await streamPlays(p, 30);
      p.elsewhere.tvs = on;
      p.storage['mediaman.watch.onTv'] = '1';
      const starts = p.starts().length;
      p.leave();
      p.comeBack();
      await settle();
      return { starts: p.starts().length - starts, says: awayState(p).says };
    }
    /* Whether a TV was known to play anything when this browser last heard. */
    const known = /^(unknown|told)/.test(how) ? {} : { 'mediaman.watch.onTv': '1' };
    let hear;
    const pending = new Promise((yes, no) => { hear = { none: () => yes({ tvs: [LIVING] }), tv: () => yes({ tvs: on }), fails: () => no({ status: 0, error: 'network_error' }) }; });
    const waits = /^(unknown|silent|late|paused)/.test(how) || how === 'pressed';
    const opened = p.reopen(null, waits ? { gets: [() => pending, () => pending] } : { tvs: on }, known);
    const out = { atOnce: opened.starts().length };
    await settle();
    if (how === 'pausedLook') {
      key(' ');
      hear.none();
      await settle();
      opened.hlsList[0].emit('hlsManifestParsed');
      await settle();
      return { starts: opened.starts().length, plays: opened.video.plays };
    }
    if (how === 'pressed') {
      key(' ');
      click(opened.q('[data-play]'));
      click(opened.q('[data-quality="low"]'));
      await settle();
      out.pressed = opened.starts().length;
      hear.tv();
      await settle();
    }
    out.starts = opened.starts().length;
    out.says = awayState(opened).says;
    if (waits && how !== 'pressed') {
      opened.tick(1400); await settle();
      out.message = messageState(opened);
      opened.tick(200); await settle();
      out.slow = { starts: opened.starts().length, button: (messageState(opened) || {}).button || null };
      if (how === 'lateNone') hear.none();
      if (how === 'lateTv') hear.tv();
      if (how === 'lateFails') hear.fails();
      await settle();
      out.after = { starts: opened.starts().length, says: awayState(opened).says, button: (messageState(opened) || {}).button || null };
    }
    out.known = opened.storage['mediaman.watch.onTv'] || null;
    if (how === 'same') { click(opened.q('[data-bring]')); await settle(); out.from = opened.hlsList[0].config.startPosition; }
    return out;
  },

  /* It is long until a way knows whether it still plays the title. */
  async away_again_slow(p) {
    const how = p.win.serverPlays;
    const way = castWay(true);
    way.media.readyState = 4; way.media.paused = false; way.media.currentTime = 300;
    let found;
    let lost;
    const opened = p.reopen((win) => {
      way.again = (key) => { way.asked = key; way.times = (way.times || 0) + 1; return new Promise((yes, no) => { found = yes; lost = no; }); };
      win.MM.playOn.register(way);
    });
    await settle();
    const play = { play: way.play, stream: { stream_id: 'k1', position_s: 300, paused: false, rating_key: '501', keep: null } };
    if (how === 'over') found(null);
    if (how === 'lost' || how === 'elsewhere') lost(Object.assign(new Error(), { code: how }));
    way.release = (key) => { way.released = [key, opened.starts().length]; };
    await settle();
    opened.tick(3000);
    await settle();
    const out = { asked: way.asked, early: opened.starts().length, told: messageState(opened) };
    opened.tick(30000);
    await settle();
    out.message = messageState(opened);
    out.starts = opened.starts().length;
    if (how === 'overLate') found(null);
    if (how === 'found') found(play);
    if (how === 'pressed') { click(opened.q('[data-retry]')); await settle(); found(play); }
    if (how === 'elsewhere') { click(opened.q('[data-retry]')); await settle(); }
    if (how === 'left') { opened.leave(); opened.comeBack(); await settle(); opened.tick(9000); await settle(); }
    if (how === 'leftLost') {
      lost(Object.assign(new Error(), { code: 'lost' }));
      await settle();
      opened.leave(); opened.comeBack(); await settle();
      out.again = { times: way.times, starts: opened.starts().length, message: messageState(opened) };
      found(null);
      await settle();
      out.over = { starts: opened.starts().length, message: messageState(opened) };
      opened.leave(); opened.comeBack(); await settle();
      found(play);
      await settle();
    }
    out.times = way.times;
    await settle();
    out.after = { starts: opened.starts().length, left: way.left, released: way.released || null, says: awayState(opened).says, message: messageState(opened) };
    return out;
  },

  /* What a rejoined play needs is kept as it starts, and is there again. */
  async away_kept(p) {
    p.replies[0] = answer('best', { markers: MARKERS, chapters: CHAPTERS });
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    const again = castWay(true);
    again.media.readyState = 4; again.media.paused = true; again.media.currentTime = 2710;
    const opened = p.reopen((win) => {
      again.again = () => Promise.resolve({ play: again.play, stream: { stream_id: 'k1', position_s: 2710, paused: true, rating_key: '501', keep: Object.assign(JSON.parse(JSON.stringify(way.kept)), { played_s: 40 }) } });
      win.MM.playOn.register(again);
    });
    await settle();
    const out = {
      tracks: opened.q('[data-tracks]').querySelectorAll('[data-subtitle]').map((b) => b.getAttribute('data-subtitle') + (b.getAttribute('aria-checked') === 'true' ? '*' : '')),
      chapters: opened.q('[data-chapter-menu]').querySelectorAll('[data-chapter]').length,
      skip: gone(opened.q('[data-skip]')) ? null : opened.q('[data-skip-text]').textContent,
    };
    out.keptAgain = [again.kept.markers.length, again.kept.chapters.length];
    out.next = way.kept.next;
    opened.tick(30 * 60 * 1000);
    await settle();
    out.saves = opened.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => [c.body.stream_id, c.body.position_s, c.body.played_s]);
    return out;
  },

  /* Brought back or stopped while it begins elsewhere: the sheet is free again. */
  async away_cancelled(p) {
    const how = p.win.serverPlays;
    const way = castWay(true);
    await streamPlays(p, 30);
    p.win.MM.playOn.register(way);
    await openSheet(p);
    let answerTv;
    let aborted = false;
    p.elsewhere.plays.push((opts) => new Promise((r, no) => {
      answerTv = () => r({ tv: Object.assign({}, LIVING, { play: { play_id: 'p7', rating_key: '501', state: 'starting' } }) });
      opts.signal.addEventListener('abort', () => { aborted = true; no(Object.assign(new Error(), { name: 'AbortError' })); });
    }));
    p.elsewhere.castFails.push((opts) => new Promise((r, no) => opts.signal.addEventListener('abort', () => { aborted = true; no(Object.assign(new Error(), { name: 'AbortError' })); })));
    const starts = p.starts().length;
    await pickRow(p, /way|late/.test(how) ? 'cast' : 'tv-5');
    const out = { says: awayState(p).says };
    if (how === 'wayLate') { p.tick(130000); await settle(); out.told = said(); } else click(p.q(/Stop/.test(how) ? '[data-stop-away]' : '[data-bring]'));
    await settle();
    out.aborted = aborted;
    out.here = { starts: p.starts().length - starts, away: awayState(p).away };
    const stops = () => p.elsewhere.sent.filter((c) => /\/commands$/.test(c.post || '')).map((c) => c.body.play_id || null);
    out.early = stops();
    if (answerTv) answerTv();
    await settle();
    out.stopped = stops();
    out.after = awayState(p).away;
    out.busy = rowOf('tv-5').getAttribute('aria-busy');
    out.here.row = rowOf('here').getAttribute('aria-current');
    await pickRow(p, 'tv-5');
    out.again = awayState(p).says;
    return out;
  },

  /* The page is left while a TV begins, and may come back. */
  async away_left_starting(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    let answerTv;
    let aborted = false;
    p.elsewhere.plays.push((opts) => new Promise((r) => { answerTv = () => r({ tv: Object.assign({}, LIVING, { play: { play_id: 'p7', rating_key: '501', state: 'starting' } }) }); opts.signal.addEventListener('abort', () => { aborted = true; }); }));
    await pickRow(p, 'tv-5');
    const starts = p.starts().length;
    p.leave();
    await settle();
    const out = { aborted: aborted };
    /* It may not be said to begin yet as the page comes back. */
    if (p.win.serverPlays === 'said') p.elsewhere.tvs = [Object.assign({}, LIVING, { play: { rating_key: '501', state: 'starting' } })];
    p.comeBack();
    await settle();
    answerTv();
    await settle();
    out.stops = p.elsewhere.sent.filter((c) => /\/commands$/.test(c.post || '')).length;
    out.starts = p.starts().length - starts;
    out.ended = p.calls.filter((c) => /\/stop$/.test(c.url)).length;
    out.says = awayState(p).says;
    return out;
  },

  /* Back from the page cache, a way is asked again what it plays. */
  async away_again_cache(p) {
    await streamPlays(p, 30);
    const way = castWay(true);
    way.media.readyState = 4; way.media.paused = false; way.media.currentTime = 300;
    way.again = () => Promise.resolve({ play: way.play, stream: { stream_id: 'k1', position_s: 300, paused: false, rating_key: '501', keep: null } });
    p.win.MM.playOn.register(way);
    const starts = p.starts().length;
    p.leave();
    p.comeBack();
    await settle();
    return { starts: p.starts().length - starts, says: awayState(p).says };
  },

  /* "Play here" of a title that may be on a TV; `serverPlays` says who knows its place. */
  async away_again_place(p) {
    const how = p.win.serverPlays;
    const way = castWay(true);
    const ms = 10 * 60 * 1000;
    const opened = p.reopen((win) => {
      way.again = () => Promise.reject(Object.assign(new Error(), { code: 'lost' }));
      way.release = () => (how === 'none' ? null : { position_s: 900, played_s: 60, age_s: how === 'way' || how === 'sought' ? 20 : 300 });
      win.MM.playOn.register(way);
    });
    await settle();
    opened.tick(ms);
    if (how === 'sought') { key('ArrowRight'); opened.tick(300); }
    /* Saved meanwhile by what plays it elsewhere, as the page now says. */
    const pages = opened.pages;
    pages['/watch/play/1'] = how === 'offline' ? 'offline' : { html: '<div id="player" data-place-s="' + (how === 'same' ? '' : 640) + '"></div>' };
    await settle();
    click(opened.q('[data-retry]'));
    await settle();
    const out = { starts: opened.starts().length, from: opened.hlsList.length ? opened.hlsList[0].config.startPosition : null, message: messageState(opened) };
    if (how === 'offline') {
      pages['/watch/play/1'] = { html: '<div id="player" data-place-s="640"></div>' };
      click(opened.q('[data-retry]'));
      await settle();
      out.again = opened.hlsList[0].config.startPosition;
    }
    return out;
  },

  /* The place of a title that ended on a TV has moved on elsewhere by the press. */
  async away_gone_place(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 120;
    way.gone('lost');
    await settle();
    p.pages['/watch/play/1'] = { html: '<div id="player" data-place-s="800"></div>' };
    click(p.q('[data-retry]'));
    await settle();
    return p.hlsList[p.hlsList.length - 1].config.startPosition;
  },

  /* What a way finds while the look at the TVs is out, or fails. */
  async away_again_and_tv(p) {
    const how = p.win.serverPlays;
    const way = castWay(true);
    way.media.readyState = 4; way.media.paused = false; way.media.currentTime = 300;
    let hear;
    const pending = new Promise((yes, no) => { hear = { tv: () => yes({ tvs: [Object.assign({}, LIVING, { play: { rating_key: '501', state: 'playing', position_s: 400 } })] }), fails: () => no({ status: 0, error: 'network_error' }) }; });
    let found;
    const opened = p.reopen((win) => { way.again = () => new Promise((yes) => { found = yes; }); win.MM.playOn.register(way); },
      { gets: [() => pending] }, { 'mediaman.watch.onTv': '1' });
    const play = { play: way.play, stream: { stream_id: 'k1', position_s: 300, paused: false, rating_key: '501', keep: null } };
    if (/First$/.test(how)) found(play);
    await settle();
    if (/pressed/.test(how)) { key(' '); await settle(); }
    if (/^next/.test(how)) { Object.assign(opened.pages, p.pages); click(opened.q('[data-next-btn]')); await settle(); }
    else if (/^tv/.test(how)) hear.tv();
    else hear.fails();
    await settle();
    const out = { message: messageState(opened) };
    if (!/First$/.test(how)) { found(how === 'failsNull' ? null : play); await settle(); }
    return Object.assign(out, { left: way.left, starts: opened.starts().length, says: awayState(opened).says, paused: way.media.paused, after: messageState(opened) });
  },

  /* It may be on a TV, and another TV that is picked does not start. */
  async away_again_refused(p) {
    const way = castWay(true);
    const [code, on] = p.win.serverPlays.split(' ');
    const opened = p.reopen((win) => { way.again = () => Promise.reject(Object.assign(new Error(), { code: code })); win.MM.playOn.register(way); });
    await settle();
    opened.elsewhere.plays.push(() => opened.elsewhere.refuse(409, 'tv_off'));
    opened.elsewhere.castFails.push(() => opened.elsewhere.refuse(502, 'unreachable'));
    await openSheet(opened);
    await pickRow(opened, on || 'tv-5');
    const out = { asked: opened.elsewhere.sent.some((c) => /\/play$/.test(c.post || '')), starts: opened.starts().length, message: messageState(opened) };
    click(opened.q('[data-retry]'));
    await settle();
    out.pressed = opened.starts().length;
    return out;
  },

  /* A play found while the person has gone on to the next episode. */
  async away_again_passed(p) {
    const way = castWay(true);
    let found;
    const opened = p.reopen((win) => { way.again = () => new Promise((yes) => { found = yes; }); win.MM.playOn.register(way); });
    await settle();
    Object.assign(opened.pages, p.pages);
    click(opened.q('[data-next-btn]'));
    await settle();
    found({ play: way.play, stream: { stream_id: 'k1', position_s: 300, paused: false, rating_key: '501', keep: null } });
    await settle();
    return { left: way.left, starts: opened.calls.filter((c) => /watch\/stream$/.test(c.url)).map((c) => c.body.rating_key), says: awayState(opened).says };
  },

  /* Moved from a TV that plays to one that does not start. */
  async away_moved_refused(p) {
    await streamPlays(p, 30);
    const way = await castPlays(p, true);
    way.media.currentTime = 150;
    click(p.q('.player-top').querySelector('[data-play-on]'));
    await settle();
    p.elsewhere.plays.push(() => p.elsewhere.refuse(409, 'tv_off'));
    await pickRow(p, 'tv-5');
    return { stops: way.stops, says: said(), from: p.hlsList[p.hlsList.length - 1].config.startPosition };
  },

  /* A look for TVs goes unanswered in an open sheet. */
  async away_looks_on(p) {
    await streamPlays(p, 30);
    await openSheet(p);
    p.elsewhere.gets.push(() => p.elsewhere.refuse(0, 'network_error'));
    p.tick(5000);
    await settle();
    const of5 = () => rowOf('tv-5').querySelector('small').textContent;
    const out = { failed: of5() };
    p.elsewhere.tvs = [BEDROOM];
    p.tick(5000);
    await settle();
    out.later = of5();
    return out;
  },

  /* A way finds what it plays as the page opens. */
  async away_again(p) {
    const media = make('video');
    media.readyState = 4; media.paused = false; media.currentTime = 300;
    const way = castWay(true);
    way.play.media = media;
    const other = p.win.serverPlays === 'other';
    const found = { play: way.play, stream: { stream_id: 'k1', position_s: 300, paused: false, rating_key: other ? '999' : '501', keep: { quality: 'low', subtitle: 'off', played_s: 120 } } };
    const opened = await p.reopen((win) => { way.again = () => Promise.resolve(found); win.MM.playOn.register(way); });
    await settle();
    const out = { starts: opened.starts().length, left: way.left, says: awayState(opened).says };
    if (!other) {
      media.currentTime = 301; fire(media, 'timeupdate'); media.currentTime = 302; fire(media, 'timeupdate');
      media.pause();
      await settle();
      out.saved = opened.calls.filter((c) => /\/progress$/.test(c.url)).map((c) => c.body).pop();
    }
    return out;
  },

  /* A title that is slow to start: the name, from a third of a second in, until it plays. */
  async intro_slow(p) {
    await settle();
    p.tick(250);
    const early = introState(p);
    p.tick(100);
    const late = introState(p);
    p.tick(4000);
    const waits = introState(p);
    await streamPlays(p, 0);
    return { early: early, late: late, waits: waits, playing: introState(p) };
  },

  /* A title that starts at once never shows the name. */
  async intro_fast(p) {
    let ever = false;
    const cl = p.root.classList;
    const add = cl.add.bind(cl);
    cl.add = (...names) => { ever = ever || names.indexOf('is-intro') >= 0; return add(...names); };
    await settle();
    p.tick(200);
    await streamPlays(p, 0);
    p.tick(1000);
    return Object.assign(introState(p), { ever: ever });
  },

  /* Buffering and seeking once it plays keep the spinner. */
  async intro_midplay(p) {
    await streamPlays(p, 30);
    fire(p.video, 'waiting');
    p.tick(1000);
    const waiting = introState(p);
    fire(p.video, 'playing');
    p.video.seeking = true;
    p.video.currentTime = 900;
    fire(p.video, 'seeking');
    fire(p.video, 'waiting');
    p.tick(1000);
    return { waiting: waiting, seeking: introState(p) };
  },

  /* The next episode is a title that starts. */
  async intro_next(p) {
    await streamPlays(p, 100);
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await settle();
    p.tick(400);
    const loading = Object.assign(introState(p), { held: holdState(p).shown });
    const v = p.video;
    await settle();
    p.hlsList[p.hlsList.length - 1].emit('hlsManifestParsed');
    await settle();
    v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.currentTime = 0;
    fire(v, 'playing');
    return { loading: loading, playing: Object.assign(introState(p), { held: holdState(p).shown }) };
  },

  /* A start that fails while the name shows: the message is not drawn over, and Try again starts it again. */
  async intro_error(p) {
    await settle();
    p.tick(400);
    const before = introState(p);
    p.replies.push({ err: { status: 503 } }, answer('1080p-8000'));
    p.hlsList[0].emit('hlsError', { fatal: true, type: 'networkError' });
    await settle();
    const cl = p.root.classList;
    const failed = Object.assign(introState(p), { out: cl.contains('is-intro-out'), message: messageState(p) !== null });
    press(p.q('[data-retry]'));
    await settle();
    p.tick(400);
    const again = Object.assign(introState(p), { message: messageState(p) !== null });
    await streamPlays(p, 0);
    return { before: before, failed: failed, again: again, playing: introState(p) };
  },

  /* Autoplay is refused and the picture is sought before it first plays: no name. */
  async intro_seek_first(p) {
    await settle();
    p.hlsList[0].emit('hlsManifestParsed');
    await settle();
    const v = p.video;
    v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720;
    v.paused = true;
    fire(v, 'pause');
    fire(v, 'canplay');
    v.seeking = true;
    fire(v, 'seeking');
    fire(v, 'waiting');
    p.tick(1000);
    return introState(p);
  },

  /* Back from the page cache while the name shows or waits. */
  async intro_back(p) {
    await settle();
    p.tick(400);
    const shown = introState(p);
    p.leave();
    p.comeBack();
    await settle();
    p.tick(1000);
    await streamPlays(p, 0);
    p.tick(1000);
    return { shown: shown, after: introState(p), out: p.root.classList.contains('is-intro-out') };
  },

  /* A second name, once the first has gone out, is drawn from the start: neither class holds it at a reflow. */
  async intro_restart(p) {
    await settle();
    p.tick(400);
    await streamPlays(p, 100);
    const cl = p.root.classList;
    const out = cl.contains('is-intro-out');
    const reflows = [];
    Object.defineProperty(p.root, 'offsetWidth', { get() { reflows.push([cl.contains('is-intro'), cl.contains('is-intro-out')]); return 0; } });
    p.replies.push(answer('1080p-8000'));
    click(p.q('[data-next-btn]'));
    await settle();
    p.tick(400);
    return { out: out, reflows: reflows, intro: cl.contains('is-intro') };
  },

  /* Sent to a TV as it starts: the page shows what it shows today. */
  async away_intro(p) {
    p.elsewhere.plays.push(() => new Promise(() => {}));
    await openSheet(p);
    await pickRow(p, 'tv-5');
    p.tick(1000);
    await settle();
    return introState(p);
  },
};

function introState(p) {
  return { intro: p.root.classList.contains('is-intro'), starting: p.root.classList.contains('is-starting'), loading: p.root.classList.contains('is-loading') };
}

(async () => {
  const [file, name, reduced, serverPlays] = process.argv.slice(2);
  const saved = reduced && reduced[0] === '{' ? JSON.parse(reduced) : null;
  const elsewhere = /^away_/.test(name) ? elsewhereOf(serverPlays) : null;
  const p = boot(fs.readFileSync(file, 'utf8'), reduced === 'reduced', saved, serverPlays, null, elsewhere);
  p.elsewhere = elsewhere;
  if (elsewhere) {
    p.held.answer = () => answer('best');
    p.win.MM.tvRemote = { open(tv) { p.opened = tv.id; } };
  }
  p.reopen = (ready, more, stored) => {
    const e = Object.assign(elsewhereOf(serverPlays), { ready: ready }, more);
    const opened = boot(fs.readFileSync(file, 'utf8'), false, Object.assign({}, saved, stored), serverPlays, null, e);
    opened.elsewhere = e;
    opened.held.answer = () => answer('best');
    return opened;
  };
  Object.entries(JSON.parse(process.env.PLAYER_PAGES || '{}')).forEach(([href, f]) => {
    p.pages[href] = { html: fs.readFileSync(f, 'utf8') };
  });
  const out = await scenarios[name](p);
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
