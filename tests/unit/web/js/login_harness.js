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
      ['div', { class: 'login-sub' }, ['span', { class: 'sub-main' }, 'Sign in to your library'],
        ...(opts.out ? [['span', { class: 'sub-out', role: 'status', 'data-said': 'Signed out. See you soon.' }, ['svg', {}]]] : [])],
      ['div', { class: 'login-body' },
        ...msg,
        ['div', { class: 'login-msg is-wait', id: 'login-down', role: 'alert', hidden: '' }, ['svg', {}], ['span', {}, 'Could not reach mediaman.']],
        ['form', { class: 'login-form', method: 'post', 'data-submit-guard': '' },
          ['div', { class: 'login-field' }, ['div', { class: opts.refused ? 'login-box is-bad' : 'login-box' },
            ['div', { class: 'pwd-box' }, ['input', { id: 'password', type: 'password' }]]]],
          ['button', { class: 'login-btn', type: 'submit' }, ['span', { class: 'btn-label' }, 'Sign in'],
            ['span', { class: 'btn-busy', 'aria-hidden': 'true' }, 'Signing in…'],
            ['span', { class: 'btn-hold', 'aria-hidden': 'true' }, 'Opening your library…']],
          ['p', { class: 'sr-only', id: 'login-said', role: 'status' }]]]]]]));
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
const timers = [];
const frames = [];
// The next frame, and the timers that come due in *ms*.
const frame = () => frames.splice(0).forEach((fn) => fn());
const after = (ms) => timers.filter((t) => t.ms === ms).forEach((t) => t.fn());
const ctx = {
  document: doc, console, URLSearchParams, JSON, Date, Array,
  location: { search: url.search, pathname: url.pathname, hash: url.hash },
  history: { state: null, replaceState: (_s, _t, to) => { replaced.push(to); } },
  navigator: { onLine: !opts.offline },
  sessionStorage,
  getComputedStyle: (el) => ({ transform: transforms[el.id] }),
  setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; }, clearTimeout(id) { if (timers[id - 1]) timers[id - 1].fn = () => {}; },
  requestAnimationFrame(fn) { frames.push(fn); },
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
const form = doc.querySelector('.login-form');
const holding = () => ({ hold: form.hasAttribute('data-hold'), said: doc.getElementById('login-said').textContent });
if (scenario === 'load') {
  const said = doc.querySelector('.sub-out');
  out.out = said && [said.textContent];
  frame();
  if (said) out.out.push(said.textContent);
}
if (scenario === 'offlineTwice') {
  submit();
  frame();
  const line = () => doc.querySelector('#login-down span');
  const first = line();
  first.mark = 'first';
  doc.activeElement = doc.body;
  submit();
  out.sameBeforeFrame = line() === first;
  frame();
  out.again = line() !== first && line().textContent === first.textContent && !doc.getElementById('login-down').hidden;
  out.focused = doc.activeElement.id;
}
if (scenario === 'hold') {
  submit();
  out.atOnce = holding();
  if (opts.released) form.removeAttribute('data-busy');
  after(1200);
  out.late = holding();
}
if (scenario === 'submit') {
  out.sent = submit();
  out.flag = store.has('mm.arrive') ? JSON.parse(store.get('mm.arrive')) : null;
  out.room = room();
  out.down = doc.getElementById('login-down').hidden;
  out.refusedShown = refused ? !refused.hidden : null;
  out.bad = doc.querySelector('.login-box').classList.contains('is-bad');
}
process.stdout.write(JSON.stringify(out));
