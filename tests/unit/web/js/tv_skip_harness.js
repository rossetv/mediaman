// Runs the real tv.js against a fake TV browser and a stepped clock, playing titles with an intro
// and credits, and prints what each scenario saw as one JSON object. Usage: node tv_skip_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.resolve(__dirname, '../../../../src/mediaman/web/static');
const SOURCE = fs.readFileSync(path.join(WEB, 'js/tv.js'), 'utf8');
const PAGE = fs.readFileSync(path.join(WEB, '../templates/tv.html'), 'utf8');
// The nodes of the page itself, which no page drawn on the stage takes away.
const FIXED = ['v', 's', 'o', 'k', 'sk'];

function tv(play) {
  let time = 1e12;
  let timers = [];
  let ids = 0;
  const later = (fn, ms, every) => { timers.push({ id: ids += 1, at: time + (ms || 0), fn, every }); return ids; };
  const element = (id) => ({
    id, innerHTML: '', className: '', style: {}, attrs: {}, on: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(k, fn) { (this.on[k] = this.on[k] || []).push(fn); },
    appendChild(n) { return n; }, getElementsByTagName: () => [], focus() {},
  });
  const nodes = {};
  FIXED.forEach((id) => { nodes[id] = element(id); });
  const video = Object.assign(nodes.v, {
    paused: true, ended: false, volume: 1, muted: false, seeking: false, duration: 2880, time: 0, sets: [],
    canPlayType: () => 'maybe',
    load() { this.paused = true; this.time = 0; },
    play() { this.paused = false; },
    pause() { this.paused = true; },
    fire(type) { (this.on[type] || []).forEach((fn) => fn({})); },
  });
  Object.defineProperty(video, 'currentTime', { get() { return this.time; }, set(v) { this.sets.push(v); this.time = v; } });
  Object.defineProperty(video, 'src', { get() { return this.attrs.src; }, set(v) { this.attrs.src = v; } });
  const body = Object.assign(element('body'), { attrs: { 'data-base': 'https://mediaman.example', 'data-hls': '/hls.js' } });
  const stage = () => nodes.s.innerHTML;
  const document = {
    body, createElement: () => element(''), addEventListener() {}, documentElement: element('html'),
    querySelector(selector) {
      if (selector === '.mid') return { outerHTML: '<div class="mid"></div>' };
      if (selector === '.brand') return { outerHTML: '<div class="brand"></div>' };
      return null;
    },
    getElementById(id) {
      if (FIXED.includes(id)) return nodes[id];
      if (!stage().includes('id="' + id + '"')) return null;
      return (nodes[id] = nodes[id] || element(id));
    },
  };
  const now = { play };
  const asked = [];
  class XMLHttpRequest {
    open(method, url) { this.url = url; }
    setRequestHeader() {}
    getResponseHeader() { return null; }
    abort() {}
    send(text) {
      const call = { path: this.url.replace('/api/tv/device/', ''), body: JSON.parse(text) };
      if (call.path === 'ask') asked.push(call.body.what);
      const data = call.path === 'poll'
        ? { ok: true, state: 'ready', id: 1, name: 'Living room', owner: 'maren', play: now.play, commands: [], poll_ms: 1000 }
        : { ok: true };
      later(() => {
        Object.assign(this, { readyState: 4, status: 200, responseText: JSON.stringify(data) });
        this.onreadystatechange();
      }, 1);
    }
  }
  const store = { 'mm-tv': 'old' };
  const ctx = {
    document, XMLHttpRequest, Date: class extends Date { static now() { return time; } },
    localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem(k, v) { store[k] = v; }, removeItem(k) { delete store[k]; } },
    location: { protocol: 'https:', host: 'x' },
    setTimeout: (fn, ms) => later(fn, ms), clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: (fn, ms) => later(fn, ms, ms), unescape, encodeURIComponent, innerWidth: 1920, innerHeight: 1080,
  };
  ctx.window = ctx;
  vm.runInContext(SOURCE, vm.createContext(ctx));
  const t = {
    video, now, asked,
    note: () => ({ shown: nodes.sk.className === 'sk on', text: nodes.sk.innerHTML.replace(/<[^>]+>/g, '') }),
    async step(ms) {
      const until = time + ms;
      for (;;) {
        await Promise.resolve();
        const due = timers.filter((x) => x.at <= until).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        const from = time;
        time = Math.max(time, due.at);
        if (!video.paused && video.attrs.src) video.time += (time - from) / 1000;
        if (due.every) due.at = time + due.every; else timers = timers.filter((x) => x !== due);
        due.fn();
      }
      if (!video.paused && video.attrs.src) video.time += (until - time) / 1000;
      time = until;
    },
  };
  return t;
}

const film = (more) => ({
  play_id: 'p1', state: 'loading', url: '/api/cast/t/index.m3u8', title: 'Glass Orchard', year: 2024, season: 1, episode: 4,
  episode_title: 'The Knife', art_url: '/art', duration_s: 2880, start_s: 50, played_s: 0, chapters: [], next: null,
  upnext_in_s: null, still_in_s: null, error: null, auto_skip: true,
  markers: [{ kind: 'intro', start_s: 60, end_s: 150, final: false }, { kind: 'credits', start_s: 2700, end_s: 2880, final: true }],
  ...more,
});

// Started at `start_s`, playing for `ms`.
async function played(more, ms) {
  const t = tv(film(more));
  await t.step(50);
  t.video.fire('loadedmetadata');
  t.video.fire('playing');
  await t.step(ms);
  return t;
}

const scenarios = {
  // The page has the note's place, over every page drawn on the stage.
  async page() { return /<div id="k"><div class="sk" id="sk" role="status"><\/div><\/div>/.test(PAGE); },
  async beforeIntro() { const t = await played({}, 5000); return { sets: t.video.sets.filter((s) => s > 100) }; },
  async intro() {
    const t = await played({}, 11000);
    const at = Math.round(t.video.time);
    const note = t.note();
    await t.step(3000);
    return { at, note, later: t.note().shown };
  },
  async off() { const t = await played({ auto_skip: false }, 15000); return { at: Math.round(t.video.time) }; },
  async noMarks() { const t = await played({ markers: [] }, 15000); return { at: Math.round(t.video.time) }; },
  // Taken back into the intro once it was skipped, it plays.
  async back() {
    const t = await played({}, 11000);
    t.video.time = 70;
    await t.step(5000);
    return { at: Math.round(t.video.time) };
  },
  async credits() {
    const t = await played({ start_s: 2690, next: { rating_key: '502', season: 1, episode: 5, title: 'Windfall' } }, 11000);
    return { to: t.video.sets.pop(), note: t.note().shown, asked: t.asked };
  },
  async midCredits() {
    const t = await played({ start_s: 1190, markers: [{ kind: 'credits', start_s: 1200, end_s: 1260, final: false }] }, 11000);
    return { at: Math.round(t.video.time), note: t.note() };
  },
  // Paused before the intro, and moved into it while paused.
  async paused() {
    const t = await played({}, 3000);
    t.video.pause();
    t.video.fire('pause');
    t.video.time = 70;
    await t.step(5000);
    return { at: Math.round(t.video.time) };
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
