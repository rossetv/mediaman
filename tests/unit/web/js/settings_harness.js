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
  const part = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]|(:checked)/y;
  while (rest) {
    part.lastIndex = 0;
    m = part.exec(rest);
    if (!m) return false;
    if (m[1] && el.id !== m[1]) return false;
    if (m[2] && !el.classList.contains(m[2])) return false;
    if (m[6] && !el.checked) return false;
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
      add: (...cs) => cs.forEach((c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); }),
      remove: (...cs) => { el.className = list().filter((x) => !cs.includes(x)).join(' '); },
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(c) : !!force;
        if (on) el.classList.add(c); else el.classList.remove(c);
        return on;
      },
    };
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === 'id') this.id = String(v);
  }
  getAttribute(k) {
    if (k === 'id') return this.id || null;
    if (k === 'type' && this.type) return this.type;
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
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  dispatchEvent(e) {
    for (let n = this; n; n = n.parentNode) (n.listeners[e.type] || []).forEach((f) => f(e));
    (this.doc.listeners[e.type] || []).forEach((f) => f(e));
  }
  fire(t, extra) { this.dispatchEvent(Object.assign({ type: t, target: this, detail: 1, preventDefault() {} }, extra)); }
  focus(opts) { this.doc.activeElement = this; this.lastFocusOpts = opts; }
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
    setTimeout: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; },
    clearTimeout: (t) => { if (timers.includes(t)) timers.splice(timers.indexOf(t), 1); }, MM: {}, UIFeedback: {},
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
  // Runs every timer due so far, and those they set in turn.
  const drain = () => { while (timers.length) timers.shift().fn(); };
  return { ctx, doc, body, load, add, timers, flush, drain, docListeners };
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

  // The settings a save sends.
  async payloadKeys() {
    const h = build();
    h.load('settings/general.js');
    return Object.keys(h.ctx.MM.settings.general.collectSettings());
  },

  // An edit made while a save is under way, or just after it, is not in what was sent.
  async editDuringSave() {
    const h = build();
    const page = h.add('div', { className: 'setg-pg' });
    const bar = h.add('div', { id: 'setg-savebar' });
    const save = h.add('button', { id: 'btn-save' }, bar);
    const discard = h.add('button', { id: 'btn-discard' }, bar);
    const status = h.add('span', { id: 'save-status' }, bar);
    const input = h.add('input', { id: 'plex_url', value: 'old', type: 'text' }, page);
    let reply;
    h.ctx.MM.api = { put: () => new Promise((done) => { reply = () => done({ status: 'saved' }); }), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.UIFeedback.confirm = () => Promise.resolve(true);
    h.load('settings/savebar.js');
    h.ctx.MM.settings.savebar.init({});
    const edit = (v) => { input.value = v; input.fire('input'); };
    const now = () => ({ on: bar.classList.contains('on'), button: save.textContent, disabled: save.disabled });
    edit('first');
    save.focus();
    save.fire('click', { detail: 0 });
    const discardWhileSending = discard.disabled;
    input.focus();
    edit('during');
    reply();
    await h.flush();
    const status1 = status.textContent;
    h.drain();
    const during = now();
    const keptPlace = h.doc.activeElement === input;
    discard.fire('click');
    await h.flush();
    const discarded = input.value;
    // the moment between the reply and the bar hiding
    edit('second');
    save.fire('click');
    reply();
    await h.flush();
    const said = save.textContent;
    edit('after');
    h.drain();
    const after = now();
    // with no edit the bar goes
    save.fire('click');
    reply();
    await h.flush();
    h.drain();
    return { discardWhileSending, during, status: status1, keptPlace, discarded, said, after, quiet: now(), discardAfter: discard.disabled };
  },

  // After a failed save any edit, a switch included, puts the bar back to unsaved.
  async editAfterFailure() {
    const h = build();
    const page = h.add('div', { className: 'setg-pg' });
    const bar = h.add('div', { id: 'setg-savebar' });
    const save = h.add('button', { id: 'btn-save', className: 'btn btn--primary' }, bar);
    const status = h.add('span', { id: 'save-status' }, bar);
    const input = h.add('input', { id: 'plex_url', value: 'a', type: 'text' }, page);
    h.ctx.MM.api = { put: () => Promise.reject({ status: 429, data: {} }), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.load('settings/savebar.js');
    h.ctx.MM.settings.savebar.init({});
    const now = () => ({ button: save.textContent, primary: save.classList.contains('btn--primary'), error: bar.classList.contains('is-error'), status: status.textContent });
    const fail = async () => { save.fire('click'); await h.flush(); return now(); };
    input.fire('input');
    const failed = await fail();
    input.fire('input');
    const typed = now();
    await fail();
    h.ctx.MM.settings.savebar.markDirty();
    return { failed, typed, switched: now() };
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

  // What the list says of someone who has unsubscribed.
  async subscriberStates() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/newsletter.js');
    const list = h.add('div', { id: 'subscriber-list' });
    h.ctx.MM.api = { get: () => Promise.resolve({ subscribers: [{ id: 1, email: 'a@x', active: true }, { id: 2, email: 'b@x', active: false }] }) };
    h.ctx.MM.settings.newsletter.init();
    await h.flush();
    return list.querySelectorAll('.sub-stat').map((s) => s.textContent);
  },

  // The avatar each name gets in the Team list.
  async avatars() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    const given = JSON.parse(process.argv[3]);
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'x', users: given.map((username, id) => ({ id, username, role: 'viewer' })) }),
    };
    h.ctx.MM.settings.users.init();
    await h.flush();
    return names(userList, 'usr-row').map((r) => [r.children[0].className, r.children[0].textContent]);
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
    // the new rows come in once the form has closed
    h.drain();
    await h.flush();
    const roleBtn = userList.querySelector('[data-user-action="role"]');
    const roleName = { text: roleBtn.textContent, aria: roleBtn.getAttribute('aria-label') };
    return { roleName, subs: names(subList, 'sub-row').map((r) => r.dataset.id), userRows: names(userList, 'usr-row').map((r) => String(r.dataset.userId)) };
  },

  // The add user form that is sending ignores its own toggle and Cancel.
  async busyAddForm() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    h.add('div', { id: 'user-list' });
    const btnAdd = h.add('button', { id: 'btn-add-user' });
    const form = h.add('form', { id: 'add-user-form', hidden: true });
    const name = h.add('input', { id: 'new-username', value: '', defaultValue: '' }, form);
    const pw = h.add('input', { id: 'new-user-password', value: '' }, form);
    h.add('button', { id: 'btn-submit-add-user' }, form);
    h.add('span', { id: 'create-user-result' }, form);
    const cancel = h.add('button', { id: 'btn-cancel-add-user' }, form);
    h.ctx.MM.api = { get: () => Promise.resolve({ current: 'me', users: [] }), post: () => new Promise(() => {}), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.MM.settings.users.init();
    await h.flush();
    btnAdd.fire('click');
    name.value = 'casey';
    pw.value = 'a-long-new-password';
    form.fire('submit');
    name.value = 'typed';
    btnAdd.fire('click');
    cancel.fire('click');
    h.drain();
    return { hidden: form.hidden, value: name.value };
  },

  // A new user's row comes in only once the add form has folded away.
  async addUserOrder() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    const btnAdd = h.add('button', { id: 'btn-add-user' });
    const form = h.add('form', { id: 'add-user-form', hidden: true });
    h.add('input', { id: 'new-username' }, form);
    h.add('input', { id: 'new-user-password' }, form);
    const submit = h.add('button', { id: 'btn-submit-add-user' }, form);
    h.add('span', { id: 'create-user-result' }, form);
    let users = [[1, 'ann']];
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'ann', users: users.map(([id, username]) => ({ id, username, role: 'viewer' })) }),
      post: () => Promise.resolve({}),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.MM.settings.users.init();
    await h.flush();
    btnAdd.fire('click');
    h.doc.getElementById('new-username').value = 'bob';
    h.doc.getElementById('new-user-password').value = 'a-long-password-1';
    users = [[1, 'ann'], [2, 'bob']];
    form.fire('submit');
    await h.flush();
    const rowsNow = () => names(userList, 'usr-row').length;
    const afterCreate = { rows: rowsNow(), open: !form.hidden };
    btnAdd.scrollCalls = [];
    btnAdd.scrollIntoView = function (o) { this.scrollCalls.push(o); };
    h.timers.splice(0).filter((t) => t.ms === 700).forEach((t) => t.fn());
    await h.flush();
    const scrollWhileClosing = btnAdd.scrollCalls.length;
    const whileClosing = { rows: rowsNow(), shut: form.classList.contains('is-shut'), hidden: form.hidden };
    form.fire('transitionend', { propertyName: 'grid-template-rows' });
    await h.flush();
    return {
      scrollWhileClosing, scrollAfterClose: btnAdd.scrollCalls,
      afterCreate, whileClosing, afterClose: { rows: rowsNow(), hidden: form.hidden, submitDisabled: submit.disabled },
      // handing focus back to the button must not scroll the page to it
      refocus: { onButton: h.doc.activeElement === btnAdd, preventScroll: !!(btnAdd.lastFocusOpts && btnAdd.lastFocusOpts.preventScroll) },
    };
  },

  // A message about what was typed goes once the person types again.
  async clearOnInput() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js'); h.load('settings/newsletter.js');
    h.add('div', { id: 'user-list' });
    h.add('div', { id: 'subscriber-list' });
    const btnAdd = h.add('button', { id: 'btn-add-user' });
    const form = h.add('form', { id: 'add-user-form', hidden: true });
    const name = h.add('input', { id: 'new-username' }, form);
    h.add('input', { id: 'new-user-password' }, form);
    h.add('button', { id: 'btn-submit-add-user' }, form);
    const msg = h.add('span', { id: 'create-user-result' }, form);
    const panel = h.add('div', { id: 'newsletter-send-panel', hidden: true });
    const recipients = h.add('div', { id: 'newsletter-recipient-list' }, panel);
    const confirm = h.add('button', { id: 'btn-confirm-newsletter' }, panel);
    const status = h.add('span', { id: 'newsletter-send-status' }, panel);
    const btnSend = h.add('button', { id: 'btn-send-newsletter' });
    h.ctx.MM.api = {
      get: (u) => Promise.resolve(u === '/api/users'
        ? { current: 'ann', users: [] }
        : { subscribers: [{ id: 1, email: 'a@x', active: true }] }),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.MM.settings.users.init();
    h.ctx.MM.settings.newsletter.init();
    await h.flush();
    btnAdd.fire('click');
    name.value = 'ab';
    form.fire('submit');
    const shown = msg.textContent;
    name.value = 'abc';
    name.fire('input');
    btnSend.fire('click');
    await h.flush();
    const box = recipients.querySelector('input');
    box.checked = false;
    confirm.fire('click');
    const nlShown = status.textContent;
    box.checked = true;
    box.fire('change');
    return { shown, afterInput: msg.textContent, nlShown, afterChange: status.textContent };
  },

  // The account page's message goes once any field is edited, but not while the form is sending.
  async accountClearOnInput() {
    const h = build();
    const form = h.add('form', { id: 'account-password-form' });
    const oldPw = h.add('input', { id: 'account-old-password', type: 'password' }, form);
    h.add('input', { id: 'account-new-password', type: 'password' }, form);
    h.add('input', { id: 'account-confirm-password', type: 'password' }, form);
    const submit = h.add('button', { id: 'account-submit-password' }, form);
    const msg = h.add('span', { id: 'account-password-result' }, form);
    let finish;
    h.ctx.MM.api = { post: () => new Promise((r) => { finish = r; }), messageOf: () => 'x' };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.load('account.js');
    form.fire('submit');
    const shown = msg.textContent;
    oldPw.value = 'a';
    oldPw.fire('input');
    const afterInput = msg.textContent;
    // a send under way keeps its message
    h.doc.getElementById('account-old-password').value = 'old';
    h.doc.getElementById('account-new-password').value = 'a-long-enough-password';
    h.doc.getElementById('account-confirm-password').value = 'a-long-enough-password';
    msg.textContent = 'Working';
    form.fire('submit');
    oldPw.fire('input');
    const whileSending = { text: msg.textContent, disabled: submit.getAttribute('aria-disabled') === 'true' };
    finish({});
    await h.flush();
    return { shown, afterInput, whileSending };
  },

  // A newsletter send under way ignores its own toggle and Cancel.
  async newsletterBusy() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/newsletter.js');
    h.add('div', { id: 'subscriber-list' });
    const panel = h.add('div', { id: 'newsletter-send-panel', hidden: true });
    h.add('div', { id: 'newsletter-recipient-list' }, panel);
    const confirm = h.add('button', { id: 'btn-confirm-newsletter' }, panel);
    const cancel = h.add('button', { id: 'btn-cancel-newsletter' }, panel);
    h.add('span', { id: 'newsletter-send-status' }, panel);
    const toggle = h.add('button', { id: 'btn-send-newsletter' });
    let finish;
    h.ctx.MM.api = {
      get: () => Promise.resolve({ subscribers: [{ id: 1, email: 'a@x', active: true }] }),
      post: () => new Promise((r) => { finish = r; }),
      messageOf: () => 'x',
    };
    h.ctx.MM.settings.newsletter.init();
    await h.flush();
    toggle.fire('click');
    await h.flush();
    confirm.fire('click');
    toggle.fire('click');
    cancel.fire('click');
    h.drain();
    const whileSending = { hidden: panel.hidden, toggle: toggle.textContent, disabled: confirm.disabled };
    finish({ sent_to: 1 });
    await h.flush();
    cancel.fire('click');
    return { whileSending, afterwards: { toggle: toggle.textContent } };
  },

  // A field the server refuses is named by its label, in a plain sentence.
  async fieldError() {
    const h = build();
    h.add('div', { className: 'setg-pg' });
    const bar = h.add('div', { id: 'setg-savebar' });
    const save = h.add('button', { id: 'btn-save' }, bar);
    const status = h.add('span', { id: 'save-status' }, bar);
    const url = h.add('input', { id: 'base_url', type: 'text' });
    url.setAttribute('aria-label', 'Base URL');
    h.ctx.MM.api = {
      put: () => Promise.reject({ status: 422, data: { detail: [
        { loc: ['body', 'base_url'], msg: "Value error, URL scheme must be http or https, got ''" },
        { loc: ['body', 'grace_days'], msg: 'Value error, Grace period must be between 1 and 365 days' },
        { loc: ['body', 'mystery_key'], msg: 'Extra inputs are not permitted' },
      ] } }),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.load('settings/savebar.js');
    h.ctx.MM.settings.savebar.init({});
    save.fire('click');
    await h.flush();
    return { text: status.textContent };
  },

  // A role change updates its row where it stands.
  async roleInPlace() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'ann', users: [{ id: 2, username: 'bob', role: 'viewer' }] }),
      patch: () => Promise.resolve({}),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.UIFeedback.success = () => {};
    h.ctx.MM.settings.users.init();
    await h.flush();
    const row = userList.querySelector('.usr-row');
    const btn = row.querySelector('[data-user-action="role"]');
    btn.focus();
    btn.fire('click');
    await h.flush();
    const again = row.querySelector('[data-user-action="role"]');
    return {
      sameRow: userList.querySelector('.usr-row') === row,
      pill: row.querySelector('.pill').textContent,
      button: again.textContent,
      focused: h.doc.activeElement === again,
      disabled: again.disabled,
    };
  },

  // An admin changes someone's email in a form that folds open in their row.
  async userEmail() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    const sent = [];
    const errors = [];
    let answer = () => Promise.resolve({ email: 'new@x.example' });
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'ann', users: [
        { id: 1, username: 'ann', role: 'admin', email: 'ann@x.example' },
        { id: 2, username: 'bob', role: 'viewer', email: 'old@x.example', created_at: '2026-01-02T12:00:00+00:00' },
      ] }),
      patch: (u, b) => { sent.push([u, b.email]); return answer(); },
      messageOf: (e) => e.message,
    };
    h.ctx.MM.reauth = { run: (f) => f() };
    h.ctx.UIFeedback.success = () => {};
    h.ctx.UIFeedback.error = (m) => errors.push(m);
    h.ctx.MM.settings.users.init();
    await h.flush();
    const [own, row] = names(userList, 'usr-row');
    const btn = row.querySelector('[data-user-action="email"]');
    const form = row.querySelector('form');
    const input = form.querySelector('input');
    const msg = form.querySelector('.inline-form-msg');
    const save = form.querySelector('[type="submit"]');
    const open = () => h.ctx.MM.settings.rows.isOpen(form);
    const sub = () => row.querySelector('.usr-sub').textContent;
    const out = { ownHasButton: !!own.querySelector('[data-user-action="email"]'), shutAtFirst: !open() };
    btn.fire('click');
    out.opened = { open: open(), value: input.value, focused: h.doc.activeElement === input };

    // refused: what was typed stays, with why
    answer = () => Promise.reject({ error: 'email_taken', message: 'Another user has this address.' });
    input.value = ' ann@x.example '; input.fire('input');
    form.fire('submit');
    await h.flush();
    out.refused = { open: open(), value: input.value, msg: msg.textContent, focused: h.doc.activeElement === input, sub: sub() };
    input.fire('input');
    out.clearedOnInput = msg.textContent;

    // turned away at the password: nothing is said
    answer = () => Promise.reject({ error: 'reauth_cancelled', message: 'Cancelled' });
    form.fire('submit');
    await h.flush();
    out.cancelled = { open: open(), msg: msg.textContent };

    // while it is sent, a second submit or the button does nothing
    let done;
    answer = () => new Promise((r) => { done = r; });
    const before = sent.length;
    form.fire('submit'); form.fire('submit'); btn.fire('click');
    out.sending = { sent: sent.length - before, open: open(), busy: save.getAttribute('aria-disabled') };
    done({ email: 'new@x.example' });
    await h.flush(); h.drain();
    out.saved = { open: open(), sub: sub(), focused: h.doc.activeElement === btn, sent: sent[sent.length - 1], busy: save.getAttribute('aria-disabled') };

    // an empty field takes it away; a cancelled edit is forgotten
    btn.fire('click');
    out.again = input.value;
    input.value = 'typo'; form.querySelector('[type="button"]').fire('click'); h.drain();
    btn.fire('click');
    out.forgotten = input.value;
    answer = () => Promise.resolve({ email: null });
    input.value = '';
    form.fire('submit');
    await h.flush(); h.drain();
    out.removed = { sub: sub(), sent: sent[sent.length - 1], errors };
    return out;
  },

  // A click on Save focuses no button there, and the password dialog gives focus back to what had it.
  async userEmailSavedWithoutFocus() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    const main = h.add('main', { id: 'main' });
    const add = h.add('button', { id: 'btn-add-user' });
    const dialog = h.add('input', { id: 'dialog' });
    const form = h.add('form', { id: 'add-user-form', hidden: true });
    h.add('input', { id: 'new-username', value: '' }, form);
    h.add('input', { id: 'new-user-password', value: '' }, form);
    const submit = h.add('button', { id: 'btn-submit-add-user' }, form);
    h.add('span', { id: 'create-user-result' }, form);
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'ann', users: [{ id: 2, username: 'bob', role: 'viewer', email: '' }] }),
      patch: () => Promise.resolve({ email: 'new@x.example' }),
      post: () => Promise.resolve({}),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => {
      const opener = h.doc.activeElement;
      dialog.focus();
      return f().then((r) => { opener.focus(); return r; });
    } };
    h.ctx.UIFeedback.success = () => {};
    h.ctx.MM.settings.users.init();
    await h.flush();
    const row = names(userList, 'usr-row')[0];
    const btn = row.querySelector('[data-user-action="email"]');
    btn.fire('click');
    main.focus();
    row.querySelector('form').fire('submit');
    await h.flush();
    const out = { email: h.doc.activeElement === btn };
    const role = row.querySelector('[data-user-action="role"]');
    h.ctx.MM.api.patch = () => Promise.resolve({});
    main.focus();
    role.fire('click');
    await h.flush(); h.drain();
    out.role = h.doc.activeElement === role;
    h.ctx.UIFeedback.confirm = () => Promise.resolve(true);
    h.ctx.MM.api.delete = () => Promise.resolve({});
    main.focus();
    row.querySelector('[data-user-action="delete"]').fire('click');
    await h.flush(); h.drain();
    out.delete = h.doc.activeElement === add;
    add.fire('click');
    h.doc.getElementById('new-username').value = 'casey';
    h.doc.getElementById('new-user-password').value = 'a-long-new-password';
    main.focus();
    form.fire('submit');
    await h.flush();
    out.addUser = h.doc.activeElement === submit;
    return out;
  },

  // The words of the password dialog when a user is deleted.
  async deleteWords() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    h.ctx.MM.api = { get: () => Promise.resolve({ current: 'ann', users: [{ id: 2, username: 'bob', role: 'viewer' }] }) };
    let words = null;
    h.ctx.MM.reauth = { run: (f, w) => { words = w || null; return new Promise(() => {}); } };
    h.ctx.UIFeedback.confirm = () => Promise.resolve(true);
    h.ctx.MM.settings.users.init();
    await h.flush();
    userList.querySelector('[data-user-action="delete"]').fire('click');
    await h.flush();
    return words;
  },

  // A click that focuses no button, as in Safari, shuts a form of the Team list.
  async shutWithoutFocus() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    const main = h.add('main', { id: 'main' });
    const add = h.add('button', { id: 'btn-add-user' });
    const addForm = h.add('form', { id: 'add-user-form', hidden: true });
    h.add('input', { id: 'new-username', value: '' }, addForm);
    const addCancel = h.add('button', { id: 'btn-cancel-add-user' }, addForm);
    h.ctx.MM.api = { get: () => Promise.resolve({ current: 'ann', users: [{ id: 2, username: 'bob', role: 'viewer', email: '' }] }) };
    h.ctx.MM.settings.users.init();
    await h.flush();
    const row = names(userList, 'usr-row')[0];
    const email = row.querySelector('[data-user-action="email"]');
    const cancel = row.querySelector('form').querySelector('[type="button"]');
    const shut = (opener, by) => {
      opener.fire('click');
      main.focus();
      by.fire('click');
      h.drain();
      return h.doc.activeElement === opener;
    };
    return { emailCancel: shut(email, cancel), emailAgain: shut(email, email), addCancel: shut(add, addCancel), addAgain: shut(add, add) };
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

  // A pressed Test that gets a 429 shows the warning tone and leaves the badge alone.
  async manualRateLimit() {
    const h = build();
    h.load('settings/integrations.js');
    const item = h.add('a', { className: 'setg-rail-item' });
    item.setAttribute('href', '#setg-integrations');
    const pill = h.add('span', { className: 'conn' });
    pill.setAttribute('data-conn', 'plex');
    const lbl = h.add('span', {}, pill);
    lbl.setAttribute('data-conn-label', '');
    const btn = h.add('button', {});
    btn.setAttribute('data-test-service', 'plex');
    h.ctx.MM.api = { post: () => Promise.reject({ status: 429 }), messageOf: () => 'Connection failed' };
    h.ctx.MM.dom = { focusStillIn: () => false };
    h.ctx.MM.settings.integrations.init();
    btn.fire('click');
    await h.flush();
    const badge = item.querySelector('.rail-count');
    return { pill: pill.className, label: pill.textContent, badge: badge ? badge.textContent : null, button: btn.textContent };
  },

  // A role change that needed the reauth dialog shows the new pill once the dialog has faded out.
  async roleAfterReauth() {
    const h = build();
    h.load('core/dom.js'); h.load('settings/rows.js'); h.load('settings/users.js');
    const userList = h.add('div', { id: 'user-list' });
    h.ctx.MM.api = {
      get: () => Promise.resolve({ current: 'ann', users: [{ id: 2, username: 'bob', role: 'viewer' }] }),
      patch: () => Promise.resolve({}),
      messageOf: () => 'x',
    };
    h.ctx.MM.reauth = { run: (f) => { h.add('div', { className: 'modal-backdrop reauth-backdrop' }); return f(); } };
    h.ctx.UIFeedback.success = () => {};
    h.ctx.MM.settings.users.init();
    await h.flush();
    const row = userList.querySelector('.usr-row');
    row.querySelector('[data-user-action="role"]').fire('click');
    await h.flush();
    const pill = () => row.querySelector('.pill');
    const during = { label: pill().textContent, changed: pill().classList.contains('is-changed') };
    h.timers.splice(0).forEach((t) => t.fn());
    return { during, after: { label: pill().textContent, changed: pill().classList.contains('is-changed') } };
  },

  // Opening an integration card folds its body open instead of popping it.
  async integrationFold() {
    const h = build();
    h.load('settings/rows.js'); h.load('settings/toggles.js');
    const page = h.add('div', { className: 'setg-pg' });
    const card = h.add('div', { className: 'intg-card is-collapsed' }, page);
    const hd = h.add('div', { className: 'intg-card-hd' }, card);
    hd.setAttribute('data-intg-toggle', ''); hd.setAttribute('aria-controls', 'body1'); hd.setAttribute('aria-expanded', 'false');
    const body = h.add('div', { id: 'body1', className: 'intg-card-bd', hidden: true }, card);
    const field = h.add('div', { className: 'fld-row' }, body);
    h.ctx.addEventListener = () => {}; h.ctx.innerHeight = 800; h.ctx.scrollY = 0;
    h.ctx.MM.settings.toggles.init();
    hd.fire('click');
    const open = { hidden: body.hidden, shut: body.classList.contains('is-shut'), moving: body.classList.contains('is-moving') };
    body.fire('transitionend', { propertyName: 'grid-template-rows' });
    hd.fire('click');
    const closing = { hidden: body.hidden, shut: body.classList.contains('is-shut') };
    body.fire('transitionend', { propertyName: 'grid-template-rows' });
    return {
      fold: body.classList.contains('setg-fold'), wrapped: field.parentNode.className === 'setg-fold-inner' && field.parentNode.parentNode === body,
      open, closing, closed: { hidden: body.hidden, expanded: hd.getAttribute('aria-expanded') },
    };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
