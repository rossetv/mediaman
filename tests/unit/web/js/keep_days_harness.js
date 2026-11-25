// Runs the real keep-days.js in the time zone TZ names and prints the day, the count and the day's other forms.
// Usage: node keep_days_harness.js <the day's instant> <now> [<where a length ends>...]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build } = require('./fake_dom.js');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', on); },
});
const doc = makeDocument();
const [at, now, ...ends] = process.argv.slice(2);
doc.body.appendChild(build(doc, ['div', {},
  ['time', { 'data-keep-day': '', datetime: at }, 'as sent'], ['span', { 'data-keep-in': at }, 'as sent'],
  ...['short', 'month', 'number'].map((form) => ['time', { 'data-keep-day': form, datetime: at }, 'as sent'])]));
if (ends.length) {
  doc.body.appendChild(build(doc, ['form', { class: 'share-keep' }, ...ends.map((end) => ['button', {}, ['time', { datetime: end }, 'as sent']])]));
  doc.body.appendChild(build(doc, ['p', { 'data-keep-none': '', hidden: '' }]));
}
class Clock extends Date { constructor(...a) { if (a.length) super(...a); else super(now); } }
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../../../../src/mediaman/web/static/js/keep-days.js'), 'utf8'),
  { document: doc, Date: Clock, Math, isNaN, Array });
if (ends.length) console.log(JSON.stringify([...doc.querySelectorAll('button'), doc.querySelector('form'), doc.querySelector('[data-keep-none]')].map((el) => !!el.hidden)));
else console.log(JSON.stringify(doc.querySelectorAll('[datetime], [data-keep-in]').map((el) => el.textContent)));
