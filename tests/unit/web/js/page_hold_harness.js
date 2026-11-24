// Runs the real core/dom.js, ui-feedback.js and core/reauth.js against the fake DOM on a page
// 1280 wide with a 15px scrollbar, and prints the width held for it (--page-bar) after each step
// of one scenario, as JSON. Usage: node page_hold_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

const doc = makeDocument();
const root = doc.documentElement;
const held = {};
root.style = { setProperty(k, v) { held[k] = v; root.clientWidth = 1280; }, removeProperty(k) { delete held[k]; root.clientWidth = 1265; } };
root.clientWidth = 1265;
const bar = () => held['--page-bar'] || null;

const closers = []; // each password dialog's close, as the shared modal would run it
const ctx = vm.createContext({
  document: doc, navigator: { platform: 'Linux' }, innerWidth: 1280, setTimeout() {}, clearTimeout() {}, requestAnimationFrame() {},
  Array, Math, Date, WeakMap, parseFloat, Promise, TypeError,
  getComputedStyle: () => ({ transitionDuration: '0s', transitionDelay: '0s' }),
  MM: { api: { APIError: Error }, modal: { setupDetail: (_el, o) => { const close = () => o.onClose(); closers.push(close); return { open() {}, close }; } } },
});
ctx.window = ctx;
['core/dom.js', 'ui-feedback.js', 'core/reauth.js'].forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));

const flush = () => new Promise((r) => setImmediate(r));
const confirm = () => {
  ctx.UIFeedback.confirm({ title: 'Sure?' });
  const all = doc.querySelectorAll('.ui-confirm-overlay');
  const cancel = all[all.length - 1].querySelectorAll('button')[0];
  return () => cancel.listeners.click.forEach((fn) => fn({}));
};

const scenarios = {
  // two dialogs, one over the other, closed oldest first and newest first
  async stacked() {
    const run = (oldestFirst) => {
      const closes = [confirm(), confirm()];
      const steps = [bar()];
      (oldestFirst ? closes : closes.reverse()).forEach((close) => { close(); steps.push(bar()); });
      return steps;
    };
    return { oldestFirst: run(true), newestFirst: run(false) };
  },
  // a save waits in the password dialog, a confirm opens over it, and the password is accepted
  async saveUnderAConfirm() {
    let accept;
    ctx.MM.reauth.openModal({ onSubmit: () => new Promise((resolve) => { accept = resolve; }) });
    const input = doc.querySelector('.reauth-sheet .inp');
    input.value = 'pw';
    input.select = () => {};
    input.listeners.keydown.forEach((fn) => fn({ key: 'Enter', preventDefault() {} }));
    const closeConfirm = confirm();
    const steps = [bar()];
    accept({ ok: true });
    await flush();
    steps.push(bar());
    closeConfirm();
    steps.push(bar());
    // closed a second time, as an answer after Escape once did
    closers[0]();
    const again = confirm();
    steps.push(bar());
    again();
    return steps.concat(bar());
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
