// Runs the settings page's library, overview and newsletter scripts against the
// small fake DOM and prints the outcome of one scenario as JSON.
// Usage: node settings_sections_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Object.defineProperty(Node.prototype, 'dataset', {
  get() {
    const attr = (k) => 'data-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
    return new Proxy({}, {
      get: (_, k) => { const v = this.getAttribute(attr(k)); return v === null ? undefined : v; },
      set: (_, k, v) => { this.setAttribute(attr(k), v); return true; },
    });
  },
});
Object.defineProperty(Node.prototype, 'type', {
  get() { return this.getAttribute('type') || ''; },
  set(v) { this.setAttribute('type', v); },
});
Node.prototype.replaceChildren = function (...kids) { this.textContent = ''; kids.forEach((k) => this.appendChild(k)); };

function page(markup, files) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { class: 'setg-pg' }, ...markup]));
  const ctx = {
    document: doc, console, Promise, setTimeout: () => 0, clearTimeout: () => {},
    addEventListener: () => {}, matchMedia: () => ({ matches: true }), getComputedStyle: () => ({}),
    MM: { dom: { focusStillIn: (el) => el.contains(doc.activeElement), inView: () => true } },
    UIFeedback: { confirm: () => Promise.resolve(true), success() {}, error() {}, toast() {} },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  files.forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, 'settings', f + '.js'), 'utf8'), ctx));
  const id = (x) => doc.getElementById(x);
  const fire = (el, type, more) => (el.listeners[type] || []).forEach((f) => f(Object.assign({ target: el, preventDefault() {} }, more)));
  return { doc, ctx, id, fire, click: (el) => click(doc, el) };
}

const refused = (error, message, status) => Promise.reject({ error, message, status });

// The libraries section. `usage` answers a disk usage read by its path.
async function libraries(opts, usage, answer) {
  const p = page([
    ['div', { id: 'plex-library-pills' }, 'served hint'],
    ['span', { id: 'lib-pill-count' }],
    ['div', { id: 'disk-threshold-rows' }],
  ], ['disk_thresholds']);
  const dirty = [];
  p.ctx.MM.settings.savebar = { markDirty: () => dirty.push(1) };
  p.ctx.MM.api = {
    get: (u) => (u === '/api/plex/libraries'
      ? (answer ? answer() : Promise.resolve({ libraries: [{ id: 1, title: 'Films' }, { id: 2, title: 'Shows' }] }))
      : (usage || (() => Promise.resolve({ usage_pct: 90 })))(decodeURIComponent(u.split('path=')[1]))),
    messageOf: (e, words) => (e && e.message) || words,
  };
  const disk = p.ctx.MM.settings.diskThresholds;
  disk.init(Object.assign({ selectedLibs: ['1'], diskThresholds: {} }, opts));
  await settle();
  const card = (n) => p.doc.querySelector('.lp-card[data-lib-id="' + n + '"]');
  const input = (n, f) => card(n).querySelector('[data-field="' + f + '"]');
  return Object.assign(p, {
    disk, dirty, card, input,
    pill: (n) => p.doc.querySelector('.lib-pill[data-lib-id="' + n + '"]'),
    pills: () => p.id('plex-library-pills').textContent,
    rows: () => p.id('disk-threshold-rows').textContent,
    says: (n) => card(n).querySelector('[data-role="state"]').textContent,
    type: async (n, folder, percent) => {
      input(n, 'path').value = folder; p.fire(input(n, 'path'), 'input'); p.fire(input(n, 'path'), 'blur');
      input(n, 'threshold').value = percent; p.fire(input(n, 'threshold'), 'input'); p.fire(input(n, 'threshold'), 'change');
      await settle();
    },
  });
}

