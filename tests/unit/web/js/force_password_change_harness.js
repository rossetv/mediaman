// Runs the real force-password-change.js, with the submit guard the page also loads, against the
// fake DOM and prints what a submit of the typed passwords did, as JSON.
// Usage: node force_password_change_harness.js <current password> <new password> <confirmation>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

const doc = makeDocument();
const form = doc.body.appendChild(build(doc, ['form', { id: 'fpc-form', 'data-submit-guard': '' },
  ['input', { id: 'old_password' }], ['input', { id: 'new_password' }], ['input', { id: 'confirm_password' }],
  ['span', { id: 'pw-meter-fill' }], ['span', { id: 'pw-strength-label' }], ['div', { id: 'pw-match' }]]));
const $ = (id) => doc.getElementById(id);
['old_password', 'new_password', 'confirm_password'].forEach((id) => { $(id).value = ''; $(id).select = () => {}; });

const ctx = vm.createContext({
  document: doc, Set, JSON, Math, setTimeout() {},
  window: { addEventListener() {} },
  MM: { dom: { q: (sel) => doc.querySelector(sel) } },
});
['force-password-change.js', 'submit-guard.js'].forEach((file) => {
  vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx);
});

const [, , old, typed, again] = process.argv;
$('old_password').value = old;
$('new_password').value = typed;
$('confirm_password').value = again;
$('new_password').focus();

// A submit as the browser raises it: on the form, then on up to the document.
const e = { target: form, sent: true, stopped: false, preventDefault() { this.sent = false; }, stopPropagation() { this.stopped = true; } };
(form.listeners.submit || []).forEach((fn) => fn(e));
if (!e.stopped) doc.listeners.submit.forEach((fn) => fn(e));

process.stdout.write(JSON.stringify({
  sent: e.sent,
  busy: form.hasAttribute('data-busy'),
  focused: doc.activeElement.id,
  says: $('pw-match').textContent.trim(),
  error: doc.querySelectorAll('.error-msg').filter((box) => !box.hidden).map((box) => box.textContent).join(' | '),
}));
