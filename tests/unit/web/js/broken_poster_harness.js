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
let frames = [];
function run(pictures) {
  vm.runInNewContext(fs.readFileSync(SRC, 'utf8'), {
    setTimeout: (fn, ms) => { timers.push([fn, ms]); },
    Image: function Image() { probes.push(this); },
    matchMedia: () => ({ matches: reduced }),
    requestAnimationFrame: (fn) => { frames.push(fn); },
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
  const faded = [];
  img.animate = () => { fades += 1; const a = {}; faded.push(a); return a; };
  img.isConnected = true;
  img.hidden = false;
  if (next) img.dataset.nextSrc = next;
  img.parentNode = { querySelectorAll: () => [] };
  run(early ? [img] : []);
  const waits = timers.map(([, ms]) => ms);
  const seen = [];
  steps.forEach((step) => {
    const before = timers.slice();
    if (step === 'tick') { const due = timers; timers = []; due.forEach(([fn]) => fn()); }
    else if (step === 'found') probes.length && probes[probes.length - 1].onload();
    else if (step === 'missing') probes.length && probes[probes.length - 1].onerror();
    else if (step === 'other') { at = '/api/watch/img/tmdb/w342/b.jpg'; delete img.dataset.broken; }
    else if (step === 'afresh') delete img.dataset.broken;
    else if (step === 'frame') { const due = frames; frames = []; due.forEach((fn) => fn()); }
    else if (step === 'faded') faded[faded.length - 1].onfinish();
    else if (step === 'gone') img.isConnected = false;
    else if (step === 'hide') img.hidden = true;
    else {
      [img.naturalWidth, img.naturalHeight] = step === 'load' ? [400, 600] : [0, 0];
      listeners[step]({ target: img });
    }
    waits.push(...timers.filter((t) => !before.includes(t)).map(([, ms]) => ms));
    seen.push([img.dataset.broken === undefined ? false : img.dataset.broken || true, probes.length, set]);
  });
  return { seen, fades, waits };
}
const tries = {
  comesBack: own(['error', 'tick', 'found', 'load', 'faded']),
  second: own(['error', 'tick', 'missing', 'tick', 'found', 'load', 'faded']),
  calm: own(['error', 'tick', 'found', 'load', 'frame'], { calm: true }),
  early: own(['tick', 'found'], { early: true }),
  never: own(['error', 'tick', 'missing', 'tick', 'missing', 'tick']),
  failsAgain: own(['error', 'tick', 'found', 'error', 'tick', 'found', 'error', 'tick']),
  laterAgain: own(['error', 'tick', 'found', 'load', 'faded', 'error', 'tick', 'missing', 'tick']),
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
  sameAgain: own(['error', 'afresh', 'error', 'tick', 'missing', 'tick', 'missing', 'tick']),
  sameAgainAsking: own(['error', 'tick', 'afresh', 'error', 'tick', 'missing', 'tick', 'missing', 'tick']),
  plain: own(['load']),
};
/* Many that failed together and are all missing: the waits before each is asked again, and how many are
   asked at once and within a second, on a clock of its own. */
function crowd(n, answerMs, inView = []) {
  let now = 0;
  let asking = 0;
  let most = 0;
  const asked = [];
  probes = [];
  timers = [];
  const push = (fn, ms) => timers.push([fn, now + ms]);
  const ctx = {
    setTimeout: push,
    Image: function Image() {
      asked.push(now);
      probes.push(this);
      asking += 1;
      most = Math.max(most, asking);
      push(() => { asking -= 1; this.onerror(); }, answerMs);
    },
    innerWidth: 800,
    innerHeight: 600,
    document: { addEventListener: (t, fn) => { listeners[t] = fn; }, querySelectorAll: () => [] },
  };
  vm.runInNewContext(fs.readFileSync(SRC, 'utf8'), ctx);
  for (let i = 0; i < n; i += 1) {
    const img = image(['data-broken-on-error'], 0, 0);
    const top = inView.includes(i) ? 100 : 5000 + i * 300;
    img.parentNode = { getBoundingClientRect: () => ({ top, bottom: top + 240, left: 0, right: 160 }) };
    img.getAttribute = () => '/api/watch/img/tmdb/w342/' + i + '.jpg';
    img.isConnected = true;
    listeners.error({ target: img });
  }
  while (timers.length) {
    timers.sort((a, b) => a[1] - b[1]);
    const [fn, at] = timers.shift();
    now = at;
    fn();
  }
  const inSecond = Math.max(...asked.map((t) => asked.filter((u) => u >= t && u < t + 1000).length));
  const order = probes.map((p) => Number(/(\d+)\.jpg/.exec(p.src)[1]));
  return { total: asked.length, most, inSecond, firstAsked: order.slice(0, 7) };
}
const crowds = { quick: crowd(100, 2), slow: crowd(100, 5000) };
const seenFirst = crowd(40, 5000, [37, 39]).firstAsked;
process.stdout.write(JSON.stringify({ tries, crowds, seenFirst, fits, retries, marks: Object.fromEntries(
  Object.entries(events).map(([k, [, img]]) => [k, marked(img)])
    .concat(Object.entries(before).map(([k, img]) => [k, marked(img)]))) }));
