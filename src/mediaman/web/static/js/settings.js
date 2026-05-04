/**
 * settings.js — settings page bootstrap.
 *
 * The four settings/<area>.js modules each expose
 * `MM.settings.<area>.init()`; this file ties them to DOMContentLoaded
 * so the page wires up after every defer'd module has executed.
 *
 * Lives outside the inline <script> the template used to carry because
 * page-level inline scripts have no CSP nonce — the browser refuses to
 * execute them under the strict script-src policy. See Wave 7 notes in
 * web/middleware/security_headers.py.
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    if (!window.MM || !MM.settings) return;
    if (MM.settings.general)      MM.settings.general.init();
    if (MM.settings.integrations) MM.settings.integrations.init();
    if (MM.settings.users)        MM.settings.users.init();
    if (MM.settings.newsletter)   MM.settings.newsletter.init();
  });
})();
