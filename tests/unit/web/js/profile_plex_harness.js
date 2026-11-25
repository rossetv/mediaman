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
  document: doc, Promise,
  setTimeout: (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length; },
  clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].live = false; },
  addEventListener: (t, fn) => { listeners[t] = fn; },
  UIFeedback: { confirm: () => Promise.resolve(confirmed), error: () => {} },
};
const tabs = [];
let blocked = false;
let inPress = false;
// A press's leave to open a tab ends with it, as on iOS.
ctx.open = (url) => {
  if (blocked || !inPress) return null;
  const tabDoc = makeDocument();
  const tab = { url, opener: ctx, closed: false, went: [], document: tabDoc };
  const make = tabDoc.createElement.bind(tabDoc);
  tabDoc.createElement = (tag) => {
    const el = make(tag);
    el.click = () => { tab.went.push({ href: el.href, rel: el.rel }); };
    return el;
  };
  tab.close = () => { tab.closed = true; };
  tabs.push(tab);
  return tab;
};
const tabsNow = () => tabs.map((t) => ({ opener: t.opener, closed: t.closed, went: t.went.map((w) => w.href) }));
let calm = false;
let clock = 0;
const moves = [];
const HEIGHTS = {
  loading: 40, unlinked: 300, failed: 90, confirm: 120,
  get waiting() { return at('plex-wait').textContent.startsWith('Not') ? 180 : 160; },
};
Object.defineProperty(card, 'offsetHeight', { get: () => HEIGHTS[shown()] || 0 });
const runs = [];
card.animate = (frames, how) => {
  moves.push([frames[0].height, frames[1].height, how.duration]);
  const run = { cancelled: false, cancel() { run.cancelled = true; } };
  runs.push(run);
  return run;
};
const scrolled = [];
let inView = true;
let hint = null;
let hints = 0;
doc.createElement('i').constructor.prototype.scrollIntoView = function () { scrolled.push(this.id); };
ctx.fetch = () => { hints += 1; return Promise.resolve({ ok: hint !== null, json: () => Promise.resolve(hint) }); };
card.setAttribute('data-password-good-for', '300');
ctx.Date = class extends Date { static now() { return clock * 1000; } };
ctx.matchMedia = () => ({ matches: calm });
ctx.getComputedStyle = () => ({ getPropertyValue: (n) => (n === '--dur-slow' ? ' .25s' : ' ease-out') });
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/core/dom.js'), 'utf8'), ctx);
ctx.MM.dom.inView = () => inView;
ctx.MM.api = {
  get: api('GET'), post: api('POST'), delete: api('DELETE'),
  messageOf: (err, fallback) => err.message || fallback,
};
const asked = [];
ctx.MM.reauth = { openModal: (opts) => { asked.push(opts); } };
const WHO = { error: 'reauth_required', name: 'APIError', status: 403, data: { reauth_required: true } };

const said = [];
// Announcements wait 100 ms after clearing their line; everything else waits for tick().
const flush = async () => {
  for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
  for (const t of timers.filter((x) => x.live && x.ms === 100)) {
    t.live = false;
    const before = at('plex-status').textContent;
    t.fn();
    if (at('plex-status').textContent !== before) said.push(at('plex-status').textContent);
  }
};
const press = (fn) => { inPress = true; try { return fn(); } finally { inPress = false; } };
const click = async (id) => { press(() => at(id).listeners.click.forEach((f) => f.call(at(id), {}))); await flush(); };
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
const NONE = { configured: true, start_in: 0, link: null, attempt: null };
const BOB = { id: 3, username: '<i>bob</i>', linked_at: '2026-03-01T12:00:00Z' };
const CAT = { id: 4, username: 'cat', linked_at: '2026-03-01T12:00:00Z' };
const LINK = { name: 'amy', linked_at: '2026-03-01T12:00:00Z', last_read_at: null, reading: true };
answers['GET /api/users/plex-links'] = [{ links: [] }];
answers['POST /api/account/plex/cancel'] = [{ signed_out: true }];

