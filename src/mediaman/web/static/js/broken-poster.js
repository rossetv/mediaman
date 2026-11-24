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

  // An ``<img data-wide-art>`` fills a wide hero. One too square or too small
  // to do that is marked as if it had failed, so the hero shows its stand-in.
  // One that fits carries ``data-fit``: ``at-once`` when it had loaded before
  // this script ran, so a page that held it back until now need not fade it in.
  var MIN_ART_RATIO = 1.4;
  var MIN_ART_WIDTH = 700;

  function unfit(img) {
    return img.naturalWidth < MIN_ART_WIDTH || img.naturalWidth < MIN_ART_RATIO * img.naturalHeight;
  }

  // ``data-next-src`` names one more picture to try before the mark. The image
  // carries ``data-retrying`` until that one has loaded or failed too.
  function fail(img) {
    delete img.dataset.fit;
    var next = img.dataset.nextSrc;
    if (next) {
      delete img.dataset.nextSrc;
      img.dataset.retrying = '';
      img.src = next;
      return;
    }
    delete img.dataset.retrying;
    img.dataset.broken = '';
  }

  function judge(img, fit) {
    if (unfit(img)) {
      fail(img);
      return;
    }
    delete img.dataset.retrying;
    img.dataset.fit = fit;
  }

  document.addEventListener(
    'load',
    function (event) {
      var target = event.target;
      if (
        target &&
        target.tagName === 'IMG' &&
        target.hasAttribute('data-wide-art')
      ) {
        // A picture judged below can still have its load event to come.
        judge(target, target.dataset.fit || '');
      }
    },
    true,
  );

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
        fail(target);
      }
    },
    true,
  );

  // This script is deferred, so an image that failed or loaded before it ran
  // fired its event unseen; mark those now.
  var failed = document.querySelectorAll('img[data-broken-on-error]');
  for (var i = 0; i < failed.length; i++) {
    var img = failed[i];
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) {
      fail(img);
    }
  }
  var wide = document.querySelectorAll('img[data-wide-art]');
  for (var j = 0; j < wide.length; j++) {
    if (wide[j].complete && wide[j].naturalWidth) judge(wide[j], 'at-once');
  }
})();
