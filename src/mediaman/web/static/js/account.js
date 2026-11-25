/**
 * Profile page — change-password and email forms, the lists of sign-ins and TVs, and the use of the
 * person's history by the AI.
 *
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom), core/reauth.js (MM.reauth),
 * settings/rows.js (MM.settings.rows), password-form.js (MM.password), ui-feedback.js (UIFeedback).
 */
(function () {
  'use strict';

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

  // What saving a new password will do, and what it did. `others` and `tvs` are how many there are.
  function willSignOut(others, tvs) {
    var what = [];
    if (others) what.push(others === 1 ? 'your other device' : 'your ' + others + ' other devices');
    if (tvs) what.push(tvs === 1 ? 'your TV' : 'your ' + tvs + ' TVs');
    return what.length ? 'Signs out ' + what.join(' and ') + '.' : 'You have no other device to sign out.';
  }
  function signedOut(others, tvs) {
    var what = [];
    if (others) what.push((others === 1 ? 'Your other device was' : 'Your ' + others + ' other devices were') + ' signed out');
    if (tvs) what.push((others ? 'your ' : 'Your ') + (tvs === 1 ? 'TV was' : tvs + ' TVs were') + ' unpaired');
    return what.length ? what.join(', and ') + '.' : 'You had no other device to sign out.';
  }

  // `devices.others()` and `devices.tvs()` are what the lists below hold, null where they did not load.
  // Returns the function that says afresh what a new password signs out.
  function initPassword(devices) {
    var form = document.getElementById('account-password-form');
    var opener = document.getElementById('account-open-password');
    var panel = document.getElementById('account-password-fold');
    var sub = document.getElementById('account-password-sub');
    var live = document.getElementById('account-password-live');
    if (!form || !opener || !panel || !sub || !live) return function () {};
    // Its script did not load: the rest of the page works without it.
    if (!MM.password) {
      opener.disabled = true;
      return function () {};
    }
    var line = opener.closest('.me-line');
    var end = opener.parentNode;
    var fold = MM.settings.rows;
    var changed = document.getElementById('account-password-changed');
    var effect = sub.lastElementChild;
    var fresh = false;
    if (changed) changed.textContent = dayOf(changed.getAttribute('datetime'));

    var pw = MM.password.form(form, { send: send });
    var says = pw.aside.querySelector('.pwd-line').lastElementChild;
    var unknown = says.textContent;

    function sayDevices() {
      var others = devices.others(), tvs = devices.tvs();
      says.textContent = others && tvs !== null ? willSignOut(others.length, tvs) : unknown;
    }

    // This device and those that go out with the change, as many as there is room for.
    function deviceRow() {
      var others = devices.others();
      var tvs = devices.tvs();
      if (!others || tvs === null || !(others.length + tvs)) return null;
      var row = el('div', 'pwd-dev-row');
      row.setAttribute('aria-hidden', 'true');
      function add(icon, cls) { row.appendChild(el('span', 'pwd-dev-i' + cls)).appendChild(MM.dom.icon(icon)); }
      add(devices.here(), ' is-here');
      var total = others.length + tvs;
      var room = total > 5 ? 4 : total;
      var sets = Math.min(tvs, Math.max(room - others.length, total > 5 ? 1 : 0));
      others.slice(0, room - sets).forEach(function (label) { add(deviceIcon(label), ''); });
      for (var i = 0; i < sets; i++) add('i-tv', '');
      if (total > room) row.appendChild(el('span', 'pwd-dev-n', '+' + (total - room)));
      return row;
    }

    // The row takes its new height as the fold takes its own, with its words and button already in place.
    function ease(change) {
      var from = line.offsetHeight + (parseFloat(getComputedStyle(line).marginBottom) || 0);
      change();
      var gap = from - line.offsetHeight;
      line.style.transition = 'none';
      line.style.marginBottom = gap + 'px';
      sub.classList.remove('is-new');
      void line.offsetHeight;
      sub.classList.toggle('is-new', gap !== 0);
      line.style.transition = '';
      line.style.marginBottom = '';
    }

    function show() {
      if (pw.isBusy()) return;
      opener.setAttribute('aria-expanded', 'true');
      var width = sub.getBoundingClientRect().width;
      ease(function () {
        // Where the button leaves its room to the words, it fades where it was.
        end.style.top = end.offsetTop + 'px';
        opener.classList.add('is-gone');
        // The line by the buttons says it, with the numbers.
        if (!changed || fresh) return;
        // On the lines it had beside the button.
        sub.style.maxWidth = width + 'px';
        effect.hidden = true;
      });
      sayDevices();
      fold.openInView(panel);
      pw.old.focus({ preventScroll: true });
    }
    function shut(then, words) {
      var held = MM.dom.focusStillIn(panel);
      opener.setAttribute('aria-expanded', 'false');
      ease(function () {
        opener.classList.remove('is-gone');
        sub.style.maxWidth = '';
        if (!fresh) effect.hidden = false;
        if (!words) return;
        sub.appendChild(document.createTextNode(' '));
        sub.appendChild(el('span', '', words));
      });
      fold.close(panel, then || pw.reset);
      if (held) opener.focus({ preventScroll: true });
    }

    opener.addEventListener('click', show);
    pw.cancel.addEventListener('click', function () {
      if (!pw.isBusy()) shut();
    });
    panel.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !pw.isBusy()) shut();
    });

    function send(values) {
      reveal(pw.submit);
      confirmed('change it', function () {
        return MM.api.post('/api/users/change-password', { old_password: values.old, new_password: values.pw });
      }).then(function (data) { saved(data || {}); }, function (err) { refused(values, err); });
    }

    function saved(data) {
      var others = devices.others();
      var known = typeof data.signed_out === 'number' && Array.isArray(data.unpaired);
      var words = known ? signedOut(data.signed_out, data.unpaired.length)
        : others && devices.tvs() !== null ? signedOut(others.length, devices.tvs())
          : 'Your other devices were signed out, and your TVs unpaired.';
      pw.forget();
      fresh = true;
      live.textContent = 'Password changed. ' + words;
      ease(function () {
        sub.textContent = sub.style.maxWidth = '';
        var now = sub.appendChild(el('span', 'is-fresh'));
        now.appendChild(MM.dom.icon('i-check'));
        now.appendChild(document.createTextNode('Changed just now.'));
      });
      MM.password.celebrate(form.parentNode, form, 'Password changed', words, deviceRow()).then(function (undo) {
        // Long enough to be read before it folds.
        setTimeout(function () {
          devices.reload();
          shut(function () {
            undo();
            pw.reset();
          }, words);
        }, 2000);
      });
    }

    function refused(values, err) {
      pw.free();
      var code = err && err.error;
      if (code === 'reauth_cancelled') return;
      var there = MM.dom.stillAt(pw.submit, form);
      var text = MM.api.messageOf(err, 'Something went wrong. Try again.');
      if (code === 'wrong_password') pw.refuse(pw.old, text, !there);
      else if (code === 'weak_password' || code === 'same_password') {
        pw.refuseNew(values.pw, text, err.issues, err.data && err.data.rules, !there);
      } else pw.refuseWhole(text, !there);
    }

    return sayDevices;
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

  /* `told` runs as the list changes. Returns the function that loads it afresh, with the labels of the
     other devices on it (null where it did not load) and the icon of this one. */
  function initSessions(told) {
    var list = document.getElementById('account-sessions');
    var revoke = document.getElementById('account-revoke-others');
    var result = document.getElementById('account-revoke-result');
    var why = document.getElementById('account-revoke-why');
    if (!list || !revoke || !result) return { load: function () {}, others: function () { return null; }, here: function () { return 'i-globe'; } };
    /* The sign-ins on show by the same names as their rows. */
    var shown = {};
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
      delete shown[key];
      // A row on its way out takes no press.
      row.inert = true;
      MM.settings.rows.leave(row, { focusSelector: '.btn', fallback: list });
    }

    function tellAlone() {
      var alone = Object.keys(rows).length < 2;
      if (alone && document.activeElement === revoke) list.focus({ preventScroll: true });
      revoke.disabled = alone;
      why.hidden = !alone || Boolean(result.textContent);
      told();
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
          shown[key] = s;
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
        shown = {};
        loaded = false;
        list.textContent = '';
        list.appendChild(el('li', 'dev dev--note', 'Could not load your sign-ins. Reload the page to try again.'));
        // They can still be ended without being listed.
        revoke.disabled = false;
        why.hidden = true;
        told();
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

    function listed() { return Object.keys(shown).map(function (key) { return shown[key]; }); }

    load();
    return {
      load: load,
      others: function () {
        if (!loaded) return null;
        return listed().filter(function (s) { return !s.current; }).map(function (s) { return s.device_label || ''; });
      },
      here: function () {
        var here = listed().filter(function (s) { return s.current; })[0];
        return deviceIcon((here && here.device_label) || '');
      },
    };
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

  /* The person's TVs and, for an admin, everyone else's. A list with no TV is not shown. `told` runs as
     the person's own change. Returns the function that loads them afresh, with how many of their own
     there are (null where they did not load). */
  function initTvs(told) {
    var mine = document.getElementById('account-tvs');
    var others = document.getElementById('account-tvs-all');
    if (!mine) return { load: function () {}, count: function () { return null; } };
    var known = false;
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
          told();
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
        if (section !== mine) return;
        known = true;
        told();
      }, function () { /* A list that was not shown stays so. */ });
    }

    function load() {
      asked += 1;
      ask('/api/tvs', mine, function (tv) { return tv.kind !== 'older'; });
      if (others) ask('/api/tvs/all', others, function (tv) { return tv.kind === 'page' && tv.owner && tv.owner !== me; });
    }

    var me = mine.getAttribute('data-me');
    load();
    return {
      load: load,
      count: function () { return known ? mine.querySelector('.dev-list').children.length : null; },
    };
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
    var sayDevices = function () {};
    function told() { sayDevices(); }
    var sessions = initSessions(told);
    var tvs = initTvs(told);
    sayDevices = initPassword({
      others: sessions.others, here: sessions.here, tvs: tvs.count,
      reload: function () {
        sessions.load();
        tvs.load();
      },
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
