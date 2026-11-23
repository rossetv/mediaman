/**
 * Account page — change-password form.
 *
 * Requires: core/api.js (MM.api), core/reauth.js (MM.reauth).
 * Posts to /api/users/change-password with the same checks, reauth retry
 * and error rendering as the Settings › Users form.
 */
(function () {
  'use strict';

  function renderApiError(target, err) {
    var msg = MM.api.messageOf(err, 'Something went wrong. Try again.');
    var issues = err && err.issues;
    target.textContent = '';
    if (issues && issues.length) {
      var p = document.createElement('div');
      p.textContent = msg;
      target.appendChild(p);
      var ul = document.createElement('ul');
      ul.className = 'inline-form-issues';
      issues.forEach(function (item) {
        var li = document.createElement('li');
        li.textContent = String(item);
        ul.appendChild(li);
      });
      target.appendChild(ul);
    } else {
      target.textContent = msg;
    }
  }

  function init() {
    var form = document.getElementById('account-password-form');
    var result = document.getElementById('account-password-result');
    var submit = document.getElementById('account-submit-password');
    if (!form || !result || !submit) return;

    // The message is announced by the status region; the cursor goes where the fix is.
    function fail(text, fieldId) {
      result.className = 'inline-form-msg err';
      result.textContent = text;
      document.getElementById(fieldId).focus();
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var oldPw = document.getElementById('account-old-password').value;
      var newPw = document.getElementById('account-new-password').value;
      var conf = document.getElementById('account-confirm-password').value;
      if (!oldPw) { fail('Enter your current password.', 'account-old-password'); return; }
      if (newPw.length < 12) { fail('New password must be at least 12 characters.', 'account-new-password'); return; }
      if (newPw !== conf) { fail("Passwords don't match.", 'account-confirm-password'); return; }

      submit.disabled = true;
      MM.reauth.run(function () {
        return MM.api.post('/api/users/change-password', {
          old_password: oldPw,
          new_password: newPw,
        });
      }).then(function () {
        submit.disabled = false;
        form.reset();
        result.className = 'inline-form-msg ok';
        result.textContent = 'Password updated.';
      }).catch(function (err) {
        submit.disabled = false;
        if (err && err.error === 'reauth_cancelled') {
          result.className = 'inline-form-msg';
          result.textContent = '';
          return;
        }
        result.className = 'inline-form-msg err';
        renderApiError(result, err);
        document.getElementById(
          err && err.error === 'wrong_password' ? 'account-old-password' : 'account-new-password'
        ).focus();
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
