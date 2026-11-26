// Runs the real /downloads scripts over the page as the server drew it (on stdin, as nested
// [tag, attrs, ...children] arrays) and prints one scenario's outcome as JSON.
// Usage: node downloads_live_harness.js <scenario> < page.json
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Node, makeDocument, build, click, settle } = require('./fake_dom');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

// What the browser reflects or does by itself and the fake DOM does not.
Object.defineProperty(Node.prototype, 'hidden', {
  get() { return this.hasAttribute('hidden'); },
  set(on) { this.toggleAttribute('hidden', !!on); },
});
['src', 'href', 'alt', 'type'].forEach((k) => Object.defineProperty(Node.prototype, k, {
  get() { const v = this.getAttribute(k); return v && v.startsWith('/') ? 'http://app' + v : v; },
  set(v) { this.setAttribute(k, v); },
}));
Object.defineProperty(Node.prototype, 'dataset', {
  get() {
    const el = this;
    const attr = (k) => 'data-' + k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
    return new Proxy({}, {
      get: (_t, k) => { const v = el.getAttribute(attr(k)); return v === null ? undefined : v; },
      set: (_t, k, v) => { el.setAttribute(attr(k), v); return true; },
      deleteProperty: (_t, k) => { el.removeAttribute(attr(k)); return true; },
    });
  },
});
Node.prototype.matches = (function (matches) {
  return function (sel) {
    const heading = sel.match(/^(.*?)\s*\bh(\d)$/);
    if (!heading) return matches.call(this, sel);
    return this.tagName === 'H' + heading[2] && (!heading[1] || !!this.parentNode.closest(heading[1]));
  };
})(Node.prototype.matches);
Node.prototype.removeChild = function (c) { c.remove(); return c; };
Node.prototype.replaceChildren = function () { [...this.childNodes].forEach((c) => c.remove()); };
Node.prototype.remove = (function (remove) {
  return function () {
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    remove.call(this);
  };
})(Node.prototype.remove);
Node.prototype.focus = function () { if (this.isConnected && !this.closest('[hidden]')) this.ownerDocument.activeElement = this; };

const shownText = (n) => (n.nodeType === 3 ? n._text : n.hidden ? '' : n.childNodes.map(shownText).join(''));

function page(spec, opts) {
  const doc = makeDocument();
  spec.forEach((s) => doc.body.appendChild(build(doc, s)));
  doc.hidden = false;
  const style = (el) => {
    const set = {};
    el.style = new Proxy(set, {
      get: (t, k) => (k === 'setProperty' ? (n, v) => { t[n] = v; } : k === 'removeProperty' ? (n) => { delete t[n]; } : k in t ? t[k] : ''),
      set: (t, k, v) => { t[k] = v; return true; },
    });
  };
  [doc.body, ...doc.body.descendants()].forEach(style);
  const create = doc.createElement;
  doc.createElement = (t) => { const el = create(t); style(el); return el; };
  const timers = [];
  const ctx = {
    document: doc, console, MM: {}, Promise, JSON, CustomEvent: class { constructor(t) { this.type = t; } },
    setTimeout: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; },
    clearTimeout: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
    AbortController, matchMedia: () => ({ matches: !(opts && opts.motion) }),
    getComputedStyle: () => ({}), location: { href: '/downloads' },
    toasts: [],
  };
  ctx.window = ctx;  ctx.addEventListener = () => {};
  ctx.UIFeedback = { toast: (text, o) => ctx.toasts.push([text, (o || {}).variant]), error: (text) => ctx.toasts.push([text, 'error']) };
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).forEach((f) => f(e));
  ctx.MM.api = { get: opts && opts.get, post: opts && opts.post, messageOf: (_e, m) => m };
  vm.createContext(ctx);
  if (opts && opts.ring) ctx.MM.downloading = opts.ring;
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  ['core/dom.js', 'core/modal.js', 'modal-a11y.js', 'dl-poster.js', 'broken-poster.js', 'downloads/build_dom.js',
    'downloads/render_hero.js', 'downloads/render_row.js', 'downloads/render_recent.js'].forEach(load);
  if (opts && opts.get) load('downloads/poll.js');
  else ctx.MM.downloads.poll = { start: (cb) => { ctx.update = cb; } };
  load('downloads.js');
  load('dl-abandon.js');
  return {
    ctx, doc, timers, q: (s) => doc.querySelector(s), qa: (s) => doc.querySelectorAll(s),
    upd: (d) => ctx.update({ subtitle: '', hero: null, queue: [], upcoming: [], recent: [], unreachable: [], outage: '', ...d }),
    text: (s) => { const e = doc.querySelector(s); return e && shownText(e).replace(/\s+/g, ' ').trim(); },
  };
}

