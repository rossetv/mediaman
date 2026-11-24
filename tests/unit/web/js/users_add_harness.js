// Runs the real settings/users.js add form against the fake DOM and prints where each refusal is said.
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
// A focused control that is disabled loses the focus, as in a browser.
Object.defineProperty(Node.prototype, 'disabled', {
  get() { return Boolean(this._disabled); },
  set(on) {
    this._disabled = on;
    if (on && this.ownerDocument.activeElement === this) this.blur();
  },
});

async function refuse({ name = 'riley', password = 'a-long-enough-password', error, focus = 'btn-submit-add-user' }) {
  const doc = makeDocument();
  doc.documentElement.clientHeight = 600;
  doc.body.appendChild(build(doc, ['main', {},
    ['button', { id: 'btn-add-user' }], ['input', { id: 'elsewhere' }],
    ['form', { id: 'add-user-form' },
      ['input', { id: 'new-username' }], ['span', { id: 'new-username-why', class: 'fld-why' }],
      ['input', { id: 'new-user-password' }], ['span', { id: 'new-user-password-why', class: 'fld-why' }],
      ['button', { id: 'btn-submit-add-user' }], ['span', { id: 'create-user-result' }]]]));
  const $ = (id) => doc.getElementById(id);
  const MM = {
    reauth: { run: (f) => f() },
  };
  const ctx = vm.createContext({ document: doc, MM, getComputedStyle: () => ({}), setTimeout, matchMedia: () => ({ matches: true }) });
  ctx.window = ctx;
  ['core/api.js', 'core/dom.js', 'settings/rows.js', 'settings/users.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  MM.api.get = () => Promise.resolve({ users: [] });
  MM.api.post = () => Promise.reject(new MM.api.APIError('x', 'No', error.status, error.issues, { message: 'No' }));
  MM.settings.users.init();
  $('new-username').value = name;
  $('new-user-password').value = password;
  $('btn-submit-add-user').focus();
  revealed.length = 0;
  $('add-user-form').listeners.submit.forEach((fn) => fn({ preventDefault() {} }));
  if (focus !== 'btn-submit-add-user') $(focus).focus();
  await settle();
  const texts = () => ['new-username-why', 'new-user-password-why', 'create-user-result'].filter((id) => $(id).textContent);
  const out = { said: texts(), words: texts().map((id) => $(id).textContent).join(), focused: doc.activeElement.id || 'body', last: revealed.slice(-2) };
  $('add-user-form').listeners.input.forEach((fn) => fn({}));
  return { ...out, typed: texts() };
}

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
    limitElsewhere: await refuse({ error: api(429), focus: 'elsewhere' }),
  };
  process.stdout.write(JSON.stringify(out));
})();
