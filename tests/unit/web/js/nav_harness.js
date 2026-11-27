// Runs the real nav.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node nav_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click } = require('./fake_dom.js');

Node.prototype.click = function () { click(this.ownerDocument, this); };

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function page(isPhone, downloading, reduced, pane, stored, who = 'robin', storage) {
  const doc = makeDocument();
  const button = ['button', { class: 'profile-btn', id: 'button', 'aria-expanded': 'false', 'aria-label': 'Profile menu, ' + who },
    ['span', { class: 'avatar' }, 'R'], ['span', { class: 'profile-ring', id: 'ring' }], ['span', { class: 'profile-count', id: 'count' }],
    ['span', { class: 'profile-btn-name' }, who]];
  const item = (id, tag) => [tag, { id, role: 'menuitem', href: '/' + id, tabindex: '-1' }, id];
  doc.body.appendChild(build(doc, ['header', { id: 'top', class: 'nav-glass' }, ['a', { class: 'skip-link', id: 'skip', href: '#main-content' }, 'Skip'], button]));
  doc.body.appendChild(build(doc, ['div', { id: 'profile-menu' },
    ['div', { id: 'panel', class: 'profile-panel' }, ['div', { class: 'profile-handle', id: 'handle' }], ['div', { class: 'profile-who' }, ['span', { class: 'avatar' }, 'R']], ['button', { class: 'profile-close', id: 'close' }, ['svg', { id: 'close-icon' }]],
    ['div', { id: 'profile-menu-items' }, item('account', 'a'), item('settings', 'a'), item('logout', 'button')]]]));
  doc.body.appendChild(build(doc, ['main', { id: 'main' }, ['div', { id: 'main-content', tabindex: '-1' }], ['button', { id: 'elsewhere' }, 'x'],
    ['div', { class: 'dlg__head' }, ['a', { id: 'dl-back', href: '/' }, 'Back']]]));
  doc.body.appendChild(build(doc, ['div', { id: 'already-inert' }]));
  if (pane) doc.body.appendChild(build(doc, ['div', { id: 'you', hidden: '' }]));
  const $ = (id) => doc.getElementById(id);
  $('already-inert').inert = true;
  const media = { matches: isPhone, on: [], addEventListener(t, fn) { this.on.push(fn); } };
  const store = { ...stored };
  const localStorage = storage || { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
  const win = { listeners: {}, scrollY: 0, location: { hash: '', pathname: '/' }, localStorage, setTimeout: (fn) => fn(), matchMedia: (q) => (/reduced-motion/.test(q) ? { matches: !!reduced } : media), addEventListener(t, fn) { win.listeners[t] = fn; } };
  win.MM = { dom: { icon: () => doc.createElement('svg') } };
  if (downloading) win.MM.downloading = downloading;
  if (pane) win.MM = { ...win.MM, you: pane };
  win.visualViewport = { height: 800, scale: 1, addEventListener(t, fn) { win.listeners['seen ' + t] = fn; } };
  const images = [];
  function Image() { images.push(this); }
  const clock = { now: 1000000 };
  const ctx = vm.createContext({ window: win, document: doc, Image, Date: { now: () => clock.now } });
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
    $, win, media, fire, images, doc, clock, store,
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

// What is ready, as the strip lists it: *n* titles, each with a key of its own.
const readyItems = (n) => Array.from({ length: n }, (_, i) => ({ key: 'radarr::r' + i, state: 'ready', playable: true }));

const scenarios = {
  // The focus comes into the bar, moves in it, and leaves it for the page, then for nothing.
  barFocus() {
    const p = page(false);
    const marked = () => p.doc.documentElement.classList.contains('is-bar-focus');
    const out = [];
    p.fire('focusin', p.$('button')); out.push(marked());
    p.fire('focusout', p.$('button'), { relatedTarget: p.$('skip') }); p.fire('focusin', p.$('skip')); out.push(marked());
    p.fire('focusout', p.$('skip'), { relatedTarget: p.$('elsewhere') }); out.push(marked());
    p.fire('focusin', p.$('elsewhere')); out.push(marked());
    p.fire('focusin', p.$('button')); p.fire('focusout', p.$('button'), { relatedTarget: null }); out.push(marked());
    // Downloading's head stays in sight as the page scrolls, as the bar does.
    p.fire('focusin', p.$('dl-back')); out.push(marked());
    return out;
  },
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
  // The second tap of a double tap falls on what came up under the finger.
  doubleTap() {
    const at = (isPhone, ms, extra) => {
      const p = page(isPhone);
      p.click('button');
      p.clock.now += ms;
      let prevented = null;
      p.doc.addEventListener('click', (e) => { prevented = e.defaultPrevented; });
      p.click('logout', extra);
      return prevented;
    };
    const p = page(true);
    p.click('button');
    p.clock.now += 200;
    p.click('profile-menu', { detail: 1 });
    return { quick: at(true, 300, { detail: 1 }), slow: at(true, 500, { detail: 1 }), key: at(true, 100, { detail: 0 }), desktop: at(false, 100, { detail: 1 }), open: p.open() };
  },
  // The handle drags the panel after the finger.
  drag() {
    // steps: [y, ms since the last step]; the finger lifts `lift` ms after the last one.
    const run = (steps, lift, reduced, lose) => {
      const p = page(true, null, reduced);
      p.click('button');
      const handle = p.$('handle');
      const panel = p.$('panel');
      const send = (type, y) => (handle.listeners[type] || []).forEach((fn) => fn({ type, pointerId: 1, button: 0, clientY: y }));
      send('pointerdown', 100);
      if (lose) { send('pointermove', 150); send('lostpointercapture', 150); return { open: p.open(), transform: p.$('panel').style.transform, after: (send('pointerup', 150), p.open()) }; }
      const follow = [];
      steps.forEach(([y, ms]) => { p.clock.now += ms; send('pointermove', y); follow.push(panel.style.transform); });
      p.clock.now += lift;
      send('pointerup', steps[steps.length - 1][0]);
      const out = { follow, open: p.open(), transform: panel.style.transform };
      const log = [];
      const style = panel.style;
      panel.style = new Proxy(style, { set(t, k, v) { log.push(k + '=' + v); t[k] = v; return true; } });
      Object.defineProperty(panel, 'offsetWidth', { get() { log.push('layout'); return 0; } });
      const set = p.$('profile-menu').setAttribute;
      p.$('profile-menu').setAttribute = function (k, v) { if (k === 'open') log.push('open'); return set.call(this, k, v); };
      p.click('button');
      out.reopened = { open: p.open(), transform: panel.style.transform, log };
      return out;
    };
    return {
      far: run([[130, 10], [220, 10]], 10, false),
      short: run([[110, 10], [140, 10]], 400, false),
      slow: run([[110, 300], [160, 300], [175, 300]], 50, false),
      upward: run([[60, 10]], 10, false),
      // a pause, then a short quick flick
      flick: run([[100, 600], [115, 40], [140, 44]], 0, false),
      still: run([[130, 10], [220, 10]], 10, true),
      // pulled far down slowly, then flicked back up
      lost: run([], 0, false, true),
      back: run([[150, 300], [220, 300], [185, 30]], 10, false),
      // a slow release past the distance still shuts
      slowFar: run([[150, 300], [220, 300], [215, 300]], 10, false),
    };
  },
  // Off, the ring is set where it begins; on, it eases from where it stands.
  ringComesBack() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    const eased = [];
    p.$('ring').style.setProperty = () => eased.push(!p.$('ring').classList.contains('is-set'));
    const at = (total, percent) => tell({ summary: { total, active: total, waiting: 0, ready: 0, percent } });
    at(1, 40); at(1, 60); at(0, 0); at(1, 5); at(1, 9);
    return { eased, left: p.$('ring').classList.contains('is-set') };
  },
  // What is downloading, as the button and the menu tell it.
  downloads() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    const set = {};
    p.$('ring').style.setProperty = (k, v) => { set.ring = v; };
    const on = (id) => ['is-on', 'is-ready', 'is-waiting', 'is-lost'].filter((c) => p.$(id).classList.contains(c));
    const at = (active, waiting, ready, percent, lost) => {
      tell({ summary: { total: active + waiting + ready, active, waiting, ready, percent }, lost: !!lost, items: readyItems(ready) });
      return {
        name: p.$('button').getAttribute('aria-label'), ring: on('ring'), filled: set.ring,
        count: on('count').length ? p.$('count').textContent : null,
      };
    };
    return { none: at(0, 0, 0, 0), one: at(1, 0, 0, 42), many: at(2, 2, 0, 40), mixed: at(2, 1, 1, 57), ready: at(0, 1, 1, 100), waiting: at(0, 3, 0, 0), lost: at(2, 1, 1, 57, true), found: at(2, 2, 0, 40), ...seenThen(), over: at(0, 0, 0, 0, true) };
    // Once what is ready has been seen, it is told apart from what is still on its way.
    function seenThen() {
      p.fire('mm:ready-shown', p.doc, { detail: { keys: readyItems(2).map((item) => item.key) } });
      return { seenDownloading: at(1, 0, 2, 30), seenMixed: at(2, 1, 1, 57), seenWaiting: at(0, 1, 1, 0) };
    }
  },
  // The answer lists fewer titles than the summary counts: the label tells the listed ones.
  cappedReady() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    p.fire('mm:ready-shown', p.doc, { detail: { keys: readyItems(2).map((item) => item.key) } });
    const items = [...readyItems(2), { key: 'sonarr::s0', state: 'downloading' }];
    tell({ summary: { total: 4, active: 1, waiting: 0, ready: 3, percent: 30 }, lost: false, items });
    const mixed = p.$('button').getAttribute('aria-label');
    tell({ summary: { total: 3, active: 0, waiting: 0, ready: 3, percent: 100 }, lost: false, items: readyItems(2) });
    return { mixed, ready: p.$('button').getAttribute('aria-label') };
  },
  // The ring is its own link beside the face, there only while something downloads.
  ringLink() {
    let tell;
    const went = [];
    const p = page(false, { subscribe(fn) { tell = fn; } }, false, { go: (to) => went.push(to) });
    const link = () => p.doc.querySelector('[data-dl-open]');
    const at = (active, waiting, ready, percent, lost) => tell({ summary: { total: active + waiting + ready, active, waiting, ready, percent }, lost: !!lost, items: readyItems(ready) });
    const seen = () => { const l = link(); return l && { hidden: l.hasAttribute('hidden'), label: l.getAttribute('aria-label'), href: l.getAttribute('href'), tag: l.tagName.toLowerCase(), inButton: p.$('button').contains(l), sibling: l.parentNode === p.$('button').parentNode }; };
    const out = { off: seen() };
    at(2, 0, 0, 89);
    out.on = seen();
    at(0, 2, 0, 0);
    out.waiting = seen().label;
    at(0, 1, 2, 100);
    out.ready = seen().label;
    at(1, 0, 0, 40, true);
    out.lost = seen().label;
    const l = link();
    l.focus();
    out.focused = p.doc.activeElement === l;
    p.click('button');
    out.face = went.slice();
    out.menuAfterFace = p.open();
    went.length = 0;
    const tap = p.fire('click', l, { button: 0 });
    out.ring = went.slice();
    out.ringPrevented = tap.prevented;
    at(0, 0, 0, 0);
    out.gone = seen().hidden;
    return out;
  },
  // On Watch the button opens the pane over it, and never the menu.
  onWatch() {
    const went = [];
    const p = page(false, null, false, { go: (to) => went.push(to) });
    p.click('button');
    p.$('button').focus();
    const arrow = p.key('ArrowDown');
    return { went, open: p.open(), arrowed: arrow.prevented, popup: p.$('button').getAttribute('aria-haspopup'), controls: p.$('button').getAttribute('aria-controls') };
  },
  // Only what is ready and not seen yet is counted: seen in Downloading, or with its title opened.
  unseen() {
    let tell;
    const went = [];
    const stored = { 'mm:ready-seen:robin': JSON.stringify(['radarr::1', 'radarr::gone']) };
    const p = page(false, { subscribe(fn) { tell = fn; } }, false, null, stored);
    const ready = (n) => ({ key: 'radarr::' + n, state: 'ready', playable: true, ref: 'tmdb-movie-' + n, tmdb_ref: 'tmdb-movie-' + n, href: '/#title=tmdb-movie-' + n });
    const items = [ready(1), ready(2), ready(3), { key: 'radarr::4', state: 'ready', playable: false }, { key: 'radarr::5', state: 'downloading' }];
    const state = { summary: { total: 5, active: 1, waiting: 1, ready: 3, percent: 50 }, items };
    const link = () => p.doc.querySelector('[data-dl-open]');
    const look = () => ({
      count: p.$('count').classList.contains('is-on') ? p.$('count').textContent : null,
      badge: p.$('count').getAttribute('title'),
      ring: link().getAttribute('aria-label'),
      name: p.$('button').getAttribute('aria-label'),
      menu: p.$('profile-ready').hasAttribute('hidden') ? null : p.$('profile-ready').textContent,
    });
    tell(state);
    const out = { first: look(), pruned: JSON.parse(p.store['mm:ready-seen:robin']) };
    p.win.location.hash = '#title=tmdb-movie-2';
    p.win.listeners.hashchange();
    out.opened = look();
    p.fire('mm:ready-shown', p.doc, { detail: { keys: ['radarr::3'] } });
    out.shown = look();
    out.kept = JSON.parse(p.store['mm:ready-seen:robin']).sort();
    // A new tab remembers it.
    let again;
    const q = page(false, { subscribe(fn) { again = fn; } }, false, null, p.store);
    again(state);
    out.newTab = q.$('count').classList.contains('is-on');
    // Somebody else signed in on this browser has seen none of it.
    let theirs;
    const r = page(false, { subscribe(fn) { theirs = fn; } }, false, null, p.store, 'sam');
    theirs(state);
    out.otherPerson = r.$('count').classList.contains('is-on') ? r.$('count').textContent : null;
    return out;
  },
  // Ready titles past what the strip lists are never counted: Downloading could not show them.
  unlisted() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    tell({ summary: { total: 25, active: 0, waiting: 0, ready: 25, percent: 100 }, items: readyItems(20) });
    p.fire('mm:ready-shown', p.doc, { detail: { keys: readyItems(20).map((item) => item.key) } });
    return { count: p.$('count').classList.contains('is-on') ? p.$('count').textContent : null, name: p.$('button').getAttribute('aria-label') };
  },
  // A browser that refuses its storage still counts, and still forgets what was seen in this tab.
  noStorage() {
    let tell;
    const refuse = () => { throw new Error('SecurityError'); };
    const p = page(false, { subscribe(fn) { tell = fn; } }, false, null, {}, 'robin', { getItem: refuse, setItem: refuse });
    const state = { summary: { total: 2, active: 0, waiting: 0, ready: 2, percent: 100 }, items: readyItems(2) };
    tell(state);
    const count = () => (p.$('count').classList.contains('is-on') ? p.$('count').textContent : null);
    const out = { first: count() };
    p.fire('mm:ready-shown', p.doc, { detail: { keys: ['radarr::r0'] } });
    out.seen = count();
    return out;
  },
  // A tap on the number goes where the ring leads, and opens neither the menu nor the pane.
  badgeTap() {
    let tell;
    const went = [];
    const p = page(false, { subscribe(fn) { tell = fn; } }, false, { go: (to) => went.push(to) });
    tell({ summary: { total: 1, active: 0, waiting: 0, ready: 1, percent: 100 }, items: [{ key: 'radarr::1', state: 'ready', playable: true }] });
    const followed = [];
    p.doc.addEventListener('click', (e) => { if (e.target.hasAttribute && e.target.hasAttribute('data-dl-open')) followed.push(e.target.getAttribute('href')); });
    p.click('count');
    return { followed, went, open: p.open() };
  },
  // The menu's line of what is ready is passed over by the arrows while it is hidden.
  readyKeys() {
    let tell;
    const p = page(false, { subscribe(fn) { tell = fn; } });
    p.click('button');
    const out = { hidden: p.focused() };
    p.click('button');
    tell({ summary: { total: 1, active: 0, waiting: 0, ready: 1, percent: 100 }, items: [{ key: 'radarr::1', state: 'ready', playable: true }] });
    p.click('button');
    out.shown = p.focused();
    out.href = p.$('profile-ready').getAttribute('href');
    return out;
  },
  // Nothing tells of downloads: the rest of the bar works.
  noDownloads() {
    const p = page(false);
    p.click('button');
    return { open: p.open(), name: p.$('button').getAttribute('aria-label') };
  },
  // The brand takes an island of its own once the page has moved under the bar.
  scrolled() {
    const p = page(false);
    const at = (y) => { p.win.scrollY = y; p.win.listeners.scroll(); return p.$('top').classList.contains('is-scrolled'); };
    return { rest: p.$('top').classList.contains('is-scrolled'), down: at(300), nudged: at(3), back: at(0) };
  },
};

process.stdout.write(JSON.stringify(scenarios[process.argv[2]]()));
