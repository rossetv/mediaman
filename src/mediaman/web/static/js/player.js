/**
 * player.js — the Watch player page (watch_player.html).
 *
 * Plays an HLS stream that mediaman proxies from Plex, always through
 * hls.js. Safari's own HLS player is never used: its requests carry a
 * different User-Agent, which fails the session check and signs the
 * person out.
 *
 * Requests:
 *   POST /api/watch/stream            {rating_key, quality} → {stream_id, playlist}
 *   GET  <playlist> and its segments  (made by hls.js)
 *   POST /api/watch/progress          {stream_id, position_s, played_s}
 *   POST /api/watch/stream/<id>/stop  (sendBeacon on leave; fetch on quality change)
 *   POST /api/watch/keep              {media_item_id, days}
 *
 * A 410 from any stream URL means the server has dropped the stream (a
 * long pause, a restart): a new one is started at the current position,
 * which is also how a quality change works. If the server refuses the
 * stream of a new quality, the quality that was playing comes back.
 * While a new stream replaces the old one, the last frame stays on screen.
 */
(function () {
  'use strict';

  var root = document.getElementById('player');
  if (!root) return;

  var QUALITY_KEY = 'mediaman.watch.quality';
  var QUALITIES = { best: 1, medium: 1, low: 1 };
  var IDLE_MS = 3000;
  var SAVE_EVERY_MS = 10000;
  var SKIP_S = 10;
  var COUNTDOWN_S = 10;
  /* Restarts that playback must outlast before more are allowed. */
  var CALM_PLAYED_S = 30;
  var MAX_GONE_RESTARTS = 2;
  var MSG_UNSUPPORTED = "This browser can't play video here. On iPhone and iPad, iOS 17.1 or later is needed.";
  var MSG_FAILED = "Can't play this right now.";
  /* Start failures by the server's error code; `retry` is whether Try again can help. */
  var START_ERRORS = {
    unreachable: { text: "Can't reach Plex right now.", retry: true },
    not_found: { text: 'No longer available.', retry: false },
    not_set_up: { text: "Plex isn't set up yet.", retry: false },
    too_many_streams: { text: 'You have too many videos playing. Close one and try again.', retry: true },
    rate_limited: { text: 'Too many tries. Wait a moment and try again.', retry: true },
  };
  /* Only a network failure or a server fault is worth one more try. */
  var RETRY_STATUS = { 0: 1, 500: 1, 502: 1, 504: 1 };

  function readJSON(name) {
    try { return JSON.parse(root.getAttribute(name) || 'null'); } catch (e) { return null; }
  }

  var item = readJSON('data-item') || {};
  var next = readJSON('data-next');
  var leaving = readJSON('data-leaving');
  var backHref = root.getAttribute('data-back-href') || '/watch';
  var showHref = root.getAttribute('data-show-href') || '';
  var startS = parseFloat(root.getAttribute('data-start-s')) || 0;

  var video = root.querySelector('video.player-video');
  var hold = root.querySelector('[data-hold]');
  var q = function (sel) { return root.querySelector(sel); };
  var els = {
    play: q('[data-play]'),
    back10: q('[data-back10]'),
    fwd10: q('[data-fwd10]'),
    mute: q('[data-mute]'),
    volume: q('[data-volume]'),
    seek: q('[data-seek]'),
    played: q('[data-played]'),
    buffered: q('[data-buffered]'),
    thumb: q('[data-thumb]'),
    elapsed: q('[data-elapsed]'),
    remaining: q('[data-remaining]'),
    qualityBtn: q('[data-quality-btn]'),
    fullscreen: q('[data-fullscreen]'),
    fullscreenLabel: q('[data-fullscreen-label]'),
    upnext: q('[data-upnext]'),
    countdown: q('[data-countdown]'),
    message: q('[data-message]'),
    messageText: q('[data-message-text]'),
    retry: q('[data-retry]'),
    keep: q('[data-keep]'),
  };

  video.playsInline = true;

  /* ---- state ---------------------------------------------------------- */

  var hls = null;
  var streamId = null;
  var generation = 0;        // bumps on every new stream; stale replies are dropped
  var starting = false;
  var swapping = false;      // the old stream is torn down; its pause event is not the person's
  var pendingStart = startS; // where the stream will start, until the video has loaded
  var wantPlaying = true;
  var hasPlayed = false;
  var playedS = 0;
  var lastTime = null;
  var dragging = false;
  var quality = readQuality();
  var goodQuality = quality; // the last quality that played, or the one the page started with
  var streamQuality = null;  // the quality of the stream now loading or playing
  var revertNotice = null;   // toast to show once the reverted stream plays

  var mediaRecoverUsed = false;
  var fatalRestartUsed = false;
  var goneRestarts = 0;
  var troubleAtPlayed = 0;

  function duration() {
    if (item.duration_s > 0) return item.duration_s;
    return isFinite(video.duration) ? video.duration : 0;
  }

  function position() {
    return pendingStart !== null ? pendingStart : video.currentTime;
  }

  /* ---- quality -------------------------------------------------------- */

  function readQuality() {
    try {
      var saved = window.localStorage.getItem(QUALITY_KEY);
      if (saved && QUALITIES[saved]) return saved;
    } catch (e) { /* storage blocked */ }
    return 'best';
  }

  function showQuality() {
    var items = root.querySelectorAll('[data-quality]');
    for (var i = 0; i < items.length; i++) {
      var on = items[i].getAttribute('data-quality') === quality;
      items[i].setAttribute('aria-checked', on ? 'true' : 'false');
      if (on) {
        var name = items[i].getAttribute('data-quality-label');
        var res = items[i].getAttribute('data-quality-res');
        els.qualityBtn.querySelector('[data-quality-name]').textContent = name + ' ·';
        els.qualityBtn.querySelector('[data-quality-res]').textContent = res;
        els.qualityBtn.setAttribute('aria-label', 'Quality: ' + name + ', ' + res);
      }
    }
  }

  function setQuality(value) {
    quality = value;
    try { window.localStorage.setItem(QUALITY_KEY, value); } catch (e) { /* storage blocked */ }
    showQuality();
  }

  function chooseQuality(value) {
    if (!QUALITIES[value] || value === quality) return;
    revertNotice = null;
    setQuality(value);
    restartStream(true);
  }

  /* The server refused the stream of a new quality, and the old stream is
     already stopped: start the quality that was playing, where it was. */
  function revertQuality(pos) {
    var label = root.querySelector('[data-quality="' + goodQuality + '"]').getAttribute('data-quality-label');
    setQuality(goodQuality);
    revertNotice = "Couldn't change the quality. Staying on " + label + '.';
    return startStream(pos);
  }

  /* ---- messages and loading ------------------------------------------- */

  function setLoading(on) {
    root.classList.toggle('is-loading', on);
    showPlayState();
  }

  /* Draw the frame on screen over the video, so tearing the stream down
     does not leave the picture black. */
  function holdFrame() {
    if (video.readyState < 2 || !video.videoWidth) return;
    hold.width = video.videoWidth;
    hold.height = video.videoHeight;
    try { hold.getContext('2d').drawImage(video, 0, 0); } catch (e) { return; }
    hold.hidden = false;
  }

  function releaseFrame() { hold.hidden = true; }

  function showMessage(text, canRetry) {
    swapping = false;
    releaseFrame();
    setLoading(false);
    els.messageText.textContent = text;
    els.retry.hidden = !canRetry;
    els.message.hidden = false;
    revertNotice = null;
    showControls();
    if (canRetry) els.retry.focus();
  }

  function hideMessage() { els.message.hidden = true; }

  /* ---- streams -------------------------------------------------------- */

  function stopOnServer(id) {
    MM.api.post('/api/watch/stream/' + encodeURIComponent(id) + '/stop').catch(function () {});
  }

  function destroyHls() {
    if (hls) {
      var old = hls;
      hls = null;
      old.destroy();
    }
  }

  /* Start a stream at `pos`; one retry before the error is shown. */
  function startStream(pos) {
    var gen = ++generation;
    starting = true;
    pendingStart = pos;
    lastTime = null;
    hideMessage();
    setLoading(true);

    function attempt(tries) {
      return MM.api.post('/api/watch/stream', { rating_key: item.rating_key, quality: quality })
        .then(function (data) {
          if (gen !== generation) {
            if (data && data.stream_id) stopOnServer(data.stream_id);
            return;
          }
          starting = false;
          streamId = data.stream_id;
          streamQuality = quality;
          attach(data.playlist, pendingStart);
        })
        .catch(function (err) {
          if (gen !== generation) return;
          var status = err && typeof err.status === 'number' ? err.status : 0;
          if (tries < 1 && RETRY_STATUS[status]) return attempt(tries + 1);
          var known = err && START_ERRORS[err.error];
          if (goodQuality && goodQuality !== quality && (!known || known.retry)) {
            return revertQuality(pos);
          }
          starting = false;
          showMessage(known ? known.text : MSG_FAILED, known ? known.retry : true);
        });
    }
    return attempt(0);
  }

  function attach(playlist, pos) {
    destroyHls();
    var h = new Hls({
      enableWorker: false,
      startPosition: pos,
      maxBufferLength: 30,
      preferManagedMediaSource: true,
    });
    hls = h;
    h.on(Hls.Events.ERROR, function (event, data) { if (h === hls) onHlsError(data); });
    h.on(Hls.Events.MANIFEST_PARSED, function () {
      if (h !== hls) return;
      swapping = false;
      if (wantPlaying) play();
      else setLoading(false);
    });
    h.loadSource(playlist);
    h.attachMedia(video);
  }

  /* Drop the current stream and start a new one where playback stands.
     `force` is for a quality change, which may replace a stream that is
     still starting. */
  function restartStream(force) {
    if (starting && !force) return;
    var pos = position();
    var old = streamId;
    streamId = null;
    swapping = true;
    hideUpnext();
    holdFrame();
    if (old) stopOnServer(old);
    destroyHls();
    startStream(pos);
  }

  function noteTrouble() { troubleAtPlayed = playedS; }

  /* The server dropped the stream; carry on with a new one, unless new
     streams keep going too before anything plays. */
  function streamGone() {
    if (starting) return;
    if (goneRestarts >= MAX_GONE_RESTARTS) { fail(); return; }
    goneRestarts++;
    noteTrouble();
    restartStream(false);
  }

  function onHlsError(data) {
    if (data.response && data.response.code === 410) {
      streamGone();
      return;
    }
    if (!data.fatal) return;
    if (goodQuality && goodQuality !== quality) {
      /* A quality that never played failed in the player: go back to the one that did. */
      var label = root.querySelector('[data-quality="' + goodQuality + '"]').getAttribute('data-quality-label');
      setQuality(goodQuality);
      revertNotice = "Couldn't change the quality. Staying on " + label + '.';
      restartStream(true);
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecoverUsed) {
      mediaRecoverUsed = true;
      noteTrouble();
      hls.recoverMediaError();
      return;
    }
    if (!fatalRestartUsed) {
      fatalRestartUsed = true;
      noteTrouble();
      restartStream(false);
      return;
    }
    fail();
  }

  function fail() {
    pendingStart = position();
    destroyHls();
    if (streamId) stopOnServer(streamId);
    streamId = null;
    generation++;
    starting = false;
    showMessage(MSG_FAILED, true);
  }

  /* ---- progress ------------------------------------------------------- */

  function progressBody() {
    return {
      stream_id: streamId,
      position_s: Math.round(position() * 10) / 10,
      played_s: Math.round(playedS),
    };
  }

  function saveProgress() {
    if (!streamId || !hasPlayed) return;
    var id = streamId;
    MM.api.post('/api/watch/progress', progressBody()).catch(function (err) {
      if (err && err.status === 410 && id === streamId) streamGone();
    });
  }

  function beaconProgress() {
    if (!streamId || !hasPlayed || !navigator.sendBeacon) return;
    var blob = new Blob([JSON.stringify(progressBody())], { type: 'application/json' });
    navigator.sendBeacon('/api/watch/progress', blob);
  }

  /* On leave one beacon both saves the last position and stops the stream,
     so the stop cannot arrive first and leave the save with a 410. */
  function beaconStop() {
    if (!streamId || !navigator.sendBeacon) return;
    var body = null;
    if (hasPlayed) {
      var p = progressBody();
      body = new Blob([JSON.stringify({ position_s: p.position_s, played_s: p.played_s })],
                      { type: 'application/json' });
    }
    navigator.sendBeacon('/api/watch/stream/' + encodeURIComponent(streamId) + '/stop', body);
  }

  var seekSaveTimer = null;
  function saveAfterSeek() {
    clearTimeout(seekSaveTimer);
    seekSaveTimer = setTimeout(saveProgress, 1000);
  }

  setInterval(function () {
    if (!video.paused && !video.ended && !swapping) saveProgress();
  }, SAVE_EVERY_MS);

  /* ---- playback ------------------------------------------------------- */

  function play() {
    wantPlaying = true;
    var p = video.play();
    if (p && p.catch) {
      p.catch(function (err) {
        /* Autoplay refused: wait for the person to press play. */
        if (err && err.name === 'NotAllowedError') {
          wantPlaying = false;
          setLoading(false);
          showPlayState();
        }
      });
    }
  }

  function togglePlay() {
    if (!els.message.hidden) return;
    if (swapping) {
      /* No video to pause yet: the new stream starts the way this leaves it. */
      wantPlaying = !wantPlaying;
      showPlayState();
      return;
    }
    if (video.paused || video.ended) {
      if (!streamId && !starting) { wantPlaying = true; restartStream(false); return; }
      play();
    } else {
      wantPlaying = false;
      video.pause();
    }
  }

  function seekTo(t) {
    var d = duration();
    t = Math.max(0, d > 0 ? Math.min(t, d - 1) : t);
    lastTime = null;
    hideUpnext();
    if (video.readyState >= 1) {
      pendingStart = null;
      video.currentTime = t;
    } else {
      /* Nothing loaded yet: move where the stream will start. */
      pendingStart = t;
      if (hls) hls.startLoad(t);
    }
    render();
  }

  /* hls.js seeks to the start position once the video has loaded. */
  function settleStart() {
    if (pendingStart !== null && video.readyState >= 1 &&
        Math.abs(video.currentTime - pendingStart) < 1) {
      pendingStart = null;
    }
  }

  function skip(delta) { seekTo(position() + delta); }

  /* While a stream loads to play, the button shows it working, not a Pause
     that has nothing to pause yet; pressing it still means "don't play". */
  function showPlayState() {
    var busy = wantPlaying && root.classList.contains('is-loading');
    var paused = (video.paused || video.ended) && !busy;
    els.play.setAttribute('aria-label', paused ? 'Play' : 'Pause');
    if (busy) els.play.setAttribute('aria-busy', 'true');
    else els.play.removeAttribute('aria-busy');
    var icon = els.play.querySelector('i');
    icon.className = busy ? 'player-btn-ring' : 'fa-solid ' + (paused ? 'fa-play' : 'fa-pause');
    root.classList.toggle('is-paused', paused);
    if (paused) showControls();
    else scheduleIdle();
  }

  /* Playing again, or moving within the episode, cancels the next-episode countdown. */
  ['play', 'playing', 'seeking'].forEach(function (name) {
    video.addEventListener(name, hideUpnext);
  });

  video.addEventListener('playing', function () {
    hasPlayed = true;
    if (streamId) goodQuality = streamQuality;
    if (revertNotice) {
      if (window.UIFeedback) window.UIFeedback.error(revertNotice);
      revertNotice = null;
    }
    swapping = false;
    pendingStart = null;
    releaseFrame();
    setLoading(false);
  });
  video.addEventListener('play', function () {
    wantPlaying = true;
    showPlayState();
  });
  video.addEventListener('pause', function () {
    if (swapping) return;
    showPlayState();
    if (video.ended) return;
    wantPlaying = false;
    saveProgress();
  });
  video.addEventListener('waiting', function () { setLoading(true); });
  video.addEventListener('canplay', function () {
    if (!video.paused) return;
    if (!wantPlaying && !swapping) releaseFrame();
    setLoading(false);
  });
  video.addEventListener('seeking', function () { lastTime = null; });
  video.addEventListener('seeked', function () {
    settleStart();
    saveAfterSeek();
  });
  video.addEventListener('progress', render);
  video.addEventListener('volumechange', showVolume);

  video.addEventListener('timeupdate', function () {
    settleStart();
    var now = video.currentTime;
    /* Count only time that really played: a seek resets the mark, and a
       jump larger than a couple of seconds is not playback. */
    if (lastTime !== null && !video.paused && !video.seeking) {
      var delta = now - lastTime;
      if (delta > 0 && delta < 2) playedS += delta;
    }
    lastTime = now;
    if (playedS - troubleAtPlayed >= CALM_PLAYED_S) {
      mediaRecoverUsed = false;
      fatalRestartUsed = false;
      goneRestarts = 0;
    }
    render();
  });

  video.addEventListener('ended', function () {
    showPlayState();
    saveProgress();
    if (next) startCountdown();
    else window.location.href = showHref || backHref;
  });

  /* ---- seek bar ------------------------------------------------------- */

  function fmt(s) {
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    var mm = h ? (m < 10 ? '0' + m : '' + m) : '' + m;
    return (h ? h + ':' : '') + mm + ':' + (sec < 10 ? '0' + sec : sec);
  }

  var previewS = 0;

  function render() {
    var d = duration();
    var pos = dragging ? previewS : position();
    var pct = d > 0 ? Math.min(100, (pos / d) * 100) : 0;
    els.played.style.width = pct + '%';
    els.thumb.style.left = pct + '%';
    els.elapsed.textContent = fmt(pos);
    els.remaining.textContent = '-' + fmt(d - pos);
    els.seek.setAttribute('aria-valuenow', String(Math.round(pos)));
    els.seek.setAttribute('aria-valuetext', fmt(pos) + ' of ' + fmt(d));

    var end = 0;
    var b = video.buffered;
    for (var i = 0; i < b.length; i++) {
      if (b.start(i) <= video.currentTime + 0.5 && b.end(i) >= video.currentTime) end = b.end(i);
    }
    els.buffered.style.width = (d > 0 ? Math.min(100, (end / d) * 100) : 0) + '%';
  }

  function seekTimeAt(clientX) {
    var rect = els.seek.getBoundingClientRect();
    var frac = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
    return Math.max(0, Math.min(1, frac)) * duration();
  }

  els.seek.addEventListener('pointerdown', function (e) {
    if (e.button !== undefined && e.button !== 0) return;
    dragging = true;
    els.seek.classList.add('is-dragging');
    if (els.seek.setPointerCapture) els.seek.setPointerCapture(e.pointerId);
    previewS = seekTimeAt(e.clientX);
    render();
    e.preventDefault();
  });
  els.seek.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    previewS = seekTimeAt(e.clientX);
    render();
    showControls();
  });
  function endDrag(e, commit) {
    if (!dragging) return;
    dragging = false;
    els.seek.classList.remove('is-dragging');
    if (commit) seekTo(seekTimeAt(e.clientX));
    else render();
  }
  els.seek.addEventListener('pointerup', function (e) { endDrag(e, true); });
  els.seek.addEventListener('pointercancel', function (e) { endDrag(e, false); });
  els.seek.addEventListener('keydown', function (e) {
    if (e.key === 'Home') { seekTo(0); e.preventDefault(); }
    else if (e.key === 'End') { seekTo(duration()); e.preventDefault(); }
  });

  /* ---- buttons -------------------------------------------------------- */

  els.play.addEventListener('click', togglePlay);
  els.back10.addEventListener('click', function () { skip(-SKIP_S); });
  els.fwd10.addEventListener('click', function () { skip(SKIP_S); });

  function showVolume() {
    var silent = video.muted || video.volume === 0;
    els.mute.setAttribute('aria-label', silent ? 'Unmute' : 'Mute');
    els.mute.querySelector('i').className = 'fa-solid ' + (silent ? 'fa-volume-xmark' : 'fa-volume-high');
    els.volume.value = video.muted ? 0 : video.volume;
  }
  els.mute.addEventListener('click', function () {
    video.muted = !video.muted;
    if (!video.muted && video.volume === 0) video.volume = 1;
  });
  els.volume.addEventListener('input', function () {
    video.volume = parseFloat(els.volume.value);
    video.muted = video.volume === 0;
  });

  root.querySelectorAll('[data-quality]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      closeMenus(true);
      chooseQuality(btn.getAttribute('data-quality'));
    });
  });

  /* ---- menus ---------------------------------------------------------- */

  /* Close every menu; with `refocus`, put focus back on the trigger of the one that was open. */
  function closeMenus(refocus) {
    var opened = openTrigger();
    root.querySelectorAll('[data-menu-trigger]').forEach(function (t) {
      t.setAttribute('aria-expanded', 'false');
      t.parentNode.querySelector('.player-menu').hidden = true;
    });
    if (refocus && opened) opened.focus();
  }
  function openTrigger() { return root.querySelector('[data-menu-trigger][aria-expanded="true"]'); }
  function menuOpen() { return !!openTrigger(); }

  /* Arrow keys, Home and End move between the items of the open menu. */
  function moveInMenu(key) {
    var items = openTrigger().parentNode.querySelectorAll('.player-menu-item');
    var at = Array.prototype.indexOf.call(items, document.activeElement);
    var to;
    if (key === 'Home') to = 0;
    else if (key === 'End') to = items.length - 1;
    else if (key === 'ArrowDown') to = at < 0 ? 0 : (at + 1) % items.length;
    else to = at < 0 ? items.length - 1 : (at - 1 + items.length) % items.length;
    items[to].focus();
  }

  root.querySelectorAll('[data-menu-trigger]').forEach(function (trigger) {
    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = trigger.getAttribute('aria-expanded') === 'true';
      closeMenus();
      if (open) return;
      trigger.setAttribute('aria-expanded', 'true');
      var menu = trigger.parentNode.querySelector('.player-menu');
      menu.hidden = false;
      var first = menu.querySelector('[aria-checked="true"]') || menu.querySelector('button');
      if (first) first.focus();
      showControls();
    });
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('.player-menu-wrap')) closeMenus();
  });

  /* ---- keep ----------------------------------------------------------- */

  if (els.keep && leaving) {
    var keeping = false;
    var keepTrigger = els.keep.querySelector('[data-menu-trigger]');
    var undoing = false;

    /* Put the pill back at once; if the server refuses, the keep stands. */
    var undoKeep = function (done, original, days) {
      if (undoing) return;
      undoing = true;
      MM.api.post('/api/watch/unkeep', { media_item_id: leaving.keep_id }).then(function () {
        var active = document.activeElement;
        var moveFocus = !active || active === document.body || done.contains(active) ||
          (active.closest && active.closest('.ui-toast'));
        els.keep.textContent = '';
        els.keep.appendChild(original);
        keeping = false;
        keepTrigger.setAttribute('aria-busy', 'false');
        if (moveFocus) keepTrigger.focus();
        undoing = false;
        if (window.UIFeedback) window.UIFeedback.toast('Keep undone', { variant: 'info' });
      }).catch(function (err) {
        undoing = false;
        var msg = MM.api.messageOf(err, 'Could not undo. It stays kept for ' + days + ' days.');
        if (window.UIFeedback) window.UIFeedback.error(msg);
      });
    };

    els.keep.querySelectorAll('[data-keep-days]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (keeping) return;
        keeping = true;
        keepTrigger.setAttribute('aria-busy', 'true');
        var days = parseInt(btn.getAttribute('data-keep-days'), 10);
        closeMenus(true);
        MM.api.post('/api/watch/keep', { media_item_id: leaving.keep_id, days: days })
          .then(function () {
            var hadFocus = els.keep.contains(document.activeElement);
            var original = document.createDocumentFragment();
            while (els.keep.firstChild) original.appendChild(els.keep.firstChild);
            var done = document.createElement('span');
            done.className = 'player-pill';
            done.textContent = 'Kept for ' + days + ' days';
            els.keep.appendChild(done);
            if (hadFocus) { done.tabIndex = -1; done.focus(); }
            if (window.UIFeedback) {
              window.UIFeedback.toast('Kept for ' + days + ' days', {
                variant: 'success',
                action: { label: 'Undo', onClick: function () { undoKeep(done, original, days); } }
              });
            }
          })
          .catch(function (err) {
            keeping = false;
            keepTrigger.setAttribute('aria-busy', 'false');
            var msg = MM.api.messageOf(err, "Couldn't keep this. Try again.");
            if (window.UIFeedback) window.UIFeedback.error(msg);
          });
      });
    });
  }

  /* ---- full screen ---------------------------------------------------- */

  function fullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function toggleFullscreen() {
    if (fullscreenElement()) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      return;
    }
    var enter = root.requestFullscreen || root.webkitRequestFullscreen;
    if (enter) {
      var p = enter.call(root);
      if (p && p.catch) p.catch(function () {});
    } else if (video.webkitEnterFullscreen) {
      /* iPhone: only the video element itself can go full screen. */
      video.webkitEnterFullscreen();
    }
  }

  function showFullscreen() {
    var on = !!fullscreenElement();
    els.fullscreen.setAttribute('aria-label', on ? 'Exit full screen' : 'Full screen');
    els.fullscreenLabel.textContent = on ? 'Exit full screen' : 'Full screen';
  }

  els.fullscreen.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', showFullscreen);
  document.addEventListener('webkitfullscreenchange', showFullscreen);

  /* ---- controls hiding ------------------------------------------------ */

  var idleTimer = null;

  function showControls() {
    root.classList.remove('is-idle');
    scheduleIdle();
  }

  /* Keyboard focus on a control keeps the controls up; mouse focus does not. */
  function keyboardInChrome() {
    var a = document.activeElement;
    if (!a || a === video || !root.contains(a)) return false;
    try { return a.matches(':focus-visible'); } catch (e) { return false; }
  }

  function scheduleIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      if (!video.paused && !dragging && !menuOpen() && els.message.hidden && !keyboardInChrome()) {
        root.classList.add('is-idle');
      }
    }, IDLE_MS);
  }

  var lastPointer = 'mouse';
  root.addEventListener('pointerdown', function (e) { lastPointer = e.pointerType || 'mouse'; });
  root.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'mouse') showControls();
  });
  /* Keyboard focus must never land on controls that cannot be seen. */
  root.addEventListener('focusin', showControls);

  /* A click on the picture plays or pauses; a tap shows or hides the controls. */
  video.addEventListener('click', function () {
    if (lastPointer === 'mouse') {
      togglePlay();
      showControls();
    } else if (root.classList.contains('is-idle')) {
      showControls();
    } else if (!video.paused) {
      clearTimeout(idleTimer);
      root.classList.add('is-idle');
    }
  });

  /* ---- keyboard ------------------------------------------------------- */

  document.addEventListener('keydown', function (e) {
    showControls();
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = e.target && e.target.tagName;
    var onControl = tag === 'BUTTON' || tag === 'A' || tag === 'INPUT';
    if (e.key === 'Escape') { closeMenus(true); return; }
    if (menuOpen()) {
      if (e.key === 'Tab') { closeMenus(true); return; }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Home' || e.key === 'End') {
        e.preventDefault();
        moveInMenu(e.key);
        return;
      }
    }
    if (e.key === ' ' || e.key === 'Spacebar') {
      if (onControl) return;
      e.preventDefault();
      togglePlay();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      if (tag === 'INPUT' || menuOpen()) return;
      e.preventDefault();
      skip(e.key === 'ArrowLeft' ? -SKIP_S : SKIP_S);
    } else if (e.key === 'f' || e.key === 'F') {
      if (tag === 'INPUT') return;
      e.preventDefault();
      toggleFullscreen();
    } else {
      return;
    }
    showControls();
  });

  /* ---- next episode --------------------------------------------------- */

  var countdownTimer = null;

  function hideUpnext() {
    clearInterval(countdownTimer);
    countdownTimer = null;
    if (els.upnext) els.upnext.hidden = true;
  }

  function startCountdown() {
    var left = COUNTDOWN_S;
    els.countdown.textContent = String(left);
    els.upnext.hidden = false;
    showControls();
    clearInterval(countdownTimer);
    countdownTimer = setInterval(function () {
      left--;
      els.countdown.textContent = String(Math.max(left, 0));
      if (left <= 0) {
        hideUpnext();
        window.location.href = next.href;
      }
    }, 1000);
  }

  if (els.upnext) {
    q('[data-upnext-play]').addEventListener('click', function () { window.location.href = next.href; });
    q('[data-upnext-cancel]').addEventListener('click', hideUpnext);
  }

  /* ---- leaving the page ----------------------------------------------- */

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') beaconProgress();
  });

  window.addEventListener('pagehide', function () {
    var resumeAt = position();
    beaconStop();
    pendingStart = resumeAt;
    streamId = null;
    generation++;
    destroyHls();
  });

  /* Back from the browser's page cache: the old stream was stopped. */
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    wantPlaying = false;
    startStream(pendingStart || 0);
  });

  els.retry.addEventListener('click', function () {
    mediaRecoverUsed = false;
    fatalRestartUsed = false;
    goneRestarts = 0;
    wantPlaying = true;
    restartStream(true);
  });

  /* ---- start ---------------------------------------------------------- */

  showQuality();
  showVolume();
  showPlayState();
  render();

  if (typeof Hls === 'undefined' || !Hls.isSupported()) {
    showMessage(MSG_UNSUPPORTED, false);
    return;
  }
  startStream(startS);
})();
