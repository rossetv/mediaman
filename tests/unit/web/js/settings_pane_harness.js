// Runs the real Settings scripts as the pane loads them, after its fragment is in the page, against
// the fake DOM with the shell's API stubbed. Prints the outcome of one scenario as JSON.
// Usage: node settings_pane_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Node.prototype.click = function () { click(this.ownerDocument, this); };
Node.prototype.scrollIntoView = function () { this.ownerDocument.revealed.push(this.id); };

const link = (sec) => ['li', {}, ['a', { class: 'stx-link', href: '/?you=settings/' + sec, 'data-sec': sec }, sec]];

// The fragment as the server sends it, cut down to what these scenarios touch.
function fragment() {
  return ['div', { 'data-you-group': 'settings' },
    ['div', { class: 'you-view', 'data-view': 'settings', 'data-title': 'Settings' },
      ['div', { class: 'setg-pg stx', 'data-stx': '' },
        ['nav', { class: 'stx__nav' }, ['ul', { class: 'stx-list' }, link('overview'), link('general'), link('integrations')]],
        ['div', { class: 'stx__body' },
          ['section', { class: 'stx-sec', 'data-sec': 'overview', 'data-title': 'Overview' }, ['div', { class: 'setg-block', id: 'setg-overview' }]],
          ['section', { class: 'stx-sec', 'data-sec': 'general', 'data-title': 'General & Schedule' },
            ['div', { class: 'setg-block', id: 'setg-general' },
              ['input', { id: 'base_url', type: 'text' }],
              ['p', {}, ['a', { href: '#intg-openai' }, 'OpenAI key']]]],
          ['section', { class: 'stx-sec', 'data-sec': 'integrations', 'data-title': 'Integrations' },
            ['div', { class: 'setg-block', id: 'setg-integrations' },
              ['div', { class: 'intg-card is-collapsed', id: 'intg-openai' },
                ['div', { class: 'intg-card-hd', 'data-intg-toggle': '' },
                  ['h3', {}, ['button', { class: 'intg-fold', 'aria-expanded': 'false', 'aria-controls': 'intg-body-openai' }, 'OpenAI']]],
                ['div', { class: 'intg-card-bd', id: 'intg-body-openai' }, ['input', { id: 'openai_model', type: 'text' }]]]]]]],
      ['div', { class: 'stx-save', id: 'setg-savebar' },
        ['b', { class: 'sb-msg' }, 'Unsaved changes'], ['span', { id: 'save-status' }],
        ['button', { id: 'btn-discard' }], ['button', { id: 'btn-save' }]]]];
}

