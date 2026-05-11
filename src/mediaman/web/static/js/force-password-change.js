/**
 * Force-password-change page — client-side password meter and sign-out wire.
 *
 * Extracted from force_password_change.html so the inline <script> can
 * be removed and the page-level CSP can drop 'unsafe-inline' once every
 * template has migrated.
 *
 * Requires: core/api.js (MM.api), core/dom.js (MM.dom).
 *
 * The current username is read from a JSON island (#fpc-bootstrap) so
 * no server-side string is interpolated into JavaScript.
 */
(function () {
  'use strict';

  var newPw     = document.getElementById('new_password');
  var confirmPw = document.getElementById('confirm_password');
  var fill      = document.getElementById('pw-meter-fill');
  var label     = document.getElementById('pw-strength-label');
  var matchEl   = document.getElementById('pw-match');
  if (!newPw || !confirmPw || !fill || !label || !matchEl) return;

  var ckLength   = MM.dom.q('.fpc-checklist li[data-rule="length"]');
  var ckClasses  = MM.dom.q('.fpc-checklist li[data-rule="classes"]');
  var ckUsername = MM.dom.q('.fpc-checklist li[data-rule="username"]');
  var ckUnique   = MM.dom.q('.fpc-checklist li[data-rule="unique"]');

  // Read the current username from the server-rendered JSON island so
  // nothing is interpolated into a script tag.
  var username = '';
  try {
    var bootNode = document.getElementById('fpc-bootstrap');
    if (bootNode && bootNode.textContent) {
      var parsed = JSON.parse(bootNode.textContent);
      if (parsed && typeof parsed.username === 'string') username = parsed.username;
    }
  } catch (_err) { username = ''; }

  function uniqueCount(s) { return new Set(s).size; }
  function classCount(s) {
    var n = 0;
    if (/[a-z]/.test(s)) n++;
    if (/[A-Z]/.test(s)) n++;
    if (/\d/.test(s))    n++;
    if (/[^A-Za-z0-9\s]/.test(s)) n++;
    return n;
  }
  function evaluate(pw) {
    var len = pw.length;
    var uniq = uniqueCount(pw);
    var cls = classCount(pw);
    var passphrase = len >= 20 && uniq >= 12;
    var u = (username || '').toLowerCase();
    var p = pw.toLowerCase();
    var hasUser = !!u && pw.length > 0 && (p.includes(u) || (u.length > 3 && u.includes(p)));
    return {
      length:   len >= 12,
      classes:  passphrase || cls >= 3,
      username: !hasUser,
      unique:   uniq >= 6,
    };
  }
  function score(pw, r) {
    if (!pw) return 0;
    var s = 0;
    if (r.length)  s += 1;
    if (r.classes) s += 1;
    if (r.unique)  s += 1;
    if (pw.length >= 16) s += 1;
    if (pw.length >= 20 || classCount(pw) >= 4) s += 1;
    return s;
  }
  function setOk(el, ok) {
    if (!el) return;
    el.classList.toggle('is-ok', ok);
  }

  function updateMatch() {
    var pw = newPw.value;
    var cp = confirmPw.value;
    if (!cp || !pw) {
      matchEl.textContent = ' ';
      matchEl.className = 'fpc-caption';
      return;
    }
    if (cp === pw) {
      matchEl.textContent = 'Passwords match';
      matchEl.className = 'fpc-caption fpc-caption-strong';
    } else {
      matchEl.textContent = 'Passwords don’t match yet';
      matchEl.className = 'fpc-caption fpc-caption-weak';
    }
  }

  function render() {
    var pw = newPw.value;
    if (!pw) {
      fill.style.width = '0%';
      fill.className = 'fpc-meter-fill';
      label.textContent = 'Pick something you’ll remember.';
      label.className = '';
      [ckLength, ckClasses, ckUsername, ckUnique].forEach(function (el) { setOk(el, false); });
      updateMatch();
      return;
    }
    var r = evaluate(pw);
    setOk(ckLength,   r.length);
    setOk(ckClasses,  r.classes);
    setOk(ckUsername, r.username);
    setOk(ckUnique,   r.unique);

    var s = score(pw, r);
    var pct = Math.min(100, (s / 5) * 100);
    fill.style.width = pct + '%';
    var tone = 'weak';
    var text = 'Too weak';
    if (s >= 5)      { tone = 'strong'; text = 'Strong'; }
    else if (s >= 3) { tone = 'ok';     text = 'Getting there'; }
    fill.className = 'fpc-meter-fill ' + tone;
    label.textContent = text;
    label.className = 'fpc-caption-' + tone;
    updateMatch();
  }

  newPw.addEventListener('input', render);
  confirmPw.addEventListener('input', updateMatch);

  var signOutLink = document.getElementById('fpc-signout');
  if (signOutLink) {
    signOutLink.addEventListener('click', function (e) {
      e.preventDefault();
      MM.api.post('/api/auth/logout')
        .catch(function () {})
        .finally(function () { window.location.href = '/login'; });
    });
  }
})();
