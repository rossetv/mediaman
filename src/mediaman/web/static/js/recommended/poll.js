/**
 * recommended/poll.js — modal status polling once a download is in flight.
 *
 * Polls /api/download/status for the recommendation currently rendered in
 * the detail modal and paints the progress bar / labels in place. Owned
 * by the modal, not the page — the page itself uses no polling.
 *
 * Exposes:
 *   MM.recommended.poll.startModalPolling(rec)
 *   MM.recommended.poll.stopModalPolling()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.recommended = MM.recommended || {};

  var pollInterval = null;

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function stopModalPolling() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
  }

  function startModalPolling(s) {
    var service = s.media_type === 'movie' ? 'radarr' : 'sonarr';
    var tmdbId = s.tmdb_id;
    if (!tmdbId) return;

    var progressEl = document.getElementById('modal-progress');
    clear(progressEl);

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

    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(function () {
      MM.api.get('/api/download/status?service=' + service + '&tmdb_id=' + tmdbId)
        .then(function (data) {
          if (data.state === 'ready') {
            clearInterval(pollInterval);
            pollInterval = null;
            progressEl.style.display = 'none';
            var successEl = document.getElementById('modal-success');
            clear(successEl);
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

  MM.recommended.poll = {
    startModalPolling: startModalPolling,
    stopModalPolling: stopModalPolling,
  };
})();
