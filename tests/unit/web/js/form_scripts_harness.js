// Runs the real show-password.js and submit-guard.js against a tiny fake DOM and
// prints the outcome of one scenario as JSON. Usage: node form_scripts_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = [];
    this.parentNode = null; this.attrs = {}; this.listeners = {};
    this.className = ''; this._text = ''; this.type = ''; this.form = null; this.dataset = {}; this.style = {}; this.title = '';
    this.selectionStart = 0; this.selectionEnd = 0;
    this.offsetWidth = 120; this.offsetHeight = 55;
    this.classList = { add: (c) => { this.className = c; }, remove: () => { this.className = ''; } };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  toggleAttribute(k, on) { if (on) this.attrs[k] = ''; else delete this.attrs[k]; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  get childNodes() { return this.children; }
  replaceChildren(...cs) { this._text = ''; this.children = cs; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c); c.parentNode = null;
    // a removed element loses focus, as in a browser
    if (this.doc.activeElement === c) this.doc.activeElement = this.doc.body;
    return c;
  }
  focus() { this.doc.activeElement = this; }
  setSelectionRange(from, to) { this.selectionStart = from; this.selectionEnd = to; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t, ev) { (this.listeners[t] || []).forEach((fn) => fn(Object.assign({ target: this }, ev))); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    if (sel === '[data-busy-label]') return this.all().filter((e) => e.hasAttribute('data-busy-label'));
    if (sel === '.pwd-eye') return this.all().filter((e) => e.className === 'pwd-eye');
    return [];
  }
  matches(sel) {
    if (sel === 'form[data-submit-guard]') return this.tagName === 'FORM' && this.hasAttribute('data-submit-guard');
    return false;
  }
}

function build() {
  const timers = [];
  const frames = [];
  const doc = {
    listeners: {},
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    fire(t, ev) { (this.listeners[t] || []).forEach((fn) => fn(ev)); },
    createElement(tag) { return new El(tag, doc); },
    querySelectorAll(sel) {
      const all = doc.body.all();
      if (sel === 'input[type="password"][data-show-password]') {
        return all.filter((e) => e.tagName === 'INPUT' && e.type === 'password' && e.hasAttribute('data-show-password'));
      }
      if (sel === 'form[data-submit-guard][data-busy]') {
        return all.filter((e) => e.tagName === 'FORM' && e.hasAttribute('data-submit-guard') && e.hasAttribute('data-busy'));
      }
      return [];
    },
  };
  doc.body = new El('body', doc);
  doc.documentElement = new El('html', doc);
  doc.activeElement = doc.body;
  const win = { listeners: {}, addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }, fire(t, ev) { (this.listeners[t] || []).forEach((fn) => fn(ev)); } };
  const ctx = {
    document: doc, window: win,
    WeakMap, Array, requestAnimationFrame: (fn) => { frames.push(fn); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms, done: false }); return timers.length; },
  };
  return { doc, win, ctx, timers, frames };
}

function load(ctx, file) {
  vm.runInNewContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx);
}

// As login.html draws it: the field and its eye in one box.
function field(doc, form, autofocus) {
  const box = doc.createElement('div');
  box.className = 'pwd-box';
  const input = doc.createElement('input');
  input.type = 'password';
  input.setAttribute('data-show-password', '');
  if (autofocus) input.setAttribute('autofocus', '');
  input.form = form;
  box.appendChild(input);
  const eye = doc.createElement('button');
  eye.className = 'pwd-eye';
  eye.setAttribute('aria-pressed', 'false');
  eye.setAttribute('aria-label', 'Show password');
  eye.title = 'Show';
  box.appendChild(eye);
  form.appendChild(box);
  return input;
}

