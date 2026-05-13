/**
 * recommended/refresh.js — manual refresh button + 24 h cooldown.
 *
 * Wires the "Fetch new suggestions" button to POST /api/recommended/refresh
 * and the page-load handshake against /api/recommended/refresh/status. The
 * server is the source of truth for cooldown; when a 429 comes back this
 * file swaps the button for a live countdown.
 *
 * Exposes:
 *   MM.recommended.refresh.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.recommended = MM.recommended || {};

  function formatCountdown(ms) {
    if (ms <= 0) return '0m';
    var totalMin = Math.ceil(ms / 60000);
    if (totalMin >= 60) {
      var h = Math.floor(totalMin / 60);
      var m = totalMin % 60;
      return m > 0 ? h + 'h ' + m + 'm' : h + 'h';
    }
    return totalMin + 'm';
  }

  function swapButtonForCooldown(nextAt) {
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
    tickCooldownClock();
  }

  var cooldownInterval = null;
  function tickCooldownClock() {
    var span = document.getElementById('refresh-cooldown');
    var clock = span && span.querySelector('[data-cooldown-clock]');
    if (!clock || !span.dataset.nextAt) return;
    var update = function () {
      var ms = new Date(span.dataset.nextAt).getTime() - Date.now();
      if (ms <= 0) {
        // Cooldown expired — keep the line hidden and let the next page
        // load render the button.
        span.textContent = 'Refresh available — reload to fetch new suggestions';
        if (cooldownInterval) { clearInterval(cooldownInterval); cooldownInterval = null; }
        return;
      }
      clock.textContent = formatCountdown(ms);
    };
    update();
    if (cooldownInterval) clearInterval(cooldownInterval);
    cooldownInterval = setInterval(update, 30000);
  }

  function startRefreshPolling(btn) {
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    btn.style.opacity = '0.6';

    // Defence: a string of consecutive "idle" responses means the worker
    // died without setting a result (lease expired and the thread was
    // somehow killed before its finally block ran). Without this, the
    // poll loop would spin silently forever. Two ticks of "idle" before
    // we abort, so a single off-by-one observation right after the
    // worker finishes doesn't kill the loop prematurely.
    var idleStreak = 0;
    var IDLE_ABORT_THRESHOLD = 2;

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
              resetButton(btn);
              showError((st.result && st.result.error) || null);
            }
            return;
          }
          if (st.status === 'idle') {
            idleStreak += 1;
            if (idleStreak >= IDLE_ABORT_THRESHOLD) {
              clearInterval(poll);
              resetButton(btn);
              showError('The refresh worker stopped responding. Check the server logs and try again.');
            }
            return;
          }
          // Any other status (e.g. "running") — keep polling and reset
          // the idle streak so a transient "idle" between live ticks
          // doesn't accumulate.
          idleStreak = 0;
        })
        .catch(function () {});
    }, 3000);
  }

  function resetButton(btn) {
    btn.textContent = 'Fetch new suggestions';
    btn.style.opacity = '1';
    btn.disabled = false;
  }

  function showError(message) {
    var errEl = document.getElementById('refresh-error');
    if (!errEl) return;
    errEl.textContent = message || "Couldn't refresh recommendations.";
    errEl.style.display = 'block';
  }

  function refreshRecommendations(btn) {
    btn.disabled = true;
    btn.textContent = 'Refreshing…';
    btn.style.opacity = '0.6';
    document.getElementById('refresh-error').style.display = 'none';

    MM.api.post('/api/recommended/refresh')
      .then(function (data) {
        // The server returns {status: "started" | "already_running"} on the
        // success path. Any {ok: false} envelope is thrown as APIError by
        // MM.api and surfaces in the .catch below — never here.
        if (data.status === 'started' || data.status === 'already_running') {
          startRefreshPolling(btn);
          return;
        }
        resetButton(btn);
        showError(data && data.error);
      })
      .catch(function (err) {
        if (err.status === 429) {
          // Server-side cooldown — hide button + start the countdown.
          var nextAt = (err.data && err.data.next_available_at) ||
            new Date(Date.now() + ((err.data && err.data.cooldown_seconds) || 0) * 1000).toISOString();
          swapButtonForCooldown(nextAt);
          showError(err.message || 'Refresh is on cooldown.');
          return;
        }
        // Every other failure (Plex not configured, network error, 5xx, etc.)
        // must show the user *why* the click did nothing — otherwise the
        // button silently resets and the page looks broken.
        resetButton(btn);
        showError(err && err.message);
      });
  }

  // Guard so init() is safe to call more than once — refresh.js now
  // self-initialises (so a JS error in recommended.js can't strand the
  // button), but recommended.js still calls init() at the bottom of its
  // own IIFE for backwards-compat. Without this guard we'd attach the
  // click listener twice and fire two POSTs per click.
  var _initialised = false;

  function init() {
    if (_initialised) return;
    _initialised = true;

    var refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', function () { refreshRecommendations(refreshBtn); });
    }

    // On page load: if a refresh is already running, attach to it. If the
    // server reports an active cooldown but the button is somehow visible
    // (race during navigation), swap it for the countdown.
    MM.api.get('/api/recommended/refresh/status')
      .then(function (st) {
        var btn = document.getElementById('refresh-btn');
        if (st.status === 'running' && btn) { startRefreshPolling(btn); return; }
        if (st.manual_refresh_available === false && btn && st.next_available_at) {
          swapButtonForCooldown(st.next_available_at);
        } else {
          tickCooldownClock();
        }
      })
      .catch(function () {});
  }

  MM.recommended.refresh = {
    init: init,
  };

  // Self-init: the script is loaded with ``defer``, so the DOM is parsed
  // by the time this runs. Calling init() here means the refresh button
  // still works even if recommended.js's IIFE throws before its own
  // bootstrap line at the bottom — the button click handler is the
  // critical wiring and must not depend on unrelated modal/JSON setup.
  init();
})();
