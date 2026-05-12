/**
 * search.js — thin bootstrap for the /search page.
 *
 * Wires the two sub-modules together after they have each registered on
 * window.MM.search:
 *   - search/shelves.js     — shelf rendering, search-result lists, fetch loop
 *   - search/detail_modal.js — detail modal open/close and content rendering
 *
 * Load order in search.html:
 *   1. search/shelves.js
 *   2. search/detail_modal.js
 *   3. search.js  (this file)
 *
 * The only cross-module dependency to resolve at boot time is the openDetail
 * callback (shelves → detail) and the refreshCurrentView callback
 * (detail → shelves).
 */
(() => {
  'use strict';

  const { shelves, detail } = MM.search;

  /* Wire cross-module callbacks, then start each module. */
  shelves.init({ openDetail: detail.openDetail });
  detail.init({ refreshCurrentView: shelves.refreshCurrentView });
})();
