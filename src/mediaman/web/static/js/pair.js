/**
 * Pair a TV: the six numbers the TV shows, typed or from its QR code, then the TV's name.
 * A code from a QR code is only filled in; the person agrees to it.
 *
 * Requires: core/api.js (MM.api), ui-feedback.js (UIFeedback).
 */
(function () {
  'use strict';

  var LATER = 'Something went wrong. Try again.';
  var WHY = {
    wrong_code: 'That code isn’t right, or it has run out. Type the one on the TV now.',
    too_many_tries: 'Too many wrong codes. Wait five minutes, then use a new code.',
    too_many_tvs: 'You have as many TVs as you can. Unpair one in your Profile, then try again.',
  };

  var form = document.getElementById('pair-form');
  var ask = document.getElementById('pair-ask');
  var steps = document.getElementById('pair-steps');
  var boxes = document.getElementById('pair-boxes');
  var code = document.getElementById('pair-code');
  var why = document.getElementById('pair-why');
  var agree = document.getElementById('pair-agree');
  var naming = document.getElementById('pair-name-form');
  var name = document.getElementById('pair-name');
  var nameWhy = document.getElementById('pair-name-why');
  var save = document.getElementById('pair-save');
  var done = document.getElementById('pair-done');
  var hint = why.textContent;
  var pills = [].slice.call(naming.querySelectorAll('.fpill'));
  var busy = false;
  var tv = null;
  var others = [];
  var touched = false;

  function paint() {
    [].forEach.call(boxes.querySelectorAll('i'), function (box, n) {
      box.textContent = code.value.charAt(n);
      box.classList.toggle('at', n === code.value.length && !code.readOnly);
    });
  }

  // `again` is an answer to a press: it is cleared and then written, so a status that has the same
  // words as before is still read out. A hint is written only when its words change.
  function say(node, text, bad, again) {
    var cls = bad ? 'pair__why bad' : 'pair__why';
    clearTimeout(node.pending);
    if (again) {
      node.className = cls;
      node.textContent = '';
      node.pending = setTimeout(function () { node.textContent = text; }, 80);
      return;
    }
    if (node.className === cls && node.textContent === text) return;
    node.className = cls;
    node.textContent = text;
  }

  // `asked` is a code from a QR code, which waits for the person's yes.
  function show(asked) {
    code.value = asked || '';
    code.readOnly = Boolean(asked);
    ask.hidden = agree.hidden = !asked;
    steps.hidden = Boolean(asked);
    boxes.classList.remove('is-bad');
    say(why, hint);
    paint();
    (asked ? agree : code).focus({ preventScroll: true });
  }

  function refuse(text) {
    show('');
    boxes.classList.add('is-bad');
    say(why, text, true, true);
  }

  function setBusy(on) {
    busy = on;
    agree.setAttribute('aria-disabled', String(on));
    code.readOnly = on || !agree.hidden;
    if (on) say(why, 'Pairing…');
    paint();
  }

  function pair(confirmed) {
    if (busy) return;
    var typed = code.value;
    setBusy(true);
    MM.api.post('/api/tvs/pair', { code: typed, confirm: Boolean(confirmed) }).then(function (data) {
      setBusy(false);
      paired(data.tv);
    }, function (err) {
      setBusy(false);
      var kind = err && err.error;
      if (kind === 'in_use') {
        say(why, hint);
        UIFeedback.confirm({
          title: 'Someone else has this TV',
          body: 'A TV is one person’s at a time. Pairing it with you unpairs them' +
            (err.data && err.data.playing ? ', and stops what they’re watching.' : '.'),
          confirmLabel: 'Pair it with me',
        }).then(function (yes) {
          if (yes) pair(true);
          else show('');
        });
      } else if (kind === 'wrong_code' || kind === 'too_many_tries' || kind === 'too_many_tvs') {
        refuse(WHY[kind]);
      } else {
        // The code may still be good: one press sends it again.
        var kept = typed;
        show(kept);
        say(why, MM.api.messageOf(err, LATER), true, true);
      }
    });
  }

  function mark() {
    pills.forEach(function (pill) {
      var on = pill.textContent.trim() === name.value.trim();
      pill.classList.toggle('on', on);
      pill.setAttribute('aria-pressed', String(on));
    });
  }

  function same(a, b) {
    // The server keeps a name with its runs of spaces made one.
    return a.split(/\s+/).join(' ').trim().toLowerCase() === b.split(/\s+/).join(' ').trim().toLowerCase();
  }

  function taken(wanted) {
    return others.some(function (other) { return same(other, wanted); });
  }

  function repeats() {
    say(nameWhy, taken(name.value) ? 'You already have a TV with this name. You can still use it.' : '');
  }

  function firstFree() {
    var pick = 'Living room';
    for (var n = 2; taken(pick); n++) pick = 'Living room ' + n;
    return pick;
  }

  function paired(got) {
    tv = got;
    others = [];
    touched = false;
    form.hidden = true;
    naming.hidden = false;
    // A TV paired again has its name.
    name.value = got.name === 'TV' ? 'Living room' : got.name;
    say(nameWhy, '');
    mark();
    document.getElementById('pair-ok').focus({ preventScroll: true });
    // The list only helps: whatever it does, the form stays as shown.
    MM.api.get('/api/tvs').then(function (data) {
      if (tv !== got || naming.hidden || !data || !Array.isArray(data.tvs)) return;
      others = data.tvs.filter(function (t) { return t && t.id !== got.id && typeof t.name === 'string'; })
        .map(function (t) { return t.name; });
      if (got.name === 'TV' && !touched) {
        name.value = firstFree();
        mark();
      }
      if (touched && !busy && nameWhy.className.indexOf('bad') < 0) repeats();
    }).catch(function () {});
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (code.value.length === 6) pair();
  });
  code.addEventListener('input', function () {
    if (busy) return;
    code.value = code.value.replace(/\D/g, '').slice(0, 6);
    boxes.classList.remove('is-bad');
    say(why, hint);
    paint();
    if (code.value.length === 6) pair();
  });

  pills.forEach(function (pill) {
    pill.addEventListener('click', function () {
      name.value = pill.textContent.trim();
      touched = true;
      repeats();
      mark();
    });
  });
  name.addEventListener('input', function () {
    touched = true;
    repeats();
    mark();
  });

  naming.addEventListener('submit', function (e) {
    e.preventDefault();
    if (busy) return;
    var wanted = name.value.trim();
    if (!wanted) {
      say(nameWhy, 'Give it a name.', true, true);
      name.focus();
      return;
    }
    busy = true;
    touched = true;
    save.setAttribute('aria-disabled', 'true');
    MM.api.patch('/api/tvs/' + tv.id, { name: wanted }).then(function (data) {
      naming.hidden = true;
      done.hidden = false;
      document.getElementById('pair-ready-name').textContent = data.tv.name + ' is ready';
      document.getElementById('pair-ready').focus({ preventScroll: true });
    }, function (err) {
      say(nameWhy, MM.api.messageOf(err, LATER), true, true);
    }).then(function () {
      busy = false;
      save.removeAttribute('aria-disabled');
    });
  });

  function fromAddress() {
    var found = /^#(\d{6})$/.exec(location.hash);
    // The code stays in no history.
    if (found) history.replaceState(null, '', location.pathname);
    return found ? found[1] : '';
  }

  // The same QR page, scanned again.
  window.addEventListener('hashchange', function () {
    var asked = fromAddress();
    if (!asked || busy) return;
    naming.hidden = done.hidden = true;
    form.hidden = false;
    show(asked);
  });

  show(fromAddress());
})();
