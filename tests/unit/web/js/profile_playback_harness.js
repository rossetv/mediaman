// Runs the real profile-playback.js on the Profile pane's Watching markup in the fake DOM and
// prints one scenario's outcome as JSON.
// Usage: node profile_playback_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click, settle, Node } = require('./fake_dom.js');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

// [hidden] as the browser keeps it: the attribute.
Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', !!on); },
});

const LANGUAGES = [
  { code: 'zh', name: 'Chinese' }, { code: 'en', name: 'English' }, { code: 'fr', name: 'French' },
  { code: 'de', name: 'German' }, { code: 'ja', name: 'Japanese' },
];

function row(id, view) {
  return ['button', { id, class: 'yo-row', 'data-you-go': view }, ['span', { 'data-row-value': '' }]];
}
function languages() {
  return [['div', { class: 'hx-search yo-lang-search' }, ['input', { type: 'search', 'data-lang-q': '' }], ['button', { 'data-lang-x': '' }]],
    ['div', { 'data-lang-list': '' }], ['p', { class: 'yo-lang-none', 'data-lang-none': '', hidden: '' }]];
}
function pick(value, text) {
  return ['button', { class: 'yo-pick-row', role: 'radio', 'data-subs': value, 'aria-checked': 'false', tabindex: '-1' }, ['span', {}, text]];
}

