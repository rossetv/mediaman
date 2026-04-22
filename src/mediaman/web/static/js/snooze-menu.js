/* Snooze-menu: split-button dropdown used on Library and Dashboard cards.
 *
 * Markup shape expected:
 *   <div class="keep-wrapper">
 *     <button class="btn-sm btn-sm-keep" ...>Keep</button>
 *     <button class="btn-sm-caret" aria-haspopup="menu" aria-expanded="false" aria-label="More keep options">&#9662;</button>
 *     <div class="snooze-dropdown" role="menu"> ... </div>
 *   </div>
 *
 * Click/tap the caret to toggle .is-open on the wrapper. Escape or outside click closes.
 */
(function () {
  'use strict';

  function closeAll(except) {
    var wrappers = document.querySelectorAll('.keep-wrapper.is-open');
    for (var i = 0; i < wrappers.length; i++) {
      if (wrappers[i] === except) continue;
      wrappers[i].classList.remove('is-open');
      var trigger = wrappers[i].querySelector('.btn-sm-caret');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
  }

  function toggle(wrapper) {
    var open = !wrapper.classList.contains('is-open');
    closeAll(wrapper);
    wrapper.classList.toggle('is-open', open);
    var trigger = wrapper.querySelector('.btn-sm-caret');
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = wrapper.querySelector('.snooze-dropdown .snooze-option');
      if (first) first.focus();
    }
  }

  document.addEventListener('click', function (e) {
    var caret = e.target.closest && e.target.closest('.btn-sm-caret');
    if (caret) {
      e.preventDefault();
      e.stopPropagation();
      var wrapper = caret.closest('.keep-wrapper');
      if (wrapper) toggle(wrapper);
      return;
    }
    // Outside click closes everything
    if (!e.target.closest || !e.target.closest('.keep-wrapper')) {
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
