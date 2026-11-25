// Runs the real profile-picture.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node profile_picture_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
Node.prototype.scrollIntoView = () => {};

const b = (id, text) => ['button', { id }, text || ''];
const DIALOG = (attrs) => ['div', { id: 'pic-dialog', class: 'modal-backdrop', role: 'dialog', 'aria-hidden': 'true', ...attrs },
  ['h2', { id: 'pic-title' }], b('pic-close'),
  ['div', { id: 'pic-body' }],
  ['div', { id: 'pic-pick' }, ['div', { id: 'pic-error' }], ['img', { id: 'pic-now' }], ['div', { id: 'pic-kind' }],
    b('pic-upload'), b('pic-remove'),
    ...[1, 2, 3, 4].map((n) => ['label', {}, ['input', { type: 'radio', value: n }], ['img', { src: '/d' + n }]]),
    b('pic-cancel'), b('pic-save', 'Save')],
  ['div', { id: 'pic-place' }, ['div', { id: 'pic-crop' }, ['canvas', {}]], ['input', { id: 'pic-zoom' }], b('pic-back'), b('pic-use')],
  ['input', { id: 'pic-file' }]];

// `decode` is the size a chosen file opens at, or null where the browser cannot open it.
function page({ attrs = { 'data-choice': '', 'data-default': '2' }, api = {}, fetch, decode = [800, 600], calm = true, wraps = false, blob = { type: 'image/jpeg' } } = {}) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.body.appendChild(build(doc, ['main', {}, ['button', { id: 'pic-open' }, ['img', { src: '/d2' }]]]));
  doc.body.appendChild(build(doc, DIALOG(attrs)));
  const $ = (id) => doc.getElementById(id);
  const vars = {};
  $('pic-zoom').style.setProperty = (k, v) => { vars[k] = v; };
  const runs = [];
  $('pic-body').animate = (frames) => { const run = { cancelled: false, cancel() { run.cancelled = true; } }; runs.push({ run, from: frames[0].height, to: frames[1].height }); return run; };
  Object.defineProperty($('pic-body'), 'offsetHeight', { get: () => ($('pic-dialog').hidden ? 0 : ($('pic-place').hidden ? 417 : 562) + ($('pic-error').textContent ? 74 : 0)
    + (wraps && $('pic-place').hidden && !$('pic-remove').hidden ? 51 : 0)) });
  $('pic-dialog').hidden = true;
  $('pic-dialog').querySelectorAll('input').forEach((r) => { r.value = r.getAttribute('value'); });
  const drawn = [];
  const outs = [];
  const make = doc.createElement;
  const asCanvas = (node) => {
    node.getContext = () => ({ clearRect() {}, drawImage: (...a) => drawn.push({ on: node, a: a.slice(1) }) });
    node.toBlob = (give, type) => { outs.push({ size: [node.width, node.height], type }); give(blob); };
    return node;
  };
  doc.createElement = (tag) => (tag === 'canvas' ? asCanvas(make(tag)) : make(tag));
  asCanvas($('pic-crop').querySelector('canvas'));
  $('pic-crop').clientWidth = 320;
  $('pic-crop').setPointerCapture = () => {};
  $('pic-crop').getBoundingClientRect = () => ({ left: 0, top: 0 });
  const toasts = [];
  const told = [];
  doc.dispatchEvent = (e) => told.push([e.type, e.detail.url]);
  let made = 0;
  let revoked = 0;
  class Img {
    set src(_v) {
      if (decode && decode.later) decode.later.push(() => { this.naturalWidth = this.naturalHeight = 500; this.onload(); });
      else if (decode) { [this.naturalWidth, this.naturalHeight] = decode; this.onload(); } else this.onerror();
    }
  }
  class APIError extends Error {
    constructor(error, message, status, issues, data) { super(message); Object.assign(this, { error, status, data: data || {} }); }
  }
  let redirects = 0;
  const ctx = {
    document: doc, fetch, Image: Img, Math, Object, Number, Promise, JSON,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    URL: { createObjectURL: () => 'blob:' + (made += 1), revokeObjectURL: () => { revoked += 1; } },
    UIFeedback: { toast: (t) => toasts.push(t), error: (t) => toasts.push('error: ' + t) },
    setTimeout: () => 0, clearTimeout: () => {}, matchMedia: () => ({ matches: calm }), parseFloat,
    getComputedStyle: () => ({ getPropertyValue: (n) => (n === '--dur-slow' ? ' .25s' : ' ease-out') }),
  };
  ctx.ModalA11y = { register() {}, onOpened() {}, onClosed() {} };
  ctx.window = ctx;
  ctx.MM = { api: { APIError, messageOf: (e, d) => (e.error === 'network_error' ? e.message : e.data && e.data.message) || d,
    get: () => { redirects += 1; return new Promise(() => {}); }, ...api } };
  vm.createContext(ctx);
  ['core/dom.js', 'core/modal.js', 'profile-picture.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  ctx.MM.dom.holdPage = () => () => {};
  const fire = (id, type, e) => ($(id).listeners[type] || []).forEach((fn) => fn({ preventDefault() {}, target: $(id), stopImmediatePropagation() {}, ...e }));
  const radios = $('pic-pick').querySelectorAll('input');
  return {
    $, vars, runs, drawn, outs, toasts, told, fire,
    click: (id) => { if (!$(id).disabled) fire(id, 'click'); },
    pick: (n) => radios[n - 1].listeners.change.forEach((fn) => fn({})),
    file: (f) => { $('pic-file').files = [f]; fire('pic-file', 'change'); },
    error: () => $('pic-error').textContent,
    radiosFree: () => radios.every((r) => !r.disabled),
    checked: () => radios.filter((r) => r.checked).map((r) => r.value).join(),
    open: () => $('pic-dialog').getAttribute('aria-hidden') === 'false',
    placing: () => !$('pic-place').hidden,
    state: () => [$('pic-kind').textContent, $('pic-remove').hidden, $('pic-save').disabled],
    left: () => made - revoked,
    redirects: () => redirects,
    key: (key, more) => fire('pic-crop', 'keydown', { key, ...more }),
    at: () => { const { a } = drawn.filter((d) => d.on === $('pic-crop').querySelector('canvas')).pop(); return a.map(Math.round); },
  };
}

