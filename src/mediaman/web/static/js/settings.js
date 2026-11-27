/**
 * settings.js — Settings bootstrap.
 *
 * The settings/<area>.js modules each expose `MM.settings.<area>.init()`; this file starts
 * them once the Settings markup is in the page: at once when the pane loads its scripts after
 * inserting the fragment, or on DOMContentLoaded. The root is marked `data-mounted`, so a
 * second load starts nothing twice. It also tells the pane when leaving would lose edits.
 *
 * Lives outside an inline <script> because
 * page-level inline scripts have no CSP nonce — the browser refuses to
 * execute them under the strict script-src policy.
 * See web/middleware/security_headers.py.
 */
(function () {
  'use strict';

  function start() {
    if (!window.MM || !MM.settings) return;
    var root = document.querySelector('[data-you-group="settings"]') || document.querySelector('.setg-pg');
    if (!root || root.hasAttribute('data-mounted')) return;
    root.setAttribute('data-mounted', '');
    if (MM.settings.general)      MM.settings.general.init();
    if (MM.settings.integrations) MM.settings.integrations.init();
    if (MM.settings.users)        MM.settings.users.init();
    if (MM.settings.tvs)          MM.settings.tvs.init();
    if (MM.settings.newsletter)   MM.settings.newsletter.init();
    if (MM.settings.sections)     MM.settings.sections.init();
    if (MM.you && MM.you.guard) {
      MM.you.guard('settings', {
        // A step to another section keeps the edits; only leaving Settings would lose them.
        dirty: function (from, to) {
          return MM.settings.savebar.isDirty() && String(to || '').split('/')[0] !== 'settings';
        },
        discard: function () { MM.settings.savebar.discardNow(); },
        title: 'Discard unsaved changes?',
        body: 'Every setting goes back to how it was last saved.',
      });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
