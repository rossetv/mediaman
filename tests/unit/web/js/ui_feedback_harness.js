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

/* A page to show toasts on: *phone* is a narrow one. */
function page(phone) {
  const doc = makeDocument();
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date, WeakMap, parseFloat,
    matchMedia: (q) => ({ matches: /max-width/.test(q) && !!phone }),
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  return { doc, ui: ctx.UIFeedback };
}

/* Toasts while something is full screen, and as that begins and ends. */
function fullscreen() {
  const { doc, ui } = page();
  const player = doc.body.appendChild(build(doc, ['div', { id: 'player' }]));
  const video = player.appendChild(build(doc, ['video', {}]));
  const home = () => { const p = doc.getElementById('ui-toast-host').parentNode; return p === doc.body ? 'body' : p.id; };
  const change = (name, el) => { doc[name] = el; (doc.listeners[name.replace('Element', 'change').toLowerCase()] || []).forEach((fn) => fn({})); };
  const out = {};
  change('fullscreenElement', player);
  ui.toast('Kept', { action: { label: 'Undo', onClick() {} } });
  out.shownIn = home();
  const undo = doc.querySelector('.ui-toast__action');
  undo.focus();
  change('fullscreenElement', null);
  out.after = home();
  out.focusKept = doc.activeElement === undo;
  change('webkitFullscreenElement', player);
  out.webkit = home();
  change('webkitFullscreenElement', null);
  /* A video alone shows nothing but itself. */
  change('fullscreenElement', video);
  out.video = home();
  out.hosts = doc.querySelectorAll('.ui-toast-host').length;
  return out;
}

/* The toasts still there after the ones named, an Undo among them where the name ends so. */
function stays(names, phone) {
  const { doc, ui } = page(phone);
  names.forEach((name) => {
    if (/Undo$/.test(name)) ui.toast(name, { action: { label: 'x', onClick() {} } });
    else if (/Error$/.test(name)) ui.error(name);
    else ui.toast(name);
  });
  return doc.querySelectorAll('.ui-toast:not(.is-leaving)').map((t) => t.textContent.replace(/x$/, ''));
}
const onPhone = () => ({
  wide: stays(['a', 'b', 'c', 'd'], false),
  wideUndo: stays(['1 Undo', '2 Undo', '3 Undo', '4 Undo'], false),
  phone: stays(['a', 'b', 'c'], true),
  phoneUndo: stays(['1 Undo', '2 Undo', '3 Undo'], true),
  undoStays: stays(['1 Undo', 'a', 'b'], true),
  errorStays: stays(['1 Undo', 'Error', '2 Undo'], true),
});
const scenarios = { pressed, fullscreen, onPhone, stacked: () => Promise.all([stacked(1), stacked(0), stacked(null)]) };
Promise.resolve(scenarios[process.argv[2]]()).then((out) => console.log(JSON.stringify(out)));
