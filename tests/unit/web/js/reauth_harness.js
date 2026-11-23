// Runs the real core/reauth.js against a tiny fake DOM and prints the outcome of
// one scenario as JSON. Usage: node reauth_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attrs = {}; this.style = {}; this.listeners = {}; this.className = ''; this._text = ''; this.value = '';
  }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(' '); },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  emit(t, extra) { (this.listeners[t] || []).forEach((f) => f({ target: this, ...extra })); }
  focus() {}
  select() {}
  remove() {}
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) { return this.all().find((e) => e.className.split(/\s+/).includes(sel.slice(1))) || null; }
}

const timers = [];
const doc = {
  body: new El('body'),
  createElement: (t) => new El(t),
  querySelectorAll: () => [],
};
const ctx = {
  document: doc, Promise, TypeError,
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = () => {}; },
  MM: { modal: { setupDetail: () => ({ open() {}, close() {} }) } },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(JS, 'core/reauth.js'), 'utf8'), ctx);

const flush = () => new Promise((r) => setImmediate(r));

function open(result) {
  ctx.MM.reauth.openModal({ onSubmit: () => Promise.resolve(result) });
  const sheet = doc.body.children[doc.body.children.length - 1].children[0];
  const msg = sheet.querySelector('.reauth-msg');
  const input = sheet.querySelector('.inp');
  const submit = async (pw) => { input.value = pw; input.emit('keydown', { key: 'Enter' }); await flush(); };
  const shown = () => ({ text: msg.textContent, on: msg.classList.contains('is-shown') });
  return { sheet, msg, submit, shown };
}

const scenarios = {
  // a refusal and an empty password both fade the message in
  async error() {
    const wrong = open({ ok: false, error: 'Password is incorrect' });
    const before = wrong.shown();
    await wrong.submit('wrong');
    const failed = wrong.shown();
    await wrong.submit('');
    return { before, failed, required: wrong.shown() };
  },
  // the next try fades the message out first and empties it only once it has gone
  async retry() {
    const d = open({ ok: false, error: 'Password is incorrect' });
    await d.submit('wrong');
    timers.length = 0;
    await d.submit('again');
    const failedAgain = d.shown();
    const slow = open(new Promise(() => {}));
    slow.msg.textContent = 'Password is incorrect';
    slow.msg.classList.add('is-shown');
    await slow.submit('x');
    const fading = slow.shown();
    timers.forEach((t) => t.fn());
    return { failedAgain, fading, emptied: slow.shown() };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
