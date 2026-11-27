// Runs watch/core.js and watch/explore.js against the fake DOM, with the search page and the grid
// it stands on as stubs, and prints one scenario's outcome as JSON.
// Usage: node watch_explore_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { makeDocument, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

/* *frames*: where the frames asked for wait to be run, or none to run them at once. */
function load(data, frames) {
  const doc = makeDocument();
  const replies = {};
  const lists = [];
  const layers = [];
  const closed = [];
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, Set, Map, URLSearchParams,
    setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: (fn) => (frames ? frames.push(fn) : fn()),
    matchMedia: () => ({ matches: true }), getComputedStyle: () => ({ getPropertyValue: () => '' }),
    location: { search: '' },
    addEventListener: () => {},
    MM: { api: { get: (url) => Promise.resolve(replies[url]) } },
    MMWatch: {
      address: { layer(def) { layers.push(def); }, push() {}, replace() {}, back() {}, url: () => '/', top: () => 'explore' },
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js'].forEach(run);
  const el = ctx.MMWatch.core.el;
  const opened = [];
  const stage = {
    data, open(node, name, what, atOnce) { opened.push({ node, atOnce: !!atOnce }); }, close(until) { closed.push(until); }, grow() {}, rise() {}, say() {}, token() {}, spring() {},
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
  return { ctx, replies, lists, opened, layers, closed };
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

  // Opened under a photo in flight: the rows under the bar go at once, and what is said of the
  // person and their titles waits for the photo to land, not to cost the flight its frames.
  async heldUntilLanded() {
    const p = load({ can_download: { movie: true, show: true } });
    p.replies['/api/watch/person/9001'] = { tmdb_configured: true, person: { person_id: 9001, name: 'Mara Ellison', job: 'Actor' },
      items: [TITLE(1, true)], total: 1, in_library: 1, next_page: null };
    let land;
    p.ctx.MMWatch.explore.openPerson({ person_id: 9001, name: 'Mara Ellison' }, null, new Promise((ok) => { land = ok; }));
    const facts = () => p.opened[0].node.querySelector('.explore-facts').textContent;
    let cards = null;
    p.lists[0].fetchPage(1).then((page) => { cards = page.cards.length; });
    await settle();
    const flying = { facts: facts(), cards, atOnce: p.opened[0].atOnce };
    land();
    await settle();
    return { flying, landed: { facts: facts(), cards } };
  },
};

Object.assign(scenarios, {
  // Back to a title kept as it was, the rows wait for the person's photo to land; other ways out wait for nothing.
  async closedUnderATitle() {
    const close = async (how, waiting) => {
      const p = load({});
      let land;
      p.ctx.MMWatch.detail = { waiting: () => waiting };
      p.ctx.MMWatch.cast = { landing: () => new Promise((ok) => { land = ok; }) };
      p.layers.filter((l) => l.name === 'explore')[0].close(how);
      const until = p.closed[0];
      if (!until) return null;
      let done = false;
      until.then(() => { done = true; });
      await settle();
      const early = done;
      land();
      await settle();
      return [early, done];
    };
    return { title: await close('pop', true), rows: await close('pop', false), pushed: await close('push', true) };
  },

  // Thirty titles come a frame apart, twelve at a time, not all in the frame the photo lands in; each part but the last waits to be said with the rest.
  async batches() {
    const frames = [];
    const p = load({ can_download: { movie: true, show: true } }, frames);
    p.replies['/api/watch/person/9001'] = { tmdb_configured: true, person: { person_id: 9001, name: 'Mara Ellison', job: 'Actor' },
      items: Array.from({ length: 30 }, (_, n) => TITLE(n, n < 5)), total: 30, in_library: 5, next_page: null };
    p.ctx.MMWatch.explore.openPerson({ person_id: 9001, name: 'Mara Ellison' }, null);
    const sizes = [];
    let next = 1;
    while (next !== null) {
      let page = null;
      p.lists[0].fetchPage(next).then((got) => { page = got; });
      await settle();
      const early = page !== null;
      frames.splice(0).forEach((fn) => fn(0));
      await settle();
      sizes.push([page.cards.length, early, !!page.part]);
      next = page.next;
    }
    return sizes;
  },

  // What stands below the heading is not drawn while the photo flies.
  async keptBack() {
    const p = load({ can_download: { movie: true, show: true } });
    p.replies['/api/watch/person/9001'] = { tmdb_configured: true, person: { person_id: 9001, name: 'Mara Ellison', job: 'Actor' },
      items: [TITLE(1, true)], total: 1, in_library: 1, next_page: null, known_for: [TITLE(2, false)] };
    let land;
    p.ctx.MMWatch.explore.openPerson({ person_id: 9001, name: 'Mara Ellison' }, null, new Promise((ok) => { land = ok; }));
    const node = p.opened[0].node;
    const look = () => ({ known: node.querySelector('.explore-known').hidden, grid: node.querySelector('.explore-grid').hidden });
    await settle();
    const flying = look();
    land();
    await settle();
    return { flying, landed: look() };
  },
});

const name = process.argv[2];
if (!scenarios[name]) { console.error('unknown scenario ' + name); process.exit(2); }
scenarios[name]().then((out) => console.log(JSON.stringify(out)), (err) => { console.error(err.stack); process.exit(1); });
