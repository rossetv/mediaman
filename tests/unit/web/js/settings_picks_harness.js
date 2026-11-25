// Runs settings/integrations.js around the Refresh of the shared picks against the fake DOM and
// prints the outcome of one scenario as JSON. Usage: node settings_picks_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click, settle } = require('./fake_dom');

const FILE = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/settings/integrations.js');
const NOW = Date.UTC(2026, 10, 25, 12);
const inHours = (h) => new Date(NOW + h * 3600000).toISOString();

async function load(opts = {}) {
  const doc = makeDocument();
  [['openai_api_key', opts.key === undefined ? '****' : opts.key], ['suggestions_enabled', opts.on || 'true']].forEach(([id, value]) => {
    doc.body.appendChild(build(doc, ['input', { id }])).value = value;
  });
  const button = doc.body.appendChild(build(doc, ['button', { id: 'btn-refresh-picks' }, 'Refresh shared picks for everyone']));
  const p = { button, calls: [], said: [], timers: [], clock: NOW, status: opts.status || { status: 'idle' }, post: opts.post || (() => Promise.resolve({ status: 'started' })) };
  const say = (kind) => (m) => p.said.push([kind, m]);
  class Clock extends Date { static now() { return p.clock; } }
  const ctx = {
    document: doc, console, Date: Clock, Promise,
    setTimeout: (fn, ms) => { p.timers.push({ fn, ms }); return fn; },
    clearTimeout: (fn) => { p.timers = p.timers.filter((t) => t.fn !== fn); },
    UIFeedback: { toast: say('toast'), success: say('success'), error: say('error') },
    MM: { api: {
      get: (u) => { p.calls.push('GET ' + u); return p.status instanceof Error ? Promise.reject(p.status) : Promise.resolve(p.status); },
      post: (u) => { if (/recommended/.test(u)) { p.calls.push('POST ' + u); return p.post(); } return Promise.resolve({}); },
      messageOf: (e, words) => words,
    } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.fetch) {
    Object.assign(ctx, { location: { pathname: '/settings', search: '', hash: '', href: '/settings' }, fetch: opts.fetch });
    vm.runInContext(fs.readFileSync(path.resolve(path.dirname(FILE), '../core/api.js'), 'utf8'), ctx);
    ctx.MM.api.hasUnsavedChanges = () => !!opts.edits;
  }
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  p.ctx = ctx;
  ctx.MM.settings.integrations.init();
  await settle();
  p.press = async () => { click(doc, button); await settle(); };
  // Every timer due now, once.
  p.tick = async () => { p.timers.splice(0).forEach((t) => t.fn()); await settle(); };
  p.look = () => ({ text: button.textContent, off: button.getAttribute('aria-disabled') === 'true', label: button.getAttribute('aria-label') });
  return p;
}

const posts = (p) => p.calls.filter((c) => /^POST/.test(c)).length;

const scenarios = {
  async waits() {
    const p = await load({ status: { status: 'idle', next_available_at: inHours(13.5) } });
    const out = { look: p.look() };
    await p.press();
    out.said = p.said;
    out.posts = posts(p);
    p.clock = NOW + 13.5 * 3600000;
    await p.tick();
    out.over = p.look();
    return out;
  },
  async done() {
    const p = await load();
    const out = { ready: p.look() };
    p.status = { status: 'running' };
    await p.press();
    await p.press();
    out.busy = p.look();
    await p.tick();
    p.status = { status: 'done', result: { ok: true }, next_available_at: inHours(24) };
    await p.tick();
    return { ...out, after: p.look(), said: p.said, calls: p.calls };
  },
  async failed() {
    const p = await load();
    await p.press();
    p.status = { status: 'done', result: { ok: false, error: 'No key quota' } };
    await p.tick();
    const none = { look: p.look(), said: p.said.splice(0) };
    p.status = { status: 'idle' };
    await p.press();
    await p.tick();
    const once = p.look().text;
    await p.tick();
    const died = { once, look: p.look(), said: p.said.splice(0) };
    p.status = new Error('offline');
    await p.press();
    for (let i = 0; i < 20 && !p.said.length; i += 1) await p.tick();
    return { none, died, offline: { look: p.look(), said: p.said, timers: p.timers.length } };
  },
  async refused() {
    const at = async (status, data) => {
      const p = await load({ post: () => Promise.reject(Object.assign(new Error('x'), { status, data })) });
      await p.press();
      return { look: p.look(), said: p.said };
    };
    return { wait: await at(429, { next_available_at: inHours(20) }), broken: await at(500, {}) };
  },
  async needs() {
    const at = async (opts) => {
      const p = await load(opts);
      await p.press();
      return { off: p.look().off, said: p.said, posts: posts(p) };
    };
    const out = { key: await at({ key: '' }), on: await at({ on: 'false' }) };
    const p = await load({ key: '' });
    p.ctx.MM.settings.integrations.recheck(['openai_api_key'], { openai_api_key: 'sk', suggestions_enabled: true });
    out.saved = p.look().off;
    return out;
  },
  // The session ends, or is no admin's any more, while a refresh is followed or as it is asked for.
  async loggedOut() {
    const at = async (status, edits, when) => {
      let ended = false;
      let polls = 0;
      const answer = (code, data) => Promise.resolve({ ok: code < 400, status: code, statusText: '', json: () => Promise.resolve(data) });
      const p = await load({ edits, fetch: (url, req) => {
        if (!/recommended/.test(url)) return answer(200, {});
        if (ended && !req.method.includes('POST')) polls += 1;
        return ended ? answer(status, { detail: 'x' }) : answer(200, { status: req.method === 'POST' ? 'started' : 'idle' });
      } });
      ended = when === 'press';
      await p.press();
      ended = true;
      if (when === 'poll') for (let i = 0; i < 12; i += 1) await p.tick();
      return { href: p.ctx.location.href, polls, said: p.said.map((s) => s[1]), text: edits ? p.look().text : null };
    };
    return { clean: await at(401, false, 'poll'), demoted: await at(403, false, 'poll'), edits: await at(401, true, 'poll'),
      demotedEdits: await at(403, true, 'poll'), press: await at(401, true, 'press'), pressClean: await at(401, false, 'press') };
  },
  async running() {
    const p = await load({ status: { status: 'running' } });
    const busy = p.look();
    p.status = { status: 'done', result: { ok: true }, next_available_at: inHours(24) };
    await p.tick();
    // One that ended before the page came is no news.
    const old = await load({ status: p.status });
    return { busy, said: p.said, old: old.said };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
