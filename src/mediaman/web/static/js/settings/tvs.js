/**
 * settings/tvs.js — the TVs & Casting section: every TV, and older ones added by address.
 *
 * What a TV says of itself is untrusted: it is only ever set as text.
 *
 * Cross-module dependencies:
 *   MM.api, MM.dom.icon, MM.settings.rows, window.UIFeedback
 *
 * Exposes:
 *   MM.settings.tvs.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var OLDER = '/api/settings/older-tvs';
  var HOME = /^(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}$/;
  var NETWORKS = 'mediaman must run in a container with a network of its own, as Docker gives one. ' +
    'Host networking, macvlan on the home network and an install without a container can’t reach TVs.';
  var ICONS = {
    ok: function () { return MM.dom.icon('i-circle-check'); },
    no: function () { return MM.dom.icon('i-circle-exclamation'); },
    run: function () { return el('span', 'tv-ring'); },
    todo: function () { return MM.dom.icon('i-circle-check-regular'); },
  };

  var wrap, panel, addBtn;
  var paintAddress = function () {};
  var refusedHttps = null; // the https address a TV could not fetch from, and the TV's name

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
    var time = at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    var days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(at).setHours(0, 0, 0, 0)) / 864e5);
    if (days <= 0) return 'today, ' + time;
    if (days < 7) return at.toLocaleDateString(undefined, { weekday: 'long' }) + ', ' + time;
    return at.toLocaleDateString(undefined, { day: 'numeric', month: 'long' });
  }

  function seen(tv) {
    if (tv.on) return 'On now.';
    return tv.last_seen_at ? 'Last seen ' + when(tv.last_seen_at) + '.' : 'Not seen yet.';
  }

  function says(tv) {
    return (tv.kind === 'older' ? 'Older TV at ' + tv.address : 'Paired by ' + tv.owner) + '. ' + seen(tv);
  }

  function urlOf(tv) { return (tv.kind === 'older' ? OLDER : '/api/tvs') + '/' + tv.id; }

  // A check's outcome as lines: [state, words, more].
  function lines(list) {
    var ul = el('ul', 'tv-chk');
    ul.setAttribute('role', 'status');
    list.forEach(function (line) {
      var li = el('li', line[0]);
      var mark = el('span');
      mark.appendChild(ICONS[line[0]]());
      var words = el('span', '', line[1]);
      if (line[2]) words.appendChild(el('small', '', line[2]));
      li.appendChild(mark);
      li.appendChild(words);
      ul.appendChild(li);
    });
    return ul;
  }

  function atHome(address) { return ['ok', address + ' is on your home network']; }

  function asking(address) {
    return [atHome(address), ['run', 'Asking the TV who it is…'], ['todo', 'Can play films from mediaman']];
  }

  function answered(address, name) {
    return [atHome(address), ['ok', 'The TV answered', 'It calls itself “' + name + '”.'],
      ['ok', 'Can play films from mediaman']];
  }

  function pictured(err) {
    switch (err.error) {
      case 'no_fetch':
        return ['no', 'It couldn’t reach mediaman',
          'Check the address TVs use, below. It must start with http:// and work from your home network.'];
      case 'no_picture':
        return ['no', 'It didn’t show a test picture', err.message + ' You can still add it and try a film.'];
      case 'no_tv_address':
        return ['no', 'It couldn’t reach mediaman', 'Set the address TVs use, below, and save. Then check again.'];
      case 'http_off':
        return ['no', 'It couldn’t reach mediaman',
          'The address TVs use starts with http. Turn on Plain http for older TVs, below, and save. Then check again.'];
      default:
        return ['no', MM.api.messageOf(err, 'Couldn’t show a test picture. Try again.')];
    }
  }

  function wrongAddress() {
    return [['no', 'That isn’t a home network address',
      'Use one such as 192.168.1.44, 10.0.0.7 or 172.16.0.20. Names and internet addresses aren’t allowed.']];
  }

  function failed(err, address) {
    switch (err.error) {
      case 'bad_address':
        return wrongAddress();
      case 'unreachable':
        return [atHome(address), ['no', 'Nothing answered',
          'Is the TV on, on the same home network, and is this its address? The TV shows it under Network. ' +
          'Some TVs answer only after you allow mediaman on their screen.']];
      case 'not_a_tv':
        return [atHome(address), ['no', 'That isn’t a TV mediaman can play on',
          'Something answered, but it can’t play films sent to it. Try pairing with a code instead.']];
      case 'own_network':
        return [['no', 'mediaman can’t reach a TV at this address', NETWORKS]];
      case 'refused':
        return [atHome(address), ['no', 'mediaman can’t use this TV', err.message]];
      default:
        return [['no', MM.api.messageOf(err, 'Couldn’t check the TV. Try again.')]];
    }
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

  // Only what a body holds is refused with details, and the address was checked before.
  function isNameRefused(err) { return !!err.data && Array.isArray(err.data.detail); }

  function nameError(err) {
    return isNameRefused(err)
      ? 'Type a name of 1 to 40 letters or numbers.'
      : MM.api.messageOf(err, 'Couldn’t save the name. Try again.');
  }

  /* ── the list ── */

  function load() {
    return Promise.all([MM.api.get('/api/tvs/all'), MM.api.get(OLDER)]).then(function (got) {
      var addresses = {};
      got[1].tvs.forEach(function (tv) { addresses[tv.id] = tv.address; });
      show(got[0].tvs.filter(function (tv) { return tv.kind !== 'older' || addresses[tv.id]; })
        .map(function (tv) { return Object.assign({}, tv, { address: addresses[tv.id] }); }));
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
    box.appendChild(document.createTextNode('Pair one from Play on, in the player. Or add an older TV here.'));
    wrap.replaceChildren(box);
  }

  function append(tv) {
    var ul = wrap.querySelector('.tv-list');
    if (!ul) { ul = el('ul', 'tv-list'); wrap.replaceChildren(ul); }
    var li = row(tv);
    ul.appendChild(li);
    MM.settings.rows.enter(li);
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

    var acts = el('div', 'tv-acts');
    var check = null;
    if (tv.kind === 'older') {
      check = button('Check', 'ghost', function () { runCheck(); });
      acts.appendChild(check);
    }
    var rename = button('Rename', 'ghost', editName);
    var forget = button('Forget', 'danger', ask);
    acts.appendChild(rename);
    acts.appendChild(forget);
    nameButtons();

    function nameButtons() {
      [check, rename, forget].forEach(function (b) {
        if (b) b.setAttribute('aria-label', (b.dataset.label || b.textContent) + ' ' + tv.name);
      });
    }

    function rest() { line.replaceChildren(ico, txt, acts); }
    rest();

    function clear(cls) {
      var old = li.querySelector(cls);
      if (old) old.remove();
    }

    function runCheck() {
      if (isBusy(check)) return;
      setBusy(check, 'Checking…');
      var out = lines(asking(tv.address));
      clear('.tv-chk');
      li.appendChild(out);
      function say(list) {
        setBusy(check, '');
        if (!out.parentNode) return;
        var next = lines(list);
        out.replaceChildren.apply(out, Array.prototype.slice.call(next.children));
      }
      MM.api.post(OLDER + '/' + tv.id + '/check', {}).then(function (got) {
        tv.last_seen_at = got.tv.last_seen_at;
        sub.textContent = says(tv);
        say([['ok', 'The TV answered', 'mediaman can reach it at ' + tv.address + '.']]);
      }, function (err) { say(failed(err, tv.address)); });
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
      box.appendChild(save);
      box.appendChild(button('Cancel', 'ghost', done));
      box.appendChild(status);
      line.replaceChildren(ico, box);
      input.focus();
      input.select();
    }

    function ask() {
      if (li.querySelector('.tv-sure')) return;
      var sure = el('div', 'tv-sure');
      sure.setAttribute('role', 'alertdialog');
      sure.setAttribute('aria-label', 'Forget ' + tv.name + '?');
      var words = el('p', '', 'Forget ' + tv.name + '? It stops playing at once and is gone from everyone’s list. ' +
        (tv.kind === 'older' ? 'You can add it again by its address.' : 'To use it again, pair it with a new code.'));
      var yes = button('Forget this TV', 'danger', remove);
      var no = button('Cancel', 'ghost', cancel);
      var status = el('p', 'tv-err');
      status.setAttribute('role', 'alert');
      var two = el('div');
      two.appendChild(yes);
      two.appendChild(no);
      sure.appendChild(words);
      sure.appendChild(two);
      sure.appendChild(status);

      function cancel() { sure.remove(); forget.focus(); }
      function remove() {
        if (isBusy(yes)) return;
        setBusy(yes, 'Forgetting…');
        MM.api.delete(urlOf(tv)).catch(function (err) {
          // Gone already is what was asked for.
          if (err.error !== 'not_found') throw err;
        }).then(function () {
          MM.settings.rows.leave(li, {
            focusSelector: '.tv-acts .btn--danger',
            fallback: addBtn,
            done: function () { if (!wrap.querySelector('.tv-li')) showEmpty(); },
          });
          window.UIFeedback.success(tv.name + ' is forgotten.');
        }, function (err) {
          setBusy(yes, '');
          status.textContent = MM.api.messageOf(err, 'Couldn’t forget it. Try again.');
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

  /* ── add an older TV ── */

  function openAdd() {
    var asked = 0;
    var found = null;

    var address = el('input', 'inp');
    address.id = 'tv-add-address';
    address.type = 'text';
    address.inputMode = 'decimal';
    address.autocomplete = 'off';
    address.placeholder = '192.168.1.44';
    var label = el('label', '', 'The TV’s address');
    label.htmlFor = address.id;
    var check = el('button', 'btn btn--secondary', 'Check');
    check.type = 'button';
    var l = el('div', 'tv-add-line');
    l.appendChild(address);
    l.appendChild(check);

    var out = el('div');
    out.setAttribute('role', 'status');
    var hint = el('p', 'tv-add-hint', 'For TVs from about 2011 to 2016 with no AirPlay or Chromecast, and no browser ' +
      'that works. Samsung, Sony and Panasonic are the likeliest to work; other makes may not. Find the address in the ' +
      'TV’s network settings. Give the TV a fixed address in your router, or it may change.');
    var named = el('div');
    var end = el('div', 'tv-add-end');
    var add = button('Add this TV', 'primary', commit);
    add.disabled = true;
    end.appendChild(add);
    end.appendChild(button('Cancel', 'ghost', close));
    var status = el('p', 'tv-err');
    status.setAttribute('role', 'alert');

    function say(list) {
      var ul = lines(list);
      ul.removeAttribute('role');
      out.replaceChildren(ul);
    }
    function forgetFound() {
      asked += 1;
      found = null;
      add.disabled = true;
      setBusy(check, '');
      named.replaceChildren();
      status.textContent = '';
    }
    function look() {
      if (isBusy(check)) return;
      var typed = address.value.trim();
      forgetFound();
      if (!HOME.test(typed)) { say(wrongAddress()); address.focus(); return; }
      var mine = asked;
      setBusy(check, 'Checking…');
      say(asking(typed).concat([['todo', 'Can reach the address TVs use']]));
      MM.api.post(OLDER + '/check', { address: typed }).then(function (got) {
        if (mine !== asked) return;
        var told = answered(typed, got.name);
        say(told.concat([['run', 'Asking it to fetch a test picture…']]));
        return MM.api.post(OLDER + '/picture', { address: got.address }).then(function (shown) {
          if (mine !== asked) return;
          say(told.concat([['ok', 'Fetched a test picture from ' + shown.from]]));
          offer(got);
        }, function (err) {
          if (mine !== asked) return;
          setBusy(check, '');
          say(told.concat([pictured(err)]));
          var used = document.getElementById('tv_address').defaultValue;
          if (err.error === 'no_fetch' && /^https:/i.test(used)) {
            refusedHttps = { address: used, name: got.name };
            paintAddress();
          }
          if (err.error === 'no_picture') offer(got);
        });
      }, function (err) {
        if (mine !== asked) return;
        setBusy(check, '');
        say(failed(err, typed));
      });
    }
    function offer(got) {
      setBusy(check, '');
      var input = nameField(got.name.slice(0, 40).trim(), 'Name');
      input.removeAttribute('aria-label');
      input.id = 'tv-add-name';
      var nameLabel = el('label', '', 'Name');
      nameLabel.htmlFor = input.id;
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
      });
      named.replaceChildren(nameLabel, input);
      found = { address: got.address, input: input };
      add.disabled = false;
      if (MM.dom.focusStillIn(panel)) MM.settings.rows.bringIntoView(end);
    }
    function commit() {
      if (!found || isBusy(add)) return;
      var mine = asked;
      status.textContent = '';
      setBusy(add, 'Adding…');
      MM.api.post(OLDER, { address: found.address, name: found.input.value.trim() }).then(function (got) {
        append(Object.assign({ kind: 'older' }, got.tv));
        window.UIFeedback.success(got.tv.name + ' is added.');
        if (mine === asked && add.isConnected) close();
      }, function (err) {
        setBusy(add, '');
        if (mine !== asked) return;
        if (isNameRefused(err)) {
          status.textContent = nameError(err);
          found.input.focus();
          return;
        }
        var where = found.address;
        forgetFound();
        say(failed(err, where));
      });
    }
    function close() {
      asked += 1;
      panel.hidden = true;
      panel.replaceChildren();
      addBtn.hidden = false;
      addBtn.setAttribute('aria-expanded', 'false');
      addBtn.focus();
    }

    check.addEventListener('click', look);
    address.addEventListener('input', function () {
      forgetFound();
      out.replaceChildren(hint);
    });
    address.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); look(); }
    });
    panel.onkeydown = function (e) { if (e.key === 'Escape') { e.preventDefault(); close(); } };

    out.appendChild(hint);
    panel.replaceChildren(label, l, out, named, end, status);
    addBtn.hidden = true;
    addBtn.setAttribute('aria-expanded', 'true');
    panel.hidden = false;
    MM.settings.rows.bringIntoView(panel);
    address.focus({ preventScroll: true });
  }

  /* ── the address TVs use ── */

  function wireAddress() {
    var input = document.getElementById('tv_address');
    var hint = document.getElementById('tv-address-hint');
    if (!input || !hint) return;
    var usual = hint.textContent;
    function paint() {
      var value = input.value.trim();
      var bad = '';
      var warn = '';
      var host = (/^https?:\/\/(\[[^\]]*\]|[^/:?#]*)/i.exec(value) || [])[1] || '';
      if (/^(localhost|.*\.localhost|127\..*|\[::1\])\.?$/i.test(host)) {
        bad = 'A TV can’t use localhost: to a TV, that means itself. Use this server’s home address.';
      } else if (/^https:/i.test(value)) {
        warn = refusedHttps && refusedHttps.address === value
          ? 'This starts with https. Older TVs often refuse it, and ' + refusedHttps.name + ' did.'
          : 'This starts with https. Older TVs often refuse it.';
      } else if (/^http:/i.test(value) && document.getElementById('older_tvs_http').value !== 'true') {
        warn = 'This starts with http, which older TVs may use only with Plain http for older TVs on.';
      }
      hint.textContent = bad || warn || usual;
      hint.classList.toggle('is-bad', !!bad);
      if (bad) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }
    paintAddress = paint;
    input.addEventListener('input', paint);
    // The switches and Discard set values without an event of their own.
    document.addEventListener('click', function () { setTimeout(paint, 0); });
    document.addEventListener('keyup', paint);
    paint();
  }

  function init() {
    wrap = document.getElementById('tv-list-wrap');
    panel = document.getElementById('tv-add');
    addBtn = document.getElementById('btn-add-tv');
    if (!wrap || !panel || !addBtn) return;
    addBtn.addEventListener('click', openAdd);
    wireAddress();
    load();
  }

  MM.settings.tvs = { init: init };
})();
