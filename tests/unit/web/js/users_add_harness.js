// Runs the real settings/users.js against the fake DOM and prints where each refusal, of the add form or of a row, is said.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');
const { parts, prepare } = require('./password_form_dom.js');

prepare(Node);

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

const GOOD = 'Copper lantern 41';
const row = { id: 7, username: 'vic', role: 'viewer', email: null };

// `timers` takes what waits for a time, for the test to run.
function page({ users = [], reauth = (f) => f(), timers, scripts = ['password-form.js'] } = {}) {
  const doc = makeDocument();
  doc.documentElement.clientHeight = 600;
  doc.body.appendChild(build(doc, ['main', {},
    ['button', { id: 'btn-add-user' }], ['input', { id: 'elsewhere' }], ['span', { id: 'add-user-live' }],
    ['div', { id: 'add-user-fold', class: 'setg-fold is-shut' }, ['div', {}, ['form', { id: 'add-user-form' }, ...parts({ user: 'new-username',
      fresh: 'new-user-password', submit: 'btn-submit-add-user', cancel: 'btn-cancel-add-user', result: 'create-user-result', words: 'Create user' })]]]]));
  doc.querySelector('main').appendChild(build(doc, ['div', { id: 'user-list' }]));
  const $ = (id) => doc.getElementById(id);
  $('add-user-fold').hidden = true;
  ['new-username', 'new-user-password'].forEach((id) => { $(id).value = ''; });
  const toasts = [];
  const UIFeedback = { confirm: () => Promise.resolve(true), success() {}, error: (text) => toasts.push(text) };
  const MM = { reauth: { run: (f) => reauth(f, doc) } };
  const ctx = vm.createContext({ document: doc, MM, UIFeedback, getComputedStyle: () => ({}), TextEncoder, addEventListener() {}, clearTimeout, setTimeout: (fn) => (timers ? timers.push(fn) : setTimeout(fn, 0)), matchMedia: () => ({ matches: true }) });
  ctx.window = ctx;
  ['core/api.js', 'core/dom.js', 'settings/rows.js', ...scripts, 'settings/users.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  MM.api.get = () => Promise.resolve({ current: 'admin', users });
  const refusal = (error) => new MM.api.APIError('x', 'No', error.status, error.issues, { message: 'No', rules: error.rules });
  MM.settings.users.init();
  const fire = (el, type) => el.listeners[type].forEach((fn) => fn({ target: el, preventDefault() {}, stopPropagation() {}, key: '' }));
  const type = (id, value) => { $(id).value = value; $('add-user-form').listeners.input.forEach((fn) => fn({ target: $(id) })); };
  return { type, doc, $, MM, toasts, refusal, fire, focused: () => doc.activeElement.id || doc.activeElement.dataset.userAction || 'body' };
}

async function refuse({ name = 'riley', password = GOOD, error, focus = 'btn-submit-add-user', top = 300 }) {
  const { doc, $, MM, refusal, fire, type } = page();
  let sends = 0;
  MM.api.post = () => { sends += 1; return Promise.reject(refusal(error)); };
  $('new-username').value = name;
  $('new-user-password').value = password;
  $('btn-submit-add-user').focus();
  revealed.length = 0;
  fire($('add-user-form'), 'submit');
  $('btn-submit-add-user').getBoundingClientRect = box(top);
  if (focus !== 'btn-submit-add-user') $(focus).focus();
  const sent = revealed.length;
  await settle();
  const texts = () => ['new-username-why', 'new-user-password-why', 'create-user-result'].filter((id) => $(id) && $(id).textContent);
  const out = { said: texts(), words: texts().map((id) => $(id).textContent).join(), focused: doc.activeElement.id || 'body', last: revealed.slice(-2), late: revealed.slice(sent), sends, kept: $('new-user-password').value === password, red: doc.querySelectorAll('.pwd-rule').filter((r) => r.classList.contains('is-bad')).map((r) => r.getAttribute('data-rule')) };
  type('new-username', name + 'x');
  type('new-user-password', password + 'x');
  return { ...out, typed: texts() };
}

// The add form with the password dialog cancelled, then with the answer still to come.
async function cancel() {
  const { $, fire, focused } = page({ reauth: (f, doc) => cancelled(doc) });
  $('new-username').value = 'riley';
  $('new-user-password').value = GOOD;
  $('btn-submit-add-user').focus();
  fire($('add-user-form'), 'submit');
  await settle();
  return { focused: focused(), said: $('create-user-result').textContent };
}

async function sending() {
  const { doc, $, MM, fire, focused } = page();
  let posts = 0;
  MM.api.post = () => { posts += 1; return new Promise(() => {}); };
  $('new-username').value = 'riley';
  $('new-user-password').value = GOOD;
  fire($('btn-add-user'), 'click');
  $('btn-submit-add-user').focus();
  ['submit', 'submit', 'input'].forEach((type) => fire($('add-user-form'), type));
  fire($('btn-add-user'), 'click');
  fire($('btn-cancel-add-user'), 'click');
  await settle();
  return { posts, focused: focused(), open: !$('add-user-fold').hidden, said: doc.querySelector('.pwd-said').textContent };
}

// A user is made: what is drawn and said, then the form gone, empty, with the row in the list.
async function made() {
  const timers = [];
  const { doc, $, MM, fire, focused } = page({ timers });
  const posts = [];
  MM.api.post = (url, body) => { posts.push([url, body]); return Promise.resolve({ ok: true }); };
  MM.api.get = () => Promise.resolve({ current: 'admin', users: [row] });
  fire($('btn-add-user'), 'click');
  $('new-username').value = ' <b>vic ';
  $('new-user-password').value = GOOD;
  $('btn-submit-add-user').focus();
  $('add-user-form').reset = () => ['new-username', 'new-user-password'].forEach((id) => { $(id).value = ''; });
  fire($('add-user-form'), 'submit');
  await settle();
  const during = { drawn: doc.querySelector('.pwd-done-s').textContent, live: $('add-user-live').textContent, open: !$('add-user-fold').hidden, left: [$('new-user-password').value] };
  for (let i = 0; i < 6; i += 1) { timers.splice(0).forEach((fn) => fn()); await settle(); }
  return { posts, during, after: { open: !$('add-user-fold').hidden, drawn: Boolean(doc.querySelector('.pwd-done')), left: [$('new-username').value, $('new-user-password').value], rows: doc.querySelectorAll('.usr-row').length, focused: focused(), busy: $('add-user-form').getAttribute('aria-busy') } };
}

// The form's script did not load: the list of users stands.
async function without() {
  const { doc, $ } = page({ users: [row], scripts: [] });
  await settle();
  return { rows: doc.querySelectorAll('.usr-row').length, add: $('btn-add-user').disabled };
}

// Escape folds it away, empty, and a form on its way stays.
async function escape() {
  const { $, fire, focused } = page();
  fire($('btn-add-user'), 'click');
  $('new-user-password').value = 'typed';
  $('add-user-form').reset = () => { $('new-user-password').value = ''; };
  $('add-user-form').listeners.keydown.forEach((fn) => fn({ key: 'Escape' }));
  await settle();
  return { open: !$('add-user-fold').hidden, left: $('new-user-password').value, focused: focused() };
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

const api = (status, issues, rules) => ({ status, issues, rules });

(async () => {
  const out = {
    shortName: await refuse({ name: 'ab' }),
    shortPassword: await refuse({ password: 'short' }),
    weakPassword: await refuse({ error: api(400, ['Add a digit'], ['common']) }),
    unnamedRule: await refuse({ error: api(400, ['Add a digit', 'And a hat']) }),
    usernameInIt: await refuse({ password: 'riley was Here 1' }),
    takenName: await refuse({ error: api(409) }),
    limit: await refuse({ error: api(429) }),
    offline: await refuse({ error: api(0) }),
    serverError: await refuse({ error: api(500) }),
    takenElsewhere: await refuse({ error: api(409), focus: 'elsewhere' }),
    takenScrolled: await refuse({ error: api(409), top: 700 }),
    limitElsewhere: await refuse({ error: api(429), focus: 'elsewhere' }),
  };
  const rows = { role: await places('role'), delete: await places('delete'), email: await places('email') };
  process.stdout.write(JSON.stringify({ add: out, cancel: await cancel(), sending: await sending(), made: await made(), escape: await escape(), without: await without(), rows }));
  process.exit(0);
})();
