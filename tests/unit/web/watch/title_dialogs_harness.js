// Dialogs opened from a title, on the page watch_detail_harness.js builds. Usage: node title_dialogs_harness.js <scenario>
'use strict';
const fs = require('fs');
const Module = require('module');
const path = require('path');
const vm = require('vm');

/* The other harness's page and helpers, without the run of a scenario of its own. */
const file = path.resolve(__dirname, '../js/watch_detail_harness.js');
const shared = new Module(file);
shared.filename = file;
const [source] = fs.readFileSync(file, 'utf8').split('scenarios[process.argv[2]]()');
shared._compile(source + '\nmodule.exports = { load, openCard, settle, LIB_SHOW, LIB_MOVIE, FACTS };', file);
const { build, click } = require('../js/fake_dom');
const { load, openCard, settle, LIB_SHOW, LIB_MOVIE, FACTS } = shared.exports;

const SHOW_FACTS = { ...FACTS, actions: { keep_wrapper: { id: '3010', is_tv: true, show_rk: '300' }, delete: { kind: 'show', show_rk: '300', title: 'Brightwater' } } };

/* An admin's title, opened from its card, with a dialog open from its Delete. */
async function asked() {
  const p = load({
    admin: true, adminApi: { pickSeasonDelete() {}, confirmDelete() {} },
    routes: { '/api/watch/title/show-300': LIB_SHOW, '/api/watch/admin/title/show-300': SHOW_FACTS,
      '/api/watch/title/movie-120': LIB_MOVIE, '/api/watch/admin/title/movie-120': FACTS },
  });
  p.run();
  const dialog = p.doc.createElement('div');
  dialog.id = 'asked';
  p.doc.body.appendChild(dialog);
  p.closed = 0;
  p.close = () => { p.closed++; p.ctx.ModalA11y.onClosed('asked'); };
  p.ctx.ModalA11y.register('asked', p.close);
  await openCard(p, 'show-300');
  p.ask = () => { p.q('[data-ttl-act="admin-delete"]').focus(); p.ctx.ModalA11y.onOpened('asked'); dialog.focus(); };
  p.at = () => { const a = p.doc.activeElement; return a.getAttribute('data-ttl-act') || a.getAttribute('data-title-ref') || a.id || a.tagName; };
  return p;
}

const scenarios = {
  async back() {
    const p = await asked();
    p.ask();
    p.ctx.history.back();
    const out = { closed: p.closed, inert: p.doc.documentElement.descendants().some((n) => n.inert), at: p.at() };
    p.ctx.history.go(1);
    await settle();
    out.forward = p.at();
    return out;
  },
  // A film, or the last season of a show, deleted from its title page.
  async deleted() {
    const out = {};
    for (const [ref, del] of [['movie-120', { action: 'delete', id: '120' }], ['show-300', { action: 'delete', id: '3010', showRk: '300' }]]) {
      const p = await asked();
      const card = (id, attrs, ...more) => ['article', { class: 'watch-card', id }, ['a', { class: 'watch-card__link', ...attrs }, ...more]];
      const path = '/watch/' + ref.replace('-', '/');
      const tmdb = ref === 'movie-120' ? 'tmdb-movie-77' : 'tmdb-tv-77';
      const taken = [];
      p.doc.body.appendChild(build(p.doc, ['section', { class: 'watch-row' },
        card('own', { href: path + '?season=2', 'data-title-ref': ref }), card('other', { href: path + '9', 'data-title-ref': ref + '9' })]));
      p.doc.body.appendChild(build(p.doc, ['section', { class: 'watch-row' },
        card('trending', { href: path, 'data-title-ref': tmdb, 'data-in-library': '' }, ['span', { class: 'watch-badge watch-badge--in' }, 'In library']),
        card('pick', { href: path, 'data-title-ref': ref, 'data-tmdb-id': '77', 'data-in-library': '' }, ['span', { class: 'watch-badge watch-badge--in' }, 'In library'])]));
      p.doc.body.appendChild(build(p.doc, ['article', { class: 'watch-card', id: 'continue', 'data-dismiss-ref': ref }, ['a', { class: 'watch-card__link', href: '/watch/play/9' }]]));
      p.ctx.MMWatch.keep.fromRow = (c) => { if (c.id) taken.push(c.id); return { focus: p.doc.getElementById('other') }; };
      p.ctx.history.back();
      await settle();
      click(p.doc, p.doc.getElementById('own').querySelector('a'));
      await settle();
      if (ref === 'show-300') delete p.routes['/api/watch/admin/title/show-300'];
      p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', ...del } }));
      await settle();
      out[ref] = { taken, at: p.at(), badges: p.doc.querySelectorAll('.watch-badge--in').length,
        links: ['trending', 'pick'].map((id) => { const a = p.doc.getElementById(id).querySelector('a'); return [a.getAttribute('data-title-ref'), a.getAttribute('href')]; }) };
    }
    return out;
  },
  // Back to the title before.
  async backToTitle() {
    const p = await asked();
    p.ctx.MMWatch.detail.open('movie-120', {});
    await settle();
    p.ask();
    p.ctx.history.back();
    await settle();
    return { closed: p.closed, title: p.text('#ttl-title'), in: p.ttl().contains(p.doc.activeElement) };
  },
  // The facts are drawn anew while the dialog is open, with the Delete and then without it.
  async redrawn() {
    const out = [];
    const p = await asked();
    p.ask();
    for (const del of [SHOW_FACTS.actions.delete, null]) {
      p.routes['/api/watch/admin/title/show-300'] = { ...SHOW_FACTS, actions: { ...SHOW_FACTS.actions, delete: del } };
      p.doc.dispatchEvent(new p.ctx.CustomEvent('mm:watch-admin', { detail: { phase: 'done', action: 'show', id: '3010', showRk: '300' } }));
      await settle();
      p.close();
      out.push([p.at(), p.doc.activeElement.isConnected]);
      if (del) p.ask();
    }
    return out;
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}, (err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
