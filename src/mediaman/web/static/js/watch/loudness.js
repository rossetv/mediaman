/**
 * watch/loudness.js — Reduce loud sounds, for a video that plays through Media Source.
 *
 * MM.loudness.create(media, failed) gives null where the browser has no Web Audio to do it with;
 * else {set(on), dry(held), on()}. Nothing is built until set(true): then, inside that press,
 * an AudioContext is made and resumed, and only once it runs is the video's sound led through
 *
 *   source ─┬─ dry gain ────────────────────────────┬─ speakers
 *           └─ compressor ─ makeup gain ─ wet gain ──┘
 *
 * so that a context the browser holds back never silences the video. Turning it on or off
 * crossfades the two gains. dry(true) plays it as it comes until dry(false), for when the
 * sound goes somewhere the context does not reach (AirPlay, picture in picture). `failed` is
 * called once where the context or the source cannot be made, and the setting is then no use.
 *
 * Not in WebKit (Safari, and every browser on an iPhone): a video it plays from Media Source, or
 * from HLS itself, is silent once led into a context, and that cannot be undone.
 */
(function (global) {
  'use strict';

  var MM = global.MM = global.MM || {};

  /* For film: speech is left about where it was and an explosion's peak comes down about 8 dB.
     The browser's compressor adds a makeup gain of its own, which MAKEUP_DB takes back. */
  var COMPRESSOR = { threshold: -28, knee: 24, ratio: 5, attack: 0.005, release: 0.25 };
  var MAKEUP_DB = -6;
  var FADE_S = 0.06;
  var GESTURES = ['pointerdown', 'keydown', 'touchend'];

  function create(media, failed) {
    var AC = global.AudioContext;
    if (!AC || typeof AC.prototype.createMediaElementSource !== 'function') return null;
    if (global.navigator && global.navigator.vendor === 'Apple Computer, Inc.') return null;
    var ctx = null;
    var dryGain = null;
    var wetGain = null;
    var wanted = false;
    var held = false;
    var broken = false;

    function fail() {
      if (broken) return;
      broken = true;
      wanted = false;
      GESTURES.forEach(function (g) { global.document.removeEventListener(g, wake, true); });
      if (ctx && !dryGain && ctx.close) ctx.close().catch(function () {});
      if (failed) failed();
    }

    function wet() { return wanted && !held; }

    function fade() {
      if (!dryGain) return;
      var t = ctx.currentTime;
      [[dryGain, wet() ? 0 : 1], [wetGain, wet() ? 1 : 0]].forEach(function (g) {
        g[0].gain.cancelScheduledValues(t);
        g[0].gain.setValueAtTime(g[0].gain.value, t);
        g[0].gain.linearRampToValueAtTime(g[1], t + FADE_S);
      });
    }

    /* The video is led through the context only once it runs. */
    function tap() {
      if (dryGain || broken || ctx.state !== 'running') return;
      var source;
      try {
        source = ctx.createMediaElementSource(media);
      } catch (e) {
        fail();
        return;
      }
      var comp = ctx.createDynamicsCompressor();
      Object.keys(COMPRESSOR).forEach(function (k) { comp[k].value = COMPRESSOR[k]; });
      var makeup = ctx.createGain();
      makeup.gain.value = Math.pow(10, MAKEUP_DB / 20);
      dryGain = ctx.createGain();
      wetGain = ctx.createGain();
      dryGain.gain.value = wet() ? 0 : 1;
      wetGain.gain.value = wet() ? 1 : 0;
      source.connect(dryGain);
      dryGain.connect(ctx.destination);
      source.connect(comp);
      comp.connect(makeup);
      makeup.connect(wetGain);
      wetGain.connect(ctx.destination);
    }

    function wake() {
      if (!ctx || broken) return;
      var p = ctx.resume();
      if (p && p.then) p.then(tap, function () {});
      tap();
    }

    function build() {
      try {
        ctx = new AC();
      } catch (e) {
        fail();
        return;
      }
      /* Held back till a press, which may come later; once tapped, a context the system
         stopped (a call, the page hidden) is started again as the video plays. */
      ctx.addEventListener('statechange', function () {
        if (ctx.state === 'running') tap();
        else if (dryGain && ctx.state !== 'closed' && !media.paused) wake();
      });
      media.addEventListener('play', wake);
      GESTURES.forEach(function (g) { global.document.addEventListener(g, wake, true); });
      wake();
    }

    return {
      /* On a press where it can be: the context may only start inside one. */
      set: function (on) {
        if (broken) return;
        wanted = !!on;
        if (wanted && !ctx) build();
        else if (wanted) wake();
        fade();
      },
      dry: function (on) {
        if (held === !!on) return;
        held = !!on;
        fade();
        if (!held) wake();
      },
      on: function () { return wanted; },
    };
  }

  MM.loudness = { create: create, COMPRESSOR: COMPRESSOR, MAKEUP_DB: MAKEUP_DB };
})(window);
