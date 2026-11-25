/**
 * password-form.js — a form that takes a new password (the parts are _password.html's, drawn by
 * _password.css): the eye in each field, the live rules, Caps Lock, the reason under the field it
 * is about, the form while it is sent, and the mark drawn at the end.
 *
 * A form with data-password-form is wired as it stands and posted as it is. What posts a second time
 * the new password, in a data-again field that only a page without scripts shows, gets it as it is sent.
 *
 * Exposes:
 *   MM.password.read(password, username)
 *     What the rules of the server's password_policy.py say of a password. A rule is only met here
 *     when it is met there; the list of common passwords is the server's alone.
 *   MM.password.form(form, options)
 *     Wires a form. Options: `whose` ("your" or "their"), `username()` (the form's username field by
 *     default), `before()` (false stops the submit) and `send(values)`; without `send` the form is
 *     posted as it is.
 *   MM.password.celebrate(stage, form, title, words, beside)
 *     Takes the form's place with a tick; resolves, once drawn, to the function that gives it back.
 *
 * Requires, once a form is wired: core/dom.js (MM.dom).
 */
(function () {
  'use strict';

  window.MM = window.MM || {};

  // Where this is loaded ahead of the page, what only works with it is there from the first paint.
  document.documentElement.classList.add('pwd-on');

  // The second field is only left out of sight by what fills it, and no password is kept in it.
  if ('FormDataEvent' in window) {
    document.documentElement.classList.add('pwd-once');
    document.addEventListener('formdata', function (e) {
      var again = e.target.querySelector('[data-again]');
      if (!again) return;
      e.formData.set(again.name, e.target.querySelector('#' + again.getAttribute('data-again')).value);
    });
  }

  var MIN = 12, MIN_DIFFERENT = 6, PHRASE = 20, PHRASE_DIFFERENT = 12, MAX_BYTES = 1024;
  // A plain post whose answer never comes leaves the page here.
  var BUSY_MS = 10000;
  var RUNS = ['abcdefghijklmnopqrstuvwxyz', '0123456789', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm'];
  RUNS = RUNS.concat(RUNS.map(function (run) { return run.split('').reverse().join(''); }));

  // Python's isspace().
  function isSpace(c) { return c !== '﻿' && /[\s\x1c-\x1f\x85]/.test(c); }

  // Python's strip().
  function strip(text) {
    var chars = Array.from(text);
    while (chars.length && isSpace(chars[0])) chars.shift();
    while (chars.length && isSpace(chars[chars.length - 1])) chars.pop();
    return chars.join('');
  }

  // The kind the server counts a character as; null for a rare one that a browser and a server of
  // different ages, or with another idea of a digit, may read as different kinds.
  function kindOf(c) {
    if (isSpace(c)) return 'space';
    var lower = /^\p{Lowercase}$/u.test(c), upper = /^\p{Uppercase}$/u.test(c), digit = /^\p{Nd}$/u.test(c);
    var at = c.codePointAt(0);
    var settled = at < 0x800 || (at >= 0x1e00 && at < 0x2000);
    // U+0295 has been a small letter and a letter of no case.
    if (at === 0x295 || /^[\p{No}\p{Nl}\p{Cn}]$/u.test(c) || (!settled && (lower || upper || digit))) return null;
    return lower ? 'lower' : upper ? 'upper' : digit ? 'digit' : 'symbol';
  }

  // What only one side may know to stand for another character: nothing is said of what it hides.
  function isUnknown(c) {
    if (/^\p{Cn}$/u.test(c)) return true;
    if (c.normalize('NFKC') === c) return false;
    var at = c.codePointAt(0);
    var old = (at >= 0x1d400 && at < 0x1d800) || (at >= 0x1f100 && at < 0x1f300) || (at >= 0x2f800 && at < 0x2fa20);
    return (at >= 0xa700 && at < 0xa800) || (at >= 0x10000 && !old);
  }

  function inOrder(low) {
    var chars = Array.from(low), hits = 0;
    if (chars.length < 6) return false;
    for (var i = 0; i < chars.length - 1; i++) {
      var pair = chars[i] + chars[i + 1];
      if (RUNS.some(function (run) { return run.indexOf(pair) !== -1; })) hits++;
    }
    return hits / (chars.length - 1) >= 0.6;
  }

  function read(password, username) {
    var pw = password.normalize('NFKC');
    var chars = Array.from(pw), low = pw.toLowerCase();
    var user = strip((username || '').toLowerCase());
    var different = new Set(chars).size, length = chars.length;
    var raw = Array.from(password);
    var unknown = raw.some(isUnknown);
    var kinds = {}, unsure = unknown;
    Array.from(raw.filter(function (c) { return !isUnknown(c); }).join('').normalize('NFKC')).forEach(function (c) {
      var kind = kindOf(c);
      if (!kind) unsure = true;
      else kinds[kind] = true;
    });
    var count = ['lower', 'upper', 'digit', 'symbol'].filter(function (kind) { return kinds[kind]; }).length;
    var phrase = !unknown && length >= PHRASE && different >= PHRASE_DIFFERENT && (chars.some(isSpace) || different / length >= 0.6);
    var holdsUser = Boolean(user) && low.indexOf(user) !== -1;
    var insideUser = Boolean(user) && length > 0 && user.indexOf(low) !== -1;
    var bytes = new TextEncoder().encode(pw).length;
    var ordered = inOrder(low);
    return {
      empty: !password, length: length, different: different, kinds: kinds, count: count, phrase: phrase,
      holdsUser: holdsUser, insideUser: insideUser, inOrder: ordered, bytes: bytes,
      tooLong: bytes > MAX_BYTES,
      // Where the server may find kept what is not shown as kept here.
      unsure: { mix: unsure, guess: unknown },
      ok: {
        length: length >= MIN && bytes <= MAX_BYTES,
        mix: phrase || count >= 3,
        guess: !unknown && different >= MIN_DIFFERENT && !holdsUser && !insideUser && !ordered,
      },
    };
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function many(n, one, more) { return n === 1 ? one : n + ' ' + more; }

  // The server's words are not all whole sentences.
  function sentence(text) { return /[.!?…]$/.test(text) ? text : text + '.'; }

  function still() { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; }

  // The line each of the server's rule names is said on. "guess" is several rules of the server's.
  var LINE_OF = { length: 'length', mix: 'mix', repeats: 'guess', username: 'guess', sequence: 'guess', same: 'same', common: 'common' };
  var KINDS = { lower: 'small letter', upper: 'capital', digit: 'number', symbol: 'symbol' };

  function form(node, options) {
    var whose = options.whose || 'your';
    var submit = node.querySelector('[type="submit"]');
    var cancel = node.querySelector('[data-cancel]');
    var said = node.querySelector('.pwd-said');
    var aside = node.querySelector('.pwd-aside');
    var msg = aside.querySelector('.pwd-msg');
    var hint = node.querySelector('input[autocomplete="username"]');
    var username = options.username || function () { return hint ? hint.value : ''; };

    function field(box) {
      if (!box) return null;
      var f = { input: box.querySelector('input'), eye: box.querySelector('.pwd-eye'), why: box.querySelector('.pwd-why') };
      f.say = f.why.firstElementChild;
      // What the server said of the last try, on the page as it came.
      f.wrong = f.say.textContent;
      f.list = null;
      f.shown = function (on, quiet) {
        f.input.type = on ? 'text' : 'password';
        if (quiet) return;
        f.eye.setAttribute('aria-pressed', String(on));
        f.eye.title = on ? 'Hide' : 'Show';
      };
      if (!f.eye) return f;
      // The cursor stays in the field while its eye is pressed.
      f.eye.addEventListener('mousedown', function (e) { e.preventDefault(); });
      // Chromium puts the cursor at the start just after the type changes, so it goes back a frame
      // later, or sooner where a key comes first. A second press before then keeps the first place.
      var back = null;
      function putBack() {
        var at = back;
        back = null;
        if (!at || document.activeElement !== f.input || f.input.value !== at.value) return;
        try { f.input.setSelectionRange(at.from, at.to, at.way); } catch (err) { /* A field that has none. */ }
      }
      f.eye.addEventListener('click', function () {
        if (!back) back = { from: f.input.selectionStart, to: f.input.selectionEnd, way: f.input.selectionDirection, value: f.input.value };
        f.shown(f.eye.getAttribute('aria-pressed') !== 'true');
        if (document.activeElement !== f.input) { back = null; return; }
        requestAnimationFrame(putBack);
      });
      f.input.addEventListener('keydown', putBack);
      f.input.addEventListener('beforeinput', putBack);
      return f;
    }
    var user = field(node.querySelector('.pwd-fld--user'));
    var old = field(node.querySelector('.pwd-fld--old'));
    var fresh = field(node.querySelector('.pwd-fld--new'));
    var fields = [user, old, fresh].filter(Boolean);
    var secret = [old, fresh].filter(Boolean);

    var lines = {};
    node.querySelectorAll('.pwd-rule').forEach(function (line) { lines[line.getAttribute('data-rule')] = line; });

    // What the server refused, until the password, or whose it is, is another one.
    var refused = { pw: null, user: '', lines: {}, guess: '' };
    function refuseLines(pw, names) {
      refused = { pw: pw, user: username(), lines: {}, guess: '' };
      names.forEach(function (name) {
        var line = LINE_OF[name];
        if (!line || !lines[line]) return;
        refused.lines[line] = true;
        if (line === 'guess' && !refused.guess) refused.guess = GUESS[name]();
      });
    }

    var GUESS = {
      username: function () { return 'It has ' + whose + ' username in it'; },
      sequence: function () { return 'Mostly in order, like 12345'; },
      repeats: function () { return 'The same few characters repeat'; },
    };
    function guessWords(r) {
      if (r.holdsUser) return GUESS.username();
      if (r.inOrder) return GUESS.sequence();
      if (r.different < MIN_DIFFERENT && r.length >= MIN) return GUESS.repeats();
      if (r.insideUser && r.length > 3) return 'It is part of ' + whose + ' username';
      return '';
    }

    function look() {
      var pw = fresh.input.value;
      var r = read(pw, username());
      var same = Boolean(lines.same && pw && old.input.value.normalize('NFKC') === pw.normalize('NFKC'));
      return {
        pw: pw, r: r, same: same,
        okSame: !lines.same || Boolean(pw && old.input.value && !same),
        bad: pw === refused.pw && username() === refused.user ? refused : { lines: {}, guess: '' },
      };
    }
    function missing(x) {
      var r = x.r, miss = [];
      if (r.tooLong) miss.push('fewer characters');
      else if (!r.ok.length) miss.push(many(MIN - r.length, 'one more character', 'more characters'));
      // What only the server can judge is not asked for.
      if (!r.ok.mix && !r.unsure.mix) miss.push(r.length >= PHRASE ? 'a third kind of character' : many(3 - r.count, 'one more kind of character', 'more kinds of character'));
      if (!r.ok.guess && !r.unsure.guess) miss.push('something harder to guess');
      if (x.same) miss.push('one you do not have now');
      return miss;
    }

    function mark(key, state, text, more) {
      var line = lines[key];
      if (!line) return;
      line.className = 'pwd-rule' + (state ? ' is-' + state : '');
      line.querySelector('.sr-only').textContent = state === 'met' ? ', met' : state === 'off' || state === 'bad' ? ', not met' : '';
      if (key === 'mix') return;
      var words = line.querySelector('.pwd-rule-w');
      MM.dom.setText(words, text || words.getAttribute('data-rest'));
      MM.dom.setText(line.querySelector('.pwd-rule-n'), more || '');
    }

    var pause = 0;
    function render() {
      var x = look(), r = x.r, bad = x.bad.lines;
      var guess = r.ok.guess ? '' : guessWords(r);
      mark('length', bad.length ? 'bad' : r.ok.length ? 'met' : r.tooLong ? 'off' : '', r.tooLong ? MAX_BYTES + ' bytes at most' : '',
        r.empty || r.tooLong || r.ok.length ? '' : ', ' + r.length + ' so far');
      mark('mix', bad.mix ? 'bad' : r.ok.mix ? 'met' : '');
      lines.mix.querySelectorAll('i').forEach(function (kind) {
        var name = kind.getAttribute('data-kind');
        kind.classList.toggle('is-on', Boolean(name === 'phrase' ? r.phrase : r.kinds[name]));
      });
      lines.mix.querySelector('.pwd-kinds').setAttribute('aria-label', Object.keys(KINDS).map(function (name) {
        return KINDS[name] + (r.empty ? '' : r.kinds[name] ? ' in it' : ' missing');
      }).join(', '));
      mark('guess', bad.guess ? 'bad' : r.ok.guess ? 'met' : guess ? 'off' : '', guess || (bad.guess ? x.bad.guess : ''));
      mark('same', bad.same ? 'bad' : x.same ? 'off' : x.okSame ? 'met' : '', x.same || bad.same ? 'The same as the one you have now' : '');
      mark('common', bad.common ? 'bad' : 'later', bad.common ? 'On the common-password list' : '');
      clearTimeout(pause);
      if (r.empty) { said.textContent = ''; return; }
      pause = setTimeout(function () {
        if (isBusy()) return;
        var needs = missing(x);
        var ready = r.ok.length && r.ok.mix && r.ok.guess && x.okSame;
        said.textContent = ready ? 'Nothing left to add. One last check as you save.' : needs.length ? 'Still needs ' + needs.join(', ') + '.' : '';
      }, 900);
    }

    var idle = null;
    var lapse = 0;
    function isBusy() { return node.classList.contains('is-busy'); }
    function busy(on) {
      if (on === isBusy()) return;
      clearTimeout(lapse);
      node.classList.toggle('is-busy', on);
      node.setAttribute('aria-busy', String(on));
      [submit, cancel].forEach(function (button) {
        if (!button) return;
        if (on) button.setAttribute('aria-disabled', 'true');
        else button.removeAttribute('aria-disabled');
      });
      fields.forEach(function (f) { f.input.readOnly = on; });
      if (on) {
        idle = submit.textContent;
        var box = submit.getBoundingClientRect();
        submit.style.flex = 'none';
        submit.style.width = box.width + 'px';
        submit.style.minHeight = box.height + 'px';
        submit.textContent = '';
        submit.appendChild(el('span', 'btn-spinner')).setAttribute('aria-hidden', 'true');
        submit.appendChild(el('span', '', 'Saving…'));
      } else {
        submit.textContent = idle;
        submit.style.flex = submit.style.width = submit.style.minHeight = '';
      }
      said.textContent = on ? 'Saving…' : '';
    }

    var capsOn = false;
    // Under a field: what is wrong with it, or that Caps Lock is on while it is in use.
    function under(f) {
      var caps = capsOn && f !== user && document.activeElement === f.input;
      f.why.className = 'pwd-why' + (f.wrong ? ' is-wrong' : caps ? ' is-caps' : '');
      MM.dom.setText(f.say, f.wrong || (caps ? 'Caps Lock is on' : ''));
      if (!f.wrong || !f.list || !f.list.length) return;
      var list = f.say.appendChild(el('ul'));
      list.setAttribute('role', 'list');
      f.list.forEach(function (item) { list.appendChild(el('li', '', String(item))); });
    }
    function clearOne(f) {
      f.wrong = '';
      f.list = null;
      f.input.removeAttribute('aria-invalid');
      under(f);
    }
    function clearWhole() {
      msg.textContent = '';
      aside.classList.remove('is-wrong');
    }
    function clear() {
      fields.forEach(clearOne);
      clearWhole();
    }
    function refuse(f, text, list, onlySay) {
      clear();
      f.wrong = sentence(text);
      f.list = list;
      under(f);
      f.input.setAttribute('aria-invalid', 'true');
      if (onlySay) return;
      MM.dom.refuse(f.input, f.say);
      f.input.select();
    }
    // What no field can put right takes the place of the line on what saving does.
    function refuseWhole(text, onlySay) {
      clear();
      msg.textContent = sentence(text);
      aside.classList.add('is-wrong');
      if (!onlySay) msg.scrollIntoView({ block: 'nearest' });
    }
    // The rules the server names turn red where they stand; a reason that does not fit its room
    // is left to them, and one with no line of its own is listed.
    function refuseNew(pw, text, issues, names, onlySay) {
      issues = issues || [];
      names = names || [];
      refuseLines(pw, names);
      render();
      var left = issues.filter(function (_issue, i) { return !lines[LINE_OF[names[i]]]; });
      if (issues.length === 1 && (left.length || issues[0].length <= fresh.why.getAttribute('data-room').length)) {
        text = issues[0];
        left = [];
      }
      refuse(fresh, text, left, onlySay);
    }

    node.addEventListener('input', function (e) {
      if (isBusy()) return;
      fields.forEach(function (f) { if (e.target === f.input) clearOne(f); });
      clearWhole();
      render();
    });
    function caps(e) {
      if (!e.getModifierState) return;
      capsOn = e.getModifierState('CapsLock');
      secret.forEach(under);
    }
    node.addEventListener('keydown', caps);
    node.addEventListener('keyup', caps);
    node.addEventListener('focusin', function () { secret.forEach(under); });
    node.addEventListener('focusout', function () { setTimeout(function () { secret.forEach(under); }, 0); });

    function wanting(x) {
      if (old && !old.input.value) { refuse(old, 'Enter your current password.'); return true; }
      var r = x.r, words = '';
      if (!x.pw) words = old ? 'Enter a new password.' : 'Enter a password.';
      else if (r.tooLong) words = 'Too long. ' + MAX_BYTES + ' bytes at most.';
      else if (!r.ok.length) words = 'Too short. Add ' + many(MIN - r.length, 'one more character', 'more characters') + '.';
      // Only the server can say how many kinds it counts.
      else if (!r.ok.mix && !r.unsure.mix) words = 'Add ' + (r.length >= PHRASE ? 'a third kind of character' : many(3 - r.count, 'one more kind of character', 'more kinds of character')) + '.';
      else if (!r.ok.guess && !r.unsure.guess) words = (guessWords(r) || 'Use at least six different characters') + '.';
      else if (x.same) words = 'That is the one you have now.';
      if (words) refuse(fresh, words);
      return Boolean(words);
    }

    node.addEventListener('submit', function (e) {
      var x = look();
      if (isBusy() || (options.before && !options.before()) || wanting(x)) {
        e.preventDefault();
        // Nothing of this form is on its way: no guard on the document may hold its next submit.
        e.stopPropagation();
        return;
      }
      clear();
      // A browser offers to save what it finds in password fields.
      secret.forEach(function (f) { f.shown(false, true); });
      busy(true);
      if (!options.send) {
        lapse = setTimeout(free, BUSY_MS);
        return;
      }
      e.preventDefault();
      options.send({ old: old && old.input.value, pw: x.pw });
    });
    // Sent and refused: the form is the person's again, as they had it.
    function free() {
      busy(false);
      secret.forEach(function (f) { f.shown(f.eye.getAttribute('aria-pressed') === 'true', true); });
    }
    window.addEventListener('pageshow', function (e) { if (e.persisted) free(); });

    // A password that was taken is not left in the page.
    function forget() { secret.forEach(function (f) { f.input.value = ''; }); }

    function reset() {
      node.reset();
      refused = { pw: null, user: '', lines: {}, guess: '' };
      clear();
      busy(false);
      secret.forEach(function (f) { f.shown(false); });
      render();
    }

    refuseLines('', (node.getAttribute('data-refused') || '').split(' ').filter(Boolean));
    if (!node.getAttribute('data-refused')) refused.pw = null;
    render();
    return {
      old: old && old.input, fresh: fresh.input, submit: submit, cancel: cancel, aside: aside,
      isBusy: isBusy, free: free, forget: forget, reset: reset, render: render,
      refuse: function (input, text, onlySay) { refuse(fields.filter(function (f) { return f.input === input; })[0], text, null, onlySay); },
      refuseNew: refuseNew, refuseWhole: refuseWhole,
    };
  }

  function wait(ms) { return new Promise(function (go) { setTimeout(go, ms); }); }

  function draw(tag, attributes, parent) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attributes).forEach(function (name) { node.setAttribute(name, attributes[name]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  function celebrate(stage, formNode, title, words, beside) {
    var there = MM.dom.stillAt(formNode.querySelector('[type="submit"]'), formNode);
    var height = stage.offsetHeight;
    var done = el('div', 'pwd-done');
    var ring = draw('svg', { class: 'pwd-mark', viewBox: '0 0 48 48', 'aria-hidden': 'true', focusable: 'false' });
    draw('circle', { class: 'c', cx: '24', cy: '24', r: '21.500', pathLength: '1' }, ring);
    draw('path', { class: 't', pathLength: '1', d: 'm15 24.800 6.200 6.200 12-13.500' }, ring);
    done.appendChild(ring);
    var text = done.appendChild(el('div', 'pwd-done-w'));
    text.appendChild(el('div', 'pwd-done-t', title));
    text.appendChild(el('div', 'pwd-done-s', words));
    if (beside) done.appendChild(beside);
    stage.style.height = height + 'px';
    stage.classList.add('is-done');
    formNode.inert = true;
    stage.appendChild(done);
    void stage.offsetHeight;
    stage.style.height = done.offsetHeight + 'px';
    var pace = still() ? 0 : 1;
    // The button pressed is at the foot of a form that may be taller than the screen.
    if (there) done.scrollIntoView({ block: 'nearest', behavior: pace ? 'smooth' : 'auto' });
    return wait(140 * pace).then(function () {
      ring.classList.add('is-round');
      return wait(230 * pace);
    }).then(function () {
      ring.classList.add('is-ticked');
      if (beside) beside.querySelectorAll('.pwd-dev-i:not(.is-here)').forEach(function (device, i) {
        setTimeout(function () { device.classList.add('is-out'); }, 90 * i * pace);
      });
      return wait(260 * pace);
    }).then(function () {
      return function () {
        stage.classList.remove('is-done');
        stage.style.height = '';
        done.remove();
        formNode.inert = false;
      };
    });
  }

  MM.password = { read: read, form: form, celebrate: celebrate };

  function wire() {
    document.querySelectorAll('form[data-password-form]').forEach(function (node) {
      node.querySelectorAll('[data-again]').forEach(function (again) { again.disabled = document.documentElement.classList.contains('pwd-once'); });
      form(node, {});
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
  else wire();
})();
