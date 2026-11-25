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
async function open(world, { player = false, playOn = true, sdk = 'good', chrome = true, secure = true, devices = true, joins = true } = {}) {
  const { clock, tv, storage } = world;
  const mine = new Set();
  const own = (n) => { mine.add(n); return n; };
  const page = { registered: [], changes: 0, scripts: [], nodes: [], posts: [], beacons: [], asked: [], listeners: {}, replies: {}, hidden: false, reloads: 0, sdk };

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
    ctx.__onGCastApiAvailable(true);
  }

  page.arrive = arrive;

  const doc = {
    hidden: false,
    getElementById: (id) => (id === 'player' && player ? {} : null),
    createElement: () => ({}),
    head: { appendChild: (s) => {
      page.scripts.push(s.src);
      page.nodes.push(s);
      if (page.sdk === 'good') Promise.resolve().then(arrive);
      else if (page.sdk === 'blocked') Promise.resolve().then(() => s.onerror());
      else if (page.sdk === 'refuses') Promise.resolve().then(() => ctx.__onGCastApiAvailable(false));
    } },
    addEventListener: (type, fn) => { page.listeners[type] = fn; },
  };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Promise, Error, Math, Uint8Array, URL, Event, EventTarget, isSecureContext: secure,
    crypto: { getRandomValues: (a) => { world.pages += 1; a.fill(world.pages); return a; } },
    navigator: { presentation: {}, sendBeacon: (url, blob) => { page.beacons.push([url, blob.text]); return true; } },
    Blob: class { constructor(parts) { this.text = JSON.parse(parts[0]); } },
    location: { href: 'https://media.example/watch', reload: () => { page.reloads += 1; } },
    localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = v; }, removeItem: (k) => { delete storage[k]; } },
    Date: class extends Date { static now() { return clock.now(); } },
    setTimeout: (fn, ms) => own(clock.setTimeout(fn, ms)), setInterval: (fn, ms) => own(clock.setInterval(fn, ms)), clearTimeout: clock.clear, clearInterval: clock.clear,
    addEventListener: (type, fn) => { page.listeners[type] = fn; },
    MM: { api: { post: (url, body) => {
      page.posts.push([url, body]);
      const reply = page.replies[url];
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
  return { clock, tv: makeTv(clock), storage: {}, pages: 0, session: null };
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
    for (const sdk of ['blocked', 'refuses', 'silent']) {
      const w = world();
      const page = await open(w, { player: true, sdk });
      page.way.look();
      await w.clock.advance(20000);
      page.sdk = 'good';
      page.way.look();
      await settle();
      out[sdk] = { there: page.way.available(), asked: page.scripts.length };
    }
    // The first fails at last while the second is on its way.
    const w = world();
    const page = await open(w, { player: true, sdk: 'silent' });
    page.way.look();
    await w.clock.advance(20000);
    page.way.look();
    page.nodes[0].onerror();
    page.arrive();
    await settle();
    out.late = { there: page.way.available(), asked: page.scripts.length };
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
      page.way.look();
      await settle();
      await w.clock.advance(20000);
      out[sdk] = { there: page.way.available(), trouble: !!page.way.trouble, start: await refused(page.way.start(what(1))), open: w.tv.loads.length };
    }
    const none = await open(world(), { player: true, devices: false });
    none.way.look();
    await settle();
    out.noTv = { there: none.way.available() };
    none.context.set('NOT_CONNECTED');
    out.found = { there: none.way.available() };
    return out;
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
    page.context.over('disconnected');
    const gone = { events: page.take(), at: page.play.media.currentTime, gone: page.gone.slice() };
    w.tv.level = 0.1;
    page.play.media.volume = 0.9;
    page.play.media.currentTime = 900;
    page.listeners.online();
    await w.clock.advance(30000);
    const still = { at: page.play.media.currentTime, asked: page.asked.slice(1), calls: w.tv.calls.slice() };
    page.take();
    page.context.resume();
    return { gone, still, back: { events: page.take(), at: page.play.media.currentTime, gone: page.gone } };
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
      out[name] = { found: await found, kept: RECORD in w.storage, ended: w.tv.ended };
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
    const stored = () => { const rec = JSON.parse(w.storage[RECORD]); return [rec.key, Math.round(rec.played), rec.keep]; };
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
    const out = { found: second.way.again(), scripts: [second.scripts, third.scripts], holds: second.sender.holds(101), other: second.sender.holds('102') };
    // A page that only keeps the place makes way.
    first.close();
    const keeper = await open(w);
    await w.clock.advance(1000);
    const saves = keeper.posts.length;
    const player = await open(w, { player: true });
    const found = await player.way.again();
    keeper.listeners.storage({ key: RECORD });
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
