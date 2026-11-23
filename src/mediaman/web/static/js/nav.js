/* Mobile "More" sheet behaviour: toggle from the More tab, close on
 * backdrop click or Escape, keep Tab inside while open, and close when
 * the viewport grows past the phone layout. The fade-out is CSS; the
 * open state changes at once. */
(function () {
  'use strict';

  function sheet() { return document.getElementById('nav-more-sheet'); }
  function trigger() {
    return document.querySelector('[aria-controls="nav-more-sheet"]');
  }

  function closeSheet(focusTrigger) {
    var s = sheet();
    if (!s) return;
    s.removeAttribute('open');
    var btn = trigger();
    if (btn) {
      btn.setAttribute('aria-expanded', 'false');
      if (focusTrigger) btn.focus();
    }
  }

  function toggleSheet() {
    var s = sheet();
    if (!s) return;
    var open = s.toggleAttribute('open');
    var btn = trigger();
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = s.querySelector('a[href], button:not([disabled])');
      if (first) first.focus();
    }
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[aria-controls="nav-more-sheet"]');
    if (t) { e.preventDefault(); toggleSheet(); return; }
    var s = sheet();
    if (!s || !s.hasAttribute('open')) return;
    if (e.target === s) closeSheet(true);
  });

  document.addEventListener('keydown', function (e) {
    var s = sheet();
    if (!s || !s.hasAttribute('open')) return;
    if (e.key === 'Escape') { closeSheet(true); return; }
    if (e.key !== 'Tab') return;
    var items = s.querySelectorAll('a[href], button:not([disabled])');
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (!s.contains(document.activeElement)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  if (window.matchMedia) {
    window.matchMedia('(min-width: 700px)').addEventListener('change', function (e) {
      if (!e.matches) return;
      var s = sheet();
      var a = document.activeElement;
      // The browser may already have dropped focus to <body> when the
      // sheet went display:none, so an open modal sheet counts as focus in it.
      var hadFocus = s && s.hasAttribute('open') && (!a || a === document.body || s.contains(a));
      closeSheet(false);
      var main = document.getElementById('main-content');
      if (hadFocus && main) main.focus();
    });
  }
})();
