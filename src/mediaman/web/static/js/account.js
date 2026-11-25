/**
 * Profile page — change-password and email forms, the lists of sign-ins and TVs, and the use of the
 * person's history by the AI.
 *
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom), core/reauth.js (MM.reauth),
 * settings/rows.js (MM.settings.rows), ui-feedback.js (UIFeedback).
 * The strength meter and match hint of the password form are password-strength.js.
 */
(function () {
  'use strict';

  function wordsOf(err) { return MM.api.messageOf(err, 'Something went wrong. Try again.'); }

  function showReason(target, text, issues) {
    target.textContent = '';
    if (issues && issues.length) {
      var p = document.createElement('div');
      p.textContent = text;
      target.appendChild(p);
      var ul = document.createElement('ul');
      ul.className = 'inline-form-issues';
      issues.forEach(function (item) {
        var li = document.createElement('li');
        li.textContent = String(item);
        ul.appendChild(li);
      });
      target.appendChild(ul);
    } else {
      target.textContent = text;
    }
  }

  // `what` ends the sentence that asks for the password. The Plex card asks for one only where it is due.
  function confirmed(what, action) {
    function tell() { document.dispatchEvent(new Event('mm:reauth')); }
    var run = MM.reauth.run(action, {
      title: 'Confirm it’s you',
      descText: 'Enter your Mediaman password to ' + what + '.',
      confirmLabel: 'Continue',
    });
    run.then(tell, tell);
    return run;
  }

  // Not `disabled`: that would drop the focus, which the password dialog returns to its button.
  // With `keepWidth` the words are for a screen reader, so what is beside the button does not move.
  function setBusy(button, words, keepWidth) {
    if (words) {
      var width = button.offsetWidth;
      button.setAttribute('aria-disabled', 'true');
      if (button._label === undefined) button._label = button.textContent;
      button.textContent = '';
      var spinner = document.createElement('span');
      spinner.className = 'btn-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      button.appendChild(spinner);
      if (keepWidth) {
        button.style.width = width + 'px';
        button._name = button.getAttribute('aria-label');
        button.setAttribute('aria-label', words);
      } else {
        button.appendChild(document.createTextNode(words));
      }
    } else {
      button.removeAttribute('aria-disabled');
      button.style.width = '';
      if (button._name) button.setAttribute('aria-label', button._name);
      if (button._label !== undefined) button.textContent = button._label;
    }
  }
  function isBusy(button) { return button.getAttribute('aria-disabled') === 'true'; }

  // On a narrow phone a status wraps under its button, where the bottom bar may cover it.
  function reveal(result) { result.scrollIntoView({ block: 'nearest' }); }

  // Say an answer in a status, which the new words may make taller.
  function answer(result, scope, text, tone) {
    var there = MM.dom.stillAt(result, scope);
    result.className = 'me-msg ' + tone;
    result.textContent = text;
    if (there) reveal(result);
  }

  function done(text) { UIFeedback.toast(text, { variant: 'success' }); }

  // Said to a screen reader; the button shows it to everyone else.
  function working(result, text) {
    result.className = 'sr-only';
    result.textContent = text;
  }

  function initPassword(reloadSessions) {
    var form = document.getElementById('account-password-form');
    var opener = document.getElementById('account-open-password');
    var cancel = document.getElementById('account-cancel-password');
    var result = document.getElementById('account-password-result');
    var submit = document.getElementById('account-submit-password');
    if (!form || !opener || !cancel || !result || !submit) return;
    var fold = MM.settings.rows;
    var newField = document.getElementById('account-new-password');
    var caption = document.getElementById('account-new-strength-label');
    var rule = caption.textContent;

    // The meter's script leaves the caption empty with the field.
    function restoreRule() { if (!newField.value) caption.textContent = rule; }

    // On a short phone the form is taller than the screen, so the reason is said beside its field.
    function fail(fieldId, text, issues, onlySay) {
      clear();
      var field = document.getElementById(fieldId);
      var why = document.getElementById(fieldId + '-why');
      field.setAttribute('aria-invalid', 'true');
      if (!why) { why = result; why.className = 'me-msg err'; }
      showReason(why, text, issues);
      if (!onlySay) MM.dom.refuse(field, why);
    }
    function clear() {
      result.className = 'me-msg';
      result.textContent = '';
      form.querySelectorAll('.fld-why').forEach(function (why) { why.textContent = ''; });
      form.querySelectorAll('input').forEach(function (field) { field.removeAttribute('aria-invalid'); });
    }

    function show() {
      opener.setAttribute('aria-expanded', 'true');
      opener.classList.add('is-gone');
      fold.openInView(form);
      document.getElementById('account-old-password').focus({ preventScroll: true });
    }
    function shut() {
      var held = MM.dom.focusStillIn(form);
      opener.setAttribute('aria-expanded', 'false');
      opener.classList.remove('is-gone');
      fold.close(form);
      form.reset();
      clear();
      setTimeout(restoreRule, 0);
      if (held) opener.focus({ preventScroll: true });
    }

    opener.addEventListener('click', show);
    cancel.addEventListener('click', function () {
      if (!isBusy(submit)) shut();
    });
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !isBusy(submit)) shut();
    });

    // A message about what was typed goes once the person types again; a send keeps its message.
    form.addEventListener('input', function () {
      if (!isBusy(submit)) clear();
      restoreRule();
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (isBusy(submit)) return;
      var oldPw = document.getElementById('account-old-password').value;
      var newPw = newField.value;
      var conf = document.getElementById('account-confirm-password').value;
      if (!oldPw) { fail('account-old-password', 'Enter your current password.'); return; }
      if (newPw.length < 12) { fail('account-new-password', 'New password must be at least 12 characters.'); return; }
      if (newPw !== conf) { fail('account-confirm-password', 'Passwords don’t match.'); return; }

      clear();
      setBusy(submit, 'Updating…');
      working(result, 'Updating…');
      reveal(submit);
      confirmed('change it', function () {
        return MM.api.post('/api/users/change-password', {
          old_password: oldPw,
          new_password: newPw,
        });
      }).then(function () {
        setBusy(submit, '');
        shut();
        done('Password updated. Your other devices are signed out.');
        reloadSessions();
      }).catch(function (err) {
        setBusy(submit, '');
        var code = err && err.error;
        if (code === 'reauth_cancelled') { clear(); return; }
        if (code === 'wrong_password' || code === 'weak_password' || code === 'same_password') {
          var there = MM.dom.stillAt(submit, form);
          fail(code === 'wrong_password' ? 'account-old-password' : 'account-new-password', wordsOf(err), err.issues, !there);
          return;
        }
        answer(result, form, wordsOf(err), 'err');
      });
    });
  }

  function initEmail() {
    var form = document.getElementById('account-email-form');
    var input = document.getElementById('account-email');
    var panel = document.getElementById('account-email-fold');
    var cancel = document.getElementById('account-cancel-email');
    var submit = document.getElementById('account-submit-email');
    var result = document.getElementById('account-email-result');
    if (!form || !input || !panel || !cancel || !submit || !result) return;
    var fold = MM.settings.rows;
    var saved = input.defaultValue;

    function changed() { return input.value.trim() !== saved; }

    function say(text, tone) {
      result.className = 'me-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
      input.setAttribute('aria-invalid', String(tone === 'err'));
    }

    // Folding takes away the button pressed.
    function follow() {
      if (changed()) {
        if (!fold.isOpen(panel)) fold.openInView(panel);
        return;
      }
      if (panel.contains(document.activeElement)) input.focus({ preventScroll: true });
      fold.close(panel);
    }

    function undo() {
      if (isBusy(submit)) return;
      input.value = saved;
      say('');
      follow();
      input.focus({ preventScroll: true });
    }

    function refuse(text, there) {
      say(text, 'err');
      if (there) MM.dom.refuse(input, result);
    }

    form.addEventListener('input', function () {
      if (isBusy(submit)) return;
      say('');
      follow();
    });
    cancel.addEventListener('click', undo);
    form.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && changed()) undo();
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = input.value.trim();
      if (isBusy(submit) || !changed()) return;
      follow();
      if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        refuse('That is not an email address.', true);
        return;
      }
      say('');
      setBusy(submit, 'Saving…');
      working(result, 'Saving…');
      reveal(submit);
      confirmed('change your email', function () {
        return MM.api.patch('/api/users/me/email', { email: email });
      }).then(function () {
        setBusy(submit, '');
        saved = email;
        say('');
        follow();
        done(email ? 'Email saved.' : 'Email removed. Alerts are off.');
      }).catch(function (err) {
        setBusy(submit, '');
        say('');
        if (err && err.error === 'reauth_cancelled') return;
        refuse(MM.api.messageOf(err, 'Could not save. Try again.'), MM.dom.stillAt(submit, form));
      });
    });

    // A reload may keep what was typed.
    if (changed()) follow();
  }

  function when(iso) {
    var at = new Date(iso);
    var day = { day: 'numeric', month: 'short' };
    if (at.getFullYear() !== new Date(Date.now()).getFullYear()) day.year = 'numeric';
    // Safari puts "at" between a date and its time made in one.
    return at.toLocaleDateString('en-GB', day) + ', ' +
      at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }

  function deviceIcon(label) {
    if (/ on (iPhone|Android)$/.test(label)) return 'i-phone';
    if (/ on iPad$/.test(label)) return 'i-tablet';
    if (/ on (macOS|ChromeOS)$/.test(label)) return 'i-laptop';
    if (/ on (Windows|Linux)$/.test(label)) return 'i-desktop';
    return 'i-globe';
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  /* Returns the function that loads the list afresh. */
  function initSessions() {
    var list = document.getElementById('account-sessions');
    var revoke = document.getElementById('account-revoke-others');
    var result = document.getElementById('account-revoke-result');
    var why = document.getElementById('account-revoke-why');
    if (!list || !revoke || !result) return function () {};
    /* The rows on show by the session each is of. */
    var rows = {};
    var loaded = false;
    /* Only the list asked for last is shown, without what was signed out since it was asked. */
    var asked = 0;
    var gone = {};

    function keyOf(s) { return s.current ? 'here' : s.public_id || s.created_at; }

    function nameOf(s) { return s.device_label || 'Signed in ' + when(s.created_at); }

    function fill(row, s) {
      row.className = s.current ? 'dev here' : 'dev';
      row.textContent = '';
      var icon = el('span', 'dev-ico');
      icon.appendChild(MM.dom.icon(deviceIcon(s.device_label || '')));
      row.appendChild(icon);
      var text = el('div', 'me-txt');
      var name = el('div', 'me-lbl', nameOf(s));
      if (s.current) {
        name.appendChild(document.createTextNode(' '));
        name.appendChild(el('span', 'dev-here', 'This device'));
      }
      text.appendChild(name);
      var dates = el('div', 'me-sub dev-sub');
      function date(words, iso) {
        var line = dates.appendChild(el('span', '', words + ' '));
        line.appendChild(el('span', 'dev-date', when(iso)));
      }
      date('Last active', s.last_used_at || s.created_at);
      if (s.device_label) {
        dates.appendChild(document.createTextNode(' '));
        date('Signed in', s.created_at);
      }
      text.appendChild(dates);
      row.appendChild(text);
      if (s.current || !s.public_id) return;
      var button = el('button', 'btn btn--ghost btn--sm', 'Sign out');
      button.type = 'button';
      button.setAttribute('aria-label', 'Sign out: ' + nameOf(s));
      button.addEventListener('click', function () { signOut(s, row, button); });
      row.appendChild(button);
    }

    function drop(key) {
      var row = rows[key];
      delete rows[key];
      // A row on its way out takes no press.
      row.inert = true;
      MM.settings.rows.leave(row, { focusSelector: '.btn', fallback: list });
    }

    function tellAlone() {
      var alone = Object.keys(rows).length < 2;
      if (alone && document.activeElement === revoke) list.focus({ preventScroll: true });
      revoke.disabled = alone;
      why.hidden = !alone || Boolean(result.textContent);
    }

    function load() {
      var mine = asked += 1;
      return MM.api.get('/api/users/sessions').then(function (data) {
        if (mine !== asked) return;
        // A sign-in that ran out stays listed until it is next used.
        var live = data.sessions.filter(function (s) { return new Date(s.expires_at) > Date.now() && !gone[keyOf(s)]; });
        live.sort(function (a, b) { return Number(b.current || false) - Number(a.current || false); });
        if (!loaded) list.textContent = '';
        loaded = true;
        var still = {};
        live.forEach(function (s) {
          var key = keyOf(s);
          still[key] = true;
          if (!rows[key]) rows[key] = list.appendChild(el('li', ''));
          // A row being signed out keeps the button pressed.
          else if (rows[key].querySelector('[aria-disabled="true"]') || rows[key].contains(document.activeElement)) return;
          fill(rows[key], s);
        });
        Object.keys(rows).forEach(function (key) { if (!still[key]) drop(key); });
        tellAlone();
      }).catch(function () {
        if (mine !== asked) return;
        rows = {};
        loaded = false;
        list.textContent = '';
        list.appendChild(el('li', 'dev dev--note', 'Could not load your sign-ins. Reload the page to try again.'));
        // They can still be ended without being listed.
        revoke.disabled = false;
        why.hidden = true;
      }).then(function () {
        if (mine === asked) list.removeAttribute('aria-busy');
      });
    }

    function signOut(s, row, button) {
      if (isBusy(button)) return;
      setBusy(button, 'Signing out…', true);
      confirmed('sign out a device', function () {
        return MM.api.delete('/api/users/sessions/' + encodeURIComponent(s.public_id));
      }).then(function () {
        gone[keyOf(s)] = true;
        done('Signed out ' + nameOf(s) + '.');
        // The list may have been loaded afresh meanwhile, without it.
        if (rows[keyOf(s)] !== row) return;
        drop(keyOf(s));
        tellAlone();
      }, function (err) {
        setBusy(button, '');
        if (err && err.error === 'reauth_cancelled') return;
        UIFeedback.error(MM.api.messageOf(err, 'Could not sign that device out. Try again.'));
      });
    }

    function say(text, tone) {
      result.className = 'me-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
    }

    revoke.addEventListener('click', function () {
      if (isBusy(revoke)) return;
      setBusy(revoke, 'Signing out…');
      working(result, 'Signing out…');
      reveal(revoke);
      confirmed('sign out your other devices', function () {
        return MM.api.post('/api/users/sessions/revoke-others');
      }).then(function () {
        answer(result, revoke.parentNode, 'Signed out everywhere else.', 'ok');
        return load();
      }).then(function () {
        setBusy(revoke, '');
      }, function (err) {
        setBusy(revoke, '');
        if (err && err.error === 'reauth_cancelled') { say(''); return; }
        answer(result, revoke.parentNode, MM.api.messageOf(err, 'Could not sign the others out. Try again.'), 'err');
      });
    });

    load();
    return load;
  }

  function seenWhen(iso) {
    var at = new Date(iso);
    var today = new Date(Date.now());
    today.setHours(0, 0, 0, 0);
    var days = Math.ceil((today - at) / 864e5);
    var time = at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    if (days <= 0) return 'today, ' + time;
    if (days < 7) return at.toLocaleDateString('en-GB', { weekday: 'long' }) + ', ' + time;
    return dayOf(iso);
  }

  function dayOf(iso) {
    var at = new Date(iso);
    var day = { day: 'numeric', month: 'long' };
    if (at.getFullYear() !== new Date(Date.now()).getFullYear()) day.year = 'numeric';
    return at.toLocaleDateString('en-GB', day);
  }

  /* The person's TVs and, for an admin, everyone else's. Returns the function that loads them
     afresh. A list with no TV is not shown. */
  function initTvs() {
    var mine = document.getElementById('account-tvs');
    var others = document.getElementById('account-tvs-all');
    if (!mine) return function () {};
    var asked = 0;
    var gone = {};

    function stateOf(tv) {
      var seen = tv.on ? 'On now' : tv.last_seen_at ? 'Last seen ' + seenWhen(tv.last_seen_at) : 'Not seen yet';
      var by = others && others.contains(tv.row) ? ' by ' + tv.owner : '';
      return seen + '. Paired' + by + ' ' + dayOf(tv.paired_at) + '.';
    }

    function action(words, tv, run) {
      var button = el('button', 'btn btn--ghost btn--sm', words);
      button.type = 'button';
      button.setAttribute('aria-label', words + ': ' + tv.name);
      button.addEventListener('click', function () { if (!isBusy(button)) run(button); });
      return button;
    }

    function fill(tv) {
      var row = tv.row;
      row.textContent = '';
      var icon = el('span', 'dev-ico');
      icon.appendChild(MM.dom.icon('i-tv'));
      row.appendChild(icon);
      var text = el('div', 'me-txt');
      text.appendChild(el('div', 'me-lbl', tv.name));
      text.appendChild(el('div', 'me-sub', stateOf(tv)));
      row.appendChild(text);
      var acts = el('div', 'dev-acts');
      var rename = action('Rename', tv, function () { edit(tv); });
      acts.appendChild(rename);
      acts.appendChild(action('Unpair', tv, function (button) { unpair(tv, button); }));
      row.appendChild(acts);
      return rename;
    }

    function edit(tv) {
      var row = tv.row;
      row.textContent = '';
      var form = el('form', 'dev-edit');
      form.noValidate = true;
      var input = el('input', 'inp');
      input.value = tv.name;
      input.maxLength = 40;
      input.setAttribute('aria-label', 'TV name');
      input.autocomplete = 'off';
      var save = el('button', 'btn btn--primary btn--sm', 'Save');
      save.type = 'submit';
      var cancel = el('button', 'btn btn--ghost btn--sm', 'Cancel');
      cancel.type = 'button';
      [input, save, cancel].forEach(function (node) { form.appendChild(node); });
      row.appendChild(form);
      function shut() {
        if (isBusy(save)) return;
        var held = MM.dom.focusStillIn(row);
        var opener = fill(tv);
        if (held) opener.focus({ preventScroll: true });
      }
      cancel.addEventListener('click', shut);
      form.addEventListener('keydown', function (e) { if (e.key === 'Escape') shut(); });
      input.addEventListener('input', function () { input.removeAttribute('aria-invalid'); });
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        if (isBusy(save)) return;
        var name = input.value.trim();
        if (!name) { input.setAttribute('aria-invalid', 'true'); input.focus(); return; }
        if (name === tv.name) { shut(); return; }
        setBusy(save, 'Saving…', true);
        MM.api.patch('/api/tvs/' + tv.id, { name: name }).then(function (data) {
          setBusy(save, '');
          tv.name = data.tv.name;
          shut();
          done('Renamed to ' + tv.name + '.');
        }, function (err) {
          setBusy(save, '');
          if (err && err.status === 404) { drop(tv); UIFeedback.error(tv.name + ' is no longer paired.'); return; }
          UIFeedback.error(MM.api.messageOf(err, 'Could not rename it. Try again.'));
        });
      });
      input.focus({ preventScroll: true });
      input.select();
    }

    function drop(tv) {
      gone[tv.id] = true;
      if (!tv.row.parentNode) return;
      var list = tv.row.parentNode;
      var section = list === mine.querySelector('.dev-list') ? mine : others;
      tv.row.inert = true;
      MM.settings.rows.leave(tv.row, {
        focusSelector: '.btn', fallback: list,
        done: function () {
          if (list.children.length) return;
          var held = MM.dom.focusStillIn(section);
          section.hidden = true;
          if (held) document.getElementById('account-sessions').focus({ preventScroll: true });
        },
      });
    }

    function unpair(tv, button) {
      var theirs = others && others.contains(tv.row);
      var sure = theirs ? UIFeedback.confirm({
        title: 'Unpair ' + tv.name + '?',
        body: tv.owner + ' won’t be able to play on it until they pair it again.',
        confirmLabel: 'Unpair',
        confirmVariant: 'danger',
      }) : Promise.resolve(true);
      sure.then(function (yes) {
        if (!yes || isBusy(button)) return;
        setBusy(button, 'Unpairing…', true);
        MM.api.delete('/api/tvs/' + tv.id).then(function () { return true; }, function (err) {
          if (err && err.status === 404) return true;
          setBusy(button, '');
          UIFeedback.error(MM.api.messageOf(err, 'Could not unpair it. Try again.'));
          return false;
        }).then(function (ok) {
          if (!ok) return;
          if (MM.tvRemote) MM.tvRemote.drop(tv.id);
          drop(tv);
          done('Unpaired. That TV now says it was signed out.');
        });
      });
    }

    function draw(section, tvs) {
      var list = section.querySelector('.dev-list');
      // A name being typed, or a press on its way, is left alone.
      if (list.querySelector('form') || list.querySelector('[aria-disabled="true"]')) return;
      var held = document.activeElement && list.contains(document.activeElement);
      list.textContent = '';
      tvs.forEach(function (tv) {
        tv.row = list.appendChild(el('li', 'dev dev--tv'));
        fill(tv);
      });
      section.hidden = !tvs.length;
      if (held) (tvs.length ? list : document.getElementById('account-sessions')).focus({ preventScroll: true });
    }

    function ask(path, section, take) {
      var turn = asked;
      return MM.api.get(path).then(function (data) {
        if (turn !== asked) return;
        draw(section, data.tvs.filter(function (tv) {
          return !gone[tv.id] && take(tv);
        }));
      }, function () { /* A list that was not shown stays so. */ });
    }

    function load() {
      asked += 1;
      ask('/api/tvs', mine, function (tv) { return tv.kind !== 'older'; });
      if (others) ask('/api/tvs/all', others, function (tv) { return tv.kind === 'page' && tv.owner && tv.owner !== me; });
    }

    var me = mine.getAttribute('data-me');
    load();
    return load;
  }

  function initAi() {
    var section = document.getElementById('account-ai');
    var toggle = document.getElementById('account-history');
    var clear = document.getElementById('account-clear-searches');
    if (!section || !toggle || !clear) return;
    var HISTORY = '/api/search/assist/history';
    var on = false;

    function draw(enabled) {
      toggle.classList.toggle('on', enabled);
      toggle.setAttribute('aria-checked', String(enabled));
    }

    function take(enabled) {
      on = enabled;
      draw(on);
    }

    function saved() {
      done(on ? 'The AI uses your history.' : 'The AI no longer uses your history.');
    }

    MM.api.get(HISTORY).then(function (data) {
      take(Boolean(data.enabled));
      section.hidden = !data.available;
    }, function () { /* Stays hidden. */ });

    toggle.addEventListener('click', function () {
      if (isBusy(toggle)) return;
      var wanted = !on;
      toggle.setAttribute('aria-disabled', 'true');
      draw(wanted);
      MM.api.put(HISTORY, { enabled: wanted }).then(function () {
        take(wanted);
        saved();
      }, function (err) {
        draw(on);
        // Where the answer was lost it may have been saved all the same.
        return MM.api.get(HISTORY).then(function (data) {
          take(Boolean(data.enabled));
        }, function () {}).then(function () {
          if (on === wanted) saved();
          else UIFeedback.error(MM.api.messageOf(err, 'Could not save. Try again.'));
        });
      }).then(function () { toggle.removeAttribute('aria-disabled'); });
    });

    clear.addEventListener('click', function () {
      if (isBusy(clear)) return;
      UIFeedback.confirm({
        title: 'Clear your recent searches?',
        body: 'The phrases and answers the AI made for you go too.',
        confirmLabel: 'Clear',
        confirmVariant: 'danger',
      }).then(function (ok) {
        if (!ok) return;
        setBusy(clear, 'Clearing…', true);
        MM.api.delete('/api/search/assist/searches').then(function () {
          // With the question the search remembers for this tab.
          try { window.sessionStorage.removeItem('mm.watch.ask'); } catch (e) { /* storage blocked */ }
          done('Recent searches cleared.');
        }, function (err) {
          UIFeedback.error(MM.api.messageOf(err, 'Could not clear them. Try again.'));
        }).then(function () { setBusy(clear, ''); });
      });
    });
  }

  function start() {
    var reloadSessions = initSessions();
    var reloadTvs = initTvs();
    initPassword(function () {
      reloadSessions();
      reloadTvs();
    });
    initEmail();
    initAi();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
