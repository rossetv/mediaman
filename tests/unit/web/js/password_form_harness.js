// Runs the real password-form.js, and the forced change page's script with the submit guard, against
// the fake DOM and prints one scenario's outcome as JSON. Usage: node password_form_harness.js <scenario>
// `read` takes [[password, username], ...] on its standard input.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');
const { parts, prepare } = require('./password_form_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const GOOD = 'Copper lantern 41';
prepare(Node);
const shown = [];
Node.prototype.scrollIntoView = function () { shown.push(this.id); };

function page({ refused, forced, options } = {}) {
  const doc = makeDocument();
  const form = doc.body.appendChild(build(doc, ['form', { id: 'fpc-form', 'data-submit-guard': '', ...(forced ? { 'data-password-form': '' } : {}), ...(refused ? { 'data-refused': refused } : {}) },
    ['input', { autocomplete: 'username' }],
    ...parts({ old: 'old_password', fresh: 'new_password', submit: 'go', result: 'result' }),
    ['input', { id: 'confirm_password', type: 'password', 'data-again': 'new_password' }]]));
  const $ = (id) => doc.getElementById(id);
  doc.querySelector('[autocomplete="username"]').value = 'jonas.ek';
  ['old_password', 'new_password', 'confirm_password'].forEach((id) => { $(id).value = ''; $(id).type = 'password'; });
  form.reset = () => ['old_password', 'new_password'].forEach((id) => { $(id).value = ''; });
  const timers = [];
  const frames = [];
  const listeners = {};
  const ctx = vm.createContext({
    document: doc, Set, JSON, Math, TextEncoder, setTimeout: (fn, ms) => timers.push({ fn, ms }), clearTimeout: (n) => { if (timers[n - 1]) timers[n - 1].fn = () => {}; },
    requestAnimationFrame: (fn) => frames.push(fn),
    FormDataEvent: Object, getComputedStyle: () => ({}), matchMedia: () => ({ matches: true }),
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  });
  ctx.window = ctx;
  const load = (file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx);
  load('core/dom.js');
  load('password-form.js');
  let pw = null;
  if (forced) load('submit-guard.js');
  else pw = ctx.MM.password.form(form, { send: (values) => sent.push(values), ...options });
  const sent = [];
  return {
    doc, $, pw, sent, timers, MM: ctx.MM,
    run: (ms) => timers.filter((t) => t.ms === ms).forEach((t) => t.fn()),
    back: () => listeners.pageshow.forEach((fn) => fn({ persisted: true })),
    type(id, value) { $(id).value = value; form.listeners.input.forEach((fn) => fn({ target: $(id) })); },
    key: (caps) => form.listeners.keydown.forEach((fn) => fn({ getModifierState: () => caps })),
    // As Chromium does: a field whose type changed has its cursor put at the start after the press, then a frame.
    press(n, { wait = false } = {}) {
      const fields = doc.querySelectorAll('input');
      const was = fields.map((f) => f.type);
      doc.querySelectorAll('.pwd-eye')[n].listeners.click.forEach((fn) => fn({}));
      fields.forEach((f, i) => { if (f.type !== was[i]) f.setSelectionRange(0, 0); });
      if (!wait) this.frame();
    },
    frame: () => frames.splice(0).forEach((fn) => fn()),
    // As the browser raises it: on the form, then on up to the document.
    submit() {
      const e = { target: form, sent: true, stopped: false, preventDefault() { this.sent = false; }, stopPropagation() { this.stopped = true; } };
      form.listeners.submit.forEach((fn) => fn(e));
      if (!e.stopped) (doc.listeners.submit || []).forEach((fn) => fn(e));
      return e.sent;
    },
    // what is posted, as the browser gathers it
    posted() {
      const data = new Map($('confirm_password').disabled ? [] : [['confirm_password', $('confirm_password').value]]);
      $('confirm_password').name = 'confirm_password';
      doc.listeners.formdata.forEach((fn) => fn({ target: form, formData: data }));
      return data.get('confirm_password');
    },
    says: () => ['old_password', 'new_password'].map((id) => $(id + '-why').textContent),
    rules: () => Object.fromEntries(doc.querySelectorAll('.pwd-rule').map((r) => [r.getAttribute('data-rule'), r.className.replace('pwd-rule', '').replace('is-', '').trim()])),
    words: (key) => doc.querySelector('[data-rule="' + key + '"]').querySelector('.pwd-rule-w').textContent,
    focused: () => doc.activeElement.id,
    said: () => doc.querySelector('.pwd-said').textContent,
    busy: () => form.classList.contains('is-busy'),
    guarded: () => form.hasAttribute('data-busy'),
  };
}

const scenarios = {
  read() {
    const p = page();
    return JSON.parse(fs.readFileSync(0, 'utf8')).map(([password, username]) => {
      const r = p.MM.password.read(password, username);
      return { ok: r.ok, unsure: r.unsure };
    });
  },
  typing() {
    const p = page();
    const type = (text) => { p.type('new_password', text); return p.rules(); };
    const out = { none: p.rules(), short: type('Copper'), good: type(GOOD) };
    p.type('old_password', 'the old one');
    out.withOld = p.rules();
    out.same = (p.type('old_password', GOOD), p.rules());
    out.username = [type('x jonas.ek'), p.words('guess')];
    out.run = [type('abcdefghijkl'), p.words('guess')];
    out.emptied = [type(''), p.words('guess')];
    out.huge = type('Aa1!' + 'é'.repeat(600));
    return out;
  },
  said() {
    const p = page();
    const said = (text) => { p.type('new_password', text); const now = p.said(); p.run(900); p.timers.length = 0; return [now, p.said()]; };
    p.type('old_password', 'the old one');
    return { short: said('Copper'), huge: said('Aa1!' + 'é'.repeat(600)), good: said(GOOD), mix: said('copper lanternab'), rare: said('copper lantern٣᠐'), newer: said('AAAAAAAAAAA1\u{1ccd6}\u{1ccd7}\u{1ccd8}\u{1ccd9}\u{1ccda}\u{1ccdb}'), emptied: said('') };
  },
  // pressed twice, or typed in, before the frame
  eyeQuick() {
    const p = page();
    const f = p.$('new_password');
    const at = () => [f.selectionStart, f.selectionEnd, f.selectionDirection];
    const from = () => { f.value = 'abcdefghij'; f.focus(); f.setSelectionRange(2, 5, 'backward'); };
    from(); p.press(1, { wait: true }); p.press(1, { wait: true }); p.frame();
    const twice = at();
    from(); p.press(1, { wait: true });
    (f.listeners.keydown || []).forEach((fn) => fn({ target: f, key: 'X' }));
    const keyGoes = at();
    f.value = 'abXfghij'; f.setSelectionRange(3, 3); p.frame();
    return { twice, keyGoes, afterKey: at(), focused: p.focused() };
  },
  eye() {
    const p = page();
    const state = () => [p.$('old_password').type, p.$('new_password').type, p.doc.querySelectorAll('.pwd-eye').map((e) => e.getAttribute('aria-pressed'))];
    const out = { first: state() };
    p.$('new_password').focus();
    p.$('new_password').setSelectionRange(2, 4, 'backward');
    p.press(1);
    out.shown = state();
    out.kept = [p.focused(), p.$('new_password').selectionStart, p.$('new_password').selectionEnd, p.$('new_password').selectionDirection];
    p.type('old_password', 'x');
    p.type('new_password', GOOD);
    p.submit();
    out.sending = state();
    p.pw.free();
    out.refused = state();
    p.pw.reset();
    out.reset = state();
    return out;
  },
  caps() {
    const p = page();
    p.$('old_password').focus();
    p.key(true);
    const on = p.says();
    p.$('new_password').focus();
    p.key(true);
    const moved = p.says();
    p.submit();
    const wrong = p.says();
    p.key(false);
    return { on, moved, wrong, off: p.says() };
  },
  refusals() {
    const tries = {
      empty: ['', GOOD], noNew: ['x', ''], short: ['x', 'Copper lan1'], mix: ['x', 'copper lantern'], username: ['x', 'Jonas.ek was here 1'],
      same: [GOOD, GOOD], newer: ['x', 'AAAAAAAAAAA1\u{1ccd6}\u{1ccd7}\u{1ccd8}\u{1ccd9}\u{1ccda}\u{1ccdb}'], huge: ['x', 'Aa1!' + 'é'.repeat(600)], good: ['x', GOOD], unsure: ['x', 'copper lantern٣᠐'],
    };
    const out = {};
    Object.entries(tries).forEach(([name, [old, fresh]]) => {
      const p = page();
      p.type('old_password', old);
      p.type('new_password', fresh);
      p.$('go').focus();
      shown.length = 0;
      p.submit();
      out[name] = { sent: p.sent.length, says: p.says(), focused: p.focused(), shown: shown.slice(), invalid: ['old_password', 'new_password'].map((id) => p.$(id).getAttribute('aria-invalid')) };
      p.type('new_password', fresh + 'x');
      out[name].typed = p.says()[1];
    });
    return out;
  },
  server() {
    const answer = (issues, names) => {
      const p = page();
      p.type('old_password', 'x');
      p.type('new_password', GOOD);
      p.submit();
      p.pw.free();
      p.pw.refuseNew(GOOD, 'It does not pass', issues, names);
      const out = { says: p.$('new_password-why').textContent, items: p.$('new_password-why').querySelectorAll('li').map((li) => li.textContent), rules: p.rules(), focused: p.focused() };
      p.type('new_password', GOOD + '!');
      const typed = { typed: p.rules(), typedSays: p.says()[1] };
      // the same password for someone else
      p.type('new_password', GOOD);
      p.doc.querySelector('[autocomplete="username"]').value = 'mira';
      p.type('old_password', 'x');
      return { ...out, ...typed, other: p.rules() };
    };
    return {
      common: answer(['On the common list.'], ['common']),
      many: answer(['Must not contain the username.', 'On the common list.'], ['username', 'common']),
      unnamed: answer(['Something new.', 'On the common list.'], ['other', 'common']),
      bare: answer(['Something new.', 'And more.'], undefined),
      long: answer(['Avoid sequential characters (abc…, 123…, qwerty…).'], ['sequence']),
      same: answer(undefined, ['same']),
      username: answer(['Must not contain the username.'], ['username']),
    };
  },
  whole() {
    const p = page();
    p.pw.refuseWhole('Too many');
    const told = [p.$('result').textContent, p.pw.aside.classList.contains('is-wrong')];
    p.type('new_password', 'x');
    return { told, typed: [p.$('result').textContent, p.pw.aside.classList.contains('is-wrong')] };
  },
  busy() {
    const p = page();
    p.type('old_password', 'x');
    p.type('new_password', GOOD);
    p.$('go').focus();
    p.submit();
    p.submit();
    const out = { sent: p.sent, focused: p.focused(), busy: p.busy(), says: p.$('go').textContent, readOnly: p.$('new_password').readOnly, disabled: p.$('go').getAttribute('aria-disabled') };
    p.pw.free();
    return { ...out, after: { busy: p.busy(), says: p.$('go').textContent, readOnly: p.$('new_password').readOnly } };
  },
  forced() {
    const go = (old, fresh) => {
      const p = page({ forced: true });
      p.type('old_password', old);
      p.type('new_password', fresh);
      const sent = p.submit();
      return { sent, focused: p.focused(), busy: p.busy(), guarded: p.guarded(), again: p.posted(), kept: p.$('confirm_password').value, attributes: JSON.stringify(p.doc.querySelectorAll('input').map((n) => n.attrs)).includes(fresh), type: p.$('confirm_password').type, off: p.$('confirm_password').disabled, hides: p.doc.documentElement.className, second: p.submit(), p };
    };
    const good = go('the old one', GOOD);
    const p = good.p;
    p.run(10000);
    const lapsed = [p.busy(), p.guarded(), p.submit()];
    p.back();
    const out = { good: { ...good, p: null }, lapsed, back: p.busy() };
    ['none', 'weak', 'same'].forEach((name) => { out[name] = { ...go(...{ none: ['', GOOD], weak: ['x', 'short'], same: [GOOD, GOOD] }[name]), p: null }; });
    return out;
  },
  // the page as the server sends it back after a refusal
  refusedPage() {
    const p = page({ forced: true, refused: 'username common' });
    const first = [p.rules(), p.words('guess')];
    p.type('new_password', GOOD);
    return { first, typed: p.rules() };
  },
  async celebrate() {
    const p = page();
    const stage = p.doc.body;
    const form = p.$('fpc-form');
    const step = async () => { await settle(); p.timers.splice(0).forEach((t) => t.fn()); await settle(); };
    let undo = null;
    p.MM.password.celebrate(stage, form, 'Password changed', '<b>words', null).then((fn) => { undo = fn; });
    const during = [form.inert, stage.querySelector('.pwd-done').textContent];
    for (let i = 0; i < 3; i += 1) await step();
    const ticked = stage.querySelector('.pwd-mark').classList.contains('is-ticked');
    undo();
    return { during, ticked, after: [form.inert, Boolean(stage.querySelector('.pwd-done')), stage.classList.contains('is-done')] };
  },
};

Promise.resolve(scenarios[process.argv[2]]()).then((out) => process.stdout.write(JSON.stringify(out)));
