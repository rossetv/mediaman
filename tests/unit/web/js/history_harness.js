// Runs the real history.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node history_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

process.env.TZ = 'Asia/Kolkata';

const { makeDocument, build, click } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function run(scenario) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  const reduced = scenario.endsWith('reduced');
  const timers = [];
  const toasts = [];
  const copied = [];
  const win = {
    location: { href: 'http://mm.test/history?page=2&event=5' },
    history: {
      replaceState: (_s, _t, url) => { win.location.href = 'http://mm.test' + url; },
    },
    matchMedia: () => ({ matches: reduced }),
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    requestAnimationFrame: (fn) => timers.push(fn),
    getComputedStyle: () => ({ animationDuration: '0.2s', animationTimingFunction: 'ease' }),
    isSecureContext: true,
    UIFeedback: { toast: (m) => toasts.push(m), error: (m) => toasts.push('error: ' + m) },
  };
  win.window = win;
  const ctx = {
    window: win, document: doc, URL,
    navigator: { clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
  };
  vm.createContext(ctx);

  const row = (id, open, at) => build(doc, ['details', { class: 'hist', 'data-event': id, ...(open ? { open: '' } : {}) },
    ['summary', { class: 'hist-sum' }, ['time', { 'data-hist-at': 'line', datetime: at }, 'stored UTC'], ['span', {}, 'title']],
    ['div', { class: 'hist-body' },
      ['dl', {}, ['div', { class: 'hist-field' }, ['dt', {}, 'Time'], ['dd', {}, ['time', { 'data-hist-at': 'full', datetime: at }, '2026-11-24 20:15:03 UTC']]],
        ['div', { class: 'hist-field' }, ['dt', {}, 'Who'], ['dd', {}, 'admin']],
        ['div', { class: 'hist-field' }, ['dt', {}, 'IP address'], ['dd', {}, '1.2.3.4']]],
      ['button', { class: 'btn', 'data-hist-copy': '' }, 'Copy details']]]);
  const list = build(doc, ['div', { class: 'card--hist' }]);
  const rows = [row('5', true, '2026-11-24T20:15:03Z'), row('6', false, 'soon')];
  rows.forEach((r) => {
    Object.defineProperty(r, 'open', { get: () => r.hasAttribute('open') });
    Object.defineProperty(r, 'dataset', { get: () => ({ event: r.getAttribute('data-event') }) });
    list.appendChild(r);
  });
  doc.body.appendChild(list);
  const after = build(doc, ['div', { class: 'pagination' }]);
  doc.body.appendChild(after);
  const moves = [];
  const watch = (name, el) => {
    el.animate = (frames, opts) => {
      const move = { name, from: frames.transform[0], to: frames.transform[1], ms: opts.duration, cancelled: false };
      moves.push(move);
      return { cancel: () => { move.cancelled = true; } };
    };
  };
  watch('first', rows[0]);
  watch('second', rows[1]);
  watch('after', after);
  rows.forEach((r) => {
    const body = r.querySelector('.hist-body');
    body.animate = () => {};
    Object.defineProperty(body, 'offsetHeight', { get: () => 300 });
  });
  vm.runInContext(fs.readFileSync(path.join(JS, 'history.js'), 'utf8'), ctx);

  const sum = (r) => r.querySelector('.hist-sum');
  const state = (r) => ({ open: r.open, closing: r.classList.contains('is-closing') });
  const out = {};

  if (scenario === 'close' || scenario === 'reduced') {
    click(doc, sum(rows[0]));
    out.afterClick = state(rows[0]);
    out.url = win.location.href;
    out.moving = moves.map((m) => ({ ...m }));
    timers.forEach((t) => t());
    out.moves = moves;
    out.settled = state(rows[0]);
    out.urlAfter = win.location.href;
  }
  if (scenario === 'open' || scenario === 'open-reduced') {
    out.prevented = clickPrevented(doc, sum(rows[1]));
    rows[1].setAttribute('open', '');
    (list.listeners.toggle || []).forEach((fn) => fn({ target: rows[1] }));
    timers.forEach((t) => t());
    out.moves = moves;
    out.url = win.location.href;
  }
  if (scenario === 'times') {
    out.times = rows.map((r) => r.querySelectorAll('time').map((t) => t.textContent));
  }
  if (scenario === 'copy') {
    click(doc, rows[0].querySelector('[data-hist-copy]'));
    return new Promise((r) => setImmediate(r)).then(() => ({ copied, toasts, stillOpen: rows[0].open }));
  }
  return Promise.resolve(out);
}

function clickPrevented(doc, target) {
  let prevented = false;
  const e = { target, button: 0, preventDefault() { prevented = true; } };
  for (let a = target; a; a = a.parentNode) (a.listeners.click || []).forEach((fn) => fn(e));
  return prevented;
}

run(process.argv[2]).then((o) => console.log(JSON.stringify(o)));
