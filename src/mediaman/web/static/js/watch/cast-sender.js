/* Playing on a Chromecast or a TV with Google Cast: a way of Play on (watch/play-on.js), and what
 * keeps a title's place while the TV plays and no player follows it. The script is on a page only
 * where an admin allows Google's; Google's own is asked for when somebody opens Play on, or while
 * a title plays on a TV.
 *
 *   MM.castSender.now()        what plays where no player of this page follows it, or null
 *   MM.castSender.on(fn)       called when that may have changed
 *   MM.castSender.toggle(), .seek(s), .stop()
 *   MM.castSender.holds(key)   whether this browser plays the title on a TV
 *
 * The TV gets the address of one stream, with a ticket in it, and no session. That address is never
 * stored here: a page that comes later reads it from the TV.
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
  /* A TV that was lost may come back for this long. */
  var LOST_MS = 120000;
  var TICK_MS = 500;
  var BEAT_MS = 5000;
  /* A page in the background hardly runs: it counts as there for this long after its last beat. */
  var ALIVE_MS = 90000;
  /* As long as a ticket plays. */
  var RECORD_MS = 6 * 60 * 60 * 1000;
  var SAVE_MS = 10000;
  var KEEP_ALIVE_MS = 60000;
  var VOLUME_MS = 150;
  /* Past this a TV that has not said it did what was pressed is taken at its word. */
  var ANSWER_MS = 10000;
  var RECORD = 'mediaman.watch.cast';
  var HLS = 'application/vnd.apple.mpegurl';

  var NOT_TAKEN = { title: 'The TV didn’t start', line: 'It didn’t take the picture. Choose it again, or watch here.' };
  var NOT_REACHED = { title: 'The TV didn’t answer', line: 'Check that it is on and on the same network, then choose it again.' };

  var now = function () { return Date.now(); };
  var me = Array.prototype.map.call(global.crypto.getRandomValues(new Uint8Array(8)), function (b) {
    return (b < 16 ? '0' : '') + b.toString(16);
  }).join('');

  function refusal(code, say) { return Object.assign(new Error(code), { code: code, say: say }); }

  /* ---- Google's script -------------------------------------------------- */

  var supported = !!(global.isSecureContext && global.chrome && global.navigator.presentation);
  var loading = null;
  var context = null; // once the script is there

  function loadSdk() {
    if (loading) return loading;
    loading = new Promise(function (resolve, reject) {
      if (!supported) { reject(refusal('failed')); return; }
      var timer = setTimeout(function () { settle(false); }, SDK_WAIT_MS);
      var settled = false;
      function settle(ok) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        global.__onGCastApiAvailable = function () {};
        if (!ok) { reject(refusal('failed')); return; }
        try {
          setUp();
          resolve();
        } catch (err) {
          context = null;
          reject(refusal('failed'));
        }
      }
      global.__onGCastApiAvailable = settle;
      var script = document.createElement('script');
      script.src = SDK;
      script.onerror = function () { settle(false); };
      document.head.appendChild(script);
    });
    var mine = loading;
    /* Play on goes without it, and asks again the next time it is opened. */
    mine.then(changed, function () { loading = null; });
    return mine;
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
    return !!context && context.getCastState() !== global.cast.framework.CastState.NO_DEVICES_AVAILABLE;
  }

  var watchers = [];
  function changed() {
    if (playOn) playOn.changed();
    watchers.slice().forEach(function (fn) { fn(); });
  }

  /* ---- what plays, for the pages that come later -------------------------- */

  function readRecord() {
    try {
      var rec = JSON.parse(global.localStorage.getItem(RECORD));
      return rec && typeof rec.stream === 'string' && now() - rec.at < RECORD_MS ? rec : null;
    } catch (err) {
      return null;
    }
  }

  function writeRecord(rec) {
    try {
      if (rec) global.localStorage.setItem(RECORD, JSON.stringify(rec));
      else global.localStorage.removeItem(RECORD);
    } catch (err) { /* Without it a reload does not find the TV again. */ }
  }

  /* A player on another page that is open follows it; one that only keeps the place makes way. */
  function ownedElsewhere(rec) { return rec.owner !== me && !rec.soft && now() - rec.beat < ALIVE_MS; }

  function note(p, left) {
    writeRecord({
      session: p.session.getSessionId(),
      stream: p.stream.stream_id,
      key: p.title.rating_key,
      keep: p.keep,
      played: p.played,
      owner: left ? '' : me,
      soft: !p.followed,
      beat: left ? 0 : now(),
      at: now(),
    });
    p.noted = now();
  }

  function forget(id) {
    var rec = readRecord();
    if (rec && rec.stream === id) writeRecord(null);
  }

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
      mine: false,       // the server has taken a save of it here: it is this person's
      saving: false,
      saved: null,       // when a save was last made here, and whether it was paused
      loads: 0,
      gone: [],
      noted: 0,
      timers: {},
    };

    var media = p.media = new EventTarget();
    function emit() {
      Array.prototype.forEach.call(arguments, function (type) { media.dispatchEvent(new Event(type)); });
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
      var receiver = p.session.getCastDevice();
      return !(receiver && receiver.volume && receiver.volume.controlType === 'fixed');
    });
    read('volume', function () {
      if (p.level !== null) return p.level;
      var level = p.session.getVolume();
      return typeof level === 'number' ? level : 1;
    }, setVolume);
    read('muted', function () { return !!p.session.isMute(); }, function (muted) {
      if (p.lost !== null) return;
      quietly(p.session.setMute(!!muted));
      emit('volumechange');
    });
    media.play = function () { return command('play'); };
    media.pause = function () { command('pause').catch(function () {}); };

    function place() {
      if (p.aim !== null) return p.aim;
      if (p.ms && p.begun && p.lost === null && !p.ended) {
        p.place = Math.max(0, Math.min(p.ms.getEstimatedTime(), p.duration || Infinity));
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
        if (!p.followed) finish(p, true);
      } else if (ms.idleReason === reasons.ERROR) {
        place();
        detach();
        p.error = { code: 2 };
        if (p.followed) emit('error');
        else finish(p, true);
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
        keeps = true;
        if (!p.done) note(p);
        changed();
      },
      onGone: function (fn) { p.gone.push(fn); },
      keep: function (kept) {
        takeKept(p, kept);
        if (!p.done && current === p) note(p);
      },
    };
    Object.defineProperty(p.face, 'name', { get: function () { return nameOf(p); }, enumerable: true });
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
    var receiver = p.session.getCastDevice();
    return receiver && receiver.friendlyName || '';
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
    if (!p.followed) keepPlace(p);
  }

  /* Over here: nothing more is said of it. */
  function drop(p, kept) {
    p.at();
    p.done = true;
    p.detach();
    p.leaveSession();
    Object.keys(p.timers).forEach(function (name) {
      clearTimeout(p.timers[name]);
      clearInterval(p.timers[name]);
    });
    if (!kept) forget(p.stream.stream_id);
    if (current === p) current = null;
  }

  /* It ended by itself, or by another's hand. */
  function goneBy(p, reason) {
    var followed = p.followed;
    drop(p);
    if (followed) p.gone.forEach(function (fn) { fn(reason); });
    else stopOnServer(p.stream.stream_id, body(p));
    changed();
  }

  /* End it on the TV; `served` saves the place and stops the stream as well. */
  function finish(p, served) {
    if (p.done) return Promise.resolve();
    drop(p);
    if (p.lost === null) p.session.endSession(true);
    changed();
    return served ? stopOnServer(p.stream.stream_id, body(p)) : Promise.resolve();
  }

  /* ---- sessions ------------------------------------------------------------ */

  function onSession(e) {
    var states = global.cast.framework.SessionState;
    var p = current;
    changed();
    if (!p && e.sessionState === states.SESSION_RESUMED) keepHere();
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
     said what it plays; null where that takes too long. */
  function waitForTv(rec) {
    return new Promise(function (resolve) {
      var framework = global.cast.framework;
      var type = framework.CastContextEventType.SESSION_STATE_CHANGED;
      var heard = null;
      var timer = setTimeout(function () { over(null); }, REJOIN_WAIT_MS);
      function over(found) {
        clearTimeout(timer);
        context.removeEventListener(type, look);
        if (heard) heard.removeEventListener(framework.SessionEventType.MEDIA_SESSION, look);
        resolve(found);
      }
      function look() {
        var session = context.getCurrentSession();
        if (!session || session.getSessionId() !== rec.session) return;
        if ((session.getSessionObj().media || []).length) {
          over({ session: session, ms: mediaOf(session, { stream_id: rec.stream }) });
        } else if (heard !== session) {
          heard = session;
          session.addEventListener(framework.SessionEventType.MEDIA_SESSION, look);
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
      return takeUp(rec, found.session, found.ms, followed);
    }).catch(function () {
      throw refusal('lost');
    });
    var done = function () { rejoining = null; };
    mine.then(done, done);
    return mine;
  }

  function takeUp(rec, session, ms, followed) {
    if (!ms || ms.idleReason) {
      forget(rec.stream);
      if (ms && ms.idleReason === global.chrome.cast.media.IdleReason.FINISHED && ms.media.duration) {
        /* It ended while no page was open. */
        stopOnServer(rec.stream, { position_s: ms.media.duration, played_s: Math.round(rec.played || 0) });
      }
      return null;
    }
    var said = ms.media.metadata || {};
    var p = makePlay(session, { rating_key: rec.key, title: said.title || '', subtitle: said.subtitle || '' }, followed);
    p.stream = { stream_id: rec.stream };
    p.keep = rec.keep;
    p.duration = ms.media.duration || 0;
    p.place = ms.getEstimatedTime();
    p.paused = ms.playerState === global.chrome.cast.media.PlayerState.PAUSED;
    p.played = Number(rec.played) || 0;
    p.begun = true;
    current = p;
    p.attach(ms);
    p.update();
    if (p.done) return null;
    note(p);
    changed();
    return p;
  }

  /* ---- keeping the place where no player does -------------------------------- */

  function body(p) {
    return {
      position_s: Math.round(p.at() * 10) / 10,
      played_s: Math.round(p.played),
    };
  }

  function stopOnServer(id, at) {
    return MM.api.post('/api/watch/stream/' + encodeURIComponent(id) + '/stop', at)
      .catch(function () { /* The server drops it when nothing asks for it. */ });
  }

  function keepPlace(p) {
    if (p.saving || p.lost !== null) return;
    var due = 0;
    if (p.saved && p.saved.paused === p.paused) due = p.paused ? KEEP_ALIVE_MS : SAVE_MS;
    if (!p.saved || now() - p.saved.t >= due) savePlace(p);
  }

  function savePlace(p) {
    p.saving = true;
    p.saved = { t: now(), paused: p.paused };
    MM.api.post('/api/watch/progress', Object.assign({ stream_id: p.stream.stream_id }, body(p))).then(function () {
      p.saving = false;
      if (p.done || p.mine) return;
      p.mine = true;
      changed();
    }, function (err) {
      p.saving = false;
      /* The stream is over, or it is not this person's: the TV cannot go on with it. */
      if (err && err.status === 410 && !p.followed) finish(p);
    });
  }

  /* No player of this page follows what plays. */
  var keeps = supported && !document.getElementById('player');

  function keepHere() {
    if (keeps && !current) rejoin(false).catch(function () { /* It is looked for again later. */ });
  }

  /* ---- coming and going -------------------------------------------------------- */

  global.addEventListener('pagehide', function () {
    var p = current;
    if (!p) return;
    count(p);
    if (!p.followed && p.mine && p.lost === null && global.navigator.sendBeacon) {
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
    var rec = readRecord();
    var p = current;
    if (p && !p.followed && rec && rec.owner && rec.owner !== me) {
      drop(p, true);
      changed();
    } else if (rec && !rec.owner && !document.hidden) {
      keepHere();
    }
  });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) keepHere();
  });

  /* ---- a way of Play on --------------------------------------------------------- */

  function start(what) {
    if (!context) return Promise.reject(refusal('failed', NOT_REACHED));
    var had = context.getCurrentSession();
    var chosen;
    try {
      chosen = had ? Promise.resolve() : context.requestSession();
    } catch (err) {
      chosen = Promise.reject(err);
    }
    return chosen.then(null, function (err) {
      if (err === global.chrome.cast.ErrorCode.CANCEL) throw refusal('cancelled');
      throw refusal('failed', NOT_REACHED);
    }).then(function () {
      var session = context.getCurrentSession();
      if (!session) throw refusal('failed', NOT_REACHED);
      return what.open().then(function (stream) {
        return begin(session, what, stream);
      }).catch(function (err) {
        /* Nothing plays on what was opened for this alone. */
        if (!had) session.endSession(true);
        throw err;
      });
    });
  }

  function begin(session, what, stream) {
    var before = current;
    if (before) {
      drop(before);
      /* The TV's picture is about to change: the one before it keeps its place. */
      if (!before.followed) stopOnServer(before.stream.stream_id, body(before));
    }
    var p = makePlay(session, stream.title || what.title, true);
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
     can be. Rejects where the TV was not found in time. */
  function again(key) {
    var rec = readRecord();
    if (!rec || ownedElsewhere(rec)) return null;
    if (key !== undefined && String(key) !== String(rec.key)) {
      keeps = true;
      keepHere();
      return null;
    }
    return rejoin(true).then(function (p) {
      if (!p) return null;
      p.followed = true;
      return {
        play: p.face,
        stream: {
          stream_id: p.stream.stream_id,
          position_s: p.at(),
          duration_s: p.duration,
          paused: p.paused,
          rating_key: p.title.rating_key,
          title: p.title,
          keep: Object.assign({}, p.keep, { played_s: Math.round(p.played) }),
        },
      };
    });
  }

  if (playOn && supported) {
    playOn.register({
      id: 'cast',
      label: 'Chromecast',
      sub: 'TVs with Chromecast or Google Cast',
      icon: 'play-on-cast',
      chooser: { title: 'Chrome shows its own list now', line: 'Choose the TV there. It comes from Chrome, not from mediaman.' },
      available: available,
      look: function () { loadSdk().catch(function () {}); },
      start: start,
      again: again,
    });
  }

  MM.castSender = {
    now: function () {
      var p = current;
      if (!p || p.followed || !p.mine) return null;
      return {
        name: nameOf(p),
        rating_key: p.title.rating_key,
        title: p.title.title,
        subtitle: p.title.subtitle,
        position_s: p.at(),
        duration_s: p.duration,
        paused: p.paused,
        lost: p.lost !== null,
      };
    },
    on: function (fn) { watchers.push(fn); },
    toggle: function () {
      if (!current) return;
      if (current.paused) quietly(current.media.play());
      else current.media.pause();
    },
    seek: function (to) { if (current) current.media.currentTime = to; },
    stop: function () { return current ? finish(current, true) : Promise.resolve(); },
    holds: function (key) {
      var rec = readRecord();
      return !!rec && String(rec.key) === String(key);
    },
  };

  keepHere();
})(window);
