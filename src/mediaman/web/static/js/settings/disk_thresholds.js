/**
 * settings/disk_thresholds.js — Plex library pills + per-library disk cards.
 *
 * Responsibilities:
 *   - Fetch /api/plex/libraries and paint the picker, a toggle button for each
 *   - Maintain the selected-library set
 *   - Build the per-library "lp-card" (path + threshold + live usage bar)
 *   - Live-poll /api/settings/disk-usage as the user edits paths
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
  // What is stored is no object of entries, which keeps every library from being scanned.
  var UNREADABLE = false;

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
      msg.textContent = 'No libraries found — check Plex URL and token, then reload.';
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
      if (kept[id]) { next = kept[id].nextSibling; return; }
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
    thrInput.value = cfg.threshold || 0;
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
    state.className = 'untested';
    state.dataset.role = 'state';
    state.textContent = cfg.threshold ? 'Checking…' : 'No threshold set — always scans';
    foot.appendChild(state);
    card.appendChild(foot);

    function refresh() {
      var path = pathInput.value.trim();
      var threshold = parseInt(thrInput.value, 10) || 0;
      if (!path || threshold <= 0) {
        paintState(card, null, threshold);
        return;
      }
      MM.api.get('/api/settings/disk-usage?path=' + encodeURIComponent(path))
        .then(function (data) {
          if (data.error) { paintState(card, 'err', threshold, data.error); return; }
          paintState(card, null, threshold, null, data.usage_pct);
        })
        .catch(function (err) { paintState(card, 'err', threshold, MM.api.messageOf(err, 'Couldn’t check this folder. Try again.')); });
    }
    pathInput.addEventListener('blur', refresh);
    thrInput.addEventListener('change', refresh);
    if (cfg.path && cfg.threshold) refresh();

    return card;
  }

  function paintState(card, force, threshold, errMsg, usagePct) {
    var pct   = card.querySelector('[data-role="pct"]');
    var bytes = card.querySelector('[data-role="bytes"]');
    var fill  = card.querySelector('[data-role="fill"]');
    var mark  = card.querySelector('[data-role="mark"]');
    var state = card.querySelector('[data-role="state"]');

    mark.style.left = threshold ? threshold + '%' : '-10px';

    if (force === 'err') {
      state.className = 'crit';
      state.textContent = errMsg || 'Path error';
      pct.textContent = '—';
      bytes.textContent = ' ';
      fill.style.width = '0%';
      return;
    }
    if (!threshold || threshold <= 0) {
      state.className = 'untested';
      state.textContent = 'No threshold set — always scans';
      pct.textContent = '—';
      bytes.textContent = ' ';
      fill.style.width = '0%';
      return;
    }
    if (usagePct === null || usagePct === undefined) {
      state.className = 'untested';
      state.textContent = 'Enter a path to check usage';
      pct.textContent = '—';
      bytes.textContent = ' ';
      fill.style.width = '0%';
      return;
    }
    pct.textContent = usagePct.toFixed(1) + '%';
    fill.style.width = Math.min(100, usagePct) + '%';
    if (usagePct >= threshold) {
      pct.className = 'pct crit';
      fill.className = 'fill crit';
      state.className = 'crit';
      state.textContent = 'Above threshold — will scan';
    } else if (usagePct >= threshold - 10) {
      pct.className = 'pct warn';
      fill.className = 'fill warn';
      state.className = 'warn';
      state.textContent = 'Approaching threshold';
    } else {
      pct.className = 'pct';
      fill.className = 'fill';
      state.className = 'ok';
      state.textContent = 'Below threshold — will skip';
    }
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

  // An entry whose fields were not touched stays exactly as it was saved.
  function keepTyped() {
    document.querySelectorAll('#disk-threshold-rows .lp-card').forEach(function (card) {
      var typed = typedIn(card);
      if (!same(typed, shown(DISK_THRESHOLDS[card.dataset.libId]))) {
        DISK_THRESHOLDS[card.dataset.libId] = typed;
      }
    });
  }

  // The entries that differ, or null. A cleared one is blank, not left out: the
  // server keeps the threshold of a library it is not sent.
  function changes(from, to) {
    var out = null;
    Object.keys(Object.assign({}, from, to)).forEach(function (libId) {
      var now = shown(to[libId]);
      if (same(shown(from[libId]), now)) return;
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
      .catch(function () {
        PLEX_LIBRARIES = [];
        LOADED = false;
        renderLibraryPills();
        var rows = document.getElementById('disk-threshold-rows');
        if (rows) {
          var note = document.createElement('div');
          note.className = 'fld-sub';
          note.textContent = 'Could not load Plex libraries; disk thresholds are unchanged.';
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

  function isObject(value) { return !!value && typeof value === 'object' && !Array.isArray(value); }

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
    var thresholds = data.disk_thresholds || {};
    UNREADABLE = !isObject(thresholds);
    return {
      libs: Array.isArray(data.plex_libraries) ? data.plex_libraries.map(String) : [],
      disk: UNREADABLE ? {} : copy(thresholds),
    };
  }

  // `lib:<id>` for each library selected and `limit:<id>` for each entry that is not blank.
  function things(state) {
    var out = {};
    state.libs.forEach(function (id) { out['lib:' + id] = true; });
    Object.keys(state.disk).forEach(function (id) {
      var cfg = shown(state.disk[id]);
      if (cfg.path || cfg.threshold) out['limit:' + id] = cfg.threshold + ' ' + cfg.path;
    });
    return out;
  }

  function rebase(mine, stored, keep) {
    function kept(id) { return keep('lib:' + id); }
    var disk = {};
    Object.keys(Object.assign({}, mine.disk, stored.disk)).forEach(function (id) {
      var entry = (keep('limit:' + id) ? mine : stored).disk[id];
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
  };
})();
