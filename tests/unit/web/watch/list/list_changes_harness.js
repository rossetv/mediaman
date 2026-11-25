// More scenarios for the page watch_list_harness.js builds. Usage: node list_changes_harness.js <scenario>
'use strict';
const fs = require('fs');
const vm = require('vm');
const Module = require('module');
const path = require('path');

/* The other harness's page and helpers, without the run of a scenario of its own. */
const file = path.resolve(__dirname, '../../js/watch_list_harness.js');
const shared = new Module(file);
shared.filename = file;
const [source] = fs.readFileSync(file, 'utf8').split('const name = process.argv[2];');
const linked = source.replace("{ class: 'title-cell' }, r.title]",
  "{ class: 'title-cell' }, r.plain ? r.title : ['a', { class: 'wl-title', href: '#' }, r.title]]");
shared._compile(linked + '\nmodule.exports = { load, click, keepOption, ROWS, settle, deferred };', file);
const { load, click, keepOption, ROWS, settle, deferred } = shared.exports;

const trigger = (p, key) => p.row(key).querySelector('.keep-trigger');
const LIST = '/watch/list';
const TV = '/watch/list?type=tv';
const tv = (p) => p.doc.querySelectorAll('#watch-list .fpill').find((n) => n.getAttribute('data-type') === 'tv');
/* What has the focus: the title or the Keep of a row, or else its own. */
const at = (p) => {
  const a = p.doc.activeElement;
  const row = a.closest('tr');
  return row && a !== row ? row.getAttribute('data-row-key') + ' ' + a.className.split(' ')[0] : a.id || a.getAttribute('data-row-key') || a.tagName;
};

/* m2 kept from the list, and the focus on its toast's Undo. */
async function kept(state) {
  const p = load({ list: true, state });
  const url = LIST + (state ? '?' + new URLSearchParams(state) : '');
  p.serve(url, state || {}, ROWS.map((r) => (r.id === 'm2' ? { ...r, kept: true } : r)));
  click(p.doc, keepOption(p, 'id:m2', 'forever'));
  await settle();
  p.serve(url, state || {}, ROWS);
  const toast = p.doc.createElement('div');
  const undo = p.doc.createElement('button');
  toast.className = 'ui-toast ui-toast--action';
  toast.appendChild(undo);
  p.doc.body.appendChild(toast);
  undo.focus();
  // As the real toast: it is on its way out when the action runs.
  p.pressUndo = async () => { toast.classList.add('is-leaving'); p.toasts[0].action.onClick(); await settle(); };
  p.onUndo = () => p.doc.activeElement === undo;
  return p;
}

/* To Browse and back: how often the list was read for it. */
async function thereAndBack(p) {
  const toggle = (view) => p.doc.querySelector('#watch-viewtoggle [data-view="' + view + '"]');
  const before = p.fetches.length;
  click(p.doc, toggle('rows')); await settle();
  click(p.doc, toggle('list')); await settle();
  return p.fetches.length - before;
}

