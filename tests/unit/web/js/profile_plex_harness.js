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
ctx.MM.reauth = { run: (fn) => fn() };

const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r)); };
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
const LINK = { name: 'amy', linked_at: '2026-03-01T12:00:00Z', last_read_at: null, reading: true };
answers['GET /api/users/plex-links'] = [{ links: [] }];
answers['POST /api/account/plex/cancel'] = [{}];

async function load(first) {
  answers[GET] = [first];
  vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/profile-plex.js'), 'utf8'), ctx);
  await flush();
}

const scenarios = {
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
    return { ...state(), waits: waits(), cancelled: calls.includes('POST /api/account/plex/cancel') };
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
      null,
    ]) {
      answers[CHECK] = [{ attempt }];
      await click(out.length ? 'plex-retry' : 'plex-start');
      await tick();
      out.push({ ...state(), text: at('plex-failed').textContent, note: !at('plex-signout-note').hidden });
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
    confirmed = false;
    await click('plex-unlink');
    const kept = { shown: shown(), calls: calls.length };
    confirmed = true;
    await click('plex-unlink');
    return { kept, after: shown(), last: calls[calls.length - 1] };
  },
  async others() {
    answers['GET /api/users/plex-links'] = [
      { links: [{ id: 3, username: '<i>bob</i>', linked_at: '2026-03-01T12:00:00Z' }] }, { links: [] },
    ];
    answers['DELETE /api/users/3/plex-link'] = [{}];
    await load(NONE);
    const box = at('plex-others');
    const name = box.querySelector('.usr-name').textContent;
    const button = box.querySelector('.link-danger');
    button.listeners.click.forEach((f) => f());
    await flush();
    return { name, label: button.getAttribute('aria-label'), left: box.textContent, deleted: calls.includes('DELETE /api/users/3/plex-link') };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
