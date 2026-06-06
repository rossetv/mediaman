/**
 * settings/savebar.js — sticky savebar, save submit, and reauth retry.
 *
 * Responsibilities:
 *   - Dirty-tracking listeners that toggle `.setg-savebar.on`
 *   - 422 / 403 / 429 response shaping into a one-line summary
 *   - PUT /api/settings submit handler with reauth retry
 *   - Discard-confirm via UIFeedback
 *
 * Cross-module dependencies:
 *   window.UIFeedback (confirm dialog)
 *   MM.reauth         (core/reauth.js — centred reauth modal)
 *
 * Exposes:
 *   MM.settings.savebar.init({ getPayload, refreshOverviewHero })
 *   MM.settings.savebar.markDirty()
 *   MM.settings.savebar.markClean()
 *
 * `getPayload` is the function general.js exposes as collectSettings();
 * keeping the contract narrow lets the file render unit-friendly without
 * pulling in the rest of the settings page.
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var savebar = null;

  function markDirty() { if (savebar) savebar.classList.add('on'); }
  function markClean() { if (savebar) savebar.classList.remove('on'); }

  // The send-newsletter panel contains ephemeral recipient checkboxes that
  // are not persisted settings, so changes inside it must not mark dirty.
  function isSettingsInput(el) {
    return el.closest('.setg-pg') && !el.closest('#newsletter-send-panel');
  }

  // Turn FastAPI 422 bodies into a one-line human summary.
  function summariseSaveError(data, statusCode) {
    if (data && Array.isArray(data.detail) && data.detail.length) {
      return data.detail.map(function (d) {
        var loc = Array.isArray(d.loc) ? d.loc.filter(function (p) { return p !== 'body'; }) : [];
        var field = loc.length ? loc.join('.') : 'request';
        var msg = (d.msg || 'invalid').replace(/^Value error,\s*/, '');
        return field + ' — ' + msg;
      }).join(' · ');
    }
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && data.error) return data.error;
    if (statusCode === 429) return 'Too many saves — slow down for a moment.';
    return 'Save failed (HTTP ' + (statusCode || '?') + ')';
  }

  function init(opts) {
    var getPayload = (opts && opts.getPayload) || function () { return {}; };
    var refreshOverviewHero = (opts && opts.refreshOverviewHero) || function () {};

    savebar = document.getElementById('setg-savebar');
    var saveBtn   = document.getElementById('btn-save');
    var statusEl  = document.getElementById('save-status');

    document.addEventListener('input', function (e) {
      if (isSettingsInput(e.target)) markDirty();
    }, true);
    document.addEventListener('change', function (e) {
      if (isSettingsInput(e.target)) markDirty();
    }, true);

    function setSaveError(msg) {
      if (savebar) savebar.classList.add('is-error');
      if (statusEl) statusEl.textContent = msg;
      if (saveBtn) {
        saveBtn.classList.remove('btn--primary');
        saveBtn.classList.add('btn--danger');
        saveBtn.textContent = 'Try again';
      }
    }

    function clearSaveError() {
      if (savebar) savebar.classList.remove('is-error');
      if (saveBtn) {
        saveBtn.classList.remove('btn--danger');
        saveBtn.classList.add('btn--primary');
      }
    }

    // runSave is extracted so the reauth retry path can re-fire the same
    // payload without duplicating response-bookkeeping. Routes through
    // MM.reauth.run() so CSRF headers, credentials, and the reauth-modal
    // flow are applied consistently (the hand-rolled fetch + openModal
    // pattern is replaced by the shared wrapper).
    function runSave(payload) {
      if (!saveBtn) return;
      saveBtn.disabled = true;
      clearSaveError();
      var orig = 'Save Settings';
      saveBtn.textContent = 'Saving…';
      if (statusEl) statusEl.textContent = '';

      MM.reauth.run(function () {
        return MM.api.put('/api/settings', payload);
      })
        .then(function (data) {
          var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });

          // MM.api rejects on non-2xx, so reaching here guarantees the save
          // succeeded. Treat any 2xx that lacks data.status==='saved' as a
          // partial success (shouldn't happen, but be defensive).
          if (data.status === 'saved' || data.ok !== false) {
            saveBtn.textContent = 'Saved ✓';
            if (statusEl) {
              statusEl.textContent = ignored.length
                ? 'Saved · ignored unknown: ' + ignored.join(', ')
                : 'Settings saved';
            }
            setTimeout(markClean, 600);
            refreshOverviewHero();
            setTimeout(function () {
              saveBtn.textContent = orig;
              saveBtn.disabled = false;
              if (statusEl && !ignored.length) {
                statusEl.textContent = 'Edits apply to every section at once.';
              }
            }, 2200);
            return;
          }

          // Unexpected envelope — surface as a save error.
          setSaveError(summariseSaveError(data, 200));
          saveBtn.disabled = false;
        })
        .catch(function (err) {
          // MM.reauth.run() rejects with 'reauth_cancelled' when the user
          // dismisses the modal — show a polite message rather than a server
          // error. All other rejections are APIError instances from MM.api.
          if (err && err.error === 'reauth_cancelled') {
            setSaveError('Save cancelled — re-authentication required.');
            saveBtn.disabled = false;
            return;
          }
          if (err && err.status) {
            // Reconstruct a minimal data envelope so summariseSaveError can
            // read detail / error fields that MM.api.APIError already parsed.
            setSaveError(summariseSaveError(err.data || {}, err.status));
          } else {
            setSaveError("Couldn't reach the server — check your connection and try again.");
          }
          if (saveBtn) saveBtn.disabled = false;
        });
    }

    if (saveBtn) saveBtn.addEventListener('click', function () { runSave(getPayload()); });

    // Clear error chrome whenever the user starts typing again.
    document.addEventListener('input', function () {
      if (savebar && savebar.classList.contains('is-error')) clearSaveError();
    }, true);

    var discardBtn = document.getElementById('btn-discard');
    if (discardBtn) discardBtn.addEventListener('click', function () {
      /* TODO(H73): use UIFeedback.confirm instead of window.confirm for consistency. */
      window.UIFeedback.confirm({
        title: 'Discard unsaved changes?',
        body: 'The page will reload and any unsaved settings will be lost.',
        confirmLabel: 'Discard',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (ok) window.location.reload();
      });
    });
  }

  MM.settings.savebar = {
    init: init,
    markDirty: markDirty,
    markClean: markClean,
  };
})();
