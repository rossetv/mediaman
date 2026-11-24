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
function run(pictures) {
  vm.runInNewContext(fs.readFileSync(SRC, 'utf8'), {
    document: {
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
process.stdout.write(JSON.stringify({ fits, retries, marks: Object.fromEntries(
  Object.entries(events).map(([k, [, img]]) => [k, marked(img)])
    .concat(Object.entries(before).map(([k, img]) => [k, marked(img)]))) }));
