// Runs the real account.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node account_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');
const { parts, prepare } = require('./password_form_dom.js');

prepare(Node);

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const OLD = 'the-old-Password-1';
const NEW = 'Copper lantern 41';
const NOW = Date.UTC(2026, 5, 1, 12);
const at = (hours) => new Date(NOW + hours * 3600e3).toISOString();
let ids = 0;
const session = (expires, more) => ({ public_id: 'id' + (ids += 1), created_at: at(-48), last_used_at: at(-1), expires_at: at(expires), ...more });

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

// `ai` adds the AI section, `tvs` the lists of TVs of a role, and `yes` answers a question.
function page(api, { ai = false, yes = true, tvs = '', changed = '', scripts = ['password-form.js'] } = {}) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.documentElement.clientHeight = 600;
  doc.body.appendChild(build(doc, ['main', { id: 'main-content' },
    ['div', { class: 'me-line', id: 'line' }, ['div', { id: 'account-password-sub' }, ...(changed ? [['time', { id: 'account-password-changed', datetime: changed }, 'long ago']] : []), ['span', {}, 'It signs out the others.']],
      ['div', { class: 'me-end' }, ['button', { id: 'account-open-password', 'aria-expanded': 'false' }]]],
    ['div', { id: 'account-password-fold', class: 'setg-fold is-shut' }, ['div', { id: 'stage' },
      ['form', { id: 'account-password-form' }, ...parts({ old: 'account-old-password', fresh: 'account-new-password', submit: 'account-submit-password',
        cancel: 'account-cancel-password', result: 'account-password-result', words: 'Save new password' })]]],
    ['span', { id: 'account-password-live' }],
    ['form', { id: 'account-email-form' },
      ['input', { id: 'account-email' }], ['div', { id: 'account-email-fold', class: 'setg-fold is-shut' }, ['button', { id: 'account-cancel-email' }], ['button', { id: 'account-submit-email' }, 'Save'], ['span', { id: 'account-email-result' }]]],
    ['ul', { id: 'account-sessions' }, ['li', {}, 'Loading']],
    ['div', {},
      ['button', { id: 'account-revoke-others' }], ['span', { id: 'account-revoke-why', hidden: '' }], ['span', { id: 'account-revoke-result' }]]]));
  if (ai) {
    doc.body.appendChild(build(doc, ['section', { id: 'account-ai' }, ['button', { id: 'account-history', class: 'tog' }], ['button', { id: 'account-clear-searches' }, 'Clear']]));
    doc.getElementById('account-ai').hidden = true;
  }
  if (tvs) {
    doc.body.appendChild(build(doc, ['section', { id: 'account-tvs', 'data-me': 'amy' }, ['ul', { class: 'dev-list' }]]));
    doc.getElementById('account-tvs').hidden = true;
  }
  if (tvs === 'admin') {
    doc.body.appendChild(build(doc, ['section', { id: 'account-tvs-all' }, ['ul', { class: 'dev-list' }]]));
    doc.getElementById('account-tvs-all').hidden = true;
  }
  const $ = (id) => doc.getElementById(id);
  let resets = 0;
  $('account-password-form').reset = () => { resets += 1; ['old', 'new'].forEach((f) => { $('account-' + f + '-password').value = ''; }); };
  ['account-password-fold', 'account-email-fold'].forEach((id) => { $(id).hidden = true; });
  $('account-email').value = $('account-email').defaultValue = 'was@example.com';
  ['old', 'new'].forEach((f) => { $('account-' + f + '-password').value = ''; });
  const MM = { api: { messageOf: (e, d) => (e instanceof Error && e.message) || d, ...api }, reauth: { run: (f, words) => { asked.push(words); return f(); } } };
  const dropped = [];
  MM.tvRemote = { drop: (id) => dropped.push(id) };
  const asked = [];
  const toasts = [];
  const UIFeedback = { toast: (text) => toasts.push(text), error: (text) => toasts.push('error: ' + text), confirm: (words) => { asked.push(words); return Promise.resolve(yes); } };
  const timers = [];
  const setTimeout = (fn) => timers.push(fn);
  class FixedDate extends Date { static now() { return NOW; } }
  // A top bar of 64px and a bottom bar of 72px.
  const getComputedStyle = () => ({ scrollPaddingTop: '64px', scrollPaddingBottom: '72px' });
  const told = [];
  const session = { 'mm.watch.ask': 'a question', other: 'x' };
  doc.dispatchEvent = (e) => { told.push(e.type); };
  const ctx = vm.createContext({ document: doc, MM, UIFeedback, setTimeout, clearTimeout() {}, TextEncoder, window: page.window = { MM, innerWidth: 390, resized: [], addEventListener(type, fn) { if (type === 'resize') this.resized.push(fn); }, matchMedia: () => ({ matches: true }), sessionStorage: { removeItem: (k) => { delete session[k]; } } }, Date: FixedDate, Event, getComputedStyle });
  ['core/dom.js', 'settings/rows.js', ...scripts, 'account.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  return {
    $, dropped, told, rows_: () => MM.settings.rows, asked, toasts, session,
    wait: () => timers.splice(0).forEach((fn) => fn()),
    resets: () => resets,
    key: (id, key) => $(id).listeners.keydown.forEach((fn) => fn({ key })),
    button: (n) => $('account-sessions').children[n].querySelector('button'),
    // a timer, and what it sets going, until nothing is left
    async rest() {
      for (let i = 0; i < 8; i += 1) { timers.splice(0).forEach((fn) => fn()); await settle(); }
    },
    fill: (old, fresh) => { $('account-old-password').value = old; $('account-new-password').value = fresh; },
    fire: (id, type) => $(id).listeners[type].forEach((fn) => fn({ target: $(id), preventDefault() {}, stopPropagation() {} })),
    type(id, value) { $(id).value = value; $('account-password-form').listeners.input.forEach((fn) => fn({ target: $(id) })); },
    says: () => ['account-old-password-why', 'account-new-password-why', 'account-password-result'].filter((id) => $(id).textContent),
    line: () => doc.querySelector('.pwd-line').lastElementChild.textContent,
    sub: () => $('account-password-sub').children.filter((n) => !n.hidden).map((n) => n.textContent).join(' '),
    focused: () => doc.activeElement.id || 'body',
    active: () => doc.activeElement,
    press: (id) => {
      const button = $(id);
      button.focus();
      (button.disabled ? [] : button.listeners.click || []).forEach((fn) => fn({ preventDefault() {} }));
    },
    rows: () => $('account-sessions').children.map((li) => li.textContent),
    say: (id) => [$(id).className, $(id).textContent],
  };
}

