/**
 * settings/disk_thresholds.js — Plex library pills + per-library disk cards.
 *
 * Responsibilities:
 *   - Fetch /api/plex/libraries and paint the picker, a toggle button for each
 *   - Maintain the selected-library set
 *   - Build the per-library "lp-card" (path + threshold + live usage bar)
 *   - Read /api/settings/disk-usage as the user edits, and say what the scanner will do
 *   - Keep each library's path and threshold, as saved and as typed, through
 *     every redraw, whether or not the library is selected
 *   - Work out which libraries' thresholds a save has to send
 *
 * Cross-module dependencies:
 *   MM.api                    (core/api.js)
 *   MM.settings.savebar.markDirty
 *
 * Exposes:
 *   MM.settings.diskThresholds.init({ selectedLibs, diskThresholds })
 *   MM.settings.diskThresholds.getSelectedLibs()
 *   MM.settings.diskThresholds.state() / restore(state) — for save and Discard
 *   MM.settings.diskThresholds.show(state)              — draws it round what is being typed
 *   MM.settings.diskThresholds.changes(from, to)        — for collectSettings
 *   MM.settings.diskThresholds.fromServer(data)         — a state, from what the server has
 *   MM.settings.diskThresholds.things(state)            — each library's selection and limit by its own key
 *   MM.settings.diskThresholds.rebase(mine, stored, keep) — `stored` with the kept things of `mine`
 *   MM.settings.diskThresholds.titleOf(id)
 *   MM.settings.diskThresholds.stored()                 — the thresholds as the server has them
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var PLEX_LIBRARIES = [];
  var LOADED = false;
  var SELECTED_LIBS = [];
  // Every library's entry: what was saved, with what is typed kept over it.
  var DISK_THRESHOLDS = {};
  // The thresholds as the server has them, readable or not.
  var STORED = {};
  // What is stored is no object of entries, which keeps every library from being scanned.
  var UNREADABLE = false;
  var NO_LIBRARIES = 'No libraries found — check Plex URL and token, then reload.';

  function markDirty() {
    if (MM.settings.savebar && MM.settings.savebar.markDirty) {
      MM.settings.savebar.markDirty();
    }
  }

  function renderLibraryPills() {
    var container = document.getElementById('plex-library-pills');
    if (!container) return;
    container.replaceChildren();
    if (!PLEX_LIBRARIES.length) {
      var msg = document.createElement('span');
      msg.className = 'fld-sub';
      msg.textContent = NO_LIBRARIES;
      container.appendChild(msg);
      updateLibPillCount();
      return;
    }
    PLEX_LIBRARIES.forEach(function (lib) {
      var pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'lib-pill';
      pill.textContent = lib.title;
      pill.dataset.libId = String(lib.id);
      pill.addEventListener('click', function () {
        var idx = SELECTED_LIBS.indexOf(pill.dataset.libId);
        if (idx === -1) SELECTED_LIBS.push(pill.dataset.libId);
        else SELECTED_LIBS.splice(idx, 1);
        keepTyped();
        renderDiskRows();
        paintPills();
        markDirty();
      });
      container.appendChild(pill);
    });
    paintPills();
  }

  function paintPills() {
    document.querySelectorAll('#plex-library-pills .lib-pill').forEach(function (pill) {
      var on = SELECTED_LIBS.indexOf(pill.dataset.libId) !== -1;
      pill.classList.toggle('on', on);
      pill.setAttribute('aria-pressed', String(on));
    });
    updateLibPillCount();
  }

  function updateLibPillCount() {
    var el = document.getElementById('lib-pill-count');
    if (!el) return;
    if (!PLEX_LIBRARIES.length) { el.textContent = SELECTED_LIBS.length + ' selected'; return; }
    var on = PLEX_LIBRARIES.filter(function (l) { return SELECTED_LIBS.indexOf(String(l.id)) !== -1; });
    el.textContent = on.length + ' of ' + PLEX_LIBRARIES.length + ' selected';
  }

  function renderDiskRows() {
    var container = document.getElementById('disk-threshold-rows');
    if (!container) return;
    var selected = PLEX_LIBRARIES.filter(function (l) { return SELECTED_LIBS.indexOf(String(l.id)) !== -1; });
    // A card that already shows its entry stays, with the caret and whatever is being typed in it.
    var kept = {};
    Array.prototype.slice.call(container.children).forEach(function (card) {
      var id = card.dataset.libId;
      var stays = SELECTED_LIBS.indexOf(id) !== -1 && same(typedIn(card), shown(DISK_THRESHOLDS[id]));
      if (stays) kept[id] = card; else card.remove();
    });
    if (!selected.length) {
      var hint = document.createElement('div');
      hint.className = 'fld-sub';
      hint.textContent = 'Select Plex libraries above to configure paths and thresholds.';
      container.appendChild(hint);
      return;
    }
    var next = container.firstChild;
    selected.forEach(function (lib) {
      var id = String(lib.id);
      if (kept[id]) {
        kept[id].anew = typedAnew(DISK_THRESHOLDS[id]);
        kept[id].refreshState(); next = kept[id].nextSibling;
        return;
      }
      container.insertBefore(buildLibPathCard(lib, DISK_THRESHOLDS[id] || {}), next);
    });
  }

  function buildLibPathCard(lib, cfg) {
    var card = document.createElement('div');
    card.className = 'lp-card';
    card.dataset.libId = String(lib.id);

    var head = document.createElement('div');
    head.className = 'lp-head';
    var glyph = document.createElement('div');
    glyph.className = 'lp-glyph';
    glyph.textContent = (lib.title || '?').charAt(0).toUpperCase();
    head.appendChild(glyph);

    var title = document.createElement('div');
    title.className = 'lp-title';
    var nameTxt = document.createElement('div');
    nameTxt.className = 'lp-name-txt';
    nameTxt.textContent = lib.title;
    var kind = document.createElement('div');
    kind.className = 'lp-kind';
    kind.textContent = 'Plex library' + (lib.type ? ' · ' + lib.type : '');
    title.appendChild(nameTxt);
    title.appendChild(kind);
    head.appendChild(title);

    var usage = document.createElement('div');
    usage.className = 'lp-usage';
    var pct = document.createElement('span');
    pct.className = 'pct';
    pct.dataset.role = 'pct';
    pct.textContent = '—';
    var bytes = document.createElement('span');
    bytes.className = 'bytes';
    bytes.dataset.role = 'bytes';
    bytes.textContent = ' ';
    usage.appendChild(pct);
    usage.appendChild(bytes);
    head.appendChild(usage);
    card.appendChild(head);

    var bar = document.createElement('div');
    bar.className = 'lp-bar2';
    var fill = document.createElement('span');
    fill.className = 'fill';
    fill.dataset.role = 'fill';
    fill.style.width = '0%';
    bar.appendChild(fill);
    var mark = document.createElement('span');
    mark.className = 'thresh-mark';
    mark.dataset.role = 'mark';
    bar.appendChild(mark);
    card.appendChild(bar);

    var body = document.createElement('div');
    body.className = 'lp-body';

    var pathFld = document.createElement('div');
    pathFld.className = 'fld';
    var pathLbl = document.createElement('div');
    pathLbl.className = 'fld-lbl';
    pathLbl.textContent = 'Filesystem path';
    var pathInput = document.createElement('input');
    pathInput.className = 'inp inp--mono';
    pathInput.type = 'text';
    pathInput.placeholder = lib.type === 'show' ? '/media/tv' : '/media/movies';
    pathInput.value = cfg.path || '';
    pathInput.setAttribute('aria-label', 'Filesystem path, ' + lib.title);
    pathInput.dataset.field = 'path';
    pathFld.appendChild(pathLbl);
    pathFld.appendChild(pathInput);
    body.appendChild(pathFld);

    var thrFld = document.createElement('div');
    thrFld.className = 'fld';
    var thrLbl = document.createElement('div');
    thrLbl.className = 'fld-lbl';
    thrLbl.textContent = 'Scan above';
    var thrWrap = document.createElement('div');
    thrWrap.className = 'thresh-field';
    var thrInput = document.createElement('input');
    thrInput.className = 'inp inp--num';
    thrInput.type = 'number';
    thrInput.min = '0';
    thrInput.max = '100';
    thrInput.value = String(shown(cfg).threshold);
    thrInput.setAttribute('aria-label', 'Scan above percent used, ' + lib.title);
    thrInput.dataset.field = 'threshold';
    thrWrap.appendChild(thrInput);
    var unit = document.createElement('span');
    unit.className = 'unit';
    unit.textContent = '% used';
    thrWrap.appendChild(unit);
    thrFld.appendChild(thrLbl);
    thrFld.appendChild(thrWrap);
    body.appendChild(thrFld);

    card.appendChild(body);

    var foot = document.createElement('div');
    foot.className = 'lp-foot';
    var state = document.createElement('span');
    state.dataset.role = 'state';
    foot.appendChild(state);
    card.appendChild(foot);

    card.anew = typedAnew(cfg);
    var asked = null;
    function refresh(again) {
      var typed = typedIn(card);
      var scan = scanOf(card);
      var what = scan.limit ? typed.threshold + ' ' + scan.limit.path : scan.text;
      var known = !!asked && asked.what === what;
      if (known && !again) return;
      var mine = asked = { what: what };
      if (!scan.limit) { paintState(card, scan, typed.threshold); return; }
      // What was read stays in view while it is read again.
      if (!known) paintState(card, { tone: 'untested', text: 'Checking…' }, typed.threshold);
      MM.api.get('/api/settings/disk-usage?path=' + encodeURIComponent(scan.limit.path))
        .then(function (data) { return usageState(data.usage_pct, scan.limit.percent); }, unreadState)
        .then(function (found) { if (mine === asked) paintState(card, found, typed.threshold); });
    }
    function check() { refresh(true); }
    var limitWas = thrInput.value;
    thrInput.addEventListener('input', function (e) {
      card.anew = isAnew(e, thrInput, limitWas, card.anew);
      limitWas = thrInput.value;
      if (unreadOf(DISK_THRESHOLDS[card.dataset.libId]) === undefined) return;
      // The save bar hears the edit first, and a number typed over itself is told by no change event.
      markDirty();
      refresh();
    });
    pathInput.addEventListener('blur', check);
    thrInput.addEventListener('change', check);
    card.refreshState = refresh;
    refresh();

    return card;
  }

  function isObject(value) { return !!value && typeof value === 'object' && !Array.isArray(value); }

  // Empty as the scanner takes it, which counts an empty list or object.
  function isEmpty(value) { return !value || (typeof value === 'object' && !Object.keys(value).length); }

  // A percentage as the scanner reads it, or null where it cannot.
  function percentOf(value) {
    if (typeof value === 'string') return /^\s*[+-]?\d+\s*$/.test(value) ? parseInt(value, 10) : null;
    if (typeof value !== 'number' && typeof value !== 'boolean') return null;
    return isFinite(value) ? Math.trunc(Number(value)) : null;
  }

  // A library's limit as the scanner reads it: null where it is always scanned, 'unreadable' where it never is.
  function limitOf(cfg) {
    if (isEmpty(cfg)) return null;
    if (!isObject(cfg)) return 'unreadable';
    if (cfg.unread !== undefined) {
      var entered = cfg.anew || cfg.threshold !== shown(cfg.unread).threshold;
      var limit = limitOf(shown(cfg));
      return entered && isObject(limit) ? limit : 'unreadable';
    }
    if (isEmpty(cfg.path) || isEmpty(cfg.threshold)) return null;
    var percent = percentOf(cfg.threshold);
    if (percent === null) return 'unreadable';
    if (percent <= 0) return null;
    if (typeof cfg.path !== 'string' || cfg.path.indexOf('\0') !== -1) return 'unreadable';
    return { path: cfg.path, percent: percent };
  }

  // What is typed over an entry the scanner cannot read is kept with that entry, as `unread`, and is a
  // limit only where its number was typed by the person: the one the card shows was never a limit.
  function unreadOf(cfg) {
    if (isObject(cfg) && cfg.unread !== undefined) return cfg.unread;
    return limitOf(cfg) === 'unreadable' ? cfg : undefined;
  }

  function typedAnew(cfg) { return isObject(cfg) && cfg.anew === true; }

  // Whether a limit field now holds a number typed in whole, rather than what was there with a keystroke undone.
  function isAnew(e, input, was, anew) {
    var how = e.inputType || '';
    if (how.indexOf('history') === 0) return false;
    // A number field holding something else, such as 9e, gives its value as empty.
    if (input.validity && input.validity.badInput) return anew;
    if (input.value === '') return true;
    if (how.indexOf('delete') === 0) return anew;
    return anew || (typeof e.data === 'string' ? e.data === input.value : input.value === was);
  }

  // Whether the scanner takes a library, from what is stored or, once changed, what is typed over it:
  // its limit, which the disk then decides, or else what to say.
  function scanOf(card) {
    var typed = typedIn(card);
    var stored = isObject(STORED) ? STORED[card.dataset.libId] : undefined;
    var entry = limitOf(stored) !== 'unreadable' && same(typed, shown(stored)) ? stored : entryOf(card);
    var limit = limitOf(entry);
    if (limit === 'unreadable' && typed.path && typed.path !== shown(unreadOf(entry)).path) {
      return { tone: 'crit', text: 'Saved limit can’t be read — scheduled scans skip this library. This path isn’t saved until you enter a limit, then save.' };
    }
    if (limit === 'unreadable') {
      return { tone: 'crit', text: 'Saved limit can’t be read — scheduled scans skip this library. Enter a path and a limit, then save.' };
    }
    if (limit) return { limit: limit };
    if (UNREADABLE) {
      return { tone: 'crit', text: 'Saved limits can’t be read — scheduled scans skip every library. Give every selected library a path and a limit, then save.' };
    }
    if (typed.threshold > 0 && !typed.path) {
      return { tone: 'warn', text: 'No path, so the limit is ignored — always scans' };
    }
    return { tone: 'untested', text: 'No threshold set — always scans' };
  }

  function usageState(usage, percent) {
    if (usage >= percent) return { tone: 'crit', text: 'Above threshold — will scan', usage: usage };
    if (usage >= percent - 10) return { tone: 'warn', text: 'Approaching threshold — scheduled scans skip it', usage: usage };
    return { tone: 'ok', text: 'Below threshold — scheduled scans skip it', usage: usage };
  }

  function unreadState(err) {
    var code = err && err.error;
    if (code === 'not_allowed') {
      return { tone: 'warn', text: 'This page can’t check this folder. If a scheduled scan can’t read it either, it scans this library.' };
    }
    var text = MM.api.messageOf(err, 'Couldn’t check this folder. Try again.');
    if (code === 'not_found') text += ' While its disk can’t be read, this library is always scanned.';
    if (code === 'fetch_failed') text += ' If a scheduled scan can’t read it either, it scans this library.';
    return { tone: 'crit', text: text };
  }

  function paintState(card, what, threshold) {
    var pct   = card.querySelector('[data-role="pct"]');
    var bytes = card.querySelector('[data-role="bytes"]');
    var fill  = card.querySelector('[data-role="fill"]');
    var mark  = card.querySelector('[data-role="mark"]');
    var state = card.querySelector('[data-role="state"]');
    var read = what.usage !== undefined;
    // Only a disk near or over its limit colours its figure.
    var tone = read && what.tone !== 'ok' ? ' ' + what.tone : '';

    mark.style.left = threshold ? threshold + '%' : '-10px';
    state.className = what.tone;
    state.textContent = what.text;
    pct.className = 'pct' + tone;
    pct.textContent = read ? what.usage.toFixed(1) + '%' : '—';
    bytes.textContent = ' ';
    fill.className = 'fill' + tone;
    fill.style.width = read ? Math.min(100, what.usage) + '%' : '0%';
  }

  // One library's entry as the page shows it; an entry that cannot be read shows as blank.
  function shown(cfg) {
    cfg = cfg || {};
    return { path: String(cfg.path || '').trim(), threshold: parseInt(cfg.threshold, 10) || 0 };
  }

  function same(a, b) {
    return a.path === b.path && a.threshold === b.threshold;
  }

  function typedIn(card) {
    return shown({
      path: card.querySelector('[data-field="path"]').value,
      threshold: card.querySelector('[data-field="threshold"]').value,
    });
  }

  // An entry whose fields were not touched, or were put back over one that cannot be read, stays exactly as it was saved.
  function entryOf(card) {
    var typed = typedIn(card);
    var entry = DISK_THRESHOLDS[card.dataset.libId];
    var unread = unreadOf(entry);
    if (unread === undefined) return same(typed, shown(entry)) ? entry : typed;
    if (same(typed, shown(unread)) && !card.anew) return unread;
    return { path: typed.path, threshold: typed.threshold, unread: unread, anew: card.anew };
  }

  function keepTyped() {
    document.querySelectorAll('#disk-threshold-rows .lp-card').forEach(function (card) {
      var entry = entryOf(card);
      if (entry !== undefined) DISK_THRESHOLDS[card.dataset.libId] = entry;
    });
  }

  // The entries that differ, or null. A cleared one is blank, not left out: the
  // server keeps the threshold of a library it is not sent. One that cannot be read is
  // replaced only by a limit: with none, the library would always be scanned.
  function changes(from, to) {
    var out = null;
    Object.keys(Object.assign({}, from, to)).forEach(function (libId) {
      var now = shown(to[libId]);
      var limit = limitOf(to[libId]);
      if (limit === 'unreadable') return;
      if (limitOf(from[libId]) === 'unreadable' ? !isObject(limit) : same(shown(from[libId]), now)) return;
      out = out || {};
      out[libId] = now;
    });
    return out;
  }

  function loadPlexLibraries() {
    MM.api.get('/api/plex/libraries')
      .then(function (data) {
        PLEX_LIBRARIES = data.libraries || [];
        LOADED = true;
        if (UNREADABLE && PLEX_LIBRARIES.length) offerMissingLibraries();
        renderLibraryPills();
        renderDiskRows();
      })
      .catch(function (err) {
        var notSetUp = !!err && err.error === 'plex_not_configured';
        PLEX_LIBRARIES = [];
        LOADED = false;
        NO_LIBRARIES = notSetUp
          ? 'Save Plex URL & token, then reload to see libraries.'
          : 'Could not load Plex libraries — check Plex URL and token, then reload.';
        renderLibraryPills();
        var rows = document.getElementById('disk-threshold-rows');
        if (rows) {
          var note = document.createElement('div');
          note.className = 'fld-sub';
          note.textContent = notSetUp
            ? 'Paths and thresholds are set here once Plex is.'
            : 'Could not load Plex libraries; disk thresholds are unchanged.';
          rows.replaceChildren(note);
        }
      });
  }

  // Unreadable thresholds are mended only by a limit for every selected library, so one Plex
  // no longer lists has to be there to be taken out.
  function offerMissingLibraries() {
    var listed = PLEX_LIBRARIES.map(function (lib) { return String(lib.id); });
    SELECTED_LIBS.forEach(function (id) {
      if (listed.indexOf(id) === -1) {
        PLEX_LIBRARIES.push({ id: id, title: 'Library ' + id + ' (not in Plex)' });
      }
    });
  }

  function init(opts) {
    var stored = fromServer({
      plex_libraries: opts && opts.selectedLibs,
      disk_thresholds: opts && opts.diskThresholds,
    });
    SELECTED_LIBS = stored.libs;
    DISK_THRESHOLDS = stored.disk;
    loadPlexLibraries();
  }

  function copy(value) { return JSON.parse(JSON.stringify(value)); }

  // The library selection and thresholds as they stand, for a save to send and Discard to go back to.
  function state() {
    keepTyped();
    return copy({ libs: SELECTED_LIBS, disk: DISK_THRESHOLDS });
  }

  function restore(saved) {
    SELECTED_LIBS = copy(saved.libs);
    DISK_THRESHOLDS = copy(saved.disk);
    if (!LOADED) return;
    renderLibraryPills();
    var rows = document.getElementById('disk-threshold-rows');
    if (rows) rows.replaceChildren();
    renderDiskRows();
  }

  function fromServer(data) {
    var thresholds = isEmpty(data.disk_thresholds) ? {} : data.disk_thresholds;
    UNREADABLE = !isObject(thresholds);
    STORED = copy(thresholds);
    return {
      libs: Array.isArray(data.plex_libraries) ? data.plex_libraries.map(String) : [],
      disk: UNREADABLE ? {} : copy(thresholds),
    };
  }

  // `lib:<id>` for each library selected and `limit:<id>` for each entry that is not blank;
  // one that cannot be read is not the same as a limit that shows alike.
  function things(state) {
    var out = {};
    state.libs.forEach(function (id) { out['lib:' + id] = true; });
    Object.keys(state.disk).forEach(function (id) {
      var cfg = shown(state.disk[id]);
      if (cfg.path || cfg.threshold) {
        out['limit:' + id] = (limitOf(state.disk[id]) === 'unreadable' ? 'unreadable ' : '') + cfg.threshold + ' ' + cfg.path;
      }
    });
    return out;
  }

  function rebase(mine, stored, keep) {
    function kept(id) { return keep('lib:' + id); }
    var disk = {};
    Object.keys(Object.assign({}, mine.disk, stored.disk)).forEach(function (id) {
      // A path with no limit yet is not sent, so a save has not stored it.
      var draft = isObject(mine.disk[id]) && mine.disk[id].unread !== undefined && limitOf(mine.disk[id]) === 'unreadable';
      var entry = (draft || keep('limit:' + id) ? mine : stored).disk[id];
      // Typed over an entry that could not be read and since can be: a limit is an ordinary edit, and no more is kept.
      if (unreadOf(entry) !== undefined && limitOf(stored.disk[id]) !== 'unreadable') {
        entry = limitOf(entry) === 'unreadable' ? stored.disk[id] : shown(entry);
      }
      if (entry !== undefined) disk[id] = entry;
    });
    return {
      libs: stored.libs.filter(function (id) { return !kept(id); }).concat(mine.libs.filter(kept)),
      disk: copy(disk),
    };
  }

  function show(state) {
    SELECTED_LIBS = copy(state.libs);
    DISK_THRESHOLDS = copy(state.disk);
    if (!LOADED) return;
    paintPills();
    renderDiskRows();
  }

  function titleOf(id) {
    var lib = PLEX_LIBRARIES.filter(function (l) { return String(l.id) === id; })[0];
    return lib ? lib.title : 'library ' + id;
  }

  MM.settings.diskThresholds = {
    init: init,
    state: state,
    restore: restore,
    changes: changes,
    show: show,
    things: things,
    rebase: rebase,
    fromServer: fromServer,
    titleOf: titleOf,
    getSelectedLibs: function () { return SELECTED_LIBS; },
    stored: function () { return STORED; },
  };
})();
