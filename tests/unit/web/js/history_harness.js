// Runs the real history.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node history_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function run(scenario) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  const reduced = scenario === 'reduced';
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
    isSecureContext: true,
    UIFeedback: { toast: (m) => toasts.push(m), error: (m) => toasts.push('error: ' + m) },
  };
  win.window = win;
  const ctx = {
    window: win, document: doc, URL,
    navigator: { clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
  };
  vm.createContext(ctx);

  const row = (id, open) => build(doc, ['details', { class: 'hist', 'data-event': id, ...(open ? { open: '' } : {}) },
    ['summary', { class: 'hist-sum' }, ['span', {}, 'title']],
    ['div', { class: 'hist-body' },
      ['dl', {}, ['div', { class: 'hist-field' }, ['dt', {}, 'Who'], ['dd', {}, 'admin']],
        ['div', { class: 'hist-field' }, ['dt', {}, 'IP address'], ['dd', {}, '1.2.3.4']]],
      ['button', { class: 'btn', 'data-hist-copy': '' }, 'Copy details']]]);
  const list = build(doc, ['div', { class: 'card--hist' }]);
  const rows = [row('5', true), row('6', false)];
  rows.forEach((r) => {
    Object.defineProperty(r, 'open', { get: () => r.hasAttribute('open') });
    Object.defineProperty(r, 'dataset', { get: () => ({ event: r.getAttribute('data-event') }) });
    list.appendChild(r);
  });
  doc.body.appendChild(list);
  vm.runInContext(fs.readFileSync(path.join(JS, 'history.js'), 'utf8'), ctx);

  const sum = (r) => r.querySelector('.hist-sum');
  const state = (r) => ({ open: r.open, closing: r.classList.contains('is-closing') });
  const out = {};

  if (scenario === 'close' || scenario === 'reduced') {
    click(doc, sum(rows[0]));
    out.afterClick = state(rows[0]);
    out.url = win.location.href;
    timers.forEach((t) => t());
    out.settled = state(rows[0]);
    out.urlAfter = win.location.href;
  }
  if (scenario === 'open') {
    out.prevented = clickPrevented(doc, sum(rows[1]));
    rows[1].setAttribute('open', '');
    (list.listeners.toggle || []).forEach((fn) => fn({ target: rows[1] }));
    out.url = win.location.href;
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
