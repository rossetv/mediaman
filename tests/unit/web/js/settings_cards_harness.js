// Runs Settings' integration-card scripts against the fake DOM and prints the outcome of one
// scenario as JSON. Usage: node settings_cards_harness.js <scenario> [json argument]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

// The fake DOM's selectors read no digit in a tag name, and it has no removeChild.
const queryAll = Node.prototype.querySelectorAll;
Node.prototype.querySelectorAll = function (sel) {
  return sel === 'h3' ? this.descendants().filter((e) => e.tagName === 'H3') : queryAll.call(this, sel);
};
Node.prototype.removeChild = function (c) { c.remove(); return c; };

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/settings');

function load(files, extra) {
  const doc = makeDocument();
  const p = { doc, said: [], timers: [], folds: [] };
  const say = (kind) => (m) => p.said.push([kind, m]);
  const ctx = {
    document: doc, console, Promise,
    setTimeout: (fn) => { p.timers.push(fn); return fn; },
    clearTimeout: () => {},
    matchMedia: () => ({ matches: false }),
    addEventListener: () => {},
    innerHeight: 800, scrollY: 0,
    UIFeedback: { toast: say('toast'), success: say('success'), error: say('error') },
    MM: {
      api: { post: (u) => (p.post ? p.post(u) : Promise.resolve({})), get: () => Promise.resolve({}), messageOf: (e, w) => w },
      dom: { focusStillIn: () => false },
      settings: {
        rows: { open: (b) => p.folds.push(['open', b.id]), close: (b) => p.folds.push(['close', b.id]) },
        savebar: { isEdited: (el) => !!(extra && extra.edited && extra.edited.includes(el.id)) },
      },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  files.forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  p.ctx = ctx;
  return p;
}

// One card as the macro draws it: the title is the fold's button, Test sits beside it.
function card(doc, parent, service, title) {
  return parent.appendChild(build(doc, ['div', { class: 'intg-card is-collapsed', id: 'intg-' + service },
    ['div', { class: 'intg-card-hd', 'data-intg-toggle': '' },
      ['div', { class: 'glyph', 'aria-hidden': 'true' }, title[0]],
      ['div', { class: 'hd-main' },
        ['div', { class: 'h-info' },
          ['h3', {}, ['button', { class: 'intg-fold', 'aria-expanded': 'false', 'aria-controls': 'intg-body-' + service }, title]],
          ['div', { class: 'sub' }, 'What it does']],
        ['div', { class: 'hd-actions' },
          ['span', { class: 'conn', 'data-conn': service }, ['span', { 'data-conn-label': '' }, 'Untested']],
          ['div', { class: 'hd-btns' },
            ['button', { class: 'btn-test', 'data-test-service': service, 'aria-label': 'Test ' + title }, 'Test'],
            ['svg', { class: 'intg-chev' }]]]]],
    ['div', { class: 'intg-card-bd', id: 'intg-body-' + service },
      ['input', { id: service + '_api_key', 'aria-label': title + ' API Key' }],
      ['button', { class: 'inp-reveal', 'data-reveal-target': service + '_api_key', 'aria-label': 'Show ' + title + ' API Key' }, 'Show']]]));
}

const scenarios = {
  // What Sync now says for the reply the server gives.
  async sync(reply) {
    const p = load(['integrations.js']);
    const btn = p.doc.body.appendChild(build(p.doc, ['button', { id: 'btn-sync-library' }, 'Sync now']));
    p.post = (u) => (u === '/api/library/sync' ? Promise.resolve(reply) : Promise.resolve({}));
    p.ctx.MM.settings.integrations.init();
    click(p.doc, btn);
    await settle();
    const shown = btn.textContent;
    p.timers.splice(0).forEach((fn) => fn());
    return { shown, said: p.said, after: btn.textContent };
  },

  // The Test buttons' names, as drawn when a card holds no edits and when it does.
  async testNames() {
    const p = load(['integrations.js'], { edited: ['radarr_api_key'] });
    const page = p.doc.body.appendChild(build(p.doc, ['div', { class: 'setg-pg' }]));
    card(p.doc, page, 'sonarr', 'Sonarr');
    card(p.doc, page, 'radarr', 'Radarr');
    p.ctx.MM.settings.integrations.paintTests();
    const look = () => p.doc.querySelectorAll('[data-test-service]').map((b) => [b.textContent, b.getAttribute('aria-label')]);
    const painted = look();
    p.post = () => new Promise(() => {});
    p.ctx.MM.settings.integrations.init();
    click(p.doc, p.doc.querySelector('[data-test-service="sonarr"]'));
    return { painted, testing: look()[0] };
  },

  // A reveal button is named for its field, with the word it shows.
  async reveal() {
    const p = load(['toggles.js']);
    const page = p.doc.body.appendChild(build(p.doc, ['div', { class: 'setg-pg' }]));
    card(p.doc, page, 'plex', 'Plex');
    p.doc.getElementById('plex_api_key').type = 'password';
    p.ctx.MM.settings.toggles.init();
    const btn = p.doc.querySelector('.inp-reveal');
    const out = [];
    for (let i = 0; i < 2; i++) {
      click(p.doc, btn);
      out.push([btn.textContent, btn.getAttribute('aria-label'), p.doc.getElementById('plex_api_key').type]);
    }
    return out;
  },

  // The title's button and the rest of the header fold the card; Test and the answer do not.
  async fold() {
    const p = load(['toggles.js']);
    const page = p.doc.body.appendChild(build(p.doc, ['div', { class: 'setg-pg' }]));
    const c = card(p.doc, page, 'sonarr', 'Sonarr');
    p.ctx.MM.settings.toggles.init();
    const fold = c.querySelector('.intg-fold');
    const state = () => [!c.classList.contains('is-collapsed'), fold.getAttribute('aria-expanded')];
    const out = {};
    click(p.doc, fold); out.button = state();
    click(p.doc, c.querySelector('.sub')); out.header = state();
    click(p.doc, c.querySelector('.btn-test')); out.test = state();
    click(p.doc, c.querySelector('[data-conn-label]')); out.answer = state();
    click(p.doc, c.querySelector('.intg-chev')); out.chevron = state();
    out.folds = p.folds;
    return out;
  },
};

scenarios[process.argv[2]](process.argv[3] && JSON.parse(process.argv[3])).then((out) => console.log(JSON.stringify(out)));
