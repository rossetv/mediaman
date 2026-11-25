// Runs ratings.js against the fake DOM and a server whose answers the scenario gives, and prints
// one scenario's outcome as JSON. Usage: node ratings_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});

function load() {
  const doc = makeDocument();
  const events = [];
  doc.dispatchEvent = (e) => events.push({ phase: e.detail.phase, ref: e.detail.ref, refs: e.detail.refs, level: e.detail.level, was: e.detail.was === undefined ? 'unknown' : e.detail.was });
  const out = []; // requests not answered yet
  const log = [];
  const listeners = {};
  let timers = [];
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Promise, Error,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    setTimeout: (fn) => { timers.push(fn); },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    location: { pathname: '/watch', search: '', hash: '' },
    fetch: (url, o) => {
      const what = o.method + ' ' + decodeURIComponent(url.replace('/api/watch/ratings', '')) + (o.body ? ' ' + JSON.parse(o.body).level : '');
      log.push(what);
      return new Promise((resolve, reject) => out.push({ what, resolve, reject }));
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  ['core/api.js', 'ratings.js'].forEach((f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx));
  const go = async () => { const due = timers; timers = []; due.forEach((fn) => fn()); await settle(); };
  const answer = async (body, status = 200) => {
    out.shift().resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
    await settle();
    await go();
  };
  return {
    ctx, events, log, go, answer, listeners, R: ctx.MM.ratings,
    swap: () => out.reverse(),
    ratings: (map) => answer({ ok: true, ratings: map }),
    title: (id, was) => answer({ ok: true, media_type: 'movie', tmdb_id: id, was }),
    fail: () => answer({ ok: false, error: 'http_500' }, 500),
    refuse: () => answer({ ok: false, error: 'too_many_requests' }, 429),
    press: (set, i) => click(doc, set.node.querySelectorAll('button')[i]),
    look: (set) => ({
      on: set.node.querySelectorAll('button').map((b, i) => ((b.getAttribute('aria-pressed') || b.getAttribute('aria-checked')) === 'true' ? i : -1)).filter((i) => i >= 0),
      dim: set.node.classList.contains('has'),
      line: set.line.textContent, bad: set.line.classList.contains('is-bad'), hidden: set.node.hidden && set.line.hidden,
    }),
  };
}
const lv = (level) => ({ level, rateable: true });

const scenarios = {
  // One set of a title rated before: what it shows, then its own thumb pressed.
  async back() {
    const p = load();
    const set = p.R.mount('movie-1');
    const first = p.look(set);
    await p.go();
    await p.ratings({ 'movie-1': lv(1) });
    const before = p.look(set);
    p.press(set, 2);
    const pending = p.look(set);
    await p.title(7);
    return { first, before, pending, after: p.look(set), log: p.log, events: p.events };
  },

  async levels() {
    const p = load();
    const all = {};
    for (const variant of ['plain', 'grid', 'strip', 'menu']) {
      const set = p.R.mount('movie-' + variant.length + variant, { variant });
      all[variant] = [set.line.textContent];
      [0, 1, 2, 3, 3].forEach((i) => { p.press(set, i); all[variant].push(set.line.textContent); });
    }
    return all;
  },

  async pop() {
    const p = load();
    const set = p.R.mount('movie-1');
    const pops = () => set.node.querySelectorAll('button').map((b) => b.classList.contains('pop'));
    p.press(set, 3);
    const on = pops();
    p.press(set, 3);
    const back = () => set.node.querySelectorAll('button').map((b) => b.classList.contains('back'));
    const off = pops();
    const taken = back();
    p.press(set, 3);
    return { on, off, taken, again: back() };
  },

  // Where the highlight slides from, for each press and for one that fails.
  async slides() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(-1) });
    const from = () => set.node.getAttribute('data-from');
    const out = [from()];
    [3, 0, 0, 2].forEach((i) => { p.press(set, i); out.push(from()); });
    await p.title(7);
    await p.title(7);
    p.press(set, 3);
    out.push(from());
    await p.refuse();
    return out.concat(from());
  },

  async fails() {
    const p = load();
    const set = p.R.mount('movie-1');
    const other = p.R.mount('movie-1', { variant: 'menu' });
    await p.go();
    await p.ratings({ 'movie-1': lv(-1) });
    p.press(set, 3);
    const pending = [p.look(set), p.look(other)];
    await p.fail();
    const failed = [p.look(set), p.look(other)];
    p.press(set, 3);
    return { pending, failed, again: p.look(set), last: p.events[2], live: [set, other].map((s) => s.line.getAttribute('aria-live')) };
  },

  // Three presses while the first save is out, and one while that fails.
  async quick() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null) });
    p.log.length = 0;
    p.press(set, 3); p.press(set, 2); p.press(set, 2); p.press(set, 0);
    const out1 = p.log.slice();
    await p.title(7);
    const out2 = p.log.slice();
    await p.title(7);
    p.press(set, 1); p.press(set, 2);
    await p.fail();
    await p.title(7);
    return { out1, out2, log: p.log, look: p.look(set), saved: p.events.filter((e) => e.phase === 'saved' || e.phase === 'failed').map((e) => [e.phase, e.level, e.was]) };
  },

  // Twice the same thumb at once: rated, then taken back.
  async double() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null) });
    p.press(set, 3); p.press(set, 3);
    await p.title(7);
    await p.title(7);
    return { log: p.log.slice(1), look: p.look(set) };
  },

  async batch() {
    const p = load();
    for (let i = 0; i < 3; i++) p.R.mount('movie-' + i);
    p.R.read(['movie-1', 'show-9']);
    await p.go();
    await p.ratings({ 'movie-0': lv(2), 'movie-1': lv(null), 'movie-2': { level: null, rateable: false }, 'show-9': lv(-2) });
    const set = p.R.mount('movie-2');
    p.R.mount('movie-0');
    await p.go();
    const many = [];
    for (let i = 0; i < 150; i++) many.push('tmdb-tv-' + (1000 + i));
    p.R.read(many);
    await p.go();
    return { log: p.log.map((l) => [l.split('?')[0], l.split(',').length]), first: p.log[0],
      got: ['movie-0', 'movie-1', 'show-9', 'movie-77'].map((r) => { const l = p.R.get(r); return l === undefined ? 'unknown' : l; }), hidden: p.look(set).hidden };
  },

  // movie-1 is TMDB's 7, which another place names outright and a third by its pick.
  async names() {
    const p = load();
    const a = p.R.mount('movie-1');
    const b = p.R.mount('tmdb-movie-7');
    const c = p.R.mount('pick-3');
    await p.go();
    await p.ratings({ 'movie-1': lv(null), 'tmdb-movie-7': lv(null), 'pick-3': lv(null) });
    p.log.length = 0;
    p.press(a, 3);
    await p.title(7);
    const b1 = p.look(b).on;
    await p.ratings({ 'pick-3': lv(2) });
    const c1 = p.look(c).on;
    p.press(b, 0);
    return { b1, c1, a2: p.look(a).on, refs: p.events[p.events.length - 1].refs, log: p.log };
  },

  // The first read is answered after a press and its save.
  async late() {
    const p = load();
    const a = p.R.mount('movie-1');
    const b = p.R.mount('pick-3');
    await p.go();
    p.press(a, 3);
    const read = p.answer.bind(null, { ok: true, ratings: { 'movie-1': lv(null), 'pick-3': lv(null) } });
    // The save's answer comes first.
    const order = p.log.slice();
    p.swap();
    await p.title(7);
    await read();
    const look = p.look(a).on;
    await p.ratings({ 'pick-3': lv(2) });
    return { order, look, b: p.look(b).on, log: p.log };
  },

  // Set mounted after a save that failed, beside the one that showed it.
  async failedBefore() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null) });
    p.press(set, 3);
    await p.refuse();
    const later = p.R.mount('movie-1', { variant: 'menu' });
    return { later: p.look(later), set: p.look(set), log: p.log.slice(2) };
  },

  // A save whose answer is lost: the server holds it, or does not.
  async lost() {
    const after = async (level) => {
      const p = load();
      const set = p.R.mount('movie-1');
      await p.go();
      await p.ratings({ 'movie-1': lv(-1) });
      p.press(set, 3);
      await p.fail();
      const asked = p.log.slice(2);
      const meanwhile = p.R.get('movie-1');
      await p.ratings({ 'movie-1': lv(level) });
      return { asked, meanwhile, look: p.look(set) };
    };
    return { held: await after(2), not: await after(-1) };
  },

  // Both the save and the read after it fail; the title is then asked for once more.
  async offline() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(-1) });
    p.press(set, 3);
    await p.fail();
    await p.fail();
    const level = p.R.get('movie-1');
    p.log.length = 0;
    p.R.read(['movie-1']);
    await p.go();
    await p.ratings({ 'movie-1': lv(2) });
    p.R.read(['movie-1']);
    await p.go();
    return { level, log: p.log, then: p.R.get('movie-1') };
  },

  // The first answer is lost, the second press is refused.
  async lostThenRefused() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null) });
    p.press(set, 3); p.press(set, 0);
    await p.fail();
    await p.refuse();
    return p.log.slice(3);
  },

  // Two refs of one title pressed while neither has settled; the second is then saved or refused.
  async twins() {
    const run = async (end) => {
      const p = load();
      const a = p.R.mount('movie-1');
      const b = p.R.mount('tmdb-movie-7');
      await p.go();
      await p.ratings({ 'movie-1': lv(null), 'tmdb-movie-7': lv(null) });
      p.press(a, 3); p.press(b, 0);
      await p.title(7);
      const out = { between: p.look(b).on };
      await p[end](7);
      out.end = [p.look(a).on, p.look(b).on];
      p.press(a, 1);
      out.next = [p.look(a).on, p.look(b).on];
      return out;
    };
    return { saved: await run('title'), refused: await run('refuse') };
  },

  // As twins, but the first ref is pressed again before the second is refused.
  async twinsBusy() {
    const p = load();
    const a = p.R.mount('movie-1');
    const b = p.R.mount('tmdb-movie-7');
    await p.go();
    await p.ratings({ 'movie-1': lv(null), 'tmdb-movie-7': lv(null) });
    p.press(a, 3); p.press(b, 0);
    await p.title(7);
    p.press(a, 1);
    await p.refuse();
    const asked = p.log.slice(3);
    await p.title(7);
    return { asked, end: [p.look(a).on, p.look(b).on] };
  },

  // The first read answers while a save is out, which is then refused.
  async readDuringSave() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    p.press(set, 3);
    await p.ratings({ 'movie-1': lv(1) });
    const during = p.look(set).on;
    await p.refuse();
    return { during, after: p.look(set).on };
  },

  // Pressed before the first read answers, with 2 stored. What each event says the level was.
  async unread() {
    const wases = (p) => p.events.map((e) => [e.phase, e.level, e.was]);
    const run = async (steps) => {
      const p = load();
      const set = p.R.mount('movie-1');
      await p.go();
      p.press(set, 0);
      p.swap(); // the save is answered before the read
      await steps(p, set);
      return wases(p);
    };
    return {
      saved: await run((p) => p.title(7, 2)),
      noRead: await run(async (p) => { p.swap(); await p.refuse(); await p.title(7, 2); }),
      quick: await run(async (p, set) => { p.press(set, 2); p.press(set, 0); p.press(set, 1); await p.title(7, 2); p.swap(); await p.title(7, -2); }),
      // Stored, its answer lost, and the read after it answers what was stored.
      lost: await run(async (p) => { await p.fail(); await p.ratings({ 'movie-1': lv(null) }); await p.ratings({ 'movie-1': lv(-2) }); }),
      lostAgain: await run(async (p, set) => { await p.fail(); await p.refuse(); p.press(set, 0); p.swap(); await p.title(7, -2); }),
      lostNotHeld: await run(async (p, set) => { await p.fail(); await p.refuse(); p.press(set, 0); p.swap(); await p.title(7, 2); }),
    };
  },

  // The level was known: the save's answer is lost, as is the read after it, and it is saved at the next press.
  async knownLost() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(1) });
    p.press(set, 0);
    await p.fail();
    await p.fail();
    p.press(set, 0);
    await p.title(7, -2);
    return p.events.map((e) => [e.phase, e.level, e.was]);
  },

  // Saved, then pressed on at once to one that is lost and to one saved after.
  async laterLost() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(1) });
    p.press(set, 3); p.press(set, 1);
    await p.title(7, 1);
    await p.fail();
    await p.fail();
    p.press(set, 0);
    await p.title(7, -1);
    return p.events.map((e) => [e.phase, e.level, e.was]).pop();
  },

  // Stored though its answer was lost, read so, then changed.
  async lostThenRead() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(1) });
    p.press(set, 0);
    await p.fail();
    await p.ratings({ 'movie-1': lv(-2) });
    p.press(set, 3);
    await p.title(7, -2);
    return p.events.map((e) => [e.phase, e.level, e.was]).pop();
  },

  // An Undo of which the level before is not known.
  async undoUnknown() {
    const p = load();
    const set = p.R.mount('movie-1');
    p.press(set, 0);
    await p.fail();
    p.log.length = 0;
    p.R.set('movie-1', undefined);
    await p.go();
    return { log: p.log, on: p.look(set).on };
  },

  // A set that has gone; another title is then saved.
  async destroyed() {
    const p = load();
    const gone = p.R.mount('movie-2', { variant: 'menu' });
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null), 'movie-2': lv(null) });
    gone.destroy();
    gone.destroy();
    p.log.length = 0;
    p.press(set, 3);
    await p.title(7);
    return p.log;
  },

  async kept() {
    const p = load();
    const set = p.R.mount('movie-1');
    await p.go();
    await p.ratings({ 'movie-1': lv(null) });
    p.listeners.pageshow({ persisted: true });
    await p.go();
    await p.ratings({ 'movie-1': lv(1) });
    return p.look(set);
  },

  async unmatched() {
    const p = load();
    const set = p.R.mount('movie-1');
    p.press(set, 3);
    await p.answer({ ok: false, error: 'no_tmdb_id', message: 'No match.' }, 409);
    return p.look(set);
  },

  async roles() {
    const p = load();
    const of = (variant) => {
      const set = p.R.mount('movie-1', { variant });
      const b = set.node.querySelectorAll('button')[0];
      const g = set.node.querySelector('.rate__set');
      return [g.getAttribute('role'), g.getAttribute('aria-label'), b.getAttribute('role'), b.getAttribute('aria-label'),
        b.getAttribute('aria-pressed'), b.getAttribute('aria-checked'), b.getAttribute('tabindex'), set.line.getAttribute('role')];
    };
    return { plain: of('plain'), menu: of('menu') };
  },

  // An Undo from elsewhere, and a set that is gone.
  async set() {
    const p = load();
    const a = p.R.mount('movie-1');
    const gone = p.R.mount('movie-1');
    gone.destroy();
    p.R.set('movie-1', -2);
    return { a: p.look(a).on, gone: p.look(gone).on, log: p.log, event: p.events[0] };
  },
};

(async () => {
  console.log(JSON.stringify(await scenarios[process.argv[2]]()));
})().catch((e) => { console.error(e); process.exit(1); });
