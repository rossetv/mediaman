/* What is downloading, asked for once per tab for whoever shows it: the
 * Watch page's row and the header's ring. On MM.downloading:
 *
 *   subscribe(fn)  fn({ summary, items, outage, lost }) now, when something
 *                  is known, and on every answer; returns what ends it.
 *                  The first one starts the asking.
 *   last()         the same, or null.
 *   now()          ask soon: something was just asked for.
 *
 * lost: mediaman itself has stopped answering, and the rest is as it last was.
 * Needs core/api.js.
 */
(function () {
  'use strict';

  var URL = '/api/downloads/strip';
  var BUSY_MS = 5000;
  var IDLE_MS = 30000;
  var SOON_MS = 1000;
  var EAGER_MS = 60000; // after now(): what was asked for takes a while to be queued
  var FAIL_MS = 10000;
  var FAIL_MAX_MS = 60000;
  var HUNG_MS = 60000;
  var LOST_AFTER = 2; // failures in a row

  var readers = [];
  var state = null;
  var timer = null;
  var inFlight = null;
  var again = false; // asked for while an answer was on its way
  var failures = 0;
  var eagerUntil = 0;
  var started = false;
  var ended = false;

  function tell() {
    readers.slice().forEach(function (fn) {
      try { fn(state); } catch (err) { console.error(err); }
    });
  }

  function busy() {
    var sum = state.summary;
    return sum.active > 0 || sum.ready > 0 || Date.now() < eagerUntil ||
      state.items.some(function (item) { return item.state === 'queued'; });
  }

  function wait(ms) {
    clearTimeout(timer);
    timer = null;
    if (ended || document.hidden) return;
    timer = setTimeout(ask, ms);
  }

  function take(data) {
    if (!data || !data.summary || !Array.isArray(data.items)) return false;
    state = { summary: data.summary, items: data.items, outage: data.outage || '', lost: false };
    return true;
  }

  function failed(err) {
    if (err && (err.status === 401 || err.status === 403)) { ended = true; return; }
    failures += 1;
    if (failures === LOST_AFTER && state) {
      state = Object.assign({}, state, { lost: true });
      tell();
    }
    wait(Math.min(FAIL_MS * Math.pow(2, failures - 1), FAIL_MAX_MS));
  }

  function ask() {
    timer = null;
    if (inFlight || ended || document.hidden) return;
    var request = inFlight = new AbortController();
    var late = false;
    var hung = setTimeout(function () { late = true; request.abort(); }, HUNG_MS);
    function over() {
      clearTimeout(hung);
      inFlight = null;
      var soon = again;
      again = false;
      return soon;
    }
    window.MM.api.get(URL, { signal: request.signal }).then(function (data) {
      var soon = over();
      if (!take(data)) { failed(null); return; }
      failures = 0;
      tell();
      wait(soon ? SOON_MS : busy() ? BUSY_MS : IDLE_MS);
    }, function (err) {
      var soon = over();
      if (!request.signal.aborted || late) failed(err);
      else if (soon) wait(0); // given up by a tab that was hidden, and is shown again
    });
  }

  /* The Watch page comes with the answer it was drawn from. */
  function drawn() {
    var island = document.getElementById('watch-page-data');
    try { return JSON.parse(island.textContent).downloading; } catch (e) { return null; }
  }

  function start() {
    started = true;
    if (take(drawn())) wait(busy() ? BUSY_MS : IDLE_MS);
    else wait(0);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        wait(0);
        if (inFlight) inFlight.abort();
      } else if (inFlight) again = true;
      else wait(0);
    });
    ['mm:title-changed', 'mediaman:poll:now'].forEach(function (name) {
      document.addEventListener(name, now);
    });
  }

  function now() {
    eagerUntil = Date.now() + EAGER_MS;
    if (inFlight) again = true;
    else if (started) wait(SOON_MS);
  }

  function subscribe(fn) {
    readers.push(fn);
    if (!started) start();
    if (state) fn(state);
    return function () {
      readers = readers.filter(function (other) { return other !== fn; });
    };
  }

  window.MM = window.MM || {};
  window.MM.downloading = {
    subscribe: subscribe,
    last: function () { return state; },
    now: now
  };
})();
