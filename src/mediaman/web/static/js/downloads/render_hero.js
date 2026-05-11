/**
 * downloads/render_hero.js — patch the hero card from a poll payload.
 *
 * Reuses MM.downloads.buildDom for DOM helpers and `stateLabel`. The
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

  /* ── Refresh the "Searched N× · last attempt Xm ago · View in Radarr" line.
         Kept alongside the state pill so the user has visible proof that
         mediaman *is* poking Radarr/Sonarr on a cadence — absence of Activity
         in *arr does not mean nothing is happening. Only shown in the
         `searching` state; hidden otherwise so it doesn't linger after the
         item starts downloading. ── */
  function updateSearchHint(card, item) {
    var d = dom();
    var hint = d.q('[data-v="search-hint"]', card);
    if (!hint) return;
    if (item.state !== 'searching' || (!item.search_hint && !item.arr_link)) {
      hint.style.display = 'none';
      return;
    }
    hint.style.display = '';
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

  function updateHero(container, item) {
    if (!item || !container) return;
    var d = dom();
    var card = container.querySelector('.dl-hero');
    if (!card) return;

    /* Update poster backdrop */
    var bg = d.q('.dl-hero-bg', card);
    if (bg && item.poster_url) {
      /* H67: set via DLPoster.apply to avoid CSS string injection. */
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
      pill.className = 'dl-state-pill dl-state-' + item.state;
      d.setText(pill, d.stateLabel(item.state));
    }

    updateSearchHint(card, item);

    /* Progress wrap (hide entirely while searching) */
    var progressWrap = d.q('[data-v="progress-wrap"]', card);
    if (progressWrap) progressWrap.style.display = (item.state === 'searching') ? 'none' : '';

    /* Progress bar */
    var fill = d.q('[data-v="fill"]', card);
    if (fill) {
      fill.style.width = item.progress + '%';
      fill.className = 'dl-hero-fill' + (item.state === 'almost_ready' ? ' green' : '');
    }

    /* Progress details */
    var pct = d.q('[data-v="pct"]', card);
    if (pct) d.setText(pct, item.progress + '%');
    var sizeDone = d.q('[data-v="size-done"]', card);
    if (sizeDone) d.setText(sizeDone, item.size_done || '');
    var sizeTotal = d.q('[data-v="size-total"]', card);
    if (sizeTotal) d.setText(sizeTotal, item.size_total || '');
    var eta = d.q('[data-v="eta"]', card);
    if (eta) d.setText(eta, item.eta || '');

    /* Update episode rows if present */
    if (item.episodes) {
      for (var i = 0; i < item.episodes.length; i++) {
        var ep = item.episodes[i];
        var epRow = d.findByEp(card, ep.label);
        if (!epRow) continue;
        var epFill = d.q('[data-v="ep-fill"]', epRow);
        if (epFill) {
          epFill.style.width = ep.progress + '%';
          epFill.className = 'dl-ep-mini-fill' + (ep.state === 'ready' ? ' green' : '');
        }
        var epPill = d.q('.dl-ep-status-pill', epRow);
        if (epPill) {
          epPill.className = 'dl-ep-status-pill ' + ep.state;
          if (ep.state === 'ready') d.setText(epPill, 'Ready');
          else if (ep.state === 'downloading') d.setText(epPill, ep.progress + '%');
          else if (ep.state === 'queued') d.setText(epPill, 'Queued');
          else d.setText(epPill, 'Searching');
        }
      }
    }

    /* Update data-dl-id if hero changed */
    if (card.getAttribute('data-dl-id') !== item.id) {
      card.setAttribute('data-dl-id', item.id);
    }
  }

  MM.downloads.renderHero = {
    updateHero: updateHero,
    updateSearchHint: updateSearchHint,
  };
})();
