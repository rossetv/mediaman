// Runs the real account.js against the fake DOM and prints one scenario's outcome as JSON.
// Usage: node account_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle, Node } = require('./fake_dom.js');
const { parts, prepare } = require('./password_form_dom.js');

prepare(Node);

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const OLD = 'the-old-Password-1';
const NEW = 'Copper lantern 41';
const NOW = Date.UTC(2026, 5, 1, 12);
const at = (hours) => new Date(NOW + hours * 3600e3).toISOString();
let ids = 0;
const session = (expires, more) => ({ public_id: 'id' + (ids += 1), created_at: at(-48), last_used_at: at(-1), expires_at: at(expires), ...more });

// A focused control that is disabled loses the focus, as in a browser.
Object.defineProperty(Node.prototype, 'disabled', {
  get() { return Boolean(this._disabled); },
  set(on) {
    this._disabled = on;
    if (on && this.ownerDocument.activeElement === this) this.blur();
  },
});

// What was scrolled into view, and how; and of that, what was outside the pane's root.
const revealed = [];
const revealedNodes = [];
Node.prototype.scrollIntoView = function (how) { revealed.push([this.id, how && how.block]); revealedNodes.push(this.closest('#root') ? '' : this.id || this.className); };

// On screen, between the page's bars, unless a scenario says otherwise.
Node.prototype.getBoundingClientRect = () => ({ top: 300, bottom: 320 });

// `ai` adds the AI section, `tvs` the lists of TVs of a role, and `yes` answers a question.
// `tiles` puts the hub's tiles of a role: History's for everyone, Settings' for an admin.
function page(api, { ai = false, yes = true, tvs = '', changed = '', tiles = '', downloading = null, scripts = ['password-form.js'] } = {}) {
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.documentElement.clientHeight = 600;
  const value = ['span', { 'data-row-value': '' }];
  doc.body.appendChild(build(doc, ['div', { 'data-you-group': 'profile', id: 'root' },
    ['button', { id: 'account-row-email', 'data-you-go': 'profile/email' }, value], ['span', { id: 'account-head-email' }],
    ['button', { id: 'account-open-password', 'data-you-go': 'profile/password' }, value],
    ['button', { id: 'account-row-devices', 'data-you-go': 'profile/devices' }, value],
    ['form', { id: 'account-logout' }, ['button', { type: 'submit' }, 'Log out']],
    ['main', { id: 'main-content' },
      ['div', { id: 'account-password-sub' }, ...(changed ? [['time', { id: 'account-password-changed', datetime: changed }, 'long ago']] : []), ['span', {}, 'It signs out the others.']],
      ['div', { id: 'stage' },
        ['form', { id: 'account-password-form' }, ...parts({ old: 'account-old-password', fresh: 'account-new-password', submit: 'account-submit-password',
          result: 'account-password-result', words: 'Save new password' })]],
      ['span', { id: 'account-password-live' }],
      ['form', { id: 'account-email-form' },
        ['input', { id: 'account-email' }], ['span', { id: 'account-email-result' }], ['button', { id: 'account-submit-email', type: 'submit' }, 'Save']],
      ['ul', { id: 'account-sessions' }, ['li', {}, 'Loading']],
      ['div', {},
        ['button', { id: 'account-revoke-others' }], ['span', { id: 'account-revoke-why', hidden: '' }], ['span', { id: 'account-revoke-result' }]]]]));
  const root = doc.getElementById('root');
  if (ai) {
    root.appendChild(build(doc, ['section', { id: 'account-ai' }, ['button', { id: 'account-history', class: 'tog' }], ['button', { id: 'account-auto', class: 'tog', 'aria-checked': 'false' }], ['button', { id: 'account-clear-searches' }, 'Clear']]));
    doc.getElementById('account-ai').hidden = true;
  }
  if (tiles) {
    root.appendChild(build(doc, ['span', { id: 'account-tile-history', class: 'yo-tile__sub', 'data-me': 'amy' }, 'What was played']));
    if (tiles === 'admin') root.appendChild(build(doc, ['span', { id: 'account-tile-settings', class: 'yo-tile__sub' }, 'Libraries and more']));
  }
  if (downloading) {
    root.appendChild(build(doc, ['div', { id: 'account-dl' }, ['a', { href: '/?dl=1' }, ['span', { 'data-row-value': '' }]]]));
    doc.getElementById('account-dl').hidden = true;
    doc.body.appendChild(build(doc, ['a', { class: 'profile-dl', href: '/?dl=1', 'data-dl-open': '' }]));
  }
  if (tvs) {
    root.appendChild(build(doc, ['section', { id: 'account-tvs', 'data-me': 'amy' }, ['ul', { class: 'dev-list' }]]));
    doc.getElementById('account-tvs').hidden = true;
  }
  if (tvs === 'admin') {
    root.appendChild(build(doc, ['section', { id: 'account-tvs-all' }, ['ul', { class: 'dev-list' }]]));
    doc.getElementById('account-tvs-all').hidden = true;
  }
  const $ = (id) => doc.getElementById(id);
  let resets = 0;
  $('account-password-form').reset = () => { resets += 1; ['old', 'new'].forEach((f) => { $('account-' + f + '-password').value = ''; }); };
  $('account-email').value = $('account-email').defaultValue = 'was@example.com';
  ['old', 'new'].forEach((f) => { $('account-' + f + '-password').value = ''; });
  // The shell's pane, as watch/you.js gives it.
  const you = { went: [], guards: [], shown: () => true, go: (to) => you.went.push(to), close: () => you.went.push('close'), guard: (group, how) => you.guards.push([group, how]), scroller: () => null };
  const MM = { api: { messageOf: (e, d) => (e instanceof Error && e.message) || d, ...api }, reauth: { run: (f, words) => { asked.push(words); return f(); } }, you };
  if (downloading) MM.downloading = { subscribe: (fn) => { downloading.readers.push(fn); } };
  const dropped = [];
  MM.tvRemote = { drop: (id) => dropped.push(id) };
  const asked = [];
  const toasts = [];
  const UIFeedback = { toast: (text) => toasts.push(text), error: (text) => toasts.push('error: ' + text), confirm: (words) => { asked.push(words); return Promise.resolve(yes); } };
  const timers = [];
  const setTimeout = (fn) => timers.push(fn);
  class FixedDate extends Date { static now() { return NOW; } }
  // A top bar of 64px and a bottom bar of 72px.
  const getComputedStyle = () => ({ scrollPaddingTop: '64px', scrollPaddingBottom: '72px' });
  const told = [];
  const session = { 'mm.watch.ask': 'a question', other: 'x' };
  doc.dispatchEvent = (e) => { told.push(e.type); };
  const ctx = vm.createContext({ document: doc, MM, UIFeedback, setTimeout, clearTimeout() {}, TextEncoder, window: page.window = { MM, innerWidth: 390, resized: [], scrolled: [], scrollTo() { this.scrolled.push('scrollTo'); }, scrollBy() { this.scrolled.push('scrollBy'); }, scroll() { this.scrolled.push('scroll'); }, get scrollY() { this.scrolled.push('scrollY'); return 0; }, addEventListener(type, fn) { if (type === 'resize') this.resized.push(fn); }, matchMedia: () => ({ matches: true }), sessionStorage: { removeItem: (k) => { delete session[k]; } } }, Date: FixedDate, Event, getComputedStyle });
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'settings/rows.js', ...scripts, 'account.js'].forEach(run);
  return {
    $, dropped, you, root, doc, run,
    guard: () => you.guards[0][1], told, rows_: () => MM.settings.rows, asked, toasts, session,
    wait: () => timers.splice(0).forEach((fn) => fn()),
    resets: () => resets,
    key: (id, key) => $(id).listeners.keydown.forEach((fn) => fn({ key })),
    button: (n) => $('account-sessions').children[n].querySelector('button'),
    // a timer, and what it sets going, until nothing is left
    async rest() {
      for (let i = 0; i < 8; i += 1) { timers.splice(0).forEach((fn) => fn()); await settle(); }
    },
    fill: (old, fresh) => { $('account-old-password').value = old; $('account-new-password').value = fresh; },
    fire: (id, type) => $(id).listeners[type].forEach((fn) => fn({ target: $(id), preventDefault() {}, stopPropagation() {} })),
    type(id, value) { $(id).value = value; $('account-password-form').listeners.input.forEach((fn) => fn({ target: $(id) })); },
    says: () => ['account-old-password-why', 'account-new-password-why', 'account-password-result'].filter((id) => $(id).textContent),
    line: () => $('account-password-sub').lastElementChild.textContent,
    sub: () => $('account-password-sub').children.filter((n) => !n.hidden).map((n) => n.textContent).join(' '),
    shown: (view) => (doc.listeners['mm:you-shown'] || []).forEach((fn) => fn({ detail: { group: view.split('/')[0], view } })),
    focused: () => doc.activeElement.id || 'body',
    active: () => doc.activeElement,
    press: (id) => {
      const button = $(id);
      button.focus();
      (button.disabled ? [] : button.listeners.click || []).forEach((fn) => fn({ preventDefault() {} }));
    },
    rows: () => $('account-sessions').children.map((li) => li.textContent),
    say: (id) => [$(id).className, $(id).textContent],
  };
}

