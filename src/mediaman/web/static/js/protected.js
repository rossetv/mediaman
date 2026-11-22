/**
 * Protected (kept items) page — client-side glue.
 *
 * Lives in an external file so the page-level CSP needs no
 * 'unsafe-inline'. The file is self-contained (no imports).
 *
 * Event delegation replaces inline onclick handlers. Values
 * are read from data-* attributes parsed via JSON to avoid any string
 * interpolation into JavaScript.
 */
(function () {
  'use strict';

  // Only an API error's message is meant for the reader; anything else is a bug.
  function userMessage(err) {
    return (err instanceof MM.api.APIError && err.message) || 'Try again.';
  }

  async function removeKeep(mediaItemId) {
    if (!window.UIFeedback) return;
    var ok = await window.UIFeedback.confirm({
      title: 'Remove keep?',
      body: 'Without a keep, this item may be scheduled for deletion on the next scan.',
      confirmLabel: 'Remove keep',
      confirmVariant: 'danger'
    });
    if (!ok) return;
    MM.api.post('/api/media/' + encodeURIComponent(mediaItemId) + '/unprotect')
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        window.UIFeedback.error("Couldn't remove keep. " + userMessage(err));
      });
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
    MM.api.post('/api/show/' + encodeURIComponent(showRatingKey) + '/remove')
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        window.UIFeedback.error("Couldn't stop keeping show. " + userMessage(err));
      });
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