const scenarios = {
  // Undo of a keep: where the focus is once it is saved, refused, or the list could not be read again.
  async undoFocus() {
    const out = {};
    const run = async (name, before) => {
      const p = await kept();
      if (before) before(p);
      await p.pressUndo();
      out[name] = p.doc.activeElement === trigger(p, 'id:m2');
    };
    await run('saved');
    await run('refused', (p) => { p.replies['post /api/media/m2/unprotect'] = () => Promise.reject(new Error('no')); });
    await run('unread', (p) => p.fail(LIST));
    return out;
  },

  // The row of the Undo is on another page, or none is left under the filter.
  async undoElsewhere() {
    const out = {};
    const p = await kept();
    p.serve('/watch/list?page=2', { page: '2' }, [{ id: 'm7', title: 'Above the screen' }, { id: 'm8', title: 'In sight' }]);
    click(p.doc, p.doc.querySelector('#watch-list a.page-btn')); await settle();
    p.row('id:m7').getBoundingClientRect = () => ({ top: -40, bottom: -10 });
    const q = await kept();
    q.serve('/watch/list?type=anime', { type: 'anime' }, []);
    click(q.doc, q.doc.querySelectorAll('#watch-list .fpill').find((n) => n.getAttribute('data-type') === 'anime')); await settle();
    for (const [name, at] of [['page', p], ['filter', q]]) {
      at.doc.activeElement.blur();
      await at.pressUndo();
      out[name] = at.doc.activeElement;
    }
    return { page: out.page === trigger(p, 'id:m8'), filter: out.filter.getAttribute('data-type') };
  },

  // Focus that is somewhere, or is of what is open over the list, is left there.
  async undoLeavesFocus() {
    const out = {};
    const open = await kept();
    open.doc.documentElement.classList.add('has-ttl');
    await open.pressUndo();
    out.details = open.onUndo();
    const asked = await kept();
    const dialog = asked.doc.createElement('div');
    dialog.className = 'wl-dialog active';
    asked.doc.body.appendChild(dialog);
    await asked.pressUndo();
    out.dialog = asked.onUndo();
    const typing = await kept();
    typing.doc.getElementById('watch-search').focus();
    await typing.pressUndo();
    out.typing = typing.doc.activeElement.id;
    return out;
  },

  // A keep whose list could not be read again: to Browse and back reads it, once.
  async unreadIsReadAgain() {
    const p = load({ list: true });
    p.fail(LIST);
    click(p.doc, keepOption(p, 'id:m2', 'forever')); await settle();
    p.serve(LIST, { label: 'now' }, ROWS);
    const reads = [await thereAndBack(p), await thereAndBack(p)];
    return { reads, summary: p.part('summary') };
  },

  // A row that left the page and could not be found in the library.
  async unfoundIsReadAgain() {
    const p = load({ list: true });
    p.serve(LIST, {}, ROWS.filter((r) => r.id !== 'm2'));
    p.fail('/watch/list?q=Saltmarsh&per_page=100');
    click(p.doc, keepOption(p, 'id:m2', 'forever')); await settle();
    return { reads: await thereAndBack(p), keys: p.rowKeys() };
  },

  // What is scheduled changed while a filter loaded that then failed: in sight, with the next read failing too, and not in sight.
  async changeDuringAFailedLoad() {
    const out = {};
    const run = async (name, before) => {
      const p = load({ list: true });
      const loading = deferred();
      p.serve(TV, {}, [], loading);
      p.serve(LIST, { label: 'now' }, ROWS);
      click(p.doc, tv(p));
      p.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
      if (before) await before(p);
      const was = p.fetches.length;
      loading.reject(new Error('gone')); await settle();
      const read = p.fetches.length - was;
      p.serve(LIST, { label: 'now' }, ROWS);
      out[name] = { read, errors: p.errors.length, again: await thereAndBack(p), summary: p.part('summary') };
    };
    await run('seen');
    await run('refused', (p) => p.fail(LIST));
    await run('left', async (p) => { click(p.doc, p.doc.querySelector('#watch-viewtoggle [data-view="rows"]')); await settle(); });
    return out;
  },

  // A keep is saved and a filter loads before the list is read again for it; the filter then fails, first or last.
  async refreshBesideAFailedLoad() {
    const out = {};
    for (const last of [true, false]) {
      const p = load({ list: true });
      const refresh = deferred();
      const loading = deferred();
      p.serve(LIST, { label: 'kept' }, ROWS.map((r) => (r.id === 'm2' ? { ...r, kept: true } : r)), refresh);
      p.serve(TV, {}, [], loading);
      click(p.doc, keepOption(p, 'id:m2', 'forever')); await settle();
      click(p.doc, tv(p));
      const steps = [() => refresh.resolve(), () => loading.reject(new Error('gone'))];
      for (const step of last ? steps : steps.reverse()) { step(); await settle(); }
      out[last ? 'last' : 'first'] = { label: trigger(p, 'id:m2').textContent, disabled: !!trigger(p, 'id:m2').disabled,
        kept: p.row('id:m2').classList.contains('wl-row--kept'), summary: p.part('summary') };
    }
    return out;
  },

  // Another list was drawn before it came: that one stays.
  async refreshAfterALoad() {
    const p = load({ list: true });
    const refresh = deferred();
    p.serve(LIST, {}, ROWS, refresh);
    p.serve(TV, { type: 'tv', label: 'tv' }, [{ id: 'm2', title: 'Saltmarsh', kept: true }]);
    click(p.doc, keepOption(p, 'id:m2', 'forever')); await settle();
    click(p.doc, tv(p)); await settle();
    refresh.resolve(); await settle();
    return { summary: p.part('summary'), label: trigger(p, 'id:m2').textContent };
  },

  // Where the focus is while a change to a row is being saved, and once it is.
  async focusWaits() {
    const out = {};
    const run = async (name, p, url, start) => {
      const reply = deferred();
      p.replies[url] = () => reply.promise;
      const drop = () => p.dropFocusIfDisabled();
      p.doc.addEventListener('mm:watch-admin', drop);
      await start(); await settle();
      out[name] = [at(p)];
      if (name === 'redrawn') { p.doc.dispatchEvent({ type: 'mm:watch-scheduled' }); await settle(); out[name].push(at(p)); }
      reply.resolve({ ok: true }); await settle();
      out[name].push(at(p));
    };
    const undo = await kept();
    await run('undo', undo, 'post /api/media/m2/unprotect', () => undo.pressUndo());
    const redrawn = await kept();
    await run('redrawn', redrawn, 'post /api/media/m2/unprotect', () => redrawn.pressUndo());
    const keep = load({ list: true });
    keep.serve(LIST, {}, ROWS);
    trigger(keep, 'id:m2').focus();
    await run('keep', keep, 'postForm /api/media/m2/keep', () => click(keep.doc, keepOption(keep, 'id:m2', 'forever')));
    const rows = [{ id: 'm2', title: 'Saltmarsh', plain: true }];
    const plain = load({ list: true, rows });
    plain.serve(LIST, {}, rows);
    trigger(plain, 'id:m2').focus();
    await run('plain', plain, 'postForm /api/media/m2/keep', () => click(plain.doc, keepOption(plain, 'id:m2', 'forever')));
    return out;
  },

  // Back from one filter to another, with the focus on what the list is drawn with anew.
  async backBetweenFilters() {
    const out = {};
    const run = async (name, focus, rows) => {
      const p = load({ list: true });
      p.serve(TV, { type: 'tv' }, [{ id: 'm2', title: 'Saltmarsh' }]);
      p.serve(LIST, {}, rows || ROWS);
      click(p.doc, tv(p)); await settle();
      focus(p).focus();
      p.popstate('?view=list'); await settle();
      const a = p.doc.activeElement;
      out[name] = a.isConnected && a.classList.contains('fpill') ? 'filter ' + a.getAttribute('data-type') : at(p);
    };
    await run('filter', tv);
    await run('row', (p) => p.row('id:m2').querySelector('a.wl-title'));
    await run('rowGone', (p) => p.row('id:m2').querySelector('a.wl-title'), []);
    await run('elsewhere', (p) => p.doc.getElementById('watch-search'));
    return out;
  },

  // A dialog opened from a row that is busy, and then drawn anew, when it is closed; and Back to the rows with it open.
  async dialogOverAChange() {
    const out = {};
    const run = async (name, go) => {
      const p = load({ list: true });
      p.serve(LIST, {}, ROWS);
      vm.runInContext(fs.readFileSync(path.resolve(path.dirname(file), '../../../../src/mediaman/web/static/js/modal-a11y.js'), 'utf8'), p.ctx);
      const dialog = p.doc.createElement('div');
      dialog.id = 'asked';
      dialog.appendChild(p.doc.createElement('button'));
      p.doc.body.appendChild(dialog);
      let closed = 0;
      p.ctx.ModalA11y.register('asked', () => { closed++; p.ctx.ModalA11y.onClosed('asked'); });
      trigger(p, 'show:77').focus();
      p.ctx.ModalA11y.onOpened('asked');
      const emit = (phase) => p.doc.dispatchEvent({ type: 'mm:watch-admin', detail: { phase, action: 'show', id: 's1', showRk: '77' } });
      await go(p, emit);
      const open = !closed;
      if (open) p.ctx.ModalA11y.onClosed('asked');
      const a = p.doc.activeElement;
      out[name] = { open, at: at(p), usable: a.isConnected && !a.disabled };
    };
    await run('busy', (p, emit) => emit('busy'));
    await run('saved', async (p, emit) => { emit('busy'); emit('done'); await settle(); });
    await run('failed', (p, emit) => { emit('busy'); emit('failed'); });
    await run('left', async (p) => {
      p.ctx.ModalA11y.onClosed('asked');
      trigger(p, 'id:m2').focus();
      p.ctx.ModalA11y.onOpened('asked');
      p.doc.dispatchEvent({ type: 'mm:watch-admin', detail: { phase: 'done', action: 'delete', id: 'm2' } });
      await settle();
    });
    await run('back', async (p) => { p.popstate(''); await settle(); });
    return out;
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}, (err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
