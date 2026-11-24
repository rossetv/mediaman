/**
 * download.js — behaviour for the per-token download confirm page.
 *
 * Reads server-supplied values from the #page-bootstrap JSON island
 * (token, poll_token, service, tmdb_id, poster_url, backdrop_url, title, media_type,
 * download_state, notify_by_email) and drives the download trigger + polling UI. Lives in
 * its own file, not an inline <script>, so the CSP can forbid inline script.
 */
(function () {
  'use strict';

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
  var _backdropUrl = boot.backdrop_url || '';
  var _title = boot.title || '';
  var _mediaType = boot.media_type || 'movie';
  var _downloadState = boot.download_state || '';
  var _notifyByEmail = boot.notify_by_email === true;
  var _pollInterval = null;
  var _pollStopped = false;

  function triggerDownload() {
    var btn = document.getElementById('btn-download');
    /* Not the disabled attribute: that would drop the keyboard's focus. */
    if (btn.getAttribute('aria-disabled') === 'true') return;
    btn.setAttribute('aria-disabled', 'true');
    btn.textContent = 'Adding…';
    /* Safari gives a clicked button no focus, and what comes next goes by where it is. */
    btn.focus();

    MM.api.post('/download/' + _token).then(function(data) {
      _service = data.service;
      _tmdbId = data.tmdb_id;
      showOnItsWay(data.poll_token, false);
    }, function(err) {
      showRefusal(err);
    });
  }

  /* Trade the title's page for the progress card. */
  function showOnItsWay(pollToken, already) {
    if (pollToken) {
      _pollToken = pollToken;
      /* A fresh token means polling can authenticate again. */
      _pollStopped = false;
    }
    var keepsFocus = focusStillOnAction();

    var cinema = document.getElementById('dl-cinema');
    var content = document.getElementById('dl-content');
    if (cinema) cinema.hidden = true;
    if (content) content.hidden = true;

    var wrapper = document.getElementById('dl-hero-wrapper');
    var heading = buildHeroCard(wrapper, {
      id: _service + ':' + _title,
      title: _title,
      media_type: _mediaType,
      poster_url: _posterUrl,
      hero_backdrop: _backdropUrl,
      state: 'searching',
      state_label: 'Searching',
      progress: 0,
      eta: '',
      size_done: '',
      size_total: '',
      episodes: null,
      episode_summary: ''
    }, already);
    wrapper.hidden = false;
    if (keepsFocus) heading.focus();

    /* A 401 already stopped polling and told the visitor to reload. */
    if (!_pollStopped) {
      pollStatus();
      _startPolling();
    }
  }

  function focusStillOnAction() {
    return MM.dom.focusStillIn(document.getElementById('download-action'));
  }

  /* The card, with what _dl_hero_card.html and download.html put around it. Returns the
     page's heading. */
  function buildHeroCard(wrapper, item, already) {
    var hero = MM.downloads.buildDom.buildHero(item);

    /* The h1 of the page went with the cinema, and the card's title is no h1. */
    var heading = document.createElement('h1');
    heading.className = 'sr-only';
    heading.tabIndex = -1;
    heading.textContent = item.title;
    heading.setAttribute('aria-describedby', 'dl-hint');

    var hint = document.createElement('div');
    hint.className = 'dl-hint';
    hint.id = 'dl-hint';
    hint.setAttribute('role', 'status');
    hint.textContent = (already ? 'This was already on its way. ' : '')
      + 'You may close this page — the download will continue in the background. ';
    /* A link minted without an email gets no notification, so promise none. */
    if (_notifyByEmail) {
      hint.appendChild(document.createElement('br'));
      hint.appendChild(document.createTextNode(
        'You’ll be notified by email when this ' + (_mediaType === 'movie' ? 'movie' : 'show') + ' is available to watch.'
      ));
    }

    var footer = document.createElement('div');
    footer.className = 'dl-footer u-mt-24';
    var link = document.createElement('a');
    link.href = '/';
    link.textContent = 'Open mediaman →';
    footer.appendChild(link);

    wrapper.replaceChildren(heading, hero, hint, footer);
    return heading;
  }

  // State labels come from the server (item.state_label). Fall back to the
  // raw state string only if the server didn't supply one (defensive — shouldn't
  // happen). The canonical map lives in services/downloads/download_format/_types.py.

  /* Polling uses setInterval internally; _startPolling / _stopPolling manage the
     timer and the visibilitychange pause so the tab doesn't hammer the
     server while hidden. */
  function _startPolling() {
    if (!_pollToken) return;
    if (_pollInterval !== null) { clearInterval(_pollInterval); }
    _pollInterval = setInterval(pollStatus, 4000);
  }

  function _stopPolling() {
    if (_pollInterval !== null) { clearInterval(_pollInterval); _pollInterval = null; }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      _stopPolling();
    } else if (_pollInterval === null && _downloadState !== 'ready' && !_pollStopped) {
      /* Resume: run one tick immediately then restart the interval. */
      pollStatus();
      _startPolling();
    }
  });

  function pollStatus() {
    /* Without a poll token the request can only be refused. */
    if (!_service || !_tmdbId || !_pollToken) return;
    /* Use the short-lived poll_token, not the long-lived download token. */
    var url = '/api/download/status?service=' + _service + '&tmdb_id=' + _tmdbId
      + '&poll_token=' + encodeURIComponent(_pollToken);
    MM.api.get(url)
      .then(function(data) {
        /* The server hands out a replacement before the current poll token expires. */
        if (data.poll_token) _pollToken = data.poll_token;
        /* The service did not answer: what is drawn is the last it said. */
        if (data.state === 'unknown') return;

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
          progressWrap.hidden = (data.state === 'searching' || data.state === 'ready');
        }

        var fill = wrapper.querySelector('[data-v="fill"]');
        if (fill) {
          MM.downloads.buildDom.setProgress(fill, data.progress || 0);
          if (data.state === 'almost_ready') fill.classList.add('green');
          else fill.classList.remove('green');
        }

        var pct = wrapper.querySelector('[data-v="pct"]');
        if (pct) pct.textContent = (data.progress || 0) + '%';

        var sizeDone = wrapper.querySelector('[data-v="size-done"]');
        if (sizeDone) sizeDone.textContent = data.size_done || '';
        var sizeTotal = wrapper.querySelector('[data-v="size-total"]');
        if (sizeTotal) sizeTotal.textContent = data.size_total || '';

        MM.downloads.buildDom.updateEtaStat(wrapper, data.eta);

        MM.downloads.buildDom.syncEpisodes(wrapper, data);
        updateEpisodeSummary(wrapper, data.episode_summary);

        if (data.state === 'ready') {
          _downloadState = 'ready';
          _stopPolling();
          showReady(wrapper);
        }
      })
      .catch(function(err) {
        /* Other failures are transient; the next tick retries. */
        if (err instanceof MM.api.APIError && err.status === 401) {
          _pollStopped = true;
          _stopPolling();
          showReloadNotice();
        }
      });
  }

  /* Polling can no longer authenticate: keep the last progress on screen and
     tell the visitor to reload, rather than leaving a bar that silently stops. */
  function showReloadNotice() {
    var wrapper = document.getElementById('dl-hero-wrapper');
    if (!wrapper) return;
    var hint = wrapper.querySelector('.dl-hint');
    if (!hint) {
      hint = document.createElement('div');
      hint.className = 'dl-hint';
      wrapper.appendChild(hint);
    }
    hint.setAttribute('role', 'status');
    hint.textContent = 'Progress updates have stopped. Reload this page to see the latest status.';
  }

  /* Where _dl_hero_card.html draws it: between the title and the state. */
  function updateEpisodeSummary(wrapper, summary) {
    var line = wrapper.querySelector('.dl-hero-ep-summary');
    if (!summary) {
      if (line) line.parentNode.removeChild(line);
      return;
    }
    if (!line) {
      line = document.createElement('div');
      line.className = 'dl-hero-ep-summary';
      var status = wrapper.querySelector('.dl-hero-status');
      status.parentNode.insertBefore(line, status);
    }
    MM.dom.setText(line, summary);
  }

  function fillResult(box, ok, title, detail) {
    var titleDiv = document.createElement('div');
    titleDiv.className = 'dl-result-title dl-result-title--' + (ok ? 'success' : 'error');
    titleDiv.textContent = title;
    var detailDiv = document.createElement('div');
    detailDiv.className = 'dl-result-detail';
    detailDiv.textContent = detail;

    box.className = 'dl-result dl-card-enter' + (ok ? '' : ' dl-result--error');
    box.replaceChildren(
      ok ? MM.dom.icon('i-circle-check', 'dl-result-icon dl-result-icon--success')
        : MM.dom.icon('i-circle-xmark', 'dl-result-icon dl-result-icon--error'),
      titleDiv,
      detailDiv
    );
    return detailDiv;
  }

  /* The card stays, so the page keeps its heading and its link; the words under it change. */
  function showReady(wrapper) {
    var hint = wrapper.querySelector('.dl-hint');
    if (hint) fillResult(hint, true, 'Ready to watch!', 'It will appear in your Plex library shortly.');
  }

  /* The request did not go through: say why where the button is. */
  function showRefusal(err) {
    var said = err.data || {};
    var status = err.status || 0;
    if (status === 409 && said.poll_token) {
      if (said.download_state === 'queued') {
        showOnItsWay(said.poll_token, true);
      } else if (said.download_state === 'in_library') {
        endWith(true, 'Already in your library', 'This title is available to watch in Plex.');
      } else if (said.download_state === 'tracked') {
        endWith(true, 'Already tracked by Sonarr', 'Sonarr has nothing to download for it right now.');
      } else {
        endWith(true, 'Already in ' + (_service === 'radarr' ? 'Radarr' : 'Sonarr'), MM.api.messageOf(err, ''));
      }
      return;
    }
    if (status === 409) {
      var detail = endWith(false, 'This link has already been used', '');
      var reload = document.createElement('a');
      reload.href = '';
      reload.textContent = 'Reload this page to see where the download is';
      detail.appendChild(reload);
      return;
    }
    var message = MM.api.messageOf(err, 'Something went wrong. Try again.');
    if (status && status !== 429 && status < 500) {
      endWith(false, 'Download failed', message);
      return;
    }

    var btn = document.getElementById('btn-download');
    btn.removeAttribute('aria-disabled');
    btn.textContent = 'Try again';
    var box = document.getElementById('download-result');
    fillResult(box, false, 'Download failed', message);
    box.hidden = false;
    btn.setAttribute('aria-describedby', box.id);
    box.scrollIntoView({ block: 'nearest' });
  }

  /* The last word: the button goes and the answer takes its place. */
  function endWith(ok, title, detail) {
    var keepsFocus = focusStillOnAction();
    var box = document.getElementById('download-result');
    var detailEl = fillResult(box, ok, title, detail);
    box.hidden = false;
    document.getElementById('download-action').hidden = true;
    if (keepsFocus) box.focus();
    return detailEl;
  }

  var dlBtn = document.getElementById('btn-download');
  if (dlBtn) {
    dlBtn.addEventListener('click', triggerDownload);
  }

  var heroWrapper = document.getElementById('dl-hero-wrapper');
  if (heroWrapper) {
    heroWrapper.addEventListener('click', function(e) {
      var toggle = e.target.closest('[data-v="ep-toggle"]');
      if (!toggle) return;
      var opening = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
      toggle.classList.toggle('open', opening);
      var list = toggle.nextElementSibling;
      if (opening) {
        list.hidden = false;
        MM.downloads.buildDom.reveal(list);
      } else {
        MM.downloads.buildDom.collapse(list, function() { list.hidden = true; });
      }
    });
  }

  if (_downloadState === 'queued' && _tmdbId) {
    pollStatus();
    _startPolling();
  }
}());