function pane(opts) {
  opts = opts || {};
  const doc = makeDocument();
  doc.readyState = 'complete';
  doc.revealed = [];
  const p = { doc, gone: [], guards: {}, said: [], shown: 'settings', wide: !!opts.wide, widths: [] };
  // Watch is the page under the pane: its search box is outside the fragment.
  doc.body.appendChild(build(doc, ['input', { id: 'search-input', type: 'search' }]));
  const location = { pathname: '/', search: '?you=' + (opts.at || 'settings'), hash: '', href: '' };
  location.href = location.pathname + location.search;
  const emit = (type, detail) => (doc.listeners[type] || []).forEach((fn) => fn({ type, detail }));
  const ctx = {
    document: doc, console, Promise, JSON, Object, Array, String, URLSearchParams,
    location,
    setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {},
    requestAnimationFrame: (fn) => { fn(); return 0; },
    matchMedia: (q) => ({
      get matches() { return q.includes('min-width') ? p.wide : false; },
      addEventListener: (type, fn) => { if (q.includes('min-width')) p.widths.push(fn); },
    }),
    addEventListener: () => {},
    fetch: () => Promise.resolve({ status: 401, ok: false, statusText: 'Unauthorized', json: () => Promise.resolve({}) }),
    UIFeedback: { error: (m) => p.said.push(m), toast: () => {}, confirm: () => Promise.resolve(true) },
    MM: {
      you: {
        shown: (group) => p.shown === group,
        scroller: (view) => view,
        go: (to) => {
          p.gone.push(to);
          location.search = '?you=' + to;
          emit('mm:you-shown', { group: to.split('/')[0], view: to });
        },
        close: () => {},
        guard: (group, o) => { p.guards[group] = o; },
        insert: () => {},
      },
      reauth: { run: (f) => f() },
      dom: { fold: () => {} },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  load('core/api.js');
  doc.body.appendChild(build(doc, fragment()));
  ['settings/savebar.js', 'settings/toggles.js', 'settings/general.js', 'settings/sections.js'].forEach(load);
  ctx.MM.settings.rows = { open: (b) => b.classList.add('is-open'), close: (b) => b.classList.remove('is-open') };
  load('settings.js');
  emit('mm:you-shown', { group: 'settings', view: opts.at || 'settings' });
  const $ = (id) => doc.getElementById(id);
  const type = (el, value) => {
    el.value = value;
    (doc.listeners.input || []).forEach((fn) => fn({ type: 'input', target: el }));
  };
  const on = () => doc.querySelector('.stx-sec.is-on');
  return Object.assign(p, { ctx, $, type, on, location, emit });
}

const leaving = (guard) => ({
  toSection: guard.dirty('settings/general', 'settings/integrations'),
  toHistory: guard.dirty('settings/integrations', 'history'),
  closed: guard.dirty('settings/integrations', null),
});

const scenarios = {
  // An edit in one section, then a step to another through the list.
  async acrossSections() {
    const p = pane({ at: 'settings/general' });
    p.type(p.$('base_url'), 'http://new');
    click(p.doc, p.doc.querySelector('.stx-link[data-sec="integrations"]'));
    await settle();
    return {
      gone: p.gone,
      on: p.on() && p.on().getAttribute('data-sec'),
      unsaved: p.ctx.MM.api.hasUnsavedChanges(),
      status: p.$('save-status').textContent,
      leaving: leaving(p.guards.settings),
    };
  },
  // Typing in Watch's search, with the Settings fragment loaded and its pane shut.
  async watchSearch() {
    const p = pane();
    p.shown = null;
    p.type(p.$('search-input'), 'alien');
    return {
      unsaved: p.ctx.MM.api.hasUnsavedChanges(),
      bar: p.$('setg-savebar').classList.contains('on'),
      leaving: leaving(p.guards.settings),
    };
  },
  // The pane opened at an integration's own address.
  async openaiAddress() {
    const p = pane({ at: 'settings/integrations/openai' });
    const card = p.$('intg-openai');
    return {
      on: p.on() && p.on().getAttribute('data-sec'),
      open: !card.classList.contains('is-collapsed'),
      focused: p.doc.activeElement === card.querySelector('.intg-fold'),
      revealed: p.doc.revealed,
    };
  },
  // The old in-page link to the OpenAI card, pressed in General.
  async openaiLink() {
    const p = pane({ at: 'settings/general' });
    let prevented = false;
    click(p.doc, p.doc.querySelector('a[href="#intg-openai"]'), {
      preventDefault() { this.defaultPrevented = true; prevented = true; },
    });
    await settle();
    const card = p.$('intg-openai');
    return {
      prevented,
      revealed: p.doc.revealed,
      gone: p.gone,
      on: p.on() && p.on().getAttribute('data-sec'),
      open: !card.classList.contains('is-collapsed'),
      focused: p.doc.activeElement === card.querySelector('.intg-fold'),
    };
  },
  // Up from a section to the list: on a narrow screen the list comes back with the row that led
  // there focused; on a wide one the list never went, and Overview shows beside it.
  async upToList() {
    const out = {};
    for (const wide of [false, true]) {
      const p = pane({ at: 'settings/integrations', wide });
      const inside = p.doc.querySelector('.setg-pg').classList.contains('is-in');
      p.emit('mm:you-shown', { group: 'settings', view: 'settings' });
      const a = p.doc.activeElement;
      out[wide ? 'wide' : 'narrow'] = {
        inside,
        after: p.doc.querySelector('.setg-pg').classList.contains('is-in'),
        on: p.on() && p.on().getAttribute('data-sec'),
        focused: a && a.getAttribute ? a.getAttribute('data-sec') : null,
      };
    }
    return out;
  },
  // A section open on a narrow screen, then the window widened past the width the list sits beside.
  async widened() {
    const p = pane({ at: 'settings/integrations' });
    const pg = p.doc.querySelector('.setg-pg');
    const nav = p.doc.querySelector('.stx__nav');
    const state = () => ({
      inside: pg.classList.contains('is-in'),
      listInert: !!nav.inert,
      on: p.on() && p.on().getAttribute('data-sec'),
      current: (p.doc.querySelector('.stx-link[aria-current]') || { getAttribute: () => null }).getAttribute('data-sec'),
    });
    const narrow = state();
    p.wide = true;
    p.widths.forEach((fn) => fn({ matches: true }));
    return { narrow, wide: state(), gone: p.gone };
  },
  // An edit, then the pane's own Discard, asked by its guard on the way out.
  async barMarked() {
    const p = pane({ at: 'settings/general' });
    const bar = p.$('setg-savebar');
    const marks = () => ({ bar: bar.classList.contains('on'), page: p.doc.documentElement.className || '' });
    const before = marks();
    p.type(p.$('base_url'), 'http://new');
    const dirty = marks();
    p.guards.settings.discard();
    await settle();
    return { before, dirty, discarded: marks() };
  },
  // A request answered 401, with and without edits waiting.
  async expired() {
    const out = {};
    for (const edited of [true, false]) {
      const p = pane({ at: 'settings/general' });
      if (edited) p.type(p.$('base_url'), 'http://new');
      p.ctx.MM.api.get('/api/settings').catch(() => {});
      await settle();
      out[edited ? 'edited' : 'clean'] = { href: p.location.href, said: p.said };
    }
    return out;
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
