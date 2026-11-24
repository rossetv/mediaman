/* Runs player.js against a small fake DOM built from the rendered player
   markup, with fake hls.js, API, clock and video element.

   Usage: node player_harness.js <markup.html> <scenario> [reduced]
          node player_harness.js <markup.html> <scenario> '<saved settings as JSON>'
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
  toggleAttribute(n, on) { if (on) this.attrs[n] = ''; else delete this.attrs[n]; }
  get id() { return this.attrs.id || ''; }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = v; }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
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
  }
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

/* A press of the mouse or a finger: what it lands on takes the focus. */
function press(el, pointerType) {
  doc.lastInput = 'pointer';
  fire(el, 'pointerdown', { bubbles: true, button: 0, pointerType: pointerType || 'mouse', pointerId: 1, clientX: 0 });
  if (/^(BUTTON|A|INPUT)$/.test(el.tagName)) el.focus();
  else doc.activeElement = doc.body;
  click(el);
}

/* A key, sent to what has the focus; Space and Enter press a button, unless the page says no. */
function key(k) {
  doc.lastInput = 'key';
  const target = doc.activeElement;
  let prevented = false;
  fire(target, 'keydown', { bubbles: true, key: k, preventDefault() { prevented = true; } });
  const presses = k === 'Enter' || k === ' ';
  if (presses && !prevented && target.tagName === 'BUTTON' && doc.activeElement === target) click(target);
}

/* Focus moved by Tab. */
function tabTo(el) { key('Tab'); el.focus(); }

/* ---- the page ------------------------------------------------------------ */

function boot(html, reduced, saved) {
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
    constructor(cfg) { this.config = cfg; this.startsAt = null; this.handlers = {}; this.destroyed = false; hlsList.push(this); }
    static isSupported() { return true; }
    on(name, fn) { this.handlers[name] = fn; }
    emit(name, data) {
      /* Like hls.js: loading starts at the configured place, once the levels are known. */
      if (name === 'hlsManifestParsed') this.startsAt = this.config.startPosition;
      if (this.handlers[name]) this.handlers[name](name, data || {});
    }
    loadSource(src) { this.src = src; }
    attachMedia(v) { this.media = v; }
    startLoad(t) { if (this.startsAt !== null) this.startsAt = t; }
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
  const storage = Object.assign({}, saved);
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
  ['core/dom.js', 'player.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx, { filename: f }));

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
  const icon = btn.querySelector('.ico');
  return {
    label: btn.getAttribute('aria-label'),
    busy: btn.getAttribute('aria-busy'),
    icon: icon.hasAttribute('hidden') ? null : icon.querySelector('use').getAttribute('href'),
    ring: !btn.querySelector('.player-btn-ring').hidden,
  };
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
    return { afterMouse: afterMouse, afterTab: idle() };
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
    const shown = !p.q('[data-message]').hidden;
    const before = starts();
    p.replies.push({ ok: { stream_id: 's3', playlist: '/p3' } });
    press(p.q('[data-play]'));
    await settle();
    const afterPlay = { started: starts() - before, shown: !p.q('[data-message]').hidden, busy: playState(p).busy };
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
    return { text: p.q('[data-message-text]').textContent, retry: !p.q('[data-retry]').hidden, playDisabled: p.q('[data-play]').disabled === true };
  },

  /* The volume the page starts with, and what is kept when it changes. */
  async volume(p) {
    const start = { volume: p.video.volume, muted: p.video.muted, mute: p.q('[data-mute]').getAttribute('aria-label'), fill: p.q('[data-volume]').style['--fill'] };
    p.video.muted = false; p.video.volume = 0.25;
    fire(p.video, 'volumechange');
    const unmuted = Object.assign({}, p.storage);
    press(p.q('[data-mute]'));
    fire(p.video, 'volumechange');
    return { start: start, unmuted: unmuted, muted: Object.assign({}, p.storage), fill: p.q('[data-volume]').style['--fill'] };
  },

  /* The keys of a slider on the seek bar. */
  async seek_keys(p) {
    await streamPlays(p, 600);
    tabTo(p.q('[data-seek]'));
    const out = {};
    for (const k of ['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown']) {
      p.video.currentTime = 600;
      key(k);
      out[k] = p.video.currentTime - 600;
    }
    return out;
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
};

(async () => {
  const [file, name, reduced] = process.argv.slice(2);
  const saved = reduced && reduced[0] === '{' ? JSON.parse(reduced) : null;
  const p = boot(fs.readFileSync(file, 'utf8'), reduced === 'reduced', saved);
  const out = await scenarios[name](p);
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
