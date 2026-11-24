// Runs the real profile-plex.js over the panels and ids of its template and prints the outcome of
// one scenario as JSON. Usage: node profile_plex_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument } = require('./fake_dom.js');

const WEB = path.resolve(__dirname, '../../../../src/mediaman/web');
const template = fs.readFileSync(path.join(WEB, 'templates/_profile_plex.html'), 'utf8');

const doc = makeDocument();
const at = (id) => doc.getElementById(id);
const card = doc.createElement('div');
card.setAttribute('id', 'plex-link');
const section = doc.body.appendChild(doc.createElement('section'));
section.setAttribute('id', 'plex');
section.appendChild(card);
let panel = card;
for (const m of template.matchAll(/data-plex-panel="(\w+)"|\bid="([\w-]+)"/g)) {
  const el = doc.createElement('div');
  if (m[1]) {
    el.setAttribute('data-plex-panel', m[1]);
    panel = card.appendChild(el);
  } else if (m[2] === 'plex-others') {
    el.setAttribute('id', m[2]);
    doc.body.appendChild(el);
  } else if (m[2] !== 'plex-link') {
    el.setAttribute('id', m[2]);
    panel.appendChild(el);
  }
}

const calls = [];
const answers = {};
let timers = [];
let confirmed = true;
const listeners = {};
function api(method) {
  return (url, body) => {
    const key = method + ' ' + url;
    calls.push(body ? key + ' ' + JSON.stringify(body) : key);
    const queue = answers[key] || [];
    const next = queue.length > 1 ? queue.shift() : queue[0];
    if (next === undefined) return Promise.reject({ status: 0, message: 'unexpected ' + key });
    if (next && typeof next.then === 'function') return next;
    return next && next.error ? Promise.reject(next) : Promise.resolve(next);
  };
}
const ctx = {
  document: doc, Promise, Date,
  setTimeout: (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length; },
  clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].live = false; },
  addEventListener: (t, fn) => { listeners[t] = fn; },
  UIFeedback: { confirm: () => Promise.resolve(confirmed), error: () => {} },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/core/dom.js'), 'utf8'), ctx);
ctx.MM.api = {
  get: api('GET'), post: api('POST'), delete: api('DELETE'),
  messageOf: (err, fallback) => err.message || fallback,
};
const asked = [];
ctx.MM.reauth = { run: (fn, words) => { asked.push(words || null); return fn(); } };

const said = [];
// Announcements wait 100 ms after clearing their line; everything else waits for tick().
const flush = async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  for (const t of timers.filter((x) => x.live && x.ms === 100)) {
    t.live = false;
    const before = ['plex-status', 'plex-others-status'].map((id) => at(id).textContent);
    t.fn();
    ['plex-status', 'plex-others-status'].forEach((id, i) => {
      if (at(id).textContent !== before[i]) said.push(at(id).textContent);
    });
  }
};
const click = async (id) => { at(id).listeners.click.forEach((f) => f.call(at(id), {})); await flush(); };
const tick = async () => {
  const due = timers.filter((t) => t.live);
  due.forEach((t) => { t.live = false; });
  due.forEach((t) => t.fn());
  await flush();
};
const waits = () => timers.filter((t) => t.live).map((t) => t.ms);
const shown = () => card.children.filter((p) => p.hasAttribute('data-plex-panel') && !p.hidden)
  .map((p) => p.getAttribute('data-plex-panel')).join();
const state = () => ({ shown: shown(), focus: doc.activeElement.id, status: at('plex-status').textContent });

const GET = 'GET /api/account/plex';
const START = 'POST /api/account/plex';
const CHECK = 'POST /api/account/plex/check';
const WAITING = {
  id: 'a1', state: 'waiting', signed_out: true, app: 'mediaman (h)', url: 'https://app.plex.tv/auth#?code=x',
};
const CONFIRM = { id: 'a1', state: 'confirm', name: '<b>amy</b>', signed_out: true };
const NONE = { configured: true, link: null, attempt: null };
const BOB = { id: 3, username: '<i>bob</i>', linked_at: '2026-03-01T12:00:00Z' };
const CAT = { id: 4, username: 'cat', linked_at: '2026-03-01T12:00:00Z' };
const LINK = { name: 'amy', linked_at: '2026-03-01T12:00:00Z', last_read_at: null, reading: true };
answers['GET /api/users/plex-links'] = [{ links: [] }];
answers['POST /api/account/plex/cancel'] = [{ signed_out: true }];

async function load(first) {
  answers[GET] = [first];
  vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/profile-plex.js'), 'utf8'), ctx);
  await flush();
}

