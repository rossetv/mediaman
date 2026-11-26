// Runs settings/integrations.js around the line by the AI switch against the fake DOM and
// prints the outcome of one scenario as JSON. Usage: node settings_ai_need_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, click, settle } = require('./fake_dom');

const FILE = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/settings/integrations.js');

const NEEDS = ['p', {}, 'Needs an ', ['a', { href: '#intg-openai' }, 'OpenAI key'], '.'];
const REFUSED = ['p', {}, 'OpenAI refused the key. Check it in the ', ['a', { href: '#intg-openai' }, 'OpenAI card'], '.'];

// The page as served: *need* is what the server found the switch lacks ('', 'key' or 'refused').
async function load({ key = '', on = true, need = 'key' } = {}) {
  const doc = makeDocument();
  const add = (spec) => doc.body.appendChild(build(doc, spec));
  add(['input', { id: 'openai_api_key' }]).value = key;
  const sw = add(['span', { class: 'tog', role: 'switch', 'data-toggle': '', 'data-target': 'suggestions_enabled', 'aria-checked': String(on) }]);
  const input = add(['input', { id: 'suggestions_enabled' }]);
  input.value = String(on);
  // As toggles.js flips it, on its own element, before the click reaches the document.
  sw.addEventListener('click', () => {
    const now = sw.getAttribute('aria-checked') !== 'true';
    sw.setAttribute('aria-checked', String(now));
    input.value = String(now);
  });
  add(['div', { id: 'setg-ai-need', class: 'setg-row-sub ai-need' + (need ? ' is-open' : ''), 'data-need': need },
    ['div', {}, need === 'refused' ? REFUSED : NEEDS]]);
  const card = add(['div', { id: 'intg-openai', class: 'intg-card is-collapsed' },
    ['div', { class: 'intg-card-hd', 'data-intg-toggle': '' }, ['h3', {}, ['button', { id: 'openai-fold', class: 'intg-fold' }, 'OpenAI']]]]);
  const p = { doc, sw, input, card, opened: 0, scrolled: false };
  const fold = card.querySelector('.intg-fold');
  fold.click = () => click(doc, fold);
  card.querySelector('.intg-card-hd').addEventListener('click', () => { card.classList.toggle('is-collapsed'); p.opened += 1; });
  card.scrollIntoView = () => { p.scrolled = true; };
  const ctx = {
    document: doc, console, Promise,
    setTimeout: () => 0, clearTimeout: () => {},
    matchMedia: () => ({ matches: false }),
    UIFeedback: { toast() {}, success() {}, error() {} },
    MM: { api: { get: () => new Promise(() => {}), post: () => new Promise(() => {}), messageOf: (e, w) => w } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  ctx.MM.settings.integrations.init();
  await settle();
  p.recheck = (keys, values) => ctx.MM.settings.integrations.recheck(keys, values);
  p.look = () => {
    const line = doc.getElementById('setg-ai-need');
    return { open: line.classList.contains('is-open'), text: line.textContent.trim() };
  };
  return p;
}

const scenarios = {
  async flip() {
    const p = await load();
    click(p.doc, p.sw);
    const off = p.look();
    click(p.doc, p.sw);
    const on = p.look();
    p.doc.getElementById('openai_api_key').value = 'sk-typed';
    const keyed = p.look();
    // Discard puts the switch back as saved, then tells this script what is stored.
    p.sw.setAttribute('aria-checked', 'false');
    p.input.value = 'false';
    p.recheck([], { openai_api_key: '', suggestions_enabled: 'false' });
    return { off, on, keyed, discarded: p.look() };
  },
  async saved() {
    const p = await load();
    p.recheck(['openai_api_key'], { openai_api_key: 'sk-new', suggestions_enabled: 'true' });
    const out = { keySaved: p.look() };
    const r = await load({ key: '****', need: 'refused' });
    r.recheck(['scan_day'], { openai_api_key: '****', suggestions_enabled: 'true' });
    out.otherSave = r.look();
    r.recheck(['openai_api_key'], { openai_api_key: 'sk-new', suggestions_enabled: 'true' });
    out.newKey = r.look();
    r.recheck(['openai_api_key'], { openai_api_key: '', suggestions_enabled: 'true' });
    out.keyCleared = r.look();
    return out;
  },
  async link() {
    const p = await load();
    const a = p.doc.querySelector('#setg-ai-need a');
    let prevented = false;
    click(p.doc, a, { preventDefault() { prevented = true; } });
    return { prevented, opened: p.opened, focus: p.doc.activeElement.id, scrolled: p.scrolled };
  },
};

scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
