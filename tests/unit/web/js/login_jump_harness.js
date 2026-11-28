'use strict';
/* Runs the Watch rows against a refused call on the page at the given location; prints where
   the browser goes. Usage: node login_jump_harness.js <json> [unsaved] */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');
const location = JSON.parse(process.argv[2]);
const FILES = ['core/dom.js', 'watch/core.js', 'watch/address.js', 'watch/keep.js', 'watch/search.js', 'watch/rows-load.js', 'watch/grid.js', 'watch/expand.js', 'watch/browse.js'];

const doc = makeDocument();
doc.hidden = false;
doc.body.appendChild(build(doc, ['div', { id: 'watch-page' }, ['input', { id: 'watch-search' }],
  ['div', { id: 'watch-browse' }, ['div', { id: 'watch-discover-slot' }, ['div', { class: 'watch-discover-skel' }]]]]));
doc.body.appendChild(build(doc, ['script', { id: 'watch-page-data' }, '{"tmdb_configured":true}']));
const timers = [];
const refuse = () => Promise.reject(Object.assign(new Error(), { status: 401 }));
const ctx = {
  document: doc, location, console, AbortController, URLSearchParams,
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {}, addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: true }),
  history: { state: null, replaceState() {} },
  URL,
};
ctx.window = ctx;
vm.createContext(ctx);
// The real api.js, for the sign-in address it builds; its calls are refused.
vm.runInContext(fs.readFileSync(path.join(JS, 'core/api.js'), 'utf8'), ctx);
Object.assign(ctx.MM.api, { get: refuse, post: refuse, messageOf: (_e, m) => m, hasUnsavedChanges: () => process.argv[3] === 'unsaved' });
FILES.forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
settle().then(() => process.stdout.write(JSON.stringify({ href: location.href })));
