// Runs watch/core.js and watch/explore.js against the fake DOM, with the search page and the grid
// it stands on as stubs, and prints one scenario's outcome as JSON.
// Usage: node watch_explore_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { makeDocument, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

function load(data) {
  const doc = makeDocument();
  const replies = {};
  const lists = [];
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, Set, Map, URLSearchParams,
    setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: (fn) => fn(),
    matchMedia: () => ({ matches: true }), getComputedStyle: () => ({ getPropertyValue: () => '' }),
    location: { search: '' },
    addEventListener: () => {},
    MM: { api: { get: (url) => Promise.resolve(replies[url]) } },
    MMWatch: {
      address: { layer() {}, push() {}, replace() {}, back() {}, url: () => '/', top: () => 'explore' },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js'].forEach(run);
  const el = ctx.MMWatch.core.el;
  const stage = {
    data, open() {}, close() {}, grow() {}, rise() {}, say() {}, token() {}, spring() {},
    face: () => el('span', '', el('span')),
    head: (title) => el('h2', 'results-group__title', title),
  };
  ctx.MMWatch.search = {
    stage, composing: () => false,
    isHeld: (item) => !!item.in_library,
    tmdbCard: (item) => el('a', 'watch-card', item.title),
  };
  ctx.MMWatch.grid = { list: (host, opts) => { lists.push(opts); return { stop() {} }; } };
  run('watch/explore.js');
  return { ctx, replies, lists };
}

const TITLE = (n, held) => ({ ref: 'tmdb-movie-' + n, title: 'T' + n, kind: 'movie', in_library: held });

const scenarios = {
  // The heading over what a person is known for that the library does not hold, by what is set up.
  async personHeads() {
    const heads = async (can) => {
      const p = load({ can_download: can });
      p.replies['/api/watch/person/9001'] = { tmdb_configured: true, person: { person_id: 9001, name: 'Mara Ellison' },
        items: [TITLE(1, true), TITLE(2, false)], total: 2, in_library: 1, next_page: null };
      p.ctx.MMWatch.explore.openPerson({ person_id: 9001, name: 'Mara Ellison' }, null);
      const page = await p.lists[0].fetchPage(1);
      await settle();
      return page.cards.filter((n) => n.tagName === 'H2').map((n) => n.textContent);
    };
    return {
      both: await heads({ movie: true, show: true }),
      films: await heads({ movie: true, show: false }),
      none: await heads({ movie: false, show: false }),
    };
  },
};

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
