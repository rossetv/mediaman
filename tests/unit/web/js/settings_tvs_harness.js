// Runs the TVs & Casting section's script against the small fake DOM and prints one scenario's
// outcome as JSON. Usage: node settings_tvs_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Object.defineProperty(Node.prototype, 'dataset', {
  get() {
    return new Proxy({}, {
      get: (_, k) => { const v = this.getAttribute('data-' + k); return v === null ? undefined : v; },
      set: (_, k, v) => { this.setAttribute('data-' + k, v); return true; },
    });
  },
});
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };
Node.prototype.select = function () {};

const PAIRED = { id: 1, kind: 'page', name: 'Living room', owner: 'maren', on: true };
const DEN = { id: 2, kind: 'page', name: 'Den', owner: 'tobias', on: false };

async function page(answers, all = [PAIRED, DEN], reduced = true) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { class: 'setg-pg' },
    ['div', { id: 'tv-list-wrap' }],
    ['details', { id: 'cast-more' }, ['summary', { id: 'cast-more-toggle' }, 'What this lets in']]]));
  doc.addEventListener = () => {};
  const folded = [];
  const calls = [];
  const call = (method) => (url, body) => {
    calls.push([method, url].concat(body ? [body] : []));
    const answer = answers[method + ' ' + url];
    return typeof answer === 'function' ? answer() : Promise.resolve(answer);
  };
  answers['GET /api/tvs/all'] = answers['GET /api/tvs/all'] || { tvs: all };
  const said = [];
  const dropped = [];
  const went = [];
  const ctx = {
    document: doc, console, Promise, Date, setTimeout: (f, ms) => { if (!ms) f(); },
    matchMedia: () => ({ matches: reduced }),
    MM: {
      api: { get: call('GET'), post: call('POST'), patch: call('PATCH'), delete: call('DELETE'),
        messageOf: (e, words) => e.message || words },
      tvRemote: { drop: (id) => dropped.push(id) },
      dom: { focusStillIn: () => true, icon: (name) => build(doc, ['svg', { class: name }]),
        fold: (details, toggle) => folded.push([details.id, toggle.id]) },
      settings: { rows: { enter() {}, bringIntoView() {}, leave: (row, o) => { went.push(row.className); row.remove(); if (o && o.done) o.done(); } } },
    },
    UIFeedback: { success: (m) => said.push(m) },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(JS, 'settings', 'tvs.js'), 'utf8'), ctx);
  ctx.MM.settings.tvs.init();
  await settle();
  const within = (root, words) => root.querySelectorAll('button').find((b) => b.textContent === words);
  const row = (n) => doc.querySelectorAll('.tv-li')[n];
  return {
    dropped,
    went,
    folded,
    doc, calls, said, row,
    press: async (root, words) => { click(doc, within(root, words)); await settle(); },
    button: within,
    list: doc.getElementById('tv-list-wrap'),
    fire: (el, type) => (el.listeners[type] || []).forEach((f) => f({ target: el, preventDefault() {} })),
  };
}

const refused = (error, message) => () => Promise.reject({ error, message, data: {} });

