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
const OLDER = { id: 2, kind: 'older', name: 'Kitchen TV', owner: null, on: false };

async function page(answers, all = [PAIRED, OLDER], reduced = true) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { class: 'setg-pg' },
    ['button', { id: 'btn-add-tv' }], ['div', { id: 'tv-list-wrap' }], ['div', { id: 'tv-add', hidden: '' }],
    ['input', { id: 'older_tvs_http' }], ['span', { id: 'tv-address-hint' }, 'usual'], ['input', { id: 'tv_address' }]]));
  doc.addEventListener = () => {};
  doc.getElementById('older_tvs_http').value = 'false';
  doc.getElementById('tv_address').value = '';
  const calls = [];
  const call = (method) => (url, body) => {
    calls.push([method, url].concat(body ? [body] : []));
    const answer = answers[method + ' ' + url];
    return typeof answer === 'function' ? answer() : Promise.resolve(answer);
  };
  answers['GET /api/tvs/all'] = answers['GET /api/tvs/all'] || { tvs: all };
  answers['GET /api/settings/older-tvs'] = { tvs: [{ id: 2, name: 'Kitchen TV', address: '192.168.1.44' }] };
  const said = [];
  const dropped = [];
  const ctx = {
    document: doc, console, Promise, Date, setTimeout: (f, ms) => { if (!ms) f(); },
    matchMedia: () => ({ matches: reduced }),
    MM: {
      api: { get: call('GET'), post: call('POST'), patch: call('PATCH'), delete: call('DELETE'),
        messageOf: (e, words) => e.message || words },
      tvRemote: { drop: (id) => dropped.push(id) },
      dom: { focusStillIn: () => true, icon: (name) => build(doc, ['svg', { class: name }]) },
      settings: { rows: { enter() {}, bringIntoView() {}, leave: (row, o) => { row.remove(); if (o.done) o.done(); } } },
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
    doc, calls, said, row,
    press: async (root, words) => { click(doc, within(root, words)); await settle(); },
    button: within,
    panel: doc.getElementById('tv-add'),
    list: doc.getElementById('tv-list-wrap'),
    add: async (address) => {
      click(doc, doc.getElementById('btn-add-tv'));
      doc.getElementById('tv-add-address').value = address;
      click(doc, within(doc.getElementById('tv-add'), 'Check'));
    },
    states: (root) => root.querySelectorAll('.tv-chk li').map((li) => li.className),
    fire: (el, type) => (el.listeners[type] || []).forEach((f) => f({ target: el, preventDefault() {} })),
  };
}

const refused = (error, message) => () => Promise.reject({ error, message, data: {} });
const CHECK = 'POST /api/settings/older-tvs/check';
const PICTURE = 'POST /api/settings/older-tvs/picture';
const FROM = { from: 'http://192.168.1.10:8282' };
const FOUND = { name: '<b>TV</b>', address: '192.168.1.44' };

