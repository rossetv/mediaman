// Runs watch/cast-sender.js against a stand-in for Google's Cast script, a TV and a server whose
// answers the scenario gives, on a clock it steps, and prints one scenario's outcome as JSON.
// Usage: node watch_cast_sender_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const T0 = Date.UTC(2026, 0, 5, 12, 0, 0);
const RECORD = 'mediaman.watch.cast';
const settle = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

// A clock that only the scenario moves.
function makeClock() {
  let t = T0;
  let id = 0;
  const due = new Map();
  const add = (fn, ms, every) => { due.set(++id, { at: t + (ms || 0), fn, every }); return id; };
  return {
    now: () => t,
    setTimeout: (fn, ms) => add(fn, ms, null),
    setInterval: (fn, ms) => add(fn, ms, ms),
    clear: (n) => due.delete(n),
    waiting: () => due.size,
    async advance(ms) {
      await settle();
      const end = t + ms;
      for (;;) {
        const next = [...due.entries()].filter(([, d]) => d.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [n, d] = next;
        t = Math.max(t, d.at);
        if (d.every) d.at += d.every; else due.delete(n);
        d.fn();
        await settle();
      }
      t = end;
      await settle();
    },
  };
}

// A TV with the default receiver, and what of it outlives a page.
function makeTv(clock) {
  return { clock, name: 'Living room', loads: [], media: [], level: 0.4, muted: false, fixed: false, sessionId: 'sess-1', ended: [], refuses: false, hangs: false, answers: true, calls: [] };
}

class Media {
  constructor(tv, request) {
    this.tv = tv;
    this.media = request.media;
    this.currentTime = request.currentTime;
    this.playerState = 'BUFFERING';
    this.idleReason = null;
    this.listeners = [];
    this.at = tv.clock.now();
  }
  addUpdateListener(fn) { this.listeners.push(fn); }
  removeUpdateListener(fn) { this.listeners = this.listeners.filter((f) => f !== fn); }
  getEstimatedTime() {
    const ran = this.playerState === 'PLAYING' ? (this.tv.clock.now() - this.at) / 1000 : 0;
    return Math.min(this.currentTime + ran, this.media.duration || Infinity);
  }
  // The TV says what it does; `alive` as the SDK gives it.
  say(state, extra, alive = true) {
    this.currentTime = this.getEstimatedTime();
    this.at = this.tv.clock.now();
    this.playerState = state;
    Object.assign(this, extra);
    this.listeners.slice().forEach((fn) => fn(alive));
  }
  _do(name, then, ok, fail) {
    this.tv.calls.push(name);
    if (this.tv.hangs) return;
    Promise.resolve().then(() => { if (this.tv.refuses) fail({ code: 'timeout' }); else { then(); ok(); } });
  }
  play(_, ok, fail) { this._do('play', () => this.say('PLAYING'), ok, fail); }
  stop(_, ok, fail) { this._do('stop', () => this.say('IDLE', { idleReason: 'CANCELLED' }), ok, fail); }
  pause(_, ok, fail) { this._do('pause', () => this.say('PAUSED'), ok, fail); }
  seek(request, ok, fail) {
    this._do('seek ' + request.currentTime, () => { this.currentTime = request.currentTime; this.at = this.tv.clock.now(); this.say(this.playerState); }, ok, fail);
  }
}

class Emitter {
  constructor() { this.on = {}; }
  addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); }
  removeEventListener(type, fn) { this.on[type] = (this.on[type] || []).filter((f) => f !== fn); }
  emit(type, e) { (this.on[type] || []).slice().forEach((fn) => fn(Object.assign({ type }, e))); }
}

