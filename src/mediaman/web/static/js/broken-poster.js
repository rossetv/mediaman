/* eslint-disable no-var */
/*
 * Delegated handler that sets ``data-broken`` on a poster ``<img>`` that fails
 * to load.  The CSP drops ``'unsafe-inline'`` from ``script-src``, so an inline
 * ``onerror=`` attribute would not fire.
 *
 * Markup contract: any ``<img>`` that should opt into the
 * "data-broken when load fails" CSS treatment must carry a
 * ``data-broken-on-error`` attribute.  CSS keys off
 * ``img[data-broken]`` for the fallback styling, so the rule below
 * stamps the same ``dataset.broken=""`` flag the inline handler used
 * to set.
 */
(function () {
  'use strict';

  // ``error`` events do not bubble in the DOM tree the way other
  // events do, but the ``capture`` phase still works on ``document``,
  // so a single delegated listener at document scope catches every
  // image-load failure.
  document.addEventListener(
    'error',
    function (event) {
      var target = event.target;
      if (
        target &&
        target.tagName === 'IMG' &&
        target.hasAttribute('data-broken-on-error')
      ) {
        target.dataset.broken = '';
      }
    },
    true,
  );
})();
