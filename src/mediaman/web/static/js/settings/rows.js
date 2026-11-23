/**
 * settings/rows.js — list rows that come and go in place.
 *
 * Responsibilities:
 *   - Fade a new row in (`enter`)
 *   - Collapse a removed row out, then move focus that was on it to the
 *     same control on the next row, the previous row, or a fallback (`leave`)
 *
 * Cross-module dependencies:
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.rows.enter(row)
 *   MM.settings.rows.leave(row, { focusSelector, fallback, done })
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  function reducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function enter(row) {
    if (reducedMotion()) return;
    row.classList.add('is-entering');
    row.addEventListener('animationend', function () { row.classList.remove('is-entering'); }, { once: true });
  }

  function leave(row, opts) {
    opts = opts || {};
    var hadFocus = MM.dom.focusStillIn(row);
    var sel = opts.focusSelector;
    var next = row.nextElementSibling, prev = row.previousElementSibling;
    var target = (sel && next && next.querySelector(sel)) ||
                 (sel && prev && prev.querySelector(sel)) ||
                 opts.fallback || null;

    var finished = false;
    function finish() {
      if (finished) return;
      finished = true;
      row.remove();
      if (opts.done) opts.done();
      if (hadFocus && target && document.body.contains(target)) target.focus();
    }

    if (reducedMotion()) { finish(); return; }
    // A fixed height first, so the collapse to zero can transition.
    row.style.height = row.offsetHeight + 'px';
    void row.offsetHeight;
    row.classList.add('is-leaving');
    row.style.height = '0px';
    // The opacity fade ends first; the row is gone only when the height has closed.
    row.addEventListener('transitionend', function (e) {
      if (e.target === row && e.propertyName === 'height') finish();
    });
    // transitionend never fires when the row is hidden or the transition is cut short.
    setTimeout(finish, 400);
  }

  MM.settings.rows = { enter: enter, leave: leave };
})();
