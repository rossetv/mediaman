/**
 * Profile page — change-password and email forms, and the list of sign-ins.
 *
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom), core/reauth.js (MM.reauth).
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

  // The Plex card asks for a password only where one is due.
  function confirmed(action) {
    function tell() { document.dispatchEvent(new Event('mm:reauth')); }
    var run = MM.reauth.run(action);
    run.then(tell, tell);
    return run;
  }

  // Not `disabled`: that would drop the focus, which the password dialog returns to its button.
  function setBusy(button, busy) {
    if (busy) button.setAttribute('aria-disabled', 'true');
    else button.removeAttribute('aria-disabled');
  }
  function isBusy(button) { return button.getAttribute('aria-disabled') === 'true'; }

  // On a narrow phone a status wraps under its button, where the bottom bar may cover it.
  function reveal(result) { result.scrollIntoView({ block: 'nearest' }); }

  // Say an answer in a status, which the new words may make taller.
  function answer(result, scope, text, tone) {
    var there = MM.dom.stillAt(result, scope);
    result.className = 'inline-form-msg ' + tone;
    result.textContent = text;
    if (there) reveal(result);
  }

  function initPassword(reloadSessions) {
    var form = document.getElementById('account-password-form');
    var result = document.getElementById('account-password-result');
    var submit = document.getElementById('account-submit-password');
    if (!form || !result || !submit) return;

    // On a short phone the form is taller than the screen, so the reason is said beside its field.
    function fail(fieldId, text, issues, onlySay) {
      clear();
      var why = document.getElementById(fieldId + '-why');
      if (!why) { why = result; why.className = 'inline-form-msg err'; }
      showReason(why, text, issues);
      if (!onlySay) MM.dom.refuse(document.getElementById(fieldId), why);
    }
    function clear() {
      result.className = 'inline-form-msg';
      result.textContent = '';
      form.querySelectorAll('.fld-why').forEach(function (why) { why.textContent = ''; });
    }

    // A message about what was typed goes once the person types again; a send keeps its message.
    form.addEventListener('input', function () {
      if (!isBusy(submit)) clear();
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (isBusy(submit)) return;
      var oldPw = document.getElementById('account-old-password').value;
      var newPw = document.getElementById('account-new-password').value;
      var conf = document.getElementById('account-confirm-password').value;
      if (!oldPw) { fail('account-old-password', 'Enter your current password.'); return; }
      if (newPw.length < 12) { fail('account-new-password', 'New password must be at least 12 characters.'); return; }
      if (newPw !== conf) { fail('account-confirm-password', "Passwords don't match."); return; }

      clear();
      setBusy(submit, true);
      result.textContent = 'Updating…';
      reveal(result);
      confirmed(function () {
        return MM.api.post('/api/users/change-password', {
          old_password: oldPw,
          new_password: newPw,
        });
      }).then(function () {
        setBusy(submit, false);
        form.reset();
        answer(result, form, 'Password updated.', 'ok');
        // A new password ends every sign-in but this one.
        reloadSessions();
      }).catch(function (err) {
        setBusy(submit, false);
        var code = err && err.error;
        if (code === 'reauth_cancelled') { clear(); return; }
        if (code === 'wrong_password' || code === 'weak_password' || code === 'same_password') {
          var there = MM.dom.stillAt(result, form);
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
    var submit = document.getElementById('account-submit-email');
    var result = document.getElementById('account-email-result');
    if (!form || !input || !submit || !result) return;

    function say(text, tone) {
      result.className = 'inline-form-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
    }

    form.addEventListener('input', function () {
      if (!isBusy(submit)) say('');
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (isBusy(submit)) return;
      var email = input.value.trim();
      setBusy(submit, true);
      say('Saving…');
      reveal(result);
      confirmed(function () {
        return MM.api.patch('/api/users/me/email', { email: email });
      }).then(function () {
        setBusy(submit, false);
        answer(result, form, email ? 'Email saved.' : 'Email removed. Alerts are off.', 'ok');
      }).catch(function (err) {
        setBusy(submit, false);
        if (err && err.error === 'reauth_cancelled') { say(''); return; }
        var there = MM.dom.stillAt(result, form);
        say(MM.api.messageOf(err, 'Could not save. Try again.'), 'err');
        if (there) MM.dom.refuse(input, result);
      });
    });
  }

  function when(iso) {
    return new Date(iso).toLocaleString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  /* Returns the function that loads the list afresh. */
  function initSessions() {
    var list = document.getElementById('account-sessions');
    var revoke = document.getElementById('account-revoke-others');
    var result = document.getElementById('account-revoke-result');
    var why = document.getElementById('account-revoke-why');
    if (!list || !revoke || !result) return function () {};

    function line(cls, text) {
      var el = document.createElement('div');
      el.className = cls;
      el.textContent = text;
      return el;
    }

    function load() {
      return MM.api.get('/api/users/sessions').then(function (data) {
        // A sign-in that ran out stays listed until it is next used.
        var live = data.sessions.filter(function (s) { return new Date(s.expires_at) > Date.now(); });
        list.textContent = '';
        live.forEach(function (s) {
          var li = document.createElement('li');
          var signedIn = line('profile-session-when', 'Signed in ' + when(s.created_at));
          if (s.current) {
            signedIn.appendChild(document.createTextNode(' '));
            var here = document.createElement('span');
            here.className = 'profile-session-here';
            here.textContent = 'This device';
            signedIn.appendChild(here);
          }
          li.appendChild(signedIn);
          li.appendChild(line('profile-session-used', 'Last active ' + when(s.last_used_at || s.created_at)));
          list.appendChild(li);
        });
        var alone = live.length < 2;
        if (alone && document.activeElement === revoke) list.focus({ preventScroll: true });
        revoke.disabled = alone;
        why.hidden = !alone || Boolean(result.textContent);
      }).catch(function () {
        list.textContent = '';
        list.appendChild(document.createElement('li')).textContent = 'Could not load your sign-ins. Reload the page to try again.';
        // They can still be ended without being listed.
        revoke.disabled = false;
        why.hidden = true;
      }).then(function () { list.removeAttribute('aria-busy'); });
    }

    function say(text, tone) {
      result.className = 'inline-form-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
    }

    revoke.addEventListener('click', function () {
      if (isBusy(revoke)) return;
      setBusy(revoke, true);
      say('Signing out…');
      reveal(result);
      confirmed(function () {
        return MM.api.post('/api/users/sessions/revoke-others');
      }).then(function () {
        answer(result, revoke.parentNode, 'Signed out everywhere else.', 'ok');
        return load();
      }).then(function () {
        setBusy(revoke, false);
      }, function (err) {
        setBusy(revoke, false);
        if (err && err.error === 'reauth_cancelled') { say(''); return; }
        answer(result, revoke.parentNode, MM.api.messageOf(err, 'Could not sign the others out. Try again.'), 'err');
      });
    });

    load();
    return load;
  }

  function start() {
    initPassword(initSessions());
    initEmail();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
