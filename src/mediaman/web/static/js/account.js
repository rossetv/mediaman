/**
 * Profile page — change-password and email forms, and the list of sign-ins.
 *
 * Requires: core/api.js (MM.api), core/reauth.js (MM.reauth).
 * The password form has the same checks, reauth retry and error rendering as
 * the Settings › Users form.
 */
(function () {
  'use strict';

  function renderApiError(target, err) {
    var msg = MM.api.messageOf(err, 'Something went wrong. Try again.');
    var issues = err && err.issues;
    target.textContent = '';
    if (issues && issues.length) {
      var p = document.createElement('div');
      p.textContent = msg;
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
      target.textContent = msg;
    }
  }

  function init() {
    var form = document.getElementById('account-password-form');
    var result = document.getElementById('account-password-result');
    var submit = document.getElementById('account-submit-password');
    if (!form || !result || !submit) return;

    // The message is announced by the status region; the cursor goes where the fix is.
    function fail(text, fieldId) {
      result.className = 'inline-form-msg err';
      result.textContent = text;
      document.getElementById(fieldId).focus();
    }

    // A message about what was typed goes once the person types again; a send keeps its message.
    form.addEventListener('input', function () {
      if (submit.disabled || !result.textContent) return;
      result.className = 'inline-form-msg';
      result.textContent = '';
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var oldPw = document.getElementById('account-old-password').value;
      var newPw = document.getElementById('account-new-password').value;
      var conf = document.getElementById('account-confirm-password').value;
      if (!oldPw) { fail('Enter your current password.', 'account-old-password'); return; }
      if (newPw.length < 12) { fail('New password must be at least 12 characters.', 'account-new-password'); return; }
      if (newPw !== conf) { fail("Passwords don't match.", 'account-confirm-password'); return; }

      submit.disabled = true;
      MM.reauth.run(function () {
        return MM.api.post('/api/users/change-password', {
          old_password: oldPw,
          new_password: newPw,
        });
      }).then(function () {
        submit.disabled = false;
        form.reset();
        result.className = 'inline-form-msg ok';
        result.textContent = 'Password updated.';
      }).catch(function (err) {
        submit.disabled = false;
        if (err && err.error === 'reauth_cancelled') {
          result.className = 'inline-form-msg';
          result.textContent = '';
          return;
        }
        result.className = 'inline-form-msg err';
        renderApiError(result, err);
        document.getElementById(
          err && err.error === 'wrong_password' ? 'account-old-password' : 'account-new-password'
        ).focus();
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
      if (!submit.disabled) say('');
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submit.disabled) return;
      var email = input.value.trim();
      submit.disabled = true;
      say('Saving…');
      MM.reauth.run(function () {
        return MM.api.patch('/api/users/me/email', { email: email });
      }).then(function () {
        submit.disabled = false;
        say(email ? 'Email saved.' : 'Email removed. Alerts are off.', 'ok');
      }).catch(function (err) {
        submit.disabled = false;
        if (err && err.error === 'reauth_cancelled') { say(''); return; }
        say(MM.api.messageOf(err, 'Could not save. Try again.'), 'err');
        input.focus();
      });
    });
  }

  function when(iso) {
    return new Date(iso).toLocaleString('en-GB', {
      day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  function initSessions() {
    var list = document.getElementById('account-sessions');
    var revoke = document.getElementById('account-revoke-others');
    var result = document.getElementById('account-revoke-result');
    if (!list || !revoke || !result) return;

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
          li.appendChild(line('profile-session-when', 'Signed in ' + when(s.created_at)));
          li.appendChild(line('profile-session-used', 'Last active ' + when(s.last_used_at || s.created_at)));
          list.appendChild(li);
        });
        revoke.disabled = live.length < 2;
      }).catch(function () {
        list.textContent = '';
        list.appendChild(document.createElement('li')).textContent = 'Could not load your sign-ins. Reload the page to try again.';
        // They can still be ended without being listed.
        revoke.disabled = false;
      }).then(function () { list.removeAttribute('aria-busy'); });
    }

    function say(text, tone) {
      result.className = 'inline-form-msg' + (tone ? ' ' + tone : '');
      result.textContent = text;
    }

    revoke.addEventListener('click', function () {
      revoke.disabled = true;
      say('Signing out…');
      MM.reauth.run(function () {
        return MM.api.post('/api/users/sessions/revoke-others');
      }).then(function () {
        say('Signed out everywhere else.', 'ok');
        return load();
      }).catch(function (err) {
        revoke.disabled = false;
        if (err && err.error === 'reauth_cancelled') { say(''); return; }
        say(MM.api.messageOf(err, 'Could not sign the others out. Try again.'), 'err');
      });
    });

    load();
  }

  function start() {
    init();
    initEmail();
    initSessions();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