const scenarios = {
  // the cursor stays in the field, where it was, while its eye is pressed
  focus() {
    const { doc, ctx } = build();
    const form = doc.createElement('form'); doc.body.appendChild(form);
    const a = field(doc, form, false);
    a.focus();
    a.selectionStart = 2; a.selectionEnd = 5;
    load(ctx, 'show-password.js');
    const eye = a.parentNode.children[1];
    let prevented = false;
    eye.fire('mousedown', { preventDefault() { prevented = true; } });
    // a browser puts the cursor at the end as the type changes
    let type = a.type;
    Object.defineProperty(a, 'type', { get: () => type, set: (t) => { type = t; a.selectionStart = a.selectionEnd = 9; } });
    eye.fire('click');
    return { prevented, active: doc.activeElement === a, selection: [a.selectionStart, a.selectionEnd], drawn: doc.documentElement.className };
  },
  autofocus() {
    const { doc, ctx } = build();
    const form = doc.createElement('form'); doc.body.appendChild(form);
    const a = field(doc, form, true);
    field(doc, form, false);
    a.focus();                                 // as the browser does for autofocus
    const box = a.parentNode;
    load(ctx, 'show-password.js');
    return { active: doc.activeElement === a, inPlace: a.parentNode === box };
  },
  toggle() {
    const { doc, ctx } = build();
    const form = doc.createElement('form'); doc.body.appendChild(form);
    const a = field(doc, form, false);
    load(ctx, 'show-password.js');
    const eye = a.parentNode.children[1];
    const now = () => ({ type: a.type, pressed: eye.getAttribute('aria-pressed'), name: eye.getAttribute('aria-label'), title: eye.title });
    const before = now();
    eye.fire('click');
    const shown = now();
    eye.fire('click');
    const hidden = now();
    eye.fire('click');
    form.fire('reset');
    return { before, shown, hidden, reset: now() };
  },
  guard() {
    const { doc, ctx, timers } = build();
    const form = doc.createElement('form');
    form.setAttribute('data-submit-guard', '');
    doc.body.appendChild(form);
    load(ctx, 'submit-guard.js');
    const submit = () => { const ev = { target: form, prevented: false, preventDefault() { this.prevented = true; } }; doc.fire('submit', ev); return ev.prevented; };
    const first = submit();
    const quick = submit();
    const busy = form.hasAttribute('data-busy');
    timers.forEach((t) => t.fn());             // the 10 s lapse
    const afterTimeout = form.hasAttribute('data-busy');
    submit();
    doc.fire('keydown', { key: 'Enter' });
    const afterEnter = form.hasAttribute('data-busy');
    doc.fire('keydown', { key: 'Escape' });
    const afterEscape = form.hasAttribute('data-busy');
    const ms = timers.map((t) => t.ms);
    ctx.window.fire('pageshow', { persisted: false });
    const afterFreshShow = form.hasAttribute('data-busy');
    ctx.window.fire('pageshow', { persisted: true });
    const afterRestore = form.hasAttribute('data-busy');
    return { first, quick, busy, afterTimeout, afterEnter, afterEscape, afterFreshShow, afterRestore, ms, aria: form.getAttribute('aria-busy') };
  },
  // a submit button that names its busy state shows it, and gets its own label back
  busyLabel() {
    const { doc, ctx, timers, win } = build();
    const form = doc.createElement('form');
    form.setAttribute('data-submit-guard', '');
    const btn = doc.createElement('button');
    btn.setAttribute('data-busy-label', 'Unsubscribing…');
    btn.appendChild(doc.createElement('span')).textContent = 'Unsubscribe';
    form.appendChild(btn);
    doc.body.appendChild(form);
    load(ctx, 'submit-guard.js');
    const submit = () => { const ev = { target: form, prevented: false, preventDefault() { this.prevented = true; } }; doc.fire('submit', ev); return ev.prevented; };
    submit();
    const whileBusy = btn.textContent;
    const second = submit();
    const stillBusy = btn.textContent;
    timers.forEach((t) => t.fn());
    return { whileBusy, second, stillBusy, afterLapse: btn.textContent, busyAfter: form.hasAttribute('data-busy') };
  },
  // a labelled button is drawn busy before its post goes, once, with the button that was pressed
  held() {
    const out = {};
    const start = () => {
      const { doc, ctx, timers, frames } = build();
      const form = doc.createElement('form');
      form.setAttribute('data-submit-guard', '');
      doc.body.appendChild(form);
      const b = form.appendChild(doc.createElement('button'));
      b.setAttribute('data-busy-label', 'Keeping…');
      const p = { form, b, timers, posts: [], valid: true,
        press(submitter) { let kept = true; doc.fire('submit', { target: form, submitter, preventDefault() { kept = false; } }); if (kept) p.posts.push(submitter === b ? 'b' : submitter); return kept; },
        frame() { frames.splice(0).forEach((fn) => fn()); p.run(0); },
        run(ms) { timers.filter((t) => t.ms === ms && !t.done).forEach((t) => { t.done = true; t.fn(); }); } };
      form.requestSubmit = (submitter) => { if (p.valid) p.press(submitter); };
      load(ctx, 'submit-guard.js');
      return p;
    };
    let p = start();
    p.press(p.b);
    out.before = p.posts.slice();
    p.press(p.b);
    p.frame();
    out.drawn = p.posts.slice();
    p.press(p.b);
    p.run(150);
    out.after = p.posts.slice();
    p.run(10000);
    p.press(p.b);
    p.frame();
    out.lapsed = p.posts.length;

    p = start();
    p.press(p.b);
    p.run(150);
    out.undrawn = p.posts.slice();
    p.frame();
    out.undrawn.push(...p.posts.slice(1));

    p = start();
    p.press(null);
    p.frame();
    out.noSubmitter = p.posts;

    p = start();
    p.valid = false;
    p.press(p.b);
    p.frame();
    out.invalid = { posts: p.posts.slice(), busy: p.form.hasAttribute('data-busy') };
    p.valid = true;
    p.press(p.b);
    p.frame();
    out.thenValid = p.posts;

    p = start();
    p.form.requestSubmit = undefined;
    p.press(p.b);
    out.old = p.posts;

    p = start();
    p.b.removeAttribute('data-busy-label');
    p.press(p.b);
    out.unlabelled = p.posts;
    return out;
  },
  // of three buttons the one pressed is the one that is busy
  pressed() {
    const { doc, ctx, win } = build();
    const form = doc.createElement('form');
    form.setAttribute('data-submit-guard', '');
    doc.body.appendChild(form);
    const buttons = ['7', '30', '90'].map((n) => {
      const b = form.appendChild(doc.createElement('button'));
      b.setAttribute('data-busy-label', 'Keeping…');
      b.appendChild(doc.createElement('span')).textContent = n;
      b.appendChild(doc.createElement('small')).textContent = ' days';
      return b;
    });
    load(ctx, 'submit-guard.js');
    const now = () => buttons.map((b) => [b.textContent, b.className, b.children[0].className, b.style.minWidth || '']);
    doc.fire('submit', { target: form, submitter: buttons[1], preventDefault() {} });
    const busy = now();
    win.fire('pageshow', { persisted: true });
    return { busy, after: now() };
  },
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
