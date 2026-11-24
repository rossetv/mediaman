// Runs the real account.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node account_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const NOW = Date.UTC(2026, 5, 1, 12);
const at = (hours) => new Date(NOW + hours * 3600e3).toISOString();
const session = (expires) => ({ created_at: at(-48), last_used_at: at(-1), expires_at: at(expires) });

// A focused control that is disabled loses the focus, as in a browser.
Object.defineProperty(Node.prototype, 'disabled', {
  get() { return Boolean(this._disabled); },
  set(on) {
    this._disabled = on;
    if (on && this.ownerDocument.activeElement === this) this.blur();
  },
});

function page(api) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.body.appendChild(build(doc, ['main', {},
    ['form', { id: 'account-password-form' },
      ['input', { id: 'account-old-password' }], ['input', { id: 'account-new-password' }], ['input', { id: 'account-confirm-password' }],
      ['button', { id: 'account-submit-password' }], ['span', { id: 'account-password-result' }]],
    ['form', { id: 'account-email-form' },
      ['input', { id: 'account-email' }], ['button', { id: 'account-submit-email' }], ['span', { id: 'account-email-result' }]],
    ['ul', { id: 'account-sessions' }, ['li', {}, 'Loading']],
    ['button', { id: 'account-revoke-others' }], ['span', { id: 'account-revoke-result' }]]));
  const $ = (id) => doc.getElementById(id);
  $('account-password-form').reset = () => {};
  const MM = { api: { messageOf: (e, d) => e.message || d, ...api }, reauth: { run: (f) => f() } };
  class FixedDate extends Date { static now() { return NOW; } }
  const ctx = vm.createContext({ document: doc, MM, window: { MM }, Date: FixedDate });
  vm.runInContext(fs.readFileSync(path.join(JS, 'account.js'), 'utf8'), ctx);
  return {
    $,
    fire: (id, type) => $(id).listeners[type].forEach((fn) => fn({ preventDefault() {} })),
    focused: () => doc.activeElement.id || 'body',
    press: (id) => {
      const button = $(id);
      button.focus();
      (button.disabled ? [] : button.listeners.click || []).forEach((fn) => fn({ preventDefault() {} }));
    },
    rows: () => $('account-sessions').children.map((li) => li.textContent),
    say: (id) => [$(id).className, $(id).textContent],
  };
}

const scenarios = {
  async sessions() {
    let list = [session(5), session(9), session(-1)];
    let posts = 0;
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { posts += 1; list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    const first = { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, text: p.rows()[0] };
    p.press('account-revoke-others');
    p.press('account-revoke-others');
    const whileSending = p.focused();
    await settle();
    return { first, posts, whileSending, after: { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, focused: p.focused(), say: p.say('account-revoke-result') } };
  },
  async sessionsFail() {
    const p = page({ get: () => Promise.reject(new Error('x')), post: () => Promise.reject(new Error('No luck')) });
    await settle();
    const first = { rows: p.rows(), disabled: p.$('account-revoke-others').disabled };
    p.press('account-revoke-others');
    await settle();
    return { first, after: { focused: p.focused(), disabled: p.$('account-revoke-others').disabled, say: p.say('account-revoke-result') } };
  },
  async password() {
    let list = [session(5), session(9)];
    let posts = 0;
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { posts += 1; list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    ['old', 'new', 'confirm'].forEach((f) => { p.$('account-' + f + '-password').value = 'a-long-enough-password'; });
    p.$('account-submit-password').focus();
    p.fire('account-password-form', 'submit');
    p.fire('account-password-form', 'submit');
    const whileSending = p.focused();
    await settle();
    return { posts, whileSending, focused: p.focused(), rows: p.rows().length, say: p.say('account-password-result') };
  },
  async strength() {
    const doc = makeDocument();
    doc.body.appendChild(build(doc, ['form', { id: 'form' },
      ['input', { id: 'pw', 'data-strength': 'pw-strength' }],
      ['span', { id: 'pw-strength-meter' }], ['span', { id: 'pw-strength-label' }],
      ['input', { id: 'again', 'data-match': 'pw' }], ['span', { id: 'again-match' }]]));
    const $ = (id) => doc.getElementById(id);
    const timers = [];
    ['pw', 'again'].forEach((id) => { $(id).value = ''; $(id).form = $('form'); });
    vm.runInContext(fs.readFileSync(path.join(JS, 'password-strength.js'), 'utf8'),
      vm.createContext({ document: doc, setTimeout: (fn) => timers.push(fn) }));
    const type = (id, value) => {
      $(id).value = value;
      $(id).listeners.input.forEach((fn) => fn());
      return { meter: $('pw-strength-meter').style.width, tone: $('pw-strength-label').className, match: $('again-match').className, says: $('again-match').textContent };
    };
    const out = {
      weak: type('pw', 'abc'), ok: type('pw', 'abcdefghijK1lmno'), strong: type('pw', 'abcdefghijK1-xyz'),
      differs: type('again', 'abc'), matches: type('again', 'abcdefghijK1-xyz'),
      changed: type('pw', 'abcdefghijK1-xyzz'), emptied: type('again', ''),
    };
    type('again', 'x');
    $('form').listeners.reset.forEach((fn) => fn());
    ['pw', 'again'].forEach((id) => { $(id).value = ''; });
    timers.forEach((fn) => fn());
    out.reset = { meter: $('pw-strength-meter').style.width, tone: $('pw-strength-label').className, match: $('again-match').className };
    return out;
  },
  async email() {
    const sent = [];
    let answer = () => Promise.resolve({ ok: true });
    const p = page({
      get: () => Promise.resolve({ sessions: [] }),
      patch: (url, body) => { sent.push([url, body]); return answer(); },
    });
    const focus = [];
    const save = async (value) => {
      p.$('account-email').value = value;
      p.$('account-submit-email').focus();
      p.fire('account-email-form', 'submit');
      p.fire('account-email-form', 'submit');
      focus.push(p.focused());
      await settle();
      focus.push(p.focused());
      return p.say('account-email-result');
    };
    const saved = await save(' vic@example.com ');
    p.fire('account-email-form', 'input');
    const typed = p.say('account-email-result')[1];
    const removed = await save('');
    answer = () => Promise.reject(new Error('Not an email address'));
    const refused = await save('nope');
    answer = () => Promise.reject({ error: 'reauth_cancelled' });
    const cancelled = await save('vic@example.com');
    return { sent, focus, saved, typed, removed, refused, cancelled };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
