/**
 * settings/sections.js — the section list and the section on show.
 *
 * The pane's address says what shows: `settings` is the list, `settings/<section>` one section,
 * and `settings/integrations/<service>` the Integrations section with that card open and focused.
 * On a wide screen the list stays beside the section, and the list alone shows Overview.
 *
 * Cross-module dependencies:
 *   MM.you (the pane: go, shown, and the mm:you-shown event, whose view is the path)
 *
 * Exposes:
 *   MM.settings.sections.init()
 *   MM.settings.sections.show(path)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var WIDE = '(min-width: 1000px)';
  var root = null;
  var path = 'settings';

  function shown() { return !MM.you || MM.you.shown('settings'); }
  function wide() { return !!(window.matchMedia && window.matchMedia(WIDE).matches); }
  function still() { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }

  function partsOf(to) {
    var parts = String(to || '').split('/');
    return parts[0] === 'settings' ? parts : null;
  }

  function addressed() {
    var at = /[?&]you=([^&#]*)/.exec(window.location.search || '');
    return at ? decodeURIComponent(at[1]) : '';
  }

  function openCard(service) {
    var card = document.getElementById('intg-' + service);
    var fold = card && card.querySelector('.intg-fold');
    if (!fold) return;
    if (card.classList.contains('is-collapsed')) fold.click();
    card.scrollIntoView({ block: 'start', behavior: still() ? 'auto' : 'smooth' });
    fold.focus({ preventScroll: true });
  }

  function show(to) {
    var parts = partsOf(to);
    if (!root || !parts) return;
    var sec = parts[1] && root.querySelector('.stx-sec[data-sec="' + parts[1] + '"]') ? parts[1] : null;
    var was = path;
    path = sec ? parts.slice(0, 3).join('/') : 'settings';
    var two = wide();
    var on = sec || (two ? 'overview' : null);
    root.querySelectorAll('.stx-sec').forEach(function (el) {
      var mine = el.getAttribute('data-sec') === on;
      if (mine && two && !el.classList.contains('is-on')) {
        el.classList.remove('is-arriving');
        void el.offsetWidth;
        el.classList.add('is-arriving');
      }
      el.classList.toggle('is-on', mine);
    });
    root.querySelectorAll('.stx-link').forEach(function (a) {
      if (a.getAttribute('data-sec') === on) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
    root.classList.toggle('is-in', !!sec && !two);
    var nav = root.querySelector('.stx__nav');
    var body = root.querySelector('.stx__body');
    if (nav) nav.inert = !!sec && !two;
    if (body) {
      body.inert = !sec && !two;
      if (sec && was.split('/')[1] !== sec) body.scrollTop = 0;
    }
    if (sec === 'integrations' && parts[2] && path !== was) openCard(parts[2]);
    // Back on the list, the row that led to the section takes the focus.
    var back = !sec && !two && was !== 'settings' && root.querySelector('.stx-link[data-sec="' + was.split('/')[1] + '"]');
    if (back && !(nav && nav.contains(document.activeElement))) back.focus({ preventScroll: true });
  }

  function go(to) {
    if (MM.you && MM.you.go) MM.you.go(to);
    show(to);
  }

  // The list's rows, and the old in-page links to a section or a card, step within the pane.
  function onClick(e) {
    if (e.defaultPrevented || e.button) return;
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || !root.contains(a)) return;
    var href = a.getAttribute('href');
    var to = a.classList.contains('stx-link') ? 'settings/' + a.getAttribute('data-sec')
      : /^#setg-[\w-]+$/.test(href) ? 'settings/' + href.slice(6)
      : /^#intg-[\w-]+$/.test(href) ? 'settings/integrations/' + href.slice(6)
      : null;
    if (!to || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go(to);
  }

  function init() {
    root = document.querySelector('.setg-pg[data-stx]');
    if (!root) return;
    root.addEventListener('click', onClick);
    // The pane tells every step, Back and Forward too, with the path it shows.
    document.addEventListener('mm:you-shown', function (e) {
      var d = e.detail || {};
      if (d.group === 'settings') show(d.view);
    });
    var mq = window.matchMedia && window.matchMedia(WIDE);
    if (mq && mq.addEventListener) mq.addEventListener('change', function () { if (shown()) show(path); });
    show(partsOf(addressed()) ? addressed() : 'settings');
  }

  MM.settings.sections = {
    init: init,
    show: show,
  };
})();
