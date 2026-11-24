/**
 * settings/overview.js — overview hero refresh + storage bar.
 *
 * Responsibilities:
 *   - Paint the "Next scan" line (#ov-scan-big / #ov-scan-when) from the saved
 *     schedule, on load and after a save or Discard.
 *   - Render the storage stack bar (#ov-storage-bar / -chips / -big) by
 *     calling /api/dashboard/stats, at the same times — shown only when at
 *     least one disk threshold is saved.
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.settings.savebar.isEdited, MM.settings.diskThresholds.stored
 *
 * Exposes:
 *   MM.settings.overview.init({ boot, diskThresholds })
 *   MM.settings.overview.refreshOverviewHero(sent) — `sent`: the values of the last save, by id
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var DAYS = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };

  // The schedule as the server has it.
  var SCHEDULE = { scan_day: 'mon', scan_time: '09:00', scan_timezone: 'UTC' };

  // A blank value is one the server was not sent or does not keep.
  function setSchedule(values) {
    Object.keys(SCHEDULE).forEach(function (key) { if (values[key]) SCHEDULE[key] = values[key]; });
  }

  function paintSchedule() {
    var big = document.getElementById('ov-scan-big');
    var when = document.getElementById('ov-scan-when');
    if (!big || !when) return;
    var days = String(SCHEDULE.scan_day).split(',').map(function (token) {
      return DAYS[token.trim().toLowerCase()] || token.trim();
    });
    // Several days are named short.
    var day = days.length > 1 ? days.map(function (name) { return name.slice(0, 3); }).join(', ') : days[0];
    big.replaceChildren();
    big.appendChild(document.createTextNode(day + ' '));
    var small = document.createElement('small');
    small.textContent = SCHEDULE.scan_time;
    big.appendChild(small);
    when.replaceChildren();
    when.appendChild(document.createTextNode('Timezone: '));
    var strong = document.createElement('b');
    strong.textContent = SCHEDULE.scan_timezone;
    when.appendChild(strong);
  }

  var storageAsked = 0;

  function paintStorageBar(diskThresholds) {
    var bar   = document.getElementById('ov-storage-bar');
    var chips = document.getElementById('ov-storage-chips');
    var big   = document.getElementById('ov-storage-big');
    if (!bar || !chips || !big) return;

    function renderUnconfigured() {
      big.textContent = 'Not configured';
      bar.replaceChildren();
      chips.replaceChildren();
      var note = document.createElement('span');
      note.style.color = 'var(--t3)';
      note.textContent = 'Set library paths under Libraries & Paths to populate this view.';
      chips.appendChild(note);
    }

    function renderStorage(s) {
      big.replaceChildren();
      // Non-breaking spaces keep each figure with its unit, and the dot with
      // the figure after it, so a wrapped line can start with "·" but never end with it.
      var nb = function (v) { return String(v).replace(/ /g, '\u00a0'); };
      big.appendChild(document.createTextNode(nb(s.used) + ' '));
      var small = document.createElement('small');
      small.textContent = 'of ' + nb(s.total) + ' used ·\u00a0' + nb(s.free) + ' free';
      big.appendChild(small);

      bar.replaceChildren();
      var segs = [
        { pct: s.movies_pct, bg: 'var(--orange)' },
        { pct: s.tv_pct,     bg: 'var(--accent)' },
        { pct: s.anime_pct,  bg: 'var(--purple)' },
        { pct: s.other_pct,  bg: 'var(--t4)' },
      ];
      segs.forEach(function (seg) {
        if (!seg.pct || seg.pct <= 0) return;
        var span = document.createElement('span');
        span.style.flex = '0 0 ' + seg.pct + '%';
        span.style.background = seg.bg;
        bar.appendChild(span);
      });

      chips.replaceChildren();
      var legend = [
        { bg: 'var(--orange)',        label: 'Movies',   val: s.movies_label, pct: s.movies_pct },
        { bg: 'var(--accent)',        label: 'TV Shows', val: s.tv_label,     pct: s.tv_pct     },
        { bg: 'var(--purple)',        label: 'Anime',    val: s.anime_label,  pct: s.anime_pct  },
        { bg: 'rgba(255,255,255,.3)', label: 'Other',    val: s.other_label,  pct: s.other_pct  },
      ];
      legend.forEach(function (item) {
        var row = document.createElement('span');
        var dot = document.createElement('span');
        dot.className = 'chip-dot';
        dot.style.background = item.bg;
        row.appendChild(dot);
        row.appendChild(document.createTextNode(item.label + ' '));
        var b = document.createElement('b');
        b.textContent = item.val + ' (' + item.pct + '%)';
        row.appendChild(b);
        chips.appendChild(row);
      });
    }

    var mine = ++storageAsked;
    var entries = Object.keys(diskThresholds || {});
    if (!entries.length) { renderUnconfigured(); return; }

    MM.api.get('/api/dashboard/stats')
      .then(function (data) {
        if (mine !== storageAsked || !data || !data.storage) return;
        renderStorage(data.storage);
      })
      .catch(function () { /* leave placeholder — network/backend hiccup */ });
  }

  // A field changed since the last save was sent shows something else than the server has.
  function refreshOverviewHero(sent) {
    var saved = {};
    Object.keys(SCHEDULE).forEach(function (key) {
      var el = document.getElementById(key);
      if (!el) return;
      saved[key] = MM.settings.savebar.isEdited(el) ? sent && sent[key] : el.value;
    });
    setSchedule(saved);
    paintSchedule();
    paintStorageBar(MM.settings.diskThresholds.stored());
  }

  function init(opts) {
    setSchedule((opts && opts.boot) || {});
    paintSchedule();
    paintStorageBar((opts && opts.diskThresholds) || {});
  }

  MM.settings.overview = {
    init: init,
    refreshOverviewHero: refreshOverviewHero,
  };
})();
