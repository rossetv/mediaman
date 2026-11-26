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
 *   POST /api/watch/ratings/ask/<key>  (in the last credits or at the end, the place saved:
 *                                     whether to ask for a rating)
 *   POST /api/cast/stream             {rating_key, quality, subtitle, page, asked_s} → {stream_id, url,
 *                                     subtitle, subtitle_tracks} (a stream for a TV; watch/play-on.js)
 *   GET  /api/tvs/<id>, POST /api/tvs/<id>/commands  (what plays on a TV of the person's)
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
 * Played elsewhere, by a way of watch/play-on.js, the title goes on with this page as its
 * remote: what the way plays takes the video's place here, so that its place, its credits, the
 * question at its end and the next episode are this page's as ever. A TV with a page of its own
 * has a remote of its own (tv-remote.js): this page only says that it plays there. Either way
 * nothing of the title is started here meanwhile, since that would end it there.
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
  /* Reached other than by playing into them, the last credits have played this long first. */
  var CREDITS_PLAYED_S = 10;
  /* Ones that begin earlier in the runtime than this are not believed, as on the server. */
  var CREDITS_FROM = 0.7;
  /* Where the server counts a title watched from, unless its final credits begin before. */
  var WATCHED_FROM = 0.9;
  /* What comes after them is a scene where it is longer than this. */
  var SCENE_S = 10;
  /* The longest a title's end waits to hear whether to ask for a rating. */
  var RATE_WAIT_MS = 4000;
  /* A rating just given stays in sight this long. */
  var RATED_MS = 1800;
  /* Left alone this long, the question lets the page go on, or the credits. */
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
  /* A title that starts sooner than this shows neither its name nor a spinner. */
  var INTRO_AFTER_MS = 300;
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
  var MSG_OTHER_TAB = 'This is playing on a TV from another tab.';
  var MSG_MAYBE_TV = 'This may still be playing on a TV.';
  var LOST_LINES = {
    tv: 'It may be off, asleep or away from its browser. If it comes back within 2 minutes, it carries on by itself.',
    older: 'It may be off or away from the network. If it answers within 2 minutes, you can go on from here.',
    way: 'It may be off or away from the network. If it comes back within 2 minutes, it carries on by itself.',
    quiet: 'It’s back, but hasn’t said what it plays. If it does within 10 minutes, it carries on by itself.',
  };
  var BAD_FILE = 'Its picture is in a form older TVs refuse, and converting it failed. Try another TV, or watch here.';
  var NOTE_QUALITY = 'TVs get up to 1080p, in a form every TV can play. 4K and HDR stay on this device. Changing quality restarts the picture where you are.';
  var NOTE_SUBTITLES = 'On a TV the subtitles are drawn into the picture. Changing them restarts the picture where you are; it takes a few seconds.';
  var CAST_URL = '/api/cast/stream';
  /* What a TV can be given, as rows of the quality menu. */
  var TV_ROWS = [
    { id: 'auto', kind: 'auto', label: 'Auto', detail: 'Up to 1080p' },
    { id: 'best', kind: 'convert', label: '1080p', detail: '8 Mbps', height: 1080, bitrate_kbps: 8000 },
    { id: 'medium', kind: 'convert', label: '720p', detail: '4 Mbps', height: 720, bitrate_kbps: 4000 },
    { id: 'low', kind: 'convert', label: '480p', detail: '1.5 Mbps', height: 480, bitrate_kbps: 1500 },
  ];
  var LOOK_WAIT_MS = 1500;
  var AGAIN_WAIT_MS = 4000;
  var PLACE_AGE_S = 90;
  var KEEP_EVERY_MS = 60000;
  var TV_EVERY_MS = 5000;
  var TV_BEGINS_MS = 1000;
  var TV_HIDDEN_MS = 30000;
  /* A TV that lets go of the title pauses it first, just before it says so. */
  var LET_GO_MS = 5000;
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
    backHref = el.getAttribute('data-back-href') || '/';
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
  var local = video;

  /* What is heard of the video, wherever it plays. */
  var heard = [];
  function onVideo(name, fn) {
    heard.push([name, fn]);
    video.addEventListener(name, fn);
  }

  function useVideo(next) {
    heard.forEach(function (h) { video.removeEventListener(h[0], h[1]); });
    video = next;
    heard.forEach(function (h) { video.addEventListener(h[0], h[1]); });
  }

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
    rateLevel: q('[data-rate-level]'),
    rateSet: q('[data-rate-set]'),
    rateStatus: q('[data-rate-status]'),
    ask: q('[data-ask]'),
    lost: q('[data-lost]'),
    playOn: q('[data-play-on]'),
    away: q('[data-away]'),
    awayOn: q('[data-away-on]'),
    awayHint: q('[data-away-hint]'),
    awayEnd: q('[data-away-end]'),
    bring: q('[data-bring]'),
    stopAway: q('[data-stop-away]'),
    openRemote: q('[data-open-remote]'),
    noVolume: q('[data-no-volume]'),
    tracks: q('[data-tracks]'),
    leaveCard: q('[data-leave-card]'),
    bottom: q('.player-bottom'),
    message: q('[data-message]'),
    messageLine: q('[data-message-line]'),
    messageActs: q('[data-message-acts]'),
    messageText: q('[data-message-text]'),
    messageBack: q('[data-message]').querySelector('a'),
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
  var playOn = MM.playOn;    // the sheet, where the page has it
  var away = null;           // {way, play} while a way plays the title, {tv} while a TV does
  var attempt = null;        // a start elsewhere by a way: {way, wanted, opened}
  var goneFrom = null;       // {name} of what it played on, while the page says that this is over
  var lostShown = false;     // the page says that what it plays on does not answer
  var lostLine = '';         // and why it may still carry on
  var holding = null;        // {way, told, heard} while it is not known whether it plays elsewhere
  var here = null;           // what this device goes on with: {rows, quality, override}
  var subtitle = null;       // what the TV is asked for: 'off', a track, or null for Plex's own choice
  var subtitleGood = null;   // what it was last given
  var given = null;          // the subtitles it can be given and has, as its start said
  var pausedHere = false;    // the pause to come is the person's
  var pausedThereAt = -Infinity;
  var autoplay = readSetting(AUTOPLAY_KEY) !== 'false';

  /* What belongs to the video being watched; resetVideo sets every one. */
  var pendingStart;          // where the stream will start, until the video has loaded
  var aimS;                  // where presses have led, until the video is sent there
  var wantPlaying;
  var hasPlayed;
  var playedS;
  var savedAs;               // the last save the server took, to make none twice
  var sent;                  // the places this tab has sent to be saved
  var anew;                  // where it ended, while it has yet to play from its start again, or null
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
  var question;              // of a rating: see askOnce

  function resetVideo(at) {
    pendingStart = at;
    dropAim();
    wantPlaying = true;
    hasPlayed = false;
    playedS = 0;
    savedAs = null;
    sent = {};
    anew = null;
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
    question = { asking: null, held: null, put: false, tried: false, creditsS: 0, due: false, answered: false, over: false, aloneS: 0 };
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
    /* Plex's length can differ from the file's; at the end the file's own is what the clock reaches. */
    if (video.ended && isFinite(video.duration) && video.duration > 0) return video.duration;
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
    addTitle(els.qualityMenu, 'Quality');
    var list = addList(els.qualityMenu);
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', 'Quality');
    rows.forEach(function (row) {
      var btn = menuRow(row.label, row.detail);
      btn.setAttribute('data-quality', row.id);
      list.appendChild(btn);
    });
    if (away) addNote(els.qualityMenu, NOTE_QUALITY);
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

  function addTitle(menu, text) {
    var title = document.createElement('p');
    title.className = 'player-menu-title';
    title.setAttribute('aria-hidden', 'true');
    title.textContent = text;
    menu.appendChild(title);
  }

  function addNote(menu, text) {
    var p = document.createElement('p');
    p.className = 'player-menu-note';
    p.textContent = text;
    menu.appendChild(p);
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
    /* What a TV is given is no choice for this device. */
    if (!away) saveSetting(QUALITY_KEY, value);
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
    if (holding) {
      good = choice();
      showQuality();
      return;
    }
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
    if (away || quality !== 'auto' || starting || !streamId) return false;
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
    if (away || quality !== 'auto' || !autoTop || starting || swapping || menuOpen()) return;
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
    showIntro();
    showNotice();
    showPlayState();
  }

  /* While a title starts here, its name stands in for the spinner; what plays elsewhere keeps its own. */
  var introTimer = null;
  function introDue() {
    return root.classList.contains('is-loading') && (!hasPlayed || switching) && (wantPlaying || starting) && !away && !attempt && !holding;
  }

  function showIntro() {
    var due = introDue();
    root.classList.toggle('is-starting', due);
    if (!due) {
      clearTimeout(introTimer);
      introTimer = null;
      if (root.classList.contains('is-intro')) {
        /* The name hides a held frame: over a picture that plays now, it is not to show again. */
        if (video.readyState >= 3) releaseFrame();
        root.classList.add('is-intro-out');
        root.classList.remove('is-intro');
      }
    } else if (introTimer === null && !root.classList.contains('is-intro')) {
      introTimer = setTimeout(function () {
        introTimer = null;
        if (!introDue()) {
          showIntro();
          return;
        }
        if (root.classList.contains('is-intro-out')) {
          /* The drawing stopped where the last one left it: it starts over. */
          var drawn = root.querySelector('.player-intro');
          if (drawn.getAnimations) drawn.getAnimations({ subtree: true }).forEach(function (a) { a.currentTime = 0; });
        }
        root.classList.remove('is-intro-out');
        root.classList.add('is-intro');
      }, INTRO_AFTER_MS);
    }
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
  function showMessage(text, again, card) {
    tryAgain = again;
    swapping = false;
    if (again !== tryLost && again !== playHere) releaseFrame();
    if (shown(els.rate)) dropRating();
    /* A message is no hand-over: the name goes at once, not by a fade over the text. */
    root.classList.remove('is-intro', 'is-intro-out');
    setLoading(false);
    els.messageText.textContent = text;
    els.retry.hidden = !again || !!card;
    els.messageLine.hidden = !card;
    els.messageLine.textContent = card ? card.line : '';
    els.messageBack.hidden = els.retry.parentNode.hidden = !!card;
    els.messageActs.hidden = !card;
    els.messageActs.textContent = '';
    if (card) card.acts.forEach(addAct);
    els.retry.textContent = again === playHere || again === playAnyway ? 'Play here' : 'Try again';
    els.play.disabled = !again;
    show(els.message);
    showPlayState();
    showSkip();
    swapNotice = null;
    showControls();
    /* No button of a card that came by itself: a press meant for the picture is not for it. */
    (card ? els.message : again ? els.retry : els.messageBack).focus();
  }

  /* A choice of a message's, as [what it says, what it does]; the first stands out. */
  function addAct(act, n) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'player-pill' + (n ? '' : ' player-pill--primary');
    btn.textContent = act[0];
    btn.addEventListener('click', act[1]);
    els.messageActs.appendChild(btn);
  }

  /* The first of the controls that are there. */
  function focusControls() {
    paintAway();
    (!isAway() || swapping ? els.play : onTv() && !els.openRemote.hidden ? els.openRemote : video.ended ? els.bring : els.play).focus();
  }

  function hideMessage() {
    els.play.disabled = false;
    if (!shown(els.message)) return;
    if (els.message.contains(document.activeElement)) focusControls();
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

  function startBody() {
    var body = {
      rating_key: item.rating_key,
      quality: asked(),
      page: pageId,
      asked_s: Math.max(0, Date.now() - pressedAt) / 1000,
    };
    if (away && subtitle !== null) body.subtitle = subtitle;
    return body;
  }

  /* Start a stream at `pos`; one retry before the error is shown. */
  function startStream(pos) {
    dropGone();
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
      var start = MM.api.post(away ? CAST_URL : '/api/watch/stream', startBody(), { signal: abort.signal });
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
          if (away) {
            takeSubtitles(data);
            if (data.markers) takeMarks(data);
            loadAway(data);
            return;
          }
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
          if (code === 'track_unavailable' && subtitle !== subtitleGood) {
            subtitle = subtitleGood;
            swapNotice = "Couldn't change the subtitles.";
            return attempt(tries);
          }
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
      playAsWanted();
    });
    h.on(Hls.Events.FRAG_LOADED, function () {
      if (h !== hls) return;
      taken();
      reached();
    });
    h.loadSource(playlist);
    h.attachMedia(video);
  }

  function playAsWanted() {
    if (wantPlaying) play();
    else setLoading(false);
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

  /* Drop the current stream and start a new one where playback stands, or `at`.
     `force` is for a quality change, which may replace a stream that is
     still starting. */
  function restartStream(force, at) {
    if (switching || (starting && !force)) return;
    var pos = at === undefined ? position() : at;
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
    endAway(true);
    showMessage(MSG_REPLACED, playHere);
  }

  /* Go on from where the other one got to, where it has saved that. */
  var hereGeneration = null; // the generation that is asking it
  function playHere(known, unreached) {
    var gen = hereGeneration = ++generation;
    pressedPlay();
    swapping = true;         // a press starts nothing from this place meanwhile
    hideMessage();
    setLoading(true);
    (known === true ? Promise.resolve(null) : fetchPage(window.location.href))
      .then(function (page) {
        var el = page && page.getElementById('player');
        var place = el ? parseFloat(el.getAttribute('data-place-s')) : NaN;
        if (gen !== generation || !(place >= 0) || place === placeS || sent[place]) return;
        placeS = place;
        /* Ended there with nothing to follow, it would end here at once and leave the player. */
        if (!next && !nextUnknown && place >= duration() - 1) return;
        pendingStart = within(place);
      })
      .catch(function (err) {
        /* From where this one stands, where that may not be newer than the server's. */
        if (gen !== generation || !unreached || !err.lost) return;
        generation++;
        unreached();
      })
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
    if (!streamId || !hasPlayed || anew !== null) return null;
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
    if (onTv()) return;
    if (shown(els.message)) {
      /* A card came by itself: a press meant for the picture is not for it. */
      if (tryAgain && (byButton || els.messageActs.hidden)) tryAgain();
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
      if (holding) holding.pressed = true;
      if (wantPlaying) pressedPlay();
      showPlayState();
      return;
    }
    if (video.paused || video.ended) {
      pressedPlay();
      if (overThere()) { playAgain(); return; }
      if (!streamId && !starting) { wantPlaying = true; restartStream(false); return; }
      play();
    } else {
      wantPlaying = false;
      /* Not what its TV does when it lets go of it. */
      pausedHere = true;
      video.pause();
    }
  }

  /* Played to its end on a TV that then holds it no more. */
  function overThere() {
    return !!away && !!away.play && away.play.carriesOn && video.ended && !swapping;
  }

  function playAgain() {
    var end = position();
    leftEnd(0);
    wantPlaying = true;
    restartStream(false, 0);
    startAnew(end);
    render();
  }

  /* What ended at `end` is watched, and stays so until it plays again. */
  function startAnew(end) { if (anew === null) anew = end; }

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
    leftEnd(t);
    if (video.readyState >= 1 && !(away && swapping)) {
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
    leftEnd(aimS);
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
    paintAway();
    if (paused) showControls();
    else scheduleIdle();
  }

  /* Playing again, or moving within the episode, cancels the next-episode countdown. */
  ['play', 'playing', 'seeking'].forEach(function (name) {
    onVideo(name, hideUpnext);
    onVideo(name, function () { leftEnd(position()); });
  });

  function onPlaying() {
    hasPlayed = true;
    anew = null;
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
  }
  onVideo('playing', onPlaying);
  onVideo('play', function () {
    if (!ownPlay) noteActivity();
    ownPlay = false;
    wantPlaying = true;
    showPlayState();
  });
  onVideo('pause', function () {
    if (swapping) return;
    stopRolling();
    showPlayState();
    if (video.ended) return;
    noteActivity();
    if (!pausedHere) pausedThereAt = Date.now();
    pausedHere = false;
    wantPlaying = false;
    saveProgress();
  });
  onVideo('waiting', function () {
    setLoading(true);
    noteStall();
  });
  onVideo('canplay', function () {
    if (!video.paused) return;
    if (!wantPlaying && !swapping) releaseOnceSeen();
    setLoading(false);
  });
  onVideo('seeking', function () {
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
  onVideo('seeked', function () {
    settleStart();
    if (heldForSeek && !swapping) releaseOnceSeen();
    saveAfterSeek();
  });
  onVideo('progress', render);
  onVideo('volumechange', function () {
    if (hasPlayed) noteActivity();
    showVolume();
    if (away) return;
    saveSetting(VOLUME_KEY, video.volume);
    saveSetting(MUTED_KEY, video.muted);
  });

  onVideo('timeupdate', function () {
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
        playedOn(lastTime, now);
      }
    }
    /* Emptied for a new stream the video stands at 0: from there to its start is no jump. */
    lastTime = pendingStart !== null ? null : now;
    if (playedS - troubleAtPlayed >= CALM_PLAYED_S) clearTrouble();
    maybeStepUp();
    render();
  });

  onVideo('ended', function () {
    /* Presses that led back from the end are to be followed; ones that led on have been. */
    if (aimS !== null && aimS < duration() - 1) {
      land();
      return;
    }
    dropAim();
    render();
    endSaved = saveProgress();
    watchedHere();
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
    els.remaining.textContent = (d - pos < 1 ? '' : '-') + fmt(d - pos);
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
  onVideo('durationchange', drawMarks);

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
    var over = video.ended || switching || onTv() || opening() || (els.ask && els.ask.open) || (els.upnext && !els.upnext.hidden);
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
    keepOffSkip();
    skipTimer = setTimeout(function () { els.skip.classList.add('is-quiet'); }, SKIP_SHOWN_MS);
  }

  /* Only the last credits lead on; a scene may follow the others. */
  function endsEpisode(m) { return !!next && m.kind === 'credits' && m.final; }

  /* A TV on which this one waits to follow another has it wait no more. */
  function watchedHere() { if (playOn && !away) playOn.watched(String(item.rating_key)); }

  function countsWatched(t) {
    var credits = lastCredits();
    return duration() > 0 && t >= Math.min(WATCHED_FROM * duration(), credits ? credits.start_s : Infinity);
  }

  function skipOn() {
    if (!skipMarker) return;
    if (!endsEpisode(skipMarker)) {
      seekTo(skipMarker.end_s);
      return;
    }
    /* Saved as at the end, so that the episode counts as watched. */
    watchedHere();
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
    var there = away && away.play ? ' the TV' : '';
    setName(els.mute, (silent ? 'Unmute' : 'Mute') + there);
    els.volume.setAttribute('aria-label', there ? 'TV volume' : 'Volume');
    MM.dom.setIcon(els.mute.querySelector('.ico'), silent ? 'i-volume-xmark' : 'i-volume-high');
    var level = video.muted ? 0 : video.volume;
    els.volume.value = level;
    els.volume.setAttribute('aria-valuetext', video.muted ? 'Muted' : Math.round(level * 100) + '%');
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
    document.documentElement.classList.remove('has-player-keep-open');
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
    // For the toasts, which make way for Keep's open menu (_ui_feedback.css).
    document.documentElement.classList.toggle('has-player-keep-open', !!trigger.closest('.player-keep'));
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
        if (window.UIFeedback) window.UIFeedback.toast('Keep undone.', { variant: 'info' });
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
      if (!video.paused && !dragging && !menuOpen() && !(playOn && playOn.isOpen()) && !shown(els.message) && !keyboardInChrome()) goIdle();
    }, IDLE_MS);
  }

  /* A preview goes with the controls, also from under a mouse that rests on the bar. */
  function goIdle() {
    if (isAway()) return;
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
    if (playOn && playOn.isOpen()) return;
    var asks = [els.ask, els.leaveCard].filter(function (card) { return card && card.open; })[0];
    if (asks) {
      /* Safari's own step passes over buttons and links. */
      if (e.key === 'Tab') {
        e.preventDefault();
        var stops = asks.querySelectorAll('.player-pill');
        (document.activeElement === stops[0] ? stops[1] : stops[0]).focus();
      }
      return;
    }
    showControls();
    /* Option and Tab is how Safari reaches the buttons. */
    if (e.key === 'Tab') focusByPointer = false;
    if (e.ctrlKey || e.metaKey || e.altKey || onTv() || opening() || lostShown) return;
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
        if (!focusByPointer || !e.target.closest('.player-chrome, .player-rate')) return;
        e.target.blur();
      }
      e.preventDefault();
      if (!e.repeat) togglePlay(false);
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      if (els.rate.contains(e.target)) {
        e.preventDefault();
        moveInRating(e.key === 'ArrowLeft' ? -1 : 1);
        return;
      }
      /* The slider's own where the keyboard is on it, not where the mouse left the focus. */
      if ((tag === 'INPUT' && !focusByPointer) || menuOpen()) return;
      e.preventDefault();
      skip(e.key === 'ArrowLeft' ? -SKIP_S : SKIP_S);
      if (e.target === els.seek) peekBubble();
    } else if ((e.key === 'f' || e.key === 'F') && !isAway()) {
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
      /* What cannot be seen is not asked. */
      if (upnextOpen() && !cannotBeSeen()) {
        if (roomForRating()) askRating(null);
        else rateAfterUpnext = true;
      }
    }
  }

  function leaveAtEnd() {
    /* The TV may be given something else from here. */
    if (away) return;
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
    if (!next || switching || onTv() || opening() || lostShown) return;
    if (byItself) sitting.count++;
    openNext(byItself, savedAt);
  }

  function openNext(byItself, savedAt) {
    var href = next.href;
    dropHold();
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
    if (away) {
      here.rows = rows;
      rows = TV_ROWS;
      /* Tracks are the title's own; that there are to be none is not. */
      if (subtitle !== 'off') subtitle = null;
    }
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
    var hadFocus = card.contains(document.activeElement);
    leave(card);
    paintAway();
    if (hadFocus) focusControls();
    /* What hides it may go on to the next episode at once. */
    if (rateAfterUpnext) setTimeout(function () {
      if (rateAfterUpnext && video.ended && !upnextOpen()) askRating(null);
    }, 0);
  }

  function stopCountdown() {
    if (countdownTimer === null) return;
    clearInterval(countdownTimer);
    countdownTimer = null;
    tellCount(null);
    showPlayState();
  }

  /* A way that carries on goes on with the count where the page is left. */
  function tellCount(until) { if (away && away.play && away.play.counts) away.play.counts(until); }

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
    paintAway();
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
    tellCount(until);
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
  var ratingRef;             // the title it rates
  var tight = window.matchMedia('(max-width: 659.98px) and (max-height: 599.98px)'); // as in the CSS
  var rateAfterUpnext = false;
  var onReturn = null;       // what the end leads to, once the hidden page is seen again
  var rateHovered = false;   // the mouse is on the card
  var overCredits = false;   // the card came while the video played
  var goOnAfter = null;      // what the end leads to once the card is done with
  var ratedTimer = null;
  var ignoredTimer = null;
  var unsavedRating = null;  // {ref, level} on its way to the server

  /* Ask the server, once `saved` is taken and if it is `still` wanted then, whether to ask what the
     person thought: the question, or null. It gives each only once, so one that cannot be shown
     as it comes is held. */
  function askOnce(saved, still) {
    var mine = question;
    mine.tried = true;
    mine.asking = saved.then(function (taken) {
      if (mine !== question) return null;
      if (!still()) mine.tried = false;
      if (!taken || !mine.tried) return null;
      return MM.api.post('/api/watch/ratings/ask/' + encodeURIComponent(item.rating_key));
    }).then(function (data) {
      mine.held = data && data.ask ? data : null;
    }, function () {}).then(function () {
      mine.asking = null;
      return mine.held;
    });
    return mine.asking;
  }

  /* The last credits, where they are to be believed as the server sees it, or null. */
  function lastCredits() {
    var m = markers.filter(function (x) { return x.kind === 'credits' && x.final; }).pop();
    return m && m.start_s > duration() * CREDITS_FROM ? m : null;
  }

  /* Places are saved in tenths: one that rounds to before the credits is not in them yet. */
  function inCredits(t) {
    var m = lastCredits();
    return !!m && Math.round(t * 10) / 10 >= m.start_s && !afterCredits(t);
  }

  /* Whether a scene that comes after the credits is on at `t`. */
  function afterCredits(t) {
    var m = lastCredits();
    return !!m && duration() - m.end_s > SCENE_S && t >= m.end_s;
  }

  function pageHidden() { return document.visibilityState === 'hidden'; }

  function cannotBeSeen() { return nativelyShown() || pageHidden(); }

  /* The video has played from one place to another. */
  function playedOn(from, to) {
    if (!inCredits(to)) {
      question.creditsS = 0;
      question.due = false;
      if (rating && overCredits && afterCredits(to)) putOffRating();
      return;
    }
    if (rating && overCredits) leaveAlone(to - from);
    if (question.put || question.over) return;
    question.creditsS += to - from;
    if (!inCredits(from) || question.creditsS >= CREDITS_PLAYED_S) question.due = true;
    if (!question.due || cannotBeSeen()) return;
    if (question.held) showRating(question.held, null);
    else if (!question.tried) askInCredits();
  }

  function askInCredits() {
    var mine = question;
    var asked = ratingAsked;
    askOnce(saveProgress(), function () { return !switching && inCredits(position()); }).then(function (held) {
      if (!held || mine !== question || mine.put || asked !== ratingAsked) return;
      if (inCredits(position()) && !switching && !video.ended && !cannotBeSeen()) showRating(held, null);
    });
  }

  /* At the end. `goOn`, or null where the next episode follows, comes after the question or
     in its place. */
  function askRating(goOn) {
    if (question.put) {
      endWithRating(goOn);
      return;
    }
    dropRating();
    if (goOn && pageHidden()) {
      onReturn = goOn;
      return;
    }
    var mine = ratingAsked;
    var since = Date.now();
    function heard(held) {
      if (mine !== ratingAsked) return;
      clearTimeout(ratingWait);
      if (held && pageHidden()) {
        onReturn = goOn;
        return;
      }
      if (held && !roomForRating()) {
        rateAfterUpnext = true;
        return;
      }
      if (held) {
        showRating(held, goOn);
        return;
      }
      ratingAsked++;
      if (goOn) goOn();
    }
    if (goOn) ratingWait = setTimeout(heard, RATE_WAIT_MS);
    /* A question that is answered too late to be put would be spent for nothing. */
    function askNow() {
      askOnce(endSaved, function () {
        return mine === ratingAsked && (!goOn || Date.now() - since <= RATE_WAIT_MS / 2);
      }).then(heard);
    }
    if (question.held) heard(question.held);
    else if (!question.asking) askNow();
    else question.asking.then(function (held) {
      if (mine !== ratingAsked) return;
      if (held) heard(held);
      else askNow();
    });
  }

  /* The question has been put before the end: open still, it is waited for. */
  function endWithRating(goOn) {
    if (!goOn) return;
    if (!rating) {
      goOn();
      return;
    }
    leaveNative();
    goOnAfter = goOn;
    waitForRating();
  }

  /* Where the card has no names by its thumbs it says the one given. */
  function showLevel(level) {
    var given = MM.ratings.LEVELS.filter(function (l) { return l.level === level; })[0];
    els.rateLevel.textContent = given ? given.name : '';
    els.rate.classList.toggle('is-rated', !!given);
  }

  /* Over the skip button where large text makes the two meet. */
  function keepOffSkip() {
    els.rate.classList.remove('player-rate--above');
    if (!shown(els.rate) || !shown(els.skip)) return;
    var card = els.rate.getBoundingClientRect();
    var skip = els.skip.getBoundingClientRect();
    els.rate.classList.toggle('player-rate--above', card.right > skip.left && card.bottom > skip.top);
  }
  window.addEventListener('resize', keepOffSkip);

  /* Whether the card can be seen: a small window has no room for it and the next episode's. */
  function roomForRating() { return !upnextOpen() || !tight.matches && !(isAway() && window.innerWidth < 1200); }

  function showRating(held, goOn) {
    var text = 'How was ' + (held.season ? 'season ' + held.season + ' of ' : '') + held.title + '?';
    if (goOn) leaveNative();
    goOnAfter = goOn;
    question.put = true;
    question.aloneS = 0;
    rateHovered = false;
    overCredits = !video.ended;
    els.rateTitle.textContent = text;
    els.rateSet.textContent = '';
    ratingRef = held.ref;
    rating = MM.ratings.mount(held.ref, { variant: 'grid', again: overCredits });
    els.rateSet.appendChild(rating.node);
    /* A small window shows no names. */
    els.rateSet.querySelectorAll('button').forEach(function (b) { b.setAttribute('title', b.getAttribute('aria-label')); });
    els.rate.classList.toggle('player-rate--beside', !goOn);
    showLevel(null);
    show(els.rate);
    keepOffSkip();
    els.rateStatus.textContent = text;
    if (!overCredits) showControls();
    waitForRating();
  }

  /* The question goes, and nothing of it is to come. */
  function dropRating() {
    ratingAsked++;
    rateAfterUpnext = false;
    onReturn = null;
    [ratingWait, ratedTimer, ignoredTimer].forEach(clearTimeout);
    ignoredTimer = null;
    goOnAfter = null;
    if (!rating) return;
    rating.destroy();
    rating = null;
    els.rateStatus.textContent = '';
    if (els.rate.contains(document.activeElement)) focusControls();
    leave(els.rate);
  }

  /* The video is, or is to be, at `t` and not at its end: one put in the credits stays in them. */
  function leftEnd(t) {
    if (rating && overCredits && afterCredits(t)) {
      putOffRating();
      return;
    }
    if (!rating || !overCredits || !inCredits(t)) {
      dropRating();
      return;
    }
    goOnAfter = null;
    clearTimeout(ignoredTimer);
    ignoredTimer = null;
  }

  /* Out of the way of a scene: without an answer the question is put again at the end. */
  function putOffRating() {
    question.put = question.answered;
    dropRating();
  }

  /* The credits have played on for this long: one that nobody is at goes, for the end to put it. */
  function leaveAlone(playedFor) {
    if (pageHidden()) return;
    question.aloneS += playedFor;
    if (rateHovered || (!focusByPointer && els.rate.contains(document.activeElement))) question.aloneS = 0;
    if (question.aloneS < RATE_IGNORED_MS / 1000) return;
    question.over = true;
    putOffRating();
  }

  els.rate.addEventListener('pointerenter', function (e) { rateHovered = e.pointerType === 'mouse'; });
  els.rate.addEventListener('pointerleave', function () { rateHovered = false; });
  els.rate.addEventListener('pointerdown', function () { question.aloneS = 0; });

  function moveInRating(by) {
    var thumbs = els.rateSet.querySelectorAll('button');
    var at = Array.prototype.indexOf.call(thumbs, document.activeElement);
    thumbs[at < 0 ? (by < 0 ? thumbs.length - 1 : 0) : (at + by + thumbs.length) % thumbs.length].focus();
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
      /* The keyboard in the card is somebody; a click leaves the focus behind. */
      if (!focusByPointer && !unsavedRating && els.rate.contains(document.activeElement)) waitForRating();
      else endRating();
    }, RATE_IGNORED_MS);
  }

  document.addEventListener('mm:rating', function (e) {
    var said = e.detail;
    if (said.phase === 'pending') unsavedRating = { ref: said.ref, level: said.level };
    else if (said.phase !== 'read') unsavedRating = null;
    if (rating && MM.ratings.canRate(ratingRef) === false) endRating();
    if (!rating || said.from !== rating.node) return;
    clearTimeout(ratedTimer);
    showLevel(said.level);
    question.answered = said.level !== null;
    if (said.phase === 'saved' && said.level !== null) ratedTimer = setTimeout(endRating, RATED_MS);
    else waitForRating();
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
    return from.origin === window.location.origin && /^\/(watch\/(show|movie)\/[0-9]+)?$/.test(from.pathname);
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
    if (stopsOnLeaving()) {
      e.preventDefault();
      els.leaveCard.showModal();
      return;
    }
    if (cameFrom !== backHref) return;
    e.preventDefault();
    goBack();
  });

  /* ---- playing elsewhere ----------------------------------------------- */

  function onTv() { return !!away && !!away.tv; }
  /* A TV has been chosen and has not begun. */
  function opening() { return !!attempt && attempt.opened; }
  function isAway() { return !!away || opening() || !!goneFrom; }
  function stopsOnLeaving() { return !!away && !!away.play && !away.play.carriesOn && !video.ended; }

  function coded(code) { return Object.assign(new Error(code), { code: code }); }
  function toast(text) { if (window.UIFeedback) window.UIFeedback.toast(text, { variant: 'info' }); }

  function awayName() {
    if (opening()) return attempt.name;
    if (!away) return goneFrom.name;
    if (onTv()) return away.tv.name;
    return away.play.name || away.way.label;
  }

  function pictureUrl() {
    var key = encodeURIComponent(item.rating_key);
    return showHref ? '/api/watch/thumb/' + key : '/api/poster/' + key + '?kind=art';
  }

  function titleNow() {
    return {
      rating_key: item.rating_key, title: item.title, subtitle: item.subtitle,
      duration_s: item.duration_s, image: pictureUrl(),
    };
  }

  /* The choice of quality, as one of those a TV can be given. */
  function tvQuality() {
    if (away && away.play) return asked();
    var row = quality === 'auto' || quality === 'original' ? null : rowFor(quality);
    if (!row) return 'auto';
    if (row.height >= AUTO_HEIGHT) return 'best';
    return row.height >= 720 ? 'medium' : 'low';
  }

  /* What plays the title now is ended and its place saved, before something else is asked to play
     it. Settles with the place. */
  function stopHere() {
    var ended = video.ended;
    var at = ended ? 0 : position();
    var was = away;
    dropHold();
    dropGone();
    leaveNative();
    if (fullscreenElement()) toggleFullscreen();
    dropRating();
    hideMessage();
    hideUpnext();
    swapping = true;
    var stopped = release();
    if (ended) startAnew(position());
    if (onTv()) {
      /* Where it is not reached it is replaced all the same. */
      stopped = inTime(stopTv(was.tv), STOP_WAIT_MS).then(function (last) {
        if (last && isThis(last)) at = last.position_s;
      }, function () {
        if (was.tv.play) at = was.tv.play.position_s || at;
      });
    }
    endAway(true);
    pendingStart = at;
    setLoading(true);
    return stopped.then(function () { return at; });
  }

  function isThis(what) { return String(what.rating_key) === String(item.rating_key); }

  /* What a start that failed has changed, as the sheet says it. */
  function leftBy(was) { return was.was ? ' What played on ' + was.was + ' stopped, and goes on here.' : ''; }

  /* What was chosen is not wanted any more. */
  function dropAttempt() {
    var was = attempt;
    attempt = null;
    if (was.abort) was.abort.abort();
    playOn.cancel();
  }

  /* Nothing came of it: go on here, at the place, or as it was `held` before. */
  function goOnHere(playing, held) {
    var at = position();
    swapping = true;
    release();
    if (held) {
      /* Still not known to be nowhere else. */
      held.told = true;
      holding = held;
      pendingStart = at;
      showAway();
      showMessage(held.says, playAnyway);
      return;
    }
    wantPlaying = playing;
    startStream(at);
    showAway();
  }

  /* A way of the browser's, in the press for it; `chosen` is told once a TV was picked. */
  function startWay(way, chosen) {
    var mine = attempt = { way: way, name: way.label, opened: false, played: false, was: '', held: null, abort: null, stream: null, data: null };
    pressedPlay();
    return way.start({
      title: titleNow(),
      position_s: position(),
      open: function () { return openFor(mine, chosen); },
    }).then(function (elsewhere) {
      if (attempt !== mine) {
        elsewhere.stop();
        throw coded('cancelled');
      }
      attempt = null;
      follow(way, elsewhere, mine.stream, mine.data);
    }, function (err) {
      if (attempt === mine) {
        attempt = null;
        if (mine.opened) goOnHere(mine.played, mine.held);
      }
      err.left = leftBy(mine);
      throw err;
    });
  }

  function openFor(mine, chosen) {
    if (attempt !== mine) return Promise.reject(coded('cancelled'));
    var asks = tvQuality();
    var at;
    var late;
    mine.was = away ? awayName() : '';
    mine.held = holding;
    mine.opened = true;
    mine.played = wantPlaying;
    chosen();
    var stopped = stopHere();
    showAway();
    return stopped.then(function (place) {
      if (attempt !== mine) throw coded('cancelled');
      at = place;
      mine.abort = new AbortController();
      late = setTimeout(function () { mine.abort.abort(); }, START_WAIT_MS);
      return MM.api.post(CAST_URL, Object.assign(startBody(), { quality: asks }), { signal: mine.abort.signal });
    }).then(function (data) {
      clearTimeout(late);
      if (attempt !== mine) {
        stopOnServer(data.stream_id);
        throw coded('cancelled');
      }
      streamId = data.stream_id;
      mine.data = data;
      mine.quality = asks;
      mine.stream = streamOf(data, at, false);
      return mine.stream;
    }, function (err) {
      clearTimeout(late);
      if (err.name === 'AbortError') throw coded(attempt === mine ? 'network_error' : 'cancelled');
      throw err.code ? err : coded(err.error || 'failed');
    });
  }

  /* A stream's answer as its way is given it. */
  function streamOf(data, at, paused) {
    return {
      stream_id: data.stream_id,
      url: window.location.origin + data.url,
      position_s: at,
      saved_s: anew,
      duration_s: data.duration_s || item.duration_s,
      paused: paused,
      title: titleNow(),
    };
  }

  /* What plays elsewhere takes the video's place here. */
  function follow(way, elsewhere, stream, data) {
    here = { rows: rows, quality: quality, override: override };
    quality = data.quality || 'auto';
    override = null;
    rows = TV_ROWS;
    away = { way: way, play: elsewhere };
    good = streamChoice = choice();
    starting = false;
    streamId = stream.stream_id;
    pendingStart = stream.position_s;
    lastTime = null;
    savedAs = null;
    wantPlaying = !stream.paused;
    clearTrouble();
    dropGone();
    useVideo(elsewhere.media);
    streamPlayed = video.readyState >= 3;
    /* A TV's stream has none. */
    takePreviews(null);
    dropFrames();
    elsewhere.onGone(function (why) { if (away && away.play === elsewhere) letGo(why); });
    buildQualityMenu();
    takeQualities(data);
    takeSubtitles(data);
    keepWith();
    swapping = false;
    render();
    showAway();
    var plays = !video.paused && video.readyState >= 3;
    playAsWanted();
    if (plays) onPlaying();
  }

  function keepWith() {
    if (!away.play.keep) return;
    var after = next && /^\/watch\/play\/([0-9]+)/.exec(next.href);
    away.play.keep({
      quality: quality, subtitle: subtitle, played_s: Math.round(playedS),
      given: given, markers: markers, chapters: chapters, next: after ? { rating_key: after[1], label: next.label } : null,
    });
  }

  /* Another stream of the same play: a new choice, the next episode, one that ended. */
  function loadAway(data) {
    var gen = generation;
    if (aimS !== null) pendingStart = aimS;
    dropAim();
    var stream = streamOf(data, pendingStart, !wantPlaying);
    swapping = true;
    away.play.load(stream).then(function () {
      if (gen !== generation) return;
      /* Moved on meanwhile. */
      if (pendingStart !== null && Math.abs(pendingStart - stream.position_s) > 1) video.currentTime = pendingStart;
      swapping = false;
      keepWith();
      playAsWanted();
    }, function () {
      if (gen !== generation) return;
      swapping = false;
      streamGone();
    });
  }

  /* Also how a way says that its TV does not answer, and that it does again. */
  ['waiting', 'canplay'].forEach(function (name) { onVideo(name, paintAway); });

  onVideo('error', function () {
    if (away && streamId && !starting && !swapping) streamGone();
  });

  /* No longer elsewhere; with `stop` what plays there is ended. */
  function endAway(stop) {
    var was = away;
    if (!was) return;
    away = null;
    clearTimeout(tvTimer);
    if (was.play) {
      rows = here.rows;
      quality = here.quality;
      override = here.override;
      good = choice();
      useVideo(local);
      if (stop) was.play.stop();
      buildQualityMenu();
    }
    here = null;
    subtitle = subtitleGood = null;
    showLost(false);
    showAway();
  }

  /* It ended elsewhere by itself. */
  function letGo(why) {
    var name = awayName();
    var ended = video.ended;
    var at = ended ? 0 : position();
    hideUpnext();
    var goesOn = wantPlaying || Date.now() - pausedThereAt < LET_GO_MS;
    swapping = true;
    release();
    if (ended) startAnew(position());
    if (why !== 'back') goneFrom = { name: name };
    endAway(false);
    pendingStart = at;
    if (why === 'back' && !ended) {
      wantPlaying = goesOn;
      startStream(at);
      return;
    }
    restHere();
    if (why !== 'back') showOver('way', null, name, why);
  }

  /* Nothing plays, here or elsewhere, until a press. */
  function restHere() {
    wantPlaying = false;
    swapping = false;
    setLoading(false);
    render();
    showPlayState();
  }

  function bringHere() {
    pressedPlay();
    hideUpnext();
    if (opening()) {
      dropAttempt();
      goOnHere(true);
    } else if (onTv()) {
      leaveTv(function (at) {
        wantPlaying = true;
        swapping = true;
        startStream(at);
        toast('Playing here again from ' + fmt(at) + '.');
      });
    } else if (away) {
      var ended = video.ended;
      var at = ended ? 0 : position();
      var note = away.way.hereNote;
      swapping = true;
      release();
      if (ended) startAnew(position());
      endAway(true);
      wantPlaying = true;
      startStream(at);
      toast('Playing here again from ' + fmt(at) + '.' + (note ? ' ' + note : ''));
    }
  }

  function stopAway() {
    hideUpnext();
    if (opening()) {
      dropAttempt();
      release();
      showAway();
      restHere();
    } else if (onTv()) {
      leaveTv(stoppedAt);
    } else if (away && video.ended && !starting) {
      release();
      endAway(true);
      leaveAtEnd();
    } else if (away) {
      var at = position();
      swapping = true;
      release();
      endAway(true);
      stoppedAt(at);
    }
  }

  function stoppedAt(at) {
    pendingStart = at;
    restHere();
    els.play.focus();
    toast(at || anew === null ? 'Stopped. Your place is saved at ' + fmt(at) + '.' : 'Stopped.');
  }

  /* A paused TV asks for nothing, and what nothing asks for is ended: the place once more, and
     nothing played since, counts as use and as no save. */
  setInterval(function () {
    if (!away || !away.play || !streamId || !hasPlayed || !video.paused || video.ended || swapping) return;
    var place = { position_s: Math.round((anew === null ? position() : anew) * 10) / 10, played_s: 0 };
    MM.api.post('/api/watch/progress', withStream(place)).catch(function () { /* found out as it plays on */ });
  }, KEEP_EVERY_MS);

  /* ---- a TV with a page of its own ---- */

  var tvTimer = null;
  var tvLeaving = false;

  function tvUrl(tv) { return '/api/tvs/' + encodeURIComponent(tv.id); }

  function stopTv(tv) {
    var body = { kind: 'stop' };
    if (tv.play && tv.play.play_id) body.play_id = tv.play.play_id;
    return MM.api.post(tvUrl(tv) + '/commands', body).then(function (data) {
      return data.tv && data.tv.last;
    }, function (err) {
      if (err && err.error === 'not_playing') return tv.last || null;
      throw err;
    });
  }

  /* End it there, and `then` go on from its place. */
  function leaveTv(then) {
    var tv = away.tv;
    if (tvLeaving) return;
    tvLeaving = true;
    stopTv(tv).then(function (last) {
      tvLeaving = false;
      if (!onTv()) return;
      if (last && !isThis(last)) {
        /* It has gone on to another episode. */
        window.location.href = '/watch/play/' + encodeURIComponent(last.rating_key) + '?resume=1';
        return;
      }
      endAway(false);
      then(last ? last.position_s : position());
    }, function (err) {
      tvLeaving = false;
      if (err && err.error === 'stale') {
        /* It has been given something else: that is not stopped unasked. */
        lookAtTv();
        if (window.UIFeedback) window.UIFeedback.error('Something else plays on the TV now.');
        return;
      }
      if (window.UIFeedback) window.UIFeedback.error(MM.api.messageOf(err, 'That didn’t work. Try again.'));
    });
  }

  function watchTv() {
    clearTimeout(tvTimer);
    tvTimer = setTimeout(lookAtTv, pageHidden() ? TV_HIDDEN_MS : root.classList.contains('is-begun') ? TV_EVERY_MS : TV_BEGINS_MS);
  }

  function lookAtTv() {
    var tv = away.tv;
    clearTimeout(tvTimer);
    MM.api.get(tvUrl(tv)).then(function (data) {
      if (!onTv() || away.tv.id !== tv.id || tvLeaving) return;
      away.tv = data.tv;
      if (data.tv.play) paintAway();
      else tvOver(data.tv.last);
    }, function (err) {
      if (!onTv() || tvLeaving) return;
      if (err && (err.status === 404 || err.status === 410)) tvOver(err.data && err.data.last, err.error);
    }).then(function () { if (onTv()) watchTv(); });
  }

  function tvOver(last, code) {
    var tv = away.tv;
    var why = code || last && last.reason;
    if (why !== 'stopped') goneFrom = { name: tv.name };
    endAway(false);
    if (last && isThis(last)) pendingStart = last.position_s;
    if (why === 'stopped') {
      stoppedAt(position());
      return;
    }
    restHere();
    showOver(tv.kind === 'older' ? 'older' : 'tv', tv, tv.name, why);
  }

  /* ---- no longer answering, and over ---- */

  function dropGone() {
    if (!goneFrom) return;
    goneFrom = null;
    showAway();
  }

  function closeGone() {
    dropGone();
    hideMessage();
    showPlayState();
    els.play.focus();
  }

  /* While it may come back. */
  function showLost(on) {
    var tv = onTv() ? away.tv : null;
    var line = on ? LOST_LINES[!tv ? (away.play.quiet ? 'quiet' : 'way') : tv.kind === 'older' ? 'older' : 'tv'] : '';
    if (on === lostShown) {
      /* Back but silent, or gone again: the same card, with the words that are true now. */
      if (on && line !== lostLine && els.messageLine.textContent === lostLine) els.messageLine.textContent = lostLine = line;
      return;
    }
    lostShown = on;
    lostLine = line;
    if (!on) {
      hideMessage();
      return;
    }
    var acts = [['Bring it here', bringHere], ['Stop', stopAway]];
    if (tv) acts.unshift(['Try again', lookAtTv]);
    showMessage(awayName() + ' stopped answering', null, { line: line, acts: acts });
  }

  /* What it played on has let go of it. */
  function showOver(kind, tv, name, why) {
    var after = position() ? ' at ' + fmt(position()) : '';
    var way = kind === 'way';
    var lines = {
      lost: [name + ' stopped answering', way ? 'It was off or away from the network for 2 minutes, so it stopped' + after + '.'
        : kind === 'older' ? 'It didn’t answer for 2 minutes, so it stopped' + after + '.'
          : 'It was off, asleep or away from its browser for 2 minutes, so it stopped' + after + '.'],
      taken: ['Someone else is using ' + name, (way || kind === 'older' ? 'They started something on it, and yours stopped' : 'They paired it, and yours stopped') + after + '.'],
      unpaired: kind === 'older' ? [name + ' can’t be used now', 'An admin removed it or turned older TVs off. Yours stopped' + after + '.']
        : [name + ' was unpaired', 'It was unpaired in Settings, or in your Profile, or on the TV. To use it again, pair it.'],
      moved: ['You’re watching on another device', 'It goes on there, so ' + name + ' stopped' + after + '.'],
      still: [name + ' stopped by itself', 'Nobody answered, so it stopped' + after + '.'],
      silent: [name + ' stopped answering', 'It came back, but said nothing of what it plays for 10 minutes. It may still be playing there.'],
      signed_out: [name + ' stopped', 'You signed out everywhere, which ends what plays on a TV.'],
    }[why] || [name + ' stopped', 'Yours stopped' + after + '.'];
    var acts = [['Bring it here', function () { tryAgain(); }], ['Close', closeGone]];
    if (why === 'moved') acts.reverse();
    if (tv && (why === 'lost' || why === 'still')) {
      acts.unshift([why === 'lost' ? 'Try again' : 'Continue watching', function () { playOnAgain(tv); }]);
    }
    showMessage(lines[0], playHere, { line: lines[1], acts: acts });
  }

  /* On the same TV, from the place it was left at. */
  var askingTv = false;
  function playOnAgain(tv) {
    if (askingTv) return;
    askingTv = true;
    pressedPlay();
    MM.api.post(tvUrl(tv) + '/play', playHost.what()).then(function (data) {
      askingTv = false;
      if (!goneFrom) return;
      hideMessage();
      dropGone();
      followTv(data.tv);
    }, function (err) {
      askingTv = false;
      if (window.UIFeedback) window.UIFeedback.error(tv.name + (err && err.error === 'tv_off' ? ' isn’t answering yet.' : ' didn’t start. Try again in a moment.'));
    });
  }

  document.addEventListener('visibilitychange', function () { if (onTv() && !pageHidden()) watchTv(); });

  /* ---- showing that it plays elsewhere ---- */

  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }

  function showAway() {
    var on = isAway();
    var plays = !!away && !!away.play;
    var silent = plays && video.canVolume === false;
    root.classList.toggle('is-on-tv', onTv() || !!goneFrom && !opening());
    root.classList.toggle('is-gone', !!goneFrom && !opening());
    root.classList.toggle('has-no-volume', silent);
    els.away.hidden = !on;
    els.awayEnd.hidden = !on || !!goneFrom && !opening();
    els.noVolume.hidden = !silent;
    if (!plays) els.tracks.hidden = true;
    els.openRemote.hidden = !(onTv() && MM.tvRemote);
    els.bring.classList.toggle('player-pill--primary', els.openRemote.hidden);
    if (on) {
      root.querySelectorAll('[data-away-art]').forEach(function (img) {
        if (img.getAttribute('src') !== pictureUrl()) {
          img.hidden = true;
          img.setAttribute('src', pictureUrl());
        }
      });
    }
    closeMenus();
    showVolume();
    showSkip();
    paintAway();
    showControls();
  }
  root.querySelectorAll('[data-away-art]').forEach(function (img) {
    img.addEventListener('load', function () { img.hidden = false; });
  });

  var lowScreen = window.matchMedia('(max-height: 520px)');
  /* What stands over the picture of where it plays keeps clear of the bar and of the words under it.
     The next episode's card goes under the words where it can; where a card would meet them, or has
     no room over them, they give way. */
  function placeCards() {
    if (!isAway()) return;
    function set(name, px) { root.style.setProperty(name, Math.max(0, Math.round(px)) + 'px'); }
    var low = lowScreen.matches;
    var head = q('.player-top').getBoundingClientRect().bottom;
    var say = q('.player-away-say').getBoundingClientRect();
    var card = 0;
    if (shown(els.rate)) card = els.rate.scrollHeight;
    if (upnextOpen()) {
      var tall = els.upnext.scrollHeight;
      var under = !low && say.bottom + 20 + tall + 12 <= els.awayEnd.getBoundingClientRect().top;
      els.upnext.classList.toggle('is-under', under);
      set('--away-under', say.bottom + 20);
      if (!under && window.innerWidth < 1200) card = Math.max(card, tall);
    }
    var over = low || card > say.top - 14 - head;
    var foot = over ? q('.player-bottom').getBoundingClientRect().top : say.top - 14;
    root.style.setProperty('--away-foot', Math.max(12, Math.round(window.innerHeight - foot)) + 'px');
    set('--away-head', head);
    set('--away-row', window.innerHeight - q('.player-seek-row').getBoundingClientRect().top + 14);
    var asks = shown(els.message) && els.message.getBoundingClientRect();
    var meets = asks && asks.bottom + 12 > say.top && asks.top - 12 < say.bottom;
    root.classList.toggle('is-wordless', !!meets || over && card > 0);
  }
  els.away.addEventListener('animationend', placeCards);
  els.message.addEventListener('animationend', placeCards);
  window.addEventListener('resize', placeCards);
  root.addEventListener('scroll', placeCards);
  if (window.ResizeObserver) {
    var sizes = new ResizeObserver(placeCards);
    /* No card itself, whose size placing it may change. */
    [q('.player-top'), q('.player-away-say'), q('.player-bottom'), els.rateSet].forEach(function (el) { sizes.observe(el); });
  }

  function paintAway() {
    var on = isAway();
    root.classList.toggle('is-away', on);
    var tv = onTv() ? away.tv : null;
    var over = !!goneFrom && !opening();
    /* A start that failed: nothing is on its way. */
    var stuck = !!away && !!away.play && !streamId && !starting && !streamPlayed && shown(els.message) && tryAgain !== tryLost;
    var begins = !over && !stuck && (opening() || (tv ? !tv.play || /^(starting|loading)$/.test(tv.play.state) : on && (starting || swapping || !streamPlayed)));
    var ended = on && !begins && !over && (tv ? tv.play.state === 'ended' : video.ended);
    var failed = stuck && !ended || !!tv && !!tv.play && tv.play.state === 'failed';
    var lost = !opening() && (tv ? !tv.on && tv.lost_s !== null && tv.lost_s !== undefined : !!away && !!away.play && !!away.play.lost);
    var paused = on && !begins && !over && !ended && (tv ? tv.play.state === 'paused' : video.paused);
    root.classList.toggle('is-begun', on && !begins);
    root.classList.toggle('is-ended', ended && !tv);
    root.classList.toggle('is-away-paused', paused);
    root.classList.toggle('is-lost', lost);
    root.querySelectorAll('[data-away-idle]').forEach(function (el) { el.inert = opening() || lost || over; });
    els.awayEnd.inert = lost;
    showLost(lost);
    if (!on) return;
    var says = begins ? 'Starting on ' : ended ? 'Finished on ' : paused ? 'Paused on ' : 'Playing on ';
    if (failed) says = 'Couldn’t play on ';
    if (lost || over) says = 'Was playing on ';
    setText(els.awayOn, says + awayName());
    var hint = 'You can lock or close this device. The TV carries on.';
    if (lost || over) hint = position() || anew === null ? 'Your place is saved at ' + fmt(position()) + '.' : 'Your place is kept.';
    else if (failed) hint = tv && tv.kind === 'older' && tv.play.error === 'tv' ? BAD_FILE : 'Your place is kept.';
    else if (begins) hint = 'Starting. This can take a few seconds.';
    else if (ended && upnextOpen()) hint = autoplay && countdownTimer !== null ? (away.play && away.play.carriesOn ? 'The next episode starts by itself, even if you leave this page.' : 'Keep this page open for the next episode.') : awayName() + ' waits for you to choose.';
    else if (ended) hint = awayName() + ' is ready for something else.';
    else if (tv && !isThis(tv.play)) hint = 'It plays ' + [tv.play.title, tv.play.episode_title].filter(Boolean).join(', ') + ' now.';
    else if (away.play && away.play.carriesOn) hint = 'You can leave this page. The TV carries on.';
    else if (away.play) hint = 'Keep this page open. ' + away.way.label + ' stops when you leave it.';
    setText(els.awayHint, hint);
    setText(els.bring, ended && !tv ? 'Play again' : 'Stop and bring it here');
    if (ended && !tv && !els.bring.firstElementChild) els.bring.insertBefore(MM.dom.icon('i-rotate-right'), els.bring.firstChild);
    setText(els.stopAway, ended && !tv ? 'Disconnect' : 'Stop');
    placeCards();
  }

  /* The subtitles the TV can be given, and which it has. */
  function takeSubtitles(data) {
    var tracks = data.subtitle_tracks || [];
    var now = data.subtitle || 'off';
    subtitleGood = subtitle;
    given = { subtitle: data.subtitle || null, subtitle_tracks: tracks };
    els.tracks.hidden = !tracks.length;
    var hadFocus = els.tracks.contains(document.activeElement);
    els.tracks.textContent = '';
    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'player-pill';
    trigger.setAttribute('data-menu-trigger', '');
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.textContent = 'Subtitles';
    var menu = document.createElement('div');
    menu.className = 'player-menu player-menu--up player-menu--wide';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', 'Subtitles');
    menu.hidden = true;
    els.tracks.appendChild(trigger);
    els.tracks.appendChild(menu);
    addTitle(menu, 'Subtitles');
    var list = addList(menu);
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', 'Subtitles');
    [{ id: 'off', label: 'Off' }].concat(tracks).forEach(function (t) {
      var btn = menuRow(t.label, '');
      btn.setAttribute('data-subtitle', t.id);
      btn.setAttribute('aria-checked', String(t.id === now));
      list.appendChild(btn);
    });
    addNote(menu, NOTE_SUBTITLES);
    if (hadFocus) trigger.focus();
  }

  els.tracks.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-subtitle]');
    if (!btn) return;
    closeMenus(true);
    if (btn.getAttribute('aria-checked') === 'true' || !away) return;
    subtitle = btn.getAttribute('data-subtitle');
    pressedPlay();
    restartStream(true);
  });

  els.bring.addEventListener('click', function (e) {
    /* The second press of a double one would take back what the first began. */
    if (e.detail > 1) return;
    if (overThere()) {
      pressedPlay();
      playAgain();
    } else if (away && away.play && !away.play.carriesOn && video.ended) {
      togglePlay(true);
    } else {
      bringHere();
    }
  });
  els.stopAway.addEventListener('click', stopAway);
  els.openRemote.addEventListener('click', function () { MM.tvRemote.open(away.tv); });

  q('[data-leave-stay]').addEventListener('click', function () { els.leaveCard.close(); });
  q('[data-leave-stop]').addEventListener('click', function () {
    els.leaveCard.close();
    beaconStop();
    streamId = null;
    generation++;
    endAway(true);
    goBack();
  });

  var playHost = {
    what: function () {
      return {
        rating_key: String(item.rating_key),
        quality: tvQuality(),
        position_s: position(),
        autoplay: autoplay,
        asked_s: Math.max(0, Date.now() - pressedAt) / 1000,
      };
    },
    on: function () { return onTv() ? 'tv-' + away.tv.id : away ? away.way.id : null; },
    before: function (tv) {
      attempt = { name: tv.name, opened: true, played: wantPlaying, was: away ? awayName() : '', held: holding };
      pressedPlay();
      var stopped = stopHere();
      showAway();
      return stopped;
    },
    failed: function () {
      if (!opening()) return '';
      var was = attempt;
      attempt = null;
      goOnHere(was.played, was.held);
      return leftBy(was);
    },
    started: function (tv) {
      /* Left and come back to meanwhile, the page may have begun something else. */
      if (!opening() && (streamId || starting)) {
        swapping = true;
        release();
      }
      attempt = null;
      dropHold();
      followTv(tv);
      if (MM.tvRemote) MM.tvRemote.open(tv);
    },
    startWay: startWay,
    here: bringHere,
  };

  function setupPlayOn() {
    if (!playOn) return;
    var buttons = root.querySelectorAll('[data-play-on]');
    var led = false;
    playOn.ready(function (any) {
      buttons.forEach(function (b) { b.hidden = !any && !isAway(); });
    });
    function open(b) {
      showControls();
      playOn.open(b, playHost);
    }
    buttons.forEach(function (b) {
      b.addEventListener('click', function () { open(b); });
    });
    /* Led here from a title for a way that needs the player. */
    if (window.location.hash === '#play-on') {
      window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
      playOn.ready(function (any) {
        var shownOne = any && Array.prototype.filter.call(buttons, function (b) { return b.getClientRects().length; })[0];
        if (!shownOne || led) return;
        led = true;
        open(shownOne);
      });
    }
  }

  function inTime(promise, ms) {
    return Promise.race([promise, new Promise(function (resolve) { setTimeout(resolve, ms, null); })]);
  }

  /* Plays here, unless it plays elsewhere: a start here would end it there. Until that is known
     a press only says whether it is to play; where it is not known soon the page says so, and
     only a press for it plays it here. */
  function begin() {
    var key = String(item.rating_key);
    var asked = playOn && playOn.again(key);
    var look = playOn && playOn.mayBeOnTv(root.hasAttribute('data-on-tv'));
    if (!asked && !look) {
      if (!holding) startStream(position());
      return;
    }
    /* Asked again behind the message that says so, it stays until there is something to follow. */
    var mine = holding = {
      way: asked ? asked.way : null,
      told: !!holding,
      says: holding ? holding.says : MSG_MAYBE_TV,
      heard: false,
      at: position(),
      pressed: false,
      drop: function () {
        clearTimeout(timer);
        if (got) got.play.leave();
        got = null;
      },
    };
    var still = mine.told;
    var timer = null;
    var got = null;            // what the way has found
    if (!still) {
      swapping = true;
      setLoading(true);
    }

    function tell(text) {
      clearTimeout(timer);
      if (text) mine.says = text;
      if (mine !== holding || mine.told) return;
      mine.told = true;
      showMessage(mine.says, playAnyway);
    }

    function tellAfter(ms) {
      clearTimeout(timer);
      if (!mine.told) timer = setTimeout(tell, ms);
    }

    function endHold() {
      var told = mine.told;
      dropHold();
      if (told) hideMessage();
    }

    function startHere() {
      mine.heard = true;
      if (mine.told && (still || asked)) return;
      endHold();
      startStream(position());
    }

    var found = asked ? asked.found : Promise.resolve(null);
    found.then(function (f) {
      got = f;
      if (f && mine !== holding) mine.drop();
    }, function () { /* said below */ });
    tellAfter(LOOK_WAIT_MS);
    (look ? playOn.onTv(key) : Promise.resolve(null)).catch(function (err) {
      if (!asked) throw err;
      /* A way may still find it; that it does not says nothing of the TVs. */
      tell();
      return null;
    }).then(function (tv) {
      if (mine !== holding) return undefined;
      if (tv) {
        endHold();
        followTv(tv);
        return undefined;
      }
      if (!asked) {
        startHere();
        return undefined;
      }
      tellAfter(AGAIN_WAIT_MS);
      return found.then(function (f) {
        if (mine !== holding) return;
        if (!f || !isThis(f.stream)) {
          startHere();
          return;
        }
        got = null;
        endHold();
        followAgain(asked.way, f, mine.pressed ? wantPlaying : null);
      });
    }).catch(function (err) {
      mine.heard = true;
      tell(err && err.code === 'elsewhere' ? MSG_OTHER_TAB : MSG_MAYBE_TV);
    });
  }

  /* It is known, or no longer asked, where it plays; what a way has found is left to its TV. */
  function dropHold() {
    var was = holding;
    holding = null;
    if (was) was.drop();
  }

  /* From the newest place there is: where it stood on a TV a moment ago, or what the server has. */
  function playAnyway() {
    var was = holding;
    var chosen = Math.abs(position() - was.at) > 1;
    var place = was.way && was.way.release ? was.way.release(String(item.rating_key)) : null;
    if (place && place.age_s < PLACE_AGE_S) was.place = place.position_s;
    dropHold();
    if (!chosen && was.place !== undefined) pendingStart = within(was.place);
    playHere(chosen || was.place !== undefined, function () {
      holding = was;
      was.heard = true;
      showMessage(MSG_LOST, playAnyway);
    });
  }

  /* `wants`: what was pressed for while it was looked for, or null. */
  function followAgain(way, found, wants) {
    var kept = found.stream.keep || {};
    hasPlayed = true;
    if (typeof found.stream.saved_s === 'number') anew = found.stream.saved_s;
    playedS = kept.played_s || 0;
    subtitle = kept.subtitle || null;
    takeMarks({ markers: kept.markers, chapters: kept.chapters });
    follow(way, found.play, found.stream, Object.assign({ quality: kept.quality }, kept.given));
    if (wants === null || wants === wantPlaying) return;
    wantPlaying = wants;
    if (wants) {
      play();
    } else {
      pausedHere = true;
      video.pause();
    }
  }

  function followTv(tv) {
    away = { tv: tv };
    if (tv.play && tv.play.position_s) pendingStart = tv.play.position_s;
    swapping = false;
    setLoading(false);
    render();
    showAway();
    watchTv();
  }

  /* ---- leaving the page ----------------------------------------------- */

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') beaconProgress();
    else if (onReturn) askRating(onReturn);
  });

  // A phone may not say so.
  window.addEventListener('beforeunload', function () { stepping = true; });

  window.addEventListener('pagehide', function () {
    clearTimeout(backTimer);
    backTimer = null;
    if (away && away.play && away.play.carriesOn) {
      if (video.ended) beaconStop();
      else beaconProgress();
      dropRating();
      keepRating();
      return;
    }
    var resumeAt = position();
    if (hasPlayed && countsWatched(resumeAt)) watchedHere();
    beaconStop();
    dropRating();
    keepRating();
    /* A TV's own start goes on without the page; a way's needs it. */
    if (attempt && attempt.way) dropAttempt();
    if (attempt) {
      attempt = null;
      showAway();
    }
    if (away && away.play) endAway(true);
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
    if (!e.persisted || away || (els.ask && els.ask.open)) return;
    if (holding && !holding.heard) return;
    /* Playing somewhere else it stays so. */
    if (shown(els.message) && tryAgain === playHere) return;
    wantPlaying = false;
    begin();
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
  setupPlayOn();
  begin();
})();
