// Runs the real broken-poster.js over pictures that fail or load and prints, as JSON, which of
// them it marks broken. Usage: node broken_poster_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/broken-poster.js');

function image(attrs, w, h, complete) {
  return {
    tagName: 'IMG', dataset: {}, complete: !!complete, naturalWidth: w, naturalHeight: h,
    hasAttribute: (a) => attrs.includes(a), getAttribute: (a) => (a === 'src' ? '/x.jpg' : null),
    matches: (sel) => attrs.includes(sel.slice(4, -1)),
  };
}
const ART = ['data-wide-art', 'data-broken-on-error'];
const events = {
  wide: ['load', image(ART, 1920, 1080)], portrait: ['load', image(ART, 1000, 1500)],
  square: ['load', image(ART, 1000, 1000)], tiny: ['load', image(ART, 480, 270)],
  poster: ['load', image(['data-broken-on-error'], 400, 600)], failed: ['error', image(ART, 0, 0)],
};
const before = { earlyPortrait: image(ART, 1000, 1500, true), earlyWide: image(ART, 1920, 1080, true), pending: image(ART, 0, 0, false) };

const listeners = {};
let timers = [];
let probes = [];
let reduced = false;
function run(pictures) {
  vm.runInNewContext(fs.readFileSync(SRC, 'utf8'), {
    setTimeout: (fn, ms) => { timers.push([fn, ms]); },
    Image: function Image() { probes.push(this); },
    matchMedia: () => ({ matches: reduced }),
    getComputedStyle: () => ({ getPropertyValue: (n) => (n === '--dur' ? ' .2s' : ' ease-out') }),
    document: {
      documentElement: {},
      addEventListener: (t, fn) => { listeners[t] = fn; },
      querySelectorAll: (sel) => pictures.filter((i) => i.matches(sel)),
    },
  });
}
run(Object.values(before));
Object.values(events).forEach(([type, img]) => listeners[type] && listeners[type]({ target: img }));
const marked = (img) => 'broken' in img.dataset;
/* A picture with one more to try: its mark, its address and whether it is between the two, at each step. */
function retried(first, second, early) {
  const img = image(ART, ...first, early);
  img.dataset.nextSrc = '/next.jpg';
  const look = () => ({ src: img.src || null, marked: marked(img), retrying: 'retrying' in img.dataset });
  const fire = ([w, h]) => { img.naturalWidth = w; img.naturalHeight = h; listeners[w ? 'load' : 'error']({ target: img }); };
  if (early) run([img]); else fire(first);
  const between = look();
  if (second) fire(second);
  return [between, look()];
}
const WIDE = [1280, 720], TALL = [1000, 1500], GONE = [0, 0];
const retries = {
  unfitThenWide: retried(TALL, WIDE), goneThenWide: retried(GONE, WIDE), unfitTwice: retried(TALL, TALL),
  unfitThenGone: retried(TALL, GONE), wide: retried(WIDE), early: retried(TALL, WIDE, true), earlyGone: retried(GONE, WIDE, true),
};
/* The mark of a wide picture that fits, as one image is given picture after picture, and of
   those the script met on the page. */
const fit = (img) => ('fit' in img.dataset ? img.dataset.fit : null);
function reused(img, w, h) {
  img.naturalWidth = w; img.naturalHeight = h;
  listeners[w ? 'load' : 'error']({ target: img });
  return fit(img);
}
const again = image(ART, 1920, 1080);
const fits = {
  loads: reused(again, ...WIDE), thenTall: reused(again, ...TALL), thenWide: reused(again, ...WIDE), thenGone: reused(again, ...GONE),
  early: fit(before.earlyWide), earlyTall: fit(before.earlyPortrait), pending: fit(before.pending), poster: fit(events.poster[1]),
  earlyThenItsEvent: reused(before.earlyWide, ...WIDE),
};
/* A picture of mediaman's own image route, given 'error', 'load', 'tick' (the waits end) and 'found' or 'missing'
   (the answer to the try behind it); each step tells whether it is marked, how many tries were made, and how
   often its address was set. Others change it under way. */
function own(steps, { src = '/api/watch/img/tmdb/w342/a.jpg', next, early, wide, calm } = {}) {
  timers = [];
  probes = [];
  reduced = !!calm;
  const img = image(wide ? ART : ['data-broken-on-error'], 0, 0, true);
  let at = src;
  let set = 0;
  let fades = 0;
  // As Safari does, an address set where it already stands is not asked for again.
  Object.defineProperty(img, 'src', { get: () => at, set: (v) => { if (v === at) return; at = v; set += 1; } });
  img.removeAttribute = () => { at = null; };
  img.getAttribute = (a) => (a === 'src' ? at : null);
  img.animate = () => { fades += 1; };
  img.isConnected = true;
  img.hidden = false;
  if (next) img.dataset.nextSrc = next;
  if (early) run([img]);
  const seen = [];
  steps.forEach((step) => {
    if (step === 'tick') { const due = timers; timers = []; due.forEach(([fn]) => fn()); }
    else if (step === 'found') probes.length && probes[probes.length - 1].onload();
    else if (step === 'missing') probes.length && probes[probes.length - 1].onerror();
    else if (step === 'other') { at = '/api/watch/img/tmdb/w342/b.jpg'; delete img.dataset.broken; }
    else if (step === 'afresh') delete img.dataset.broken;
    else if (step === 'gone') img.isConnected = false;
    else if (step === 'hide') img.hidden = true;
    else {
      [img.naturalWidth, img.naturalHeight] = step === 'load' ? [400, 600] : [0, 0];
      listeners[step]({ target: img });
    }
    seen.push([marked(img), probes.length, set]);
  });
  return { seen, fades, waits: timers.map(([, ms]) => ms) };
}
const tries = {
  comesBack: own(['error', 'tick', 'found', 'load']),
  second: own(['error', 'tick', 'missing', 'tick', 'found', 'load']),
  calm: own(['error', 'tick', 'found', 'load'], { calm: true }),
  early: own(['tick', 'found'], { early: true }),
  never: own(['error', 'tick', 'missing', 'tick', 'missing', 'tick']),
  failsAgain: own(['error', 'tick', 'found', 'error', 'tick', 'found', 'error', 'tick']),
  laterAgain: own(['error', 'tick', 'found', 'load', 'error', 'tick', 'missing', 'tick']),
  otherWaiting: own(['error', 'other', 'tick']),
  otherAsking: own(['error', 'tick', 'other', 'found']),
  afreshAsking: own(['error', 'tick', 'afresh', 'found']),
  gone: own(['error', 'gone', 'tick']),
  goneAsking: own(['error', 'tick', 'gone', 'found']),
  hidden: own(['error', 'hide', 'tick']),
  hiddenAsking: own(['error', 'tick', 'hide', 'found']),
  elsewhere: own(['error', 'tick'], { src: '/api/poster/7' }),
  next: own(['error', 'tick', 'error'], { next: '/api/poster/7' }),
  unfit: own(['load', 'tick'], { wide: true }),
  plain: own(['load']),
};
const waits = [1, 2, 3, 4].map(() => own(['error']).waits[0]);
process.stdout.write(JSON.stringify({ tries, waits, fits, retries, marks: Object.fromEntries(
  Object.entries(events).map(([k, [, img]]) => [k, marked(img)])
    .concat(Object.entries(before).map(([k, img]) => [k, marked(img)]))) }));
