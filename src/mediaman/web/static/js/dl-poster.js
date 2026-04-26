/**
 * dl-poster.js — safe background-image injection for download hero cards.
 *
 * Reads the URL from data-bg-url on .dl-hero-bg elements and applies it via
 * element.style.setProperty('background-image', 'url(' + JSON.stringify(url) + ')')
 * rather than server-side CSS string injection (H67).
 *
 * Also handles the JS-built hero card in download.html (buildHeroCard).
 */
(function () {
  'use strict';

  /**
   * Apply a poster URL to a single .dl-hero-bg element safely.
   * JSON.stringify quotes and escapes the URL, preventing CSS injection.
   *
   * @param {HTMLElement} bgEl  Element with class dl-hero-bg.
   */
  function applyPosterBg(bgEl) {
    var url = bgEl.getAttribute('data-bg-url');
    if (!url) return;
    /* Validate scheme before applying — only http / https are acceptable. */
    if (!/^https?:\/\//i.test(url)) return;
    bgEl.style.setProperty('background-image', 'url(' + JSON.stringify(url) + ')');
  }

  /**
   * Wire all .dl-hero-bg[data-bg-url] elements already in the DOM.
   */
  function wireAll() {
    var els = document.querySelectorAll('.dl-hero-bg[data-bg-url]');
    for (var i = 0; i < els.length; i++) applyPosterBg(els[i]);
  }

  /**
   * Expose for JS-built hero cards (download.html buildHeroCard).
   * Call window.DLPoster.apply(bgEl) after inserting the element.
   */
  window.DLPoster = { apply: applyPosterBg, wireAll: wireAll };

  /* Run immediately for server-rendered cards. */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireAll);
  } else {
    wireAll();
  }
}());
