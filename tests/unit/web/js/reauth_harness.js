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
  focus() { doc.activeElement = this; }
  blur() { if (doc.activeElement === this) doc.activeElement = doc.body; }
  select() {}
  remove() {}
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelector(sel) { return this.all().find((e) => e.className.split(/\s+/).includes(sel.slice(1))) || null; }
}

const timers = [];
const closers = [];
const doc = {
  body: new El('body'),
  documentElement: new El('html'),
  createElement: (t) => new El(t),
  querySelectorAll: () => [],
  activeElement: null,
};
const ctx = {
  document: doc, Promise, TypeError,
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].fn = () => {}; },
  MM: { api: { APIError: Error }, modal: { setupDetail: (_el, o) => { closers.push(o.onClose); return { open() {}, close() { o.onClose(); } }; } } },
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
  // the width of the page's scrollbar is held while the dialog is open
  async bar() {
    const root = doc.documentElement;
    Object.assign(root, { clientWidth: 1265, style: { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } } });
    ctx.innerWidth = 1280;
    open({ ok: true });
    const held = root.style['--page-bar'];
    doc.body.children[doc.body.children.length - 1].children[0].querySelector('.btn--ghost').emit('click');
    return { held, after: root.style['--page-bar'] || null };
  },
  // opened with the keyboard on nothing, as after a click in Safari: where it is once the dialog has closed
  async letGo() {
    doc.activeElement = doc.body;
    const input = open({ ok: true }).sheet.querySelector('.inp');
    timers.forEach((t) => t.fn());
    const had = doc.activeElement === input;
    closers[closers.length - 1]();
    return { had, kept: doc.activeElement === input };
  },
  // the text of what names the dialog and of what describes it
  async named() {
    const { sheet } = open({ ok: true });
    const text = (attr) => (sheet.all().find((e) => e.id && e.id === sheet.attrs[attr]) || {}).textContent || null;
    return { name: text('aria-labelledby'), about: text('aria-describedby') };
  },
  // what the dialog of run() says, with the caller's words and without
  async words() {
    const says = async (words) => {
      ctx.MM.reauth.run(() => Promise.reject({ name: 'APIError', status: 403, data: { reauth_required: true } }), words).catch(() => {});
      await flush();
      const sheet = doc.body.children[doc.body.children.length - 1].children[0];
      return ['.reauth-title', '.reauth-desc', '.btn--primary'].map((c) => sheet.querySelector(c).textContent);
    };
    return {
      own: await says({ title: 'T', descText: 'D', confirmLabel: 'C' }),
      none: await says(),
    };
  },
  // Cancel hands focus back to the control that asked for the password
  async focusReturn() {
    const asker = new El('button');
    doc.activeElement = asker;
    open({ ok: true });
    doc.activeElement = doc.body.children[doc.body.children.length - 1];
    const cancel = doc.activeElement.children[0].querySelector('.btn--ghost');
    cancel.emit('click');
    return { restored: doc.activeElement === asker };
  },
  // a refusal and an empty password both fade the message in
  async error() {
    const wrong = open({ ok: false, error: 'Password is incorrect' });
    const before = wrong.shown();
    await wrong.submit('wrong');
    const failed = wrong.shown();
    await wrong.submit('');
    return { before, failed, required: wrong.shown() };
  },
  // Enter pressed again while the password is being checked, and as the dialog goes: how often each request went
  async again() {
    const asked = [];
    ctx.MM.api.post = (url) => new Promise((resolve) => { asked.push(resolve); });
    let runs = 0;
    const ran = ctx.MM.reauth.run(() => {
      runs += 1;
      return runs === 1 ? Promise.reject({ name: 'APIError', status: 403, data: { reauth_required: true } }) : Promise.resolve('done');
    });
    await flush();
    const sheet = doc.body.children[doc.body.children.length - 1].children[0];
    const input = sheet.querySelector('.inp');
    input.value = 'pw';
    const press = () => input.emit('keydown', { key: 'Enter' });
    press();
    press();
    press();
    asked.forEach((resolve) => resolve({}));
    await ran;
    await flush();
    input.value = 'pw';
    press();
    return { passwords: asked.length, runs };
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
