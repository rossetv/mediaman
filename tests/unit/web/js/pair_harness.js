// Runs the real pair.js on the fake DOM and prints one scenario's outcome as JSON.
// Usage: node pair_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build, settle } = require('./fake_dom.js');

const FILE = path.resolve(__dirname, '../../../../src/mediaman/web/static/js/pair.js');

function page(hash, answers, yes = true) {
  const doc = makeDocument();
  const input = (id) => ['input', { id }];
  doc.body.appendChild(build(doc, ['main', {},
    ['form', { id: 'pair-form' }, ['p', { id: 'pair-ask' }], ['ol', { id: 'pair-steps' }],
      ['div', { id: 'pair-boxes' }, ['i', {}], ['i', {}], ['i', {}], ['i', {}], ['i', {}], ['i', {}], input('pair-code')],
      ['p', { id: 'pair-why' }, 'Only pair a TV you can see.'], ['button', { id: 'pair-agree' }]],
    ['form', { id: 'pair-name-form' }, ['p', { id: 'pair-ok', tabindex: '-1' }],
      ['button', { class: 'fpill' }, 'Living room'], ['button', { class: 'fpill' }, 'Bedroom TV'],
      input('pair-name'), ['p', { id: 'pair-name-why' }], ['button', { id: 'pair-save' }]],
    ['div', { id: 'pair-done' }, ['p', { id: 'pair-ready', tabindex: '-1' }], ['span', { id: 'pair-ready-name' }]]]));
  const $ = (id) => doc.getElementById(id);
  ['pair-agree', 'pair-name-form', 'pair-done', 'pair-ask'].forEach((id) => { $(id).hidden = true; });
  ['pair-form', 'pair-steps'].forEach((id) => { $(id).hidden = false; });
  $('pair-code').value = $('pair-name').value = '';
  const sent = [];
  const asked = [];
  const answer = (url, body) => {
    sent.push([url, body]);
    const next = answers.shift();
    return next.error ? Promise.reject(next) : Promise.resolve(next);
  };
  const location = { hash, pathname: '/pair' };
  const replaced = [];
  const listeners = {};
  const ctx = vm.createContext({
    document: doc, location, history: { replaceState: (a, b, url) => { replaced.push(url); location.hash = ''; } },
    MM: { api: { post: answer, patch: answer, messageOf: (e, d) => (e && e.words) || d } },
    UIFeedback: { confirm: (o) => { asked.push(o.body); return Promise.resolve(yes); } },
    window: { addEventListener: (t, fn) => { listeners[t] = fn; } },
  });
  vm.runInContext(fs.readFileSync(FILE, 'utf8'), ctx);
  const fire = (id, type) => $(id).listeners[type].forEach((fn) => fn({ preventDefault() {} }));
  return {
    $, sent, asked, replaced, fire,
    scan(h) { location.hash = h; listeners.hashchange(); },
    type: async (text) => { $('pair-code').value = text; fire('pair-code', 'input'); await settle(); },
    say: () => [$('pair-why').className, $('pair-why').textContent],
    boxes: () => $('pair-boxes').querySelectorAll('i').map((b) => b.textContent).join(''),
    pills: () => doc.querySelectorAll('.fpill').map((b) => b.getAttribute('aria-pressed')),
    shown: () => ['pair-form', 'pair-name-form', 'pair-done'].filter((id) => !$(id).hidden),
    focused: () => doc.activeElement.id,
  };
}
const TV = { tv: { id: 7, name: 'TV' } };

const scenarios = {
  async fromQr() {
    const p = page('#482916', [TV, { tv: { id: 7, name: 'Bedroom TV' } }]);
    await settle();
    const before = { sent: p.sent.length, boxes: p.boxes(), agree: !p.$('pair-agree').hidden, readOnly: p.$('pair-code').readOnly, replaced: p.replaced };
    p.fire('pair-form', 'submit');
    p.fire('pair-form', 'submit');
    await settle();
    const named = { shown: p.shown(), name: p.$('pair-name').value, pills: p.pills() };
    p.$('pair-name').value = ' Bedroom TV ';
    p.fire('pair-name-form', 'submit');
    await settle();
    return { before, named, sent: p.sent, shown: p.shown(), says: p.$('pair-ready-name').textContent, focused: p.focused() };
  },
  async typed() {
    const p = page('', [{ error: 'wrong_code' }, { error: 'too_many_tries' }, { error: 'too_many_tvs' }, { error: 'http_502', words: 'Away' }, TV]);
    await p.type('48a29');
    const early = p.sent.length;
    const out = [];
    for (const code of ['482 916', '111111', '222222', '333333']) { await p.type(code); out.push([p.sent[p.sent.length - 1][1].code, p.say(), p.$('pair-code').value]); }
    p.fire('pair-form', 'submit');
    await settle();
    return { early, out, last: p.sent[p.sent.length - 1], shown: p.shown() };
  },
  async inUse() {
    const yes = page('#482916', [{ error: 'in_use', data: { playing: true } }, TV]);
    yes.fire('pair-form', 'submit');
    await settle();
    const no = page('#482916', [{ error: 'in_use', data: { playing: false } }], false);
    no.fire('pair-form', 'submit');
    await settle();
    return { asked: yes.asked, sent: yes.sent.map((s) => s[1]), shown: yes.shown(), no: { asked: no.asked, sent: no.sent.length, code: no.$('pair-code').value, agree: !no.$('pair-agree').hidden } };
  },
  async again() {
    const p = page('#482916', [{ tv: { id: 7, name: 'Den' } }, { error: 'x', words: 'Away' }]);
    p.fire('pair-form', 'submit');
    await settle();
    const name = p.$('pair-name').value;
    p.$('pair-name').value = '  ';
    p.fire('pair-name-form', 'submit');
    await settle();
    const empty = [p.sent.length, p.$('pair-name-why').textContent];
    p.$('pair-name').value = 'Den 2';
    p.fire('pair-name-form', 'submit');
    await settle();
    const failed = [p.shown(), p.$('pair-name-why').textContent];
    p.scan('#111222');
    return { name, empty, failed, scanned: [p.shown(), p.boxes(), p.sent.length] };
  },
};

scenarios[process.argv[2]]().then((out) => process.stdout.write(JSON.stringify(out)), (e) => { console.error(e); process.exit(1); });
