// Runs the real settings page scripts against a small fake DOM and prints the
// outcome of one scenario as JSON. Usage: node settings_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

// One compound selector: tag, #id, .class, [attr], [attr="value"].
function matchesCompound(el, sel) {
  const re = /^([a-zA-Z][\w-]*)?|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/g;
  let rest = sel;
  const tag = rest.match(/^[a-zA-Z][\w-]*/);
  if (tag) { if (el.tagName !== tag[0].toUpperCase()) return false; rest = rest.slice(tag[0].length); }
  let m;
  const part = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/y;
  while (rest) {
    part.lastIndex = 0;
    m = part.exec(rest);
    if (!m) return false;
    if (m[1] && el.id !== m[1]) return false;
    if (m[2] && !el.classList.contains(m[2])) return false;
    if (m[3]) {
      const v = el.getAttribute(m[3]);
      const want = m[4] !== undefined ? m[4] : m[5];
      if (v === null || (want !== undefined && v !== want)) return false;
    }
    rest = rest.slice(m[0].length);
  }
  void re;
  return true;
}
function matches(el, selector) {
  return selector.split(',').some((one) => {
    const parts = one.trim().split(/\s+/);
    if (!matchesCompound(el, parts[parts.length - 1])) return false;
    let anc = el.parentNode;
    for (let i = parts.length - 2; i >= 0; i--) {
      while (anc && !matchesCompound(anc, parts[i])) anc = anc.parentNode;
      if (!anc) return false;
      anc = anc.parentNode;
    }
    return true;
  });
}

