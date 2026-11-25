/* The Play on sheet: this device, the person's TVs, and the browser's own ways to a TV.
 *
 *   MM.playOn.register(way)      a way of the browser's: AirPlay (airplay.js), Google Cast
 *   MM.playOn.changed()          what a way says of itself may be different now
 *   MM.playOn.again()            a play that one of the ways finds already on, or null
 *   MM.playOn.ready(fn)          fn(whether there is anything to play on), now and as it changes
 *   MM.playOn.open(button, host) the sheet, by the button
 *   MM.playOn.button(href, className) the button of a title on a page without a player, by the
 *                                address it plays at; it lists TVs alone
 *
 * A way: {id, label, sub, icon, available(), chooser, start(what), look?, again?}. Its start is
 * called in the press itself, since the browser shows its list of TVs for a press alone.
 *
 * A host is the page's own player, or a page that has none:
 *   what()        {rating_key, position_s, quality, subtitle, autoplay, asked_s}
 *   on()          optional: null, or the id of what plays it now ('tv-5', a way's)
 *   before(tv)    optional: what plays is stopped and its place saved; a promise of the place
 *   failed()      optional: nothing came of it; it plays on as before
 *   started(tv)   optional: a TV plays it
 *   startWay(way) optional: plays it by a way of the browser's; a promise
 *   here()        optional: bring it back to this device
 *
 * The TVs are those of GET /api/tvs. Where mediaman has no such route there are none.
 *
 * Requires: core/api.js, core/dom.js. Uses tv-remote.js where the page has it.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  var TVS = '/api/tvs';
  var TV_ICON = 'play-on-tv'; // of _play_on.html
  var LOOK_MS = 5000;
  var START_WAIT_MS = 125000;
  var LEAVE_MS = 300;
  var NAME_MAX = 30;          // a longer one does not stand in a sentence
  var DAY_MS = 24 * 60 * 60 * 1000;
  var NAMES = ['Living room', 'Bedroom TV', 'Kitchen TV', 'Playroom'];
  var PAIR_WHY = 'Only pair a TV you can see.';
  var PAIR_ERRORS = {
    wrong_code: 'That code isn’t right, or it has run out. Type the one on the TV now.',
    too_many_tries: 'Too many wrong codes. Wait five minutes, then use a new code.',
    too_many_tvs: 'You have as many TVs as you can. Unpair one in your Profile, then try again.',
  };
  var LIMIT = ['You’re watching on three screens', 'That’s the most at once. Stop one, then try again.'];
  var UNCHANGED = ' Nothing changed here.';
  /* A later press won: what the person sees is already that. */
  var SILENT = ['cancelled', 'superseded', 'replaced'];
  var FAILED = {
    unreachable: 'mediaman couldn’t reach Plex. Wait a moment, then try again.',
    not_found: 'It is no longer in the library, or the TV is no longer yours.',
    not_set_up: 'Plex isn’t set up.',
    track_unavailable: 'Those subtitles can’t be played.',
    rate_limited: 'Too many tries. Wait a minute, then try again.',
    network_error: 'mediaman didn’t answer. Check that this device is online, then try again.',
  };

  var ways = [];
  var listeners = [];
  var tvs = null;             // the TVs as last heard, null before
  var lost = false;           // the last look found no answer
  var absent = false;         // mediaman has no TVs to look for, or not for this person
  var looking = null;

  var sheet = null;           // while open: {node, btn, host, pairing}
  var say = null;             // {id, title, line, bad, again, take}
  var wait = null;            // the id of what is starting
  var limit = false;          // three streams play already
  var asking = 0;             // bumps with every press that starts something

  function now() { return Date.now(); }

  function usable() {
    return ways.filter(function (w) { return w.available(); });
  }

  function hasTvs() { return !absent && (tvs !== null || lost); }
  function any() { return hasTvs() || usable().length > 0; }

  function changed() {
    listeners.forEach(function (fn) { fn(any()); });
    MM.dom.qa('[data-play-on-title]').forEach(function (b) { b.hidden = !hasTvs(); });
    /* What is being typed there is left alone. */
    if (sheet && !sheet.pairing) draw();
  }

  /* ---- the TVs ---------------------------------------------------------- */

  function look() {
    if (looking) return looking;
    var before = JSON.stringify([tvs, lost, absent]);
    looking = MM.api.get(TVS).then(function (data) {
      tvs = data.tvs || [];
      lost = absent = false;
    }, function (err) {
      var status = err && err.status;
      absent = status === 404 || status === 403;
      lost = !absent;
      if (absent) tvs = null;
    }).then(function () {
      looking = null;
      if (JSON.stringify([tvs, lost, absent]) !== before) changed();
    });
    return looking;
  }

  function idOf(tv) { return 'tv-' + tv.id; }
  function isOlder(tv) { return tv.kind === 'older'; }

  function lastSeen(tv) {
    var at = new Date(tv.last_seen_at || '');
    if (isNaN(at)) return 'Not open';
    var time = at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    var today = new Date(now());
    var days = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate()) -
      new Date(at.getFullYear(), at.getMonth(), at.getDate())) / DAY_MS);
    if (days <= 0) return 'Last seen today, ' + time;
    if (days < 7) return 'Last seen ' + at.toLocaleDateString('en-GB', { weekday: 'long' }) + ', ' + time;
    return 'Last seen ' + at.toLocaleDateString('en-GB', { day: 'numeric', month: 'long' });
  }

  function about(tv) {
    if (tv.busy) return 'Playing for someone else';
    if (tv.on && tv.play && tv.play.title && tv.play.state !== 'ended') return 'Playing ' + tv.play.title;
    if (isOlder(tv)) return tv.on ? 'Older TV, added by an admin' : 'Older TV. Not answering.';
    return tv.on ? 'On and ready' : lastSeen(tv);
  }

  function short(tv, otherwise) { return tv.name.length > NAME_MAX ? otherwise || 'This TV' : tv.name; }

  function tvAddress() { return global.location.host + '/tv'; }

  function notOpen(tv) {
    return {
      id: idOf(tv),
      title: short(tv) + ' isn’t open',
      line: isOlder(tv) ? ['It didn’t answer. Turn it on, wait a moment, then try again.']
        : ['Turn it on, open its web browser and go to ', { code: tvAddress() }, '. It will appear here by itself.'],
    };
  }

  function busy(tv) {
    return {
      id: idOf(tv),
      title: 'Someone is watching on this TV',
      line: ['Playing here stops what they’re watching.'],
      take: function () { startTv(tv, true); },
    };
  }

  /* What is said where `tv` did not start. */
  function refused(tv, err) {
    var code = err && err.error;
    if (code === 'in_use') return busy(tv);
    if (isOlder(tv) && (code === 'tv_off' || code === 'unreachable')) return notOpen(tv);
    var line = code === 'tv_off' ? 'It stopped answering. Check it still shows mediaman, then choose it again.'
      : err && err.data && err.data.message || FAILED[code] || 'Choose it again in a moment.';
    return {
      id: idOf(tv),
      title: short(tv, 'It') + ' didn’t start',
      line: [line + UNCHANGED],
      bad: true,
      again: function () { startTv(tv, false); },
    };
  }

  function overLimit() {
    limit = true;
    if (!sheet && global.UIFeedback) global.UIFeedback.error(LIMIT[0] + '. ' + LIMIT[1]);
  }

  /* Under its row, or as a toast where the sheet was closed meanwhile. */
  function tell(s) {
    say = s;
    if (!sheet && global.UIFeedback) {
      global.UIFeedback.error(s.title + '. ' + s.line.map(function (part) { return part.code || part; }).join(''));
    }
  }

  /* `confirm` is the person's yes to taking the TV from somebody. */
  function startTv(tv, confirm) {
    if (wait) return;
    var mine = ++asking;
    var host = sheet.host;
    var done;
    say = null;
    limit = false;
    wait = idOf(tv);
    draw();
    Promise.resolve(host.before ? host.before(tv) : undefined).then(function (place) {
      var what = host.what();
      if (place !== undefined) what.position_s = place;
      if (confirm) what.confirm = true;
      var stopper = new AbortController();
      var late = setTimeout(function () { stopper.abort(); }, START_WAIT_MS);
      done = function () { clearTimeout(late); };
      return MM.api.post(TVS + '/' + encodeURIComponent(tv.id) + '/play', what, { signal: stopper.signal });
    }).then(function (data) {
      if (done) done();
      if (mine !== asking) return;
      wait = null;
      close();
      if (host.started) host.started(data.tv || tv);
      else if (MM.tvRemote) MM.tvRemote.open(data.tv || tv);
    }, function (err) {
      if (done) done();
      if (mine !== asking) return;
      wait = null;
      if (host.failed) host.failed();
      if (err && err.error === 'too_many_streams') overLimit();
      else if (!err || SILENT.indexOf(err.error) === -1) tell(refused(tv, err));
      draw();
      look();
    });
  }

  /* ---- the browser's own ways ------------------------------------------- */

  function startWay(way) {
    if (wait) return;
    var mine = ++asking;
    limit = false;
    say = way.chooser ? { id: way.id, title: way.chooser.title, line: [way.chooser.line] } : null;
    var playing = sheet.host.startWay(way, function () {
      /* The person has chosen a TV in the browser's own list. */
      if (mine !== asking) return;
      say = null;
      wait = way.id;
      draw();
    });
    draw();
    playing.then(function () {
      if (mine !== asking) return;
      say = wait = null;
      close();
    }, function (err) {
      if (mine !== asking) return;
      var code = err && (err.code || err.error);
      wait = null;
      say = null;
      if (code === 'too_many_streams') overLimit();
      else if (SILENT.indexOf(code) === -1) {
        var told = err && err.say;
        tell({
          id: way.id,
          title: told ? told.title : way.label + ' didn’t start',
          line: [told ? told.line : (FAILED[code] || 'Choose it again in a moment.') + UNCHANGED],
          bad: true,
          again: function () { startWay(way); },
        });
      }
      draw();
    });
  }

  /* ---- building the sheet ----------------------------------------------- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function button(className, text, fn) {
    var b = el('button', className, text);
    b.type = 'button';
    if (fn) b.addEventListener('click', fn);
    return b;
  }

  function head(title, back) {
    var top = el('div', 'play-on__head');
    if (back) {
      var b = button('play-on__back', '', back);
      b.setAttribute('aria-label', 'Back');
      b.setAttribute('data-key', 'back');
      b.appendChild(MM.dom.icon('i-chevron-left'));
      top.appendChild(b);
    }
    var t = el('span', '', title);
    t.id = 'play-on-title';
    top.appendChild(t);
    var x = button('', '', close);
    x.setAttribute('aria-label', 'Close');
    x.setAttribute('data-key', 'close');
    x.appendChild(MM.dom.icon('i-xmark'));
    top.appendChild(x);
    return top;
  }

  /* What is said under a row, `line` as texts and {code}. */
  function sayBox(s) {
    var box = el('div', 'play-on__say' + (s.bad ? ' is-bad' : '') + (s.seen ? '' : ' is-new'));
    s.seen = true;
    box.setAttribute('role', s.bad ? 'alert' : 'status');
    box.appendChild(el('b', '', s.title));
    s.line.forEach(function (part) {
      box.appendChild(part.code ? el('code', '', part.code) : document.createTextNode(part));
    });
    if (s.again || s.take) box.appendChild(el('br'));
    if (s.again) addKeyed(box, 'again', 'btn btn--secondary btn--sm', 'Try again', s.again);
    if (s.take) {
      addKeyed(box, 'take', 'btn btn--danger btn--sm', 'Play here anyway', s.take);
      addKeyed(box, 'unsay', 'btn btn--ghost btn--sm', 'Cancel', function () { say = null; draw(); });
    }
    return box;
  }

  function addKeyed(box, key, className, text, fn) {
    var b = button(className, text, fn);
    b.setAttribute('data-key', key);
    box.appendChild(b);
  }

  function ring() { return el('span', 'play-on__ring'); }

  /* o: {id, icon, name, sub, on, off, starting, more} */
  function row(list, o, fn) {
    var li = el('li');
    var b = button('play-on__row' + (o.on || o.starting ? ' is-on' : '') + (o.off ? ' is-off' : ''), '', fn);
    b.setAttribute('data-key', o.id);
    if (o.on) b.setAttribute('aria-current', 'true');
    if (o.starting) b.setAttribute('aria-busy', 'true');
    var icon = el('span', 'play-on__icon');
    icon.appendChild(MM.dom.icon(o.icon));
    var text = el('span', 'play-on__text');
    text.appendChild(el('b', '', o.name));
    /* Keeps the two apart in the row's spoken name. */
    text.appendChild(document.createTextNode(' '));
    if (o.sub) text.appendChild(el('small', '', o.sub));
    var end = el('span', 'play-on__end');
    if (o.starting) end.appendChild(ring());
    else if (o.on) end.appendChild(MM.dom.icon('i-check'));
    else if (o.more) end.appendChild(MM.dom.icon('i-chevron-right'));
    b.appendChild(icon);
    b.appendChild(text);
    b.appendChild(end);
    li.appendChild(b);
    list.appendChild(li);
    if (say && say.id === o.id) {
      var under = el('li');
      under.appendChild(sayBox(say));
      list.appendChild(under);
    }
  }

  function deviceIcon() {
    if (global.innerWidth < 700) return 'i-phone';
    return global.innerWidth < 900 ? 'i-tablet' : 'i-laptop';
  }

  function tvRow(list, tv, on) {
    var id = idOf(tv);
    var here = on === id;
    row(list, {
      id: id, icon: TV_ICON, name: tv.name, on: here, starting: wait === id, off: !tv.on && !tv.busy,
      sub: here ? 'Playing here now' : wait === id ? 'Starting…' : about(tv),
    }, function () {
      if (here) { close(); return; }
      if (wait) return;
      limit = false;
      /* An older TV's answer is known only by asking it. */
      if (!tv.on && !tv.busy && !isOlder(tv)) say = notOpen(tv);
      else if (tv.busy) say = busy(tv);
      else { startTv(tv, false); return; }
      draw();
    });
  }

  function panel(body) {
    var host = sheet.host;
    var on = host.on ? host.on() : null;
    var known = tvs || [];
    var found = host.startWay ? usable() : [];
    var first = !tvs && !lost && !absent;
    body.appendChild(head('Play on'));
    var list = el('ul', 'play-on__list');
    list.setAttribute('role', 'list');
    body.appendChild(list);
    var free = !on && !wait;
    row(list, {
      id: 'here', icon: deviceIcon(), name: 'This device', on: free,
      sub: on ? 'Bring it back here' : '',
    }, function () {
      close();
      if (on && host.here) host.here();
    });
    known.filter(function (tv) { return tv.on || tv.busy; }).forEach(function (tv) { tvRow(list, tv, on); });
    found.forEach(function (way) {
      var here = on === way.id;
      row(list, {
        id: way.id, icon: way.icon, name: way.label, on: here, starting: wait === way.id, more: true,
        sub: here ? 'Playing here now' : wait === way.id ? 'Starting…' : way.sub,
      }, function () {
        if (here) close();
        else startWay(way);
      });
    });
    known.filter(function (tv) { return !tv.on && !tv.busy; }).forEach(function (tv) { tvRow(list, tv, on); });

    if (first) {
      var looks = el('div', 'play-on__wait');
      looks.setAttribute('role', 'status');
      looks.appendChild(ring());
      looks.appendChild(document.createTextNode('Looking for TVs…'));
      body.appendChild(looks);
    } else if (lost && !tvs) {
      body.appendChild(sayBox({
        title: 'Couldn’t look for TVs', bad: true,
        line: ['mediaman didn’t answer. Check that this device is online, then try again.'],
        again: function () { lost = false; draw(); look(); },
      }));
    } else if (!known.length && !absent) {
      body.appendChild(el('p', 'play-on__note', 'No TV found yet. ' + (found.length
        ? 'Turn the TV on and join it to the same Wi-Fi, or pair it with a code.'
        : 'Pair any TV that has a web browser. It takes a minute, once.')));
    }
    if (limit) {
      body.appendChild(sayBox({
        title: LIMIT[0], bad: true, line: [LIMIT[1]],
      }));
    }
    if (absent) return;
    body.appendChild(el('div', 'play-on__rule'));
    var more = el('ul', 'play-on__list');
    more.setAttribute('role', 'list');
    body.appendChild(more);
    row(more, { id: 'pair', icon: 'i-plus', name: 'Pair a TV', sub: 'With the code it shows', more: true }, function () {
      sheet.pairing = { code: '', why: '', bad: false, busy: false, take: false, tv: null };
      draw();
      focusKey('code');
    });
  }

  /* ---- pairing ---------------------------------------------------------- */

  function pairing(body) {
    var p = sheet.pairing;
    body.appendChild(head('Pair a TV', function () {
      sheet.pairing = null;
      draw();
      focusKey('pair');
    }));
    var box = el('div', 'play-on__pair');
    body.appendChild(box);
    if (p.tv) { naming(box, p); return; }

    var steps = el('ol');
    steps.setAttribute('role', 'list');
    ['On the TV, open its web browser.', '', 'Type the code it shows.'].forEach(function (text) {
      var li = el('li', '', text);
      if (!text) {
        li.appendChild(document.createTextNode('Go to '));
        li.appendChild(el('b', '', tvAddress()));
      }
      steps.appendChild(li);
    });
    box.appendChild(steps);
    var code = el('div', 'play-on__code' + (p.bad ? ' is-bad' : ''));
    var boxes = [];
    for (var n = 0; n < 6; n++) {
      var one = el('i');
      one.setAttribute('aria-hidden', 'true');
      boxes.push(one);
      code.appendChild(one);
    }
    var input = el('input');
    input.setAttribute('inputmode', 'numeric');
    input.setAttribute('autocomplete', 'one-time-code');
    input.setAttribute('maxlength', '12');
    input.setAttribute('aria-label', 'The six numbers on the TV');
    input.setAttribute('aria-describedby', 'play-on-why');
    input.setAttribute('data-key', 'code');
    input.value = p.code;
    input.readOnly = p.busy || p.take;
    code.appendChild(input);
    box.appendChild(code);
    var why = el('p', 'play-on__why' + (p.bad ? ' is-bad' : ''), p.busy ? 'Pairing…' : p.why || PAIR_WHY);
    why.id = 'play-on-why';
    why.setAttribute('role', 'status');
    box.appendChild(why);

    function paint() {
      boxes.forEach(function (b, i) {
        b.textContent = input.value.charAt(i);
        b.classList.toggle('at', i === input.value.length && !input.readOnly);
      });
    }
    paint();
    input.addEventListener('input', function () {
      input.value = input.value.replace(/\D/g, '').slice(0, 6);
      p.code = input.value;
      if (p.bad) {
        p.bad = false;
        p.why = '';
        code.classList.remove('is-bad');
        why.className = 'play-on__why';
        why.textContent = PAIR_WHY;
      }
      paint();
      if (p.code.length === 6) pair(p, false);
    });
    if (p.take) {
      addKeyed(box, 'take', 'btn btn--danger', 'Pair it with me', function () { pair(p, true); });
      addKeyed(box, 'unsay', 'btn btn--ghost', 'Cancel', function () {
        p.take = false;
        p.code = p.why = '';
        draw();
        focusKey('code');
      });
    }
  }

  function pair(p, confirm) {
    if (p.busy) return;
    p.busy = true;
    p.take = p.bad = false;
    p.why = '';
    draw();
    MM.api.post(TVS + '/pair', confirm ? { code: p.code, confirm: true } : { code: p.code }).then(function (data) {
      p.tv = data.tv;
      look();
    }, function (err) {
      var kind = err && err.error;
      if (kind === 'in_use') {
        p.take = true;
        p.why = 'Someone else has this TV. Pairing it with you unpairs them' +
          (err.data && err.data.playing ? ', and stops what they’re watching.' : '.');
        return;
      }
      p.bad = true;
      p.why = PAIR_ERRORS[kind] || MM.api.messageOf(err, 'Something went wrong. Try again.');
      /* A code that may still be good is kept, its last number typed again to send it. */
      p.code = PAIR_ERRORS[kind] ? '' : p.code.slice(0, 5);
    }).then(function () {
      p.busy = false;
      if (!sheet || sheet.pairing !== p) return;
      draw();
      focusKey(p.tv ? 'play' : p.take ? 'take' : 'code');
    });
  }

  function naming(box, p) {
    var ok = el('p', 'play-on__ok');
    ok.appendChild(MM.dom.icon('i-circle-check'));
    ok.appendChild(document.createTextNode('Paired'));
    ok.setAttribute('role', 'status');
    box.appendChild(ok);
    box.appendChild(el('p', '', 'What do you call this TV?'));
    if (p.name === undefined) p.name = p.tv.name === 'TV' ? NAMES[0] : p.tv.name;
    var pills = el('div', 'pill-row');
    var input = el('input', 'inp');
    function paint() {
      Array.prototype.forEach.call(pills.children, function (b) {
        var on = b.textContent === p.name.trim();
        b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', String(on));
      });
    }
    NAMES.forEach(function (n) {
      var b = button('fpill', n, function () { input.value = p.name = n; paint(); });
      b.setAttribute('data-key', 'name-' + n);
      pills.appendChild(b);
    });
    box.appendChild(pills);
    input.setAttribute('aria-label', 'TV name');
    input.setAttribute('maxlength', '40');
    input.setAttribute('data-key', 'name');
    input.value = p.name;
    input.addEventListener('input', function () { p.name = input.value; paint(); });
    box.appendChild(input);
    var why = el('p', 'play-on__why' + (p.bad ? ' is-bad' : ''), p.why || '');
    why.setAttribute('role', 'status');
    box.appendChild(why);
    addKeyed(box, 'play', 'btn btn--primary', 'Play on this TV', function () { playOnPaired(p); });
    paint();
  }

  function playOnPaired(p) {
    var name = p.name.trim();
    if (p.busy || wait) return;
    if (!name) { p.bad = true; p.why = 'Give it a name.'; draw(); return; }
    p.busy = true;
    var named = name === p.tv.name ? Promise.resolve()
      : MM.api.patch(TVS + '/' + encodeURIComponent(p.tv.id), { name: name }).catch(function () { /* it plays all the same */ });
    named.then(look).then(function () {
      if (!sheet || sheet.pairing !== p) return;
      var tv = (tvs || []).filter(function (t) { return t.id === p.tv.id; })[0] || { id: p.tv.id, name: name };
      sheet.pairing = null;
      startTv(tv, false);
      focusKey(idOf(tv));
    });
  }

  /* ---- showing it ------------------------------------------------------- */

  function focusKey(key) {
    var to = sheet && sheet.node.querySelector('[data-key="' + key + '"]');
    if (to) to.focus({ preventScroll: true });
    return !!to;
  }

  function draw() {
    if (!sheet) return;
    var node = sheet.node;
    var at = document.activeElement;
    var key = at && node.contains(at) && at.getAttribute('data-key');
    var body = el('div', 'play-on__body');
    if (sheet.pairing) pairing(body);
    else panel(body);
    var top = node.scrollTop;
    node.textContent = '';
    node.appendChild(body);
    node.scrollTop = top;
    /* A row that is gone gives the focus to the sheet, never to the page behind it. */
    if (key && !focusKey(key)) node.focus({ preventScroll: true });
    place();
  }

  /* By its button; below 700 px the stylesheet makes it a sheet at the bottom. */
  function place() {
    /* Its button may be gone meanwhile, with the card it was on. */
    if (!sheet || !sheet.btn.isConnected) return;
    var s = sheet.node.style;
    s.left = s.top = s.bottom = s.maxHeight = '';
    if (global.innerWidth < 700) return;
    var r = sheet.btn.getBoundingClientRect();
    var w = sheet.node.offsetWidth;
    var left = sheet.btn.hasAttribute('data-play-on-left') ? r.left : r.right - w;
    s.left = Math.max(12, Math.min(global.innerWidth - w - 12, left)) + 'px';
    if (r.top > global.innerHeight / 2) {
      s.bottom = global.innerHeight - r.top + 6 + 'px';
      s.top = 'auto';
      s.maxHeight = Math.max(200, Math.min(620, r.top - 22)) + 'px';
    } else {
      s.top = r.bottom + 6 + 'px';
      s.maxHeight = Math.max(200, Math.min(620, global.innerHeight - r.bottom - 22)) + 'px';
    }
  }

  var poll = null;

  function open(btn, host) {
    if (sheet) return;
    var node = el('dialog', 'play-on');
    node.setAttribute('aria-labelledby', 'play-on-title');
    node.tabIndex = -1;
    sheet = { node: node, btn: btn, host: host, pairing: null };
    say = null;
    limit = false;
    document.body.appendChild(node);
    draw();
    node.showModal();
    node.focus({ preventScroll: true });
    place();
    btn.setAttribute('aria-expanded', 'true');
    node.addEventListener('click', function (e) {
      var r = node.getBoundingClientRect();
      if (e.target === node && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) close();
    });
    node.addEventListener('keydown', function (e) {
      /* The player's own keys are not for what is typed or pressed here. */
      e.stopPropagation();
      if (e.key === 'Tab') keepIn(e);
      /* What else on the page ends with Escape may have taken the dialog's own. */
      if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    });
    node.addEventListener('close', closed);
    global.addEventListener('resize', place);
    ways.forEach(function (w) { if (w.look) w.look(); });
    look();
    poll = setInterval(function () { if (!document.hidden && !absent && !lost) look(); }, LOOK_MS);
  }

  /* Safari's own step passes over buttons, and leads out of the dialog to its own bars. */
  function keepIn(e) {
    var all = Array.prototype.filter.call(sheet.node.querySelectorAll('button, input'), function (b) { return !b.disabled; });
    var at = all.indexOf(document.activeElement);
    var to = at + (e.shiftKey ? -1 : 1);
    if (to < 0) to = all.length - 1;
    e.preventDefault();
    all[to % all.length].focus();
  }

  function close() {
    if (!sheet) return;
    sheet.node.close();
    closed();
  }

  function closed() {
    var was = sheet;
    if (!was || was.node.open) return;
    sheet = null;
    say = null;
    clearInterval(poll);
    global.removeEventListener('resize', place);
    was.btn.setAttribute('aria-expanded', 'false');
    /* Safari focuses no button that is clicked, and playing elsewhere has another one. */
    var back = [was.btn].concat(MM.dom.qa('[data-play-on]')).filter(function (b) { return b.getClientRects().length; })[0];
    if (back) back.focus({ preventScroll: true });
    setTimeout(function () { was.node.remove(); }, LEAVE_MS);
  }

  /* ---- a title on a page without a player -------------------------------- */

  function saved(key) {
    try { return global.localStorage.getItem('mediaman.watch.' + key); } catch (e) { return null; }
  }

  /* The quality kept for the player, as one of those a TV can be given. */
  function savedQuality() {
    var height = parseInt((/^([0-9]+)p-/.exec(saved('quality') || '') || [])[1], 10);
    if (!height) return /^(best|medium|low)$/.test(saved('quality') || '') ? saved('quality') : 'auto';
    return height >= 1080 ? 'best' : height >= 720 ? 'medium' : 'low';
  }

  function titleButton(href, className) {
    var key = (/^\/watch\/play\/([0-9]+)/.exec(href) || [])[1];
    var b = button(className);
    var pressed = 0;
    b.setAttribute('data-play-on', '');
    b.setAttribute('data-play-on-left', '');
    b.setAttribute('data-play-on-title', '');
    b.setAttribute('aria-haspopup', 'dialog');
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-label', 'Play on a TV');
    b.setAttribute('title', 'Play on a TV');
    b.hidden = !hasTvs();
    b.appendChild(MM.dom.icon(TV_ICON, 'ico--tv'));
    b.addEventListener('click', function () {
      open(b, {
        what: function () {
          return {
            rating_key: key, quality: savedQuality(), autoplay: saved('autoplay') !== 'false',
            asked_s: Math.max(0, now() - pressed) / 1000,
          };
        },
        before: function () { pressed = now(); },
        on: function () {
          var tv = (tvs || []).filter(function (t) { return t.play && String(t.play.rating_key) === key && t.play.state !== 'ended'; })[0];
          return tv ? idOf(tv) : null;
        },
        here: function () { global.location.href = href; },
      });
    });
    if (tvs === null && !absent) look();
    return b;
  }

  MM.playOn = {
    button: titleButton,
    register: function (way) { ways.push(way); changed(); },
    changed: changed,
    again: function () {
      for (var i = 0; i < ways.length; i++) {
        var found = ways[i].again && ways[i].again();
        if (found) return { way: ways[i], found: found };
      }
      return null;
    },
    ready: function (fn) {
      listeners.push(fn);
      fn(any());
      if (tvs === null && !absent) look();
    },
    open: open,
    close: close,
    isOpen: function () { return !!sheet; },
  };
})(window);
