/**
 * settings/rows.js — list rows and inline panels that come and go in place.
 *
 * Responsibilities:
 *   - Fade a new row in (`enter`)
 *   - Collapse a removed row out, then move focus that was on it to the
 *     same control on the next row, the previous row, or a fallback (`leave`)
 *   - Fold an inline form or panel (`.setg-fold`) open and shut (`open`,
 *     `close`, `isOpen`)
 *
 * Cross-module dependencies:
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.rows.enter(row)
 *   MM.settings.rows.leave(row, { focusSelector, fallback, done })
 *   MM.settings.rows.open(panel)
 *   MM.settings.rows.close(panel, done)
 *   MM.settings.rows.isOpen(panel)
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

  // A panel folds through its `is-shut` class and is hidden once shut. Each
  // open or close supersedes the one before, so a panel reopened mid-close
  // stays open and the earlier close never finishes.
  function isOpen(panel) {
    return !panel.hidden && !panel.classList.contains('is-shut');
  }

  function settle(panel, then) {
    var token = panel._foldToken = (panel._foldToken || 0) + 1;
    var settled = false;
    function onEnd(e) { if (e.target === panel && e.propertyName === 'grid-template-rows') finish(); }
    function finish() {
      if (settled) return;
      settled = true;
      panel.removeEventListener('transitionend', onEnd);
      if (panel._foldToken !== token) return;
      panel.classList.remove('is-moving');
      then();
    }
    if (reducedMotion()) { finish(); return; }
    panel.addEventListener('transitionend', onEnd);
    setTimeout(finish, 400);
  }

  function open(panel) {
    if (panel.hidden) {
      panel.classList.add('is-shut');
      panel.hidden = false;
      void panel.offsetHeight;
    }
    panel.classList.add('is-moving');
    panel.classList.remove('is-shut');
    settle(panel, function () {});
  }

  function close(panel, done) {
    done = done || function () {};
    if (panel.hidden) { done(); return; }
    panel.classList.add('is-moving', 'is-shut');
    settle(panel, function () {
      panel.hidden = true;
      panel.classList.remove('is-shut');
      done();
    });
  }

  MM.settings.rows = { enter: enter, leave: leave, open: open, close: close, isOpen: isOpen };
})();