const scenarios = {
  async list() {
    const p = await page({});
    return { rows: p.doc.querySelectorAll('.tv-li').map((li) => [li.querySelector('.tv-sub').textContent, !!p.button(li, 'Check')]) };
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
  async found() {
    let answer, shown;
    const p = await page({
      [CHECK]: () => new Promise((resolve) => { answer = resolve; }),
      [PICTURE]: () => new Promise((resolve) => { shown = resolve; }),
      'POST /api/settings/older-tvs': { tv: { id: 3, name: 'Attic', address: '192.168.1.44' } },
    });
    await p.add('192.168.1.44');
    const out = { checking: p.states(p.panel), early: p.button(p.panel, 'Add this TV').disabled };
    answer(FOUND);
    await settle();
    out.showing = [p.states(p.panel), p.button(p.panel, 'Add this TV').disabled];
    out.picture = p.calls[p.calls.length - 1];
    shown(FROM);
    await settle();
    out.found = p.states(p.panel);
    out.from = p.panel.textContent.includes('Fetched a test picture from http://192.168.1.10:8282');
    out.text = p.panel.textContent.includes('<b>TV</b>') && !p.panel.querySelector('li b');
    p.doc.getElementById('tv-add-name').value = ' Attic ';
    await p.press(p.panel, 'Add this TV');
    return Object.assign(out, { sent: p.calls.pop(), rows: p.doc.querySelectorAll('.tv-li').length, shut: p.panel.hidden });
  },
  async edited() {
    const p = await page({ [CHECK]: FOUND, [PICTURE]: FROM });
    await p.add('192.168.1.44');
    await settle();
    const ready = !p.button(p.panel, 'Add this TV').disabled;
    p.fire(p.doc.getElementById('tv-add-address'), 'input');
    return { ready, after: p.button(p.panel, 'Add this TV').disabled };
  },
  async late() {
    let answer;
    const p = await page({ [CHECK]: () => new Promise((resolve) => { answer = resolve; }) });
    await p.add('192.168.1.44');
    p.fire(p.doc.getElementById('tv-add-address'), 'input');
    answer(FOUND);
    await settle();
    return p.button(p.panel, 'Add this TV').disabled;
  },
  async typed() {
    const p = await page({});
    await p.add('tv.example.com');
    return { states: p.states(p.panel), asked: p.calls.some((c) => c[0] === 'POST') };
  },
  async failures() {
    const out = {};
    for (const code of ['unreachable', 'not_a_tv', 'own_network', 'off']) {
      const p = await page({ [CHECK]: refused(code, 'said ' + code) });
      await p.add('192.168.1.44');
      await settle();
      out[code] = [p.states(p.panel).pop(), p.panel.querySelector('.tv-chk').textContent];
    }
    return out;
  },
  async pictures() {
    const out = {};
    for (const code of ['no_fetch', 'no_picture', 'no_tv_address', 'http_off', 'host_not_allowed', 'busy']) {
      const p = await page({ [CHECK]: FOUND, [PICTURE]: refused(code, 'said ' + code) });
      const input = p.doc.getElementById('tv_address');
      input.value = input.defaultValue = 'https://mediaman.example';
      await p.add('192.168.1.44');
      await settle();
      out[code] = [p.states(p.panel), p.panel.querySelector('.tv-chk').textContent,
        p.button(p.panel, 'Add this TV').disabled, p.doc.getElementById('tv-address-hint').textContent];
    }
    return out;
  },
  async checked() {
    const answers = { 'POST /api/settings/older-tvs/2/check': refused('unreachable', 'x') };
    const p = await page(answers);
    await p.press(p.row(1), 'Check');
    const off = p.states(p.row(1));
    answers['POST /api/settings/older-tvs/2/check'] = { tv: { last_seen_at: new Date().toISOString(), address: '192.168.1.77' } };
    await p.press(p.row(1), 'Check');
    return { off, on: p.states(p.row(1)), says: p.row(1).querySelector('.tv-sub').textContent };
  },
  // Which lines of the steps before are still the same elements, by their words.
  async stands() {
    const pending = [];
    const later = () => new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
    const p = await page({ [CHECK]: later, 'POST /api/settings/older-tvs/picture': { from: 'x' }, 'POST /api/settings/older-tvs/2/check': later });
    const out = {};
    for (const [name, root, start, answer] of [
      ['add', p.panel, () => p.add('192.168.1.44'), FOUND],
      ['row', p.row(1), () => p.press(p.row(1), 'Check'), { tv: { last_seen_at: null, address: '192.168.1.44' } }]]) {
      await start();
      const before = root.querySelectorAll('.tv-chk li');
      pending.pop().resolve(answer);
      await settle();
      out[name] = root.querySelectorAll('.tv-chk li').filter((li) => before.includes(li)).map((li) => [li.className, li.textContent]);
    }
    return out;
  },
  async leaving() {
    const p = await page({}, [PAIRED, OLDER], false);
    const done = (n) => p.fire(n, 'animationend');
    await p.press(p.row(1), 'Forget');
    const first = p.row(1).querySelector('.tv-sure');
    await p.press(p.row(1), 'Cancel');
    const out = { question: [first.isConnected, first.className, first.inert] };
    done(first);
    out.gone = first.isConnected;
    await p.add('x');
    await p.press(p.panel, 'Cancel');
    out.panel = [p.panel.hidden, p.panel.className, p.doc.getElementById('btn-add-tv').hidden];
    await p.add('x');
    done(p.panel);
    out.again = [p.panel.hidden, p.panel.className, p.panel.inert];
    await p.press(p.panel, 'Cancel');
    done(p.panel);
    out.shut = [p.panel.hidden, p.panel.childNodes.length];
    const still = await page({});
    await still.add('x');
    await still.press(still.panel, 'Cancel');
    return Object.assign(out, { reduced: still.panel.hidden });
  },
  async forget() {
    const p = await page({});
    await p.press(p.row(1), 'Forget');
    const asked = p.calls.some((c) => c[0] === 'DELETE');
    await p.press(p.row(1), 'Cancel');
    const kept = p.doc.querySelectorAll('.tv-li').length;
    await p.press(p.row(1), 'Forget');
    await p.press(p.row(1), 'Forget this TV');
    await p.press(p.row(0), 'Forget');
    await p.press(p.row(0), 'Forget this TV');
    return { asked, kept, deleted: p.calls.filter((c) => c[0] === 'DELETE').map((c) => c[1]), left: p.list.textContent, dropped: p.dropped };
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
  async address() {
    const p = await page({});
    const input = p.doc.getElementById('tv_address');
    const hint = p.doc.getElementById('tv-address-hint');
    return ['http://localhost:8282', 'https://mediaman.example', 'http://192.168.1.10', 'http://tv.example.com:8282'].map((v) => {
      input.value = v;
      p.fire(input, 'input');
      return [input.getAttribute('aria-invalid'), hint.textContent];
    });
  },
  // The page's own save bar, with the section's fields and one of the add form's.
  async saved() {
    const doc = makeDocument();
    doc.addEventListener = () => {};
    const field = (id) => ['input', { id }];
    doc.body.appendChild(build(doc, ['div', { class: 'setg-pg' },
      field('older_tvs_enabled'), field('older_tvs_http'), field('google_cast_enabled'), field('tv_address'),
      ['div', { class: 'tv-add' }, field('tv-add-address')],
      ['div', { id: 'setg-savebar', class: 'savebar' }, ['span', { class: 'sb-msg' }], ['span', { id: 'save-status' }],
        ['button', { id: 'btn-discard' }], ['button', { id: 'btn-save' }]]]));
    const set = (id, value) => { doc.getElementById(id).value = value; };
    const start = { older_tvs_enabled: 'false', older_tvs_http: 'false', google_cast_enabled: 'false', tv_address: '', 'tv-add-address': '' };
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
    set('tv-add-address', '192.168.1.44');
    const afterAdd = shows();
    set('older_tvs_enabled', 'true');
    set('older_tvs_http', 'true');
    set('google_cast_enabled', 'true');
    set('tv_address', ' http://192.168.1.10:8282 ');
    const afterEdit = shows();
    click(doc, doc.getElementById('btn-save'));
    await settle();
    return { afterAdd, afterEdit, sent };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
