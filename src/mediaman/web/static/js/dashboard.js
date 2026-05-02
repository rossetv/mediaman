/**
 * Dashboard page — client-side glue.
 *
 * Extracted from dashboard.html so the inline <script> can be removed
 * and the page-level CSP can drop 'unsafe-inline' once every template
 * has migrated. The file is self-contained (no imports).
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
      fetch('/api/scan/status')
        .then(function (r) { return r.json(); })
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
        });
    }, 3000);
  }

  function triggerScan(btn) {
    btn.disabled = true;
    btn.classList.add('scan-running');
    btn.textContent = 'Scanning';
    _scanElapsed = 0;
    updateScanStatus('Starting scan…');
    fetch('/api/scan/trigger', { method: 'POST' })
      .then(function (r) { return r.json(); })
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

  /* Re-download a deleted item using stable identifiers from data-*.
     Finding 34: identifiers, never raw titles, drive the API call. */
  function redownload(btn) {
    var title, mediaItemId, mediaType;
    try { title = JSON.parse(btn.dataset.title); } catch (_) { title = ''; }
    try { mediaItemId = JSON.parse(btn.dataset.mediaItemId); } catch (_) { mediaItemId = ''; }
    try { mediaType = JSON.parse(btn.dataset.mediaType); } catch (_) { mediaType = ''; }

    if (!mediaItemId && !title) {
      if (window.UIFeedback) {
        window.UIFeedback.error('No identifier available — cannot re-download this item.');
      }
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Adding…';
    fetch('/api/media/redownload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title, media_item_id: mediaItemId, media_type: mediaType }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          btn.textContent = 'Queued ✓';
          btn.classList.add('is-success');
        } else {
          btn.textContent = data.error && data.error.length < 30 ? data.error : 'Failed';
          btn.classList.add('is-error');
          btn.disabled = false;
        }
      })
      .catch(function () {
        btn.textContent = 'Error';
        btn.classList.add('is-error');
        btn.disabled = false;
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
    btn.disabled = true;
    btn.textContent = 'Clearing…';
    fetch('/api/scan/clear-scheduled', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          btn.textContent = 'Cleared ' + data.cleared;
          btn.classList.add('is-success');
          setTimeout(function () { window.location.reload(); }, 1000);
        }
      });
  }

  /* Submit a keep request from a dashboard tile. The library.html keep-dialog
     flow is too heavy for the dashboard scheduled view, so we just submit
     directly with the chosen duration. */
  function dashSubmitKeep(btn, duration) {
    var wrapper = btn.closest('.keep-wrapper');
    if (!wrapper) return;
    var mediaId = wrapper.dataset.id;
    fetch('/api/media/' + encodeURIComponent(mediaId) + '/keep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ duration: duration }).toString(),
    }).then(function (r) {
      if (r.ok) {
        window.location.reload();
      } else {
        r.json().then(function (d) {
          if (window.UIFeedback) {
            window.UIFeedback.error("Couldn't save keep. " + (d.error || r.status));
          }
        });
      }
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
      var duration = keepBtn.dataset.duration || '30d';
      dashSubmitKeep(keepBtn, duration);
      return;
    }
  });
})();
