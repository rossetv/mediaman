/* Playing on a Chromecast or a TV with Google Cast: a way of Play on (watch/play-on.js), and what
 * keeps a title's place and goes on with it while the TV plays and no player follows it. The script
 * is on a page only where an admin allows Google's; Google's own is asked for when somebody opens
 * Play on, or while a title plays on a TV.
 *
 *   MM.castSender.now()        what plays where no player of this page follows it, or null
 *   MM.castSender.over()       how the last such play went by itself, or null
 *   MM.castSender.on(fn)       called as that may have changed, with what over() says as it goes
 *   MM.castSender.play(), .pause(), .toggle(), .seek(s), .setVolume(0..1), .setMuted(bool)
 *   MM.castSender.start({quality} | {subtitle} | {next: true})   another stream on the same TV
 *   MM.castSender.cancel()     the next episode does not begin by itself
 *   MM.castSender.stop()       a promise of {position_s, saved}
 *   MM.castSender.holds(key)   whether this browser plays the title on a TV
 *
 * The TV gets the address of one stream, with a ticket in it, and no session. That address is never
 * stored here: a page that comes later reads it from the TV. What is stored is bound to the account
 * that is signed in, and is not there for another.
 *
 * Requires: core/api.js. Uses: watch/play-on.js, where the page has it.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  var playOn = MM.playOn;

  var SDK = 'https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1';
  var SDK_WAIT_MS = 15000;
  var LOAD_WAIT_MS = 45000;
  var REJOIN_WAIT_MS = 10000;
  /* How long a TV that was joined may take to say what it plays. */
  var SAY_WAIT_MS = 3000;
  /* A place this far past the one a stream started at has been played to. */
  var MOVED_S = 3;
  /* A TV that was lost may come back for this long. */
  var LOST_MS = 120000;
  var TICK_MS = 500;
  var BEAT_MS = 5000;
  /* A page in the background hardly runs: it counts as there for this long after its last beat. */
  var ALIVE_MS = 90000;
  /* As long as a ticket plays. */
  var RECORD_MS = 6 * 60 * 60 * 1000;
  /* Pages look for a TV by themselves until it was not found so often. */
  var MISSES = 3;
  var SAVE_MS = 10000;
  var STOP_WAIT_MS = 10000;
  var START_WAIT_MS = 120000;
  var PAGE_WAIT_MS = 15000;
  /* How long Chrome may search before there is no TV. */
  var SEARCH_MS = 4000;
  var COUNTDOWN_MS = 10000;
  var COUNT_OLD_MS = 60000;
  /* "Are you still watching?": after this many episodes that began by themselves, or this long
     without a sign of anybody. */
  var ASK_AFTER_EPISODES = 3;
  var ASK_AFTER_MS = 2 * 60 * 60 * 1000;
  var STILL_MS = 10 * 60 * 1000;
  var AUTOPLAY_KEY = 'mediaman.watch.autoplay';
  var KEEP_ALIVE_MS = 60000;
  var VOLUME_MS = 150;
  /* Past this a TV that has not said it did what was pressed is taken at its word. */
  var ANSWER_MS = 10000;
  var RECORD = 'mediaman.watch.cast';
  var KEPT = 'mediaman.watch.cast.kept';
  var HLS = 'application/vnd.apple.mpegurl';

  var NOT_TAKEN = { title: 'The TV didn’t start', line: 'It didn’t take the picture. Choose it again, or watch here.' };
  var NO_SCRIPT = { title: 'Chromecast isn’t answering', line: 'Google’s script didn’t load. A blocker may stop it. A paired TV works without it.' };
  var NOT_REACHED = { title: 'The TV didn’t answer', line: 'Check that it is on and on the same network, then choose it again.' };

  var now = function () { return Date.now(); };
  var me = Array.prototype.map.call(global.crypto.getRandomValues(new Uint8Array(8)), function (b) {
    return (b < 16 ? '0' : '') + b.toString(16);
  }).join('');

  /* Whose page this is: it says nothing of the account. */
  var account = document.body.getAttribute('data-cast-account') || '';

  /* What Google's script answers, or `other` where it fails. */
  function safely(fn, other) {
    try { return fn(); } catch (err) { return other; }
  }

  function refusal(code, say) { return Object.assign(new Error(code), { code: code, say: say }); }

  /* ---- Google's script -------------------------------------------------- */

  var supported = !!(global.isSecureContext && global.chrome && global.navigator.presentation);
  var tag = null;
  var loading = null;
  var failed = false;
  var context = null; // once the script is there
  var searching = false;

  /* Asked for once more where it failed; where it is only slow it is waited for, however late. */
  function loadSdk() {
    if (context) return Promise.resolve();
    if (loading) return loading;
    var mine = loading = new Promise(function (resolve, reject) {
      if (!supported) { reject(refusal('failed', NO_SCRIPT)); return; }
      var timer = setTimeout(function () { over(false); }, SDK_WAIT_MS);
      function over(ok) {
        clearTimeout(timer);
        if (ok) resolve();
        else reject(refusal('failed', NO_SCRIPT));
      }
      heard = over;
      global.__onGCastApiAvailable = arrived;
      if (safely(function () { return global.chrome.cast.isAvailable && !!global.cast.framework; }, false)) arrived(true);
      else if (!tag) addTag();
    });
    mine.then(null, function () {
      loading = null;
      failed = true;
      changed();
    });
    return mine;
  }

  var heard = null;

  function addTag() {
    tag = document.createElement('script');
    tag.src = SDK;
    tag.referrerPolicy = 'no-referrer';
    tag.onerror = function () { arrived(false); };
    document.head.appendChild(tag);
  }

  function arrived(ok) {
    if (context) return;
    if (ok) ok = safely(function () { setUp(); return true; }, false);
    if (ok) {
      failed = false;
      searching = true;
      setTimeout(function () { searching = false; changed(); }, SEARCH_MS);
    } else {
      context = null;
      if (tag) tag.remove();
      tag = null;
    }
    heard(ok);
    changed();
  }

  function setUp() {
    var framework = global.cast.framework;
    context = framework.CastContext.getInstance();
    context.addEventListener(framework.CastContextEventType.CAST_STATE_CHANGED, changed);
    context.addEventListener(framework.CastContextEventType.SESSION_STATE_CHANGED, onSession);
    context.setOptions({
      receiverApplicationId: global.chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
      /* A page opened later joins what an earlier one of this browser began. */
      autoJoinPolicy: global.chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
    });
  }

  function available() {
    return !!context && safely(function () {
      return context.getCastState() !== global.cast.framework.CastState.NO_DEVICES_AVAILABLE;
    }, false);
  }

  /* It is not known yet whether there is a TV. */
  function looking() { return !failed && !available() && (!context || searching); }

  var watchers = [];
  function tell(over) { watchers.slice().forEach(function (fn) { fn(over || null); }); }

  function changed(over) {
    if (playOn) playOn.changed();
    tell(over && over.reason ? over : null);
  }

  /* ---- what plays, for the pages that come later -------------------------- */

  function readStored(name) {
    try {
      return JSON.parse(global.localStorage.getItem(name));
    } catch (err) {
      return null;
    }
  }

  function store(name, value) {
    try {
      if (value) global.localStorage.setItem(name, JSON.stringify(value));
      else global.localStorage.removeItem(name);
    } catch (err) { /* Without it a reload does not find the TV again. */ }
  }

  function readRecord() {
    var rec = readStored(RECORD);
    var mine = rec && rec.account === account && typeof rec.stream === 'string';
    return mine && now() - rec.at < RECORD_MS ? rec : null;
  }

  /* Another page that is open has it. */
  function livesElsewhere(rec) { return !!rec.owner && rec.owner !== me && now() - rec.beat < ALIVE_MS; }

  /* One whose player follows it, or that is just putting another stream on the TV, keeps it; one
     that only keeps the place makes way. */
  function ownedElsewhere(rec) { return livesElsewhere(rec) && (!rec.soft || rec.busy); }

  function note(p, left) {
    p.noted = now();
    if (!account) return;
    store(RECORD, {
      account: account,
      session: p.session.getSessionId(),
      stream: p.stream.stream_id,
      key: p.title.rating_key,
      place: p.at(),
      played: p.played,
      owner: left ? '' : me,
      soft: !p.followed,
      busy: p.changing,
      wait: p.wait,
      saved: p.savedAt,
      auto: p.auto,
      beat: left ? 0 : now(),
      at: now(),
    });
  }

  /* What the player keeps is large beside the place, and changes seldom. */
  function noteKept(p) {
    if (account) store(KEPT, { account: account, stream: p.stream.stream_id, title: p.title, keep: p.keep });
  }

  function readKept(id) {
    var kept = readStored(KEPT);
    return kept && kept.account === account && kept.stream === id ? kept : {};
  }

  function forgetStored() {
    store(RECORD, null);
    store(KEPT, null);
  }

  function forget(id) {
    var rec = readRecord();
    if (rec && rec.stream === id) forgetStored();
  }

  /* Somebody signed in over the person before: without the record an open tab of theirs lets go. */
  function forgetOthers() {
    var rec = readStored(RECORD);
    if (account && rec && rec.account && rec.account !== account) forgetStored();
  }
  forgetOthers();

  /* ---- a play ------------------------------------------------------------- */

  var current = null;

  /* A play on `session`; `media` is followed as the player follows its <video>. */
  function makePlay(session, title, followed) {
    var p = {
      session: session,
      title: title,
      stream: null,
      keep: null,
      followed: followed,
      done: false,
      ms: null,          // what the TV says of the stream, once it has taken it
      begun: false,      // the TV's place is the stream's, not one before it
      place: 0,
      duration: 0,
      paused: false,
      ended: false,
      error: null,
      ready: 0,
      aim: null,         // where a seek leads until the TV is there
      asked: null,       // what was pressed until the TV has done it: 'play' or 'pause'
      level: null,       // a volume on its way to the TV
      lost: null,        // when the TV was lost
      was: '',           // what the TV said last
      played: 0,
      seen: null,        // the place and time the played seconds were last counted at
      mine: false,       // this page began it, or the server has taken a save of it here
      savedAt: null,     // the place to save until the stream plays, where that is not its start
      saving: false,
      saved: null,       // when a save was last made here, and whether it was paused
      loads: 0,
      changes: 0,
      cut: false,        // the page before was left as it changed its stream
      changing: false,   // another stream is on its way
      wait: null,        // at its end: 'upnext' or 'still', and when that is over
      auto: 0,           // episodes that began by themselves
      sign: now(),       // when somebody last pressed
      gone: [],
      noted: 0,
      timers: {},
    };

    var media = p.media = new EventTarget();
    function emit() {
      var types = Array.prototype.slice.call(arguments);
      types.forEach(function (type) { media.dispatchEvent(new Event(type)); });
      if (!p.followed && !p.done && current === p && String(types) !== 'timeupdate') tell();
    }
    p.emit = emit;

    function read(name, get, set) { Object.defineProperty(media, name, { get: get, set: set, enumerable: true }); }
    read('currentTime', place, seek);
    read('duration', function () { return p.duration || NaN; });
    read('paused', function () { return p.paused; });
    read('ended', function () { return p.ended; });
    read('seeking', function () { return p.aim !== null; });
    read('readyState', function () { return p.ready; });
    read('error', function () { return p.error; });
    read('buffered', function () { return { length: 0 }; });
    read('videoWidth', function () { return 0; });
    read('videoHeight', function () { return 0; });
    read('canVolume', function () {
      return safely(function () {
        var receiver = p.session.getCastDevice();
        return !(receiver && receiver.volume && receiver.volume.controlType === 'fixed');
      }, false);
    });
    read('volume', function () {
      if (p.level !== null) return p.level;
      var level = safely(function () { return p.session.getVolume(); }, 1);
      return typeof level === 'number' ? level : 1;
    }, setVolume);
    read('muted', function () { return safely(function () { return !!p.session.isMute(); }, false); }, function (muted) {
      if (p.lost !== null) return;
      quietly(p.session.setMute(!!muted));
      emit('volumechange');
    });
    media.play = function () { return command('play'); };
    media.pause = function () { command('pause').catch(function () {}); };

    function place() {
      if (p.aim !== null) return p.aim;
      if (p.ms && p.begun && p.lost === null && !p.ended) {
        var at = safely(function () { return p.ms.getEstimatedTime(); }, p.place);
        p.place = Math.max(0, Math.min(at, p.duration || Infinity));
      }
      return p.place;
    }
    p.at = place;

    function seek(to) {
      if (!p.ms || p.lost !== null || p.done) return;
      var ms = p.ms;
      var aim = p.aim = Math.max(0, Math.min(Number(to) || 0, p.duration || Infinity));
      var request = new global.chrome.cast.media.SeekRequest();
      request.currentTime = aim;
      emit('seeking', 'timeupdate');
      var landed = within(function () {
        if (ms !== p.ms || p.aim !== aim || p.done) return;
        p.aim = null;
        emit('seeked', 'timeupdate');
      });
      ms.seek(request, landed, landed);
    }

    function command(what) {
      if (!p.ms || p.lost !== null || p.done) return Promise.reject(refusal('failed'));
      var ms = p.ms;
      p.asked = what;
      setPaused(what === 'pause');
      return new Promise(function (resolve, reject) {
        var over = within(function (err) {
          if (ms === p.ms && !p.done) {
            p.asked = null;
            update();
          }
          if (err) reject(refusal('failed'));
          else resolve();
        });
        ms[what](null, function () { over(); }, over);
      });
    }

    function setPaused(paused) {
      if (p.paused === paused) return;
      if (paused) place();
      p.paused = paused;
      emit(paused ? 'pause' : 'play');
    }

    function setVolume(level) {
      if (p.lost !== null) return;
      p.level = Math.max(0, Math.min(1, Number(level) || 0));
      emit('volumechange');
      if (p.timers.volume) return;
      p.timers.volume = setTimeout(function () {
        var to = p.level;
        p.timers.volume = null;
        quietly(p.session.setVolume(to).then(null, function () {}).then(function () {
          if (p.level === to) p.level = null;
        }));
      }, VOLUME_MS);
    }

    /* What the TV says now. */
    function update(alive) {
      var ms = p.ms;
      if (!ms || p.done) return;
      var states = global.chrome.cast.media.PlayerState;
      if (alive === false || ms.playerState === states.IDLE) { idle(ms, alive); return; }
      if (ms.media && ms.media.duration > 0) p.duration = ms.media.duration;
      var before = p.was;
      p.was = ms.playerState;
      if (ms.playerState === states.BUFFERING) {
        p.ready = 1;
        if (before !== p.was) emit('waiting');
        return;
      }
      var paused = ms.playerState === states.PAUSED;
      p.begun = true;
      if (!paused) p.savedAt = null;
      if (p.asked === null) setPaused(paused);
      if (p.ready !== 4) {
        p.ready = 4;
        emit('canplay');
      }
      if (!paused && before !== p.was) emit('playing');
    }

    function idle(ms, alive) {
      var reasons = global.chrome.cast.media.IdleReason;
      if (ms.idleReason === reasons.FINISHED) {
        detach();
        p.place = p.duration || p.place;
        p.aim = null;
        p.paused = p.ended = true;
        emit('timeupdate', 'pause', 'ended');
        if (!p.followed) atEnd(p);
      } else if (ms.idleReason === reasons.ERROR) {
        place();
        detach();
        p.error = { code: 2 };
        if (p.followed) emit('error');
        /* Where another stream is on its way this one may have ended for it. */
        else if (!p.changing) goneBy(p, 'stopped', true);
      } else if (ms.idleReason === reasons.INTERRUPTED) {
        goneBy(p, 'taken');
      } else if (ms.idleReason === reasons.CANCELLED) {
        goneBy(p, 'stopped');
      } else if (alive === false) {
        /* The session's own end, or the TV lost, is told of in a moment. */
        clearTimeout(p.timers.idle);
        p.timers.idle = setTimeout(function () {
          if (ms === p.ms && p.lost === null) goneBy(p, 'stopped');
        }, 1000);
      }
    }

    function attach(ms) {
      p.ms = ms;
      p.updated = function (alive) { if (ms === p.ms) update(alive); };
      ms.addUpdateListener(p.updated);
    }
    function detach() {
      if (p.ms) p.ms.removeUpdateListener(p.updated);
      p.ms = null;
    }
    p.attach = attach;
    p.detach = detach;
    p.update = update;

    function volumeChanged() { emit('volumechange'); }
    function otherMedia(e) {
      if (p.ms && !p.done && e.mediaSession && !isOurs(e.mediaSession, p.stream)) goneBy(p, 'taken');
    }
    p.join = function (joined) {
      var types = global.cast.framework.SessionEventType;
      p.leaveSession();
      p.session = joined;
      joined.addEventListener(types.VOLUME_CHANGED, volumeChanged);
      joined.addEventListener(types.MEDIA_SESSION, otherMedia);
    };
    p.leaveSession = function () {
      var types = global.cast.framework.SessionEventType;
      p.session.removeEventListener(types.VOLUME_CHANGED, volumeChanged);
      p.session.removeEventListener(types.MEDIA_SESSION, otherMedia);
    };
    p.join(session);

    p.timers.tick = setInterval(function () { tick(p); }, TICK_MS);

    p.face = {
      media: media,
      carriesOn: true,
      load: function (stream) { return load(p, stream); },
      stop: function () { return finish(p); },
      leave: function () {
        p.followed = false;
        p.mine = p.mine || p.own === true;
        keeps = true;
        if (!p.done) note(p);
        changed();
      },
      onGone: function (fn) { p.gone.push(fn); },
      keep: function (kept) {
        takeKept(p, kept);
        if (p.done || current !== p) return;
        note(p);
        noteKept(p);
      },
    };
    Object.defineProperty(p.face, 'name', { get: function () { return nameOf(p); }, enumerable: true });
    Object.defineProperty(p.face, 'lost', { get: function () { return p.lost !== null && !p.done; }, enumerable: true });
    return p;
  }

  /* `fn` once: when called, or after the longest wait for the TV. */
  function within(fn) {
    var timer = setTimeout(once, ANSWER_MS);
    var called = false;
    function once(value) {
      if (called) return;
      called = true;
      clearTimeout(timer);
      fn(value);
    }
    return once;
  }

  function nameOf(p) {
    return safely(function () { return p.session.getCastDevice().friendlyName; }, '') || '';
  }

  function quietly(promise) {
    if (promise && promise.catch) promise.catch(function () { /* The TV's own word follows. */ });
  }

  function isOurs(ms, stream) {
    var info = ms.media || {};
    /* A receiver that is not Google's own may not give back what it was sent beside the address. */
    return !!info.customData && info.customData.stream === stream.stream_id ||
      !!stream.url && info.contentId === stream.url;
  }

  function mediaOf(session, stream) {
    var all = session.getSessionObj().media || [];
    return all.filter(function (ms) { return isOurs(ms, stream); })[0] || null;
  }

  /* The player's count of the played time takes the place of the one made here. */
  function takeKept(p, kept) {
    p.keep = kept;
    if (kept && typeof kept.played_s === 'number') p.played = kept.played_s;
  }

  function takeStream(p, stream) {
    if (stream.title && String(stream.title.rating_key) !== String(p.title.rating_key)) {
      p.played = 0;
      p.keep = null;
    }
    p.stream = { stream_id: stream.stream_id, url: new URL(stream.url, global.location.href).href };
    if (stream.title) p.title = stream.title;
    if (stream.keep !== undefined) takeKept(p, stream.keep);
    p.place = stream.position_s || 0;
    p.savedAt = typeof stream.saved_s === 'number' ? stream.saved_s : null;
    p.duration = stream.duration_s || 0;
    p.paused = !!stream.paused;
    p.begun = p.ended = false;
    p.error = p.aim = p.asked = p.seen = null;
    p.was = '';
    p.ready = 0;
  }

  /* Put `stream` on the TV; settles when the TV has taken it. */
  function load(p, stream) {
    var mine = ++p.loads;
    var superseded = function () { return p.done || mine !== p.loads; };
    if (p.done) return Promise.reject(refusal('failed', NOT_TAKEN));
    p.detach();
    takeStream(p, stream);
    p.emit('waiting');
    note(p);
    noteKept(p);

    var cast = global.chrome.cast.media;
    var info = new cast.MediaInfo(p.stream.url, HLS);
    info.streamType = cast.StreamType.BUFFERED;
    if (p.duration) info.duration = p.duration;
    info.metadata = new cast.GenericMediaMetadata();
    info.metadata.title = p.title.title || '';
    info.metadata.subtitle = p.title.subtitle || '';
    info.customData = { stream: stream.stream_id };
    var request = new cast.LoadRequest(info);
    request.currentTime = p.place;
    request.autoplay = !p.paused;

    return new Promise(function (resolve, reject) {
      var timer = setTimeout(failed, LOAD_WAIT_MS);
      function failed() {
        clearTimeout(timer);
        reject(refusal(superseded() ? 'superseded' : 'failed', NOT_TAKEN));
      }
      p.session.loadMedia(request).then(function () {
        clearTimeout(timer);
        if (superseded()) { failed(); return; }
        var ms = mediaOf(p.session, p.stream);
        if (!ms) { failed(); return; }
        p.attach(ms);
        p.ready = 1;
        p.emit('loadedmetadata', 'durationchange');
        p.update();
        changed();
        resolve();
      }, failed);
    });
  }

  /* What is skipped is not played. */
  function count(p) {
    var t = now();
    if (!p.ms || !p.begun || p.lost !== null || p.aim !== null) { p.seen = null; return; }
    var at = p.at();
    if (p.seen) p.played += Math.max(0, Math.min(at - p.seen.at, (t - p.seen.t) / 1000));
    p.seen = { at: at, t: t };
  }

  function tick(p) {
    if (p.done) return;
    count(p);
    if (!p.paused && p.ms && p.begun && p.lost === null && p.aim === null) {
      p.emit('timeupdate');
    }
    if (now() - p.noted >= BEAT_MS) {
      var rec = readRecord();
      if (rec && rec.stream === p.stream.stream_id && !ownedElsewhere(rec)) note(p);
    }
    if (p.followed) return;
    if (p.wait && !p.changing && p.wait.until !== null && now() >= p.wait.until) waitOver(p);
    else keepPlace(p);
  }

  /* Over here: nothing more is said of it. */
  function drop(p, kept) {
    p.at();
    p.done = true;
    safely(p.detach);
    safely(p.leaveSession);
    Object.keys(p.timers).forEach(function (name) {
      clearTimeout(p.timers[name]);
      clearInterval(p.timers[name]);
    });
    if (!kept) forget(p.stream.stream_id);
    if (current === p) current = null;
  }

  /* It ended by itself, or by another's hand. */
  function goneBy(p, reason, end) {
    var followed = p.followed;
    var ms = p.ms;
    var atEnd = p.wait;
    var last = !followed && p.mine ? Object.assign({}, describe(p), { reason: reason }) : null;
    drop(p);
    if (last) ended = last;
    if (end) endOn(p.session, ms, p.lost);
    if (followed) p.gone.forEach(function (fn) { fn(reason); });
    else if (!atEnd) stopOnServer(p.stream.stream_id, body(p));
    changed(last);
  }

  /* Nothing of `ms` plays on the TV any more. The session is left to a start that waits on it. */
  function endOn(session, ms, lost) {
    if (lost !== null) return;
    if (pending) opened = session;
    safely(function () {
      if (!pending) session.endSession(true);
      else if (ms) ms.stop(null, function () {}, function () {});
    });
  }

  /* End it on the TV. */
  function finish(p) {
    if (p.done) return Promise.resolve();
    var ms = p.ms;
    drop(p);
    endOn(p.session, ms, p.lost);
    changed();
    return Promise.resolve();
  }

  /* ---- sessions ------------------------------------------------------------ */

  function onSession(e) {
    var states = global.cast.framework.SessionState;
    var p = current;
    changed();
    if (!p && e.sessionState === states.SESSION_RESUMED) keepHere(true);
    if (!p || !e.session || e.session.getSessionId() !== p.session.getSessionId()) return;
    if (e.sessionState === states.SESSION_ENDED) {
      if (e.session.getSessionObj().status === global.chrome.cast.SessionStatus.DISCONNECTED) lose(p);
      else goneBy(p, 'stopped');
    } else if (e.sessionState === states.SESSION_RESUMED && p.lost !== null) {
      find(p, e.session);
    }
  }

  function lose(p) {
    if (p.lost !== null) return;
    p.at();
    p.lost = now();
    p.aim = p.asked = null;
    p.was = '';
    p.ready = 1;
    p.emit('waiting');
    p.timers.lost = setTimeout(function () { goneBy(p, 'lost'); }, LOST_MS);
  }

  /* The TV is back. */
  function find(p, session) {
    clearTimeout(p.timers.lost);
    p.lost = null;
    p.detach();
    p.join(session);
    var ms = mediaOf(session, p.stream);
    if (!ms) {
      goneBy(p, session.getMediaSession() ? 'taken' : 'stopped');
      return;
    }
    p.attach(ms);
    p.update();
    changed();
  }

  global.addEventListener('online', function () {
    if (current && current.lost !== null) global.chrome.cast.requestSessionById(current.session.getSessionId());
  });

  /* The session a record names and the media there, once this page has joined it and the TV has
     said what it plays, or nothing; null where it is not joined in time. */
  function waitForTv(rec) {
    return new Promise(function (resolve) {
      var framework = global.cast.framework;
      var type = framework.CastContextEventType.SESSION_STATE_CHANGED;
      var heard = null;
      var quiet = null;
      var timer = setTimeout(function () { over(null); }, REJOIN_WAIT_MS);
      function over(found) {
        clearTimeout(timer);
        context.removeEventListener(type, look);
        clearTimeout(quiet);
        if (heard) heard.removeEventListener(framework.SessionEventType.MEDIA_SESSION, look);
        resolve(found);
      }
      function look() {
        var session = context.getCurrentSession();
        if (!session || session.getSessionId() !== rec.session) return;
        if ((session.getSessionObj().media || []).length || rec.wait) {
          over({ session: session, ms: mediaOf(session, { stream_id: rec.stream }) });
        } else if (heard !== session) {
          heard = session;
          clearTimeout(timer);
          session.addEventListener(framework.SessionEventType.MEDIA_SESSION, look);
          quiet = setTimeout(function () { over({ session: session, ms: null }); }, SAY_WAIT_MS);
        }
      }
      context.addEventListener(type, look);
      look();
    });
  }

  var rejoining = null;

  /* The play a record names, as the TV has it now; null where it is over. Rejects where the TV
     could not be found, which may be playing it still. */
  function rejoin(followed) {
    if (current) return Promise.resolve(current);
    if (rejoining) return rejoining;
    var rec = readRecord();
    if (!rec || ownedElsewhere(rec)) return Promise.resolve(null);
    var mine = rejoining = loadSdk().then(function () {
      return waitForTv(rec);
    }).then(function (found) {
      if (!found) throw refusal('lost');
      if (current) return current;
      var last = readRecord();
      if (!last) {
        /* The person plays it here meanwhile, or nobody is signed in who may know of it. */
        if (released === rec.stream && found.ms && !found.ms.idleReason) endOn(found.session, found.ms, null);
        return null;
      }
      /* Another page has gone on with it meanwhile. */
      if (last.stream !== rec.stream || ownedElsewhere(last)) return null;
      return takeUp(last, found.session, found.ms, followed);
    }).catch(function () {
      miss(rec.stream);
      throw refusal('lost');
    });
    var done = function () { rejoining = null; };
    mine.then(done, done);
    return mine;
  }

  function miss(id) {
    var rec = readRecord();
    if (rec && rec.stream === id) store(RECORD, Object.assign(rec, { misses: (rec.misses || 0) + 1 }));
  }

  function takeUp(rec, session, ms, followed) {
    var kept = readKept(rec.stream);
    var plays = !!ms && !ms.idleReason;
    var other = !plays && session.getMediaSession();
    var end = !!ms && ms.idleReason === global.chrome.cast.media.IdleReason.FINISHED && ms.media.duration;
    /* At the end of an episode it waits, or it ended while no page was open and something follows. */
    var waits = !plays && !other && kept.keep && kept.keep.next && (rec.wait || end && { kind: 'upnext', until: null });
    if (!plays && !waits) {
      /* The page that has it knows better. */
      if (livesElsewhere(rec)) return null;
      forget(rec.stream);
      /* A session that plays nothing is no TV to play on unasked. */
      if (!other) endOn(session, null, null);
      /* It ended while no page was open: where it was last, unless the TV says it played to the end. */
      stopOnServer(rec.stream, { position_s: end || tenths(typeof rec.saved === 'number' ? rec.saved : rec.place), played_s: Math.round(rec.played || 0) });
      return null;
    }
    var said = plays && ms.media.metadata || {};
    var p = makePlay(session, kept.title || { rating_key: rec.key, title: said.title || '', subtitle: said.subtitle || '' }, followed);
    p.stream = { stream_id: rec.stream };
    p.keep = kept.keep || null;
    p.played = Number(rec.played) || 0;
    p.auto = Number(rec.auto) || 0;
    if (typeof rec.saved === 'number') p.savedAt = rec.saved;
    p.cut = !!rec.busy;
    ended = null;
    current = p;
    if (waits) {
      p.place = p.duration = rec.wait ? rec.place : end;
      p.paused = p.ended = p.mine = true;
      /* A count that ran out with no page open begins nothing. */
      var stale = waits.kind === 'upnext' && now() - waits.until > COUNT_OLD_MS;
      p.wait = stale ? { kind: 'upnext', until: null } : waits;
      if (!rec.wait) p.endSaved = stopOnServer(rec.stream, body(p));
    } else {
      p.duration = ms.media.duration || 0;
      p.place = safely(function () { return ms.getEstimatedTime(); }, Number(rec.place) || 0);
      /* It has played there while no page was open. */
      if (p.place > (Number(rec.place) || 0) + MOVED_S) p.savedAt = null;
      p.paused = ms.playerState === global.chrome.cast.media.PlayerState.PAUSED;
      p.begun = true;
      p.attach(ms);
      p.update();
      if (p.done) return null;
    }
    note(p);
    changed();
    return p;
  }

  /* ---- keeping the place where no player does -------------------------------- */

  function tenths(s) { return Math.round((s || 0) * 10) / 10; }

  function body(p) {
    return {
      position_s: tenths(p.savedAt === null ? p.at() : p.savedAt),
      played_s: Math.round(p.played),
    };
  }

  function stopOnServer(id, at) {
    /* Where it is not told the server drops it when nothing asks for it. */
    return limited(STOP_WAIT_MS, function (signal) {
      return MM.api.post('/api/watch/stream/' + encodeURIComponent(id) + '/stop', at, { signal: signal });
    }).then(function () { return true; }, function () { return false; });
  }

  /* `ask` with a signal that ends it after `ms`. */
  function limited(ms, ask) {
    var abort = new AbortController();
    var timer = setTimeout(function () { abort.abort(); }, ms);
    var over = function () { clearTimeout(timer); };
    var asked = ask(abort.signal);
    asked.then(over, over);
    return asked;
  }

  function keepPlace(p) {
    if (p.saving || p.lost !== null || p.changing || !p.ms) return;
    var due = 0;
    if (p.saved && p.saved.paused === p.paused) due = p.paused ? KEEP_ALIVE_MS : SAVE_MS;
    if (!p.saved || now() - p.saved.t >= due) savePlace(p);
  }

  function savePlace(p) {
    var id = p.stream.stream_id;
    p.saving = true;
    p.saved = { t: now(), paused: p.paused };
    MM.api.post('/api/watch/progress', Object.assign({ stream_id: id }, body(p))).then(function () {
      p.saving = false;
      if (id === p.stream.stream_id) p.cut = false;
      if (p.done || p.mine) return;
      p.mine = true;
      changed();
    }, function (err) {
      p.saving = false;
      if (!err || err.status !== 410 || p.followed || p.done || p.changing || id !== p.stream.stream_id) return;
      /* A page that was left as it put another stream on the TV: it is opened again. Else the stream
         is over, or it is not this person's, and the TV cannot go on with it. */
      if (p.cut) reopen(p);
      else goneBy(p, 'stopped', true);
    });
  }

  /* ---- going on with it where no player does ----------------------------------- */

  /* What it has come to, as a page without a player shows it. */
  var ended = null;

  function describe(p) {
    var keep = p.keep || {};
    var given = keep.given || {};
    var line = p.title.subtitle || '';
    /* As the player names an episode; of a film the line is its year. */
    var ep = /^(?:S(\d+) · )?E(\d+)(?: — (.*))?$/.exec(line);
    var state = p.paused ? 'paused' : 'playing';
    if (p.wait) state = p.wait.kind;
    else if (p.changing || !p.ms || !p.begun) state = 'loading';
    else if (p.ready < 4 && p.lost === null) state = 'buffering';
    return {
      id: p.stream.stream_id,
      name: nameOf(p),
      rating_key: p.title.rating_key,
      title: p.title.title,
      subtitle: line,
      season: ep && ep[1] ? Number(ep[1]) : null,
      episode: ep ? Number(ep[2]) : null,
      episode_title: ep ? ep[3] || '' : null,
      position_s: p.at(),
      duration_s: p.duration,
      paused: p.paused,
      lost: p.lost !== null,
      state: state,
      volume: p.media.volume,
      muted: p.media.muted,
      can_volume: p.media.canVolume,
      markers: keep.markers || [],
      chapters: keep.chapters || [],
      quality: keep.quality || 'auto',
      subtitle_id: keep.subtitle || given.subtitle || null,
      subtitle_tracks: given.subtitle_tracks || [],
      next: keep.next || null,
      upnext_in_s: waits(p, 'upnext'),
      still_in_s: waits(p, 'still'),
    };
  }

  function waits(p, kind) {
    if (!p.wait || p.wait.kind !== kind || p.wait.until === null) return null;
    return Math.max(0, Math.ceil((p.wait.until - now()) / 1000));
  }

  function atEnd(p) {
    var autoplay = safely(function () { return global.localStorage.getItem(AUTOPLAY_KEY) !== 'false'; }, true);
    if (!p.keep || !p.keep.next) {
      goneBy(p, 'ended', true);
      return;
    }
    p.endSaved = stopOnServer(p.stream.stream_id, body(p));
    var ask = p.auto >= ASK_AFTER_EPISODES || now() - p.sign >= ASK_AFTER_MS;
    if (autoplay && ask) p.wait = { kind: 'still', until: now() + STILL_MS };
    else p.wait = { kind: 'upnext', until: autoplay ? now() + COUNTDOWN_MS : null };
    note(p);
    changed();
  }

  function waitOver(p) {
    if (p.wait.kind === 'still') {
      goneBy(p, 'stopped', true);
      return;
    }
    p.auto++;
    goOn(p, { next: true }).catch(function () {
      /* It waits for a press. */
      if (p.done || !p.wait) return;
      p.wait = { kind: 'upnext', until: null };
      note(p);
      tell();
    });
  }

  /* What the player's page of `key` says: its words, where it starts and what follows. */
  function readPage(key) {
    var href = '/watch/play/' + encodeURIComponent(key);
    return limited(PAGE_WAIT_MS, function (signal) {
      return fetch(href, { credentials: 'same-origin', signal: signal }).then(function (resp) {
        if (!resp.ok || resp.redirected) throw refusal('unreachable');
        return resp.text();
      });
    }).then(function (html) {
      var el = new DOMParser().parseFromString(html, 'text/html').getElementById('player');
      if (!el) throw refusal('unreachable');
      var item = JSON.parse(el.getAttribute('data-item'));
      var next = JSON.parse(el.getAttribute('data-next') || 'null');
      return {
        title: { rating_key: String(item.rating_key), title: item.title, subtitle: item.subtitle || '' },
        start_s: parseFloat(el.getAttribute('data-start-s')) || 0,
        next: next && { rating_key: next.href.split('?')[0].split('/').pop(), label: next.label },
      };
    });
  }

  function openStream(key, quality, subtitle) {
    var ask = { rating_key: key, quality: quality, page: me, asked_s: 0 };
    if (subtitle) ask.subtitle = subtitle;
    return limited(START_WAIT_MS, function (signal) {
      return MM.api.post('/api/cast/stream', ask, { signal: signal });
    }).catch(function (err) { throw refusal(err && err.error || 'network_error'); });
  }

  /* Put another stream on the TV: another quality or subtitle where it stands, or the next episode. */
  function goOn(p, change) {
    var keep = p.keep || {};
    if (p.lost !== null || (change.next ? !keep.next : !!p.wait)) return Promise.reject(refusal('failed', NOT_TAKEN));
    var asked = { quality: change.quality || keep.quality || 'auto', subtitle: change.subtitle || keep.subtitle || null };
    /* A track is one film's own. */
    if (change.next && asked.subtitle !== 'off') asked.subtitle = null;
    return swap(p, function (stale) {
      var page = change.next ? readPage(keep.next.rating_key) : Promise.resolve(null);
      return page.then(function (about) {
        if (stale()) throw refusal('superseded');
        return openStream(about ? about.title.rating_key : p.title.rating_key, asked.quality, asked.subtitle).then(function (data) {
          return put(p, stale, data, about, asked);
        });
      }).catch(function (err) {
        /* What was refused may have ended the stream that plays. */
        var old = !stale() && !change.next && bringBack(p, stale);
        return Promise.resolve(old).then(function () { throw err; });
      });
    });
  }

  /* `go` with the play marked as changing its stream; a later change or the end makes it stale. */
  function swap(p, go) {
    var mine = ++p.changes;
    var stale = function () { return p.done || mine !== p.changes; };
    function over() {
      if (stale()) return;
      p.changing = false;
      note(p);
      changed();
    }
    p.changing = true;
    note(p);
    tell();
    var gone = go(stale);
    gone.then(over, over);
    return gone;
  }

  /* Load what the server opened; where nothing waits for it any more it is stopped there. */
  function put(p, stale, data, about, asked) {
    if (stale()) {
      stopOnServer(data.stream_id);
      return Promise.reject(refusal('superseded'));
    }
    if (about && !p.wait) stopOnServer(p.stream.stream_id, body(p));
    var next = about ? about.next : p.keep && p.keep.next || null;
    p.wait = null;
    return load(p, {
      stream_id: data.stream_id,
      url: data.url,
      position_s: about ? about.start_s : p.at(),
      saved_s: about ? null : p.savedAt,
      duration_s: data.duration_s,
      paused: about ? false : p.paused,
      title: about ? about.title : p.title,
    }).then(function () {
      p.cut = false;
      p.mine = true;
      p.keep = {
        quality: asked.quality,
        subtitle: asked.subtitle,
        played_s: Math.round(p.played),
        given: { subtitle: data.subtitle || null, subtitle_tracks: data.subtitle_tracks || [] },
        markers: data.markers || [],
        chapters: data.chapters || [],
        next: next,
      };
      noteKept(p);
    }, function (err) {
      /* The TV has let go of the one before. */
      if (stale()) stopOnServer(data.stream_id);
      else goneBy(p, 'stopped', true);
      throw err;
    });
  }

  /* The stream that played is opened again where it is gone. */
  function bringBack(p, stale) {
    return MM.api.post('/api/watch/progress', Object.assign({ stream_id: p.stream.stream_id }, body(p))).then(function () {
      return !p.ms;
    }, function (err) {
      return !p.ms || !!err && err.status === 410;
    }).then(function (over) {
      return over && !stale() ? openAgain(p, stale) : undefined;
    });
  }

  function openAgain(p, stale) {
    var keep = p.keep || {};
    var asked = { quality: keep.quality || 'auto', subtitle: keep.subtitle || null };
    return openStream(p.title.rating_key, asked.quality, asked.subtitle).then(function (data) {
      return put(p, stale, data, null, asked);
    }).catch(function () {
      if (!stale()) goneBy(p, 'stopped', true);
    });
  }

  function reopen(p) {
    swap(p, function (stale) { return openAgain(p, stale); });
  }

  /* No player of this page follows what plays. */
  var keeps = supported && !document.getElementById('player');

  /* `told`: there is a sign of the TV, or a press. */
  function keepHere(told) {
    var rec = keeps && !current && readRecord();
    if (rec && (told || !(rec.misses >= MISSES))) rejoin(false).catch(function () { /* It is looked for again later. */ });
  }

  /* ---- coming and going -------------------------------------------------------- */

  global.addEventListener('pagehide', function () {
    var p = current;
    if (!p) return;
    count(p);
    if (!p.followed && p.mine && p.ms && p.lost === null && !p.changing && global.navigator.sendBeacon) {
      global.navigator.sendBeacon('/api/watch/progress', new Blob([
        JSON.stringify(Object.assign({ stream_id: p.stream.stream_id }, body(p))),
      ], { type: 'application/json' }));
    }
    note(p, true);
  });

  /* What it knew of the TV is old. */
  global.addEventListener('pageshow', function (e) {
    if (e.persisted && current) global.location.reload();
  });

  global.addEventListener('storage', function (e) {
    if (e.key !== RECORD) return;
    var told = safely(function () { return JSON.parse(e.newValue); }, null);
    var rec = readRecord();
    var p = current;
    /* Another page's player ends the count. */
    if (p && p.wait && rec && rec.wait && rec.stream === p.stream.stream_id) p.wait = rec.wait;
    if (told && told.released) {
      /* Another page plays it there after all. */
      released = told.released;
      if (p && p.stream.stream_id === released) goneBy(p, 'stopped', true);
    } else if (p && !p.followed && rec && rec.owner && rec.owner !== me) {
      drop(p, true);
      changed();
    } else if (p && !rec) {
      /* Somebody signs out: what the TV plays is left to it. */
      letGo(p);
    } else if (!p && rec && !rec.owner && !document.hidden) {
      keepHere();
    }
  });

  function letGo(p) {
    drop(p, true);
    if (p.followed) p.gone.forEach(function (fn) { fn('stopped'); });
    changed();
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) keepHere();
  });

  /* ---- a way of Play on --------------------------------------------------------- */

  var starts = 0;
  var pending = 0;
  /* A session opened for a start that plays nothing yet. */
  var opened = null;

  /* The session a start may go to without a choice: one that plays, or waits for, this page's own. */
  function ownSession() {
    var session = context && safely(function () { return context.getCurrentSession(); }, null);
    if (!session) return null;
    return current && current.lost === null && current.session === session || opened === session ? session : null;
  }

  function start(what) {
    pending++;
    return (context ? choose() : loadSdk().then(choose)).then(function (session) {
      var mine = ++starts;
      return what.open().then(function (stream) {
        if (mine !== starts) throw refusal('superseded');
        return begin(session, what, stream);
      });
    }).then(function (face) {
      pending--;
      opened = null;
      return face;
    }, function (err) {
      pending--;
      if (opened && !pending) {
        /* Nothing plays on what was opened for it. */
        opened.endSession(true);
        opened = null;
      }
      throw err;
    });
  }

  /* The session to play on: this page's own, or the one chosen in the browser's list. */
  function choose() {
    var own = ownSession();
    if (own) return Promise.resolve(own);
    var before = safely(function () { return context.getCurrentSession().getSessionId(); }, null);
    var chosen;
    try {
      chosen = context.requestSession();
    } catch (err) {
      chosen = Promise.reject(err);
    }
    return chosen.then(function () {
      var session = context.getCurrentSession();
      if (!session) throw refusal('failed', NOT_REACHED);
      if (session.getSessionId() !== before) opened = session;
      return session;
    }, function (err) {
      if (err === global.chrome.cast.ErrorCode.CANCEL) throw refusal('cancelled');
      throw refusal('failed', NOT_REACHED);
    });
  }

  function begin(session, what, stream) {
    var before = current;
    if (before) {
      drop(before);
      /* The TV's picture is about to change: the one before it keeps its place. */
      if (!before.followed && !before.wait) stopOnServer(before.stream.stream_id, body(before));
    }
    var p = makePlay(session, stream.title || what.title, true);
    p.own = true;
    ended = null;
    current = p;
    return load(p, stream).then(function () {
      return p.face;
    }, function (err) {
      drop(p);
      changed();
      throw err;
    });
  }

  /* What an earlier page put on the TV, where it is the title `key`; null at once where nothing
     can be. Rejects where another page follows it, or the TV was not found in time. */
  function again(key) {
    var rec = readRecord();
    if (!rec) return null;
    if (rec.wait) {
      /* The player goes on here with what would begin there. */
      var next = (readKept(rec.stream).keep || {}).next || {};
      if (String(key) === String(rec.key) || String(key) === String(next.rating_key)) hold(rec);
      keeps = true;
      keepHere(true);
      return null;
    }
    if (key !== undefined && String(key) !== String(rec.key)) {
      keeps = true;
      keepHere(true);
      return null;
    }
    if (ownedElsewhere(rec)) return Promise.reject(refusal('elsewhere'));
    return rejoin(true).then(function (p) {
      if (!p) return null;
      p.followed = true;
      return {
        play: p.face,
        stream: {
          stream_id: p.stream.stream_id,
          position_s: p.at(),
          saved_s: p.savedAt === null ? undefined : p.savedAt,
          duration_s: p.duration,
          paused: p.paused,
          rating_key: p.title.rating_key,
          title: p.title,
          keep: Object.assign({}, p.keep, { played_s: Math.round(p.played) }),
        },
      };
    });
  }

  /* What waits at an end begins by itself no more. */
  function hold(rec) {
    store(RECORD, Object.assign(rec, { wait: { kind: 'upnext', until: null }, owner: '', beat: 0 }));
  }

  var released = null;

  /* The person plays `key` here after all: what plays on the TV is looked for no more. Returns the
     place it was at and how long ago that was, or null. */
  function release(key) {
    var rec = readRecord();
    if (!rec || String(rec.key) !== String(key)) return null;
    released = rec.stream;
    /* Told to the other pages. */
    store(RECORD, { released: rec.stream });
    forgetStored();
    return {
      position_s: tenths(rec.place),
      played_s: Math.round(rec.played || 0),
      age_s: (now() - rec.at) / 1000,
    };
  }

  if (playOn && supported) {
    playOn.register({
      id: 'cast',
      label: 'Chromecast',
      sub: 'TVs with Chromecast or Google Cast',
      icon: 'play-on-cast',
      chooser: { title: 'Chrome shows its own list now', line: 'Choose the TV there. It comes from Chrome, not from mediaman.' },
      available: available,
      looking: looking,
      /* Why pressing it would lead nowhere. */
      trouble: function () { return failed ? NO_SCRIPT : null; },
      /* Whether pressing it opens the browser's list. */
      asks: function () { return !failed && !ownSession(); },
      look: function () { loadSdk().catch(function () {}); },
      start: start,
      again: again,
      release: release,
    });
  }

  /* What plays with no player, once it is known to be this person's. */
  function kept() { return current && !current.followed && current.mine ? current : null; }

  /* Do something with it: somebody is there. */
  function pressed(fn) {
    var p = kept();
    if (!p) return undefined;
    p.sign = now();
    p.auto = 0;
    return fn(p);
  }

  function setPlaying(p, plays) {
    if (p.paused !== plays || p.wait) return;
    if (plays) quietly(p.media.play());
    else p.media.pause();
  }

  MM.castSender = {
    now: function () { return safely(function () { return kept() && describe(kept()); }, null); },
    over: function () { return ended; },
    on: function (fn) { watchers.push(fn); },
    play: function () { pressed(function (p) { setPlaying(p, true); }); },
    pause: function () { pressed(function (p) { setPlaying(p, false); }); },
    toggle: function () { pressed(function (p) { setPlaying(p, p.paused); }); },
    seek: function (to) { pressed(function (p) { p.media.currentTime = to; }); },
    setVolume: function (level) { pressed(function (p) { p.media.volume = level; }); },
    setMuted: function (muted) { pressed(function (p) { p.media.muted = muted; }); },
    start: function (change) {
      return pressed(function (p) { return goOn(p, change); }) || Promise.reject(refusal('failed', NOT_TAKEN));
    },
    cancel: function () {
      pressed(function (p) {
        if (!p.wait) return;
        p.wait = { kind: 'upnext', until: null };
        note(p);
        tell();
      });
    },
    stop: function () {
      var p = kept();
      if (!p) return Promise.resolve({ position_s: 0, saved: false });
      var at = body(p);
      /* Whether the end was saved is not known where the wait was taken over: it is told again. */
      var saved = p.wait && p.endSaved || stopOnServer(p.stream.stream_id, at);
      finish(p);
      return saved.then(function (ok) { return { position_s: at.position_s, saved: ok }; });
    },
    holds: function (key) {
      var p = current;
      if (p) return (p.followed || p.mine) && !p.wait && String(p.title.rating_key) === String(key);
      var rec = readRecord();
      return !!rec && livesElsewhere(rec) && String(rec.key) === String(key);
    },
  };

  keepHere();
})(window);
