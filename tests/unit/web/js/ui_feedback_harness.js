// Runs the real ui-feedback.js against the fake DOM: whether Ctrl+Z presses a toast's Undo.
// Usage: node ui_feedback_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom.js');

const FILE = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/ui-feedback.js');

/* *dialog*: one on the page, as { attrs, box: false for none, visibility }. */
function press({ dialog, confirm, key = {}, field } = {}) {
  const doc = makeDocument();
  let visibility = 'visible';
  if (dialog) {
    const d = doc.body.appendChild(build(doc, ['div', { role: 'dialog', 'aria-modal': 'true', ...dialog.attrs }]));
    if (dialog.box === false) d.getClientRects = () => [];
    visibility = dialog.visibility || 'visible';
  }
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date, parseFloat, Promise,
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ display: 'flex', visibility, transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  if (confirm) ctx.UIFeedback.confirm({ title: 'Sure?' });
  let undone = 0;
  const toast = ctx.UIFeedback.toast('Kept', { action: { label: 'Undo', onClick: () => { undone += 1; } } });
  const button = toast.querySelector('.ui-toast__action');
  button.click = () => button.listeners.click.forEach((fn) => fn({}));
  const target = field ? doc.createElement(field) : doc.body;
  doc.listeners.keydown.forEach((fn) => fn({ key: 'z', ctrlKey: true, target, preventDefault() {}, ...key }));
  return undone === 1;
}

const open = { attrs: {} };
console.log(JSON.stringify({
  nothingOpen: press(),
  closedInside: press({ dialog: { attrs: {}, box: false } }),
  open: press({ dialog: open }),
  ariaHidden: press({ dialog: { attrs: { 'aria-hidden': 'true' } } }),
  invisible: press({ dialog: { attrs: {}, visibility: 'hidden' } }),
  notModal: press({ dialog: { attrs: { 'aria-modal': 'false' } } }),
  confirm: press({ confirm: true }),
  textField: press({ field: 'input' }),
  redo: press({ key: { shiftKey: true } }),
}));
