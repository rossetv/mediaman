// Runs the real download.js (the per-token page) against a tiny fake DOM and
// prints the outcome as JSON. Usage: node download_page_harness.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS = path.resolve(__dirname, '../../../../src/mediaman/web/static/js');

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attrs = {}; this.style = {}; this.listeners = {}; this.className = ''; this._text = ''; this.id = '';
  }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (c) => { if (!list().includes(c)) el.className = list().concat(c).join(' '); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(' '); },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  addEventListener() {}
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

async function main() {
  const root = new El('div');
  const doc = {
    hidden: false,
    createElement: (t) => new El(t),
    createTextNode: (t) => { const e = new El('#text'); e._text = t; return e; },
    addEventListener() {},
    querySelector: () => null,
    getElementById: (id) => (id === 'dl-root' ? root : root.all().find((e) => e.id === id) || null),
  };
  const add = (id, text) => { const e = new El('div'); e.setAttribute('id', id); if (text) e._text = text; root.appendChild(e); return e; };
  add('page-bootstrap', JSON.stringify({
    token: 't', poll_token: 'p', service: 'radarr', tmdb_id: 7, title: 'Film',
    media_type: 'movie', download_state: 'queued',
  }));
  const wrapper = add('dl-hero-wrapper');

  let payload = {};
  const ctx = {
    document: doc, console, JSON, Array, Object, String, Math, Promise,
    setInterval: (fn) => { ctx.tick = fn; return 1; }, clearInterval() {}, MM: {},
  };
  ctx.window = ctx;
  ctx.MM.api = { get: () => Promise.resolve(payload), post: () => new Promise(() => {}), APIError: class extends Error {} };
  vm.createContext(ctx);
  const load = (f) => vm.runInContext(fs.readFileSync(path.join(JS, f), 'utf8'), ctx);
  load('core/dom.js');
  load('downloads/build_dom.js');
  // the page arrives with a hero that has no ETA yet
  wrapper.appendChild(ctx.MM.downloads.buildDom.buildHero({
    id: 'radarr:Film', title: 'Film', media_type: 'movie', state: 'searching', progress: 0, eta: '',
  }));

  const settle = () => new Promise((r) => setImmediate(r));
  const stats = () => wrapper.querySelector('.dl-hero-details').children.map((c) => c.textContent);
  const out = {};
  out.start = stats();
  load('download.js');           // queued: polls once straight away
  await settle();
  out.noEta = stats();

  const step = async (p) => {
    payload = Object.assign({ state: 'downloading', state_label: 'Downloading', progress: 30, size_done: '1 GB', size_total: '3 GB' }, p);
    ctx.tick();
    await settle();
    return stats();
  };
  out.noEtaYet = await step({ eta: '' });
  out.arrives = await step({ eta: '5 min' });
  out.changes = await step({ eta: '4 min' });
  out.clears = await step({ eta: '' });
  console.log(JSON.stringify(out));
}

main();
