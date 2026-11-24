/* Watch poster rows: arrow buttons and keys that page by whole cards, edge
 * fades while there is more to see, and posters that fade in as they load.
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

  var POSTER_SELECTOR = '.watch-card__poster > img, .watch-hero__poster > img, .watch-ep__thumb > img, .watch-banner__bg';

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

  /* Scroll so the card *by* places from the current first card leads the row. */
  function moveBy(scroller, by) {
    var step = cardStep(scroller);
    if (!step) return;
    var from = heading.has(scroller) ? heading.get(scroller) : scroller.scrollLeft;
    var index = Math.round(from / step) + by;
    var max = scroller.scrollWidth - scroller.clientWidth;
    if (by > 0 && scroller.scrollLeft >= max - EDGE_SLACK) askForMore(scroller);
    goTo(scroller, Math.max(0, Math.min(max, index * step)));
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
