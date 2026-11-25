// Runs the real settings/users.js against the fake DOM and prints where each refusal, of the add form or of a row, is said.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

const revealed = [];
Node.prototype.scrollIntoView = function () { revealed.push(this.id); };
Node.prototype.getBoundingClientRect = () => ({ top: 300, bottom: 320 });
Node.prototype.reset = () => {};
Node.prototype.replaceChildren = function (...cs) { this.textContent = ''; this.append(...cs); };
Object.defineProperty(Node.prototype, 'dataset', {
  get() { return (this._dataset = this._dataset || {}); },
});
// A focused control that is disabled loses the focus, as in a browser.
Object.defineProperty(Node.prototype, 'disabled', {
  get() { return Boolean(this._disabled); },
  set(on) {
    this._disabled = on;
    if (on && this.ownerDocument.activeElement === this) this.blur();
  },
});

// Where the status at the button pressed has been scrolled to: on screen, or off it.
const box = (top) => () => ({ top, bottom: top + 20 });

// As the password dialog does: it takes the focus, and gives it back to what had it.
function cancelled(doc) {
  const opener = doc.activeElement;
  doc.getElementById('elsewhere').focus();
  return Promise.resolve().then(() => {
    if (opener === doc.body) doc.activeElement.blur(); else opener.focus();
    throw { error: 'reauth_cancelled' };
  });
}

const row = { id: 7, username: 'vic', role: 'viewer', email: null };

