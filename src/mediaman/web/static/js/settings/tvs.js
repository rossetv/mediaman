/**
 * settings/tvs.js — the TVs & Casting section: every paired TV, and the fold of what Chromecast lets in.
 *
 * What a TV says of itself is untrusted: it is only ever set as text.
 *
 * Cross-module dependencies:
 *   MM.api, MM.dom.icon, MM.dom.fold, MM.settings.rows, window.UIFeedback
 *
 * Exposes:
 *   MM.settings.tvs.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var wrap;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(text, kind, press) {
    var b = el('button', 'btn btn--' + kind + ' btn--sm', text);
    b.type = 'button';
    b.addEventListener('click', press);
    return b;
  }

  function setBusy(b, text) {
    b.setAttribute('aria-disabled', text ? 'true' : 'false');
    if (text) { b.dataset.label = b.textContent; b.textContent = text; }
    else if (b.dataset.label) b.textContent = b.dataset.label;
  }

  function isBusy(b) { return b.getAttribute('aria-disabled') === 'true'; }

  function when(iso) {
    var at = new Date(iso);
    if (isNaN(at)) return '';
    var time = at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    var days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(at).setHours(0, 0, 0, 0)) / 864e5);
    if (days <= 0) return 'today, ' + time;
    if (days < 7) return at.toLocaleDateString('en-GB', { weekday: 'long' }) + ', ' + time;
    return at.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });
  }

  function seen(tv) {
    if (tv.on) return 'On now.';
    return tv.last_seen_at ? 'Last seen ' + when(tv.last_seen_at) + '.' : 'Not seen yet.';
  }

  function says(tv) {
    var who = tv.people && tv.people.length ? tv.people : [tv.owner];
    var paired = who.length > 1 ? who.slice(0, -1).join(', ') + ' and ' + who[who.length - 1] : who[0];
    return (who.length > 1 ? 'Paired with ' : 'Paired by ') + paired + '. ' + seen(tv);
  }

  function urlOf(tv) { return '/api/tvs/' + tv.id; }

  // Fades a part out, then calls `gone`, unless `come` had it back first.
  function go(node, gone) {
    if (node.classList.contains('is-leaving')) return;
    node.inert = true;
    node.classList.add('is-leaving');
    function ended(e) { if (e.target === node) finish(); }
    function finish() {
      node.removeEventListener('animationend', ended);
      if (!node.classList.contains('is-leaving') || node._going !== finish) return;
      come(node);
      gone();
    }
    node._going = finish;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { finish(); return; }
    node.addEventListener('animationend', ended);
    // animationend never fires for what is not drawn.
    setTimeout(finish, 400);
  }

  function come(node) {
    node.inert = false;
    node.classList.remove('is-leaving');
    node._going = null;
  }

  function nameField(value, label) {
    var input = el('input', 'inp');
    input.type = 'text';
    input.maxLength = 40;
    input.value = value;
    input.setAttribute('aria-label', label);
    input.autocomplete = 'off';
    return input;
  }

  // Only what a body holds is refused with details.
  function isNameRefused(err) { return !!err.data && Array.isArray(err.data.detail); }

  function nameError(err) {
    return isNameRefused(err)
      ? 'Type a name of 1 to 40 letters or numbers.'
      : MM.api.messageOf(err, 'Couldn’t save the name. Try again.');
  }

  /* ── the list ── */

  function load() {
    return MM.api.get('/api/tvs/all').then(function (got) {
      show(got.tvs);
    }, function () {
      var box = el('div', 'tv-empty');
      box.appendChild(el('b', '', 'Couldn’t load the TVs'));
      box.appendChild(button('Try again', 'ghost', load));
      wrap.replaceChildren(box);
    });
  }

  function show(tvs) {
    if (!tvs.length) { showEmpty(); return; }
    var ul = el('ul', 'tv-list');
    tvs.forEach(function (tv) { ul.appendChild(row(tv)); });
    wrap.replaceChildren(ul);
  }

  function showEmpty() {
    var box = el('div', 'tv-empty');
    box.appendChild(el('b', '', 'No TVs yet'));
    wrap.replaceChildren(box);
  }

  function row(tv) {
    var li = el('li', 'tv-li');
    var line = el('div', 'tv-in');
    var ico = el('span', 'tv-ico');
    ico.appendChild(MM.dom.icon('i-tv'));
    li.appendChild(line);

    var txt = el('div', 'tv-txt');
    var label = el('div', 'tv-lbl', tv.name);
    var sub = el('div', 'tv-sub', says(tv));
    txt.appendChild(label);
    txt.appendChild(sub);
    if (tv.people && tv.people.length > 1) txt.appendChild(people());

    // Each person it is paired with, any of whom can be unpaired alone.
    function people() {
      var ul = el('ul', 'tv-ppl');
      tv.people.forEach(function (who) {
        var chip = el('li');
        chip.appendChild(MM.dom.avatar(who, tv.avatars && tv.avatars[who]));
        chip.appendChild(el('span', '', who));
        var x = el('button', 'tv-ppl__x');
        x.type = 'button';
        x.appendChild(MM.dom.icon('i-xmark'));
        x.setAttribute('aria-label', 'Unpair ' + who + ' from ' + tv.name);
        x.addEventListener('click', function () {
          if (x.disabled) return;
          x.disabled = true;
          MM.api.delete('/api/tvs/' + tv.id + '/people/' + encodeURIComponent(who)).catch(function (err) {
            // Unpaired already is what was asked for.
            if (err.error !== 'not_found') throw err;
          }).then(function () {
            var held = MM.dom.focusStillIn(ul);
            tv.people = tv.people.filter(function (p) { return p !== who; });
            sub.textContent = says(tv);
            // One person left needs no list of people: it folds away, as a row does.
            if (tv.people.length < 2) MM.settings.rows.leave(ul);
            else MM.settings.rows.leave(chip);
            if (held) li.querySelector('.tv-acts .btn--danger').focus({ preventScroll: true });
            window.UIFeedback.success(who + ' is unpaired from ' + tv.name + '.');
          }, function (err) {
            x.disabled = false;
            window.UIFeedback.error(MM.api.messageOf(err, 'Couldn’t unpair ' + who + '. Try again.'));
          });
        });
        chip.appendChild(x);
        ul.appendChild(chip);
      });
      return ul;
    }

    var acts = el('div', 'tv-acts');
    var rename = button('Rename', 'ghost', editName);
    var unpair = button('Unpair', 'danger', ask);
    acts.appendChild(rename);
    acts.appendChild(unpair);
    nameButtons();

    function nameButtons() {
      [rename, unpair].forEach(function (b) {
        b.setAttribute('aria-label', (b.dataset.label || b.textContent) + ' ' + tv.name);
      });
    }

    function rest() { line.replaceChildren(ico, txt, acts); }
    rest();

    function clear(cls) {
      var old = li.querySelector(cls);
      if (old) old.remove();
    }

    function editName() {
      clear('.tv-sure');
      var box = el('div', 'tv-edit');
      var input = nameField(tv.name, 'TV name');
      var save = button('Save', 'primary', commit);
      var status = el('p', 'tv-err');
      status.setAttribute('role', 'alert');

      function done() {
        rest();
        rename.focus();
      }
      function commit() {
        if (isBusy(save)) return;
        var name = input.value.trim();
        if (name === tv.name) { done(); return; }
        setBusy(save, 'Saving…');
        MM.api.patch(urlOf(tv), { name: name }).then(function (got) {
          tv.name = got.tv.name;
          label.textContent = tv.name;
          nameButtons();
          if (box.parentNode) done();
        }, function (err) {
          setBusy(save, '');
          status.textContent = nameError(err);
          input.focus();
        });
      }
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        if (e.key === 'Escape') { e.preventDefault(); done(); }
      });
      box.appendChild(input);
      box.appendChild(button('Cancel', 'ghost', done));
      box.appendChild(save);
      box.appendChild(status);
      line.replaceChildren(ico, box);
      input.focus();
      input.select();
    }

    function ask() {
      var shown = li.querySelector('.tv-sure');
      if (shown && !shown.classList.contains('is-leaving')) return;
      clear('.tv-sure');
      var sure = el('div', 'tv-sure');
      sure.setAttribute('role', 'alertdialog');
      sure.setAttribute('aria-label', 'Unpair ' + tv.name + '?');
      var words = el('p', '', 'Unpair ' + tv.name + '? It stops playing at once and is ' +
        (tv.people && tv.people.length > 1 ? 'unpaired for everyone: ' + tv.people.join(' and ') + '. ' : 'gone from everyone’s list. ') +
        'To use it again, pair it with a new code.');
      var yes = button('Unpair this TV', 'danger', remove);
      var no = button('Cancel', 'ghost', cancel);
      var status = el('p', 'tv-err');
      status.setAttribute('role', 'alert');
      var two = el('div');
      two.appendChild(no);
      two.appendChild(yes);
      sure.appendChild(words);
      sure.appendChild(two);
      sure.appendChild(status);

      function cancel() {
        go(sure, function () { sure.remove(); });
        unpair.focus();
      }
      function remove() {
        if (isBusy(yes)) return;
        setBusy(yes, 'Unpairing…');
        // An admin among those it is paired with unpairs it for all of them, not just themself.
        MM.api.delete(urlOf(tv) + (tv.people && tv.people.length > 1 ? '?everyone=1' : '')).catch(function (err) {
          // Gone already is what was asked for.
          if (err.error !== 'not_found') throw err;
        }).then(function () {
          if (MM.tvRemote) MM.tvRemote.drop(tv.id);
          MM.settings.rows.leave(li, {
            focusSelector: '.tv-acts .btn--danger',
            fallback: wrap,
            done: function () { if (!wrap.querySelector('.tv-li')) showEmpty(); },
          });
          window.UIFeedback.success(tv.name + ' is unpaired.');
        }, function (err) {
          setBusy(yes, '');
          status.textContent = MM.api.messageOf(err, 'Couldn’t unpair it. Try again.');
        });
      }
      sure.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { e.preventDefault(); cancel(); }
      });
      li.appendChild(sure);
      no.focus();
    }

    return li;
  }

  function init() {
    wrap = document.getElementById('tv-list-wrap');
    if (!wrap) return;
    var more = document.getElementById('cast-more');
    if (more) MM.dom.fold(more, document.getElementById('cast-more-toggle'));
    load();
  }

  MM.settings.tvs = { init: init };
})();
