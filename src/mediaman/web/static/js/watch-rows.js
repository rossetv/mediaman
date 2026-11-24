/* Watch poster rows: arrow buttons and keys that page by whole cards, edge
 * fades while there is more to see, and posters that fade in as they load. A
 * row of cards is one Tab stop.
 *
 * Without this script the rows still scroll (touch, trackpad, keyboard) and
 * the posters simply appear. MMWatch.rows.init(viewport) starts a row added
 * after the page loaded.
 */
(function () {
  'use strict';

  var EDGE_SLACK = 2; // px; sub-pixel scroll positions never quite reach the end

  var page = document.getElementById('watch-page');
  if (!page) return;

  /* ── Posters fade in ── */

  var POSTER_SELECTOR = '.watch-card__poster > img, .watch-ep__thumb > img, .watch-banner__bg';

  function markLoaded(event) {
    var img = event.target;
    if (img && img.tagName === 'IMG' && img.matches(POSTER_SELECTOR)) img.classList.add('is-loaded');
  }

  // Images that loaded (or failed) before this deferred script ran fire no
  // further events, so they are marked first and never hidden.
  Array.prototype.forEach.call(page.querySelectorAll(POSTER_SELECTOR), function (img) {
    if (img.complete) img.classList.add('is-loaded');
  });
  document.addEventListener('load', markLoaded, true);
  document.addEventListener('error', markLoaded, true);
  page.classList.add('has-poster-fade');

  /* ── Rows ── */

  function smooth() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  }

  /* The first card of a poster row, or pill of a season line. */
  function firstCard(scroller) {
    return scroller.querySelector('.watch-card, .watch-pill');
  }

  /* Distance from one card's start to the next. */
  function cardStep(scroller) {
    var first = firstCard(scroller);
    if (!first) return 0;
    var second = first.nextElementSibling;
    return second ? second.offsetLeft - first.offsetLeft : first.offsetWidth;
  }

  /* How many whole cards fit side by side. */
  function cardsInView(scroller, step) {
    var style = getComputedStyle(scroller);
    var inner = scroller.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    var gap = step - firstCard(scroller).offsetWidth;
    return Math.max(1, Math.floor((inner + gap) / step));
  }

  /* Where a running smooth scroll is headed, so presses made meanwhile add to
   * it instead of starting from the row's mid-way position. */
  var heading = new WeakMap();
  var settleTimers = new WeakMap();
  var HAS_SCROLLEND = 'onscrollend' in window;
  var SETTLE_MS = 300; // without scrollend: quiet this long means the scroll ended
  var CHECK_MS = 700; // past a smooth scroll of JUMP_PX
  var CLEAR = 8; // px between what has the focus and the bars over the page
  var JUMP_PX = 500; // Chrome's smooth scroll takes about half a second at this distance

  function settle(scroller) {
    heading.delete(scroller);
  }

  function armSettle(scroller) {
    if (HAS_SCROLLEND) return;
    clearTimeout(settleTimers.get(scroller));
    settleTimers.set(scroller, setTimeout(function () { settle(scroller); }, SETTLE_MS));
  }

  function goTo(scroller, left, instant) {
    var behavior = instant ? 'auto' : smooth();
    if (behavior === 'auto') settle(scroller);
    else {
      heading.set(scroller, left);
      armSettle(scroller);
    }
    scroller.scrollTo({ left: left, behavior: behavior });
  }

  /* Scroll so the card *by* places from the current first card leads the row (on a line of pills, the next one that way). */
  function moveBy(scroller, by) {
    if (isLine(scroller)) { moveLine(scroller, by, false); return; }
    var step = cardStep(scroller);
    if (!step) return;
    var from = heading.has(scroller) ? heading.get(scroller) : scroller.scrollLeft;
    var index = Math.round(from / step) + by;
    var max = scroller.scrollWidth - scroller.clientWidth;
    if (by > 0 && scroller.scrollLeft >= max - EDGE_SLACK) askForMore(scroller);
    goTo(scroller, Math.max(0, Math.min(max, index * step)));
  }

  /* Season pills are not all one width, so their line goes by where each one
     stands: *whole* a lineful, to the first not wholly in view, else one pill. */
  function moveLine(scroller, direction, whole) {
    var style = getComputedStyle(scroller);
    var padLeft = parseFloat(style.paddingLeft);
    var padRight = parseFloat(style.paddingRight);
    var from = heading.has(scroller) ? heading.get(scroller) : scroller.scrollLeft;
    var pills = Array.prototype.slice.call(scroller.querySelectorAll('.watch-pill'));
    function start(p) { return p.offsetLeft - padLeft; }
    function end(p) { return p.offsetLeft + p.offsetWidth + padRight - scroller.clientWidth; }
    var ahead = pills.filter(function (p) { return direction > 0 ? start(p) > from + EDGE_SLACK : start(p) < from - EDGE_SLACK; });
    if (direction < 0) ahead.reverse();
    if (whole && direction > 0) ahead = ahead.filter(function (p) { return end(p) > from + EDGE_SLACK; });
    var to = ahead[0];
    if (!to) return;
    // Back by a lineful: to the first pill beside which the one cut off at the start still fits whole. Always
    // to a pill's start: a line that snaps to one would go on to the next and leave a pill out.
    if (whole && direction < 0) to = ahead.filter(function (p) { return start(p) >= end(to) - EDGE_SLACK; }).pop() || to;
    goTo(scroller, Math.max(0, Math.min(scroller.scrollWidth - scroller.clientWidth, start(to))));
  }

  function isLine(scroller) {
    var first = firstCard(scroller);
    return !!first && first.classList.contains('watch-pill');
  }

  /* Forward from the very end: the row's loader (watch/rows-load.js) may have more. */
  function askForMore(scroller) {
    var loader = window.MMWatch && window.MMWatch.rowsLoad;
    if (loader) loader.forward(scroller.parentNode);
  }

  function moveTo(scroller, left) {
    var max = scroller.scrollWidth - scroller.clientWidth;
    left = Math.max(0, Math.min(max, left));
    goTo(scroller, left, Math.abs(left - scroller.scrollLeft) > JUMP_PX);
  }

  function pageBy(scroller, direction) {
    if (isLine(scroller)) { moveLine(scroller, direction, true); return; }
    var step = cardStep(scroller);
    if (step) moveBy(scroller, direction * cardsInView(scroller, step));
  }

  function refresh(viewport, scroller) {
    var max = scroller.scrollWidth - scroller.clientWidth;
    viewport.classList.toggle('has-prev', scroller.scrollLeft > EDGE_SLACK);
    // data-row-more: more cards will load past the last one.
    viewport.classList.toggle('has-next', scroller.scrollLeft < max - EDGE_SLACK || viewport.hasAttribute('data-row-more'));
  }

  function arrow(direction, label) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'watch-row__arrow watch-row__arrow--' + direction;
    // A pointer-only shortcut: keyboard users page with the arrow keys on
    // the row itself, so the buttons stay out of the tab order.
    btn.tabIndex = -1;
    btn.setAttribute('aria-hidden', 'true');
    btn.title = label;
    btn.appendChild(MM.dom.icon(direction === 'prev' ? 'i-chevron-left' : 'i-chevron-right'));
    // Keep focus where it was: a button that hides at the end must not take it.
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    return btn;
  }

  /* ── Keys ── */

  /* How far the row has to be scrolled for *card* to be whole, at a card's start, where it snaps. */
  function leftFor(scroller, card, from) {
    var style = getComputedStyle(scroller);
    var start = card.offsetLeft - parseFloat(style.paddingLeft);
    var end = card.offsetLeft + card.offsetWidth + parseFloat(style.paddingRight) - scroller.clientWidth;
    if (start < from - EDGE_SLACK) return start;
    if (end <= from + EDGE_SLACK) return from;
    var step = cardStep(scroller);
    return Math.min(start, step ? Math.ceil((end - EDGE_SLACK) / step) * step : end);
  }

  function inSight(scroller, card) {
    return leftFor(scroller, card, scroller.scrollLeft) === scroller.scrollLeft;
  }

  /* The Tab stop of a row of cards is the row, which hands the focus to a card: Tab goes by a
     link in Safari. What is in the cards is out of the Tab order and has the arrow keys; while
     it has the focus, so is the row, or Shift+Tab could not leave it. */
  function rove(scroller) {
    var last = null; // the card that had the focus
    var checking = null;

    function cards() {
      return Array.prototype.filter.call(scroller.children, function (node) {
        return node.matches('.watch-card, .watch-row__more') && !node.classList.contains('is-kept');
      });
    }

    function controls(root) { return Array.prototype.slice.call(root.querySelectorAll('a[href], button')); }

    function sync() {
      controls(scroller).forEach(function (node) { if (node.getAttribute('tabindex') !== '-1') node.tabIndex = -1; });
      var active = document.activeElement;
      scroller.tabIndex = active !== scroller && scroller.contains(active) ? -1 : 0;
    }

    /* Safari can drop a smooth scroll that others ran into: look again once they are over. */
    function show(card, again) {
      var from = heading.has(scroller) ? heading.get(scroller) : scroller.scrollLeft;
      var left = leftFor(scroller, card, from);
      clearTimeout(checking);
      if (left !== from) moveTo(scroller, left);
      if (again || !heading.has(scroller)) return;
      checking = setTimeout(function () {
        settle(scroller);
        if (card.contains(document.activeElement)) show(card, true);
      }, CHECK_MS);
    }

    function focusOn(card, control) {
      control.focus({ preventScroll: true });
      show(card);
      var area = window.MMWatch.core.clearArea();
      var box = control.getBoundingClientRect();
      var by = box.top < area.top ? box.top - area.top - CLEAR
        : box.bottom > area.bottom ? Math.min(box.bottom - area.bottom + CLEAR, box.top - area.top - CLEAR) : 0;
      if (by) window.scrollBy(0, by);
    }

    function enter() {
      var all = cards();
      var card = all.indexOf(last) >= 0 && inSight(scroller, last) ? last
        : all.filter(function (c) { return inSight(scroller, c); })[0] || all[0];
      var control = card && controls(card)[0];
      if (!control) return;
      // Said once, on the way in.
      control.setAttribute('aria-describedby', 'watch-row-keys');
      focusOn(card, control);
    }

    scroller.addEventListener('focusin', function (e) {
      if (e.target === scroller) {
        // Not when it was pressed: a card would take the focus, and scroll.
        if (scroller.matches(':focus-visible')) enter();
        return;
      }
      last = e.target.closest('.watch-card, .watch-row__more');
      scroller.tabIndex = -1;
    });
    scroller.addEventListener('focusout', function (e) {
      if (e.target !== scroller) e.target.removeAttribute('aria-describedby');
      if (!scroller.contains(e.relatedTarget)) scroller.tabIndex = 0;
    });

    scroller.addEventListener('keydown', function (e) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var all = cards();
      var i = all.findIndex(function (card) { return card.contains(e.target); });
      if (i < 0) return;
      var own = controls(all[i]);
      var at = own.indexOf(e.target);
      var to = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1
        : e.key === 'Home' ? 0 : e.key === 'End' ? all.length - 1 : null;
      if (to === null) {
        // With nothing further that way, the page scrolls.
        var next = e.key === 'ArrowDown' ? own[at + 1] : e.key === 'ArrowUp' ? own[at - 1] : null;
        if (!next) return;
        focusOn(all[i], next);
      } else if (all[to]) {
        var there = controls(all[to]);
        if (there.length) focusOn(all[to], there[Math.max(0, Math.min(at, there.length - 1))]);
      }
      e.preventDefault();
    });

    ['touchstart', 'wheel'].forEach(function (name) {
      scroller.addEventListener(name, function () { clearTimeout(checking); }, { passive: true });
    });
    new MutationObserver(sync).observe(scroller, { childList: true });
    sync();
  }

  var started = new WeakSet();

  /* Give one row its arrows, keys and fades; a row already given them is left alone. */
  function initRow(viewport) {
    var scroller = viewport.querySelector('.watch-row__scroll');
    if (!scroller || started.has(viewport)) return;
    started.add(viewport);

    var prev = arrow('prev', 'Previous');
    var next = arrow('next', 'Next');
    viewport.appendChild(prev);
    viewport.appendChild(next);
    prev.addEventListener('click', function () { pageBy(scroller, -1); });
    next.addEventListener('click', function () { pageBy(scroller, 1); });

    if (scroller.hasAttribute('tabindex')) rove(scroller);

    scroller.addEventListener('keydown', function (e) {
      if (e.target !== scroller || e.altKey || e.ctrlKey || e.metaKey) return;
      if (e.key === 'ArrowLeft') moveBy(scroller, -1);
      else if (e.key === 'ArrowRight') moveBy(scroller, 1);
      else if (e.key === 'Home') moveTo(scroller, 0);
      else if (e.key === 'End') moveTo(scroller, scroller.scrollWidth);
      else return;
      e.preventDefault();
    });

    var queued = false;
    function schedule() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        refresh(viewport, scroller);
      });
    }

    scroller.addEventListener('scroll', function () { armSettle(scroller); schedule(); }, { passive: true });
    scroller.addEventListener('scrollend', function () { settle(scroller); });
    // The user taking over the row ends the scroll the keys started.
    ['touchstart', 'wheel'].forEach(function (name) {
      scroller.addEventListener(name, function () { settle(scroller); }, { passive: true });
    });
    if ('ResizeObserver' in window) new ResizeObserver(schedule).observe(scroller);
    else window.addEventListener('resize', schedule);
    // Keep takes cards out of a row, the loader adds them.
    new MutationObserver(schedule).observe(scroller, { childList: true });
    new MutationObserver(schedule).observe(viewport, { attributes: true, attributeFilter: ['data-row-more'] });
    refresh(viewport, scroller);
  }

  // Rows put on the page later (the Discover rows) are started by whoever adds them.
  window.MMWatch = window.MMWatch || {};
  window.MMWatch.rows = { init: initRow };
  Array.prototype.forEach.call(page.querySelectorAll('.watch-row__viewport'), initRow);
})();
