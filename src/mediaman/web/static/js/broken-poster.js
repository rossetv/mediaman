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

  // This route fails where it is asked too much at once or its source is slow,
  // so the same address may well answer a little later. What it refuses it goes on
  // refusing for a minute, so the last try comes after that.
  var OWN_ROUTE = '/api/watch/img/';
  var RETRY_MS = [3000, 65000];
  // Pictures that failed together are asked for again a few at a time.
  var ABREAST = 4;
  var APART_MS = 500;
  var queue = [];
  var asking = 0;
  var tried = new WeakMap(); // image -> { src, times, pending }
  // The mark of a picture that came back, while its stand-in still shows.
  var COMING = 'coming';

  function failedToLoad(img) {
    var src = img.getAttribute('src') || '';
    var again = !img.dataset.nextSrc && src.indexOf(OWN_ROUTE) === 0;
    fail(img);
    if (!again) { tried.delete(img); return; }
    var state = tried.get(img);
    if (!state || state.src !== src) {
      state = { src: src, times: 0, pending: false };
      tried.set(img, state);
    }
    if (!state.pending) retry(img, state);
  }

  /* Asks for it behind the stand-in, and puts it in where it came and is still wanted. */
  function retry(img, state) {
    if (state.times >= RETRY_MS.length) return;
    state.pending = true;
    setTimeout(function () {
      queue.push([img, state]);
      askNext();
    }, RETRY_MS[state.times]);
  }

  // One the person can see goes before those further on.
  function askNext() {
    while (asking < ABREAST && queue.length) {
      var at = queue.findIndex(function (entry) { return inView(entry[0]); });
      ask.apply(null, queue.splice(Math.max(at, 0), 1)[0]);
    }
  }

  // A marked picture is not drawn, so where it stands is where its stand-in is.
  function inView(img) {
    var box = img.parentNode && img.parentNode.getBoundingClientRect ? img.parentNode.getBoundingClientRect() : null;
    return !!box && box.bottom > 0 && box.right > 0 && box.top < innerHeight && box.left < innerWidth;
  }

  function wanted(img, state) {
    if (tried.get(img) !== state) return false;
    if (img.dataset.broken === '' && img.isConnected && !img.hidden && img.getAttribute('src') === state.src) return true;
    tried.delete(img);
    return false;
  }

  function ask(img, state) {
    if (!wanted(img, state)) return;
    state.times += 1;
    asking += 1;
    var probe = new Image();
    function answered() {
      state.pending = false;
      setTimeout(function () { asking -= 1; askNext(); }, APART_MS);
    }
    probe.onload = function () {
      answered();
      if (!wanted(img, state)) return;
      img.dataset.broken = COMING;
      img.decoding = 'sync'; // or it can be drawn empty for a frame
      // Safari keeps the failure for an address set where it already stands.
      img.removeAttribute('src');
      img.src = state.src;
    };
    probe.onerror = function () {
      answered();
      retry(img, state);
    };
    probe.src = state.src;
  }

  function settle(img) {
    if (img.dataset.broken === COMING) delete img.dataset.broken;
  }

  /* Fades the picture in over its stand-in, or a banner's stand-ins, which lie over it, out. */
  function fadeIn(img) {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
      // Once drawn whole, so no transition of its own can start from nothing as its stand-in goes.
      requestAnimationFrame(function () { settle(img); });
      return;
    }
    var style = getComputedStyle(document.documentElement);
    var over = img.hasAttribute('data-wide-art')
      ? [].slice.call(img.parentNode.querySelectorAll('.watch-banner__bg--fill:not([data-broken]), .watch-banner__poster:not([data-broken])'))
      : [];
    var frames = over.length ? [{}, { opacity: 0 }] : [{ opacity: 0 }, {}];
    (over.length ? over : [img]).forEach(function (part) {
      part.animate(frames, {
        duration: parseFloat(style.getPropertyValue('--dur')) * 1000 || 200,
        easing: style.getPropertyValue('--ease-out').trim() || 'ease-out',
      }).onfinish = function () { settle(img); };
    });
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
      if (!target || target.tagName !== 'IMG') return;
      tried.delete(target);
      if (target.hasAttribute('data-wide-art')) {
        // A picture judged below can still have its load event to come.
        judge(target, target.dataset.fit || '');
      }
      if (target.dataset.broken === COMING) fadeIn(target);
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
        failedToLoad(target);
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
      failedToLoad(img);
    }
  }
  var wide = document.querySelectorAll('img[data-wide-art]');
  for (var j = 0; j < wide.length; j++) {
    if (wide[j].complete && wide[j].naturalWidth) judge(wide[j], 'at-once');
  }
})();