const dlState = (total, active, percent, lost = false) => ({ summary: { total, active, ready: 0, waiting: total - active, percent }, items: [], lost });

const tvOf = (id, name, more) => ({ id, name, on: false, paired_at: '2026-03-12T09:00:00Z', last_seen_at: at(-30), ...more });
// The TV lists' answers by address; the rest of the page gets none.
const tvApi = (mine, all, more) => ({
  get: (url) => (url === '/api/tvs' ? Promise.resolve({ tvs: mine }) : url === '/api/tvs/all' ? Promise.resolve({ tvs: all }) : Promise.resolve({ sessions: [] })),
  ...more,
});
const tvRows = (p, id = 'account-tvs') => p.$(id).querySelector('.dev-list').children;
const buttonOf = (row, words) => row.querySelectorAll('button').find((b) => b.textContent === words);
const push = (button) => button.listeners.click.forEach((fn) => fn({}));

const scenarios = {
  // Downloads ready to watch are what the hub's row says, even while another still downloads.
  async hubReady() {
    const downloading = { readers: [] };
    const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { downloading });
    const says = (summary) => {
      downloading.readers.forEach((fn) => fn({ summary, items: [], lost: false }));
      return p.$('account-dl').querySelector('[data-row-value]').textContent;
    };
    return {
      one: says({ total: 1, active: 0, ready: 1, waiting: 0, percent: 100 }),
      busy: says({ total: 3, active: 1, ready: 2, waiting: 0, percent: 40 }),
    };
  },
  // While something downloads the hub has a row that leads to it: the pane closes, then Downloading opens.
  async hubDownloading() {
    const downloading = { readers: [] };
    const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { downloading });
    const row = p.$('account-dl');
    const tell = (state) => downloading.readers.forEach((fn) => fn(state));
    const seen = () => ({ hidden: row.hidden, says: row.querySelector('[data-row-value]').textContent });
    tell(dlState(2, 1, 89));
    const busy = seen();
    tell(dlState(1, 0, 0, true));
    const lost = seen();
    p.doc.removeEventListener = (t, fn) => { p.doc.listeners[t] = (p.doc.listeners[t] || []).filter((f) => f !== fn); };
    let ringClicks = 0;
    p.doc.querySelector('.profile-dl').click = () => { ringClicks += 1; };
    const { click } = require('./fake_dom.js');
    const e = { defaultPrevented: false };
    click(p.doc, row.querySelector('a'), { preventDefault() { e.defaultPrevented = true; this.defaultPrevented = true; } });
    const before = { went: p.you.went.slice(), ringClicks, prevented: e.defaultPrevented };
    (p.doc.listeners['mm:you-hidden'] || []).forEach((fn) => fn({ detail: { group: 'profile', view: 'profile' } }));
    await p.rest();
    const after = ringClicks;
    (p.doc.listeners['mm:you-hidden'] || []).forEach((fn) => fn({ detail: { group: 'profile', view: 'profile' } }));
    await p.rest();
    tell(dlState(0, 0, 0));
    return { busy, lost, before, after, again: ringClicks, none: row.hidden };
  },
  async tvs() {
    const p = page(tvApi([tvOf(1, 'Living room', { on: true }), tvOf(2, '<b>Den'), tvOf(3, 'New', { last_seen_at: at(-400) }), tvOf(4, 'Kitchen TV', { kind: 'older' })], []), { tvs: 'viewer' });
    await settle();
    const none = page(tvApi([], []), { tvs: 'viewer' });
    await settle();
    return { hidden: p.$('account-tvs').hidden, rows: tvRows(p).map((r) => r.querySelector('.me-txt').children.map((c) => c.textContent)), none: none.$('account-tvs').hidden };
  },
  async tvsOfAll() {
    const all = [tvOf(1, 'Mine', { owner: 'amy', kind: 'page' }), tvOf(2, 'Theirs', { owner: 'bob', kind: 'page' }), tvOf(3, 'Older', { owner: null, kind: 'older' })];
    const p = page(tvApi([tvOf(1, 'Mine')], all), { tvs: 'admin', yes: false });
    await settle();
    push(buttonOf(tvRows(p, 'account-tvs-all')[0], 'Unpair'));
    await settle();
    return { rows: tvRows(p, 'account-tvs-all').map((r) => r.textContent), asked: p.asked, left: tvRows(p, 'account-tvs-all').length };
  },
  async tvRename() {
    const sent = [];
    let answer = () => Promise.reject({ error: 'x' });
    const p = page(tvApi([tvOf(1, 'TV')], [], { patch: (url, body) => { sent.push([url, body]); return answer(body); } }), { tvs: 'viewer' });
    await settle();
    Node.prototype.select = () => {};
    const row = tvRows(p)[0];
    push(buttonOf(row, 'Rename'));
    const form = row.querySelector('form');
    const input = row.querySelector('input');
    const drawn = form.childNodes.map((n) => n.textContent || n.tagName.toLowerCase());
    const submit = () => form.listeners.submit.forEach((fn) => fn({ preventDefault() {} }));
    input.value = '  ';
    submit();
    const empty = [sent.length, input.getAttribute('aria-invalid')];
    input.value = ' Den ';
    submit();
    submit();
    await settle();
    const failed = [sent.length, p.toasts.splice(0), Boolean(row.querySelector('form'))];
    answer = (body) => Promise.resolve({ tv: { id: 1, name: body.name } });
    submit();
    await settle();
    return { drawn, empty, failed, sent: sent[1], name: row.querySelector('.me-lbl').textContent, label: buttonOf(row, 'Rename').getAttribute('aria-label'), toasts: p.toasts };
  },
  async tvLeave() {
    const sent = [];
    const p = page(tvApi([tvOf(1, 'Den', { people: ['amy', 'bob'] })], [], { delete: (url) => { sent.push(url); return Promise.resolve({ ok: true }); } }), { tvs: 'viewer' });
    await settle();
    const row = tvRows(p)[0];
    const words = row.querySelectorAll('button').map((b) => [b.textContent, b.getAttribute('aria-label')]);
    push(row.querySelectorAll('button')[1]);
    await settle();
    p.wait();
    return { words, sent, toasts: p.toasts, asked: p.asked.length };
  },
  // Left as one's own film plays on it, and as another's does.
  async tvLeavePlaying() {
    const out = {};
    for (const mine of [true, false]) {
      const order = [];
      const api = tvApi([tvOf(1, 'Den', { people: ['amy', 'bob'] })], [], { delete: (url) => { order.push('DELETE ' + url); return Promise.resolve({ ok: true }); } });
      const list = api.get;
      api.get = (url) => {
        if (url !== '/api/tvs/1') return list(url);
        order.push('GET ' + url);
        return Promise.resolve({ tv: { id: 1, play: { title: 'Dune', state: 'playing', mine } } });
      };
      const p = page(api, { tvs: 'viewer' });
      await settle();
      push(buttonOf(tvRows(p)[0], 'Leave'));
      await settle();
      p.wait();
      out[mine ? 'mine' : 'theirs'] = { order, toasts: p.toasts };
    }
    return out;
  },
  async tvUnpair() {
    const sent = [];
    let answer = () => Promise.reject({ status: 500 });
    const p = page(tvApi([tvOf(1, 'A'), tvOf(2, 'B')], [], { delete: (url) => { sent.push(url); return answer(); } }), { tvs: 'viewer' });
    await settle();
    const look = [buttonOf(tvRows(p)[0], 'Unpair').className, buttonOf(tvRows(p)[0], 'Rename').className];
    const press = async () => {
      push(buttonOf(tvRows(p)[0], 'Unpair'));
      await settle();
      p.wait();
    };
    await press();
    const failed = [tvRows(p).length, p.toasts.splice(0)];
    const kept = p.dropped.length;
    answer = () => Promise.resolve({ ok: true });
    await press();
    const one = [tvRows(p).length, p.$('account-tvs').hidden, p.toasts.splice(0)];
    // already unpaired by someone else
    answer = () => Promise.reject({ status: 404 });
    await press();
    return { look, failed, one, sent, last: [tvRows(p).length, p.$('account-tvs').hidden], asked: p.asked.length, kept, dropped: p.dropped };
  },
  async sessions() {
    const here = session(9, { current: true });
    let list = [session(5), here, session(-1)];
    let posts = 0;
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { posts += 1; list = [{ ...here, public_id: 'fresh' }]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    const first = { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, text: p.rows()[1], why: !p.$('account-revoke-why').hidden };
    p.press('account-revoke-others');
    p.press('account-revoke-others');
    const whileSending = p.focused();
    await settle();
    return { first, posts, whileSending, after: { rows: p.rows().length, disabled: p.$('account-revoke-others').disabled, focused: p.focused(), say: p.say('account-revoke-result'), why: !p.$('account-revoke-why').hidden } };
  },
  async thisDevice() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9), session(5, { current: true, device_label: 'Chrome on macOS' }), session(5, { created_at: '2025-11-02T20:20:00Z', device_label: '<b>Safari on iPhone' })] }) });
    await settle();
    return p.rows();
  },
  // one device signed out: refused first, then gone
  async signOutOne() {
    const list = [session(9, { current: true }), session(5, { device_label: 'Safari on iPhone', public_id: 'a/b' }), session(6)];
    const sent = [];
    let answer = () => Promise.reject(Object.assign(new Error('No luck')));
    const p = page({ get: () => Promise.resolve({ sessions: list }), delete: (url) => { sent.push(url); return answer(); } });
    await settle();
    const own = p.button(0);
    const label = p.button(1).getAttribute('aria-label');
    const push = async () => {
      const button = p.button(1);
      button.focus();
      for (let i = 0; i < 2; i += 1) button.listeners.click.forEach((fn) => fn({}));
      const busy = [button.textContent, button.style.width, button.getAttribute('aria-label')];
      await settle();
      return busy;
    };
    const busy = await push();
    const labelAfter = p.button(1).getAttribute('aria-label');
    const refused = { rows: p.rows().length, says: p.button(1).textContent, toasts: p.toasts.splice(0), focusKept: p.$('account-sessions').children[1].contains(p.active()) };
    answer = () => Promise.reject({ error: 'reauth_cancelled' });
    await push();
    const cancelled = { rows: p.rows().length, toasts: p.toasts.splice(0) };
    answer = () => Promise.resolve({ ok: true });
    await push();
    const gone = { rows: p.rows().length, toasts: p.toasts.splice(0), onNext: p.button(1) === p.active(), why: !p.$('account-revoke-why').hidden };
    p.toasts.length = 0;
    await push();
    return { own: Boolean(own), label, labelAfter, busy, sent, words: p.asked[0], refused, cancelled, gone, last: { rows: p.rows().length, focused: p.focused(), disabled: p.$('account-revoke-others').disabled, why: !p.$('account-revoke-why').hidden } };
  },
  // the first list, asked before a new password, is answered after the one asked since
  async lateList() {
    const gives = [];
    const here = session(9, { current: true });
    const p = page({ get: () => new Promise((give, fail) => gives.push({ give, fail })), post: () => Promise.resolve({ ok: true }) });
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    await p.rest();
    gives[1].give({ sessions: [here] });
    await settle();
    gives[0].give({ sessions: [here, session(5), session(5)] });
    await settle();
    return p.rows().length;
  },
  // a list asked before a device was signed out still holds it
  async listBeforeSignOut() {
    const gives = [];
    const here = session(9, { current: true });
    const other = session(5);
    const p = page({
      get: () => new Promise((give) => gives.push(give)),
      post: () => Promise.resolve({ ok: true }), delete: () => Promise.resolve({ ok: true }),
    });
    gives[0]({ sessions: [here, other, session(5)] });
    await settle();
    p.press('account-revoke-others');
    await settle();
    p.button(1).listeners.click.forEach((fn) => fn({}));
    await settle();
    gives[1]({ sessions: [here, other] });
    await settle();
    return p.rows().length;
  },
  // what is typed in the email and password views, and what the pane's guard is told of it
  async guard() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) });
    await settle();
    const [group, how] = p.you.guards[0];
    const dirty = () => [['profile/email', 'profile'], ['profile/email', null], ['profile/email', 'profile/email'], ['profile/password', 'profile'], ['profile', 'history']]
      .map(([from, to]) => how.dirty(from, to));
    const typeEmail = (value) => { p.$('account-email').value = value; p.fire('account-email-form', 'input'); };
    const out = { group, title: how.title, body: how.body, guards: p.you.guards.length, clean: dirty() };
    typeEmail(' was@example.com ');
    out.sameEmail = dirty();
    typeEmail('new@example.com');
    out.email = dirty();
    how.discard();
    out.emailDiscarded = [p.$('account-email').value, p.$('account-submit-email').disabled, dirty()];
    p.type('account-old-password', 'x');
    out.old = dirty();
    p.type('account-old-password', '');
    p.type('account-new-password', 'y');
    out.fresh = dirty();
    how.discard();
    out.passwordDiscarded = [p.$('account-old-password').value, p.$('account-new-password').value, dirty()];
    return out;
  },
  // the hub's tiles: the latest event, and for an admin how the services stood when Settings last checked them
  async tiles() {
    const clock = (iso) => new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    const event = (who, hours, more) => ({ who: { name: who }, fields: [{ key: 'when' }, { key: 'when', at: at(hours) }], line_title: 'Interstellar', ...more });
    const signIn = (who, hours) => event(who, hours, { action: 'sec:login.success', is_security: true, line_title: 'Signed in', action_label: 'security' });
    // `conn` is Settings' pills as its own checks left them ([service, tone]), or null where Settings never loaded.
    async function hub(items, conn, role = 'admin') {
      const asked = [];
      const p = page({
        get: (url) => { asked.push(url); return Promise.resolve(url.indexOf('/api/history') === 0 ? { items } : { sessions: [] }); },
        post: (url) => { asked.push(url); return Promise.resolve({ ok: true }); },
      }, { tiles: role });
      (conn || []).forEach(([service, tone]) => {
        p.doc.body.appendChild(build(p.doc, ['span', { class: 'conn ' + tone, 'data-conn': service }, ['span', { 'data-conn-label': '' }, tone]]));
      });
      await p.rest();
      p.shown('profile');
      await p.rest();
      const first = asked.filter((u) => u !== '/api/users/sessions');
      p.shown('profile');
      await p.rest();
      const tile = (id) => p.$(id) && [p.$(id).textContent, p.$(id).classList.contains('is-bad')];
      return { history: tile('account-tile-history'), settings: tile('account-tile-settings'), asked: first, again: asked.length > first.length + 1 };
    }
    return {
      play: [await hub([signIn('amy', -1), event('marlowe', -2, { action: 'play', finished: true, action_label: 'finished' })], null),
        'marlowe finished Interstellar · ' + clock(at(-2))],
      down: await hub([], [['plex', 'err'], ['openai', 'err'], ['sonarr', 'warn'], ['radarr', 'off']]),
      oneDown: await hub([signIn('amy', -1)], [['plex', 'ok'], ['sonarr', 'err'], ['sonarr', 'err']]),
      fine: await hub([event('amy', -30, { action: 'downloaded', action_label: 'downloaded', line_title: 'Weapons' })], [['plex', 'ok'], ['sonarr', 'ok'], ['radarr', 'off']]),
      limited: await hub([], [['plex', 'ok'], ['sonarr', 'warn']]),
      testing: await hub([], [['plex', 'ok'], ['sonarr', 'untested']]),
      none: await hub([], [['plex', 'off'], ['sonarr', 'off']]),
      noVerb: await hub([event('mediaman', -2, { action: 'delete_failed', action_label: 'delete failed', line_title: 'Big Hero 6' })], null, 'viewer'),
      own: [await hub([signIn('amy', -1), signIn('rosa', -3)], null, 'viewer'), clock(at(-1))],
      someone: await hub([signIn('rosa', -3)], null, 'viewer'),
      day: new Date(at(-30)).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }),
    };
  },
  // Save waits for what the rules ask; pressed before, it says what is missing
  async passwordHold() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => new Promise(() => {}) });
    await settle();
    const held = () => p.$('account-submit-password').getAttribute('aria-disabled') === 'true';
    const out = { empty: held() };
    p.fire('account-password-form', 'submit');
    out.pressed = p.says().map((id) => p.$(id).textContent);
    p.type('account-old-password', OLD);
    p.type('account-new-password', 'short');
    out.weak = held();
    p.type('account-new-password', OLD);
    out.same = held();
    p.type('account-new-password', NEW);
    out.ready = held();
    p.type('account-old-password', '');
    out.noOld = held();
    p.type('account-old-password', OLD);
    p.fire('account-password-form', 'submit');
    await settle();
    out.sending = held();
    return out;
  },
  // opening the view puts the cursor in its first field; the hub takes nothing
  async passwordFocus() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) });
    await settle();
    p.$('account-open-password').focus();
    p.shown('profile');
    const hub = p.focused();
    p.shown('profile/password');
    return [hub, p.focused()];
  },
  async passwordRule() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) });
    await settle();
    const says = () => p.$('account-password-form').querySelector('.pwd-rule-w').textContent + p.$('account-password-form').querySelector('.pwd-rule-n').textContent;
    const out = [says()];
    p.type('account-new-password', 'abc');
    out.push(says());
    p.type('account-new-password', '');
    out.push(says());
    p.type('account-new-password', 'abcd');
    p.guard().discard();
    out.push(says());
    return out;
  },
  // when it was changed last, in the view and on the hub's row
  async passwordChanged() {
    const out = [];
    for (const changed of ['', '2026-03-14T09:26:53+00:00', '2025-03-14T09:26:53+00:00']) {
      const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { changed });
      await settle();
      out.push([p.sub(), p.$('account-open-password').textContent]);
    }
    return out;
  },
  // what the note under the form says of so many other sign-ins and TVs; null lists did not load
  async devicesLine() {
    const says = async (others, tvs) => {
      const list = (n, make) => (n === null ? () => Promise.reject(new Error('x')) : () => Promise.resolve(make(n)));
      const answers = { '/api/users/sessions': list(others, (n) => ({ sessions: [session(9, { current: true })].concat(Array.from({ length: n }, () => session(5))) })), '/api/tvs': list(tvs, (n) => ({ tvs: Array.from({ length: n }, (_x, i) => tvOf(i + 1, 'TV')) })) };
      const p = page({ get: (url) => answers[url]() }, { tvs: 'viewer' });
      await settle();
      return p.line();
    };
    return [await says(0, 0), await says(1, 0), await says(2, 1), await says(0, 3), await says(null, 1), await says(1, null)];
  },
  // what is drawn and said once it is saved, and what the note keeps of it
  async passwordDone() {
    const out = [];
    for (const [others, tvs, answer] of [[2, 1, { signed_out: 2, unpaired: ['<b>Den'] }], [0, 0, { signed_out: 0, unpaired: [] }], [7, 3, { signed_out: 7, unpaired: ['a', 'b', 'c'] }], [1, 2, {}]]) {
      const api = tvApi(Array.from({ length: tvs }, (_x, i) => tvOf(i + 1, 'TV')), [], { post: () => Promise.resolve(answer) });
      const signIns = [session(9, { current: true, device_label: 'Safari on macOS' })].concat(Array.from({ length: others }, () => session(5, { device_label: 'Safari on iPhone' })));
      const get = (url) => (url === '/api/users/sessions' ? Promise.resolve({ sessions: signIns }) : api.get(url));
      const p = page({ ...api, get }, { tvs: 'viewer', changed: '2026-03-14T09:26:53+00:00' });
      await settle();
      p.fill(OLD, NEW);
      p.fire('account-password-form', 'submit');
      await settle();
      const done = p.$('stage').querySelector('.pwd-done');
      const row = done.querySelector('.pwd-dev-row');
      const during = { says: done.querySelector('.pwd-done-s').textContent, live: p.$('account-password-live').textContent, sub: p.$('account-password-sub').children[0].textContent, hub: p.$('account-open-password').textContent, icons: row ? row.querySelectorAll('use').map((u) => u.getAttribute('href').slice(3)).join(' ') : '', more: row && row.querySelector('.pwd-dev-n') ? row.querySelector('.pwd-dev-n').textContent : '' };
      await p.rest();
      out.push({ ...during, after: p.$('account-password-sub').textContent, faded: p.$('account-password-sub').children.filter((n) => n.tagName === 'SPAN' && !n.classList.contains('is-fresh')).map((n) => n.textContent), left: Boolean(p.$('stage').querySelector('.pwd-done')) });
    }
    return out;
  },
  async emailSave() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), patch: () => { throw new Error('sent'); } });
    await settle();
    const type = (value) => { p.$('account-email').value = value; p.fire('account-email-form', 'input'); return !p.$('account-submit-email').disabled; };
    const out = [!p.$('account-submit-email').disabled, type('new@example.com'), type(' was@example.com '), type('new')];
    p.fire('account-email-form', 'submit');
    const bad = [p.say('account-email-result'), p.$('account-email').getAttribute('aria-invalid'), p.focused()];
    p.guard().discard();
    return { out, bad, undone: [p.$('account-email').value, !p.$('account-submit-email').disabled, p.say('account-email-result')[1]] };
  },
  async onlySession() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9)] }) });
    await settle();
    return { disabled: p.$('account-revoke-others').disabled, why: !p.$('account-revoke-why').hidden };
  },
  async sessionsFail() {
    const p = page({ get: () => Promise.reject(new Error('x')), post: () => Promise.reject(new Error('No luck')) });
    await settle();
    const first = { rows: p.rows(), disabled: p.$('account-revoke-others').disabled };
    p.press('account-revoke-others');
    await settle();
    return { first, after: { focused: p.focused(), disabled: p.$('account-revoke-others').disabled, say: p.say('account-revoke-result') } };
  },
  async password() {
    let list = [session(5), session(9)];
    const sent = [];
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: (_url, body) => { sent.push(body); list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    p.fill(OLD, NEW);
    p.$('account-submit-password').focus();
    p.fire('account-password-form', 'submit');
    p.fire('account-password-form', 'submit');
    const whileSending = p.focused();
    p.type('account-new-password', NEW);
    // leaving the view meanwhile empties nothing on its way
    p.guard().discard();
    const working = [p.$('account-password-form').querySelector('.pwd-said').textContent, p.$('account-submit-password').textContent, p.$('account-new-password').value];
    const revealedSending = revealed.splice(0);
    await settle();
    p.guard().discard();
    const drawn = Boolean(p.$('stage').querySelector('.pwd-done'));
    await p.rest();
    const out = { sent, whileSending, working, drawn, asked: p.asked, went: p.you.went, focused: p.focused(), rows: p.rows().length, toasts: p.toasts, back: !p.$('stage').querySelector('.pwd-done'), resets: p.resets(), left: [p.$('account-old-password').value, p.$('account-new-password').value], says: p.$('account-submit-password').textContent };
    revealed.splice(0);
    p.fill('x', 'short');
    p.fire('account-password-form', 'submit');
    out.revealedRefusal = revealed.splice(0);
    p.type('account-new-password', 'shorter');
    return { ...out, revealedSending, revealedEmptied: revealed.splice(0) };
  },
  // what is typed is out of the page once it is saved, and the form is whole again after
  async passwordForgotten() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    const left = [p.$('account-old-password').value, p.$('account-new-password').value];
    await p.rest();
    const form = p.$('account-password-form');
    return { left, state: [Boolean(p.$('stage').querySelector('.pwd-done')), form.inert, form.getAttribute('aria-busy')] };
  },
  // the pane's guard discards as the tick is drawn
  async passwordDiscardedAsItIsDrawn() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    p.guard().discard();
    await p.rest();
    const form = p.$('account-password-form');
    return [p.resets(), form.inert, form.getAttribute('aria-busy'), p.$('account-old-password').readOnly];
  },
  // the view holds the form ready to type in, and nothing listens to the window's size
  async passwordView() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { changed: '2026-03-14T09:26:53+00:00' });
    await settle();
    p.type('account-old-password', 'x');
    return { ready: !p.$('account-submit-password').disabled && !p.$('account-old-password').readOnly, resized: page.window.resized.length };
  },
  // the form's script did not load: the rest of the pane stands
  async passwordWithout() {
    const p = page({ get: () => Promise.resolve({ sessions: [session(9)] }) }, { scripts: [] });
    await settle();
    p.$('account-email').value = 'new@example.com';
    p.fire('account-email-form', 'input');
    return { submit: p.$('account-submit-password').disabled, rows: p.rows().length, email: !p.$('account-submit-email').disabled, dirty: p.guard().dirty('profile/password', 'profile') };
  },
  // one who went to another field while it was drawn keeps their place
  async passwordLeft() {
    const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.resolve({ ok: true }) });
    await settle();
    p.fill(OLD, NEW);
    p.fire('account-password-form', 'submit');
    await settle();
    p.$('account-email').focus();
    await p.rest();
    return [p.focused(), Boolean(p.$('stage').querySelector('.pwd-done'))];
  },
  // for each form, what an answer brings into view when the reader is still there (with the keyboard on what is around
  // the button too, as Safari leaves a clicked one), has scrolled its status out of view or under a bar, or has the keyboard in something else
  async lateAnswer() {
    const ids = { email: 'account-submit-email', password: 'account-submit-password', sessions: 'account-revoke-others' };
    const results = { email: 'account-email-result', password: 'account-password-result', sessions: 'account-revoke-result' };
    const late = async (which, refused, { top = 300, focus } = {}) => {
      let give;
      const pending = () => new Promise((resolve, reject) => { give = refused ? () => reject(Object.assign(new Error('No'), { error: 'wrong_password' })) : () => resolve({ ok: true }); });
      const p = page({ get: () => Promise.resolve({ sessions: [session(5), session(9)] }), patch: pending, post: pending });
      await settle();
      p.$('account-email').value = 'vic@example.com';
      p.fire('account-email-form', 'input');
      revealed.splice(0);
      p.fill(OLD, NEW);
      if (which === 'sessions') p.press(ids.sessions);
      else { p.$(ids[which]).focus(); p.fire('account-' + which + '-form', 'submit'); }
      const onPress = revealed.splice(0).map(([id]) => id).join() === ids[which];
      p.$(which === 'sessions' ? results[which] : ids[which]).getBoundingClientRect = () => ({ top, bottom: top + 20 });
      if (focus === 'around') p.$('main-content').focus();
      else if (focus) p.$(which === 'email' ? 'account-old-password' : 'account-email').focus();
      give();
      await settle();
      await p.rest();
      const field = refused && { email: 'account-email', password: 'account-old-password' }[which];
      const shown = field ? [field, which === 'email' ? results[which] : field + '-why'].join() : results[which];
      const answered = revealed.splice(0).map(([id]) => id).join();
      const moved = answered === '' ? false : answered === shown || answered;
      return [onPress, moved, refused ? (field && p.focused() === field) || false : p.focused(), Boolean(p.$(field === 'account-old-password' ? field + '-why' : results[which]).textContent) || p.toasts.length > 0 || Boolean(p.$('account-password-live').textContent)];
    };
    const each = async (which, refused) => ({
      there: await late(which, refused),
      around: await late(which, refused, { focus: 'around' }),
      below: await late(which, refused, { top: 700 }),
      above: await late(which, refused, { top: -300 }),
      underTheBottomBar: await late(which, refused, { top: 540 }),
      underTheTopBar: await late(which, refused, { top: 20 }),
      elsewhere: await late(which, refused, { focus: 'elsewhere' }),
    });
    return {
      email: await each('email'), password: await each('password'), sessions: await each('sessions'),
      sessionsRefused: await each('sessions', true),
      emailRefused: await each('email', true), passwordRefused: await each('password', true),
    };
  },
  // where each refusal of a new password is said, and where the cursor goes
  async passwordRefusals() {
    const refuse = async (fill, error, { again } = {}) => {
      const p = page({ get: () => Promise.resolve({ sessions: [] }), post: () => Promise.reject(error) });
      await settle();
      p.fill(fill.old, fill.new);
      p.$('account-submit-password').focus();
      if (again) {
        p.fire('account-password-form', 'submit');
        await settle();
        p.$('account-old-password').value = '';
      }
      revealed.splice(0);
      p.fire('account-password-form', 'submit');
      await settle();
      const said = p.says();
      const red = p.$('account-password-form').querySelectorAll('.pwd-rule').filter((r) => r.classList.contains('is-bad')).map((r) => r.getAttribute('data-rule'));
      const out = { said, red, words: said.map((id) => p.$(id).textContent).join(), focused: p.focused(), kept: p.$('account-new-password').value === fill.new, busy: p.$('account-password-form').getAttribute('aria-busy'), last: revealed.splice(0).slice(-2).map(([id]) => id) };
      p.type('account-old-password', 'typed');
      p.type('account-new-password', 'typed');
      return { ...out, typed: p.says() };
    };
    const failure = (error, issues, rules) => Object.assign(new Error('No'), { error, issues, data: { rules } });
    const good = { old: OLD, new: NEW };
    return {
      old: await refuse({ ...good, old: '' }), new: await refuse({ ...good, new: 'short' }), known: await refuse({ old: NEW, new: NEW }),
      wrong: await refuse(good, failure('wrong_password')),
      weak: await refuse(good, failure('weak_password', ['a digit'], ['common'])),
      same: await refuse(good, failure('same_password', undefined, ['same'])),
      limit: await refuse(good, failure('too_many_requests')),
      unknown: await refuse(good, failure('forbidden')),
      cancelled: await refuse(good, failure('reauth_cancelled')),
      // the status still says the limit, and the field was emptied by what fires no input event
      afterLimit: await refuse(good, failure('too_many_requests'), { again: true }),
    };
  },
  // how the focus goes to the list of sign-ins once the others are signed out
  async focusOnTheList() {
    let list = [session(5), session(9)];
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: () => { list = [session(9)]; return Promise.resolve({ ok: true }); },
    });
    await settle();
    let how = null;
    p.$('account-sessions').focus = (opts) => { how = opts || {}; };
    p.press('account-revoke-others');
    await settle();
    return how;
  },
  async toldOfPassword() {
    const out = [];
    for (const refused of [false, true]) {
      const p = page({ get: () => Promise.resolve({ sessions: [] }), patch: () => (refused ? Promise.reject(new Error('no')) : Promise.resolve({})) });
      await settle();
      p.$('account-email').value = 'vic@example.com';
      p.fire('account-email-form', 'submit');
      const during = p.told.length;
      await settle();
      out.push([during, p.told]);
    }
    return out;
  },
  async email() {
    const sent = [];
    let answer = () => Promise.resolve({ ok: true });
    const p = page({
      get: () => Promise.resolve({ sessions: [] }),
      patch: (url, body) => { sent.push([url, body]); return answer(); },
    });
    const focus = [];
    const save = async (value) => {
      p.$('account-email').value = value;
      p.$('account-submit-email').focus();
      p.fire('account-email-form', 'submit');
      p.fire('account-email-form', 'submit');
      focus.push(p.focused());
      await settle();
      focus.push(p.focused());
      return p.say('account-email-result').concat(p.toasts.splice(0), p.$('account-submit-email').disabled);
    };
    const saved = await save(' vic@example.com ');
    p.fire('account-email-form', 'input');
    const typed = p.say('account-email-result')[1];
    const removed = await save('');
    answer = () => Promise.reject(new Error('Not an email address'));
    const refused = await save('nope@example.com');
    answer = () => Promise.reject({ error: 'reauth_cancelled' });
    const cancelled = await save('vic@example.com');
    return { sent, focus, saved, typed, removed, refused, cancelled };
  },
  // the switch as first drawn, pressed twice at once, refused, and with its answer lost
  async history() {
    const stored = { enabled: true, available: true };
    const sent = [];
    let put = (body) => { stored.enabled = body.enabled; return Promise.resolve({}); };
    let get = () => Promise.resolve({ ...stored });
    const p = page({
      get: (url) => (url === '/api/search/assist/history' ? get() : Promise.resolve({ sessions: [] })),
      put: (url, body) => { sent.push([url, body.enabled]); return put(body); },
    }, { ai: true });
    const hiddenAtFirst = p.$('account-ai').hidden;
    await settle();
    const state = () => [p.$('account-history').getAttribute('aria-checked'), p.$('account-history').classList.contains('on')];
    const push = async () => {
      p.press('account-history');
      p.press('account-history');
      const during = state();
      await settle();
      return { during, after: state(), toasts: p.toasts.splice(0), focused: p.focused(), busy: p.$('account-history').getAttribute('aria-disabled') };
    };
    const first = { hiddenAtFirst, hidden: p.$('account-ai').hidden, state: state() };
    const off = await push();
    const good = put;
    put = () => Promise.reject(new Error('No luck'));
    const refused = await push();
    get = () => Promise.reject(new Error('gone'));
    const offline = await push();
    get = () => Promise.resolve({ ...stored });
    put = (body) => { good(body); return Promise.reject(new Error('lost')); };
    const lost = await push();
    return { first, off, refused, offline, lost, sent };
  },
  // Ask AI as I type: pressed and saved, then pressed and refused
  async autoAsk() {
    const sent = [];
    let put = () => Promise.resolve({});
    const p = page({
      get: (url) => Promise.resolve(url === '/api/search/assist/history' ? { enabled: true, available: true } : { sessions: [] }),
      put: (url, body) => { sent.push([url, body]); return put(); },
    }, { ai: true });
    let loads = 0;
    page.window.MMWatch = { ask: { load: () => { loads += 1; } } };
    await settle();
    const state = () => [p.$('account-auto').getAttribute('aria-checked'), p.$('account-auto').classList.contains('on')];
    const push = async () => {
      p.press('account-auto');
      p.press('account-auto');
      const during = state();
      await settle();
      return { during, after: state(), loads, toasts: p.toasts.splice(0), busy: p.$('account-auto').getAttribute('aria-disabled') };
    };
    const on = await push();
    put = () => Promise.reject(new Error('No luck'));
    const refused = await push();
    return { on, refused, sent };
  },
  async historyHidden() {
    const hidden = async (get) => {
      const p = page({ get: (url) => (url === '/api/search/assist/history' ? get() : Promise.resolve({ sessions: [] })) }, { ai: true });
      await settle();
      return p.$('account-ai').hidden;
    };
    return [await hidden(() => Promise.resolve({ enabled: true, available: false })), await hidden(() => Promise.reject(new Error('x')))];
  },
  async clearSearches() {
    const run = async (yes, answer) => {
      const sent = [];
      const p = page({
        get: (url) => Promise.resolve(url === '/api/search/assist/history' ? { enabled: true, available: true } : { sessions: [] }),
        delete: (url) => { sent.push(url); return answer(); },
      }, { ai: true, yes });
      await settle();
      p.press('account-clear-searches');
      await settle();
      return { asked: p.asked.length, sent, left: Object.keys(p.session), toasts: p.toasts, says: p.$('account-clear-searches').textContent, busy: p.$('account-clear-searches').getAttribute('aria-disabled') };
    };
    return {
      no: await run(false, () => Promise.resolve({})),
      yes: await run(true, () => Promise.resolve({})),
      failed: await run(true, () => Promise.reject(new Error('No luck'))),
    };
  },
  // the hub's rows say what their views hold
  async hub() {
    const signIns = [session(9, { current: true }), session(5)];
    const p = page({ ...tvApi([tvOf(1, 'TV')], []), get: (url) => (url === '/api/users/sessions' ? Promise.resolve({ sessions: signIns }) : tvApi([tvOf(1, 'TV')], []).get(url)), patch: () => Promise.resolve({}) }, { tvs: 'viewer' });
    await settle();
    const out = { devices: p.$('account-row-devices').textContent };
    const save = async (value) => {
      p.$('account-email').value = value;
      p.fire('account-email-form', 'submit');
      await settle();
      return [p.$('account-row-email').textContent, p.$('account-head-email').textContent, p.$('account-head-email').hidden];
    };
    out.saved = await save('vic@example.com');
    out.removed = await save('');
    return out;
  },
  // Log out is asked first, and only a yes sends the form
  async logout() {
    const run = async (yes) => {
      const p = page({ get: () => Promise.resolve({ sessions: [] }) }, { yes });
      await settle();
      const form = p.$('account-logout');
      let sent = 0;
      form.submit = () => { sent += 1; };
      let stopped = false;
      form.listeners.submit.forEach((fn) => fn({ preventDefault() { stopped = true; } }));
      await settle();
      return { stopped, sent, asked: p.asked.map((a) => a.title) };
    };
    return { no: await run(false), yes: await run(true) };
  },
  // the script run again once its fragment is in, as the pane loads it, wires nothing twice
  async again() {
    let asked = 0;
    const p = page({ get: () => { asked += 1; return Promise.resolve({ sessions: [] }); } });
    await settle();
    p.run('account.js');
    await settle();
    return { asked, guards: p.you.guards.length, mounted: p.root.getAttribute('data-mounted') };
  },
  // every save and refusal of the pane scrolls within its view, never the window
  async noWindowScroll() {
    let list = [session(5), session(9)];
    let fail = false;
    const p = page({
      get: () => Promise.resolve({ sessions: list }),
      post: (url) => { if (fail) return Promise.reject(Object.assign(new Error('No'), { error: 'wrong_password' })); list = [session(9)]; return Promise.resolve({ ok: true }); },
      patch: () => (fail ? Promise.reject(new Error('No')) : Promise.resolve({})),
      delete: () => Promise.resolve({}),
    });
    await settle();
    let opened = 0;
    p.rows_().openInView = () => { opened += 1; };
    revealedNodes.splice(0);
    for (const refused of [false, true]) {
      fail = refused;
      p.$('account-email').value = refused ? 'nope' : 'vic@example.com';
      p.$('account-submit-email').focus();
      p.fire('account-email-form', 'submit');
      p.fill(OLD, NEW);
      p.$('account-submit-password').focus();
      p.fire('account-password-form', 'submit');
      await p.rest();
    }
    p.press('account-revoke-others');
    await p.rest();
    const outside = revealedNodes.filter(Boolean);
    return { window: page.window.scrolled, opened, revealed: revealedNodes.length, outside };
  },

};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
