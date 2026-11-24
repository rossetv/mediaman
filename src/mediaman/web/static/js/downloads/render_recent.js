/**
 * downloads/render_recent.js — recently-added grid and upcoming list.
 *
 * The recent-grid is rebuilt only when its titles or posters change, so
 * images do not reload and focus survives each poll. The upcoming list diffs by
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

  function recentSignature(titles, posters) {
    return JSON.stringify([titles, posters]);
  }

  /* The signature of what is on screen, read back from the DOM so the
     server-rendered grid compares equal to the first poll's payload. */
  function renderedSignature(container) {
    var titles = [];
    var posters = [];
    var items = container.querySelectorAll('.dl-recent-item');
    for (var i = 0; i < items.length; i++) {
      var t = items[i].querySelector('.dl-recent-title');
      var img = items[i].querySelector('img');
      titles.push(t ? t.textContent : '');
      posters.push(img ? img.getAttribute('src') : '');
    }
    return recentSignature(titles, posters);
  }

  function updateRecent(container, recent) {
    if (!container) return;
    var d = dom();
    var shown = container.querySelector('.dl-recent-section');
    if (!recent || recent.length === 0) {
      if (shown) {
        if (!d.isCollapsing(shown)) d.collapseAway(shown);
      } else {
        while (container.firstChild) container.removeChild(container.firstChild);
      }
      return;
    }
    if (shown && d.isCollapsing(shown)) d.reveal(shown);
    var incoming = recentSignature(
      recent.map(function (r) { return r.title; }),
      recent.map(function (r) { return r.poster_url || ''; })
    );
    if (renderedSignature(container) === incoming) return;
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

    var shown = container ? container.querySelectorAll('.dl-upcoming-row') : [];
    if (upcoming.length === 0) {
      if (container && !d.isCollapsing(container) && !d.dropAbsent(shown, {}, data)) {
        d.collapseAway(container);
      }
      return;
    }

    var opening = !container || d.isCollapsing(container);
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

    var present = {};
    for (var i = 0; i < upcoming.length; i++) {
      var item = upcoming[i];
      present[item.id] = true;
      var rowEl = d.findByDlId(listEl, item.id);
      if (rowEl) {
        if (d.isCollapsing(rowEl)) d.reveal(rowEl);
        row().updateUpcomingRow(rowEl, item);
      } else {
        rowEl = listEl.appendChild(d.buildUpcomingRow(item));
        if (!opening) d.reveal(rowEl);
      }
    }
    d.dropAbsent(shown, present, data);
    if (opening) d.reveal(container);
  }


  MM.downloads.renderRecent = {
    updateRecent: updateRecent,
    updateUpcomingList: updateUpcomingList,
  };
})();
