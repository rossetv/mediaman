/* Runs player.js against a small fake DOM built from the rendered player
   markup, with fake hls.js, API, clock and video element.

   Usage: node player_harness.js <markup.html> <scenario> [reduced]
   Prints the scenario's observations as one JSON object. */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PLAYER_JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/player.js');
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
    this.style = {};
    const el = this;
    this.classList = {
      list() { return (el.attrs.class || '').split(/\s+/).filter(Boolean); },
      contains(c) { return this.list().indexOf(c) >= 0; },
      add(c) { if (!this.contains(c)) el.attrs.class = this.list().concat(c).join(' '); },
      remove(c) { el.attrs.class = this.list().filter((x) => x !== c).join(' '); },
      toggle(c, on) {
        if (on === undefined) on = !this.contains(c);
        if (on) this.add(c); else this.remove(c);
        return on;
      },
    };
  }
  getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  removeAttribute(n) { delete this.attrs[n]; }
  hasAttribute(n) { return n in this.attrs; }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  matches(sel) {
    if (sel === ':focus-visible') return false;
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
  focus() { doc.activeElement = this; }
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
  }
  play() {
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

function make(tag, attrs) {
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
  const ev = Object.assign({ type: type, bubbles: false, preventDefault() {}, stopPropagation() { this.stopped = true; } }, extra || {});
  target.dispatchEvent(ev);
  return ev;
}

function click(el) { fire(el, 'click', { bubbles: true, button: 0 }); }

/* ---- the page ------------------------------------------------------------ */

function boot(html, reduced) {
  doc = new Node();
  doc.documentElement = make('html');
  doc.appendChild(doc.documentElement);
  doc.body = make('body');
  doc.documentElement.appendChild(doc.body);
  parse(html, doc.body);
  doc.activeElement = doc.body;
  doc.visibilityState = 'visible';
  doc.getElementById = (id) => doc.querySelector('#' + id);
  doc.createElement = (tag) => make(tag.toLowerCase());
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
      clock.now = due.at;
      if (due.every) due.at += due.every; else clearTimer(due.id);
      due.fn();
    }
    clock.now = end;
  }

  const calls = [];
  /* The page starts its first stream as soon as it loads. */
  const replies = [{ ok: { stream_id: 's1', playlist: '/p1' } }];
  const api = {
    post(url, body) {
      calls.push({ url: url, body: body });
      if (!/\/api\/watch\/stream$/.test(url)) return Promise.resolve({});
      const reply = replies.shift();
      if (!reply) return new Promise(() => {});
      return reply.ok ? Promise.resolve(reply.ok) : Promise.reject(reply.err);
    },
    messageOf(err, fallback) { return fallback; },
  };

  const hlsList = [];
  class Hls {
    constructor(cfg) { this.cfg = cfg; this.handlers = {}; this.destroyed = false; hlsList.push(this); }
    static isSupported() { return true; }
    on(name, fn) { this.handlers[name] = fn; }
    emit(name, data) { if (this.handlers[name]) this.handlers[name](name, data || {}); }
    loadSource(src) { this.src = src; }
    attachMedia(v) { this.media = v; }
    startLoad() {}
    recoverMediaError() {}
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
  Hls.Events = { ERROR: 'hlsError', MANIFEST_PARSED: 'hlsManifestParsed' };
  Hls.ErrorTypes = { MEDIA_ERROR: 'mediaError', NETWORK_ERROR: 'networkError' };

  const toasts = [];
  const storage = {};
  const win = {
    document: doc,
    Hls: Hls,
    MM: { api: api },
    UIFeedback: {
      error(text) { toasts.push({ kind: 'error', text: text }); },
      toast(text, opts) { toasts.push({ kind: (opts && opts.variant) || 'toast', text: text }); },
    },
    localStorage: {
      getItem: (k) => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
    location: { href: '/watch/play/1' },
    navigator: { sendBeacon() { return true; } },
    matchMedia: (q) => ({ matches: !!reduced && /prefers-reduced-motion:\s*reduce/.test(q) }),
    setTimeout: (fn, ms) => addTimer(fn, ms, false),
    clearTimeout: clearTimer,
    setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearInterval: clearTimer,
    requestAnimationFrame: (fn) => addTimer(fn, 16, false),
    Blob: function Blob() {},
    console: console,
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  };
  win.window = win;
  win.navigator = win.navigator;
  const ctx = vm.createContext(win);
  vm.runInContext(fs.readFileSync(PLAYER_JS, 'utf8'), ctx, { filename: 'player.js' });

  const root = doc.getElementById('player');
  return {
    root: root,
    video: root.querySelector('video'),
    hold: root.querySelector('[data-hold]'),
    hlsList: hlsList,
    replies: replies,
    calls: calls,
    toasts: toasts,
    storage: storage,
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
  v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.currentTime = pos || 0;
  fire(v, 'playing');
}

function playState(p) {
  const btn = p.q('[data-play]');
  return { label: btn.getAttribute('aria-label'), busy: btn.getAttribute('aria-busy'), icon: btn.querySelector('i').className };
}

function holdState(p) {
  return {
    shown: !!p.hold && !p.hold.hidden,
    drewVideo: !!p.hold && p.hold.drawn.some((d) => d.src === p.video && d.w === 1280 && d.h === 720),
  };
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
    return { whileLoading: whileLoading, afterManifest: afterManifest, afterPlaying: { hold: holdState(p), play: playState(p), held: p.root.classList.contains('is-held') } };
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
    fire(card, 'animationend', { target: card, animationName: 'player-drop' });
    const afterExit = { hidden: card.hidden, leaving: card.classList.contains('is-leaving'), focus: doc.activeElement.getAttribute('data-play') !== null };
    p.tick(20000);
    return { shown: shown, afterClick: afterClick, afterExit: afterExit, href: p.win.location.href };
  },
};

(async () => {
  const [file, name, reduced] = process.argv.slice(2);
  const p = boot(fs.readFileSync(file, 'utf8'), reduced === 'reduced');
  const out = await scenarios[name](p);
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
