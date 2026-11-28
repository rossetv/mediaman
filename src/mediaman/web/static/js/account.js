/**
 * Profile pane — its hub, the change-password and email views, the lists of sign-ins and TVs, and the
 * use of the person's history by the AI. Typing left in the email or password view is guarded.
 *
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom), core/reauth.js (MM.reauth),
 * settings/rows.js (MM.settings.rows), password-form.js (MM.password), ui-feedback.js (UIFeedback),
 * watch/you.js (MM.you); downloading.js (MM.downloading) where it is on the page.
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

  // Within the view, the one thing in the pane that scrolls; a status may wrap under the foot that stays.
  function reveal(result) { result.scrollIntoView({ block: 'nearest' }); }

  // Say an answer in a status, which the new words may make taller.
  function answer(result, scope, text, tone) {
    var there = MM.dom.stillAt(result, scope);
    result.className = 'me-msg ' + tone;
    result.textContent = text;
    if (there) reveal(result);
  }

  function done(text) { UIFeedback.toast(text, { variant: 'success' }); }

  // What a row of the hub says of the view it opens.
  function rowSays(row, text) { row.querySelector('[data-row-value]').textContent = text; }

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
  // Returns whether anything is typed, what empties the form, and what says afresh what a new password signs out.
  function initPassword(devices) {
    var form = document.getElementById('account-password-form');
    var sub = document.getElementById('account-password-sub');
    var live = document.getElementById('account-password-live');
    var none = { typed: function () { return false; }, clear: function () {}, sayDevices: function () {}, opened: function () {} };
    if (!form || !sub || !live) return none;
    var submit = form.querySelector('[type="submit"]');
    var row = document.getElementById('account-open-password');
    var changed = document.getElementById('account-password-changed');
    if (changed) {
      changed.textContent = dayOf(changed.getAttribute('datetime'));
      if (row) rowSays(row, 'Changed ' + changed.textContent);
    }
    // Its script did not load: the rest of the pane works without it.
    if (!MM.password) {
      submit.disabled = true;
      return none;
    }
    var effect = sub.lastElementChild;
    var unknown = effect.textContent;
    var fresh = false;

    var pw = MM.password.form(form, { send: send, hold: true });

    function sayDevices() {
      if (fresh) return;
      var others = devices.others(), tvs = devices.tvs();
      effect.textContent = others && tvs !== null ? willSignOut(others.length, tvs) : unknown;
    }

    // This device and those that go out with the change, as many as there is room for.
    function deviceRow() {
      var others = devices.others();
      var tvs = devices.tvs();
      if (!others || tvs === null || !(others.length + tvs)) return null;
      var line = el('div', 'pwd-dev-row');
      line.setAttribute('aria-hidden', 'true');
      function add(icon, cls) { line.appendChild(el('span', 'pwd-dev-i' + cls)).appendChild(MM.dom.icon(icon)); }
      add(devices.here(), ' is-here');
      var total = others.length + tvs;
      var room = total > 5 ? 4 : total;
      var sets = Math.min(tvs, Math.max(room - others.length, total > 5 ? 1 : 0));
      others.slice(0, room - sets).forEach(function (label) { add(deviceIcon(label), ''); });
      for (var i = 0; i < sets; i++) add('i-tv', '');
      if (total > room) line.appendChild(el('span', 'pwd-dev-n', '+' + (total - room)));
      return line;
    }

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
      sub.textContent = '';
      var now = sub.appendChild(el('span', 'is-fresh'));
      now.appendChild(MM.dom.icon('i-check'));
      now.appendChild(document.createTextNode('Changed just now.'));
      sub.appendChild(document.createTextNode(' '));
      sub.appendChild(el('span', '', words));
      sub.classList.remove('is-new');
      void sub.offsetHeight;
      sub.classList.add('is-new');
      if (row) rowSays(row, 'Changed just now');
      MM.password.celebrate(form.parentNode, form, 'Password changed', words, deviceRow()).then(function (undo) {
        // Long enough to be read before the form is back, empty.
        setTimeout(function () {
          var held = MM.dom.focusStillIn(form.parentNode);
          devices.reload();
          undo();
          pw.reset();
          if (held) pw.old.focus({ preventScroll: true });
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

    return {
      typed: function () { return Boolean(pw.old.value || pw.fresh.value); },
      clear: function () { if (!pw.isBusy()) pw.reset(); },
      sayDevices: sayDevices,
      opened: function () { if (!pw.isBusy() && form.isConnected) pw.old.focus({ preventScroll: true }); },
    };
  }

  // Returns whether the field differs from what is saved, and what puts it back.
  function initEmail() {
    var form = document.getElementById('account-email-form');
    var input = document.getElementById('account-email');
    var submit = document.getElementById('account-submit-email');
    var result = document.getElementById('account-email-result');
    if (!form || !input || !submit || !result) return { changed: function () { return false; }, undo: function () {} };
    var row = document.getElementById('account-row-email');
    var head = document.getElementById('account-head-email');
    var saved = input.defaultValue;

    function changed() { return input.value.trim() !== saved; }

    function say(text, tone) {
      result.className = 'me-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
      input.setAttribute('aria-invalid', String(tone === 'err'));
    }

    // Save is there to press only for a change; a press it takes away leaves the focus in the field.
    function follow() {
      if (isBusy(submit)) return;
      var off = !changed();
      if (off && document.activeElement === submit) input.focus({ preventScroll: true });
      submit.disabled = off;
    }

    function shown(email) {
      if (row) rowSays(row, email || 'Not set');
      if (!head) return;
      head.textContent = email;
      head.hidden = !email;
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

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = input.value.trim();
      if (isBusy(submit) || !changed()) return;
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
        shown(email);
        done(email ? 'Email saved.' : 'Email removed. Alerts are off.');
      }).catch(function (err) {
        setBusy(submit, '');
        say('');
        follow();
        if (err && err.error === 'reauth_cancelled') return;
        refuse(MM.api.messageOf(err, 'Could not save. Try again.'), MM.dom.stillAt(submit, form));
      });
    });

    // A reload may keep what was typed.
    follow();
    return {
      changed: changed,
      undo: function () {
        if (isBusy(submit)) return;
        input.value = saved;
        say('');
        follow();
      },
    };
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
    if (!list || !revoke || !result) return { load: function () {}, count: function () { return null; }, others: function () { return null; }, here: function () { return 'i-globe'; } };
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
      count: function () { return loaded ? listed().length : null; },
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

    function namesOf(people) {
      return people.length > 1 ? people.slice(0, -1).join(', ') + ' and ' + people[people.length - 1] : people[0];
    }

    // Everyone else it is paired with, as the list gives them.
    function othersOn(tv) {
      return (tv.people || []).filter(function (who) { return who !== me; });
    }

    // One's own TV that others are paired with too goes on for them: one only leaves it.
    function leaves(tv) {
      return !(others && others.contains(tv.row)) && othersOn(tv).length > 0;
    }

    function stateOf(tv) {
      var seen = tv.on ? 'On now' : tv.last_seen_at ? 'Last seen ' + seenWhen(tv.last_seen_at) : 'Not seen yet';
      var theirs = others && others.contains(tv.row);
      var withThem = othersOn(tv);
      if (theirs && withThem.length) return seen + '. Paired with ' + namesOf(withThem) + '.';
      if (withThem.length) return seen + '. Shared with ' + namesOf(withThem) + '.';
      return seen + '. Paired' + (theirs ? ' by ' + tv.owner : '') + ' ' + dayOf(tv.paired_at) + '.';
    }

    function action(words, kind, tv, run) {
      var button = el('button', 'btn btn--' + kind + ' btn--sm', words);
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
      var rename = action('Rename', 'ghost', tv, function () { edit(tv); });
      acts.appendChild(rename);
      acts.appendChild(action(leaves(tv) ? 'Leave' : 'Unpair', 'danger', tv, function (button) { unpair(tv, button); }));
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
      [input, cancel, save].forEach(function (node) { form.appendChild(node); });
      row.appendChild(form);
      function shut() {
        if (isBusy(save)) return;
        var held = MM.dom.focusStillIn(row);
        var opener = fill(tv);
        if (held) opener.focus({ preventScroll: true });
      }
      cancel.addEventListener('click', shut);
      form.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        // The pane is left as it is.
        e.preventDefault();
        shut();
      });
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
        body: (othersOn(tv).length ? namesOf(othersOn(tv)) : tv.owner) + ' won’t be able to play on it until it is paired again.',
        confirmLabel: 'Unpair',
        confirmVariant: 'danger',
      }) : Promise.resolve(true);
      sure.then(function (yes) {
        if (!yes || isBusy(button)) return;
        setBusy(button, leaves(tv) ? 'Leaving…' : 'Unpairing…', true);
        var stopped = '';
        // Asked now, not from the list: one's own film may have begun on it since.
        (leaves(tv) ? MM.api.get('/api/tvs/' + tv.id).then(function (data) {
          var p = data && data.tv && data.tv.play;
          if (p && p.mine && ['ended', 'failed'].indexOf(p.state) === -1) stopped = p.title;
        }, function () { /* Left all the same. */ }) : Promise.resolve()).then(function () {
          return MM.api.delete('/api/tvs/' + tv.id);
        }).then(function () { return true; }, function (err) {
          if (err && err.status === 404) return true;
          setBusy(button, '');
          UIFeedback.error(MM.api.messageOf(err, leaves(tv) ? 'Could not leave it. Try again.' : 'Could not unpair it. Try again.'));
          return false;
        }).then(function (ok) {
          if (!ok) return;
          if (MM.tvRemote) MM.tvRemote.drop(tv.id);
          drop(tv);
          done(leaves(tv) ? 'You left ' + tv.name + '. ' + (stopped ? stopped + ' stopped; your place is saved. ' : '') + namesOf(othersOn(tv)) + ' can still play on it.'
            : 'Unpaired. That TV now says it was signed out.');
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
          return !gone[tv.id] && (!take || take(tv));
        }));
        if (section !== mine) return;
        known = true;
        told();
      }, function () { /* A list that was not shown stays so. */ });
    }

    function load() {
      asked += 1;
      ask('/api/tvs', mine);
      if (others) ask('/api/tvs/all', others, function (tv) { return tv.kind === 'page' && tv.owner && (tv.people || [tv.owner]).indexOf(me) < 0; });
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
    var more = document.getElementById('account-history-more');
    if (more) MM.dom.fold(more, document.getElementById('account-history-more-toggle'));
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

    var auto = document.getElementById('account-auto');
    var drawAuto = function (enabled) {
      auto.classList.toggle('on', enabled);
      auto.setAttribute('aria-checked', String(enabled));
    };
    if (auto) {
      auto.addEventListener('click', function () {
        if (isBusy(auto)) return;
        var was = auto.getAttribute('aria-checked') === 'true';
        auto.setAttribute('aria-disabled', 'true');
        drawAuto(!was);
        MM.api.put(HISTORY, { auto: !was }).then(function () {
          // An open search asks, or waits for Return, as the new setting says.
          var ask = window.MMWatch && window.MMWatch.ask;
          if (ask && ask.load) ask.load();
        }, function (err) {
          drawAuto(was);
          UIFeedback.error(MM.api.messageOf(err, 'Could not save. Try again.'));
        }).then(function () { auto.removeAttribute('aria-disabled'); });
      });
    }

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

  // How many sign-ins and TVs there are, on the hub's row of them.
  function devicesSay(sessions, tvs) {
    var n = sessions.count(), t = tvs.count();
    if (n === null) return '';
    return n + (n === 1 ? ' sign-in' : ' sign-ins') + (t ? ' · ' + t + (t === 1 ? ' TV' : ' TVs') : '');
  }

  function initLogout() {
    var form = document.getElementById('account-logout');
    if (!form) return;
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      // Already logging out: asking again would only stand a second confirm over the page leaving.
      if (document.documentElement.classList.contains('is-leaving')) return;
      UIFeedback.confirm({
        title: 'Log out?',
        body: 'You will need your password to come back.',
        confirmLabel: 'Log out',
        confirmVariant: 'danger',
        leave: true,
      }).then(function (yes) { if (yes) window.MM.logOut(form, form.closest('.you__panel')); });
    });
  }

  /* The tiles of the hub: History's latest event and, for an admin, how the services stood when Settings
     last checked them. The hub never checks a service itself: the checks share one rate limit with Settings.
     Each tile is said again as the hub shows; the newest answer wins. */

  function lastWhen(iso) {
    var at = new Date(iso);
    if (isNaN(at)) return '';
    var today = new Date(Date.now());
    if (at.toDateString() === today.toDateString()) return at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    return at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  }

  // The server's labels that read as a verb before a title; the others follow the title.
  var VERBS = ['downloaded', 're-downloaded', 'deleted', 'kept', 'scheduled'];

  // "marlowe finished Interstellar · 21:04". A security event is named by its title, and an event whose
  // label is no verb as "Title: label".
  function eventLine(item, me) {
    var name = item.who && item.who.name;
    var mine = name === me;
    var verb = item.action === 'play' ? (item.finished ? 'finished' : 'started') : item.action_label;
    var words;
    if (item.is_security) words = mine ? item.line_title : item.line_title + ' by ' + (name || 'a former user');
    else if (item.action === 'play' || VERBS.indexOf(verb) >= 0) words = (mine ? 'You' : name || 'A former user') + ' ' + verb + ' ' + item.line_title;
    else words = item.line_title + ': ' + verb;
    var at = (item.fields || []).filter(function (f) { return f.at; })[0];
    var time = at ? lastWhen(at.at) : '';
    return time ? words + ' · ' + time : words;
  }

  function initTiles() {
    var history = document.getElementById('account-tile-history');
    var settings = document.getElementById('account-tile-settings');
    var neutral = settings ? settings.textContent : '';
    if (!history) return function () {};
    var asked = 0;

    function sayHistory(me) {
      MM.api.get('/api/history?per_page=20').then(function (data) {
        if (me !== asked) return;
        var items = data.items || [];
        var shown = items.filter(function (i) { return !i.is_security; })[0] || items[0];
        history.textContent = shown ? eventLine(shown, history.getAttribute('data-me')) : 'Nothing yet';
      }, function () { /* It keeps what it says. */ });
    }

    // From the pills Settings' own checks left in the page. A check refused for the rate limit, or not yet
    // answered, says nothing either way; with any such, or Settings not yet loaded, the tile keeps its line.
    function saySettings() {
      var tones = {};
      Array.prototype.forEach.call(document.querySelectorAll('[data-conn]'), function (pill) {
        var tone = ['ok', 'err', 'off'].filter(function (t) { return pill.classList.contains(t); })[0] || 'unknown';
        // A service that answered and turned the key away is not one that does not answer.
        if (tone === 'err' && pill.classList.contains('is-refused')) tone = 'refused';
        tones[pill.getAttribute('data-conn')] = tone;
      });
      var known = Object.keys(tones).map(function (k) { return tones[k]; });
      var down = known.filter(function (t) { return t === 'err'; }).length;
      var refused = known.filter(function (t) { return t === 'refused'; }).length;
      var bad = down + refused;
      var keys = refused === 1 ? ' refused its key' : ' refused their keys';
      var text = down && refused ? down + ' not answering, ' + refused + keys
        : refused ? refused + (refused === 1 ? ' service' : ' services') + keys
        : down ? (down === 1 ? '1 service is not answering' : down + ' services are not answering')
        : !known.length || known.indexOf('unknown') !== -1 ? neutral
        : known.indexOf('ok') === -1 ? 'No services are set up yet'
        : 'Everything is connected';
      settings.textContent = text;
      settings.classList.toggle('is-bad', !!bad);
    }

    return function () {
      var me = ++asked;
      sayHistory(me);
      if (settings) saySettings();
    };
  }

  // While something downloads the hub leads to it, as the ring round the picture does: on a phone
  // the ring is too thin to tap. The pane closes first; the ring's own link then opens it.
  function initDownloading() {
    var row = document.getElementById('account-dl');
    if (!row || !MM.downloading) return;
    var link = row.querySelector('a');
    MM.downloading.subscribe(function (state) {
      var sum = state.summary;
      row.hidden = !sum.total;
      if (!sum.total) return;
      // What can be watched now matters more than how far the rest has come.
      rowSays(link, state.lost ? 'Not updating' : sum.ready ? sum.ready + ' ready to watch'
        : sum.active ? sum.percent + '%' : sum.waiting + ' waiting');
    });
    link.addEventListener('click', function (e) {
      var ring = document.querySelector('.profile-dl');
      if (!ring || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      function closed() {
        document.removeEventListener('mm:you-hidden', closed);
        // Once the step back that closed the pane is over.
        setTimeout(function () { ring.click(); }, 0);
      }
      document.addEventListener('mm:you-hidden', closed);
      MM.you.close();
    });
  }

  // Marks the pane's root as wired by `name`; false where it already was.
  function mount(root, name) {
    var on = (root.getAttribute('data-mounted') || '').split(' ').filter(Boolean);
    if (on.indexOf(name) >= 0) return false;
    root.setAttribute('data-mounted', on.concat(name).join(' '));
    return true;
  }

  function start() {
    var root = document.querySelector('[data-you-group="profile"]');
    if (!root || !mount(root, 'account')) return;
    var devicesRow = document.getElementById('account-row-devices');
    var password = null;
    function told() {
      if (password) password.sayDevices();
      if (devicesRow) rowSays(devicesRow, devicesSay(sessions, tvs));
    }
    var sessions = initSessions(told);
    var tvs = initTvs(told);
    password = initPassword({
      others: sessions.others, here: sessions.here, tvs: tvs.count,
      reload: function () {
        sessions.load();
        tvs.load();
      },
    });
    var email = initEmail();
    var tiles = initTiles();
    initAi();
    initLogout();
    initDownloading();

    document.addEventListener('mm:you-shown', function (e) {
      if (!e.detail || e.detail.group !== 'profile') return;
      if (e.detail.view === 'profile') tiles();
      else if (e.detail.view === 'profile/password') password.opened();
    });

    MM.you.guard('profile', {
      dirty: function (from, to) {
        if (to === from) return false;
        return (from === 'profile/email' && email.changed()) || (from === 'profile/password' && password.typed());
      },
      discard: function () {
        email.undo();
        password.clear();
      },
      title: 'Discard what you typed?',
      body: 'Your email and password stay as they were.',
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
