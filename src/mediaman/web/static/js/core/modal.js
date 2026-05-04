/**
 * core/modal.js — reusable modal lifecycle management.
 *
 * Public surface (under the global MM.modal namespace):
 *
 *   MM.modal.setupDetail(modalEl, options?) → { open(payload?), close() }
 *
 *   Sets up a full-screen detail modal with:
 *     - Close button wired via data-close-modal attribute (or fallback
 *       to the first [data-close-modal] child).
 *     - ESC key handling (via ModalA11y if available, direct otherwise).
 *     - Backdrop-click-to-close (click on the modal element itself).
 *     - Registration with window.ModalA11y for focus trapping and
 *       focus-restore on close.
 *
 *   options:
 *     onOpen(payload?)  — called when open() is invoked, after ARIA attrs
 *                         are set and ModalA11y.onOpened has fired.
 *     onClose()         — called when the modal closes (any route).
 *
 *   Returns an object with:
 *     open(payload?)  — shows the modal; passes payload to onOpen.
 *     close()         — hides the modal; calls onClose then restores focus.
 *
 * Mirrors the lifecycle pattern used by recommended.js and search.js
 * today so those files can be migrated incrementally. This module itself
 * has no dependency on page-specific files — it only reads window.ModalA11y.
 *
 * No external dependencies beyond core/dom.js being loaded first
 * (for MM.dom, used internally if available).
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  /**
   * Wire up a detail modal element and return open/close handles.
   *
   * @param {HTMLElement} modalEl  - The modal root (the backdrop element).
   * @param {object}      [opts]   - Optional hooks: { onOpen, onClose }.
   * @returns {{ open: function, close: function }}
   */
  function setupDetail(modalEl, opts) {
    opts = opts || {};

    function close() {
      if (!modalEl) return;
      modalEl.style.display = 'none';
      modalEl.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';

      if (typeof opts.onClose === 'function') opts.onClose();

      if (global.ModalA11y) {
        global.ModalA11y.onClosed(modalEl.id);
      }
    }

    function open(payload) {
      if (!modalEl) return;
      modalEl.style.display = 'flex';
      modalEl.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';

      if (global.ModalA11y) {
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
    var _escHandler = null;
    if (!global.ModalA11y) {
      _escHandler = function (e) {
        if (e.key !== 'Escape') return;
        if (!modalEl || modalEl.style.display === 'none') return;
        if (modalEl.getAttribute('aria-hidden') === 'true') return;
        close();
      };
      document.addEventListener('keydown', _escHandler);
    }

    /* ── Register with ModalA11y so it owns the ESC / focus-trap ── */
    if (global.ModalA11y && modalEl.id) {
      global.ModalA11y.register(modalEl.id, close);
    }

    /* ── Wire [data-close-modal] buttons inside the modal ── */
    var closeBtns = modalEl.querySelectorAll('[data-close-modal]');
    for (var i = 0; i < closeBtns.length; i++) {
      closeBtns[i].addEventListener('click', close);
    }

    return { open: open, close: close };
  }

  global.MM.modal = {
    setupDetail: setupDetail,
  };

})(window);
