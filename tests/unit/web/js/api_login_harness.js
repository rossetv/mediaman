'use strict';
/* Runs core/api.js against a 401 on the page at the given location; prints where the browser goes. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const file = path.join(__dirname, '../../../../src/mediaman/web/static/js/core/api.js');
const location = JSON.parse(process.argv[2]);
const window = {
  location,
  fetch: () => Promise.resolve({ status: 401, ok: false, json: () => Promise.resolve({}) }),
};
vm.runInNewContext(fs.readFileSync(file, 'utf8'), Object.assign(window, { window, console }));
window.MM.api.get('/api/x').catch(() => {});
setImmediate(() => process.stdout.write(JSON.stringify({ href: location.href })));
