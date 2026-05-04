/**
 * core/tiles.js — single poster tile renderer for mediaman grids.
 *
 * Public surface (under the global MM.tiles namespace):
 *
 *   MM.tiles.render(container, items, options?)
 *
 *   Empties container, then appends one `.poster-card` element per item.
 *   This matches the DOM shape produced by search.js `renderCard()` and
 *   the `_rec_card.html` server template (search page variant).
 *
 *   items — array of objects; recognised fields:
 *     tmdb_id        (string|number) — drives data-id on the card
 *     media_type     (string)        — 'movie' | 'tv'; shown in subtitle
 *     title          (string)        — card title and alt text
 *     poster_url     (string)        — poster image src; empty → placeholder
 *     year           (string|number) — shown in subtitle
 *     rating         (string)        — TMDB/IMDb star rating chip
 *     rt_rating      (string)        — Rotten Tomatoes chip
 *     download_state (string)        — state badge on the poster ('in_library',
 *                                      'downloading', 'queued', …)
 *
 *   options:
 *     onClick(item, cardEl) — called when a card is clicked.
 *     kind (string)         — reserved for future variant support; ignored now.
 *
 * All DOM construction uses createElement/textContent — no innerHTML.
 * Safe by default even if item fields contain untrusted strings.
 *
 * No external dependencies. Load after MM namespace is available
 * (i.e. after api.js or dom.js, which both bootstrap window.MM).
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  /**
   * Build rating chip elements and append them to chipsEl.
   * Returns chipsEl (or null if no chips were added).
   */
  function _buildChips(item) {
    var hasRating = item.rating || item.rt_rating;
    if (!hasRating) return null;

    var chips = document.createElement('div');
    chips.className = 'poster-chips';

    if (item.rating) {
      var imdb = document.createElement('span');
      imdb.className = 'poster-chip r-imdb';
      var star = document.createElement('span');
      star.className = 'chip-icon';
      star.textContent = '★';
      imdb.appendChild(star);
      imdb.appendChild(document.createTextNode(String(item.rating)));
      chips.appendChild(imdb);
    }
    if (item.rt_rating) {
      var rt = document.createElement('span');
      rt.className = 'poster-chip r-rt';
      var tomato = document.createElement('span');
      tomato.className = 'chip-icon';
      tomato.textContent = '🍅';
      rt.appendChild(tomato);
      rt.appendChild(document.createTextNode(String(item.rt_rating)));
      chips.appendChild(rt);
    }
    return chips;
  }

  /**
   * Build and return a single `.poster-card` element for the given item.
   * onClick(item, cardEl) is wired if provided.
   */
  function _buildCard(item, onClick) {
    var card = document.createElement('div');
    card.className = 'poster-card';
    card.dataset.type = String(item.media_type || '');
    card.dataset.id   = String(item.tmdb_id || '');

    /* ── Poster ── */
    var posterWrap = document.createElement('div');
    posterWrap.className = 'poster';

    if (item.poster_url) {
      var img = document.createElement('img');
      img.src = item.poster_url;
      img.alt = item.title || '';
      img.loading = 'lazy';
      img.setAttribute('referrerpolicy', 'no-referrer');
      posterWrap.appendChild(img);
    } else {
      var empty = document.createElement('div');
      empty.className = 'poster-empty';
      empty.textContent = '—';
      posterWrap.appendChild(empty);
    }

    var chips = _buildChips(item);
    if (chips) posterWrap.appendChild(chips);

    if (item.download_state) {
      var badge = document.createElement('span');
      badge.className = 'poster-state ' + item.download_state;
      badge.textContent = item.download_state.replace(/_/g, ' ');
      posterWrap.appendChild(badge);
    }
    card.appendChild(posterWrap);

    /* ── Body: title + meta ── */
    var body = document.createElement('div');
    body.className = 'poster-body';

    var titleEl = document.createElement('div');
    titleEl.className = 'card-title';
    titleEl.textContent = item.title || '';
    body.appendChild(titleEl);

    var metaParts = [];
    if (item.year) metaParts.push(String(item.year));
    metaParts.push(item.media_type === 'movie' ? 'Movie' : 'TV');
    var meta = document.createElement('div');
    meta.className = 'card-meta';
    meta.textContent = metaParts.join(' · ');
    body.appendChild(meta);

    card.appendChild(body);

    /* ── Click handler ── */
    if (typeof onClick === 'function') {
      card.addEventListener('click', function () { onClick(item, card); });
    }

    return card;
  }

  /**
   * Render a list of items into container as poster cards.
   *
   * @param {HTMLElement} container
   * @param {Array}       items
   * @param {object}      [options]   { onClick, kind }
   */
  function render(container, items, options) {
    if (!container) return;
    options = options || {};
    container.replaceChildren
      ? container.replaceChildren()
      : (function () { while (container.firstChild) container.removeChild(container.firstChild); })();

    (items || []).forEach(function (item) {
      container.appendChild(_buildCard(item, options.onClick));
    });
  }

  global.MM.tiles = {
    render: render,
  };

})(window);
