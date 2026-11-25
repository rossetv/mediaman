/**
 * settings/savebar.js — sticky savebar, save submit, and reauth retry.
 *
 * Responsibilities:
 *   - Dirty-tracking listeners that toggle `.setg-savebar.on`
 *   - 422 / 403 / 429 response shaping into a one-line summary
 *   - PUT /api/settings submit handler with reauth retry
 *   - A stored secret whose field is emptied is removed only once confirmed,
 *     and one whose mask was edited is kept
 *   - What a save whose answer was lost may have stored stays unsaved until
 *     Save or Discard has asked the server what it has
 *   - Discard-confirm via UIFeedback, then the last saved values put back
 *     in place
 *
 * Cross-module dependencies:
 *   window.UIFeedback (confirm dialog, toast)
 *   MM.reauth         (core/reauth.js — centred reauth modal)
 *   MM.api            (core/api.js — told when edits are unsaved)
 *   MM.settings.diskThresholds / MM.settings.toggles (Discard puts their
 *                     state back; a save takes in what the server stored)
 *   MM.settings.integrations.paintTests (told of every edit, save and discard)
 *   MM.settings.integrations.recheck (told what a save has stored)
 *
 * Exposes:
 *   MM.settings.savebar.init({ getPayload, refreshOverviewHero })
 *   MM.settings.savebar.markDirty()
 *   MM.settings.savebar.markClean()
 *   MM.settings.savebar.isEdited(el)   — el differs, or may differ, from what was last saved
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

  // A save or a discard leaves nothing in the bar to press. Only a keyboard press restores focus, since a
  // pointer user has no focus to lose and a refocus would scroll them back or
  // raise a phone keyboard. Focus already moved elsewhere is left alone; focus
  // lost to the body or the bar returns to the last edited control, or to the
  // page heading when that cannot take focus.
  function giveFocusBack(fromKeyboard) {
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

  function settleAfterHide(fromKeyboard) {
    markClean();
    giveFocusBack(fromKeyboard);
  }

  // The send-newsletter panel holds ephemeral recipient checkboxes, and the
  // account forms (add user, password, email), the add-subscriber row and the
  // TVs have their own buttons. None of them are saved settings, so changes inside
  // them must not mark dirty.
  function isSettingsInput(el) {
    return el.closest('.setg-pg') &&
      !el.closest('#newsletter-send-panel, .inline-form, .sub-add, .tv-list, .tv-add');
  }

  // The values Discard goes back to: those on the page at load, then those of the last save.
  var saved = null;

  // What saves with no answer may have stored: by thing (see `things`), the value before the
  // first of them and the values they sent.
  var unsure = {};

  function isUnsure() { return Object.keys(unsure).length > 0; }

  // The settings of saves with no answer: their services are checked once it is known what is stored.
  var unchecked = [];

  function recheck(keys) {
    if (MM.settings.integrations) MM.settings.integrations.recheck(unchecked.concat(keys), saved.values);
    unchecked = [];
  }

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
    return !!saved && el.id in saved.values && (el.id in unsure || saved.values[el.id] !== valueOf(el));
  }

  // What the server sent for a control, which a browser going back to the page may have put typed text over.
  function servedValue(el) {
    if (el.type === 'checkbox' || el.type === 'radio') return el.defaultChecked;
    if (el.tagName === 'SELECT') {
      var chosen = el.querySelector('option[selected]') || el.querySelector('option');
      return chosen ? chosen.value : '';
    }
    // A served value its field cannot hold, such as a time written 9:00, shows as empty.
    return el.value === '' ? '' : el.defaultValue;
  }

  function snapshot(read) {
    var values = {};
    settingsControls().forEach(function (el) { values[el.id] = (read || valueOf)(el); });
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

  // Each setting, with each library's selection and limit as one of its own.
  function things(snap) {
    return Object.assign({}, snap.values, snap.disk && MM.settings.diskThresholds.things(snap.disk));
  }

  function rememberUnsure(sent) {
    var was = things(saved);
    var now = things(sent);
    Object.keys(Object.assign({}, was, now)).forEach(function (key) {
      if (was[key] === now[key] && !unsure[key]) return;
      unsure[key] = unsure[key] || { old: was[key], sent: [] };
      unsure[key].sent.push(now[key]);
    });
    paintTests();
  }

  // `onto`, with the things of `mine` that are kept.
  function rebase(mine, onto, keep) {
    var values = {};
    Object.keys(onto.values).forEach(function (id) { values[id] = (keep(id) ? mine : onto).values[id]; });
    var disk = mine.disk && onto.disk && MM.settings.diskThresholds.rebase(mine.disk, onto.disk, keep);
    return { values: values, disk: disk };
  }

  // Puts `snap` on the page, under what was typed or clicked since `base` was read from it.
  function show(snap, base) {
    var page = snapshot();
    var was = things(base);
    var now = things(page);
    var next = rebase(page, snap, function (key) { return now[key] !== was[key]; });
    put(next.values);
    if (next.disk) MM.settings.diskThresholds.show(next.disk);
    paintTests();
  }

  var DAYS = ['min_age_days', 'inactivity_days', 'grace_days'];

  // A limit with no path or no percentage is none, and the library is always scanned.
  function limitOf(thing) {
    var parts = /^(\d+) ./.exec(thing || '');
    return parts ? Number(parts[1]) : 0;
  }

  // Whether more may be deleted with one value of a thing than with another.
  function deletesMore(key, to, from) {
    if (key.indexOf('lib:') === 0) return !!to && !from;
    if (key.indexOf('limit:') === 0) return limitOf(to) < limitOf(from);
    return DAYS.indexOf(key) !== -1 && Number(to) < Number(from);
  }

  // The server tells no secret, and keeps the stored one when it is sent a blank or masked field.
  function isSecret(key) {
    return !!document.querySelector('.inp-reveal[data-reveal-target="' + key + '"]');
  }

  // Asks the server to delete a secret: it takes an empty one as unchanged.
  var CLEAR_SECRET = '__CLEAR__';
  // Stands for a stored secret, which only the server knows.
  var MASKED_SECRET = '****';

  // Spaces are no secret, and would be stored in place of one.
  function emptyBlankSecrets() {
    settingsControls().forEach(function (el) {
      if (isSecret(el.id) && !el.value.trim()) el.value = '';
    });
  }

  // Asterisks alone, or the mask with something typed before or after it.
  function isMaskEdit(value) {
    return /^\*+$|^\*{4}|\*{4}$/.test(value.trim());
  }

  // Puts back each secret whose mask was edited, and says so: what is stored cannot be had back once replaced.
  function putBackMasks() {
    var back = {};
    var onlyAsterisks = {};
    settingsControls().forEach(function (el) {
      if (!isSecret(el.id) || el.value === saved.values[el.id] || !isMaskEdit(el.value)) return;
      back[el.id] = saved.values[el.id];
      onlyAsterisks[el.id] = /^\*+$/.test(el.value.trim());
    });
    var keys = Object.keys(back);
    if (!keys.length) return false;
    put(back);
    var untouched = keys.filter(function (key) { return onlyAsterisks[key] && saved.values[key]; });
    var refused = keys.filter(function (key) { return untouched.indexOf(key) === -1; });
    var said = [];
    if (untouched.length) {
      said.push('Nothing new was entered for ' + inWords(untouched.map(fieldLabel)) + ', so ' +
        (untouched.length > 1 ? 'they stay as they were.' : 'it stays as it was.'));
    }
    if (refused.length) {
      said.push('A secret cannot start or end with ****. ' + inWords(refused.map(fieldLabel)) +
        (refused.length > 1 ? ' stay as they were.' : ' stays as it was.'));
    }
    window.UIFeedback.toast(said.join(' '), { duration: 8000 });
    return true;
  }

  // A caret inside the mask would add what is typed to it.
  function selectMask(el) {
    if (!el || el.value !== MASKED_SECRET || !isSecret(el.id)) return;
    if (el.selectionStart !== 0 || el.selectionEnd !== el.value.length) el.select();
  }

  function clearedSecrets(sent) {
    return changedKeys(saved, sent).filter(function (key) { return isSecret(key) && sent.values[key] === ''; });
  }

  function confirmClear(keys) {
    var many = keys.length > 1;
    var labels = keys.map(fieldLabel);
    return window.UIFeedback.confirm({
      title: many ? 'Remove ' + keys.length + ' saved secrets?' : 'Remove the saved ' + labels[0] + '?',
      body: many
        ? 'mediaman will delete the saved ' + inWords(labels) + '. What uses them stops working until you enter them again.'
        : 'mediaman deletes what it has stored, and what uses it stops working until you enter it again.',
      confirmLabel: 'Remove and save',
      confirmVariant: 'danger',
      cancelLabel: many ? 'Keep them' : 'Keep it',
    });
  }

  function thingLabel(key) {
    var part = /^(lib|limit):(.*)$/.exec(key);
    if (!part) return fieldLabel(key);
    var title = MM.settings.diskThresholds.titleOf(part[2]);
    return part[1] === 'lib' ? 'scanning of ' + title : 'disk threshold of ' + title;
  }

  function leftAsStored(keys) {
    var names = inWords(keys.map(thingLabel));
    return names.charAt(0).toUpperCase() + names.slice(1) +
      (keys.length > 1 ? ' were left as they were stored.' : ' was left as it was stored.');
  }

  function askToCheck(keys) {
    if (!keys.length) return;
    window.UIFeedback.toast(
      'Shown as mediaman has them now. Check: ' + keys.map(thingLabel).join(', ') + '.',
      { duration: 8000 }
    );
  }

  // A value as its control would hold it.
  function asHeld(el, value) {
    var was = valueOf(el);
    el.value = value;
    var held = valueOf(el);
    el.value = was;
    return held;
  }

  // What is stored, from the server's settings: every library, and the settings not known for sure.
  function storedFrom(data) {
    var values = Object.assign({}, saved.values);
    Object.keys(unsure).forEach(function (key) {
      if (!(key in values)) return;
      if (key in data) values[key] = asHeld(document.getElementById(key), String(data[key]));
      else if (isSecret(key)) values[key] = '';
    });
    return { values: values, disk: saved.disk && MM.settings.diskThresholds.fromServer(data) };
  }

  // Of the things not known for sure, those whose value a person who put them back would not expect.
  function unexpected(stored, keys) {
    var has = things(stored);
    return keys.filter(function (key) {
      return has[key] !== unsure[key].old || (isSecret(key) && has[key] !== '');
    });
  }

  // What to send of `now` once the server has told what is stored: a thing put back is sent only
  // where a save with no answer did change it and the undo lets no more be deleted. Otherwise it
  // is taken from the server, where another page may have set it since; `kept` are those that were.
  function settle(now, data) {
    var stored = storedFrom(data);
    var was = things(saved);
    var mine = things(now);
    var has = things(stored);
    function putBack(key) { return !!unsure[key] && mine[key] === unsure[key].old; }
    function undone(key) {
      if (isSecret(key) && mine[key] === MASKED_SECRET) return false;
      return unsure[key].sent.indexOf(has[key]) !== -1 && !deletesMore(key, mine[key], has[key]);
    }
    var sent = rebase(now, stored, function (key) {
      if (putBack(key)) return undone(key);
      return !!unsure[key] || mine[key] !== was[key];
    });
    var notPutBack = unexpected(stored, Object.keys(unsure).filter(function (key) {
      return putBack(key) && !undone(key);
    }));
    saved = stored;
    var next = things(sent);
    Object.keys(unsure).forEach(function (key) { if (next[key] === has[key]) delete unsure[key]; });
    show(sent, now);
    return { sent: sent, kept: notPutBack };
  }

  function put(values) {
    settingsControls().forEach(function (el) {
      if (!(el.id in values) || valueOf(el) === values[el.id]) return;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = values[el.id];
      else el.value = values[el.id];
    });
    if (MM.settings.toggles && MM.settings.toggles.sync) MM.settings.toggles.sync();
  }

  function restoreSaved() {
    if (!saved) return;
    put(saved.values);
    if (saved.disk && MM.settings.diskThresholds) MM.settings.diskThresholds.restore(saved.disk);
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
    [/home address with nothing after it/i, 'must have nothing after the server name, such as https://mediaman.example.com.'],
    [/home address with a port in use/i, 'needs a port other than 0.'],
    [/must be a home address/i, 'must be this server’s home address, such as http://192.168.1.10:8282, or a name ' +
      'with https, such as https://mediaman.example.com. Before a name, use https:// and not http://.'],
    [/invalid URL/i, 'is not a valid address.'],
    [/must not exceed|at most \d+ characters|too long/i, 'is too long.'],
    [/start or end with \*{4}/, 'cannot start or end with ****.'],
    [/CR, LF, or NUL|ASCII printable/i, 'has characters that are not allowed.'],
    [/must be between (\d+) and (\d+)( \w+)?/i, 'must be between $1 and $2$3.'],
    [/integer/i, 'must be a whole number.'],
    [/timezone/i, 'is not a time zone mediaman knows.'],
    [/must be one of/i, 'is not one of the choices offered.'],
  ];

  function inWords(names) {
    return names.length < 2 ? names.join('') : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
  }

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
    // The page's room for the bar follows its height, whatever it says.
    if (savebar && window.ResizeObserver) {
      new ResizeObserver(function () {
        document.documentElement.style.setProperty('--savebar-h', savebar.offsetHeight + 'px');
      }).observe(savebar, { box: 'border-box' });
    }
    // An expired session must not redirect away from edits that are not saved.
    MM.api.hasUnsavedChanges = function () {
      return !!savebar && savebar.classList.contains('on');
    };
    var saveBtn   = document.getElementById('btn-save');
    var discardBtn = document.getElementById('btn-discard');
    var statusEl  = document.getElementById('save-status');
    var msgEl = savebar && savebar.querySelector('.sb-msg');
    var SAVE_LABEL = 'Save Settings';
    var UNSAVED_TEXT = 'Unsaved changes';
    var STANDING_TEXT = 'Edits apply to every section at once.';
    var UNSURE_TEXT = 'The last save may have been stored. Save again to be sure.';

    document.addEventListener('input', function (e) {
      if (isSettingsInput(e.target)) { lastEdited = e.target; markDirty(); }
    }, true);
    document.addEventListener('change', function (e) {
      if (isSettingsInput(e.target)) { lastEdited = e.target; markDirty(); }
    }, true);

    document.addEventListener('focusin', function (e) { selectMask(e.target); });
    document.addEventListener('selectionchange', function () { selectMask(document.activeElement); });

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
      if (savebar) savebar.classList.remove('is-error', 'is-partial');
      if (saveBtn) {
        saveBtn.classList.remove('btn--danger');
        saveBtn.classList.add('btn--primary');
        saveBtn.textContent = SAVE_LABEL;
      }
      if (statusEl) statusEl.textContent = STANDING_TEXT;
    }

    // The success state, until its timers have run or an edit ends it early.
    var successTimers = [];
    // What the bar says was left as it was stored, for a toast where an edit takes those words away.
    var leftOut = [];
    function tellLeftOut() {
      askToCheck(leftOut);
      leftOut = [];
    }
    function endSuccess() {
      if (!successTimers.length) return;
      successTimers.forEach(clearTimeout);
      successTimers = [];
      saveBtn.textContent = SAVE_LABEL;
      saveBtn.disabled = false;
      if (discardBtn) discardBtn.disabled = false;
      if (msgEl) msgEl.textContent = UNSAVED_TEXT;
      savebar.classList.remove('is-saved', 'is-partial');
    }
    // The bar shows while anything differs from what was saved. A save under way keeps it and its words.
    var saving = false;
    onEdit = function () {
      if (!savebar || saving) return;
      if (savebar.classList.contains('is-error')) clearSaveError();
      var clean = !isUnsure() && !changedKeys(saved, snapshot()).length;
      if (clean) recheck([]);
      // After a save the bar goes by itself.
      if (clean && successTimers.length) return;
      if (successTimers.length || clean) {
        endSuccess();
        savebar.classList.remove('is-partial');
        if (statusEl) statusEl.textContent = STANDING_TEXT;
        tellLeftOut();
      }
      savebar.classList.toggle('on', !clean);
      if (isUnsure() && statusEl && statusEl.textContent !== UNSURE_TEXT) {
        savebar.classList.add('is-partial');
        statusEl.textContent = UNSURE_TEXT;
      }
    };

    // The button is the way back in, so give it focus again.
    function reenable(hadFocus) {
      saveBtn.disabled = false;
      if (discardBtn) discardBtn.disabled = false;
      if (hadFocus && focusLost()) saveBtn.focus({ preventScroll: true });
    }

    // Nothing in the bar answers while the server is asked. Returns whether the focus was in it,
    // since disabling its buttons takes the focus away.
    function startAsking() {
      var hadFocus = savebar && savebar.contains(document.activeElement);
      saving = true;
      leftOut = [];
      saveBtn.disabled = true;
      // Discard would put back values the server no longer holds.
      if (discardBtn) discardBtn.disabled = true;
      clearSaveError();
      return hadFocus;
    }

    // What a save with no answer stored is known only to the server.
    function askStored(hadFocus, not, then) {
      MM.api.get('/api/settings').then(then, function () {
        saving = false;
        setSaveError('Could not check what the last save stored, so nothing was ' + not + '.');
        reenable(hadFocus);
      });
    }

    function runSave(fromKeyboard) {
      if (!saveBtn) return;
      if (putBackMasks()) {
        markDirty();
        if (!savebar.classList.contains('on')) { settleAfterHide(fromKeyboard); return; }
      }
      var hadFocus = startAsking();
      saveBtn.textContent = 'Saving…';
      emptyBlankSecrets();
      var now = snapshot();
      if (!isUnsure()) { send(now, fromKeyboard, hadFocus, false, []); return; }
      askStored(hadFocus, 'saved', function (data) {
        var settled = settle(now, data);
        send(settled.sent, fromKeyboard, hadFocus, !changedKeys(saved, settled.sent).length, settled.kept);
      });
    }

    function send(sent, fromKeyboard, hadFocus, done, kept) {
      var cleared = clearedSecrets(sent);
      if (!cleared.length) { deliver(sent, fromKeyboard, hadFocus, done, kept); return; }
      saveBtn.textContent = SAVE_LABEL;
      // The dialog gives the focus back to what has it now.
      reenable(hadFocus);
      confirmClear(cleared).then(function (ok) {
        if (ok) {
          startAsking();
          saveBtn.textContent = 'Saving…';
          deliver(sent, fromKeyboard, hadFocus, done, kept);
          return;
        }
        saving = false;
        var back = {};
        cleared.forEach(function (key) {
          back[key] = saved.values[key];
          // The server has just shown it is still there, so a lost removal of it was not made.
          if (unsure[key] && unsure[key].sent.every(function (value) { return value === ''; })) delete unsure[key];
        });
        put(back);
        askToCheck(kept);
        markDirty();
        if (!savebar.classList.contains('on')) settleAfterHide(fromKeyboard);
      });
    }

    // Routes through MM.reauth.run() so CSRF headers, credentials, and the reauth-modal
    // flow are applied consistently. With `done` the server has it all; of `kept`, which the person
    // put back, it has what it had.
    function deliver(sent, fromKeyboard, hadFocus, done, kept) {
      // Only what was changed is sent: the rest may be a default nobody chose, or older than what another tab saved.
      var all = getPayload(sent, saved);
      var payload = {};
      changedKeys(saved, sent).forEach(function (key) { if (key in all) payload[key] = all[key]; });
      clearedSecrets(sent).forEach(function (key) { payload[key] = CLEAR_SECRET; });
      if ('plex_libraries' in payload) payload.plex_libraries_base = saved.disk.libs;

      (done ? Promise.resolve({ status: 'saved' }) : MM.reauth.run(function () {
        return MM.api.put('/api/settings', payload);
      }))
        .then(function (data) {
          saving = false;
          var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });

          // MM.api rejects on non-2xx, so reaching here guarantees the save
          // succeeded. Treat any 2xx that lacks data.status==='saved' as a
          // partial success (shouldn't happen, but be defensive).
          if (data.status === 'saved' || data.ok !== false) {
            unsure = {};
            // A save may store more than this page sent, such as another page's choices.
            var merged = sent.disk && 'plex_libraries' in data;
            saved = { values: sent.values, disk: merged ? MM.settings.diskThresholds.fromServer(data) : sent.disk };
            show(saved, sent);
            recheck(Object.keys(payload));
            refreshOverviewHero();
            var edited = changedKeys(saved, snapshot()).length > 0;
            var partial = edited || kept.length > 0;
            saveBtn.textContent = partial ? SAVE_LABEL : 'Saved ✓';
            savebar.classList.add(partial ? 'is-partial' : 'is-saved');
            if (statusEl) {
              statusEl.textContent = partial
                ? (edited ? 'Saved, except what you changed since.' : 'Saved.') + (kept.length ? ' ' + leftAsStored(kept) : '')
                : ignored.length ? 'Saved · ignored unknown: ' + ignored.join(', ') : 'Settings saved';
            }
            leftOut = kept;
            if (edited) { reenable(hadFocus); return; }
            if (msgEl) msgEl.textContent = 'No unsaved changes';
            giveFocusBack(fromKeyboard);
            // What was left out takes as long to read as a toast that names it.
            var hideAfter = kept.length ? 8000 : 600;
            successTimers = [
              setTimeout(function () { leftOut = []; markClean(); }, hideAfter),
              setTimeout(function () {
                endSuccess();
                if (statusEl && !ignored.length) statusEl.textContent = STANDING_TEXT;
              }, hideAfter + 1600),
            ];
            return;
          }

          // Unexpected envelope — surface as a save error.
          askToCheck(kept);
          setSaveError(summariseSaveError(data, 200));
          reenable(hadFocus);
        })
        .catch(function (err) {
          saving = false;
          askToCheck(kept);
          // MM.reauth.run() rejects with 'reauth_cancelled' when the user
          // dismisses the modal — show a polite message rather than a server
          // error. All other rejections are APIError instances from MM.api.
          if (err && err.error === 'reauth_cancelled') {
            setSaveError('Save cancelled — re-authentication required.');
            reenable(hadFocus);
            return;
          }
          // With no answer, or the server's own failure, the settings may be stored all the same.
          if (!err || !err.status || err.status >= 500) {
            unchecked = unchecked.concat(Object.keys(payload));
            rememberUnsure(sent);
          }
          if (err && err.status) {
            // Reconstruct a minimal data envelope so summariseSaveError can
            // read detail / error fields that MM.api.APIError already parsed.
            setSaveError(summariseSaveError(err.data || {}, err.status));
          } else {
            // Status 0 is a request with no answer; its message is the
            // shared network wording.
            setSaveError(MM.api.messageOf(err, 'Save failed — try again.'));
          }
          reenable(hadFocus);
        });
    }

    if (saveBtn) saveBtn.addEventListener('click', function (e) {
      // A click from Enter or Space on the button has detail 0; a pointer click has 1 or more.
      runSave(e.detail === 0);
    });

    saved = snapshot(servedValue);
    // Chrome puts back what was typed only once the page has loaded.
    window.addEventListener('pageshow', function () { onEdit(); });

    function putBackSaved(fromKeyboard) {
      leftOut = [];
      restoreSaved();
      clearSaveError();
      refreshOverviewHero();
      settleAfterHide(fromKeyboard);
      recheck([]);
    }

    function discardUnsure(fromKeyboard) {
      var hadFocus = startAsking();
      askStored(hadFocus, 'discarded', function (data) {
        saving = false;
        saveBtn.disabled = false;
        discardBtn.disabled = false;
        saved = storedFrom(data);
        askToCheck(unexpected(saved, Object.keys(unsure)));
        unsure = {};
        putBackSaved(fromKeyboard);
      });
    }

    if (discardBtn) discardBtn.addEventListener('click', function (e) {
      var fromKeyboard = e.detail === 0;
      window.UIFeedback.confirm({
        title: 'Discard unsaved changes?',
        body: 'Every setting goes back to how it was last saved.',
        confirmLabel: 'Discard',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (!ok) return;
        if (isUnsure()) discardUnsure(fromKeyboard); else putBackSaved(fromKeyboard);
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
