// Runs the real tv-remote.js on its own template against the fake DOM, a TV that lives in this
// file and a clock that is stepped by hand, and prints one scenario's outcome as JSON.
// Usage: node tv_remote_harness.js <scenario>   (the rendered template on stdin)
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, click, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const TEMPLATE = fs.readFileSync(0, 'utf8');
const VOID = ['img', 'input', 'use', 'path', 'rect'];

function parse(doc, html, into) {
  const stack = [into];
  const re = /<!--[\s\S]*?-->|<\/([a-z0-9]+)>|<([a-z0-9]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/gi;
  let hit;
  while ((hit = re.exec(html))) {
    const top = stack[stack.length - 1];
    if (hit[1]) stack.pop();
    else if (hit[2]) {
      const el = doc.createElement(hit[2]);
      for (const [, k, v] of hit[3].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) el.setAttribute(k, v || '');
      // Of an HTML element alone the property is the attribute; on an SVG one it does nothing.
      if (!['svg', 'use'].includes(hit[2])) {
        Object.defineProperty(el, 'hidden', { get: () => el.hasAttribute('hidden'), set: (on) => el.toggleAttribute('hidden', on) });
      }
      el.style.setProperty = () => {};
      top.appendChild(el);
      if (!hit[4] && !VOID.includes(hit[2])) stack.push(el);
    } else if (hit[5] && hit[5].trim()) top.appendChild(doc.createTextNode(hit[5]));
  }
}

function aPlay(more) {
  return {
    play_id: 'p1', state: 'playing', title: 'Glass Orchard', year: 2022, season: 1, episode: 4,
    episode_title: 'The Grafting Knife', duration_s: 2880, markers: [], chapters: [], next: null,
    upnext_in_s: null, still_in_s: null, still_since_s: null, error: null, rating_key: '9001',
    position_s: 1200, quality: 'auto', subtitle_tracks: [{ id: '11', label: 'English' }], subtitle: null, ...more,
  };
}
function aTv(more) {
  return { id: 7, name: 'Living room', on: true, lost_s: null, acked: 0, volume: 0.4, muted: false, can_volume: true, play: aPlay(), last: null, ...more };
}

function aCast(more) {
  return {
    id: 's1', name: 'Den', rating_key: '9001', title: 'Glass Orchard', subtitle: 'S1 · E4 — The Grafting Knife',
    season: 1, episode: 4, episode_title: 'The Grafting Knife', position_s: 1200, duration_s: 2880, paused: false, lost: false,
    state: 'playing', volume: 0.4, muted: false, can_volume: true, markers: [], chapters: [], quality: 'auto', subtitle_id: 'off',
    subtitle_tracks: [], next: null, upnext_in_s: null, still_in_s: null, ...more,
  };
}

async function page(opts = {}) {
  const doc = makeDocument();
  if (opts.player) parse(doc, '<div id="player"></div>', doc.body);
  parse(doc, TEMPLATE, doc.body);
  doc.hidden = false;
  let clock = 1e12;
  let timers = [];
  let ids = 0;
  const api = { tvs: [aTv()], calls: [], fail: null, commandId: 0, hold: null, answer: null, ...opts };
  class APIError extends Error {
    constructor(error, status, data) { super(error); this.error = error; this.status = status; this.data = data || {}; }
  }
  const copy = (x) => JSON.parse(JSON.stringify(x));
  async function call(method, url, body, o) {
    api.calls.push({ method, url, body: body && copy(body), at: clock });
    if (api.hang) {
      await new Promise((_r, no) => {
        if (o && o.signal) o.signal.addEventListener('abort', () => no(Object.assign(new Error('aborted'), { name: 'AbortError' })));
      });
    }
    // What api.js makes of an answer whose body is cut off.
    if (api.empty) return {};
    if (api.fail) throw new APIError(...api.fail);
    if (method === 'POST' && api.hold) await new Promise((r) => { api.hold.push(r); });
    if (url === '/api/tvs') return { tvs: copy(api.tvs) };
    const tv = api.tvs.find((t) => url.startsWith('/api/tvs/' + t.id) && !(api.goneIds || []).includes(t.id));
    if (!tv) throw new APIError(api.gone ? api.gone.reason : 'not_found', api.gone ? 410 : 404, { last: api.gone && api.gone.last });
    if (method === 'GET') return { tv: copy(tv) };
    if (api.refuse) throw new APIError(api.refuse, 409);
    const id = ++api.commandId;
    if (body.kind === 'stop') {
      tv.last = { ...tv.play, reason: 'stopped' };
      tv.play = null;
    }
    return { id, tv: copy(tv) };
  }
  const toasts = [];
  // A step is made later, as a browser makes it.
  const history = {
    entries: [opts.state ? { ...opts.state } : { mm: { id: 'a' } }], index: 0,
    get state() { return history.entries[history.index]; },
    pushState(s) { history.entries.splice(history.index + 1); history.entries.push(copy(s)); history.index += 1; },
    replaceState(s) { history.entries[history.index] = copy(s); },
    go(by) {
      timers.push({ id: ++ids, at: clock + 5, fn() {
        if (by < 0 && history.index + by < 0 || history.index + by >= history.entries.length) return;
        history.index += by;
        let heard = true;
        win.listeners.popstate({ stopImmediatePropagation() { heard = false; } });
        if (heard) heardByPage.push(history.index);
      } });
    },
  };
  if (opts.reloadedAt) { history.entries.push(opts.reloadedAt); history.index = 1; }
  const heardByPage = [];
  const session = {};
  const win = {
    listeners: {}, history,
    location: { href: '', replace(to) { history.entries[history.index] = null; win.location.href = to; } }, innerWidth: 1000, innerHeight: 800, navigator: { onLine: true },
    sessionStorage: { getItem: (k) => session[k] || null, setItem: (k, v) => { session[k] = v; } },
    matchMedia: (q) => ({ matches: !!opts.still && /reduced-motion/.test(q) }),
    addEventListener(t, fn) { win.listeners[t] = fn; },
    UIFeedback: { toast: (t) => toasts.push(t), error: (t) => toasts.push('! ' + t) },
    ModalA11y: { register() {}, onOpened() {}, onClosed() {} },
    MM: { api: { get: (u, o) => call('GET', u, null, o), post: (u, b) => call('POST', u, b), messageOf: (e, f) => f } },
  };
  // What cast-sender.js tells and does; the tab that follows the play is another with `away`.
  const cast = { now: null, over: null, did: [], away: false, saved: true, hold: null, refuse: null, change() {}, ...opts.cast };
  // It goes by itself.
  cast.end = (reason) => { cast.over = { ...cast.now, reason }; cast.now = null; cast.change(cast.over); };
  if (opts.cast) {
    const note = (...what) => { cast.did.push(what.length > 1 ? what : what[0]); };
    const paused = (on) => { if (cast.now.paused === on) return; note(on ? 'pause' : 'play'); Object.assign(cast.now, { paused: on, state: on ? 'paused' : 'playing' }); cast.change(null); };
    win.MM.castSender = {
      now: () => cast.now && { ...cast.now },
      over: () => cast.over,
      on(fn) { cast.change = fn; },
      play: () => paused(false),
      pause: () => paused(true),
      seek(s) { note('seek', Math.round(s)); cast.now.position_s = s; },
      setVolume(v) { note('volume', v); cast.now.volume = v; },
      setMuted(m) { note('mute', m); cast.now.muted = m; },
      cancel() { note('cancel'); cast.now.upnext_in_s = null; cast.change(null); },
      async start(what) {
        note('start', what);
        if (cast.hold) await cast.hold;
        if (cast.refuse) throw Object.assign(new Error(cast.refuse), { code: cast.refuse });
        Object.assign(cast.now, what.quality ? { quality: what.quality } : what.subtitle ? { subtitle_id: what.subtitle } : { id: 's2', episode: 5, state: 'playing', position_s: 0 });
      },
      async stop() {
        note('stop');
        const at = cast.now.position_s;
        cast.now = null;
        cast.change(null);
        if (cast.hold) await cast.hold;
        return { position_s: at, saved: cast.saved };
      },
      holds: () => cast.away,
    };
  }
  const $ = (sel) => doc.querySelector(sel);
  const ctx = vm.createContext({
    window: win, document: doc, AbortController,
    Date: class extends Date { constructor(...a) { super(...(a.length ? a : [clock])); } static now() { return clock; } },
    setTimeout: (fn, ms) => { timers.push({ id: ++ids, at: clock + ms, fn }); return ids; },
    clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: (fn, ms) => { timers.push({ id: ++ids, at: clock + ms, fn, every: ms }); return ids; },
  });
  vm.runInContext(fs.readFileSync(path.join(JS, 'core/dom.js'), 'utf8'), ctx);
  const seek = $('[data-seek]');
  seek.getBoundingClientRect = () => ({ left: 0, right: 1000, width: 1000, top: 0, bottom: 44 });
  seek.setPointerCapture = () => {};
  vm.runInContext(fs.readFileSync(path.join(JS, 'tv-remote.js'), 'utf8'), ctx);
  await settle();
  const p = {
    $, doc, win, api, toasts, history, heardByPage, cast,
    async step(by) { history.go(by); await p.pass(20); },
    trail: () => ({ at: history.index, entries: history.entries.length, open: p.isOpen(), menu: p.seen('[data-po]') }),
    seen: (sel) => !p.$(sel).hidden && !p.$(sel).classList.contains('is-leaving'),
    async pass(ms) {
      const end = clock + ms;
      for (;;) {
        const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        clock = Math.max(clock, due.at);
        if (due.every) due.at += due.every; else timers = timers.filter((t) => t !== due);
        due.fn();
        await settle();
      }
      clock = end;
    },
    async press(sel) { click(doc, $(sel)); await settle(); },
    async pressText(text) { click(doc, doc.querySelectorAll('button').find((b) => b.textContent === text)); await settle(); },
    async on(sel, type, e) {
      $(sel).listeners[type].forEach((fn) => fn({ button: 0, pointerId: 1, pointerType: 'touch', preventDefault() {}, stopPropagation() {}, target: $(sel), ...e }));
      await settle();
    },
    key(sel, key) {
      const e = { key, target: $(sel), preventDefault() {}, stopPropagation() {} };
      $('#tv-remote').listeners.keydown.forEach((fn) => fn(e));
    },
    sent: () => api.calls.filter((c) => c.method === 'POST').map((c) => c.body),
    reads: () => api.calls.filter((c) => c.method === 'GET').map((c) => c.url),
    clock: (ms) => { const d = new Date(clock + ms); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2); },
    at: () => $('[data-elapsed]').textContent,
    bar: () => ({ shown: !$('#tv-mini').hidden && !$('#tv-mini').classList.contains('is-leaving'), says: $('[data-mini-say]').textContent }),
    card: () => ($('[data-card]').hidden || $('[data-card]').classList.contains('is-leaving') ? null : $('[data-card-title]').textContent),
    isOpen: () => !$('#tv-remote').hidden && !$('#tv-remote').classList.contains('is-leaving'),
    open: () => p.press('[data-open]'),
  };
  return p;
}

