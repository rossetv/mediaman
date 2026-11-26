// Runs Sync now against the fake DOM and prints how long each toast it raises is asked to stay.
// Usage: node settings_sync_toast_harness.js <json reply>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

// The fake DOM has no removeChild.
Node.prototype.removeChild = function (c) { c.remove(); return c; };

const SRC = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/settings/integrations.js');

(async () => {
  const doc = makeDocument();
  const toasts = [];
  const raise = (kind) => (message, opts) => toasts.push({ kind, message, duration: (opts || {}).duration || null });
  const ctx = {
    document: doc, console, Promise,
    setTimeout: () => 0, clearTimeout: () => {}, addEventListener: () => {},
    matchMedia: () => ({ matches: false }),
    UIFeedback: { toast: raise('toast'), success: raise('success'), error: raise('error') },
    MM: {
      api: { post: () => Promise.resolve(JSON.parse(process.argv[2])), get: () => Promise.resolve({}), messageOf: (e, w) => w },
      dom: { focusStillIn: () => false },
      settings: { rows: {}, savebar: { isEdited: () => false } },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(SRC, 'utf8'), ctx);
  const btn = doc.body.appendChild(build(doc, ['button', { id: 'btn-sync-library' }, 'Sync now']));
  ctx.MM.settings.integrations.init();
  click(doc, btn);
  await settle();
  console.log(JSON.stringify(toasts));
})();
