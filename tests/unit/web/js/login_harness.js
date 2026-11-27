'use strict';
/* Runs the sign-in page's login.js (with the real submit-guard.js) against a fake DOM built as
   login.html draws it. Usage: node login_harness.js <scenario> <json options>; prints JSON. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const scenario = process.argv[2];
const opts = JSON.parse(process.argv[3] || '{}');

const doc = makeDocument();
const msg = opts.refused
  ? [['div', { class: 'login-msg', role: 'alert' }, ['span', {}, opts.refused]]]
  : [];
doc.body.appendChild(build(doc, ['main', {},
  ['div', { class: 'login-page login-page--signin' },
    ['div', { class: 'mm-sky', 'aria-hidden': 'true' }, ['i', { id: 'sky-a' }], ['i', { id: 'sky-b' }]],
    ['div', { class: 'login-card' },
      ['div', { class: 'login-body' },
        ...msg,
        ['div', { class: 'login-msg is-wait', id: 'login-down', role: 'alert', hidden: '' }],
        ['form', { class: 'login-form', method: 'post', 'data-submit-guard': '' },
          ['div', { class: 'login-field' }, ['div', { class: opts.refused ? 'login-box is-bad' : 'login-box' },
            ['div', { class: 'pwd-box' }, ['input', { id: 'password', type: 'password' }]]]],
          ['button', { class: 'login-btn', type: 'submit' }, 'Sign in']]]]]]));
doc.getElementById('login-down').hidden = true;
const refused = doc.querySelector('.login-msg:not(#login-down)');
if (refused) refused.hidden = false;

// Layout as the browser would give it: a shown line pushes the form down and the centred card up.
const lines = () => doc.querySelectorAll('.login-msg').filter((m) => !m.hidden).length;
doc.querySelector('.login-card').getBoundingClientRect = () => ({ top: 300 - 25 * lines() });
doc.querySelector('.login-form').getBoundingClientRect = () => ({ top: 400 + 25 * lines() });

const store = new Map(Object.entries(opts.stored || {}));
const sessionStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
const url = new URL(opts.href || 'https://mm.test/login');
const replaced = [];
const transforms = { 'sky-a': 'matrix(1.02, 0, 0, 1.02, 12, 8)', 'sky-b': 'none' };
const ctx = {
  document: doc, console, URLSearchParams, JSON, Date, Array,
  location: { search: url.search, pathname: url.pathname, hash: url.hash },
  history: { state: null, replaceState: (_s, _t, to) => { replaced.push(to); } },
  navigator: { onLine: !opts.offline },
  sessionStorage,
  getComputedStyle: (el) => ({ transform: transforms[el.id] }),
  setTimeout() { return 0; }, clearTimeout() {}, requestAnimationFrame() {},
  addEventListener() {},
};
ctx.window = ctx;
vm.createContext(ctx);
['submit-guard.js', 'login.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));

// A submit event from the form, bubbling to the document unless stopped.
function submit() {
  const form = doc.querySelector('.login-form');
  let stopped = false;
  const e = {
    target: form, submitter: form.querySelector('.login-btn'), defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { stopped = true; },
  };
  (form.listeners.submit || []).forEach((fn) => fn(e));
  if (!stopped) (doc.listeners.submit || []).forEach((fn) => fn(e));
  return { posted: !e.defaultPrevented, busy: form.hasAttribute('data-busy') };
}

const page = doc.querySelector('.login-page');
const room = () => ({ on: page.classList.contains('is-room'), dc: page.style['--dc'], df: page.style['--df'] });
const out = { stored: Object.fromEntries(store), replaced, room: room() };
if (scenario === 'submit') {
  out.sent = submit();
  out.flag = store.has('mm.arrive') ? JSON.parse(store.get('mm.arrive')) : null;
  out.room = room();
  out.down = doc.getElementById('login-down').hidden;
  out.refusedShown = refused ? !refused.hidden : null;
  out.bad = doc.querySelector('.login-box').classList.contains('is-bad');
}
process.stdout.write(JSON.stringify(out));