const item = (id, extra) => ({
  id, title: 'T' + id, media_type: 'movie', poster_url: '', state: 'downloading', state_label: 'Downloading', progress: 0,
  eta: '', size_done: '', size_total: '', episodes: null, episode_summary: '', search_hint: '', arr_link: '', arr_source: '',
  abandon_visible: false, stuck_seasons: [], release_label: '', ...extra,
});

const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const scenarios = {
  // a row the server drew before its download had begun
  async rowBegins() {
    const p = page(spec);
    const right = () => ['pct', 'eta'].map((v) => p.text(`.dl-compact [data-v="${v}"]`));
    p.upd({ hero: item('h'), queue: [item('q', { progress: 37, eta: '~4 min' })] });
    const begun = right();
    const quiet = () => p.q('.dl-compact .dl-compact-pct').classList.contains('dl-compact-pct--muted');
    const lit = !quiet();
    p.upd({ hero: item('h'), queue: [item('q')] });
    return { begun, lit, back: right(), quietAgain: quiet() };
  },

  // a queued show whose episodes change after the server drew it
  async rowEpisodes() {
    const p = page(spec);
    const ep = (label, extra) => ({ label, title: 'Title ' + label, state: 'searching', progress: 0, is_pack_episode: false, ...extra });
    const poll = (episodes, extra) => p.upd({ hero: item('h'), queue: [item('q', { media_type: 'series', episodes, ...extra })] });
    const look = () => ({
      rows: p.qa('.dl-compact .dl-ep-row').map((r) => r.children.map((c) => c.textContent.trim()).join('|')),
      toggle: p.text('.dl-compact [data-v="ep-toggle"]'), inline: p.qa('.dl-compact [data-v="ep-count"]').length,
      inside: p.qa('.dl-compact-row [data-v="ep-toggle"]').length + p.qa('.dl-compact-row [data-v="ep-list"]').length,
      open: !!p.q('.dl-compact [data-v="ep-list"]') && !p.q('.dl-compact [data-v="ep-list"]').hidden,
    });
    poll([ep('S01E01', { state: 'downloading', progress: 20 })]);
    const toggle = p.q('.dl-compact [data-v="ep-toggle"]');
    if (toggle) click(p.doc, toggle);
    const first = look();
    poll([ep('S01E01', { state: 'ready', progress: 100 }), ep('S01E02', { title: 'Named late', state: 'queued' })]);
    const more = look();
    poll([ep('S01E01', { is_pack_episode: true }), ep('S01E02', { is_pack_episode: true })], { has_pack: true });
    const pack = look();
    poll([]);
    return { first, more, pack, none: look() };
  },

  // the art of the hero as one title follows another, and of a row whose poster comes late
  async art() {
    const p = page(spec);
    const art = () => [p.q('.dl-hero-poster img'), p.q('.dl-hero-bg')].map((e, i) =>
      (!e ? null : i ? e.style['background-image'] || null : e.getAttribute('src')));
    const drawn = art();
    const img = p.q('.dl-hero-poster img');
    p.upd({ hero: item('h', { poster_url: 'https://art/h.jpg' }), queue: [item('q')] });
    const kept = p.q('.dl-hero-poster img') === img;
    p.upd({ hero: item('b', { media_type: 'series' }), queue: [item('q', { poster_url: 'https://art/q.jpg' })] });
    const bare = { art: art(), pill: p.text('.dl-hero .pill'), row: p.q('.dl-compact-poster img') && p.q('.dl-compact-poster img').getAttribute('src') };
    p.upd({ hero: item('c', { poster_url: 'https://art/c.jpg' }) });
    return { drawn, kept, bare, next: art() };
  },

  // every poster fails to load, first the server's and then those the polls bring
  async broken() {
    const p = page(spec);
    const fail = () => {
      p.qa('img').forEach((img) => p.doc.listeners.error.forEach((f) => f({ target: img })));
      return p.qa('img').map((img) => img.hasAttribute('data-broken'));
    };
    const drawn = fail();
    const at = (n) => ({ poster_url: `https://art/${n}.jpg` });
    p.upd({ hero: item('b', at(1)), queue: [item('r', at(2))], upcoming: [item('v', at(3))], recent: [{ title: 'S', ...at(4) }] });
    const fresh = p.qa('img').map((img) => img.hasAttribute('data-broken'));
    return { drawn, fresh, polled: fail() };
  },

  // a person is on "View in ..." of the hero, then of a row, while the polls go on
  async hintFocus() {
    const p = page(spec);
    const searching = (id, hint, link) => item(id, { state: 'searching', search_hint: hint, arr_link: link, arr_source: 'Radarr' });
    const out = {};
    for (const [where, card] of [['hero', '.dl-hero'], ['row', '.dl-compact']]) {
      p.q(card + ' [data-v="arr-link"]').focus();
      const poll = (hint, link) => {
        p.upd({ hero: searching('h', hint, link), queue: [searching('q', hint, link)] });
        const on = p.doc.activeElement;
        return [on.getAttribute('href'), p.text(card + ' [data-v="search-hint"]')];
      };
      out[where] = [poll('Searched 2× · next attempt in 3m', 'http://arr/b'), poll('', 'http://arr/c')];
    }
    p.upd({ hero: searching('h', 'Searched once', ''), queue: [searching('n', 'Searched once', 'http://arr/n')] });
    out.noLink = [p.q('.dl-hero [data-v="arr-link"]').hidden, p.q('.dl-hero [data-v="search-hint-sep"]').hidden];
    out.built = p.text('.dl-compact [data-v="search-hint"]');
    out.dots = p.qa('.dl-hint-sep').length;
    return out;
  },

  // polls that fail, hang or are never heard back from
  // The header's ring is told by the page's own polls, whatever the page makes of them.
  async ring() {
    const told = [];
    const ring = { hear: (d) => told.push(d), lose: () => told.push('lost') };
    const answers = [{ hero: null, queue: [null], strip: 1 }, null, null, { hero: null, queue: [], upcoming: [], recent: [], subtitle: '', strip: 2 }];
    const p = page(spec, { ring, get: () => { const a = answers.shift(); return a ? Promise.resolve(a) : Promise.reject(new Error('down')); } });
    for (const ms of [2000, 2000, 4000, 8000]) { const t = p.timers.find((x) => x.ms === ms); p.timers.splice(p.timers.indexOf(t), 1); t.fn(); await settle(); }
    return told;
  },

  async lost() {
    const calls = [];
    const p = page(spec, { get: (_url, o) => new Promise((resolve, reject) => {
      calls.push({ resolve, reject });
      if (o && o.signal) o.signal.addEventListener('abort', () => reject(Object.assign(new Error('x'), { name: 'AbortError' })));
    }) });
    const run = async (ms) => { const t = p.timers.find((x) => x.ms === ms); if (!t) return false; p.timers.splice(p.timers.indexOf(t), 1); t.fn(); await settle(); return true; };
    const answer = async (n, data) => { calls[n].resolve({ subtitle: '1 item in progress', hero: item('h', { eta: '5 min' }), queue: [], upcoming: [], recent: [], unreachable: [], outage: '', ...data }); await settle(); };
    const fail = async (n) => { calls[n].reject(new Error('down')); await settle(); };
    const look = () => [p.text('.ph p'), p.qa('.dl-hero').length, p.qa('.dl-recent-item').length];
    const out = {};
    await run(2000); await fail(0);
    out.once = look();
    await run(4000); await fail(1);
    out.twice = look();
    await run(8000); await answer(2, { recent: undefined });
    out.back = look();
    await run(2000); await run(20000);
    out.late = look();
    await run(120000);
    out.retried = await run(4000) && calls.length === 5;
    await answer(4, { recent: undefined });
    out.backAgain = look();
    // put away while a poll is out, and taken out again
    await run(2000);
    p.doc.hidden = true; p.doc.listeners.visibilitychange.forEach((f) => f()); await settle();
    p.doc.hidden = false; p.doc.listeners.visibilitychange.forEach((f) => f()); await settle();
    out.shownAgain = [calls.length, look()[1]];
    return out;
  },

  // the width held for the page's scrollbar (15px here): with the abandon dialog open, opened
  // again for another press without a close between, and closed
  async held() {
    const p = page(spec, { post: () => new Promise(() => {}) });
    const root = p.doc.documentElement;
    const style = {};
    root.style = { setProperty(k, v) { style[k] = v; }, removeProperty(k) { delete style[k]; } };
    root.clientWidth = 1265;
    p.ctx.innerWidth = 1280;
    const trigger = p.q('[data-abandon-trigger]');
    click(p.doc, trigger);
    const open = style['--page-bar'];
    root.clientWidth = 1280;
    click(p.doc, trigger);
    const again = style['--page-bar'];
    click(p.doc, p.q('[data-abandon-cancel]'));
    return { open, again, closed: style['--page-bar'] || null };
  },

  // a person abandons a row, the last row, the hero's search and something coming soon, from
  // the keyboard; `on` is where each leaves them
  async abandoned() {
    const posts = [];
    const p = page(spec, { post: (url, body) => new Promise((resolve) => posts.push({ body, resolve })) });
    const stuck = (id, extra) => item(id, { state: 'searching', abandon_visible: true, ...extra });
    const on = () => {
      const a = p.doc.activeElement;
      return a.getAttribute('data-abandon-trigger') === null ? a.tagName : a.getAttribute('data-dl-id');
    };
    const tick = async () => { p.timers.splice(0).forEach((t) => t.fn()); await settle(); };
    const press = async (id) => {
      const trigger = p.qa('[data-abandon-trigger]').find((b) => b.getAttribute('data-dl-id') === id);
      trigger.focus();
      click(p.doc, trigger);
      await tick();
      const confirm = p.q('[data-abandon-confirm]');
      confirm.focus();
      click(p.doc, confirm);
    };
    const finish = async () => { posts[posts.length - 1].resolve({}); await settle(); await tick(); };
    const out = { on: [] };
    const poll = (hero, rows, upcoming) => p.upd({ hero, queue: rows.map((id) => stuck(id)), upcoming: (upcoming || []).map((id) => item(id)) });

    await press('q1'); await finish(); poll(stuck('h'), ['q2'], ['u']);
    out.on.push(on());
    // the poll that drops the row comes before the answer does
    await press('q2'); poll(stuck('h'), [], ['u']); await finish();
    out.on.push(on());
    await press('h'); await finish(); poll(item('h'), [], ['u']);
    out.on.push(on());
    await press('u'); await finish(); poll(item('h'), []);
    out.on.push(on());
    // someone who has moved on is left where they are
    p.upd({ hero: item('h'), queue: [stuck('q3'), stuck('q4')] });
    await press('q3'); await finish();
    p.qa('[data-abandon-trigger]').find((b) => b.getAttribute('data-dl-id') === 'q4').focus();
    p.upd({ hero: item('h'), queue: [stuck('q4')] });
    out.on.push(on());
    return { ...out, toasts: p.ctx.toasts };
  },

  // the next poll of the same titles, and others that bring another hero
  async pollLook(motion) {
    const p = page(spec, { motion });
    const ghosts = () => p.qa('.dl-hero-ghost');
    p.upd({ hero: item('h', { progress: 5 }), queue: [item('q', { progress: 5 })] });
    const out = { same: ghosts().length, pill: p.q('.dl-state-pill--xs').style.fontSize };
    p.upd({ hero: item('b', { state: 'searching', abandon_visible: true }), queue: [] });
    const first = ghosts().map((g) => ({
      says: g.querySelector('.dl-hero-title').textContent, inert: g.hasAttribute('inert'), hidden: g.getAttribute('aria-hidden'),
      hooks: g.descendants().filter((e) => Object.keys(e.attrs).some((k) => /^(id|data-(v|ep|dl-id|abandon-trigger))$/.test(k))).length,
    }));
    p.upd({ hero: item('c'), queue: [] });
    const second = ghosts().map((g) => g.querySelector('.dl-hero-title').textContent);
    p.timers.splice(0).forEach((t) => t.fn());
    return { ...out, first, second, after: ghosts().length, hero: p.text('.dl-hero-title') };
  },
  // polls that come while the old hero still fades out
  async pollDuringFade() {
    const p = page(spec, { motion: true });
    const has = (sel) => !!p.q('.dl-hero').querySelector(sel);
    const searching = { state: 'searching', abandon_visible: true };
    p.upd({ hero: item('b') });
    const bare = [has('.dl-hero-abandon'), has('.dl-hero-ep-summary')];
    p.upd({ hero: item('b', { ...searching, episode_summary: '1 searching' }) });
    const out = { bare, full: [has('[data-abandon-trigger]'), p.q('.dl-hero').querySelectorAll('.dl-hero-ep-summary').map((e) => e.textContent)], fading: p.qa('.dl-hero-ghost').length };
    p.upd({ hero: item('b') });
    return { ...out, old: p.q('.dl-hero-ghost').querySelectorAll('.dl-hero-ep-summary').map((e) => e.textContent) };
  },
  // the hero's height as titles of other heights take its place, the second while it still moves
  async heroHeight(motion) {
    const p = page(spec, { motion });
    const card = p.q('.dl-hero');
    const style = {};
    const set = [];
    let natural = 500, rendered = 0;
    card.style = new Proxy(style, { get: (t, k) => t[k] || '', set: (t, k, v) => { t[k] = v; if (k === 'height') set.push(v); return true; } });
    card.getBoundingClientRect = () => ({ height: style.height ? rendered : natural });
    const swap = (id, to) => {
      const title = p.q('.dl-hero-title');
      Object.defineProperty(title, 'textContent', { configurable: true, get: () => '', set: () => { natural = to; } });
      p.upd({ hero: item(id) });
      return set.splice(0);
    };
    const out = { first: swap('b', 407) };
    const timers = p.timers.splice(0);
    rendered = 450;
    out.second = swap('c', 460);
    timers.forEach((t) => t.fn());
    out.afterFirstTimer = style.height || '';
    p.timers.splice(0).forEach((t) => t.fn());
    out.settled = style.height || '';
    set.splice(0);
    p.upd({ hero: item('c', { progress: 4 }) });
    out.same = set.length;
    return out;
  },
  async heroHeightMoving() { return scenarios.heroHeight(true); },
  async pollLookMoving() { return scenarios.pollLook(true); },

  // the hero's background from mediaman's own address, and from one that only looks like it
  async ownArt() {
    const p = page(spec);
    const bg = () => p.q('.dl-hero-bg').style['background-image'] || null;
    const drawn = bg();
    const other = ['//elsewhere/b.jpg', '/\\elsewhere/b.jpg', '/\t/elsewhere/b.jpg'].map((poster_url, i) => {
      p.upd({ hero: item('b' + i, { poster_url }) });
      return bg();
    });
    return { drawn, other };
  },

  // a queued row changes place while a person is on its episodes button
  async reordered() {
    const p = page(spec);
    const toggle = p.q('.dl-compact [data-v="ep-toggle"]');
    toggle.focus();
    const episodes = [{ label: 'S01E01', title: '', state: 'downloading', progress: 1 }];
    p.upd({ hero: item('h'), queue: [item('q2', { media_type: 'series', episodes }), item('q1', { state: 'searching' })] });
    const on = p.doc.activeElement === toggle;
    return { rows: p.qa('.dl-compact').map((r) => r.getAttribute('data-dl-id')), on };
  },

  // another title takes the hero while a person is on one of its controls, its list open
  async heroFollowed() {
    const p = page(spec);
    const ep = (label) => ({ label, title: '', state: 'queued', progress: 0 });
    const toggle = p.q('.dl-hero [data-v="ep-toggle"]');
    const open = () => [toggle.classList.contains('open'), toggle.getAttribute('aria-expanded'), !p.q('.dl-hero [data-v="ep-list"]').hidden];
    const hero = (id) => item(id, { media_type: 'series', state: 'searching', arr_link: 'http://arr/' + id, search_hint: 'x', abandon_visible: true, episodes: [ep('S01E01')] });
    click(p.doc, toggle);
    const out = { on: [] };
    for (const [i, part] of ['arr-link', 'ep-toggle', 'abandon'].entries()) {
      const at = p.q('.dl-hero ' + (part === 'abandon' ? '[data-abandon-trigger]' : `[data-v="${part}"]`));
      at.focus();
      p.upd({ hero: hero(i ? 'n' + (i - 1) : 'h') });
      const stays = p.doc.activeElement === at;
      p.upd({ hero: hero('n' + i) });
      out.on.push([stays, p.doc.activeElement.tagName]);
      if (!i) out.closed = open();
    }
    return out;
  },

  // a poll whose answer cannot be drawn
  async unreadable() {
    const polls = [];
    const p = page(spec, { get: () => { polls.push(1); return Promise.resolve({ hero: null, queue: [null], upcoming: [], recent: [], subtitle: 'x' }); } });
    const logged = [];
    p.ctx.console = { error: (e) => logged.push(String(e)) };
    for (let i = 0; i < 3; i++) {
      const t = p.timers.find((x) => x.ms === 2000);
      if (t) { p.timers.splice(p.timers.indexOf(t), 1); t.fn(); await settle(); }
    }
    return { polls: polls.length, logged: logged.length, line: p.text('.ph p'), dimmed: p.qa('.is-held').length };
  },

  // a poster comes to something coming soon, and another takes its place
  async upcomingPoster() {
    const p = page(spec);
    const src = () => p.qa('.dl-upcoming-row img').map((i) => i.getAttribute('src'));
    const out = [src()];
    for (const poster_url of ['https://art/1.jpg', 'https://art/2.jpg', '']) {
      p.upd({ hero: item('h'), upcoming: [item('u', { poster_url })] });
      out.push(src());
    }
    return { out };
  },

  // a click that gives a button no focus, as in Safari, on an abandon that fails
  async abandonFailsByClick() {
    const posts = [];
    const p = page(spec, { post: () => new Promise((_resolve, reject) => posts.push(reject)) });
    const tick = async () => { p.timers.splice(0).forEach((t) => t.fn()); await settle(); };
    click(p.doc, p.q('[data-abandon-trigger]'));
    await tick();
    const confirm = p.q('[data-abandon-confirm]');
    p.doc.body.focus();
    click(p.doc, confirm);
    const during = p.doc.activeElement === confirm;
    p.doc.body.focus();
    click(p.doc, confirm);
    posts[0](new Error('down'));
    await settle();
    await tick();
    const after = p.doc.activeElement === confirm;
    click(p.doc, p.q('[data-abandon-cancel]'));
    await tick();
    const back = p.doc.activeElement === p.q('[data-abandon-trigger]');
    return { posts: posts.length, during, after, back, dialog: p.q('#abandon-modal').getAttribute('tabindex'), errors: p.ctx.toasts.length };
  },

  // everything coming soon gives way to something else in one poll
  async upcomingReplaced() {
    const p = page(spec);
    p.upd({ hero: item('h'), upcoming: [item('v')] });
    return { rows: p.qa('.dl-upcoming-row').map((r) => r.getAttribute('data-dl-id')) };
  },

  // what a screen reader is told of each bar and section
  async spoken() {
    const p = page(spec);
    const bars = () => p.qa('[role="progressbar"]').map((b) => [b.getAttribute('aria-label'), b.getAttribute('aria-valuenow')]);
    const headings = () => p.q('#dl-root').descendants().filter((e) => /^H\d$/.test(e.tagName)).map((h) => h.tagName + ' ' + h.textContent);
    const poll = (n) => p.upd({
      hero: item('h' + n, { progress: 10 * n, media_type: 'series', episodes: [{ label: 'S01E01', title: '', state: 'downloading', progress: 5 * n }] }),
      queue: [item('q', { progress: n })], upcoming: [item('u')], recent: [{ title: 'R', poster_url: '' }],
    });
    const drawn = bars();
    poll(2);
    const first = { bars: bars(), headings: headings() };
    p.qa('.dl-compact').forEach((r) => r.remove());
    poll(3);
    const rebuilt = bars();
    p.upd({ hero: item('h3', { media_type: 'series', episodes: ['S01E01', 'S01E02'].map((label) => ({ label, title: '', state: 'downloading', progress: 7 })) }) });
    return { drawn, first, rebuilt, brought: bars().slice(1) };
  },

  // the episodes button says whether its list is open from the moment it is drawn
  async heroEpisodes() {
    const p = page(spec);
    p.upd({ hero: item('h', { media_type: 'series', episodes: [{ label: 'S01E01', title: 'One', state: 'queued', progress: 0 }] }) });
    const toggle = p.q('.dl-hero [data-v="ep-toggle"]');
    const expanded = () => toggle.getAttribute('aria-expanded');
    const drawn = expanded();
    click(p.doc, toggle);
    return { drawn, opened: expanded(), inInfo: toggle.parentNode.classList.contains('dl-hero-info') };
  },
};

module.exports = { page, item, spec, scenarios, click, settle };
if (require.main === module) scenarios[process.argv[2]]().then((out) => console.log(JSON.stringify(out)));
