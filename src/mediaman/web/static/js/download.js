/**
 * download.js — behaviour for the per-token download confirm page.
 *
 * Reads server-supplied values from the #page-bootstrap JSON island
 * (token, poll_token, service, tmdb_id, poster_url, title, media_type,
 * download_state) and drives the download trigger + polling UI. Moved
 * out of the inline <script> in download.html for CSP-friendliness.
 */
(function () {
  'use strict';

  /* ── Bootstrap from JSON island ── */
  var bootstrapEl = document.getElementById('page-bootstrap');
  if (!bootstrapEl) return;
  var raw = bootstrapEl.textContent;
  if (raw.toLowerCase().indexOf('</script') !== -1) {
    throw new Error('page-bootstrap: </script breakout detected — server-side escaping regression');
  }
  var boot = JSON.parse(raw);

  var _token = boot.token || '';
  var _pollToken = boot.poll_token || '';
  var _service = boot.service || '';
  var _tmdbId = boot.tmdb_id || 0;
  var _posterUrl = boot.poster_url || '';
  var _title = boot.title || '';
  var _mediaType = boot.media_type || 'movie';
  var _downloadState = boot.download_state || '';
  var _pollInterval = null;

  /* ── Download button click handler ── */
  function triggerDownload() {
    var btn = document.getElementById('btn-download');
    btn.textContent = 'Adding…';
    btn.disabled = true;
    btn.style.opacity = '0.6';

    MM.api.post('/download/' + _token)
      .then(function(data) {
        _service = data.service;
        _tmdbId = data.tmdb_id;
        if (data.poll_token) _pollToken = data.poll_token;

        /* Hide cinematic metadata, show hero card wrapper */
        var cinema = document.getElementById('dl-cinema');
        var content = document.getElementById('dl-content');
        if (cinema) cinema.style.display = 'none';
        if (content) content.style.display = 'none';

        var wrapper = document.getElementById('dl-hero-wrapper');
        buildHeroCard(wrapper, {
          id: _service + ':' + _title,
          title: _title,
          media_type: _mediaType,
          poster_url: _posterUrl,
          state: 'searching',
          progress: 0,
          eta: '',
          size_done: '',
          size_total: '',
          episodes: null,
          episode_summary: ''
        });
        wrapper.style.display = '';

        document.getElementById('download-action').style.display = 'none';

        pollStatus();
        _pollInterval = setInterval(pollStatus, 4000);
      })
      .catch(function(err) {
        if (err instanceof MM.api.APIError) {
          showResult(false, err.message || 'Download failed');
        } else {
          showResult(false, 'Network error — please try again');
        }
      });
  }

  /* ── Build hero card DOM matching _dl_hero_card.html structure ──
     Values are set via textContent / DOM properties — safe by default. */
  function buildHeroCard(wrapper, item) {
    var bgUrl = item.poster_url || '';

    /* Build the root .dl-hero container */
    var hero = document.createElement('div');
    hero.className = 'dl-hero dl-card-enter';
    hero.setAttribute('data-dl-id', item.id);

    /* Background — set via DLPoster.apply (H67: avoids CSS string injection). */
    var bg = document.createElement('div');
    bg.className = 'dl-hero-bg';
    if (bgUrl) bg.setAttribute('data-bg-url', bgUrl);
    hero.appendChild(bg);
    if (bgUrl && window.DLPoster) window.DLPoster.apply(bg);

    /* Overlay */
    var overlay = document.createElement('div');
    overlay.className = 'dl-hero-overlay';
    hero.appendChild(overlay);

    /* Content wrapper */
    var content = document.createElement('div');
    content.className = 'dl-hero-content';

    /* Poster */
    var posterWrap = document.createElement('div');
    posterWrap.className = 'dl-hero-poster';
    if (bgUrl) {
      var posterImg = document.createElement('img');
      posterImg.src = bgUrl;
      posterImg.alt = '';
      posterWrap.appendChild(posterImg);
    } else {
      var placeholder = document.createElement('div');
      placeholder.className = 'dl-hero-poster-placeholder';
      posterWrap.appendChild(placeholder);
    }
    content.appendChild(posterWrap);

    /* Info section */
    var info = document.createElement('div');
    info.className = 'dl-hero-info';

    var titleEl = document.createElement('div');
    titleEl.className = 'dl-hero-title';
    titleEl.textContent = item.title;
    info.appendChild(titleEl);

    /* State pill */
    var statusWrap = document.createElement('div');
    statusWrap.className = 'dl-hero-status';
    var pill = document.createElement('span');
    pill.className = 'dl-state-pill dl-state-' + item.state;
    pill.setAttribute('data-v', 'pill');
    pill.textContent = item.state_label || item.state;
    statusWrap.appendChild(pill);
    info.appendChild(statusWrap);

    /* Progress bar */
    var progressWrap = document.createElement('div');
    progressWrap.className = 'dl-hero-progress';
    progressWrap.setAttribute('data-v', 'progress-wrap');
    if (item.state === 'searching') progressWrap.style.display = 'none';

    var bar = document.createElement('div');
    bar.className = 'dl-hero-bar';
    var fill = document.createElement('div');
    fill.className = 'dl-hero-fill';
    fill.setAttribute('data-v', 'fill');
    fill.style.width = item.progress + '%';
    bar.appendChild(fill);
    progressWrap.appendChild(bar);

    var details = document.createElement('div');
    details.className = 'dl-hero-details';
    var pctSpan = document.createElement('span');
    var pctInner = document.createElement('span');
    pctInner.className = 'dl-hero-pct';
    pctInner.setAttribute('data-v', 'pct');
    pctInner.textContent = item.progress + '%';
    pctSpan.appendChild(pctInner);
    details.appendChild(pctSpan);
    var etaSpan = document.createElement('span');
    etaSpan.setAttribute('data-v', 'eta');
    etaSpan.textContent = item.eta;
    details.appendChild(etaSpan);
    progressWrap.appendChild(details);
    info.appendChild(progressWrap);

    content.appendChild(info);
    hero.appendChild(content);

    /* Hint text */
    var hint = document.createElement('div');
    hint.className = 'dl-hint';
    hint.textContent = 'You may close this page — the download will continue in the background. ';
    var br = document.createElement('br');
    hint.appendChild(br);
    var hintLine2 = document.createTextNode(
      'You’ll be notified by email when this ' + (_mediaType === 'movie' ? 'movie' : 'show') + ' is available to watch.'
    );
    hint.appendChild(hintLine2);

    /* Clear wrapper and append */
    wrapper.replaceChildren(hero, hint);
  }

  // State labels come from the server (item.state_label). Fall back to the
  // raw state string only if the server didn't supply one (defensive — shouldn't
  // happen). The canonical map lives in services/downloads/download_format/_types.py.

  /* ── Poll download status ── */
  function pollStatus() {
    if (!_service || !_tmdbId) return;
    /* Finding 14: use the short-lived poll_token, not the long-lived download token. */
    var url = '/api/download/status?service=' + _service + '&tmdb_id=' + _tmdbId
      + (_pollToken ? '&poll_token=' + encodeURIComponent(_pollToken) : '');
    MM.api.get(url)
      .then(function(data) {
        if (data.state === 'ready') {
          clearInterval(_pollInterval);
          showResult(true, 'Ready to watch!');
          return;
        }

        /* Update the hero card in-place */
        var wrapper = document.getElementById('dl-hero-wrapper');
        if (!wrapper) return;

        var pill = wrapper.querySelector('[data-v="pill"]');
        if (pill) {
          pill.className = 'dl-state-pill dl-state-' + data.state;
          pill.textContent = data.state_label || data.state;
        }

        var progressWrap = wrapper.querySelector('[data-v="progress-wrap"]');
        if (progressWrap) {
          progressWrap.style.display = (data.state === 'searching') ? 'none' : '';
        }

        var fill = wrapper.querySelector('[data-v="fill"]');
        if (fill) {
          fill.style.width = (data.progress || 0) + '%';
          if (data.state === 'almost_ready') fill.classList.add('green');
          else fill.classList.remove('green');
        }

        var pct = wrapper.querySelector('[data-v="pct"]');
        if (pct) pct.textContent = (data.progress || 0) + '%';

        var sizeDone = wrapper.querySelector('[data-v="size-done"]');
        if (sizeDone) sizeDone.textContent = data.size_done || '';
        var sizeTotal = wrapper.querySelector('[data-v="size-total"]');
        if (sizeTotal) sizeTotal.textContent = data.size_total || '';

        var eta = wrapper.querySelector('[data-v="eta"]');
        if (eta) eta.textContent = data.eta || '';

        /* Episode rows for series */
        if (data.episodes && data.episodes.length) {
          updateEpisodeRows(wrapper, data.episodes);
        }

        /* Update episode summary */
        updateEpisodeSummary(wrapper, data.episode_summary);
      })
      .catch(function() {});
  }

  function updateEpisodeRows(wrapper, episodes) {
    var epToggle = wrapper.querySelector('[data-v="ep-toggle"]');
    var epList = wrapper.querySelector('[data-v="ep-list"]');
    if (!epToggle && !epList) {
      /* Build episode rows dynamically using DOM methods */
      var hero = wrapper.querySelector('.dl-hero');
      if (!hero) return;

      var toggle = document.createElement('div');
      toggle.className = 'dl-hero-episodes-toggle';
      toggle.setAttribute('data-v', 'ep-toggle');
      var toggleSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      toggleSvg.setAttribute('width', '14'); toggleSvg.setAttribute('height', '14');
      toggleSvg.setAttribute('viewBox', '0 0 24 24'); toggleSvg.setAttribute('fill', 'none');
      toggleSvg.setAttribute('stroke', 'currentColor'); toggleSvg.setAttribute('stroke-width', '2.5');
      toggleSvg.setAttribute('stroke-linecap', 'round');
      var polyline = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      polyline.setAttribute('points', '9 18 15 12 9 6');
      toggleSvg.appendChild(polyline);
      toggle.appendChild(toggleSvg);
      var countSpan = document.createElement('span');
      countSpan.textContent = episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '');
      toggle.appendChild(countSpan);
      hero.appendChild(toggle);

      var list = document.createElement('div');
      list.className = 'dl-hero-episodes';
      list.setAttribute('data-v', 'ep-list');
      list.style.display = 'none';
      for (var i = 0; i < episodes.length; i++) {
        list.appendChild(buildEpisodeRow(episodes[i]));
      }
      hero.appendChild(list);

      toggle.addEventListener('click', function() {
        list.style.display = list.style.display === 'none' ? '' : 'none';
      });
    } else {
      /* Update existing episode rows */
      for (var j = 0; j < episodes.length; j++) {
        var ep = episodes[j];
        var row = MM.dom.findByAttr(wrapper, 'data-ep', ep.label);
        if (!row) continue;
        var epFill = row.querySelector('[data-v="ep-fill"]');
        if (epFill) {
          epFill.style.width = (ep.progress || 0) + '%';
          if (ep.state === 'ready') epFill.classList.add('green');
        }
        var epPill = row.querySelector('.dl-ep-status-pill');
        if (epPill) {
          epPill.className = 'dl-ep-status-pill ' + ep.state;
          epPill.textContent = ep.state === 'ready' ? 'Ready' : (ep.state === 'downloading' ? (ep.progress || 0) + '%' : 'Searching');
        }
      }
      if (epToggle) {
        var cs = epToggle.querySelector('span');
        if (cs) cs.textContent = episodes.length + ' episode' + (episodes.length !== 1 ? 's' : '');
      }
    }
  }

  function buildEpisodeRow(ep) {
    var epState = ep.state || 'searching';
    var row = document.createElement('div');
    row.className = 'dl-ep-row';
    row.setAttribute('data-ep', ep.label);

    var num = document.createElement('span');
    num.className = 'dl-ep-number';
    num.textContent = ep.label;
    row.appendChild(num);

    var title = document.createElement('span');
    title.className = 'dl-ep-title';
    title.textContent = ep.title;
    row.appendChild(title);

    var miniBar = document.createElement('div');
    miniBar.className = 'dl-ep-mini-bar';
    var miniFill = document.createElement('div');
    miniFill.className = 'dl-ep-mini-fill' + (epState === 'ready' ? ' green' : '');
    miniFill.setAttribute('data-v', 'ep-fill');
    miniFill.style.width = (ep.progress || 0) + '%';
    miniBar.appendChild(miniFill);
    row.appendChild(miniBar);

    var pill = document.createElement('span');
    pill.className = 'dl-ep-status-pill ' + epState;
    pill.textContent = epState === 'ready' ? 'Ready' : (epState === 'downloading' ? (ep.progress || 0) + '%' : 'Searching');
    row.appendChild(pill);

    return row;
  }

  function updateEpisodeSummary(wrapper, summary) {
    var epSummary = wrapper.querySelector('.dl-hero-ep-summary');
    if (summary) {
      if (!epSummary) {
        var titleEl = wrapper.querySelector('.dl-hero-title');
        if (titleEl) {
          var sumDiv = document.createElement('div');
          sumDiv.className = 'dl-hero-ep-summary';
          sumDiv.textContent = summary;
          titleEl.parentNode.insertBefore(sumDiv, titleEl.nextSibling);
        }
      } else {
        epSummary.textContent = summary;
      }
    }
  }

  /* ── Show final result (success or error) ── */
  function showResult(success, message) {
    var actionEl = document.getElementById('download-action');
    if (actionEl) actionEl.style.display = 'none';
    var heroWrapper = document.getElementById('dl-hero-wrapper');
    if (heroWrapper) heroWrapper.style.display = 'none';

    var resultEl = document.getElementById('download-result');
    resultEl.replaceChildren();

    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '48'); svg.setAttribute('height', '48');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', success ? '#30d158' : '#ff453a');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
    var circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '10');
    svg.appendChild(circle);
    if (success) {
      var pl = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      pl.setAttribute('points', '8 12 11 15 16 9');
      svg.appendChild(pl);
    } else {
      var l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      l1.setAttribute('x1','15'); l1.setAttribute('y1','9'); l1.setAttribute('x2','9'); l1.setAttribute('y2','15');
      var l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      l2.setAttribute('x1','9'); l2.setAttribute('y1','9'); l2.setAttribute('x2','15'); l2.setAttribute('y2','15');
      svg.appendChild(l1); svg.appendChild(l2);
    }
    resultEl.appendChild(svg);

    var titleDiv = document.createElement('div');
    titleDiv.className = 'dl-result-title';
    titleDiv.style.color = success ? '#30d158' : '#ff453a';
    titleDiv.textContent = success ? message : 'Download failed';
    resultEl.appendChild(titleDiv);

    var detail = document.createElement('div');
    detail.className = 'dl-result-detail';
    detail.textContent = success ? 'It will appear in your Plex library shortly.' : message;
    resultEl.appendChild(detail);

    resultEl.style.display = '';
    resultEl.style.borderColor = success ? 'rgba(48,209,88,0.1)' : 'rgba(255,69,58,0.1)';
    resultEl.style.background = success ? 'rgba(48,209,88,0.04)' : 'rgba(255,69,58,0.04)';
  }

  /* ── Wire up the download button click (was inline onclick="triggerDownload()"). ── */
  var dlBtn = document.getElementById('btn-download');
  if (dlBtn) {
    dlBtn.addEventListener('click', triggerDownload);
  }

  /* ── Wire up episode toggle for server-rendered hero cards ── */
  (function() {
    var toggle = document.querySelector('#dl-hero-wrapper [data-v="ep-toggle"]');
    if (toggle) {
      toggle.addEventListener('click', function() {
        var list = document.querySelector('#dl-hero-wrapper [data-v="ep-list"]');
        if (list) list.style.display = list.style.display === 'none' ? '' : 'none';
      });
    }
  })();

  /* ── Start polling immediately if already queued ── */
  if (_downloadState === 'queued' && _tmdbId) {
    pollStatus();
    _pollInterval = setInterval(pollStatus, 4000);
  }
}());
