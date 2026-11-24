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
Node.prototype.removeChild = function (c) { c.remove(); return c; };
Node.prototype.replaceChildren = function () { [...this.childNodes].forEach((c) => c.remove()); };
Node.prototype.remove = (function (remove) {
  return function () {
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    remove.call(this);
  };
})(Node.prototype.remove);
Node.prototype.focus = function () { if (this.isConnected && !this.closest('[hidden]')) this.ownerDocument.activeElement = this; };

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
    AbortController, matchMedia: () => ({ matches: true }),
    getComputedStyle: () => ({}), location: { href: '/downloads' },
    toasts: [],
  };
  ctx.window = ctx;
  ctx.UIFeedback = { toast: (text, o) => ctx.toasts.push([text, (o || {}).variant]), error: (text) => ctx.toasts.push([text, 'error']) };
  doc.dispatchEvent = (e) => (doc.listeners[e.type] || []).forEach((f) => f(e));
  ctx.MM.api = { get: opts && opts.get, post: opts && opts.post, messageOf: (_e, m) => m };
  vm.createContext(ctx);
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
    text: (s) => { const e = doc.querySelector(s); return e && e.textContent.replace(/\s+/g, ' ').trim(); },
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
      toggle: p.text('.dl-compact [data-v="ep-toggle"]'), count: p.text('.dl-compact [data-v="ep-count"]'),
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
    return { drawn, first, rebuilt: bars() };
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
