/* Snooze-menu: dropdown used on Library (whole pill) and Dashboard (caret).
 *
 * Markup shape expected:
 *   <div class="keep-wrapper">
 *     <button class="keep-trigger" aria-haspopup="menu" aria-expanded="false">...</button>
 *     <div class="snooze-dropdown" role="menu"> ... </div>
 *   </div>
 *
 * Any element matching `.keep-trigger` or `.btn-sm-caret` toggles the dropdown
 * on its parent `.keep-wrapper`. Escape or outside-click closes.
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

  function toggle(wrapper) {
    var open = !wrapper.classList.contains('is-open');
    closeAll(wrapper);
    wrapper.classList.toggle('is-open', open);
    var trigger = wrapper.querySelector(TRIGGER_SELECTOR);
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
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

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
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