function page(held, { put = null, fails = false } = {}) {
  const doc = makeDocument();
  doc.body.appendChild(build(doc, ['div', { id: 'you' }, ['button', { 'data-you-back': '' }], ['div', { 'data-you-group': 'profile' },
    row('account-row-audio', 'profile/audio'), row('account-row-subs', 'profile/subtitles'),
    ['div', { class: 'you-view', 'data-view': 'profile/audio' }, ...languages()],
    ['div', { class: 'you-view', 'data-view': 'profile/subtitles' },
      ['div', { role: 'radiogroup' }, pick('needed', 'Only when needed'),
        ['button', { class: 'yo-pick-row', role: 'radio', 'data-subs': 'always', 'aria-checked': 'false', tabindex: '-1' }, 'Always, in ', ['span', { 'data-sub-lang-name': '' }]],
        pick('off', 'Off')],
      ['div', { class: 'yo-fold-in', 'data-sub-fold': '' }, row('account-row-sublang', 'profile/subtitles/language')]],
    ['div', { class: 'you-view', 'data-view': 'profile/subtitles/language' }, ...languages()]]]));
  const puts = [];
  const toasts = [];
  const timers = [];
  const answer = () => ({ ...held, languages: LANGUAGES });
  const MM = {
    api: {
      get: () => Promise.resolve(answer()),
      put: (url, body) => {
        puts.push([url, body]);
        if (fails) return Promise.reject(new Error('No luck'));
        Object.assign(held, body);
        return Promise.resolve(answer());
      },
      messageOf: (e, d) => (e instanceof Error && e.message) || d,
    },
  };
  const UIFeedback = { success: (t) => toasts.push(t), error: (t) => toasts.push('error: ' + t) };
  const ctx = vm.createContext({
    document: doc, MM, UIFeedback, Intl, window: { MM },
    navigator: { languages: ['ja-JP', 'en-GB'] },
    setTimeout: (fn) => timers.push(fn), clearTimeout() {},
  });
  ['core/dom.js', 'profile-playback.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const $ = (s) => doc.querySelector(s);
  const view = (v) => $('[data-view="' + v + '"]');
  return {
    doc, puts, toasts, $, view,
    wait: () => timers.splice(0).forEach((fn) => fn()),
    value: (id) => $('#' + id + ' [data-row-value]').textContent,
    checked: (v) => view(v).querySelectorAll('[aria-checked="true"]').map((r) => r.getAttribute('data-audio-lang') ?? r.getAttribute('data-sub-lang') ?? r.getAttribute('data-subs')),
    rowOf: (v, attr, value) => view(v).querySelectorAll('[' + attr + '="' + value + '"]').pop(),
    shown: (v) => view(v).querySelectorAll('.yo-pick-row').filter((r) => !r.hidden && !r.closest('[hidden]')).map((r) => r.getAttribute('data-audio-lang') ?? r.getAttribute('data-sub-lang')),
    heads: (v) => view(v).querySelectorAll('[data-lang-head]').filter((h) => !h.hidden).length,
    search(v, text) {
      const input = view(v).querySelector('[data-lang-q]');
      input.value = text;
      (doc.querySelector('[data-you-group]').listeners.input || []).forEach((fn) => fn({ target: input }));
    },
    folded: () => $('[data-sub-fold]').classList.contains('is-open'),
  };
}

const scenarios = {
  async hub() {
    const p = page({ audio_lang: 'de', subtitle_lang: 'fr' });
    await settle();
    const out = { audio: p.value('account-row-audio'), subs: p.value('account-row-subs'), folded: p.folded(),
      checked: [p.checked('profile/audio'), p.checked('profile/subtitles'), p.checked('profile/subtitles/language')],
      suggested: p.shown('profile/audio').slice(0, 4) };
    const q = page({ audio_lang: null, subtitle_lang: null });
    await settle();
    out.plain = { audio: q.value('account-row-audio'), subs: q.value('account-row-subs'), folded: q.folded(), checked: q.checked('profile/audio') };
    return out;
  },

  async pickAudio() {
    const p = page({ audio_lang: 'en', subtitle_lang: null });
    await settle();
    click(p.doc, p.rowOf('profile/audio', 'data-audio-lang', 'fr'));
    const at = { checked: p.checked('profile/audio') };
    await settle();
    p.wait();
    return { at, puts: p.puts, audio: p.value('account-row-audio'), toasts: p.toasts };
  },

  async pickFails() {
    const p = page({ audio_lang: 'en', subtitle_lang: null }, { fails: true });
    await settle();
    click(p.doc, p.rowOf('profile/audio', 'data-audio-lang', 'fr'));
    click(p.doc, p.rowOf('profile/subtitles', 'data-subs', 'always'));
    await settle();
    p.wait();
    return { audio: p.checked('profile/audio'), subs: p.checked('profile/subtitles'), folded: p.folded(),
      value: p.value('account-row-audio'), toasts: p.toasts };
  },

  async search() {
    const p = page({ audio_lang: 'en', subtitle_lang: null });
    await settle();
    p.search('profile/audio', 'germ');
    const germ = { shown: p.shown('profile/audio'), heads: p.heads('profile/audio') };
    p.search('profile/audio', 'english');
    const english = p.shown('profile/audio');
    p.search('profile/audio', 'Klingon');
    const none = p.view('profile/audio').querySelector('[data-lang-none]');
    const nothing = { shown: p.shown('profile/audio'), none: none.hidden ? null : none.textContent };
    p.search('profile/audio', '');
    return { germ, english, nothing, cleared: { heads: p.heads('profile/audio'), none: none.hidden, count: p.shown('profile/audio').length } };
  },

  async always() {
    const p = page({ audio_lang: 'de', subtitle_lang: null });
    await settle();
    const before = p.folded();
    click(p.doc, p.rowOf('profile/subtitles', 'data-subs', 'always'));
    await settle();
    const always = { folded: p.folded(), subs: p.value('account-row-subs') };
    click(p.doc, p.rowOf('profile/subtitles/language', 'data-sub-lang', 'ja'));
    await settle();
    const lang = { subs: p.value('account-row-subs'), sub: p.value('account-row-sublang') };
    click(p.doc, p.rowOf('profile/subtitles', 'data-subs', 'off'));
    await settle();
    return { before, always, lang, after: { folded: p.folded(), subs: p.value('account-row-subs') }, puts: p.puts.map((x) => x[1]) };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)));
