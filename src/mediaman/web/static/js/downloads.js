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

  var CUT_OFF = ' · can\'t reach mediaman';

  /* True of every row under it: those that do not download yet are what is up next. */
  function queueHeading(queue) {
    return queue.every(function (item) { return item.state === 'queued'; }) ? 'Up next' : 'Also in progress';
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
    if (!order.some(function (at, i) { return at !== i; })) return;
    var next = list.firstElementChild;
    var active = document.activeElement;
    /* A row put back in the list would play its entrance again, over the slide. */
    rows.forEach(function (row) { row.classList.remove('dl-card-enter'); });
    order.forEach(function (i) {
      if (rows[i] === next) next = next.nextElementSibling;
      else list.insertBefore(rows[i], next);
    });
    /* A row loses the focus in it when it moves. */
    if (active && document.activeElement !== active && list.contains(active)) active.focus({ preventScroll: true });
  }

  /* Where each row of a list is seen, read before a poll changes any, as long as motion is
     wanted. */
  function placesOf(list) {
    if (!list || buildDom().reducedMotion()) return [];
    var top = list.getBoundingClientRect().top;
    return Array.prototype.map.call(list.children, function (row) {
      return { row: row, at: row.getBoundingClientRect().top - top };
    });
  }

  /* Rows now elsewhere in their list than they were seen, whether they moved or one above
     them changed height, start from there and ease to their places. */
  function slideRows(list, places) {
    if (!places.length) return;
    var dom = buildDom();
    var top = list.getBoundingClientRect().top;
    places.forEach(function (place) {
      if (place.row.parentNode === list && !dom.isCollapsing(place.row)) slideFrom(place.row, place.at + top);
    });
  }

  function slideFrom(row, top) {
    var run = row._dlSlide = {};
    /* Its place, without what is left of a slide still running. */
    row.classList.remove('is-sliding');
    row.style.transform = '';
    var by = top - row.getBoundingClientRect().top;
    if (!(Math.abs(by) >= 1)) return;
    row.style.transform = 'translateY(' + by + 'px)';
    void row.offsetHeight;
    row.classList.add('is-sliding');
    row.style.transform = '';
    setTimeout(function () { if (row._dlSlide === run) row.classList.remove('is-sliding'); }, 400);
  }

  function shownIn(dom, box, selector) {
    if (!box || dom.isCollapsing(box)) return [];
    return Array.prototype.filter.call(box.querySelectorAll(selector), function (row) { return !dom.isCollapsing(row); });
  }

  /* The server's line counts what it lists. While a viewer is told only that downloads are
     not available, the rows beside it are those that may be missing; with none, the card
     says it, and the line is left to what is coming. */
  function subtitleFor(dom, data) {
    var viewer = !dom.canAbandonHere();
    if (!data.outage || !viewer) return data.subtitle;
    var active = shownIn(dom, document.getElementById('dl-hero-container'), '.dl-hero')
      .concat(shownIn(dom, document.getElementById('dl-queue-container'), '.dl-compact'));
    var coming = shownIn(dom, document.getElementById('dl-upcoming-container'), '.dl-upcoming-row');
    var soon = coming.length ? coming.length + ' coming soon' : '';
    if (!active.length) return soon;
    return active.length + ' item' + (active.length !== 1 ? 's' : '') + ' in progress' +
      (soon ? ' · ' + soon : '') + ' · some may be missing';
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
    var queuePlaces = placesOf(document.getElementById('dl-queue-list'));
    var upcomingPlaces = placesOf(document.getElementById('dl-upcoming-list'));

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
    } else {
      closeSection(dom, heroContainer);
    }

    /* Queue — the item that is now the hero is no row any more. */
    var present = {};
    if (data.hero) present[data.hero.id] = false;
    var queue = data.queue || [];
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
        var qHeader = document.createElement('h2');
        qHeader.className = 'dl-compact-header';
        qHeader.textContent = queueHeading(queue);
        queueContainer.appendChild(qHeader);
        list = document.createElement('div');
        list.className = 'dl-compact-list';
        list.id = 'dl-queue-list';
        queueContainer.appendChild(list);
      }
      dom.setText(queueContainer.querySelector('.dl-compact-header'), queueHeading(queue));
      var added = [];
      for (var i = 0; i < queue.length; i++) {
        var item = queue[i];
        present[item.id] = true;
        var row = dom.findByDlId(list, item.id);
        if (row) {
          if (dom.isCollapsing(row)) dom.reveal(row);
          renderRow().updateCompactRow(row, item);
        } else {
          added.push(dom.placeInOrder(list, dom.buildCompactRow(item), queue, i));
        }
      }
      orderRows(list);
      slideRows(list, queuePlaces);
      if (!opening) added.forEach(dom.reveal);
      dom.dropAbsent(list.querySelectorAll('.dl-compact'), present);
      if (opening) dom.reveal(queueContainer);
    } else {
      closeSection(dom, queueContainer);
    }

    /* Empty state — #dl-empty-container is always in the DOM at a fixed
       position (above "Coming soon"); fill it when nothing is downloading,
       with the outage card if a service did not answer, clear it otherwise. */
    if (emptyContainer) {
      var nothingActive = !data.hero && queue.length === 0;
      var shown = emptyContainer.firstElementChild;
      if (nothingActive) {
        var wanted = dom.buildEmptyState(data.outage, data.downloads_set_up, data.refused);
        if (shown && shown.textContent !== wanted.textContent) {
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
    var upcomingList = document.getElementById('dl-upcoming-list');
    if (upcomingList) slideRows(upcomingList, upcomingPlaces);

    /* Recent */
    if (data.recent) renderRecent().updateRecent(recentContainer, data.recent);

    if (header) updateSubtitle(dom, header, subtitleFor(dom, data));
  }

  /* The header's ring is told by the same answer. */
  function hear(data) {
    if (MM.downloading) MM.downloading.hear(data.strip);
    update(data);
  }

  /* mediaman itself does not answer: what is shown stays as it was, and the line says so. */
  function lost() {
    if (MM.downloading) MM.downloading.lose();
    var header = root.querySelector('.ph');
    if (!header) return;
    var shown = header.querySelector('p');
    var base = shown ? shown.textContent.replace(CUT_OFF, '') : '';
    updateSubtitle(buildDom(), header, base ? base + CUT_OFF : 'Can\'t reach mediaman');
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
  MM.downloads.poll.start(hear, lost);
}());
