/**
 * recommended/poll.js — modal status polling once a download is in flight.
 *
 * Polls /api/download/status for the recommendation currently rendered in
 * the detail modal and paints the progress bar / labels in place. Owned
 * by the modal, not the page — the page itself uses no polling.
 *
 * Uses a setTimeout-chain (matching downloads/poll.js) rather than
 * setInterval so a slow response can never queue a second overlapping
 * request. Includes:
 *   - Failure backoff: doubles the interval per consecutive failure up to
 *     30 s, then stops entirely after 8 consecutive failures.
 *   - 401/403 redirect to /login rather than hammering indefinitely.
 *   - visibilitychange pause: polling stops when the tab is hidden and
 *     resumes when visible.
 *
 * Exposes:
 *   MM.recommended.poll.startModalPolling(rec)
 *   MM.recommended.poll.stopModalPolling()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.recommended = MM.recommended || {};

  var POLL_MS               = 4000;
  var MAX_FAILURES          = 8;

  /* Active poll state — reset on each startModalPolling() call. */
  var pollTimer             = null;
  var consecutiveFailures   = 0;
  var polling               = false; /* true while a fetch is in flight */

  /* Captured refs to progress DOM nodes (set when the progress UI is built). */
  var _fillDiv    = null;
  var _labelSpan  = null;
  var _detailDiv  = null;

  /* The rec object passed to startModalPolling — retained for render callbacks. */
  var _rec        = null;

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function stopModalPolling() {
    if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
    polling = false;
    consecutiveFailures = 0;
    _fillDiv = _labelSpan = _detailDiv = _rec = null;
  }

  function _scheduleNext(ms) {
    if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
    pollTimer = setTimeout(function () { pollTimer = null; _runPoll(); }, ms);
  }

  function _runPoll() {
    /* Guard: don't fire while hidden or while another fetch is still in flight. */
    if (document.hidden || polling || !_rec) return;
    polling = true;

    var service = _rec.media_type === 'movie' ? 'radarr' : 'sonarr';
    var tmdbId  = _rec.tmdb_id;

    MM.api.get('/api/download/status?service=' + service + '&tmdb_id=' + tmdbId)
      .then(function (data) {
        consecutiveFailures = 0;
        _handleData(data);
        /* Only schedule the next poll if we haven't stopped (e.g. state===ready). */
        if (_rec !== null) _scheduleNext(POLL_MS);
      })
      .catch(function (err) {
        /* Auth failure — redirect instead of looping forever. */
        if (err && (err.status === 401 || err.status === 403)) {
          stopModalPolling();
          window.location.href = '/login';
          return;
        }
        consecutiveFailures += 1;
        if (consecutiveFailures >= MAX_FAILURES) {
          /* Too many consecutive errors — give up silently. The user can
             close and reopen the modal to restart polling. */
          stopModalPolling();
          return;
        }
        /* Exponential backoff, capped at 30 s. */
        var backoff = Math.min(POLL_MS * Math.pow(2, consecutiveFailures), 30000);
        _scheduleNext(backoff);
      })
      .finally(function () { polling = false; });
  }

  function _handleData(data) {
    var progressEl = document.getElementById('modal-progress');
    if (!progressEl) return;

    if (data.state === 'ready') {
      stopModalPolling();
      progressEl.style.display = 'none';
      var successEl = document.getElementById('modal-success');
      clear(successEl);
      var iconWrap = document.createElement('div');
      iconWrap.className = 'success-icon';
      var okIcon = document.createElement('i');
      okIcon.className = 'fa-solid fa-circle-check';
      okIcon.setAttribute('aria-hidden', 'true');
      iconWrap.appendChild(okIcon);
      successEl.appendChild(iconWrap);
      var title = document.createElement('div');
      title.className = 'success-title';
      title.textContent = 'Ready to watch!';
      successEl.appendChild(title);
      var detail = document.createElement('div');
      detail.className = 'success-detail';
      /* _rec was retained until stopModalPolling() but we need the title now — cache it. */
      var recTitle = _rec ? _rec.title : '';
      detail.textContent = recTitle + ' is now available in your Plex library.';
      successEl.appendChild(detail);
      successEl.style.display = '';
      var btn = document.querySelector('#modal-actions .btn-download');
      if (btn) { btn.textContent = 'In Library ✓'; btn.className = 'btn-download ready'; }
      /* _rec.download_state updated for the benefit of _paintActions re-renders. */
      if (_rec) _rec.download_state = 'in_library';
      /* stopModalPolling() clears _rec — update state before that call. */
      /* (already called above) */
    } else if (data.state === 'downloading') {
      var pct = data.progress || 0;
      if (_fillDiv) _fillDiv.style.width = pct + '%';
      if (_labelSpan) _labelSpan.textContent = 'Downloading…';
      var detailText = pct + '%';
      if (data.eta) detailText += ' · ' + data.eta + ' remaining';
      if (_detailDiv) _detailDiv.textContent = detailText;
    } else if (data.state === 'almost_ready') {
      if (_fillDiv) _fillDiv.style.width = '100%';
      if (_labelSpan) _labelSpan.textContent = 'Almost ready…';
      if (_detailDiv) _detailDiv.textContent = 'Importing into your library';
    } else if (data.state === 'queued') {
      if (_labelSpan) _labelSpan.textContent = 'Queued — waiting on indexer';
      if (_detailDiv) _detailDiv.textContent = 'Grabbed release waiting to start';
    } else if (data.state === 'searching') {
      if (_labelSpan) _labelSpan.textContent = 'Searching for release…';
      if (_detailDiv && _rec) _detailDiv.textContent = 'Checking indexers for ' + _rec.title;
    }
  }

  function startModalPolling(s) {
    var tmdbId = s.tmdb_id;
    if (!tmdbId) return;

    /* Tear down any previous poll before starting a fresh one. */
    stopModalPolling();
    _rec = s;

    var progressEl = document.getElementById('modal-progress');
    clear(progressEl);

    var statusDiv = document.createElement('div');
    statusDiv.className = 'progress-status';
    var iconDiv = document.createElement('div');
    iconDiv.className = 'progress-icon';
    var spinIcon = document.createElement('i');
    spinIcon.className = 'fa-solid fa-spinner fa-spin-pulse spinner';
    spinIcon.setAttribute('aria-hidden', 'true');
    iconDiv.appendChild(spinIcon);
    statusDiv.appendChild(iconDiv);
    _labelSpan = document.createElement('span');
    _labelSpan.textContent = 'Searching for release…';
    statusDiv.appendChild(_labelSpan);
    progressEl.appendChild(statusDiv);

    var trackDiv = document.createElement('div');
    trackDiv.className = 'progress-bar-track';
    _fillDiv = document.createElement('div');
    _fillDiv.className = 'progress-bar-fill';
    trackDiv.appendChild(_fillDiv);
    progressEl.appendChild(trackDiv);

    _detailDiv = document.createElement('div');
    _detailDiv.className = 'progress-detail';
    _detailDiv.textContent = 'Checking indexers for ' + s.title;
    progressEl.appendChild(_detailDiv);

    var hintDiv = document.createElement('div');
    hintDiv.className = 'progress-hint';
    hintDiv.textContent = 'You may close this page — the download will continue in the background. You\'ll be notified by email when this ' + (s.media_type === 'movie' ? 'movie' : 'show') + ' is available to watch.';
    hintDiv.style.display = 'none';
    progressEl.appendChild(hintDiv);

    progressEl.style.display = '';
    setTimeout(function () { hintDiv.style.display = ''; }, 2000);

    /* Register visibilitychange handler once per page load. Multiple calls to
       startModalPolling reuse the same handler because it reads the module-level
       polling state. The listener is cheap (no-op when nothing is polling). */
    if (!_visibilityWired) {
      _visibilityWired = true;
      document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
          /* Pause: cancel any pending timer (in-flight fetch finishes naturally). */
          if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
        } else if (_rec !== null && pollTimer === null && !polling) {
          /* Resume: run immediately rather than waiting for the next scheduled tick. */
          _runPoll();
        }
      });
    }

    _scheduleNext(POLL_MS);
  }

  /* Tracks whether the visibilitychange listener has been attached. */
  var _visibilityWired = false;

  MM.recommended.poll = {
    startModalPolling: startModalPolling,
    stopModalPolling: stopModalPolling,
  };
})();
