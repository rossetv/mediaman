// Runs the real download.js (the per-token page) against a tiny fake DOM and
// prints the outcome as JSON. Usage: node download_page_harness.js [scenario]; with none, a
// title already on its way when the page opens.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attrs = {}; this.style = {}; this.listeners = {}; this.className = ''; this._text = ''; this.id = '';
    this.hidden = false;
  }
  get firstChild() { return this.children[0] || null; }
  get nextElementSibling() { const all = this.parentNode.children; return all[all.indexOf(this) + 1] || null; }
  insertBefore(c, before) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const at = before ? this.children.indexOf(before) : this.children.length;
    c.parentNode = this; this.children.splice(at, 0, c); return c;
  }
  contains(o) { for (let e = o; e; e = e.parentNode) if (e === this) return true; return false; }
  closest(sel) { for (let e = this; e; e = e.parentNode) if (e.matches(sel)) return e; return null; }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() { El.focused = this; }
  scrollIntoView() { El.scrolledTo = this; }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(' '); },
      toggle: (c, on) => {
        if (on === undefined) on = !list().includes(c);
        el.className = list().filter((x) => x !== c).concat(on ? c : []).join(' ');
        return on;
      },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener() {}
  replaceChildren(...cs) { this.children = []; this._text = ''; cs.forEach((c) => this.appendChild(c)); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  matches(sel) {
    let m;
    if ((m = sel.match(/^\.([\w-]+)$/))) return this.classList.contains(m[1]);
    if ((m = sel.match(/^\[([\w-]+)="([^"]*)"\]$/))) return this.getAttribute(m[1]) === m[2];
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(sel) { return this.all().filter((e) => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const settle = () => new Promise((r) => setImmediate(r));

// The page with its scripts not yet run. `boot` overrides what the server says of the title.
function page(boot) {
  El.focused = null;
  const root = new El('div');
  const doc = {
    hidden: false,
    get activeElement() { return El.focused; },
    createElement: (t) => new El(t),
    createElementNS: (ns, t) => new El(t),
    createTextNode: (t) => { const e = new El('#text'); e._text = t; return e; },
    addEventListener() {},
    querySelector: () => null,
    getElementById: (id) => (id === 'dl-root' ? root : root.all().find((e) => e.id === id) || null),
  };
  const add = (id, text, parent) => { const e = new El('div'); e.setAttribute('id', id); if (text) e._text = text; (parent || root).appendChild(e); return e; };
  add('page-bootstrap', JSON.stringify(Object.assign({
    token: 't', poll_token: 'p', service: 'radarr', tmdb_id: 7, title: 'Film',
    media_type: 'movie', download_state: 'queued',
    poster_url: 'https://image.tmdb.org/t/p/w500/poster.jpg',
    backdrop_url: 'https://image.tmdb.org/t/p/w1280/wide.jpg',
  }, boot)));
  const cinema = add('dl-cinema');
  const content = add('dl-content');
  const action = add('download-action', '', content);
  const button = add('btn-download', 'Download', action);
  const result = add('download-result', '', content);
  result.hidden = true;
  const wrapper = add('dl-hero-wrapper');

  const p = { payload: {}, posted: 0, answer: () => Promise.resolve({ service: 'radarr', tmdb_id: 7, poll_token: 'p2' }) };
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise,
    setInterval: (fn) => { ctx.tick = fn; return 1; }, clearInterval() {}, MM: {},
  };
  ctx.window = ctx;
  ctx.setTimeout = (fn) => fn();
  ctx.matchMedia = () => ({ matches: true });
  ctx.MM.api = {
    get: () => Promise.resolve(p.payload),
    post: () => { p.posted += 1; return p.answer(); },
    APIError: class extends Error {},
    messageOf: (err, fallback) => (err.data && err.data.message) || fallback,
  };
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  load('core/dom.js');
  load('downloads/build_dom.js');
  return Object.assign(p, { ctx, load, root, cinema, content, action, button, result, wrapper });
}

// Pressing Download gets `answer`; the second press, when there is one, gets `then`.
function refusal(status, data) {
  return () => Promise.reject(Object.assign(new Error(), { status, data: data || {} }));
}

async function press(p) {
  // A click in Safari puts the focus on the page around the button.
  El.focused = p.root;
  p.button.listeners.click.forEach((fn) => fn());
  const during = { label: p.button.textContent, busy: p.button.getAttribute('aria-disabled'), focused: El.focused === p.button };
  p.button.listeners.click.forEach((fn) => fn());
  await settle();
  return during;
}

const seen = (p) => ({
  card: !p.wrapper.hidden && !!p.wrapper.querySelector('.dl-hero'),
  hint: (p.wrapper.querySelector('.dl-hint') || { textContent: null }).textContent,
  content: !p.content.hidden,
  button: p.action.hidden ? null : p.button.textContent,
  busy: p.button.getAttribute('aria-disabled'),
  result: p.result.hidden ? null : p.result.textContent,
  error: p.result.classList.contains('dl-result--error'),
  focus: El.focused === p.button ? 'button' : El.focused === p.result ? 'result' : El.focused && El.focused.tagName,
  posted: p.posted,
});

const ANSWERS = {
  unanswered: refusal(0, { message: 'Could not reach mediaman.' }),
  down: refusal(502, { message: 'Check service connectivity' }),
  limited: refusal(429, { message: 'Too many requests' }),
  expired: refusal(410, { message: 'Token expired or invalid' }),
  used: refusal(409, { message: 'This download link has already been used' }),
  queued: refusal(409, { poll_token: 'p3', download_state: 'queued' }),
  inLibrary: refusal(409, { poll_token: 'p3', download_state: 'in_library' }),
  tracked: refusal(409, { poll_token: 'p3', download_state: 'tracked' }),
};

const SCENARIOS = {
  async answers() {
    const out = {};
    for (const name of Object.keys(ANSWERS)) {
      const p = page({ download_state: '', poll_token: '' });
      p.load('download.js');
      p.answer = ANSWERS[name];
      await press(p);
      out[name] = seen(p);
    }
    return out;
  },
  async retry() {
    const p = page({ download_state: '', poll_token: '' });
    p.load('download.js');
    const good = p.answer;
    p.answer = ANSWERS.down;
    const first = await press(p);
    const failed = seen(p);
    p.answer = good;
    const second = await press(p);
    return { first, failed, second, done: seen(p) };
  },
  async hint() {
    const p = page({ download_state: '', poll_token: '', notify_by_email: true });
    p.ctx.MM.api.get = () => new Promise(() => {});
    p.load('download.js');
    await press(p);
    return { hint: seen(p).hint };
  },
  async card() {
    const p = page({ download_state: '', poll_token: '', media_type: 'tv', service: 'sonarr' });
    p.ctx.MM.api.get = () => new Promise(() => {});
    p.load('download.js');
    await press(p);
    const card = p.wrapper.querySelector('.dl-hero');
    return {
      headings: p.wrapper.querySelectorAll('h1').map((e) => e.textContent),
      focused: El.focused.tagName,
      pills: card.querySelectorAll('.pill').map((e) => e.textContent),
      state: card.querySelector('[data-v="pill"]').textContent,
      links: p.wrapper.querySelectorAll('a').map((e) => e.href),
    };
  },
  async episodes() {
    const p = page({ media_type: 'tv', service: 'sonarr' });
    p.wrapper.appendChild(p.ctx.MM.downloads.buildDom.buildHero({ id: 'x', title: 'Show', media_type: 'tv', state: 'searching' }));
    const ep = (label, state) => ({ label, title: label, state, progress: 0 });
    const step = async (episodes) => {
      p.payload = { state: 'downloading', state_label: 'Downloading', progress: 3, episodes };
      if (p.ctx.tick) p.ctx.tick(); else p.load('download.js');
      await settle();
    };
    await step([ep('S01E01', 'queued'), ep('S01E02', 'searching')]);
    const toggle = p.wrapper.querySelector('[data-v="ep-toggle"]');
    const list = p.wrapper.querySelector('[data-v="ep-list"]');
    const press = () => p.wrapper.listeners.click.forEach((fn) => fn({ target: toggle.querySelector('span') }));
    const state = () => ({ expanded: toggle.getAttribute('aria-expanded'), shown: !list.hidden });
    const out = { tag: toggle.tagName, inInfo: !!toggle.closest('.dl-hero-info') && !!list.closest('.dl-hero-info') };
    out.pills = list.querySelectorAll('.dl-ep-status-pill').map((e) => e.textContent);
    out.closed = state();
    press();
    out.open = state();
    await step([ep('S01E01', 'ready'), ep('S01E02', 'searching'), ep('S01E03', 'queued')]);
    out.after = { ...state(), rows: list.querySelectorAll('.dl-ep-status-pill').map((e) => e.textContent) };
    p.payload = { state: 'unknown', progress: 0 };
    p.ctx.tick();
    await settle();
    out.unanswered = p.wrapper.querySelector('[data-v="pill"]').textContent;
    press();
    out.closedAgain = state();
    return out;
  },
  async ready() {
    const p = page({ download_state: '', poll_token: '' });
    p.load('download.js');
    p.payload = { state: 'ready', state_label: 'Ready', progress: 100 };
    await press(p);
    return {
      status: p.wrapper.querySelectorAll('[role="status"]').map((e) => e.textContent),
      card: !!p.wrapper.querySelector('.dl-hero'),
      progress: !p.wrapper.querySelector('[data-v="progress-wrap"]').hidden,
      links: p.wrapper.querySelectorAll('a').length,
    };
  },
};

async function main() {
  const scenario = SCENARIOS[process.argv[2]];
  if (scenario) { console.log(JSON.stringify(await scenario())); return; }
  const p = page({});
  const { ctx, wrapper, button, load } = p;
  // the page arrives with a hero that has no ETA yet
  wrapper.appendChild(ctx.MM.downloads.buildDom.buildHero({
    id: 'radarr:Film', title: 'Film', media_type: 'movie', state: 'searching', progress: 0, eta: '',
  }));

  const stats = () => wrapper.querySelector('.dl-hero-details').children.map((c) => c.textContent);
  const out = {};
  out.start = stats();
  load('download.js');           // queued: polls once straight away
  await settle();
  out.noEta = stats();

  const step = async (polled) => {
    p.payload = Object.assign({ state: 'downloading', state_label: 'Downloading', progress: 30, size_done: '1 GB', size_total: '3 GB' }, polled);
    ctx.tick();
    await settle();
    return stats();
  };
  out.noEtaYet = await step({ eta: '' });
  out.arrives = await step({ eta: '5 min' });
  out.changes = await step({ eta: '4 min' });
  out.clears = await step({ eta: '' });

  // pressing Download builds the queued card: wide backdrop behind it, the poster as its poster
  button.listeners.click[0]();
  await settle();
  const built = wrapper.querySelector('.dl-hero');
  out.queuedCard = {
    background: built.querySelector('.dl-hero-bg').getAttribute('data-bg-url'),
    poster: built.querySelector('.dl-hero-poster').querySelector('IMG').src,
  };
  console.log(JSON.stringify(out));
}

main();
