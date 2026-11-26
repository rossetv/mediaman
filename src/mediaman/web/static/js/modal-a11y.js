/* Modal a11y helpers:
 *
 *   - The page behind an open modal is inert, all but the toasts, the Keep menu and, under a title's
 *     page, the bar of what plays on a TV, and Tab goes round the modal's own controls: those of the last opened, where one is over another
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
 * `MM.inertPage.hold(node[, spare])` as it opens and `MM.inertPage.release(node)` as it closes,
 * for the inert page and the Tab trap alone. What draws a trigger anew, or takes away what
 * modals were opened from, says so with `ModalA11y.handOn(find)` and `ModalA11y.closeFrom(node)`.
 */
(function (global) {
  'use strict';

  var FOCUSABLE = [
    'a[href]', 'area[href]', 'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])', 'textarea:not([disabled])',
    'summary', 'iframe', '[tabindex]', '[contenteditable="true"]'
  ].join(',');

  var registry = {};           // id -> { closeFn, and while open: node, lastTrigger }
  var open = [];               // the open modals, in open order
  var held = [];               // what the last of them has made inert
  var spares = [];             // by open modal, what it hides from assistive tech in place of inert
  var veiled = [];             // what is hidden so, with the aria-hidden it had

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
     modal is on. Toasts and the Keep menu stay in reach; toasts may stand in what is full screen. A confirm
     fading out still catches clicks, which what is inert does not. */
  function shutOut(node, keep, spare) {
    Array.prototype.forEach.call(node.children, function (child) {
      if (child.inert || keep.indexOf(child) !== -1) return;
      var holds = function (k) { return child.contains(k); };
      if (keep.some(holds)) { shutOut(child, keep, spare); return; }
      if (spare.indexOf(child) !== -1) {
        veiled.push([child, child.getAttribute('aria-hidden')]);
        child.setAttribute('aria-hidden', 'true');
        return;
      }
      if (spare.some(holds)) { shutOut(child, keep, spare); return; }
      child.inert = true;
      held.push(child);
    });
  }

  /* What plays on a TV stays in reach over a title's page, after whose own controls Tab comes to it. */
  function tvBar() {
    return topModal() && topModal().id === 'ttl' ? document.getElementById('tv-mini') : null;
  }

  function settle() {
    held.forEach(function (node) { node.inert = false; });
    held = [];
    veiled.forEach(function (was) {
      if (was[1] === null) was[0].removeAttribute('aria-hidden');
      else was[0].setAttribute('aria-hidden', was[1]);
    });
    veiled = [];
    if (!open.length) return;
    var keep = [topModal(), document.getElementById('ui-toast-host'), document.getElementById('watch-keep-menu')].filter(Boolean);
    Array.prototype.push.apply(keep, document.querySelectorAll('.ui-confirm-overlay.is-leaving'));
    if (tvBar()) keep.push(tvBar());
    shutOut(document.body, keep, [].concat.apply([], spares));
  }

  /* *spare*: what is too big to make inert at once (that restyles all it holds) and that the
     modal covers; Tab is held in the modal all the same. Spared under one modal, it is spared
     under those over it. */
  function hold(modal, spare) {
    if (open.indexOf(modal) !== -1) return;
    open.push(modal);
    spares.push(spare || []);
    settle();
  }

  function release(modal) {
    var at = open.indexOf(modal);
    if (at === -1) return;
    open.splice(at, 1);
    spares.splice(at, 1);
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

  /* A group of radios is one stop, at the one with the keyboard, else the checked one, else the first. */
  function oneRadioEach(stops, active) {
    function group(el) { return el.matches('input[type="radio"]') && el.getAttribute('name'); }
    return stops.filter(function (el) {
      var name = group(el);
      if (!name) return true;
      var mates = stops.filter(function (other) { return group(other) === name; });
      var stop = mates.indexOf(active) !== -1 ? active
        : mates.filter(function (other) { return other.checked; })[0] || mates[0];
      return el === stop;
    });
  }

  /* Safari's own step passes over buttons and checkboxes, and out of the modal, so every
     step is made here. From what is no tab stop itself, such as a heading, it goes on from
     where that stands. */
  function trapTab(e, modal) {
    if (e.key !== 'Tab' || e.defaultPrevented) return;
    e.preventDefault();
    var active = document.activeElement;
    var bar = tvBar();
    var focusables = oneRadioEach(getFocusable(modal).concat(getFocusable(bar)), active);
    function stopsIn(node) {
      return Array.prototype.filter.call(node ? node.querySelectorAll('*') : [], function (el) {
        return el === active || focusables.indexOf(el) !== -1;
      });
    }
    var stops = stopsIn(modal).concat(stopsIn(bar));
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
    }
  }

  document.addEventListener('keydown', onKeydown);

  /* After the page's own keydown handlers: a menu gives the focus back to its button first. */
  global.addEventListener('keydown', function (e) {
    if (topModal()) trapTab(e, topModal());
  });

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
    entry.node = modal;
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
    if (!entry || !entry.node) return;
    var modal = entry.node; // it may have left the page, or been replaced on it
    var trigger = entry.lastTrigger;
    entry.node = entry.lastTrigger = null;
    release(modal);
    // What was opened from here, and is still open, hands the focus on to where this one would.
    handOn(function (from) { return modal.contains(from) ? trigger : null; });
    if (trigger && typeof trigger.focus === 'function') {
      try { trigger.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }
  }

  /* Where the open modals give the focus back to: what *find* makes of each one's
     trigger, or that itself where it makes nothing of it. */
  function handOn(find) {
    open.forEach(function (modal) {
      var entry = registry[modal.id];
      if (entry && entry.lastTrigger) entry.lastTrigger = find(entry.lastTrigger) || entry.lastTrigger;
    });
  }

  /* Closes what was opened from within *node*, or from what has left the page since,
     and what was opened from that. */
  function closeFrom(node) {
    var within = [node];
    open.filter(function (modal) {
      var entry = registry[modal.id];
      var from = entry && entry.closeFn && entry.lastTrigger;
      if (!from || (from.isConnected && !within.some(function (n) { return n.contains(from); }))) return false;
      within.push(modal);
      return true;
    }).reverse().forEach(function (modal) { registry[modal.id].closeFn(); });
  }

  global.ModalA11y = {
    register: register,
    onOpened: onOpened,
    onClosed: onClosed,
    handOn: handOn,
    closeFrom: closeFrom
  };
  global.MM = global.MM || {};
  global.MM.inertPage = { hold: hold, release: release };
})(window);
