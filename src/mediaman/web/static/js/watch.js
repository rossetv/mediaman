/* Watch browse pages: starts the modules under watch/ and wires the Plex
 * "Try again" button. Loaded after them; the unavailable page loads only this.
 */
(function () {
  'use strict';

  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-watch-retry]')) window.location.reload();
  });

  var watch = window.MMWatch;
  if (!watch || !watch.core) return;
  if (watch.search) watch.search.init();
  if (watch.seasons) watch.seasons.init();
})();
