// Runs the real watch/arrive.js with what the sign-in page left in sessionStorage, and prints
// what the page is marked with before its first paint, and once the arrival has played.
// Usage: node watch_arrive_harness.js '<stored value, or null>'
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const NOW = 5000000;

const stored = JSON.parse(process.argv[2]);
const store = stored === null ? {} : { 'mm.arrive': stored };
const classes = new Set();
const props = {};
const ready = [];
const timers = [];
const html = {
  classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) },
  style: { setProperty: (k, v) => { props[k] = v; } },
};
const window = {
  sessionStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    removeItem: (k) => { delete store[k]; },
  },
  setTimeout: (fn, ms) => timers.push({ fn, ms }),
};
const document = {
  documentElement: html,
  addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') ready.push(fn); },
};
const ctx = vm.createContext({ window, document, Date: { now: () => NOW }, JSON, Array });
vm.runInContext(fs.readFileSync(path.join(JS, 'watch/arrive.js'), 'utf8'), ctx);

const out = { painted: [...classes], props: { ...props }, left: Object.keys(store) };
ready.forEach((fn) => fn());
out.playedMs = timers.map((t) => t.ms);
timers.forEach((t) => t.fn());
out.after = [...classes];
process.stdout.write(JSON.stringify(out));
