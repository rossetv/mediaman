/* Profile menu: one menu for both profile buttons. A press on either, or an arrow
 * key on it, opens the menu; arrows, Home and End move in it; Escape, Tab, a press
 * outside and focus moving away close it. Below 700 px it is a bottom sheet: the
 * rest of the page is inert and does not scroll under it. The fade is CSS; the
 * open state changes at once. */
(function () {
  'use strict';

  // iOS Safari only applies :active when a touchstart listener exists on the
  // document or body; this empty one switches the pressed states on.
  document.addEventListener('touchstart', function () {}, { passive: true });

  var menu = document.getElementById('profile-menu');
  if (!menu) return;

  var phone = window.matchMedia('(max-width: 699.98px)');
  var opener = null;
  var held = [];

  function items() {
    return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
  }
  function buttons() {
    return Array.prototype.slice.call(document.querySelectorAll('.profile-btn'));
  }
  function buttonOf(target) {
    return target && target.closest ? target.closest('.profile-btn') : null;
  }
  function isOpen() { return menu.hasAttribute('open'); }

  function holdPage() {
    Array.prototype.forEach.call(document.body.children, function (node) {
      if (node === menu || node.inert) return;
      node.inert = true;
      held.push(node);
    });
    document.body.style.overflow = 'hidden';
  }

  function releasePage() {
    if (!held.length) return;
    held.forEach(function (node) { node.inert = false; });
    held = [];
    document.body.style.overflow = '';
  }

  function open(button, last) {
    opener = button;
    menu.setAttribute('open', '');
    button.setAttribute('aria-expanded', 'true');
    if (phone.matches) holdPage();
    var list = items();
    list[last ? list.length - 1 : 0].focus();
  }

  function close(returnFocus) {
    if (!isOpen()) return;
    menu.removeAttribute('open');
    releasePage();
    opener.setAttribute('aria-expanded', 'false');
    if (returnFocus) opener.focus();
  }

  document.addEventListener('click', function (e) {
    var button = buttonOf(e.target);
    if (button) {
      if (isOpen()) close(true); else open(button, false);
      return;
    }
    if (!isOpen()) return;
    // The sheet's backdrop is the menu's own box around the panel.
    if (e.target === menu) close(true);
    else if (e.target.closest && e.target.closest('a[role="menuitem"]')) close(false);
  });

  document.addEventListener('pointerdown', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  }, true);

  document.addEventListener('focusin', function (e) {
    if (isOpen() && !menu.contains(e.target) && !buttonOf(e.target)) close(false);
  });

  // Capture, so the page's own shortcuts do not act on a key the menu took.
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (!isOpen()) {
      var button = buttonOf(e.target);
      if (!button || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
      e.preventDefault();
      e.stopPropagation();
      open(button, e.key === 'ArrowUp');
      return;
    }
    // Focus goes on from the button, as if the menu had not been there.
    if (e.key === 'Tab') { close(true); return; }
    var list = items();
    var at = list.indexOf(document.activeElement);
    var to = null;
    if (e.key === 'ArrowDown') to = at + 1;
    else if (e.key === 'ArrowUp') to = at < 0 ? list.length - 1 : at - 1;
    else if (e.key === 'Home') to = 0;
    else if (e.key === 'End') to = list.length - 1;
    if (to !== null) list[(to + list.length) % list.length].focus();
    else if (e.key === 'Escape') close(true);
    // A link answers Enter by itself, but not the space bar.
    else if (e.key === ' ' && at >= 0 && list[at].tagName === 'A') list[at].click();
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  // The button that was pressed goes out of sight with its layout.
  phone.addEventListener('change', function () {
    if (!isOpen()) return;
    close(false);
    var shown = buttons().filter(function (b) { return b.getClientRects().length; })[0];
    if (shown) shown.focus();
  });

  // Back brings the page as it was left, open menu and all.
  window.addEventListener('pageshow', function () { close(false); });
})();