const scenarios = {
  async idle() {
    const p = await page({ tvs: [aTv({ play: null })] });
    await p.pass(120000);
    return { bar: p.bar().shown, reads: p.reads().length };
  },
  async player() {
    const p = await page({ player: true });
    await p.pass(60000);
    const out = { reads: p.reads().length };
    p.win.MM.tvRemote.open(p.api.tvs[0]);
    await settle();
    out.open = p.isOpen();
    out.bar = p.bar().shown;
    return out;
  },
  async polling() {
    const p = await page();
    const count = async (ms) => { const n = p.reads().length; await p.pass(ms); return p.reads().length - n; };
    const out = { bar: p.bar(), barMinute: await count(60000) };
    await p.open();
    out.openMinute = await count(60000);
    p.doc.hidden = true;
    p.doc.listeners.visibilitychange.forEach((fn) => fn());
    out.hiddenMinute = await count(60000);
    return out;
  },
  async drag() {
    const p = await page();
    await p.open();
    await p.on('[data-seek]', 'pointerdown', { clientX: 500 });
    await p.on('[data-seek]', 'pointermove', { clientX: 750 });
    await p.pass(4000);
    const out = { held: p.at(), sentWhileHeld: p.sent().length };
    await p.on('[data-seek]', 'pointerup', {});
    out.sent = p.sent();
    await p.pass(3000);
    out.beforeTheTvDid = p.at();
    Object.assign(p.api.tvs[0], { acked: 1 });
    p.api.tvs[0].play.position_s = 2161;
    await p.pass(1100);
    out.after = p.at();
    // Someone else seeks: this remote follows.
    p.api.tvs[0].play.position_s = 60;
    await p.pass(1100);
    out.other = p.at();
    return out;
  },
  async dragOver() {
    const p = await page();
    await p.open();
    await p.on('[data-seek]', 'pointerdown', { clientX: 900 });
    p.api.tvs[0].play = aPlay({ play_id: 'p2', episode: 5, position_s: 0 });
    await p.pass(1100);
    await p.on('[data-seek]', 'pointerup', {});
    return { sent: p.sent(), at: p.at() };
  },
  async cancelled() {
    const p = await page();
    await p.open();
    await p.on('[data-seek]', 'pointerdown', { clientX: 900 });
    await p.on('[data-seek]', 'pointercancel', {});
    return { sent: p.sent(), at: p.at() };
  },
  async arrows() {
    const p = await page();
    await p.open();
    for (let i = 0; i < 4; i++) p.key('[data-seek]', 'ArrowRight');
    const at = p.at();
    await p.pass(400);
    return { at, sent: p.sent() };
  },
  async volume() {
    const p = await page();
    await p.open();
    p.api.hold = [];
    for (const v of ['41', '50', '60', '70']) { p.$('[data-volume]').value = v; await p.on('[data-volume]', 'input', {}); }
    p.api.hold.shift()();
    await settle();
    await p.pass(300);
    p.api.hold.shift()();
    return { levels: p.sent().map((b) => b.level) };
  },
  async pause() {
    const p = await page();
    await p.press('[data-mini-play]');
    const out = { says: p.bar().says, sent: p.sent() };
    await p.pass(2000);
    Object.assign(p.api.tvs[0], { acked: 1 });
    p.api.tvs[0].play.state = 'paused';
    await p.pass(5000);
    // Another phone presses play.
    p.api.tvs[0].play.state = 'playing';
    await p.pass(5000);
    out.other = p.bar().says;
    return out;
  },
  async refused() {
    const p = await page();
    p.api.refuse = 'tv_off';
    await p.press('[data-mini-play]');
    return { says: p.bar().says, toast: p.toasts.length };
  },
  async unheard() {
    const p = await page();
    await p.press('[data-mini-play]');
    await p.pass(21000);
    return p.bar().says;
  },
  async lost() {
    const p = await page();
    await p.open();
    Object.assign(p.api.tvs[0], { on: false, lost_s: 11 });
    await p.pass(1100);
    const out = { card: p.card(), focus: p.doc.activeElement.textContent, dead: p.$('[data-bottom]').inert, closeLives: !p.$('.player-top').inert };
    Object.assign(p.api.tvs[0], { on: true, lost_s: null });
    await p.pass(1100);
    out.back = { card: p.card(), dead: p.$('[data-bottom]').inert };
    return out;
  },
  async lostForGood() {
    const p = await page();
    Object.assign(p.api.tvs[0], { on: false, play: null, last: { ...aPlay({ position_s: 1268 }), reason: 'lost' } });
    await p.pass(5100);
    const out = { bar: p.bar() };
    await p.press('[data-dismiss]');
    await p.pass(1000);
    out.dismissed = p.bar().shown;
    return out;
  },
  async dismissed() {
    const p = await page();
    Object.assign(p.api.tvs[0], { on: false, lost_s: 11 });
    await p.pass(5100);
    await p.press('[data-dismiss]');
    await p.pass(6000);
    const out = { shown: p.bar().shown };
    Object.assign(p.api.tvs[0], { on: true, lost_s: null });
    await p.pass(5100);
    out.back = p.bar().shown;
    return out;
  },
  async taken() {
    const p = await page();
    await p.open();
    p.api.tvs = [];
    p.api.gone = { reason: 'taken', last: aPlay({ position_s: 1268 }) };
    await p.pass(1100);
    const out = { card: p.card(), hint: p.$('[data-hint]').textContent };
    const n = p.reads().length;
    await p.pass(60000);
    out.asksOn = p.reads().length - n;
    await p.pressText('Bring it here');
    out.href = p.win.location.href;
    return out;
  },
  async stoppedElsewhere() {
    const p = await page();
    await p.open();
    Object.assign(p.api.tvs[0], { play: null, last: { ...aPlay({ position_s: 1268 }), reason: 'stopped' } });
    await p.pass(1100);
    return { open: p.isOpen(), bar: p.bar().shown, toasts: p.toasts };
  },
  async stop() {
    const p = await page();
    await p.press('[data-mini-stop]');
    await p.pass(1000);
    return { bar: p.bar().shown, toasts: p.toasts, sent: p.sent() };
  },
  async bring() {
    const p = await page();
    await p.open();
    await p.press('[data-bring]');
    return { sent: p.sent(), href: p.win.location.href, toasts: p.toasts };
  },
  async offline() {
    const p = await page();
    await p.open();
    p.api.fail = ['network_error', 0];
    p.win.navigator.onLine = false;
    await p.pass(1100);
    const out = { once: p.card() };
    await p.pass(3100);
    out.twice = p.card();
    p.api.fail = null;
    await p.pass(3100);
    out.back = p.card();
    return out;
  },
  async signedOut() {
    const p = await page();
    p.api.fail = ['http_401', 401];
    await p.pass(5100);
    const n = p.reads().length;
    await p.pass(60000);
    return p.reads().length - n;
  },
  async upnext() {
    const p = await page();
    await p.open();
    const focus = p.doc.activeElement;
    p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: 8, next: { season: 1, episode: 5, title: 'Windfall' } });
    await p.pass(1000);
    const out = { card: p.card(), line: p.$('[data-card-line]').textContent, focusKept: p.doc.activeElement === focus, status: p.$('[data-status]').textContent !== '' };
    p.api.hold = [];
    await p.pass(0);
    await p.pressText('Play now');
    await p.pressText('Play now');
    out.sent = p.sent();
    return out;
  },
  async still() {
    const p = await page();
    await p.open();
    p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
    await p.pass(1100);
    const out = { card: p.card(), focus: p.doc.activeElement.textContent };
    await p.pressText('Continue watching');
    out.sent = p.sent();
    return out;
  },
  async subtitles() {
    const p = await page();
    await p.open();
    await p.press('[data-menu="subtitles"]');
    const out = { focus: p.doc.activeElement.textContent, under: p.$('[data-bottom]').inert };
    p.key('[data-po]', 'Escape');
    out.escape = { menu: p.seen('[data-po]'), open: p.isOpen(), focus: p.doc.activeElement.getAttribute('data-menu') };
    await p.press('[data-menu="subtitles"]');
    await p.pressText('English');
    out.sent = p.sent();
    return out;
  },
  async back() {
    const p = await page();
    await p.open();
    await p.press('[data-menu="quality"]');
    const out = [p.trail()];
    await p.step(-1);
    out.push(p.trail());
    await p.step(-1);
    await p.pass(400);
    out.push(p.trail());
    return { out, page: p.heardByPage, mark: p.history.state.mm };
  },
  async closedOtherwise() {
    const p = await page();
    await p.open();
    await p.press('[data-menu="quality"]');
    await p.pressText('Auto' + 'Up to 1080p');
    await p.pass(20);
    const out = [p.trail()];
    await p.press('[data-menu="quality"]');
    p.api.tvs[0].play = aPlay({ play_id: 'p2' });
    await p.pass(1200);
    out.push(p.trail());
    await p.press('[data-close]');
    await p.open(); // before the step back is made
    await p.pass(400);
    out.push(p.trail());
    await p.press('[data-close]');
    await p.pass(400);
    out.push(p.trail());
    return { out, page: p.heardByPage };
  },
  async forward() {
    const p = await page();
    await p.open();
    await p.step(-1);
    await p.pass(400);
    await p.step(1);
    await p.pass(400);
    const out = [p.trail()];
    await p.step(-1);
    await p.pass(400);
    Object.assign(p.api.tvs[0], { play: null });
    await p.step(1);
    await p.pass(400);
    out.push(p.trail());
    return { out, page: p.heardByPage };
  },
  async otherEntries() {
    const p = await page();
    await p.open();
    await p.step(-1);
    await p.pass(400);
    // The page goes on to an entry of its own, and back: that is the page's.
    p.history.pushState({ mm: { id: 'b' } });
    await p.step(-1);
    const own = p.heardByPage.slice();
    p.history.pushState({ mm: { id: 'c' } });
    await p.open();
    await p.step(-2); // past the remote's entry and the page's own
    await p.pass(400);
    return { own, both: p.heardByPage, trail: p.trail() };
  },
  async reloaded() {
    const state = { mm: { id: 'a' }, mmTv: { level: 1, id: 7, base: 'x' } };
    const p = await page({ state: { ...state, mmTv: { level: 0, base: 'x' } }, reloadedAt: state });
    await p.pass(2000);
    return { at: p.history.index, open: p.isOpen(), bar: p.bar().shown, page: p.heardByPage };
  },
  async hasty() {
    const p = await page();
    p.history.pushState({ mm: { id: 'b' } });
    await p.open();
    await p.press('[data-close]');
    await p.open();
    await p.press('[data-close]');
    await p.pass(2500);
    return { at: p.history.index, open: p.isOpen() };
  },
  async bringTrail() {
    const p = await page();
    await p.open();
    await p.press('[data-bring]');
    await p.pass(100);
    return { left: p.history.entries.length, replaced: p.history.entries[1] === null, href: p.win.location.href };
  },
  async stands() {
    const p = await page();
    await p.open();
    p.api.hold = [];
    Object.assign(p.api.tvs[0], { on: false, lost_s: null });
    await p.pass(1100);
    const at = p.at();
    await p.press('[data-play]');
    await p.press('[data-mini-play]');
    // Polls that come late give no newer place.
    p.api.fail = ['network_error', 0];
    await p.pass(2000);
    return { stands: p.at() === at, sent: p.sent().length, inert: p.$('[data-seek]').inert };
  },
  async hang() {
    const p = await page();
    await p.open();
    await p.pass(1100);
    p.api.hang = true;
    await p.pass(11500);
    const out = { card: p.card() };
    const at = p.at();
    await p.pass(20000);
    out.stands = p.at() === at;
    p.api.hang = false;
    await p.pass(14000);
    out.back = p.card();
    return out;
  },
  async cutOff() {
    const p = await page();
    p.api.empty = true;
    await p.pass(6000);
    const out = [p.bar().shown];
    await p.open();
    await p.pass(5000);
    p.api.empty = false;
    await p.pass(5000);
    return { bar: out.concat(p.bar().shown), open: p.isOpen(), card: p.card() };
  },
  async atTheEnd() {
    const out = {};
    const next = { rating_key: '9002', season: 1, episode: 5, title: 'Windfall' };
    const cases = { still: ['still', 'still', next], lost: ['upnext', 'lost', next], none: ['ended', 'lost', null], unknown: ['still', 'still', { season: null, episode: null, title: null }], unseen: ['playing', 'lost', next] };
    for (const [name, [state, reason, following]] of Object.entries(cases)) {
      const p = await page();
      await p.open();
      const what = aPlay({ state, next: following, position_s: 2880 });
      p.api.tvs[0].play = what;
      await p.pass(1100);
      Object.assign(p.api.tvs[0], { play: null, last: { reason, rating_key: '9001', title: 'x', position_s: name === 'still' ? 2870 : 2880, duration_s: 2880 } });
      await p.pass(1100);
      const labels = p.doc.querySelectorAll('[data-card-actions] button').map((b) => b.textContent);
      p.api.calls.length = 0;
      await p.pressText(labels[0]);
      out[name] = { labels, sent: p.sent(), open: p.isOpen() };
      if (labels.length > 1) { await p.pressText('Bring it here'); out[name].href = p.win.location.href; }
    }
    return out;
  },
  async midway() {
    const p = await page();
    await p.open();
    Object.assign(p.api.tvs[0], { play: null, last: { ...aPlay({ position_s: 1268 }), reason: 'lost' } });
    await p.pass(1100);
    await p.pressText('Try again');
    return p.sent();
  },
  async lostOfTwo() {
    const p = await page({ tvs: [aTv(), aTv({ id: 8, name: 'Bedroom TV' })] });
    p.api.tvs.shift();
    p.api.gone = { reason: 'taken', last: aPlay({ position_s: 1268 }) };
    await p.pass(5100);
    const out = { says: [p.bar().says], other: !p.$('[data-other]').hidden && p.$('[data-other]').textContent };
    const n = p.reads().length;
    await p.pass(15500);
    out.asks = p.reads().length - n;
    await p.press('[data-other]');
    out.says.push(p.bar().says);
    await p.pass(6000);
    out.says.push(p.bar().says);
    return out;
  },
  async dismissedOfTwo() {
    const p = await page({ tvs: [aTv(), aTv({ id: 8, name: 'Bedroom TV', play: aPlay({ play_id: 'p8' }) })] });
    Object.assign(p.api.tvs[0], { on: false, lost_s: 11 });
    await p.pass(5100);
    await p.press('[data-dismiss]');
    await p.pass(100);
    const out = { live: p.bar().says };
    const q = await page();
    Object.assign(q.api.tvs[0], { on: false, lost_s: 11 });
    await q.pass(5100);
    await q.press('[data-dismiss]');
    Object.assign(q.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'lost' } });
    q.api.tvs.push(aTv({ id: 8, name: 'Bedroom TV', play: aPlay({ play_id: 'p8' }) }));
    await q.pass(5100);
    out.ended = q.bar();
    return out;
  },
  async beganMeanwhile() {
    const p = await page();
    await p.open();
    p.api.tvs.push(aTv({ id: 8, name: 'Bedroom TV' }));
    Object.assign(p.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'lost' } });
    await p.pass(1100);
    await p.press('[data-close]');
    return !p.$('[data-other]').hidden && p.$('[data-other]').textContent;
  },
  async anotherStarts() {
    const p = await page();
    Object.assign(p.api.tvs[0], { on: false, play: null, last: { ...aPlay(), reason: 'lost' } });
    await p.pass(5100);
    const out = { before: !p.$('[data-other]').hidden };
    p.api.tvs.push(aTv({ id: 8, name: 'Bedroom TV' }));
    p.doc.listeners.visibilitychange.forEach((fn) => fn());
    await settle();
    out.after = !p.$('[data-other]').hidden;
    return out;
  },
  async waiting() {
    const p = await page({ tvs: [aTv({ play: aPlay({ state: 'starting' }) })] });
    await p.open();
    const drawn = () => ['[data-mini-play]', '[data-play]'].map((sel) => p.$(sel).querySelectorAll('.ico, .player-btn-ring')
      .filter((n) => !n.hasAttribute('hidden')).map((n) => n.getAttribute('class')));
    const out = { waits: drawn() };
    p.api.tvs[0].play.state = 'playing';
    await p.pass(1100);
    out.plays = drawn();
    return out;
  },
  async leaves() {
    const out = {};
    for (const still of [false, true]) {
      const p = await page({ still });
      await p.open();
      const at = () => ['[data-po]', '[data-po-veil]', '[data-card]', '[data-dim]'].map((s) => (p.$(s).hidden ? 'gone' : p.seen(s) ? 'there' : 'leaving'));
      await p.press('[data-menu="quality"]');
      await p.press('[data-po-close]');
      const menu = [at(), p.$('[data-po]').inert];
      await p.pass(200);
      menu.push(at());
      await p.press('[data-menu="quality"]');
      await p.press('[data-po-close]');
      await p.press('[data-menu="quality"]'); // again before it has gone
      await p.pass(200);
      menu.push(at()[0]);
      await p.press('[data-po-close]');
      await p.pass(200);
      p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
      await p.pass(1100);
      const card = [at()];
      p.api.tvs[0].play = aPlay({ play_id: 'p2' });
      await p.pass(0);
      for (let i = 0; i < 100 && at()[2] === 'there'; i++) await p.pass(20);
      card.push(at(), p.$('[data-card]').inert);
      // Back before it has gone.
      p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
      await p.pass(1500);
      card.push(at(), p.$('[data-card]').inert);
      out[still ? 'still' : 'moves'] = { menu, card };
    }
    return out;
  },
  async stillIcon() {
    const p = await page();
    await p.open();
    p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
    await p.pass(1100);
    const out = [p.$('[data-play]').querySelector('use').getAttribute('href')];
    p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: null });
    await p.pass(1100);
    return out.concat(p.$('[data-play]').querySelector('use').getAttribute('href'));
  },
  async takenOptions() {
    const chapters = [{ title: 'One', start_s: 0, end_s: 9 }, { title: 'Two', start_s: 100, end_s: 200 }];
    const p = await page({ tvs: [aTv({ play: aPlay({ chapters, quality: 'low', next: { season: 1, episode: 5, title: 'x', rating_key: '2' } }) })] });
    await p.open();
    const shown = () => ['[data-menu="chapters"]', '[data-menu="subtitles"]', '[data-next]'].map((s) => !p.$(s).hidden).concat(p.$('[data-quality]').textContent, p.$('[data-play]').querySelector('use').getAttribute('href'));
    const out = [shown()];
    p.api.tvs = [];
    p.api.gone = { reason: 'taken', last: { reason: 'taken', rating_key: '9001', position_s: 1268, title: 'Glass Orchard', year: 2022, season: 1, episode: 4, episode_title: 'The Grafting Knife', duration_s: 2880 } };
    await p.pass(1100);
    out.push(shown(), p.$('[data-hint]').textContent);
    return out;
  },
  async endFocus() {
    const out = {};
    for (const from of ['playing', 'upnext', 'still', 'waits']) {
      const p = await page();
      await p.open();
      p.$('[data-play]').focus();
      if (from === 'upnext') p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: 8 });
      if (from === 'still') p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
      if (from === 'waits') p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: null });
      await p.pass(1100);
      if (from === 'waits') p.$('#tv-remote').focus(); // where a browser's focus goes from what is out of reach
      p.api.tvs[0].play = aPlay({ state: 'ended' });
      await p.pass(1100);
      out[from] = p.doc.activeElement.textContent;
    }
    return out;
  },
  async upnextHint() {
    const out = [];
    for (const s of [8, null]) {
      const p = await page();
      await p.open();
      p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: s, next: { season: 1, episode: 5, title: 'x' } });
      await p.pass(1100);
      out.push([p.$('[data-hint]').textContent, p.$('[data-bottom]').contains(p.$('[data-card]'))]);
      p.api.tvs[0].play = aPlay({ state: 'still', still_since_s: 60, still_in_s: 540 });
      await p.pass(1100);
      out[out.length - 1].push(p.$('[data-bottom]').contains(p.$('[data-card]')));
    }
    return out;
  },
  async mouseClose() {
    const p = await page();
    await p.open();
    const out = [];
    // Safari gives a clicked button no focus: it is with the page, or what it was in.
    for (const at of [p.doc.body, p.$('#tv-remote')]) {
      p.doc.activeElement = at;
      await p.press('[data-close]');
      out.push(p.doc.activeElement.hasAttribute('data-open'));
      await p.pass(400);
      await p.open();
    }
    return out;
  },
  async ownUnpair() {
    const p = await page({ tvs: [aTv(), aTv({ id: 8, name: 'Bedroom TV' })] });
    p.api.tvs.shift();
    p.api.gone = { reason: 'unpaired', last: aPlay() };
    p.win.MM.tvRemote.drop(7);
    await p.pass(400);
    const out = [p.bar().says];
    p.win.MM.tvRemote.drop(99);
    out.push(p.bar().shown && p.bar().says);
    await p.pass(400);
    p.api.tvs = [];
    await p.pass(5100);
    out.push(p.bar().says); // another person's doing, which the bar says
    p.win.MM.tvRemote.drop('8');
    await p.pass(400);
    return out.concat(p.bar().shown, p.toasts.length);
  },
  async olderTaken() {
    const out = [];
    for (const open of [false, true]) {
      const p = await page({ tvs: [aTv({ kind: 'older' })] });
      if (open) await p.open();
      p.api.tvs[0].play = null; // everyone's: it stays listed, with nothing of theirs
      p.api.gone = { reason: 'taken', last: aPlay({ position_s: 1268 }) };
      p.api.goneIds = [7];
      await p.pass(5100);
      out.push(open ? p.card() : p.bar().says);
    }
    return out;
  },
  async olderWords() {
    const out = {};
    for (const kind of ['page', 'older']) {
      out[kind] = {};
      for (const reason of ['taken', 'unpaired', 'signed_out', 'lost']) {
        const p = await page({ tvs: [aTv({ kind })] });
        await p.open();
        Object.assign(p.api.tvs[0], { play: null, last: { ...aPlay(), reason } });
        await p.pass(1100);
        out[kind][reason] = p.card() + '. ' + p.$('[data-card-line]').textContent;
      }
      const p = await page({ tvs: [aTv({ kind })] });
      await p.open();
      Object.assign(p.api.tvs[0], { on: false, lost_s: 11 });
      await p.pass(1100);
      out[kind].live = p.$('[data-card-line]').textContent;
      const q = await page({ tvs: [aTv({ kind })] });
      q.api.refuse = 'tv_off';
      await q.press('[data-mini-play]');
      out[kind].off = q.toasts[0];
    }
    return out;
  },
  async barOffline() {
    const p = await page();
    p.api.fail = ['network_error', 0];
    await p.pass(5100);
    const says = [p.bar().says];
    await p.pass(5100);
    says.push(p.bar().says);
    const buttons = () => ['[data-mini-play]', '[data-mini-back]', '[data-mini-stop]', '[data-dismiss]'].filter((s) => !p.$(s).hidden);
    const down = buttons();
    p.api.fail = null;
    await p.pass(5100);
    return { says, down, back: p.bar().says, up: buttons() };
  },
  async restarted() {
    const out = {};
    for (const kind of ['page', 'older']) {
      const p = await page({ tvs: [aTv({ kind, boot: 'a' })] });
      await p.open();
      p.api.tvs[0].play.position_s = 1268;
      await p.pass(1000);
      Object.assign(p.api.tvs[0], { play: null, boot: 'b' });
      await p.pass(1100);
      const card = [p.card(), p.$('[data-card-line]').textContent];
      click(p.doc, p.$('[data-card-actions]').querySelectorAll('button').find((b) => b.textContent === 'Continue watching'));
      await settle();
      const q = await page({ tvs: [aTv({ kind, boot: 'a' })] });
      Object.assign(q.api.tvs[0], { play: null, boot: 'b' });
      await q.pass(5100);
      out[kind] = { card, sent: p.sent(), bar: q.bar().shown, toasts: q.toasts };
    }
    const same = await page({ tvs: [aTv({ boot: 'a' })] });
    Object.assign(same.api.tvs[0], { play: null });
    await same.pass(5100);
    const both = await page({ tvs: [aTv({ boot: 'a' })] });
    Object.assign(both.api.tvs[0], { play: null, boot: 'b', last: { ...aPlay(), reason: 'lost' } });
    await both.pass(5100);
    return { ...out, same: same.toasts, both: both.bar().says };
  },
  async openEnded() {
    const marked = (p) => p.doc.querySelectorAll('#tv-remote [data-modal-focus]').filter((n) => !n.hidden).map((n) => n.textContent.trim() || n.getAttribute('aria-label'));
    const p = await page({ tvs: [aTv({ play: aPlay({ state: 'ended' }) })] });
    await p.open();
    const out = [marked(p)];
    p.api.tvs[0].play = aPlay({ play_id: 'p2' });
    await p.pass(5100);
    return out.concat([marked(p)]);
  },
  async statusCleared() {
    const p = await page();
    await p.open();
    p.api.tvs[0].play = aPlay({ state: 'upnext', upnext_in_s: 8 });
    await p.pass(1100);
    const said = p.$('[data-status]').textContent !== '';
    p.api.tvs[0].play = aPlay({ play_id: 'p2' });
    await p.pass(1100);
    return { said, left: p.$('[data-status]').textContent };
  },
  async two() {
    const p = await page({ tvs: [aTv(), aTv({ id: 8, name: 'Bedroom TV' })] });
    const out = [p.bar().says];
    await p.press('[data-other]');
    out.push(p.bar().says);
    await p.press('[data-mini-play]');
    await p.pass(6000);
    return { says: out, to: p.api.calls.find((c) => c.method === 'POST').url, stays: p.bar().says };
  },
  // A Chromecast beside no TV of mediaman's, with all the sender knows of it, and with the least.
  async cast() {
    const full = aCast({ chapters: [{ title: 'The boat', start_s: 0 }, { title: 'The shore', start_s: 1500 }],
      subtitle_tracks: [{ id: '11', label: 'English' }], next: { rating_key: '9002', label: 'S1 · E5 — Windfall' } });
    const p = await page({ tvs: [], cast: { now: full } });
    const reads = p.reads().length;
    const out = { bar: p.bar(), title: p.$('[data-mini-title]').textContent };
    await p.pass(30000);
    out.asks = p.reads().length - reads;
    await p.open();
    const controls = ['[data-play]', '[data-back10]', '[data-seek-row]', '[data-bring]', '[data-stop]', '[data-volume]', '[data-mute]',
      '[data-menu="chapters"]', '[data-menu="subtitles"]', '[data-menu="quality"]', '[data-next]', '[data-no-volume]'];
    out.shown = controls.filter((c) => p.seen(c)).length;
    out.nextName = p.$('[data-next]').getAttribute('aria-label');
    out.hint = p.$('[data-hint]').textContent;
    out.subtitle = p.$('[data-subtitle]').textContent;
    await p.press('[data-play]');
    out.paused = p.$('[data-on]').textContent;
    Object.assign(p.cast.now, { paused: false, state: 'playing' });
    await p.pass(600);
    out.byItsOwnRemote = p.$('[data-on]').textContent;
    await p.press('[data-fwd10]');
    await p.press('[data-fwd10]');
    await p.pass(400);
    out.at = p.at();
    p.cast.now.position_s = 1300;
    await p.pass(600);
    out.follows = p.at();
    p.$('[data-volume]').value = '70';
    await p.on('[data-volume]', 'change', {});
    await p.press('[data-mute]');
    await p.press('[data-menu="subtitles"]');
    await p.pressText('English');
    await p.press('[data-menu="quality"]');
    await p.pressText('720p4 Mbps');
    out.quality = p.$('[data-quality]').textContent;
    await p.press('[data-next]');
    await p.pass(600);
    out.next = p.$('[data-subtitle]').textContent;
    out.did = p.cast.did;
    out.sent = p.sent();
    const q = await page({ tvs: [], cast: { now: aCast({ can_volume: false, quality: null, season: null, episode: null }) } });
    await q.open();
    out.least = controls.filter((c) => q.seen(c)).slice(5).join();
    out.film = q.$('[data-hint]').textContent;
    return out;
  },
  // The TV's own remote was quicker: what was pressed holds already.
  async castRaced() {
    const p = await page({ tvs: [], cast: { now: aCast() } });
    Object.assign(p.cast.now, { paused: true, state: 'paused' });
    await p.press('[data-mini-play]');
    return { did: p.cast.did, says: p.bar().says };
  },
  async castRefused() {
    const p = await page({ tvs: [], cast: { now: aCast(), refuse: 'too_many_streams' } });
    await p.open();
    await p.press('[data-menu="quality"]');
    await p.pressText('720p4 Mbps');
    return { toasts: p.toasts, on: p.$('[data-on]').textContent, quality: p.$('[data-quality]').textContent };
  },
  async castUpnext() {
    const p = await page({ tvs: [], cast: { now: aCast() } });
    await p.open();
    Object.assign(p.cast.now, { state: 'upnext', upnext_in_s: 8, next: { rating_key: '9002', label: 'S1 · E5 — Windfall' } });
    p.cast.change(null);
    const out = { card: p.card(), line: p.$('[data-card-line]').textContent };
    await p.pressText('Cancel');
    out.waits = p.card();
    await p.pressText('Play now');
    await p.pass(600);
    out.playing = p.$('[data-on]').textContent;
    Object.assign(p.cast.now, { state: 'still', still_in_s: 540 });
    p.cast.change(null);
    out.still = [p.card(), p.$('[data-card-line]').textContent.includes('in 9 minutes'), p.$('[data-card-line]').textContent.includes('since ' + p.clock(-60000))];
    out.did = p.cast.did;
    return out;
  },
  async castStop() {
    const p = await page({ tvs: [], cast: { now: aCast() } });
    await p.press('[data-mini-stop]');
    await p.pass(600);
    const q = await page({ tvs: [], cast: { now: aCast() } });
    await q.open();
    await q.press('[data-bring]');
    await q.pass(600);
    const r = await page({ tvs: [], cast: { now: aCast(), saved: false } });
    await r.press('[data-mini-stop]');
    await r.pass(600);
    return { bar: p.bar().shown, toasts: p.toasts, did: [p.cast.did, q.cast.did], href: q.win.location.href, said: q.toasts, unsaved: r.toasts };
  },
  // The server takes its time over the place, with another TV playing.
  async castStopSlow() {
    let go;
    const p = await page({ cast: { now: aCast(), hold: new Promise((r) => { go = r; }) } });
    await p.press('[data-other]');
    await p.open();
    await p.press('[data-stop]');
    await p.pass(3000);
    const out = { on: p.$('[data-on]').textContent, dead: p.$('[data-stop]').disabled, open: p.isOpen() };
    await p.press('[data-stop]');
    go();
    await p.pass(600);
    return { ...out, after: [p.isOpen(), p.bar().says], sent: p.sent(), did: p.cast.did, toasts: p.toasts };
  },
  async castLost() {
    const p = await page({ tvs: [], cast: { now: aCast() } });
    await p.open();
    const acts = () => p.$('[data-card-actions]').querySelectorAll('button').map((b) => b.textContent);
    const lost = (on) => { p.cast.now.lost = on; p.cast.change(null); };
    lost(true);
    const out = { card: p.card(), acts: acts(), dead: p.$('[data-play]').inert };
    lost(false);
    out.back = p.card();
    lost(true);
    p.cast.now.position_s = 1268;
    p.cast.end('lost');
    await settle();
    out.over = [p.card(), p.$('[data-card-line]').textContent, acts()];
    // The page was hidden, or asleep, all the while.
    const h = await page({ tvs: [], cast: { now: aCast() } });
    h.doc.hidden = true;
    h.cast.end('lost');
    await h.pass(1000);
    out.hidden = h.bar();
    // Dismissed, back and lost again.
    const d = await page({ tvs: [], cast: { now: aCast() } });
    const turn = async (on) => { d.cast.now.lost = on; d.cast.change(null); await d.pass(300); };
    await turn(true);
    await d.press('[data-dismiss]');
    await d.pass(300);
    out.dismissed = [d.bar().shown];
    await turn(false);
    await turn(true);
    out.dismissed.push(d.bar().shown);
    return out;
  },
  // It ends by itself, something else plays there, or a tab of its own goes on with it.
  async castOver() {
    const over = async (reason, away) => {
      const p = await page({ tvs: [], cast: { now: aCast(), away } });
      await p.open();
      p.cast.end(reason);
      await p.pass(600);
      return { open: p.isOpen(), bar: p.bar().shown, toasts: p.toasts, card: p.card() };
    };
    return { ended: await over('ended'), stopped: await over('stopped'), taken: await over('taken'), elsewhere: await over('ended', true) };
  },
  // What begins later than the page, beside a TV, and with mediaman out of reach.
  async castBeside() {
    const late = await page({ tvs: [], cast: {} });
    const before = late.bar().shown;
    late.cast.now = aCast();
    late.cast.change(null);
    // It ends while the list of TVs is on its way.
    const left = await page({ cast: { now: aCast() } });
    await left.press('[data-other]');
    const held = [];
    const get = left.win.MM.api.get;
    left.win.MM.api.get = async (...a) => { const r = await get(...a); await new Promise((go) => held.push(go)); return r; };
    await left.pass(8000);
    left.cast.end('ended');
    held.forEach((go) => go());
    await settle();
    const ended = await page({ cast: { now: aCast() } });
    await ended.press('[data-other]');
    await ended.pass(1000);
    ended.cast.end('ended');
    await settle();
    // A TV's play ends under the open remote: what else plays has the bar, then and later.
    const after = [];
    for (const when of ['before', 'later']) {
      const t = await page({ cast: { now: when === 'before' ? aCast() : null } });
      await t.open();
      Object.assign(t.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'stopped' } });
      await t.pass(1100);
      if (when === 'later') { t.cast.now = aCast(); t.cast.change(null); }
      await t.pass(600);
      after.push(t.bar().says);
      // The list of TVs does not come.
      const f = await page({ cast: { now: null } });
      await f.open();
      Object.assign(f.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'stopped' } });
      const one = f.win.MM.api.get;
      f.win.MM.api.get = (u, o) => (u === '/api/tvs' ? Promise.reject(new Error('down')) : one(u, o));
      await f.pass(1100);
      f.cast.now = aCast();
      f.cast.change(null);
      await f.pass(600);
      after.push(f.bar().says);
    }
    const two = await page({ cast: { now: aCast() } });
    const says = [two.bar().says];
    await two.press('[data-other]');
    says.push(two.bar().says);
    two.api.fail = ['down', 502];
    await two.pass(20000);
    says.push(two.bar().says);
    await two.press('[data-mini-play]');
    return { late: [before, late.bar()], says, after: [left.bar().says, ended.bar().says, ...after], did: two.cast.did, alone: (await page({ tvs: [], cast: {} })).bar().shown };
  },
  // Play on from the remote: what its sheet is told, and what becomes of both TVs.
  async playOn() {
    const make = async (opts) => {
      const p = await page(opts);
      const sheet = { open: false, host: null };
      p.win.MM.playOn = { open: (b, host) => { sheet.open = true; sheet.host = host; }, isOpen: () => sheet.open, close: () => { sheet.open = false; } };
      await p.open();
      await p.press('#tv-remote [data-play-on]');
      return { p, sheet, host: sheet.host };
    };
    const a = await make();
    const b = a.p.$('#tv-remote [data-play-on]');
    const out = { shown: p_shown(b), on: a.host.on(), at: a.host.before({ id: 8 }), what: a.host.what() };
    function p_shown(n) { return [n.hidden, n.classList.contains('is-on')]; }
    // The TV it is on ends first; then the other has it.
    Object.assign(a.p.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'moved' } });
    await a.p.pass(1100);
    out.between = [a.p.card(), a.p.isOpen()];
    const other = aTv({ id: 8, name: 'Bedroom TV', play: aPlay({ play_id: 'p9' }) });
    a.p.api.tvs.push(other);
    a.host.started(other);
    await settle();
    out.moved = a.p.$('[data-on]').textContent;
    const f = await make();
    f.host.before({ id: 8 });
    Object.assign(f.p.api.tvs[0], { play: null });
    await f.p.pass(1100);
    f.host.failed();
    await settle();
    out.failed = [f.p.isOpen(), f.sheet.open];
    const g = await make();
    out.unpressed = g.host.what().asked_s;
    g.host.before({ id: 8 });
    g.host.failed();
    Object.assign(g.p.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'stopped' } });
    await g.p.pass(1100);
    out.failedThenStopped = g.p.toasts.length;
    // Another that plays is not shown while one is moved.
    const m = await make({ cast: { now: aCast() } });
    m.host.before({ id: 8 });
    Object.assign(m.p.api.tvs[0], { play: null, last: { ...aPlay(), reason: 'moved' } });
    await m.p.pass(1100);
    m.p.cast.change(null);
    out.besideMove = m.p.$('[data-on]').textContent;
    const none = await page();
    await none.open();
    out.without = none.$('#tv-remote [data-play-on]').hidden;
    const c = await make({ tvs: [], cast: { now: aCast({ subtitle_id: null, next: { rating_key: '9002', label: 'x' } }) } });
    out.cast = [c.host.on(), 'subtitle' in c.host.what()];
    c.p.api.tvs.push(other);
    c.host.started(other);
    await settle();
    out.fromCast = [c.p.cast.did, c.p.$('[data-on]').textContent];
    const t = await make({ cast: { now: null } });
    t.p.cast.now = aCast();
    t.host.started({ id: 'cast', name: 'Den' });
    await settle();
    out.toCast = [t.p.$('[data-on]').textContent, t.p.toasts];
    const h = await make();
    h.host.here();
    await h.p.pass(600);
    out.here = h.p.win.location.href;
    const u = await make({ tvs: [aTv({ play: aPlay({ state: 'upnext', next: { rating_key: '9002', season: 1, episode: 5, title: 'x' } }) })] });
    u.host.before({ id: 8 });
    out.upnext = [u.host.what().rating_key, u.host.before({ id: 8 })];
    return out;
  },
  async paintsTitles() {
    const p = await page();
    const b = p.doc.createElement('button');
    b.setAttribute('data-play-on-key', '9001');
    const c = p.doc.createElement('button');
    c.setAttribute('data-play-on-key', '42');
    p.doc.body.appendChild(b);
    p.doc.body.appendChild(c);
    await p.pass(5100);
    const look = () => [b.classList.contains('is-on'), b.getAttribute('aria-label'), c.classList.contains('is-on'), p.win.MM.tvRemote.plays('9001')];
    const out = [look()];
    await p.open();
    p.api.tvs[0].play.state = 'ended';
    await p.pass(1100);
    return out.concat([look()]);
  },
  async noVolume() {
    const p = await page({ tvs: [aTv({ can_volume: false })] });
    await p.open();
    return { slider: !p.$('[data-volume]').hidden, words: !p.$('[data-no-volume]').hidden };
  },
  async opens() {
    const p = await page({ tvs: [aTv({ play: null })] });
    const playroom = aTv({ id: 9, name: 'Playroom', play: aPlay({ state: 'starting' }) });
    p.api.tvs.push(playroom);
    p.win.MM.tvRemote.open(playroom);
    await settle();
    const given = { open: p.isOpen(), on: p.$('[data-on]').textContent };
    const q = await page();
    q.win.MM.tvRemote.open(7);
    await settle();
    return { given, byId: q.isOpen() };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
