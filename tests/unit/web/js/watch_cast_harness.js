// Runs watch/core.js and watch/cast.js against the fake DOM with a stepped clock, and prints one
// scenario's outcome as JSON. Usage: node watch_cast_harness.js <scenario>
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); },
});
Node.prototype.focus = function () { this.ownerDocument.activeElement = this; };
Object.defineProperty(Node.prototype, 'lastElementChild', { get() { return this.children[this.children.length - 1] || null; } });

const CAST = ['Mara Ellison', 'Tomas Adeyemi', 'Iris Lindqvist', 'Hugh Pell'].map((name, i) => (
  { name, character: 'Part ' + i, profile_url: i === 3 ? null : '/api/watch/img/tmdb/w185/' + i + '.jpg', person_id: 9001 + i }));

function reply(over) {
  return {
    ok: true, tmdb_configured: true, total: 9, in_library: 2, next_page: null, items: [],
    person: Object.assign({ person_id: 9001, name: 'Mara Ellison', job: 'Actor', profile_url: null, photo_url: null,
      birthday: '1979-03-02', deathday: null, age: 47, biography: 'Trained in <b>Leeds</b>.' }, over),
    known_for: [
      { ref: 'tmdb-movie-7', library_ref: 'movie-12', title: 'Undertow', kind: 'movie', poster_url: '/api/poster/12' },
      { ref: 'tmdb-tv-8', library_ref: null, title: 'Low Water', kind: 'show', poster_url: null }],
  };
}

function load(cast = CAST, motion = false) {
  const doc = makeDocument();
  const clock = { now: Date.UTC(2026, 10, 24, 12) };
  let timers = [];
  let id = 0;
  const asked = [];
  const waiting = {};
  const opened = [];
  const people = [];
  const page = build(doc, ['main', {}, ['section', { id: 'watch-results' }], ['div', { id: 'ttl' }]]);
  doc.body.appendChild(page);
  const root = doc.getElementById('ttl');
  root.scrollTop = 0;
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Number, Math, Promise, Infinity,
    Date: class extends Date { static now() { return clock.now; } },
    setTimeout: (fn, ms) => { timers.push({ id: ++id, fn, at: clock.now + (ms || 0) }); return id; },
    clearTimeout: (n) => { timers = timers.filter((t) => t.id !== n); },
    requestAnimationFrame: (fn) => fn(),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: !motion }), // reduced motion: nothing flies
    addEventListener: () => {},
    innerHeight: 900,
    MMWatch: {
      address: { layer() {} },
    },
  };
  const stubs = {
    explore: {
      person: (n) => { asked.push(n); return new Promise((resolve, reject) => { waiting[n] = { resolve, reject }; }); },
      openPerson: (who, from) => people.push([who, from]),
    },
    detail: { open: (ref, o) => opened.push([ref, o]) },
  };
  ctx.window = ctx;
  Object.defineProperty(Object.getPrototypeOf(root.style), 'setProperty', { value() {}, configurable: true });
  vm.createContext(ctx);
  const run = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'watch/core.js', 'watch/explore.js'].forEach(run);
  Object.assign(ctx.MMWatch.explore, stubs.explore);
  ctx.MMWatch.detail = stubs.detail;
  run('watch/cast.js');
  root.appendChild(ctx.MMWatch.cast.section(cast));
  const q = (sel) => root.querySelector(sel);
  const item = (i) => root.querySelectorAll('.ttl-cast__item')[i];
  const fire = (type, e) => (root.listeners[type] || []).forEach((fn) => fn(e));
  const over = (target, kind) => fire('pointermove', { target, pointerType: kind || 'mouse' });
  async function wait(ms) {
    const end = clock.now + ms;
    for (;;) {
      const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
      if (!due) break;
      timers = timers.filter((t) => t !== due);
      clock.now = due.at;
      due.fn();
    }
    clock.now = end;
    await settle();
  }
  const answer = async (n, r) => { if (r instanceof Error) waiting[n].reject(r); else waiting[n].resolve(r); await settle(); };
  /* What the card shows, as far as it is open. */
  const card = () => {
    const c = q('.cast-card');
    if (!c.classList.contains('is-open')) return null;
    const pane = c.querySelectorAll('.cast-card__pane').filter((n) => !n.classList.contains('is-old')).pop();
    const text = (sel) => { const n = pane.querySelector(sel); return n ? n.textContent : null; };
    return { name: text('.cast-card__name'), role: text('.cast-card__role'), born: text('.cast-card__born'), bio: text('.cast-card__bio'),
      known: pane.querySelectorAll('button.cast-card__poster').map((b) => b.getAttribute('aria-label')),
      failed: text('.cast-card__failed'), loading: pane.querySelectorAll('.skel').length > 0 };
  };
  return { ctx, doc, root, q, item, fire, over, wait, asked, answer, card, opened, people, cast: ctx.MMWatch.cast,
    words: () => q('#ttl-cast-words').textContent };
}

