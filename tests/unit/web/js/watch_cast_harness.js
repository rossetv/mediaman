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
    cancelAnimationFrame: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    matchMedia: () => ({ matches: !motion }), // reduced motion: nothing flies
    performance: { now: () => clock.now },
    listeners: {},
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); },
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
  ['core/dom.js', 'watch/core.js', 'watch/explore.js', 'watch/carry.js'].forEach(run);
  Object.assign(ctx.MMWatch.explore, stubs.explore);
  ctx.MMWatch.detail = stubs.detail;
  run('watch/cast.js');
  root.appendChild(ctx.MMWatch.cast.section(cast));
  const q = (sel) => root.querySelector(sel);
  const item = (i) => root.querySelectorAll('.ttl-cast__item')[i];
  const fire = (type, e) => (root.listeners[type] || []).forEach((fn) => fn(e));
  const over = (target, kind, clientX, clientY) => fire('pointermove', { target, pointerType: kind || 'mouse', clientX, clientY });
  /* A wheel event over *target*: the window hears it first, then it goes up from the target. Says whether it was kept from scrolling. */
  const wheel = (target, deltaY, extra) => {
    const e = { target, deltaY, deltaX: 0, deltaMode: 0, timeStamp: clock.now, cancelable: true, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, ...extra };
    (ctx.listeners.wheel || []).slice().forEach((fn) => fn(e));
    for (let a = target; a; a = a.parentNode) (a.listeners.wheel || []).forEach((fn) => fn(e));
    return e.defaultPrevented;
  };
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
  return { ctx, doc, root, q, item, fire, over, wheel, wait, asked, answer, card, opened, people, cast: ctx.MMWatch.cast,
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

  // Scrolled down over an open card, a little at a time and then by a wheel's notch.
  async scrolledOpen() {
    const p = load();
    p.over(p.item(0));
    await p.wait(450);
    const pane = () => p.q('.cast-card__pane');
    p.over(pane(), 'mouse', 100, 100);
    const kept = [];
    for (let i = 0; i < 4; i++) { kept.push(p.wheel(pane(), 30)); await p.wait(10); }
    const short = p.people.length;
    kept.push(p.wheel(pane(), 30));
    const after = p.people.map(([who]) => who.name);
    await p.wait(10);
    const rest = p.wheel(p.doc.getElementById('watch-results'), 40);
    await p.wait(1000);
    const fresh = p.wheel(p.doc.getElementById('watch-results'), 40);
    p.over(p.item(1));
    await p.wait(450);
    p.over(pane(), 'mouse', 100, 100);
    p.wheel(pane(), 3, { deltaMode: 1 });
    return { kept, short, after, rest, fresh, notch: p.people.map(([who]) => who.name) };
  },

  // Where things may move, the card goes with the scroll: it rises as the scroll goes on, settles back when it
  // stops short, and opens their page once the scroll has gone far enough.
  async scrolledCarried() {
    const p = load(CAST, true);
    let queued = [];
    p.ctx.requestAnimationFrame = (fn) => { queued.push(fn); return queued.length; };
    p.ctx.cancelAnimationFrame = () => { queued = []; };
    const frames = async (n) => {
      for (let i = 0; i < n; i++) {
        await p.wait(16);
        const now = queued.splice(0);
        now.forEach((fn) => fn(p.ctx.performance.now()));
      }
    };
    const risen = () => p.q('.cast-card').style.scale || '';
    p.over(p.item(0));
    await p.wait(300);
    await frames(10);
    const pane = () => p.q('.cast-card__pane');
    p.over(pane(), 'mouse', 100, 100);
    p.wheel(pane(), 40);
    await frames(4);
    const going = { scale: Number(risen()) > 1, opened: p.people.length };
    await frames(40);
    const back = { scale: risen(), opened: p.people.length };
    for (let i = 0; i < 4; i++) { p.wheel(pane(), 40); await frames(1); }
    await frames(40);
    return { going, back, opened: p.people.map(([who]) => who.name) };
  },

  // A card standing well inside the detail, carried part of the way: where its middle is drawn against where it stands.
  async carriedAtAnOffset() {
    const p = load(CAST, true);
    let queued = [];
    p.ctx.requestAnimationFrame = (fn) => { queued.push(fn); return queued.length; };
    p.ctx.cancelAnimationFrame = () => { queued = []; };
    const frames = async (n) => {
      for (let i = 0; i < n; i++) {
        await p.wait(16);
        queued.splice(0).forEach((fn) => fn(p.ctx.performance.now()));
      }
    };
    p.root.clientWidth = 1400;
    p.root.clientHeight = 900;
    const at = (r) => () => ({ left: r[0], top: r[1], width: r[2], height: r[3], right: r[0] + r[2], bottom: r[1] + r[3] });
    p.item(0).querySelector('.ttl-cast__avatar').getBoundingClientRect = at([600, 500, 64, 64]);
    p.item(0).getBoundingClientRect = at([590, 496, 84, 110]);
    p.over(p.item(0));
    await p.wait(300);
    await frames(10);
    const card = p.q('.cast-card');
    const to = card.style['--to'].match(/-?[\d.]+/g).map(Number);
    const pane = p.q('.cast-card__pane');
    p.over(pane, 'mouse', 100, 100);
    p.wheel(pane, 60);
    await frames(6);
    const s = Number(card.style.scale);
    const t = card.style.translate.match(/-?[\d.]+/g).map(Number);
    const w = card.offsetWidth;
    const h = p.q('.cast-card__shell').offsetHeight;
    // The card's own middle, drawn: translate, then scale about the scroller's corner, then the place it stands.
    const middle = [t[0] + s * (to[0] + w / 2), t[1] + s * (to[1] + h / 2)];
    return { grown: s > 1, at: (s - 1) / 0.1, stands: [to[0] + w / 2, to[1] + h / 2], middle };
  },

  // A carried card that opens their page: in the frame it goes, its photo is already in the air and the rest of it
  // is still there, fading, with no photo of its own under the flying one; once faded it is gone.
  async carriedHandOff() {
    const p = load(CAST, true);
    let queued = [];
    p.ctx.requestAnimationFrame = (fn) => { queued.push(fn); return queued.length; };
    p.ctx.cancelAnimationFrame = () => { queued = []; };
    const frames = async (n) => {
      for (let i = 0; i < n; i++) {
        await p.wait(16);
        queued.splice(0).forEach((fn) => fn(p.ctx.performance.now()));
      }
    };
    const fades = [];
    Node.prototype.animate = function (keys, opts) {
      const a = { node: this, keys, opts, onfinish: null };
      fades.push(a);
      return a;
    };
    // The photo stays in the air for as long as the test looks.
    Node.prototype.getAnimations = function () {
      return this.classList.contains('cast-fly') ? [{ finished: new Promise(() => {}) }] : [];
    };
    try {
      p.ctx.MMWatch.explore.openPerson = () => {
        p.doc.getElementById('watch-results').appendChild(build(p.doc, ['div', { class: 'results-page' },
          ['h1', { class: 'explore-title' }, 'Mara Ellison'], ['span', { class: 'explore-photo' }]]));
      };
      p.over(p.item(0));
      await p.wait(300);
      await frames(10);
      const pane = p.q('.cast-card__pane');
      p.over(pane, 'mouse', 100, 100);
      let handed = null;
      for (let i = 0; i < 80 && !handed; i++) {
        if (i < 4) p.wheel(pane, 40);
        await frames(1);
        if (p.doc.querySelectorAll('.cast-fly').length) {
          const left = p.root.querySelectorAll('.cast-card').filter((c) => c.classList.contains('is-open') && !c.hidden);
          const fade = fades.filter((a) => left.includes(a.node))[0];
          handed = {
            flying: true,
            left: left.length,
            ownPhoto: left.map((c) => c.querySelectorAll('.cast-face').filter((f) => f.style.visibility !== 'hidden').length),
            fadesOut: !!fade && fade.keys[fade.keys.length - 1].opacity === 0,
            ms: fade ? fade.opts.duration : null,
          };
          if (fade && fade.onfinish) fade.onfinish();
          handed.afterwards = p.root.querySelectorAll('.cast-card').filter((c) => !c.hidden).length;
        }
      }
      return handed;
    } finally {
      delete Node.prototype.animate;
      delete Node.prototype.getAnimations;
    }
  },

  // The same hand-off once what is known of them has come in late: the copy left behind, the photo in the air, and their clocks.
  // *gap*: how long the frame after the first one takes.
  async carriedHandOffSettled(gap = 150) {
    const p = load(CAST, true);
    let queued = [];
    p.ctx.requestAnimationFrame = (fn) => { queued.push(fn); return queued.length; };
    p.ctx.cancelAnimationFrame = () => { queued = []; };
    const frame = async (ms) => {
      await p.wait(ms);
      queued.splice(0).forEach((fn) => fn(p.ctx.performance.now()));
    };
    p.doc.timeline = { get currentTime() { return p.ctx.performance.now(); } };
    const fades = [];
    Node.prototype.animate = function (keys, opts) {
      const a = { node: this, keys, opts, onfinish: null, startTime: 0, get playState() { return this.startTime === null ? 'paused' : 'running'; } };
      fades.push(a);
      this.fading = a;
      return a;
    };
    Node.prototype.getAnimations = function () {
      if (this.fading) return [this.fading];
      if (!this.classList.contains('cast-fly')) return [];
      this.moving = this.moving || { finished: new Promise(() => {}), startTime: 0, get playState() { return this.startTime === null ? 'paused' : 'running'; } };
      return [this.moving];
    };
    try {
      p.ctx.MMWatch.explore.openPerson = () => {
        p.doc.getElementById('watch-results').appendChild(build(p.doc, ['div', { class: 'results-page' },
          ['h1', { class: 'explore-title' }, 'Mara Ellison'], ['span', { class: 'explore-photo' }]]));
      };
      p.over(p.item(0));
      await p.wait(300);
      await frame(16);
      await p.wait(16);
      await p.answer('9001', reply());
      for (let i = 0; i < 10; i++) await frame(16);
      const pane = p.q('.cast-card__pane');
      const arriving = pane.querySelectorAll('.is-arriving').length;
      const face = pane.querySelector('.cast-face');
      p.over(pane, 'mouse', 100, 100);
      for (let i = 0; i < 80 && !p.doc.querySelector('.cast-fly'); i++) {
        if (i < 4) p.wheel(pane, 40);
        await frame(16);
      }
      const fly = p.doc.querySelector('.cast-fly');
      if (!fly) return null;
      const ghost = p.root.querySelectorAll('.cast-card').filter((c) => !c.hidden)[0];
      const fade = fades.filter((a) => a.node === ghost)[0];
      const clocks = () => [fade.startTime, fly.getAnimations()[0].startTime];
      const held = clocks();
      await frame(16);
      const first = { at: p.ctx.performance.now(), starts: clocks() };
      await frame(gap);
      const slow = clocks();
      await frame(16);
      return {
        arriving,
        stillArriving: ghost.querySelectorAll('.in, .is-arriving, .is-new').length,
        ownPhoto: fly === face,
        heldAtStart: held,
        first: first.starts.map((s) => s - first.at),
        heldBy: slow.map((s, i) => s - first.starts[i]),
        then: clocks().map((s, i) => s - slow[i]),
      };
    } finally {
      delete Node.prototype.animate;
      delete Node.prototype.getAnimations;
    }
  },

  // A card with no room above or under the person stands over the photo the pointer rests on: a scroll there is the page's.
  async scrolledOnThePhoto() {
    const p = load();
    const pane = () => p.q('.cast-card__pane');
    p.over(p.item(0), 'mouse', 10, 10);
    await p.wait(450);
    p.over(pane(), 'mouse', 10, 10);
    const still = p.wheel(pane(), 200);
    const opened = p.people.length;
    await p.wait(400);
    p.over(pane(), 'mouse', 100, 100);
    const moved = p.wheel(pane(), 200);
    return { still, opened, moved, then: p.people.length };
  },

  // Scrolls that are the page's: begun off the card, up, sideways, with a key, or as the card has just opened.
  async scrolledPast() {
    const p = load();
    const pane = () => p.q('.cast-card__pane');
    p.over(p.item(0));
    await p.wait(290);
    p.over(pane(), 'mouse', 100, 100);
    const early = p.wheel(pane(), 200);
    await p.wait(400);
    const out = { early };
    p.wheel(p.q('.ttl__sec-title'), 30);
    await p.wait(10);
    out.passing = p.wheel(pane(), 200);
    await p.wait(400);
    out.up = p.wheel(pane(), -200);
    await p.wait(400);
    out.sideways = p.wheel(pane(), 20, { deltaX: 200 });
    await p.wait(400);
    out.zoom = p.wheel(pane(), 200, { ctrlKey: true });
    await p.wait(400);
    return Object.assign(out, { people: p.people.length, card: !!p.card() });
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

  // The page is told when the photo flying to it lands, and at once where nothing flies.
  async landing() {
    const told = async (motion) => {
      const p = load(CAST, motion);
      const trips = [];
      p.item(0).querySelector('.ttl-cast__avatar').offsetLeft = p.item(0).querySelector('.ttl-cast__avatar').offsetTop = 0;
      Node.prototype.getAnimations = function () {
        if (!this.classList.contains('cast-fly')) return [];
        return [{ finished: new Promise((resolve) => trips.push(resolve)) }];
      };
      let landed = false;
      try {
        p.ctx.MMWatch.explore.openPerson = (who, from, landing) => {
          landing.then(() => { landed = true; });
          p.doc.getElementById('watch-results').appendChild(build(p.doc, ['div', { class: 'results-page' },
            ['h1', { class: 'explore-title' }, 'Mara Ellison'], ['span', { class: 'explore-photo' }]]));
        };
        click(p.doc, p.item(0));
        await settle();
        const flying = landed;
        trips.forEach((go) => go());
        await settle();
        return [flying, landed];
      } finally {
        delete Node.prototype.getAnimations;
      }
    };
    return { flies: await told(true), still: await told(false) };
  },
};

// How long each of a steady 30 fps frame and a stall a little longer held the hand-off back.
scenarios.handOffGaps = async () => {
  const held = async (gap) => (await scenarios.carriedHandOffSettled(gap)).heldBy;
  return { thirty: await held(1000 / 30), stall: await held(50) };
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
