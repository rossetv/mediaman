/**
 * The eye beside a password input marked data-show-password (the .pwd-eye of _password.html):
 * it shows the password and hides it again, as on the password form.
 *
 * Without JavaScript the eye stays out of sight and the field is a plain password input. Lives
 * in an external file so the page CSP needs no 'unsafe-inline'.
 */
(function () {
  'use strict';

  document.documentElement.classList.add('pwd-on');

  document.querySelectorAll('input[type="password"][data-show-password]').forEach(function (input) {
    var eye = input.parentNode.querySelector('.pwd-eye');
    if (!eye) return;
    function setShown(show) {
      input.type = show ? 'text' : 'password';
      eye.setAttribute('aria-pressed', String(show));
      eye.title = show ? 'Hide' : 'Show';
    }
    // The cursor stays in the field while its eye is pressed.
    eye.addEventListener('mousedown', function (e) { e.preventDefault(); });
    eye.addEventListener('click', function () {
      var from = input.selectionStart, to = input.selectionEnd, way = input.selectionDirection, value = input.value;
      setShown(eye.getAttribute('aria-pressed') !== 'true');
      if (document.activeElement !== input) return;
      // Chromium puts the cursor at the start just after the type changes, so it goes back a frame later.
      requestAnimationFrame(function () {
        if (document.activeElement !== input || input.value !== value) return;
        try { input.setSelectionRange(from, to, way); } catch (err) { /* A field that has none. */ }
      });
    });
    // A reopened form must not keep showing the last password typed into it.
    if (input.form) input.form.addEventListener('reset', function () { setShown(false); });
  });
})();
