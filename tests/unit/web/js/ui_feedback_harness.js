// Runs the real ui-feedback.js against the fake DOM: whether Ctrl+Z presses a toast's Undo (pressed), and confirms asked
// together (stacked). Usage: node ui_feedback_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
/* ui-feedback.js and the helper its confirm needs, in a page's context. */
const load = (ctx) => ['core/dom.js', 'ui-feedback.js'].forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));

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
  load(ctx);
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
  let now = 0; // each look at the clock is a second on, so no click comes too soon after a dialog opens
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date: { now: () => (now += 1000) }, WeakMap, parseFloat, Promise,
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  load(ctx);
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

/* What names the dialog and what describes it, as text. */
function named() {
  const says = (opts) => {
    const { doc, ui } = page();
    ui.confirm(opts);
    const dialog = doc.querySelector('.ui-confirm-overlay');
    const text = (attr) => (dialog.hasAttribute(attr) ? doc.getElementById(dialog.getAttribute(attr)).textContent : null);
    return [text('aria-labelledby'), text('aria-describedby')];
  };
  return { body: says({ title: 'Delete it?', body: 'For good.' }), none: says({ title: 'Delete it?' }) };
}

/* The width of the page's scrollbar, held while a confirm is open. */
function bar() {
  const { doc, ui, win } = page();
  const root = doc.documentElement;
  Object.assign(root, { clientWidth: 1265, style: { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } } });
  win.innerWidth = 1280;
  ui.confirm({ title: 'Sure?' });
  const held = root.style['--page-bar'];
  ui.dismissConfirm();
  return { held, after: root.style['--page-bar'] || null };
}

/* Whether a toast is still there as its time runs out, and once the pointer has left it. */
function underPointer(show, pointer) {
  const doc = makeDocument();
  const timers = []; // a toast's own time, not the short wait for its fade
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout(fn, ms) { if (ms >= 2000) timers.push(fn); }, requestAnimationFrame() {}, Array, Math, Date, WeakMap, parseFloat, Promise,
    matchMedia: (q) => ({ matches: /hover/.test(q) && pointer }),
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  load(ctx);
  const toast = show(ctx.UIFeedback);
  let over = true;
  toast.matches = (sel) => sel === ':hover' && over;
  const timeUp = () => { timers.splice(0).forEach((fn) => fn()); return !toast.classList.contains('is-leaving'); };
  const held = timeUp();
  over = false;
  return [held, timeUp()];
}
const held = () => ({
  error: underPointer((ui) => ui.error('No luck'), true),
  // a tap leaves :hover on the toast until the next one
  errorAfterATap: underPointer((ui) => ui.error('No luck'), false),
  plain: underPointer((ui) => ui.toast('Saved'), true),
});

/* What a confirm answers to clicks, each [on, ms, detail]: on its main button, its Cancel, its title or its backdrop,
   *ms* after it opened, as click number *detail* of a run of clicks. null where they did nothing. */
const clicked = (on, ms, detail) => clicks([[on, ms, detail]]);
async function clicks(list) {
  const doc = makeDocument();
  let now = 1000;
  const ctx = {
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, WeakMap, parseFloat, Promise,
    Date: { now: () => now },
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  load(ctx);
  let answer = null;
  ctx.UIFeedback.confirm({ title: 'Delete it?' }).then((yes) => { answer = yes; });
  const overlay = doc.querySelector('.ui-confirm-overlay');
  const [cancel, main] = overlay.querySelectorAll('button');
  const opened = now;
  list.forEach(([on, ms, detail]) => {
    const target = { main, cancel, backdrop: overlay, title: overlay.querySelector('.ui-confirm-title') }[on];
    now = opened + ms;
    for (let el = target; el; el = el.parentNode) (el.listeners.click || []).forEach((fn) => fn({ target, detail }));
  });
  await Promise.resolve();
  await Promise.resolve();
  return answer;
}
const stray = async () => ({
  // the second click of a double click on what asked
  doubleClick: [await clicked('main', 150, 2), await clicked('backdrop', 150, 2), await clicked('cancel', 150, 2)],
  // a slow second click, before the dialog could be read
  early: [await clicked('main', 250, 1), await clicked('backdrop', 250, 1), await clicked('cancel', 250, 1)],
  meant: [await clicked('main', 400, 1), await clicked('backdrop', 400, 1), await clicked('cancel', 400, 1)],
  // quick clicks of which the first came too soon, or was on the words, and only the last could be meant
  run: [await clicks([['main', 200, 1], ['main', 350, 2]]), await clicks([['main', 150, 1], ['main', 250, 2], ['main', 400, 3]]), await clicks([['title', 100, 1], ['backdrop', 350, 2]])],
  soon: [await clicks([['main', 100, 1], ['main', 250, 2]]), await clicks([['backdrop', 100, 1], ['backdrop', 250, 2]])],
  // a double or triple click on what asked, slow enough to outlast the first moments
  strayRun: [await clicks([['main', 200, 2], ['main', 400, 3]]), await clicks([['backdrop', 200, 2], ['backdrop', 400, 3]]), await clicks([['cancel', 200, 2], ['cancel', 400, 3]])],
  // Enter or Space on a button is a click numbered 0
  byKey: [await clicked('main', 400, 0), await clicked('cancel', 400, 0)],
});

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
    document: doc, navigator: { platform: 'Linux' }, setTimeout() {}, requestAnimationFrame() {}, Array, Math, Date, WeakMap, parseFloat, Promise,
    matchMedia: (q) => ({ matches: /max-width/.test(q) && !!phone }),
    getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  load(ctx);
  return { doc, ui: ctx.UIFeedback, win: ctx };
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
const scenarios = { pressed, named, bar, held, stray, fullscreen, onPhone, stacked: () => Promise.all([stacked(1), stacked(0), stacked(null)]) };
Promise.resolve(scenarios[process.argv[2]]()).then((out) => console.log(JSON.stringify(out)));
