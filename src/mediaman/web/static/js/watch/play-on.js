/* The Play on sheet: this device, the person's TVs, and the browser's own ways to a TV.
 *
 *   MM.playOn.register(way)      a way of the browser's: AirPlay (airplay.js), Google Cast
 *   MM.playOn.changed()          what a way says of itself may be different now
 *   MM.playOn.again(key)         null, or {way, found}: a promise of the play of that title which a
 *                                way may find already on
 *   MM.playOn.mayBeOnTv()        whether a TV was playing anything when last heard of
 *   MM.playOn.onTv(key)          a promise of the TV that title plays on, or null; rejects where
 *                                that cannot be known
 *   MM.playOn.cancel()           what is starting is not wanted any more
 *   MM.playOn.ready(fn)          fn(whether there is anything to play on), now and as it changes
 *   MM.playOn.open(button, host) the sheet, by the button
 *   MM.playOn.button(href, className) the button of a title on a page without a player, by the
 *                                address it plays at
 *
 * A way: {id, label, sub, icon, available(), chooser, start(what), asks?, looking?, trouble?, look?, again?, release?,
 * leads?}. Its start is called in the press itself, since the browser shows its list of TVs for a
 * press alone; asks() says whether it will. looking() is true while it is not known yet whether it
 * is available, and trouble() says why it is not, as {title, line}. One that `leads` needs the player, which is opened.
 *
 * A host is the page's own player, or a page that has none:
 *   what()        {rating_key, position_s, quality, subtitle, autoplay, asked_s}
 *   on()          optional: null, or the id of what plays it now ('tv-5', a way's)
 *   before(tv)    optional, of the TV or the way: what plays is stopped and its place saved; a promise of the place
 *   failed()      optional: nothing came of it; it plays on as before, or what it says
 *   started(tv)   optional: a TV plays it, or the way {id, name}
 *   startWay(way) optional: plays it by a way of the browser's; a promise. Without it a way plays
 *                 it by itself, with no page to follow it
 *   here()        optional: bring it back to this device
 *
 * The TVs are those of GET /api/tvs. Where mediaman has no such route there are none.
 *
 * Requires: core/api.js, core/dom.js. Uses tv-remote.js where the page has it.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  if (MM.playOn) return;
  var TVS = '/api/tvs';
  var TV_ICON = 'play-on-tv'; // of _play_on.html
  var ON_TV_KEY = 'mediaman.watch.onTv';
  var CAST_URL = '/api/cast/stream';
  var LOOK_MS = 5000;
  var START_WAIT_MS = 125000;
  /* A TV just paired is heard of with its next question. */
  var FRESH_MS = 6000;
  var FRESH_EVERY_MS = 800;
  /* An older TV says only as it begins whether it can play the film. */
  var BEGIN_EVERY_MS = 1000;
  var BEGIN_WAIT_MS = 60000;
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
  /* What a TV says while it has a title. */
  var PLAYING = ['starting', 'loading', 'playing', 'paused', 'buffering'];
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

  /* What a way says of itself; one that fails says nothing. */
  function told(way, member) {
    try { return !!(way[member] && way[member]()); } catch (e) { return false; }
  }

  function usable() {
    return ways.filter(function (w) { return told(w, 'available'); });
  }

  /* Those that are, may come to be, or say why they are not. */
  function listed() {
    return ways.filter(function (w) { return told(w, 'available') || told(w, 'looking') || told(w, 'trouble'); });
  }

  function hasTvs() { return !absent && (tvs !== null || lost); }
  function any() { return hasTvs() || usable().length > 0; }

  function changed() {
    listeners.forEach(function (fn) { fn(any()); });
    MM.dom.qa('[data-play-on-title]').forEach(function (b) { b.hidden = !any(); });
    /* What is being typed there is left alone, and so is what the pointer may be on. */
    if (sheet && !sheet.pairing) draw(true);
  }

  /* ---- the TVs ---------------------------------------------------------- */

  function look() {
    if (looking) return looking;
    var before = JSON.stringify([tvs, lost, absent]);
    looking = MM.api.get(TVS).then(function (data) {
      tvs = data.tvs || [];
      lost = absent = false;
      remember(tvs.some(isPlaying));
    }, function (err) {
      var status = err && err.status;
      absent = status === 404 || status === 403;
      lost = !absent;
      if (absent) {
        tvs = null;
        remember(false);
      }
    }).then(function () {
      looking = null;
      if (JSON.stringify([tvs, lost, absent]) !== before) changed();
    });
    return looking;
  }

  function isPlaying(tv) { return !!tv.play && PLAYING.indexOf(tv.play.state) !== -1; }

  /* Kept for the pages to come: where nothing plays on a TV they have nothing to wait for. */
  function remember(on) {
    try {
      if (on) global.localStorage.setItem(ON_TV_KEY, '1');
      else global.localStorage.removeItem(ON_TV_KEY);
    } catch (e) { /* storage blocked */ }
  }
  function remembered() {
    try { return global.localStorage.getItem(ON_TV_KEY) !== null; } catch (e) { return true; }
  }

  function tvUrl(tv) { return TVS + '/' + encodeURIComponent(tv.id); }
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

  /* What is said where `tv` did not start, and what that `left`. */
  function refused(tv, err, left) {
    var code = err && err.error;
    if (code === 'in_use') return busy(tv);
    if (isOlder(tv) && (code === 'tv_off' || code === 'unreachable')) return notOpen(tv);
    if (code === 'cant_play') {
      return {
        id: idOf(tv),
        title: short(tv) + ' can’t play this one',
        line: ['Its picture is in a form older TVs refuse, and converting it failed. Try another TV, or watch here.' + (left || '')],
        bad: true,
      };
    }
    var line = code === 'tv_off' ? 'It stopped answering. Check it still shows mediaman, then choose it again.'
      : err && err.data && err.data.message || FAILED[code] || 'Choose it again in a moment.';
    return {
      id: idOf(tv),
      title: short(tv, 'It') + ' didn’t start',
      line: [line + (left || UNCHANGED)],
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

  function later(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* Asked again for a while where the TV was paired a moment ago: until it next asks, it is off. */
  function askPlay(tv, what, signal, freshUntil, wanted) {
    return MM.api.post(tvUrl(tv) + '/play', what, { signal: signal }).catch(function (err) {
      if (!err || err.error !== 'tv_off' || now() >= freshUntil || !wanted()) throw err;
      return later(FRESH_EVERY_MS).then(function () { return askPlay(tv, what, signal, freshUntil, wanted); });
    });
  }

  /* Settles as an older TV has begun; rejects where it cannot play it. */
  function begun(tv, wanted, until) {
    var play = tv.play;
    if (!play || !wanted() || now() > until) return Promise.resolve(tv);
    if (play.state === 'failed') {
      stopTv(tv).catch(function () { /* it plays nothing */ });
      return Promise.reject({ error: play.error === 'tv' ? 'cant_play' : play.error });
    }
    if (play.state !== 'starting' && play.state !== 'loading') return Promise.resolve(tv);
    return later(BEGIN_EVERY_MS).then(function () {
      return MM.api.get(tvUrl(tv)).then(function (data) { return data.tv; }, function () { return tv; });
    }).then(function (heard) { return begun(heard, wanted, until); });
  }

  /* `confirm` is the person's yes to taking the TV from somebody; `freshUntil`: just paired. */
  function startTv(tv, confirm, freshUntil) {
    if (wait) return;
    var mine = ++asking;
    var host = sheet.host;
    var stopper = new AbortController();
    var late;
    function wanted() { return mine === asking; }
    say = null;
    limit = false;
    wait = idOf(tv);
    draw();
    Promise.resolve(host.before ? host.before(tv) : undefined).then(function (place) {
      if (mine !== asking) throw new Error('cancelled');
      var what = host.what();
      if (place !== undefined) what.position_s = place;
      if (confirm) what.confirm = true;
      late = setTimeout(function () { stopper.abort(); }, START_WAIT_MS);
      remember(true);
      return askPlay(tv, what, stopper.signal, freshUntil || 0, wanted);
    }).then(function (data) {
      clearTimeout(late);
      if (!isOlder(tv) || !data.tv) return data;
      return begun(data.tv, wanted, now() + BEGIN_WAIT_MS).then(function (heard) { return { tv: heard }; });
    }).then(function (data) {
      if (mine !== asking) {
        /* Called off too late not to begin: that play is stopped, and nothing the TV has since. */
        if (data.tv && data.tv.play) stopTv(data.tv).catch(function () { /* it is over */ });
        return;
      }
      wait = null;
      close();
      if (host.started) host.started(data.tv || tv);
      else if (MM.tvRemote) MM.tvRemote.open(data.tv || tv);
    }, function (err) {
      clearTimeout(late);
      if (mine !== asking) return;
      wait = null;
      var left = host.failed && host.failed();
      if (err && err.error === 'too_many_streams') overLimit();
      else if (!err || SILENT.indexOf(err.error) === -1) tell(refused(tv, err, left));
      draw();
      look();
    });
  }

  /* End what `tv` says it plays, and nothing it plays since. */
  function stopTv(tv) {
    var body = { kind: 'stop' };
    if (tv.play.play_id) body.play_id = tv.play.play_id;
    return MM.api.post(tvUrl(tv) + '/commands', body);
  }

  function cancel() {
    asking++;
    say = wait = null;
    draw();
  }

  function tvWith(key) {
    return (tvs || []).filter(function (tv) {
      return isPlaying(tv) && String(tv.play.rating_key) === String(key);
    })[0] || null;
  }

  /* ---- the browser's own ways ------------------------------------------- */

  function startWay(way) {
    if (wait) return;
    var host = sheet.host;
    if (way.leads) {
      close();
      global.location.href = playerOf(host.what().rating_key) + '#play-on';
      return;
    }
    var mine = ++asking;
    var by = host.startWay || function (w, chosen) { return startAlone(w, chosen, host, function () { return mine === asking; }); };
    limit = false;
    say = way.chooser && (!way.asks || told(way, 'asks')) ? { id: way.id, title: way.chooser.title, line: [way.chooser.line] } : null;
    var playing = by(way, function () {
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
          line: [told ? told.line : (FAILED[code] || 'Choose it again in a moment.') + (err && err.left || UNCHANGED)],
          bad: true,
          again: function () { startWay(way); },
        });
      }
      draw();
    });
  }

  function playerOf(key) { return '/watch/play/' + encodeURIComponent(key) + '?resume=1'; }

  /* What the player's own page says of a title: its words, its place and what follows it. */
  function titleOf(key) {
    return fetch(playerOf(key), { credentials: 'same-origin' }).then(function (resp) { return resp.text(); }).then(function (html) {
      var el = new DOMParser().parseFromString(html, 'text/html').getElementById('player');
      var item = JSON.parse(el.getAttribute('data-item'));
      var next = JSON.parse(el.getAttribute('data-next') || 'null');
      var after = next && /^\/watch\/play\/([0-9]+)/.exec(next.href);
      return {
        item: item,
        image: el.getAttribute('data-show-href') ? '/api/watch/thumb/' + key : '/api/poster/' + key + '?kind=art',
        place: parseFloat(el.getAttribute('data-start-s')) || 0,
        next: after ? { rating_key: after[1], label: next.label } : null,
      };
    });
  }

  /* Plays it by a way where no player is: the way keeps its place from then on. */
  function startAlone(way, chosen, host, wanted) {
    var what = host.what();
    var began = now();
    var about = { rating_key: what.rating_key, title: '', subtitle: '', duration_s: 0, image: '' };
    var kept = null;
    var streamId = null;
    function cancelled() { return Object.assign(new Error('cancelled'), { code: 'cancelled' }); }
    function endStream() {
      if (streamId) MM.api.post('/api/watch/stream/' + encodeURIComponent(streamId) + '/stop').catch(function () { /* it ends by itself */ });
      streamId = null;
    }
    return way.start({
      title: about,
      position_s: what.position_s || 0,
      open: function () {
        if (!wanted()) return Promise.reject(cancelled());
        chosen();
        var place;
        var told;
        return Promise.all([titleOf(what.rating_key), host.before ? host.before(way) : undefined]).then(function (both) {
          if (!wanted()) throw cancelled();
          told = both[0];
          place = [both[1], what.position_s, told.place].filter(function (s) { return typeof s === 'number'; })[0];
          var body = {
            rating_key: what.rating_key, quality: what.quality || 'auto', page: pageName(),
            asked_s: (now() - began) / 1000,
          };
          if (what.subtitle) body.subtitle = what.subtitle;
          return MM.api.post(CAST_URL, body);
        }).then(function (data) {
          streamId = data.stream_id;
          if (!wanted()) throw cancelled();
          kept = {
            quality: data.quality, subtitle: what.subtitle || null, played_s: 0, given: { subtitle: data.subtitle || null, subtitle_tracks: data.subtitle_tracks || [] },
            markers: data.markers || [], chapters: data.chapters || [],
            next: told.next,
          };
          about = {
            rating_key: what.rating_key, title: told.item.title, subtitle: told.item.subtitle,
            duration_s: told.item.duration_s, image: told.image,
          };
          return {
            stream_id: data.stream_id, url: global.location.origin + data.url, position_s: place,
            duration_s: data.duration_s, paused: false, title: about,
          };
        }).catch(function (err) {
          endStream();
          throw err.code ? err : Object.assign(new Error('failed'), { code: err.error || 'failed' });
        });
      },
    }).then(function (play) {
      if (!wanted()) {
        play.stop();
        endStream();
        throw cancelled();
      }
      if (play.keep) play.keep(kept);
      play.leave();
      var on = { id: way.id, name: play.name || way.label };
      setTimeout(function () {
        if (host.started) host.started(on);
        else if (MM.tvRemote) MM.tvRemote.open(way.id);
      }, 0);
    }, function (err) {
      endStream();
      if (host.failed) err.left = host.failed();
      throw err;
    });
  }

  function pageName() {
    return Array.prototype.map.call(global.crypto.getRandomValues(new Uint8Array(12)), function (b) {
      return (b < 16 ? '0' : '') + b.toString(16);
    }).join('');
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
    if (o.starting || o.looks) end.appendChild(ring());
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

  function wayRow(list, way, on) {
    var here = on === way.id;
    var there = told(way, 'available');
    var looks = !there && told(way, 'looking');
    var trouble = !there && told(way, 'trouble') ? way.trouble() : null;
    var none = !there && !looks && !trouble && !here && wait !== way.id;
    row(list, {
      id: way.id, icon: way.icon, name: way.label, on: here, starting: wait === way.id, looks: looks, off: none, more: !none,
      sub: here ? 'Playing here now' : wait === way.id ? 'Starting…' : looks ? 'Looking for TVs…' : none ? 'No TV found' : way.sub,
    }, function () {
      if (here) {
        close();
      } else if (trouble || none) {
        say = trouble ? { id: way.id, title: trouble.title, line: [trouble.line], bad: true } : null;
        if (way.look) way.look();
        draw();
      } else {
        startWay(way);
      }
    });
  }

  /* What there is to play on, as it stood when the sheet opened: what comes meanwhile is last, what
     goes stays, so that no row moves from under a press. */
  function rowsOf(known, found) {
    known.forEach(function (tv) { sheet.rows[idOf(tv)] = { tv: tv }; });
    found.forEach(function (way) { sheet.rows[way.id] = { way: way }; });
    Object.keys(sheet.rows).forEach(function (id) {
      var one = sheet.rows[id];
      if (one.tv && known.indexOf(one.tv) === -1) one.tv = Object.assign({}, one.tv, { on: false, busy: false });
    });
    var off = function (tv) { return !tv.on && !tv.busy; };
    var now = known.filter(function (tv) { return !off(tv); }).map(idOf)
      .concat(found.map(function (way) { return way.id; }), known.filter(off).map(idOf));
    sheet.order = sheet.order.concat(now.filter(function (id) { return sheet.order.indexOf(id) === -1; }));
    return sheet.order.map(function (id) { return sheet.rows[id]; });
  }

  function panel(body) {
    var host = sheet.host;
    var on = host.on ? host.on() : null;
    var known = tvs || [];
    var found = listed();
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
      if ((on || wait) && host.here) host.here();
    });
    if (!first) {
      rowsOf(known, found).forEach(function (one) {
        if (one.tv) tvRow(list, one.tv, on);
        else wayRow(list, one.way, on);
      });
    }

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
      p.freshUntil = now() + FRESH_MS;
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
      startTv(tv, false, p.freshUntil);
      focusKey(idOf(tv));
    });
  }

  /* ---- showing it ------------------------------------------------------- */

  function focusKey(key) {
    var to = sheet && sheet.node.querySelector('[data-key="' + key + '"]');
    if (to) to.focus({ preventScroll: true });
    return !!to;
  }

  function keysIn(node) {
    return Array.prototype.map.call(node.querySelectorAll('[data-key]'), function (b) { return b.getAttribute('data-key'); }).join();
  }

  /* `held`: not for a press, so the sheet keeps its place and gets no lower, and rows wait to come
     while the pointer is on one. */
  function draw(held) {
    if (!sheet) return;
    var node = sheet.node;
    var at = document.activeElement;
    var key = at && node.contains(at) && at.getAttribute('data-key');
    var body = el('div', 'play-on__body');
    if (sheet.pairing) pairing(body);
    else panel(body);
    sheet.waits = !!held && sheet.over && keysIn(body) !== keysIn(node);
    if (sheet.waits) return;
    node.style.minHeight = held ? node.offsetHeight + 'px' : '';
    var top = node.scrollTop;
    node.textContent = '';
    node.appendChild(body);
    node.scrollTop = top;
    /* A row that is gone gives the focus to the sheet, never to the page behind it. */
    if (key && !focusKey(key)) node.focus({ preventScroll: true });
    if (!held) place();
  }

  /* By its button; below 700 px the stylesheet makes it a sheet at the bottom. */
  function place() {
    /* Its button may be gone meanwhile, with the card it was on or for the remote's: it stays. */
    if (!sheet || !sheet.btn.isConnected || !sheet.btn.getClientRects().length) return;
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
    ways.forEach(function (w) {
      try { if (w.look) w.look(); } catch (e) { /* it is not there */ }
    });
    sheet = { node: node, btn: btn, host: host, pairing: null, rows: {}, order: [], over: false, waits: false };
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
    node.addEventListener('pointerover', function (e) { pointed(!!e.target.closest('.play-on__row')); });
    node.addEventListener('pointerleave', function (e) {
      /* A finger's press is yet to come as it lifts. */
      if (e.pointerType === 'mouse') pointed(false);
      else setTimeout(function () { pointed(false); }, LEAVE_MS);
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
    look();
    poll = setInterval(function () { if (!document.hidden && !absent) look(); }, LOOK_MS);
  }

  function pointed(over) {
    if (!sheet) return;
    sheet.over = over;
    if (!over && sheet.waits) draw(true);
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

  function castNow(key) {
    var sender = MM.castSender;
    var plays = sender && sender.now && sender.now();
    return !!plays && String(plays.rating_key) === key;
  }
  function castHolds(key) { return !!(MM.castSender && MM.castSender.holds && MM.castSender.holds(key)); }

  function titleButton(href, className) {
    var key = (/^\/watch\/play\/([0-9]+)/.exec(href) || [])[1];
    var b = button(className);
    var pressed = 0;
    b.setAttribute('data-play-on', '');
    b.setAttribute('data-play-on-left', '');
    b.setAttribute('data-play-on-title', '');
    b.setAttribute('aria-haspopup', 'dialog');
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('data-play-on-key', key);
    var plays = !!(MM.tvRemote && MM.tvRemote.plays && MM.tvRemote.plays(key));
    b.classList.toggle('is-on', plays);
    b.setAttribute('aria-label', plays ? 'Playing on a TV' : 'Play on a TV');
    b.setAttribute('title', plays ? 'Playing on a TV' : 'Play on a TV');
    b.hidden = !any();
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
          var tv = tvWith(key);
          return tv ? idOf(tv) : castHolds(key) ? 'cast' : null;
        },
        here: function () {
          var tv = tvWith(key);
          function go() { global.location.href = playerOf(key); }
          if (wait) cancel();
          else if (tv) stopTv(tv).then(go, go);
          else if (castNow(key)) MM.castSender.stop().then(go, go);
          /* Of another tab: the player asks. */
          else if (castHolds(key)) go();
        },
      });
    });
    if (tvs === null && !absent) look();
    return b;
  }

  MM.playOn = {
    button: titleButton,
    register: function (way) { ways.push(way); changed(); },
    changed: changed,
    cancel: cancel,
    mayBeOnTv: function () { return !absent && remembered(); },
    onTv: function (key) {
      return look().then(function () {
        if (lost) throw new Error('lost');
        return tvWith(key);
      });
    },
    again: function (key) {
      for (var i = 0; i < ways.length; i++) {
        var found = ways[i].again && ways[i].again(key);
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
