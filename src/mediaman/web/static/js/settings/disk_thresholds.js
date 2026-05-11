/**
 * settings/disk_thresholds.js — Plex library pills + per-library disk cards.
 *
 * Responsibilities:
 *   - Fetch /api/plex/libraries and paint the pill picker
 *   - Maintain the selected-library set
 *   - Build the per-library "lp-card" (path + threshold + live usage bar)
 *   - Live-poll /api/settings/disk-usage as the user edits paths
 *   - Collect the disk-threshold map for the save payload
 *
 * Cross-module dependencies:
 *   MM.api                    (core/api.js)
 *   MM.settings.savebar.markDirty
 *
 * Exposes:
 *   MM.settings.diskThresholds.init({ selectedLibs, diskThresholds })
 *   MM.settings.diskThresholds.collect()                — for collectSettings
 *   MM.settings.diskThresholds.getSelectedLibs()        — for collectSettings
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var PLEX_LIBRARIES = [];
  var SELECTED_LIBS = [];
  var DISK_THRESHOLDS = {};

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
      var pill = document.createElement('span');
      pill.className = 'lib-pill' + (SELECTED_LIBS.indexOf(String(lib.id)) !== -1 ? ' on' : '');
      pill.textContent = lib.title;
      pill.dataset.libId = String(lib.id);
      pill.addEventListener('click', function () {
        var idx = SELECTED_LIBS.indexOf(this.dataset.libId);
        if (idx === -1) { SELECTED_LIBS.push(this.dataset.libId); this.classList.add('on'); }
        else { SELECTED_LIBS.splice(idx, 1); this.classList.remove('on'); }
        renderDiskRows();
        updateLibPillCount();
        markDirty();
      });
      container.appendChild(pill);
    });
    updateLibPillCount();
  }

  function updateLibPillCount() {
    var el = document.getElementById('lib-pill-count');
    if (!el) return;
    el.textContent = SELECTED_LIBS.length + ' of ' + PLEX_LIBRARIES.length + ' selected';
  }

  function renderDiskRows() {
    var container = document.getElementById('disk-threshold-rows');
    if (!container) return;
    container.replaceChildren();
    var selected = PLEX_LIBRARIES.filter(function (l) { return SELECTED_LIBS.indexOf(String(l.id)) !== -1; });
    if (!selected.length) {
      var hint = document.createElement('div');
      hint.className = 'fld-sub';
      hint.textContent = 'Select Plex libraries above to configure paths and thresholds.';
      container.appendChild(hint);
      return;
    }
    selected.forEach(function (lib) {
      var cfg = DISK_THRESHOLDS[String(lib.id)] || {};
      container.appendChild(buildLibPathCard(lib, cfg));
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
    pathInput.placeholder = '/media/movies';
    pathInput.value = cfg.path || '';
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
        .catch(function () { paintState(card, 'err', threshold, 'Fetch error'); });
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

  function collect() {
    var out = {};
    document.querySelectorAll('#disk-threshold-rows .lp-card').forEach(function (card) {
      var libId = card.dataset.libId;
      var path = card.querySelector('[data-field="path"]').value.trim();
      var threshold = parseInt(card.querySelector('[data-field="threshold"]').value, 10) || 0;
      if (path || threshold) { out[libId] = { path: path, threshold: threshold }; }
    });
    return out;
  }

  function loadPlexLibraries() {
    MM.api.get('/api/plex/libraries')
      .then(function (data) {
        PLEX_LIBRARIES = data.libraries || [];
        renderLibraryPills();
        renderDiskRows();
      })
      .catch(function () { PLEX_LIBRARIES = []; renderLibraryPills(); });
  }

  function init(opts) {
    SELECTED_LIBS = ((opts && opts.selectedLibs) || []).map(String);
    DISK_THRESHOLDS = (opts && opts.diskThresholds) || {};
    loadPlexLibraries();
  }

  MM.settings.diskThresholds = {
    init: init,
    collect: collect,
    getSelectedLibs: function () { return SELECTED_LIBS; },
  };
})();