at('plex-start').textContent = 'Link Plex account';

async function load(first) {
  const good = Number(card.getAttribute('data-password-good-for'));
  answers[GET] = [{ password_good_for: good, start_in: 0, ...first }];
  vm.runInContext(fs.readFileSync(path.join(WEB, 'static/js/profile-plex.js'), 'utf8'), ctx);
  await flush();
}

const scenarios = {
  // the password dialog of linking speaks of linking, not saving
  async asks() {
    answers[START] = [WHO];
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
  async firstPress() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; })];
    await click('plex-start');
    const pressed = { tabs: tabsNow(), says: tabs.map((t) => t.document.body.textContent).join(), cancel: !at('plex-start-stop').hidden, label: at('plex-start').textContent };
    answer({ attempt: WAITING });
    await flush();
    return { pressed, after: tabsNow(), rel: tabs[0].went[0].rel, ...state(), lead: at('plex-lead').textContent,
      open: at('plex-open').textContent, loud: at('plex-open').classList.contains('btn--primary'),
      cancel: !at('plex-start-stop').hidden, label: at('plex-start').textContent };
  },
  async tabOnFailure() {
    const out = [];
    await load(NONE);
    for (const answer of [
      { error: 'plex_unavailable', status: 503, message: 'Plex is down.' },
      { error: 'network_error', status: 0, message: 'Unreachable.' },
      { attempt: { ...WAITING, url: 'http://app.plex.tv/auth#?code=x' } },
      { attempt: { ...WAITING, url: 'https://app.plex.tv.evil.example/auth#?' } },
      { error: 'plex_not_set_up', status: 409, message: 'Not set up.' },
    ]) {
      answers[START] = [answer];
      await click(out.length ? 'plex-retry' : 'plex-start');
      out.push({ ...tabsNow().pop(), shown: shown(), text: at('plex-failed').textContent });
    }
    return out;
  },
  async blockedTab() {
    await load(NONE);
    blocked = true;
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    const look = () => ({ lead: at('plex-lead').textContent, open: at('plex-open').textContent, loud: at('plex-open').classList.contains('btn--primary') });
    const out = { shown: shown(), href: at('plex-open').href, first: look() };
    await click('plex-open');
    return { ...out, pressed: look() };
  },
  async closedEarly() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; })];
    await click('plex-start');
    tabs[0].close();
    answer({ attempt: WAITING });
    await flush();
    return { shown: shown(), went: tabs[0].went, lead: at('plex-lead').textContent, loud: at('plex-open').classList.contains('btn--primary') };
  },
  async cancelOpening() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; })];
    await click('plex-start');
    at('plex-start-stop').focus();
    await click('plex-start-stop');
    const at1 = { ...tabsNow()[0], note: at('plex-note').textContent, said: said.slice(-1)[0], label: at('plex-start').textContent };
    answer({ attempt: WAITING });
    await flush();
    return { at1, ...state(), went: tabs[0].went, cancelled: calls.includes('POST /api/account/plex/cancel {"attempt":"a1"}') };
  },
  async cancelWaiting() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    await click('plex-cancel');
    const out = { closed: tabs[0].closed, note: at('plex-note').textContent, ...state() };
    await click('plex-start');
    return { ...out, noteAfter: at('plex-note').textContent };
  },
  async password() {
    await load(NONE);
    answers[START] = [WHO, { attempt: WAITING }];
    answers['POST /api/auth/reauth'] = [{ error: 'bad', status: 403, message: 'Wrong.' }, {}];
    await click('plex-start');
    const out = { asked: tabsNow() };
    out.wrong = await press(() => asked[0].onSubmit('no'));
    out.afterWrong = tabsNow();
    out.right = await press(() => asked[0].onSubmit('yes'));
    await flush();
    return { ...out, after: tabsNow(), shown: shown() };
  },
  async passwordDue() {
    const out = {};
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers['POST /api/auth/reauth'] = [{ error: 'bad', status: 403, message: 'Wrong.' }, { expires_in_seconds: 300 }];
    await click('plex-start');
    out.asked = { tabs: tabs.length, starts: calls.filter((c) => c === START).length, label: at('plex-start').textContent };
    await press(() => asked[0].onSubmit('no'));
    out.wrong = { closed: tabs[0].closed, label: at('plex-start').textContent };
    await press(() => asked[0].onSubmit('yes'));
    await flush();
    out.after = tabsNow().pop();
    await click('plex-cancel');
    clock = 200;
    await click('plex-start');
    out.good = { asked: asked.length, tab: tabsNow().pop() };
    await click('plex-cancel');
    clock = 296;
    await click('plex-start');
    out.nearlyOut = { asked: asked.length, tabs: tabs.length };
    asked[1].onCancel();
    await flush();
    out.cancelled = { label: at('plex-start').textContent, starts: calls.filter((c) => c === START).length };
    return out;
  },
  async sessionEnded() {
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers['POST /api/auth/reauth'] = [new Promise(() => {}), { expires_in_seconds: 300 }];
    await click('plex-start');
    press(() => asked[0].onSubmit('pw'));
    const before = tabs[0].closed;
    listeners.pagehide({});
    const gone = tabs[0].closed;
    listeners.pageshow({ persisted: true });
    await flush();
    const late = await press(() => asked[0].onSubmit('pw'));
    return { before, gone, tabs: tabs.length, late: late.ok, label: at('plex-start').textContent, shown: shown() };
  },
  async frozenDialog() {
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers['POST /api/auth/reauth'] = [{ expires_in_seconds: 300 }];
    await click('plex-start');
    await click('plex-start');
    listeners.pagehide({});
    listeners.pageshow({ persisted: true });
    await flush();
    press(() => asked[0].onSubmit('pw'));
    await flush();
    return { asked: asked.length, tabs: tabsNow(), shown: shown() };
  },
  async stuckStart() {
    await load(NONE);
    answers[START] = [new Promise(() => {}), { attempt: WAITING }];
    await click('plex-start');
    listeners.pagehide({});
    listeners.pageshow({ persisted: true });
    await flush();
    await click('plex-start');
    return { first: tabs[0].closed, shown: shown(), tabs: tabs.length };
  },
  async freshHint() {
    const out = [];
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    for (const fire of [
      () => listeners.focus(),
      () => doc.listeners.visibilitychange.forEach((f) => f()),
      () => doc.listeners['mm:reauth'].forEach((f) => f()),
    ]) {
      hint = { password_good_for: 0, start_in: 0 };
      listeners.focus();
      await flush();
      hint = { password_good_for: 300, start_in: 0 };
      fire();
      await flush();
      answers[START] = [{ attempt: WAITING }];
      await click('plex-start');
      out.push(asked.length, shown());
      await click('plex-cancel');
    }
    hint = { password_good_for: 0, start_in: 0 };
    listeners.focus();
    await flush();
    await click('plex-start');
    return { out, due: asked.length };
  },
  async staleHint() {
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    let answer;
    ctx.fetch = () => new Promise((r) => { answer = r; });
    listeners.focus();
    answers['POST /api/auth/reauth'] = [{ expires_in_seconds: 300 }];
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    press(() => asked[0].onSubmit('pw'));
    await flush();
    answer({ ok: true, json: () => Promise.resolve({ password_good_for: 0, start_in: 0 }) });
    await flush();
    await click('plex-cancel');
    await click('plex-start');
    return asked.length;
  },
  async race() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; }), { attempt: { ...WAITING, id: 'a2', url: WAITING.url + '2' } }];
    let ended;
    answers['POST /api/account/plex/cancel'] = [new Promise((r) => { ended = r; })];
    await click('plex-start');
    await click('plex-start-stop');
    await click('plex-start');
    const held = calls.filter((c) => c === START).length;
    answer({ attempt: WAITING });
    await flush();
    const ending = { starts: calls.filter((c) => c === START).length, cancel: calls.includes('POST /api/account/plex/cancel {"attempt":"a1"}') };
    ended({ signed_out: true });
    await flush();
    return { held, ending, tabs: tabsNow(), shown: shown(), starts: calls.filter((c) => c === START).length };
  },
  async startUnanswered() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; }), { attempt: { ...WAITING, id: 'a2', url: WAITING.url + '2' } }];
    await click('plex-start');
    await click('plex-start-stop');
    await click('plex-start');
    const held = { starts: calls.filter((c) => c === START).length, waits: waits() };
    await tick();
    const capped = { shown: shown(), starts: calls.filter((c) => c === START).length, went: tabs[1].went.length };
    answer({ attempt: WAITING });
    await flush();
    return { held, capped, shown: shown(), href: at('plex-open').href, cancel: calls.filter((c) => c.includes('/cancel')) };
  },
  async cancelUnanswered() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers['POST /api/account/plex/cancel'] = [new Promise(() => {})];
    await click('plex-start');
    await click('plex-cancel');
    await click('plex-start');
    const held = calls.filter((c) => c === START).length;
    await tick();
    return { held, starts: calls.filter((c) => c === START).length, shown: shown() };
  },
  async oneGlance() {
    await load(NONE);
    hint = { password_good_for: 300, start_in: 0 };
    doc.listeners.visibilitychange.forEach((f) => f());
    listeners.focus();
    const once = hints;
    await flush();
    listeners.focus();
    doc.listeners['mm:reauth'].forEach((f) => f());
    return { once, later: hints };
  },
  async displaced() {
    await load(NONE);
    answers[START] = [{ error: 'expired', status: 409, message: 'Ran out.' }];
    answers[GET] = [{ ...NONE, password_good_for: 300, attempt: WAITING }];
    await click('plex-start');
    return { shown: shown(), closed: tabs[0].closed };
  },
  async raceCancelledTwice() {
    await load(NONE);
    let answer;
    answers[START] = [new Promise((r) => { answer = r; }), { attempt: WAITING }];
    await click('plex-start');
    await click('plex-start-stop');
    await click('plex-start');
    await click('plex-start-stop');
    answer({ attempt: WAITING });
    await flush();
    return { starts: calls.filter((c) => c === START).length, shown: shown(), open: tabs.filter((t) => !t.closed).length };
  },
  async scroll() {
    const out = {};
    inView = false;
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: WAITING }, { attempt: CONFIRM }];
    at('plex-start').focus();
    await click('plex-start');
    const early = scrolled.length;
    runs[runs.length - 1].onfinish();
    out.press = { early, after: scrolled.splice(0) };
    doc.listeners.visibilitychange.forEach((f) => f());
    await flush();
    const n = moves.length;
    runs[runs.length - 1].onfinish();
    out.wording = { eased: moves[n - 1], scrolled: scrolled.splice(0) };
    await tick();
    runs[runs.length - 1].onfinish();
    out.background = { shown: shown(), focus: doc.activeElement.id, scrolled: scrolled.splice(0) };
    inView = true;
    calm = true;
    await click('plex-decline');
    out.inView = scrolled.splice(0);
    inView = false;
    await click('plex-start');
    out.calm = scrolled.slice();
    return out;
  },
  async ease() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    const first = runs[runs.length - 1];
    inView = false;
    await click('plex-cancel');
    const running = runs[runs.length - 1];
    first.onfinish();
    return { cancelled: first.cancelled, scrolledByOld: scrolled.slice(), last: running.cancelled };
  },
  async limited() {
    const out = {};
    await load({ ...NONE, password_good_for: 300, start_in: 60 });
    answers[START] = [{ error: 'too_many_requests', status: 429, message: 'Too many.' }];
    await click('plex-start');
    out.known = { tabs: tabs.length, text: at('plex-failed').textContent };
    answers[START] = [{ attempt: WAITING }];
    clock = 61;
    await click('plex-retry');
    out.over = tabs.length;
    return out;
  },
  async limitedReady() {
    await load({ ...NONE, password_good_for: 300, start_in: 60 });
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    return { tabs: tabs.length, lead: at('plex-lead').textContent };
  },
  async limitedPassword() {
    card.setAttribute('data-password-good-for', '0');
    await load(NONE);
    answers['POST /api/auth/reauth'] = [{ error: 'too_many_requests', status: 429 }, { expires_in_seconds: 300 }];
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    await press(() => asked[0].onSubmit('pw'));
    await press(() => asked[0].onSubmit('pw'));
    await flush();
    return { tabs: tabsNow(), shown: shown() };
  },
  async heights() {
    await load(NONE);
    moves.length = 0;
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    const out = moves.splice(0);
    calm = true;
    await click('plex-cancel');
    return { out, calm: moves };
  },
  async passwordCancelled() {
    await load(NONE);
    answers[START] = [WHO, { attempt: WAITING }];
    let accept;
    answers['POST /api/auth/reauth'] = [new Promise((r) => { accept = r; })];
    await click('plex-start');
    press(() => asked[0].onSubmit('yes'));
    asked[0].onCancel();
    accept({});
    await flush();
    return { tabs: tabsNow(), starts: calls.filter((c) => c === START).length, shown: shown(), note: at('plex-note').textContent };
  },
  async expired() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    answers[CHECK] = [{ attempt: null, message: 'It ran out.' }];
    await click('plex-start');
    await tick();
    const text = at('plex-failed').textContent;
    await click('plex-retry');
    return { text, tabs: tabsNow(), shown: shown() };
  },
  async returning() {
    await load(NONE);
    answers[START] = [{ attempt: WAITING }];
    await click('plex-start');
    let stuck;
    answers[CHECK] = [new Promise((r, no) => { stuck = no; }), { attempt: WAITING }];
    await tick();
    const back = () => { doc.listeners.visibilitychange.forEach((f) => f()); listeners.focus(); };
    back();
    await flush();
    stuck({ error: 'network_error', status: 0 });
    await flush();
    const n = calls.filter((c) => c === CHECK).length;
    const first = { checks: n, wait: at('plex-wait').textContent, waits: waits() };
    listeners.focus();
    await flush();
    return { first, focusAlone: calls.filter((c) => c === CHECK).length - n, waits: waits() };
  },
  async success() {
    await load({ configured: true, link: null, attempt: CONFIRM });
    answers[START] = [{}];
    const linked = { configured: true, link: { ...LINK, name: '<b>amy</b>' }, attempt: null };
    answers[GET] = [linked];
    await click('plex-confirm');
    const out = { done: !at('plex-done').hidden, name: at('plex-done-name').textContent, said: said.slice(-1)[0], since: !at('plex-linked').hidden };
    await tick();
    out.kept = !at('plex-done').hidden;
    listeners.pageshow({ persisted: true });
    await flush();
    return { ...out, later: !at('plex-done').hidden, sinceLater: !at('plex-linked').hidden };
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
  async more() {
    await load(NONE);
    const more = at('plex-more');
    const toggle = at('plex-more-toggle');
    Object.defineProperty(more, 'offsetHeight', { get: () => (more.open ? 175 : 44) });
    Object.defineProperty(toggle, 'offsetHeight', { get: () => 44 });
    const ran = [];
    more.animate = (frames, how) => { const run = { cancel() {} }; ran.push({ from: frames[0].height, to: frames[1].height, ms: how.duration, run }); return run; };
    const push = () => {
      let held = false;
      toggle.listeners.click.forEach((f) => f.call(toggle, { preventDefault() { held = true; } }));
      return held;
    };
    const end = () => ran[ran.length - 1].run.onfinish();
    const held = push();
    end();
    const opened = more.open;
    push();
    const shutting = [more.open, more.classList.contains('is-shutting')];
    push(); // open again before it has shut
    ran[1].run.onfinish();
    end();
    const reopened = [more.open, more.classList.contains('is-shutting')];
    push();
    end();
    const shut = [more.open, more.classList.contains('is-shutting')];
    calm = true;
    return { held, opened, shutting, reopened, shut, steps: ran.map((r) => [r.from, r.to, r.ms]), calm: [push(), ran.length] };
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
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
