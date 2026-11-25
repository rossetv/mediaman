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

// What was scrolled into view, and how.
const revealed = [];
Node.prototype.scrollIntoView = function (how) { revealed.push([this.id, how && how.block]); };

// On screen, between the page's bars, unless a scenario says otherwise.
Node.prototype.getBoundingClientRect = () => ({ top: 300, bottom: 320 });

function page(api) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.documentElement.clientHeight = 600;
  doc.body.appendChild(build(doc, ['main', { id: 'main-content' },
    ['form', { id: 'account-password-form' },
      ...['old', 'new', 'confirm'].flatMap((f) => [['input', { id: 'account-' + f + '-password' }], ['span', { id: 'account-' + f + '-password-why', class: 'fld-why' }]]),
      ['button', { id: 'account-submit-password' }], ['span', { id: 'account-password-result' }]],
    ['form', { id: 'account-email-form' },
      ['input', { id: 'account-email' }], ['button', { id: 'account-submit-email' }], ['span', { id: 'account-email-result' }]],
    ['ul', { id: 'account-sessions' }, ['li', {}, 'Loading']],
    ['div', {},
      ['button', { id: 'account-revoke-others' }], ['span', { id: 'account-revoke-why', hidden: '' }], ['span', { id: 'account-revoke-result' }]]]));
  const $ = (id) => doc.getElementById(id);
  $('account-password-form').reset = () => {};
  const MM = { api: { messageOf: (e, d) => (e instanceof Error && e.message) || d, ...api }, reauth: { run: (f) => f() } };
  class FixedDate extends Date { static now() { return NOW; } }
  // A top bar of 64px and a bottom bar of 72px.
  const getComputedStyle = () => ({ scrollPaddingTop: '64px', scrollPaddingBottom: '72px' });
  const ctx = vm.createContext({ document: doc, MM, window: { MM }, Date: FixedDate, getComputedStyle });
  ['core/dom.js', 'account.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
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
    const first = { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, text: p.rows()[0], why: !p.$('account-revoke-why').hidden };
    p.press('account-revoke-others');
    p.press('account-revoke-others');
    const whileSending = p.focused();
    await settle();
    return { first, posts, whileSending, after: { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, focused: p.focused(), say: p.say('account-revoke-result'), why: !p.$('account-revoke-why').hidden } };
  },
  async thisDevice() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9), { ...session(5), current: true }] }) });
    await settle();
    return p.rows();
  },
  async onlySession() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9)] }) });
    await settle();
    return { disabled: p.$('account-revoke-others').disabled, why: !p.$('account-revoke-why').hidden };
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
    p.fire('account-password-form', 'input');
    const working = p.say('account-password-result');
    await settle();
    const out = { posts, whileSending, working, focused: p.focused(), rows: p.rows().length, say: p.say('account-password-result') };
    out.revealed = revealed.splice(0);
    p.$('account-new-password').value = 'short';
    p.fire('account-password-form', 'submit');
    out.revealedRefusal = revealed.splice(0);
    p.fire('account-password-form', 'input');
    return { ...out, revealedEmptied: revealed.splice(0) };
  },
  // for each form, what an answer brings into view when the reader is still there (with the keyboard on what is around
  // the button too, as Safari leaves a clicked one), has scrolled its status out of view or under a bar, or has the keyboard in something else
  async lateAnswer() {
    const ids = { email: 'account-submit-email', password: 'account-submit-password', sessions: 'account-revoke-others' };
    const results = { email: 'account-email-result', password: 'account-password-result', sessions: 'account-revoke-result' };
    const late = async (which, refused, { top = 300, focus } = {}) => {
      let give;
      const pending = () => new Promise((resolve, reject) => { give = refused ? () => reject(Object.assign(new Error('No'), { error: 'wrong_password' })) : () => resolve({ ok: true }); });
      const p = page({ get: () => Promise.resolve({ sessions: [session(5), session(9)] }), patch: pending, post: pending });
      await settle();
      p.$('account-email').value = 'vic@example.com';
      ['old', 'new', 'confirm'].forEach((f) => { p.$('account-' + f + '-password').value = 'a-long-enough-password'; });
      if (which === 'sessions') p.press(ids.sessions);
      else { p.$(ids[which]).focus(); p.fire('account-' + which + '-form', 'submit'); }
      const onPress = revealed.splice(0).map(([id]) => id).join() === results[which];
      p.$(results[which]).getBoundingClientRect = () => ({ top, bottom: top + 20 });
      if (focus === 'around') p.$('main-content').focus();
      else if (focus) p.$(which === 'email' ? 'account-old-password' : 'account-email').focus();
      give();
      await settle();
      const field = refused && { email: 'account-email', password: 'account-old-password' }[which];
      const shown = field ? [field, which === 'email' ? results[which] : field + '-why'].join() : results[which];
      const answered = revealed.splice(0).map(([id]) => id).join();
      const moved = answered === '' ? false : answered === shown || answered;
      return [onPress, moved, (field && p.focused() === field) || false, Boolean(p.$(field === 'account-old-password' ? field + '-why' : results[which]).textContent)];
    };
    const each = async (which, refused) => ({
      there: await late(which, refused),
      around: await late(which, refused, { focus: 'around' }),
      below: await late(which, refused, { top: 700 }),
      above: await late(which, refused, { top: -300 }),
      underTheBottomBar: await late(which, refused, { top: 540 }),
      underTheTopBar: await late(which, refused, { top: 20 }),
      elsewhere: await late(which, refused, { focus: 'elsewhere' }),
    });
    return {
      email: await each('email'), password: await each('password'), sessions: await each('sessions'),
      sessionsRefused: await each('sessions', true),
      emailRefused: await each('email', true), passwordRefused: await each('password', true),
    };
  },
  // where each refusal of a new password is said, and where the cursor goes
  async passwordRefusals() {
    const refuse = async (fill, error, { without, again } = {}) => {
      const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.reject(error) });
      await settle();
      Object.entries(fill).forEach(([f, v]) => { p.$('account-' + f + '-password').value = v; });
      p.$('account-submit-password').focus();
      if (without) p.$(without).remove();
      if (again) {
        p.fire('account-password-form', 'submit');
        await settle();
        p.$('account-old-password').value = '';
      }
      revealed.splice(0);
      p.fire('account-password-form', 'submit');
      await settle();
      const said = () => ['account-old-password-why', 'account-new-password-why', 'account-confirm-password-why', 'account-password-result'].filter((id) => p.$(id) && p.$(id).textContent);
      const out = { said: said(), words: said().map((id) => p.$(id).textContent).join(), focused: p.focused(), last: revealed.splice(0).slice(-2).map(([id]) => id) };
      p.fire('account-password-form', 'input');
      return { ...out, typed: said() };
    };
    const failure = (error, issues) => Object.assign(new Error('No'), { error, issues });
    const good = { old: 'x', new: 'a-long-enough-password', confirm: 'a-long-enough-password' };
    return {
      old: await refuse({ ...good, old: '' }), new: await refuse({ ...good, new: 'short' }), confirm: await refuse({ ...good, confirm: 'other' }),
      wrong: await refuse(good, failure('wrong_password')),
      weak: await refuse(good, failure('weak_password', ['a digit'])),
      same: await refuse(good, failure('same_password')),
      limit: await refuse(good, failure('too_many_requests')),
      // the status still says the limit, and the field was emptied by what fires no input event
      afterLimit: await refuse(good, failure('too_many_requests'), { again: true }),
      noPlace: await refuse({ ...good, old: '' }, null, { without: 'account-old-password-why' }),
    };
  },
  // how the focus goes to the list of sign-ins once the others are signed out
  async focusOnTheList() {
    let list = [session(5), session(9)];
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    let how = null;
    p.$('account-sessions').focus = (opts) => { how = opts || {}; };
    p.press('account-revoke-others');
    await settle();
    return how;
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
