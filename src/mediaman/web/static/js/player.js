/**
 * player.js — the Watch player page (watch_player.html).
 *
 * Plays an HLS stream that mediaman proxies from Plex, always through
 * hls.js. Safari's own HLS player is never used: its requests carry a
 * different User-Agent, which fails the session check and signs the
 * person out.
 *
 * Requests:
 *   POST /api/watch/stream            {rating_key, quality, page, asked_s} → {stream_id,
 *                                     playlist, quality, qualities, previews, markers, chapters}
 *   GET  <playlist> and its segments  (made by hls.js)
 *   GET  /api/watch/stream/<id>/preview/<n>  (a frame for the play bar)
 *   POST /api/watch/progress          {stream_id, position_s, played_s}
 *   POST /api/watch/stream/<id>/stop  {position_s, played_s} or nothing to save (a beacon,
 *                                     or a fetch where the next episode waits for it)
 *   POST /api/watch/keep              {media_item_id, days}
 *   POST /api/watch/ratings/ask/<key>  (at the end, its place saved: whether to ask for a rating)
 *   GET  /healthz                     (whether mediaman is reached where something went unanswered)
 *
 * A 410 from any stream URL means the server has dropped the stream (a
 * long pause, a restart): a new one is started at the current position,
 * which is also how a quality change works. A title plays where its person
 * last asked for it: every start says which page asks and how long ago the
 * person pressed for it there, and the server starts none asked for before
 * another page's. A 410 that says the stream was replaced by another page
 * means that the person plays the title there now: this player stops until
 * they press Play here. What answers 429 is asked again when it
 * says. If the server refuses the stream of a new quality, the quality that
 * was playing comes back.
 * Automatic steps the quality down when playback keeps stalling, and
 * slowly up again when it no longer does.
 * While a new stream replaces the old one, the last frame stays on screen.
 * A connection that drops, with an error or in silence, is tried again for a
 * while before it is an error.
 *
 * The next episode takes over in this same page, from what its own page
 * (GET /watch/play/<key>) says, so that full screen, picture in picture and
 * sound carry on. A page that is no player's is opened instead.
 */
