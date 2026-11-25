/**
 * Forced password change page: the form (password-form.js) and the way out.
 *
 * Requires: password-form.js (MM.password), core/api.js (MM.api).
 */
(function () {
  'use strict';

  var form = document.getElementById('fpc-form');
  if (form) {
    MM.password.form(form, { same: true });
    // A password manager is to see one new password.
    var again = document.getElementById('confirm_password');
    again.type = 'hidden';
    form.addEventListener('submit', function () {
      again.value = document.getElementById('new_password').value;
    });
  }

  var signOutLink = document.getElementById('fpc-signout');
  if (signOutLink) {
    signOutLink.addEventListener('click', function (e) {
      e.preventDefault();
      MM.api.post('/api/auth/logout')
        .catch(function () {})
        .finally(function () { window.location.href = '/login'; });
    });
  }
})();
