// Runs watch/airplay.js on a player's page against a stand-in for Safari's video element and its
// AirPlay list, and prints one scenario's outcome as JSON.
// Usage: node watch_airplay_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

// A video element as Safari has it: where it plays is the TV's to say.
class Video extends EventTarget {
  constructor() {
    super();
    this.paused = true;
    this.seeks = [];
    this.at = 0;
    this.webkitCurrentPlaybackTargetIsWireless = false;
    this.attrs = {};
    this.parentNode = { insertBefore: () => {} };
  }
  get currentTime() { return this.at; }
  set currentTime(t) { this.at = t; this.seeks.push(t); }
  setAttribute(n, v) { this.attrs[n] = v; }
  play() { return Promise.resolve(); }
  pause() { this.paused = true; }
  load() {}
  webkitShowPlaybackTargetPicker() { this.picked = true; }
  querySelector() { return null; }
  say(type) { this.dispatchEvent(new Event(type)); }
}

function page() {
  const first = new Video();
  const air = new Video();
  const ctx = {
    console, Promise, Object, Error, Event, EventTarget,
    setTimeout: () => 0, clearTimeout: () => {},
    WebKitPlaybackTargetAvailabilityEvent: function () {},
    navigator: { userAgent: 'Safari', maxTouchPoints: 0 },
    document: {
      getElementById: (id) => (id === 'player' ? { querySelector: () => first } : null),
      createElement: () => air,
    },
    MM: { playOn: { register: (way) => { ctx.way = way; }, changed() {} } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch/airplay.js'), 'utf8'), ctx);
  return { ctx, air };
}

// The person chooses the TV, and the stream comes to the element at `place`.
async function onTv(place) {
  const p = page();
  const started = p.ctx.way.start({ title: {}, position_s: place, open: () => Promise.resolve({ url: 'https://media.example/api/cast/t/index.m3u8', position_s: place }) });
  p.air.webkitCurrentPlaybackTargetIsWireless = true;
  p.air.say('webkitcurrentplaybacktargetiswirelesschanged');
  await settle();
  p.air.say('loadedmetadata');
  await started;
  p.air.seeks.length = 0;
  return p;
}

const scenarios = {
  // The TV begins the stream from its start, then plays at the place; then the person goes back.
  async resumes() {
    const { air } = await onTv(1268);
    air.at = 0;
    air.paused = false;
    air.say('playing');
    const sent = air.seeks.slice();
    air.at = 1270;
    air.say('timeupdate');
    air.currentTime = 0;
    air.seeks.length = 0;
    air.say('timeupdate');
    return { sent, later: air.seeks };
  },
  // From the start, nothing is moved.
  async fromStart() {
    const { air } = await onTv(0);
    air.paused = false;
    air.say('playing');
    air.say('timeupdate');
    return { seeks: air.seeks };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (err) => { console.error(err); process.exit(1); });
