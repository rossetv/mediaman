/* Watch page: the row of what the person asked to download and has not watched,
 * under Continue watching: what is there, then what is on its way. It loads
 * after the page, follows what is watched, asked for or arrives while the page
 * is open, and is not there when it has nothing to show. What stands in
 * Continue watching is not repeated in it.
 * Needs watch/core.js, watch/address.js, watch/keep.js, watch/marks.js, watch/search.js, watch/grid.js, watch/expand.js and watch/browse.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var toArray = core.toArray;

  var ASKED_URL = '/api/watch/asked';
  var ID = 'asked';
  var TITLE = 'You asked for these';
  var LINE = 'and haven’t watched them yet';
  var ROW_MAX = 20; // the rest are in the grid
  var NEW_DAYS = 14;
  var DAY_MS = 86400000;
  var POLL_MS = 60000; // while something downloads
  var AGAIN_MS = 5000; // at least, from one asking to the next
  var SWEEP_MS = 1400; // past the arrival's animations
  var SETTLE_MS = 900; // a card just marked shows its mark before it goes
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

  var anchor = core.byId('watch-asked');

  var section = null;
  var known = []; // what the server last said
  var items = []; // as they are shown
  var timer = null; // of the next look at what is on its way
  var due = null; // the timer of an asking that something called for
  var dueOwn = false; // what called for it is the person's own doing
  var again = null; // the same, come due while one was on its way: { own }
  var askedAt = 0;
  var asking = null;

  /* ── What a card says ── */

  function midnight(date) { return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime(); }

  /* Days since *item* came, by the person's own calendar; null when long ago, unknown or begun since. */
  function daysHere(item) {
    var at = new Date(item.arrived_at || NaN);
    if (item.state !== 'ready' || item.started || isNaN(at.getTime())) return null;
    var days = Math.max(0, Math.round((midnight(new Date(Date.now())) - midnight(at)) / DAY_MS));
    return days < NEW_DAYS ? days : null;
  }

  /* { day, month, year } of "2027-03-14", or null. */
  function dateOf(item) {
    var found = /^(\d{4})-(\d{2})-(\d{2})$/.exec(item.release_date || '');
    var month = found && MONTHS[Number(found[2]) - 1];
    return month ? { day: Number(found[3]), month: month, year: Number(found[1]) } : null;
  }

  function comingText(item) {
    var date = dateOf(item);
    if (!date) return 'Not out yet';
    if (date.year === new Date(Date.now()).getFullYear()) return 'Coming ' + date.day + ' ' + date.month;
    return 'Coming ' + date.day + ' ' + date.month.slice(0, 3) + ' ' + date.year;
  }

  /* [what it says, its icon or null for a dot] */
  function says(item) {
    var days = daysHere(item);
    if (days !== null) return ['Arrived ' + (days === 0 ? 'today' : days === 1 ? 'yesterday' : days + ' days ago'), null];
    if (item.state === 'ready') return [item.started ? 'Started' : 'Ready to watch', 'i-play'];
    if (item.state === 'arriving') return ['Almost ready', 'i-clock-regular'];
    if (item.state === 'downloading') return ['Downloading', 'i-arrow-down'];
    if (item.state === 'queued') return ['Looking for it', 'i-magnifying-glass'];
    return [comingText(item), 'i-clock-regular'];
  }

  function stateLine(item) {
    var said = says(item);
    var line = el('span', 'asked-state');
    if (said[1]) line.appendChild(icon(said[1]));
    else line.appendChild(el('span', 'asked-state__dot'));
    line.appendChild(el('span', 'asked-state__text', said[0]));
    return line;
  }

  /* The date a title comes out, as a leaf of a calendar; the line under it says it. */
  function leaf(date) {
    var node = el('span', 'asked-leaf');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(el('span', 'asked-leaf__month', date.month.slice(0, 3)));
    node.appendChild(el('span', 'asked-leaf__day', String(date.day)));
    return node;
  }

  function card(item) {
    var node = watch.browse.card(item);
    if (!node) return null;
    var poster = node.querySelector('.watch-card__poster');
    var sub = node.querySelector('.watch-card__sub');
    if (item.subtitle) core.caption(sub, item.subtitle);
    var days = daysHere(item);
    node.classList.add('watch-card--asked');
    node.setAttribute('data-asked', item.key);
    node.setAttribute('data-asked-state', days === null ? item.state : 'new');
    var date = item.state === 'coming' ? dateOf(item) : null;
    if (date) poster.appendChild(leaf(date));
    if (days !== null && item.leaving_days === null) {
      var mark = el('span', 'watch-badge asked-new', 'New');
      mark.setAttribute('aria-hidden', 'true');
      poster.appendChild(mark);
    }
    node.querySelector('.watch-card__link').appendChild(stateLine(item));
    return node;
  }

  /* ── The row ── */

  function scroller() { return section.querySelector('.watch-row__scroll'); }

  function cardsShown() {
    return toArray(scroller().children).filter(function (n) {
      return n.hasAttribute('data-asked') && !n.classList.contains('is-kept');
    });
  }

  function startRow() {
    if (watch.rows) watch.rows.init(section.querySelector('.watch-row__viewport'));
    watch.expand.row(section);
    watch.address.putRowsBack(section);
  }

  function count() {
    section.setAttribute('data-row-total', String(items.length));
    section.querySelector('.watch-row__viewport').toggleAttribute('data-row-more', items.length > ROW_MAX);
  }

  function makeSection() {
    section = el('section', 'watch-row asked-row');
    section.setAttribute('data-row-id', ID);
    section.appendChild(watch.expand.head(ID, TITLE));
    section.appendChild(el('p', 'asked-line', LINE));
    var viewport = el('div', 'watch-row__viewport');
    viewport.setAttribute('data-row-kind', ID);
    var strip = el('div', 'watch-row__scroll');
    strip.setAttribute('tabindex', '0');
    strip.setAttribute('role', 'region');
    strip.setAttribute('aria-label', TITLE + ' ' + LINE);
    viewport.appendChild(strip);
    section.appendChild(viewport);
    var said = el('p', 'sr-only');
    said.setAttribute('role', 'status');
    section.appendChild(said);
    items.slice(0, ROW_MAX).forEach(function (item) {
      var node = card(item);
      if (node) strip.appendChild(node);
    });
    count();
  }

  function skeleton() {
    var next = anchor.nextElementSibling;
    return next && next.classList.contains('asked-skel') ? next : null;
  }

  /* Put the row on the page in place of its skeleton, or let it grow in. */
  function show() {
    var skel = skeleton();
    if (skel) core.swap(skel, section);
    else anchor.after(section);
    startRow();
    if (!skel) watch.keep.growIn(section, false, function () { watch.expand.row(section); });
  }

  function noRow() {
    var skel = skeleton();
    if (skel) watch.keep.foldAway(skel);
  }

  /* ── A newer answer ── */

  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  function byKey(list) {
    var found = {};
    list.forEach(function (item) { found[item.key] = item; });
    return found;
  }

  /* Bring the row to *fresh*: what is over settles out of it, what is new or
     has moved on takes its place. */
  function update(fresh) {
    var before = byKey(items);
    items = fresh;
    var wanted = fresh.slice(0, ROW_MAX);
    var here = {};
    var over = [];
    var play = core.flip(cardsShown());
    cardsShown().forEach(function (node) {
      var key = node.getAttribute('data-asked');
      if (wanted.some(function (item) { return item.key === key; })) here[key] = node;
      else over.push(node);
    });
    var arrived = [];
    var order = wanted.map(function (item) {
      var node = here[item.key];
      if (node && same(before[item.key], item)) return node;
      var made = card(item);
      if (!made) return null;
      if (node) {
        var focused = node.contains(document.activeElement);
        node.replaceWith(made);
        if (focused) made.querySelector('.watch-card__link').focus({ preventScroll: true });
      } else {
        scroller().appendChild(made);
        core.fadeIn(made);
      }
      if (item.state === 'ready' && before[item.key] && before[item.key].state !== 'ready') arrived.push([made, item]);
      return made;
    }).filter(Boolean);
    // Not before the new ones stand: the last card of a row takes the row with it.
    over.forEach(drop);
    var now = cardsShown();
    if (order.some(function (node, i) { return now[i] !== node; })) {
      var focus = document.activeElement;
      var left = scroller().scrollLeft;
      order.forEach(function (node) { scroller().appendChild(node); });
      // What comes first is seen by whoever stands at the row's start.
      scroller().scrollTo({ left: left, behavior: 'instant' });
      // Moving a node takes the focus off what is in it.
      if (focus && document.activeElement !== focus && section.contains(focus)) focus.focus({ preventScroll: true });
      play();
    }
    arrived.forEach(function (a) { celebrate(a[0]); });
    if (arrived.length) {
      section.querySelector('[role="status"]').textContent =
        arrived.map(function (a) { return a[1].title; }).join(', ') + (arrived.length === 1 ? ' is' : ' are') + ' ready to watch.';
    }
    count();
    watch.expand.row(section);
  }

  /* Light goes once over a poster whose title has come. */
  function celebrate(node) {
    if (core.reduceMotion()) return;
    var sweep = el('span', 'asked-sweep');
    sweep.setAttribute('aria-hidden', 'true');
    node.classList.add('asked-arriving');
    node.querySelector('.watch-card__poster').appendChild(sweep);
    setTimeout(function () { // the grid's copies and a moved card do not play it again
      sweep.remove();
      node.classList.remove('asked-arriving');
    }, SWEEP_MS);
  }

  function drop(node) {
    var focused = node.contains(document.activeElement);
    var left = watch.keep.fromRow(node, section, node.getClientRects().length > 0, '.watch-card__link');
    if (focused && left.focus) core.focusSoftly(left.focus);
  }

  function take(data) {
    var fresh = (data.items || []).filter(function (item) { return item && item.key; });
    if (data.complete === false) {
      // Nothing is known of what is on its way: it stays as it was.
      var here = byKey(fresh);
      fresh = fresh.concat(known.filter(function (item) { return item.state !== 'ready' && !here[item.key]; }));
    }
    known = fresh;
    put(known.filter(notContinued));
  }

  function notContinued(item) { return !watch.marks.continues(item.held_ref); }

  function put(fresh) {
    if (section && section.isConnected && fresh.length) {
      update(fresh);
    } else if (fresh.length) {
      items = fresh;
      makeSection();
      show();
    } else if (section && section.isConnected) {
      items = [];
      cardsShown().forEach(drop);
      section = null;
    } else {
      noRow();
    }
  }

  function later() {
    clearTimeout(timer);
    var moving = items.some(function (item) { return item.state === 'downloading' || item.state === 'arriving'; });
    if (moving) timer = setTimeout(function () { if (document.hidden) later(); else refresh(); }, POLL_MS);
  }

  function settled() {
    asking = null;
    later();
    var next = again;
    again = null;
    if (next) soon(0, next.own);
  }

  function ask() {
    if (asking) return asking;
    askedAt = Date.now();
    asking = window.MM.api.get(ASKED_URL).then(function (data) {
      // Not over a row on show an answer from before the person's own mark or undo, which asks anew.
      if (!section || !(dueOwn || (again && again.own))) take(data);
    }).then(settled, function (err) {
      if (!section) noRow();
      settled();
      throw err;
    });
    return asking;
  }

  function refresh() { ask().catch(function () { /* it stays as it is until something asks again */ }); }

  /* Ask again *ms* from now, or once the last asking is long enough ago, which the person's
     *own* mark does not wait for. One on its way may have been answered before what called
     for this. */
  function soon(ms, own) {
    if (dueOwn && !own) return;
    clearTimeout(due);
    dueOwn = !!own;
    due = setTimeout(function () {
      dueOwn = false;
      if (asking) again = { own: own || !!(again && again.own) };
      else refresh();
    }, own ? ms : Math.max(ms, askedAt + AGAIN_MS - Date.now(), 0));
  }

  /* ── Its grid ── */

  function gridOf() {
    function page() { return { cards: items.map(card).filter(Boolean), next: null }; }
    var empty = { title: 'Nothing to show here now', text: 'What you ask for shows up here until you start it.', link: 'Back to all rows', href: '/watch' };
    if (section) return Object.assign(page(), { total: items.length, empty: empty });
    return { cards: [], next: 'first', empty: empty, fetchPage: function () { return ask().then(page); } };
  }

  if (!anchor || !watch.expand) return;
  watch.expand.source(ID, gridOf);

  document.addEventListener('mm:watch-marked', function (e) {
    var shown = items.some(function (item) { return item.held_ref === e.detail.ref; });
    soon(shown ? SETTLE_MS : 0, true);
  });
  document.addEventListener('mm:watch-continue', function () {
    var left = items.filter(notContinued);
    if (left.length < items.length) put(left);
    soon(SETTLE_MS); // what left it may have been watched, so the server is asked first
  });
  document.addEventListener('mm:title-changed', function () { soon(0); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) soon(0); });
  window.addEventListener('pageshow', function (e) { if (e.persisted) soon(0); });

  refresh();
})();
