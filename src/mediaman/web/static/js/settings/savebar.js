/**
 * settings/savebar.js — sticky savebar, save submit, and reauth modal.
 *
 * Responsibilities:
 *   - Dirty-tracking listeners that toggle `.setg-savebar.on`
 *   - 422 / 403 / 429 response shaping into a one-line summary
 *   - PUT /api/settings submit handler with reauth retry
 *   - Discard-confirm via UIFeedback
 *   - Reauth modal (centred dialog) used by the sensitive-settings gate
 *
 * Cross-module dependencies:
 *   window.UIFeedback (confirm dialog)
 *
 * Exposes:
 *   MM.settings.savebar.init({ getPayload, refreshOverviewHero })
 *   MM.settings.savebar.markDirty()
 *   MM.settings.savebar.markClean()
 *   MM.settings.savebar.openReauthModal(opts)
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

  function openReauthModal(opts) {
    document.querySelectorAll('.modal-backdrop.reauth-backdrop').forEach(function (n) { n.remove(); });

    var backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop reauth-backdrop';
    backdrop.style.alignItems = 'center';

    var sheet = document.createElement('div');
    sheet.className = 'modal-sheet reauth-sheet';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-labelledby', 'reauth-title');

    var title = document.createElement('h2');
    title.className = 'reauth-title';
    title.id = 'reauth-title';
    title.textContent = 'Re-authenticate to save';
    sheet.appendChild(title);

    var desc = document.createElement('p');
    desc.className = 'reauth-desc';
    desc.textContent = 'mediaman requires a recent password confirmation before changing credentials or other sensitive options.';
    sheet.appendChild(desc);

    var field = document.createElement('label');
    field.className = 'reauth-field';
    var fieldLbl = document.createElement('span');
    fieldLbl.className = 'reauth-field-lbl';
    fieldLbl.textContent = 'Your password';
    field.appendChild(fieldLbl);
    var input = document.createElement('input');
    input.className = 'inp';
    input.type = 'password';
    input.autocomplete = 'current-password';
    field.appendChild(input);
    sheet.appendChild(field);

    var actions = document.createElement('div');
    actions.className = 'reauth-actions';
    var msg = document.createElement('span');
    msg.className = 'reauth-msg';
    actions.appendChild(msg);
    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn btn--ghost btn--sm';
    cancelBtn.textContent = 'Cancel';
    actions.appendChild(cancelBtn);
    var confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'btn btn--primary btn--sm';
    confirmBtn.textContent = 'Confirm & save';
    actions.appendChild(confirmBtn);
    sheet.appendChild(actions);

    backdrop.appendChild(sheet);
    document.body.appendChild(backdrop);
    setTimeout(function () { input.focus(); }, 0);

    var cancelled = false;
    function close(viaCancel) {
      if (cancelled) return;
      cancelled = true;
      backdrop.remove();
      document.removeEventListener('keydown', onKey);
      if (viaCancel && typeof opts.onCancel === 'function') opts.onCancel();
    }
    function onKey(e) { if (e.key === 'Escape') close(true); }
    document.addEventListener('keydown', onKey);
    cancelBtn.addEventListener('click', function () { close(true); });
    backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(true); });

    function submit() {
      var pw = input.value;
      if (!pw) { msg.textContent = 'Password required.'; return; }
      confirmBtn.disabled = true;
      msg.textContent = '';
      Promise.resolve(opts.onSubmit(pw)).then(function (res) {
        confirmBtn.disabled = false;
        if (res && res.ok) {
          cancelled = true; // suppress onCancel
          backdrop.remove();
          document.removeEventListener('keydown', onKey);
        } else {
          msg.textContent = (res && res.error) || 'Failed';
          input.focus();
          input.select();
        }
      }).catch(function () {
        confirmBtn.disabled = false;
        msg.textContent = 'Network error. Try again.';
      });
    }
    confirmBtn.addEventListener('click', submit);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') submit();
    });
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
    // payload without duplicating response-bookkeeping.
    function runSave(payload) {
      if (!saveBtn) return;
      saveBtn.disabled = true;
      clearSaveError();
      var orig = 'Save Settings';
      saveBtn.textContent = 'Saving…';
      if (statusEl) statusEl.textContent = '';

      var statusCode = 0;
      fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (r) { statusCode = r.status; return r.json().catch(function () { return {}; }); })
        .then(function (data) {
          var ok = statusCode >= 200 && statusCode < 300 && data.status === 'saved';
          var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });

          if (ok) {
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

          // Sensitive-settings reauth gate. The backend returns 403 with
          // reauth_required: true when the session has no recent reauth
          // ticket. Open the reauth modal; on success, re-fire the same
          // payload so the user keeps their unsaved edits.
          if (statusCode === 403 && data && data.reauth_required) {
            saveBtn.textContent = orig;
            saveBtn.disabled = false;
            if (statusEl) statusEl.textContent = '';
            openReauthModal({
              onSubmit: function (pw) {
                return fetch('/api/auth/reauth', {
                  method: 'POST',
                  credentials: 'same-origin',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ password: pw }),
                }).then(function (r) {
                  var sc = r.status;
                  return r.json().catch(function () { return {}; }).then(function (d) {
                    if (sc >= 200 && sc < 300 && d && d.ok) {
                      runSave(payload);
                      return { ok: true };
                    }
                    return { ok: false, error: (d && d.error) || ('Reauth failed (HTTP ' + sc + ')') };
                  });
                });
              },
              onCancel: function () {
                setSaveError('Save cancelled — re-authentication required.');
              },
            });
            return;
          }

          setSaveError(summariseSaveError(data, statusCode));
          saveBtn.disabled = false;
        })
        .catch(function () {
          setSaveError("Couldn't reach the server — check your connection and try again.");
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
    openReauthModal: openReauthModal,
  };
})();
