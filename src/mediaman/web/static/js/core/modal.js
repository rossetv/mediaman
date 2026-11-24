/**
 * core/modal.js — reusable modal lifecycle management.
 *
 * Public surface (under the global MM.modal namespace):
 *
 *   MM.modal.setupDetail(modalEl, options?) → { open(payload?), close() }
 *
 *   Sets up a modal with:
 *     - Close buttons wired via [data-close-modal] descendants and any
 *       extra selectors listed in ``options.closeSelectors``.
 *     - ESC key handling (via ModalA11y if available, direct otherwise).
 *     - Backdrop-click-to-close (click on the modal element itself).
 *     - Registration with window.ModalA11y for focus trapping and
 *       focus-restore on close.
 *
 *   options:
 *     onOpen(payload?)   — called when open() is invoked, after ARIA attrs
 *                          are set and ModalA11y.onOpened has fired.
 *     onClose()          — called when the modal closes (any route).
 *     activeClass        — when set, toggles this class on the modal element
 *                          to show/hide instead of the ``hidden`` attribute.
 *                          Used by the ``.is-visible`` / ``.active`` modals
 *                          where the CSS owns the display rule.
 *     manageBodyOverflow — when true (the default), sets
 *                          ``body.style.overflow = 'hidden'`` on open and
 *                          restores it on close. Disable for inline modals
 *                          that don't need scroll locking.
 *     useModalA11y       — when false, skips the ModalA11y.register /
 *                          onOpened / onClosed lifecycle (e.g. when the
 *                          modal handles its own ESC + focus management);
 *                          the page behind is out of reach all the same.
 *                          Default: true.
 *     closeSelectors     — array of CSS selectors whose descendants
 *                          should also close the modal on click (in
 *                          addition to ``[data-close-modal]``).
 *
 *   Returns an object with:
 *     open(payload?)  — shows the modal; passes payload to onOpen.
 *     close()         — hides the modal; calls onClose then restores focus.
 *
 * No external dependencies beyond core/dom.js being loaded first
 * (for MM.dom, used internally if available).
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  /**
   * Wire up a modal element and return open/close handles.
   *
   * @param {HTMLElement} modalEl  - The modal root (the backdrop element).
   * @param {object}      [opts]   - Optional hooks: { onOpen, onClose,
   *                                  activeClass,
   *                                  manageBodyOverflow, useModalA11y,
   *                                  closeSelectors }.
   * @returns {{ open: function, close: function }}
   */
  function setupDetail(modalEl, opts) {
    opts = opts || {};
    var activeClass = opts.activeClass || null;
    var manageBodyOverflow = (opts.manageBodyOverflow !== false);
    var useModalA11y = (opts.useModalA11y !== false);

    /* A .modal-backdrop or .keep-dialog-overlay plays its exit (`.is-closing`) before it is hidden.
       The dialog counts as closed from the moment close() runs; reopening
       during the exit cancels it. */
    var _leaving = null;
    var _timer = null;
    var _onEnd = null;

    function _cancelExit() {
      _leaving = null;
      if (_onEnd) { modalEl.removeEventListener('animationend', _onEnd); _onEnd = null; }
      if (_timer !== null) { clearTimeout(_timer); _timer = null; }
      modalEl.classList.remove('is-closing');
    }
    function _show() {
      _cancelExit();
      if (activeClass) modalEl.classList.add(activeClass);
      else modalEl.hidden = false;
    }
    function _hideAfterExit() {
      if (_leaving) return;
      var reduce = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
      var exits = modalEl.classList.contains('modal-backdrop') || modalEl.classList.contains('keep-dialog-overlay');
      if (reduce || !exits || !_isVisible()) {
        _cancelExit();
        _hide();
        return;
      }
      _leaving = {};
      function done(e) {
        if (e && e.target !== modalEl) return;
        _cancelExit();
        _hide();
      }
      modalEl.classList.add('is-closing');
      _onEnd = done;
      modalEl.addEventListener('animationend', done);
      /* animationend never comes if the page is hidden mid-way. */
      _timer = setTimeout(done, 250);
    }
    function _hide() {
      if (activeClass) modalEl.classList.remove(activeClass);
      else modalEl.hidden = true;
    }
    function _isVisible() {
      if (_leaving) return false;
      if (activeClass) return modalEl.classList.contains(activeClass);
      return !modalEl.hidden;
    }

    /* Without ModalA11y the modal handles Escape itself, but only while it
       is open: open() adds the listener and close() takes it away again. */
    var _escHandler = null;

    function close() {
      if (!modalEl) return;
      if (_escHandler) document.removeEventListener('keydown', _escHandler);
      _hideAfterExit();
      modalEl.setAttribute('aria-hidden', 'true');
      if (manageBodyOverflow) document.body.style.overflow = '';
      // Before onClose, which may hand the focus back to the page.
      if (!useModalA11y && global.MM.inertPage) global.MM.inertPage.release(modalEl);

      if (typeof opts.onClose === 'function') opts.onClose();

      if (useModalA11y && global.ModalA11y) {
        global.ModalA11y.onClosed(modalEl.id);
      }
    }

    function open(payload) {
      if (!modalEl) return;
      _show();
      if (_escHandler) {
        document.removeEventListener('keydown', _escHandler);
        document.addEventListener('keydown', _escHandler);
      }
      modalEl.setAttribute('aria-hidden', 'false');
      if (manageBodyOverflow) document.body.style.overflow = 'hidden';

      if (useModalA11y && global.ModalA11y) {
        global.ModalA11y.onOpened(modalEl.id);
      } else if (!useModalA11y && global.MM.inertPage) {
        global.MM.inertPage.hold(modalEl);
      }

      if (typeof opts.onOpen === 'function') opts.onOpen(payload);
    }

    modalEl.addEventListener('click', function (e) {
      if (e.target === modalEl) close();
    });

    /* ESC key: fall back if ModalA11y isn't loaded. */
    if (!useModalA11y || !global.ModalA11y) {
      _escHandler = function (e) {
        if (e.key !== 'Escape') return;
        if (!_isVisible()) return;
        if (modalEl.getAttribute('aria-hidden') === 'true') return;
        close();
      };
    }

    if (useModalA11y && global.ModalA11y && modalEl.id) {
      global.ModalA11y.register(modalEl.id, close);
    }

    var closeBtns = modalEl.querySelectorAll('[data-close-modal]');
    for (var i = 0; i < closeBtns.length; i++) {
      closeBtns[i].addEventListener('click', close);
    }
    if (Array.isArray(opts.closeSelectors)) {
      opts.closeSelectors.forEach(function (sel) {
        var els = modalEl.querySelectorAll(sel);
        for (var j = 0; j < els.length; j++) {
          els[j].addEventListener('click', close);
        }
      });
    }

    return { open: open, close: close };
  }

  global.MM.modal = {
    setupDetail: setupDetail,
  };

})(window);