class El {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.doc = doc; this.children = []; this.parentNode = null;
    this.attrs = {}; this.style = {}; this.listeners = {}; this.dataset = {};
    this.className = ''; this._text = ''; this.id = ''; this.hidden = false; this.disabled = false;
    this.value = ''; this.defaultValue = ''; this.checked = false; this.type = '';
    this.offsetHeight = 40;
  }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); },
      remove: (...cs) => { el.className = list().filter((x) => !cs.includes(x)).join(' '); },
      toggle: (c, on) => (on ? el.classList.add(c) : el.classList.remove(c)),
    };
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === 'id') this.id = String(v);
  }
  getAttribute(k) {
    if (k === 'id') return this.id || null;
    if (k.startsWith('data-')) {
      const key = k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());
      if (key in this.dataset) return String(this.dataset[key]);
    }
    return k in this.attrs ? this.attrs[k] : null;
  }
  removeAttribute(k) { delete this.attrs[k]; }
  set textContent(t) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  replaceChildren(...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); }
  appendChild(c) { return this.insertBefore(c, null); }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.children = c.parentNode.children.filter((x) => x !== c);
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(n) { const p = this.parentNode; p.insertBefore(n, this); this.remove(); }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  get nextElementSibling() { return this.nextSibling; }
  get previousElementSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) - 1] || null : null; }
  contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n.tagName && matches(n, sel)) return n; return null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatchEvent(e) {
    for (let n = this; n; n = n.parentNode) (n.listeners[e.type] || []).forEach((f) => f(e));
    (this.doc.listeners[e.type] || []).forEach((f) => f(e));
  }
  fire(t, extra) { this.dispatchEvent(Object.assign({ type: t, target: this, detail: 1, preventDefault() {} }, extra)); }
  focus() { this.doc.activeElement = this; }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) { return this.all().filter((e) => matches(e, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  reset() { this.all().forEach((e) => { if (e.tagName === 'INPUT') e.value = e.defaultValue; }); }
}

function build() {
  const timers = [];
  const docListeners = {};
  const doc = {
    activeElement: null,
    createElement: (t) => new El(t, doc),
    createTextNode: (t) => { const e = new El('#text', doc); e._text = t; return e; },
    getElementById: (id) => body.all().find((e) => e.id === id) || null,
    querySelector: (s) => body.querySelector(s),
    querySelectorAll: (s) => body.querySelectorAll(s),
    listeners: docListeners,
    addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  };
  const body = new El('body', doc);
  doc.body = body;
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise, Set, Event: class { constructor(t) { this.type = t; } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {}, MM: {}, UIFeedback: {},
    matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  const add = (tag, props, parent) => {
    const e = new El(tag, doc);
    Object.assign(e, props || {});
    if (props && props.id) e.setAttribute('id', props.id);
    (parent || body).appendChild(e);
    return e;
  };
  const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
  return { ctx, doc, body, load, add, timers, flush, docListeners };
}

const names = (list, cls) => list.children.filter((c) => c.classList.contains(cls));

const scenarios = {
  // The row goes only when its height has closed, and focus moves to the next row.
  async leave() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js');
    const list = h.add('div', { id: 'list' });
    const rows = ['a', 'b', 'c'].map((n) => {
      const r = h.add('div', { className: 'r' }, list);
      r.dataset.n = n;
      h.add('button', { className: 'link-danger' }, r);
      return r;
    });
    rows[1].children[0].focus();
    let done = 0;
    h.ctx.MM.settings.rows.leave(rows[1], { focusSelector: '.link-danger', done: () => { done += 1; } });
    rows[1].fire('transitionend', { propertyName: 'opacity' });
    const afterOpacity = { present: !!rows[1].parentNode, done };
    rows[1].fire('transitionend', { propertyName: 'height' });
    const focusOnNext = h.doc.activeElement === rows[2].children[0];
    // a second row, left to the timeout fallback
    rows[2].children[0].focus();
    h.ctx.MM.settings.rows.leave(rows[2], { focusSelector: '.link-danger' });
    h.timers.forEach((t) => t.fn());
    return {
      afterOpacity, afterHeight: { present: !!rows[1].parentNode, done }, focusOnNext,
      fallbackRemoved: !rows[2].parentNode, focusOnPrev: h.doc.activeElement === rows[0].children[0],
    };
  },

  // Discard puts back what was on the page at load, and what was last saved after a save.
  async savebar() {
    const h = build();
    const page = h.add('div', { className: 'setg-pg' });
    const bar = h.add('div', { id: 'setg-savebar' });
    const save = h.add('button', { id: 'btn-save' }, bar);
    const discard = h.add('button', { id: 'btn-discard' }, bar);
    const input = h.add('input', { id: 'plex_url', value: 'http://a', type: 'text' }, page);
    const box = h.add('input', { id: 'flag', type: 'checkbox', checked: true }, page);
    h.ctx.MM.api = { put: () => Promise.resolve({ status: 'saved' }), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    let confirmed = true;
    h.ctx.UIFeedback.confirm = () => Promise.resolve(confirmed);
    h.load('settings/savebar.js');
    h.ctx.MM.settings.savebar.init({});
    const edit = (v, c) => { input.value = v; box.checked = c; input.fire('input'); };
    edit('http://b', false);
    const dirty = bar.classList.contains('on');
    discard.fire('click');
    await h.flush();
    const afterDiscard = { value: input.value, checked: box.checked, dirty: bar.classList.contains('on') };
    // save a new value, then edit again and discard: back to the saved value
    edit('http://c', true);
    save.fire('click');
    await h.flush();
    edit('http://d', false);
    discard.fire('click');
    await h.flush();
    const afterSaveDiscard = { value: input.value, flag: box.checked };
    // a refused dialog leaves the edits alone
    confirmed = false;
    edit('http://e', false);
    discard.fire('click');
    await h.flush();
    return { dirty, afterDiscard, afterSaveDiscard, afterSave: { value: input.value === 'http://e' ? 'kept' : input.value, flag: box.checked } };
  },

  // A subscriber is removed only after the dialog is confirmed.
  async remove() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/newsletter.js');
    const list = h.add('div', { id: 'subscriber-list' });
    const calls = { deletes: [], toasts: [] };
    let answer = false;
    h.ctx.MM.api = {
      get: () => Promise.resolve({ subscribers: [{ id: 1, email: 'a@x', active: true }, { id: 2, email: 'b@x', active: true }] }),
      delete: (u) => { calls.deletes.push(u); return Promise.resolve({}); },
      messageOf: () => 'x',
    };
    h.ctx.UIFeedback.confirm = () => Promise.resolve(answer);
    h.ctx.UIFeedback.success = (m) => calls.toasts.push(m);
    h.ctx.MM.settings.newsletter.init();
    await h.flush();
    const rowsNow = () => names(list, 'sub-row').map((r) => r.dataset.id);
    const btn = () => list.querySelector('.sub-row[data-id="1"]').querySelector('.link-danger');
    btn().fire('click');
    await h.flush();
    const declined = { deletes: calls.deletes.length, rows: rowsNow(), label: btn().textContent, disabled: btn().disabled };
    answer = true;
    const b = btn();
    b.fire('click');
    await h.flush();
    const working = { label: b.textContent, aria: b.getAttribute('aria-label') };
    const row = list.children[0];
    row.fire('transitionend', { propertyName: 'height' });
    return { declined, working, deletes: calls.deletes, rows: rowsNow(), toasts: calls.toasts, idleAria: declined.label };
  },

  // New rows land where the server lists them.
  async insertOrder() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/newsletter.js'); h.load('settings/users.js');
    const subList = h.add('div', { id: 'subscriber-list' });
    const subInput = h.add('input', { id: 'new-subscriber-email', value: 'x@x' });
    const subBtn = h.add('button', { id: 'btn-add-subscriber' });
    const userList = h.add('div', { id: 'user-list' });
    const addForm = h.add('form', { id: 'add-user-form' });
    h.add('input', { id: 'new-username', value: 'dan' }, addForm);
    h.add('input', { id: 'new-user-password', value: 'a-long-password-1' }, addForm);
    h.add('button', { id: 'btn-submit-add-user' }, addForm);
    h.add('span', { id: 'create-user-result' }, addForm);
    let subs = [[1, 'a'], [3, 'c']];
    let users = [[1, 'ann'], [3, 'cat'], [5, 'eve']];
    h.ctx.MM.api = {
      get: (u) => Promise.resolve(u === '/api/users'
        ? { current: 'ann', users: users.map(([id, username]) => ({ id, username, role: 'viewer' })) }
        : { subscribers: subs.map(([id, email]) => ({ id, email, active: true })) }),
      postForm: () => Promise.resolve({}),
      post: () => Promise.resolve({}),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.MM.settings.newsletter.init();
    h.ctx.MM.settings.users.init();
    await h.flush();
    subs = [[1, 'a'], [2, 'b'], [3, 'c'], [4, 'd']];
    subBtn.fire('click');
    users = [[1, 'ann'], [2, 'bob'], [3, 'cat'], [4, 'dan'], [5, 'eve']];
    addForm.fire('submit');
    await h.flush();
    const roleBtn = userList.querySelector('[data-user-action="role"]');
    const roleName = { text: roleBtn.textContent, aria: roleBtn.getAttribute('aria-label') };
    return { roleName, subs: names(subList, 'sub-row').map((r) => r.dataset.id), userRows: names(userList, 'usr-row').map((r) => String(r.dataset.userId)) };
  },

  // Opening another form leaves one that is still sending where it is.
  async busyForm() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const $ = (tag, id, parent, props) => h.add(tag, Object.assign({ id }, props), parent);
    $('div', 'user-list');
    const pwForm = $('form', 'self-password-form', null, { hidden: true });
    $('input', 'self-old-password', pwForm, { value: 'old' });
    $('input', 'self-new-password', pwForm, { value: 'a-long-new-password' });
    $('input', 'self-confirm-password', pwForm, { value: 'a-long-new-password' });
    const btnPw = $('button', 'btn-change-password');
    const submitPw = $('button', 'btn-submit-password', pwForm);
    $('span', 'password-result', pwForm);
    const cancelPw = $('button', 'btn-cancel-password', pwForm);
    const emailForm = $('form', 'self-email-form', null, { hidden: true });
    const emailInput = $('input', 'self-email-input', emailForm, { value: 'me@x', defaultValue: 'me@x' });
    $('span', 'email-result', emailForm);
    $('button', 'btn-cancel-email', emailForm);
    $('button', 'btn-submit-email', emailForm);
    const btnEmail = $('button', 'btn-edit-email');
    let finish;
    h.ctx.MM.api = { get: () => Promise.resolve({ current: 'me', users: [] }), post: () => new Promise((r) => { finish = r; }), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.MM.settings.users.init();
    await h.flush();
    btnPw.fire('click');
    pwForm.fire('submit');
    btnEmail.fire('click');
    const whileBusy = { pwHidden: pwForm.hidden, emailHidden: emailForm.hidden };
    // its own toggle and Cancel do not close or reset it mid-request
    h.doc.getElementById('self-old-password').value = 'typed';
    btnPw.fire('click');
    cancelPw.fire('click');
    const ownButtons = { hidden: pwForm.hidden, value: h.doc.getElementById('self-old-password').value };
    finish({});
    await h.flush();
    // a form that is idle closes, and closing puts the typed values back to their start
    emailInput.value = 'typed@x';
    h.timers.forEach((t) => t.fn());
    btnPw.fire('click');
    const afterIdle = { emailHidden: emailForm.hidden, emailValue: emailInput.value, submitDisabled: submitPw.disabled };
    return { whileBusy, ownButtons, afterIdle };
  },

  // A throttled service is not counted as not connected.
  async rail() {
    const h = build();
    h.load('settings/integrations.js');
    const item = h.add('a', { className: 'setg-rail-item' });
    item.setAttribute('href', '#setg-integrations');
    const pill = (svc) => { const p = h.add('span', { className: 'conn' }); p.setAttribute('data-conn', svc); return p; };
    pill('plex'); pill('sonarr');
    h.ctx.MM.api = { post: (u) => Promise.reject(u.endsWith('plex') ? { status: 429 } : { status: 502, error: 'x' }), messageOf: () => 'Connection failed' };
    ['plex_url', 'plex_token', 'sonarr_url', 'sonarr_api_key'].forEach((id) => h.add('input', { id, value: 'v' }));
    h.ctx.MM.settings.integrations.init();
    h.timers.forEach((t) => t.fn());
    await h.flush();
    const badge = item.querySelector('.rail-count');
    return { plex: h.doc.querySelector('[data-conn="plex"]').className, badge: badge ? badge.textContent : null };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
