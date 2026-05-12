/**
 * downloads/poll.js — 2 s polling loop with exponential backoff.
 *
 * Owns the timer / fetch / failure handling and the visibility +
 * mediaman:poll:now wiring. The page bootstrap hands in a callback that
 * receives the parsed JSON payload from /api/downloads.
 *
 * Exposes:
 *   MM.downloads.poll.start(onData)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  var POLL_MS = 2000;
  var polling = false;
  var consecutiveFailures = 0;
  var intervalId = null;
  var onData = null;

  function scheduleNextPoll(ms) {
    if (intervalId !== null) { clearTimeout(intervalId); }
    intervalId = setTimeout(function () { intervalId = null; runPoll(); }, ms);
  }

  function runPoll() {
    if (polling) return;
    polling = true;
    MM.api.get('/api/downloads')
      .then(function (data) {
        if (data && onData) onData(data);
        consecutiveFailures = 0;
        scheduleNextPoll(POLL_MS);
      })
      .catch(function (err) {
        if (err.status === 401 || err.status === 403) {
          window.location.href = '/login';
          return;
        }
        consecutiveFailures += 1;
        var backoff = Math.min(POLL_MS * Math.pow(2, consecutiveFailures), 30000);
        scheduleNextPoll(backoff);
      })
      .finally(function () { polling = false; });
  }

  function start(cb) {
    onData = cb;
    scheduleNextPoll(POLL_MS);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
      } else if (intervalId === null) {
        runPoll();
      }
    });

    /* Allow external code (e.g. dl-abandon.js) to request an immediate poll. */
    document.addEventListener('mediaman:poll:now', function () {
      if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
      runPoll();
    });
  }

  MM.downloads.poll = {
    start: start,
  };
})();
