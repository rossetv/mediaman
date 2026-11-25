// Runs the real nav.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node nav_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function page(isPhone) {
  const doc = makeDocument();
  const button = (layout) => ['button', { class: 'profile-btn', id: layout, 'aria-expanded': 'false' }, ['span', { class: 'avatar' }, 'R']];
  const item = (id, tag) => [tag, { id, role: 'menuitem', href: '/' + id, tabindex: '-1' }, id];
  doc.body.appendChild(build(doc, ['header', { id: 'top' }, button('desktop')]));
  doc.body.appendChild(build(doc, ['div', { id: 'phone-top' }, button('phone')]));
  doc.body.appendChild(build(doc, ['div', { id: 'profile-menu' },
    ['div', { id: 'panel' }, ['div', { class: 'profile-who' }, ['span', { class: 'avatar' }, 'R']], ['button', { class: 'profile-close', id: 'close' }, ['svg', { id: 'close-icon' }]], item('account', 'a'), item('settings', 'a'), item('logout', 'button')]]));
  doc.body.appendChild(build(doc, ['main', { id: 'main' }, ['button', { id: 'elsewhere' }, 'x']]));
  doc.body.appendChild(build(doc, ['div', { id: 'already-inert' }]));
  const $ = (id) => doc.getElementById(id);
  $('already-inert').inert = true;
  const media = { matches: isPhone, on: [], addEventListener(t, fn) { this.on.push(fn); } };
  const win = { listeners: {}, matchMedia: () => media, addEventListener(t, fn) { win.listeners[t] = fn; } };
  // Only the button of the layout in use has a box.
  $('desktop').getClientRects = () => (media.matches ? [] : [1]);
  $('phone').getClientRects = () => (media.matches ? [1] : []);
  const images = [];
  function Image() { images.push(this); }
  const ctx = vm.createContext({ window: win, document: doc, Image });
  ['modal-a11y.js', 'nav.js'].forEach((file) => vm.runInContext(fs.readFileSync(path.join(JS, file), 'utf8'), ctx));

  const fire = (type, target, extra) => {
    const e = {
      target, prevented: false, stopped: false,
      preventDefault() { e.prevented = true; }, stopPropagation() { e.stopped = true; }, ...extra,
    };
    (doc.listeners[type] || []).forEach((fn) => fn(e));
    return e;
  };
  return {
    $, win, media, fire, images, doc,
    click: (id) => click(doc, $(id)),
    key: (key) => fire('keydown', doc.activeElement, { key }),
    focused: () => doc.activeElement.id,
    inert: () => doc.body.children.filter((n) => n.inert).map((n) => n.id),
    scrolls: () => doc.body.style.overflow !== 'hidden',
    open: () => $('profile-menu').hasAttribute('open'),
    menuInert: () => $('profile-menu').inert,
    expanded: (id) => $(id).getAttribute('aria-expanded'),
  };
}

const scenarios = {
  avatar() {
    const p = page(false);
    const avatars = p.doc.querySelectorAll('.avatar');
    const shown = () => avatars.map((a) => { const img = a.querySelector('img'); return img && img.src; });
    p.fire('mm:avatar', p.doc, { detail: { url: '/old' } });
    p.fire('mm:avatar', p.doc, { detail: { url: '/new' } });
    const out = { loading: shown() };
    p.images[0].onload(); out.stale = shown();
    p.images[1].onload(); out.shown = shown();
    out.initials = avatars.map((a) => a.textContent).join('');
    return out;
  },
  toggle() {
    const p = page(false);
    p.click('desktop');
    const opened = { open: p.open(), expanded: p.expanded('desktop'), focused: p.focused(), inert: p.inert() };
    p.click('desktop');
    return { opened, closed: { open: p.open(), expanded: p.expanded('desktop'), focused: p.focused() } };
  },
  keys() {
    const p = page(false);
    p.$('desktop').focus();
    const trail = [];
    const press = (k) => { p.key(k); trail.push(p.focused()); };
    press('ArrowDown'); press('ArrowDown'); press('ArrowDown'); press('ArrowDown');
    press('ArrowUp'); press('Home'); press('End');
    const esc = p.key('Escape');
    return { trail, open: p.open(), focused: p.focused(), escStopped: esc.stopped };
  },
  upOpensAtTheEnd() {
    const p = page(false);
    p.$('desktop').focus();
    p.key('ArrowUp');
    return { focused: p.focused() };
  },
  space() {
    const p = page(false);
    p.click('desktop');
    let clicks = 0;
    p.$('account').click = () => { clicks += 1; };
    const e = p.key(' ');
    return { clicks, prevented: e.prevented };
  },
  tab() {
    const p = page(false);
    p.click('desktop');
    const e = p.key('Tab');
    return { open: p.open(), focused: p.focused(), prevented: e.prevented };
  },
  leaving() {
    const p = page(false);
    const after = {};
    p.click('desktop'); p.fire('pointerdown', p.$('elsewhere')); after.press = p.open();
    p.click('desktop'); p.fire('focusin', p.$('elsewhere')); after.focus = p.open();
    p.click('desktop'); p.fire('pointerdown', p.$('panel')); after.inside = p.open();
    p.win.listeners.pageshow(); after.back = p.open();
    p.click('desktop'); p.click('settings'); after.link = p.open();
    return after;
  },
  sheet() {
    const p = page(true);
    p.click('phone');
    const held = { inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
    p.click('profile-menu');
    return { held, freed: { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() } };
  },
  // A dialog opens over the sheet, which closes as the focus leaves it for the dialog.
  dialogOverSheet() {
    const p = page(true);
    p.click('phone');
    const dialog = p.$('main').parentNode.appendChild(build(p.$('main').ownerDocument, ['div', { id: 'dialog' }, ['input', { id: 'password' }]]));
    p.win.MM.inertPage.hold(dialog);
    p.fire('focusin', p.$('password'));
    const over = { open: p.open(), inert: p.inert() };
    p.win.MM.inertPage.release(dialog);
    return { over, freed: p.inert() };
  },
  closeButton() {
    const p = page(true);
    p.click('phone');
    p.click('close-icon');
    return { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
  },
  // The CSS fade keeps a closed menu visible for a moment: only inert keeps it out of reach.
  fade() {
    const p = page(false);
    const fresh = p.menuInert();
    p.click('desktop');
    const opened = p.menuInert();
    p.click('desktop');
    const closed = { inert: p.menuInert(), focused: p.focused() };
    p.click('desktop');
    const reopened = { inert: p.menuInert(), focused: p.focused() };
    const sheet = page(true);
    sheet.click('phone');
    sheet.click('close-icon');
    return { fresh, opened, closed, reopened, sheet: { inert: sheet.menuInert(), focused: sheet.focused() } };
  },
  resize() {
    const p = page(true);
    p.click('phone');
    p.media.matches = false;
    p.media.on.forEach((fn) => fn());
    return { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
  },
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
