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
 *                          to show/hide instead of writing ``style.display``.
 *                          Used by the ``.is-visible`` / ``.active`` modals
 *                          where the CSS owns the display rule.
 *     displayValue       — when no ``activeClass`` is set, the inline
 *                          ``style.display`` value applied on open
 *                          (default ``'flex'``). The empty string is
 *                          honoured so the modal falls back to whatever
 *                          the stylesheet specifies.
 *     manageBodyOverflow — when true (the default), sets
 *                          ``body.style.overflow = 'hidden'`` on open and
 *                          restores it on close. Disable for inline modals
 *                          that don't need scroll locking.
 *     useModalA11y       — when false, skips the ModalA11y.register /
 *                          onOpened / onClosed lifecycle (e.g. when the
 *                          modal handles its own ESC + focus management).
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
   *                                  activeClass, displayValue,
   *                                  manageBodyOverflow, useModalA11y,
   *                                  closeSelectors }.
   * @returns {{ open: function, close: function }}
   */
  function setupDetail(modalEl, opts) {
    opts = opts || {};
    var activeClass = opts.activeClass || null;
    var displayValue = (typeof opts.displayValue === 'string')
      ? opts.displayValue
      : 'flex';
    var manageBodyOverflow = (opts.manageBodyOverflow !== false);
    var useModalA11y = (opts.useModalA11y !== false);

    function _show() {
      if (activeClass) modalEl.classList.add(activeClass);
      else modalEl.style.display = displayValue;
    }
    function _hide() {
      if (activeClass) modalEl.classList.remove(activeClass);
      else modalEl.style.display = 'none';
    }
    function _isVisible() {
      if (activeClass) return modalEl.classList.contains(activeClass);
      return modalEl.style.display && modalEl.style.display !== 'none';
    }

    function close() {
      if (!modalEl) return;
      _hide();
      modalEl.setAttribute('aria-hidden', 'true');
      if (manageBodyOverflow) document.body.style.overflow = '';

      if (typeof opts.onClose === 'function') opts.onClose();

      if (useModalA11y && global.ModalA11y) {
        global.ModalA11y.onClosed(modalEl.id);
      }
    }

    function open(payload) {
      if (!modalEl) return;
      _show();
      modalEl.setAttribute('aria-hidden', 'false');
      if (manageBodyOverflow) document.body.style.overflow = 'hidden';

      if (useModalA11y && global.ModalA11y) {
        global.ModalA11y.onOpened(modalEl.id);
      }

      if (typeof opts.onOpen === 'function') opts.onOpen(payload);
    }

    /* ── Backdrop click ── */
    modalEl.addEventListener('click', function (e) {
      if (e.target === modalEl) close();
    });

    /* ── ESC key — fall back if ModalA11y isn't loaded ──
     * Only act on ESC when this specific modal is currently visible —
     * avoids closing a hidden modal (wasteful) and avoids two
     * setupDetail-managed modals on the same page from each handling
     * every ESC press. */
    if (!useModalA11y || !global.ModalA11y) {
      document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        if (!modalEl) return;
        if (!_isVisible()) return;
        if (modalEl.getAttribute('aria-hidden') === 'true') return;
        close();
      });
    }

    /* ── Register with ModalA11y so it owns the ESC / focus-trap ── */
    if (useModalA11y && global.ModalA11y && modalEl.id) {
      global.ModalA11y.register(modalEl.id, close);
    }

    /* ── Wire [data-close-modal] buttons inside the modal ── */
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
