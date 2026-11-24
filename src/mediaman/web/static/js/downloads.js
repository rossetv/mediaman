/**
 * downloads.js — page entry point for /downloads.
 *
 * Polls /api/downloads on a 2 s cadence and updates the hero / queue /
 * upcoming / recent grid in place. The heavy lifting
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


  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  /* Close a section that has content, emptying it once it has closed. */
  function closeSection(dom, box) {
    if (box && box.firstElementChild && !dom.isCollapsing(box)) dom.collapse(box, function () { clear(box); });
  }

  var lastHero = null;

  function heldHero(dom, box, data) {
    var card = box && !dom.isCollapsing(box) && box.querySelector('.dl-hero');
    if (!card) return null;
    var title = card.querySelector('.dl-hero-title');
    return dom.held(card.getAttribute('data-dl-id'), title ? title.textContent : '', data) ? card : null;
  }

  /* The order the server sorts states in. */
  var STATES = ['downloading', 'almost_ready', 'queued', 'searching'];

  function rankOf(state) {
    var rank = STATES.indexOf(state);
    return rank === -1 ? STATES.length : rank;
  }

  function stateRank(card) {
    var pill = card.querySelector('.dl-state-pill');
    var state = pill && /\bdl-state-(?!pill)(\S+)/.exec(pill.className);
    return rankOf(state ? state[1] : '');
  }

  /* Rows keep their place among those of the same state. */
  function orderRows(list) {
    var rows = Array.prototype.slice.call(list.querySelectorAll('.dl-compact'));
    var ranks = rows.map(stateRank);
    var order = rows.map(function (_, i) { return i; }).sort(function (a, b) { return ranks[a] - ranks[b] || a - b; });
    var next = list.firstElementChild;
    order.forEach(function (i) {
      if (rows[i] === next) next = next.nextElementSibling;
      else list.insertBefore(rows[i], next);
    });
  }

  function shownIn(dom, box, selector) {
    if (!box || dom.isCollapsing(box)) return [];
    return Array.prototype.filter.call(box.querySelectorAll(selector), function (row) { return !dom.isCollapsing(row); });
  }

  /* The server's line counts what it lists, so while rows are held the same
     line (_downloads_subtitle in download_queue) is built from what is shown. */
  function subtitleFor(dom, data) {
    var active = shownIn(dom, document.getElementById('dl-hero-container'), '.dl-hero')
      .concat(shownIn(dom, document.getElementById('dl-queue-container'), '.dl-compact'));
    var coming = shownIn(dom, document.getElementById('dl-upcoming-container'), '.dl-upcoming-row');
    var anyHeld = active.concat(coming).some(function (row) { return row.classList.contains('is-held'); });
    if (!anyHeld || !data.outage) return data.subtitle;
    var soon = coming.length ? ' · ' + coming.length + ' coming soon' : '';
    if (!active.length) return data.outage + soon;
    return active.length + ' item' + (active.length !== 1 ? 's' : '') + ' in progress' + soon +
      ' · ' + data.outage.charAt(0).toLowerCase() + data.outage.slice(1);
  }

  function updateSubtitle(dom, header, text) {
    var subtitle = header.querySelector('p');
    if (text) {
      if (!subtitle) {
        subtitle = document.createElement('p');
        header.appendChild(subtitle);
        dom.setText(subtitle, text);
        dom.reveal(subtitle);
      } else {
        dom.setText(subtitle, text);
        if (dom.isCollapsing(subtitle)) dom.reveal(subtitle);
      }
    } else if (subtitle && !dom.isCollapsing(subtitle)) {
      dom.collapseAway(subtitle);
    }
  }

  function update(data) {
    var dom = buildDom();
    var heroContainer = document.getElementById('dl-hero-container');
    var queueContainer = document.getElementById('dl-queue-container');
    var emptyContainer = document.getElementById('dl-empty-container');
    var recentContainer = document.getElementById('dl-recent-container');
    var header = root.querySelector('.ph');
    var held = false;  // something stays shown because its service does not answer

    /* A hero whose service went down keeps its place above one further from done. */
    var kept = heldHero(dom, heroContainer, data);
    if (kept && data.hero && stateRank(kept) < rankOf(data.hero.state)) {
      data = Object.assign({}, data, { hero: null, queue: [data.hero].concat(data.queue || []) });
    }

    /* Hero — the card is rebuilt when the container was emptied by an
       earlier poll that had no hero. */
    if (data.hero) {
      if (!heroContainer) {
        heroContainer = document.createElement('div');
        heroContainer.id = 'dl-hero-container';
        if (header) header.after(heroContainer);
      }
      if (!heroContainer.querySelector('.dl-hero')) {
        heroContainer.appendChild(dom.buildHeroPlaceholder(data.hero.id));
        renderHero().updateHero(heroContainer, data.hero);
        dom.reveal(heroContainer);
      } else {
        if (dom.isCollapsing(heroContainer)) dom.reveal(heroContainer);
        renderHero().updateHero(heroContainer, data.hero);
      }
    } else if (kept) {
      held = true;
      dom.markHeld(kept);
    } else {
      lastHero = null;
      closeSection(dom, heroContainer);
    }

    /* Queue — the item that is now the hero is no row any more. */
    var present = {};
    if (data.hero) present[data.hero.id] = false;
    var queue = data.queue || [];
    /* A hero whose service went down gives way to another item and becomes a row. */
    var demoted = data.hero && lastHero && lastHero.id !== data.hero.id &&
        dom.held(lastHero.id, lastHero.title, data) ? lastHero : null;
    if (demoted) queue = queue.concat(demoted);
    if (data.hero) lastHero = data.hero;
    if (queue.length > 0) {
      if (!queueContainer) {
        queueContainer = document.createElement('div');
        queueContainer.id = 'dl-queue-container';
        var insertAfter = heroContainer || (header ? header : root.firstElementChild);
        insertAfter.after(queueContainer);
      }
      var list = document.getElementById('dl-queue-list');
      /* A section that opens brings its rows with it; only rows added to an
         open list open on their own. */
      var opening = !list || dom.isCollapsing(queueContainer);
      if (!list) {
        var qHeader = document.createElement('div');
        qHeader.className = 'dl-compact-header';
        qHeader.textContent = 'Also downloading';
        queueContainer.appendChild(qHeader);
        list = document.createElement('div');
        list.className = 'dl-compact-list';
        list.id = 'dl-queue-list';
        queueContainer.appendChild(list);
      }
      var added = [];
      for (var i = 0; i < queue.length; i++) {
        var item = queue[i];
        present[item.id] = true;
        var row = dom.findByDlId(list, item.id);
        if (row) {
          if (dom.isCollapsing(row)) dom.reveal(row);
          renderRow().updateCompactRow(row, item);
        } else {
          row = dom.buildCompactRow(item);
          list.insertBefore(row, item === demoted ? list.firstElementChild : null);
          added.push(row);
        }
        if (item === demoted) dom.markHeld(row);
      }
      orderRows(list);
      if (!opening) added.forEach(dom.reveal);
      dom.dropAbsent(list.querySelectorAll('.dl-compact'), present, data);
      if (opening) dom.reveal(queueContainer);
    } else if (queueContainer && !dom.isCollapsing(queueContainer) &&
        dom.dropAbsent(queueContainer.querySelectorAll('.dl-compact'), present, data)) {
      held = true;
    } else {
      closeSection(dom, queueContainer);
    }

    /* Empty state — #dl-empty-container is always in the DOM at a fixed
       position (above "Coming soon"); fill it when nothing is downloading,
       with the outage card if a service did not answer, clear it otherwise. */
    if (emptyContainer) {
      var nothingActive = !data.hero && queue.length === 0 && !held;
      var shown = emptyContainer.firstElementChild;
      if (nothingActive) {
        var wanted = dom.buildEmptyState(data.outage);
        if (shown && dom.q('h3', shown).textContent !== dom.q('h3', wanted).textContent) {
          clear(emptyContainer);
          emptyContainer.appendChild(wanted);
        } else if (!shown) {
          emptyContainer.appendChild(wanted);
          dom.reveal(emptyContainer);
        }
        if (dom.isCollapsing(emptyContainer)) dom.reveal(emptyContainer);
      } else {
        closeSection(dom, emptyContainer);
      }
    }

    /* Upcoming */
    renderRecent().updateUpcomingList(root, data);

    /* Recent */
    renderRecent().updateRecent(recentContainer, data.recent);

    if (header) updateSubtitle(dom, header, subtitleFor(dom, data));
  }

  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-v="ep-toggle"]');
    if (!toggle) return;
    var isOpen = toggle.classList.toggle('open');
    toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    var list = toggle.nextElementSibling;
    if (!list || list.getAttribute('data-v') !== 'ep-list') return;
    var dom = buildDom();
    if (isOpen) {
      list.hidden = false;
      dom.reveal(list);
    } else {
      dom.collapse(list, function () { list.hidden = true; });
    }
  });

  /* Kick off the polling loop. */
  MM.downloads.poll.start(update);
}());
