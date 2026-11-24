// Runs the real ui-feedback.js against the fake DOM: whether Ctrl+Z presses a toast's Undo (pressed), and confirms asked
// together (stacked). Usage: node ui_feedback_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom.js');

const FILE = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/ui-feedback.js');

/* *dialog*: one on the page, as { attrs, box: false for none, visibility }. */
function press({ dialog, own, other, confirm, key = {}, field } = {}) {
  const doc = makeDocument();
  let visibility = 'visible';
  let from = null;
  if (dialog) {
    const d = doc.body.appendChild(build(doc, ['div', { role: 'dialog', 'aria-modal': 'true', ...dialog.attrs }]));
    if (own) from = d;
    if (dialog.box === false) d.getClientRects = () => [];
    visibility = dialog.visibility || 'visible';
  }
  if (other) doc.body.appendChild(build(doc, ['div', { role: 'dialog', 'aria-modal': 'true' }]));
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date, WeakMap, parseFloat, Promise,
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ display: 'flex', visibility, transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  if (confirm) ctx.UIFeedback.confirm({ title: 'Sure?' });
  let undone = 0;
  const toast = ctx.UIFeedback.toast('Kept', { dialog: from, action: { label: 'Undo', onClick: () => { undone += 1; } } });
  const button = toast.querySelector('.ui-toast__action');
  button.click = () => button.listeners.click.forEach((fn) => fn({}));
  const target = field ? doc.createElement(field) : doc.body;
  doc.listeners.keydown.forEach((fn) => fn({ key: 'z', ctrlKey: true, target, preventDefault() {}, ...key }));
  return undone === 1;
}

/* Two confirms asked at once; *close* says what closes first. What each answered, and how many are still open. */
async function stacked(close) {
  const doc = makeDocument();
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date, WeakMap, parseFloat, Promise,
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  const answers = [null, null];
  [0, 1].forEach((i) => ctx.UIFeedback.confirm({ title: 'Sure?' }).then((yes) => { answers[i] = yes; }));
  const overlays = doc.querySelectorAll('.ui-confirm-overlay');
  const ids = new Set(overlays.map((o) => o.id)).size;
  if (close !== null) overlays[close].querySelectorAll('button')[1].listeners.click.forEach((fn) => fn({}));
  ctx.UIFeedback.dismissConfirm();
  await Promise.resolve();
  await Promise.resolve();
  return { ids, answers };
}

const open = { attrs: {} };
const pressed = () => ({
  nothingOpen: press(),
  closedInside: press({ dialog: { attrs: {}, box: false } }),
  open: press({ dialog: open }),
  itsOwn: press({ dialog: open, own: true }),
  anotherOverItsOwn: press({ dialog: open, own: true, other: true }),
  confirmOverItsOwn: press({ dialog: open, own: true, confirm: true }),
  textFieldInItsOwn: press({ dialog: open, own: true, field: 'input' }),
  ariaHidden: press({ dialog: { attrs: { 'aria-hidden': 'true' } } }),
  invisible: press({ dialog: { attrs: {}, visibility: 'hidden' } }),
  notModal: press({ dialog: { attrs: { 'aria-modal': 'false' } } }),
  confirm: press({ confirm: true }),
  textField: press({ field: 'input' }),
  redo: press({ key: { shiftKey: true } }),
});
const scenarios = { pressed, stacked: () => Promise.all([stacked(1), stacked(0), stacked(null)]) };
Promise.resolve(scenarios[process.argv[2]]()).then((out) => console.log(JSON.stringify(out)));
