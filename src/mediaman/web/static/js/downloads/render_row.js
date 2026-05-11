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
    d.setText(d.q('.dl-compact-title', row), item.title);
    var pill = d.q('.dl-state-pill', row);
    if (pill) {
      pill.className = 'dl-state-pill dl-state-' + item.state;
      pill.style.fontSize = '9px';
      d.setText(pill, d.stateLabel(item.state));
    }
    if (hero().updateSearchHint) hero().updateSearchHint(row, item);
    var fill = d.q('[data-v="fill"]', row);
    if (fill) {
      fill.style.width = item.progress + '%';
      fill.className = 'dl-compact-fill' + (item.state === 'almost_ready' ? ' green' : '');
    }
    var pct = d.q('[data-v="pct"]', row);
    if (pct) d.setText(pct, item.progress > 0 ? (item.progress + '%') : '—');
    var sizeInfo = d.q('[data-v="size-info"]', row);
    if (sizeInfo) sizeInfo.style.display = (item.state === 'searching') ? 'none' : '';
    var sizeDone = d.q('[data-v="size-done"]', row);
    if (sizeDone) d.setText(sizeDone, item.size_done || '');
    var sizeTotal = d.q('[data-v="size-total"]', row);
    if (sizeTotal) d.setText(sizeTotal, item.size_total || '');
    var eta = d.q('[data-v="eta"]', row);
    if (eta) d.setText(eta, item.eta || '');

    /* Update episode rows (same shape as hero) */
    if (item.episodes) {
      for (var i = 0; i < item.episodes.length; i++) {
        var ep = item.episodes[i];
        var epRow = d.findByEp(row, ep.label);
        if (!epRow) continue;
        var epFill = d.q('[data-v="ep-fill"]', epRow);
        if (epFill) {
          epFill.style.width = ep.progress + '%';
          epFill.className = 'dl-ep-mini-fill' + (ep.state === 'ready' ? ' green' : '');
        }
        var epPill = d.q('.dl-ep-status-pill', epRow);
        if (epPill) {
          epPill.className = 'dl-ep-status-pill ' + ep.state;
          if (ep.state === 'ready') d.setText(epPill, 'Ready');
          else if (ep.state === 'downloading') d.setText(epPill, ep.progress + '%');
          else if (ep.state === 'queued') d.setText(epPill, 'Queued');
          else d.setText(epPill, 'Searching');
        }
      }
    }
  }

  function updateUpcomingRow(row, item) {
    var d = dom();
    d.setText(d.q('.dl-compact-title', row), item.title);
    var pill = d.q('[data-v="release-label"]', row);
    if (pill) d.setText(pill, item.release_label || '');
  }

  MM.downloads.renderRow = {
    updateCompactRow: updateCompactRow,
    updateUpcomingRow: updateUpcomingRow,
  };
})();