// One page of the browser, with the script on it.
async function open(world, { player = false, playOn = true, sdk = 'good', chrome = true, secure = true, devices = true, joins = true, account = 'aa11' } = {}) {
  const { clock, tv, storage } = world;
  const mine = new Set();
  const own = (n) => { mine.add(n); return n; };
  const page = { registered: [], changes: 0, scripts: [], nodes: [], policies: [], fetched: [], posts: [], beacons: [], asked: [], listeners: {}, replies: {}, hidden: false, reloads: 0, sdk };

  class Session extends Emitter {
    constructor() { super(); this.status = 'connected'; }
    getSessionId() { return tv.sessionId; }
    getSessionObj() { return { status: this.status, media: tv.media }; }
    getMediaSession() { return tv.media.filter((m) => !m.idleReason)[0] || null; }
    getCastDevice() { return { friendlyName: tv.name, volume: { controlType: tv.fixed ? 'fixed' : 'attenuation' } }; }
    getVolume() { return tv.level; }
    isMute() { return tv.muted; }
    setVolume(v) { tv.calls.push('volume ' + v); tv.level = v; return Promise.resolve(); }
    setMute(m) { tv.calls.push('mute ' + m); tv.muted = m; return Promise.resolve(); }
    endSession(stop) { tv.ended.push(stop); context.over('stopped'); }
    loadMedia(request) {
      tv.loads.push({ url: request.media.contentId, type: request.media.contentType, at: request.currentTime, autoplay: request.autoplay, title: request.media.metadata.title, line: request.media.metadata.subtitle, keys: Object.keys(request).sort(), info: Object.keys(request.media).sort(), images: request.media.metadata.images || null });
      if (tv.answers === 'late') return new Promise((resolve) => { tv.answer = resolve; });
      if (tv.answers === 'never') return new Promise(() => {});
      if (!tv.answers) return Promise.reject('load_media_failed');
      const m = new Media(tv, request);
      if (tv.forgetful) m.media = Object.assign({}, m.media, { customData: undefined });
      tv.media.forEach((old) => old.say('IDLE', { idleReason: 'INTERRUPTED' }));
      tv.media = [m];
      this.emit('mediasession', { mediaSession: m });
      return Promise.resolve();
    }
  }

  class Context extends Emitter {
    constructor() { super(); this.session = null; this.state = devices ? 'NOT_CONNECTED' : 'NO_DEVICES_AVAILABLE'; }
    setOptions(o) { page.options = o; if (joins && world.session) this.resume(); }
    getCastState() { return this.state; }
    getCurrentSession() { return this.session; }
    requestSession() {
      page.asked.push('picker');
      if (page.pick) return Promise.reject(page.pick);
      this.begin('SESSION_STARTED');
      return Promise.resolve(null);
    }
    begin(how) { this.session = world.session = new Session(); this.set('CONNECTED'); this.tell(how); }
    resume() { this.begin('SESSION_RESUMED'); }
    tell(state) { this.emit('sessionstatechanged', { session: this.session, sessionState: state }); }
    set(state) { this.state = state; this.emit('caststatechanged', { castState: state }); }
    over(status) {
      if (!this.session) return;
      this.session.status = status;
      this.state = 'NOT_CONNECTED';
      const s = this.session;
      this.emit('sessionstatechanged', { session: s, sessionState: 'SESSION_ENDED' });
      this.session = null;
      if (status === 'stopped') { world.session = null; tv.media = []; }
    }
  }
  const context = page.context = new Context();

  page.framework = null;
  const same = (...names) => Object.fromEntries(names.map((n) => [n, n]));
  function arrive() {
    ctx.cast = { framework: {
      CastContext: { getInstance: () => context },
      CastContextEventType: { CAST_STATE_CHANGED: 'caststatechanged', SESSION_STATE_CHANGED: 'sessionstatechanged' },
      SessionEventType: { VOLUME_CHANGED: 'volumechanged', MEDIA_SESSION: 'mediasession' },
      SessionState: same('SESSION_STARTED', 'SESSION_RESUMED', 'SESSION_ENDED'),
      CastState: same('NO_DEVICES_AVAILABLE', 'NOT_CONNECTED', 'CONNECTED'),
    } };
    ctx.chrome.cast = {
      AutoJoinPolicy: { ORIGIN_SCOPED: 'origin_scoped' }, ErrorCode: { CANCEL: 'cancel' },
      SessionStatus: { DISCONNECTED: 'disconnected', STOPPED: 'stopped' },
      isAvailable: true,
      requestSessionById: (id) => page.asked.push('session ' + id),
      media: {
        DEFAULT_MEDIA_RECEIVER_APP_ID: 'CC1AD845',
        PlayerState: same('IDLE', 'PLAYING', 'PAUSED', 'BUFFERING'),
        IdleReason: same('CANCELLED', 'INTERRUPTED', 'FINISHED', 'ERROR'),
        StreamType: { BUFFERED: 'BUFFERED' },
        MediaInfo: class { constructor(id, type) { this.contentId = id; this.contentType = type; } },
        GenericMediaMetadata: class {},
        LoadRequest: class { constructor(media) { this.media = media; } },
        SeekRequest: class {},
      },
    };
    page.context = context;
    if (ctx.__onGCastApiAvailable) ctx.__onGCastApiAvailable(true);
  }

  page.arrive = arrive;

  const doc = {
    body: { getAttribute: (n) => (n === 'data-cast-account' ? account : null) },
    hidden: false,
    getElementById: (id) => (id === 'player' && player ? {} : null),
    createElement: () => ({ remove() { this.removed = true; } }),
    head: { appendChild: (s) => {
      page.scripts.push(s.src);
      page.policies.push(s.referrerPolicy);
      page.nodes.push(s);
      if (page.sdk === 'good') Promise.resolve().then(arrive);
      else if (page.sdk === 'blocked') Promise.resolve().then(() => s.onerror());
      else if (page.sdk === 'refuses') Promise.resolve().then(() => ctx.__onGCastApiAvailable(false));
    } },
    addEventListener: (type, fn) => { page.listeners[type] = fn; },
  };
  const ctx = {
    document: doc, AbortController, console, JSON, Array, Object, String, Number, Promise, Error, Math, Uint8Array, URL, Event, EventTarget, isSecureContext: secure,
    crypto: { getRandomValues: (a) => { world.pages += 1; a.fill(world.pages); return a; } },
    navigator: { presentation: {}, sendBeacon: (url, blob) => { page.beacons.push([url, blob.text]); return true; } },
    Blob: class { constructor(parts) { this.text = JSON.parse(parts[0]); } },
    location: { href: 'https://media.example/watch', reload: () => { page.reloads += 1; } },
    localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = v; world.writes.push([k, v.length, v]); }, removeItem: (k) => { delete storage[k]; } },
    Date: class extends Date { static now() { return clock.now(); } },
    setTimeout: (fn, ms) => own(clock.setTimeout(fn, ms)), setInterval: (fn, ms) => own(clock.setInterval(fn, ms)), clearTimeout: clock.clear, clearInterval: clock.clear,
    addEventListener: (type, fn) => { page.listeners[type] = fn; },
    DOMParser: class { parseFromString(html) { const at = JSON.parse(html); return { getElementById: () => at && { getAttribute: (n) => (n in at ? at[n] : null) } }; } },
    fetch: (url) => { page.fetched.push(url); const at = (page.pages || {})[url]; return Promise.resolve({ ok: !!at, redirected: false, text: () => Promise.resolve(JSON.stringify(at || null)) }); },
    MM: { api: { post: (url, body, opts) => {
      page.posts.push([url, body]);
      if (page.hangs === url) return new Promise((_, reject) => { opts.signal.addEventListener('abort', () => reject(new Error('aborted'))); });
      const reply = page.replies[url];
      if (typeof reply === 'function') return reply(body);
      return reply ? Promise.reject(reply) : Promise.resolve({});
    } } },
  };
  if (chrome) ctx.chrome = {};
  if (playOn) ctx.MM.playOn = { register: (way) => page.registered.push(way), changed: () => { page.changes += 1; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch/cast-sender.js'), 'utf8'), ctx);
  page.ctx = ctx;
  page.doc = doc;
  page.way = page.registered[0];
  page.sender = ctx.MM.castSender;
  // The page is left: it runs no more.
  page.close = () => {
    page.listeners.pagehide();
    mine.forEach(clock.clear);
    tv.media.forEach((m) => { m.listeners = []; });
    context.on = {};
    if (context.session) context.session.on = {};
  };
  await settle();
  return page;
}

const world = () => {
  const clock = makeClock();
  return { clock, tv: makeTv(clock), storage: {}, writes: [], pages: 0, session: null };
};
const TITLE = { rating_key: '101', title: 'Glass Orchard', subtitle: 'S1 · E4' };
const stream = (n, more) => Object.assign({ stream_id: 'st' + n, url: `/api/cast/ticket${n}/index.m3u8`, position_s: 100, duration_s: 3000, paused: false, title: TITLE }, more);
const what = (n, more) => ({ title: TITLE, position_s: 90, open: () => Promise.resolve(stream(n, more)) });

// A player's page that follows a play on the TV.
async function casting(w, more) {
  const page = await open(w, { player: true });
  page.way.look();
  await settle();
  const play = await page.way.start(what(1, more));
  page.events = [];
  page.gone = [];
  ['loadedmetadata', 'canplay', 'play', 'playing', 'pause', 'waiting', 'seeking', 'seeked', 'timeupdate', 'volumechange', 'ended', 'error']
    .forEach((type) => play.media.addEventListener(type, () => page.events.push(type)));
  play.onGone((why) => page.gone.push(why));
  w.tv.media[0].say(more && more.paused ? 'PAUSED' : 'PLAYING');
  await settle();
  page.play = play;
  page.take = () => page.events.splice(0);
  return page;
}
const places = (page) => page.posts.map(([url, body]) => [url.replace('/api/watch/', ''), body.position_s, body.played_s]);
const refused = (promise) => promise.then(() => 'taken', (err) => ({ code: err.code, says: !!err.say }));

const scenarios = {
  async quiet() {
    const w = world();
    const page = await open(w, { player: true });
    const before = { scripts: page.scripts.slice(), there: page.way.available() };
    page.way.look();
    page.way.look();
    await settle();
    return { before, scripts: page.scripts, there: page.way.available(), changes: page.changes > 0, options: page.options };
  },
  // A script that failed once, and then comes.
  async retries() {
    const out = {};
    for (const sdk of ['blocked', 'refuses']) {
      const w = world();
      const page = await open(w, { player: true, sdk });
      page.way.look();
      await w.clock.advance(20000);
      page.sdk = 'good';
      page.way.look();
      await settle();
      out[sdk] = { there: page.way.available(), asked: page.scripts.length, policies: page.policies };
    }
    return out;
  },
  async unable() {
    const out = {};
    for (const [name, o] of [['notChrome', { chrome: false }], ['plainHttp', { secure: false }], ['noSheet', { playOn: false }]]) {
      const page = await open(world(), Object.assign({ player: true }, o));
      out[name] = { ways: page.registered.length, scripts: page.scripts };
    }
    return out;
  },
  async fails() {
    const out = {};
    for (const sdk of ['blocked', 'refuses', 'silent']) {
      const w = world();
      const page = await open(w, { player: true, sdk });
      const before = { looking: page.way.looking(), trouble: page.way.trouble() };
      page.way.look();
      await settle();
      await w.clock.advance(20000);
      const told = page.changes;
      const trouble = page.way.trouble();
      const started = refused(page.way.start(what(1)));
      await w.clock.advance(20000);
      const start = await started;
      out[sdk] = { before, told: told > 0, there: page.way.available(), looking: page.way.looking(), asks: page.way.asks(), trouble: trouble && trouble.title, start, open: w.tv.loads.length };
      page.sdk = 'good';
      page.way.look();
      if (sdk === 'silent') page.arrive();
      await settle();
      out[sdk].after = { there: page.way.available(), trouble: page.way.trouble() };
    }
    const w = world();
    const none = await open(w, { player: true, devices: false });
    none.way.look();
    await settle();
    out.noTv = { there: none.way.available(), looking: none.way.looking() };
    await w.clock.advance(4000);
    out.searched = { there: none.way.available(), looking: none.way.looking() };
    none.context.set('NOT_CONNECTED');
    out.found = { there: none.way.available(), looking: none.way.looking() };
    return out;
  },
  // Google's script comes after it was given up, and where it is there already.
  async slowScript() {
    const w = world();
    const page = await open(w, { player: true, sdk: 'silent' });
    page.way.look();
    await w.clock.advance(20000);
    const gaveUp = page.way.trouble() !== null;
    page.way.look();
    const tags = page.scripts.length;
    page.arrive();
    await settle();
    page.way.look();
    const other = await open(world(), { player: true });
    other.arrive();
    other.way.look();
    await settle();
    return { gaveUp, tags, then: page.way.available(), after: page.scripts.length, trouble: page.way.trouble(), there: other.way.available(), asked: other.scripts };
  },
  async throws() {
    const w = world();
    const page = await casting(w);
    const boom = () => { throw new Error('boom'); };
    page.context.getCastState = boom;
    const way = { there: page.way.available(), looking: page.way.looking(), asks: page.way.asks() };
    page.play.leave();
    page.context.session.getCastDevice = boom;
    page.context.session.getVolume = boom;
    page.context.session.isMute = boom;
    page.context.getCurrentSession = boom;
    const now = page.sender.now();
    return { way, asks: page.way.asks(), name: page.play.name, now: now && [now.name, now.can_volume, now.volume, now.muted] };
  },
  // Another account's page, in the same browser.
  async account() {
    const w = world();
    const first = await casting(w);
    first.play.keep({ quality: 'low' });
    await w.clock.advance(10000);
    first.close();
    const stored = JSON.stringify(w.storage);
    const other = await open(w, { player: true, account: 'bb22' });
    const otherRoom = await open(w, { account: 'bb22' });
    await w.clock.advance(11000);
    const unknown = await open(w, { player: true, account: null });
    const seen = [other, unknown].map((p) => [p.way.again('101'), p.way.release('101'), p.sender.holds('101'), p.sender.now()]);
    const out = { seen, scripts: [other, otherRoom, unknown].map((p) => p.scripts.length), posts: otherRoom.posts, ended: w.tv.ended.slice(), same: JSON.stringify(w.storage) === stored };
    // Nor is what is kept beside it read for another.
    w.storage[RECORD] = JSON.stringify(Object.assign(JSON.parse(w.storage[RECORD]), { account: 'bb22' }));
    out.kept = (await other.way.again('101')).stream.keep;
    return out;
  },
  async unmarked() {
    const w = world();
    const page = await open(w, { player: true, account: null });
    page.way.look();
    await settle();
    const play = await page.way.start(what(1));
    play.keep({ quality: 'low' });
    await w.clock.advance(12000);
    page.listeners.pagehide();
    return { plays: w.tv.loads.length, stored: Object.keys(w.storage) };
  },
  // The sign-in page is opened in another tab.
  async signIn() {
    const out = {};
    for (const kind of ['player', 'keeper']) {
      const w = world();
      const first = await casting(w);
      first.play.keep({ quality: 'low' });
      let page = first;
      if (kind === 'keeper') { first.close(); page = await open(w); await w.clock.advance(1000); }
      const local = { removeItem: (k) => { delete w.storage[k]; } };
      vm.runInNewContext(fs.readFileSync(path.join(JS, 'signed-out.js'), 'utf8'), { window: { sessionStorage: local, localStorage: local } });
      const stored = Object.keys(w.storage);
      page.listeners.storage({ key: RECORD, newValue: null });
      await w.clock.advance(6000);
      out[kind] = { stored, gone: first.gone, ended: w.tv.ended, after: Object.keys(w.storage), now: page.sender.now() };
    }
    return out;
  },
  // A session that is joined and plays nothing of this page's.
  async joined() {
    const w = world();
    const page = await open(w, { player: true });
    page.way.look();
    await settle();
    page.context.begin('SESSION_RESUMED');
    const asks = [page.way.asks()];
    const play = await page.way.start(what(1));
    w.tv.media[0].say('PLAYING');
    asks.push(page.way.asks());
    const pickers = page.asked.length;
    await page.way.start(what(2));
    asks.push(page.way.asks());
    const second = page.asked.length;
    w.tv.media[0].say('IDLE', { idleReason: 'CANCELLED' });
    asks.push(page.way.asks());
    await page.way.start(what(3));
    return { asks, pickers, second, third: page.asked.length, name: play.name };
  },
  // A TV that is not found is not looked for by itself for ever.
  async misses() {
    const w = world();
    (await casting(w)).close();
    const loads = [];
    let last;
    for (let i = 0; i < 5; i++) {
      last = await open(w, { joins: false });
      await w.clock.advance(11000);
      last.listeners.visibilitychange();
      loads.push(last.scripts.length);
      last.close();
    }
    const kept = RECORD in w.storage;
    const player = await open(w, { player: true });
    const found = !!(await player.way.again('101'));
    player.close();
    const again = await open(w);
    return { loads, kept, found, again: again.scripts.length };
  },
  async starts() {
    const w = world();
    w.tv.forgetful = process.argv[3] === 'forgetful';
    const page = await casting(w);
    return { load: w.tv.loads[0], name: page.play.name, carriesOn: page.play.carriesOn, events: page.take(), stored: w.storage[RECORD], ready: page.play.media.readyState, posts: page.posts };
  },
  async closed() {
    const w = world();
    const page = await open(w, { player: true });
    page.way.look();
    await settle();
    let opened = 0;
    const w2 = { title: TITLE, open: () => { opened += 1; return Promise.reject(Object.assign(new Error('x'), { code: 'too_many_streams' })); } };
    page.pick = 'cancel';
    const cancelled = await refused(page.way.start(w2));
    page.pick = 'receiver_unavailable';
    const failed = await refused(page.way.start(w2));
    const before = opened;
    page.pick = null;
    const full = await refused(page.way.start(w2));
    return { cancelled, failed, before, full, opened, ended: w.tv.ended, loads: w.tv.loads.length, kept: RECORD in w.storage };
  },
  async refuses() {
    const out = {};
    for (const answers of [false, 'never']) {
      const w = world();
      const page = await open(w, { player: true });
      page.way.look();
      await settle();
      w.tv.answers = answers;
      const tried = refused(page.way.start(what(1)));
      await settle();
      await w.clock.advance(50000);
      out[String(answers)] = { tried: await tried, ended: w.tv.ended, kept: RECORD in w.storage, timers: w.clock.waiting() };
    }
    return out;
  },
  async controls() {
    const w = world();
    const page = await casting(w);
    const media = page.play.media;
    page.take();
    const out = {};
    media.pause();
    out.pausedAtOnce = { paused: media.paused, events: page.take() };
    await settle();
    out.pausedThen = { paused: media.paused, events: page.take() };
    const going = media.play();
    out.playedAtOnce = { paused: media.paused };
    await going;
    out.playedThen = page.take();
    media.currentTime = 500;
    out.seeking = { seeking: media.seeking, at: media.currentTime, events: page.take() };
    await settle();
    out.seeked = { seeking: media.seeking, at: media.currentTime, events: page.take() };
    media.currentTime = 9999;
    await settle();
    out.past = media.currentTime;
    media.volume = 0.5;
    media.volume = 0.6;
    out.volume = media.volume;
    await w.clock.advance(200);
    media.muted = true;
    out.muted = media.muted;
    out.calls = w.tv.calls;
    return out;
  },
  async stubborn() {
    const w = world();
    const page = await casting(w);
    const media = page.play.media;
    w.tv.refuses = true;
    page.take();
    media.pause();
    await settle();
    const pause = { paused: media.paused, events: page.take() };
    media.currentTime = 700;
    await settle();
    const seek = { seeking: media.seeking, at: media.currentTime };
    w.tv.refuses = false;
    w.tv.hangs = true;
    media.currentTime = 800;
    media.pause();
    await w.clock.advance(11000);
    return { pause, seek, hung: { seeking: media.seeking, paused: media.paused, far: media.currentTime < 200 } };
  },
  async remote() {
    const w = world();
    const page = await casting(w);
    page.take();
    w.tv.media[0].say('PAUSED');
    const paused = [page.play.media.paused, page.take()];
    w.tv.media[0].say('PLAYING');
    const on = [page.play.media.paused, page.take()];
    w.tv.media[0].say('BUFFERING');
    const waits = [page.play.media.readyState, page.take()];
    w.tv.media[0].say('PLAYING');
    const plays = [page.play.media.readyState, page.take()];
    w.tv.media[0].say('PLAYING');
    const twice = page.take();
    w.tv.level = 0.9;
    page.context.session.emit('volumechanged', {});
    const volume = [page.play.media.volume, page.take(), page.play.media.canVolume];
    w.tv.fixed = true;
    return { paused, on, waits, plays, twice, volume, fixed: page.play.media.canVolume };
  },
  async ticks() {
    const w = world();
    const page = await casting(w);
    page.take();
    await w.clock.advance(3000);
    const playing = { updates: page.take().filter((e) => e === 'timeupdate').length, at: page.play.media.currentTime };
    w.tv.media[0].say('PAUSED');
    page.take();
    await w.clock.advance(3000);
    return { playing, paused: page.take(), posts: page.posts };
  },
  // A quality change, then the next episode, on the same TV.
  async change() {
    const w = world();
    const page = await casting(w);
    await w.clock.advance(20000);
    page.take();
    const media = page.play.media;
    const loading = page.play.load(stream(2, { position_s: 118, paused: true }));
    const during = { at: media.currentTime, paused: media.paused, ready: media.readyState, events: page.take() };
    await loading;
    const taken = { at: media.currentTime, events: page.take() };
    w.tv.media[0].say('PAUSED');
    const next = { rating_key: '102', title: 'Glass Orchard', subtitle: 'S1 · E5' };
    await page.play.load(stream(3, { position_s: 0, title: next }));
    return { during, taken, gone: page.gone, loads: w.tv.loads.slice(1), key: JSON.parse(w.storage[RECORD]).key, sessions: page.asked, ended: w.tv.ended };
  },
  async hurried() {
    const w = world();
    const page = await casting(w);
    w.tv.answers = 'late';
    const first = refused(page.play.load(stream(2)));
    w.tv.answers = true;
    await page.play.load(stream(3));
    w.tv.answer();
    await w.clock.advance(50000);
    return { first: await first, stream: JSON.parse(w.storage[RECORD]).stream, gone: page.gone };
  },
  async ends() {
    const w = world();
    const page = await casting(w);
    page.take();
    w.tv.media[0].say('IDLE', { idleReason: 'FINISHED' });
    return { events: page.take(), at: page.play.media.currentTime, ended: page.play.media.ended, paused: page.play.media.paused, gone: page.gone };
  },
  async errs() {
    const w = world();
    const page = await casting(w);
    await w.clock.advance(5000);
    page.take();
    w.tv.media[0].say('IDLE', { idleReason: 'ERROR' });
    const out = { events: page.take(), error: page.play.media.error, at: page.play.media.currentTime, gone: page.gone.slice() };
    await page.play.load(stream(2, { position_s: 105 }));
    return Object.assign(out, { after: page.play.media.error });
  },
  async over() {
    const out = {};
    const ways = {
      stopped: (w) => w.tv.media[0].say('IDLE', { idleReason: 'CANCELLED' }),
      another: (w) => w.tv.media[0].say('IDLE', { idleReason: 'INTERRUPTED' }),
      told: (w, page) => page.context.session.emit('mediasession', { mediaSession: { media: { customData: { stream: 'someone' } } } }),
      session: (w, page) => page.context.over('stopped'),
      silent: (w) => w.tv.media[0].say('PLAYING', {}, false),
    };
    for (const name of Object.keys(ways)) {
      const w = world();
      const page = await casting(w);
      await w.clock.advance(7000);
      page.take();
      ways[name](w, page);
      await w.clock.advance(1500);
      page.context.over('stopped');
      const calls = w.tv.calls.length;
      page.play.media.pause();
      out[name] = { gone: page.gone, at: Math.round(page.play.media.currentTime), kept: RECORD in w.storage, posts: page.posts, late: w.tv.calls.length - calls, timers: w.clock.waiting() };
    }
    return out;
  },
  async stop() {
    const w = world();
    const page = await casting(w);
    await w.clock.advance(4000);
    page.take();
    await page.play.stop();
    await page.play.stop();
    return { ended: w.tv.ended, gone: page.gone, events: page.take(), at: page.play.media.currentTime, kept: RECORD in w.storage, posts: page.posts, timers: w.clock.waiting() };
  },
  async lost() {
    const w = world();
    const page = await casting(w);
    await w.clock.advance(5000);
    page.take();
    const told = page.changes;
    page.context.over('disconnected');
    const lost = [page.play.lost, page.changes > told];
    const gone = { events: page.take(), at: page.play.media.currentTime, gone: page.gone.slice() };
    w.tv.level = 0.1;
    page.play.media.volume = 0.9;
    page.play.media.currentTime = 900;
    page.listeners.online();
    await w.clock.advance(30000);
    const still = { at: page.play.media.currentTime, asked: page.asked.slice(1), calls: w.tv.calls.slice() };
    page.take();
    page.context.resume();
    return { lost, after: page.play.lost, gone, still, back: { events: page.take(), at: page.play.media.currentTime, gone: page.gone } };
  },
  async lostForGood() {
    const w = world();
    const page = await casting(w);
    page.context.over('disconnected');
    await w.clock.advance(119000);
    const before = page.gone.slice();
    await w.clock.advance(2000);
    return { before, after: page.gone, kept: RECORD in w.storage };
  },
  async stopLost() {
    const w = world();
    const page = await casting(w);
    await w.clock.advance(5000);
    page.context.over('disconnected');
    await page.play.stop();
    return { ended: w.tv.ended, kept: RECORD in w.storage, timers: w.clock.waiting() };
  },
  async backToAnother() {
    const w = world();
    const page = await casting(w);
    page.context.over('disconnected');
    w.tv.media = [new Media(w.tv, { media: { customData: { stream: 'someone' } }, currentTime: 0 })];
    page.context.resume();
    return page.gone;
  },
  async reload() {
    const w = world();
    const first = await casting(w);
    first.play.keep({ quality: 'low', played_s: 40 });
    await w.clock.advance(10000);
    first.close();
    const stored = w.storage[RECORD];
    const second = await open(w, { player: true });
    const before = second.scripts.length;
    const found = await second.way.again();
    const media = found.play.media;
    await w.clock.advance(1000);
    media.pause();
    await settle();
    return { stored, before, scripts: second.scripts.length, stream: found.stream, name: found.play.name, at: media.currentTime, calls: w.tv.calls, posts: second.posts, beacons: first.beacons };
  },
  async nothing() {
    const out = {};
    const page = await open(world(), { player: true });
    out.never = { found: page.way.again(), scripts: page.scripts };
    for (const [name, change] of [
      ['emptied', (w) => { w.tv.media[0].idleReason = 'CANCELLED'; }],
      ['another', (w) => { w.tv.media = [new Media(w.tv, { media: { customData: { stream: 'someone' } }, currentTime: 0 })]; }],
    ]) {
      const w = world();
      (await casting(w)).close();
      change(w);
      const p = await open(w, { player: true });
      const found = p.way.again();
      await w.clock.advance(11000);
      out[name] = { found: await found, kept: RECORD in w.storage, ended: w.tv.ended, posts: places(p) };
    }
    const w = world();
    (await casting(w)).close();
    await w.clock.advance(6 * 3600 * 1000 + 1);
    const late = await open(w, { player: true });
    out.old = { found: late.way.again(), scripts: late.scripts, holds: late.sender.holds('101') };
    return out;
  },
  // The TV is not found in time, though it plays on; a page opened later finds it.
  async slow() {
    const w = world();
    (await casting(w)).close();
    const player = await open(w, { player: true, joins: false });
    const asked = refused(player.way.again('101'));
    await w.clock.advance(11000);
    const first = { answer: await asked, kept: RECORD in w.storage, ended: w.tv.ended };
    const blocked = await open(w, { player: true, sdk: 'silent' });
    const never = refused(blocked.way.again('101'));
    await w.clock.advance(16000);
    const second = { answer: await never, kept: RECORD in w.storage };
    const keeper = await open(w, { joins: false });
    await w.clock.advance(11000);
    const before = keeper.posts.length;
    keeper.context.resume();
    await w.clock.advance(1000);
    const late = { before, saves: keeper.posts.length };
    keeper.close();
    const next = await open(w, { player: true });
    return { first, second, late, found: !!(await next.way.again('101')) };
  },
  // The TV is joined and plays nothing.
  async empty() {
    const w = world();
    const first = await casting(w);
    first.play.keep({ played_s: 50 });
    await w.clock.advance(12000);
    first.close();
    w.tv.media = [];
    const page = await open(w, { player: true, joins: false });
    const asked = page.way.again('101');
    // Joined only just in time.
    await w.clock.advance(9000);
    page.context.resume();
    await w.clock.advance(3500);
    return { found: await asked, kept: RECORD in w.storage, posts: places(page), ended: w.tv.ended };
  },
  // "Play here", whatever became of the TV.
  async released() {
    const w = world();
    const first = await casting(w);
    first.play.keep({ played_s: 50 });
    await w.clock.advance(12000);
    first.close();
    const page = await open(w, { player: true, joins: false });
    const asked = refused(page.way.again('101'));
    await w.clock.advance(11000);
    await asked;
    const none = page.way.release('999');
    const other = RECORD in w.storage;
    const place = page.way.release('101');
    const twice = page.way.release('101');
    const again = await open(w, { player: true });
    return { none, place, twice, other, kept: Object.keys(w.storage), found: again.way.again('101'), holds: again.sender.holds('101'), ended: w.tv.ended };
  },
  async releasedMeanwhile() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w, { player: true, joins: false });
    const asked = page.way.again('101');
    await w.clock.advance(5000);
    page.way.release('101');
    page.context.resume();
    await settle();
    return { found: await asked, ended: w.tv.ended, kept: Object.keys(w.storage), posts: page.posts };
  },
  // "Play here" in a second tab, while the first follows the TV.
  async releasedElsewhere() {
    const out = {};
    for (const kind of ['player', 'keeper']) {
      const w = world();
      const first = await casting(w);
      let page = first;
      if (kind === 'keeper') {
        first.close();
        page = await open(w);
      }
      await w.clock.advance(1000);
      page.posts.length = 0;
      const second = await open(w, { player: true });
      second.way.release('101');
      page.listeners.storage({ key: RECORD, newValue: w.writes[w.writes.length - 1][2] });
      await settle();
      out[kind] = { ended: w.tv.ended, gone: first.gone, posts: places(page), kept: Object.keys(w.storage) };
    }
    return out;
  },
  async elsewhere() {
    const w = world();
    await casting(w);
    const second = await open(w, { player: true });
    const same = await refused(Promise.resolve(second.way.again('101')));
    return { same, other: second.way.again('102'), scripts: second.scripts };
  },
  // A start called back and chosen again; `first` and `second` say how each ends.
  async chosenAgain() {
    const out = {};
    const run = async (name, go) => {
      const w = world();
      const page = await open(w, { player: true });
      page.way.look();
      await settle();
      const wait = () => { let d; const promise = new Promise((resolve, reject) => { d = { resolve, reject }; }); return Object.assign(d, { promise }); };
      const a = wait(); const b = wait();
      const one = refused(page.way.start({ title: TITLE, open: () => a.promise }));
      await settle();
      const two = refused(page.way.start({ title: TITLE, open: () => b.promise }));
      await settle();
      const no = Object.assign(new Error('x'), { code: 'superseded' });
      await go(a, b, no);
      await settle();
      out[name] = { one: await one, two: await two, ended: w.tv.ended, loads: w.tv.loads.map((l) => l.url.split('/')[5]) };
    };
    await run('secondPlays', async (a, b, no) => { b.resolve(stream(2)); await settle(); a.reject(no); });
    await run('firstFailsEarly', async (a, b, no) => { a.reject(no); await settle(); b.resolve(stream(2)); });
    await run('bothFail', async (a, b, no) => { a.reject(no); await settle(); b.reject(no); });
    await run('firstLate', async (a, b) => { b.resolve(stream(2)); await settle(); a.resolve(stream(1)); });
    // The second press is called back in the browser's list, and so counts for nothing.
    const c = world();
    const p = await open(c, { player: true });
    p.way.look();
    await settle();
    p.way.start(what(1)).then(() => { out.kept = c.tv.loads.length; }, (err) => { out.kept = err.code; });
    p.pick = 'cancel';
    p.context.session = null;
    const back = refused(p.way.start(what(2)));
    p.context.session = c.session;
    out.back = await back;
    await settle();
    // The first is taken after all while the second waits, and the player stops it.
    for (const ends of ['plays', 'fails', 'alone']) {
      const w = world();
      const page = await open(w, { player: true });
      page.way.look();
      await settle();
      const play = await page.way.start(what(1));
      w.tv.media[0].say('PLAYING');
      let b;
      const two = ends !== 'alone' && refused(page.way.start({ title: TITLE, open: () => new Promise((resolve, reject) => { b = { resolve, reject }; }) }));
      await settle();
      await play.stop();
      await settle();
      const stopped = { ended: w.tv.ended.slice(), calls: w.tv.calls.slice() };
      if (ends === 'plays') b.resolve(stream(2));
      else if (ends === 'fails') b.reject(Object.assign(new Error('x'), { code: 'unreachable' }));
      out['stale ' + ends] = { stopped, two: await two, ended: w.tv.ended, loads: w.tv.loads.length };
    }
    // A TV found after "Play here" is not ended under a start that waits.
    const f = world();
    (await casting(f)).close();
    const q = await open(f, { player: true, joins: false });
    q.way.again('101');
    await settle();
    q.way.release('101');
    q.context.session = f.session;
    const on = refused(q.way.start({ title: TITLE, open: () => new Promise(() => {}) }));
    await settle();
    q.context.resume();
    await settle();
    out.found = f.tv.ended;
    // The TV that is taken from here by someone else is not ended by a start that fails later.
    const w = world();
    const page = await casting(w);
    w.tv.media[0].say('IDLE', { idleReason: 'INTERRUPTED' });
    const late = await refused(page.way.start({ title: TITLE, open: () => Promise.reject(Object.assign(new Error('x'), { code: 'replaced' })) }));
    out.taken = { late, ended: w.tv.ended };
    return out;
  },
  // What is kept is large, and is written when it changes; the place on every beat.
  async writes() {
    const w = world();
    const page = await casting(w);
    const big = { quality: 'low', markers: 'x'.repeat(5000), played_s: 0 };
    page.play.keep(big);
    w.writes.length = 0;
    await w.clock.advance(30000);
    const beats = { writes: w.writes.length, largest: Math.max(...w.writes.map((x) => x[1])) };
    const place = JSON.parse(w.storage[RECORD]).place;
    w.writes.length = 0;
    page.play.keep({ quality: 'best', markers: 'y' });
    const changed = w.writes.map((x) => x[0]);
    page.close();
    const second = await open(w, { player: true });
    const kept = (await second.way.again('101')).stream.keep;
    const held = Object.keys(w.storage).length;
    second.close();
    w.storage[RECORD + '.kept'] = JSON.stringify({ stream: 'st9', keep: { quality: 'low' } });
    const third = await open(w, { player: true });
    const others = (await third.way.again('101')).stream.keep;
    w.storage[RECORD + '.kept'] = JSON.stringify({ stream: 'st1', keep: {} });
    await (await third.way.again('101')).play.stop();
    return { beats, place, changed, kept, held, others, after: Object.keys(w.storage) };
  },
  // The session is joined a moment before the TV says what it plays.
  async mediaLater() {
    const w = world();
    (await casting(w)).close();
    const media = w.tv.media;
    w.tv.media = [];
    const page = await open(w, { player: true });
    const asked = page.way.again('101');
    await w.clock.advance(1000);
    w.tv.media = media;
    page.context.session.emit('mediasession', { mediaSession: media[0] });
    await settle();
    return !!(await asked);
  },
  // Another page has put something else on a TV while this one looked.
  async newer() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w, { player: true });
    w.tv.media = [new Media(w.tv, { media: { customData: { stream: 'someone' } }, currentTime: 0 })];
    const asked = page.way.again('101');
    w.storage[RECORD] = JSON.stringify(Object.assign(JSON.parse(w.storage[RECORD]), { stream: 'st7' }));
    await w.clock.advance(1000);
    return { found: await asked, stream: JSON.parse(w.storage[RECORD]).stream };
  },
  async otherTitle() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w, { player: true });
    const found = page.way.again('999');
    await w.clock.advance(2000);
    return { found, saves: places(page) };
  },
  // Played time is each title's own.
  async played() {
    const w = world();
    const page = await casting(w);
    const stored = () => { const rec = JSON.parse(w.storage[RECORD]); const kept = w.storage[RECORD + '.kept']; return [rec.key, Math.round(rec.played), kept ? JSON.parse(kept).keep : null]; };
    page.play.keep({ quality: 'low', played_s: 900 });
    await w.clock.advance(10000);
    const first = stored();
    await page.play.load(stream(2, { position_s: 110 }));
    w.tv.media[0].say('PLAYING');
    await w.clock.advance(10000);
    const quality = stored();
    await page.play.load(stream(3, { position_s: 0, title: { rating_key: '102', title: 'Glass Orchard', subtitle: 'S1 · E5' } }));
    const started = stored();
    w.tv.media[0].say('PLAYING');
    await w.clock.advance(10000);
    const next = stored();
    page.play.keep({ quality: 'low', played_s: 4 });
    const set = stored();
    page.play.keep({ quality: 'best' });
    const unsaid = stored();
    await w.clock.advance(5000);
    page.close();
    const after = await open(w);
    await w.clock.advance(1000);
    return { first, quality, started, next, set, unsaid, saved: places(after)[0] };
  },
  async twoTabs() {
    const w = world();
    const first = await casting(w);
    const second = await open(w, { player: true });
    const third = await open(w);
    const out = { found: second.way.again('102'), scripts: [second.scripts, third.scripts], holds: second.sender.holds(101), other: second.sender.holds('102') };
    // A page that only keeps the place makes way.
    first.close();
    const keeper = await open(w);
    await w.clock.advance(1000);
    const saves = keeper.posts.length;
    const player = await open(w, { player: true });
    const found = await player.way.again();
    keeper.listeners.storage({ key: RECORD, newValue: w.storage[RECORD] });
    await w.clock.advance(60000);
    return Object.assign(out, { followed: !!found, saves, after: keeper.posts.length, posts: player.posts, record: !!w.storage[RECORD] });
  },
  // A page with no player, opened while the TV plays.
  async keeper() {
    const w = world();
    const player = await casting(w);
    player.play.keep({ played_s: 60 });
    player.close();
    const page = await open(w);
    const early = page.sender.now();
    await w.clock.advance(31000);
    const played = places(page);
    const now = page.sender.now();
    w.tv.media[0].say('PAUSED');
    await w.clock.advance(1000);
    const paused = places(page).slice(played.length);
    await w.clock.advance(130000);
    const kept = places(page).slice(played.length + paused.length);
    w.tv.media[0].say('PLAYING');
    await w.clock.advance(5000);
    page.listeners.pagehide();
    const beacon = page.beacons;
    w.tv.media[0].say('IDLE', { idleReason: 'FINISHED' });
    await settle();
    return { early, played, now, paused, kept, beacon, last: places(page).pop(), record: RECORD in w.storage, ended: w.tv.ended, nowhere: page.sender.now(), timers: w.clock.waiting() };
  },
  async keeperControls() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w);
    await w.clock.advance(1000);
    let told = 0;
    page.sender.on(() => { told += 1; });
    page.sender.toggle();
    await settle();
    const paused = page.sender.now().paused;
    page.sender.seek(400);
    await settle();
    await page.sender.stop();
    return { paused, calls: w.tv.calls, stop: page.posts.pop(), ended: w.tv.ended, told: told > 0, now: page.sender.now() };
  },
  async keeperGone() {
    const out = {};
    for (const status of [410, 502]) {
      const w = world();
      (await casting(w)).close();
      const page = await open(w);
      page.replies['/api/watch/progress'] = { status };
      await w.clock.advance(1000);
      out[status] = { ended: w.tv.ended, record: RECORD in w.storage, now: page.sender.now() };
    }
    return out;
  },
  // What a page without a player may do with it.
  async bar() {
    const w = world();
    const page = await casting(w);
    const heard = [];
    page.sender.on((over) => heard.push(over ? over.reason : (page.sender.now() || {}).state));
    const before = page.sender.now();
    page.play.keep({
      quality: 'low', subtitle: '44', played_s: 60, given: { subtitle: '44', subtitle_tracks: [{ id: '44', label: 'English' }] },
      markers: [{ kind: 'intro' }], chapters: [{ title: 'One' }], next: { rating_key: '102', label: 'S1 · E5 — Windfall' },
    });
    page.play.leave();
    const now = page.sender.now();
    const films = {};
    for (const line of ['2021', 'E7', 'S2 · E3 — A — b']) {
      const f = world();
      const q = await casting(f, { title: { rating_key: '5', title: 'T', subtitle: line } });
      q.play.leave();
      const n = q.sender.now();
      films[line] = [n.season, n.episode, n.episode_title];
    }
    w.tv.calls.length = 0;
    page.sender.play();
    page.sender.pause();
    await settle();
    page.sender.pause();
    await settle();
    const calls = w.tv.calls.splice(0);
    const state = page.sender.now().state;
    page.sender.toggle();
    page.sender.setVolume(0.7);
    page.sender.setMuted(true);
    await w.clock.advance(300);
    const said = [page.sender.now().volume, page.sender.now().muted];
    heard.length = 0;
    w.tv.media[0].say('BUFFERING');
    const buffering = [page.sender.now().state, heard.splice(0)];
    w.tv.media[0].say('PLAYING');
    heard.length = 0;
    page.context.over('disconnected');
    const lost = [page.sender.now().lost, heard.splice(0).length > 0];
    await w.clock.advance(121000);
    return { before, now, films, calls, state, after: w.tv.calls, said, buffering, lost, gone: heard.pop(), over: page.sender.over().reason, at: page.sender.over().position_s, followed: page.gone };
  },
  async stops() {
    const out = {};
    for (const how of ['kept', 'refused', 'never']) {
      const w = world();
      const page = await casting(w);
      page.play.leave();
      await w.clock.advance(3000);
      const url = '/api/watch/stream/st1/stop';
      if (how === 'refused') page.replies[url] = { status: 502 };
      if (how === 'never') page.hangs = url;
      let said = null;
      page.sender.stop().then((x) => { said = x; });
      await settle();
      const at = [said, page.sender.now()];
      await w.clock.advance(10000);
      out[how] = { at, said, ended: w.tv.ended, over: page.sender.over() };
    }
    return out;
  },
  // Another stream of it, with no player.
  async other() {
    const out = {};
    const run = async (name, setup, change, more) => {
      const w = world();
      const page = await casting(w, more);
      page.play.keep({ quality: 'best', subtitle: 'off', next: { rating_key: '102', label: 'S1 · E5' }, chapters: [1] });
      page.play.leave();
      await w.clock.advance(10000);
      page.replies['/api/cast/stream'] = (body) => { page.opened = (page.opened || []).concat([body]); return Promise.resolve({ stream_id: 'st2', url: '/api/cast/ticket2/index.m3u8', duration_s: 2000, subtitle: null, subtitle_tracks: [{ id: '9', label: 'Polski' }], markers: [], chapters: [2] }); };
      page.pages = { '/watch/play/102': { 'data-item': JSON.stringify({ rating_key: '102', title: 'Glass Orchard', subtitle: 'S1 · E5 — Windfall' }), 'data-next': JSON.stringify({ href: '/watch/play/103?from=x', label: 'S1 · E6' }), 'data-start-s': '30' } };
      if (setup) setup(page, w);
      page.posts.length = 0;
      const going = refused(page.sender.start(change));
      const during = page.sender.now() && [page.sender.now().state, page.sender.now().id, page.sender.now().title];
      await settle();
      if (w.tv.media[0]) w.tv.media[0].say(more && more.paused ? 'PAUSED' : 'PLAYING');
      const answer = await going;
      const now = page.sender.now();
      out[name] = {
        during, answer, loads: w.tv.loads.slice(1).map((l) => [l.url.split('/')[5], l.at, l.autoplay, l.line]), opened: page.opened || [],
        now: now && [now.id, now.state, now.rating_key, now.quality, now.subtitle_id, now.chapters, now.next, now.subtitle_tracks.length], posts: places(page).filter((x) => x[0] !== 'progress'), ended: w.tv.ended,
        kept: w.storage[RECORD + '.kept'] ? JSON.parse(w.storage[RECORD + '.kept']).stream : null,
      };
      return { w, page };
    };
    await run('quality', null, { quality: 'low' });
    await run('paused', null, { subtitle: '9' }, { paused: true });
    const n = await run('next', null, { next: true });
    n.page.posts.length = 0;
    await n.w.clock.advance(11000);
    out.next.saves = n.page.posts.map(([, b]) => [b.stream_id, b.position_s, b.played_s]);
    await run('noPage', (page) => { page.pages = {}; }, { next: true });
    await run('refused', (page) => { page.replies['/api/cast/stream'] = { error: 'rate_limited', status: 429 }; }, { quality: 'low' });
    // Refused, and the stream that played is gone with it: the old one is opened again.
    await run('gone', (page) => {
      const good = page.replies['/api/cast/stream'];
      page.replies['/api/cast/stream'] = (body) => (body.quality === 'low' ? Promise.reject({ error: 'unreachable', status: 502 }) : good(body));
      page.replies['/api/watch/progress'] = (body) => (body.stream_id === 'st1' ? Promise.reject({ status: 410 }) : Promise.resolve({}));
    }, { quality: 'low' });
    // The TV let go of the old stream while the new one was asked for, and that was refused.
    await run('fell', (page, w) => {
      const good = page.replies['/api/cast/stream'];
      page.replies['/api/cast/stream'] = (body) => {
        if (body.quality !== 'low') return good(body);
        w.tv.media[0].say('IDLE', { idleReason: 'ERROR' });
        return Promise.reject({ error: 'unreachable', status: 502 });
      };
    }, { quality: 'low' });
    await run('noTv', (page, w) => { w.tv.answers = false; }, { quality: 'low' });
    await run('lost', (page) => { page.context.over('disconnected'); }, { quality: 'low' });
    return out;
  },
  async otherTwice() {
    const w = world();
    const page = await casting(w);
    page.play.keep({ quality: 'best' });
    page.play.leave();
    const answers = [];
    page.replies['/api/cast/stream'] = (body) => new Promise((resolve) => answers.push(() => resolve({ stream_id: 'q-' + body.quality, url: '/api/cast/' + body.quality + '/index.m3u8', duration_s: 10 })));
    const one = refused(page.sender.start({ quality: 'low' }));
    await settle();
    const two = refused(page.sender.start({ quality: 'medium' }));
    await settle();
    answers[1]();
    await settle();
    page.posts.length = 0;
    answers[0]();
    await settle();
    // The first stream's place is not saved while another is on its way.
    return { one: await one, two: await two, loads: w.tv.loads.slice(1).map((l) => l.url.split('/')[5]), stopped: page.posts.map((x) => x[0]), id: page.sender.now().id };
  },
  async savesMeanwhile() {
    const w = world();
    const page = await casting(w);
    page.play.keep({ quality: 'best' });
    page.play.leave();
    await w.clock.advance(1000);
    page.posts.length = 0;
    page.replies['/api/cast/stream'] = () => new Promise(() => {});
    page.sender.start({ quality: 'low' });
    w.tv.media[0].say('IDLE', { idleReason: 'ERROR' });
    await w.clock.advance(30000);
    return { posts: page.posts.map((x) => x[0]), state: page.sender.now().state, ended: w.tv.ended };
  },
  // The end of an episode with no player open.
  async upnext() {
    const out = {};
    const episode = (n, next) => ({ 'data-item': JSON.stringify({ rating_key: String(n), title: 'Glass Orchard', subtitle: 'S1 · E' + n }), 'data-next': JSON.stringify(next ? { href: '/watch/play/' + next, label: 'S1 · E' + next } : null), 'data-start-s': '0' });
    const make = async (autoplay, next = true) => {
      const w = world();
      if (autoplay === false) w.storage['mediaman.watch.autoplay'] = 'false';
      const page = await casting(w);
      page.play.keep({ quality: 'best', next: next ? { rating_key: '102', label: 'S1 · E102' } : null });
      page.play.leave();
      page.pages = {};
      for (let n = 102; n < 110; n++) page.pages['/watch/play/' + n] = episode(n, n + 1);
      let i = 1;
      page.replies['/api/cast/stream'] = () => { i += 1; return Promise.resolve({ stream_id: 'st' + i, url: '/api/cast/ticket' + i + '/index.m3u8', duration_s: 2000 }); };
      await w.clock.advance(1000);
      page.posts.length = 0;
      return { w, page, end: async () => { w.tv.media[0].say('IDLE', { idleReason: 'FINISHED' }); await settle(); } };
    };
    const look = (page) => { const n = page.sender.now(); return n && [n.state, n.upnext_in_s, n.still_in_s, n.rating_key]; };

    let t = await make();
    await t.end();
    const counted = look(t.page);
    const stopped = places(t.page);
    await t.w.clock.advance(6000);
    const later = look(t.page);
    const record = JSON.parse(t.w.storage[RECORD]).stream;
    await t.w.clock.advance(5000);
    t.w.tv.media[0].say('PLAYING');
    out.counted = { counted, stopped, later, record, then: look(t.page), ended: t.w.tv.ended, streams: places(t.page).length };

    t = await make();
    await t.end();
    t.page.sender.cancel();
    const cancelled = look(t.page);
    await t.w.clock.advance(60000);
    out.cancelled = { cancelled, still: look(t.page), loads: t.w.tv.loads.length };
    t.page.sender.start({ next: true });
    await settle();
    out.cancelled.pressed = t.w.tv.loads.length;

    t = await make(false);
    await t.end();
    await t.w.clock.advance(30000);
    out.off = look(t.page);
    out.stop = { said: await t.page.sender.stop(), posts: places(t.page), ended: t.w.tv.ended };

    t = await make(true, false);
    await t.end();
    out.film = { now: look(t.page), over: t.page.sender.over().reason, posts: places(t.page), ended: t.w.tv.ended };

    t = await make();
    const shown = [];
    for (let i = 0; i < 4; i++) {
      await t.end();
      shown.push(look(t.page)[0]);
      await t.w.clock.advance(11000);
      if (i < 3) t.w.tv.media[0].say('PLAYING');
    }
    const asked = look(t.page);
    await t.w.clock.advance(600000);
    out.asked = { shown, asked: asked.slice(0, 2), in: asked[2], loads: t.w.tv.loads.length, over: t.page.sender.over().reason, ended: t.w.tv.ended, record: RECORD in t.w.storage };

    t = await make();
    for (let i = 0; i < 4; i++) {
      await t.end();
      if (i === 3) break;
      await t.w.clock.advance(11000);
      t.w.tv.media[0].say('PLAYING');
      if (i === 1) t.page.sender.setVolume(0.2);
    }
    out.signs = look(t.page)[0];

    t = await make();
    await t.w.clock.advance(2 * 3600 * 1000);
    await t.end();
    out.long = look(t.page)[0];

    t = await make();
    t.page.pages = {};
    await t.end();
    await t.w.clock.advance(12000);
    out.failed = { now: look(t.page), ended: t.w.tv.ended };
    return out;
  },
  // The player's page of another title leaves the play to the script.
  async left() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w, { player: true });
    const found = await page.way.again();
    const before = page.posts.length;
    found.play.leave();
    await w.clock.advance(12000);
    return { key: found.stream.rating_key, before, saves: places(page), ended: w.tv.ended };
  },
  // Another title takes the TV from one the script kept.
  async replaces() {
    const w = world();
    (await casting(w)).close();
    const page = await open(w, { player: true });
    (await page.way.again()).play.leave();
    await w.clock.advance(1000);
    const next = { rating_key: '7', title: 'Locks', subtitle: '2024' };
    const play = await page.way.start({ title: next, open: () => Promise.resolve(stream(9, { title: next })) });
    await settle();
    return { pickers: page.asked, stop: page.posts.pop(), ended: w.tv.ended, title: w.tv.loads.pop().title, name: play.name, key: JSON.parse(w.storage[RECORD]).key };
  },
  async finishedAway() {
    const w = world();
    (await casting(w)).close();
    w.tv.media[0].playerState = 'IDLE';
    w.tv.media[0].idleReason = 'FINISHED';
    const page = await open(w);
    await w.clock.advance(1000);
    return { posts: places(page), record: RECORD in w.storage };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (err) => { console.error(err); process.exit(1); });
