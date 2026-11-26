/**
 * downloads/render_hero.js — patch the hero card from a poll payload.
 *
 * Reuses MM.downloads.buildDom for DOM helpers. The
 * caller (downloads.js) owns container lookup; this module only touches
 * the hero card in place.
 *
 * Cross-module dependencies:
 *   MM.downloads.buildDom
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
         item starts downloading. Of one that needs attention it says instead what
         Radarr or Sonarr says is wrong and where to fix it, which only an admin is sent; or,
         to anyone, that mediaman will look for another copy itself. */
  function updateSearchHint(card, item) {
    var d = dom();
    var hint = d.q('[data-v="search-hint"]', card);
    if (!hint) return;
    var held = item.state === 'needs_attention';
    /* The link into Radarr or Sonarr is an admin's alone; for anyone else it is never drawn. */
    var link = !held && d.canAbandonHere() ? item.arr_link : '';
    var words = (held ? item.attention : item.search_hint) || '';
    var fix = held ? (item.retrying ? 'mediaman will look for another copy' : item.attention_hint) || '' : '';
    if ((item.state !== 'searching' && !held) || (!words && !link && !fix)) {
      d.passFocus(hint);
      hint.hidden = true;
      return;
    }
    hint.hidden = false;
    /* The parts are kept from poll to poll, so a link with the focus keeps it. */
    var text = d.q('[data-v="search-hint-text"]', hint);
    var sep = d.q('[data-v="search-hint-sep"]', hint);
    var a = d.q('[data-v="arr-link"]', hint);
    if (!text) {
      text = document.createElement('span');
      text.setAttribute('data-v', 'search-hint-text');
      hint.appendChild(text);
    }
    if (link && !a) {
      sep = document.createElement('span');
      sep.className = 'dl-hint-sep';
      sep.setAttribute('data-v', 'search-hint-sep');
      sep.textContent = ' · ';
      a = document.createElement('a');
      a.className = 'dl-arr-link';
      a.setAttribute('data-v', 'arr-link');
      a.target = '_blank';
      a.rel = 'noopener';
      hint.appendChild(sep);
      hint.appendChild(a);
    }
    d.setText(text, words);
    text.hidden = !words;
    var fixed = d.q('[data-v="attention-hint"]', hint);
    if (fix && !fixed) {
      fixed = document.createElement('span');
      fixed.className = 'dl-attention-hint';
      fixed.setAttribute('data-v', 'attention-hint');
      hint.appendChild(fixed);
    }
    if (fixed) {
      d.setText(fixed, fix);
      fixed.hidden = !fix;
    }
    if (sep) sep.hidden = !words || !link;
    if (a) {
      if (!link) d.passFocus(a);
      a.hidden = !link;
      if (link) {
        a.href = link;
        d.setText(a, 'View in ' + (item.arr_source || 'Radarr/Sonarr') + ' ↗');
      }
    }
  }

  function updateHero(container, item) {
    if (!item || !container) return;
    var d = dom();
    var card = container.querySelector('.dl-hero');
    if (!card) return;
    var ease = null;
    if (card.getAttribute('data-dl-id') !== item.id) {
      ease = d.easeHeight(card);
      /* The controls become another title's, which nobody on one of them asked for. */
      d.passFocus(card);
      d.fadeOver(card);
      d.closeEpisodes(card);
      card.setAttribute('data-dl-id', item.id);
    }

    d.syncArt(card, item);

    /* The hero can go from a film to a show between polls. */
    d.syncTypePill(d.q('.dl-hero-info', card), item.media_type);

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
    if (progressWrap) progressWrap.hidden = (item.state === 'searching' || item.state === 'needs_attention' || item.state === 'finding_copy');

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
    if (ease) ease();
  }

  MM.downloads.renderHero = {
    updateHero: updateHero,
    updateSearchHint: updateSearchHint,
  };
})();
