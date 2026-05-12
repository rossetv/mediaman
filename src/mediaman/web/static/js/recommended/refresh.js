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
          startRefreshPolling(btn);
        } else {
          btn.textContent = 'Fetch new suggestions';
          btn.style.opacity = '1';
          btn.disabled = false;
          var errEl2 = document.getElementById('refresh-error');
          errEl2.textContent = data.error || "Couldn't refresh recommendations.";
          errEl2.style.display = 'block';
        }
      })
      .catch(function (err) {
        if (err.status === 429) {
          // Server-side cooldown — hide button + start the countdown.
          var nextAt = (err.data && err.data.next_available_at) ||
            new Date(Date.now() + ((err.data && err.data.cooldown_seconds) || 0) * 1000).toISOString();
          swapButtonForCooldown(nextAt);
          var errEl = document.getElementById('refresh-error');
          errEl.textContent = err.message || 'Refresh is on cooldown.';
          errEl.style.display = 'block';
          return;
        }
        btn.textContent = 'Fetch new suggestions';
        btn.style.opacity = '1';
        btn.disabled = false;
      });
  }

  function init() {
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
})();
