/**
 * Forced password change page: the way out. The form is password-form.js's.
 *
 * Requires: core/api.js (MM.api).
 */
(function () {
  'use strict';

  var signOutLink = document.getElementById('fpc-signout');
  if (!signOutLink) return;
  signOutLink.addEventListener('click', function (e) {
    e.preventDefault();
    MM.api.post('/api/auth/logout')
      .catch(function () {})
      .finally(function () { window.location.href = '/login'; });
  });
})();