const scenarios = {
  async pills() {
    const p = await libraries({});
    const look = (n) => [p.pill(n).tagName, p.pill(n).type, p.pill(n).getAttribute('aria-pressed')];
    const out = { before: [look(1), look(2)] };
    p.click(p.pill(2));
    p.click(p.pill(1));
    out.after = [look(1), look(2)];
    p.disk.restore({ libs: ['1'], disk: {} });
    out.restored = [look(1), look(2)];
    p.disk.show({ libs: ['2'], disk: {} });
    out.shown = [look(1), look(2)];
    return out;
  },

  // What the card of library 1 says for each stored entry, or undefined for no entry.
  async stored(entries) {
    const out = [];
    for (const entry of JSON.parse(entries)) {
      const p = await libraries({ diskThresholds: entry === null ? {} : { 1: entry } });
      out.push(p.says(1));
    }
    return out;
  },

  // What the cards say for each whole stored setting.
  async wholes(settings) {
    const out = [];
    for (const diskThresholds of JSON.parse(settings)) out.push((await libraries({ diskThresholds })).says(1));
    return out;
  },

  async typing() {
    const p = await libraries({ diskThresholds: { 1: { path: '/a', threshold: 80 } } });
    const out = { limited: p.says(1) };
    await p.type(1, '', 80);
    out.noPath = p.says(1);
    await p.type(1, '/a', 95);
    out.below = p.says(1);
    await p.type(1, '/a', 0);
    out.noLimit = p.says(1);
    return out;
  },

  async mended() {
    const p = await libraries({ selectedLibs: ['1', '2'], diskThresholds: 'odd' });
    const out = { before: [p.says(1), p.says(2)] };
    await p.type(1, '/a', 80);
    await p.type(2, '/b', 0);
    out.typed = [p.says(1), p.says(2)];
    const stored = { 1: { path: '/a', threshold: 80 }, 2: { path: '/b', threshold: 0 } };
    p.disk.show(p.disk.fromServer({ plex_libraries: ['1', '2'], disk_thresholds: stored }));
    await settle();
    out.saved = [p.says(1), p.says(2)];
    return out;
  },

  // A read that comes back after the folder was changed says nothing about the new one.
  async lateRead() {
    const reads = {};
    const p = await libraries({ diskThresholds: { 1: { path: '/slow', threshold: 80 } } },
      (folder) => new Promise((done) => { reads[folder] = done; }));
    await p.type(1, '/quick', 80);
    reads['/quick']({ usage_pct: 10 });
    await settle();
    reads['/slow']({ usage_pct: 99 });
    await settle();
    return p.says(1);
  },

  // What a card says for each answer to the read of its folder.
  async unreadDisk() {
    const says = async (error, message, status) => (await libraries({ diskThresholds: { 1: { path: '/a', threshold: 80 } } },
      () => refused(error, message, status))).says(1);
    return {
      missing: await says('not_found', 'Folder not found.', 404),
      hidden: await says('not_allowed', 'This page may not read this folder.', 403),
      slow: await says('fetch_failed', 'Failed to read disk usage.', 400),
      lost: await says('network_error', 'No connection.', 0),
    };
  },

  // Typing the shown values again over an entry the scanner cannot read.
  async retyped(entry, folder, percent) {
    const p = await libraries({ diskThresholds: { 1: JSON.parse(entry) } });
    const saved = p.disk.state();
    const look = () => {
      const now = p.disk.state();
      return {
        says: p.says(1), changes: p.disk.changes(saved.disk, now.disk),
        sameThing: JSON.stringify(p.disk.things(saved)) === JSON.stringify(p.disk.things(now)),
      };
    };
    const out = { untouched: look() };
    await p.type(1, folder, percent);
    out.typedSays = p.says(1);
    out.typed = look();
    p.disk.show(saved);
    await settle();
    out.shownAgain = look();
    await p.type(1, folder, percent);
    p.disk.state();
    p.disk.restore(saved);
    await settle();
    out.discarded = look();
    return out;
  },

  // What each run of edits leaves: [field, value, how, text put in], as the browser tells them; 'redraw'
  // for the rows being drawn again, 'elsewhere' for a save of something else, 'mendedElsewhere' for one
  // that finds a limit another page has stored.
  async edits(entry, runs) {
    const out = [];
    for (const run of JSON.parse(runs)) {
      const p = await libraries({ diskThresholds: { 1: JSON.parse(entry) } });
      let base = p.disk.state();
      for (const step of run) {
        if (step === 'redraw') { p.click(p.pill(1)); p.click(p.pill(1)); continue; }
        if (step === 'elsewhere' || step === 'mendedElsewhere') {
          const was = p.disk.things(base);
          const mine = p.disk.state();
          const now = p.disk.things(mine);
          base = p.disk.fromServer({ plex_libraries: ['1'], disk_thresholds: { 1: step === 'elsewhere' ? JSON.parse(entry) : { path: '/else', threshold: 70 } } });
          p.disk.show(p.disk.rebase(mine, base, (key) => now[key] !== was[key]));
          continue;
        }
        const [field, value, inputType, data, badInput] = step;
        const el = p.input(1, field);
        el.value = value;
        el.validity = { badInput: !!badInput };
        p.fire(el, 'input', { inputType, data });
        p.fire(el, field === 'path' ? 'blur' : 'change');
      }
      await settle();
      const now = p.disk.state();
      out.push({
        changes: p.disk.changes(base.disk, now.disk), says: p.says(1),
        sameThing: JSON.stringify(p.disk.things(base)) === JSON.stringify(p.disk.things(now)),
        shows: ['path', 'threshold'].map((f) => String(p.input(1, f).value)),
      });
    }
    return out;
  },

  async plexAnswers() {
    const look = async (answer) => { const p = await libraries({}, null, answer); return [p.pills(), p.rows()]; };
    return {
      notSetUp: await look(() => refused('plex_not_configured', 'x', 200)),
      failed: await look(() => refused('fetch_failed', 'x', 200)),
      none: await look(() => Promise.resolve({ libraries: [] })),
    };
  },

  async overview() {
    const p = page([
      ['div', { id: 'ov-scan-big' }], ['div', { id: 'ov-scan-when' }],
      ['div', { id: 'ov-storage-big' }], ['div', { id: 'ov-storage-bar' }], ['div', { id: 'ov-storage-chips' }],
      ['select', { id: 'scan_day' }], ['input', { id: 'scan_time' }], ['select', { id: 'scan_timezone' }],
      ['div', { id: 'plex-library-pills' }], ['div', { id: 'disk-threshold-rows' }],
      ['script', { id: 'setg-bootstrap' }, JSON.stringify({
        scan_day: 'mon,thu', scan_time: '09:00', scan_timezone: 'Asia/Kolkata', disk_thresholds: {}, selected_libraries: ['1'],
      })],
    ], ['disk_thresholds', 'overview', 'general']);
    const edited = [];
    let savebar;
    p.ctx.MM.settings.savebar = { init: (o) => { savebar = o; }, isEdited: (el) => edited.includes(el.id), markDirty() {} };
    let used = '1 TB';
    p.ctx.MM.api = {
      get: (u) => (u === '/api/dashboard/stats'
        ? Promise.resolve({ storage: { used, total: '4 TB', free: '3 TB', movies_pct: 25 } })
        : Promise.resolve({ libraries: [] })),
      messageOf: (e, w) => w,
    };
    const set = (day, time, zone) => { p.id('scan_day').value = day; p.id('scan_time').value = time; p.id('scan_timezone').value = zone; };
    const scan = () => p.id('ov-scan-big').textContent + ' ' + p.id('ov-scan-when').textContent;
    const storage = () => [p.id('ov-storage-big').textContent.replace(/ /g, ' '), p.id('ov-storage-bar').children.length];
    const save = async (values, thresholds) => {
      savebar.getPayload({ values, disk: { libs: [], disk: {} } }, { disk: { disk: {} } });
      p.ctx.MM.settings.diskThresholds.fromServer({ plex_libraries: ['1'], disk_thresholds: thresholds });
      savebar.refreshOverviewHero();
      await settle();
      return { scan: scan(), storage: storage() };
    };
    // what a browser put back over the served values
    set('fri', '01:00', 'UTC');
    p.ctx.MM.settings.general.init();
    await settle();
    const out = { loaded: { scan: scan(), storage: storage() } };
    set('tue', '10:00', 'Europe/London');
    out.saved = await save({ scan_day: 'tue', scan_time: '10:00', scan_timezone: 'Europe/London' }, { 1: { path: '/a', threshold: 80 } });
    used = '2 TB';
    out.other = await save({ scan_day: 'tue', scan_time: '10:00', scan_timezone: 'Europe/London' }, { 1: { path: '/b', threshold: 80 } });
    // changed again while its save is under way
    set('wed', '10:00', 'Europe/London');
    edited.push('scan_day');
    out.during = await save({ scan_day: 'sat', scan_time: '10:00', scan_timezone: 'Europe/London' }, {});
    return out;
  },

  async recipients() {
    const p = await newsletter();
    const { doc, boxes, ticks, add, remove, list, person } = p;
    const out = {};
    out.opened = ticks();
    await add(person(3));
    out.added = ticks();
    boxes()[1].checked = false;
    const held = boxes()[1];
    await add(person(4), () => held.focus());
    out.addedAfterAChoice = ticks();
    out.focusKept = doc.activeElement === held && doc.documentElement.contains(held);
    await remove(1);
    out.removed = ticks();
    await add(person(5, false));
    out.optedOut = ticks().length;
    await remove(2); await remove(3); await remove(4);
    out.none = list().textContent;
    await add(person(6));
    out.again = [ticks(), list().textContent.includes('No active')];
    return out;
  },

  // A list that cannot be read again after an add or a removal.
  async staleRecipients() {
    const p = await newsletter();
    const { boxes, ticks, add, remove, note, person, subscribers } = p;
    boxes()[1].checked = false;
    const out = {};
    p.fail(Infinity);
    await add(person(3));
    out.failed = { ticks: ticks(), note: note(), subscribers: subscribers(), errors: p.errors.slice() };
    p.fail(0);
    await add(person(4));
    out.recovered = { ticks: ticks(), note: note(), subscribers: subscribers() };
    p.fail(Infinity);
    await remove(4);
    out.failedAgain = { ticks: ticks().length, note: note() };
    p.fail(0);
    p.click(p.id('btn-send-newsletter'));
    p.click(p.id('btn-send-newsletter'));
    await settle();
    out.reopened = { ticks: ticks().length, note: note() };
    return out;
  },

  // What the dialog says of removing someone who gets the newsletter, and of one who opted out.
  async removalWords() {
    const p = await newsletter([1]);
    await p.add(p.person(2, false));
    const said = [];
    p.ctx.UIFeedback.confirm = (o) => { said.push(o.body); return Promise.resolve(false); };
    await p.remove(1);
    await p.remove(2);
    return said;
  },

  // Where the focus goes when the recipient that has it is taken off the list.
  async recipientFocus() {
    const p = await newsletter([1, 2, 3]);
    const still = [];
    const focused = async (n) => {
      p.id('recipient-' + n).focus();
      const ways = [];
      const focus = Node.prototype.focus;
      Node.prototype.focus = function (how) { ways.push(!!(how && how.preventScroll)); return focus.call(this, how); };
      await p.remove(n, true);
      Node.prototype.focus = focus;
      still.push(ways[ways.length - 1]);
      const active = p.doc.activeElement;
      return p.doc.documentElement.contains(active) ? active.id : 'nowhere';
    };
    const out = [await focused(2), await focused(3), await focused(1)];
    const q = await newsletter([1]);
    q.list().querySelector('button').focus();
    await q.remove(1, true);
    out.push(q.doc.documentElement.contains(q.doc.activeElement) ? q.doc.activeElement.id : 'nowhere');
    return { to: out, still };
  },

};

