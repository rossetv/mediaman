// Runs the real modal-a11y.js (with ui-feedback.js and core/modal.js over it) against the fake DOM
// and prints one scenario's outcome as JSON. Usage: node modal_a11y_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

// What the browser refuses and the fake DOM does not.
const unseen = (n) => { for (; n; n = n.parentNode) if (n.hasAttribute('hidden') || n.hidden) return true; return false; };
Node.prototype.getClientRects = function () { return unseen(this) || !this.isConnected ? [] : [1]; };
Object.defineProperty(Node.prototype, 'offsetParent', { get() { return unseen(this) ? null : this.ownerDocument.body; } });
Node.prototype.focus = function () {
  for (let n = this; n; n = n.parentNode) if (n.inert) return;
  if (unseen(this) || !this.isConnected || this.style.visibility === 'hidden') return;
  this.ownerDocument.activeElement = this;
};

function page() {
  const doc = makeDocument();
  doc.removeEventListener = (t, fn) => { doc.listeners[t] = (doc.listeners[t] || []).filter((f) => f !== fn); };
  const button = (id, attrs) => ['button', { id, ...attrs }, id];
  doc.body.append(
    build(doc, ['header', { id: 'top' }, button('brand')]),
    build(doc, ['main', { id: 'main' },
      ['section', { id: 'list' }, button('opener')],
      ['div', { id: 'inside', hidden: '' }, button('inside-no'), button('inside-yes')]]),
    build(doc, ['div', { id: 'closed-panel' }]),
    build(doc, ['div', { id: 'low', hidden: '' },
      ['div', { id: 'card' }, ['h3', { id: 'heading', tabindex: '-1' }, 'x'], button('low-no'), ['input', { id: 'field' }], button('low-yes')]]),
    build(doc, ['div', { id: 'plain', hidden: '' }, ['input', { id: 'password' }], button('plain-yes')]),
    build(doc, ['div', { id: 'ui-toast-host' }, button('undo')]));
  const $ = (id) => doc.getElementById(id);
  $('closed-panel').inert = true;
  ['inside', 'low', 'plain'].forEach((id) => {
    Object.defineProperty($(id), 'hidden', {
      get() { return this.hasAttribute('hidden'); }, set(on) { this.toggleAttribute('hidden', on); },
    });
  });
  const ctx = vm.createContext({
    document: doc, navigator: { platform: 'Linux' }, setTimeout: (fn, ms) => { if (!ms) fn(); }, clearTimeout() {}, requestAnimationFrame() {},
    getComputedStyle: (n) => ({ visibility: n.style.visibility || 'visible', transitionDuration: '0s', transitionDelay: '0s' }),
    Array, Math, Date, performance, WeakMap, parseFloat, Promise, matchMedia: () => ({ matches: true }),
  });
  ctx.window = ctx;
  const onWindow = [];
  ctx.addEventListener = (t, fn) => onWindow.push(fn);
  ['core/dom.js', 'core/modal.js', 'modal-a11y.js', 'ui-feedback.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.MM.dom.holdPage = () => () => {};
  $('opener').focus();
  const focused = () => doc.activeElement.id || doc.activeElement.textContent || 'body';
  return {
    doc, $, ctx, focused,
    key(key, extra) {
      const e = { key, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; }, ...extra };
      (doc.listeners.keydown || []).concat(onWindow).forEach((fn) => fn(e));
      return e.defaultPrevented;
    },
    inert: () => doc.documentElement.descendants().filter((n) => n.inert).map((n) => n.id || n.className),
    setup: (id, opts) => ctx.MM.modal.setupDetail($(id), { manageBodyOverflow: false, ...opts }),
    confirm() {
      ctx.UIFeedback.confirm({ title: 'Sure?' });
      const all = doc.querySelectorAll('.ui-confirm-overlay');
      return all[all.length - 1];
    },
  };
}

