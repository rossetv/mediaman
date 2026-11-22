/* Snooze-menu: dropdown used on Library (whole pill) and Dashboard (caret).
 *
 * Markup shape expected:
 *   <div class="keep-wrapper">
 *     <button class="keep-trigger" aria-haspopup="menu" aria-expanded="false">...</button>
 *     <div class="snooze-dropdown" role="menu"> ... </div>
 *   </div>
 *
 * Any element matching `.keep-trigger` or `.btn-sm-caret` toggles the dropdown
 * on its parent `.keep-wrapper`. Escape or outside-click closes. The menu opens
 * downward or upward, whichever has the room between the fixed bars.
 */
(function () {
  'use strict';

  var TRIGGER_SELECTOR = '.keep-trigger, .btn-sm-caret';

  function closeAll(except) {
    var wrappers = document.querySelectorAll('.keep-wrapper.is-open');
    for (var i = 0; i < wrappers.length; i++) {
      if (wrappers[i] === except) continue;
      wrappers[i].classList.remove('is-open');
      var trigger = wrappers[i].querySelector(TRIGGER_SELECTOR);
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
  }

  /* The room the menu may use: from under the sticky top bar to above the
     fixed bottom tab bar, whichever of them are showing. */
  function visibleBounds() {
    var top = 0;
    var bottom = window.innerHeight;
    var bars = document.querySelectorAll('.nav-glass, .nav-topbar');
    for (var i = 0; i < bars.length; i++) {
      if (getComputedStyle(bars[i]).display !== 'none') {
        top = Math.max(top, bars[i].getBoundingClientRect().bottom);
      }
    }
    var tabs = document.querySelector('.nav-tabs');
    if (tabs && getComputedStyle(tabs).display !== 'none') {
      bottom = Math.min(bottom, tabs.getBoundingClientRect().top);
    }
    return { top: top, bottom: bottom };
  }

  /* Open downward unless the menu does not fit below and there is more room
     above; then cap its height to the room on the chosen side. */
  function place(wrapper) {
    var menu = wrapper.querySelector('.snooze-dropdown');
    if (!menu) return;
    var gap = 12;
    menu.style.maxHeight = '';
    wrapper.classList.remove('opens-up');
    var bounds = visibleBounds();
    var rect = wrapper.getBoundingClientRect();
    var below = bounds.bottom - rect.bottom - gap;
    var above = rect.top - bounds.top - gap;
    var need = menu.offsetHeight;
    var up = need > below && above > below;
    wrapper.classList.toggle('opens-up', up);
    var room = Math.max(up ? above : below, 80);
    if (need > room) menu.style.maxHeight = room + 'px';
  }

  function placeOpen() {
    var open = document.querySelector('.keep-wrapper.is-open');
    if (open) place(open);
  }

  function toggle(wrapper) {
    var open = !wrapper.classList.contains('is-open');
    closeAll(wrapper);
    wrapper.classList.toggle('is-open', open);
    var trigger = wrapper.querySelector(TRIGGER_SELECTOR);
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      place(wrapper);
      var first = wrapper.querySelector('.snooze-dropdown .snooze-option');
      if (first) first.focus();
    }
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var trigger = e.target.closest(TRIGGER_SELECTOR);
    if (trigger) {
      e.preventDefault();
      e.stopPropagation();
      var wrapper = trigger.closest('.keep-wrapper');
      if (wrapper) toggle(wrapper);
      return;
    }
    // Outside click closes everything
    if (!e.target.closest('.keep-wrapper')) {
      closeAll(null);
    }
  });

  window.addEventListener('resize', placeOpen);
  window.addEventListener('scroll', placeOpen, { passive: true });

  window.SnoozeMenu = { closeAll: closeAll };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var openWrapper = document.querySelector('.keep-wrapper.is-open');
      var openTrigger = openWrapper && openWrapper.querySelector(TRIGGER_SELECTOR);
      closeAll(null);
      if (openTrigger && openWrapper.contains(e.target)) openTrigger.focus();
      return;
    }
    if (e.key === 'Tab' && e.target.closest && e.target.closest('.snooze-dropdown')) {
      // Focus moves on by itself; the menu must not stay open behind it.
      closeAll(null);
      return;
    }
    if (!e.target.classList || !e.target.classList.contains('snooze-option')) return;
    var options = Array.prototype.slice.call(
      e.target.closest('.snooze-dropdown').querySelectorAll('.snooze-option')
    );
    var idx = options.indexOf(e.target);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      options[(idx + 1) % options.length].focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      options[(idx - 1 + options.length) % options.length].focus();
    } else if (e.key === 'Home') {
      e.preventDefault();
      options[0].focus();
    } else if (e.key === 'End') {
      e.preventDefault();
      options[options.length - 1].focus();
    }
  });
})();