// The newsletter section with its recipients open.
async function newsletter(people) {
    const p = page([
      ['div', { id: 'subscriber-list' }],
      ['input', { id: 'new-subscriber-email' }], ['button', { id: 'btn-add-subscriber' }, 'Add'],
      ['button', { id: 'btn-send-newsletter' }],
      ['div', { id: 'newsletter-send-panel', class: 'setg-fold' },
        ['div', { id: 'newsletter-recipient-list' }], ['div', { id: 'newsletter-recipient-note' }],
        ['button', { id: 'btn-confirm-newsletter' }, 'Send newsletter'],
        ['span', { id: 'newsletter-send-status' }]],
    ], ['rows', 'newsletter']);
    let failing = 0;
    let subs = (people || [1, 2]).map((n) => ({ id: n, email: 'p' + n + '@example.com', active: true }));
    const errors = [];
    p.ctx.UIFeedback.error = (text) => errors.push(text);
    const person = (n, active) => ({ id: n, email: 'p' + n + '@example.com', active: active !== false });
    p.ctx.MM.api = {
      get: () => (failing-- > 0 ? refused('http_500', 'x', 500) : Promise.resolve({ subscribers: subs.slice() })),
      postForm: () => Promise.resolve({}),
      delete: (u) => { subs = subs.filter((s) => '/api/subscribers/' + s.id !== u); return Promise.resolve({}); },
      messageOf: (e, w) => w,
    };
    Node.prototype.scrollIntoView = () => {};
    const panel = p.id('newsletter-send-panel');
    panel.hidden = true;
    p.ctx.MM.settings.newsletter.init();
    await settle();
    const boxes = () => p.id('newsletter-recipient-list').querySelectorAll('input');
    const ticks = () => boxes().map((b) => [b.value, b.checked]);
    const add = async (who, meanwhile) => {
      subs = subs.concat(who);
      p.id('new-subscriber-email').value = who.email;
      p.click(p.id('btn-add-subscriber'));
      if (meanwhile) meanwhile();
      await settle();
    };
    // `keepFocus`: the person has moved on to the recipients by the time the removal is done.
    const remove = async (n, keepFocus) => {
      const active = p.doc.activeElement;
      p.click(p.doc.querySelector('.sub-row[data-id="' + n + '"]').querySelector('.link-danger'));
      if (keepFocus) active.focus();
      await settle();
    };
    p.click(p.id('btn-send-newsletter'));
    await settle();
    return Object.assign(p, {
      boxes, ticks, add, remove, person, errors,
      list: () => p.id('newsletter-recipient-list'),
      note: () => p.id('newsletter-recipient-note').textContent,
      subscribers: () => p.doc.querySelectorAll('.sub-row').map((r) => r.querySelector('.em').textContent),
      fail: (n) => { failing = n; },
    });
}

scenarios[process.argv[2]](...process.argv.slice(3)).then(
  (out) => process.stdout.write(JSON.stringify(out)),
  (err) => { console.error(err); process.exit(1); }
);
