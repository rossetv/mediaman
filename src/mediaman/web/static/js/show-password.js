/**
 * Show/hide toggle for password inputs marked data-show-password.
 *
 * The button is added here, so without JavaScript the field is a plain
 * password input. Lives in an external file so the page CSP needs no
 * 'unsafe-inline'.
 */
(function () {
  'use strict';

  document.querySelectorAll('input[type="password"][data-show-password]').forEach(function (input) {
    var wrap = document.createElement('div');
    wrap.className = 'pw-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle';
    btn.textContent = 'Show';
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', 'Show password');
    btn.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    });
    wrap.appendChild(btn);
  });
})();