const tvOf = (id, name, more) => ({ id, name, on: false, paired_at: '2026-03-12T09:00:00Z', last_seen_at: at(-30), ...more });
// The TV lists' answers by address; the rest of the page gets none.
const tvApi = (mine, all, more) => ({
  get: (url) => (url === '/api/tvs' ? Promise.resolve({ tvs: mine }) : url === '/api/tvs/all' ? Promise.resolve({ tvs: all }) : Promise.resolve({ sessions: [] })),
  ...more,
});
const tvRows = (p, id = 'account-tvs') => p.$(id).querySelector('.dev-list').children;
const buttonOf = (row, words) => row.querySelectorAll('button').find((b) => b.textContent === words);
const push = (button) => button.listeners.click.forEach((fn) => fn({}));

const scenarios = {
  async tvs() {
    const p = page(tvApi([tvOf(1, 'Living room', { on: true }), tvOf(2, '<b>Den'), tvOf(3, 'New', { last_seen_at: at(-400) }), tvOf(4, 'Kitchen TV', { kind: 'older' })], []), { tvs: 'viewer' });
    await settle();
    const none = page(tvApi([], []), { tvs: 'viewer' });
    await settle();
    return { hidden: p.$('account-tvs').hidden, rows: tvRows(p).map((r) => r.querySelector('.me-txt').children.map((c) => c.textContent)), none: none.$('account-tvs').hidden };
  },
  async tvsOfAll() {
    const all = [tvOf(1, 'Mine', { owner: 'amy', kind: 'page' }), tvOf(2, 'Theirs', { owner: 'bob', kind: 'page' }), tvOf(3, 'Older', { owner: null, kind: 'older' })];
    const p = page(tvApi([tvOf(1, 'Mine')], all), { tvs: 'admin', yes: false });
    await settle();
    push(buttonOf(tvRows(p, 'account-tvs-all')[0], 'Unpair'));
    await settle();
    return { rows: tvRows(p, 'account-tvs-all').map((r) => r.textContent), asked: p.asked, left: tvRows(p, 'account-tvs-all').length };
  },
  async tvRename() {
    const sent = [];
    let answer = () => Promise.reject({ error: 'x' });
    const p = page(tvApi([tvOf(1, 'TV')], [], { patch: (url, body) => { sent.push([url, body]); return answer(body); } }), { tvs: 'viewer' });
    await settle();
    Node.prototype.select = () => {};
    const row = tvRows(p)[0];
    push(buttonOf(row, 'Rename'));
    const form = row.querySelector('form');
    const input = row.querySelector('input');
    const drawn = form.childNodes.map((n) => n.textContent || n.tagName.toLowerCase());
    const submit = () => form.listeners.submit.forEach((fn) => fn({ preventDefault() {} }));
    input.value = '  ';
    submit();
    const empty = [sent.length, input.getAttribute('aria-invalid')];
    input.value = ' Den ';
    submit();
    submit();
    await settle();
    const failed = [sent.length, p.toasts.splice(0), Boolean(row.querySelector('form'))];
    answer = (body) => Promise.resolve({ tv: { id: 1, name: body.name } });
    submit();
    await settle();
    return { drawn, empty, failed, sent: sent[1], name: row.querySelector('.me-lbl').textContent, label: buttonOf(row, 'Rename').getAttribute('aria-label'), toasts: p.toasts };
  },
  async tvUnpair() {
    const sent = [];
    let answer = () => Promise.reject({ status: 500 });
    const p = page(tvApi([tvOf(1, 'A'), tvOf(2, 'B')], [], { delete: (url) => { sent.push(url); return answer(); } }), { tvs: 'viewer' });
    await settle();
    const press = async () => {
      push(buttonOf(tvRows(p)[0], 'Unpair'));
      await settle();
      p.wait();
    };
    await press();
    const failed = [tvRows(p).length, p.toasts.splice(0)];
    const kept = p.dropped.length;
    answer = () => Promise.resolve({ ok: true });
    await press();
    const one = [tvRows(p).length, p.$('account-tvs').hidden, p.toasts.splice(0)];
    // already unpaired by someone else
    answer = () => Promise.reject({ status: 404 });
    await press();
    return { failed, one, sent, last: [tvRows(p).length, p.$('account-tvs').hidden], asked: p.asked.length, kept, dropped: p.dropped };
  },
  async sessions() {
    const here = session(9, { current: true });
    let list = [session(5), here, session(-1)];
    let posts = 0;
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { posts += 1; list = [{ ...here, public_id: 'fresh' }]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    const first = { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, text: p.rows()[1], why: !p.$('account-revoke-why').hidden };
    p.press('account-revoke-others');
    p.press('account-revoke-others');
    const whileSending = p.focused();
    await settle();
    return { first, posts, whileSending, after: { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, focused: p.focused(), say: p.say('account-revoke-result'), why: !p.$('account-revoke-why').hidden } };
  },
  async thisDevice() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9), session(5, { current: true, device_label: 'Chrome on macOS' }), session(5, { created_at: '2025-11-02T20:20:00Z', device_label: '<b>Safari on iPhone' })] }) });
    await settle();
    return p.rows();
  },
  // one device signed out: refused first, then gone
  async signOutOne() {
    const list = [session(9, { current: true }), session(5, { device_label: 'Safari on iPhone', public_id: 'a/b' }), session(6)];
    const sent = [];
    let answer = () => Promise.reject(Object.assign(new Error('No luck')));
    const p = page({ get: () => Promise.resolve({ sessions: list }), delete: (url) => { sent.push(url); return answer(); } });
    await settle();
    const own = p.button(0);
    const label = p.button(1).getAttribute('aria-label');
    const push = async () => {
      const button = p.button(1);
      button.focus();
      for (let i = 0; i < 2; i += 1) button.listeners.click.forEach((fn) => fn({}));
      const busy = [button.textContent, button.style.width, button.getAttribute('aria-label')];
      await settle();
      return busy;
    };
    const busy = await push();
    const labelAfter = p.button(1).getAttribute('aria-label');
    const refused = { rows: p.rows().length, says: p.button(1).textContent, toasts: p.toasts.splice(0), focusKept: p.$('account-sessions').children[1].contains(p.active()) };
    answer = () => Promise.reject({ error: 'reauth_cancelled' });
    await push();
    const cancelled = { rows: p.rows().length, toasts: p.toasts.splice(0) };
    answer = () => Promise.resolve({ ok: true });
    await push();
    const gone = { rows: p.rows().length, toasts: p.toasts.splice(0), onNext: p.button(1) === p.active(), why: !p.$('account-revoke-why').hidden };
    p.toasts.length = 0;
    await push();
    return { own: Boolean(own), label, labelAfter, busy, sent, words: p.asked[0], refused, cancelled, gone, last: { rows: p.rows().length, focused: p.focused(), disabled: p.$('account-revoke-others').disabled, why: !p.$('account-revoke-why').hidden } };
  },
  // the first list, asked before a new password, is answered after the one asked since
  async lateList() {
    const gives = [];
    const here = session(9, { current: true });
    const p = page({ get: () => new Promise((give, fail) => gives.push({ give, fail })), post: () => Promise.resolve({ ok: true }) });
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    await p.rest();
    gives[1].give({ sessions: [here] });
    await settle();
    gives[0].give({ sessions: [here, session(5), session(5)] });
    await settle();
    return p.rows().length;
  },
  // a list asked before a device was signed out still holds it
  async listBeforeSignOut() {
    const gives = [];
    const here = session(9, { current: true });
    const other = session(5);
    const p = page({
      get: () => new Promise((give) => gives.push(give)),
      post: () => Promise.resolve({ ok: true }), delete: () => Promise.resolve({ ok: true }),
    });
    gives[0]({ sessions: [here, other, session(5)] });
    await settle();
    p.press('account-revoke-others');
    await settle();
    p.button(1).listeners.click.forEach((fn) => fn({}));
    await settle();
    gives[1]({ sessions: [here, other] });
    await settle();
    return p.rows().length;
  },
  async passwordFold() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) });
    await settle();
    const state = () => [p.$('account-password-fold').hidden, p.$('account-open-password').getAttribute('aria-expanded'), p.focused()];
    const shut = state();
    p.press('account-open-password');
    const open = state();
    p.$('account-old-password').value = 'typed';
    p.press('account-cancel-password');
    const cancelled = state().concat(p.$('account-old-password').value);
    p.press('account-open-password');
    p.key('account-password-fold', 'Escape');
    return { shut, open, cancelled, escaped: state() };
  },
  async passwordRule() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) });
    await settle();
    const says = () => p.$('account-password-form').querySelector('.pwd-rule-w').textContent + p.$('account-password-form').querySelector('.pwd-rule-n').textContent;
    const out = [says()];
    p.type('account-new-password', 'abc');
    out.push(says());
    p.type('account-new-password', '');
    out.push(says());
    p.type('account-new-password', 'abcd');
    p.press('account-cancel-password');
    out.push(says());
    return out;
  },
  // when it was changed last, shut and open
  async passwordChanged() {
    const out = [];
    for (const changed of ['', '2026-03-14T09:26:53+00:00', '2025-03-14T09:26:53+00:00']) {
      const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { changed });
      await settle();
      const shut = p.sub();
      p.press('account-open-password');
      const open = p.sub();
      p.press('account-cancel-password');
      out.push([shut, open, p.sub() === shut]);
    }
    return out;
  },
  // what the line by the buttons says of so many other sign-ins and TVs; null lists did not load
  async devicesLine() {
    const says = async (others, tvs) => {
      const list = (n, make) => (n === null ? () => Promise.reject(new Error('x')) : () => Promise.resolve(make(n)));
      const answers = { '/api/users/sessions': list(others, (n) => ({ sessions: [session(9, { current: true })].concat(Array.from({ length: n }, () => session(5))) })), '/api/tvs': list(tvs, (n) => ({ tvs: Array.from({ length: n }, (_x, i) => tvOf(i + 1, 'TV')) })) };
      const p = page({ get: (url) => answers[url]() }, { tvs: 'viewer' });
      await settle();
      return p.line();
    };
    return [await says(0, 0), await says(1, 0), await says(2, 1), await says(0, 3), await says(null, 1), await says(1, null)];
  },
  // what is drawn and said once it is saved, and what the row keeps of it
  async passwordDone() {
    const out = [];
    for (const [others, tvs, answer] of [[2, 1, { signed_out: 2, unpaired: ['<b>Den'] }], [0, 0, { signed_out: 0, unpaired: [] }], [7, 3, { signed_out: 7, unpaired: ['a', 'b', 'c'] }], [1, 2, {}]]) {
      const api = tvApi(Array.from({ length: tvs }, (_x, i) => tvOf(i + 1, 'TV')), [], { post: () => Promise.resolve(answer) });
      const signIns = [session(9, { current: true, device_label: 'Safari on macOS' })].concat(Array.from({ length: others }, () => session(5, { device_label: 'Safari on iPhone' })));
      const get = (url) => (url === '/api/users/sessions' ? Promise.resolve({ sessions: signIns }) : api.get(url));
      const p = page({ ...api, get }, { tvs: 'viewer', changed: '2026-03-14T09:26:53+00:00' });
      await settle();
      p.press('account-open-password');
      p.fill(OLD, NEW);
      p.fire('account-password-form', 'submit');
      await settle();
      const done = p.$('stage').querySelector('.pwd-done');
      const row = done.querySelector('.pwd-dev-row');
      const during = { says: done.querySelector('.pwd-done-s').textContent, live: p.$('account-password-live').textContent, sub: p.sub(), icons: row ? row.querySelectorAll('use').map((u) => u.getAttribute('href').slice(3)).join(' ') : '', more: row && row.querySelector('.pwd-dev-n') ? row.querySelector('.pwd-dev-n').textContent : '' };
      await p.rest();
      out.push({ ...during, after: p.$('account-password-sub').textContent, faded: p.$('account-password-sub').children.filter((n) => n.tagName === 'SPAN' && !n.classList.contains('is-fresh')).map((n) => n.textContent), left: Boolean(p.$('stage').querySelector('.pwd-done')) });
    }
    return out;
  },
  async emailFold() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), patch: () => { throw new Error('sent'); } });
    await settle();
    const type = (value) => { p.$('account-email').value = value; p.fire('account-email-form', 'input'); return !p.$('account-email-fold').hidden; };
    const out = [!p.$('account-email-fold').hidden, type('new@example.com'), type(' was@example.com '), type('new')];
    p.fire('account-email-form', 'submit');
    const bad = [p.say('account-email-result'), p.$('account-email').getAttribute('aria-invalid'), p.focused()];
    p.key('account-email-form', 'Escape');
    return { out, bad, undone: [p.$('account-email').value, !p.$('account-email-fold').hidden, p.say('account-email-result')[1]] };
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
    const sent = [];
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: (_url, body) => { sent.push(body); list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    p.press('account-open-password');
    p.fill(OLD, NEW);
    p.$('account-submit-password').focus();
    p.fire('account-password-form', 'submit');
    p.fire('account-password-form', 'submit');
    const whileSending = p.focused();
    p.type('account-new-password', NEW);
    p.press('account-cancel-password');
    p.key('account-password-fold', 'Escape');
    const working = [p.$('account-password-form').querySelector('.pwd-said').textContent, p.$('account-submit-password').textContent, p.$('account-password-fold').hidden];
    const revealedSending = revealed.splice(0);
    await settle();
    // a cancel does not shut what was just saved
    p.press('account-cancel-password');
    const drawn = p.$('account-password-fold').hidden;
    await p.rest();
    const out = { sent, whileSending, working, drawn, focused: p.focused(), rows: p.rows().length, toasts: p.toasts, shut: p.$('account-password-fold').hidden, resets: p.resets(), left: [p.$('account-old-password').value, p.$('account-new-password').value], says: p.$('account-submit-password').textContent };
    p.press('account-open-password');
    revealed.splice(0);
    p.fill('x', 'short');
    p.fire('account-password-form', 'submit');
    out.revealedRefusal = revealed.splice(0);
    p.type('account-new-password', 'shorter');
    return { ...out, revealedSending, revealedEmptied: revealed.splice(0) };
  },
  // what is typed is out of the page once it is saved, and a press as it folds does not strand the form
  async passwordForgotten() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.press('account-open-password');
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    const left = [p.$('account-old-password').value, p.$('account-new-password').value];
    p.press('account-open-password');
    await p.rest();
    const form = p.$('account-password-form');
    const state = [p.$('account-password-fold').hidden, form.inert, form.getAttribute('aria-busy')];
    p.press('account-open-password');
    return { left, state, again: p.$('account-password-fold').hidden };
  },
  // the same press while the fold is closing, which takes a moment
  async passwordPressedAsItFolds() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.press('account-open-password');
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    const closing = [];
    p.rows_().close = (_panel, done) => closing.push(done);
    await p.rest();
    p.press('account-open-password');
    closing.forEach((done) => done());
    const form = p.$('account-password-form');
    return [closing.length, form.inert, form.getAttribute('aria-busy'), p.$('account-open-password').getAttribute('aria-expanded')];
  },
  // the row keeps its height at first, and then takes its own
  async passwordEased() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { changed: '2026-03-14T09:26:53+00:00' });
    await settle();
    const line = p.$('line');
    const set = [];
    let shown = '';
    Object.defineProperty(line, 'offsetHeight', { get: () => (p.$('account-open-password').classList.contains('is-gone') ? 77 : 111) });
    line.style = { get marginBottom() { return shown; }, set marginBottom(v) { shown = v; set.push(v); } };
    const sub = p.$('account-password-sub');
    line.querySelector('.me-end').offsetTop = 33;
    p.press('account-open-password');
    const open = [set.splice(0), sub.classList.contains('is-new'), line.querySelector('.me-end').style.top];
    p.press('account-cancel-password');
    return { open, shut: [set.splice(0), sub.classList.contains('is-new')] };
  },
  // the window is resized, by a phone's keyboard at first, while the form is open
  async passwordTurned() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { changed: '2026-03-14T09:26:53+00:00' });
    await settle();
    const sub = p.$('account-password-sub');
    sub.getBoundingClientRect = () => ({ width: 164.5 });
    const resize = (to) => { page.window.innerWidth = to; page.window.resized.forEach((fn) => fn()); return sub.style.maxWidth; };
    p.press('account-open-password');
    const out = { opened: sub.style.maxWidth, keyboard: resize(390), turned: resize(844), back: resize(390) };
    p.press('account-cancel-password');
    p.press('account-open-password');
    out.again = sub.style.maxWidth;
    p.press('account-cancel-password');
    return { ...out, shut: sub.style.maxWidth };
  },
  // the form's script did not load: the rest of the page stands
  async passwordWithout() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9)] }) }, { scripts: [] });
    await settle();
    p.$('account-email').value = 'new@example.com';
    p.fire('account-email-form', 'input');
    return { opener: p.$('account-open-password').disabled, rows: p.rows().length, email: !p.$('account-email-fold').hidden };
  },
  // one who went to another field while it was drawn keeps their place
  async passwordLeft() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.press('account-open-password');
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    p.$('account-email').focus();
    await p.rest();
    return [p.focused(), p.$('account-password-fold').hidden];
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
      if (which === 'password') p.press('account-open-password');
      p.$('account-email').value = 'vic@example.com';
      p.fire('account-email-form', 'input');
      revealed.splice(0);
      p.fill(OLD, NEW);
      if (which === 'sessions') p.press(ids.sessions);
      else { p.$(ids[which]).focus(); p.fire('account-' + which + '-form', 'submit'); }
      const onPress = revealed.splice(0).map(([id]) => id).join() === ids[which];
      p.$(which === 'sessions' ? results[which] : ids[which]).getBoundingClientRect = () => ({ top, bottom: top + 20 });
      if (focus === 'around') p.$('main-content').focus();
      else if (focus) p.$(which === 'email' ? 'account-old-password' : 'account-email').focus();
      give();
      await settle();
      await p.rest();
      const field = refused && { email: 'account-email', password: 'account-old-password' }[which];
      const shown = field ? [field, which === 'email' ? results[which] : field + '-why'].join() : results[which];
      const answered = revealed.splice(0).map(([id]) => id).join();
      const moved = answered === '' ? false : answered === shown || answered;
      return [onPress, moved, refused ? (field && p.focused() === field) || false : p.focused(), Boolean(p.$(field === 'account-old-password' ? field + '-why' : results[which]).textContent) || p.toasts.length > 0 || Boolean(p.$('account-password-live').textContent)];
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
    const refuse = async (fill, error, { again } = {}) => {
      const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.reject(error) });
      await settle();
      p.fill(fill.old, fill.new);
      p.$('account-submit-password').focus();
      if (again) {
        p.fire('account-password-form', 'submit');
        await settle();
        p.$('account-old-password').value = '';
      }
      revealed.splice(0);
      p.fire('account-password-form', 'submit');
      await settle();
      const said = p.says();
      const red = p.$('account-password-form').querySelectorAll('.pwd-rule').filter((r) => r.classList.contains('is-bad')).map((r) => r.getAttribute('data-rule'));
      const out = { said, red, words: said.map((id) => p.$(id).textContent).join(), focused: p.focused(), kept: p.$('account-new-password').value === fill.new, busy: p.$('account-password-form').getAttribute('aria-busy'), last: revealed.splice(0).slice(-2).map(([id]) => id) };
      p.type('account-old-password', 'typed');
      p.type('account-new-password', 'typed');
      return { ...out, typed: p.says() };
    };
    const failure = (error, issues, rules) => Object.assign(new Error('No'), { error, issues, data: { rules } });
    const good = { old: OLD, new: NEW };
    return {
      old: await refuse({ ...good, old: '' }), new: await refuse({ ...good, new: 'short' }), known: await refuse({ old: NEW, new: NEW }),
      wrong: await refuse(good, failure('wrong_password')),
      weak: await refuse(good, failure('weak_password', ['a digit'], ['common'])),
      same: await refuse(good, failure('same_password', undefined, ['same'])),
      limit: await refuse(good, failure('too_many_requests')),
      unknown: await refuse(good, failure('forbidden')),
      cancelled: await refuse(good, failure('reauth_cancelled')),
      // the status still says the limit, and the field was emptied by what fires no input event
      afterLimit: await refuse(good, failure('too_many_requests'), { again: true }),
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
  async toldOfPassword() {
    const out = [];
    for (const refused of [false, true]) {
      const p = page({ get: () => Promise.resolve({ sessions: [] }), patch: () => (refused ? Promise.reject(new Error('no')) : Promise.resolve({})) });
      await settle();
      p.$('account-email').value = 'vic@example.com';
      p.fire('account-email-form', 'submit');
      const during = p.told.length;
      await settle();
      out.push([during, p.told]);
    }
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
      return p.say('account-email-result').concat(p.toasts.splice(0), p.$('account-email-fold').hidden);
    };
    const saved = await save(' vic@example.com ');
    p.fire('account-email-form', 'input');
    const typed = p.say('account-email-result')[1];
    const removed = await save('');
    answer = () => Promise.reject(new Error('Not an email address'));
    const refused = await save('nope@example.com');
    answer = () => Promise.reject({ error: 'reauth_cancelled' });
    const cancelled = await save('vic@example.com');
    return { sent, focus, saved, typed, removed, refused, cancelled };
  },
  // the switch as first drawn, pressed twice at once, refused, and with its answer lost
  async history() {
    const stored = { enabled: true, available: true };
    const sent = [];
    let put = (body) => { stored.enabled = body.enabled; return Promise.resolve({}); };
    let get = () => Promise.resolve({ ...stored });
    const p = page({
      get: (url) => (url === '/api/search/assist/history' ? get() : Promise.resolve({ sessions: [] })),
      put: (url, body) => { sent.push([url, body.enabled]); return put(body); },
    }, { ai: true });
    const hiddenAtFirst = p.$('account-ai').hidden;
    await settle();
    const state = () => [p.$('account-history').getAttribute('aria-checked'), p.$('account-history').classList.contains('on')];
    const push = async () => {
      p.press('account-history');
      p.press('account-history');
      const during = state();
      await settle();
      return { during, after: state(), toasts: p.toasts.splice(0), focused: p.focused(), busy: p.$('account-history').getAttribute('aria-disabled') };
    };
    const first = { hiddenAtFirst, hidden: p.$('account-ai').hidden, state: state() };
    const off = await push();
    const good = put;
    put = () => Promise.reject(new Error('No luck'));
    const refused = await push();
    get = () => Promise.reject(new Error('gone'));
    const offline = await push();
    get = () => Promise.resolve({ ...stored });
    put = (body) => { good(body); return Promise.reject(new Error('lost')); };
    const lost = await push();
    return { first, off, refused, offline, lost, sent };
  },
  async historyHidden() {
    const hidden = async (get) => {
      const p = page({ get: (url) => (url === '/api/search/assist/history' ? get() : Promise.resolve({ sessions: [] })) }, { ai: true });
      await settle();
      return p.$('account-ai').hidden;
    };
    return [await hidden(() => Promise.resolve({ enabled: true, available: false })), await hidden(() => Promise.reject(new Error('x')))];
  },
  async clearSearches() {
    const run = async (yes, answer) => {
      const sent = [];
      const p = page({
        get: (url) => Promise.resolve(url === '/api/search/assist/history' ? { enabled: true, available: true } : { sessions: [] }),
        delete: (url) => { sent.push(url); return answer(); },
      }, { ai: true, yes });
      await settle();
      p.press('account-clear-searches');
      await settle();
      return { asked: p.asked.length, sent, left: Object.keys(p.session), toasts: p.toasts, says: p.$('account-clear-searches').textContent, busy: p.$('account-clear-searches').getAttribute('aria-disabled') };
    };
    return {
      no: await run(false, () => Promise.resolve({})),
      yes: await run(true, () => Promise.resolve({})),
      failed: await run(true, () => Promise.reject(new Error('No luck'))),
    };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
