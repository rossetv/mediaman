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
    // Moving an element drops its focus, so note it now and give it back.
    var hadFocus = document.activeElement === input;
    var wrap = document.createElement('div');
    wrap.className = 'pw-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-toggle';
    btn.textContent = 'Show';
    // The name starts with the visible word, so voice control can say "click Hide".
    btn.setAttribute('aria-label', 'Show password');
    function setShown(show) {
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? 'Hide' : 'Show';
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    }
    btn.addEventListener('click', function () {
      setShown(input.type === 'password');
    });
    wrap.appendChild(btn);

    if (hadFocus || (input.hasAttribute('autofocus') && (!document.activeElement || document.activeElement === document.body))) {
      input.focus();
    }
    // A reopened form must not keep showing the last password typed into it.
    if (input.form) input.form.addEventListener('reset', function () { setShown(false); });
  });
})();