function page({ users = [], reauth = (f) => f() } = {}) {
  const doc = makeDocument();
  doc.documentElement.clientHeight = 600;
  doc.body.appendChild(build(doc, ['main', {},
    ['button', { id: 'btn-add-user' }], ['button', { id: 'btn-cancel-add-user' }], ['input', { id: 'elsewhere' }],
    ['form', { id: 'add-user-form' },
      ['input', { id: 'new-username' }], ['span', { id: 'new-username-why', class: 'fld-why' }],
      ['input', { id: 'new-user-password' }], ['span', { id: 'new-user-password-why', class: 'fld-why' }],
      ['button', { id: 'btn-submit-add-user' }], ['span', { id: 'create-user-result' }]]]));
  doc.querySelector('main').appendChild(build(doc, ['div', { id: 'user-list' }]));
  const $ = (id) => doc.getElementById(id);
  const toasts = [];
  const UIFeedback = { confirm: () => Promise.resolve(true), success() {}, error: (text) => toasts.push(text) };
  const MM = { reauth: { run: (f) => reauth(f, doc) } };
  const ctx = vm.createContext({ document: doc, MM, UIFeedback, getComputedStyle: () => ({}), setTimeout: (fn) => setTimeout(fn, 0), matchMedia: () => ({ matches: true }) });
  ctx.window = ctx;
  ['core/api.js', 'core/dom.js', 'settings/rows.js', 'settings/users.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  MM.api.get = () => Promise.resolve({ current: 'admin', users });
  const refusal = (error) => new MM.api.APIError('x', 'No', error.status, error.issues, { message: 'No' });
  MM.settings.users.init();
  const fire = (el, type) => el.listeners[type].forEach((fn) => fn({ preventDefault() {}, key: '' }));
  return { doc, $, MM, toasts, refusal, fire, focused: () => doc.activeElement.id || doc.activeElement.dataset.userAction || 'body' };
}

async function refuse({ name = 'riley', password = 'a-long-enough-password', error, focus = 'btn-submit-add-user', top = 300, without }) {
  const { doc, $, MM, refusal, fire } = page();
  MM.api.post = () => Promise.reject(refusal(error));
  if (without) $(without).remove();
  $('new-username').value = name;
  $('new-user-password').value = password;
  $('btn-submit-add-user').focus();
  revealed.length = 0;
  fire($('add-user-form'), 'submit');
  $('create-user-result').getBoundingClientRect = box(top);
  if (focus !== 'btn-submit-add-user') $(focus).focus();
  const sent = revealed.length;
  await settle();
  const texts = () => ['new-username-why', 'new-user-password-why', 'create-user-result'].filter((id) => $(id) && $(id).textContent);
  const out = { said: texts(), words: texts().map((id) => $(id).textContent).join(), focused: doc.activeElement.id || 'body', last: revealed.slice(-2), late: revealed.slice(sent) };
  fire($('add-user-form'), 'input');
  return { ...out, typed: texts() };
}

// The add form with the password dialog cancelled, then with the answer still to come.
async function cancel() {
  const { $, fire, focused } = page({ reauth: (f, doc) => cancelled(doc) });
  $('new-username').value = 'riley';
  $('new-user-password').value = 'a-long-enough-password';
  $('btn-submit-add-user').focus();
  fire($('add-user-form'), 'submit');
  await settle();
  return { focused: focused(), said: $('create-user-result').textContent };
}

async function sending() {
  const { $, MM, fire, focused } = page();
  let posts = 0;
  MM.api.post = () => { posts += 1; return new Promise(() => {}); };
  $('new-username').value = 'riley';
  $('new-user-password').value = 'a-long-enough-password';
  $('btn-submit-add-user').focus();
  ['submit', 'submit', 'input'].forEach((type) => fire($('add-user-form'), type));
  fire($('btn-add-user'), 'click');
  fire($('btn-cancel-add-user'), 'click');
  await settle();
  return { posts, focused: focused(), open: !$('add-user-form').hidden, said: $('create-user-result').textContent };
}

// A refused change of a row, with the reader where they pressed, gone elsewhere, or scrolled away with the focus nowhere.
async function refuseRow(action, { focus, top = 300 } = {}) {
  const { doc, $, MM, toasts, refusal, fire, focused } = page({ users: [row] });
  let give;
  MM.api.patch = MM.api.delete = () => new Promise((_resolve, reject) => { give = () => reject(refusal({ status: 400 })); });
  await settle();
  const pressed = doc.querySelectorAll('button').find((b) => b.dataset.userAction === action);
  let status = pressed;
  pressed.focus();
  fire(pressed, 'click');
  if (action === 'email') {
    const form = doc.querySelector('.usr-email');
    form.querySelector('input').id = 'field';
    status = form.querySelector('.inline-form-msg');
    form.querySelectorAll('button')[1].focus();
    fire(form, 'submit');
  }
  await settle();
  revealed.length = 0;
  status.getBoundingClientRect = box(top);
  if (focus) $(focus).focus(); else if (focus === null) doc.activeElement.blur();
  give();
  await settle();
  return { focused: focused(), moved: revealed.length > 0, said: [...toasts, action === 'email' ? status.textContent : ''].join('') };
}

const places = async (action) => ({
  there: await refuseRow(action),
  elsewhere: await refuseRow(action, { focus: 'elsewhere' }),
  scrolled: await refuseRow(action, { focus: null, top: 700 }),
});

const api = (status, issues) => ({ status, issues });

(async () => {
  const out = {
    shortName: await refuse({ name: 'ab' }),
    shortPassword: await refuse({ password: 'short' }),
    weakPassword: await refuse({ error: api(400, ['Add a digit']) }),
    takenName: await refuse({ error: api(409) }),
    limit: await refuse({ error: api(429) }),
    offline: await refuse({ error: api(0) }),
    serverError: await refuse({ error: api(500) }),
    takenElsewhere: await refuse({ error: api(409), focus: 'elsewhere' }),
    takenScrolled: await refuse({ error: api(409), top: 700 }),
    noPlace: await refuse({ name: 'ab', without: 'new-username-why' }),
    limitElsewhere: await refuse({ error: api(429), focus: 'elsewhere' }),
  };
  const rows = { role: await places('role'), delete: await places('delete'), email: await places('email') };
  process.stdout.write(JSON.stringify({ add: out, cancel: await cancel(), sending: await sending(), rows }));
  process.exit(0);
})();
