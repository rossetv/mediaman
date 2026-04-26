/* Modal a11y helpers:
 *
 *   - Tab-focus trap inside any element with data-modal="true"
 *   - Escape closes the active modal via a caller-supplied close function
 *     (attach by calling window.ModalA11y.register(id, closeFn))
 *   - On open, first focusable element inside the modal receives focus
 *   - On close, focus returns to the element that was focused when the
 *     modal opened (the trigger)
 *
 * A modal is considered open when either:
 *   - the element has the class .is-visible or .active, OR
 *   - its inline style.display is not "none"
 *
 * Integration:
 *   ModalA11y.register('delete-modal', closeDeleteModal);
 *   ModalA11y.open('delete-modal');
 *   ModalA11y.close('delete-modal');
 *
 * If the codebase already toggles the class directly, you can call
 * `ModalA11y.onOpened('delete-modal')` after adding the class to fire
 * the focus trap and initial focus, and `ModalA11y.onClosed('delete-modal')`
 * to restore focus to the trigger.
 */
(function (global) {
  'use strict';

  var FOCUSABLE = [
    'a[href]', 'area[href]', 'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])', 'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
    '[contenteditable="true"]'
  ].join(',');

  var registry = {};           // id -> { closeFn, lastTrigger, isOpen }
  var openStack = [];          // ids in open order

  function getModal(id) { return document.getElementById(id); }

  function getFocusable(modal) {
    if (!modal) return [];
    return Array.prototype.filter.call(
      modal.querySelectorAll(FOCUSABLE),
      function (el) {
        return !el.hasAttribute('disabled')
          && el.getAttribute('aria-hidden') !== 'true'
          && el.offsetParent !== null;  // visible
      }
    );
  }

  function focusFirst(modal) {
    var focusables = getFocusable(modal);
    var target = focusables[0] || modal;
    if (target && typeof target.focus === 'function') {
      // Ensure the modal itself is focusable as a last resort
      if (target === modal && !modal.hasAttribute('tabindex')) {
        modal.setAttribute('tabindex', '-1');
      }
      target.focus();
    }
  }

  function trapTab(e, modal) {
    if (e.key !== 'Tab') return;
    var focusables = getFocusable(modal);
    if (focusables.length === 0) {
      e.preventDefault();
      modal.focus();
      return;
    }
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function topModal() {
    if (!openStack.length) return null;
    return openStack[openStack.length - 1];
  }

  function onKeydown(e) {
    var topId = topModal();
    if (!topId) return;
    var modal = getModal(topId);
    if (!modal) return;
    if (e.key === 'Escape') {
      var entry = registry[topId];
      if (entry && typeof entry.closeFn === 'function') {
        e.preventDefault();
        entry.closeFn();
      }
      return;
    }
    trapTab(e, modal);
  }

  document.addEventListener('keydown', onKeydown);

  /* Wire data-close-modal buttons (H74: replaces hardcoded onclick="closeModal()").
     Delegates to the registered close function for the nearest ancestor modal. */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-close-modal]');
    if (!btn) return;
    /* Walk up DOM to find the modal id */
    var modal = btn.closest('[id][role="dialog"]');
    if (modal && registry[modal.id] && typeof registry[modal.id].closeFn === 'function') {
      registry[modal.id].closeFn();
    }
  });

  function register(id, closeFn) {
    registry[id] = registry[id] || {};
    registry[id].closeFn = closeFn;
  }

  function onOpened(id) {
    var modal = getModal(id);
    if (!modal) return;
    var entry = registry[id] = registry[id] || {};
    entry.lastTrigger = document.activeElement;
    entry.isOpen = true;
    if (openStack.indexOf(id) === -1) openStack.push(id);
    // ARIA defaults
    if (!modal.hasAttribute('role')) modal.setAttribute('role', 'dialog');
    if (!modal.hasAttribute('aria-modal')) modal.setAttribute('aria-modal', 'true');
    // Focus something inside
    // Defer one tick so any display/opacity transition has applied
    setTimeout(function () { focusFirst(modal); }, 0);
  }

  function onClosed(id) {
    var entry = registry[id];
    if (!entry) return;
    entry.isOpen = false;
    var idx = openStack.indexOf(id);
    if (idx !== -1) openStack.splice(idx, 1);
    if (entry.lastTrigger && typeof entry.lastTrigger.focus === 'function') {
      try { entry.lastTrigger.focus(); } catch (e) { /* ignore */ }
    }
  }

  global.ModalA11y = {
    register: register,
    onOpened: onOpened,
    onClosed: onClosed
  };
})(window);
