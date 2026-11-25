/* What plays on the person's TV: the small bar on every page and the remote it opens
 * (_tv_remote.html; the server's side is routes/tv.py).
 *
 *   MM.tvRemote.open(tv)   opens the remote; tv is a TV as the API gives it, or its id.
 *   MM.tvRemote.drop(id)   lets go, without a word, of a TV the person has just unpaired.
 *
 * Every page asks once what plays. While something does, the bar asks every few seconds and the
 * open remote every second; both far less while the page is hidden, and not at all when nothing
 * plays. Nothing here is the truth: the TV, another tab and another phone act too. What the person
 * has just set is shown until the TV has carried it out, then the TV's own word is again.
 *
 * Requires: core/api.js, core/dom.js, modal-a11y.js, ui-feedback.js.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  var mini = document.getElementById('tv-mini');
  var root = document.getElementById('tv-remote');
  if (!mini || !root) return;

  // The player fills the screen with controls of its own: the remote opens over it, the bar does not.
  var hasBar = !document.getElementById('player');

  var LIST = '/api/tvs';
  var OPEN_MS = 1000;
  var BAR_MS = 5000;
  var HIDDEN_MS = 30000;
  var RETRY_MS = 3000;
  var LIMITED_MS = 10000;
  var TICK_MS = 500;
  var GAP_MS = 200; // between two commands, so that a dragged volume is not a hundred
  var KEY_SEEK_MS = 300; // after the last press of an arrow
  var HOLD_MS = 15000; // that what the person set is shown without a word of the TV
  var ANSWER_MS = 10000;
  var STEP_MS = 1000;
  var EXIT_MS = 250;
  var LEAVE_MS = 150; // --dur-fast
  var SKIP_S = 10;
  var FAILS = 2; // polls in a row before it is said
  var GAP_APART_PX = 4;
  var DISMISSED = 'mm.tv.dismissed';

  var QUALITIES = [
    ['auto', 'Auto', 'Up to 1080p'],
    ['best', '1080p', '8 Mbps'],
    ['medium', '720p', '4 Mbps'],
    ['low', '480p', '1.5 Mbps'],
  ];
  var FAILED = {
    gone: 'The stream ended.',
    tv: 'The TV couldn’t open the picture.',
    unreachable: 'mediaman couldn’t reach Plex.',
    not_found: 'It is no longer in the library.',
    too_many_streams: 'You’re watching on three screens, the most at once.',
    track_unavailable: 'Those subtitles can’t be played.',
    rate_limited: 'Too many tries. Wait a minute.',
    not_set_up: 'Plex isn’t set up.',
  };
  var REFUSED = {
    tv_off: 'The TV isn’t answering. Check it still shows mediaman.',
    in_use: 'Someone else is watching on that TV.',
    stale: 'Something else plays on the TV now.',
    not_playing: 'Nothing is playing on the TV now.',
    no_next: 'There is no next episode.',
    too_many_streams: 'You’re watching on three screens, the most at once. Stop one, then try again.',
    rate_limited: 'Too many tries. Wait a minute.',
    unreachable: 'mediaman couldn’t reach Plex. Try again in a moment.',
    track_unavailable: 'Those subtitles can’t be played.',
    not_found: 'It is no longer in the library.',
    not_set_up: 'Plex isn’t set up.',
  };
  // Said of an older TV, which shows no page of mediaman.
  var REFUSED_OLDER = {
    tv_off: 'The TV isn’t answering. Check it’s on.',
    unreachable: 'The TV or Plex didn’t answer. Try again in a moment.',
  };
  // A later press won: what the person sees is already that.
  var SILENT = ['superseded', 'replaced'];

  function $(sel) { return root.querySelector(sel); }
  function m(sel) { return mini.querySelector(sel); }
  var els = {
    title: $('[data-title]'), subtitle: $('[data-subtitle]'), on: $('[data-on]'), hint: $('[data-hint]'),
    ring: $('[data-ring]'), mid: $('[data-mid]'), bottom: $('[data-bottom]'), top: $('.player-top'),
    seekRow: $('[data-seek-row]'), seek: $('[data-seek]'), played: $('[data-played]'), thumb: $('[data-thumb]'),
    marks: $('[data-marks]'), hover: $('[data-seek-hover]'), bubble: $('[data-bubble]'),
    bubbleTitle: $('[data-bubble-title]'), bubbleTime: $('[data-bubble-time]'),
    elapsed: $('[data-elapsed]'), remaining: $('[data-remaining]'),
    play: $('[data-play]'), mute: $('[data-mute]'), volume: $('[data-volume]'), noVolume: $('[data-no-volume]'),
    chapter: $('[data-chapter]'), quality: $('[data-quality]'), next: $('[data-next]'), again: $('[data-again]'),
    bring: $('[data-bring]'), stop: $('[data-stop]'), dim: $('[data-dim]'), card: $('[data-card]'),
    cardTitle: $('[data-card-title]'), cardLine: $('[data-card-line]'), cardActions: $('[data-card-actions]'),
    status: $('[data-status]'), po: $('[data-po]'), poVeil: $('[data-po-veil]'), poTitle: $('[data-po-title]'),
    poList: $('[data-po-list]'), poNote: $('[data-po-note]'),
  };
  var live = MM.dom.qa('[data-live]', root);
  var arts = MM.dom.qa('[data-art]', root);

  var tv = null; // the TV shown, as last heard
  var others = []; // the person's TVs with something playing
  var heardAt = 0; // when the request of that was sent
  var place = { s: 0, at: 0 }; // where it plays, and when that was so
  var wants = {}; // what the person set that the TV has not carried out: kind -> {value, id, until}
  var gone = null; // how the play ended that was shown: {reason, last, tv}
  var seen = null; // the play last shown, {play_id, rating_key, ...}
  var starting = {}; // by TV: whether a press that starts a stream is not answered yet
  var leaving = null; // a stop of this page under way: what it is for
  var fails = 0;
  var offline = false;
  var isOpen = false;
  var letGo = null;
  var drag = null; // {play, t} while a finger or the mouse holds the seek bar
  var keySeek = null;
  var menuOpen = null; // {kind, from}
  var cardKey = null;
  var asks = false; // whether the card is one to be answered, with all else out of reach
  var timer = null;
  var asking = false; // a poll under way
  var ended = false; // signed out, or not let in: nothing more is asked
  var dismissed = '';
  try { dismissed = global.sessionStorage.getItem(DISMISSED) || ''; } catch (e) { /* no storage */ }

  function now() { return Date.now(); }
  function isHidden() { return !!document.hidden; }
  function fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600);
    var min = Math.floor(s / 60) % 60;
    var sec = s % 60;
    return (h ? h + ':' + (min < 10 ? '0' : '') : '') + min + ':' + (sec < 10 ? '0' : '') + sec;
  }
  function episode(p) { return p && p.season != null && p.episode != null; }
  function code(p, between) { return 'S' + p.season + (between || ' ') + 'E' + p.episode; }
  function play() { return tv && tv.play; }
  function about() { return play() || (gone && gone.last) || seen; }
  function isOlder() { var t = tv || (gone && gone.tv); return !!t && t.kind === 'older'; }
  function name() { return (tv && tv.name) || (gone && gone.tv.name) || 'the TV'; }
  function duration() { var p = about(); return (p && p.duration_s) || 0; }
  // By the attribute: an SVG icon has no such property.
  function setHidden(el, off) { if (el.hasAttribute('hidden') !== off) el.toggleAttribute('hidden', off); }
  /* What opens in the remote goes as it came: out of reach at once, then out of sight. */
  var leaves = new Map(); // what is on its way out, and its timer
  function enter(el) {
    clearTimeout(leaves.get(el));
    leaves.delete(el);
    el.classList.remove('is-leaving');
    el.inert = false;
    el.hidden = false;
  }
  function leave(el) {
    if (el.hidden || leaves.has(el)) return;
    el.inert = true;
    if (global.matchMedia('(prefers-reduced-motion: reduce)').matches) { el.hidden = true; return; }
    el.classList.add('is-leaving');
    leaves.set(el, setTimeout(function () {
      leaves.delete(el);
      el.classList.remove('is-leaving');
      el.hidden = true;
    }, LEAVE_MS));
  }
  function attr(el, key, value) { if (el.getAttribute(key) !== value) el.setAttribute(key, value); }
  function say(text) { global.UIFeedback.toast(text); }

  /* ── what the person set, until the TV has ── */

  function want(kind, value) { wants[kind] = { value: value, id: null, until: now() + HOLD_MS }; }
  function wanted(kind) {
    var w = wants[kind];
    if (!w) return null;
    if (now() > w.until || (w.id !== null && tv && tv.acked >= w.id)) {
      delete wants[kind];
      return null;
    }
    return w;
  }
  function shown(kind, real) { var w = wanted(kind); return w ? w.value : real; }

  function position() {
    if (drag) return drag.t;
    var w = wanted('seek');
    if (w) return w.value;
    var p = play();
    if (!p) return gone && gone.last ? gone.last.position_s || 0 : 0;
    var t = place.s;
    // What was heard of a TV that no longer answers does not run on.
    if (p.state === 'playing' && tv.on && shown('state', 'play') === 'play') t += Math.min(now() - place.at, ANSWER_MS) / 1000;
    var d = duration();
    return Math.max(0, d ? Math.min(d, t) : t);
  }

  /* ── hearing from the server ── */

  function hear(heard, sentAt) {
    if (sentAt < heardAt) return; // an answer overtaken by a later one
    heardAt = sentAt;
    var before = play();
    var boot = tv && tv.boot;
    var was = position();
    tv = heard;
    var p = tv.play;
    if (!before || !p || before.play_id !== p.play_id) {
      wants = {};
      line = [];
      endDrag(false);
      dropKeySeek();
      closeMenu();
    }
    if (p) {
      gone = null;
      seen = p;
      place = { s: p.position_s || 0, at: now() };
      // What is waited for elsewhere is not counted on from the answer's own clock.
      if (p.upnext_in_s != null) p.upnextAt = now() + p.upnext_in_s * 1000;
    } else if (before && !tv.last && tv.boot !== boot) {
      over('restarted', Object.assign({}, seen, { position_s: was }));
    } else if (before) {
      over(tv.last ? tv.last.reason : '', tv.last);
    }
  }

  /* What was shown plays no more. */
  function over(reason, last) {
    var mine = leaving;
    leaving = null;
    // The last word of a play has its place and title; its options are as they were last seen.
    var same = last && seen && seen.rating_key === last.rating_key;
    var was = { reason: reason, last: same ? Object.assign({}, seen, last) : last || seen, tv: tv };
    if (mine) return;
    if (reason === 'restarted' && !isOpen) {
      say('mediaman restarted. ' + keptIn(last));
      return;
    }
    if (reason === 'stopped' && isOpen) {
      say(place_kept('Stopped.', last));
      close();
    } else if (reason && reason !== 'stopped' && (isOpen || reason !== 'moved' && !(seen && seen.play_id === dismissed))) {
      gone = was;
    } else if (isOpen) {
      say('Nothing is playing on ' + name() + ' now.');
      close();
    }
  }

  function keptIn(what) { return 'Your place in ' + what.title + ' is kept.'; }
  function place_kept(before, last) {
    return last && last.position_s ? before + ' Your place is saved at ' + fmt(last.position_s) + '.' : before;
  }

  /* The TV is no longer the person's: 410 says why while it is fresh, 404 says nothing. */
  function lostTv(err) {
    var before = play();
    if (tv) tv.play = null;
    wants = {};
    endDrag(false);
    closeMenu();
    if (!before && !isOpen) { tv = null; return; }
    if (err.status === 410) over(err.error, err.data && err.data.last);
    else over('', null);
    if (!gone) tv = null;
  }

  function isGone(err) { return err.status === 404 && err.error === 'not_found' || err.status === 410 && err.error !== 'replaced'; }

  /* An answer cut off in its body comes as an empty one. */
  function get(path, key) {
    var stopper = new AbortController();
    var late = setTimeout(function () { stopper.abort(); }, ANSWER_MS);
    return MM.api.get(LIST + path, { signal: stopper.signal }).then(function (data) {
      clearTimeout(late);
      if (!data[key]) throw new Error('no answer');
      return data[key];
    }, function (err) {
      clearTimeout(late);
      throw err;
    });
  }
  function getOne(id) { return get('/' + encodeURIComponent(id), 'tv'); }

  function askOne() {
    var id = tv.id;
    var at = now();
    return getOne(id).then(function (heard) {
      if (tv && tv.id === id) hear(heard, at);
    }, function (err) {
      if (!isGone(err)) throw err;
      if (tv && tv.id === id) lostTv(err);
    });
  }

  /* Whether what is shown is a lost play the person has dismissed. */
  function putAway() { return !isOpen && isLost() && play().play_id === dismissed; }

  function askAll() {
    var at = now();
    return get('', 'tvs').then(function (all) {
      others = all.filter(function (t) { return t.play; });
      var mine = tv && all.filter(function (t) { return t.id === tv.id; })[0];
      // Of an older TV, which everyone keeps, the list does not say why it ended.
      if (play() && !(mine && (mine.play || mine.last))) return askOne();
      if (play() || (mine && mine.play)) hear(mine, at);
      if (gone || play() && !(putAway() && others.some(function (t) { return t.on && t.id !== tv.id; }))) return;
      tv = null;
      var next = others.filter(function (t) { return t.on; })[0] || others[0];
      if (next) hear(next, at);
    });
  }

  function tick() {
    clearTimeout(timer);
    timer = null;
    if (asking || ended || !(isOpen || hasBar)) return;
    asking = true;
    (isOpen && tv && !gone ? askOne().then(function () { return gone && askAll(); }) : askAll()).then(function () {
      fails = 0;
      offline = false;
      return 0;
    }, function (err) {
      if (err.status === 401 || err.status === 403) { ended = true; return 0; }
      // Seconds without a word are not one lost request.
      fails += err.name === 'AbortError' ? FAILS : 1;
      if (fails >= FAILS && err.error !== 'rate_limited' && err.status !== 429) offline = true;
      return err.status === 429 ? LIMITED_MS : RETRY_MS;
    }).then(function (wait) {
      asking = false;
      render();
      schedule(wait);
    });
  }

  function schedule(wait) {
    clearTimeout(timer);
    timer = null;
    if (asking || ended) return;
    if (!play() && !(gone && elsewhere().length) || !(isOpen || hasBar)) return;
    var every = isOpen && !gone ? OPEN_MS : BAR_MS;
    timer = setTimeout(tick, isHidden() ? HIDDEN_MS : Math.max(every, wait || 0));
  }

  /* ── commands ── */

  var line = []; // steering not sent yet, the newest of a kind
  var sending = false;

  function post(body) {
    var id = tv.id;
    var at = now();
    var stopper = new AbortController();
    var late = setTimeout(function () { stopper.abort(); }, body.kind === 'next' || body.kind === 'again' || body.kind === 'quality' || body.kind === 'subtitles' ? 110000 : ANSWER_MS);
    return MM.api.post(LIST + '/' + encodeURIComponent(id) + '/commands', body, { signal: stopper.signal }).then(function (data) {
      clearTimeout(late);
      if (data.tv && tv && tv.id === id) hear(data.tv, at);
      return data;
    }, function (err) {
      clearTimeout(late);
      throw err;
    });
  }

  function refuse(err) {
    if (isGone(err)) { lostTv(err); render(); return; }
    if (SILENT.indexOf(err.error) === -1) {
      var text = err.name === 'AbortError' ? 'mediaman didn’t answer. Try again.'
        : isOlder() && REFUSED_OLDER[err.error] || REFUSED[err.error] || MM.api.messageOf(err, 'That didn’t work. Try again.');
      global.UIFeedback.error(text);
    }
    tick();
  }

  /* Sent one at a time and in order: of what waits, only the newest of its kind. */
  function steer(kind, body, value) {
    var p = play();
    if (!p) return;
    body.play_id = p.play_id;
    want(kind, value);
    var item = { kind: kind, body: body, want: wants[kind], tv: tv.id };
    line = line.filter(function (x) { return x.kind !== kind; });
    line.push(item);
    render();
    pump();
  }

  function pump() {
    if (sending || !line.length) return;
    var item = line.shift();
    if (!tv || tv.id !== item.tv) { pump(); return; }
    sending = true;
    post(item.body).then(function (data) {
      item.want.id = data.id;
    }, function (err) {
      if (wants[item.kind] === item.want) delete wants[item.kind];
      // A newer one of the kind is on its way: it speaks for both.
      if (!line.some(function (x) { return x.kind === item.kind; })) refuse(err);
    }).then(function () {
      render();
      setTimeout(function () { sending = false; pump(); }, GAP_MS);
    });
  }

  /* What starts a stream, and may take long: the steering goes on meanwhile. */
  function start(body) {
    var p = play();
    if (!p || busy()) return;
    var id = tv.id;
    body.play_id = p.play_id;
    starting[id] = true;
    render();
    post(body).catch(refuse).then(function () {
      delete starting[id];
      render();
      schedule();
    });
  }

  function playAgain() {
    var on = gone && onward(gone.last);
    if (!on || busy()) return;
    var id = tv.id;
    var at = now();
    starting[id] = true;
    render();
    MM.api.post(LIST + '/' + encodeURIComponent(id) + '/play', { rating_key: on.rating_key, position_s: on.position_s })
      .then(function (data) { if (tv && tv.id === id) hear(data.tv, at); }, refuse)
      .then(function () {
        delete starting[id];
        render();
        schedule();
      });
  }

  function stop(then) {
    var p = play();
    if (!p || leaving) return;
    var was = { name: name(), ended: p.state === 'ended', play: p };
    leaving = was;
    line = [];
    render();
    function done(last) {
      leaving = null;
      if (then) { then(last || p); return; }
      say(was.ended ? 'Disconnected from ' + was.name + '.' : place_kept('Stopped.', last));
      close();
      render();
      tick();
    }
    post({ kind: 'stop', play_id: p.play_id }).then(function (data) {
      done(data.tv && data.tv.last);
    }, function (err) {
      leaving = null;
      if (err.error === 'not_playing') done(null);
      else refuse(err);
      render();
    });
  }

  function watchHere(what) {
    var to = '/watch/play/' + encodeURIComponent(what.rating_key) + '?resume=1';
    // In the place of the remote's own entry, or Back from the player would lead to the remote.
    settled(function () {
      if (level()) global.location.replace(to);
      else global.location.href = to;
    });
  }
  /* What goes on from a play: the title at its place or, past its end, what follows it, if anything. */
  function onward(what) {
    if (!what || !what.rating_key) return null;
    var same = seen && seen.rating_key === what.rating_key;
    var d = what.duration_s || 0;
    var over = same && ['ended', 'upnext', 'still'].indexOf(seen.state) !== -1 || d && what.position_s >= d - 1;
    if (!over) return { rating_key: what.rating_key, position_s: what.position_s || 0 };
    var next = same && seen.next;
    return next && next.rating_key ? { rating_key: next.rating_key, position_s: 0, next: true } : null;
  }
  function bring() {
    function go(what) { watchHere(onward(what) || what); }
    if (play()) stop(go);
    else if (gone && gone.last) go(gone.last);
  }

  function busy() { return !!tv && !!starting[tv.id]; }
  function canSteer() {
    var p = play();
    return !!p && !gone && !offline && !leaving && !busy() && tv.on && ['playing', 'paused', 'buffering'].indexOf(p.state) !== -1;
  }
  function toggle() {
    if (!canSteer()) return;
    var to = isPaused() ? 'play' : 'pause';
    // Where it stands does not jump as the wish is shown.
    place = { s: position(), at: now() };
    steer('state', { kind: to }, to);
  }
  function isPaused() { var p = play(); return shown('state', p && p.state === 'paused' ? 'pause' : 'play') === 'pause'; }
  function seekTo(t) {
    dropKeySeek();
    if (!canSteer()) return;
    t = Math.max(0, duration() ? Math.min(duration(), t) : t);
    steer('seek', { kind: 'seek', position_s: t }, t);
  }
  function dropKeySeek() {
    if (keySeek) clearTimeout(keySeek);
    keySeek = null;
  }
  /* Presses in a row are one seek. */
  function skip(d) {
    if (!canSteer()) return;
    var t = position() + d;
    t = Math.max(0, duration() ? Math.min(duration(), t) : t);
    want('seek', t);
    dropKeySeek();
    keySeek = setTimeout(function () { keySeek = null; seekTo(t); }, KEY_SEEK_MS);
    render();
  }
  function setVolume(level) {
    if (!canSteer()) return;
    steer('volume', { kind: 'volume', level: level }, level);
  }
  function mute() {
    if (!canSteer()) return;
    var to = !shown('mute', !!tv.muted);
    steer('mute', { kind: 'mute', muted: to }, to);
  }

  /* ── drawing ── */

  var artOf = null;
  function showArt(p) {
    var src = p && p.rating_key
      ? (episode(p) ? '/api/watch/thumb/' + encodeURIComponent(p.rating_key) : '/api/poster/' + encodeURIComponent(p.rating_key) + '?kind=art')
      : '';
    if (src === artOf) return;
    artOf = src;
    arts.concat(m('[data-mini-art]')).forEach(function (img) {
      img.hidden = true;
      if (src) img.src = src;
    });
  }
  arts.concat(m('[data-mini-art]')).forEach(function (img) {
    img.addEventListener('load', function () { img.hidden = false; });
    img.addEventListener('error', function () { img.hidden = true; });
  });

  function setPlayButton(btn, waiting, paused) {
    setHidden(btn.querySelector('.ico'), waiting);
    setHidden(btn.querySelector('.player-btn-ring'), !waiting);
    if (!waiting) MM.dom.setIcon(btn.querySelector('.ico'), paused ? 'i-play' : 'i-pause');
    attr(btn, 'aria-label', waiting ? 'Waiting for the TV' : paused ? 'Play' : 'Pause');
  }

  function isLoading(p) { return busy() || p.state === 'starting' || p.state === 'loading'; }
  function isWaiting(p) { return isLoading(p) || !!leaving || p.state === 'buffering' || (!!wanted('seek') && !keySeek && !drag); }
  function isLost() { return !!play() && !tv.on && tv.lost_s != null; }

  function render() {
    var p = play();
    showArt(about());
    renderMini(p);
    if (isOpen) renderRemote(p);
    paintTime();
  }

  var miniLeaving = null;
  function renderMini(p) {
    if (!hasBar) return;
    var bad = !!gone || isLost();
    var key = gone ? (seen && seen.play_id) || '' : p ? p.play_id : '';
    var away = offline && !gone;
    var show = !!tv && (!!p || !!gone) && !(bad && key === dismissed);
    if (!show) {
      if (mini.hidden || miniLeaving) return;
      mini.classList.add('is-leaving');
      miniLeaving = setTimeout(function () { miniLeaving = null; mini.hidden = true; }, EXIT_MS);
      return;
    }
    if (miniLeaving) clearTimeout(miniLeaving);
    miniLeaving = null;
    mini.classList.remove('is-leaving');
    setHidden(mini, false);

    var what = about();
    var title = what.title + (episode(what) ? ' · ' + code(what) : '');
    var line1 = gone ? (gone.reason === 'lost' ? 'Lost ' : 'Stopped on ') + name() + '. Tap to see why.'
      : away ? 'Can’t reach mediaman' : isLost() ? 'Lost ' + name() + '. Tap to see why.'
        : p.state === 'failed' ? 'Can’t play on ' + name()
          : p.state === 'still' ? 'Still watching on ' + name() + '?'
            : p.state === 'ended' || p.state === 'upnext' ? 'Finished on ' + name()
              : isLoading(p) ? 'Starting on ' + name()
                : (isPaused() ? 'Paused on ' : 'Playing on ') + name();
    mini.classList.toggle('is-bad', bad || away || !!p && p.state === 'failed');
    MM.dom.setText(m('[data-mini-title]'), title);
    MM.dom.setText(m('[data-mini-say]'), line1);
    attr(m('[data-open]'), 'aria-label', 'Open the remote: ' + title + '. ' + line1);
    MM.dom.setText(m('[data-mini-status]'), bad || away ? line1 : '');
    var steers = !bad && !away && !!p && (canSteer() || isLoading(p));
    setHidden(m('[data-mini-back]'), !steers);
    setHidden(m('[data-mini-play]'), !steers);
    if (steers) setPlayButton(m('[data-mini-play]'), isWaiting(p), isPaused());
    setHidden(m('[data-mini-stop]'), bad || away);
    setHidden(m('[data-dismiss]'), !bad || away);
    var more = m('[data-other]');
    var all = [tv].concat(elsewhere());
    var order = others.map(function (t) { return t.id; }).indexOf(tv.id);
    setHidden(more, all.length < 2);
    if (!more.hidden) {
      MM.dom.setText(more, (Math.max(0, order) + 1) + '/' + all.length);
      attr(more, 'aria-label', 'Show the next TV, ' + all[1].name);
    }
  }

  function hintFor(p) {
    if (gone) return place_kept('', gone.last).trim() || 'Your place is kept.';
    if (offline) return 'The TV may still be playing.';
    if (isLost()) return place_kept('', { position_s: position() }).trim() || 'Your place is kept.';
    if (p.state === 'failed') return 'Your place is kept.';
    if (p.state === 'still') return 'Waiting for an answer';
    if (p.state === 'upnext' && p.upnext_in_s == null) return name() + ' waits for you to choose.';
    if (p.state === 'ended') return name() + ' is ready for something else.';
    if (isLoading(p)) return 'Starting. This can take a few seconds.';
    return 'You can lock or close this device. The TV carries on.';
  }

  function renderRemote(p) {
    var what = about();
    if (!what || !p && !gone) return;
    var bad = !!gone || isLost() || offline;
    var isEnd = !bad && !!p && p.state === 'ended';
    var loading = !bad && !!p && isLoading(p);
    MM.dom.setText(els.title, what.title);
    MM.dom.setText(els.subtitle, episode(what) ? code(what, ' · ') + (what.episode_title ? ' · ' + what.episode_title : '') : '');
    setHidden(els.subtitle, !episode(what));
    root.classList.toggle('is-gone', bad);
    root.classList.toggle('is-load', loading);
    root.classList.toggle('is-upnext', !bad && !loading && !!p && p.state === 'upnext');
    root.classList.toggle('is-paused', !bad && !!p && (isPaused() || p.state === 'still'));
    setHidden(els.ring, !loading);
    MM.dom.setText(els.on, (loading ? 'Starting on ' : gone || isLost() ? 'Was playing on '
      : isEnd ? 'Finished on ' : offline ? 'Playing on '
        : p.state === 'failed' ? 'Couldn’t play on ' : p.state === 'upnext' ? 'Finished on '
          : isPaused() || p.state === 'still' ? 'Paused on ' : 'Playing on ') + name());
    MM.dom.setText(els.hint, hintFor(p));

    var steers = canSteer();
    live.forEach(function (el) {
      el.classList.toggle('is-dead', !steers && el !== els.play);
      el.inert = !steers;
    });
    els.play.inert = !steers && !loading;
    els.play.classList.toggle('is-dead', !steers && !loading);
    els.seekRow.classList.toggle('is-dead', !steers);
    els.seek.inert = !steers;
    live.forEach(function (el) { setHidden(el, isEnd); });
    setHidden(els.seekRow, isEnd || !duration());
    setPlayButton(els.play, !!p && isWaiting(p), !p || isPaused() || p.state === 'still' || p.state === 'upnext');

    var chapters = (what.chapters || []).length > 1;
    if (!isEnd) {
      setHidden($('[data-menu="chapters"]'), !chapters);
      setHidden($('[data-menu="subtitles"]'), !(what.subtitle_tracks || []).length);
      setHidden(els.next, !what.next);
    }
    MM.dom.setText(els.quality, (QUALITIES.filter(function (q) { return q[0] === what.quality; })[0] || QUALITIES[0])[1]);
    if (p && p.next && p.next.season != null) {
      attr(els.next, 'aria-label', 'Next episode, ' + code(p.next) + (p.next.title ? ', ' + p.next.title : ''));
    } else attr(els.next, 'aria-label', 'Next episode');

    var level = shown('volume', tv ? tv.volume : null);
    var muted = shown('mute', !!(tv && tv.muted));
    var volumeKnown = !!tv && tv.can_volume && level != null;
    // Before the TV has said whether it can, nothing is said of it.
    setHidden(els.volume, !volumeKnown);
    setHidden(els.mute, !volumeKnown);
    setHidden(els.noVolume, volumeKnown || loading || !tv || tv.can_volume !== false);
    if (volumeKnown) {
      var pct = Math.round(level * 100);
      if (!dragVolume && Number(els.volume.value) !== pct) els.volume.value = String(pct);
      els.volume.style.setProperty('--fill', (dragVolume ? els.volume.value : pct) + '%');
      MM.dom.setIcon(els.mute.querySelector('.ico'), muted || !pct ? 'i-volume-xmark' : 'i-volume-high');
      attr(els.mute, 'aria-label', muted ? 'Turn the TV’s sound on' : 'Mute the TV');
    }

    setHidden(els.again, !isEnd);
    setHidden(els.bring, isEnd);
    els.bring.disabled = !!leaving;
    els.stop.disabled = !!leaving;
    MM.dom.setText(els.stop, isEnd ? 'Disconnect' : 'Stop');
    drawMarks(what);
    renderCard(p);
    reach();
  }

  /* What had the focus went out of reach: the keys would be the page's again. */
  var wasEnd = false;
  function keepFocus() {
    if (!isOpen) return;
    var a = document.activeElement;
    var isEnd = !els.again.hidden;
    var lost = a === document.body || root.contains(a) && a.closest('[inert], [hidden]') || a === root && isEnd && !wasEnd;
    wasEnd = isEnd;
    if (!lost || menuOpen) return;
    (asks ? els.cardActions.firstElementChild : isEnd ? els.again : root).focus({ preventScroll: true });
  }

  var marksOf = '';
  function drawMarks(p) {
    var d = p.duration_s || 0;
    var width = els.seek.getBoundingClientRect().width;
    var key = [p.play_id, d, width].join();
    if (key === marksOf || !width) return;
    marksOf = key;
    els.marks.textContent = '';
    if (!d) return;
    function mark(cls, from, to) {
      var el = document.createElement('span');
      el.className = cls;
      el.style.left = from / d * 100 + '%';
      if (to) el.style.width = (to - from) / d * 100 + '%';
      els.marks.appendChild(el);
    }
    (p.markers || []).forEach(function (x) {
      mark('player-band' + (x.start_s / d * width < GAP_APART_PX ? ' is-first' : '') +
        ((d - x.end_s) / d * width < GAP_APART_PX ? ' is-last' : ''), x.start_s, x.end_s);
    });
    var last = 0;
    (p.chapters || []).forEach(function (c) {
      var x = c.start_s / d * width;
      if (x - last < GAP_APART_PX || width - x < GAP_APART_PX) return;
      last = x;
      mark('player-gap', c.start_s);
    });
  }
  global.addEventListener('resize', function () { if (isOpen) render(); });

  function chapterAt(t) {
    var list = (about() && about().chapters) || [];
    if (list.length < 2) return null;
    return list.filter(function (c) { return c.start_s <= t; }).pop() || list[0];
  }

  function paintTime() {
    var t = position();
    var d = duration();
    if (!mini.hidden) m('[data-mini-bar]').style.transform = 'scaleX(' + (d ? t / d : 0) + ')';
    if (!isOpen) return;
    var pct = d ? t / d * 100 + '%' : '0%';
    els.played.style.width = pct;
    els.thumb.style.left = pct;
    MM.dom.setText(els.elapsed, fmt(t));
    MM.dom.setText(els.remaining, d ? '-' + fmt(d - t) : '');
    attr(els.seek, 'aria-valuemax', String(Math.round(d)));
    attr(els.seek, 'aria-valuenow', String(Math.round(t)));
    attr(els.seek, 'aria-valuetext', fmt(t) + ' of ' + fmt(d));
    var c = chapterAt(t);
    MM.dom.setText(els.chapter, c ? c.title : '');
    attr($('[data-menu="chapters"]'), 'aria-label', c ? 'Chapters: ' + c.title : 'Chapters');
    var p = play();
    if (cardKey && p && p.state === 'upnext') MM.dom.setText(els.cardTitle, upnextTitle(p));
  }
  setInterval(function () { if (play() && !isHidden()) paintTime(); }, TICK_MS);

  /* ── cards ── */

  function upnextTitle(p) {
    if (p.upnextAt == null) return 'Next episode';
    return 'Next episode in ' + Math.max(0, Math.ceil((p.upnextAt - now()) / 1000)) + ' s';
  }
  function clock(ms) {
    var d = new Date(ms);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function cardFor(p) {
    var n = name();
    var last = gone && gone.last;
    var here = ['Bring it here', bring];
    if (gone) {
      var after = last && last.position_s ? ' at ' + fmt(last.position_s) : '';
      var card = {
        lost: [n + ' stopped answering', 'It was off, asleep or away from its browser for 2 minutes, so it stopped' + after + '.'],
        taken: ['Someone else is using ' + n, 'They paired it, and yours stopped' + after + '.'],
        unpaired: [n + ' was unpaired', 'It was forgotten in Settings, or in your Profile, or on the TV. To use it again, pair it.'],
        moved: ['You’re watching on another device', 'It goes on there, so ' + n + ' stopped' + after + '.'],
        still: [n + ' stopped by itself', 'Nobody answered, so it stopped' + after + '.'],
        signed_out: [n + ' stopped', 'You signed out everywhere, which ends what plays on a TV.'],
        restarted: ['mediaman restarted', keptIn(last || {})],
      }[gone.reason] || [n + ' stopped', 'Yours stopped' + after + '.'];
      var older = isOlder() && {
        lost: 'It didn’t answer for 2 minutes, so it stopped' + after + '.',
        taken: 'They started something on it, and yours stopped' + after + '.',
        unpaired: 'An admin removed it or turned older TVs off. Yours stopped' + after + '.',
        signed_out: 'The sign-in that started it has ended, so it stopped' + after + '.',
      }[gone.reason];
      if (older) card = [gone.reason === 'unpaired' ? n + ' can’t be used now' : card[0], older];
      var mine = ['lost', 'still', 'restarted'].indexOf(gone.reason) !== -1;
      var on = onward(last);
      var acts = mine ? [[gone.reason === 'still' ? 'Continue watching' : on && on.next ? 'Play next episode' : gone.reason === 'restarted' ? 'Play again' : 'Try again', playAgain], here, ['Close', dismiss]]
        : gone.reason === 'moved' ? [['Close', dismiss], here] : [here, ['Close', dismiss]];
      if (!on) acts = [['Close', dismiss]];
      return { key: 'gone' + gone.reason, title: card[0], line: card[1], acts: acts };
    }
    if (offline) {
      var off = global.navigator && global.navigator.onLine === false;
      return { key: 'offline', title: off ? 'This device is offline' : 'mediaman isn’t answering',
        line: 'You can’t control the TV until it’s back. Use the TV’s own remote meanwhile.', acts: [['Try again', tick]] };
    }
    if (!p || leaving) return null;
    if (isLost()) {
      return { key: 'lost', title: n + ' stopped answering',
        line: isOlder() ? 'It may be off or away from the network. If it answers within 2 minutes, you can go on from here.'
          : 'It may be off, asleep or away from its browser. If it comes back within 2 minutes, it carries on by itself.',
        acts: [['Try again', tryTv]].concat(onward(p) ? [here] : [], [['Stop', function () { stop(); }]]) };
    }
    // A press that was just made is being carried out.
    if (busy()) return null;
    if (p.state === 'failed') {
      return { key: 'failed' + p.error, title: 'This can’t play on ' + n,
        line: (FAILED[p.error] || FAILED.tv) + ' Your place is kept.',
        acts: [['Try again', function () { start({ kind: 'again' }); }], here, ['Stop', function () { stop(); }]] };
    }
    if (p.state === 'still') {
      var mins = p.still_in_s == null ? 10 : Math.max(1, Math.ceil(p.still_in_s / 60));
      return { key: 'still', title: 'Are you still watching?', ask: true,
        line: n + ' has waited since ' + clock(now() - (p.still_since_s || 0) * 1000) + '. It stops by itself in ' +
          mins + (mins === 1 ? ' minute' : ' minutes') + ' and keeps your place.',
        acts: [['Continue watching', function () { start({ kind: 'next' }); }], ['Stop', function () { stop(); }]] };
    }
    if (p.state === 'upnext') {
      var nx = p.next || {};
      return { key: 'upnext', title: upnextTitle(p), upnext: true,
        line: nx.season != null ? code(nx, ' · ') + (nx.title ? ' · ' + nx.title : '') : p.title,
        acts: [['Play now', function () { start({ kind: 'next' }); }], ['Cancel', cancelNext]] };
    }
    return null;
  }

  function tryTv() {
    tick();
    els.status.textContent = '';
    setTimeout(function () { if (isLost()) els.status.textContent = name() + ' is still not answering.'; }, 1500);
  }
  function cancelNext() {
    var p = play();
    if (p) post({ kind: 'cancel', play_id: p.play_id }).then(render, refuse);
  }
  function dismiss() {
    dismissed = gone ? (seen && seen.play_id) || '' : play() ? play().play_id : '';
    try { global.sessionStorage.setItem(DISMISSED, dismissed); } catch (e) { /* no storage */ }
    if (gone) { gone = null; tv = null; }
    close();
    render();
    tick();
  }

  function renderCard(p) {
    var card = cardFor(p);
    var key = card && card.key;
    if (!card) {
      if (cardKey) {
        var held = els.card.contains(document.activeElement);
        cardKey = null;
        asks = false;
        els.status.textContent = '';
        leave(els.card);
        leave(els.dim);
        reach();
        if (held) (!els.again.hidden ? els.again : els.play.inert || els.play.hidden ? $('[data-close]') : els.play).focus({ preventScroll: true });
      }
      return;
    }
    MM.dom.setText(els.cardTitle, card.title);
    MM.dom.setText(els.cardLine, card.line);
    if (key === cardKey) return;
    var refocus = els.card.contains(document.activeElement);
    cardKey = key;
    asks = !card.upnext;
    closeMenu();
    // Over the controls, whatever their height; what asks is in the middle.
    if (card.upnext) els.bottom.appendChild(els.card);
    else root.insertBefore(els.card, els.status);
    els.card.className = 'player-card ' + (card.upnext ? 'player-upnext' : 'player-message');
    els.card.setAttribute('role', card.upnext ? 'group' : 'alertdialog');
    els.card.setAttribute('aria-labelledby', 'tv-card-title');
    els.card.setAttribute('aria-describedby', 'tv-card-line');
    els.cardActions.textContent = '';
    card.acts.forEach(function (a, i) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'player-pill' + (i ? '' : ' player-pill--primary');
      if (!i && !card.upnext) btn.setAttribute('data-modal-focus', '');
      btn.textContent = a[0];
      btn.addEventListener('click', a[1]);
      els.cardActions.appendChild(btn);
    });
    enter(els.card);
    if (asks) enter(els.dim);
    else leave(els.dim);
    reach();
    // A card that waits for nobody does not take the focus: a press meant to pause would start it.
    if (!card.upnext || refocus) els.cardActions.firstElementChild.focus({ preventScroll: true });
    else els.status.textContent = card.title + ': ' + card.line;
  }

  /* What is under a card or a menu is out of reach; the way out only under a menu. */
  function reach() {
    var blocked = asks;
    root.classList.toggle('is-asking', blocked);
    els.mid.inert = els.bottom.inert = blocked || !!menuOpen;
    els.card.inert = !cardKey || !!menuOpen;
    els.top.inert = !!menuOpen;
    els.play.toggleAttribute('data-modal-focus', !blocked && !els.play.hidden && !els.play.inert);
    keepFocus();
  }

  /* ── menus ── */

  var MENU = {
    chapters: function (p) {
      var at = chapterAt(position());
      return { title: 'Chapters', rows: (p.chapters || []).map(function (c) {
        return [c.title, fmt(c.start_s), c === at, function () { seekTo(c.start_s); }];
      }) };
    },
    subtitles: function (p) {
      function row(id, label) {
        var on = (p.subtitle || 'off') === id;
        return [label, '', on, function () { if (!on) start({ kind: 'subtitles', subtitle: id }); }];
      }
      return { title: 'Subtitles', rows: [row('off', 'Off')].concat((p.subtitle_tracks || []).map(function (t) { return row(t.id, t.label); })),
        note: 'On a TV the subtitles are drawn into the picture. Changing them restarts the picture where you are; it takes a few seconds.' };
    },
    quality: function (p) {
      return { title: 'Quality', rows: QUALITIES.map(function (q) {
        var on = p.quality === q[0];
        return [q[1], q[2], on, function () { if (!on) start({ kind: 'quality', quality: q[0] }); }];
      }), note: 'TVs get up to 1080p, in a form every TV can play. 4K and HDR stay on this device. Changing quality restarts the picture where you are.' };
    },
  };

  function openMenu(kind, from) {
    var p = play();
    if (!p || !canSteer()) return;
    var menu = MENU[kind](p);
    els.poTitle.textContent = menu.title;
    els.poList.textContent = '';
    menu.rows.forEach(function (r) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'player-menu-item player-menu-item--split';
      btn.setAttribute('role', 'menuitemradio');
      btn.setAttribute('aria-checked', r[2] ? 'true' : 'false');
      var main = document.createElement('span');
      main.className = 'player-menu-main';
      main.textContent = r[0];
      var side = document.createElement('span');
      side.className = 'player-menu-side';
      side.textContent = r[1];
      btn.append(main, side, MM.dom.icon('i-check', 'player-menu-tick'));
      btn.addEventListener('click', function () { closeMenu(); r[3](); });
      els.poList.appendChild(btn);
    });
    els.poNote.textContent = menu.note || '';
    els.poNote.hidden = !menu.note;
    enter(els.po);
    enter(els.poVeil);
    from.setAttribute('aria-expanded', 'true');
    menuOpen = { kind: kind, from: from };
    follow();
    placeMenu();
    reach();
    (els.poList.querySelector('[aria-checked="true"]') || els.poList.firstElementChild).focus({ preventScroll: true });
  }

  /* Over its button; a sheet on a phone, where the stylesheet places it. */
  function placeMenu() {
    if (!menuOpen) return;
    var style = els.po.style;
    style.left = style.bottom = '';
    if (global.matchMedia('(max-width: 699.98px)').matches) return;
    var r = menuOpen.from.getBoundingClientRect();
    style.left = Math.max(12, Math.min(global.innerWidth - els.po.offsetWidth - 12, r.right - els.po.offsetWidth)) + 'px';
    style.bottom = Math.max(12, Math.min(global.innerHeight - r.top + 6, global.innerHeight - els.po.offsetHeight - 12)) + 'px';
  }
  global.addEventListener('resize', placeMenu);

  function closeMenu() {
    if (!menuOpen) return;
    var from = menuOpen.from;
    var held = els.po.contains(document.activeElement);
    menuOpen = null;
    follow();
    leave(els.po);
    leave(els.poVeil);
    from.setAttribute('aria-expanded', 'false');
    reach();
    if (held) from.focus({ preventScroll: true });
  }

  /* ── the seek bar ── */

  function timeAt(x) {
    var r = els.seek.getBoundingClientRect();
    return r.width ? Math.max(0, Math.min(1, (x - r.left) / r.width)) * duration() : 0;
  }
  function bubble(t) {
    var d = duration();
    var c = chapterAt(t);
    els.bubble.hidden = els.hover.hidden = false;
    els.hover.style.left = t / d * 100 + '%';
    els.bubbleTitle.textContent = c ? c.title : '';
    els.bubbleTitle.hidden = !c;
    els.bubbleTime.textContent = fmt(t);
    var w = els.seek.getBoundingClientRect().width;
    var b = els.bubble.offsetWidth;
    els.bubble.style.transform = 'translateX(' + Math.round(Math.max(-40, Math.min(w - b + 40, t / d * w - b / 2))) + 'px)';
  }
  function endDrag(commit) {
    if (!drag) return;
    var t = drag.t;
    drag = null;
    els.seek.classList.remove('is-dragging');
    els.bubble.hidden = els.hover.hidden = true;
    if (commit) seekTo(t);
    else paintTime();
  }
  els.seek.addEventListener('pointerdown', function (e) {
    if (e.button || !canSteer() || !duration()) return;
    dropKeySeek();
    drag = { t: timeAt(e.clientX) };
    els.seek.setPointerCapture(e.pointerId);
    els.seek.classList.add('is-dragging');
    bubble(drag.t);
    paintTime();
  });
  els.seek.addEventListener('pointermove', function (e) {
    if (!duration() || !canSteer() && !drag) return;
    var t = timeAt(e.clientX);
    if (drag) { drag.t = t; paintTime(); } else if (e.pointerType !== 'mouse') return;
    bubble(t);
  });
  els.seek.addEventListener('pointerup', function () { endDrag(true); });
  els.seek.addEventListener('pointercancel', function () { endDrag(false); });
  els.seek.addEventListener('pointerleave', function () { if (!drag) els.bubble.hidden = els.hover.hidden = true; });

  var dragVolume = false;
  els.volume.addEventListener('input', function () {
    dragVolume = true;
    setVolume(Number(els.volume.value) / 100);
  });
  els.volume.addEventListener('change', function () {
    dragVolume = false;
    setVolume(Number(els.volume.value) / 100);
  });

  /* ── presses and keys ── */

  root.addEventListener('click', function (e) {
    function hit(sel) { return e.target.closest(sel); }
    var pill = hit('[data-menu]');
    if (hit('[data-close]')) close();
    else if (hit('[data-po-close]') || hit('[data-po-veil]')) closeMenu();
    else if (hit('[data-play]')) toggle();
    else if (hit('[data-back10]')) skip(-SKIP_S);
    else if (hit('[data-fwd10]')) skip(SKIP_S);
    else if (hit('[data-mute]')) mute();
    else if (pill) openMenu(pill.getAttribute('data-menu'), pill);
    else if (hit('[data-next]')) start({ kind: 'next' });
    else if (hit('[data-again]')) start({ kind: 'again' });
    else if (hit('[data-bring]')) bring();
    else if (hit('[data-stop]')) stop();
  });

  root.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (menuOpen) {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        closeMenu();
        return;
      }
      var rows = MM.dom.qa('[role="menuitemradio"]', els.poList);
      var i = rows.indexOf(document.activeElement);
      var to = { ArrowDown: (i + 1) % rows.length, ArrowUp: (i - 1 + rows.length) % rows.length, Home: 0, End: rows.length - 1 }[e.key];
      if (to !== undefined) {
        e.preventDefault();
        e.stopPropagation();
        rows[to].focus();
      }
      return;
    }
    if (e.key === 'Escape') {
      if (!drag) return;
      e.preventDefault();
      e.stopPropagation();
      endDrag(false);
      return;
    }
    if (e.key === 'Tab') return;
    e.stopPropagation(); // the page's own keys are not the remote's
    if (e.target === els.volume || els.card.contains(e.target)) return;
    if (e.key === ' ' && !(e.target.closest && e.target.closest('button'))) {
      e.preventDefault();
      toggle();
      return;
    }
    var step = { ArrowLeft: -SKIP_S, ArrowRight: SKIP_S }[e.key];
    if (e.target === els.seek) {
      if (step === undefined) step = { ArrowDown: -SKIP_S, ArrowUp: SKIP_S, PageDown: -60, PageUp: 60, Home: -Infinity, End: Infinity }[e.key];
    }
    if (step === undefined) return;
    e.preventDefault();
    skip(step);
  });

  mini.addEventListener('click', function (e) {
    function hit(sel) { return e.target.closest(sel); }
    if (hit('[data-mini-play]')) toggle();
    else if (hit('[data-mini-back]')) skip(-SKIP_S);
    else if (hit('[data-mini-stop]')) stop();
    else if (hit('[data-dismiss]')) dismiss();
    else if (hit('[data-other]')) nextTv();
    else if (hit('[data-open]') && tv) open(tv);
  });
  // Over a title's page, which takes the focus back from what is no part of it.
  mini.addEventListener('focusin', function (e) { e.stopPropagation(); });
  /* The other TVs with something playing, from the one after this round to it. */
  function elsewhere() {
    var at = others.map(function (t) { return t.id; }).indexOf(tv && tv.id);
    return others.slice(at + 1).concat(others.slice(0, Math.max(0, at)));
  }
  function nextTv() {
    var next = elsewhere()[0];
    if (!next) return;
    tv = null;
    heardAt = 0;
    wants = {};
    line = [];
    hear(next, now());
    render();
    tick();
  }

  /* ── Back ── */

  /* The remote, and a menu in it, are each an entry of the history, so that Back closes them. Each is
     a copy of the entry the remote was opened on, whose mark (base) it carries: a step between those
     changes nothing of the page, which does not hear of it. */
  function mine() { return (global.history.state || {}).mmTv || null; }
  function level() { var t = mine(); return t ? t.level : 0; }
  var at = level();
  var base = mine() && mine().base;
  var moving = null; // a step of this page's own, not made yet
  var after = []; // what waits for it

  function write(how, tvMark) {
    global.history[how](Object.assign({}, global.history.state, { mmTv: tvMark }), '');
  }

  /* Brings the history into line with what is open. */
  function follow() {
    if (moving) return;
    var to = isOpen ? (menuOpen ? 2 : 1) : 0;
    if (level() === to) return;
    if (level() > to) {
      moving = setTimeout(arrived, STEP_MS); // a step the browser never makes
      global.history.go(to - level());
      return;
    }
    if (!level()) {
      base = Math.random().toString(36).slice(2, 10);
      write('replaceState', { level: 0, base: base });
    }
    while (level() < to) write('pushState', { level: level() + 1, base: base, id: tv.id });
    at = level();
  }

  function arrived() {
    clearTimeout(moving);
    moving = null;
    at = level();
    follow();
    if (!moving) after.splice(0).forEach(function (fn) { fn(); });
  }
  function settled(fn) {
    if (moving) after.push(fn);
    else fn();
  }

  global.addEventListener('popstate', function (e) {
    var was = at;
    var t = mine();
    at = level();
    if (t && t.base === base && (was || at)) e.stopImmediatePropagation();
    base = t && t.base;
    if (moving) { arrived(); return; }
    if (at < 2) closeMenu();
    if (!at) close();
    else if (!isOpen) reopen();
    else follow();
  });

  /* Forward leads to the remote: open where its TV still plays, else no longer there. */
  function reopen() {
    var id = mine().id;
    getOne(id).then(function (heard) {
      if (!level() || isOpen) return;
      if (heard.play) open(heard);
    }, function () {}).then(function () {
      follow();
      if (!isOpen) tick();
    });
  }

  /* ── opening and closing ── */

  var exit = null;
  function open(which) {
    var id = typeof which === 'object' ? which.id : which;
    if (!tv || tv.id !== id) {
      wants = {};
      line = [];
      gone = null;
      heardAt = 0;
      tv = null;
    }
    if (typeof which === 'object' && which !== tv) {
      hear(which, now());
    } else if (!tv) {
      getOne(id).then(open, refuseOpen);
      return;
    }
    if (!play() && !gone) { say('Nothing is playing on ' + name() + ' now.'); return; }
    ended = false;
    if (!isOpen) {
      isOpen = true;
      follow();
      if (exit) clearTimeout(exit);
      exit = null;
      root.classList.remove('is-leaving');
      root.inert = false;
      root.hidden = false;
      root.scrollTop = 0;
      letGo = MM.dom.holdPage();
      document.documentElement.classList.add('has-tv-remote');
      global.ModalA11y.onOpened('tv-remote'); // before a card takes the focus, so that it goes back where it was
    }
    render();
    if (play()) tick();
  }
  function refuseOpen(err) { global.UIFeedback.error(MM.api.messageOf(err, 'That TV can’t be reached now.')); }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    closeMenu();
    follow();
    endDrag(false);
    cardKey = null;
    asks = false;
    leave(els.card);
    leave(els.dim);
    reach();
    document.documentElement.classList.remove('has-tv-remote');
    letGo();
    render();
    root.classList.add('is-leaving');
    global.ModalA11y.onClosed('tv-remote');
    root.inert = true;
    // A button clicked in Safari takes no focus, so there was none to give back.
    if ((document.activeElement === document.body || root.contains(document.activeElement)) && !mini.hidden && !miniLeaving) m('[data-open]').focus({ preventScroll: true });
    exit = setTimeout(function () { exit = null; root.hidden = true; }, EXIT_MS);
    schedule();
  }
  global.ModalA11y.register('tv-remote', close);

  document.addEventListener('visibilitychange', function () {
    if (isHidden()) schedule();
    else tick();
  });
  global.addEventListener('pageshow', function (e) { if (e.persisted) tick(); });
  global.addEventListener('online', function () { if (play()) tick(); });

  function drop(id) {
    others = others.filter(function (t) { return String(t.id) !== String(id); });
    if (!tv || String(tv.id) !== String(id)) return;
    tv = null;
    gone = null;
    wants = {};
    line = [];
    close();
    render();
    tick();
  }

  MM.tvRemote = { open: open, drop: drop };
  // What else a page loaded on the remote's entry stands on opens first: it goes back to that.
  follow();
  tick();
})(window);
