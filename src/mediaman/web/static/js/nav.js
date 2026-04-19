/* Mobile "More" sheet behaviour: toggle from the More tab, close on
 * backdrop click, close on Escape. */
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
  }

  document.addEventListener('click', function (e) {
    // Trigger button
    var t = e.target.closest && e.target.closest('[aria-controls="nav-more-sheet"]');
    if (t) { e.preventDefault(); toggleSheet(); return; }
    // Backdrop click
    var s = sheet();
    if (!s || !s.hasAttribute('open')) return;
    if (e.target === s) closeSheet(false);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var s = sheet();
    if (!s || !s.hasAttribute('open')) return;
    closeSheet(true);
  });
})();
