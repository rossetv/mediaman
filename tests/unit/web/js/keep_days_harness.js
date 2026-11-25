// Runs the real keep-days.js in the time zone TZ names and prints the day, the count and the day's other forms.
// Usage: node keep_days_harness.js <the day's instant> <now>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { makeDocument, build } = require('./fake_dom.js');

const doc = makeDocument();
const [at, now] = process.argv.slice(2);
doc.body.appendChild(build(doc, ['div', {},
  ['time', { 'data-keep-day': '', datetime: at }, 'as sent'], ['span', { 'data-keep-in': at }, 'as sent'],
  ...['short', 'month', 'number'].map((form) => ['time', { 'data-keep-day': form, datetime: at }, 'as sent'])]));
class Clock extends Date { constructor(...a) { if (a.length) super(...a); else super(now); } }
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../../../../src/mediaman/web/static/js/keep-days.js'), 'utf8'),
  { document: doc, Date: Clock, Math, isNaN });
console.log(JSON.stringify(doc.querySelectorAll('[datetime], [data-keep-in]').map((el) => el.textContent)));