const scenarios = {
  // A confirm over a page whose storage panel is closed, and inert as it always is then.
  confirm() {
    const p = page();
    const before = p.inert();
    p.confirm();
    const open = p.inert();
    p.key('Escape');
    return { before, open, closed: p.inert(), focused: p.focused() };
  },
  // Tab in a menu over a dialog, whose own handler gives the focus back to its button.
  menuOver() {
    const p = page();
    const m = p.setup('low');
    m.open();
    p.$('undo').focus();
    p.doc.addEventListener('keydown', () => p.$('low-no').focus());
    p.key('Tab');
    return p.focused();
  },
  // A dialog's button, off screen by the time it closes.
  scrolls() {
    const p = page();
    const m = p.setup('low');
    m.open();
    let how = null;
    p.$('opener').focus = (o) => { how = o; };
    m.close();
    return how;
  },
  // A dialog that stands inside the page's main part, opened twice.
  inside() {
    const p = page();
    const m = p.setup('inside');
    m.open();
    m.open();
    const open = p.inert();
    m.close();
    const closed = p.inert();
    m.close();
    return { open, closed, again: p.inert() };
  },
  // The toasts stand in what is full screen, which the dialog is not in.
  toastsFullScreen() {
    const p = page();
    p.$('list').appendChild(p.$('ui-toast-host'));
    p.confirm();
    return p.inert();
  },
  // A confirm over a dialog: closed newest first, then oldest first, then a dialog that keeps its own focus.
  stacked() {
    const p = page();
    const look = () => ({ inert: p.inert().filter((id) => id !== 'closed-panel'), focused: p.focused() });
    const run = (opts, oldestFirst) => {
      p.$('opener').focus();
      const m = p.setup(opts.useModalA11y === false ? 'plain' : 'low', opts);
      m.open();
      const one = look();
      p.confirm();
      const two = look().inert;
      const steps = [() => p.ctx.UIFeedback.dismissConfirm(), () => m.close()];
      (oldestFirst ? steps.reverse() : steps)[0]();
      const mid = look();
      steps[1]();
      p.doc.querySelector('.ui-confirm-overlay').remove();
      return { one, two, mid, end: look() };
    };
    return { newest: run({}, false), oldest: run({}, true), plain: run({ useModalA11y: false }, false) };
  },
  // What a dialog that keeps its own focus gives back, it can hand on as it closes.
  giveBackFirst() {
    const p = page();
    const m = p.setup('plain', { useModalA11y: false, onClose: () => p.$('opener').focus() });
    m.open();
    p.$('password').focus();
    const open = p.inert();
    m.close();
    return { open, closed: p.inert(), focused: p.focused() };
  },
  // A confirm answered before its first focus, which comes a moment after it opens.
  closedAtOnce() {
    const p = page();
    const later = [];
    p.ctx.setTimeout = (fn, ms) => { if (!ms) later.push(fn); };
    p.confirm();
    p.ctx.UIFeedback.dismissConfirm();
    later.forEach((fn) => fn());
    return p.focused();
  },
  // Tab and Shift+Tab, each stopped from the browser's own step, which in Safari skips buttons.
  tab() {
    const p = page();
    p.setup('low').open();
    const go = (shiftKey, from, n) => {
      if (from) p.$(from).focus();
      const out = [];
      for (let i = 0; i < n; i++) { const held = p.key('Tab', { shiftKey }); out.push(held ? p.focused() : 'the browser'); }
      return out;
    };
    const out = { on: go(false, 'low-no', 3), back: go(true, 'low-no', 3) };
    out.heading = [go(false, 'heading', 1)[0], go(true, 'heading', 1)[0]];
    p.doc.body.focus();
    out.nowhere = [go(false, null, 1)[0], (p.doc.body.focus(), go(true, null, 1)[0])];
    out.toast = go(false, 'undo', 1);
    p.$('field').style.visibility = 'hidden';
    out.unseen = go(false, 'low-no', 1);
    p.doc.body.focus();
    p.$('card').hidden = true;
    out.none = go(false, null, 1);
    return out;
  },
  // A title's page, and the bar of what plays on a TV, which is shown, under a dialog, and hidden.
  tvBar() {
    const p = page();
    const button = (id) => ['button', { id }, id];
    p.doc.body.append(
      build(p.doc, ['section', { id: 'tv-mini' }, button('remote'), button('pause')]),
      build(p.doc, ['div', { id: 'ttl' }, button('shut'), button('watch')]));
    const go = (n, shiftKey) => { const out = []; for (let i = 0; i < n; i++) { p.key('Tab', { shiftKey }); out.push(p.focused()); } return out; };
    p.$('remote').focus();
    const other = p.setup('plain', { useModalA11y: false });
    other.open();
    const out = { otherDialog: p.inert().includes('tv-mini') };
    other.close();
    p.ctx.MM.inertPage.hold(p.$('ttl'));
    out.inert = p.inert().filter((id) => id !== 'closed-panel');
    p.$('shut').focus();
    out.tab = go(4);
    out.back = go(2, true);
    const m = p.setup('low');
    m.open();
    out.under = p.inert().includes('tv-mini');
    out.underTab = go(3);
    m.close();
    p.$('tv-mini').hidden = true;
    p.$('shut').focus();
    out.hidden = go(2);
    return out;
  },
  tabInPlain() {
    const p = page();
    p.setup('plain', { useModalA11y: false }).open();
    p.$('password').focus();
    const out = [];
    for (let i = 0; i < 2; i++) { p.key('Tab'); out.push(p.focused()); }
    return out;
  },
  // Controls the browser's own Tab would pass over, and two it would not.
  stops() {
    const p = page();
    p.$('card').textContent = '';
    p.$('card').append(...[
      ['button', { id: 'passed', tabindex: '-1' }], ['a', { id: 'further', href: '#', tabindex: '-2' }], ['button', { id: 'first' }],
      ['div', { inert: '' }, ['button', { id: 'out' }]], ['details', {}, ['summary', { id: 'more' }]], ['iframe', { id: 'frame' }], ['div', { id: 'own', tabindex: '0' }],
    ].map((n) => build(p.doc, n)));
    p.setup('low').open();
    const out = [p.focused()];
    for (let i = 0; i < 4; i++) { p.key('Tab'); out.push(p.focused()); }
    return out;
  },
  // A group of radios between two buttons: none checked, one checked, and one with the keyboard.
  radios() {
    const p = page();
    p.$('card').textContent = '';
    const radio = (id, name) => ['input', { type: 'radio', name, id }];
    p.$('card').append(...[['button', { id: 'before' }], radio('a1', 'a'), radio('a2', 'a'), radio('a3', 'a'), radio('b1', 'b'), ['button', { id: 'after' }]].map((n) => build(p.doc, n)));
    p.setup('low').open();
    const go = (from, shiftKey) => {
      p.$(from).focus();
      const out = [];
      for (let i = 0; i < 4; i += 1) { p.key('Tab', { shiftKey }); out.push(p.focused()); }
      return out;
    };
    const none = go('before');
    p.$('a3').checked = true;
    return { none, checked: go('before'), back: go('before', true), from: go('a1') };
  },
  // A confirm closed once it has left the page, over a dialog and alone.
  goneFromThePage() {
    const p = page();
    const look = () => ({ inert: p.inert().filter((id) => id !== 'closed-panel'), focused: p.focused() });
    const gone = () => { p.confirm().remove(); p.ctx.UIFeedback.dismissConfirm(); return look(); };
    const alone = gone();
    const m = p.setup('low');
    m.open();
    const over = gone();
    m.close();
    return { alone, over, after: look() };
  },
  // Close what was opened from the main part: a dialog, and a confirm opened from that.
  closeFrom() {
    const run = (from, opener, change) => {
      const p = page();
      p.$(opener).focus();
      p.setup('low').open();
      p.confirm();
      if (change) change(p);
      p.ctx.ModalA11y.closeFrom(p.$(from));
      return { left: [!p.$('low').hidden, p.doc.querySelectorAll('.ui-confirm-overlay:not(.is-leaving)').length], focused: p.focused() };
    };
    return {
      within: run('main', 'opener'),
      elsewhere: run('main', 'brand'),
      gone: run('main', 'brand', (p) => p.$('brand').remove()),
    };
  },
  // Escape with a dialog that answers it itself over one that does not.
  escape() {
    const p = page();
    p.setup('low').open();
    p.setup('plain', { useModalA11y: false }).open();
    const shown = () => ['low', 'plain'].filter((id) => !p.$(id).hidden);
    p.key('Escape');
    const once = shown();
    p.key('Escape');
    return { once, twice: shown() };
  },
  // The first focus: what is marked, what is marked but cannot be seen, and nothing that can.
  first() {
    const go = (set) => {
      const p = page();
      set(p.$);
      p.setup('low').open();
      return p.focused();
    };
    const mark = (n) => n.setAttribute('data-modal-focus', '');
    return {
      marked: go(($) => mark($('heading'))),
      hidden: go(($) => { mark($('heading')); $('heading').hidden = true; }),
      invisible: go(($) => { mark($('heading')); $('heading').style.visibility = 'hidden'; }),
      nextMarked: go(($) => { mark($('low-no')); mark($('low-yes')); $('low-no').hidden = true; }),
      firstUnseen: go(($) => { $('low-no').style.visibility = 'hidden'; }),
      nothing: go(($) => { $('card').hidden = true; }),
    };
  },
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