const scenarios = {
  async list() {
    const p = await page({});
    const names = (li) => li.querySelectorAll('button').map((b) => b.textContent);
    return { rows: p.doc.querySelectorAll('.tv-li').map((li) => [li.querySelector('.tv-sub').textContent, names(li)]),
      asked: p.calls.map((c) => c.slice(0, 2).join(' ')), folded: p.folded };
  },
  async empty() { return (await page({}, [])).list.textContent; },
  async unloaded() {
    const answers = { 'GET /api/tvs/all': refused('x') };
    const p = await page(answers);
    const before = p.list.textContent;
    answers['GET /api/tvs/all'] = { tvs: [PAIRED] };
    await p.press(p.list, 'Try again');
    return { before, after: p.doc.querySelectorAll('.tv-li').length };
  },
  async leaving() {
    const p = await page({}, [PAIRED, DEN], false);
    const done = (n) => p.fire(n, 'animationend');
    await p.press(p.row(1), 'Unpair');
    const first = p.row(1).querySelector('.tv-sure');
    await p.press(p.row(1), 'Cancel');
    const out = { question: [first.isConnected, first.className, first.inert] };
    done(first);
    out.gone = first.isConnected;
    const still = await page({});
    await still.press(still.row(1), 'Unpair');
    await still.press(still.row(1), 'Cancel');
    return Object.assign(out, { reduced: !still.row(1).querySelector('.tv-sure') });
  },
  async forget() {
    const p = await page({});
    await p.press(p.row(1), 'Unpair');
    const asked = p.calls.some((c) => c[0] === 'DELETE');
    await p.press(p.row(1), 'Cancel');
    const kept = p.doc.querySelectorAll('.tv-li').length;
    await p.press(p.row(1), 'Unpair');
    await p.press(p.row(1), 'Unpair this TV');
    await p.press(p.row(0), 'Unpair');
    await p.press(p.row(0), 'Unpair this TV');
    return { asked, kept, deleted: p.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), left: p.list.textContent, dropped: p.dropped };
  },
  // A TV paired with two people: each can be unpaired alone, or the TV for both.
  async people() {
    const p = await page({ 'DELETE /api/tvs/1/people/tobias': {}, 'DELETE /api/tvs/1?everyone=1': {} },
      [{ ...PAIRED, people: ['maren', 'tobias'] }]);
    const before = p.row(0).querySelector('.tv-sub').textContent;
    const x = p.row(0).querySelectorAll('button').find((b) => b.getAttribute('aria-label') === 'Unpair tobias from Living room');
    click(p.doc, x);
    await settle();
    const after = p.row(0).querySelector('.tv-sub').textContent;
    return { before, after, chips: p.row(0).querySelectorAll('.tv-ppl li').length, deleted: p.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), went: p.went };
  },
  // The buttons of each panel in the order they are drawn, and every word a person reads there.
  async order() {
    const p = await page({ 'DELETE /api/tvs/1': refused('x') });
    const names = (root) => root.querySelectorAll('button').map((b) => b.textContent);
    const read = (root) => [root.textContent].concat(root.querySelectorAll('[aria-label]').map((n) => n.getAttribute('aria-label')));
    const out = { row: names(p.row(0)), words: read(p.row(0)) };
    await p.press(p.row(0), 'Unpair');
    const sure = p.row(0).querySelector('.tv-sure');
    out.sure = [names(sure), p.doc.activeElement === p.button(sure, 'Cancel')];
    await p.press(sure, 'Unpair this TV');
    out.words = out.words.concat(read(p.row(0)));
    await p.press(p.row(1), 'Unpair');
    out.again = p.row(1).querySelector('.tv-sure').textContent;
    await p.press(p.row(1), 'Rename');
    out.rename = names(p.row(1).querySelector('.tv-edit'));
    return out;
  },
  async rename() {
    const answers = { 'PATCH /api/tvs/1': refused('x', 'No such luck') };
    const p = await page(answers);
    await p.press(p.row(0), 'Rename');
    p.row(0).querySelector('input').value = 'Den';
    await p.press(p.row(0), 'Save');
    const failed = p.row(0).textContent.includes('No such luck') && !!p.row(0).querySelector('input');
    answers['PATCH /api/tvs/1'] = { tv: { id: 1, name: 'Den' } };
    await p.press(p.row(0), 'Save');
    return { failed, name: p.row(0).querySelector('.tv-lbl').textContent };
  },
  // The page's own save bar, with the section's switch and a TV's name being typed.
  async saved() {
    const doc = makeDocument();
    doc.addEventListener = () => {};
    const field = (id) => ['input', { id }];
    doc.body.appendChild(build(doc, ['div', { class: 'setg-pg' },
      field('google_cast_enabled'),
      ['ul', { class: 'tv-list' }, field('tv-name')],
      ['div', { id: 'setg-savebar', class: 'savebar' }, ['span', { class: 'sb-msg' }], ['span', { id: 'save-status' }],
        ['button', { id: 'btn-discard' }], ['button', { id: 'btn-save' }]]]));
    const set = (id, value) => { doc.getElementById(id).value = value; };
    const start = { google_cast_enabled: 'false', 'tv-name': '' };
    Object.keys(start).forEach((id) => { set(id, start[id]); doc.getElementById(id).defaultValue = start[id]; });
    const sent = [];
    const ctx = {
      document: doc, console, Promise, setTimeout: () => 0, clearTimeout: () => {}, isFinite, addEventListener: () => {},
      MM: { api: { put: (_, body) => { sent.push(body); return Promise.resolve({ status: 'saved' }); } }, reauth: { run: (f) => f() } },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    ['savebar', 'general'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, 'settings', f + '.js'), 'utf8'), ctx));
    ctx.MM.settings.general.init();
    const bar = doc.getElementById('setg-savebar');
    const shows = () => { ctx.MM.settings.savebar.markDirty(); return bar.classList.contains('on'); };
    set('tv-name', 'Den');
    const afterName = shows();
    set('google_cast_enabled', 'true');
    const afterEdit = shows();
    click(doc, doc.getElementById('btn-save'));
    await settle();
    return { afterName, afterEdit, sent };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
