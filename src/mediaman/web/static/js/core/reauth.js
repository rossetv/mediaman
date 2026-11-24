/**
 * core/reauth.js — centred password re-authentication modal and a
 * request wrapper that opens it on demand.
 *
 * Public surface (global MM.reauth):
 *
 *   MM.reauth.openModal({ onSubmit, onCancel, title, descText, confirmLabel })
 *       Open the centred modal, "Re-authenticate to save" unless worded otherwise. onSubmit(pw)
 *       returns Promise<{ok, error?}>; resolving ok=true closes the
 *       modal, ok=false surfaces the error inline and keeps it open.
 *       onCancel runs on Cancel / Escape / backdrop click.
 *
 *   MM.reauth.run(requestFn, words)
 *       Call requestFn() and return its promise. If it rejects with an
 *       APIError where status === 403 and err.data.reauth_required ===
 *       true, open the modal; on a successful POST /api/auth/reauth,
 *       retry requestFn() once and resolve with the retry result. If
 *       the user cancels the modal, reject with an APIError carrying
 *       error code 'reauth_cancelled' so callers can distinguish
 *       "backed out" from "request failed". `words` may hold the modal's
 *       title, descText and confirmLabel, for what is not a save.
 *
 * Depends on:
 *   MM.api          (core/api.js — APIError type, fetch wrapper)
 *   MM.dom          (core/dom.js — holdPage)
 *   MM.modal        (core/modal.js — Escape, backdrop click and the fade out)
 *
 * Load after core/api.js, before any consumer (settings/savebar.js,
 * settings/users.js).
 */
(function () {
  'use strict';

  window.MM = window.MM || {};

  /* Closes the dialog that is open, so a second one cancels the first instead of orphaning it. */
  var closeOpen = null;

  function openModal(opts) {
    opts = opts || {};
    if (typeof opts.onSubmit !== 'function') {
      throw new TypeError('MM.reauth.openModal: opts.onSubmit must be a function');
    }

    if (closeOpen) closeOpen();
    /* The dialog does not trap focus, so on close it hands it back to what had it. */
    var opener = document.activeElement;
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
    desc.id = 'reauth-desc';
    sheet.setAttribute('aria-describedby', 'reauth-desc');
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
    msg.setAttribute('aria-live', 'polite');
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
    var letGo = MM.dom.holdPage();
    document.body.appendChild(backdrop);
    setTimeout(function () { input.focus(); }, 0);

    /* The shared modal owns Escape, backdrop clicks and the fade out; its
       onClose runs for every route, so a close that is not a success is a cancel. */
    var done = false;
    var closed = false;
    var modal = MM.modal.setupDetail(backdrop, {
      manageBodyOverflow: false, /* _ui_feedback.css holds the page */
      useModalA11y: false,
      onClose: function () {
        if (closed) return;
        closed = true;
        input.value = '';
        letGo();
        if (opener && opener !== document.body && opener.focus) opener.focus();
        /* Safari scrolls the page to its end when the field that has the keyboard is removed. */
        if (document.activeElement === input) input.blur();
        if (closeOpen === modal.close) closeOpen = null;
        /* The exit is 150 ms and the modal gives up waiting for it at 250 ms; 400 is past both. */
        setTimeout(function () {
          if (document.activeElement === input) input.blur();
          backdrop.remove();
        }, 400);
        if (!done) {
          done = true;
          if (typeof opts.onCancel === 'function') opts.onCancel();
        }
      },
    });
    modal.open();
    closeOpen = modal.close;
    cancelBtn.addEventListener('click', modal.close);

    /* The line is always laid out; showing it only fades it, so nothing moves.
       The old words stay until the fade has gone, so it never empties mid-fade. */
    var clearTimer = null;
    function say(text) {
      clearTimeout(clearTimer);
      if (text) {
        msg.textContent = text;
        msg.classList.add('is-shown');
      } else {
        msg.classList.remove('is-shown');
        clearTimer = setTimeout(function () { msg.textContent = ''; }, 200);
      }
    }

    function submit() {
      if (confirmBtn.disabled) return;
      var pw = input.value;
      if (!pw) { say('Password required.'); return; }
      confirmBtn.disabled = true;
      say('');
      Promise.resolve(opts.onSubmit(pw)).then(function (res) {
        /* Closed while the password was checked: the answer is for nobody. */
        if (closed) return;
        if (res && res.ok) {
          done = true;
          modal.close();
        } else {
          confirmBtn.disabled = false;
          say((res && res.error) || 'Failed');
          input.focus();
          input.select();
        }
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

  function run(requestFn, words) {
    words = words || {};
    return new Promise(function (resolve, reject) {
      var cancelled = false;
      function attempt() {
        Promise.resolve(requestFn()).then(resolve, function (err) {
          if (!isReauthRequired(err)) { reject(err); return; }
          openModal({
            title: words.title,
            descText: words.descText,
            confirmLabel: words.confirmLabel,
            onSubmit: function (pw) {
              return MM.api.post('/api/auth/reauth', { password: pw })
                .then(function () {
                  /* The password may be accepted after the dialog was closed; that must not run the action. */
                  if (!cancelled) attempt();
                  return { ok: true };
                })
                .catch(function (reauthErr) {
                  return {
                    ok: false,
                    error: MM.api.messageOf(reauthErr, 'Reauth failed'),
                  };
                });
            },
            onCancel: function () {
              cancelled = true;
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
