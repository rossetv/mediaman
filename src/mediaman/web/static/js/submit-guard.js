/**
 * Submit guard — stops a form from posting twice.
 *
 * Any <form data-submit-guard> ignores a second submit while the first is in
 * flight and shows it is busy (aria-busy, dimmed buttons). The buttons are not
 * disabled: a disabled submitter would drop its own name/value from the post.
 * Lives in an external file so the page CSP needs no 'unsafe-inline'.
 */
(function () {
  'use strict';

  // Esc or Stop during a slow post leaves the page here with the form still
  // marked busy, so the mark also lapses on its own after this long.
  var BUSY_MS = 10000;

  function setBusy(form, busy) {
    form.toggleAttribute('data-busy', busy);
    if (busy) form.setAttribute('aria-busy', 'true');
    else form.removeAttribute('aria-busy');
  }

  function clearBusy() {
    document.querySelectorAll('form[data-submit-guard][data-busy]').forEach(function (f) {
      setBusy(f, false);
    });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('form[data-submit-guard]')) return;
    if (form.hasAttribute('data-busy')) { e.preventDefault(); return; }
    setBusy(form, true);
    setTimeout(function () { setBusy(form, false); }, BUSY_MS);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') clearBusy();
  });

  // Back/forward restores the page from cache with the form still marked busy.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    clearBusy();
  });
})();
