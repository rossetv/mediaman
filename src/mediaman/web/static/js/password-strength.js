/**
 * Strength meter and match hint for password fields.
 *
 *   data-strength="key"      on an input fills #key-meter and says #key-label
 *   data-match="password-id" on a confirm input says #<its own id>-match
 *
 * Lives in an external file so the page CSP needs no 'unsafe-inline'.
 */
(function () {
  'use strict';

  function classCount(s) {
    var n = 0;
    if (/[a-z]/.test(s)) n++;
    if (/[A-Z]/.test(s)) n++;
    if (/\d/.test(s)) n++;
    if (/[^A-Za-z0-9\s]/.test(s)) n++;
    return n;
  }

  function score(pw) {
    var len = pw.length, uniq = new Set(pw).size, cls = classCount(pw);
    var passphrase = len >= 20 && uniq >= 12;
    var s = 0;
    if (len >= 12) s++;
    if (passphrase || cls >= 3) s++;
    if (uniq >= 6) s++;
    if (len >= 16) s++;
    if (len >= 20 || cls >= 4) s++;
    // The server also refuses common passwords, so the best this can say is "so far".
    var tone = 'weak', label = 'Too simple so far';
    if (s >= 5)      { tone = 'strong'; label = 'Looks good so far'; }
    else if (s >= 3) { tone = 'ok';     label = 'Getting there'; }
    return { pct: (s / 5) * 100, tone: tone, label: label };
  }

  function caption(el, text, tone) {
    el.textContent = text || ' ';
    el.className = 'fpc-caption' + (tone ? ' fpc-caption-' + tone : '');
  }

  // Reset fires before the fields are emptied.
  function watch(input, render) {
    input.addEventListener('input', render);
    if (input.form) input.form.addEventListener('reset', function () { setTimeout(render, 0); });
  }

  document.querySelectorAll('input[data-strength]').forEach(function (input) {
    var key = input.getAttribute('data-strength');
    var meter = document.getElementById(key + '-meter');
    var label = document.getElementById(key + '-label');
    watch(input, function () {
      var r = input.value ? score(input.value) : { pct: 0, tone: '', label: '' };
      meter.style.width = r.pct + '%';
      meter.className = 'fpc-meter-fill' + (r.tone ? ' ' + r.tone : '');
      caption(label, r.label, r.tone);
    });
  });

  document.querySelectorAll('input[data-match]').forEach(function (confirm) {
    var password = document.getElementById(confirm.getAttribute('data-match'));
    var label = document.getElementById(confirm.id + '-match');
    function render() {
      if (!confirm.value || !password.value) caption(label, '', '');
      else if (confirm.value === password.value) caption(label, 'Passwords match', 'strong');
      else caption(label, 'Passwords don’t match yet', 'weak');
    }
    watch(confirm, render);
    watch(password, render);
  });
})();
