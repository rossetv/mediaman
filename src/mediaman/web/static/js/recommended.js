/**
 * recommended.js — page glue for /recommended.
 *
 * Extracted from the inline <script> in recommended.html so the page
 * can drop the CSP `'unsafe-inline'` permission once every other
 * template follows suit. The accompanying JSON island
 * (<script id="rec-data" type="application/json">…</script>) stays in
 * the template — it is non-executable (typed application/json) so it
 * does not need a CSP nonce.
 *
 * Responsibilities:
 *  - manual-refresh button + 24h cooldown countdown
 *  - active-refresh status polling on page load
 *  - tile-click and download-button delegation
 *  - detail modal population from the rec-data JSON island
 *  - per-tile download / share-token / status polling
 *
 * All DOM building uses safe APIs (createElement / textContent); we
 * never assign to element.innerHTML. The file is self-contained — no
 * imports — so it can be served as a static asset.
 */
(function () {
  'use strict';

  function _formatCountdown(ms) {
    if (ms <= 0) return '0m';
    var totalMin = Math.ceil(ms / 60000);
    if (totalMin >= 60) {
      var h = Math.floor(totalMin / 60);
      var m = totalMin % 60;
      return m > 0 ? h + 'h ' + m + 'm' : h + 'h';
    }
    return totalMin + 'm';
  }

  function _swapButtonForCooldown(nextAt) {
    var btn = document.getElementById('refresh-btn');
    if (!btn) return;
    var span = document.createElement('span');
    span.id = 'refresh-cooldown';
    span.className = 'sub';
    span.dataset.nextAt = nextAt;
    span.appendChild(document.createTextNode('Next refresh available in '));
    var clock = document.createElement('span');
    clock.dataset.cooldownClock = '';
    clock.textContent = '24h';
    span.appendChild(clock);
    btn.replaceWith(span);
    _tickCooldownClock();
  }

  var _cooldownInterval = null;
  function _tickCooldownClock() {
    var span = document.getElementById('refresh-cooldown');
    var clock = span && span.querySelector('[data-cooldown-clock]');
    if (!clock || !span.dataset.nextAt) return;
    var update = function () {
      var ms = new Date(span.dataset.nextAt).getTime() - Date.now();
      if (ms <= 0) {
        // Cooldown expired — keep the line hidden and let the next page
        // load render the button.
        span.textContent = 'Refresh available — reload to fetch new suggestions';
        if (_cooldownInterval) { clearInterval(_cooldownInterval); _cooldownInterval = null; }
        return;
      }
      clock.textContent = _formatCountdown(ms);
    };
    update();
    if (_cooldownInterval) clearInterval(_cooldownInterval);
    _cooldownInterval = setInterval(update, 30000);
  }

  function _startRefreshPolling(btn) {
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    btn.style.opacity = '0.6';

    var poll = setInterval(function () {
      MM.api.get('/api/recommended/refresh/status')
        .then(function (st) {
          if (st.status === 'done') {
            clearInterval(poll);
            if (st.result && st.result.ok) {
              btn.textContent = 'Done ✓';
              btn.classList.add('is-success');
              setTimeout(function () { window.location.reload(); }, 800);
            } else {
              btn.textContent = 'Fetch new suggestions';
              btn.style.opacity = '1';
              btn.disabled = false;
              var err = (st.result && st.result.error) || "Couldn't refresh recommendations.";
              var errEl = document.getElementById('refresh-error');
              errEl.textContent = err;
              errEl.style.display = 'block';
            }
          }
        })
        .catch(function () {});
    }, 3000);
  }

  function refreshRecommendations(btn) {
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    btn.style.opacity = '0.6';
    document.getElementById('refresh-error').style.display = 'none';

    MM.api.post('/api/recommended/refresh')
      .then(function (data) {
        if (data.status === 'started' || data.status === 'already_running') {
          _startRefreshPolling(btn);
        }
      })
      .catch(function (err) {
        btn.textContent = 'Fetch new suggestions';
        btn.style.opacity = '1';
        btn.disabled = false;

        /* Distinguish a structured APIError (server replied, just unhappy)
           from a bare network failure (no .status field). The network case
           stays silent — matches the pre-MM.api behaviour. */
        if (!(err instanceof MM.api.APIError)) return;

        var data = (err && err.data) || {};
        if (err.status === 429) {
          /* Server-side cooldown — read structured fields (next_available_at,
             cooldown_seconds) from err.data and start the countdown. */
          var nextAt = data.next_available_at ||
            new Date(Date.now() + (data.cooldown_seconds || 0) * 1000).toISOString();
          _swapButtonForCooldown(nextAt);
        }
        var errEl = document.getElementById('refresh-error');
        errEl.textContent = err.message || "Couldn't refresh recommendations.";
        errEl.style.display = 'block';
      });
  }

  /* Wire the refresh button via data-action instead of onclick. */
  (function () {
    var refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', function () { refreshRecommendations(refreshBtn); });
    }
  })();

  // On page load: if a refresh is already running, attach to it. If the
  // server reports an active cooldown but the button is somehow visible
  // (race during navigation), swap it for the countdown.
  (function () {
    MM.api.get('/api/recommended/refresh/status')
      .then(function (st) {
        var btn = document.getElementById('refresh-btn');
        if (st.status === 'running' && btn) { _startRefreshPolling(btn); return; }
        if (st.manual_refresh_available === false && btn && st.next_available_at) {
          _swapButtonForCooldown(st.next_available_at);
        } else {
          _tickCooldownClock();
        }
      })
      .catch(function () {});
  })();

  function downloadRecommendation(btn, id) {
    btn.disabled = true;
    btn.textContent = 'Adding…';
    btn.style.opacity = '0.6';

    MM.api.post('/api/recommended/' + id + '/download')
      .then(function () {
        btn.textContent = 'Queued ✓';
        btn.classList.remove('btn--primary');
        btn.classList.add('btn--success');
        btn.style.opacity = '1';
      })
      .catch(function (err) {
        if (err instanceof MM.api.APIError) {
          var msg = err.message || 'Failed';
          btn.textContent = msg.length < 30 ? msg : 'Failed';
          btn.title = msg;
          btn.classList.remove('btn--primary');
          btn.classList.add('btn--danger');
          btn.style.opacity = '1';
          btn.disabled = false;
        } else {
          btn.textContent = 'Error';
          btn.classList.add('btn--danger');
          btn.style.opacity = '1';
          btn.disabled = false;
        }
      });
  }

  /* H66: parse from application/json script tag — textContent is safe because the
     browser never interprets the content as script. The assert below catches any
     server-side regression where un-escaped </script would appear in the payload. */
  (function () {
    var raw = document.getElementById('rec-data').textContent;
    if (raw.toLowerCase().indexOf('</script') !== -1) {
      throw new Error('rec-data: </script breakout detected — server-side escaping regression');
    }
  })();
  var _recData = JSON.parse(document.getElementById('rec-data').textContent);
  var _modalRecId = null;
  var _modalPollInterval = null;

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

  function _parseJSON(s) { try { return JSON.parse(s || '[]'); } catch (e) { return []; } }

  /* ── Detail-modal lifecycle (MM.modal.setupDetail).
       The `onClose` hook handles per-page state: clearing the trailer
       iframe, cancelling the in-flight progress poll, and stripping the
       deep-link hash from the URL. setupDetail itself owns the
       display:flex/none + aria-hidden + body overflow + ModalA11y dance. */
  var _detailModalEl = document.getElementById('detail-modal');
  var _detailModal = _detailModalEl ? MM.modal.setupDetail(_detailModalEl, {
    onClose: function () {
      var trailer = document.getElementById('modal-trailer');
      if (trailer) trailer.replaceChildren();
      if (_modalPollInterval) { clearInterval(_modalPollInterval); _modalPollInterval = null; }
      _modalRecId = null;
      history.replaceState(null, '', window.location.pathname + window.location.search);
    },
  }) : null;
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

  function openModal(id) {
    var s = _recData[id];
    if (!s) return;
    _modalRecId = id;
    history.replaceState(null, '', '#recommendation-' + id);

    var heroEl = document.getElementById('modal-hero');
    heroEl.replaceChildren();
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
    quickEl.replaceChildren();
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
    ratingsEl.replaceChildren();
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

    var trailerEl = document.getElementById('modal-trailer');
    var trailerLabel = document.getElementById('modal-trailer-label');
    var trailerFallback = document.getElementById('modal-trailer-fallback');
    trailerEl.replaceChildren();
    trailerFallback.replaceChildren();

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
      trailerEl.replaceChildren();
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
      trailerFallback.replaceChildren();
      trailerFallback.appendChild(link);
      trailerFallback.style.display = '';
    } else {
      trailerEl.style.display = 'none';
      trailerLabel.style.display = 'none';
      trailerFallback.style.display = 'none';
    }

    var castEl = document.getElementById('modal-cast');
    var castLabel = document.getElementById('modal-cast-label');
    castEl.replaceChildren();
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

    document.getElementById('modal-progress').style.display = 'none';
    document.getElementById('modal-success').style.display = 'none';

    var actionsEl = document.getElementById('modal-actions');
    actionsEl.replaceChildren();

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
      _startModalPolling(s);
    } else if (s.download_state === 'queued' || s.downloaded_at) {
      var qBtn = document.createElement('button');
      qBtn.className = 'btn-download queued';
      qBtn.textContent = 'Queued';
      actionsEl.appendChild(qBtn);
      _startModalPolling(s);
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
      MM.api.post('/api/recommended/' + s.id + '/share-token')
        .then(function (data) {
          if (!data.share_url) {
            shareBtn.textContent = 'Failed';
            shareBtn.disabled = false;
            setTimeout(function () { shareBtn.textContent = 'Share'; }, 2000);
            return;
          }
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
        })
        .catch(function () {
          shareBtn.textContent = 'Error';
          shareBtn.disabled = false;
          setTimeout(function () { shareBtn.textContent = 'Share'; }, 2000);
        });
    };
    actionsEl.appendChild(shareBtn);

    if (_detailModal) _detailModal.open();
  }

  function _modalDownload(btn, s) {
    btn.textContent = 'Adding to ' + (s.media_type === 'movie' ? 'Radarr' : 'Sonarr') + '…';
    btn.className = 'btn-download adding';

    MM.api.post('/api/recommended/' + s.id + '/download')
      .then(function () {
        btn.textContent = 'Queued';
        btn.className = 'btn-download queued';
        s.downloaded_at = new Date().toISOString();
        _startModalPolling(s);
      })
      .catch(function (err) {
        if (err instanceof MM.api.APIError) {
          var msg = err.message || 'Failed';
          btn.textContent = msg.length < 40 ? msg : 'Failed';
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
        } else {
          btn.textContent = 'Error';
          btn.className = 'btn-download';
        }
      });
  }

  function _startModalPolling(s) {
    var service = s.media_type === 'movie' ? 'radarr' : 'sonarr';
    var tmdbId = s.tmdb_id;
    if (!tmdbId) return;

    var progressEl = document.getElementById('modal-progress');
    progressEl.replaceChildren();

    var statusDiv = document.createElement('div');
    statusDiv.className = 'progress-status';
    var iconDiv = document.createElement('div');
    iconDiv.className = 'progress-icon';
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'spinner');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '20'); svg.setAttribute('height', '20');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'var(--accent)');
    svg.setAttribute('stroke-width', '2.5'); svg.setAttribute('stroke-linecap', 'round');
    var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '10');
    circle.setAttribute('stroke-opacity', '0.2');
    var arc = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    arc.setAttribute('d', 'M12 2a10 10 0 0 1 10 10');
    svg.appendChild(circle); svg.appendChild(arc);
    iconDiv.appendChild(svg);
    statusDiv.appendChild(iconDiv);
    var labelSpan = document.createElement('span');
    labelSpan.textContent = 'Searching for release…';
    statusDiv.appendChild(labelSpan);
    progressEl.appendChild(statusDiv);

    var trackDiv = document.createElement('div');
    trackDiv.className = 'progress-bar-track';
    var fillDiv = document.createElement('div');
    fillDiv.className = 'progress-bar-fill';
    trackDiv.appendChild(fillDiv);
    progressEl.appendChild(trackDiv);

    var detailDiv = document.createElement('div');
    detailDiv.className = 'progress-detail';
    detailDiv.textContent = 'Checking indexers for ' + s.title;
    progressEl.appendChild(detailDiv);

    var hintDiv = document.createElement('div');
    hintDiv.className = 'progress-hint';
    hintDiv.textContent = 'You may close this page — the download will continue in the background. You\'ll be notified by email when this ' + (s.media_type === 'movie' ? 'movie' : 'show') + ' is available to watch.';
    hintDiv.style.display = 'none';
    progressEl.appendChild(hintDiv);

    progressEl.style.display = '';
    setTimeout(function () { hintDiv.style.display = ''; }, 2000);

    if (_modalPollInterval) clearInterval(_modalPollInterval);
    _modalPollInterval = setInterval(function () {
      MM.api.get('/api/download/status?service=' + service + '&tmdb_id=' + tmdbId)
        .then(function (data) {
          if (data.state === 'ready') {
            clearInterval(_modalPollInterval);
            _modalPollInterval = null;
            progressEl.style.display = 'none';
            var successEl = document.getElementById('modal-success');
            successEl.replaceChildren();
            var svgOk = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svgOk.setAttribute('viewBox', '0 0 24 24');
            svgOk.setAttribute('width', '48'); svgOk.setAttribute('height', '48');
            svgOk.setAttribute('fill', 'none'); svgOk.setAttribute('stroke', 'var(--success)');
            svgOk.setAttribute('stroke-width', '2'); svgOk.setAttribute('stroke-linecap', 'round');
            svgOk.setAttribute('stroke-linejoin', 'round');
            var c2 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
            c2.setAttribute('cx', '12'); c2.setAttribute('cy', '12'); c2.setAttribute('r', '10');
            var pl2 = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
            pl2.setAttribute('points', '8 12 11 15 16 9');
            svgOk.appendChild(c2); svgOk.appendChild(pl2);
            var iconWrap = document.createElement('div');
            iconWrap.className = 'success-icon';
            iconWrap.appendChild(svgOk);
            successEl.appendChild(iconWrap);
            var title = document.createElement('div');
            title.className = 'success-title';
            title.textContent = 'Ready to watch!';
            successEl.appendChild(title);
            var detail = document.createElement('div');
            detail.className = 'success-detail';
            detail.textContent = s.title + ' is now available in your Plex library.';
            successEl.appendChild(detail);
            successEl.style.display = '';
            var btn = document.querySelector('#modal-actions .btn-download');
            if (btn) { btn.textContent = 'In Library ✓'; btn.className = 'btn-download ready'; }
            s.download_state = 'in_library';
          } else if (data.state === 'downloading') {
            var pct = data.progress || 0;
            fillDiv.style.width = pct + '%';
            labelSpan.textContent = 'Downloading…';
            var detailText = pct + '%';
            if (data.eta) detailText += ' · ' + data.eta + ' remaining';
            detailDiv.textContent = detailText;
          } else if (data.state === 'almost_ready') {
            fillDiv.style.width = '100%';
            labelSpan.textContent = 'Almost ready…';
            detailDiv.textContent = 'Importing into your library';
          } else if (data.state === 'queued') {
            labelSpan.textContent = 'Queued — waiting on indexer';
            detailDiv.textContent = 'Grabbed release waiting to start';
          } else if (data.state === 'searching') {
            labelSpan.textContent = 'Searching for release…';
            detailDiv.textContent = 'Checking indexers for ' + s.title;
          }
        })
        .catch(function () {});
    }, 4000);
  }

  /* Backdrop click + ESC + ModalA11y registration are all handled by
     MM.modal.setupDetail above. */

  (function () {
    var hash = window.location.hash;
    if (hash && hash.indexOf('#recommendation-') === 0) {
      var id = parseInt(hash.replace('#recommendation-', ''), 10);
      if (id) setTimeout(function () { openModal(id); }, 200);
    }
  })();
})();
