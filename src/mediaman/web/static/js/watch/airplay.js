/* AirPlay as a way of the Play on sheet, in Safari.
 *
 * The TV reads the stream itself, by an address that needs no sign-in, so it plays from an element
 * of its own: what hls.js feeds cannot be sent to a TV. Until a TV is chosen the element holds a
 * short silent clip, since Safari opens its list of TVs only for something that can play. Nothing
 * is asked of mediaman and nothing stops here before the person has chosen one.
 *
 * On a page without a player it only says whether there is a TV, and leads to the player.
 *
 * Requires: play-on.js. player.js follows what plays.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  if (!MM || !MM.playOn) return;
  var root = document.getElementById('player');
  var el = document.createElement('video');
  if (!global.WebKitPlaybackTargetAvailabilityEvent || !el.webkitShowPlaybackTargetPicker) return;

  var nearby = false;
  el.addEventListener('webkitplaybacktargetavailabilitychanged', function (e) {
    nearby = e.availability === 'available';
    MM.playOn.changed();
  });

  var way = {
    id: 'airplay',
    label: 'AirPlay',
    sub: 'Apple TV and TVs with AirPlay',
    icon: 'play-on-airplay',
    available: function () { return nearby; },
  };

  if (!root) {
    el.className = 'sr-only';
    el.tabIndex = -1;
    el.preload = 'none';
    el.setAttribute('aria-hidden', 'true');
    document.body.appendChild(el);
    way.leads = true;
    MM.playOn.register(way);
    return;
  }

  var CLIP = '/static/video/airplay.mp4';
  var CHOOSE_MS = 90000;     // a TV chosen later was not chosen here
  var LOAD_MS = 45000;
  /* A new stream is not on the TV at once, and Safari says so meanwhile. */
  var ARRIVE_MS = 10000;
  var GONE_MS = 1500;

  var choosing = null;       // {what, resolve, reject, timer} while the list is open
  var play = null;           // what plays on the TV, or is about to
  var loading = false;
  var arriving = false;      // the stream has yet to be seen on the TV
  var goneTimer = null;

  el.className = 'player-video player-air';
  el.playsInline = true;
  el.preload = 'metadata';
  el.tabIndex = -1;
  el.setAttribute('webkit-playsinline', '');
  el.setAttribute('x-webkit-airplay', 'allow');
  el.setAttribute('aria-hidden', 'true');
  el.canVolume = false;
  var first = root.querySelector('video');
  first.parentNode.insertBefore(el, first.nextSibling);
  rest();

  function rest() {
    el.pause();
    el.muted = true;
    el.loop = true;
    el.src = CLIP;
  }

  function onTv() { return !!el.webkitCurrentPlaybackTargetIsWireless; }

  function coded(code, say) { return Object.assign(new Error(code), { code: code, say: say }); }

  function dropChoice(code) {
    var was = choosing;
    if (!was) return;
    choosing = null;
    clearTimeout(was.timer);
    was.reject(coded(code));
  }

  function start(what) {
    dropChoice('cancelled');
    return new Promise(function (resolve, reject) {
      choosing = { what: what, resolve: resolve, reject: reject };
      choosing.timer = setTimeout(function () {
        dropChoice('cancelled');
        if (!play) el.pause();
      }, CHOOSE_MS);
      /* Played by a press once, it may play by itself later. */
      var p = el.play();
      if (p && p.catch) p.catch(function () { /* the list opens all the same */ });
      try {
        if (onTv()) begin();
        else el.webkitShowPlaybackTargetPicker();
      } catch (e) {
        dropChoice('failed');
      }
    });
  }

  /* A TV was chosen. */
  function begin() {
    var was = choosing;
    choosing = null;
    clearTimeout(was.timer);
    var gone = [];
    var mine = play = {
      name: '',
      carriesOn: false,
      media: el,
      load: load,
      stop: function () {
        if (play !== mine) return Promise.resolve();
        play = null;
        clearTimeout(goneTimer);
        rest();
        return Promise.resolve();
      },
      onGone: function (fn) { gone.push(fn); },
      gone: function () {
        play = null;
        gone.forEach(function (fn) { fn('back'); });
        rest();
      },
    };
    was.what.open().then(function (stream) {
      if (play !== mine) throw coded('cancelled');
      return load(stream);
    }).then(function () { was.resolve(mine); }, function (err) {
      mine.stop();
      was.reject(err);
    });
  }

  /* Settles once the stream stands at its place. */
  function load(stream) {
    clearTimeout(goneTimer);
    loading = arriving = true;
    el.loop = false;
    el.muted = false;
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { done(coded('failed')); }, LOAD_MS);
      function there() {
        el.currentTime = stream.position_s;
        done(null);
      }
      function failed() { done(coded('failed')); }
      function done(err) {
        clearTimeout(timer);
        el.removeEventListener('loadedmetadata', there);
        el.removeEventListener('error', failed);
        loading = false;
        if (err) { reject(err); return; }
        lookFor(ARRIVE_MS);
        resolve();
      }
      el.addEventListener('loadedmetadata', there);
      el.addEventListener('error', failed);
      el.src = stream.url;
      el.load();
    });
  }

  /* Whether it is on the TV, some time from now. */
  function lookFor(ms) {
    clearTimeout(goneTimer);
    goneTimer = setTimeout(function () {
      /* What has not played yet need not be on the TV yet. */
      if (play && !loading && !onTv() && !(arriving && el.paused)) play.gone();
    }, ms);
  }

  el.addEventListener('webkitcurrentplaybacktargetiswirelesschanged', function () {
    if (onTv() && choosing && !play) begin();
    else if (onTv()) arriving = false;
    else if (play && !loading) lookFor(arriving ? ARRIVE_MS : GONE_MS);
  });

  el.addEventListener('playing', function () {
    if (!play || loading) return;
    if (onTv()) arriving = false;
    else lookFor(ARRIVE_MS);
  });

  function lister() {
    var ua = global.navigator.userAgent;
    if (/iPhone/.test(ua)) return 'iOS';
    return /iPad/.test(ua) || global.navigator.maxTouchPoints > 1 ? 'iPadOS' : 'Safari';
  }

  MM.playOn.register(Object.assign(way, {
    available: function () { return nearby || !!play; },
    chooser: {
      title: 'Your device shows its own list now',
      line: 'Choose the TV there. It comes from ' + lister() + ', not from mediaman.',
    },
    hereNote: 'If the sound stays on the TV, choose this device in the AirPlay list.',
    start: start,
  }));
})(window);
