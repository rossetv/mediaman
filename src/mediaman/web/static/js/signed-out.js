/* The sign-in page: whoever signs in next finds nothing of what this tab remembers of the
 * person before (watch/ask.js). */
(function () {
  'use strict';

  try { window.sessionStorage.removeItem('mm.watch.ask'); } catch (e) { /* storage blocked */ }
})();
