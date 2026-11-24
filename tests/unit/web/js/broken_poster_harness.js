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
vm.runInNewContext(fs.readFileSync(SRC, 'utf8'), {
  document: {
    addEventListener: (t, fn) => { listeners[t] = fn; },
    querySelectorAll: (sel) => Object.values(before).filter((i) => i.matches(sel)),
  },
});
Object.values(events).forEach(([type, img]) => listeners[type] && listeners[type]({ target: img }));
const marked = (img) => 'broken' in img.dataset;
process.stdout.write(JSON.stringify(Object.fromEntries(
  Object.entries(events).map(([k, [, img]]) => [k, marked(img)])
    .concat(Object.entries(before).map(([k, img]) => [k, marked(img)])))));
