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
 * All values flow in via data-* attributes parsed as JSON; nothing is
 * interpolated server-side into JavaScript.
 */
(function () {
  'use strict';

  var _scanElapsed = 0;

  function updateScanStatus(msg) {
    var el = document.getElementById('scan-status');
    if (!el) return;
    el.textContent = '';
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

  function pollScanStatus(btn) {
    setTimeout(function check() {
      _scanElapsed += 3;
      MM.api.get('/api/scan/status')
        .then(function (data) {
          if (data.running) {
            updateScanStatus('Scanning libraries — ' + fmtElapsed(_scanElapsed));
            setTimeout(check, 3000);
          } else {
            btn.classList.remove('scan-running');
            btn.classList.add('is-success');
            btn.textContent = 'Scan complete ✓';
            var statusEl = document.getElementById('scan-status');
            if (statusEl) {
              statusEl.textContent = 'Finished in ' + fmtElapsed(_scanElapsed) + ' — reloading…';
              statusEl.classList.add('scan-status--success');
            }
            setTimeout(function () { window.location.reload(); }, 1500);
          }
        })
        .catch(function () { /* keep silent on poll failure. */ });
    }, 3000);
  }

  function triggerScan(btn) {
    btn.disabled = true;
    btn.classList.add('scan-running');
    btn.textContent = 'Scanning';
    _scanElapsed = 0;
    updateScanStatus('Starting scan…');
    MM.api.post('/api/scan/trigger')
      .then(function (data) {
        if (data.status === 'already_running') updateScanStatus('Scan already in progress');
        pollScanStatus(btn);
      })
      .catch(function () {
        btn.textContent = 'Scan failed';
        btn.classList.remove('scan-running');
        btn.classList.add('is-error');
        btn.disabled = false;
        updateScanStatus('');
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

    btn.disabled = true;
    btn.textContent = 'Adding…';
    MM.api.post('/api/media/redownload', payload)
      .then(function () {
        btn.textContent = 'Queued ✓';
        btn.classList.add('is-success');
      })
      .catch(function (err) {
        if (err instanceof MM.api.APIError) {
          var msg = err.message || 'Failed';
          btn.textContent = msg.length < 30 ? msg : 'Failed';
          btn.classList.add('is-error');
          btn.disabled = false;
        } else {
          btn.textContent = 'Error';
          btn.classList.add('is-error');
          btn.disabled = false;
        }
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
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Clearing…';
    MM.api.post('/api/scan/clear-scheduled')
      .then(function (data) {
        var text = 'Cleared ' + data.cleared;
        if (data.already_deleting) {
          text += '; ' + data.already_deleting +
            (data.already_deleting === 1 ? ' was' : ' were') + ' already being deleted';
        }
        btn.textContent = text;
        btn.classList.add('is-success');
        setTimeout(function () { window.location.reload(); }, data.already_deleting ? 4000 : 1000);
      })
      .catch(function (err) {
        var msg = (err && err.message) || 'Try again.';
        window.UIFeedback.error("Couldn't clear scheduled deletions. " + msg);
        btn.disabled = false;
        btn.textContent = label;
      });
  }

  /* Submit a keep request from a dashboard tile. The library.html keep-dialog
     flow is too heavy for the dashboard scheduled view, so we just submit
     directly with the chosen duration. */
  function dashSubmitKeep(btn, duration) {
    var wrapper = btn.closest('.keep-wrapper');
    if (!wrapper) return;
    var mediaId = wrapper.dataset.id;
    MM.api.postForm('/api/media/' + encodeURIComponent(mediaId) + '/keep',
      { duration: duration })
      .then(function (data) {
        /* Saved, but the item was already being deleted: say so before reloading. */
        if (data && data.deletion_running && data.message) {
          window.UIFeedback.toast(data.message, { duration: 8000 });
          setTimeout(function () { window.location.reload(); }, 8000);
          return;
        }
        window.location.reload();
      })
      .catch(function (err) {
        var msg = (err && err.message) || 'Try again.';
        window.UIFeedback.error("Couldn't save keep. " + msg);
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
