/**
 * core/reauth.js — centred password re-authentication modal and a
 * request wrapper that opens it on demand.
 *
 * Public surface (global MM.reauth):
 *
 *   MM.reauth.openModal({ onSubmit, onCancel })
 *       Open the centred "Re-authenticate to save" modal. onSubmit(pw)
 *       returns Promise<{ok, error?}>; resolving ok=true closes the
 *       modal, ok=false surfaces the error inline and keeps it open.
 *       onCancel runs on Cancel / Escape / backdrop click.
 *
 *   MM.reauth.run(requestFn)
 *       Call requestFn() and return its promise. If it rejects with an
 *       APIError where status === 403 and err.data.reauth_required ===
 *       true, open the modal; on a successful POST /api/auth/reauth,
 *       retry requestFn() once and resolve with the retry result. If
 *       the user cancels the modal, reject with an APIError carrying
 *       error code 'reauth_cancelled' so callers can distinguish
 *       "backed out" from "request failed".
 *
 * Depends on:
 *   MM.api          (core/api.js — APIError type, fetch wrapper)
 *
 * Load after core/api.js, before any consumer (settings/savebar.js,
 * settings/users.js).
 */
(function () {
  'use strict';

  window.MM = window.MM || {};

  function openModal(opts) {
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
    title.textContent = opts.title || 'Re-authenticate to save';
    sheet.appendChild(title);

    var desc = document.createElement('p');
    desc.className = 'reauth-desc';
    desc.textContent = opts.descText ||
      'mediaman requires a recent password confirmation before changing credentials or other sensitive options.';
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
    confirmBtn.textContent = opts.confirmLabel || 'Confirm & save';
    actions.appendChild(confirmBtn);
    sheet.appendChild(actions);

    backdrop.appendChild(sheet);
    document.body.appendChild(backdrop);
    setTimeout(function () { input.focus(); }, 0);

    var closed = false;
    function close(viaCancel) {
      if (closed) return;
      closed = true;
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
          closed = true;
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

  function isReauthRequired(err) {
    return err &&
      err.name === 'APIError' &&
      err.status === 403 &&
      err.data &&
      err.data.reauth_required === true;
  }

  function run(requestFn) {
    return new Promise(function (resolve, reject) {
      function attempt() {
        Promise.resolve(requestFn()).then(resolve, function (err) {
          if (!isReauthRequired(err)) { reject(err); return; }
          openModal({
            onSubmit: function (pw) {
              return MM.api.post('/api/auth/reauth', { password: pw })
                .then(function () { attempt(); return { ok: true }; })
                .catch(function (reauthErr) {
                  return {
                    ok: false,
                    error: (reauthErr && reauthErr.message) || 'Reauth failed',
                  };
                });
            },
            onCancel: function () {
              reject(new MM.api.APIError(
                'reauth_cancelled',
                'Cancelled',
                0,
                null,
                {},
              ));
            },
          });
        });
      }
      attempt();
    });
  }

  MM.reauth = {
    openModal: openModal,
    run: run,
  };
})();
