/**
 * settings/savebar.js — sticky savebar, save submit, and reauth retry.
 *
 * Responsibilities:
 *   - Dirty-tracking listeners that toggle `.setg-savebar.on`
 *   - 422 / 403 / 429 response shaping into a one-line summary
 *   - PUT /api/settings submit handler with reauth retry
 *   - Discard-confirm via UIFeedback, then the last saved values put back
 *     in place
 *
 * Cross-module dependencies:
 *   window.UIFeedback (confirm dialog)
 *   MM.reauth         (core/reauth.js — centred reauth modal)
 *   MM.api            (core/api.js — told when edits are unsaved)
 *   MM.settings.diskThresholds / MM.settings.toggles (Discard puts their
 *                     state back)
 *   MM.settings.integrations.paintTests (told of every edit, save and discard)
 *
 * Exposes:
 *   MM.settings.savebar.init({ getPayload, refreshOverviewHero })
 *   MM.settings.savebar.markDirty()
 *   MM.settings.savebar.markClean()
 *   MM.settings.savebar.isEdited(el)   — el differs from what was last saved
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

  var onEdit = function () {};

  function markDirty() {
    onEdit();
    paintTests();
  }

  // Each Test button says when it would test other values than those shown.
  function paintTests() {
    if (MM.settings.integrations) MM.settings.integrations.paintTests();
  }
  function markClean() { if (savebar) savebar.classList.remove('on'); }

  // The last settings control the user edited, so focus lost to the hiding
  // bar can go back to where they were working.
  var lastEdited = null;

  // Focus on the body or in the bar is nobody's place; anywhere else is left alone.
  function focusLost() {
    var active = document.activeElement;
    return !active || active === document.body || !!(savebar && savebar.contains(active));
  }

  // The bar hides after a save or a discard. Only a keyboard press restores focus, since a
  // pointer user has no focus to lose and a refocus would scroll them back or
  // raise a phone keyboard. Focus already moved elsewhere is left alone; focus
  // lost to the body or the bar returns to the last edited control, or to the
  // page heading when that cannot take focus.
  function settleAfterHide(fromKeyboard) {
    markClean();
    if (!fromKeyboard || !focusLost()) return;
    if (lastEdited && document.body.contains(lastEdited) && !lastEdited.disabled) {
      lastEdited.focus();
      if (document.activeElement === lastEdited) return;
    }
    var heading = document.querySelector('.setg-pg h1');
    if (!heading) return;
    heading.setAttribute('tabindex', '-1');
    heading.focus();
  }

  // The send-newsletter panel holds ephemeral recipient checkboxes, and the
  // account forms (add user, password, email) and the add-subscriber row have
  // their own buttons. None of them are saved settings, so changes inside
  // them must not mark dirty.
  function isSettingsInput(el) {
    return el.closest('.setg-pg') &&
      !el.closest('#newsletter-send-panel, .inline-form, .sub-add');
  }

  // The values Discard goes back to: those on the page at load, then those of the last save.
  var saved = null;

  function settingsControls() {
    return Array.prototype.filter.call(
      document.querySelectorAll('.setg-pg input[id], .setg-pg select[id], .setg-pg textarea[id]'),
      function (el) { return isSettingsInput(el) && !el.closest('#disk-threshold-rows'); }
    );
  }

  function valueOf(el) {
    return (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value;
  }

  function isEdited(el) {
    return !!saved && el.id in saved.values && saved.values[el.id] !== valueOf(el);
  }

  function snapshot() {
    var values = {};
    settingsControls().forEach(function (el) { values[el.id] = valueOf(el); });
    var disk = MM.settings.diskThresholds ? MM.settings.diskThresholds.state() : null;
    return { values: values, disk: disk };
  }

  // The settings that differ between two snapshots, by the key each is saved under.
  function changedKeys(from, to) {
    var keys = Object.keys(to.values).filter(function (id) { return to.values[id] !== from.values[id]; });
    if (!from.disk || !to.disk) return keys;
    if (from.disk.libs.slice().sort().join() !== to.disk.libs.slice().sort().join()) keys.push('plex_libraries');
    if (MM.settings.diskThresholds.changes(from.disk.disk, to.disk.disk)) keys.push('disk_thresholds');
    return keys;
  }

  function restoreSaved() {
    if (!saved) return;
    settingsControls().forEach(function (el) {
      if (!(el.id in saved.values)) return;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = saved.values[el.id];
      else el.value = saved.values[el.id];
    });
    if (saved.disk && MM.settings.diskThresholds) MM.settings.diskThresholds.restore(saved.disk);
    if (MM.settings.toggles && MM.settings.toggles.sync) MM.settings.toggles.sync();
    paintTests();
  }

  // Plain wording for the codes the settings route sends without a message.
  var SAVE_ERROR_WORDS = {
    ssrf_blocked: 'One of the addresses points somewhere mediaman may not connect to.',
    invalid_url: 'One of the addresses is not a valid http or https URL.',
    url_too_long: 'One of the addresses is too long.',
    too_many_requests: 'Too many saves — slow down for a moment.',
    settings_decrypt_failed: 'Saved secrets could not be read — check the secret key.',
  };

  // Plain endings for what the server says about one field, matched on its
  // message; the field's label from the page goes in front.
  var FIELD_ERROR_WORDS = [
    [/scheme must be http or https/i, 'must start with http:// or https://.'],
    [/must include a host/i, 'needs a server name after http:// or https://.'],
    [/invalid URL/i, 'is not a valid address.'],
    [/must not exceed|at most \d+ characters|too long/i, 'is too long.'],
    [/CR, LF, or NUL|ASCII printable/i, 'has characters that are not allowed.'],
    [/must be between (\d+) and (\d+)( \w+)?/i, 'must be between $1 and $2$3.'],
    [/integer/i, 'must be a whole number.'],
    [/timezone/i, 'is not a time zone mediaman knows.'],
    [/must be one of/i, 'is not one of the choices offered.'],
  ];

  // The label a person sees for a setting, or its key in words when the page has none.
  function fieldLabel(key) {
    var el = document.getElementById(key);
    var label = el && el.getAttribute('aria-label');
    if (label) return label;
    var words = key.replace(/_/g, ' ');
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function fieldError(key, msg) {
    for (var i = 0; i < FIELD_ERROR_WORDS.length; i++) {
      var m = msg.match(FIELD_ERROR_WORDS[i][0]);
      if (m) {
        var words = FIELD_ERROR_WORDS[i][1].replace(/\$(\d)/g, function (_, n) { return m[n] || ''; });
        return fieldLabel(key) + ' ' + words;
      }
    }
    return fieldLabel(key) + ' is not valid.';
  }

  // Turn FastAPI 422 bodies into a one-line human summary.
  function summariseSaveError(data, statusCode) {
    if (data && Array.isArray(data.detail) && data.detail.length) {
      return data.detail.map(function (d) {
        var loc = Array.isArray(d.loc) ? d.loc.filter(function (p) { return p !== 'body'; }) : [];
        if (!loc.length || typeof loc[0] !== 'string') return 'Save failed — try again.';
        return fieldError(loc[0], String(d.msg || ''));
      }).filter(function (line, i, all) { return all.indexOf(line) === i; }).join(' ');
    }
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && data.message) return data.message;
    if (data && data.error) return SAVE_ERROR_WORDS[data.error] || 'Save failed — try again.';
    if (statusCode === 429) return 'Too many saves — slow down for a moment.';
    return 'Save failed — try again.';
  }

  function init(opts) {
    var getPayload = (opts && opts.getPayload) || function () { return {}; };
    var refreshOverviewHero = (opts && opts.refreshOverviewHero) || function () {};

    savebar = document.getElementById('setg-savebar');
    // An expired session must not redirect away from edits that are not saved.
    MM.api.hasUnsavedChanges = function () {
      return !!savebar && savebar.classList.contains('on');
    };
    var saveBtn   = document.getElementById('btn-save');
    var discardBtn = document.getElementById('btn-discard');
    var statusEl  = document.getElementById('save-status');
    var SAVE_LABEL = 'Save Settings';
    var STANDING_TEXT = 'Edits apply to every section at once.';

    document.addEventListener('input', function (e) {
      if (isSettingsInput(e.target)) { lastEdited = e.target; markDirty(); }
    }, true);
    document.addEventListener('change', function (e) {
      if (isSettingsInput(e.target)) { lastEdited = e.target; markDirty(); }
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
        saveBtn.textContent = SAVE_LABEL;
      }
      if (statusEl) statusEl.textContent = STANDING_TEXT;
    }

    // The success state, until its timers have run or an edit ends it early.
    var successTimers = [];
    function endSuccess() {
      if (!successTimers.length) return;
      successTimers.forEach(clearTimeout);
      successTimers = [];
      saveBtn.textContent = SAVE_LABEL;
      saveBtn.disabled = false;
    }
    // The bar shows while anything differs from what was saved. A save under way keeps it and its words.
    var saving = false;
    onEdit = function () {
      if (!savebar || saving) return;
      if (savebar.classList.contains('is-error')) clearSaveError();
      var clean = !changedKeys(saved, snapshot()).length;
      // After a save the bar goes by itself.
      if (clean && successTimers.length) return;
      if (successTimers.length || clean) {
        endSuccess();
        if (statusEl) statusEl.textContent = STANDING_TEXT;
      }
      savebar.classList.toggle('on', !clean);
    };

    // runSave is a named function so the reauth retry path can re-fire the same
    // payload without duplicating response-bookkeeping. Routes through
    // MM.reauth.run() so CSRF headers, credentials, and the reauth-modal
    // flow are applied consistently.
    function runSave(fromKeyboard) {
      if (!saveBtn) return;
      // Disabling the button blurs it, so note now whether focus was in the bar.
      var hadFocus = savebar && savebar.contains(document.activeElement);
      saving = true;
      saveBtn.disabled = true;
      // Discard would put back values the server no longer holds.
      if (discardBtn) discardBtn.disabled = true;
      clearSaveError();
      var sent = snapshot();
      var payload = getPayload(sent, saved);
      saveBtn.textContent = 'Saving…';
      if (statusEl) statusEl.textContent = '';

      // The button is the way back in, so give it focus again.
      function reenable() {
        saveBtn.disabled = false;
        if (discardBtn) discardBtn.disabled = false;
        if (hadFocus && focusLost()) saveBtn.focus();
      }

      MM.reauth.run(function () {
        return MM.api.put('/api/settings', payload);
      })
        .then(function (data) {
          saving = false;
          var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });

          // MM.api rejects on non-2xx, so reaching here guarantees the save
          // succeeded. Treat any 2xx that lacks data.status==='saved' as a
          // partial success (shouldn't happen, but be defensive).
          if (data.status === 'saved' || data.ok !== false) {
            saved = sent;
            paintTests();
            refreshOverviewHero();
            if (changedKeys(sent, snapshot()).length) {
              saveBtn.textContent = SAVE_LABEL;
              if (statusEl) statusEl.textContent = 'Saved, except what you changed since.';
              reenable();
              return;
            }
            if (discardBtn) discardBtn.disabled = false;
            saveBtn.textContent = 'Saved ✓';
            if (statusEl) {
              statusEl.textContent = ignored.length
                ? 'Saved · ignored unknown: ' + ignored.join(', ')
                : 'Settings saved';
            }
            successTimers = [
              setTimeout(function () { settleAfterHide(fromKeyboard); }, 600),
              setTimeout(function () {
                endSuccess();
                if (statusEl && !ignored.length) statusEl.textContent = STANDING_TEXT;
              }, 2200),
            ];
            return;
          }

          // Unexpected envelope — surface as a save error.
          setSaveError(summariseSaveError(data, 200));
          reenable();
        })
        .catch(function (err) {
          saving = false;
          // MM.reauth.run() rejects with 'reauth_cancelled' when the user
          // dismisses the modal — show a polite message rather than a server
          // error. All other rejections are APIError instances from MM.api.
          if (err && err.error === 'reauth_cancelled') {
            setSaveError('Save cancelled — re-authentication required.');
            reenable();
            return;
          }
          if (err && err.status) {
            // Reconstruct a minimal data envelope so summariseSaveError can
            // read detail / error fields that MM.api.APIError already parsed.
            setSaveError(summariseSaveError(err.data || {}, err.status));
          } else {
            // Status 0 is a request that never reached the server; its
            // message is the shared network wording.
            setSaveError(MM.api.messageOf(err, 'Save failed — try again.'));
          }
          reenable();
        });
    }

    if (saveBtn) saveBtn.addEventListener('click', function (e) {
      // A click from Enter or Space on the button has detail 0; a pointer click has 1 or more.
      runSave(e.detail === 0);
    });

    saved = snapshot();

    if (discardBtn) discardBtn.addEventListener('click', function (e) {
      var fromKeyboard = e.detail === 0;
      window.UIFeedback.confirm({
        title: 'Discard unsaved changes?',
        body: 'Every setting goes back to how it was last saved.',
        confirmLabel: 'Discard',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (!ok) return;
        restoreSaved();
        clearSaveError();
        refreshOverviewHero();
        settleAfterHide(fromKeyboard);
      });
    });
  }

  MM.settings.savebar = {
    init: init,
    markDirty: markDirty,
    markClean: markClean,
    isEdited: isEdited,
  };
})();