const JPEG = { type: 'image/jpeg', size: 5000, name: 'me.jpg' };
const answer = (status, body) => () => Promise.resolve({ ok: status < 400, status, json: () => (body ? Promise.resolve(body) : Promise.reject(new Error('html'))) });

const scenarios = {
  async pick() {
    const sent = [];
    const p = page({ api: {
      put: (url, body) => { sent.push(['PUT', url, body]); return Promise.resolve({ kind: 'drawn', choice: 3, url: '/d3' }); },
      delete: (url) => { sent.push(['DELETE', url]); return Promise.resolve({ kind: 'default', choice: null, url: '/d2' }); },
    } });
    p.click('pic-open');
    const first = { state: p.state(), checked: p.checked() };
    p.pick(3);
    const picked = { state: p.state(), now: p.$('pic-now').src, sentYet: sent.length };
    p.click('pic-save');
    p.click('pic-save');
    await settle();
    const saved = { open: p.open(), told: p.told.splice(0), toasts: p.toasts.splice(0), head: p.$('pic-open').querySelector('img').src };
    p.click('pic-open');
    const again = { state: p.state(), checked: p.checked() };
    p.click('pic-remove');
    const removed = p.state();
    p.click('pic-save');
    await settle();
    return { first, picked, saved, again, removed, last: { told: p.told, toasts: p.toasts }, sent };
  },
  async cancel() {
    const p = page();
    p.click('pic-open');
    p.pick(4);
    p.click('pic-cancel');
    const open = p.open();
    p.click('pic-open');
    return { open, state: p.state(), checked: p.checked(), told: p.told };
  },
  async files() {
    const say = (file, decode) => {
      const p = page({ decode });
      p.click('pic-open');
      p.file(file);
      return [p.error(), p.placing(), p.left()];
    };
    return {
      gif: say({ type: 'image/gif', size: 5, name: 'a.gif' }),
      big: say({ ...JPEG, size: 14 * 1048576 }),
      justOver: say({ ...JPEG, size: 10 * 1048576 + 1 }),
      small: say(JPEG, [90, 72]),
      broken: say(JPEG, null),
      heicHere: say({ type: 'image/heic', size: 5, name: 'a.heic' }),
      heicNot: say({ type: '', size: 5, name: 'A.HEIC' }, null),
      good: say(JPEG),
    };
  },
  async place() {
    const p = page();
    p.click('pic-open');
    p.file(JPEG);
    const first = p.at();
    p.key('ArrowLeft');
    const left = p.at();
    p.key('ArrowDown');
    const stuck = p.at(); // as high as it is, it has no room up or down
    for (let i = 0; i < 40; i += 1) p.key('ArrowRight');
    const edge = p.at();
    for (let i = 0; i < 40; i += 1) p.key('+');
    const most = p.$('pic-zoom').value;
    p.key('-', { metaKey: true });
    const browsers = p.$('pic-zoom').value;
    for (let i = 0; i < 40; i += 1) p.key('-');
    const least = p.$('pic-zoom').value;
    const down = (id, x, y) => p.fire('pic-crop', 'pointerdown', { pointerId: id, clientX: x, clientY: y, button: 0 });
    const move = (id, x, y) => p.fire('pic-crop', 'pointermove', { pointerId: id, clientX: x, clientY: y });
    const before = p.at();
    down(1, 100, 160);
    move(1, 110, 160);
    const dragged = p.at()[0] - before[0];
    down(2, 210, 160);
    move(2, 310, 160);
    const pinched = p.$('pic-zoom').value;
    ['1', '2'].forEach((id) => p.fire('pic-crop', 'pointerup', { pointerId: id }));
    move(1, 150, 160);
    const released = p.$('pic-zoom').value;
    p.fire('pic-crop', 'wheel', { deltaY: -100 });
    return { first, left, stuck, edge, most, browsers, least, dragged, pinched, released, wheeled: p.$('pic-zoom').value > released };
  },
  async cut() {
    const run = (decode, type, zoom) => {
      const p = page({ decode });
      p.click('pic-open');
      p.file({ ...JPEG, type });
      if (zoom) { p.$('pic-zoom').value = zoom; p.fire('pic-zoom', 'input'); }
      p.click('pic-use');
      const from = p.drawn[p.drawn.length - 1].a.slice(0, 4).map(Math.round);
      return { ...p.outs[0], from, work: [p.drawn[0].on.width, p.drawn[0].on.height], state: p.state(), placing: p.placing(), focus: p.$('pic-save') === p.$('pic-save').ownerDocument.activeElement };
    };
    return { plain: run([800, 600], 'image/jpeg'), small: run([130, 128], 'image/png', 400), huge: run([9000, 6000], 'image/webp') };
  },
  async photo() {
    const run = async (reply, { leave, blob } = {}) => {
      const sent = [];
      const p = page({ blob, fetch: (url, o) => { sent.push([o.method, url, o.headers['Content-Type'], o.body.type]); return reply(); } });
      p.click('pic-open');
      p.file(JPEG);
      p.click('pic-use');
      p.click('pic-save');
      if (leave) p.click('pic-close');
      await settle();
      return { sent, error: p.error(), open: p.open(), told: p.told, toasts: p.toasts, label: p.$('pic-save').textContent, state: p.state(), left: p.left(), redirects: p.redirects() };
    };
    return {
      saved: await run(answer(200, { kind: 'photo', choice: null, url: '/api/avatars/1?v=x' })),
      refused: await run(answer(400, { error: 'too_small', message: 'Too small.' })),
      proxy: await run(answer(413)),
      broken: await run(answer(502)),
      offline: await run(() => Promise.reject(new TypeError('x'))),
      signedOut: await run(answer(401, {})),
      left: await run(answer(400, { message: 'Too small.' }), { leave: true }),
      unmade: await run(answer(200, {}), { blob: null }),
    };
  },
  // closed while a photo is saved over another, opened again before the answer
  async reopened() {
    let give;
    const sent = [];
    const p = page({
      attrs: { 'data-choice': '', 'data-default': '2', 'data-photo': '/old' },
      api: { delete: (url) => { sent.push('DELETE ' + url); return new Promise(() => {}); } },
      fetch: (url, o) => { sent.push(o.method + ' ' + url); return new Promise((r) => { give = r; }); },
    });
    p.click('pic-open');
    p.file(JPEG);
    p.click('pic-use');
    p.click('pic-save');
    p.click('pic-close');
    p.click('pic-open');
    give(await answer(200, { kind: 'photo', choice: null, url: '/new' })());
    await settle();
    const after = { open: p.open(), state: p.state(), now: p.$('pic-now').src };
    p.$('pic-save').disabled = false;
    p.click('pic-save');
    await settle();
    return { after, sent };
  },
  // what can be pressed while a picture is saved, and a photo still being read as Save is pressed
  async locked() {
    let give;
    const decode = { later: [] };
    const p = page({ decode, api: { put: () => new Promise((r, no) => { give = no; }) } });
    const free = () => ['pic-upload', 'pic-remove'].map((id) => !p.$(id).disabled).concat(p.radiosFree(), !p.$('pic-close').disabled);
    p.click('pic-open');
    p.pick(3);
    const before = free();
    p.click('pic-save');
    const during = free();
    give(new Error('x'));
    await settle();
    const after = free();
    p.file(JPEG);
    const reading = p.$('pic-save').disabled;
    decode.later.forEach((open) => open());
    return { before, during, after, reading, placing: p.placing(), thenSave: (p.click('pic-back'), p.$('pic-save').disabled) };
  },
  async notJson() {
    const out = [];
    for (const way of ['photo', 'drawn']) {
      const p = page({ fetch: answer(200), api: { put: () => Promise.resolve({}) } });
      p.click('pic-open');
      if (way === 'photo') { p.file(JPEG); p.click('pic-use'); } else p.pick(3);
      p.click('pic-save');
      await settle();
      out.push({ error: p.error(), told: p.told, toasts: p.toasts, head: p.$('pic-open').querySelector('img').getAttribute('src'), open: p.open() });
    }
    return out;
  },
  // the height while the photo is placed and left again, in full motion and with motion reduced
  async height() {
    const run = (calm) => {
      const p = page({ calm });
      p.click('pic-open');
      p.file(JPEG);
      p.click('pic-back');
      return p.runs.map((r) => [r.from, r.to, r.run.cancelled]);
    };
    return { full: run(false), calm: run(true) };
  },
  // a refused file and its reason gone again, and what is brought into view
  async errorHeight() {
    const p = page({ calm: false });
    const shown = [];
    Node.prototype.scrollIntoView = function (how) { shown.push([this.className, how.block]); };
    p.click('pic-open');
    p.file({ type: 'image/gif', size: 5, name: 'a.gif' });
    p.pick(3);
    p.pick(4);
    return { runs: p.runs.map((r) => [r.from, r.to]), shown };
  },
  // on a narrow phone, where Remove goes under Upload: a drawing picked, removed, and a photo placed
  async wraps() {
    const p = page({ calm: false, wraps: true });
    const step = (go) => { go(); return p.runs.splice(0).map((r) => [r.from, r.to]); };
    p.click('pic-open');
    return {
      picked: step(() => p.pick(3)), removed: step(() => p.click('pic-remove')),
      placed: step(() => { p.file(JPEG); p.click('pic-use'); }),
    };
  },
  async fill() {
    const p = page();
    p.click('pic-open');
    p.file(JPEG);
    const out = [p.vars['--at']];
    p.$('pic-zoom').value = 250;
    p.fire('pic-zoom', 'input');
    return out.concat(p.vars['--at']);
  },
  async backdrop() {
    const p = page();
    p.click('pic-open');
    const stopped = [];
    const press = (from) => {
      p.fire('pic-dialog', 'pointerdown', { target: p.$(from) });
      let stop = false;
      for (const fn of p.$('pic-dialog').listeners.click) {
        if (stop) break;
        fn({ target: p.$('pic-dialog'), stopImmediatePropagation() { stop = true; } });
      }
      stopped.push(p.open());
    };
    press('pic-crop');
    press('pic-dialog');
    return stopped;
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