const scenarios = {
  // the password dialog of linking speaks of linking, not saving
  async asks() {
    answers[START] = [{ attempt: WAITING }];
    await load(NONE);
    await click('plex-start');
    return asked;
  },
  async link() {
    const out = {};
    await load(NONE);
    out.first = state();
    section.focus();
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: WAITING }, { attempt: CONFIRM }];
    await click('plex-start');
    out.waiting = { ...state(), href: at('plex-open').href, app: at('plex-app').textContent };
    await tick();
    out.still = shown();
    await tick();
    out.confirm = { ...state(), name: at('plex-found').textContent, waits: waits() };
    answers[START] = [{}];
    answers[GET] = [
      { configured: true, link: LINK, attempt: null },
      { configured: true, link: { ...LINK, reading: false, last_read_at: '2026-03-01T12:01:00Z' }, attempt: null },
    ];
    await click('plex-confirm');
    out.linked = { ...state(), read: at('plex-read').textContent };
    await tick();
    out.read = { text: at('plex-read').textContent, waits: waits() };
    out.sent = calls.filter((c) => c.startsWith(START + ' '));
    return out;
  },
  async moves() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: CONFIRM }];
    await click('plex-start');
    await tick();
    const untold = !at('plex-moves').hidden;
    at('plex-confirm').focus();
    answers[START] = [{ attempt: { ...CONFIRM, moves: true } }, {}];
    answers[GET] = [{ configured: true, link: { ...LINK, reading: false }, attempt: null }];
    await click('plex-confirm');
    const told = { shown: shown(), notice: !at('plex-moves').hidden, said: said[said.length - 1], focus: doc.activeElement.id };
    await click('plex-confirm');
    return { untold, told, shown: shown(), sent: calls.filter((c) => c.startsWith(START + ' ')) };
  },
  async movesFromTheStart() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: { ...CONFIRM, moves: true } }];
    await click('plex-start');
    await tick();
    return state();
  },
  async heldKey() {
    await load(NONE);
    return [['Enter', true], [' ', true], ['Enter', false], ['Tab', true]].map(([key, repeat]) => {
      let stopped = false;
      card.listeners.keydown.forEach((f) => f({ key, repeat, preventDefault() { stopped = true; } }));
      return stopped;
    });
  },
  async moved() {
    await load({ ...NONE, moved_at: '2026-03-01T12:00:00+00:00' });
    const first = { shown: shown(), note: !at('plex-moved').hidden, when: at('plex-moved-when').textContent };
    const url = 'DELETE /api/account/plex/notice?moved_at=2026-03-01T12%3A00%3A00%2B00%3A00';
    answers[url] = [
      { error: 'network_error', status: 0, message: 'Unreachable.' },
      { moved_at: '2026-03-02T12:00:00+00:00' },
    ];
    answers['DELETE /api/account/plex/notice?moved_at=2026-03-02T12%3A00%3A00%2B00%3A00'] = [{ moved_at: null }];
    at('plex-moved-dismiss').focus();
    await click('plex-moved-dismiss');
    const failed = { note: !at('plex-moved').hidden, error: at('plex-error').textContent };
    await click('plex-moved-dismiss');
    const newer = at('plex-moved-when').textContent;
    await click('plex-moved-dismiss');
    return { first, failed, newer, after: { note: !at('plex-moved').hidden, ...state() } };
  },
  async movedUnset() {
    await load({ ...NONE, configured: false, moved_at: '2026-03-01T12:00:00Z' });
    answers['DELETE /api/account/plex/notice?moved_at=2026-03-01T12%3A00%3A00Z'] = [{ moved_at: null }];
    at('plex-moved-dismiss').focus();
    await click('plex-moved-dismiss');
    return { note: !at('plex-moved').hidden, ...state() };
  },
  async lostWhileOpen() {
    await load({ configured: true, link: { ...LINK, reading: false }, attempt: null });
    answers['DELETE /api/account/plex'] = [{}];
    answers[GET] = [{ ...NONE, moved_at: '2026-03-01T12:00:00Z' }];
    await click('plex-unlink');
    return { shown: shown(), note: !at('plex-moved').hidden };
  },
  async failedLoad() {
    answers[GET] = [{ error: 'network_error', status: 0, message: 'Unreachable.' }, NONE];
    vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/profile-plex.js'), 'utf8'), ctx);
    await flush();
    const first = { shown: shown(), text: at('plex-failed').textContent };
    await click('plex-retry');
    return { first, shown: shown(), started: calls.includes(START) };
  },
  async quietFailure() {
    await load({ configured: true, link: LINK, attempt: null });
    answers[GET] = [
      { error: 'network_error', status: 0, message: 'Unreachable.' },
      { configured: true, link: { ...LINK, reading: false, last_read_at: '2026-03-01T12:01:00Z' }, attempt: null },
    ];
    await tick();
    const during = { shown: shown(), waits: waits() };
    await tick();
    return { during, shown: shown(), read: at('plex-read').textContent };
  },
  async otherTab() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: null }];
    answers[GET] = [{ configured: true, link: { ...LINK, reading: false }, attempt: null }];
    await click('plex-start');
    await tick();
    return { shown: shown(), waits: waits() };
  },
  async otherTabConfirmed() {
    const out = [];
    await load({ configured: true, link: null, attempt: CONFIRM });
    for (const error of ['expired', 'already_linked']) {
      answers[START] = [{ error, status: 409, message: 'Too late.' }];
      answers[GET] = [{ configured: true, link: { ...LINK, reading: false }, attempt: null }];
      await click('plex-confirm');
      out.push(shown());
    }
    answers[START] = [{ error: 'expired', status: 409, message: 'Too late.' }];
    answers[GET] = [NONE];
    await click('plex-confirm');
    out.push(shown(), at('plex-failed').textContent);
    return out;
  },
  async repeat() {
    await load(NONE);
    answers[START] = [{ error: 'plex_unavailable', status: 503, message: 'Plex is down.' }];
    await click('plex-start');
    await click('plex-retry');
    return said;
  },
  async cancelNote() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers['POST /api/account/plex/cancel'] = [{ signed_out: false }];
    await click('plex-start');
    await click('plex-cancel');
    return { shown: shown(), note: !at('plex-signout-note').hidden };
  },
  async busy() {
    await load(NONE);
    at('plex-start').focus();
    let answer;
    answers[START] = [new Promise((r) => { answer = r; })];
    await click('plex-start');
    await click('plex-start');
    const during = { disabled: !!at('plex-start').disabled, aria: at('plex-start').getAttribute('aria-disabled') };
    answer({ attempt: WAITING });
    await flush();
    return { during, starts: calls.filter((c) => c === START).length, after: at('plex-start').getAttribute('aria-disabled') };
  },
  async badUrl() {
    await load(NONE);
    answers[START] = [{ attempt: { ...WAITING, url: 'https://app.plex.tv.evil.example/auth#?' } }];
    await click('plex-start');
    return { ...state(), href: at('plex-open').href || null };
  },
  async cancelWhileChecking() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    let answer;
    answers[CHECK] = [new Promise((r) => { answer = r; })];
    await tick();
    await click('plex-cancel');
    answer({ attempt: CONFIRM });
    await flush();
    return { ...state(), waits: waits(), cancelled: calls.includes('POST /api/account/plex/cancel {"attempt":"a1"}') };
  },
  async hidden() {
    await load({ configured: true, link: null, attempt: WAITING });
    answers[CHECK] = [{ attempt: WAITING }];
    doc.hidden = true;
    await tick();
    const whileHidden = calls.filter((c) => c === CHECK).length;
    doc.hidden = false;
    doc.listeners.visibilitychange.forEach((f) => f());
    await flush();
    return { whileHidden, after: calls.filter((c) => c === CHECK).length, waits: waits() };
  },
  async failures() {
    const out = [];
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    for (const attempt of [
      { id: 'a1', state: 'failed', reason: 'not_on_server', message: 'No access here.', signed_out: false },
      { id: 'a1', state: 'failed', reason: 'not_on_server', message: 'No access here.', signed_out: true },
      null,
    ]) {
      answers[CHECK] = [{ attempt, message: attempt ? undefined : 'It ran out; see Authorized Devices.' }];
      await click(out.length ? 'plex-retry' : 'plex-start');
      await tick();
      out.push({ ...state(), said: said.splice(0), text: at('plex-failed').textContent, note: !at('plex-signout-note').hidden });
    }
    return out;
  },
  async slowPlex() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ error: 'network_error', status: 0 }];
    await click('plex-start');
    await tick();
    return { shown: shown(), wait: at('plex-wait').textContent, waits: waits() };
  },
  async unlink() {
    await load({ configured: true, link: { ...LINK, reading: false }, attempt: null });
    answers['DELETE /api/account/plex'] = [{}];
    answers[GET] = [NONE];
    confirmed = false;
    await click('plex-unlink');
    const kept = { shown: shown(), calls: calls.length };
    confirmed = true;
    await click('plex-unlink');
    return { kept, after: shown(), deleted: calls.includes('DELETE /api/account/plex') };
  },
  async others() {
    answers['GET /api/users/plex-links'] = [
      { links: [BOB, CAT] }, { links: [CAT] }, { links: [] },
    ];
    answers['DELETE /api/users/3/plex-link'] = [{}];
    answers['DELETE /api/users/4/plex-link'] = [{}];
    await load(NONE);
    const box = at('plex-others');
    const name = box.querySelector('.usr-name').textContent;
    const button = box.querySelector('.link-danger');
    button.focus();
    button.listeners.click.forEach((f) => f());
    await flush();
    const next = { focus: doc.activeElement.getAttribute('aria-label'), said: said.slice() };
    box.querySelector('.link-danger').listeners.click.forEach((f) => f());
    await flush();
    return { next, focus: doc.activeElement.id, said,  name, label: button.getAttribute('aria-label'), left: box.textContent, deleted: calls.includes('DELETE /api/users/3/plex-link') };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
