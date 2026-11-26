// Runs Settings' integration-card scripts against the fake DOM and prints the outcome of one
// scenario as JSON. Usage: node settings_cards_harness.js <scenario> [json argument]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

// The fake DOM has no removeChild.
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

};

scenarios[process.argv[2]](process.argv[3] && JSON.parse(process.argv[3])).then((out) => console.log(JSON.stringify(out)));
