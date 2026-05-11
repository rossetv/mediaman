/**
 * downloads.js — page entry point for /downloads.
 *
 * Polls /api/downloads on a 2 s cadence and updates the hero / queue /
 * upcoming / recent grid in place. After Phase 8B the heavy lifting
 * lives in:
 *   - downloads/build_dom.js     — DOM helpers + placeholder builders
 *   - downloads/render_hero.js   — patch the hero card in place
 *   - downloads/render_row.js    — patch compact + upcoming rows
 *   - downloads/render_recent.js — recently-added grid + upcoming list
 *   - downloads/poll.js          — fetch loop + visibility/poll:now
 *
 * This file owns the cross-module wiring: it owns the root element, the
 * episode-toggle click delegation, and the `update(data)` reducer that
 * fans the poll payload out to the renderer modules.
 *
 * Cross-module dependencies:
 *   MM.downloads.buildDom
 *   MM.downloads.renderHero
 *   MM.downloads.renderRow
 *   MM.downloads.renderRecent
 *   MM.downloads.poll
 */
(function () {
  'use strict';

  var root = document.getElementById('dl-root');
  if (!root) return;

  function buildDom()     { return MM.downloads.buildDom; }
  function renderHero()   { return MM.downloads.renderHero; }
  function renderRow()    { return MM.downloads.renderRow; }
  function renderRecent() { return MM.downloads.renderRecent; }

  /* ── Main update ── */
  function update(data) {
    var dom = buildDom();
    var heroContainer = document.getElementById('dl-hero-container');
    var queueContainer = document.getElementById('dl-queue-container');
    var emptyEl = document.getElementById('dl-empty');
    var recentContainer = document.getElementById('dl-recent-container');
    var header = root.querySelector('.page-header');
    var subtitle = header ? header.querySelector('p') : null;

    var totalActive = (data.hero ? 1 : 0) + (data.queue ? data.queue.length : 0);

    /* Update subtitle */
    if (subtitle) {
      if (totalActive > 0) {
        dom.setText(subtitle, totalActive + ' item' + (totalActive !== 1 ? 's' : '') + ' in progress');
      } else {
        dom.setText(subtitle, '');
      }
    }

    /* Hero */
    if (data.hero) {
      if (!heroContainer) {
        heroContainer = document.createElement('div');
        heroContainer.id = 'dl-hero-container';
        heroContainer.appendChild(dom.buildHeroPlaceholder(data.hero.id));
        if (header) header.after(heroContainer);
      }
      renderHero().updateHero(heroContainer, data.hero);
      /* Remove empty state */
      if (emptyEl) { emptyEl.style.display = 'none'; }
    } else {
      if (heroContainer) { while (heroContainer.firstChild) heroContainer.removeChild(heroContainer.firstChild); }
    }

    /* Queue */
    if (data.queue && data.queue.length > 0) {
      if (!queueContainer) {
        queueContainer = document.createElement('div');
        queueContainer.id = 'dl-queue-container';
        var qHeader = document.createElement('div');
        qHeader.className = 'dl-compact-header';
        qHeader.textContent = 'Also downloading';
        queueContainer.appendChild(qHeader);
        var qList = document.createElement('div');
        qList.className = 'dl-compact-list';
        qList.id = 'dl-queue-list';
        queueContainer.appendChild(qList);
        var insertAfter = heroContainer || (header ? header : root.firstElementChild);
        insertAfter.after(queueContainer);
      }
      var list = document.getElementById('dl-queue-list');
      if (list) {
        /* Track active IDs */
        var activeIds = {};
        for (var i = 0; i < data.queue.length; i++) {
          activeIds[data.queue[i].id] = true;
          var row = dom.findByDlId(list, data.queue[i].id);
          if (row) {
            renderRow().updateCompactRow(row, data.queue[i]);
          }
          /* New items will appear on next full page load — keep it simple */
        }
        /* Remove departed rows */
        var rows = list.querySelectorAll('.dl-compact');
        for (var j = 0; j < rows.length; j++) {
          var id = rows[j].getAttribute('data-dl-id');
          if (id && !activeIds[id]) {
            rows[j].classList.add('dl-card-exit');
            (function (el) {
              el.addEventListener('animationend', function () {
                if (el.parentNode) el.parentNode.removeChild(el);
              }, { once: true });
            })(rows[j]);
          }
        }
      }
      /* Remove empty state */
      if (emptyEl) { emptyEl.style.display = 'none'; }
    } else {
      if (queueContainer) {
        while (queueContainer.firstChild) queueContainer.removeChild(queueContainer.firstChild);
      }
    }

    /* Empty state */
    if (!data.hero && (!data.queue || data.queue.length === 0)) {
      if (!emptyEl) {
        emptyEl = dom.buildEmptyState();
        var afterHeader = heroContainer || queueContainer || header;
        if (afterHeader) afterHeader.after(emptyEl);
      }
      emptyEl.style.display = '';
    }

    /* Upcoming */
    renderRecent().updateUpcomingList(root, data);

    /* Recent */
    renderRecent().updateRecent(recentContainer, data.recent);
  }

  /* ── Episode toggle ── */
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-v="ep-toggle"]');
    if (!toggle) return;
    toggle.classList.toggle('open');
    var list = toggle.nextElementSibling;
    if (list && list.getAttribute('data-v') === 'ep-list') {
      list.style.display = list.style.display === 'none' ? '' : 'none';
    }
  });

  /* Kick off the polling loop. */
  MM.downloads.poll.start(update);
}());
