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
      el.hidden = el.hasAttribute('hidden');
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
  async function call(method, url, body) {
    api.calls.push({ method, url, body: body && copy(body), at: clock });
    if (api.fail) throw new APIError(...api.fail);
    if (method === 'POST' && api.hold) await new Promise((r) => { api.hold.push(r); });
    if (url === '/api/tvs') return { tvs: copy(api.tvs) };
    const tv = api.tvs.find((t) => url.startsWith('/api/tvs/' + t.id));
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
    matchMedia: () => ({ matches: false }),
    addEventListener(t, fn) { win.listeners[t] = fn; },
    UIFeedback: { toast: (t) => toasts.push(t), error: (t) => toasts.push('! ' + t) },
    ModalA11y: { register() {}, onOpened() {}, onClosed() {} },
    MM: { api: { get: (u) => call('GET', u), post: (u, b) => call('POST', u, b), messageOf: (e, f) => f } },
  };
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
    $, doc, win, api, toasts, history, heardByPage,
    async step(by) { history.go(by); await p.pass(20); },
    trail: () => ({ at: history.index, entries: history.entries.length, open: p.isOpen(), menu: !p.$('[data-po]').hidden }),
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
    at: () => $('[data-elapsed]').textContent,
    bar: () => ({ shown: !$('#tv-mini').hidden && !$('#tv-mini').classList.contains('is-leaving'), says: $('[data-mini-say]').textContent }),
    card: () => ($('[data-card]').hidden ? null : $('[data-card-title]').textContent),
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
    out.escape = { menu: !p.$('[data-po]').hidden, open: p.isOpen(), focus: p.doc.activeElement.getAttribute('data-menu') };
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
  async two() {
    const p = await page({ tvs: [aTv(), aTv({ id: 8, name: 'Bedroom TV' })] });
    const out = [p.bar().says];
    await p.press('[data-other]');
    out.push(p.bar().says);
    await p.press('[data-mini-play]');
    await p.pass(6000);
    return { says: out, to: p.api.calls.find((c) => c.method === 'POST').url, stays: p.bar().says };
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
