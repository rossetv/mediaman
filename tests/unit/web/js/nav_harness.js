// Runs the real nav.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node nav_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function page(isPhone, downloading) {
  const doc = makeDocument();
  const button = ['button', { class: 'profile-btn', id: 'button', 'aria-expanded': 'false', 'aria-label': 'Profile menu, robin' },
    ['span', { class: 'avatar' }, 'R'], ['span', { class: 'profile-ring', id: 'ring' }], ['span', { class: 'profile-count', id: 'count' }]];
  const item = (id, tag) => [tag, { id, role: 'menuitem', href: '/' + id, tabindex: '-1' }, id];
  doc.body.appendChild(build(doc, ['header', { id: 'top' }, ['a', { class: 'skip-link', id: 'skip', href: '#main-content' }, 'Skip'], button]));
  doc.body.appendChild(build(doc, ['div', { id: 'profile-menu' },
    ['div', { id: 'panel' }, ['div', { class: 'profile-who' }, ['span', { class: 'avatar' }, 'R']], ['button', { class: 'profile-close', id: 'close' }, ['svg', { id: 'close-icon' }]], item('account', 'a'), item('settings', 'a'), ['span', { id: 'profile-note', hidden: '' }], item('logout', 'button')]]));
  doc.body.appendChild(build(doc, ['main', { id: 'main' }, ['div', { id: 'main-content', tabindex: '-1' }], ['button', { id: 'elsewhere' }, 'x']]));
  doc.body.appendChild(build(doc, ['div', { id: 'already-inert' }]));
  const $ = (id) => doc.getElementById(id);
  $('already-inert').inert = true;
  const media = { matches: isPhone, on: [], addEventListener(t, fn) { this.on.push(fn); } };
  const win = { listeners: {}, location: { hash: '' }, matchMedia: () => media, addEventListener(t, fn) { win.listeners[t] = fn; } };
  if (downloading) win.MM = { downloading };
  win.visualViewport = { height: 800, scale: 1, addEventListener(t, fn) { win.listeners['seen ' + t] = fn; } };
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
    click: (id, extra) => click(doc, $(id), extra),
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
  // The skip link pressed again on the address it already led to: the browser moves the
  // focus but leaves the page where it was.
  skip() {
    const p = page(false);
    const scrolled = [];
    p.$('main-content').scrollIntoView = () => scrolled.push(1);
    let prevented = false;
    p.doc.addEventListener('click', (e) => { prevented = e.defaultPrevented; });
    const press = (extra) => {
      scrolled.length = 0;
      p.$('elsewhere').focus();
      p.click('skip', extra);
      return { prevented, scrolled: scrolled.length, focused: p.focused() };
    };
    const out = { first: press({}) };
    p.win.location.hash = '#main-content';
    out.again = press({});
    out.ctrl = press({ ctrlKey: true });
    out.middle = press({ button: 1 });
    p.win.location.hash = '#elsewhere';
    out.otherAddress = press({});
    return out;
  },
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
  // How the focus goes back to a button in the bar: each way of closing, and the layout changing.
  focusBack() {
    const ways = {
      escape: (p) => p.key('Escape'), tab: (p) => p.key('Tab'), close: (p) => p.click('close'),
      button: (p) => p.click('button'), resize: (p) => { p.media.matches = false; p.media.on.forEach((fn) => fn()); },
    };
    return Object.fromEntries(Object.entries(ways).map(([name, way]) => {
      const p = page(true);
      p.click('button');
      const calls = [];
      const focus = p.$('button').focus;
      p.$('button').focus = function (how) { calls.push(Boolean(how && how.preventScroll)); focus.call(this, how); };
      way(p);
      return [name, calls];
    }));
  },
  toggle() {
    const p = page(false);
    p.click('button');
    const opened = { open: p.open(), expanded: p.expanded('button'), focused: p.focused(), inert: p.inert() };
    p.click('button');
    return { opened, closed: { open: p.open(), expanded: p.expanded('button'), focused: p.focused() } };
  },
  keys() {
    const p = page(false);
    p.$('button').focus();
    const trail = [];
    const press = (k) => { p.key(k); trail.push(p.focused()); };
    press('ArrowDown'); press('ArrowDown'); press('ArrowDown'); press('ArrowDown');
    press('ArrowUp'); press('Home'); press('End');
    const esc = p.key('Escape');
    return { trail, open: p.open(), focused: p.focused(), escStopped: esc.stopped };
  },
  upOpensAtTheEnd() {
    const p = page(false);
    p.$('button').focus();
    p.key('ArrowUp');
    return { focused: p.focused() };
  },
  space() {
    const p = page(false);
    p.click('button');
    let clicks = 0;
    p.$('account').click = () => { clicks += 1; };
    const e = p.key(' ');
    return { clicks, prevented: e.prevented };
  },
  tab() {
    const p = page(false);
    p.click('button');
    const e = p.key('Tab');
    return { open: p.open(), focused: p.focused(), prevented: e.prevented };
  },
  leaving() {
    const p = page(false);
    const after = {};
    p.click('button'); p.fire('pointerdown', p.$('elsewhere')); after.press = p.open();
    p.click('button'); p.fire('focusin', p.$('elsewhere')); after.focus = p.open();
    p.click('button'); p.fire('pointerdown', p.$('panel')); after.inside = p.open();
    p.win.listeners.pageshow(); after.back = p.open();
    p.click('button'); p.click('settings'); after.link = p.open();
    return after;
  },
  sheet() {
    const p = page(true);
    p.click('button');
    const held = { inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
    p.click('profile-menu');
    return { held, freed: { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() } };
  },
  // A dialog opens over the sheet, which closes as the focus leaves it for the dialog.
  dialogOverSheet() {
    const p = page(true);
    p.click('button');
    const dialog = p.$('main').parentNode.appendChild(build(p.$('main').ownerDocument, ['div', { id: 'dialog' }, ['input', { id: 'password' }]]));
    p.win.MM.inertPage.hold(dialog);
    p.fire('focusin', p.$('password'));
    const over = { open: p.open(), inert: p.inert() };
    p.win.MM.inertPage.release(dialog);
    return { over, freed: p.inert() };
  },
  closeButton() {
    const p = page(true);
    p.click('button');
    p.click('close-icon');
    return { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
  },
  // The CSS fade keeps a closed menu visible for a moment: only inert keeps it out of reach.
  fade() {
    const p = page(false);
    const fresh = p.menuInert();
    p.click('button');
    const opened = p.menuInert();
    p.click('button');
    const closed = { inert: p.menuInert(), focused: p.focused() };
    p.click('button');
    const reopened = { inert: p.menuInert(), focused: p.focused() };
    const sheet = page(true);
    sheet.click('button');
    sheet.click('close-icon');
    return { fresh, opened, closed, reopened, sheet: { inert: sheet.menuInert(), focused: sheet.focused() } };
  },
  resize() {
    const p = page(true);
    p.click('button');
    p.media.matches = false;
    p.media.on.forEach((fn) => fn());
    return { open: p.open(), inert: p.inert(), scrolls: p.scrolls(), focused: p.focused() };
  },
  // What is downloading, as the button and the menu tell it.
  downloads() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    const set = {};
    ['ring', 'profile-note'].forEach((id) => { p.$(id).style.setProperty = (k, v) => { set[id] = v; }; });
    const on = (id) => ['is-on', 'is-ready', 'is-waiting'].filter((c) => p.$(id).classList.contains(c));
    const at = (active, waiting, ready, percent) => {
      tell({ summary: { total: active + waiting + ready, active, waiting, ready, percent } });
      const note = p.$('profile-note');
      return {
        name: p.$('button').getAttribute('aria-label'), ring: on('ring'), filled: set.ring,
        count: on('count').length ? p.$('count').textContent : null,
        note: note.hidden ? null : note.textContent, tone: on('profile-note'), bar: note.querySelectorAll('i').length,
      };
    };
    return { none: at(0, 0, 0, 0), one: at(1, 0, 0, 42), many: at(2, 2, 0, 40), mixed: at(2, 1, 1, 57), ready: at(0, 1, 1, 100), waiting: at(0, 3, 0, 0), over: at(0, 0, 0, 0) };
  },
  // Nothing tells of downloads: the rest of the bar works.
  noDownloads() {
    const p = page(false);
    p.click('button');
    return { open: p.open(), name: p.$('button').getAttribute('aria-label') };
  },
  // A keep menu 60px above a phone's bar at the foot, and one with no such bar.
  keepMenu() {
    const at = (footed) => {
      const doc = makeDocument();
      const box = (node, top, bottom) => { node.getBoundingClientRect = () => ({ top, bottom }); return node; };
      box(doc.body.appendChild(build(doc, ['header', { class: 'nav-glass' }])), 0, 48);
      box(doc.body.appendChild(build(doc, ['div', { class: 'nav-foot' }])), 772, 844);
      const wrap = box(doc.body.appendChild(build(doc, ['div', { class: 'keep-wrapper' },
        ['button', { class: 'keep-trigger', id: 'keep' }], ['div', { class: 'snooze-dropdown' }]])), 680, 700);
      const menu = wrap.querySelector('.snooze-dropdown');
      Object.defineProperty(menu, 'offsetHeight', { value: 100 });
      const win = { innerHeight: 844, addEventListener() {} };
      const getComputedStyle = (n) => ({ display: n.classList.contains('nav-foot') && !footed ? 'none' : 'block' });
      vm.runInContext(fs.readFileSync(path.join(JS, 'snooze-menu.js'), 'utf8'), vm.createContext({ window: win, document: doc, getComputedStyle }));
      click(doc, doc.getElementById('keep'));
      return wrap.classList.contains('opens-up');
    };
    return { phone: at(true), wide: at(false) };
  },
  keyboard() {
    const p = page(true);
    const root = p.doc.documentElement;
    root.clientHeight = 800;
    const at = (height, scale) => {
      Object.assign(p.win.visualViewport, { height, scale });
      p.win.listeners['seen resize']();
      return root.classList.contains('has-keyboard');
    };
    return { whole: at(800, 1), toolbar: at(720, 1), keyboard: at(450, 1), zoomed: at(400, 2), zoomedTyping: at(225, 2), away: at(800, 1) };
  },
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
