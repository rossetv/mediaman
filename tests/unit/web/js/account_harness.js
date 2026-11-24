// Runs the real account.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node account_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const NOW = Date.UTC(2026, 5, 1, 12);
const at = (hours) => new Date(NOW + hours * 3600e3).toISOString();
const session = (expires) => ({ created_at: at(-48), last_used_at: at(-1), expires_at: at(expires) });

function page(api) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.body.appendChild(build(doc, ['main', {},
    ['form', { id: 'account-email-form' },
      ['input', { id: 'account-email' }], ['button', { id: 'account-submit-email' }], ['span', { id: 'account-email-result' }]],
    ['ul', { id: 'account-sessions' }, ['li', {}, 'Loading']],
    ['button', { id: 'account-revoke-others' }], ['span', { id: 'account-revoke-result' }]]));
  const $ = (id) => doc.getElementById(id);
  const MM = { api: { messageOf: (e, d) => e.message || d, ...api }, reauth: { run: (f) => f() } };
  class FixedDate extends Date { static now() { return NOW; } }
  const ctx = vm.createContext({ document: doc, MM, window: { MM }, Date: FixedDate });
  vm.runInContext(fs.readFileSync(path.join(JS, 'account.js'), 'utf8'), ctx);
  return {
    $,
    fire: (id, type) => $(id).listeners[type].forEach((fn) => fn({ preventDefault() {} })),
    rows: () => $('account-sessions').children.map((li) => li.textContent),
    say: (id) => [$(id).className, $(id).textContent],
  };
}

const scenarios = {
  async sessions() {
    let list = [session(5), session(9), session(-1)];
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    const first = { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, text: p.rows()[0] };
    p.fire('account-revoke-others', 'click');
    await settle();
    return { first, after: { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, say: p.say('account-revoke-result') } };
  },
  async sessionsFail() {
    const p = page({ get: () => Promise.reject(new Error('x')), post: () => Promise.reject(new Error('No luck')) });
    await settle();
    const first = { rows: p.rows(), disabled: p.$('account-revoke-others').disabled };
    p.fire('account-revoke-others', 'click');
    await settle();
    return { first, after: { disabled: p.$('account-revoke-others').disabled, say: p.say('account-revoke-result') } };
  },
  async email() {
    const sent = [];
    let answer = () => Promise.resolve({ ok: true });
    const p = page({
      get: () => Promise.resolve({ sessions: [] }),
      patch: (url, body) => { sent.push([url, body]); return answer(); },
    });
    const save = async (value) => {
      p.$('account-email').value = value;
      p.fire('account-email-form', 'submit');
      p.fire('account-email-form', 'submit');
      await settle();
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
    return { sent, saved, typed, removed, refused, cancelled };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
