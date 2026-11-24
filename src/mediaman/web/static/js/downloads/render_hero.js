/**
 * downloads/render_hero.js — patch the hero card from a poll payload.
 *
 * Reuses MM.downloads.buildDom for DOM helpers. The
 * caller (downloads.js) owns container lookup; this module only touches
 * the hero card in place.
 *
 * Cross-module dependencies:
 *   MM.downloads.buildDom
 *   window.DLPoster   (optional — used to apply background image safely)
 *
 * Exposes:
 *   MM.downloads.renderHero.updateHero(container, item)
 *   MM.downloads.renderHero.updateSearchHint(card, item)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  var dom = function () { return MM.downloads.buildDom; };

  /* Refresh the "Searched N× · last attempt Xm ago · View in Radarr" line.
         Kept alongside the state pill so the user has visible proof that
         mediaman *is* poking Radarr/Sonarr on a cadence — absence of Activity
         in *arr does not mean nothing is happening. Only shown in the
         `searching` state; hidden otherwise so it doesn't linger after the
         item starts downloading. */
  function updateSearchHint(card, item) {
    var d = dom();
    var hint = d.q('[data-v="search-hint"]', card);
    if (!hint) return;
    if (item.state !== 'searching' || (!item.search_hint && !item.arr_link)) {
      hint.hidden = true;
      return;
    }
    hint.hidden = false;
    while (hint.firstChild) hint.removeChild(hint.firstChild);
    if (item.search_hint) {
      var text = document.createElement('span');
      text.setAttribute('data-v', 'search-hint-text');
      text.textContent = item.search_hint;
      hint.appendChild(text);
    }
    if (item.arr_link) {
      if (item.search_hint) hint.appendChild(document.createTextNode(' · '));
      var a = document.createElement('a');
      a.className = 'dl-arr-link';
      a.setAttribute('data-v', 'arr-link');
      a.href = item.arr_link;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = 'View in ' + (item.arr_source || 'Radarr/Sonarr') + ' ↗';
      hint.appendChild(a);
    }
  }

  /* The type pill is kept in sync because the hero can swap
        between a series and a movie item between polls. The server-rendered
        pill belongs to whichever item was hero at SSR time; once polling
        promotes a different item to hero we have to repaint this pill or
        the user sees a "TV" badge on a movie until they hard-refresh. */
  function updateTypePill(card, mediaType) {
    var d = dom();
    var info = d.q('.dl-hero-info', card);
    if (!info) return;
    var pillRow = info.querySelector('.pill-row');
    if (!pillRow) {
      pillRow = document.createElement('div');
      pillRow.className = 'pill-row u-mb-12';
      info.insertBefore(pillRow, info.firstChild);
    }
    var isMovie = mediaType === 'movie';
    var pill = pillRow.querySelector('.pill');
    if (!pill) {
      pill = document.createElement('span');
      pillRow.appendChild(pill);
    }
    pill.className = 'pill pill--' + (isMovie ? 'movie' : 'tv');
    d.setText(pill, isMovie ? 'Movie' : 'TV');
  }

  function updateHero(container, item) {
    if (!item || !container) return;
    var d = dom();
    var card = container.querySelector('.dl-hero');
    if (!card) return;
    d.unmarkHeld(card);

    /* Update poster backdrop */
    var bg = d.q('.dl-hero-bg', card);
    if (bg && item.poster_url) {
      /* Set via DLPoster.apply to avoid CSS string injection. */
      bg.setAttribute('data-bg-url', item.poster_url);
      if (window.DLPoster) window.DLPoster.apply(bg);
    }

    /* Update poster img */
    var posterDiv = d.q('.dl-hero-poster', card);
    if (posterDiv && item.poster_url) {
      var img = posterDiv.querySelector('img');
      if (img) { if (img.src !== item.poster_url) img.src = item.poster_url; }
      else {
        var ph = posterDiv.querySelector('.dl-hero-poster-placeholder');
        if (ph) posterDiv.removeChild(ph);
        img = document.createElement('img');
        img.src = item.poster_url;
        img.alt = '';
        posterDiv.appendChild(img);
      }
    }

    /* Type pill before the title — must be kept in sync per updateTypePill's note. */
    updateTypePill(card, item.media_type);

    /* Title */
    d.setText(d.q('.dl-hero-title', card), item.title);

    /* Episode summary */
    var epSum = d.q('.dl-hero-ep-summary', card);
    if (item.episode_summary) {
      if (!epSum) {
        epSum = document.createElement('div');
        epSum.className = 'dl-hero-ep-summary';
        var info = d.q('.dl-hero-info', card);
        var status = d.q('.dl-hero-status', info);
        if (info && status) info.insertBefore(epSum, status);
      }
      d.setText(epSum, item.episode_summary);
    } else if (epSum) {
      epSum.parentNode.removeChild(epSum);
    }

    /* State pill */
    var pill = d.q('.dl-state-pill', card);
    if (pill) {
      pill.className = pill.className.replace(/\bdl-state-(?!pill)\S*/g, '').trim() + ' dl-state-' + item.state;
      d.setText(pill, item.state_label || item.state);
    }

    updateSearchHint(card, item);

    /* Progress wrap (hide entirely while searching) */
    var progressWrap = d.q('[data-v="progress-wrap"]', card);
    if (progressWrap) progressWrap.hidden = (item.state === 'searching');

    /* Progress bar */
    var fill = d.q('[data-v="fill"]', card);
    if (fill) {
      d.setProgress(fill, item.progress);
      fill.parentNode.setAttribute('aria-label', item.title);
      fill.className = 'dl-hero-fill' + (item.state === 'almost_ready' ? ' green' : '');
    }

    /* Progress details */
    var pct = d.q('[data-v="pct"]', card);
    if (pct) d.setText(pct, item.progress + '%');
    var sizeDone = d.q('[data-v="size-done"]', card);
    if (sizeDone) d.setText(sizeDone, item.size_done || '');
    var sizeTotal = d.q('[data-v="size-total"]', card);
    if (sizeTotal) d.setText(sizeTotal, item.size_total || '');
    d.updateEtaStat(card, item.eta);

    /* Episodes — create, refresh, or remove the toggle + list section. */
    d.syncEpisodes(card, item);

    d.syncAbandon(card, item, 'hero');

    /* Update data-dl-id if hero changed */
    if (card.getAttribute('data-dl-id') !== item.id) {
      card.setAttribute('data-dl-id', item.id);
    }
  }

  MM.downloads.renderHero = {
    updateHero: updateHero,
    updateSearchHint: updateSearchHint,
    updateTypePill: updateTypePill,
  };
})();
