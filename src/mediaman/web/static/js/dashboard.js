/**
 * Dashboard page — client-side glue.
 *
 * Lives in an external file so the page-level CSP needs no
 * 'unsafe-inline'. The file is self-contained (no imports).
 *
 * Wires up:
 *   - Scan trigger button (data-action="trigger-scan")
 *   - Re-download buttons   (data-action="redownload")
 *   - Clear-scheduled button (data-action="clear-scheduled")
 *   - Per-tile keep buttons  (data-action="keep-tile" + data-duration)
 *
 * Nothing reloads the page: the [data-fragment] parts are re-read from the
 * server and swapped in place.
 *
 * All values flow in via data-* attributes parsed as JSON; nothing is
 * interpolated server-side into JavaScript.
 */
(function () {
  'use strict';

  var _scanElapsed = 0;
  var _scanStart = 0;

  /* Delays after a failed status poll, then every 10s; stop after 5 in a row. */
  var POLL_RETRY_MS = [2000, 4000, 8000, 10000];
  var POLL_MAX_FAILS = 5;
  var POLL_MAX_MS = 10 * 60 * 1000;
  /* A hung status request is aborted and counts as one failed poll. Polls give
     up after POLL_MAX_FAILS in a row or POLL_MAX_MS of unbroken failure. */
  var POLL_TIMEOUT_MS = 10000;

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* Keeps in flight: a page refresh waits for the last one, so a tile whose
     request has not finished is not put back by an earlier answer. */
  var keepsInFlight = 0;

  /* Re-read the page and swap in the parts that change with a scan, a clear or
     a keep, so nothing reloads and the scroll position stays put. Resolves true
     when the parts were swapped. */
  function refreshFragments() {
    return fetch(window.location.pathname, {
      credentials: 'same-origin',
      headers: { Accept: 'text/html' }
    }).then(function (res) {
      if (!res.ok) throw new Error('refresh failed');
      return res.text();
    }).then(function (html) {
      if (keepsInFlight > 0) return false;
      var doc = new DOMParser().parseFromString(html, 'text/html');
      MM.dom.qa('[data-fragment]').forEach(function (cur) {
        var fresh = doc.querySelector('[data-fragment="' + cur.dataset.fragment + '"]');
        if (!fresh) return;
        var hadFocus = cur.contains(document.activeElement);
        var node = document.importNode(fresh, true);
        cur.replaceWith(node);
        if (hadFocus) {
          var target = node.querySelector('h2');
          if (target) { target.tabIndex = -1; target.focus(); }
        }
      });
      return true;
    });
  }

  function updateScanStatus(msg) {
    var el = document.getElementById('scan-status');
    if (!el) return;
    el.textContent = '';
    el.classList.remove('scan-status--error', 'scan-status--success');
    if (!msg) return;
    el.appendChild(document.createTextNode(msg + ' '));
    var dots = document.createElement('span');
    dots.className = 'scan-dots';
    for (var i = 0; i < 3; i++) {
      var d = document.createElement('span');
      d.textContent = '·';
      dots.appendChild(d);
    }
    el.appendChild(dots);
  }

  function fmtElapsed(sec) {
    return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  }

  /* The button was disabled for the scan, so focus fell to the body; say why it
     stopped in a toast too, since #scan-status is not a live region. */
  function scanStopped(btn, reason, label) {
    btn.classList.remove('scan-running');
    btn.textContent = label || 'Scan now';
    btn.disabled = false;
    if (window.UIFeedback) window.UIFeedback.error(reason);
    if (MM.dom.focusStillIn(btn)) btn.focus();
  }

  function pollScanStatus(btn) {
    var fails = 0;
    var firstFailAt = 0;
    setTimeout(function check() {
      _scanElapsed = Math.round((Date.now() - _scanStart) / 1000);
      var ctrl = new AbortController();
      var abortTimer = setTimeout(function () { ctrl.abort(); }, POLL_TIMEOUT_MS);
      MM.api.get('/api/scan/status', { signal: ctrl.signal })
        .then(function (data) {
          fails = 0;
          firstFailAt = 0;
          if (data.running) {
            updateScanStatus('Scanning libraries — ' + fmtElapsed(_scanElapsed));
            setTimeout(check, 3000);
          } else if (data.status === 'skipped') {
            var skipReason = !data.reason ? 'Scan skipped' : 'Scan skipped: ' +
              (data.reason === 'Plex not configured' ? 'Plex is not configured' : data.reason);
            scanStopped(btn, skipReason);
            var skipEl = document.getElementById('scan-status');
            if (skipEl) {
              skipEl.classList.remove('scan-status--error', 'scan-status--success');
              skipEl.textContent = skipReason + '. ';
              var link = document.createElement('a');
              link.href = '/settings';
              link.textContent = 'Open Settings';
              skipEl.appendChild(link);
            }
          } else if (data.status === 'error') {
            var errReason = 'Scan failed: ' + (data.reason || 'unknown error');
            scanStopped(btn, errReason, 'Retry scan');
            var errEl = document.getElementById('scan-status');
            if (errEl) {
              errEl.classList.remove('scan-status--success');
              errEl.classList.add('scan-status--error');
              errEl.textContent = errReason;
            }
          } else {
            btn.classList.remove('scan-running');
            btn.classList.add('is-success');
            btn.textContent = 'Scan complete ✓';
            var statusEl = document.getElementById('scan-status');
            var took = 'Finished in ' + fmtElapsed(_scanElapsed);
            if (statusEl) {
              statusEl.textContent = took;
              statusEl.classList.add('scan-status--success');
            }
            refreshFragments().catch(function () {
              if (statusEl) statusEl.textContent = took + '. Refresh the page to see the results.';
            }).then(function () {
              setTimeout(function () {
                btn.classList.remove('is-success');
                btn.textContent = 'Scan now';
                btn.disabled = false;
                if (MM.dom.focusStillIn(btn)) btn.focus();
              }, 2000);
            });
          }
        })
        .catch(function () {
          fails += 1;
          if (!firstFailAt) firstFailAt = Date.now();
          if (fails >= POLL_MAX_FAILS || Date.now() - firstFailAt >= POLL_MAX_MS) {
            var lostReason = 'Lost track of the scan. Refresh to check its status.';
            scanStopped(btn, lostReason, 'Retry scan');
            var lostEl = document.getElementById('scan-status');
            if (lostEl) {
              lostEl.classList.remove('scan-status--success');
              lostEl.classList.add('scan-status--error');
              lostEl.textContent = lostReason;
            }
            return;
          }
          setTimeout(check, POLL_RETRY_MS[Math.min(fails, POLL_RETRY_MS.length) - 1]);
        })
        .finally(function () { clearTimeout(abortTimer); });
    }, 3000);
  }

  function triggerScan(btn) {
    btn.disabled = true;
    btn.classList.remove('is-error', 'is-success');
    btn.classList.add('scan-running');
    btn.textContent = 'Scanning';
    _scanElapsed = 0;
    _scanStart = Date.now();
    updateScanStatus('Starting scan…');
    MM.api.post('/api/scan/trigger')
      .then(function (data) {
        if (data.status === 'already_running') updateScanStatus('Scan already in progress');
        pollScanStatus(btn);
      })
      .catch(function (err) {
        var reason = "Couldn't start the scan. " + MM.api.messageOf(err, 'Try again.');
        btn.textContent = 'Retry scan';
        btn.classList.remove('scan-running');
        btn.classList.add('is-error');
        btn.disabled = false;
        updateScanStatus('');
        var statusEl = document.getElementById('scan-status');
        if (statusEl) {
          statusEl.textContent = reason;
          statusEl.classList.add('scan-status--error');
        }
        if (window.UIFeedback) window.UIFeedback.error(reason);
        /* The button was disabled during the request, so focus fell to the body. */
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  /* Re-download a deleted item with the ids stored when it was deleted. */
  function redownload(btn) {
    var payload;
    try { payload = JSON.parse(btn.dataset.redownload); } catch (_) { payload = null; }

    if (!payload) {
      if (window.UIFeedback) {
        window.UIFeedback.error('No identifier available — cannot re-download this item.');
      }
      return;
    }

    if (btn.getAttribute('aria-disabled') === 'true') return;
    btn.disabled = true;
    btn.classList.remove('is-error', 'is-success');
    btn.textContent = 'Adding…';
    MM.api.post('/api/media/redownload', payload)
      .then(function () {
        /* aria-disabled, not disabled, so a focused button keeps focus. */
        btn.setAttribute('aria-disabled', 'true');
        btn.disabled = false;
        btn.textContent = 'Queued ✓';
        btn.classList.add('is-success');
        if (MM.dom.focusStillIn(btn)) btn.focus();
      })
      .catch(function (err) {
        var msg = MM.api.messageOf(err, '');
        btn.textContent = msg && msg.length < 30 ? msg : 'Failed';
        if (window.UIFeedback) {
          window.UIFeedback.error("Couldn't re-download. " + MM.api.messageOf(err, 'Try again.'));
        }
        btn.classList.add('is-error');
        btn.disabled = false;
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  async function clearScheduled(btn) {
    if (!window.UIFeedback) return;
    var proceed = await window.UIFeedback.confirm({
      title: 'Clear all scheduled deletions?',
      body: 'Nothing is deleted now. Items will be re-evaluated on the next scan and may be rescheduled.',
      confirmLabel: 'Clear all',
      confirmVariant: 'danger'
    });
    if (!proceed) return;
    /* The label stays as it is so the button does not change width. */
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    MM.api.post('/api/scan/clear-scheduled')
      .then(function (data) {
        var text = 'Cleared ' + data.cleared + ' scheduled deletion' + (data.cleared === 1 ? '' : 's');
        if (data.already_deleting) {
          text += '; ' + data.already_deleting +
            (data.already_deleting === 1 ? ' was' : ' were') + ' already being deleted';
        }
        window.UIFeedback.toast(text, { variant: 'success', duration: data.already_deleting ? 8000 : undefined });
        return refreshFragments().catch(function () {
          window.UIFeedback.error('Cleared, but the page could not update. Refresh to see the list.');
        });
      }, function (err) {
        window.UIFeedback.error("Couldn't clear scheduled deletions. " + MM.api.messageOf(err, 'Try again.'));
      })
      .then(function () {
        if (!btn.isConnected) return;
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  /* Submit a keep request from a dashboard tile. The library.html keep-dialog
     flow is too heavy for the dashboard scheduled view, so we just submit
     directly with the chosen duration. */
  /* Shrink a tile away and take it off the page. */
  function removeTile(tile) {
    return new Promise(function (resolve) {
      if (reduceMotion() || !tile.animate) { tile.remove(); resolve(); return; }
      tile.style.overflow = 'hidden';
      var anim = tile.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.96)' }],
        { duration: 200, easing: 'ease-out', fill: 'forwards' });
      anim.onfinish = function () { tile.remove(); resolve(); };
    });
  }

  function dashSubmitKeep(btn, duration) {
    var wrapper = btn.closest('.keep-wrapper');
    if (!wrapper) return;
    /* A second click while the request runs sends nothing. */
    if (wrapper.dataset.keepBusy === '1') return;
    wrapper.dataset.keepBusy = '1';
    wrapper.setAttribute('aria-busy', 'true');
    var tile = wrapper.closest('.tile');
    if (tile) { tile.style.transition = 'opacity .15s ease-out'; tile.style.opacity = '.6'; }
    if (window.SnoozeMenu) window.SnoozeMenu.closeAll(null);
    keepsInFlight += 1;
    var mediaId = wrapper.dataset.id;
    MM.api.postForm('/api/media/' + encodeURIComponent(mediaId) + '/keep',
      { duration: duration })
      .then(function (data) {
        keepsInFlight -= 1;
        /* Saved, but the item was already being deleted: say so. */
        if (data && data.deletion_running && data.message) {
          window.UIFeedback.toast(data.message, { duration: 8000 });
        } else {
          window.UIFeedback.success(duration === 'forever' ? 'Kept forever' : 'Kept for ' + duration);
        }
        var grid = tile && tile.parentNode;
        var next = tile && (tile.nextElementSibling || tile.previousElementSibling);
        var hadFocus = tile && tile.contains(document.activeElement);
        return (tile ? removeTile(tile) : Promise.resolve()).then(function () {
          if (hadFocus) {
            var go = next && next.querySelector('.btn');
            if (!go && grid) { go = grid.closest('[data-fragment]'); go = go && go.querySelector('h2'); }
            if (go) { if (!go.matches('button, a')) go.tabIndex = -1; go.focus(); }
          }
          return refreshFragments();
        }).catch(function () {});
      }, function (err) {
        keepsInFlight -= 1;
        delete wrapper.dataset.keepBusy;
        wrapper.removeAttribute('aria-busy');
        if (tile) tile.style.opacity = '';
        var msg = MM.api.messageOf(err, 'Try again.');
        window.UIFeedback.error("Couldn't save keep. " + msg);
        var trigger = wrapper.querySelector('.keep-trigger, .btn-sm-caret');
        if (trigger && MM.dom.focusStillIn(wrapper)) trigger.focus();
      });
  }

  /* Single delegated click handler — every action is data-action driven. */
  document.addEventListener('click', function (e) {
    var scanBtn = e.target.closest('[data-action="trigger-scan"]');
    if (scanBtn) { triggerScan(scanBtn); return; }

    var redlBtn = e.target.closest('[data-action="redownload"]');
    if (redlBtn) { redownload(redlBtn); return; }

    var clearBtn = e.target.closest('[data-action="clear-scheduled"]');
    if (clearBtn) { clearScheduled(clearBtn); return; }

    var keepBtn = e.target.closest('[data-action="keep-tile"]');
    if (keepBtn) {
      var duration = keepBtn.dataset.duration || '30 days';
      dashSubmitKeep(keepBtn, duration);
      return;
    }
  });
})();
