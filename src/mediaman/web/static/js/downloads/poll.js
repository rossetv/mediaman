/**
 * downloads/poll.js — 2 s polling loop with exponential backoff.
 *
 * Owns the timer / fetch / failure handling and the visibility +
 * mediaman:poll:now wiring. The page bootstrap hands in a callback that
 * receives the parsed JSON payload from /api/downloads, and one that is
 * called when mediaman itself does not answer.
 *
 * Exposes:
 *   MM.downloads.poll.start(onData, onLost)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.downloads = MM.downloads || {};

  var POLL_MS = 2000;
  /* A poll this late is told to the person; one that outlasts what the server
     itself gives a service that hangs will never come, and another is sent. */
  var LATE_MS = 20000;
  var HUNG_MS = 120000;
  var polling = false;
  var pendingNow = false;
  var consecutiveFailures = 0;
  var intervalId = null;
  var onData = null;
  var onLost = null;
  var inFlight = null;

  function scheduleNextPoll(ms) {
    if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
    /* A hidden tab stays quiet; the visibilitychange handler restarts the poll. */
    if (document.hidden) return;
    intervalId = setTimeout(function () { intervalId = null; runPoll(); }, ms);
  }

  function runPoll() {
    if (polling || document.hidden) return;
    polling = true;
    var request = inFlight = new AbortController();
    var late = setTimeout(function () { onLost(); }, LATE_MS);
    var hung = setTimeout(function () { request.abort(); }, HUNG_MS);
    MM.api.get('/api/downloads', { signal: request.signal })
      .then(function (data) {
        /* What cannot be drawn is no poll that failed. */
        try { if (data) onData(data); } catch (err) { console.error(err); }
        consecutiveFailures = 0;
        scheduleNextPoll(POLL_MS);
      })
      .catch(function (err) {
        if (err.status === 401 || err.status === 403) {
          window.location.href = '/login';
          return;
        }
        /* A hidden tab gave its poll up; the next starts when it is shown. */
        if (document.hidden) return;
        consecutiveFailures += 1;
        /* One failed poll is no news: the next is close behind. */
        if (consecutiveFailures > 1) onLost();
        var backoff = Math.min(POLL_MS * Math.pow(2, consecutiveFailures), 30000);
        scheduleNextPoll(backoff);
      })
      .finally(function () {
        clearTimeout(late);
        clearTimeout(hung);
        inFlight = null;
        polling = false;
        /* A refresh asked for while this poll was in flight may have missed
           the change that prompted it, so poll again straight away. */
        if (pendingNow) {
          pendingNow = false;
          if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
          runPoll();
        }
      });
  }

  function start(cb, lost) {
    onData = cb;
    onLost = lost;
    scheduleNextPoll(POLL_MS);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
        /* A phone put away may never hear back, and would wait for it when taken out again. */
        if (inFlight) inFlight.abort();
      } else if (intervalId === null && !polling) {
        runPoll();
      }
    });

    /* Allow external code (e.g. dl-abandon.js) to request an immediate poll. */
    document.addEventListener('mediaman:poll:now', function () {
      if (polling) { pendingNow = true; return; }
      if (intervalId !== null) { clearTimeout(intervalId); intervalId = null; }
      runPoll();
    });
  }

  MM.downloads.poll = {
    start: start,
  };
})();