(function () {
  'use strict';

  var root = document.getElementById('player');
  if (!root) return;

  /* The three choices of an item without media details, as rows. */
  var BASIC_IDS = { best: '1080p-8000', medium: '720p-4000', low: '480p-1500' };
  /* The best conversion Automatic starts with. */
  var AUTO_HEIGHT = 1080;
  var AUTO_KBPS = 8000;
  var QUALITY_KEY = 'mediaman.watch.quality';
  var VOLUME_KEY = 'mediaman.watch.volume';
  var MUTED_KEY = 'mediaman.watch.muted';
  var IDLE_MS = 3000;
  var SAVE_EVERY_MS = 10000;
  var SKIP_S = 10;
  /* The video goes where presses in quick succession lead this long after the last. */
  var LAND_MS = 250;
  var PAGE_S = 60;
  var AUTOPLAY_KEY = 'mediaman.watch.autoplay';
  var COUNTDOWN_S = 10;
  var DAY_MS = 24 * 60 * 60 * 1000;
  /* "Are you still watching?": after this many episodes that began by
     themselves, or this long without a sign of anybody. */
  var SITTING_KEY = 'mediaman.watch.sitting';
  var ASK_AFTER_EPISODES = 3;
  var ASK_AFTER_MS = 2 * 60 * 60 * 1000;
  /* How long the next episode waits for its page, and for the old stream to stop. */
  var PAGE_WAIT_MS = 15000;
  var AGAIN_MS = 2000;       // a step back not begun by then may be asked for again
  var STOP_WAIT_MS = 5000;
  /* The longest a title's end waits to hear whether to ask for a rating. */
  var RATE_WAIT_MS = 4000;
  /* A rating just given stays in sight this long. */
  var RATED_MS = 1800;
  /* Left alone this long, the question lets the page go on. */
  var RATE_IGNORED_MS = 30000;
  /* How long a start may go without an answer: on the server, with each of its calls to
     a slow Plex at its limit, one takes up to 110 s. */
  var START_WAIT_MS = 120000;
  /* Restarts that playback must outlast before more are allowed. */
  var CALM_PLAYED_S = 30;
  var MAX_GONE_RESTARTS = 2;
  var MAX_REVERTS = 2;
  /* A lost connection is tried again with waits that double, until there has been
     nothing to play for this long. */
  var LOST_FIRST_MS = 1000;
  var LOST_MAX_MS = 8000;
  var LOST_FOR_MS = 60000;
  /* And where the server refuses, or refuses and is lost by turns, for this long. */
  var NONE_FOR_MS = 2 * LOST_FOR_MS;
  /* How long a light request may go unanswered before the connection counts as lost. It needs
     neither Plex nor, sent with no session, the database. */
  var LIGHT_URL = '/healthz';
  var LIGHT_WAIT_MS = 5000;
  /* Under the message about it the server is asked for this often: it may come back with no word
     from the browser. */
  var LIGHT_EVERY_MS = 30000;
  /* A server found again so, with nothing played since, is tried this often before the message
     stays for a press: each time it comes up it takes the focus and is said. */
  var BACK_ROUNDS = 3;
  /* How long the picture waits before it is said why: a drop, and a busy server or a slow start. */
  var LOST_NOTICE_MS = 300;
  var SLOW_NOTICE_MS = 8000;
  /* The longest a busy server may ask for before it is asked again. */
  var BUSY_MAX_S = 30;
  /* Automatic steps down when playback stalls twice within this much of the
     video, or once for this long. */
  var STALL_WINDOW_S = 60;
  var STALL_MS = 10000;
  /* It steps up after this much of the video without a stall; a step up that
     stalls this soon doubles the wait for the next one. */
  var UP_AFTER_S = 300;
  var UP_AFTER_MAX_S = 3600;
  var UP_TRIAL_S = 120;
  /* Not worth a new stream this close to the end. */
  var UP_LEFT_S = 120;
  /* While the pointer moves a frame preview is asked for this often, and this many are kept. */
  var FRAME_EVERY_MS = 300;
  var FRAMES_KEPT = 60;
  /* The longest the last frame stays for the first one of the new stream. */
  var SEEN_WAIT_MS = 1000;
  /* A video may be a little longer than its frames. */
  var LAST_FRAMES = 4;
  var FRAME_BACKOFF_MS = 2000;
  var FRAME_BACKOFF_MAX_MS = 30000;
  /* How close the preview may come to the edge of the window. */
  var BUBBLE_EDGE = 12;
  /* How long it shows where a key on the bar led. */
  var PEEK_MS = 1000;
  /* Chapter gaps on the bar stay at least this many pixels apart. */
  var GAP_APART_PX = 8;
  /* The cut at a band's end is this wide, as in the CSS. */
  var BAND_CUT_PX = 2;
  /* The skip button stays this long; after that it comes and goes with the controls. */
  var SKIP_SHOWN_MS = 10000;
  var MARKER_NAMES = { intro: 'Intro', credits: 'Credits' };
  var MSG_UNSUPPORTED = "This browser can't play video here. On iPhone and iPad, iOS 17.1 or later is needed.";
  var MSG_FAILED = "Can't play this right now.";
  var MSG_LOST = "Can't reach mediaman. Check your connection.";
  var MSG_BUSY = 'The server is busy right now. Try again in a moment.';
  var NOTICE_LOST = 'Connection lost. Trying again…';
  var NOTICE_BUSY = 'The server is busy. Trying again…';
  var NOTICE_STARTING = 'Still starting…';
  var MSG_REPLACED = 'Playing somewhere else.';
  var MSG_NEXT_UNREACHED = "Can't reach the next episode right now.";
  var MSG_NEXT_UNKNOWN = "Can't find the next episode right now.";
  /* Start failures by the server's error code; `retry` is whether Try again can help. */
  var START_ERRORS = {
    unreachable: { text: "Can't reach Plex right now.", retry: true },
    not_found: { text: 'No longer available.', retry: false },
    not_set_up: { text: "Plex isn't set up yet.", retry: false },
    too_many_streams: { text: 'You have too many videos playing. Close one and try again.', retry: true },
    rate_limited: { text: 'Too many tries. Wait a moment and try again.', retry: true },
  };
  /* Only a fault of the server's or of its answer is worth one more try. */
  var RETRY_STATUS = { 0: 1, 500: 1, 502: 1, 504: 1 };

  function readJSON(el, name) {
    try { return JSON.parse(el.getAttribute(name) || 'null'); } catch (e) { return null; }
  }

  /* What a player page says about its video, from its #player element. */
  var item;
  var rows;                  // the qualities its menu offers, best first
  var next;
  var nextUnknown;           // Plex could not say whether an episode follows
  var leaving;
  var backHref;
  var cameFrom;              // the page the history's last entry holds, when it opened the player
  var showHref;
  var startS;
  var placeS;                // the place last known to be saved by other than this tab

  function readPage(el) {
    item = readJSON(el, 'data-item') || {};
    rows = readJSON(el, 'data-qualities') || [];
    leaving = readJSON(el, 'data-leaving');
    backHref = el.getAttribute('data-back-href') || '/watch';
    showHref = el.getAttribute('data-show-href') || '';
    startS = parseFloat(el.getAttribute('data-start-s')) || 0;
    placeS = parseFloat(el.getAttribute('data-place-s'));
    readNext(el);
  }

  function readNext(el) {
    next = readJSON(el, 'data-next');
    nextUnknown = el.getAttribute('data-next-state') === 'unknown';
  }

  readPage(root);

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
    seekHover: q('[data-seek-hover]'),
    bubble: q('[data-bubble]'),
    marks: q('[data-marks]'),
    chapters: q('[data-chapters]'),
    chapterBtn: q('[data-chapter-btn]'),
    chapterName: q('[data-chapter-name]'),
    chapterMenu: q('[data-chapter-menu]'),
    bubbleText: q('[data-bubble-text]'),
    bubbleTitle: q('[data-bubble-title]'),
    bubbleTime: q('[data-bubble-time]'),
    frame: q('[data-frame]'),
    frameImg: q('[data-frame-img]'),
    elapsed: q('[data-elapsed]'),
    remaining: q('[data-remaining]'),
    skip: q('[data-skip]'),
    skipText: q('[data-skip-text]'),
    qualityBtn: q('[data-quality-btn]'),
    qualityMenu: q('[data-quality-menu]'),
    fullscreen: q('[data-fullscreen]'),
    upnextStatus: q('[data-upnext-status]'),
    rate: q('[data-rate]'),
    rateTitle: q('[data-rate-title]'),
    rateSet: q('[data-rate-set]'),
    rateStatus: q('[data-rate-status]'),
    ask: q('[data-ask]'),
    lost: q('[data-lost]'),
    message: q('[data-message]'),
    messageText: q('[data-message-text]'),
    retry: q('[data-retry]'),
  };

  video.playsInline = true;

  /* ---- coming and going ------------------------------------------------ */

  var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function shown(el) { return !el.hidden && !el.classList.contains('is-leaving'); }

  function show(el) {
    el.classList.remove('is-leaving');
    el.inert = false;
    el.hidden = false;
  }

  function hide(el) {
    show(el);
    el.hidden = true;
  }

  /* Out of reach at once; it hides when its exit has played. */
  function leave(el) {
    if (!shown(el)) return;
    if (reducedMotion.matches) {
      el.hidden = true;
      return;
    }
    el.inert = true;
    el.classList.add('is-leaving');
  }

  ['animationend', 'animationcancel'].forEach(function (name) {
    root.addEventListener(name, function (e) {
      /* Shown again meanwhile, it stays: that too cuts its exit short. */
      if (e.animationName === 'player-drop' && e.target.classList.contains('is-leaving')) hide(e.target);
    });
  });

  /* ---- state ---------------------------------------------------------- */

  var hls = null;
  var streamId = null;
  var generation = 0;        // bumps on every new stream; stale replies are dropped
  var starting = false;
  /* This page's name among the person's players. No secret: it opens nothing. */
  var pageId = Array.prototype.map.call(window.crypto.getRandomValues(new Uint8Array(12)), function (b) {
    return (b < 16 ? '0' : '') + b.toString(16);
  }).join('');
  var pressedAt = Date.now(); // when the person last pressed for the video here
  function pressedPlay() { pressedAt = Date.now(); }
  var noneSince = null;      // the first refusal since a request was taken, with nothing left to play
  var stalled = false;       // the buffer ran dry and playback has not gone on since
  var startedAt = null;      // when the stream that has not played yet was asked for
  var waitingSince = null;   // when the picture began to wait
  var bytesAt = -Infinity;   // when a segment last brought any
  var switching = false;     // the next episode's page is on its way
  var swapping = false;      // the old stream is torn down; its pause event is not the person's
  var dragging = false;
  var countdownTimer = null; // runs while the next-episode card counts down
  var quality = readSetting(QUALITY_KEY) || 'auto';   // the person's choice
  var autoplay = readSetting(AUTOPLAY_KEY) !== 'false';

  /* What belongs to the video being watched; resetVideo sets every one. */
  var pendingStart;          // where the stream will start, until the video has loaded
  var aimS;                  // where presses have led, until the video is sent there
  var wantPlaying;
  var hasPlayed;
  var playedS;
  var savedAs;               // the last save the server took, to make none twice
  var sent;                  // the places this tab has sent to be saved
  var lastTime;
  var override;              // the quality to ask for in place of the choice, or null
  var good;                  // the choice and override that last played, or the video started with
  var streamChoice;          // those of the stream now loading or playing
  var playing;               // the quality the server says that stream has
  var streamPlayed;
  var originalPlayed;
  var swapNotice;            // toast to show once the stream that takes over plays
  var rolling;               // playback has moved since the last start, seek or stall
  var autoTop;               // the best quality Automatic may step up to
  var stalls;
  var stalledAt;             // the three below are in seconds played, not wall time
  var steppedAt;
  var steppedUpAt;
  var upAfter;
  var previews;              // {interval_s, count} of this stream's frames, or null
  var frames;                // object URLs of the frames kept, by number
  var frameOrder;            // their numbers, the one shown last at the end
  var missing;               // the numbers this stream has none for
  var missingInRow;
  var markers;               // the intro and the credits, as {kind, start_s, end_s, final}
  var chapters;              // {title, start_s}, in order
  var chapterShown;          // the one the chapters button names
  var skipMarker;            // the marker the skip button now offers to skip, or null
  var troubleAtPlayed;
  var mediaRecoverUsed;
  var fatalRestartUsed;
  var goneRestarts;
  var reverts;

  function resetVideo(at) {
    pendingStart = at;
    dropAim();
    wantPlaying = true;
    hasPlayed = false;
    playedS = 0;
    savedAs = null;
    sent = {};
    lastTime = null;
    override = null;
    good = choice();
    streamChoice = null;
    playing = null;
    streamPlayed = false;
    originalPlayed = false;
    swapNotice = null;
    stopRolling();
    autoTop = null;
    stalls = 0;
    stalledAt = -Infinity;
    steppedAt = 0;
    steppedUpAt = -Infinity;
    upAfter = UP_AFTER_S;
    takePreviews(null);
    dropFrames();
    markers = [];
    chapters = [];
    buildChapters();
    offerSkip(null);
    troubleAtPlayed = 0;
    clearTrouble();
    clearTimeout(seekSaveTimer);
    hideUpnext();
  }

  /* Every way of getting over a failing stream may be tried again. */
  function clearTrouble() {
    mediaRecoverUsed = false;
    fatalRestartUsed = false;
    goneRestarts = 0;
    reverts = 0;
  }

  resetVideo(startS);

  function duration() {
    if (item.duration_s > 0) return item.duration_s;
    return isFinite(video.duration) ? video.duration : 0;
  }

  function position() {
    if (aimS !== null) return aimS;
    return pendingStart !== null ? pendingStart : video.currentTime;
  }

  /* An icon control's name, and the same with its key as a tooltip. */
  function setName(el, label, key) {
    el.setAttribute('aria-label', label);
    el.setAttribute('title', key ? label + ' (' + key + ')' : label);
  }

  /* ---- saved settings ------------------------------------------------- */

  function readSetting(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; /* storage blocked */ }
  }

  function saveSetting(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage blocked */ }
  }

  function readVolume() {
    var saved = parseFloat(readSetting(VOLUME_KEY));
    if (saved >= 0 && saved <= 1) video.volume = saved;
    video.muted = readSetting(MUTED_KEY) === 'true';
  }

  /* ---- quality -------------------------------------------------------- */

  /* What a choice of quality comes to: the person's, and what stands in for it. */
  function choice() { return { quality: quality, override: override }; }
  function asked() { return override || quality; }
  function isNew() { return good.quality !== quality || good.override !== override; }
  function mayRevert() { return isNew() && reverts < MAX_REVERTS; }

  function convertRows() {
    return rows.filter(function (r) { return r.kind === 'convert'; });
  }

  function findRow(id) {
    return rows.filter(function (r) { return r.id === id; })[0];
  }

  /* The row of a quality, or the nearest lower one, the way the server picks
     it. Undefined only while there are no rows. */
  function rowFor(id) {
    var exact = findRow(id);
    if (exact && exact.kind !== 'auto') return exact;
    var converts = convertRows();
    if (id === 'original') return converts[0];
    var parts = /^(\d+)p-(\d+)$/.exec(BASIC_IDS[id] || id) || [0, AUTO_HEIGHT, AUTO_KBPS];
    return converts.filter(function (r) {
      return r.height < +parts[1] || (r.height === +parts[1] && r.bitrate_kbps <= +parts[2]);
    })[0] || converts[converts.length - 1];
  }

  /* A row in one line: "1080p · 8 Mbps". */
  function rowText(row) { return row.label + (row.detail ? ' · ' + row.detail : ''); }

  function buildQualityMenu() {
    var hadFocus = els.qualityMenu.contains(document.activeElement);
    els.qualityMenu.textContent = '';
    var title = document.createElement('p');
    title.className = 'player-menu-title';
    title.setAttribute('aria-hidden', 'true');
    title.textContent = 'Quality';
    els.qualityMenu.appendChild(title);
    var list = addList(els.qualityMenu);
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', 'Quality');
    rows.forEach(function (row) {
      var btn = menuRow(row.label, row.detail);
      btn.setAttribute('data-quality', row.id);
      list.appendChild(btn);
    });
    if (showHref) addAutoplayRow();
    showQuality();
    fadeEnds(list);
    if (hadFocus) focusMenu(els.qualityMenu);
  }

  /* The part of a menu that scrolls. */
  function addList(menu) {
    var list = document.createElement('div');
    list.className = 'player-menu-list';
    list.addEventListener('scroll', function () { fadeEnds(list); });
    menu.appendChild(list);
    return list;
  }

  function fadeEnds(list) {
    list.classList.toggle('has-more-above', list.scrollTop > 1);
    list.classList.toggle('has-more-below', list.scrollTop + list.clientHeight < list.scrollHeight - 1);
  }

  function addAutoplayRow() {
    var rule = document.createElement('div');
    rule.className = 'player-menu-rule';
    rule.setAttribute('role', 'separator');
    els.qualityMenu.appendChild(rule);
    var btn = menuRow('Autoplay next episode', '');
    btn.setAttribute('role', 'menuitemcheckbox');
    btn.setAttribute('data-autoplay', '');
    btn.setAttribute('aria-checked', String(autoplay));
    els.qualityMenu.appendChild(btn);
  }

  /* A choice of a menu: what it is, the detail of it, and its tick. */
  function menuRow(main, side) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'player-menu-item player-menu-item--split';
    btn.setAttribute('role', 'menuitemradio');
    addSpan(btn, 'player-menu-main', main);
    addSpan(btn, 'player-menu-side', side);
    btn.appendChild(MM.dom.icon('i-check', 'player-menu-tick'));
    return btn;
  }

  function addSpan(parent, className, text) {
    var span = document.createElement('span');
    span.className = className;
    span.textContent = text;
    parent.appendChild(span);
    /* Keeps the texts of one control apart in its spoken name. */
    parent.appendChild(document.createTextNode(' '));
  }

  /* Tick the choice, or what stands in for it, and say on the pill what plays. */
  function showQuality() {
    var now = rowFor(playing || asked());
    var auto = quality === 'auto' && findRow('auto');
    var ticked = auto || now;
    els.qualityMenu.querySelectorAll('[data-quality]').forEach(function (btn) {
      var id = btn.getAttribute('data-quality');
      btn.setAttribute('aria-checked', ticked && ticked.id === id ? 'true' : 'false');
      if (id === 'auto') btn.querySelector('.player-menu-side').textContent = auto && now ? rowText(now) : '';
    });
    var res = now ? now.height + 'p' : 'Auto';
    var before = '';
    var after = '';
    if (auto && now) before = 'Auto · ';
    else if (now && now.kind === 'original') before = 'Original · ';
    else if (now && now.label !== res) before = now.label + ' · ';
    else if (now) after = ' · ' + now.detail;
    els.qualityBtn.querySelector('[data-quality-before]').textContent = before;
    els.qualityBtn.querySelector('[data-quality-res]').textContent = res;
    els.qualityBtn.querySelector('[data-quality-after]').textContent = after;
    els.qualityBtn.setAttribute('aria-label', 'Quality: ' + before + res + after);
  }

  function setQuality(value) {
    quality = value;
    saveSetting(QUALITY_KEY, value);
  }

  function chooseQuality(value) {
    var same = value === quality && !override;
    pressedPlay();
    /* Even the same one: a saved choice the server does not know may stand in its place. */
    setQuality(value);
    var stopped = shown(els.message) && tryAgain;
    if (same) {
      if (stopped === retry || stopped === tryLost || stopped === playHere) tryAgain();
      return;
    }
    reverts = 0;
    swapNotice = null;
    override = null;
    stalls = 0;
    stopWatching();
    if (value === playing && streamId) {
      /* It already plays, as what Automatic picked. */
      streamChoice = choice();
      if (streamPlayed) good = streamChoice;
      showQuality();
      return;
    }
    if (stopped === playHere || hereGeneration === generation) playHere();
    else restartStream(true);
  }

  /* A new quality cannot play: go back to the one that did. */
  function revertQuality() {
    reverts++;
    noteTrouble();
    if (quality !== good.quality) {
      var stays = good.quality === 'original' && good.override || good.quality;
      var row = findRow(stays) || rowFor(good.override || good.quality);
      swapNotice = "Couldn't change the quality." + (row ? ' Staying on ' + rowText(row) + '.' : '');
      setQuality(good.quality);
    } else {
      /* A step of Automatic's own that failed is not tried again soon. */
      upAfter = Math.min(upAfter * 2, UP_AFTER_MAX_S);
    }
    override = good.override;
  }

  /* This browser cannot play the source as it is: the best conversion can. */
  function leaveOriginal() {
    var row = convertRows()[0];
    if (!row) { fail(); return; }
    override = row.id;
    if (good.quality === 'original' && !good.override) good = choice();
    swapNotice = "The original can't play here. Playing " + rowText(row) + ' instead.';
    restartStream(true);
  }

  /* What the server's answer to a start says about the qualities. */
  function takeQualities(data) {
    playing = data.quality || asked();
    if (asked() === 'auto' && quality === 'auto') autoTop = playing;
    if (data.qualities && data.qualities.length &&
        JSON.stringify(data.qualities) !== JSON.stringify(rows)) {
      rows = data.qualities;
      buildQualityMenu();
    } else {
      showQuality();
    }
  }

  /* ---- automatic quality ---------------------------------------------- */

  var waitTimer = null;

  function stopWatching() {
    clearTimeout(waitTimer);
    waitTimer = null;
  }

  function stopRolling() {
    rolling = false;
    stopWatching();
    stalled = false;
  }

  /* The buffer ran dry while the video was playing on its own: a seek or a
     start that has to wait says nothing about the quality. */
  function noteStall() {
    if (swapping) return;
    if (!rolling) {
      if (waitTimer === null) watchWait();
      return;
    }
    stopRolling();
    /* What was loaded before is all played. */
    if (lost) countLost();
    if (noneSince !== null) noneSince = Date.now();
    stalled = true;
    if (lost || noneSince !== null) return;
    if (quality !== 'auto' || !countSlow()) watchWait();
  }

  /* While the picture waits it is looked at every so often: a connection may be slow, or drop
     and say nothing. */
  function watchWait() {
    stopWatching();
    if (lost || noneSince !== null) return;
    waitTimer = setTimeout(lookAtWait, STALL_MS);
  }

  function lookAtWait() {
    var h = hls;
    var since = waitingSince;
    var waits = function () { return h && h === hls && since !== null && since === waitingSince && !swapping; };
    waitTimer = null;
    if (!waits()) return;
    ifReached(function () {
      if (waits() && !(stalled && stepDown())) watchWait();
    }, function () {
      if (waits()) loseLoads(h, 'segment');
    });
  }

  /* Whether this sign of a slow connection, with the last, made for a step down. */
  function countSlow() {
    stalls = playedS - stalledAt <= STALL_WINDOW_S ? stalls + 1 : 1;
    stalledAt = playedS;
    return stalls > 1 && stepDown();
  }

  function stepTo(row) {
    stalls = 0;
    steppedAt = playedS;
    override = row.id;
    restartStream(true);
  }

  /* What is left below what plays. */
  function lowerRows() {
    var converts = convertRows();
    return converts.slice(converts.indexOf(rowFor(playing)) + 1);
  }

  /* One step, or further where the connection measured slower than that. One
     that has played nothing has measured nothing: half of what is left. */
  function stepDown() {
    if (quality !== 'auto' || starting || !streamId) return false;
    var lower = lowerRows();
    if (!lower.length) return false;
    if (playedS - steppedUpAt <= UP_TRIAL_S) upAfter = Math.min(upAfter * 2, UP_AFTER_MAX_S);
    var last = lower[lower.length - 1];
    var kbps = (hls && hls.bandwidthEstimate || 0) / 1000;
    if (!streamPlayed) stepTo(lower[Math.floor((lower.length - 1) / 2)]);
    else if (!kbps) stepTo(lower[0]);
    else stepTo(lower.filter(function (r) { return r.bitrate_kbps <= kbps * 0.8; })[0] || last);
    return true;
  }

  function maybeStepUp() {
    if (quality !== 'auto' || !autoTop || starting || swapping || menuOpen()) return;
    if (playedS - Math.max(steppedAt, stalledAt) < upAfter) return;
    if (duration() - video.currentTime < UP_LEFT_S) return;
    var converts = convertRows();
    var at = converts.indexOf(rowFor(playing));
    if (at <= converts.indexOf(rowFor(autoTop))) return;
    var kbps = (hls && hls.bandwidthEstimate || 0) / 1000;
    if (kbps && kbps < converts[at - 1].bitrate_kbps * 1.5) return;
    steppedUpAt = playedS;
    stepTo(converts[at - 1]);
  }

  /* ---- messages and loading ------------------------------------------- */

  function setLoading(on) {
    root.classList.toggle('is-loading', on);
    if (!on) waitingSince = null;
    else if (waitingSince === null) waitingSince = Date.now();
    showNotice();
    showPlayState();
  }

  var heldForSeek = false;
  var heldFor = 0;           // bumps as a frame is held or let go; what waits for an older one is dropped

  /* Draw the frame on screen over the video, so tearing the stream down
     does not leave the picture black. It dims where another video follows. */
  function holdFrame(another) {
    heldFor++;
    heldForSeek = false;
    root.classList.toggle('is-switching', !!another);
    if (!shown(hold) && video.readyState >= 2 && video.videoWidth) {
      hold.width = video.videoWidth;
      hold.height = video.videoHeight;
      try { hold.getContext('2d').drawImage(video, 0, 0); } catch (e) { return; }
    } else if (hold.hidden) {
      return;
    }
    show(hold);
    root.classList.add('is-held');
  }

  function releaseFrame() {
    heldFor++;
    heldForSeek = false;
    leave(hold);
    root.classList.remove('is-held');
  }

  /* The browser says it plays before it shows any of it, and before it has come to its place:
     once a frame from there is on screen the held one fades. */
  function releaseOnceSeen() {
    var mine = ++heldFor;
    var seen = function () { if (mine === heldFor) releaseFrame(); };
    var there = function () {
      video.removeEventListener('seeked', there);
      if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(seen);
      else requestAnimationFrame(function () { requestAnimationFrame(seen); });
    };
    setTimeout(seen, SEEN_WAIT_MS);
    if (video.seeking) video.addEventListener('seeked', there);
    else there();
  }

  /* `again` is what Try again does; without it there is no such button. The last
     frame stays under a message that the video can go on from. */
  var tryAgain = null;
  function showMessage(text, again) {
    tryAgain = again;
    swapping = false;
    if (again !== tryLost && again !== playHere) releaseFrame();
    if (shown(els.rate)) dropRating();
    setLoading(false);
    els.messageText.textContent = text;
    els.retry.hidden = !again;
    els.retry.textContent = again === playHere ? 'Play here' : 'Try again';
    els.play.disabled = !again;
    show(els.message);
    showPlayState();
    showSkip();
    swapNotice = null;
    showControls();
    (again ? els.retry : els.message.querySelector('a')).focus();
  }

  function hideMessage() {
    els.play.disabled = false;
    if (!shown(els.message)) return;
    if (els.message.contains(document.activeElement)) els.play.focus();
    leave(els.message);
    showPlayState();
  }

  /* ---- a lost connection ----------------------------------------------- */

  /* While mediaman cannot be reached: since when (`at`), what asks `again`, what
     tidies up once it `gaveUp`, the next `wait`, and whether the message is up
     (`over`). What asks again drops a try still under way; one that is better left
     under way has a `look` instead, which the timer makes. */
  var lost = null;
  var backRounds = 0;        // times the server was found again with nothing played since

  function waitOut(again, gaveUp, look) {
    if (!lost) {
      lost = { at: Date.now(), gaveUp: gaveUp, wait: LOST_FIRST_MS, over: false, tryTimer: null };
      countLost();
      showNotice();
    }
    stopWatching();
    if (lost.over) return;
    lost.again = again;
    clearTimeout(lost.tryTimer);
    lost.tryTimer = setTimeout(function () {
      lost.tryTimer = null;
      if (look && !lostLong()) look();
      else tryUnattended();
    }, lost.wait);
    lost.wait = Math.min(lost.wait * 2, LOST_MAX_MS);
  }

  function askAgain() {
    clearTimeout(lost.tryTimer);
    lost.tryTimer = null;
    lost.again();
  }

  /* Whether nobody may be there any more: timers stand still while a computer sleeps. */
  function lostLong() { return Date.now() - lost.at >= ASK_AFTER_MS; }

  function tryUnattended() {
    if (lostLong()) giveUp();
    else askAgain();
  }

  /* The person moved, in a drop. */
  function tryAtOnce() {
    if (!lost || lost.over) return;
    lost.at = Date.now();
    lost.wait = LOST_FIRST_MS;
    askAgain();
  }

  /* Something went unanswered: a server that answers a light request is only slow. Neither
     is called where a drop was found meanwhile and is being tried again. */
  function ifReached(yes, no) {
    var asked = Date.now();
    askLight(function (answered) {
      if (lost && (lost.over || lost.tryTimer !== null)) return;
      if (!answered && bytesAt < asked) { no(); return; }
      reached();
      yes();
    });
  }

  function askLight(then) {
    var abort = new AbortController();
    var timer = setTimeout(function () { abort.abort(); }, LIGHT_WAIT_MS);
    /* A login page in front of mediaman may answer with its address: an answer too. */
    fetch(LIGHT_URL, { credentials: 'omit', cache: 'no-store', redirect: 'manual', signal: abort.signal })
      .then(function (resp) { return !fromProxy(resp.status); }, function () { return false; })
      .then(function (answered) {
        clearTimeout(timer);
        then(answered);
      });
  }

  /* While `open` says so nothing has come: the server may be slow, or the connection lost. */
  function watchSlow(open, unreached) {
    setTimeout(function () {
      if (!open()) return;
      ifReached(function () { watchSlow(open, unreached); }, function () { if (open()) unreached(); });
    }, SLOW_NOTICE_MS);
  }

  /* What was loaded before may still play for long. */
  function countLost() {
    clearTimeout(lost.endTimer);
    lost.endTimer = setTimeout(function () {
      if (rolling) countLost();
      else giveUp();
    }, LOST_FOR_MS);
  }

  function giveUp() {
    clearTimeout(lost.tryTimer);
    clearTimeout(lost.endTimer);
    noneSince = null;
    lost.over = true;
    if (lost.gaveUp) lost.gaveUp();
    leaveNative();
    showMessage(MSG_LOST, tryLost);
    if (backRounds < BACK_ROUNDS) watchBack();
  }

  function watchBack() {
    var mine = lost;
    var again = function () { if (mine === lost && lost.over) watchBack(); };
    clearTimeout(lost.backTimer);
    lost.backTimer = setTimeout(function () {
      if (lostLong()) return;
      if (document.visibilityState === 'hidden') { again(); return; }
      askLight(function (answered) {
        if (mine !== lost || !lost.over) return;
        if (answered && document.visibilityState !== 'hidden') {
          backRounds++;
          cameBack();
        } else {
          again();
        }
      });
    }, LIGHT_EVERY_MS);
  }

  function goOnTrying() {
    clearTimeout(lost.backTimer);
    lost.over = false;
    lost.wait = LOST_FIRST_MS;
    countLost();
    hideMessage();
    setLoading(video.ended || video.readyState < 3);
    askAgain();
  }

  /* Try again. */
  function tryLost() {
    pressedPlay();
    backRounds = 0;
    lost.at = Date.now();
    wantPlaying = true;
    if (streamId && video.paused && !video.ended) play();
    goOnTrying();
  }

  function endLost() {
    if (!lost) return;
    clearTimeout(lost.tryTimer);
    clearTimeout(lost.endTimer);
    clearTimeout(lost.backTimer);
    lost = null;
    showNotice();
  }

  function reached() {
    if (!lost) return;
    if (lost.over) hideMessage();
    if (lostLong()) {
      wantPlaying = false;
      video.pause();
    }
    endLost();
    watchWait();
  }

  /* What the picture has waited for long enough to be told; asks again when it will have. */
  var noticeTimer = null;
  function noticeDue() {
    var text = null;
    var since;
    var after = SLOW_NOTICE_MS;
    if (waitingSince === null) {
      return null;
    } else if (lost) {
      if (!lost.over) text = NOTICE_LOST;
      since = waitingSince;
      after = LOST_NOTICE_MS;
    } else if (noneSince !== null) {
      text = NOTICE_BUSY;
      since = noneSince;
    } else if (startedAt !== null) {
      text = NOTICE_STARTING;
      since = startedAt;
    }
    if (!text) return null;
    var left = Math.max(since, waitingSince) + after - Date.now();
    if (left <= 0) return text;
    noticeTimer = setTimeout(showNotice, left);
    return null;
  }

  function showNotice() {
    clearTimeout(noticeTimer);
    var text = noticeDue();
    if (!text) {
      leave(els.lost);
      return;
    }
    if (shown(els.lost) && els.lost.textContent === text) return;
    els.lost.textContent = text;
    show(els.lost);
  }

  function cameBack() {
    if (!lost) return;
    if (lost.over && lostLong()) return;
    if (lost.over) goOnTrying();
    else tryUnattended();
  }

  window.addEventListener('online', cameBack);

  /* A request was taken; from now on a wait is the connection's. */
  function taken() {
    var refused = noneSince !== null;
    noneSince = null;
    if (refused) watchWait();
    showNotice();
  }

  function noneTooLong() {
    var b = video.buffered;
    for (var i = 0; i < b.length; i++) {
      /* Less is a stall too. */
      if (b.start(i) <= video.currentTime && b.end(i) - video.currentTime > 1) noneSince = null;
    }
    if (noneSince === null) noneSince = Date.now();
    showNotice();
    return Date.now() - noneSince >= NONE_FOR_MS;
  }

  /* Whether a proxy, not mediaman, answered: one in front of it says so with these. */
  function fromProxy(status) {
    return (status >= 502 && status <= 504) || status >= 520;
  }

  /* ---- streams -------------------------------------------------------- */

  function stopUrl(id) { return '/api/watch/stream/' + encodeURIComponent(id) + '/stop'; }

  function stopOnServer(id) {
    MM.api.post(stopUrl(id)).catch(function () { /* the server drops it when nothing asks for it */ });
  }

  function destroyHls() {
    if (hls) {
      var old = hls;
      hls = null;
      old.destroy();
    }
  }

  /* A start that hangs in a drop holds a connection, and presses could pile them up. */
  var lastStart = null;
  function endHungStart() {
    if (lastStart) lastStart.abort();
  }

  /* Start a stream at `pos`; one retry before the error is shown. */
  function startStream(pos) {
    var gen = ++generation;
    if (lost) endHungStart();
    endLost();
    noneSince = null;
    startedAt = Date.now();
    dropAim();
    starting = true;
    pendingStart = pos;
    lastTime = null;
    playing = null;
    streamPlayed = false;
    stopRolling();
    showQuality();
    hideMessage();
    setLoading(true);

    function gaveUp() { starting = false; }

    var attempts = 0;
    function attempt(tries) {
      var mine = ++attempts;
      var dropped = function () { return gen !== generation || mine !== attempts; };
      var again = function () {
        starting = true;
        swapping = true;
        endHungStart();
        attempt(tries);
      };
      /* The connection that dropped has not come back, or the server hangs. */
      var unanswered = function () {
        if (lost) waitOut(again, gaveUp);
        else fail();
      };
      var open = true;
      var waits = function () { return open && !dropped(); };
      /* It may still be answered: asked again only once the server is there. */
      var look = function () {
        if (!waits()) return;
        ifReached(function () { if (waits()) again(); }, function () { if (waits()) waitOut(again, gaveUp, look); });
      };
      watchSlow(waits, function () { waitOut(again, gaveUp, look); });
      var answered = function () {
        open = false;
        clearTimeout(limit);
      };
      var limit = setTimeout(function () {
        answered();
        if (dropped()) return;
        attempts++;
        unanswered();
      }, START_WAIT_MS);
      var abort = lastStart = new AbortController();
      var start = MM.api.post('/api/watch/stream', {
        rating_key: item.rating_key,
        quality: asked(),
        page: pageId,
        asked_s: Math.max(0, Date.now() - pressedAt) / 1000,
      }, { signal: abort.signal });
      start.then(answered, answered);
      return start
        .then(function (data) {
          if (dropped()) {
            if (data && data.stream_id) stopOnServer(data.stream_id);
            return;
          }
          starting = false;
          reached();
          streamId = data.stream_id;
          streamChoice = choice();
          takeQualities(data);
          takePreviews(data.previews);
          takeMarks(data);
          if (aimS !== null) land();
          attach(data.playlist, pendingStart);
        })
        .catch(function (err) {
          if (dropped()) return;
          var status = err && typeof err.status === 'number' ? err.status : 0;
          var code = err && err.error;
          if (code === 'network_error' || (fromProxy(status) && code === 'http_' + status)) {
            waitOut(again, gaveUp);
            return;
          }
          reached();
          /* Asked for by itself, after the person asked another page. */
          if (status === 410 && code === 'replaced') { streamReplaced(); return; }
          if (tries < 1 && RETRY_STATUS[status]) return attempt(tries + 1);
          if (status === 422 && quality !== 'auto' && !hasPlayed) {
            /* A saved choice the server does not know. It stays saved. */
            quality = 'auto';
            good = choice();
            showQuality();
            return attempt(tries);
          }
          var known = err && START_ERRORS[err.error];
          if (mayRevert() && (!known || known.retry)) {
            /* The old stream is already stopped. */
            revertQuality();
            return startStream(position());
          }
          starting = false;
          showMessage(known ? known.text : MSG_FAILED, known && !known.retry ? null : retry);
        });
    }
    return attempt(0);
  }

  var playlistUrl;

  function attach(playlist, pos) {
    destroyHls();
    playlistUrl = playlist;
    var h = new Hls({
      enableWorker: false,
      startPosition: pos,
      maxBufferLength: 30,
      preferManagedMediaSource: true,
      xhrSetup: function (xhr, url) {
        if (!/\/seg\//.test(url)) return;
        xhr.addEventListener('readystatechange', function () {
          if (h !== hls || xhr.status !== 200) return;
          bytesAt = Date.now();
          if (xhr.readyState === 2) taken();
        });
      },
    });
    hls = h;
    var waits = true;        // for the playlist
    watchSlow(function () { return waits && h === hls; }, function () { loseLoads(h, 'manifest'); });
    h.on(Hls.Events.ERROR, function (event, data) {
      waits = false;
      if (h === hls) onHlsError(data);
    });
    h.on(Hls.Events.MANIFEST_PARSED, function () {
      waits = false;
      if (h !== hls) return;
      reached();
      swapping = false;
      if (wantPlaying) play();
      else setLoading(false);
    });
    h.on(Hls.Events.FRAG_LOADED, function () {
      if (h !== hls) return;
      taken();
      reached();
    });
    h.loadSource(playlist);
    h.attachMedia(video);
  }

  /* hls.js has stopped at an error of this stream's that may pass: go on loading. */
  function loadAgain(h, details) {
    if (h !== hls) return;
    if (/^manifest/.test(details)) attach(playlistUrl, position());
    else h.startLoad(pendingStart === null ? -1 : pendingStart);
  }

  /* Nothing is heard of what it loads: stop, and ask again later. */
  function loseLoads(h, details) {
    var id = streamId;
    h.stopLoad();
    /* A try for the playlist is a new hls.js. */
    waitOut(function () { if (id === streamId) loadAgain(hls, details); });
  }

  /* hls.js empties the video and fills it again, but says nothing of its place or of play. */
  function recoverMedia() {
    var h = hls;
    pendingStart = position();
    dropAim();
    lastTime = null;
    swapping = true;
    stopRolling();
    holdFrame();
    setLoading(true);
    video.addEventListener('loadedmetadata', function goOn() {
      video.removeEventListener('loadedmetadata', goOn);
      if (h !== hls) return;
      swapping = false;
      pendingStart = position();
      dropAim();
      video.currentTime = pendingStart;
      if (wantPlaying) play();
      else setLoading(false);
    });
    h.recoverMediaError();
  }

  /* Drop the current stream and start a new one where playback stands.
     `force` is for a quality change, which may replace a stream that is
     still starting. */
  function restartStream(force) {
    if (switching || (starting && !force)) return;
    var pos = position();
    /* A beacon, since the page may be on its way out. */
    if (streamId) beacon(stopUrl(streamId), unsaved());
    streamId = null;
    swapping = true;
    hideUpnext();
    holdFrame();
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

  /* A 410 of the stream's. Replaced by a start of this page's own, one that reached the
     server late, it goes on as after any other. */
  function streamEnded(answer) {
    if (answer && answer.error === 'replaced' && answer.by !== pageId) streamReplaced();
    else streamGone();
  }

  /* The same title was started somewhere else. This stream's place is no longer
     the one to save. */
  function streamReplaced() {
    wantPlaying = false;
    swapping = true;
    leaveNative();
    hideUpnext();
    holdFrame();
    pendingStart = position();
    streamId = null;
    release();
    showMessage(MSG_REPLACED, playHere);
  }

  /* Go on from where the other one got to, where it has saved that. */
  var hereGeneration = null; // the generation that is asking it
  function playHere() {
    var gen = hereGeneration = ++generation;
    pressedPlay();
    swapping = true;         // a press starts nothing from this place meanwhile
    hideMessage();
    setLoading(true);
    fetchPage(window.location.href)
      .then(function (page) {
        var el = page.getElementById('player');
        var place = el ? parseFloat(el.getAttribute('data-place-s')) : NaN;
        if (gen !== generation || !(place >= 0) || place === placeS || sent[place]) return;
        placeS = place;
        /* Ended there with nothing to follow, it would end here at once and leave the player. */
        if (!next && !nextUnknown && place >= duration() - 1) return;
        pendingStart = within(place);
      })
      .catch(function () { /* from where this one stands */ })
      .then(function () { if (gen === generation) startOver(); });
  }

  /* The server's own failed answer; null where it is none of its own. */
  function answerOf(data) {
    var xhr = data.networkDetails;
    try {
      var text = xhr.responseType === 'arraybuffer' ? new TextDecoder().decode(xhr.response) : xhr.responseText;
      return JSON.parse(text);
    } catch (e) {
      return null;
    }
  }

  function codeOf(data) {
    var answer = answerOf(data);
    return answer && answer.error || null;
  }

  function retryAfterMs(data) {
    var s = parseInt(data.networkDetails.getResponseHeader('Retry-After'), 10);
    return Math.min(s > 0 ? s : 1, BUSY_MAX_S) * 1000;
  }

  function onHlsError(data) {
    var h = hls;
    var status = data.response ? data.response.code : undefined;
    if (status === 410) {
      streamEnded(answerOf(data));
      return;
    }
    /* hls.js would ask for a refused segment again at once, six times over, before it
       calls the error fatal; for one the network lost, after waits of its own. */
    if (status === 429) {
      reached();
      stopWatching();
      if (noneTooLong()) { fail(MSG_BUSY); return; }
      h.stopLoad();
      setTimeout(function () { loadAgain(h, data.details); }, retryAfterMs(data));
      return;
    }
    /* Leaving the page cancels its requests too, before it says that it is left. */
    if (status === 0 || (fromProxy(status) && !codeOf(data))) {
      loseLoads(h, data.details);
      return;
    }
    if (/TimeOut$/.test(data.details)) {
      ifReached(function () {
        if (h !== hls) return;
        /* Also how a connection too slow to start at all shows. */
        if (quality === 'auto' && data.details === Hls.ErrorDetails.FRAG_LOAD_TIMEOUT && countSlow()) return;
        if (data.fatal) fatalError(data);
      }, function () {
        if (h === hls) loseLoads(h, data.details);
      });
      return;
    }
    if (data.fatal) fatalError(data);
  }

  function fatalError(data) {
    /* An original that has played is one this browser can play: only the picture
       itself failing says otherwise. */
    var unplayable = data.type === Hls.ErrorTypes.MEDIA_ERROR || !originalPlayed;
    if (playing === 'original' && !streamPlayed && unplayable) { leaveOriginal(); return; }
    /* Nothing above can play where the lowest step of Automatic's could not. */
    var lost = quality === 'auto' && !streamPlayed && !lowerRows().length;
    if (mayRevert() && !lost) {
      revertQuality();
      restartStream(true);
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecoverUsed) {
      mediaRecoverUsed = true;
      noteTrouble();
      recoverMedia();
      return;
    }
    if (!fatalRestartUsed) {
      fatalRestartUsed = true;
      noteTrouble();
      restartStream(false);
      return;
    }
    if (playing === 'original') { leaveOriginal(); return; }
    fail();
  }

  /* Streams let go of that the server has not said it stopped, with the place to save. */
  var unstopped = [];

  function sendStop(old) {
    return MM.api.post(stopUrl(old.id), old.body).then(function () {
      unstopped = unstopped.filter(function (s) { return s !== old; });
    }).catch(function () { /* sent again later */ });
  }

  /* Let go of the stream, its place saved, or `at`, and of any the server has
     missed. Settles when it is done with them, or has had long enough. */
  function release(at) {
    if (streamId) unstopped.push({ id: streamId, key: item.rating_key, body: unsaved(at) });
    pendingStart = position();
    streamId = null;
    generation++;
    starting = false;
    startedAt = null;
    endLost();
    dropAim();
    stopRolling();
    destroyHls();
    return Promise.race([
      Promise.all(unstopped.map(sendStop)),
      new Promise(function (resolve) { setTimeout(resolve, STOP_WAIT_MS); }),
    ]);
  }

  function fail(text) {
    release();
    showMessage(text || MSG_FAILED, retry);
  }

  /* ---- progress ------------------------------------------------------- */

  /* The place to save, or null where the server has it already: saved again
     later, it could undo what the person has marked since. */
  function unsaved(at) {
    if (!streamId || !hasPlayed) return null;
    var body = { position_s: Math.round((at === undefined ? position() : at) * 10) / 10, played_s: Math.round(playedS) };
    if (JSON.stringify(body) === savedAs) return null;
    sent[body.position_s] = true;
    return body;
  }

  function withStream(body) {
    return { stream_id: streamId, position_s: body.position_s, played_s: body.played_s };
  }

  /* The last save, and those before it that are still under way; whether it was taken. */
  var saving = Promise.resolve(true);
  var endSaved;              // whether the save at the end was taken, once that is known
  function saveProgress() {
    var body = unsaved();
    if (!body) return streamId ? saving : Promise.resolve(false);
    var id = streamId;
    var before = saving;
    var mine = MM.api.post('/api/watch/progress', withStream(body)).then(function () {
      if (id === streamId) savedAs = JSON.stringify(body);
      return true;
    }).catch(function (err) {
      if (err && err.status === 410 && id === streamId) {
        streamEnded({ error: err.error, by: err.data && err.data.by });
      }
      return false;
    });
    saving = before.then(function () { return mine; });
    return saving;
  }

  function beacon(url, body) {
    if (!navigator.sendBeacon) return;
    navigator.sendBeacon(url, body && new Blob([JSON.stringify(body)], { type: 'application/json' }));
  }

  function beaconProgress() {
    var body = unsaved();
    if (!body) return;
    beacon('/api/watch/progress', withStream(body));
  }

  /* On leave one beacon both saves the last position and stops the stream,
     so the stop cannot arrive first and leave the save with a 410. */
  function beaconStop() {
    unstopped.forEach(function (old) { beacon(stopUrl(old.id), old.body); });
    unstopped = [];
    if (!streamId) return;
    beacon(stopUrl(streamId), unsaved());
  }

  var seekSaveTimer = null;
  function saveAfterSeek() {
    clearTimeout(seekSaveTimer);
    seekSaveTimer = setTimeout(saveProgress, 1000);
  }

  setInterval(function () {
    /* In a drop saves would only pile up, unanswered. */
    if (!video.paused && !video.ended && !swapping && !lost) saveProgress();
  }, SAVE_EVERY_MS);

  /* ---- playback ------------------------------------------------------- */

  var ownPlay = false;       // the play event to come is the page's own
  function play() {
    wantPlaying = true;
    ownPlay = video.paused;
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

  /* Try again after an error. */
  function retry() {
    pressedPlay();
    startOver();
  }

  function startOver() {
    unstopped.forEach(sendStop);
    clearTrouble();
    wantPlaying = true;
    restartStream(true);
  }

  /* Where the next-episode card waits only the Play button plays this one again. */
  function togglePlay(byButton) {
    if (shown(els.message)) {
      if (tryAgain) tryAgain();
      return;
    }
    if (aimS !== null) land();
    if (countdownTimer !== null) {
      /* The video is over and the countdown is what runs: stop that. */
      hideUpnext();
      return;
    }
    if (!byButton && upnextOpen()) return;
    if (swapping) {
      /* No video to pause yet: the new stream starts the way this leaves it. */
      wantPlaying = !wantPlaying;
      if (wantPlaying) pressedPlay();
      showPlayState();
      return;
    }
    if (video.paused || video.ended) {
      pressedPlay();
      if (!streamId && !starting) { wantPlaying = true; restartStream(false); return; }
      play();
    } else {
      wantPlaying = false;
      video.pause();
    }
  }

  function within(t) {
    var d = duration();
    return Math.max(0, d > 0 ? Math.min(t, d - 1) : t);
  }

  function isLoaded(t) {
    var b = video.buffered;
    for (var i = 0; i < b.length; i++) {
      if (b.start(i) <= t && t < b.end(i)) return true;
    }
    return false;
  }

  function seekTo(t) {
    pressedPlay();
    moveTo(t);
  }

  function moveTo(t) {
    t = within(t);
    dropAim();
    lastTime = null;
    hideUpnext();
    dropRating();
    if (video.readyState >= 1) {
      pendingStart = null;
      /* WebKit shows black until the place has loaded. */
      if (!isLoaded(t)) {
        holdFrame();
        heldForSeek = true;
      }
      video.currentTime = t;
    } else {
      /* Nothing loaded yet: move where the stream will start. */
      pendingStart = t;
      if (hls) {
        /* hls.js drops a startLoad that comes before the playlist. */
        hls.config.startPosition = t;
        hls.startLoad(t);
      }
      tryAtOnce();
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

  /* Presses move the bar at once and the video when they stop: a seek each would
     have Plex convert every place on the way. */
  var landTimer = null;

  function aimAt(t) {
    pressedPlay();
    aimS = within(t);
    hideUpnext();
    dropRating();
    clearTimeout(landTimer);
    landTimer = setTimeout(land, LAND_MS);
    if (hls && video.readyState < 1) hls.config.startPosition = aimS;
    render();
  }

  function land() { moveTo(aimS); }

  function dropAim() {
    clearTimeout(landTimer);
    aimS = null;
  }

  function skip(delta) { aimAt(position() + delta); }

  /* While a stream loads to play, the button shows it working, not a Pause
     that has nothing to pause yet; pressing it still means "don't play". */
  function showPlayState() {
    var busy = wantPlaying && root.classList.contains('is-loading');
    var paused = shown(els.message) || ((video.paused || video.ended) && !busy && countdownTimer === null);
    setName(els.play, paused ? 'Play' : 'Pause', 'Space');
    if (busy) els.play.setAttribute('aria-busy', 'true');
    else els.play.removeAttribute('aria-busy');
    var icon = els.play.querySelector('.ico');
    MM.dom.setIcon(icon, paused ? 'i-play' : 'i-pause');
    icon.toggleAttribute('hidden', busy);
    els.play.querySelector('.player-btn-ring').toggleAttribute('hidden', !busy);
    root.classList.toggle('is-paused', paused);
    if (paused) showControls();
    else scheduleIdle();
  }

  /* Playing again, or moving within the episode, cancels the next-episode countdown. */
  ['play', 'playing', 'seeking'].forEach(function (name) {
    video.addEventListener(name, hideUpnext);
    video.addEventListener(name, dropRating);
  });

  video.addEventListener('playing', function () {
    hasPlayed = true;
    startedAt = null;
    stopWatching();
    stalled = false;
    if (streamId) {
      good = streamChoice;
      streamPlayed = true;
      if (playing === 'original') originalPlayed = true;
      /* Its place from here on is this stream's to save. */
      unstopped.forEach(function (old) { if (old.key === item.rating_key) old.body = null; });
    }
    if (swapNotice) {
      if (window.UIFeedback) window.UIFeedback.error(swapNotice);
      swapNotice = null;
    }
    swapping = false;
    pendingStart = null;
    releaseOnceSeen();
    setLoading(false);
  });
  video.addEventListener('play', function () {
    if (!ownPlay) noteActivity();
    ownPlay = false;
    wantPlaying = true;
    showPlayState();
  });
  video.addEventListener('pause', function () {
    if (swapping) return;
    stopRolling();
    showPlayState();
    if (video.ended) return;
    noteActivity();
    wantPlaying = false;
    saveProgress();
  });
  video.addEventListener('waiting', function () {
    setLoading(true);
    noteStall();
  });
  video.addEventListener('canplay', function () {
    if (!video.paused) return;
    if (!wantPlaying && !swapping) releaseOnceSeen();
    setLoading(false);
  });
  video.addEventListener('seeking', function () {
    /* A jump from where it played; hls.js only nudges. */
    if (lastTime !== null && Math.abs(video.currentTime - lastTime) > 2) noteActivity();
    lastTime = null;
    stopRolling();
    /* What is wanted now may not be what was refused. */
    noneSince = null;
    showNotice();
    /* A message over a video that can be played is about its end. */
    if (streamId && !starting) {
      if (lost && lost.over) {
        lost.at = Date.now();
        goOnTrying();
      } else {
        hideMessage();
        tryAtOnce();
      }
    }
    /* A wait that a seek begins again may not be said again. */
    watchWait();
  });
  video.addEventListener('seeked', function () {
    settleStart();
    if (heldForSeek && !swapping) releaseOnceSeen();
    saveAfterSeek();
  });
  video.addEventListener('progress', render);
  video.addEventListener('volumechange', function () {
    if (hasPlayed) noteActivity();
    showVolume();
    saveSetting(VOLUME_KEY, video.volume);
    saveSetting(MUTED_KEY, video.muted);
  });

  video.addEventListener('timeupdate', function () {
    settleStart();
    var now = video.currentTime;
    /* Count only time that really played: a seek resets the mark, and a
       jump larger than a couple of seconds is not playback. */
    if (lastTime !== null && !video.paused && !video.seeking) {
      var delta = now - lastTime;
      if (delta > 0 && delta < 2) {
        playedS += delta;
        rolling = true;
        backRounds = 0;
      }
    }
    /* Emptied for a new stream the video stands at 0: from there to its start is no jump. */
    lastTime = pendingStart !== null ? null : now;
    if (playedS - troubleAtPlayed >= CALM_PLAYED_S) clearTrouble();
    maybeStepUp();
    render();
  });

  video.addEventListener('ended', function () {
    /* Presses that led back from the end are to be followed; ones that led on have been. */
    if (aimS !== null && aimS < duration() - 1) {
      land();
      return;
    }
    dropAim();
    endSaved = saveProgress();
    showSkip();
    atEnd();
    showPlayState();
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

  var dragS = 0;

  function render() {
    var d = duration();
    var pos = dragging ? dragS : position();
    var pct = d > 0 ? Math.min(100, (pos / d) * 100) : 0;
    els.played.style.width = pct + '%';
    els.thumb.style.left = pct + '%';
    els.elapsed.textContent = fmt(pos);
    els.remaining.textContent = '-' + fmt(d - pos);
    els.seek.setAttribute('aria-valuenow', String(Math.round(pos)));
    showSkip();
    var chapter = showChapter(pos);
    els.seek.setAttribute('aria-valuetext', fmt(pos) + ' of ' + fmt(d) + (chapter ? ', ' + chapter.title : ''));

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

  /* ---- chapters, intro and credits ------------------------------------ */

  /* They belong to the video: a later stream of it that comes without them takes none away. */
  function takeMarks(data) {
    if (!markers.length) markers = data.markers || [];
    if (!chapters.length && data.chapters && data.chapters.length > 1) {
      chapters = data.chapters;
      buildChapters();
    }
    drawMarks();
    render();
  }

  function addMark(className, fromS, toS) {
    var d = duration();
    var mark = document.createElement('span');
    mark.className = className;
    mark.style.left = (fromS / d) * 100 + '%';
    if (toS) mark.style.width = ((toS - fromS) / d) * 100 + '%';
    els.marks.appendChild(mark);
  }

  function drawMarks() {
    els.marks.textContent = '';
    var d = duration();
    if (!(d > 0)) return;
    var width = els.seek.getBoundingClientRect().width;
    var px = function (s) { return (s / d) * width; };
    markers.forEach(function (m) {
      // Where the bar itself ends, a band is not cut off.
      addMark('player-band' + (px(m.start_s) < BAND_CUT_PX ? ' is-first' : '') + (px(d - m.end_s) < BAND_CUT_PX ? ' is-last' : ''),
        m.start_s, m.end_s);
    });
    var last = 0;
    chapters.forEach(function (c) {
      var x = px(c.start_s);
      if (x - last < GAP_APART_PX || width - x < GAP_APART_PX) return;
      last = x;
      addMark('player-gap', c.start_s);
    });
  }
  window.addEventListener('resize', drawMarks);
  video.addEventListener('durationchange', drawMarks);

  /* The last of a sorted list that has started by `t`. */
  function startedBy(list, t) {
    return list.filter(function (x) { return x.start_s <= t; }).pop();
  }

  function markerAt(t) {
    var m = startedBy(markers, t);
    return m && t < m.end_s - 1 ? m : null;
  }

  function buildChapters() {
    chapterShown = null;
    els.chapters.hidden = !chapters.length;
    els.chapterMenu.textContent = '';
    var list = addList(els.chapterMenu);
    chapters.forEach(function (c, i) {
      var btn = menuRow(c.title, fmt(c.start_s));
      btn.setAttribute('data-chapter', String(i));
      list.appendChild(btn);
    });
  }

  /* Name the chapter at `t` on its button and tick it in its menu. */
  function showChapter(t) {
    var c = startedBy(chapters, t) || chapters[0];
    if (!c || c === chapterShown) return c;
    chapterShown = c;
    els.chapterName.textContent = c.title;
    setName(els.chapterBtn, 'Chapters: ' + c.title);
    var at = String(chapters.indexOf(c));
    els.chapterMenu.querySelectorAll('[data-chapter]').forEach(function (btn) {
      btn.setAttribute('aria-checked', btn.getAttribute('data-chapter') === at ? 'true' : 'false');
    });
    return c;
  }

  els.chapterMenu.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-chapter]');
    if (!btn) return;
    closeMenus(true);
    seekTo(chapters[+btn.getAttribute('data-chapter')].start_s);
  });

  /* ---- skip intro and credits ----------------------------------------- */

  var skipTimer;

  function showSkip() {
    var over = video.ended || switching || (els.ask && els.ask.open) || (els.upnext && !els.upnext.hidden);
    var m = !shown(els.message) && !over ? markerAt(position()) : null;
    if (m !== skipMarker) offerSkip(m);
  }

  function offerSkip(m) {
    skipMarker = m;
    clearTimeout(skipTimer);
    els.skip.classList.remove('is-quiet');
    if (!m && els.skip === document.activeElement) els.play.focus();
    if (!m) {
      leave(els.skip);
      return;
    }
    show(els.skip);
    var label = endsEpisode(m) ? 'Next episode' : 'Skip ' + MARKER_NAMES[m.kind].toLowerCase();
    els.skipText.textContent = label;
    els.skip.setAttribute('title', label + ' (S)');
    skipTimer = setTimeout(function () { els.skip.classList.add('is-quiet'); }, SKIP_SHOWN_MS);
  }

  /* Only the last credits lead on; a scene may follow the others. */
  function endsEpisode(m) { return !!next && m.kind === 'credits' && m.final; }

  function skipOn() {
    if (!skipMarker) return;
    if (!endsEpisode(skipMarker)) {
      seekTo(skipMarker.end_s);
      return;
    }
    /* Saved as at the end, so that the episode counts as watched. */
    goNext(false, duration());
  }
  els.skip.addEventListener('click', skipOn);

  /* ---- frame preview -------------------------------------------------- */

  function takePreviews(data) {
    previews = data && data.interval_s > 0 ? data : null;
    missing = {};
    missingInRow = 0;
  }

  var bubbleS = 0;           // the time it shows
  var peekTimer = null;
  var frameWanted = null;
  var frameShown = null;
  var frameAskedAt = -Infinity;
  var frameBusy = false;
  var frameBlocked = false;  // Plex or the server asked for a rest
  var frameBackoff = FRAME_BACKOFF_MS;
  var frameTimer = null;

  /* The frame and time under the pointer, over the bar. The bar itself and
     what it tells a screen reader stay at what is playing. */
  function pointBubble(clientX) {
    clearTimeout(peekTimer);
    showBubble(seekTimeAt(clientX), !dragging);
  }

  /* Where a key on the bar led, for a moment. */
  function peekBubble() {
    showBubble(position(), false);
    clearTimeout(peekTimer);
    peekTimer = setTimeout(hideBubble, PEEK_MS);
  }

  function showBubble(t, marked) {
    var d = duration();
    if (!(d > 0)) return;
    bubbleS = t;
    if (!shown(els.bubble)) {
      els.frameImg.hidden = true;
      frameShown = null;
    }
    show(els.bubble);
    els.seekHover.hidden = !marked;
    var chapter = startedBy(chapters, t);
    var marker = markerAt(t);
    els.bubbleTitle.textContent = chapter ? chapter.title : '';
    els.bubbleTitle.hidden = !chapter;
    els.bubbleTime.textContent = fmt(t) + (marker ? ' · ' + MARKER_NAMES[marker.kind] : '');
    wantFrame(t);
    placeBubble(t / d);
  }

  function placeBubble(frac) {
    var rect = els.seek.getBoundingClientRect();
    var half = els.bubble.offsetWidth / 2;
    var x = Math.min(frac * rect.width, window.innerWidth - BUBBLE_EDGE - half - rect.left);
    x = Math.max(x, BUBBLE_EDGE + half - rect.left);
    els.bubble.style.transform = 'translateX(' + Math.round(x - half) + 'px)';
    /* At the ends of the bar the words go on with the pointer, as far as the frame reaches. */
    var room = half - els.bubbleText.offsetWidth / 2;
    var ahead = Math.max(-room, Math.min(room, frac * rect.width - x));
    els.bubbleText.style.transform = 'translateX(' + Math.round(ahead) + 'px)';
    els.seekHover.style.left = frac * 100 + '%';
  }

  function hideBubble() {
    clearTimeout(peekTimer);
    leave(els.bubble);
    els.seekHover.hidden = true;
  }

  function wantFrame(t) {
    els.frame.hidden = !previews;
    if (!previews) return;
    frameWanted = Math.min(previews.count - 1, Math.floor(t / previews.interval_s));
    paintFrame();
    askFrame();
  }

  /* The frame wanted, or the nearest there is. */
  function paintFrame() {
    var near = frameOrder.slice().sort(function (a, b) {
      return Math.abs(a - frameWanted) - Math.abs(b - frameWanted);
    })[0];
    els.frame.classList.toggle('is-stale', near !== frameWanted && !missing[frameWanted]);
    if (near === undefined) return;
    frameOrder.splice(frameOrder.indexOf(near), 1);
    frameOrder.push(near);
    if (near === frameShown) return;
    frameShown = near;
    els.frameImg.src = frames[near];
    els.frameImg.hidden = false;
  }

  /* The first at once, then one now and again while the pointer moves, and where it rests. */
  function askFrame() {
    if (frameTimer !== null || frameBusy || haveFrame()) return;
    var wait = frameAskedAt + FRAME_EVERY_MS - Date.now();
    if (wait <= 0) loadFrame();
    else frameTimer = setTimeout(function () { frameTimer = null; loadFrame(); }, wait);
  }

  function haveFrame() { return frames[frameWanted] || missing[frameWanted]; }

  /* One request at a time, for the frame wanted by then. */
  function loadFrame() {
    if (frameBusy || frameBlocked || !shown(els.bubble) || !previews || !streamId || haveFrame()) return;
    var n = frameWanted;
    var id = streamId;
    var mine = frames;
    frameBusy = true;
    frameAskedAt = Date.now();
    fetch('/api/watch/stream/' + encodeURIComponent(id) + '/preview/' + n, { credentials: 'same-origin' })
      .then(function (resp) {
        if (resp.ok) return resp.blob().then(function (blob) { if (mine === frames) keepFrame(n, blob); });
        if (resp.status !== 404 && resp.status !== 410) throw new Error(String(resp.status));
        if (id !== streamId || !previews) return;
        /* The stream is gone, or has none: the next one may have them. */
        if (resp.status === 410) noPreviews();
        else missFrame(n);
      })
      .catch(function () {
        frameBlocked = true;
        setTimeout(function () { frameBlocked = false; loadFrame(); }, frameBackoff);
        frameBackoff = Math.min(frameBackoff * 2, FRAME_BACKOFF_MAX_MS);
      })
      .then(function () {
        frameBusy = false;
        askFrame();
      });
  }

  function keepFrame(n, blob) {
    frames[n] = URL.createObjectURL(blob);
    frameOrder.push(n);
    frameBackoff = FRAME_BACKOFF_MS;
    missingInRow = 0;
    paintFrame();
    if (frameOrder.length > FRAMES_KEPT) {
      var old = frameOrder.shift();
      URL.revokeObjectURL(frames[old]);
      delete frames[old];
    }
  }

  /* One that is missing says nothing of the others. */
  function missFrame(n) {
    missing[n] = true;
    if (n < previews.count - LAST_FRAMES) missingInRow++;
    if (missingInRow > 1) noPreviews();
    else paintFrame();
  }

  function dropFrames() {
    Object.keys(frames || {}).forEach(function (n) { URL.revokeObjectURL(frames[n]); });
    frames = {};
    frameOrder = [];
    frameShown = null;
  }

  function noPreviews() {
    previews = null;
    if (shown(els.bubble)) showBubble(bubbleS, !els.seekHover.hidden);
  }
  els.frameImg.addEventListener('error', noPreviews);

  /* ---- seeking on the bar --------------------------------------------- */

  els.seek.addEventListener('pointerdown', function (e) {
    if (e.button !== undefined && e.button !== 0) return;
    dragging = true;
    els.seek.classList.add('is-dragging');
    if (els.seek.setPointerCapture) els.seek.setPointerCapture(e.pointerId);
    dragS = seekTimeAt(e.clientX);
    render();
    pointBubble(e.clientX);
    e.preventDefault();
  });
  els.seek.addEventListener('pointermove', function (e) {
    if (!dragging && e.pointerType === 'touch') return;
    if (dragging) {
      dragS = seekTimeAt(e.clientX);
      render();
    }
    pointBubble(e.clientX);
    showControls();
  });
  els.seek.addEventListener('pointerleave', function (e) {
    if (dragging) return;
    /* WebKit says it as a click lets go of the bar, the mouse still on it. */
    if (e.pointerType === 'mouse' && els.seek.contains(document.elementFromPoint(e.clientX, e.clientY))) return;
    hideBubble();
    scheduleIdle();
  });
  function endDrag(e, commit) {
    if (!dragging) return;
    dragging = false;
    els.seek.classList.remove('is-dragging');
    /* A mouse that is still on the bar goes on showing the frame under it. */
    if (e.pointerType === 'touch' || !commit) hideBubble();
    else els.seekHover.hidden = !shown(els.bubble);
    if (commit) seekTo(seekTimeAt(e.clientX));
    else render();
    scheduleIdle();
  }
  els.seek.addEventListener('pointerup', function (e) { endDrag(e, true); });
  els.seek.addEventListener('pointercancel', function (e) { endDrag(e, false); });
  els.seek.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Home') aimAt(0);
    else if (e.key === 'End') aimAt(duration());
    else if (e.key === 'ArrowUp') skip(SKIP_S);
    else if (e.key === 'ArrowDown') skip(-SKIP_S);
    else if (e.key === 'PageUp') skip(PAGE_S);
    else if (e.key === 'PageDown') skip(-PAGE_S);
    else return;
    e.preventDefault();
    peekBubble();
  });

  /* ---- buttons -------------------------------------------------------- */

  els.play.addEventListener('click', function () { togglePlay(true); });
  els.retry.addEventListener('click', function () { tryAgain(); });
  els.back10.addEventListener('click', function () { skip(-SKIP_S); });
  els.fwd10.addEventListener('click', function () { skip(SKIP_S); });

  function showVolume() {
    var silent = video.muted || video.volume === 0;
    setName(els.mute, silent ? 'Unmute' : 'Mute');
    MM.dom.setIcon(els.mute.querySelector('.ico'), silent ? 'i-volume-xmark' : 'i-volume-high');
    var level = video.muted ? 0 : video.volume;
    els.volume.value = level;
    els.volume.style.setProperty('--fill', level * 100 + '%');
  }
  els.mute.addEventListener('click', function () {
    video.muted = !video.muted;
    if (!video.muted && video.volume === 0) video.volume = 1;
  });
  /* Dragged to nothing it mutes, at the volume there was before the drag. */
  var volumeBefore = null;
  els.volume.addEventListener('input', function () {
    if (volumeBefore === null) volumeBefore = video.volume;
    var level = parseFloat(els.volume.value);
    video.muted = level === 0;
    video.volume = level || volumeBefore;
  });
  els.volume.addEventListener('change', function () { volumeBefore = null; });

  els.qualityMenu.addEventListener('click', function (e) {
    if (e.target.closest('[data-autoplay]')) { toggleAutoplay(); return; }
    var btn = e.target.closest('[data-quality]');
    if (!btn) return;
    closeMenus(true);
    chooseQuality(btn.getAttribute('data-quality'));
  });

  /* ---- menus ---------------------------------------------------------- */

  /* Close every menu; with `refocus`, put focus back on the trigger of the one that was open. */
  function closeMenus(refocus) {
    var opened = openTrigger();
    if (!opened) return;
    opened.setAttribute('aria-expanded', 'false');
    if (refocus) opened.focus();
    leave(opened.parentNode.querySelector('.player-menu'));
  }
  function openTrigger() { return root.querySelector('[data-menu-trigger][aria-expanded="true"]'); }
  function menuOpen() { return !!openTrigger(); }

  function focusMenu(menu) {
    var first = menu.querySelector('[aria-checked="true"]') || menu.querySelector('button');
    if (first) first.focus();
  }

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

  root.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-menu-trigger]');
    if (!trigger) return;
    e.stopPropagation();
    var open = trigger.getAttribute('aria-expanded') === 'true';
    closeMenus();
    if (open) return;
    trigger.setAttribute('aria-expanded', 'true');
    var menu = trigger.parentNode.querySelector('.player-menu');
    show(menu);
    focusMenu(menu);
    var list = menu.querySelector('.player-menu-list');
    if (list) fadeEnds(list);
    showControls();
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest || !e.target.closest('.player-menu-wrap')) closeMenus();
  });

  /* ---- keep ----------------------------------------------------------- */

  function setupKeep() {
    var keep = q('[data-keep]');
    if (!keep || !leaving) return;
    var keepId = leaving.keep_id;
    var keeping = false;
    var keepTrigger = keep.querySelector('[data-menu-trigger]');
    var undoing = false;

    /* Put the pill back at once; if the server refuses, the keep stands. */
    var undoKeep = function (done, original, days) {
      if (undoing) return;
      undoing = true;
      MM.api.post('/api/watch/unkeep', { media_item_id: keepId }).then(function () {
        var active = document.activeElement;
        var moveFocus = !active || active === document.body || done.contains(active) ||
          (active.closest && active.closest('.ui-toast'));
        keep.textContent = '';
        keep.appendChild(original);
        keeping = false;
        keepTrigger.setAttribute('aria-busy', 'false');
        /* The next episode may have taken over, and the pill gone with this one. */
        if (moveFocus) (root.contains(keepTrigger) ? keepTrigger : els.play).focus();
        undoing = false;
        if (window.UIFeedback) window.UIFeedback.toast('Keep undone', { variant: 'info' });
      }).catch(function (err) {
        undoing = false;
        var msg = MM.api.messageOf(err, 'Could not undo. It stays kept for ' + days + ' days.');
        if (window.UIFeedback) window.UIFeedback.error(msg);
      });
    };

    keep.querySelectorAll('[data-keep-days]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (keeping) return;
        keeping = true;
        keepTrigger.setAttribute('aria-busy', 'true');
        var days = parseInt(btn.getAttribute('data-keep-days'), 10);
        closeMenus(true);
        MM.api.post('/api/watch/keep', { media_item_id: keepId, days: days })
          .then(function (data) {
            var hadFocus = keep.contains(document.activeElement);
            /* Cut off from the page it could not play its exit out. */
            hide(keep.querySelector('.player-menu'));
            var original = document.createDocumentFragment();
            while (keep.firstChild) original.appendChild(keep.firstChild);
            var done = document.createElement('span');
            done.className = 'player-pill';
            done.textContent = 'Kept for ' + days + ' days';
            keep.appendChild(done);
            if (hadFocus) { done.tabIndex = -1; done.focus(); }
            if (window.UIFeedback) {
              var until = new Date(Date.now() + days * DAY_MS)
                .toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
              window.UIFeedback.toast('Kept until ' + until, {
                variant: 'success',
                action: data.undo === false ? undefined
                  : { label: 'Undo', onClick: function () { undoKeep(done, original, days); } }
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
    setName(els.fullscreen, on ? 'Exit full screen' : 'Full screen', 'F');
    MM.dom.setIcon(els.fullscreen.querySelector('.ico'), on ? 'i-compress' : 'i-expand');
  }

  els.fullscreen.addEventListener('click', toggleFullscreen);
  video.addEventListener('dblclick', function () {
    if (lastPointer === 'mouse' && !startedElsewhere) toggleFullscreen();
  });
  ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (name) {
    document.addEventListener(name, function () {
      showFullscreen();
      /* Escape leaves full screen without a word to the page: it says the same there. */
      if (!fullscreenElement() && countdownTimer !== null) hideUpnext();
    });
  });

  /* ---- controls hiding ------------------------------------------------ */

  var idleTimer = null;

  function showControls() {
    root.classList.remove('is-idle');
    scheduleIdle();
    if (ignoredTimer !== null) waitForRating();
  }

  /* Whether the mouse or a finger, not the keyboard, last moved the focus.
     :focus-visible cannot say: any key pressed after a click sets it. */
  var focusByPointer = false;

  /* Keyboard focus on a control keeps the controls up; mouse focus does not. */
  function keyboardInChrome() {
    return !focusByPointer && root.contains(document.activeElement);
  }

  function scheduleIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      if (!video.paused && !dragging && !menuOpen() && !shown(els.message) && !keyboardInChrome()) goIdle();
    }, IDLE_MS);
  }

  /* A preview goes with the controls, also from under a mouse that rests on the bar. */
  function goIdle() {
    clearTimeout(idleTimer);
    root.classList.add('is-idle');
    hideBubble();
  }

  var lastPointer = 'mouse';
  root.addEventListener('pointerdown', function (e) {
    lastPointer = e.pointerType || 'mouse';
    focusByPointer = true;
  });
  root.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'mouse') showControls();
  });
  /* Keyboard focus must never land on controls that cannot be seen. */
  root.addEventListener('focusin', showControls);

  /* A click on the picture plays or pauses; a tap shows or hides the controls.
     With a menu open either only closes it; at the end of the video neither
     starts it over, and neither tries again after an error: only Play and
     Space do. The second click of a double one only undoes the first, and does
     nothing where the first was on a button that has gone since. */
  var clickToggled = false;
  var startedElsewhere = false;
  document.addEventListener('mousedown', function (e) {
    if (!(e.detail > 1)) startedElsewhere = e.target !== video;
  }, true);
  video.addEventListener('click', function (e) {
    var undoes = clickToggled;
    clickToggled = false;
    if (menuOpen()) {
      closeMenus();
      showControls();
    } else if (video.ended) {
      hideUpnext();
      showControls();
    } else if (lastPointer === 'mouse') {
      if (!shown(els.message) && !startedElsewhere && (undoes || !(e.detail > 1))) {
        togglePlay(false);
        clickToggled = true;
      }
      showControls();
    } else if (root.classList.contains('is-idle')) {
      showControls();
    } else if (!video.paused) {
      goIdle();
    }
  });

  /* ---- keyboard ------------------------------------------------------- */

  var heldOn;                // where the key now held down went down
  document.addEventListener('keydown', function (e) {
    if (!e.repeat) {
      heldOn = e.target;
    } else if (e.key === 'Enter' && (e.target !== heldOn || e.target === els.play)) {
      e.preventDefault();
      return;
    }
    if (els.ask && els.ask.open) {
      /* Safari's own step passes over buttons and links. */
      if (e.key === 'Tab') {
        e.preventDefault();
        var stop = q('[data-ask-continue]');
        (document.activeElement === stop ? q('[data-ask-back]') : stop).focus();
      }
      return;
    }
    showControls();
    /* Option and Tab is how Safari reaches the buttons. */
    if (e.key === 'Tab') focusByPointer = false;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = e.target && e.target.tagName;
    var onControl = tag === 'BUTTON' || tag === 'A' || tag === 'INPUT';
    if (menuOpen()) focusByPointer = false;
    if (e.key === 'Escape') {
      if (menuOpen()) closeMenus(true);
      else if (shown(els.rate) && (!upnextOpen() || els.rate.contains(document.activeElement))) endRating();
      else hideUpnext();
      return;
    }
    if (menuOpen()) {
      if (e.key === 'Tab') { closeMenus(true); return; }
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Home' || e.key === 'End') {
        e.preventDefault();
        moveInMenu(e.key);
        return;
      }
    }
    if (e.key === ' ' || e.key === 'Spacebar') {
      if (onControl) {
        /* Space presses a control the keyboard is on, not one the mouse left the focus on. */
        if (!focusByPointer || !e.target.closest('.player-chrome')) return;
        e.target.blur();
      }
      e.preventDefault();
      if (!e.repeat) togglePlay(false);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      /* The slider's own where the keyboard is on it, not where the mouse left the focus. */
      if ((tag === 'INPUT' && !focusByPointer) || menuOpen()) return;
      e.preventDefault();
      skip(e.key === 'ArrowLeft' ? -SKIP_S : SKIP_S);
      if (e.target === els.seek) peekBubble();
    } else if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      toggleFullscreen();
    } else if (e.key === 's' || e.key === 'S') {
      if (!skipMarker || menuOpen()) return;
      e.preventDefault();
      skipOn();
    } else {
      return;
    }
    showControls();
  });

  /* ---- next episode --------------------------------------------------- */

  /* What the video's end leads to. */
  function atEnd() {
    if (nextUnknown) findNext();
    else if (!next) askRating(leaveAtEnd);
    else {
      if (autoplay) goOrAsk(showUpnext);
      else showUpnext();
      /* What the system's own player hides is not asked. */
      if (upnextOpen() && !nativelyShown()) askRating(null);
    }
  }

  function leaveAtEnd() {
    if (showHref) window.location.href = showHref;
    else goBack();
  }

  /* The document at `href`: not a player's where the video cannot be played, and
     an empty one where the address leads elsewhere. */
  function fetchPage(href) {
    var abort = new AbortController();
    var timer = setTimeout(function () { abort.abort(); }, PAGE_WAIT_MS);
    return fetch(href, { credentials: 'same-origin', signal: abort.signal })
      .then(function (resp) {
        if (!resp.redirected && !resp.ok) throw Object.assign(new Error('HTTP ' + resp.status), { status: resp.status });
        return resp.redirected ? '' : resp.text();
      })
      .catch(function (err) {
        clearTimeout(timer);
        throw Object.assign(err, { lost: !err.status || fromProxy(err.status) });
      })
      .then(function (html) {
        clearTimeout(timer);
        var page = new DOMParser().parseFromString(html, 'text/html');
        /* Plex may answer again: the page that says it does not is no reason to leave this one. */
        if (page.querySelector('[data-plex-unavailable]')) throw new Error('Plex unavailable');
        return page;
      });
  }

  /* Ask Plex again what follows, through this video's own page. */
  var nextAsked = 0;
  function findNext() {
    var mine = ++nextAsked;
    var gen = generation;
    hideMessage();
    setLoading(true);
    fetchPage(window.location.href)
      .then(function (page) {
        if (gen !== generation || mine !== nextAsked) return;
        reached();
        var el = page.getElementById('player');
        readNext(el);
        fillSlot(el, 'next');
        fillSlot(el, 'upnext');
        setupNext();
      })
      .catch(function (err) {
        return err.lost; // Otherwise still unknown.
      })
      .then(function (unreached) {
        if (gen !== generation || mine !== nextAsked) return;
        if (unreached && video.ended) {
          waitOut(function () {
            if (video.ended) findNext();
            else endLost();
          });
          return;
        }
        endLost();
        setLoading(false);
        if (!video.ended) return;
        if (nextUnknown) {
          leaveNative();
          showMessage(MSG_NEXT_UNKNOWN, findNext);
        } else {
          atEnd();
        }
      });
  }

  function fillSlot(el, name) {
    var slot = q('[data-slot="' + name + '"]');
    var from = el.querySelector('[data-slot="' + name + '"]');
    var hadFocus = slot.contains(document.activeElement);
    /* The same title put in again would be said again. */
    if (name === 'titles' && from && from.textContent === slot.textContent) return;
    slot.textContent = '';
    while (from && from.firstChild) slot.appendChild(from.firstChild);
    if (hadFocus) (slot.querySelector('a') || els.play).focus();
  }

  /* Play the next episode here, or open its page when that cannot be done.
     `savedAt` is the place to save this one at, where that is not where it stands. */
  function goNext(byItself, savedAt) {
    if (!byItself) pressedPlay();
    if (!next || switching) return;
    if (byItself) sitting.count++;
    openNext(byItself, savedAt);
  }

  function openNext(byItself, savedAt) {
    var href = next.href;
    switching = true;
    swapping = true;
    wantPlaying = true;
    clickToggled = false;
    hideUpnext();
    dropRating();
    hideMessage();
    holdFrame(true);
    showSubtitle(next.label);
    var stopped = release(savedAt);
    var gen = generation;
    setLoading(true);
    showSkip();
    showControls();
    askPage();

    function askPage() {
      Promise.all([fetchPage(href), stopped])
        .then(function (done) {
          if (gen !== generation) return;
          reached();
          takePage(done[0], href);
          switching = false;
          startStream(startS);
        }, function (err) {
          if (gen !== generation) return;
          if (err.lost) waitOut(askPage, stay);
          else nextUnreached(err);
        })
        .catch(function () {
          if (gen !== generation) return;
          saveSitting(byItself);
          window.location.href = href;
          /* The browser may stay here after all: stopped, or with nothing to show. */
          setTimeout(nextUnreached, PAGE_WAIT_MS);
        });
    }

    function nextUnreached(err) {
      if (gen !== generation) return;
      endLost();
      stay();
      leaveNative();
      showMessage(MSG_NEXT_UNREACHED, err && err.status === 404 ? null : again);
    }

    function again() { goNext(false); }

    function stay() {
      switching = false;
      showSubtitle(item.subtitle);
      saveSitting(false);
      if (lost) lost.again = function () { openNext(byItself); };
    }
  }

  function showSubtitle(text) {
    q('.player-subtitle').textContent = text;
  }

  /* Become the page of another video. */
  function takePage(page, href) {
    var el = page.getElementById('player');
    if (!el) throw new Error('not a player page');
    var want = wantPlaying;
    var stepped = quality === 'auto' && override;
    closeMenus(true);
    endDrag({ pointerType: 'touch' }, false);
    hideBubble();
    readPage(el);
    resetVideo(startS);
    wantPlaying = want;
    if (stepped) {
      /* The connection is the same: Automatic goes on from where it had got to. */
      override = rowFor(stepped).id;
      good = choice();
      autoTop = rowFor('auto').id;
    }
    ['titles', 'keep', 'next', 'upnext'].forEach(function (name) { fillSlot(el, name); });
    setupKeep();
    setupNext();
    root.querySelectorAll('[data-back]').forEach(function (a) { a.setAttribute('href', backHref); });
    els.seek.setAttribute('aria-valuemax', String(Math.round(item.duration_s || 0)));
    document.title = page.title;
    window.history.replaceState(window.history.state, '', href);
    showMedia();
    buildQualityMenu();
    drawMarks();
    render();
  }

  /* What the system's own media controls call the video. */
  function showMedia() {
    if (!navigator.mediaSession || !window.MediaMetadata) return;
    navigator.mediaSession.metadata = new MediaMetadata(
      showHref ? { title: item.subtitle, artist: item.title } : { title: item.title });
  }

  function upnextOpen() {
    return !!els.upnext && shown(els.upnext);
  }

  function hideUpnext() {
    stopCountdown();
    els.upnextStatus.textContent = '';
    var card = els.upnext;
    if (!upnextOpen()) return;
    card.classList.remove('is-counting');
    if (card.contains(document.activeElement)) els.play.focus();
    leave(card);
  }

  function stopCountdown() {
    if (countdownTimer === null) return;
    clearInterval(countdownTimer);
    countdownTimer = null;
    showPlayState();
  }

  /* The card, counting down where the next episode begins by itself: not where it is to `wait`. */
  function showUpnext(wait) {
    var counts = autoplay && !wait;
    stopCountdown();
    els.upnextIn.hidden = !counts;
    els.upnextFill.style.animationDuration = COUNTDOWN_S + 's';
    els.upnext.classList.toggle('is-counting', counts);
    show(els.upnext);
    els.upnextStatus.textContent = counts
      ? 'Next episode in ' + COUNTDOWN_S + ' seconds. Space or Escape cancels.'
      : 'Next episode: ' + next.label;
    showControls();
    if (!counts) return;
    var until = Date.now() + COUNTDOWN_S * 1000;
    var count = function () {
      var left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      els.countdown.textContent = String(left);
      if (left > 0) return;
      hideUpnext();
      goOrAsk(function () { goNext(true); });
    };
    countdownTimer = setInterval(count, 250);
    count();
  }

  function toggleAutoplay() {
    autoplay = !autoplay;
    saveSetting(AUTOPLAY_KEY, autoplay);
    q('[data-autoplay]').setAttribute('aria-checked', String(autoplay));
    if (upnextOpen()) {
      showUpnext();
      showPlayState();
    }
  }

  /* The picture of the next episode, or of its show; without either the card has none. */
  function setupStill() {
    var img = els.still = els.upnext.querySelector('[data-still]');
    var there = function () { img.hidden = false; };
    var none = function () {
      var instead = img.getAttribute('data-fallback');
      img.removeAttribute('data-fallback');
      if (instead) img.setAttribute('src', instead);
      else img.hidden = true;
    };
    img.addEventListener('load', there);
    img.addEventListener('error', none);
    /* It may have come, or failed, before this script. */
    if (img.complete) (img.naturalWidth ? there : none)();
  }

  /* The controls of the next episode, in the page's own slots. */
  function setupNext() {
    els.upnext = q('[data-upnext]');
    if (!els.upnext) return;
    els.upnextIn = q('[data-upnext-in]');
    els.upnextFill = q('[data-upnext-fill]');
    els.countdown = q('[data-countdown]');
    setupStill();
    q('[data-next-btn]').addEventListener('click', function (e) {
      /* A new tab or window stays the browser's. */
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      goNext(false);
    });
    q('[data-upnext-play]').addEventListener('click', function () { goNext(false); });
    q('[data-upnext-cancel]').addEventListener('click', hideUpnext);
  }

  /* ---- are you still watching ------------------------------------------ */

  /* The episodes that began by themselves and the last sign of anybody since. */
  var sitting;

  /* Opening a page is a sign of somebody, unless this one opened by itself
     because the last could not go on to it in place. */
  function startSitting() {
    var held = null;
    try { held = JSON.parse(window.sessionStorage.getItem(SITTING_KEY)); } catch (e) { /* storage blocked */ }
    if (held && held.goes_on) {
      sitting = { count: held.count, lastActivity: held.lastActivity };
      if (held.pressed < pressedAt) pressedAt = held.pressed;
      saveSitting(false);
    } else {
      noteActivity();
    }
  }

  function saveSitting(goesOn) {
    var held = { count: sitting.count, lastActivity: sitting.lastActivity, goes_on: !!goesOn, pressed: pressedAt };
    try { window.sessionStorage.setItem(SITTING_KEY, JSON.stringify(held)); } catch (e) { /* storage blocked */ }
  }

  function noteActivity() {
    sitting = { count: 0, lastActivity: Date.now() };
    saveSitting(false);
  }

  ['pointerdown', 'keydown', 'click'].forEach(function (name) {
    document.addEventListener(name, function (e) { if (e.isTrusted) noteActivity(); }, true);
  });

  /* Go on, unless it is time to ask first. */
  function goOrAsk(go) {
    var due = sitting.count >= ASK_AFTER_EPISODES || Date.now() - sitting.lastActivity >= ASK_AFTER_MS;
    if (due) ask();
    else go();
  }

  function ask() {
    dropRating();
    leaveNative();
    swapping = true;
    holdFrame();
    release();
    setLoading(false);
    q('[data-ask-line]').textContent = 'Next: ' + next.label;
    var still = els.ask.querySelector('[data-still]');
    still.hidden = els.still.hidden;
    if (!still.hidden) still.setAttribute('src', els.still.getAttribute('src'));
    q('[data-ask-back]').setAttribute('href', showHref);
    els.ask.returnValue = '';
    els.ask.showModal();
    showSkip();
  }

  /* A question must not hide behind the system's own player. */
  function inNative() {
    return !!video.webkitPresentationMode && video.webkitPresentationMode !== 'inline';
  }

  function nativelyShown() { return inNative() || !!document.pictureInPictureElement; }

  function leaveNative() {
    if (inNative()) {
      video.webkitSetPresentationMode('inline');
    } else if (document.pictureInPictureElement) {
      document.exitPictureInPicture().catch(function () {});
    }
  }

  if (els.ask) {
    q('[data-ask-continue]').addEventListener('click', function () {
      els.ask.close('continue');
      goNext(false);
    });
    els.ask.addEventListener('cancel', function (e) {
      e.preventDefault();
      window.location.href = showHref;
    });
    /* The browser closes it at a second Escape, even where the details do not open. */
    els.ask.addEventListener('close', function () {
      if (els.ask.returnValue === 'continue') return;
      swapping = false;
      showUpnext(true);
      showPlayState();
    });
  }

  /* ---- a rating at the end --------------------------------------------- */

  var ratingAsked = 0;       // bumps with every question and whenever one no longer matters
  var ratingWait = null;     // runs until it is known whether to ask
  var rating = null;         // the set of thumbs of the card
  var goOnAfter = null;      // what the end leads to once the card is done with
  var ratedTimer = null;
  var ignoredTimer = null;
  var unsavedRating = null;  // {ref, level} on its way to the server

  /* Whether the server wants to ask what the person thought of what just ended. `goOn`,
     or null where the next episode follows, comes after the question or in its place. */
  function askRating(goOn) {
    dropRating();
    var mine = ratingAsked;
    function heard(question) {
      if (mine !== ratingAsked) return;
      clearTimeout(ratingWait);
      if (question) {
        showRating(question, goOn);
        return;
      }
      ratingAsked++;
      if (goOn) goOn();
    }
    if (goOn) ratingWait = setTimeout(heard, RATE_WAIT_MS);
    endSaved.then(function (saved) {
      if (!saved || mine !== ratingAsked) return null;
      return MM.api.post('/api/watch/ratings/ask/' + encodeURIComponent(item.rating_key));
    }).then(function (data) {
      heard(data && data.ask ? data : null);
    }, function () { heard(null); });
  }

  function showRating(question, goOn) {
    var text = 'How was ' + (question.season ? 'season ' + question.season + ' of ' : '') + question.title + '?';
    if (goOn) leaveNative();
    goOnAfter = goOn;
    els.rateTitle.textContent = text;
    els.rateSet.textContent = '';
    rating = MM.ratings.mount(question.ref, { variant: 'grid' });
    els.rateSet.appendChild(rating.node);
    els.rate.classList.toggle('player-rate--beside', !goOn);
    show(els.rate);
    els.rateStatus.textContent = text;
    showControls();
    waitForRating();
  }

  /* The question goes, and nothing of it is to come. */
  function dropRating() {
    ratingAsked++;
    [ratingWait, ratedTimer, ignoredTimer].forEach(clearTimeout);
    ignoredTimer = null;
    goOnAfter = null;
    if (!rating) return;
    rating.destroy();
    rating = null;
    els.rateStatus.textContent = '';
    if (els.rate.contains(document.activeElement)) els.play.focus();
    leave(els.rate);
  }

  function endRating() {
    var goOn = goOnAfter;
    dropRating();
    if (goOn) goOn();
  }

  function waitForRating() {
    clearTimeout(ignoredTimer);
    ignoredTimer = null;
    if (!goOnAfter) return;
    ignoredTimer = setTimeout(function () {
      if (els.rate.contains(document.activeElement) || menuOpen()) waitForRating();
      else endRating();
    }, RATE_IGNORED_MS);
  }

  document.addEventListener('mm:rating', function (e) {
    var said = e.detail;
    if (said.phase === 'pending') unsavedRating = { ref: said.ref, level: said.level };
    else if (said.phase !== 'read') unsavedRating = null;
    if (!rating || said.from !== rating.node) return;
    clearTimeout(ratedTimer);
    if (said.phase === 'pending') {
      clearTimeout(ignoredTimer);
      ignoredTimer = null;
    } else if (said.phase === 'saved' && said.level !== null) {
      ratedTimer = setTimeout(endRating, RATED_MS);
    } else {
      waitForRating();
    }
  });

  q('[data-rate-close]').addEventListener('click', endRating);

  /* Leaving may cut off the request that a rating went out with. */
  function keepRating() {
    if (!unsavedRating) return;
    var none = unsavedRating.level === null;
    fetch('/api/watch/ratings/' + encodeURIComponent(unsavedRating.ref), {
      method: none ? 'DELETE' : 'PUT',
      credentials: 'same-origin',
      keepalive: true,
      headers: none ? {} : { 'Content-Type': 'application/json' },
      body: none ? undefined : JSON.stringify({ level: unsavedRating.level }),
    }).catch(function () {});
  }

  /* ---- back ------------------------------------------------------------ */

  var backTimer = null;      // runs while a step back is being made
  var backAt = 0;            // when it was asked for
  var stepping = false;      // the browser has begun to make it

  /* Whether a page that plays things led here, in this tab. */
  function ledHere() {
    if (window.history.length < 2 || !document.referrer) return false;
    var from = new URL(document.referrer);
    return from.origin === window.location.origin && /^\/watch(\/(show|movie)\/[0-9]+)?$/.test(from.pathname);
  }

  /* Kept with the entry, so a reload knows it too. */
  function noteOrigin() {
    var state = window.history.state || {};
    if (state.mmFrom === undefined && ledHere()) {
      state = Object.assign({}, state, { mmFrom: backHref });
      window.history.replaceState(state, '', window.location.href);
    }
    cameFrom = state.mmFrom;
  }

  /* Through the history the page comes back as it was left; a next episode may
     lead elsewhere than it did. A step the browser never makes is a link. */
  function goBack() {
    if (cameFrom !== backHref) { window.location.href = backHref; return; }
    // A second step would lead past the page that is on its way; one that is not is a link.
    if (backTimer !== null) {
      if (!stepping && Date.now() - backAt >= AGAIN_MS) window.location.href = backHref;
      return;
    }
    stepping = false;
    backAt = Date.now();
    window.history.back();
    backTimer = setTimeout(function () {
      backTimer = null;
      if (!stepping) window.location.href = backHref;
    }, PAGE_WAIT_MS);
  }

  root.addEventListener('click', function (e) {
    var link = e.target.closest('[data-back]');
    if (!link || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    dropRating();
    if (cameFrom !== backHref) return;
    e.preventDefault();
    goBack();
  });

  /* ---- leaving the page ----------------------------------------------- */

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') beaconProgress();
  });

  // A phone may not say so.
  window.addEventListener('beforeunload', function () { stepping = true; });

  window.addEventListener('pagehide', function () {
    clearTimeout(backTimer);
    backTimer = null;
    var resumeAt = position();
    beaconStop();
    dropRating();
    keepRating();
    pendingStart = resumeAt;
    streamId = null;
    generation++;
    endLost();
    dropAim();
    if (switching) showSubtitle(item.subtitle);
    switching = false;
    destroyHls();
  });

  /* Back from the browser's page cache: the old stream was stopped. */
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted || (els.ask && els.ask.open)) return;
    /* Playing somewhere else it stays so. */
    if (shown(els.message) && tryAgain === playHere) return;
    wantPlaying = false;
    startStream(pendingStart || 0);
  });

  /* ---- start ---------------------------------------------------------- */

  startSitting();
  noteOrigin();
  setupKeep();
  setupNext();
  showMedia();
  buildQualityMenu();
  readVolume();
  showVolume();
  showPlayState();
  render();

  if (typeof Hls === 'undefined' || !Hls.isSupported()) {
    showMessage(MSG_UNSUPPORTED, null);
    return;
  }
  startStream(startS);
})();
