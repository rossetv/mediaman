/**
 * Submit guard — stops a form from posting twice.
 *
 * Any <form data-submit-guard> ignores a second submit while the first is in
 * flight and shows it is busy: aria-busy, dimmed buttons, and on the one pressed
 * (is-pressed) a spinner and its busy label when it has one. The buttons are not
 * disabled: a disabled submitter would drop its own name/value from the post.
 * Lives in an external file so the page CSP needs no 'unsafe-inline'.
 */
(function () {
  'use strict';

  // Stop during a slow post leaves the page here with the form still marked
  // busy, so the mark lapses on its own after this long. Esc does not release
  // it: it can close a password-manager popup while the post is still running.
  var BUSY_MS = 10000;

  var idle = new WeakMap(); // a busy button's own content

  function setBusy(form, busy, pressed) {
    form.toggleAttribute('data-busy', busy);
    if (busy) form.setAttribute('aria-busy', 'true');
    else form.removeAttribute('aria-busy');
    form.querySelectorAll('[data-busy-label]').forEach(function (b) {
      if (busy && b === pressed) {
        idle.set(b, Array.from(b.childNodes));
        b.style.minWidth = b.offsetWidth + 'px';
        b.style.minHeight = b.offsetHeight + 'px';
        var spin = document.createElement('span');
        spin.className = 'btn-spinner';
        spin.setAttribute('aria-hidden', 'true');
        var label = document.createElement('span');
        label.textContent = b.getAttribute('data-busy-label');
        b.replaceChildren(spin, label);
        b.classList.add('is-pressed');
      } else if (!busy && idle.has(b)) {
        b.replaceChildren.apply(b, idle.get(b));
        idle.delete(b);
        b.style.minWidth = b.style.minHeight = '';
        b.classList.remove('is-pressed');
      }
    });
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
    setBusy(form, true, e.submitter || form.querySelector('[data-busy-label]'));
    setTimeout(function () { setBusy(form, false); }, BUSY_MS);
  });

  // Back/forward restores the page from cache with the form still marked busy.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    clearBusy();
  });
})();
