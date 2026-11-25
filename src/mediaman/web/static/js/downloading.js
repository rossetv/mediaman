/* What is downloading, asked for once per tab for whoever shows it: the
 * Watch page's row and the header's ring. On MM.downloading:
 *
 *   subscribe(fn)  fn({ summary, items, outage, lost }) now, when something
 *                  is known, and on every answer; returns what ends it.
 *                  The first one starts the asking.
 *   last()         the same, or null.
 *   now()          ask soon: something was just asked for.
 *   hear(answer), lose()   on the Downloads page, whose own asking brings the
 *                  answer, without its items, so that nothing is asked here.
 *
 * lost: mediaman itself has stopped answering, and the rest is as it last was;
 * signedOut: because nobody is signed in any more.
 */
(function () {
  'use strict';

  var URL = '/api/downloads/strip';
  var BUSY_MS = 5000;
  var IDLE_MS = 30000;
  var HOLD_MS = 4000; // how long the server gives everybody one answer
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
  var age = 0; // how old the answer was as it came
  var started = false;
  var ended = false;
  var island = document.getElementById('downloads-page-data');
  var fed = !!island;

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
    if (fed || ended || document.hidden) return;
    timer = setTimeout(ask, ms);
  }

  function take(data) {
    if (!data || !data.summary || !Array.isArray(data.items)) return false;
    age = data.age_ms || 0;
    state = { summary: data.summary, items: data.items, outage: data.outage || '', lost: false };
    return true;
  }

  /* An older answer misses what has started since it was made. */
  function idle() {
    if (age <= SOON_MS || state.outage) return IDLE_MS;
    return age < HOLD_MS ? Math.max(SOON_MS, HOLD_MS - age) : BUSY_MS;
  }

  function lose(how) {
    if (!state || (state.lost && !how)) return;
    state = Object.assign({}, state, { lost: true }, how);
    tell();
  }

  function hear(data) { if (take(data)) tell(); }

  function failed(err) {
    if (err && (err.status === 401 || err.status === 403)) {
      ended = true;
      lose({ signedOut: true });
      return;
    }
    failures += 1;
    if (failures === LOST_AFTER) lose();
    wait(Math.min(FAIL_MS * Math.pow(2, failures - 1), FAIL_MAX_MS));
  }

  /* Not MM.api: its 401 leads to the login page, which nobody here asked for. */
  function get(signal) {
    return fetch(URL, { credentials: 'same-origin', signal: signal }).then(function (resp) {
      if (!resp.ok) throw Object.assign(new Error('http'), { status: resp.status });
      return resp.json();
    });
  }

  function ask() {
    timer = null;
    if (inFlight || ended) return;
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
    get(request.signal).then(function (data) {
      var soon = over();
      if (!take(data)) { failed(null); return; }
      failures = 0;
      tell();
      wait(soon ? SOON_MS : busy() ? BUSY_MS : idle());
    }, function (err) {
      var soon = over();
      if (!request.signal.aborted || late) failed(err);
      else if (soon) wait(0); // given up by a tab that was hidden, and is shown again
    });
  }

  /* The Watch page and Downloads come with the answer they were drawn from. */
  function drawn() {
    var from = island || document.getElementById('watch-page-data');
    try { return JSON.parse(from.textContent).downloading; } catch (e) { return null; }
  }

  function start() {
    started = true;
    // It may be half a minute old, so it is only what is shown first.
    wait(take(drawn()) ? SOON_MS : 0);
    if (fed) return;
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) {
        wait(0);
        if (inFlight) inFlight.abort();
        return;
      }
      ended = false; // signed in again elsewhere, maybe
      if (inFlight) again = true;
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
    now: now,
    hear: hear,
    lose: function () { lose(); }
  };
})();
