'use strict';
/* Runs one read or write through core/api.js against a given answer; prints how it settled
   and where the browser went. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const file = path.join(__dirname, '../../../../src/mediaman/web/static/js/core/api.js');
const c = JSON.parse(process.argv[2]);
const origin = 'https://mm.example';
const location = Object.assign({ origin, search: '', hash: '' }, c.location);
location.href = origin + location.pathname + location.search + location.hash;
const start = location.href;
const toasts = [];
const answer = Object.assign({ status: 200, redirected: false, url: origin + '/api/x', body: {} }, c.answer);
const window = {
  location,
  URL,
  UIFeedback: { error: (t) => toasts.push(t) },
  fetch: () => Promise.resolve({
    status: answer.status,
    ok: answer.status >= 200 && answer.status < 300,
    redirected: answer.redirected,
    url: answer.url,
    statusText: '',
    json: () => (answer.body === null ? Promise.reject(new SyntaxError('html')) : Promise.resolve(answer.body)),
  }),
};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), Object.assign(window, { window, console }));
if (c.unsaved) window.MM.api.hasUnsavedChanges = () => true;
const out = { settled: 'pending', toasts };
const call = c.method === 'post' ? window.MM.api.post('/api/x', {}, c.options) : window.MM.api.get('/api/x', c.options);
call.then(
  (data) => { out.settled = 'resolved'; out.data = data; },
  (err) => {
    out.settled = 'rejected';
    out.apiError = err instanceof window.MM.api.APIError;
    out.status = err.status;
    out.redirect = err.data && err.data.redirect;
  },
);
setImmediate(() => setImmediate(() => {
  out.href = location.href === start ? null : location.href;
  process.stdout.write(JSON.stringify(out));
}));
