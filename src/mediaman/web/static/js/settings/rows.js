/**
 * settings/rows.js — list rows and inline panels that come and go in place.
 *
 * Responsibilities:
 *   - Fade a new row in (`enter`)
 *   - Collapse a removed row out, then move focus that was on it to the
 *     same control on the next row, the previous row, or a fallback (`leave`)
 *   - Fold an inline form or panel (`.setg-fold`) open and shut (`open`,
 *     `close`, `isOpen`), or open and into view (`openInView`)
 *   - Bring a form's status into view (`reveal`), with the field at fault
 *     (`refuse`), and for a late answer only where
 *     the reader still is (`stillAt`)
 *
 * Cross-module dependencies:
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.rows.enter(row)
 *   MM.settings.rows.leave(row, { focusSelector, fallback, done })
 *   MM.settings.rows.open(panel, done)
 *   MM.settings.rows.openInView(panel)
 *   MM.settings.rows.close(panel, done)
 *   MM.settings.rows.isOpen(panel)
 *   MM.settings.rows.holdFocus(btn, within)
 *   MM.settings.rows.inView(el)
 *   MM.settings.rows.bringIntoView(el)
 *   MM.settings.rows.reveal(status)
 *   MM.settings.rows.refuse(field, status)
 *   MM.settings.rows.stillAt(status, scope)
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

  function open(panel, done) {
    if (panel.hidden) {
      panel.classList.add('is-shut');
      panel.hidden = false;
      void panel.offsetHeight;
    }
    panel.classList.add('is-moving');
    panel.classList.remove('is-shut');
    settle(panel, done || function () {});
  }

  function bringIntoView(el) {
    el.scrollIntoView({ block: 'nearest', behavior: reducedMotion() ? 'auto' : 'smooth' });
  }

  // A panel has its height only once it has unfolded.
  function openInView(panel) {
    open(panel, function () { bringIntoView(panel); });
  }

  // On a phone a status sits under its buttons, where the bottom bar may cover it.
  function reveal(status) { status.scrollIntoView({ block: 'nearest' }); }

  // The cursor goes to the field to fix, and its reason with it. Safari scrolls to a focused
  // field only later, which would take the reason out of view again.
  function refuse(field, status) {
    field.focus({ preventScroll: true });
    field.scrollIntoView({ block: 'nearest' });
    reveal(status);
  }

  // Whether some of it shows between the bars at the top and bottom.
  function inView(el) {
    var root = document.documentElement;
    var bars = getComputedStyle(root);
    var box = el.getBoundingClientRect();
    return box.bottom > (parseFloat(bars.scrollPaddingTop) || 0) &&
      box.top < root.clientHeight - (parseFloat(bars.scrollPaddingBottom) || 0);
  }

  // Whether the reader is still where they pressed, so a late answer does not pull the page back.
  // A button clicked in Safari takes no focus, what is around it does: that is not somewhere else.
  function stillAt(status, scope) {
    var at = document.activeElement || document.body;
    return (scope.contains(at) || at.contains(scope)) && inView(status);
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

  // Safari focuses no button that is clicked, and a dialog returns the focus to what had it.
  function holdFocus(btn, within) {
    if (!(within || btn).contains(document.activeElement)) btn.focus({ preventScroll: true });
  }

  MM.settings.rows = {
    enter: enter, leave: leave, open: open, openInView: openInView, close: close, isOpen: isOpen,
    holdFocus: holdFocus, inView: inView, bringIntoView: bringIntoView, reveal: reveal, refuse: refuse, stillAt: stillAt,
  };
})();
