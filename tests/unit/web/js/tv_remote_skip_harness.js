// Runs the real tv-remote.js on its own template against the fake DOM, a TV playing a title with
// an intro and credits, and a stepped clock; prints what each scenario saw as one JSON object.
// Usage: node tv_remote_skip_harness.js   (the rendered template on stdin)
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
      if (!['svg', 'use'].includes(hit[2])) {
        Object.defineProperty(el, 'hidden', { get: () => el.hasAttribute('hidden'), set: (on) => el.toggleAttribute('hidden', on) });
      }
      el.style.setProperty = () => {};
      top.appendChild(el);
      if (!hit[4] && !VOID.includes(hit[2])) stack.push(el);
    } else if (hit[5] && hit[5].trim()) top.appendChild(doc.createTextNode(hit[5]));
  }
}

const INTRO = { kind: 'intro', start_s: 60, end_s: 150, final: false };
const CREDITS = { kind: 'credits', start_s: 2700, end_s: 2880, final: true };

function aTv(play) {
  return {
    id: 7, name: 'Living room', on: true, lost_s: null, acked: 0, volume: 0.4, muted: false, can_volume: true, last: null,
    play: {
      play_id: 'p1', state: 'playing', title: 'Glass Orchard', year: 2022, season: 1, episode: 4, episode_title: 'The Knife',
      duration_s: 2880, markers: [INTRO, CREDITS], chapters: [], next: null, upnext_in_s: null, still_in_s: null,
      error: null, rating_key: '9001', position_s: 70, quality: 'auto', subtitle_tracks: [], subtitle: null, auto_skip: false, ...play,
    },
  };
}

async function remote(play) {
  const doc = makeDocument();
  parse(doc, TEMPLATE, doc.body);
  doc.hidden = false;
  let clock = 1e12;
  let timers = [];
  let ids = 0;
  const tv = aTv(play);
  const sent = [];
  const copy = (x) => JSON.parse(JSON.stringify(x));
  async function call(method, url, body) {
    if (method === 'POST') sent.push(copy(body));
    if (url === '/api/tvs') return { tvs: [copy(tv)] };
    if (/^\/api\/watch\/tracks\//.test(url)) return { remembered: false };
    if (method === 'GET') return { tv: copy(tv) };
    return { id: sent.length, tv: copy(tv) };
  }
  const win = {
    listeners: {},
    history: {
      entries: [{ mm: { id: 'a' } }], index: 0,
      get state() { return this.entries[this.index]; },
      pushState(st) { this.entries.splice(this.index + 1); this.entries.push(copy(st)); this.index += 1; },
      replaceState(st) { this.entries[this.index] = copy(st); },
      go() {},
    },
    location: { href: '', pathname: '/watch', search: '', hash: '', replace() {} }, innerWidth: 1000, innerHeight: 800, navigator: { onLine: true },
    sessionStorage: { getItem: () => null, setItem() {} },
    matchMedia: () => ({ matches: false }),
    addEventListener(t, fn) { win.listeners[t] = fn; },
    UIFeedback: { toast() {}, error() {} },
    ModalA11y: { register() {}, onOpened() {}, onClosed() {} },
    MM: { api: { get: (u) => call('GET', u), post: (u, b) => call('POST', u, b), put: (u, b) => call('PUT', u, b), delete: (u) => call('DELETE', u), messageOf: (e, f) => f } },
  };
  const ctx = vm.createContext({
    fetch: () => new Promise(() => {}), URL, window: win, document: doc, AbortController,
    Date: class extends Date { constructor(...a) { super(...(a.length ? a : [clock])); } static now() { return clock; } },
    setTimeout: (fn, ms) => { timers.push({ id: ++ids, at: clock + ms, fn }); return ids; },
    clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    clearInterval: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: (fn, ms) => { timers.push({ id: ++ids, at: clock + ms, fn, every: ms }); return ids; },
  });
  vm.runInContext(fs.readFileSync(path.join(JS, 'core/dom.js'), 'utf8'), ctx);
  const $ = (sel) => doc.querySelector(sel);
  $('[data-seek]').getBoundingClientRect = () => ({ left: 0, right: 1000, width: 1000, top: 0, bottom: 44 });
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch/tracks.js'), 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'tv-remote.js'), 'utf8'), ctx);
  await settle();
  const r = {
    tv, sent,
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
    skip: () => ($('[data-skip]').hidden || $('[data-skip]').classList.contains('is-leaving') ? null : $('[data-skip-text]').textContent),
    async press(sel) { click(doc, $(sel)); await settle(); },
  };
  await r.press('[data-open]');
  await r.pass(100);
  return r;
}

const scenarios = {
  async intro() {
    const r = await remote({});
    const shown = r.skip();
    await r.press('[data-skip]');
    await r.pass(400);
    return { shown, sent: r.sent };
  },
  // Its end a second away, it goes, as the player's own button does.
  async introEnds() {
    const r = await remote({});
    r.tv.play.position_s = 149.5;
    await r.pass(3000);
    return r.skip();
  },
  async outside() { return (await remote({ position_s: 400 })).skip(); },
  async autoSkips() { return (await remote({ auto_skip: true })).skip(); },
  async lastCredits() {
    const r = await remote({ position_s: 2710, next: { rating_key: '9002', season: 1, episode: 5, title: 'Windfall' } });
    const shown = r.skip();
    await r.press('[data-skip]');
    await r.pass(400);
    return { shown, sent: r.sent };
  },
  // Credits a scene may follow are skipped, not led on from, though a next episode is there.
  async midCredits() {
    const r = await remote({
      position_s: 1210, markers: [INTRO, { kind: 'credits', start_s: 1200, end_s: 1260, final: false }, CREDITS],
      next: { rating_key: '9002', season: 1, episode: 5, title: 'Windfall' },
    });
    return r.skip();
  },
  async filmCredits() {
    const r = await remote({ position_s: 2710, season: null, episode: null, episode_title: null });
    const shown = r.skip();
    await r.press('[data-skip]');
    await r.pass(400);
    return { shown, sent: r.sent };
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
