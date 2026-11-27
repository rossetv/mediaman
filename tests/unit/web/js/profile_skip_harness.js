// Runs the real profile-playback.js on the Profile pane's Watching markup in the fake DOM, with
// the switch "Skip intros and credits on the TV", and prints what each scenario saw as one JSON
// object. Usage: node profile_skip_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click, settle } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function page({ fails = false, saved = true } = {}) {
  const doc = makeDocument();
  const view = (name) => ['div', { class: 'you-view', 'data-view': name }, ['div', { 'data-lang-list': '' }], ['p', { 'data-lang-none': '' }]];
  const row = (id) => ['button', { id }, ['span', { 'data-row-value': '' }]];
  doc.body.appendChild(build(doc, ['div', { 'data-you-group': 'profile' },
    row('account-row-audio'), row('account-row-subs'), row('account-row-sublang'),
    ['button', { id: 'account-skip', class: 'tog on', role: 'switch', 'aria-checked': 'true' }],
    view('profile/audio'), view('profile/subtitles'), view('profile/subtitles/language'),
    ['div', { 'data-sub-fold': '' }], ['span', { 'data-sub-lang-name': '' }]]));
  const puts = [];
  const toasts = [];
  const timers = [];
  const held = { audio_lang: null, subtitle_lang: null, auto_skip: saved, languages: [] };
  const MM = {
    api: {
      get: () => Promise.resolve({ ...held }),
      put: (url, body) => {
        puts.push([url, body]);
        if (fails) return Promise.reject(new Error('No luck'));
        Object.assign(held, body);
        return Promise.resolve({ ...held });
      },
      messageOf: (e, d) => (e instanceof Error && e.message) || d,
    },
  };
  const UIFeedback = { success: (t) => toasts.push(t), error: (t) => toasts.push('error: ' + t) };
  const ctx = vm.createContext({
    document: doc, MM, UIFeedback, Intl, window: { MM }, navigator: { languages: ['en-GB'] },
    setTimeout: (fn) => timers.push(fn), clearTimeout() {},
  });
  ['core/dom.js', 'profile-playback.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const skip = doc.querySelector('#account-skip');
  return {
    puts, toasts,
    state: () => ({ checked: skip.getAttribute('aria-checked'), on: skip.classList.contains('on') }),
    async press() { click(doc, skip); await settle(); timers.splice(0).forEach((fn) => fn()); },
    pressNow() { click(doc, skip); },
  };
}

const scenarios = {
  async off() {
    const p = page();
    await settle();
    await p.press();
    const off = { state: p.state(), puts: p.puts.slice(), toasts: p.toasts.slice() };
    await p.press();
    return { off, on: p.state(), puts: p.puts };
  },
  async fails() {
    const p = page({ fails: true });
    await settle();
    p.pressNow();
    const moved = p.state();
    await settle();
    return { moved, back: p.state(), toasts: p.toasts };
  },
  // Turned off elsewhere since the page was drawn on, it shows off.
  async fresh() {
    const p = page({ saved: false });
    await settle();
    return p.state();
  },
  // A second press before the answer is not sent.
  async twice() {
    const p = page();
    await settle();
    p.pressNow();
    p.pressNow();
    await settle();
    return p.puts.length;
  },
};

(async () => {
  const out = {};
  for (const name of Object.keys(scenarios)) out[name] = await scenarios[name]();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
