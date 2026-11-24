// Runs watch-rows.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node watch_rows_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument, build } = require('./fake_dom');

const PAD = 20;
const GAP = 8;

/* A line of season pills of these widths in a view *view* wide, which snaps to the nearest pill's
   start as a touch screen's does. */
function line(widths, view) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['div', { class: 'watch-row__viewport' },
    ['nav', { class: 'watch-row__scroll' }, ...widths.map((w, i) => ['a', { class: 'watch-pill' }, 'Season ' + (i + 1)])]]]));
  const scroller = doc.querySelector('.watch-row__scroll');
  const pills = doc.querySelectorAll('.watch-pill');
  let at = PAD;
  pills.forEach((p, i) => {
    Object.defineProperty(p, 'offsetLeft', { value: at });
    Object.defineProperty(p, 'offsetWidth', { value: widths[i] });
    at += widths[i] + GAP;
  });
  scroller.clientWidth = view;
  scroller.scrollWidth = at - GAP + PAD;
  scroller.scrollLeft = 0;
  const max = scroller.scrollWidth - view;
  const stops = [...pills.map((p) => Math.min(max, p.offsetLeft - PAD)), max];
  scroller.scrollTo = (o) => {
    scroller.scrollLeft = stops.reduce((best, s) => (Math.abs(s - o.left) < Math.abs(best - o.left) ? s : best));
  };
  const ctx = {
    document: doc, Array, Math, WeakMap, WeakSet, parseFloat,
    getComputedStyle: () => ({ paddingLeft: PAD + 'px', paddingRight: PAD + 'px' }),
    matchMedia: () => ({ matches: true }),
    requestAnimationFrame: (fn) => fn(),
    MutationObserver: class { observe() {} disconnect() {} },
    addEventListener() {},
    MM: { dom: { icon: () => doc.createElement('svg') } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'watch-rows.js'), 'utf8'), ctx);
  return {
    max,
    at: () => scroller.scrollLeft,
    page(to) { doc.querySelector('.watch-row__arrow--' + to).listeners.click.forEach((fn) => fn()); },
    whole: () => pills.map((p, i) => [i + 1, p.offsetLeft - scroller.scrollLeft])
      .filter(([n, left]) => left >= PAD && left + widths[n - 1] <= view - PAD).map(([n]) => n),
  };
}

/* The seasons in full view at each stop, to the end and back. */
function thereAndBack(widths, view) {
  const l = line(widths, view);
  const there = [l.whole()];
  for (let i = 0; i < 20 && l.at() < l.max; i++) { l.page('next'); there.push(l.whole()); }
  const back = [l.whole()];
  for (let i = 0; i < 20 && l.at() > 0; i++) { l.page('prev'); back.push(l.whole()); }
  return { there, back };
}

const scenarios = {
  seasons: () => ({
    phone: thereAndBack([92, 94, 94, 94, 94, 94, 94, 94, 94, 102], 320),
    wide: thereAndBack([94, 94, 94, 400, 94, 94], 320),
  }),
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
