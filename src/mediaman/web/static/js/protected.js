/**
 * Protected (kept items) page — client-side glue.
 *
 * Extracted from protected.html so the inline <script> can be removed
 * and the page-level CSP can drop 'unsafe-inline' once every template
 * has migrated. The file is self-contained (no imports).
 *
 * Finding 17: event delegation replaces inline onclick handlers. Values
 * are read from data-* attributes parsed via JSON to avoid any string
 * interpolation into JavaScript.
 */
(function () {
  'use strict';

  async function removeKeep(mediaItemId) {
    if (!window.UIFeedback) return;
    var ok = await window.UIFeedback.confirm({
      title: 'Remove keep?',
      body: 'Without a keep, this item may be scheduled for deletion on the next scan.',
      confirmLabel: 'Remove keep',
      confirmVariant: 'danger'
    });
    if (!ok) return;
    fetch('/api/media/' + encodeURIComponent(mediaItemId) + '/unprotect', { method: 'POST' })
      .then(function (r) {
        if (r.ok) {
          window.location.reload();
        } else {
          r.json().then(function (d) {
            window.UIFeedback.error("Couldn't remove keep. " + (d.error || 'Try again.'));
          });
        }
      })
      .catch(function () { window.UIFeedback.error('Network error. Try again.'); });
  }

  async function removeShowKeep(showRatingKey) {
    if (!window.UIFeedback) return;
    var ok = await window.UIFeedback.confirm({
      title: 'Stop keeping this show?',
      body: 'Season-level keeps stay in place. New seasons will no longer be automatically protected.',
      confirmLabel: 'Stop keeping show',
      confirmVariant: 'danger'
    });
    if (!ok) return;
    fetch('/api/show/' + encodeURIComponent(showRatingKey) + '/remove', { method: 'POST' })
      .then(function (r) {
        if (r.ok) {
          window.location.reload();
        } else {
          r.json().then(function (d) {
            window.UIFeedback.error("Couldn't stop keeping show. " + (d.error || 'Try again.'));
          });
        }
      })
      .catch(function () { window.UIFeedback.error('Network error. Try again.'); });
  }

  document.addEventListener('click', function (e) {
    var removeKeepBtn = e.target.closest('[data-action="remove-keep"]');
    if (removeKeepBtn) {
      var mid;
      try { mid = JSON.parse(removeKeepBtn.dataset.mediaId); } catch (_) { return; }
      removeKeep(mid);
      return;
    }
    var removeShowBtn = e.target.closest('[data-action="remove-show-keep"]');
    if (removeShowBtn) {
      var rk;
      try { rk = JSON.parse(removeShowBtn.dataset.showRk); } catch (_) { return; }
      removeShowKeep(rk);
    }
  });
})();
