/**
 * downloads/render_row.js — patch compact and upcoming rows in place.
 *
 * Cross-module dependencies:
 *   MM.downloads.buildDom
 *   MM.downloads.renderHero.updateSearchHint   — compact rows reuse the
 *                                                hero hint behaviour
 *
 * Exposes:
 *   MM.downloads.renderRow.updateCompactRow(row, item)
 *   MM.downloads.renderRow.updateUpcomingRow(row, item)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  function dom()     { return MM.downloads.buildDom; }
  function hero()    { return MM.downloads.renderHero; }

  function updateCompactRow(row, item) {
    var d = dom();
    d.syncPoster(d.q('.dl-compact-poster', row), item.poster_url);
    d.setText(d.q('.dl-compact-title', row), item.title);
    var pill = d.q('.dl-state-pill', row);
    if (pill) {
      pill.className = pill.className.replace(/\bdl-state-(?!pill)\S*/g, '').trim() + ' dl-state-' + item.state;
      d.setText(pill, item.state_label || item.state);
    }
    if (hero().updateSearchHint) hero().updateSearchHint(row, item);
    /* Nothing more is coming of one that needs attention: no bar, no share of it. */
    var held = item.state === 'needs_attention';
    var fill = d.q('[data-v="fill"]', row);
    if (fill) {
      d.setProgress(fill, item.progress);
      fill.className = 'dl-compact-fill' + (item.state === 'almost_ready' ? ' green' : '');
      fill.parentNode.hidden = held;
    }
    var pct = d.q('[data-v="pct"]', row);
    if (pct) {
      d.setText(pct, item.progress > 0 && !held ? (item.progress + '%') : '—');
      pct.classList.toggle('dl-compact-pct--muted', !(item.progress > 0) || held);
    }
    var sizeInfo = d.q('[data-v="size-info"]', row);
    if (sizeInfo) sizeInfo.hidden = (item.state === 'searching');
    var sizeDone = d.q('[data-v="size-done"]', row);
    if (sizeDone) d.setText(sizeDone, item.size_done || '');
    var sizeTotal = d.q('[data-v="size-total"]', row);
    if (sizeTotal) d.setText(sizeTotal, item.size_total || '');
    var eta = d.q('[data-v="eta"]', row);
    if (eta) d.setText(eta, item.eta || '');

    d.syncAbandon(row, item, 'row');

    d.syncEpisodes(row, item);
  }

  function updateUpcomingRow(row, item) {
    var d = dom();
    d.syncPoster(d.q('.dl-compact-poster', row), item.poster_url);
    d.setText(d.q('.dl-compact-title', row), item.title);
    var pill = d.q('[data-v="release-label"]', row);
    if (pill) d.setText(pill, item.release_label || '');
    var btn = d.q('[data-abandon-trigger]', row);
    if (btn) d.fillAbandonButton(btn, item);
  }

  MM.downloads.renderRow = {
    updateCompactRow: updateCompactRow,
    updateUpcomingRow: updateUpcomingRow,
  };
})();
