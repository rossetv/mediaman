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
const asked = [];
const answer = Object.assign({ status: 200, redirected: false, url: origin + '/api/x', body: {} }, c.answer);
// Window listeners run as a browser runs them at the window: capture first, each in the order added.
const listeners = [];
const timers = [];
function dispatch(type, init) {
  const e = Object.assign({ type, defaultPrevented: false, returnValue: undefined }, init);
  let stopped = false;
  e.stopImmediatePropagation = () => { stopped = true; };
  e.preventDefault = () => { e.defaultPrevented = true; };
  const order = listeners.filter((l) => l.type === type && l.capture)
    .concat(listeners.filter((l) => l.type === type && !l.capture));
  for (const l of order) {
    if (stopped) break;
    if (listeners.includes(l)) l.fn(e);
  }
  return e;
}
const window = {
  location,
  URL,
  setTimeout: (fn) => timers.push(fn),
  clearTimeout: (id) => { timers[id - 1] = null; },
  addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture: !!capture }),
  removeEventListener: (type, fn, capture) => {
    const i = listeners.findIndex((l) => l.type === type && l.fn === fn && l.capture === !!capture);
    if (i >= 0) listeners.splice(i, 1);
  },
  UIFeedback: Object.assign(
    { error: (t) => toasts.push(t) },
    'confirm' in c ? { confirm: (o) => { asked.push(o); return Promise.resolve(c.confirm); } } : {},
  ),
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
// The page's own guard, as Watch's pane adds one while edits are held.
window.addEventListener('beforeunload', (e) => { e.preventDefault(); e.returnValue = ''; });
const out = { settled: 'pending', toasts, asked, all: [] };
const call = (method) => (method === 'post' ? window.MM.api.post('/api/x', {}, c.options) : window.MM.api.get('/api/x', c.options));
for (let i = 0; i < (c.reads || 1); i++) {
  out.all.push('pending');
  call(c.method).then(
    (data) => { out.all[i] = 'resolved'; if (!i) { out.settled = 'resolved'; out.data = data; } },
    (err) => {
      out.all[i] = 'rejected';
      if (i) return;
      out.settled = 'rejected';
      out.apiError = err instanceof window.MM.api.APIError;
      out.status = err.status;
      out.redirect = err.data && err.data.redirect;
    },
  );
}
const tick = () => new Promise((r) => setImmediate(() => setImmediate(r)));
(async () => {
  await tick();
  out.href = location.href === start ? null : location.href;
  out.guardAsks = dispatch('beforeunload').defaultPrevented;
  // The browser did not go: the time a page that leaves would have gone passes.
  if (c.stays) {
    timers.splice(0).forEach((fn) => fn && fn());
    await tick();
    out.afterStay = { first: out.all[0], later: {} };
    for (const method of ['get', 'post']) {
      out.afterStay.later[method] = 'pending';
      call(method).then(() => { out.afterStay.later[method] = 'resolved'; }, () => { out.afterStay.later[method] = 'rejected'; });
    }
    await tick();
    out.afterStay.guardAsks = dispatch('beforeunload').defaultPrevented;
  }
  process.stdout.write(JSON.stringify(out));
})();
