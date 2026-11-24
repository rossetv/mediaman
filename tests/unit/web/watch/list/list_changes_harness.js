// More scenarios for the page watch_list_harness.js builds. Usage: node list_changes_harness.js <scenario>
'use strict';
const fs = require('fs');
const Module = require('module');
const path = require('path');

/* The other harness's page and helpers, without the run of a scenario of its own. */
const file = path.resolve(__dirname, '../../js/watch_list_harness.js');
const shared = new Module(file);
shared.filename = file;
const [source] = fs.readFileSync(file, 'utf8').split('const name = process.argv[2];');
shared._compile(source + '\nmodule.exports = { load, click, keepOption, ROWS, settle };', file);
const { load, click, keepOption, ROWS, settle } = shared.exports;

const trigger = (p, key) => p.row(key).querySelector('.keep-trigger');
const LIST = '/watch/list';

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

  // What is scheduled changed while a filter loaded that then failed.
  async changeDuringAFailedLoad() {
    const p = load({ list: true });
    let fail;
    p.serve('/watch/list?type=tv', {}, [], { promise: new Promise((_res, rej) => { fail = rej; }) });
    click(p.doc, p.doc.querySelectorAll('#watch-list .fpill').find((n) => n.getAttribute('data-type') === 'tv'));
    p.doc.dispatchEvent({ type: 'mm:watch-scheduled' });
    fail(new Error('gone')); await settle();
    p.serve(LIST, { label: 'now' }, ROWS);
    const reads = await thereAndBack(p);
    return { reads, summary: p.part('summary') };
  },
};

scenarios[process.argv[2]]().then((out) => {
  process.stdout.write(JSON.stringify(out));
}, (err) => {
  process.stderr.write(String(err && err.stack || err));
  process.exit(1);
});
