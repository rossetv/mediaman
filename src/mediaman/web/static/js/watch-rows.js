/* Watch poster rows: arrow buttons and keys that page by whole cards, edge
 * fades while there is more to see, and posters that fade in as they load.
 *
 * Without this script the rows still scroll (touch, trackpad, keyboard) and
 * the posters simply appear.
 */
(function () {
  'use strict';

  var EDGE_SLACK = 2; // px; sub-pixel scroll positions never quite reach the end

  var page = document.getElementById('watch-page');
  if (!page) return;

  /* ── Posters fade in ── */

  var POSTER_SELECTOR = '.watch-card__poster > img, .watch-hero__poster > img, .watch-ep__thumb > img';

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

  function cards(scroller) {
    return scroller.querySelectorAll('.watch-card');
  }

  /* Distance from one card's start to the next. */
  function cardStep(scroller) {
    var list = cards(scroller);
    if (!list.length) return 0;
    if (list.length > 1) return list[1].offsetLeft - list[0].offsetLeft;
    return list[0].offsetWidth;
  }

  /* How many whole cards fit side by side. */
  function cardsInView(scroller, step) {
    var style = getComputedStyle(scroller);
    var inner = scroller.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    var gap = step - cards(scroller)[0].offsetWidth;
    return Math.max(1, Math.floor((inner + gap) / step));
  }

  /* Scroll so the card *by* places from the current first card leads the row. */
  function moveBy(scroller, by) {
    var step = cardStep(scroller);
    if (!step) return;
    var index = Math.round(scroller.scrollLeft / step) + by;
    var max = scroller.scrollWidth - scroller.clientWidth;
    scroller.scrollTo({ left: Math.max(0, Math.min(max, index * step)), behavior: smooth() });
  }

  function pageBy(scroller, direction) {
    var step = cardStep(scroller);
    if (step) moveBy(scroller, direction * cardsInView(scroller, step));
  }

  function refresh(viewport, scroller) {
    var max = scroller.scrollWidth - scroller.clientWidth;
    viewport.classList.toggle('has-prev', scroller.scrollLeft > EDGE_SLACK);
    viewport.classList.toggle('has-next', scroller.scrollLeft < max - EDGE_SLACK);
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
    var icon = document.createElement('i');
    icon.className = 'fa-solid fa-chevron-' + (direction === 'prev' ? 'left' : 'right');
    btn.appendChild(icon);
    // Keep focus where it was: a button that hides at the end must not take it.
    btn.addEventListener('mousedown', function (e) { e.preventDefault(); });
    return btn;
  }

  function initRow(viewport) {
    var scroller = viewport.querySelector('.watch-row__scroll');
    if (!scroller) return;

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
      else if (e.key === 'Home') scroller.scrollTo({ left: 0, behavior: smooth() });
      else if (e.key === 'End') scroller.scrollTo({ left: scroller.scrollWidth, behavior: smooth() });
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

    scroller.addEventListener('scroll', schedule, { passive: true });
    if ('ResizeObserver' in window) new ResizeObserver(schedule).observe(scroller);
    else window.addEventListener('resize', schedule);
    // Keep takes cards out of a row.
    new MutationObserver(schedule).observe(scroller, { childList: true });
    refresh(viewport, scroller);
  }

  Array.prototype.forEach.call(page.querySelectorAll('.watch-row__viewport'), initRow);
})();
