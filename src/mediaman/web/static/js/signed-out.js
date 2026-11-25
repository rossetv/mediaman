/* The sign-in page: whoever signs in next finds nothing of what this tab, or this browser, remembers
 * of the person before (watch/ask.js, watch/cast-sender.js). */
(function () {
  'use strict';

  try { window.sessionStorage.removeItem('mm.watch.ask'); } catch (e) { /* storage blocked */ }
  try {
    /* Opened by somebody who is signed in, it is the way to nobody else yet. */
    if (window.document.getElementById('tv-remote')) return;
    window.localStorage.removeItem('mediaman.watch.cast');
    window.localStorage.removeItem('mediaman.watch.cast.kept');
  } catch (e) { /* storage blocked */ }
})();
