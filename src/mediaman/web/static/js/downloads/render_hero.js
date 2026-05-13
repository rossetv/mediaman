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

  /* ── Type pill (Movie / TV) — kept in sync because the hero can swap
        between a series and a movie item between polls. The server-rendered
        pill belongs to whichever item was hero at SSR time; once polling
        promotes a different item to hero we have to repaint this pill or
        the user sees a "TV" badge on a movie until they hard-refresh. ── */
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

  function buildEpisodeRow(ep) {
    var row = document.createElement('div');
    row.className = 'dl-ep-row' + (ep.is_pack_episode ? ' dl-ep-row-pack' : '');
    row.setAttribute('data-ep', ep.label);

    var num = document.createElement('span');
    num.className = 'dl-ep-number';
    num.textContent = ep.label;
    row.appendChild(num);

    var title = document.createElement('span');
    title.className = 'dl-ep-title';
    title.textContent = ep.title || '';
    row.appendChild(title);

    if (ep.is_pack_episode) {
      var packPill = document.createElement('span');
      packPill.className = 'dl-ep-status-pill downloading';
      packPill.textContent = 'In pack';
      row.appendChild(packPill);
      return row;
    }

    var miniBar = document.createElement('div');
    miniBar.className = 'dl-ep-mini-bar';
    var miniFill = document.createElement('div');
    miniFill.className = 'dl-ep-mini-fill' + (ep.state === 'ready' ? ' green' : '');
    miniFill.setAttribute('data-v', 'ep-fill');
    miniFill.style.width = (ep.progress || 0) + '%';
    miniBar.appendChild(miniFill);
    row.appendChild(miniBar);

    var statePill = document.createElement('span');
    statePill.className = 'dl-ep-status-pill ' + (ep.state || '');
    if (ep.state === 'ready') statePill.textContent = 'Ready';
    else if (ep.state === 'downloading') statePill.textContent = (ep.progress || 0) + '%';
    else if (ep.state === 'queued') statePill.textContent = 'Queued';
    else statePill.textContent = 'Searching';
    row.appendChild(statePill);

    return row;
  }

  function buildEpisodeToggle(count, hasPack) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'dl-hero-episodes-toggle';
    btn.setAttribute('data-v', 'ep-toggle');
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2.5');
    svg.setAttribute('stroke-linecap', 'round');
    var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    poly.setAttribute('points', '9 18 15 12 9 6');
    svg.appendChild(poly);
    btn.appendChild(svg);
    var label = document.createElement('span');
    label.textContent = count + ' episode' + (count !== 1 ? 's' : '') + (hasPack ? ' · pack download' : '');
    btn.appendChild(label);
    return btn;
  }

  /* ── Episode toggle + list — present only when the item is a series
        with episodes. Add the whole section when missing, remove it when
        the new hero is a movie, and rebuild it when the episode set has
        changed (different show, or pack contents changed). ── */
  function syncEpisodes(card, item) {
    var d = dom();
    var info = d.q('.dl-hero-info', card);
    var existingToggle = d.q('[data-v="ep-toggle"]', card);
    var existingList = d.q('[data-v="ep-list"]', card);
    var episodes = item.episodes;

    if (!episodes || !episodes.length || !info) {
      if (existingToggle && existingToggle.parentNode) existingToggle.parentNode.removeChild(existingToggle);
      if (existingList && existingList.parentNode) existingList.parentNode.removeChild(existingList);
      return;
    }

    var count = episodes.length;
    var toggleText = count + ' episode' + (count !== 1 ? 's' : '') + (item.has_pack ? ' · pack download' : '');
    if (!existingToggle) {
      existingToggle = buildEpisodeToggle(count, !!item.has_pack);
      info.appendChild(existingToggle);
    } else {
      var lbl = existingToggle.querySelector('span');
      if (lbl) lbl.textContent = toggleText;
    }

    if (!existingList) {
      existingList = document.createElement('div');
      existingList.className = 'dl-hero-episodes';
      existingList.setAttribute('data-v', 'ep-list');
      existingList.style.display = 'none';
      info.appendChild(existingList);
    }

    /* Rebuild the list when the set of episode labels differs from what
       we already rendered; otherwise patch each row in place to keep the
       toggle state and progress transitions smooth. */
    var existingRows = existingList.querySelectorAll('[data-ep]');
    var needsRebuild = existingRows.length !== count;
    if (!needsRebuild) {
      for (var i = 0; i < count; i++) {
        if (existingRows[i].getAttribute('data-ep') !== episodes[i].label) { needsRebuild = true; break; }
      }
    }
    if (needsRebuild) {
      while (existingList.firstChild) existingList.removeChild(existingList.firstChild);
      for (var bi = 0; bi < count; bi++) existingList.appendChild(buildEpisodeRow(episodes[bi]));
      return;
    }
    for (var ui = 0; ui < count; ui++) {
      var ep = episodes[ui];
      var epRow = d.findByEp(card, ep.label);
      if (!epRow) continue;
      var epFill = d.q('[data-v="ep-fill"]', epRow);
      if (epFill) {
        epFill.style.width = ep.progress + '%';
        epFill.className = 'dl-ep-mini-fill' + (ep.state === 'ready' ? ' green' : '');
      }
      var epPill = d.q('.dl-ep-status-pill', epRow);
      if (epPill && !ep.is_pack_episode) {
        epPill.className = 'dl-ep-status-pill ' + ep.state;
        if (ep.state === 'ready') d.setText(epPill, 'Ready');
        else if (ep.state === 'downloading') d.setText(epPill, ep.progress + '%');
        else if (ep.state === 'queued') d.setText(epPill, 'Queued');
        else d.setText(epPill, 'Searching');
      }
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
      pill.className = 'dl-state-pill dl-state-' + item.state;
      d.setText(pill, item.state_label || item.state);
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

    /* Episodes — create, refresh, or remove the toggle + list section. */
    syncEpisodes(card, item);

    /* Update data-dl-id if hero changed */
    if (card.getAttribute('data-dl-id') !== item.id) {
      card.setAttribute('data-dl-id', item.id);
    }
  }

  MM.downloads.renderHero = {
    updateHero: updateHero,
    updateSearchHint: updateSearchHint,
    updateTypePill: updateTypePill,
    syncEpisodes: syncEpisodes,
  };
})();