const scenarios = {
  // The pointer goes along the whole strip without resting, then off it.
  async sweep() {
    const p = load();
    for (let n = 0; n < 3; n++) for (let i = 0; i < 4; i++) { p.over(p.item(i)); await p.wait(90); }
    p.over(p.q('.ttl__sec-title'));
    await p.wait(2000);
    return { asked: p.asked, card: p.card() };
  },

  // Resting on a person: the card at once, what is known of them, and the same card again later.
  async rest() {
    const p = load();
    p.over(p.item(0));
    await p.wait(300);
    const loading = p.card();
    await p.answer('9001', reply());
    const full = p.card();
    const described = p.item(0).getAttribute('aria-describedby') === 'ttl-cast-words' ? p.words() : null;
    p.over(p.q('.ttl__sec-title'));
    await p.wait(1000);
    const left = p.card();
    p.over(p.item(0));
    await p.wait(300);
    return { loading, full, described, markup: p.root.querySelectorAll('.cast-card__bio b').length, left, again: p.card(), asked: p.asked };
  },

  // From one person to others along the strip while the first one's answer is still on its way.
  async pass() {
    const p = load();
    p.over(p.item(0));
    await p.wait(300);
    p.over(p.item(1));
    await p.wait(400);
    const second = p.card().name;
    const early = p.asked.slice();
    p.over(p.item(2));
    await p.wait(400);
    const before = p.asked.slice();
    await p.answer('9001', reply());
    const late = p.card();
    await p.answer('9003', reply({ biography: 'Someone else.' }));
    return { second, early, before, late, asked: p.asked, then: p.card().bio };
  },

  // With a card open, a person only passed over is not asked about.
  async brush() {
    const p = load();
    p.over(p.item(0));
    await p.wait(300);
    await p.answer('9001', reply());
    p.over(p.item(1));
    await p.wait(100);
    const passed = p.card().name;
    p.over(p.item(2));
    await p.wait(100);
    p.over(p.q('.ttl__sec-title'));
    await p.wait(2000);
    return { passed, asked: p.asked };
  },

  // An answer that fails, the person come back to, and asked about again by a press.
  async failed() {
    const p = load();
    p.over(p.item(0));
    await p.wait(300);
    await p.answer('9001', new Error('502'));
    const failed = p.card();
    p.over(p.q('.ttl__sec-title'));
    await p.wait(1000);
    p.over(p.item(0));
    await p.wait(300);
    const back = { failed: p.card().failed !== null, asked: p.asked.length };
    click(p.doc, p.q('.cast-card__again'));
    const again = { loading: p.card().loading, asked: p.asked.length };
    await p.answer('9001', reply());
    const gone = load();
    gone.over(gone.item(0));
    await gone.wait(300);
    await gone.answer('9001', Object.assign(new Error('404'), { status: 404 }));
    return { failed, back, again, then: p.card().bio, people: p.people.length, retryWhenGone: !!gone.q('.cast-card__again') };
  },

  // What little is known: their years, a biography and titles that are not there.
  async little() {
    const out = [];
    for (const [person, known] of [
      [{ deathday: '2019-05-01', age: 70, birthday: '1949-01-01' }],
      [{ birthday: null, age: null }],
      [{ birthday: '1990-01-01', age: null }],
      [{ biography: '' }, []]]) {
      const p = load();
      p.over(p.item(0));
      await p.wait(300);
      await p.answer('9001', Object.assign(reply(person), known ? { known_for: known } : {}));
      const c = p.card();
      out.push(known ? { bio: c.bio, none: p.q('.cast-card__known').textContent } : c.born);
    }
    return out;
  },

  // A touch, a press, and a press on what the card holds.
  async presses() {
    const p = load();
    p.over(p.item(0), 'touch');
    await p.wait(1000);
    const touch = p.card();
    click(p.doc, p.item(0));
    const tapped = p.people.slice();
    await p.wait(300);
    p.over(p.item(1));
    await p.wait(300);
    await p.answer('9002', reply({ name: 'Tomas Adeyemi', job: 'Director' }));
    click(p.doc, p.q('button.cast-card__poster'));
    const poster = { opened: p.opened.slice(), card: p.card(), people: p.people.length };
    await p.wait(1000);
    p.over(p.item(1));
    await p.wait(400);
    click(p.doc, p.q('.cast-card__all'));
    return { touch, tapped, poster, all: p.people[1], card: p.card(), asked: p.asked.length };
  },

  // Someone TMDB gives no id for, and the same strip by the keyboard.
  async unknownAndKeys() {
    const p = load([{ name: 'Ada Rimmington', character: null, profile_url: null, person_id: null }].concat(CAST));
    const plain = { tag: p.item(0).tagName, tabbable: p.item(0).hasAttribute('tabindex') };
    p.over(p.item(0));
    await p.wait(1000);
    plain.card = p.card();
    p.item(1).focus();
    p.fire('focusin', { target: p.item(1) });
    await p.wait(300);
    const focused = p.card().name;
    const first = p.cast.dismiss();
    return { plain, focused, first, card: p.card(), second: p.cast.dismiss() };
  },

  // The strip drawn again under an open card (a mark, a Keep).
  async redrawn() {
    const p = load();
    p.over(p.item(0));
    await p.wait(300);
    p.q('.ttl__castsec').replaceWith(p.cast.section(CAST));
    await p.answer('9001', reply());
    const gone = p.card();
    p.over(p.item(0));
    await p.wait(300);
    return { gone, again: p.card().bio, asked: p.asked };
  },

  // Scrolled under a pointer that does not move, then moved along the strip once that has settled.
  async scrolled() {
    const p = load();
    p.fire('scroll', {});
    p.over(p.item(0));
    await p.wait(150);
    const soon = p.card();
    await p.wait(400);
    const still = p.card();
    p.over(p.item(0));
    await p.wait(300);
    return { soon, still, then: !!p.card() };
  },

  // The pointer has left a person by the time a busy page comes to open their card.
  async leftBeforeTheFrame() {
    const p = load();
    const frames = [];
    p.ctx.requestAnimationFrame = (fn) => frames.push(fn);
    p.over(p.item(0));
    await p.wait(300);
    p.over(p.q('.ttl__sec-title'));
    frames.splice(0).forEach((fn) => fn());
    const left = p.card();
    p.over(p.item(1));
    await p.wait(300);
    frames.splice(0).forEach((fn) => fn());
    await p.wait(10);
    return { left, rested: p.card().name, asked: p.asked };
  },

  // A photo on its way to the page is called home: the trip out ending says nothing of the trip home.
  async turnedRound() {
    const p = load(CAST, true);
    const trips = [];
    p.item(0).querySelector('.ttl-cast__avatar').offsetLeft = p.item(0).querySelector('.ttl-cast__avatar').offsetTop = 0;
    Node.prototype.getAnimations = function () {
      if (!this.classList.contains('cast-fly')) return [];
      return [{ finished: new Promise((resolve) => trips.push(resolve)) }];
    };
    try {
      p.ctx.MMWatch.explore.openPerson = () => {
        p.doc.getElementById('watch-results').appendChild(build(p.doc, ['div', { class: 'results-page' },
          ['h1', { class: 'explore-title' }, 'Mara Ellison'], ['span', { class: 'explore-photo' }]]));
      };
      click(p.doc, p.item(0));
      const flying = () => p.doc.querySelectorAll('.cast-fly').length;
      const out = [flying()];
      p.doc.querySelector('.results-page').classList.add('is-leaving');
      p.cast.land('cast-0');
      out.push(trips.length);
      trips[0]();
      await settle();
      out.push(flying(), p.item(0).classList.contains('is-out'), p.item(0).classList.contains('is-away'));
      trips[1]();
      await settle();
      out.push(flying(), p.item(0).classList.contains('is-out'), p.item(0).classList.contains('is-away'));
      return out;
    } finally {
      delete Node.prototype.getAnimations;
    }
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
