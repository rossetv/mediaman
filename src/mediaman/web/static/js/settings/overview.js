/**
 * settings/overview.js — overview hero refresh + storage bar.
 *
 * Responsibilities:
 *   - Repaint the "Next scan" line (#ov-scan-big / #ov-scan-when) whenever
 *     the schedule fields change or after a save round-trip.
 *   - Render the storage stack bar (#ov-storage-bar / -chips / -big) by
 *     calling /api/dashboard/stats — shown only when at least one disk
 *     threshold is configured.
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *
 * Exposes:
 *   MM.settings.overview.init({ boot, diskThresholds })
 *   MM.settings.overview.refreshOverviewHero()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var BOOT = {};

  function refreshOverviewHero() {
    var big = document.getElementById('ov-scan-big');
    var when = document.getElementById('ov-scan-when');
    if (!big || !when) return;
    var dayEl  = document.getElementById('scan_day');
    var timeEl = document.getElementById('scan_time');
    var tzEl   = document.getElementById('scan_timezone');
    var days   = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };
    var dayKey = dayEl ? dayEl.value : BOOT.scan_day;
    var day    = days[dayKey] || 'Monday';
    var time   = (timeEl && timeEl.value) || BOOT.scan_time || '09:00';
    var tz     = (tzEl && tzEl.value) || BOOT.scan_timezone || 'UTC';
    big.replaceChildren();
    big.appendChild(document.createTextNode(day + ' '));
    var small = document.createElement('small');
    small.textContent = time;
    big.appendChild(small);
    when.replaceChildren();
    when.appendChild(document.createTextNode('Timezone: '));
    var strong = document.createElement('b');
    strong.textContent = tz;
    when.appendChild(strong);
  }

  function paintStorageBar(diskThresholds) {
    var bar   = document.getElementById('ov-storage-bar');
    var chips = document.getElementById('ov-storage-chips');
    var big   = document.getElementById('ov-storage-big');
    if (!bar || !chips || !big) return;

    function renderUnconfigured() {
      big.textContent = 'Not configured';
      chips.replaceChildren();
      var note = document.createElement('span');
      note.style.color = 'var(--t3)';
      note.textContent = 'Set library paths under Libraries & Paths to populate this view.';
      chips.appendChild(note);
    }

    function renderStorage(s) {
      big.replaceChildren();
      big.appendChild(document.createTextNode(s.used + ' '));
      var small = document.createElement('small');
      small.textContent = 'of ' + s.total + ' used · ' + s.free + ' free';
      big.appendChild(small);

      bar.replaceChildren();
      var segs = [
        { pct: s.movies_pct, bg: 'var(--orange)' },
        { pct: s.tv_pct,     bg: 'var(--accent)' },
        { pct: s.anime_pct,  bg: 'var(--purple)' },
        { pct: s.other_pct,  bg: 'rgba(255,255,255,.2)' },
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

    var entries = Object.keys(diskThresholds || {});
    if (!entries.length) { renderUnconfigured(); return; }

    MM.api.get('/api/dashboard/stats')
      .then(function (data) {
        if (!data || !data.storage) return;
        renderStorage(data.storage);
      })
      .catch(function () { /* leave placeholder — network/backend hiccup */ });
  }

  function init(opts) {
    BOOT = (opts && opts.boot) || {};
    refreshOverviewHero();
    paintStorageBar((opts && opts.diskThresholds) || {});
  }

  MM.settings.overview = {
    init: init,
    refreshOverviewHero: refreshOverviewHero,
  };
})();
