// Runs the Watch page's words for the time picks are made (core, making) against the fake DOM and
// prints the outcome of every scenario as JSON. Usage: node watch_making_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const { makeDocument } = require('./fake_dom');

function load(opts = {}) {
  const doc = makeDocument();
  doc.hidden = !!opts.hidden;
  doc.removeEventListener = (t, fn) => { doc.listeners[t] = doc.listeners[t].filter((f) => f !== fn); };
  const clock = { now: 1000000, beats: new Map(), timers: [], made: 0 };
  let seed = 7;
  const math = Object.create(Math);
  math.random = opts.random || (() => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; });
  const ctx = {
    document: doc, Math: math, Date: { now: () => clock.now },
    setInterval: (fn, ms) => { clock.made += 1; clock.beats.set(clock.made, { fn, ms }); return clock.made; },
    clearInterval: (id) => { clock.beats.delete(id); },
    setTimeout: (fn, ms) => { clock.timers.push({ fn, ms }); },
    clearTimeout() {},
    matchMedia: () => ({ matches: !!opts.still }),
    addEventListener() {},
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/dom.js', 'watch/core.js', 'watch/making.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const p = { doc, clock, making: ctx.MMWatch.making };
  /* One change of the words, *ms* later. */
  p.beat = () => { const b = [...clock.beats.values()][0]; clock.now += b.ms; b.fn(); };
  p.show = (hidden) => { doc.hidden = hidden; (doc.listeners.visibilitychange || []).slice().forEach((fn) => fn()); };
  return p;
}

function words(run) {
  const lines = run.node.children;
  return lines[lines.length - 1].textContent;
}

function through(p, run, n) {
  const out = [words(run)];
  while (out.length < n) { p.beat(); out.push(words(run)); }
  return out;
}

function begin(p, opts) { const run = p.making.start(opts || {}); p.doc.body.appendChild(run.node); return run; }

const scenarios = {
  sets() { const { sets } = load().making; return sets; },

  // Every line of a run, to well past the end of the set.
  run() {
    const p = load();
    const run = begin(p);
    return through(p, run, p.making.sets.lines.length + 40);
  },

  // Chance itself gives the same order to both.
  sameChance() {
    const p = load({ random: () => 0.3 });
    return [0, 1].map(() => { const run = begin(p); const out = through(p, run, 12); run.stop(); return out; });
  },

  few() {
    const p = load();
    return through(p, begin(p, { few: true }), 40);
  },

  hiddenTab() {
    const p = load();
    const run = begin(p);
    const out = { before: p.clock.beats.size, first: words(run) };
    p.show(true);
    out.hidden = p.clock.beats.size;
    p.clock.now += 60000;
    p.show(false);
    out.back = p.clock.beats.size;
    out.same = words(run);
    run.stop();
    p.show(true);
    p.show(false);
    out.stopped = p.clock.beats.size;
    const behind = load({ hidden: true });
    begin(behind);
    out.startedHidden = behind.clock.beats.size;
    return out;
  },

  // The line before is there to fade while motion is asked for, and not otherwise.
  motion() {
    return [false, true].map((still) => {
      const p = load({ still });
      const run = begin(p);
      const ms = [...p.clock.beats.values()][0].ms;
      p.beat();
      const during = run.node.children.length;
      p.clock.timers.splice(0).forEach((t) => t.fn());
      return { ms, during, after: run.node.children.length };
    });
  },

  // The end: said once, then handed on; stopped, nothing is.
  end() {
    const p = load();
    const run = begin(p);
    let over = 0;
    run.end('Here they are.', () => { over += 1; });
    const out = { words: words(run), beats: p.clock.beats.size, ending: run.ending(), early: over };
    p.clock.timers.splice(0).forEach((t) => t.fn());
    out.over = over;
    const cut = begin(p);
    cut.end('Here they are.', () => { over += 1; });
    cut.stop();
    p.clock.timers.splice(0).forEach((t) => t.fn());
    out.cut = over;
    return out;
  },

  hiddenFromReaders() {
    const p = load();
    const run = begin(p);
    return [run.node.getAttribute('aria-hidden'), run.node.hasAttribute('aria-live') || run.node.hasAttribute('role'),
      p.making.reel().getAttribute('aria-hidden')];
  },
};

const out = {};
Object.keys(scenarios).forEach((name) => { out[name] = scenarios[name](); });
process.stdout.write(JSON.stringify(out));
