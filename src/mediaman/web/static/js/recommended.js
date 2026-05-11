/**
 * recommended.js — page glue for /recommended.
 *
 * Owns the JSON-island parse, the tile-click / download-button delegation,
 * and the detail modal (openModal / closeModal / _modalDownload / share).
 *
 * The refresh button (with 24 h cooldown) lives in recommended/refresh.js;
 * the modal status polling lives in recommended/poll.js. All DOM building
 * uses safe APIs (createElement / textContent); we never assign to
 * element.innerHTML.
 *
 * Cross-module dependencies:
 *   MM.recommended.refresh — page-load refresh wiring
 *   MM.recommended.poll    — modal status polling
 *   window.ModalA11y       — focus-trap + escape-to-close behaviour
 *
 * Public surface lives on `window.MM.recommended` so the modal-h2 a11y
 * test can still assert on this file's contents.
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.recommended = MM.recommended || {};

  /* H66: parse from application/json script tag — textContent is safe because the
     browser never interprets the content as script. The assert below catches any
     server-side regression where un-escaped </script would appear in the payload. */
  var _recDataEl = document.getElementById('rec-data');
  if (_recDataEl) {
    var raw = _recDataEl.textContent;
    if (raw.toLowerCase().indexOf('</script') !== -1) {
      throw new Error('rec-data: </script breakout detected — server-side escaping regression');
    }
  }
  var _recData = _recDataEl ? JSON.parse(_recDataEl.textContent) : {};
  var _modalRecId = null;

  function _clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function _parseJSON(s) { try { return JSON.parse(s || '[]'); } catch (e) { return []; } }
  function _addRating(parent, text, klass) {
    var span = document.createElement('span');
    span.className = 'rating-pill ' + klass;
    span.textContent = text;
    parent.appendChild(span);
  }
  function _initials(name) {
    var parts = (name || '').split(' ');
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    return (name || '?')[0].toUpperCase();
  }

  function downloadRecommendation(btn, id) {
    btn.disabled = true;
    btn.textContent = 'Adding…';
    btn.style.opacity = '0.6';

    fetch('/api/recommended/' + id + '/download', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          btn.textContent = 'Queued ✓';
          btn.classList.remove('btn--primary');
          btn.classList.add('btn--success');
          btn.style.opacity = '1';
        } else {
          btn.textContent = data.error && data.error.length < 30 ? data.error : 'Failed';
          btn.classList.remove('btn--primary');
          btn.classList.add('btn--danger');
          btn.style.opacity = '1';
          btn.disabled = false;
          btn.title = data.error || '';
        }
      })
      .catch(function () { btn.textContent = 'Error'; btn.classList.add('btn--danger'); btn.style.opacity = '1'; btn.disabled = false; });
  }

  function _modalDownload(btn, s) {
    btn.textContent = 'Adding to ' + (s.media_type === 'movie' ? 'Radarr' : 'Sonarr') + '…';
    btn.className = 'btn-download adding';

    fetch('/api/recommended/' + s.id + '/download', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          btn.textContent = 'Queued';
          btn.className = 'btn-download queued';
          s.downloaded_at = new Date().toISOString();
          if (MM.recommended.poll) MM.recommended.poll.startModalPolling(s);
        } else {
          btn.textContent = data.error && data.error.length < 40 ? data.error : 'Failed';
          btn.className = 'btn-download';
          btn.style.background = 'rgba(255,69,58,0.12)';
          btn.style.color = 'var(--danger)';
          setTimeout(function () {
            btn.textContent = 'Download';
            btn.style.background = '';
            btn.style.color = '';
            btn.className = 'btn-download';
            btn.onclick = function () { _modalDownload(btn, s); };
          }, 3000);
        }
      })
      .catch(function () { btn.textContent = 'Error'; btn.className = 'btn-download'; });
  }

  function openModal(id) {
    var s = _recData[id];
    if (!s) return;
    _modalRecId = id;
    history.replaceState(null, '', '#recommendation-' + id);

    var heroEl = document.getElementById('modal-hero');
    _clear(heroEl);
    if (s.poster_url) {
      var heroImg = document.createElement('img');
      heroImg.src = s.poster_url.replace('/w300', '/w780').replace('/w200', '/w780');
      heroImg.alt = '';
      heroEl.appendChild(heroImg);
    }
    var gradient = document.createElement('div');
    gradient.className = 'detail-modal-hero-gradient';
    heroEl.appendChild(gradient);
    var heroInfo = document.createElement('div');
    heroInfo.className = 'detail-modal-hero-info';
    // Finding 11 (a11y): the visible title is the modal's h2; #detail-modal-title
    // matches aria-labelledby on the dialog wrapper so AT announces the real title.
    var heroTitle = document.createElement('h2');
    heroTitle.id = 'detail-modal-title';
    heroTitle.className = 'detail-modal-hero-title';
    heroTitle.textContent = s.title || '';
    if (s.year) {
      var yearSpan = document.createElement('span');
      yearSpan.className = 'year';
      yearSpan.textContent = ' (' + s.year + ')';
      heroTitle.appendChild(yearSpan);
    }
    heroInfo.appendChild(heroTitle);
    heroEl.appendChild(heroInfo);

    var quickEl = document.getElementById('modal-quick');
    _clear(quickEl);
    var typePill = document.createElement('span');
    typePill.className = 'type-pill';
    typePill.textContent = s.media_type === 'movie' ? 'MOVIE' : 'TV';
    quickEl.appendChild(typePill);
    var metaParts = [];
    if (s.runtime) metaParts.push(s.runtime + ' min');
    if (s.director) metaParts.push('Directed by ' + s.director);
    if (metaParts.length) {
      var metaSpan = document.createElement('span');
      metaSpan.className = 'meta-text';
      metaSpan.textContent = metaParts.join(' · ');
      quickEl.appendChild(metaSpan);
    }
    _parseJSON(s.genres).forEach(function (g) {
      var gSpan = document.createElement('span');
      gSpan.className = 'pill pill--neutral';
      gSpan.textContent = g;
      quickEl.appendChild(gSpan);
    });

    var ratingsEl = document.getElementById('modal-ratings');
    _clear(ratingsEl);
    if (s.rating) _addRating(ratingsEl, '★ ' + s.rating, 'r-tmdb');
    if (s.imdb_rating) _addRating(ratingsEl, 'IMDb ' + s.imdb_rating, 'r-imdb');
    if (s.rt_rating) _addRating(ratingsEl, '🍅 ' + s.rt_rating, 'r-rt');
    if (s.metascore) _addRating(ratingsEl, 'MC ' + s.metascore, 'r-meta');
    ratingsEl.style.display = ratingsEl.children.length ? '' : 'none';

    var taglineEl = document.getElementById('modal-tagline');
    taglineEl.textContent = s.tagline ? '"' + s.tagline + '"' : '';
    taglineEl.style.display = s.tagline ? '' : 'none';

    document.getElementById('modal-desc').textContent = s.description || '';

    var reasonEl = document.getElementById('modal-reason');
    reasonEl.textContent = s.reason || '';
    reasonEl.style.display = s.reason ? '' : 'none';

    _paintTrailer(s);
    _paintCast(s);

    document.getElementById('modal-progress').style.display = 'none';
    document.getElementById('modal-success').style.display = 'none';

    _paintActions(s);

    var dm = document.getElementById('detail-modal');
    dm.style.display = 'flex';
    dm.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    if (window.ModalA11y) window.ModalA11y.onOpened('detail-modal');
  }

  function _paintTrailer(s) {
    var trailerEl = document.getElementById('modal-trailer');
    var trailerLabel = document.getElementById('modal-trailer-label');
    var trailerFallback = document.getElementById('modal-trailer-fallback');
    _clear(trailerEl);
    _clear(trailerFallback);

    /* Finding 19: enforce exactly-11-char pattern before building iframe.
       Finding 18: only emit fallback link if URL is a YouTube HTTPS URL. */
    if (s.trailer_key && /^[A-Za-z0-9_-]{11}$/.test(s.trailer_key)) {
      var iframe = document.createElement('iframe');
      iframe.src = 'https://www.youtube.com/embed/' + s.trailer_key + '?rel=0';
      iframe.setAttribute('allowfullscreen', '');
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
      iframe.setAttribute('allow', 'autoplay; encrypted-media; fullscreen');
      iframe.setAttribute('referrerpolicy', 'strict-origin');
      iframe.setAttribute('loading', 'lazy');
      _clear(trailerEl);
      trailerEl.appendChild(iframe);
      trailerEl.style.display = '';
      trailerLabel.style.display = '';
      trailerFallback.style.display = 'none';
    } else if (s.trailer_url && s.trailer_url.startsWith('https://www.youtube.com/')) {
      trailerEl.style.display = 'none';
      trailerLabel.style.display = 'none';
      var link = document.createElement('a');
      link.href = s.trailer_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'trailer-link';
      link.textContent = '▶ Watch trailer on YouTube';
      _clear(trailerFallback);
      trailerFallback.appendChild(link);
      trailerFallback.style.display = '';
    } else {
      trailerEl.style.display = 'none';
      trailerLabel.style.display = 'none';
      trailerFallback.style.display = 'none';
    }
  }

  function _paintCast(s) {
    var castEl = document.getElementById('modal-cast');
    var castLabel = document.getElementById('modal-cast-label');
    _clear(castEl);
    var castData = _parseJSON(s.cast_json);
    castData.forEach(function (cm) {
      var div = document.createElement('div');
      div.className = 'cast-item';
      var avatar = document.createElement('div');
      avatar.className = 'cast-avatar';
      avatar.textContent = _initials(cm.name);
      var nameEl = document.createElement('div');
      nameEl.className = 'cast-name';
      nameEl.textContent = cm.name || '';
      var charEl = document.createElement('div');
      charEl.className = 'cast-char';
      charEl.textContent = cm.character || '';
      div.appendChild(avatar);
      div.appendChild(nameEl);
      div.appendChild(charEl);
      castEl.appendChild(div);
    });
    castEl.style.display = castData.length ? '' : 'none';
    castLabel.style.display = castData.length ? '' : 'none';
  }

  function _paintActions(s) {
    var actionsEl = document.getElementById('modal-actions');
    _clear(actionsEl);

    var pollMod = MM.recommended.poll;

    if (s.download_state === 'in_library') {
      var libBtn = document.createElement('button');
      libBtn.className = 'btn-download ready';
      libBtn.textContent = 'In Library ✓';
      actionsEl.appendChild(libBtn);
    } else if (s.download_state === 'downloading') {
      var dlBtn = document.createElement('button');
      dlBtn.className = 'btn-download adding';
      dlBtn.textContent = 'Downloading';
      actionsEl.appendChild(dlBtn);
      if (pollMod) pollMod.startModalPolling(s);
    } else if (s.download_state === 'queued' || s.downloaded_at) {
      var qBtn = document.createElement('button');
      qBtn.className = 'btn-download queued';
      qBtn.textContent = 'Queued';
      actionsEl.appendChild(qBtn);
      if (pollMod) pollMod.startModalPolling(s);
    } else {
      var dlBtn2 = document.createElement('button');
      dlBtn2.className = 'btn-download';
      dlBtn2.textContent = 'Download';
      dlBtn2.onclick = function () { _modalDownload(dlBtn2, s); };
      actionsEl.appendChild(dlBtn2);
    }

    // Share button — token is minted on demand (not pre-embedded) so page
    // viewers don't walk away with a warehouse of pre-authorised download links.
    var shareBtn = document.createElement('button');
    shareBtn.className = 'btn-share';
    shareBtn.textContent = 'Share';
    shareBtn.onclick = function () {
      shareBtn.disabled = true;
      shareBtn.textContent = 'Generating…';
      fetch('/api/recommended/' + s.id + '/share-token', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok && data.share_url) {
            navigator.clipboard.writeText(data.share_url).then(function () {
              shareBtn.textContent = 'Copied!';
              shareBtn.disabled = false;
              setTimeout(function () { shareBtn.textContent = 'Share'; }, 1500);
            }).catch(function () {
              // Clipboard denied — fall back to prompt
              window.prompt('Copy the share link:', data.share_url);
              shareBtn.textContent = 'Share';
              shareBtn.disabled = false;
            });
          } else {
            shareBtn.textContent = 'Failed';
            shareBtn.disabled = false;
            setTimeout(function () { shareBtn.textContent = 'Share'; }, 2000);
          }
        })
        .catch(function () {
          shareBtn.textContent = 'Error';
          shareBtn.disabled = false;
          setTimeout(function () { shareBtn.textContent = 'Share'; }, 2000);
        });
    };
    actionsEl.appendChild(shareBtn);
  }

  function closeModal() {
    var dm = document.getElementById('detail-modal');
    dm.style.display = 'none';
    dm.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    _clear(document.getElementById('modal-trailer'));
    if (MM.recommended.poll) MM.recommended.poll.stopModalPolling();
    _modalRecId = null;
    history.replaceState(null, '', window.location.pathname + window.location.search);
    if (window.ModalA11y) window.ModalA11y.onClosed('detail-modal');
  }
  if (window.ModalA11y) window.ModalA11y.register('detail-modal', closeModal);

  /* ── Event delegation for tile cards and download buttons ──
     Replaces inline onclick attributes on .tile and [data-download-rec]. */
  document.addEventListener('click', function (e) {
    /* Stop propagation for elements marked with data-stop-propagation */
    var stopEl = e.target.closest('[data-stop-propagation]');
    if (stopEl) { e.stopPropagation(); }

    /* Download button inside a tile */
    var dlBtn = e.target.closest('[data-download-rec]');
    if (dlBtn) {
      e.stopPropagation();
      downloadRecommendation(dlBtn, parseInt(dlBtn.dataset.downloadRec, 10));
      return;
    }

    /* Tile card itself */
    var tile = e.target.closest('[data-rec-id]');
    if (tile && !stopEl) {
      openModal(parseInt(tile.dataset.recId, 10));
    }
  });

  /* Bootstrap: kick off the refresh-button wiring and re-open the modal if
     the URL contained a #recommendation-N hash. */
  if (MM.recommended.refresh) MM.recommended.refresh.init();

  var hash = window.location.hash;
  if (hash && hash.indexOf('#recommendation-') === 0) {
    var id = parseInt(hash.replace('#recommendation-', ''), 10);
    if (id) setTimeout(function () { openModal(id); }, 200);
  }

  var detailModal = document.getElementById('detail-modal');
  if (detailModal) detailModal.addEventListener('click', function (e) {
    if (e.target === this) closeModal();
  });
})();
