/* What plays on the person's TV: the small bar on every page and the remote it opens
 * (_tv_remote.html; the server's side is routes/tv.py).
 *
 *   MM.tvRemote.open(tv)   opens the remote; tv is a TV as the API gives it, or its id.
 *   MM.tvRemote.plays(key) whether a TV plays that title, as last heard.
 *   MM.tvRemote.drop(id)   lets go, without a word, of a TV the person has just unpaired.
 *
 * Every page asks once what plays. While something does, the bar asks every few seconds and the
 * open remote every second; both far less while the page is hidden, and not at all when nothing
 * plays. Nothing here is the truth: the TV, another tab and another phone act too. What the person
 * has just set is shown until the TV has carried it out, then the TV's own word is again.
 *
 * A Chromecast that this browser plays on, and that no player follows, is one of the TVs here: it is
 * heard of and steered by watch/cast-sender.js, where the page has it, and not by the server.
 *
 * Audio & subtitles is the player's own chooser (watch/tracks.js): a pick is kept for the title
 * (PUT /api/watch/tracks/<key>), then the TV starts the picture again where it is, with it.
 *
 * Requires: core/api.js, core/dom.js, modal-a11y.js, ui-feedback.js, watch/tracks.js.
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
  var CALL_OFF_MS = 10000; // that a TV which was starting is waited for
  var STEP_MS = 1000;
  var EXIT_MS = 250;
  var LEAVE_MS = 150; // --dur-fast
  var SKIP_S = 10;
  var FAILS = 2; // polls in a row before it is said
  var GAP_APART_PX = 4;
  var DISMISSED = 'mm.tv.dismissed';
  var CAST = 'cast';

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
    unplayable: 'Plex can’t play this file.',
    unplayable_dolby_vision_5: 'Plex can’t convert its picture (Dolby Vision Profile 5).',
  };
  // What no try can mend: no Try again is offered for it.
  var FOR_GOOD = { unplayable: 1, unplayable_dolby_vision_5: 1 };
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
    unplayable: 'This file can’t play here. Plex can’t play this file.',
    unplayable_dolby_vision_5: 'This file can’t play here. Plex can’t convert its picture (Dolby Vision Profile 5).',
  };
  var STILL_S = 600; // that a question waits
  // Why a TV is no longer the person's, as a 410 says.
  var UNPAIRED = ['removed', 'left', 'account'];
  // A later press won: what the person sees is already that.
  var SILENT = ['superseded', 'replaced'];
  // What a TV going from one stream to the next answers for a moment: a press is sent once more.
  var PASSING = ['stale', 'not_playing'];
  var AGAIN_MS = 1500;
  var NOTE_MS = 6000; // that a press the TV refused is said on the card

  function $(sel) { return root.querySelector(sel); }
  function m(sel) { return mini.querySelector(sel); }
  var els = {
    title: $('[data-title]'), subtitle: $('[data-subtitle]'), on: $('[data-on]'), hint: $('[data-hint]'), say: $('.tv-rem__say'),
    ring: $('[data-ring]'), mid: $('[data-mid]'), bottom: $('[data-bottom]'), top: $('.player-top'),
    seekRow: $('[data-seek-row]'), seek: $('[data-seek]'), played: $('[data-played]'), thumb: $('[data-thumb]'),
    marks: $('[data-marks]'), hover: $('[data-seek-hover]'), bubble: $('[data-bubble]'),
    bubbleTitle: $('[data-bubble-title]'), bubbleTime: $('[data-bubble-time]'),
    elapsed: $('[data-elapsed]'), remaining: $('[data-remaining]'),
    play: $('[data-play]'), mute: $('[data-mute]'), volume: $('[data-volume]'), noVolume: $('[data-no-volume]'),
    chapter: $('[data-chapter]'), quality: $('[data-quality]'), next: $('[data-next]'), again: $('[data-again]'),
    bring: $('[data-bring]'), playOn: $('[data-play-on]'), stop: $('[data-stop]'), dim: $('[data-dim]'), card: $('[data-card]'),
    cardTitle: $('[data-card-title]'), cardLine: $('[data-card-line]'), cardActions: $('[data-card-actions]'),
    status: $('[data-status]'), po: $('[data-po]'), poVeil: $('[data-po-veil]'), poTitle: $('[data-po-title]'),
    poList: $('[data-po-list]'), poNote: $('[data-po-note]'), skip: $('[data-skip]'), skipText: $('[data-skip-text]'),
    sheet: $('[data-sheet]'), glass: $('[data-glass]'), veil: $('[data-veil]'), hero: $('[data-hero]'), art: $('[data-morph-art]'),
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
  var note = null; // a press the TV refused, as said on the card: {text, until}
  var ended = false; // signed out, or not let in: nothing more is asked
  var me = mini.getAttribute('data-me') || '';
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
  var sender = null; // of the Chromecast
  function kind() { var t = tv || (gone && gone.tv); return t ? t.kind : ''; }
  function isCast() { return kind() === CAST; }
  // A Chromecast is steered without mediaman.
  function isOff() { return offline && !isCast(); }
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
  /* Over the remote a toast stands under its title (_tv_remote.css): one said as it closes is
     shown once it has gone, where it stays. */
  var afterClose = [];
  function sayClosing(text) {
    if (!isOpen) { sayGone(text); return; }
    afterClose.push(text);
    close();
  }
  function sayGone(text) {
    if (exit) afterClose.push(text);
    else say(text);
  }
  function sayHeld() { afterClose.splice(0).forEach(say); }

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
      if (p.replaced && before && before.play_id !== p.play_id) say(tookOver(p) + ' on ' + name() + '. ' + keptIn(p.replaced));
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
    if (mine || handing) return;
    if (reason === 'restarted' && !isOpen) {
      say('mediaman restarted. ' + keptIn(last));
      return;
    }
    // Someone else's stop is said even with only the bar shown; one's own only where the remote is open.
    var what = was.last || {};
    var stopper = reason === 'stopped' && last && last.stopped_by !== me && last.stopped_by;
    var theirs = what.mine === false;
    if (reason === 'stopped' && (isOpen || stopper)) {
      if (!stopper) sayClosing(place_kept('Stopped.', last, theirs ? what.by : null));
      else if (theirs) sayClosing(stopper + ' stopped ' + (what.title || 'it') + '. ' + (what.by && what.by !== stopper ? what.by + '’s' : 'Their') + ' place is kept.');
      else sayClosing(place_kept(stopper + ' stopped ' + (what.title || 'it') + ' on ' + name() + '.', last));
    } else if (reason && reason !== 'stopped' && (isOpen || reason !== 'moved' && !(seen && seen.play_id === dismissed))) {
      gone = was;
    } else if (isOpen) {
      sayClosing('Nothing is playing on ' + name() + ' now.');
    }
  }

  function keptIn(what) { return 'Your place in ' + what.title + ' is kept.'; }
  function tookOver(p) { return (p.started_by || p.by) + ' started ' + p.title; }
  function unpaired(d) {
    var at = d.tv || name();
    var first = d.reason === 'removed' ? (d.by ? d.by + ' unpaired you from ' : 'You were unpaired from ') + at + '.'
      : d.reason === 'left' ? 'You left ' + at + '.' : 'You’re no longer paired with ' + at + '.';
    return d.stopped ? first + ' ' + d.stopped + ' stopped; your place is saved.' : first;
  }
  // `whose` names the person a film of someone else's was started by.
  function place_kept(before, last, whose) {
    var place = whose ? ' ' + whose + '’s place' : ' Your place';
    if (last && last.saved === false) return before + place + ' couldn’t be saved.';
    return last && last.position_s ? before + place + ' is saved at ' + fmt(last.position_s) + '.' : before;
  }

  /* The TV is no longer the person's: 410 says why while it is fresh, 404 says nothing. */
  function lostTv(err) {
    var before = play();
    if (tv) tv.play = null;
    wants = {};
    endDrag(false);
    closeMenu();
    if (!before && !isOpen) { tv = null; return; }
    var d = err.data || {};
    if (err.status === 410 && UNPAIRED.indexOf(d.reason || err.error) !== -1) {
      var id = tv.id;
      var told = unpaired(Object.assign({ reason: err.error }, d));
      leaving = null;
      gone = null;
      tv = null;
      others = others.filter(function (t) { return t.id !== id; });
      sayClosing(told);
      return;
    }
    if (err.status === 410) over(err.error, err.data && err.data.last);
    else over('', null);
    if (!gone) tv = null;
  }

  function isGone(err) { return err.status === 404 && err.error === 'not_found' || err.status === 410 && err.error !== 'replaced'; }

  /* A page the server sends the person on to: api.js only tells of it (`stay`), so the remote
     goes first. The sign-in leads back here. */
  var SENT_TO = ['/login', '/force-password-change'];
  function holdsEdits() { return !!(MM.api.hasUnsavedChanges && MM.api.hasUnsavedChanges()); }
  function away(to) {
    var was = isOpen;
    ended = true;
    clearTimeout(timer);
    // The person chose to keep the edits leaving would lose (api.js asked): the page stays.
    if (holdsEdits()) return new Promise(function () {});
    close();
    if (to === '/login') to = MM.api.loginUrl();
    settled(function () { setTimeout(function () { global.location.href = to; }, was ? SHRINK_MS : 0); });
    return new Promise(function () {});
  }
  function sentTo(err) { return notHere(err && err.data && SENT_TO.indexOf(err.data.redirect) !== -1 ? err.data.redirect : ''); }
  // On the very page it is sent to, going there would only load it again, to be sent on again.
  function notHere(to) { return to === global.location.pathname ? '' : to; }

  /* An answer cut off in its body comes as an empty one. Where the page holds edits, api.js
     asks before leaving, as for any read. */
  function get(path, key) {
    var stopper = new AbortController();
    var late = setTimeout(function () { stopper.abort(); }, ANSWER_MS);
    return MM.api.get(LIST + path, { signal: stopper.signal, stay: !holdsEdits() }).then(function (data) {
      clearTimeout(late);
      if (!data[key]) throw new Error('no answer');
      return data[key];
    }, function (err) {
      clearTimeout(late);
      var to = sentTo(err) || notHere(err && err.status === 401 ? '/login' : '');
      if (to) return away(to);
      throw err;
    });
  }
  function getOne(id) { return id === CAST ? getCast() : get('/' + encodeURIComponent(id), 'tv'); }

  /* ── the Chromecast ── */

  /* As the server would tell of it, or null. */
  function castTv() {
    listen();
    var c = sender && sender.now();
    if (!c) return null;
    return {
      id: CAST, kind: CAST, name: c.name || 'Chromecast', on: !c.lost, lost_s: c.lost ? 0 : null, acked: 0,
      volume: c.volume, muted: c.muted, can_volume: c.can_volume,
      play: {
        play_id: CAST + '-' + c.id, state: c.state, rating_key: c.rating_key, title: c.title,
        season: c.season, episode: c.episode, episode_title: c.episode_title,
        position_s: c.position_s, duration_s: c.duration_s, markers: c.markers, chapters: c.chapters,
        quality: c.quality, subtitle: c.subtitle_id, subtitle_tracks: c.subtitle_tracks,
        audio: c.audio_id, audio_tracks: c.audio_tracks, next: c.next,
        upnext_in_s: c.upnext_in_s, still_in_s: c.still_in_s,
        still_since_s: c.still_in_s == null ? null : STILL_S - c.still_in_s,
        // Its screen shows nothing of mediaman's: a count to a skip is the card's.
        auto_skip: false, skip_in_s: c.skip_in_s,
      },
    };
  }

  function getCast() {
    var heard = castTv();
    return heard ? Promise.resolve(heard) : Promise.reject(castOver());
  }

  /* As the server would refuse. */
  function castOver() {
    var was = sender && sender.over();
    var reason = was ? was.reason : '';
    // Only what was lost, or taken, is kept on the bar: of the rest there is nothing to choose.
    var last = { rating_key: was && was.rating_key, title: was && was.title, position_s: was && was.position_s, duration_s: was && was.duration_s };
    return { status: 410, error: reason === 'ended' ? '' : reason, data: { last: was && reason !== 'stopped' ? Object.assign({}, seen, last) : null } };
  }

  function withCast(all) { return all.concat(castTv() || []); }

  function hearCast() {
    var heard = castTv();
    others = withCast(others.filter(function (t) { return t.id !== CAST; }));
    if (heard && heard.on && heard.play.play_id === dismissed) dismissed = '';
    if (heard && tv && tv.id === CAST) {
      hear(heard, now());
    } else if (heard && !play() && !gone && !handing) {
      tv = null;
      heardAt = 0;
      hear(heard, now());
    } else if (!heard && tv && tv.id === CAST && play() && !leaving) {
      // Followed from another tab of this browser now.
      if (sender.holds(play().rating_key)) { drop(CAST); return; }
      lostTv(castOver());
      tick(); // another TV may play
    }
    render();
  }

  function steerCast(body) {
    if (body.kind === 'seek') sender.seek(body.position_s);
    else if (body.kind === 'volume') sender.setVolume(body.level);
    else if (body.kind === 'mute') sender.setMuted(body.muted);
    else sender[body.kind]();
  }

  function postCast(body) {
    if (body.kind === 'cancel') return Promise.resolve(sender.cancel());
    if (body.kind === 'stop') {
      var p = play();
      return sender.stop().then(function (at) {
        if (tv && tv.id === CAST) tv.play = null;
        return { tv: { last: Object.assign({}, p, at) } };
      });
    }
    var what = body.kind === 'quality' ? { quality: body.quality } : body.kind === 'subtitles' ? { subtitle: body.subtitle } : { next: true };
    if (body.audio) what.audio = body.audio;
    return sender.start(what).then(function () {
      hearCast();
      return {};
    }, function (err) {
      throw Object.assign(err, { error: err.code });
    });
  }

  function listen() {
    if (sender || !MM.castSender) return;
    sender = MM.castSender;
    sender.on(hearCast);
  }
  listen();

  function askOne() {
    var id = tv.id;
    var at = now();
    return getOne(id).then(function (heard) {
      if (tv && tv.id === id) hear(heard, at);
    }, function (err) {
      if (!isGone(err)) throw err;
      // A Chromecast is gone from the press of Stop on, while its place is being saved.
      if (tv && tv.id === id && !(id === CAST && leaving)) lostTv(err);
    });
  }

  /* Whether what is shown is a lost play the person has dismissed. */
  function putAway() { return !isOpen && isLost() && play().play_id === dismissed; }

  function askAll() {
    var at = now();
    return get('', 'tvs').then(function (all) {
      all = withCast(all);
      others = all.filter(function (t) { return t.play; });
      var mine = tv && all.filter(function (t) { return t.id === tv.id; })[0];
      // The list does not say why a play it no longer holds ended: the TV is asked.
      if (play() && !(mine && (mine.play || mine.last))) return askOne();
      if (play() || (mine && mine.play)) hear(mine, at);
      if (handing || gone || play() && !(putAway() && others.some(function (t) { return t.on && t.id !== tv.id; }))) return;
      tv = null;
      var next = others.filter(function (t) { return t.on; })[0] || others[0];
      heardAt = 0;
      if (next) hear(next, at);
    });
  }

  function tick() {
    clearTimeout(timer);
    timer = null;
    if (asking || ended || !(isOpen || hasBar)) return;
    asking = true;
    (isOpen && tv && !gone ? askOne().then(function () { return (gone || hasBar && !play()) && askAll(); }) : askAll()).then(function () {
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
    if (isCast() && !elsewhere().length) return;
    var every = isOpen && !gone ? OPEN_MS : BAR_MS;
    timer = setTimeout(tick, isHidden() ? HIDDEN_MS : Math.max(every, wait || 0));
  }

  /* ── commands ── */

  var line = []; // steering not sent yet, the newest of a kind
  var sending = false;

  function post(body) {
    var id = tv.id;
    var at = now();
    if (id === CAST) return postCast(body);
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

  function refusal(err) {
    return err.name === 'AbortError' ? 'mediaman didn’t answer. Try again.'
      : REFUSED[err.error] || MM.api.messageOf(err, 'That didn’t work. Try again.');
  }

  function refuse(err) {
    if (sentTo(err)) { away(sentTo(err)); return; }
    if (isGone(err)) { lostTv(err); render(); return; }
    if (SILENT.indexOf(err.error) === -1) global.UIFeedback.error(refusal(err));
    tick();
  }

  /* A press the TV refused: said on the open card, under what plays, else as any refusal. */
  function refuseSteer(err) {
    if (!isOpen || sentTo(err) || isGone(err) || SILENT.indexOf(err.error) !== -1) { refuse(err); return; }
    note = { text: refusal(err), until: now() + NOTE_MS };
    tick();
  }

  function later(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* Sent one at a time and in order: of what waits, only the newest of its kind. */
  function steer(kind, body, value) {
    var p = play();
    if (!p) return;
    if (isCast()) {
      delete wants[kind];
      steerCast(body);
      hearCast();
      return;
    }
    body.play_id = p.play_id;
    want(kind, value);
    var item = { kind: kind, body: body, want: wants[kind], tv: tv.id, title: p.rating_key };
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
    // A newer one of the kind is on its way: it speaks for both.
    function newer() { return line.some(function (x) { return x.kind === item.kind; }); }
    post(item.body).catch(function (err) {
      if (err.status !== 409 || PASSING.indexOf(err.error) === -1) throw err;
      return later(AGAIN_MS).then(askOne).then(function () {
        // Sent again only to the same title, steerable now: on another, the press meant nothing.
        var p = play();
        if (newer() || !p || p.rating_key !== item.title || !canSteer()) throw err;
        item.body.play_id = p.play_id;
        return post(item.body);
      }, function () { throw err; });
    }).then(function (data) {
      item.want.id = data.id;
      note = null;
    }, function (err) {
      if (wants[item.kind] === item.want) delete wants[item.kind];
      if (!newer()) refuseSteer(err);
    }).then(function () {
      render();
      setTimeout(function () { sending = false; pump(); }, GAP_MS);
    });
  }

  /* What starts a stream, and may take long: the steering goes on meanwhile. */
  function start(body) {
    var p = play();
    if (!p || busy() || coming) return;
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
    if (!p || leaving || coming) return;
    var was = { name: name(), ended: p.state === 'ended', play: p };
    leaving = was;
    line = [];
    render();
    function done(last) {
      leaving = null;
      if (then) { then(last || p); return; }
      sayClosing(was.ended ? 'Disconnected from ' + was.name + '.' : place_kept('Stopped.', last, p.mine === false ? p.by : null));
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

  /* Where it plays, or goes on from where it was: another TV, or here. */
  var handing = false; // a TV is asked for what plays on this one, which ends it here
  var coming = false; // it comes here once the TV that was starting is stopped
  function choose() {
    if (coming) return;
    var from = tv;
    var pressed = 0;
    function what() { return onward(about()) || { rating_key: about().rating_key, position_s: 0 }; }
    MM.playOn.open(els.playOn, {
      what: function () {
        var p = about();
        return Object.assign({ quality: p.quality || 'auto', autoplay: autoplays(), asked_s: pressed ? Math.max(0, now() - pressed) / 1000 : 0 },
          // With none showing the server picks the person's own: "off" would hide the next episodes' signs and songs.
          p.subtitle ? { subtitle: p.subtitle } : {},
          { rating_key: what().rating_key, position_s: what().position_s });
      },
      on: function () { return !play() || ['ended', 'failed'].indexOf(play().state) !== -1 ? null : isCast() ? CAST : 'tv-' + tv.id; },
      before: function () {
        pressed = now();
        var at = what().next ? 0 : position();
        handing = true;
        return at;
      },
      failed: function () {
        handing = false;
        if (isOpen && !play() && !gone) { sayClosing('Nothing is playing on ' + name() + ' now.'); tick(); }
      },
      started: function (t) {
        handing = false;
        open(t.id === CAST ? CAST : t);
        if (from && from.id === CAST && t.id !== CAST) sender.stop();
      },
      here: function () {
        coming = true;
        render();
        // Away from this page its stop would never be sent.
        Promise.race([MM.playOn.cancel(), new Promise(function (resolve) { setTimeout(resolve, CALL_OFF_MS); })]).then(function () {
          var given = handing && !play() && !gone; // the TV has given it up for the one that was starting
          coming = handing = false;
          render();
          if (given) watchHere(onward(about()) || about());
          else bring();
        });
      },
    });
  }
  function autoplays() {
    try { return global.localStorage.getItem('mediaman.watch.autoplay') !== 'false'; } catch (e) { return true; }
  }

  function busy() { return !!tv && !!starting[tv.id]; }
  function canSteer() {
    var p = play();
    return !!p && !gone && !isOff() && !coming && !leaving && !busy() && tv.on && ['playing', 'paused', 'buffering'].indexOf(p.state) !== -1;
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
  function isWaiting(p) { return coming || isLoading(p) || !!leaving || p.state === 'buffering' || (!!wanted('seek') && !keySeek && !drag); }
  function isLost() { return !!play() && !tv.on && tv.lost_s != null; }

  function render() {
    var p = play();
    showArt(about());
    renderMini(p);
    if (isOpen) renderRemote(p);
    paintTime();
    paintTitles();
  }

  /* A title's own Play on button says whether a TV plays it. */
  var ON = ['starting', 'loading', 'playing', 'paused', 'buffering'];
  function plays(key) {
    return others.filter(function (t) { return !tv || t.id !== tv.id; }).concat(tv || []).some(function (t) {
      return !!t.play && String(t.play.rating_key) === String(key) && ON.indexOf(t.play.state) !== -1;
    });
  }
  function paintTitles() {
    MM.dom.qa('[data-play-on-key]').forEach(function (b) {
      var on = plays(b.getAttribute('data-play-on-key'));
      if (b.classList.contains('is-on') === on) return;
      b.classList.toggle('is-on', on);
      ['aria-label', 'title'].forEach(function (a) { b.setAttribute(a, on ? 'Playing on a TV' : 'Play on a TV'); });
    });
  }

  var miniLeaving = null;
  /* The page says what of it shows, for what makes room for it (_tv_remote.css). */
  function showMini(on, leaving) {
    if (leaving && leans) unlean();
    setHidden(mini, !on);
    mini.classList.toggle('is-leaving', leaving);
    document.documentElement.classList.toggle('has-tv-mini', on);
    document.documentElement.classList.toggle('is-tv-mini-leaving', leaving);
  }
  function showRemote(on) {
    root.hidden = !on;
    document.documentElement.classList.toggle('has-tv-rem', on);
  }
  function renderMini(p) {
    if (!hasBar) return;
    var bad = !!gone || isLost();
    var key = gone ? (seen && seen.play_id) || '' : p ? p.play_id : '';
    var away = isOff() && !gone;
    var show = !!tv && (!!p || !!gone) && !(bad && key === dismissed);
    if (!show) {
      if (mini.hidden || miniLeaving) return;
      showMini(true, true);
      miniLeaving = setTimeout(function () { miniLeaving = null; showMini(false, false); }, EXIT_MS);
      return;
    }
    if (miniLeaving) clearTimeout(miniLeaving);
    miniLeaving = null;
    showMini(true, false);

    var what = about();
    var title = what.title + (episode(what) ? ' · ' + code(what) : '');
    var line1 = coming ? 'Bringing it here' : gone ? (gone.reason === 'lost' ? 'Lost ' : 'Stopped on ') + name() + '. Tap to see why.'
      : away ? 'Can’t reach mediaman' : isLost() ? 'Lost ' + name() + '. Tap to see why.'
        : p.state === 'failed' ? 'Can’t play on ' + name()
          : p.state === 'still' ? 'Still watching on ' + name() + '?'
            : p.state === 'ended' || p.state === 'upnext' ? 'Finished on ' + name()
              : isLoading(p) ? 'Starting on ' + name()
                : isPaused() && p.paused_by ? 'Paused by ' + p.paused_by
                  : p.mine === false ? p.by + '’s · on ' + name()
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
    setHidden(m('[data-mini-stop]'), bad || away || coming);
    setHidden(m('[data-dismiss]'), !bad || away);
    var more = m('[data-other]');
    var all = [tv].concat(elsewhere());
    var order = others.map(function (t) { return t.id; }).indexOf(tv.id);
    setHidden(more, all.length < 2 || coming);
    if (!more.hidden) {
      MM.dom.setText(more, (Math.max(0, order) + 1) + '/' + all.length);
      attr(more, 'aria-label', 'Show the next TV, ' + all[1].name);
    }
  }

  function hintFor(p) {
    if (coming) return 'Stopping the TV that was starting.';
    if (gone) return place_kept('', gone.last).trim() || 'Your place is kept.';
    if (isOff()) return 'The TV may still be playing.';
    if (isLost()) return place_kept('', { position_s: position() }).trim() || 'Your place is kept.';
    if (p.state === 'failed') return 'Your place is kept.';
    if (p.state === 'still') return 'Waiting for an answer';
    if (p.state === 'upnext' && p.upnext_in_s == null) return name() + ' waits for you to choose.';
    if (p.state === 'ended') return name() + ' is ready for something else.';
    if (isLoading(p)) return p.replaced ? tookOver(p) + '. ' + keptIn(p.replaced) : 'Starting. This can take a few seconds.';
    if (p.mine === false) return p.by + ' started this. You can pause, skip or stop it. It counts towards ' + p.by + '’s Continue watching.';
    if (isCast()) return 'You can leave this page. ' + (p.next ? 'Keep mediaman open for the next episode.' : 'The TV carries on.');
    return 'You can lock or close this device. The TV carries on.';
  }

  function renderRemote(p) {
    var what = about();
    if (!what || !p && !gone && !coming) return;
    var bad = !coming && (!!gone || isLost() || isOff());
    var isEnd = !bad && !coming && !!p && p.state === 'ended';
    var loading = coming || !bad && !!p && isLoading(p);
    MM.dom.setText(els.title, what.title);
    attr(els.title, 'title', what.title || '');
    MM.dom.setText(els.subtitle, episode(what) ? code(what, ' · ') + (what.episode_title ? ' · ' + what.episode_title : '') : '');
    setHidden(els.subtitle, !episode(what));
    root.classList.toggle('is-gone', bad);
    root.classList.toggle('is-load', loading);
    var upnext = !bad && !loading && !!p && p.state === 'upnext';
    // Other words of another height must not move the picture.
    if (upnext !== root.classList.contains('is-upnext')) els.say.style.minHeight = upnext ? els.say.getBoundingClientRect().height + 'px' : '';
    root.classList.toggle('is-upnext', upnext);
    root.classList.toggle('is-paused', !bad && !!p && (isPaused() || p.state === 'still'));
    setHidden(els.ring, !loading);
    MM.dom.setText(els.on, coming ? 'Bringing it here' : (loading ? 'Starting on ' : gone || isLost() ? 'Was playing on '
      : isEnd ? 'Finished on ' : isOff() ? 'Playing on '
        : p.state === 'failed' ? 'Couldn’t play on ' : p.state === 'upnext' ? 'Finished on '
          : isPaused() && p.paused_by ? 'Paused by ' + p.paused_by + ' on '
            : isPaused() || p.state === 'still' ? 'Paused on ' : 'Playing on ') + name());
    var refused = note && now() < note.until ? note.text : '';
    MM.dom.setText(els.hint, refused || hintFor(p));
    els.hint.classList.toggle('is-refused', !!refused);

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
    setPlayButton(els.play, coming || !!p && isWaiting(p), !p || isPaused() || p.state === 'still' || p.state === 'upnext');

    var chapters = (what.chapters || []).length > 1;
    if (!isEnd) {
      setHidden($('[data-menu="chapters"]'), !chapters);
      setHidden($('[data-menu="quality"]'), isCast() && !what.quality);
      setHidden(els.next, !what.next);
    }
    showTracks(isEnd ? null : what, steers);
    MM.dom.setText(els.quality, (QUALITIES.filter(function (q) { return q[0] === what.quality; })[0] || QUALITIES[0])[1]);
    if (p && p.next && p.next.season != null) {
      attr(els.next, 'aria-label', 'Next episode, ' + code(p.next) + (p.next.title ? ', ' + p.next.title : ''));
    } else attr(els.next, 'aria-label', 'Next episode' + (p && p.next && p.next.label ? ', ' + p.next.label : ''));

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
    // Another person's title goes on as theirs: it is not brought to this device.
    setHidden(els.bring, isEnd || !!p && p.mine === false);
    els.bring.disabled = els.stop.disabled = !!leaving || coming;
    setHidden(els.playOn, !MM.playOn);
    els.playOn.classList.toggle('is-on', !!p && !bad);
    // Not disabled, which would lose the focus the sheet gives back to it.
    [els.playOn, els.bring, els.stop].forEach(function (el) { el.classList.toggle('is-dead', coming); });
    attr(els.playOn, 'aria-disabled', String(coming));
    MM.dom.setText(els.stop, isEnd ? 'Disconnect' : 'Stop');
    drawMarks(what);
    showSkip(p);
    renderCard(p);
    reach();
  }

  /* ── skip intro and credits ── */

  /* The intro or credits on the TV's screen, where it does not skip them itself; as the
     player's own button, from a second before their end it is gone. */
  function markerNow(p) {
    if (!p || p.auto_skip !== false || !canSteer()) return null;
    var t = position();
    var m = (p.markers || []).filter(function (x) { return x.start_s <= t; }).pop();
    return m && t < m.end_s - 1 ? m : null;
  }
  /* Only the last credits lead on; a scene may follow the others. */
  function leadsOn(p, m) { return m.kind === 'credits' && m.final && !!p.next && p.next.season != null; }

  /* The Chromecast's count to a skip, with no player to show it. */
  function skipCount(p, m) { return m && p.skip_in_s != null && !leadsOn(p, m) ? p.skip_in_s : null; }

  var skipShown = '';
  function showSkip(p) {
    var m = markerNow(p);
    var n = skipCount(p, m);
    var label = !m ? '' : n != null ? 'Skipping ' + (m.kind === 'intro' ? 'intro' : 'credits') + ' in ' + n + ' · Cancel'
      : leadsOn(p, m) ? 'Next episode' : m.kind === 'intro' ? 'Skip intro' : 'Skip credits';
    els.skip.classList.toggle('is-counting', n != null);
    if (label === skipShown) return;
    skipShown = label;
    if (!m) { leave(els.skip); return; }
    MM.dom.setText(els.skipText, label);
    enter(els.skip);
  }
  function skipOn() {
    var p = play();
    var m = markerNow(p);
    if (!m) return;
    if (skipCount(p, m) != null) {
      sender.keepSkip();
      hearCast();
    } else if (leadsOn(p, m)) start({ kind: 'next' });
    else seekTo(m.end_s);
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
    showSkip(p);
  }
  setInterval(function () {
    if (!play() || isHidden()) return;
    if (isCast()) hearCast();
    else paintTime();
  }, TICK_MS);

  /* ── cards ── */

  function upnextTitle(p) {
    if (p.upnextAt == null) return 'Next episode';
    return 'Next episode in ' + Math.max(0, Math.ceil((p.upnextAt - now()) / 1000)) + ' s · Press to cancel';
  }
  function clock(ms) {
    var d = new Date(ms);
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }

  function cardFor(p) {
    if (coming) return null;
    var n = name();
    var last = gone && gone.last;
    var here = ['Bring it here', bring];
    if (gone) {
      var after = last && last.position_s ? ' at ' + fmt(last.position_s) : '';
      var card = {
        lost: [n + ' stopped answering', 'It was off, asleep or away from its browser for 2 minutes, so it stopped' + after + '.'],
        taken: ['Someone else is using ' + n, 'They paired it, and yours stopped' + after + '.'],
        unpaired: [n + ' was unpaired', 'It was unpaired in Settings, or in your Profile, or on the TV. To use it again, pair it.'],
        moved: ['You’re watching on another device', 'It goes on there, so ' + n + ' stopped' + after + '.'],
        still: [n + ' stopped by itself', 'Nobody answered, so it stopped' + after + '.'],
        signed_out: [n + ' stopped', 'You signed out everywhere, which ends what plays on a TV.'],
        restarted: ['mediaman restarted', keptIn(last || {})],
      }[gone.reason] || [n + ' stopped', 'Yours stopped' + after + '.'];
      if (isCast() && gone.reason === 'lost') card[1] = 'It was off or away from the network for 2 minutes, so it stopped' + after + '.';
      if (isCast() && gone.reason === 'taken') card = [n + ' plays something else', 'Yours stopped' + after + '.'];
      var mine = ['lost', 'still', 'restarted'].indexOf(gone.reason) !== -1;
      var on = onward(last);
      var acts = mine ? [[gone.reason === 'lost' ? (on && on.next ? 'Play next episode' : 'Try again') : 'Continue watching', playAgain], here, ['Close', dismiss]]
        : gone.reason === 'moved' ? [['Close', dismiss], here] : [here, ['Close', dismiss]];
      if (isCast()) acts = [here, ['Close', dismiss]];
      if (!on) acts = [['Close', dismiss]];
      return { key: 'gone' + gone.reason, title: card[0], line: card[1], acts: acts };
    }
    if (isOff()) {
      var off = global.navigator && global.navigator.onLine === false;
      return { key: 'offline', title: off ? 'This device is offline' : 'mediaman isn’t answering',
        line: 'You can’t control the TV until it’s back. Use the TV’s own remote meanwhile.', acts: [['Try again', tick]] };
    }
    if (!p || leaving) return null;
    if (isLost()) {
      return { key: 'lost', title: n + ' stopped answering',
        line: isCast() ? 'It may be off or away from the network. If it comes back within 2 minutes, it carries on by itself.'
          : 'It may be off, asleep or away from its browser. If it comes back within 2 minutes, it carries on by itself.',
        // Chrome looks for a Chromecast by itself.
        acts: (isCast() ? [] : [['Try again', tryTv]]).concat(onward(p) ? [here] : [], [['Stop', function () { stop(); }]]) };
    }
    // A press that was just made is being carried out.
    if (busy()) return null;
    if (p.state === 'failed') {
      return { key: 'failed' + p.error, title: 'This can’t play on ' + n,
        line: (FAILED[p.error] || FAILED.tv) + ' Your place is kept.',
        acts: (FOR_GOOD[p.error] ? [] : [['Try again', function () { start({ kind: 'again' }); }]])
          .concat([here, ['Stop', function () { stop(); }]]) };
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
        line: nx.season != null ? code(nx, ' · ') + (nx.title ? ' · ' + nx.title : '') : nx.label || p.title,
        // Cancel is the one to press: to watch on, the person need only wait.
        acts: [['Cancel', cancelNext], ['Play now', function () { start({ kind: 'next' }); }]] };
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
    // A card that waits for nobody does not take the focus: a press meant to pause would start it,
    // so it is said; one that asks is read out as it takes the focus, and the old word goes.
    els.status.textContent = card.upnext ? card.title + ': ' + card.line : '';
    if (!card.upnext || refocus) els.cardActions.firstElementChild.focus({ preventScroll: true });
  }

  /* What is under a card or a menu is out of reach; the way out only under a menu. */
  function reach() {
    var blocked = asks;
    root.classList.toggle('is-asking', blocked);
    var av = !!chooser && chooser.isOpen();
    els.mid.inert = blocked || !!menuOpen || av;
    els.bottom.inert = blocked || !!menuOpen;
    els.card.inert = !cardKey || !!menuOpen;
    els.top.inert = !!menuOpen || av;
    els.play.toggleAttribute('data-modal-focus', !blocked && !els.play.hidden && !els.play.inert);
    els.again.toggleAttribute('data-modal-focus', !blocked && !els.again.hidden);
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

  /* ── audio and subtitles ── */

  var avWrap = $('[data-av]');
  var chooser = MM.tracks ? MM.tracks.create({ wraps: [avWrap], opening: openTracks, closed: underTracks, pick: pickTracks }) : null;

  /* While it is open what is under it is out of reach, as under the remote's menus. */
  function underTracks(open) {
    els.mid.inert = els.top.inert = els.seekRow.inert = !!open;
    MM.dom.qa('.player-controls > *', root).forEach(function (el) { if (el !== avWrap) el.inert = !!open; });
    if (!open) render();
  }
  /* By title, whether a choice is kept for it, what a change that fails goes back to, what is
     kept for its subtitles as the server gave it, and the person's defaults:
     {remembered, subtitle, defaults}. */
  var keptFor = {};
  function openTracks() {
    closeMenu();
    underTracks(true);
    var p = play();
    var key = p && String(p.rating_key);
    if (!key || key in keptFor) return;
    MM.api.get(tracksUrl(key)).then(function (data) {
      if (heardKept(key, data)) showTracks(play(), canSteer());
    }, function () { /* not known: what was is kept as a choice */ });
  }

  function heardKept(key, data) {
    if (!data || typeof data.remembered !== 'boolean') return false;
    keptFor[key] = { remembered: data.remembered, subtitle: data.remembered_subtitle_choice || null, defaults: data.defaults || (keptFor[key] || {}).defaults || null };
    return true;
  }

  function trackState(p, change) {
    var kept = keptFor[String(p.rating_key)] || {};
    return Object.assign({
      audio: p.audio || null, subtitle: p.subtitle && p.subtitle !== 'off' ? p.subtitle : null,
      audio_tracks: p.audio_tracks || [], subtitle_tracks: p.subtitle_tracks || [],
      remembered: !!kept.remembered, defaults: kept.defaults || null,
      title: p.title, series: episode(p), on: name(),
    }, change || {});
  }

  /* What the TV plays, or played; a pick only while it may be steered. */
  function showTracks(what, steers) {
    if (!chooser) return;
    chooser.set(what ? trackState(what) : null);
    avWrap.inert = !steers;
    avWrap.classList.toggle('is-dead', !steers);
    if (!steers) chooser.close(false);
    else if (chooser.isOpen()) underTracks(true);
  }

  function tracksUrl(key) { return '/api/watch/tracks/' + encodeURIComponent(key); }

  /* Kept for the title, then the TV starts the picture again where it is, with what the server
     says now plays for the person. Only a change to tracks the TV cannot play goes back to what
     was; on any other failure the choice stays, for the next start. */
  function pickTracks(change) {
    var p = play();
    if (!p || !canSteer()) return;
    var id = tv.id;
    var key = String(p.rating_key);
    var what = change.reset ? 'reset' : change.audio !== undefined ? 'audio' : 'subtitle';
    var before = trackState(p);
    chooser.changing(true);
    var kept = what === 'reset' ? MM.api.delete(tracksUrl(key)) : MM.api.put(tracksUrl(key), what === 'audio' ? { audio: change.audio } : { subtitle: change.subtitle });
    kept.then(function (data) {
      var was = keptFor[key] && keptFor[key].remembered;
      var wasSubtitle = keptFor[key] && keptFor[key].subtitle;
      heardKept(key, data);
      var after = trackState(p, { audio: data.audio || null, subtitle: data.subtitle || null });
      if (after.audio === before.audio && after.subtitle === before.subtitle) {
        say(MM.tracks.said(what, after, false));
        return null;
      }
      var body = restartWith(p, what, after);
      starting[id] = true;
      render();
      return (isCast() ? postCast(body) : post(body)).then(function () {
        say(MM.tracks.said(what, after, false));
      }, function (err) {
        if (!err || err.error !== 'track_unavailable') { refuse(err || {}); return; }
        // The subtitles kept, given back as they were; not known, none is named, as Off would
        // hide every episode's signs and songs. A reset puts back the audio that played as well.
        var subtitles = wasSubtitle ? { subtitle_choice: wasSubtitle } : { subtitle: null };
        var back = was === false || (what === 'reset' && !was) ? null : what === 'audio' ? { audio: before.audio }
          : what === 'subtitle' ? subtitles : Object.assign({ audio: before.audio }, subtitles);
        (back ? MM.api.put(tracksUrl(key), back) : MM.api.delete(tracksUrl(key))).then(function (d) {
          heardKept(key, d);
        }, function () { /* said below */ });
        global.UIFeedback.error(MM.tracks.said(what, before, true));
      }).then(function () {
        delete starting[id];
        render();
        schedule();
      });
    }, function () {
      global.UIFeedback.error(MM.tracks.said(what, before, true));
    }).then(function () {
      chooser.changing(false);
    });
  }

  /* What starts the picture again with the tracks the server now picks for the person. "off" is
     sent only for Off picked: it is kept on to the next episodes, and hides their signs and songs.
     Otherwise the subtitle that is to play is named, or null clears the TV's own choice, so the
     server goes back to the person's. */
  function restartWith(p, what, after) {
    var body = { kind: 'subtitles', subtitle: after.subtitle || (what === 'subtitle' ? 'off' : null), play_id: p.play_id };
    if (isCast() && after.audio) body.audio = after.audio;
    return body;
  }

  /* Over its button; a sheet on a phone, where the stylesheet places it. */
  function placeMenu() {
    if (!menuOpen) return;
    var style = els.po.style;
    style.left = style.bottom = '';
    if (global.matchMedia('(max-width: 699.98px)').matches) return;
    var r = menuOpen.from.getBoundingClientRect();
    // Under its pill's right edge, or its left where that would run off the window.
    var left = r.right - els.po.offsetWidth < 12 ? r.left : r.right - els.po.offsetWidth;
    style.left = Math.max(12, Math.min(global.innerWidth - els.po.offsetWidth - 12, left)) + 'px';
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
    // The second press of a double click on the bar lands on the card as it opens: a press that
    // began before the card settled does nothing on it, nor beside it where the bar stood. A key
    // acts at once.
    if (e.detail && pressedAt >= openedAt && pressedAt < openedAt + SETTLE_MS && !hit('[data-close]')) {
      var b = e.target === els.veil && openBar;
      if (!b || e.clientX >= b.left && e.clientX <= b.right && e.clientY >= b.top && e.clientY <= b.bottom) return;
    }
    if (hit('[data-close]') || e.target === els.veil) close();
    else if (hit('[data-po-close]') || hit('[data-po-veil]')) closeMenu();
    else if (hit('[data-play]')) toggle();
    else if (hit('[data-back10]')) skip(-SKIP_S);
    else if (hit('[data-fwd10]')) skip(SKIP_S);
    else if (hit('[data-mute]')) mute();
    else if (pill) openMenu(pill.getAttribute('data-menu'), pill);
    else if (hit('[data-next]')) start({ kind: 'next' });
    else if (hit('[data-skip]')) skipOn();
    else if (hit('[data-again]')) start({ kind: 'again' });
    else if (hit('[data-play-on]')) choose();
    else if (hit('[data-bring]')) bring();
    else if (hit('[data-stop]')) stop();
  });

  root.addEventListener('pointerdown', function () { pressedAt = now(); }, true);

  root.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (chooser && chooser.key(e)) {
      e.stopPropagation();
      return;
    }
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

  /* ── the card growing out of the bar, and back into it ── */

  /* The card is laid out where it ends, and drawn from where it starts. Its glass is one surface that
     grows out of the bar's box into its own: a layer with nothing in it, so its place, size and
     corners are what move, and its edge, blur and shadow stay true all the way, where a stretch would
     squash them. The bar's picture, title and line travel to the card's by transform, the bar's title
     beside the card's as one fades into the other; the rest comes in row after row once the glass is
     most of the way. Each part moves over a stretch of the whole: opening (`span`), from how it looks
     at the bar (`shut`) to how it is drawn; closing (`back`), the other way and quicker, where what
     came in last goes first. A move turned round starts from where it is. */
  var GROW_MS = 560;
  var SHRINK_MS = 340;
  var FADE_MS = 150;
  var ROW_AT = 0.3; // of the opening, when the first row of the rest comes in: the glass is 85% there
  var ROW_STEP = 0.04;
  var ROW_FOR = 0.28;
  var HOME_AT = 0.75; // of the closing, when what travels is home: the bar comes back over the last of it
  var PULL_PX = 110; // a sheet pulled down this far closes as it is let go
  var FLICK_PX_MS = 0.5; // and so does one flicked down faster than this
  var SETTLE_MS = 600; // that a press on the card must begin after it opens to act
  var LEAN_MS = 440;
  var UNLEAN_MS = 260;
  var moves = [];
  var moveId = 0;
  var spring = null;

  function calm() { return global.matchMedia('(prefers-reduced-motion: reduce)').matches; }
  function box(el) {
    var r = el && el.getBoundingClientRect();
    return r && r.width && r.height ? r : null;
  }
  /* The bar where it can be seen, which the card grows out of. */
  function barBox() { return hasBar && !mini.hidden && !miniLeaving ? box(mini) : null; }
  function curve(name) { return global.getComputedStyle(root).getPropertyValue(name).trim(); }
  function springCurve() {
    if (spring === null) {
      var s = curve('--rem-spring');
      spring = s && global.CSS && global.CSS.supports && global.CSS.supports('transition-timing-function', s) ? s : curve('--ease-push') || 'ease-out';
    }
    return spring;
  }
  /* The transform that draws box *to* in the place of box *from*: stretched, or (`even`, a scale
     of words, as their sizes are) the same both ways, its line on the same middle. */
  function fit(from, to, even) {
    if (even) {
      var dy = from.top + from.height / 2 - to.top - to.height * even / 2;
      return 'translate(' + (from.left - to.left) + 'px, ' + dy + 'px) scale(' + even + ', ' + even + ')';
    }
    return 'translate(' + (from.left - to.left) + 'px, ' + (from.top - to.top) + 'px) scale(' + from.width / to.width + ', ' + from.height / to.height + ')';
  }
  /* How wide the words of *el* are drawn: as long as they run, up to its own width. */
  function shownWidth(el) {
    var range = document.createRange && document.createRange();
    if (!range) return box(el).width;
    range.selectNodeContents(el);
    var run = 0;
    Array.prototype.forEach.call(range.getClientRects(), function (r) { run = Math.max(run, r.right - box(el).left); });
    return Math.min(run || box(el).width, box(el).width);
  }
  /* How much larger the words of *to* are than those of *from*. */
  function wordsOf(from, to) {
    var a = parseFloat(global.getComputedStyle(from).fontSize);
    var b = parseFloat(global.getComputedStyle(to).fontSize);
    return a && b ? a / b : box(from).height / box(to).height;
  }
  function corners(el) { return global.getComputedStyle(el).borderRadius || ''; }

  function tracksFrom(bar, dropBy) {
    // With less motion it only fades, part by part: glass under a fading parent would blur nothing.
    if (calm()) {
      return [els.veil, els.glass, els.hero, els.mid, els.bottom].map(function (el) { return { el: el, k: 'opacity', shut: '0', open: '1', span: [0, 1] }; });
    }
    // Closing, the page clears as evenly as the card goes, not all in its first frames.
    var veil = { el: els.veil, k: 'opacity', shut: '0', open: '1', span: [0, 0.5], back: [0, 0.9], backEase: 'cubic-bezier(.45, 0, .55, 1)' };
    var sheet = box(els.sheet);
    if (dropBy !== undefined && sheet) {
      // Let go of, the sheet goes on down from where the finger left it, and the bar comes back.
      return [
        { el: els.sheet, k: 'transform', shut: 'translateY(' + Math.ceil(global.innerHeight - sheet.top + dropBy) + 'px)', open: 'translateY(' + dropBy + 'px)', span: [0, 1] },
        veil, { el: mini, k: 'opacity', shut: '1', open: '0', span: [0, 0.6] },
      ];
    }
    if (!bar || !sheet) {
      // The sheet rises; its parts fade each on its own, as glass under a fading parent blurs nothing.
      return [veil, { el: els.sheet, k: 'transform', shut: 'translateY(48px)', open: 'none', span: [0, 1], spring: true }]
        .concat([els.glass, els.hero, els.mid, els.bottom].map(function (el) { return { el: el, k: 'opacity', shut: '0', open: '1', span: [0, 0.5] }; }));
    }
    // The glass stands on the bar, as the bar, and takes its place before the bar goes. Closing, the
    // bar is back under it before it goes, once all is within a few pixels of home: the card's
    // picture and line are left on the bar's, which they are, until the card is hidden.
    var t = [veil];
    var onBar = { top: bar.top - sheet.top, left: bar.left - sheet.left, width: bar.width, height: bar.height };
    var whole = { top: 0, left: 0, width: sheet.width, height: sheet.height };
    Object.keys(onBar).forEach(function (k) {
      t.push({ el: els.glass, k: k, shut: onBar[k] + 'px', open: whole[k] + 'px', span: [0, 1], spring: true });
    });
    if (corners(mini) && corners(els.glass)) t.push({ el: els.glass, k: 'borderRadius', shut: corners(mini), open: corners(els.glass), span: [0, 1], spring: true });
    t.push({ el: els.glass, k: 'opacity', shut: '0', open: '1', span: [0, 0.1], back: [0.72, 0.92] });
    t.push({ el: mini, k: 'opacity', shut: '1', open: '0', span: [0.1, 0.2], back: [0.6, 0.8] });
    function travel(from, to, even) {
      var a = box(from);
      var b = box(to);
      if (a && b) t.push({ el: to, k: 'transform', shut: fit(a, b, even && wordsOf(from, to)), open: 'none', span: [0, 1], spring: true });
      return a && b;
    }
    var shared = [];
    var art = box(els.art);
    var pic = box(m('.tv-mini__pic'));
    if (travel(m('.tv-mini__pic'), els.art)) {
      shared.push(els.art);
      // Its corners stay the size they are drawn as it is stretched, from the bar's to its own.
      var r = parseFloat(corners(m('.tv-mini__pic')));
      if (r && corners(els.art)) {
        t.push({ el: els.art, k: 'borderRadius', shut: r * art.width / pic.width + 'px / ' + r * art.height / pic.height + 'px', open: corners(els.art), span: [0, 1], spring: true });
      }
      t.push({ el: $('.tv-rem__shade'), k: 'opacity', shut: '0', open: '1', span: [0.15, 0.55], back: [0, 0.4] });
      // What stands on the picture rides on it, as if drawn on it, and comes in as it lands.
      [$('.tv-rem__grab'), $('[data-close]'), els.playOn, els.subtitle].forEach(function (el) {
        var at = box(el);
        if (!at) return;
        t.push({ el: el, k: 'transform', shut: fit(pic, art), open: 'none', span: [0, 1], spring: true, origin: (art.left - at.left) + 'px ' + (art.top - at.top) + 'px' });
        t.push({ el: el, k: 'opacity', shut: '0', open: '1', span: [0.3, 0.6], back: [0, 0.08] });
        shared.push(el);
      });
    }
    // A title that would run past the bar's at the bar's size comes and goes on the card instead.
    var barTitle = m('[data-mini-title]');
    var words = box(barTitle);
    var fits = words && box(els.title) && shownWidth(els.title) * wordsOf(barTitle, els.title) <= words.width + 8;
    if (fits && travel(barTitle, els.title, true)) {
      shared.push(els.title);
      // The bar's words go along with the card's, so that one fades into the other in one place.
      t.push({ el: barTitle, k: 'transform', shut: 'none', open: fit(box(els.title), words, wordsOf(els.title, barTitle)), span: [0, 1], spring: true });
      t.push({ el: els.title, k: 'opacity', shut: '0', open: '1', span: [0, 0.12], back: [0.7, 0.85] });
    }
    var track = els.seek.querySelector('.player-seek-track');
    if (travel(m('.tv-mini__bar'), track)) {
      shared.push(track);
      t.push({ el: els.thumb, k: 'opacity', shut: '0', open: '1', span: [0.45, 0.75], back: [0, 0.08] });
    }
    // The rest is not stretched: it comes in on the glass row by row, rising a little, and goes at once.
    [[els.art, $('[data-close]'), els.playOn, els.subtitle, els.title], [els.mid], [els.elapsed, track, els.remaining],
      [$('.player-volume')], MM.dom.qa('.player-controls > .opt, .player-av-btn', root), [$('.tv-rem__end')],
      [$('[data-back10]'), els.play, $('[data-fwd10]')]].filter(function (row) {
      return row.some(function (el) { return el && !el.hidden && shared.indexOf(el) === -1; });
    }).forEach(function (row, i) {
      var span = [ROW_AT + i * ROW_STEP, ROW_AT + i * ROW_STEP + ROW_FOR];
      row.forEach(function (el) {
        if (!el || el.hidden || shared.indexOf(el) !== -1) return;
        // A control out of reach is drawn faint: it comes in to that, not to full and then back.
        t.push({ el: el, k: 'opacity', shut: '0', open: global.getComputedStyle(el).opacity || '1', span: span, back: [0, 0.08] });
        if (el !== track) t.push({ el: el, k: 'transform', shut: 'translateY(14px)', open: 'none', span: span, back: [0, 0.08], spring: true });
      });
    });
    return t;
  }

  function stopMoves() {
    moves.splice(0).forEach(function (a) {
      a.onfinish = null;
      a.cancel();
      if (a.effect && a.effect.target) a.effect.target.style.transformOrigin = '';
    });
  }
  /* Opens (*opening*) or closes the card, then calls *done*. */
  function move(opening, done, opts) {
    opts = opts || {};
    var id = ++moveId;
    // Without animations the card still takes the time a move would.
    if (typeof root.animate !== 'function') { setTimeout(function () { if (id === moveId) done(); }, opening ? 0 : SHRINK_MS); return; }
    // Where a move is turned round, each part starts from where it is now; what it moves to is
    // measured once the move is stopped, as the parts are laid out and not as they are drawn.
    var now = moves.map(function (a) { return { el: a.track.el, k: a.track.k, at: global.getComputedStyle(a.track.el)[a.track.k] }; });
    stopMoves();
    var tracks = tracksFrom(opts.bar, opts.dropBy);
    // The bar's title leaves its box as it goes with the card's; its line is drawn by the card's.
    mini.classList.toggle('is-morphing', !!opts.bar && opts.dropBy === undefined && !calm());
    var whole = calm() ? FADE_MS : opening ? GROW_MS : SHRINK_MS;
    var smooth = curve('--ease-out') || 'ease-out';
    var left = tracks.length;
    tracks.forEach(function (tr) {
      var span = opening ? tr.span : tr.back || (tr.spring ? [0, HOME_AT] : [1 - tr.span[1], 1 - tr.span[0]]);
      var a = {};
      var b = {};
      var was = now.filter(function (x) { return x.el === tr.el && x.k === tr.k; })[0];
      a[tr.k] = was && was.at ? was.at : opening ? tr.shut : tr.open;
      b[tr.k] = opening ? tr.open : tr.shut;
      if (tr.origin) tr.el.style.transformOrigin = tr.origin;
      var anim = tr.el.animate([a, b], {
        duration: whole * (span[1] - span[0]),
        delay: whole * span[0],
        easing: tr.spring ? (opening ? springCurve() : curve('--ease-push') || 'ease-out') : !opening && tr.backEase || smooth,
        fill: 'both',
      });
      anim.track = tr;
      anim.onfinish = function () { if (--left === 0) finish(); };
      moves.push(anim);
    });
    function finish() {
      mini.classList.remove('is-morphing');
      done();
      stopMoves();
    }
  }

  /* A sheet pulled down by a finger on its picture or title closes as it is let go far enough down,
     or flicked; else it springs back. */
  var pull = null;
  function pulled(by) {
    pull.by = by;
    // Pulled up, it gives a little, as a rubber band.
    var give = by < 0 ? -12 * (1 - 1 / (1 - by / 40)) : by;
    els.sheet.style.transform = 'translateY(' + Math.round(give) + 'px)';
    els.veil.style.opacity = String(Math.max(0, Math.min(1, 1 - by / (els.sheet.offsetHeight || 1))));
  }
  function letGoOf(e) {
    if (!pull || e.pointerId !== pull.id) return;
    var by = pull.by;
    var fast = by > 30 && by / Math.max(1, e.timeStamp - pull.t) > FLICK_PX_MS;
    pull = null;
    els.sheet.style.transform = '';
    els.veil.style.opacity = '';
    if (!isOpen) return;
    if (e.type === 'pointerup' && (by > PULL_PX || fast)) { close({ dropBy: Math.max(0, by) }); return; }
    if (by && els.sheet.animate) {
      els.sheet.animate([{ transform: 'translateY(' + Math.max(0, by) + 'px)' }, { transform: 'none' }], { duration: SHRINK_MS, easing: springCurve() });
    }
  }
  els.hero.addEventListener('pointerdown', function (e) {
    if (!isOpen || e.pointerType !== 'touch' || moves.length || (e.target.closest && e.target.closest('button'))) return;
    pull = { id: e.pointerId, y: e.clientY, t: e.timeStamp, by: 0 };
    if (els.hero.setPointerCapture) els.hero.setPointerCapture(e.pointerId);
  });
  els.hero.addEventListener('pointermove', function (e) { if (pull && e.pointerId === pull.id) pulled(e.clientY - pull.y); });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(function (type) { els.hero.addEventListener(type, letGoOf); });

  /* ── the bar leaning towards the card ── */

  /* Under the mouse the bar swells towards the card it opens into, as if pushing to open, then holds
     back; it goes back as the mouse goes, each from wherever it is. Its glass is stretched up and
     out from its foot, which rounds its corners on the way, and what is on it is drawn back to its
     own size (_tv_remote.css holds how far, and the curve). A finger, and less motion, get none
     of it. Opened, the bar is held as it was, so the card grows from where it is seen, and leans
     again as it closes under a mouse still on it. */
  var mouse = global.matchMedia('(hover: hover) and (pointer: fine)');
  var leans = [];
  var lastMouse = null;
  function leanParts() { return [mini].concat(MM.dom.qa('.tv-mini__pic, .tv-mini__t, .tv-mini > .player-btn', mini)); }
  function leanTo(on) {
    if (typeof mini.animate !== 'function' || isOpen || mini.style.transform) return;
    if (on ? !mouse.matches || calm() || mini.classList.contains('is-leaving') : !leans.length) return;
    var drawn = global.getComputedStyle(mini);
    var bend = drawn.getPropertyValue('--mini-lean-curve').trim();
    var to = { bar: drawn.getPropertyValue('--mini-lean').trim(), part: drawn.getPropertyValue('--mini-lean-part').trim() };
    var parts = leanParts();
    var from = parts.map(function (el) { return global.getComputedStyle(el).transform; });
    unlean();
    leans = parts.map(function (el, i) {
      return el.animate([{ transform: from[i] }, { transform: on ? (el === mini ? to.bar : to.part) : 'none' }], {
        duration: on ? LEAN_MS : UNLEAN_MS,
        easing: on && bend && global.CSS && global.CSS.supports && global.CSS.supports('transition-timing-function', bend) ? bend : 'ease-out',
        fill: 'forwards',
      });
    });
    var mine = leans;
    if (!on) mine[0].onfinish = function () { if (leans === mine) unlean(); };
  }
  function unlean() {
    leans.splice(0).forEach(function (a) { a.cancel(); });
  }
  function holdBar(on) {
    var parts = leanParts();
    if (on) {
      var at = global.getComputedStyle ? parts.map(function (el) { return global.getComputedStyle(el).transform; }) : [];
      unlean();
      parts.forEach(function (el, i) {
        if (!at[i] || at[i] === 'none') return;
        el.style.transition = 'none';
        el.style.transform = at[i];
      });
      return;
    }
    // It eases back from there, on its own transition, or leans again where the mouse still is.
    parts.forEach(function (el) {
      el.style.transition = '';
      el.style.transform = '';
    });
    var over = lastMouse && document.elementFromPoint && document.elementFromPoint(lastMouse.x, lastMouse.y);
    if (over && over.closest && over.closest('[data-open]') === m('[data-open]')) leanTo(true);
  }
  m('[data-open]').addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') leanTo(true); });
  m('[data-open]').addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') leanTo(false); });
  document.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'mouse') lastMouse = { x: e.clientX, y: e.clientY };
  }, { passive: true });

  /* ── opening and closing ── */

  var exit = null; // the card on its way out
  var openedAt = -Infinity;
  var openBar = null; // where the bar stood as the card opened
  var pressedAt = -Infinity; // when the last press on the remote began
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
    if (!play() && !gone) { sayGone('Nothing is playing on ' + name() + ' now.'); return; }
    ended = false;
    var opening = !isOpen;
    // Measured before this draws the bar: one that was not in sight has nothing to grow out of.
    var bar = opening && barBox();
    if (opening) {
      holdBar(true);
      isOpen = true;
      openedAt = now();
      openBar = bar || null;
      root.classList.add('is-growing');
      follow();
      exit = null;
      sayHeld();
      root.classList.remove('is-leaving');
      root.inert = false;
      showRemote(true);
      root.scrollTop = 0;
      letGo = MM.dom.holdPage();
      document.documentElement.classList.add('has-tv-remote');
      global.ModalA11y.onOpened('tv-remote'); // before a card takes the focus, so that it goes back where it was
    }
    render();
    if (opening) move(true, function () { root.classList.remove('is-growing'); }, { bar: bar });
    if (play()) tick();
  }
  function refuseOpen(err) { global.UIFeedback.error(MM.api.messageOf(err, 'That TV can’t be reached now.')); }

  /* opts.dropBy: how far down a finger has pulled the sheet. */
  function close(opts) {
    if (!isOpen) return;
    opts = opts && typeof opts === 'object' ? opts : {};
    if (chooser) chooser.close(false);
    isOpen = false;
    if (MM.playOn && MM.playOn.isOpen()) MM.playOn.close();
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
    var bar = barBox();
    root.classList.remove('is-growing');
    root.classList.add('is-leaving');
    global.ModalA11y.onClosed('tv-remote');
    root.inert = true;
    // A button clicked in Safari takes no focus, so there was none to give back.
    if ((document.activeElement === document.body || root.contains(document.activeElement)) && !mini.hidden && !miniLeaving) m('[data-open]').focus({ preventScroll: true });
    exit = true;
    move(false, function () { exit = null; showRemote(false); holdBar(false); sayHeld(); }, { bar: bar, dropBy: opts.dropBy });
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

  MM.tvRemote = { open: open, drop: drop, plays: plays };
  // What else a page loaded on the remote's entry stands on opens first: it goes back to that.
  follow();
  tick();
})(window);
