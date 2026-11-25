/* Modal a11y helpers:
 *
 *   - The page behind an open modal is inert, all but the toasts, and Tab goes round
 *     the modal's own controls: those of the last opened, where one is over another
 *   - Escape closes the active modal via a caller-supplied close function
 *     (attach by calling window.ModalA11y.register(id, closeFn))
 *   - On open, the element marked data-modal-focus (a control, or a heading with
 *     tabindex="-1") receives focus, or else the first focusable one inside the modal
 *   - On close, focus returns to the element that was focused when the
 *     modal opened (the trigger)
 *
 * A modal is considered open when either:
 *   - the element has the class .is-visible or .active, OR
 *   - its [hidden] attribute is absent
 *
 * Integration:
 *   ModalA11y.register('delete-modal', closeDeleteModal);
 *   ModalA11y.open('delete-modal');
 *   ModalA11y.close('delete-modal');
 *
 * If the codebase already toggles the class directly, you can call
 * `ModalA11y.onOpened('delete-modal')` after adding the class to fire
 * the focus trap and initial focus, and `ModalA11y.onClosed('delete-modal')`
 * to restore focus to the trigger. Something with a focus of its own calls
 * `MM.inertPage.hold(node)` as it opens and `MM.inertPage.release(node)` as it closes,
 * for the inert page and the Tab trap alone.
 */
(function (global) {
  'use strict';

  var FOCUSABLE = [
    'a[href]', 'area[href]', 'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])', 'textarea:not([disabled])',
    'summary', 'iframe', '[tabindex]', '[contenteditable="true"]'
  ].join(',');

  var registry = {};           // id -> { closeFn, lastTrigger }
  var open = [];               // the open modals, in open order
  var held = [];               // what the last of them has made inert

  function getModal(id) { return document.getElementById(id); }

  function isVisible(el) {
    return el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  }

  function getFocusable(modal) {
    if (!modal) return [];
    return Array.prototype.filter.call(
      modal.querySelectorAll(FOCUSABLE),
      function (el) {
        return !el.hasAttribute('disabled')
          && !(parseInt(el.getAttribute('tabindex'), 10) < 0)
          && !el.closest('[inert]')
          && el.getAttribute('aria-hidden') !== 'true'
          && isVisible(el);
      }
    );
  }

  function topModal() { return open[open.length - 1] || null; }

  /* Makes all that stands outside the modal inert, whatever level of the page the
     modal is on. Toasts stay in reach; they may stand in what is full screen. A confirm
     fading out still catches clicks, which what is inert does not. */
  function shutOut(node, keep) {
    Array.prototype.forEach.call(node.children, function (child) {
      if (child.inert || keep.indexOf(child) !== -1) return;
      if (keep.some(function (k) { return child.contains(k); })) { shutOut(child, keep); return; }
      child.inert = true;
      held.push(child);
    });
  }

  function settle() {
    held.forEach(function (node) { node.inert = false; });
    held = [];
    if (!open.length) return;
    var keep = [topModal(), document.getElementById('ui-toast-host')].filter(Boolean);
    Array.prototype.push.apply(keep, document.querySelectorAll('.ui-confirm-overlay.is-leaving'));
    shutOut(document.body, keep);
  }

  function hold(modal) {
    if (open.indexOf(modal) !== -1) return;
    open.push(modal);
    settle();
  }

  function release(modal) {
    var at = open.indexOf(modal);
    if (at === -1) return;
    open.splice(at, 1);
    settle();
  }

  function focusFirst(modal) {
    var marked = Array.prototype.filter.call(modal.querySelectorAll('[data-modal-focus]:not([disabled])'), isVisible);
    var target = marked[0] || getFocusable(modal)[0] || modal;
    if (target && typeof target.focus === 'function') {
      // Ensure the modal itself is focusable as a last resort
      if (target === modal && !modal.hasAttribute('tabindex')) {
        modal.setAttribute('tabindex', '-1');
      }
      target.focus();
    }
  }

  /* Safari's own step passes over buttons and checkboxes, and out of the modal, so every
     step is made here. From what is no tab stop itself, such as a heading, it goes on from
     where that stands. */
  function trapTab(e, modal) {
    if (e.key !== 'Tab' || e.defaultPrevented) return;
    e.preventDefault();
    var active = document.activeElement;
    var focusables = getFocusable(modal);
    var stops = Array.prototype.filter.call(modal.querySelectorAll('*'), function (el) {
      return el === active || focusables.indexOf(el) !== -1;
    });
    if (!stops.length) { focusFirst(modal); return; }
    var at = stops.indexOf(active);
    if (e.shiftKey) at = at <= 0 ? stops.length - 1 : at - 1;
    else at = (at + 1) % stops.length;
    stops[at].focus();
  }

  function onKeydown(e) {
    var modal = topModal();
    if (!modal) return;
    if (e.key === 'Escape') {
      var entry = registry[modal.id];
      if (entry && typeof entry.closeFn === 'function') {
        e.preventDefault();
        entry.closeFn();
      }
      return;
    }
    trapTab(e, modal);
  }

  document.addEventListener('keydown', onKeydown);

  /* Wire data-close-modal buttons (replaces hardcoded onclick="closeModal()").
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
    hold(modal);
    // ARIA defaults
    if (!modal.hasAttribute('role')) modal.setAttribute('role', 'dialog');
    if (!modal.hasAttribute('aria-modal')) modal.setAttribute('aria-modal', 'true');
    // Focus something inside
    // Defer one tick so any display/opacity transition has applied
    setTimeout(function () { if (topModal() === modal) focusFirst(modal); }, 0);
  }

  function onClosed(id) {
    var entry = registry[id];
    if (!entry) return;
    var modal = getModal(id);
    release(modal);
    // What was opened from here, and is still open, hands the focus on to where this one would.
    open.forEach(function (other) {
      var over = registry[other.id];
      if (over && modal.contains(over.lastTrigger)) over.lastTrigger = entry.lastTrigger;
    });
    if (entry.lastTrigger && typeof entry.lastTrigger.focus === 'function') {
      try { entry.lastTrigger.focus(); } catch (e) { /* ignore */ }
    }
  }

  global.ModalA11y = {
    register: register,
    onOpened: onOpened,
    onClosed: onClosed
  };
  global.MM = global.MM || {};
  global.MM.inertPage = { hold: hold, release: release };
})(window);
