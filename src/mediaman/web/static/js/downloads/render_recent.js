/**
 * downloads/render_recent.js — recently-added grid and upcoming list.
 *
 * The recent-grid is rebuilt wholesale on every poll (the result set is
 * small, and the items are stateless). The upcoming list diffs by
 * `data-dl-id` so rows survive across polls and abandon-clicks animate
 * out cleanly.
 *
 * Cross-module dependencies:
 *   MM.downloads.buildDom
 *   MM.downloads.renderRow.updateUpcomingRow
 *
 * Exposes:
 *   MM.downloads.renderRecent.updateRecent(container, recent)
 *   MM.downloads.renderRecent.updateUpcomingList(root, data)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  function dom() { return MM.downloads.buildDom; }
  function row() { return MM.downloads.renderRow; }

  function updateRecent(container, recent) {
    if (!container) return;
    if (!recent || recent.length === 0) {
      while (container.firstChild) container.removeChild(container.firstChild);
      return;
    }
    var d = dom();
    /* Rebuild the recent grid using safe DOM methods */
    var section = document.createElement('div');
    section.className = 'dl-recent-section';
    section.id = 'dl-recent';

    var header = document.createElement('div');
    header.className = 'dl-recent-header';
    header.textContent = 'Recently added';
    section.appendChild(header);

    var grid = document.createElement('div');
    grid.className = 'dl-recent-grid';
    for (var i = 0; i < recent.length; i++) {
      grid.appendChild(d.buildRecentItem(recent[i]));
    }
    section.appendChild(grid);

    while (container.firstChild) container.removeChild(container.firstChild);
    container.appendChild(section);
  }

  function updateUpcomingList(root, data) {
    var d = dom();
    var container = document.getElementById('dl-upcoming-container');
    var upcoming = data.upcoming || [];

    if (upcoming.length === 0) {
      if (container) container.parentNode.removeChild(container);
      return;
    }

    if (!container) {
      container = document.createElement('div');
      container.id = 'dl-upcoming-container';
      var header = document.createElement('div');
      header.className = 'dl-compact-header';
      header.textContent = 'Coming soon';
      container.appendChild(header);
      var list = document.createElement('div');
      list.className = 'dl-compact-list';
      list.id = 'dl-upcoming-list';
      container.appendChild(list);

      /* Insert before the recent container */
      var recent = document.getElementById('dl-recent-container');
      if (recent && recent.parentNode) {
        recent.parentNode.insertBefore(container, recent);
      } else if (root) {
        root.appendChild(container);
      }
    }

    var listEl = document.getElementById('dl-upcoming-list');
    if (!listEl) return;

    var activeIds = {};
    for (var i = 0; i < upcoming.length; i++) {
      var item = upcoming[i];
      activeIds[item.id] = true;
      var rowEl = d.findByDlId(listEl, item.id);
      if (rowEl) {
        row().updateUpcomingRow(rowEl, item);
      } else {
        listEl.appendChild(d.buildUpcomingRow(item));
      }
    }
    /* Remove departed rows */
    var rows = listEl.querySelectorAll('.dl-upcoming-row');
    for (var j = 0; j < rows.length; j++) {
      var id = rows[j].getAttribute('data-dl-id');
      if (id && !activeIds[id]) {
        rows[j].parentNode.removeChild(rows[j]);
      }
    }
  }

  MM.downloads.renderRecent = {
    updateRecent: updateRecent,
    updateUpcomingList: updateUpcomingList,
  };
})();
