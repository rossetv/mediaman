/**
 * The TV's own page. Pairs this TV with a code, plays what its person sends it and answers the
 * remote's keys. Talks to /api/tv/device with the TV's token alone; no session, no account.
 *
 * ES5 and XMLHttpRequest only: the browsers of TVs from 2016 run it, so nothing of the app's
 * other scripts is used but watch/loudness.js, ES5 too, and nothing is loaded from elsewhere.
 */
(function () {
  'use strict';

  var API = '/api/tv/device/';
  var KEPT = 'mm-tv';
  var KEPT_PLACE = 'mm-tv-place';
  var KEPT_RESUME = 'mm-tv-resume';
  var KEPT_VERSION = 'mm-tv-version';  // of the scripts this page last loaded itself anew for
  var MAX_CODES = 12;        // shown by themselves, with no key pressed
  var RESTARTED_MS = 8000;
  var LOST_AFTER = 8000;
  var OVERLAY_MS = 4000;
  var STALL_MS = 1500;
  var NET_GRACE_MS = 1500;   // a falter as a film starts or goes on is not yet worth a word
  var PUT_AGAIN_MS = 1500;
  var GIVE_UP_MS = 4000;
  var BROKEN_MS = 45000;
  var PAUSED_DIM_MS = 300000;
  var IDLE_DIM_MS = 600000;
  var MOVE_MS = 60000;
  var LIT_MS = 500;
  var SKIPPED_MS = 3000;
  var STORE_MS = 2000;       // what the TV keeps of what plays is written no more often than this
  var LEAVE_MS = 450;        // the page before fades out for as long as tv.css takes, and a little more
  // A wait shown this long darkens the film and takes its middle. Until then the pill alone says
  // so: a dropped Wi-Fi or a stream filling again is over long before, and is not worth more.
  var WAIT_LONG_MS = 60000;

  var body = document.body;
  var stage = document.getElementById('s');
  var leaving = document.getElementById('o');
  var notes = document.getElementById('k');
  var video = document.getElementById('v');
  var base = body.getAttribute('data-base') || (location.protocol + '//' + location.host);
  var version = body.getAttribute('data-v');

  // A TV sets its clock while the page runs. Date alone, where there is no other, never goes back.
  var latest = Date.now(), behind = 0;
  function now() {
    if (window.performance && performance.now) return performance.now();
    var at = Date.now();
    if (at < latest) behind += latest - at;
    latest = at;
    return at + behind;
  }

  /* ── words ── */

  function esc(text) {
    return String(text === null || text === undefined ? '' : text).replace(/[&<>"']/g, function (c) {
      return '&#' + c.charCodeAt(0) + ';';
    });
  }
  function clock(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
    return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (x < 10 ? '0' : '') + x;
  }
  function episode(of, name) {
    var parts = [];
    if (of.season !== null && of.season !== undefined) parts.push('S' + of.season);
    if (of.episode !== null && of.episode !== undefined) parts.push('E' + of.episode);
    if (name) parts.push(esc(name));
    return parts.join(' &middot; ');
  }
  function under(of) {
    return of.episode_title || of.season !== null && of.season !== undefined ?
      episode(of, of.episode_title) : esc(of.year);
  }

  /* ── QR code: byte mode, level M ── */

  // For each version from 1: codewords in all, per block for correction, and blocks.
  var QR_SIZES = [[26, 10, 1], [44, 16, 1], [70, 26, 1], [100, 18, 2], [134, 24, 2], [172, 16, 4],
    [196, 18, 4], [242, 22, 4], [292, 22, 5], [346, 26, 5]];
  var QR_ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42],
    [6, 26, 46], [6, 28, 50]];

  function gfMultiply(a, b) {
    var product = 0;
    for (var i = 7; i >= 0; i--) {
      product = (product << 1) ^ ((product >>> 7) * 0x11d);
      product ^= ((b >>> i) & 1) * a;
    }
    return product;
  }

  function qrCorrection(data, degree) {
    var divisor = [], i, j, root = 1;
    for (i = 0; i < degree; i++) divisor.push(i === degree - 1 ? 1 : 0);
    for (i = 0; i < degree; i++) {
      for (j = 0; j < degree; j++) {
        divisor[j] = gfMultiply(divisor[j], root);
        if (j + 1 < degree) divisor[j] ^= divisor[j + 1];
      }
      root = gfMultiply(root, 2);
    }
    var rest = [];
    for (i = 0; i < degree; i++) rest.push(0);
    for (i = 0; i < data.length; i++) {
      var factor = data[i] ^ rest.shift();
      rest.push(0);
      for (j = 0; j < degree; j++) rest[j] ^= gfMultiply(divisor[j], factor);
    }
    return rest;
  }

  function qrCodewords(bytes, version) {
    var size = QR_SIZES[version - 1], blocks = size[2], perBlock = size[1];
    var room = size[0] - perBlock * blocks;
    var bits = [], i, j;
    function put(value, length) { for (var n = length - 1; n >= 0; n--) bits.push((value >>> n) & 1); }
    put(4, 4);
    put(bytes.length, version < 10 ? 8 : 16);
    for (i = 0; i < bytes.length; i++) put(bytes[i], 8);
    put(0, Math.min(4, room * 8 - bits.length));
    put(0, (8 - bits.length % 8) % 8);
    for (i = 0; bits.length < room * 8; i++) put(i % 2 ? 0x11 : 0xec, 8);
    var data = [];
    for (i = 0; i < bits.length; i += 8) {
      var word = 0;
      for (j = 0; j < 8; j++) word = word << 1 | bits[i + j];
      data.push(word);
    }
    // The later blocks are one longer where the data does not divide.
    var short = Math.floor(room / blocks), longer = room % blocks, parts = [], fixes = [], at = 0;
    for (i = 0; i < blocks; i++) {
      var part = data.slice(at, at += short + (i >= blocks - longer ? 1 : 0));
      parts.push(part);
      fixes.push(qrCorrection(part, perBlock));
    }
    var out = [];
    for (i = 0; i <= short; i++) for (j = 0; j < blocks; j++) if (i < parts[j].length) out.push(parts[j][i]);
    for (i = 0; i < perBlock; i++) for (j = 0; j < blocks; j++) out.push(fixes[j][i]);
    return out;
  }

  /* Rows of 0 and 1, or null where the text is too long. */
  function qr(text) {
    var utf8 = unescape(encodeURIComponent(text)), bytes = [], i, j, k;
    for (i = 0; i < utf8.length; i++) bytes.push(utf8.charCodeAt(i));
    var version = 1;
    for (; version <= 10; version++) {
      var s = QR_SIZES[version - 1];
      if (12 + (version > 9 ? 8 : 0) + bytes.length * 8 <= (s[0] - s[1] * s[2]) * 8) break;
    }
    if (version > 10) return null;
    var n = 17 + version * 4, grid = [], fixed = [];
    for (i = 0; i < n; i++) {
      grid.push([]); fixed.push([]);
      for (j = 0; j < n; j++) { grid[i].push(0); fixed[i].push(false); }
    }
    function set(x, y, dark) { grid[y][x] = dark ? 1 : 0; fixed[y][x] = true; }
    function ring(d) { return Math.max(Math.abs(d[0]), Math.abs(d[1])); }
    function finder(x, y) {
      for (var dy = -4; dy <= 4; dy++) for (var dx = -4; dx <= 4; dx++) {
        if (x + dx < 0 || x + dx >= n || y + dy < 0 || y + dy >= n) continue;
        var r = ring([dx, dy]);
        set(x + dx, y + dy, r !== 2 && r !== 4);
      }
    }
    for (i = 0; i < n; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    finder(3, 3); finder(n - 4, 3); finder(3, n - 4);
    var align = QR_ALIGN[version - 1], last = align.length - 1;
    for (i = 0; i <= last; i++) for (j = 0; j <= last; j++) {
      if (!i && !j || !i && j === last || i === last && !j) continue;
      for (var ay = -2; ay <= 2; ay++) for (var ax = -2; ax <= 2; ax++) {
        set(align[i] + ax, align[j] + ay, ring([ax, ay]) !== 1);
      }
    }
    function format(mask) {
      // Level M is 0.
      var rest = mask;
      for (var f = 0; f < 10; f++) rest = (rest << 1) ^ ((rest >>> 9) * 0x537);
      var word = (mask << 10 | rest) ^ 0x5412;
      function bit(b) { return (word >>> b) & 1; }
      for (f = 0; f <= 5; f++) set(8, f, bit(f));
      set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
      for (f = 9; f < 15; f++) set(14 - f, 8, bit(f));
      for (f = 0; f < 8; f++) set(n - 1 - f, 8, bit(f));
      for (f = 8; f < 15; f++) set(8, n - 15 + f, bit(f));
      set(8, n - 8, 1);
    }
    format(0);
    if (version >= 7) {
      var left = version;
      for (i = 0; i < 12; i++) left = (left << 1) ^ ((left >>> 11) * 0x1f25);
      var about = version << 12 | left;
      for (i = 0; i < 18; i++) {
        var a = n - 11 + i % 3, b = Math.floor(i / 3);
        set(a, b, (about >>> i) & 1); set(b, a, (about >>> i) & 1);
      }
    }
    var words = qrCodewords(bytes, version), at = 0;
    for (var right = n - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (k = 0; k < n; k++) for (j = 0; j < 2; j++) {
        var x = right - j, y = ((right + 1) & 2) === 0 ? n - 1 - k : k;
        if (fixed[y][x] || at >= words.length * 8) continue;
        grid[y][x] = (words[at >>> 3] >>> (7 - (at & 7))) & 1;
        at++;
      }
    }
    function flip(m, px, py) {
      switch (m) {
        case 0: return (px + py) % 2;
        case 1: return py % 2;
        case 2: return px % 3;
        case 3: return (px + py) % 3;
        case 4: return (Math.floor(px / 3) + Math.floor(py / 2)) % 2;
        case 5: return px * py % 2 + px * py % 3;
        case 6: return (px * py % 2 + px * py % 3) % 2;
        default: return ((px + py) % 2 + px * py % 3) % 2;
      }
    }
    function masked(m) {
      for (var my = 0; my < n; my++) for (var mx = 0; mx < n; mx++) {
        if (!fixed[my][mx] && flip(m, mx, my) === 0) grid[my][mx] ^= 1;
      }
    }
    function penalty() {
      var bad = 0, dark = 0, lines = [], p, q;
      for (p = 0; p < n; p++) {
        var row = '', column = '';
        for (q = 0; q < n; q++) { row += grid[p][q]; column += grid[q][p]; dark += grid[p][q]; }
        lines.push(row, column);
      }
      for (p = 0; p < lines.length; p++) {
        var runs = lines[p].match(/0{5,}|1{5,}/g) || [];
        for (q = 0; q < runs.length; q++) bad += runs[q].length - 2;
        for (q = 0; q + 11 <= n; q++) {
          var piece = lines[p].substr(q, 11);
          if (piece === '10111010000' || piece === '00001011101') bad += 40;
        }
      }
      for (p = 1; p < n; p++) for (q = 1; q < n; q++) {
        var four = grid[p][q] + grid[p - 1][q] + grid[p][q - 1] + grid[p - 1][q - 1];
        if (four === 0 || four === 4) bad += 3;
      }
      return bad + Math.floor(Math.abs(dark * 20 / (n * n) - 10)) * 10;
    }
    var best = 0, least = Infinity;
    for (var m = 0; m < 8; m++) {
      masked(m); format(m);
      var worth = penalty();
      if (worth < least) { least = worth; best = m; }
      masked(m);
    }
    masked(best); format(best);
    return grid;
  }

  function qrPicture(text) {
    var rows = qr(text);
    if (!rows) return '';
    var d = '';
    for (var y = 0; y < rows.length; y++) for (var x = 0; x < rows.length; x++) {
      if (rows[y][x]) d += 'M' + (x + 4) + ' ' + (y + 4) + 'h1v1h-1z';
    }
    var side = rows.length + 8;
    return '<div class="qr" role="img" aria-label="QR code to pair this TV"><svg viewBox="0 0 ' + side + ' ' + side +
      '" shape-rendering="crispEdges" aria-hidden="true"><path d="' + d + '"/></svg></div>';
  }

  /* ── the server ── */

  // `done` is given the answer's status (0 where there was none), its body and its Retry-After.
  function send(path, token, data, wait, done) {
    var xhr = new XMLHttpRequest(), over = false;
    function end() {
      if (over) return;
      over = true;
      clearTimeout(late);
      var answer = null;
      try { answer = JSON.parse(xhr.responseText); } catch (e) { answer = null; }
      var status = xhr.status;
      reached = status > 0;
      // A proxy's own page is not an answer.
      if (!answer || typeof answer !== 'object') { answer = {}; status = 0; }
      done(status, answer, status === 429 ? Number(xhr.getResponseHeader('Retry-After')) || 10 : 0);
    }
    // Some TVs have no xhr.timeout.
    var late = setTimeout(function () {
      if (over) return;
      over = true;
      reached = false;
      xhr.abort();
      done(0, {}, 0);
    }, wait);
    xhr.onreadystatechange = function () { if (xhr.readyState === 4) end(); };
    xhr.open('POST', API + path, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('X-TV-Token', token || 'new');
    xhr.send(JSON.stringify(data));
  }

  function remember(key, value) {
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch (e) { /* Kept for as long as the page is open. */ }
  }
  function remembered(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  /* ── what this TV is ── */

  var token = null;          // the paired TV's
  var pending = null;        // the code shown: {token, code, until, ranOut}
  var me = null;             // {name, owner, people, avatars}
  var play = null;           // as the last poll gave it
  var cutOff = null;         // whose film ended as this TV's token was refused
  var mode = 'boot';         // what the page shows while nothing plays
  var codes = 0;             // since the last key
  var waitUntil = 0;         // no new code before
  var heardAt = now();
  var away = false;
  var reached = true;        // whether mediaman's address gave the last answer, though not one of its own

  /* ── pairing ── */

  var asking = false;
  var pairings = 0;          // counts up as this TV is paired or unpairs itself

  function newCode(ranOut) {
    if (asking) return;
    if (ranOut && codes >= MAX_CODES) { pending = null; mode = 'press'; show(); return; }
    asking = true;
    codes += 1;
    send('code', token, {}, 15000, function (status, got, retry) {
      asking = false;
      if (status === 200) {
        heard();
        var old = pending;
        var fresh = pending = { token: got.token, code: got.code, until: now() + got.expires_in_s * 1000, ranOut: Boolean(ranOut) };
        waitUntil = 0;
        if (mode !== 'conn' && mode !== 'taken') mode = 'code';
        pollPending(fresh);
        if (old) pollOld(old, 0);
      } else {
        codes -= 1;
        // A code that still works stays up.
        if (pending && pending.until > now()) return;
        pending = null;
        waitUntil = now() + (retry || 5) * 1000;
        if (status === 429) { heard(); mode = 'slow'; } else missed(true);
        setTimeout(function () {
          if (!pending && (mode === 'slow' || mode === 'boot' || mode === 'code')) newCode(ranOut);
        }, (retry || 5) * 1000);
      }
      show();
    });
  }

  /* A code that was shown before may still be entered. */
  function pollOld(old, wait) {
    var of = pairings;
    setTimeout(function () {
      if (of !== pairings || now() >= old.until) return;
      send('poll', old.token, {}, 10000, function (status, got, retry) {
        if (of !== pairings || status === 404) return;
        if (status === 200 && got.state === 'ready') { paired(old, got); return; }
        if (status === 200 && got.state === 'joined') { joined(); return; }
        pollOld(old, retry ? retry * 1000 : got.poll_ms || 2000);
      });
    }, wait);
  }

  function pollPending(mine) {
    setTimeout(function () {
      if (pending !== mine) return;
      if (now() >= mine.until) { codeGone(); return; }
      send('poll', mine.token, {}, 10000, function (status, got, retry) {
        if (pending !== mine) return;
        if (status === 200) heard();
        if (status === 200 && got.state === 'ready') { paired(mine, got); return; }
        if (status === 200 && got.state === 'joined') { joined(); return; }
        if (status === 404) { codeGone(); return; }
        // Unpaired, or paired anew and about to be told so: the code is good either way.
        if (status === 200 && mode === 'boot' && !token) { mode = 'code'; show(); }
        if (status === 0 || status >= 500) missed(false);
        mine.gap = retry ? retry * 1000 : got.poll_ms;
        pollPending(mine);
      });
    }, mine.gap || 2000);
  }

  function codeGone() {
    pending = null;
    if (mode === 'code') newCode(true);
    else if (mode === 'boot' && !token) signedOut();
    show();
  }

  function paired(mine, got) {
    var from = cutOff || starter();
    var other = Boolean(from) && from !== got.owner;
    cutOff = null;
    pending = null;
    pairings += 1;
    token = mine.token;
    remember(KEPT, token);
    remember(KEPT_PLACE, null);
    keepResume(null);
    codes = 0;
    lastDone = 0;
    play = null;
    unload();
    mode = other ? 'taken' : 'conn';
    if (other) setTimeout(function () { if (mode === 'taken') { mode = 'conn'; show(); } }, 4000);
    loop += 1;
    pollSoon(got.poll_ms);
    take(got);
  }

  /* Someone joined the people this TV is paired with: it goes on as it was, and says who they are. */
  function joined() {
    pending = null;
    pairings += 1;
    codes = 0;
    tell();
    // The code stays until the poll says who joined, which their page is drawn with at once.
    if (play) show();
    else mode = 'conn';
  }

  /* ── the poll ── */

  var loop = 0, pollTimer = null, polling = false, again = false, polledAt = 0;
  var lastDone = 0, active = false;

  function heard() {
    heardAt = now();
    away = false;
  }
  function missed(always) {
    if (always || now() - heardAt > LOST_AFTER) away = true;
    show();
  }

  function pollSoon(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms);
  }
  /* Tell the server now, though never twice in half a second. */
  function tell() {
    if (polling) again = true;
    else if (token) pollSoon(Math.max(0, polledAt + 500 - now()));
  }

  function poll() {
    if (!token || polling) return;
    var mine = loop;
    var data = { ack: lastDone, active: active };
    if (play && play.url && loaded === play.play_id) try { data.report = report(); } catch (e) { /* Nothing to report. */ }
    else if (letGo && letGo.held && letGo.report) data.report = letGo.report;
    active = false;
    polling = true;
    polledAt = now();
    send('poll', token, data, 10000, function (status, got, retry) {
      polling = false;
      if (mine !== loop || !token) { if (token) pollSoon(1000); return; }
      if (status === 404 && got.error === 'not_found') { heard(); refused(); return; }
      if (status !== 200) {
        if (data.active) active = true;
        pollSoon(retry ? retry * 1000 : 2000);
        if (status === 429) heard(); else missed(false);
        return;
      }
      var wasAway = now() - heardAt > LOST_AFTER;
      heard();
      // First, so that nothing a TV's browser throws below ends the polling.
      pollSoon(again ? 500 : got.poll_ms || 3000);
      again = false;
      take(got, wasAway);
    });
  }

  function refused() {
    token = null;
    loop += 1;
    cutOff = starter();
    play = null;
    unload();
    // Someone may have entered the code: what it answers decides.
    if (pending && pending.until > now()) { if (mode !== 'code') mode = 'boot'; } else signedOut();
    show();
  }

  function signedOut() {
    token = cutOff = null;
    me = null;
    remember(KEPT, null);
    remember(KEPT_PLACE, null);
    keepResume(null);
    mode = 'out';
  }

  var boot;                  // which run of mediaman last answered
  var endedWith = null;      // what played as it restarted

  function restarted(was) {
    endedWith = was;
    mode = 'restarted';
    setTimeout(function () {
      if (mode === 'restarted' && endedWith === was) { mode = 'idle'; show(); }
    }, RESTARTED_MS);
  }

  function take(got, wasAway) {
    me = { name: got.name, owner: got.owner, people: got.people || [], avatars: got.avatars || {} };
    if (mode === 'boot') mode = 'idle';
    if (letGo) got = stillLetGo(got);
    var was = play;
    var lost = lostPlay(was, got, wasAway);
    // Where it was, before the page lets the film go.
    var at = lost && was && loaded === was.play_id ? position() : lastPlace({ play_id: lost && lost.id, start_s: null });
    var paused = was ? !wanted : lost && lost.paused;
    if (lost && was && loaded === was.play_id) still();
    playing(got.play);
    if (lost) resume(lost, at, paused, got.boot, Boolean(was));
    else if (was && !got.play && boot !== got.boot) restarted(was);
    if (!lost && resuming !== got.boot) keepResume(got.play && got.resume ? { ticket: got.resume, id: got.play.play_id, boot: got.boot, title: got.play.title } : null);
    boot = got.boot;
    obey(got.commands || []);
    show();
    renew(got);
  }

  /* The page stays open through mediaman's updates: once nothing plays, it loads what is new. A
     page that comes back as it was, from a cache, is not loaded again for the same version. */
  function renew(got) {
    if (!version || !got.v || got.v === version || got.play || mode !== 'idle') return;
    if (remembered(KEPT_VERSION) === got.v) return;
    remember(KEPT_VERSION, got.v);
    location.reload();
  }

  /* A film stopped on the TV through a long wait, while mediaman could not be told: the TV is ready
     at once, and mediaman, once it answers, is told where the film got to and that it stopped. */
  var letGo = null;          // {id, report, held, asking}

  function letItGo() {
    var at = null;
    if (loaded === play.play_id) try { at = report(); at.state = 'paused'; } catch (e) { at = null; }
    letGo = { id: play.play_id, report: at, held: false, asking: false };
    play = null;
    unload();
    pickingUp = false;
    mode = 'idle';
  }
  /* What the answer is, the film let go of left out of it. */
  function stillLetGo(got) {
    // It is not picked up again.
    var kept = keptResume();
    if (kept && kept.id === letGo.id) forgetResume();
    if (!got.play || got.play.play_id !== letGo.id) {
      letGo = null;
      return got;
    }
    // Where it got to goes with the next poll, while mediaman is known to hold it; then the stop.
    if (!letGo.held && letGo.report) { letGo.held = true; tell(); }
    else if (!letGo.asking) {
      var mine = letGo;
      mine.asking = true;
      send('ask', token, { play_id: mine.id, what: 'stop' }, 10000, function (status) {
        mine.asking = false;
        if (status === 200) tell();
      });
    }
    got.play = got.resume = null;
    return got;
  }

  /* ── going on after mediaman, or the TV's network, dropped out ── */

  var resumable = null;      // {ticket, id, boot, title, paused}: the server's word that what plays may go on
  var resuming = null;       // the run of mediaman asked to go on, until it answers
  var resumingFrom = false;  // whether mediaman restarted, rather than the network dropped out
  var askAgain = null;       // the pick-up mediaman did not answer, asked again as it next answers
  var pickingUp = false;     // the film that was lost goes on over its own last picture, until it plays

  var storedAt = 0;

  function keepResume(kept) {
    var was = resumable, at = now();
    if (document.hidden) kept = null;
    if (kept) kept.paused = !wanted;
    var changed = !was !== !kept || Boolean(kept) && (was.id !== kept.id || was.boot !== kept.boot || was.paused !== kept.paused);
    resumable = kept;
    // A TV's storage is slow to write: as what it holds changes, and its ticket as often as its
    // place, which mediaman holds a pick-up close to.
    if (changed || (kept && at - storedAt > STORE_MS)) {
      storedAt = at;
      remember(KEPT_RESUME, kept && JSON.stringify(kept));
    }
  }
  function forgetResume() {
    resumable = null;
    remember(KEPT_RESUME, null);
  }
  /* A page put away, as the TV goes to standby or another app is opened, was left on purpose: what
     played may have gone to a phone meanwhile, so it is not picked up as the TV comes back. A restart
     or a dropped network while the page is on screen keeps it. */
  document.addEventListener('visibilitychange', function () { if (document.hidden) forgetResume(); });
  if (window.addEventListener) window.addEventListener('pagehide', forgetResume);

  function keptResume() {
    if (resumable) return resumable;
    try { return JSON.parse(remembered(KEPT_RESUME) || 'null'); } catch (e) { return null; }
  }

  /* What played and was lost, where it is to go on: mediaman restarted, or the network dropped out
     for longer than mediaman holds a play. A page opened again knows only of a restart. */
  function lostPlay(was, got, wasAway) {
    var kept = keptResume();
    if (got.play || !kept || !kept.ticket || resuming === got.boot) return null;
    if (was && was.play_id !== kept.id) return null;
    if (askAgain === kept) return kept;
    return kept.boot !== got.boot || (was && wasAway) ? kept : null;
  }

  /* `seen`: whether this page was showing the film as it was lost. One opened afresh has nothing to
     tell of a pick-up that was refused. */
  function resume(kept, at, paused, run, seen) {
    endedWith = { title: kept.title, state: 'playing' };
    resuming = run;
    resumingFrom = kept.boot !== run;
    // Asked again, it goes on over the same last picture.
    pickingUp = seen || (pickingUp && askAgain === kept);
    mode = 'resuming';
    var wish = { play_id: kept.id, what: 'resume', ticket: kept.ticket, paused: Boolean(paused) };
    if (at) wish.position_s = Math.max(0, at);
    send('ask', token, wish, 110000, function (status) {
      if (mode !== 'resuming') return;
      askAgain = null;
      if (status === 200) { tell(); return; }
      // Only mediaman's own no ends the pick-up: a proxy's error, a lost answer or a busy Plex do not.
      if (!status || status === 429 || status >= 500) {
        resuming = null;
        resumable = askAgain = kept;
        return;
      }
      // From the store too, where a page opened afresh found it: it is refused once.
      forgetResume();
      resuming = null;
      pickingUp = false;
      if (resumingFrom && seen) restarted(endedWith); else mode = 'idle';
      show();
    });
  }

  /* ── what is to play ── */

  var asked = null;          // the play a key or the countdown has asked to go on from
  var askNote = '';
  var stopping = '';         // words over a film that was stopped, until the server ends it
  var endsAt = 0;            // of the countdown
  var tries = 0;             // of a play whose stream ended under it, by itself
  var triedPlay = null;

  function playing(next) {
    var was = play;
    play = next;
    if (!next) {
      if (was) { unload(); if (mode !== 'conn' && mode !== 'code' && mode !== 'taken') mode = 'idle'; }
      asked = null;
      return;
    }
    if (mode === 'conn' || mode === 'unpair' || mode === 'taken' || mode === 'code' || mode === 'restarted' || mode === 'resuming') mode = 'idle';
    resuming = askAgain = null;
    if (!was || was.play_id !== next.play_id) {
      unload();
      rouse();
      asked = null;
      askNote = stopping = '';
      wanted = !next.paused;
      counted = next.played_s || 0;
      breaks = 0;
      skipped = {};
      upCard = null;
      if (loud && (next.reduce_loud || loud.on())) loud.set(Boolean(next.reduce_loud));
    }
    endsAt = next.upnext_in_s === null || next.upnext_in_s === undefined ? 0 : now() + next.upnext_in_s * 1000;
    if (next.url && loaded !== next.play_id) load(lastPlace(next));
    else if (!next.url && loaded) unload();
    else if (reloadDue) { reloadDue = false; load(position()); }
    // The countdown over the credits ran out: mediaman's own is not shown again.
    if (ranOut === next.play_id && next.state === 'upnext' && endsAt && asked !== next.play_id && now() >= autoAt) ask('auto');
    if (next.state === 'failed' && next.error === 'gone' && triedPlay !== next.play_id && tries < 2) {
      triedPlay = next.play_id;
      tries += 1;
      ask('again', true);
    }
  }

  /* Where to start: a page that was closed and opened again goes on from where it was. */
  function lastPlace(of) {
    var kept = (remembered(KEPT_PLACE) || '').split(' ');
    return kept[0] === of.play_id && Number(kept[1]) || of.start_s;
  }

  function ask(what, byItself) {
    if (!play) return;
    var id = play.play_id;
    var stop = what === 'stop';
    // What starts something has the page wait for it.
    asked = stop || what === 'cancel' ? null : id;
    // A countdown's tries again keep the word on why the last could not start.
    if (what !== 'auto') askNote = '';
    if (stop) {
      stopping = 'Stopping';
      if (loaded) hold();
    }
    var wish = { play_id: id, what: what };
    if (what === 'again') wish.paused = Boolean(byItself) && !wanted;
    send('ask', token, wish, stop ? 10000 : 110000, function (status, got) {
      var same = play && play.play_id === id;
      // Gone on from credits counted down here, it waits for the next episode, not their card.
      if (asked === id && !(status === 200 && ranOut === id)) asked = null;
      // Until a poll says what came of it.
      if (what === 'auto') autoAt = now() + 5000;
      if (same && status !== 200 && got.error !== 'stale') {
        askNote = status ? WHY[got.error] || WHY.refused : WHY.silent;
        if (stopping && !status) stopping = 'mediaman isn’t answering. Press Stop again.';
      }
      tell();
      show();
    });
    show();
  }
  var autoAt = 0;

  function obey(commands) {
    var did = false;
    for (var i = 0; i < commands.length; i++) {
      var c = commands[i];
      if (c.id <= lastDone) continue;
      lastDone = c.id;
      did = true;
      if (!play || loaded !== play.play_id) continue;
      try { carryOut(c); } catch (e) { /* This browser cannot. */ }
    }
    if (did) { rouse(); tell(); }
  }
  function carryOut(c) {
    if (c.kind === 'play') go();
    else if (c.kind === 'pause') hold();
    else if (c.kind === 'seek') seekAsked(c.position_s);
    else if (c.kind === 'volume') setVolume(c.level);
    else if (c.kind === 'mute') video.muted = c.muted;
  }

  /* ── the video ── */

  var loaded = null;         // the play whose stream the video has
  var hls = null;
  var byHls = null;          // whether hls.js plays, once it is known
  var wanted = true;         // whether it is to play, rather than be paused
  var ready = false;
  var there = false;         // at its place
  var place = 0;
  var puts = 0, putAt = 0;   // times it was put there, and when last
  var begunThere = false;    // whether it has played since it was first put there
  var blocked = false;       // the browser wants a key first
  var counted = 0;           // seconds played
  var stalled = false;
  var keptAt = 0;
  var movedAt = 0, lastTime = 0, lastTick = now();
  var breaks = 0, reloadDue = false, broken = false, overAt = null;
  var falteredAt = 0;
  var canVolume = null;
  // Reduce loud sounds, as whoever started what plays chose; none where the browser cannot.
  var loud = window.MM && window.MM.loudness ? window.MM.loudness.create(video) : null;

  function native() {
    return Boolean(video.canPlayType && (video.canPlayType('application/vnd.apple.mpegurl') || video.canPlayType('application/x-mpegURL')));
  }
  function sources() {
    var M = window.MediaSource || window.WebKitMediaSource;
    return Boolean(M && M.isTypeSupported && M.isTypeSupported('video/mp4; codecs="avc1.42E01E,mp4a.40.2"'));
  }

  function unload() {
    loaded = null;
    ready = there = blocked = stalled = broken = reloadDue = false;
    overAt = null;
    if (hls) { hls.destroy(); hls = null; }
    if (video.getAttribute('src')) {
      try { video.pause(); } catch (e) { /* Nothing played. */ }
      video.removeAttribute('src');
      video.load();
    }
  }

  function load(from) {
    unload();
    loaded = play.play_id;
    place = from || 0;
    puts = 0;
    begunThere = false;
    movedAt = now();
    if (byHls === null) byHls = !native() && sources();
    if (byHls) loadHls(play.play_id, play.url);
    else {
      video.src = play.url;
      video.load();
      start();
    }
  }

  function loadHls(id, url) {
    if (!window.Hls) {
      var script = document.createElement('script');
      script.src = body.getAttribute('data-hls');
      script.onload = function () { if (loaded === id && window.Hls) loadHls(id, url); };
      script.onerror = function () { if (loaded === id) broke(); };
      body.appendChild(script);
      return;
    }
    var h = hls = new window.Hls({ enableWorker: false, startPosition: place, maxBufferLength: 30 });
    h.on(window.Hls.Events.ERROR, function (event, data) { if (h === hls && data.fatal) broke(); });
    h.on(window.Hls.Events.MANIFEST_PARSED, function () { if (h === hls) start(); });
    h.loadSource(url);
    h.attachMedia(video);
  }

  function atPlace() {
    if (there || !loaded || video.seeking) return;
    // A browser that lands elsewhere each time, as it plays, is left where it lands.
    var off = Math.abs(video.currentTime - place) >= 3;
    if (!off || (puts > 1 && begunThere && wanted && now() - putAt > GIVE_UP_MS)) there = true;
    else if (!puts || (puts < 2 && now() - putAt > PUT_AGAIN_MS)) putThere();
  }
  function putThere() {
    puts += 1;
    putAt = now();
    try { video.currentTime = place; } catch (e) { /* Too early; canplay comes. */ }
  }

  function start() {
    if (wanted) go();
  }

  function go() {
    wanted = true;
    blocked = false;
    stopping = '';
    movedAt = now();
    var mine = loaded;
    if (overAt !== null) return;
    var promise = video.play();
    if (promise && promise.then) {
      promise.then(null, function (e) {
        if (mine === loaded && e && e.name === 'NotAllowedError') needsKey();
      });
    }
    show();
  }
  function needsKey() {
    blocked = ready = true;
    wanted = false;
    show();
    tell();
  }
  function hold() {
    wanted = false;
    video.pause();
    show();
  }
  function seek(to) {
    var end = video.duration || (play && play.duration_s) || 0;
    if (end) to = Math.min(to, end - 1);
    place = Math.max(0, to);
    there = false;
    puts = 0;
    begunThere = false;
    movedAt = now();
    putThere();
    atPlace();
    tick();
  }
  /* A place a person asked for: an intro or credits there are theirs to watch. */
  function seekAsked(to) {
    var list = (play && play.markers) || [];
    for (var i = 0; i < list.length; i++) {
      if (to >= list[i].start_s && to < list[i].end_s) skipped[list[i].start_s] = true;
    }
    seek(to);
  }
  function setVolume(level) {
    video.volume = level;
    canVolume = Math.abs(video.volume - level) < 0.02;
  }

  function position() {
    if (overAt !== null) return overAt;
    return there ? video.currentTime : place;
  }

  function stateNow() {
    if (broken) return 'error';
    if (overAt !== null) return 'ended';
    if (!ready) return 'loading';
    if (!wanted) return 'paused';
    return stalled ? 'buffering' : 'playing';
  }

  function report() {
    if (canVolume === null) {
      var was = video.volume;
      setVolume(was === 1 ? 0.99 : 1);
      video.volume = was;
    }
    var out = {
      play_id: play.play_id, state: stateNow(), position_s: position() || 0, played_s: counted,
      volume: video.volume, muted: Boolean(video.muted), can_volume: canVolume,
    };
    if (overAt !== null && ranOut === play.play_id) out.counted = true;
    return out;
  }

  /* The stream stopped. Try once more when the server next answers, and say so if that fails. */
  function broke() {
    if (!loaded || broken || reloadDue || overAt !== null) return;
    var from = position();
    if (!ready && byHls === false && sources()) byHls = true;
    breaks += 1;
    if (breaks > 2) { broken = true; tell(); show(); return; }
    place = from;
    there = false;
    stalled = true;
    reloadDue = true;
    falteredAt = now();
    show();
  }

  /* A browser may take no place until it plays. */
  function triesAnew() {
    if (!begunThere) puts = 0;
    begunThere = true;
  }
  function begun() {
    if (!ready) begunThere = false;
    triesAnew();
    atPlace();
    if (!ready) wake();
    ready = true;
    stalled = blocked = false;
    movedAt = now();
    show();
    tell();
  }

  function finish() {
    overAt = (play && play.duration_s) || video.duration || position();
    show();
    tell();
  }

  /* A film loaded paused is ready, however little its browser loads before it plays. */
  function readyPaused() {
    if (!loaded || ready || wanted) return;
    ready = true;
    show();
    tell();
  }

  video.addEventListener('loadedmetadata', function () {
    // What was put before the length was known may be lost.
    puts = 0;
    atPlace();
    readyPaused();
  });
  video.addEventListener('canplay', atPlace);
  video.addEventListener('seeked', atPlace);
  video.addEventListener('playing', function () {
    // It plays from the start for a moment before it is at its place.
    if (loaded && !video.seeking) begun();
  });
  video.addEventListener('pause', function () {
    if (!loaded || !ready || video.ended || reloadDue || broken || overAt !== null) return;
    wanted = false;
    if (there) keepPlace(video.currentTime);
    show();
    tell();
  });
  video.addEventListener('play', function () {
    if (!loaded || !ready) return;
    wanted = true;
    blocked = false;
    show();
    tell();
  });
  video.addEventListener('ended', function () {
    if (loaded && ready) finish();
  });
  video.addEventListener('error', function () {
    if (!hls && video.getAttribute('src')) broke();
  });

  /* Where the film is, kept on the TV itself: mediaman may be away when it is wanted. */
  function keepPlace(time) {
    keptAt = now();
    remember(KEPT_PLACE, play.play_id + ' ' + Math.floor(time));
  }

  function tick() {
    var at = now(), since = Math.min(2000, Math.max(0, at - lastTick));
    lastTick = at;
    if (loaded && overAt === null && !broken && !reloadDue) {
      var time = video.currentTime;
      // As a film plays on, which a seek's jump is not; a browser may not say that it plays.
      var moving = wanted && !video.paused && !video.seeking && time > lastTime && time - lastTime < 2;
      if (moving) triesAnew();
      if (!there && (moving || (ready && wanted))) atPlace();
      if (!wanted && at - movedAt > 5000) readyPaused();
      if (time !== lastTime || (ready && !wanted)) {
        movedAt = at;
        if (ready && wanted && !video.paused) counted += since / 1000;
        if (stalled) { stalled = false; tell(); show(); }
        if (!ready && moving && there) begun();
      } else if (ready && at - movedAt > STALL_MS && !stalled) {
        stalled = true;
        falteredAt = at;
        tell();
        show();
      }
      // Some browsers stop short of the end and never say so.
      if (stalled && at - movedAt > 5000 && video.duration && time > video.duration - 3) finish();
      else if (wanted && at - movedAt > BROKEN_MS) broke();
      // A minute of play is worth more tries.
      if (counted - (play.played_s || 0) > 60) { breaks = 0; }
      if (ready && wanted && there && time - play.start_s > 60) tries = 0;
      lastTime = time;
      if (there && ready && at - keptAt > STORE_MS) keepPlace(time);
    }
    if (shown === 'slow') show();
    if (pending && mode === 'code') {
      if (now() >= pending.until) codeGone();
      else put('cd', codeLine());
    }
    if (play && play.state === 'upnext' && endsAt && asked !== play.play_id) {
      if (at >= endsAt && at >= autoAt) ask('auto');
      else put('inn', String(nextLeft(endsAt)));
    }
    if (shown === 'credits' && upCard && upCard.until) put('inn', String(nextLeft(upCard.until)));
    if (play) bar();
    if ((stalled || reloadDue) && which() !== shown) show();
    skipMarks();
    rest();
  }
  setInterval(tick, 500);

  /* ── intros and credits ── */

  var skipped = {};          // of the play, by where each starts: a marker is skipped once
  var noteTimer = null;
  var noteText = '';
  var entered = null;        // {play, m, last, at, land, count}: the marker the TV plays in, where in it the TV began it, and its timing
  var LAND_S = { intro: 3, credits: 4 };
  var COUNT_S = 5;
  var NEXT_S = 10;           // the next episode's countdown, as long as mediaman's own
  var upCard = null;         // {play, until}: the next episode, offered over the last credits; until is 0 where it waits for a press
  var ranOut = null;         // the play whose countdown over its credits ran out

  /* Where its person wants it, the TV plays on past an intro or credits: a TV's remote may have no
     key to skip with. It lands a few seconds in, so nothing is cut off at its first frame, and an
     intro or credits a scene follows count down on screen first, which Back or OK stops. Credits
     that end the title, where a next episode follows, play on under the next episode's card. */
  function skipMarks() {
    // A seek, a stop or another play ends a count; a pause, the card, which comes again as it plays.
    if (entered && (!play || entered.play !== play.play_id || !there || video.seeking)) leaveMark();
    if (upCard && !wanted) dropCard();
    if (!play || !play.auto_skip || !loaded || !ready || !there || !wanted || video.seeking || overAt !== null || broken || reloadDue) return;
    var t = video.currentTime, list = play.markers || [];
    var end = video.duration || play.duration_s || 0;
    var goesOn = !!play.next && play.next.season != null;
    for (var i = 0; i < list.length; i++) {
      var m = list[i];
      if (t < m.start_s || t >= m.end_s - 1 || skipped[m.start_s]) continue;
      var last = m.kind === 'credits' && (m.final || (end && m.end_s >= end - 1));
      if (last && !goesOn) continue;
      var name = m.kind === 'intro' ? 'intro' : 'credits';
      if (!entered || entered.m.start_s !== m.start_s || t < entered.at) entered = timed(m, last, name, t);
      var left = entered.land + entered.count - (t - entered.at);
      if (left > 0) {
        if (left <= entered.count) countNote(name, left);
        return;
      }
      if (last) offerNext();
      else jumpPast(m, name);
      return;
    }
    if (entered) leaveMark();
  }

  /* The jump comes before the marker's last second, where nothing is skipped: a short one counts
     less, and one with no room for a count of 2 is jumped past without one. */
  function timed(m, last, name, t) {
    var room = m.end_s - 1.5 - t, land = LAND_S[name], count = last ? 0 : COUNT_S;
    if (!last && land + count > room) {
      count = Math.floor(room - land);
      if (count < 2) { count = 0; land = Math.max(0, Math.min(land, room)); }
    }
    return { play: play.play_id, m: m, last: last, at: t, land: land, count: count };
  }

  function jumpPast(m, name) {
    entered = null;
    skipped[m.start_s] = true;
    seek(m.end_s);
    tell();
    note(name === 'intro' ? 'Skipped intro' : 'Skipped credits');
  }

  /* The card counts down as mediaman's does at the end. Where it runs out, the episode ends where
     it is: a jump to its end would have the stream fill again, for nothing. */
  function offerNext() {
    if (!upCard) {
      upCard = { play: play.play_id, until: play.autoplay === false ? 0 : now() + NEXT_S * 1000 };
      show();
    }
    if (upCard.until && now() >= upCard.until) endEarly();
  }
  function endEarly() {
    upCard = entered = null;
    ranOut = play.play_id;
    finish();
    video.pause();
  }
  function dropCard() {
    upCard = null;
    show();
  }
  /* Watch credits: the card goes, and the credits play on to the end. */
  function watchCredits() {
    if (entered) skipped[entered.m.start_s] = true;
    entered = null;
    dropCard();
  }

  /* Its number ticks where a reader is not read; the seconds are said once, as the note comes.
     "Press" is any key a remote has for it: Back, or OK with the controls hidden. */
  function countNote(name, left) {
    var n = Math.ceil(left);
    var spoken = n + (n === 1 ? ' second' : ' seconds');
    if (!note('Skipping ' + name + ' in <span aria-hidden="true"></span><span class="sr">' + spoken + '. Press to cancel.</span><span aria-hidden="true"> · Press to cancel</span><i class="dr"></i>', n, 'Skipping ' + name)) return;
    var bar = document.getElementById('sk').getElementsByTagName('i')[0];
    if (!bar) return;
    bar.style.webkitAnimationDuration = bar.style.animationDuration = entered.count + 's';
    bar.style.webkitAnimationDelay = bar.style.animationDelay = (left - entered.count) + 's';
  }

  function counting() {
    var node = document.getElementById('sk');
    return !!(entered && play && entered.play === play.play_id && node && node.className === 'sk on' && noteText.indexOf('Skipping') === 0);
  }

  function hideNote() {
    var node = document.getElementById('sk');
    clearTimeout(noteTimer);
    noteText = '';
    if (node) node.className = 'sk';
    room();
  }

  /* The title keeps clear of a note up beside it. */
  function topClass() {
    var node = document.getElementById('sk');
    return node && node.className === 'sk on' ? 'top noted' : 'top';
  }
  function room() {
    var top = document.getElementById('tp');
    if (top && top.className !== topClass()) top.className = topClass();
  }

  /* Out of the marker before its jump, its count goes with it. */
  function leaveMark() {
    entered = null;
    if (noteText.indexOf('Skipping') === 0) hideNote();
    if (upCard) dropCard();
  }

  /* Back or OK during a countdown keeps the intro or credits playing. */
  function keepMark() {
    if (!counting()) return false;
    skipped[entered.m.start_s] = true;
    entered = null;
    hideNote();
    return true;
  }

  /* A note is written once, as it comes: a reader of the status hears it once. Its `key` says
     which note it is where its text changes as it is shown; a count, `n`, ticks in its first span.
     Whether it was written. */
  function note(text, n, key) {
    var node = document.getElementById('sk');
    if (!node) return false;
    key = key || text;
    var wrote = noteText !== key || node.className !== 'sk on';
    if (wrote) node.innerHTML = glyph('step') + text;
    noteText = key;
    if (n !== undefined) node.getElementsByTagName('span')[0].textContent = String(n);
    node.className = 'sk on';
    room();
    clearTimeout(noteTimer);
    noteTimer = setTimeout(function () { node.className = 'sk'; room(); }, SKIPPED_MS);
    return wrote;
  }

  function put(id, text) {
    var node = document.getElementById(id);
    if (node && node.innerHTML !== text) node.innerHTML = text;
  }
  function bar() {
    if (shown !== 'play' && shown !== 'pause') return;
    var total = play.duration_s || video.duration || 0, at = position() || 0;
    var fill = document.getElementById('pr');
    if (fill) fill.style.width = (total ? Math.min(100, at / total * 100) : 0) + '%';
    put('el', clock(at));
    put('re', total ? '-' + clock(total - at) : '');
  }

  /* ── pages ── */

  var WHY = {
    tv: 'The TV couldn’t open the picture. Your place is kept.<br>Try again, or watch it on your phone or computer.',
    gone: 'It stopped on the server. Your place is kept.<br>Try again, or watch it on your phone or computer.',
    silent: 'mediaman isn’t answering.<br>Try again in a moment.',
    unreachable: 'mediaman couldn’t reach Plex. Your place is kept.<br>Try again in a moment.',
    refused: 'mediaman couldn’t do that just now.<br>Try again in a moment.',
    no_next: 'There is no next episode.',
    not_playing: 'This isn’t playing on the TV any more.<br>Start it again on your phone or computer.',
    not_found: 'Plex doesn’t have this one any more.<br>Choose something else.',
    too_many_streams: 'You’re watching on three screens, the most at once.<br>Stop one, then try again.',
    track_unavailable: 'The subtitles chosen aren’t there any more.<br>Choose others on your phone or computer.',
    rate_limited: 'Too many were started in a short time.<br>Wait a minute, then try again.',
    not_set_up: 'mediaman isn’t connected to Plex.<br>Ask an admin to check Settings.',
    superseded: 'Something else was started meanwhile.<br>Try again.',
    unplayable: 'Plex can’t play this file.<br>Choose something else.',
    unplayable_dolby_vision_5: 'Plex can’t convert its picture (Dolby Vision Profile 5).<br>Choose something else.',
  };
  /* What no try can mend: the page offers no Try again for it. */
  var FOR_GOOD = { unplayable: 1, unplayable_dolby_vision_5: 1 };
  var ICONS = {
    play: 'M7 4l13 8-13 8z', pause: 'M6 4h4v16H6zM14 4h4v16h-4z', back: 'M11 5v14l-9-7zM22 5v14l-9-7z',
    fwd: 'M2 5v14l9-7zM13 5v14l9-7z', step: 'M4 4v16l12-8zM17 4h3v16h-3z', stop: 'M5 5h14v14H5z',
  };
  function glyph(name) { return '<svg class="i" viewBox="0 0 24 24"><path d="' + ICONS[name] + '"/></svg>'; }

  // The page holds the name and its first wait as drawn, until the first of these pages takes its place.
  var BRAND = document.querySelector('.brand').outerHTML;
  var BOOT = document.querySelector('.mid').outerHTML;
  var SPIN = '<div class="spin"><i></i></div>';
  var buttons = [];          // what the page offers: [label, action]

  function offer(list) {
    buttons = list;
    var out = '<div class="bs">';
    for (var i = 0; i < list.length; i++) out += '<button type="button" class="b" id="b' + i + '">' + list[i][0] + '</button>';
    return list.length ? out + '</div>' : '';
  }
  function middle(head, lines, list, extra) {
    return '<div class="mid">' + BRAND + (extra || '') + '<div class="h">' + head + '</div><div class="l">' + lines + '</div>' + offer(list) + '</div>';
  }
  /* Whose film plays: who started it, not whose TV it is. */
  function starter() { return play ? play.by : null; }
  function everyone() { return (me.people && me.people.length ? me.people : [me.owner]); }
  /* A person's picture over their letter, which shows where there is none or it fails to load.
     The letter is drawn from an attribute, so the words of the page stay the person's name. */
  function face(name, small) {
    var url = me && me.avatars && me.avatars[name];
    return '<span class="av' + (small ? ' av--s' : '') + '" data-l="' + esc(String(name).charAt(0).toUpperCase()) + '">' +
      (url ? '<img src="' + esc(url) + '" alt="">' : '') + '</span>';
  }
  var FACES_MAX = 7;         // in a row on the TV, the last of them standing for any more
  function faces() {
    var all = everyone();
    var listed = all.length > FACES_MAX ? all.slice(0, FACES_MAX - 1) : all;
    return '<div class="ppl">' + listed.map(function (w) {
      // A long name breaks after a dot or a dash before it breaks inside a word.
      return '<div class="pp">' + face(w) + '<div class="pn">' + esc(w).replace(/([._-])/g, '$1<wbr>') + '</div></div>';
    }).join('') + (listed.length < all.length ? '<div class="pp"><span class="av" data-l="+' + (all.length - listed.length) + '"></span></div>' : '') + '</div>';
  }
  /* A picture that fails to load leaves its letter. */
  function gone() { this.className = 'gone'; }
  function watchFaces() {
    var pictures = stage.getElementsByTagName('img');
    for (var i = 0; i < pictures.length; i++) pictures[i].onerror = gone;
  }
  // A TV that is paired need not pair anew.
  function leave() { return token ? [['Back', 'i']] : []; }
  function page(inner) { return '<div class="page">' + inner + '</div>'; }
  /* The picture, and over it the shade the words need: one layer, so that it fades in whole and
     the picture is never less shaded than it ends. */
  function art(shaded) {
    return '<div class="pic" id="pic">' + (shaded ? '<div class="shade"></div>' : '') + '</div>';
  }
  function titles() {
    return '<div class="t1">' + esc(play.title) + '</div><div class="t2">' + under(play) + '</div>';
  }
  function codeLine() {
    var left = clock((pending.until - now()) / 1000);
    return pending.ranOut ? 'The last code ran out. This one works for ' + left + '.' : 'This code works for ' + left + '.';
  }
  /* Minutes while there are more than one, then seconds by fives: the page is drawn anew only as
     the words change. */
  function waitLeft() {
    var s = Math.max(0, Math.ceil((waitUntil - now()) / 1000));
    if (s > 60) return Math.ceil(s / 60) + ' minutes';
    return Math.max(5, Math.ceil(s / 5) * 5) + ' seconds';
  }
  function nextLeft(until) { return Math.max(0, Math.ceil((until - now()) / 1000)); }
  /* As the skip's count is: drawn once, so that a reader says it once; only its number ticks. */
  var upLine = { of: '', html: '' };
  function nextLine(until, of) {
    if (askNote) return 'Next episode couldn’t start';
    if (!until) return 'Next episode';
    of = play.play_id + ' ' + of;
    if (upLine.of !== of) {
      var n = nextLeft(until);
      upLine = { of: of, html: '<span aria-hidden="true">Next episode in <span id="inn">' + n + '</span> &middot; Press to cancel</span>' +
        '<span class="sr">Next episode in ' + n + (n === 1 ? ' second' : ' seconds') + '. Press to cancel.</span><i class="dr" id="ndr"></i>', left: n };
    }
    return upLine.html;
  }
  function marks() {
    var total = play.duration_s, at = [], left = [], i;
    for (i = 0; i < (play.markers || []).length; i++) at.push(play.markers[i].start_s, play.markers[i].end_s);
    for (i = 0; i < (play.chapters || []).length; i++) at.push(play.chapters[i].start_s);
    for (i = 0; total && i < at.length; i++) {
      if (at[i] > 0 && at[i] < total) left.push((at[i] / total * 100).toFixed(2) + '%');
    }
    return left;
  }
  function markNodes() {
    var out = '';
    for (var i = 0, n = marks().length; i < n; i++) out += '<i id="m' + i + '"></i>';
    return out;
  }
  /* What would be a style in the markup, which browsers from before style-src-attr refuse. */
  function dress() {
    var picture = document.getElementById('pic');
    if (picture) {
      var url = 'url("' + encodeURI(play.art_url) + '")';
      if (dressed !== url) picture.style.backgroundImage = dressed = url;
    }
    if (shown !== 'play' && shown !== 'pause') return;
    var left = marks();
    for (var i = 0; i < left.length; i++) {
      var node = document.getElementById('m' + i);
      if (node) node.style.left = left[i];
    }
  }
  function overlay(paused) {
    return '<div class="ov" id="ov"><div class="top" id="tp">' + titles() + '</div><div class="bot" id="bt">' +
      (paused ? '<div class="pz" id="pz">' + (stopping ? glyph('stop') + stopping : blocked ? glyph('play') + 'Press OK to play' : glyph('pause') + 'Paused' + (play && play.paused_by && me && me.people && me.people.length > 1 ? ' by ' + face(play.paused_by, true) + esc(play.paused_by) : '')) + '</div>' : '') +
      '<div class="bar"><div class="p" id="pr"></div>' + markNodes() + '</div><div class="tms"><span id="el"></span><span id="re"></span></div>' +
      offer([[glyph('back') + '10 s', 'b'], [glyph(paused ? 'play' : 'pause') + (paused ? 'Play' : 'Pause'), 'p'], [glyph('fwd') + '10 s', 'f'], [glyph('stop') + 'Stop', 's']]) +
      '</div></div>';
  }

  // A code on a TV already paired adds a person to those it is paired with.
  function codeHead() { return token && me ? 'Pair another person' : 'Watch on this TV'; }

  var PAGES = {
    boot: function () { return page(BOOT) + offer([]); },
    code: function () {
      if (!pending) return PAGES.boot();
      var address = base.replace(/^https?:\/\//, '') + '/pair';
      var picture = qrPicture(base + '/pair#' + pending.code);
      return page('<div class="code"><div class="lc">' + BRAND + '<div class="h">' + codeHead() + '</div>' +
        '<div class="n"><u>1</u><div>On your phone or computer, go to<div class="addr' + (address.length > 30 ? ' addr--long' : '') + '">' + esc(address).replace(/([.\/])/g, '$1<wbr>') + '</div></div></div>' +
        '<div class="n"><u>2</u><div>Type this code<div class="big">' + esc(pending.code.substr(0, 3) + ' ' + pending.code.substr(3)) + '</div></div></div>' +
        '<div class="sm" id="cd"></div>' + offer([['New code', 'n']].concat(leave())) + '</div>' +
        '<div class="rc">' + (picture && picture + '<div class="cap">Or point your phone&rsquo;s camera here.<br>Check the code matches.</div>') + '</div></div>');
    },
    press: function () { return page(middle(codeHead(), 'The last code ran out.<br>Press OK for another.', [['New code', 'n']].concat(leave()))); },
    slow: function () { return page(middle('Too many codes', 'A new one comes by itself in ' + waitLeft() + '.', leave(), SPIN)); },
    conn: function () {
      var all = everyone().map(esc);
      // Past two, the names under the faces say who.
      var head = all.length > 2 ? 'You can all play here' : all.length > 1 ? all[0] + ' and ' + all[1] + ' can play here' : 'Connected to ' + all[0];
      return page('<div class="hello">' + middle(head, (all.length > 1 ? 'Each of you can start, pause and stop what plays on ' + esc(me.name) + '.' : 'This TV is now called ' + esc(me.name) + '.') + '<br>Press OK on the TV&rsquo;s remote once, so films can start with sound.',
        [['OK', 'i']], faces()) + '</div>');
    },
    idle: function () {
      return page('<div class="idle">' + middle(esc(me.name) + ' is ready', 'On your phone or computer, open a film,<br>press the TV button and choose ' + esc(me.name) + '.',
        [['Pair another person', 'n'], ['Unpair this TV', 'u']], faces()) + '</div>');
    },
    unpair: function () {
      var all = everyone().map(esc);
      var who = all.length > 1 ? all.slice(0, -1).join(', ') + ' and ' + all[all.length - 1] + ' won&rsquo;t be able to play here<br>until it is paired again.'
        : all[0] + ' won&rsquo;t be able to play here<br>until they pair it again.';
      return page(middle('Unpair this TV?', who, [['Keep it', 'i'], ['Unpair', 'o']]));
    },
    out: function () {
      return page(middle('Signed out', 'This TV was unpaired. Nothing of yours is left on it.<br>Pair it again to watch.', [['Pair this TV', 'n']]));
    },
    taken: function () { return page(middle('Someone else has this TV now', 'The film stopped. Its place is kept.', [], SPIN)); },
    resuming: function () {
      return page(resumingFrom ? middle('mediaman restarted', 'Picking up ' + esc(endedWith.title) + ' where you were.', [], SPIN)
        : middle('Picking up ' + esc(endedWith.title) + ' where you were.', '', [], SPIN));
    },
    restarted: function () {
      var over = /^(ended|upnext|still)$/.test(endedWith.state);
      return page(middle('mediaman restarted', over ? 'The TV is ready for something else.' : 'Your place in ' + esc(endedWith.title) + ' is kept.', [['OK', 'i']]));
    },
    lost: function () {
      return page(middle(offline() ? 'Waiting for the network' : 'mediaman isn&rsquo;t answering', 'Trying again by itself. If it stays like this,<br>check the TV is on the internet.', [], SPIN));
    },
    old: function () {
      return page(middle('This TV&rsquo;s browser can&rsquo;t play films', 'It is missing what mediaman needs.<br>Ask an admin to add this TV by its address.', [['Try anyway', 'a']]));
    },
    load: function () {
      return art(true) + '<div class="mid start">' + SPIN + titles() + (starter() ? '<div class="sm who">' + face(starter(), true) + esc(starter()) + ' is starting it</div>' : '') + '</div>' + offer([]);
    },
    soon: function () { return art(true) + '<div class="mid start">' + SPIN + '<div class="l">Starting</div></div>' + offer([]); },
    play: function () { return overlay(false); },
    pause: function () { return overlay(true); },
    /* Over the film's own picture, which stays: the words are in the pill at first, and this
       darkens the picture and takes its middle only once the wait has gone on. */
    buf: function () {
      var words = waitWords();
      // Once dark, the pill is away and this says the wait itself.
      return '<div class="wl' + (waitLong ? ' on' : '') + '" id="wl"' + (waitLong ? '' : ' aria-hidden="true"') + '><div class="mid">' + SPIN + '<div class="l">' + words[0] + '</div>' +
        (words[1] && '<div class="t2">' + words[1] + '</div>') + offer(mayLetGo() ? [['Stop', 'x']] : []) + '</div></div>';
    },
    next: function () {
      var after = play.next || {};
      // Cancel comes first and has the focus: whoever wants the next episode need only wait.
      return art() + '<div class="up"><div class="t2" id="in">' + nextLine(endsAt, play.state) + '</div><div class="t1">' + (episode(after, after.title) || esc(play.title)) + '</div>' +
        (askNote && '<div class="t2">' + askNote + '</div>') + offer([['Cancel', 'c'], ['Play now', 'k']]) + '</div>';
    },
    /* Over the last credits, which play on under it: no picture of its own. */
    credits: function () {
      var after = play.next || {};
      return '<div class="up"><div class="t2" id="in">' + nextLine(upCard.until, 'credits') + '</div><div class="t1">' + episode(after, after.title) + '</div>' +
        (askNote && '<div class="t2">' + askNote + '</div>') + offer([['Watch credits', 'w'], ['Play now', 'k']]) + '</div>';
    },
    still: function () {
      var minutes = Math.max(1, Math.ceil((play.still_in_s || 0) / 60));
      return art(true) + middle('Are you still watching?', 'Press OK here, or answer on your phone.<br>It stops in ' + minutes + (minutes === 1 ? ' minute' : ' minutes') + ' and keeps your place.' + (askNote && '<br>' + askNote),
        [['Continue watching', 'k'], ['Stop', 's']]).replace(BRAND, '');
    },
    ended: function () {
      return art(true) + '<div class="mid">' + titles() + '<div class="sm">' + (askNote || 'That&rsquo;s the end.') + '</div>' + offer([['Play again', 'r'], ['Close', 's']]) + '</div>';
    },
    err: function () {
      var acts = FOR_GOOD[play.error] ? [['Back', 's']] : [['Try again', 'r'], ['Back', 's']];
      return page(middle('This can&rsquo;t play here', askNote || WHY[play.error] || WHY.tv, acts));
    },
  };
  var FOCUS = { play: 1, pause: 1 };

  function faltering() { return now() - falteredAt >= NET_GRACE_MS; }

  function which() {
    if (play && token && mode !== 'taken') {
      var state = play.state;
      // A film on screen as mediaman went away waits over its own picture, as does a wait under way.
      if (away && shown !== 'buf' && !pickingUp && !(loaded && ready && !broken && overAt === null)) return 'lost';
      if (asked === play.play_id) return 'soon';
      if (state === 'failed') return 'err';
      if (state === 'upnext') return 'next';
      if (state === 'still' || state === 'ended') return state;
      if (broken || overAt !== null) return 'soon';
      // A film taken up again, after a wait or a restart, goes on waiting rather than starting over.
      if (!loaded || !ready) return (reloadDue && faltering()) || pickingUp || shown === 'buf' ? 'buf' : 'load';
      if (!wanted) return 'pause';
      // A stream given up on and loaded again goes on with the wait already shown.
      if (stalled && (faltering() || shown === 'buf')) return 'buf';
      return upCard && upCard.play === play.play_id ? 'credits' : 'play';
    }
    if (mode === 'resuming' && pickingUp) return 'buf';
    if (away && mode !== 'old') return 'lost';
    if ((mode === 'idle' || mode === 'conn' || mode === 'unpair') && !me) return 'boot';
    return mode;
  }

  var leftTimer = null;
  var shown = '', drawn = '', dressed = '', focus = 0, hideTimer = null, hidden = false;

  function show() {
    var name = which(), from = shown;
    var html = PAGES[name]();
    if (html !== drawn) {
      // With no countdown, nothing comes by itself: Play now is the press to make.
      var kept = name === shown ? focus : (name === 'next' && !endsAt) || (name === 'credits' && !upCard.until) ? 1 : FOCUS[name] || 0;
      // The overlay of a film that pauses or plays on is the same place.
      if (FOCUS[name] && FOCUS[shown]) kept = focus;
      var before = document.getElementById('ov');
      var tucked = Boolean(before) && before.className === 'ov hide';
      var pausedLine = name === 'play' && shown === 'pause' && document.getElementById('pz');
      swap(name !== shown && !(FOCUS[name] && FOCUS[shown]));
      stage.innerHTML = html;
      // Controls that were away come back in as a film is paused, rather than appear at once.
      var after = tucked && document.getElementById('ov');
      if (after) { after.className = 'ov hide'; void after.offsetWidth; }
      if (pausedLine) fadeOut(pausedLine);
      room();
      watchFaces();
      drawn = html;
      dressed = '';
      focus = Math.min(kept, Math.max(0, buttons.length - 1));
      paint();
      if (name === 'next' || name === 'credits') drain();
    }
    waiting(name);
    if (name !== shown) {
      shown = name;
      rouse();
      // A film that comes back from a wait plays on as it was, its controls away.
      if (name === 'play' && from === 'buf') { clearTimeout(hideTimer); hidden = true; }
      else if (name === 'play' || name === 'pause') wake();
    }
    if (pending) put('cd', codeLine());
    if (play) {
      var until = name === 'credits' ? upCard.until : endsAt;
      if (until) put('inn', String(nextLeft(until)));
      bar();
      dress();
    }
    hush();
  }

  /* A wait grows only as it lasts. A falter of a moment shows nothing; then a pill in the corner
     says so over the film's own picture; only a long wait darkens the picture. As the film comes
     back, the pill fades away without a word. */
  var waitFrom = 0, waitLong = false, waitTimer = null, pillSaid = '';
  function waitWords() {
    if (pickingUp && !away) return resumingFrom ? ['mediaman restarted', 'Picking up ' + esc(endedWith.title) + ' where you were.'] : ['Picking up ' + esc(endedWith.title) + ' where you were.', ''];
    if (!away) return ['Waiting for the network', ''];
    return ['Waiting for the network', offline() ? 'Trying again by itself.' : 'mediaman isn&rsquo;t answering yet. Trying again by itself.'];
  }
  /* Only where mediaman's address answered, though not as mediaman, is mediaman to blame. */
  function offline() { return !reached || Boolean(window.navigator && window.navigator.onLine === false); }
  /* Through a long wait a film may be stopped, its place kept. */
  function mayLetGo() { return waitLong && Boolean(play); }
  function waiting(name) {
    var pill = document.getElementById('nw');
    var frame = document.getElementById('fr');
    clearTimeout(waitTimer);
    if (name === 'buf') {
      if (shown !== 'buf') {
        waitFrom = now();
        if (!frame || frame.className !== 'on') still();
      }
      var left = waitFrom + WAIT_LONG_MS - now();
      if (left > 0) waitTimer = setTimeout(show, left);
      else if (!waitLong) {
        // Drawn again with its way out, the layer drawn clear until now darkens as a fade.
        waitLong = true;
        stage.innerHTML = drawn = PAGES.buf();
        var layer = document.getElementById('wl');
        if (layer) { layer.className = 'wl'; void layer.offsetWidth; layer.className = 'wl on'; }
        focus = 0;
        paint();
      }
      tellPill(pill, waitWords(), waitLong ? 'nw' : 'nw on');
      return;
    }
    if (shown !== 'buf') return;
    waitLong = false;
    if (frame) frame.className = '';
    pickingUp = false;
    tellPill(pill, null, 'nw');
  }
  function tellPill(pill, words, look) {
    if (!pill) return;
    var said = words ? '<b class="ec"><i></i></b><span class="nwt"><span>' + words[0] + '</span>' + (words[1] && '<span class="nws">' + words[1] + '</span>') + '</span>' : pillSaid;
    // Drawn again only as its words change, so that a reader says them once.
    if (said !== pillSaid) pill.innerHTML = pillSaid = said;
    if (pill.className !== look) pill.className = look;
  }
  /* The film's last picture, kept over where it was as the film is let go to be taken up again. */
  function still() {
    var frame = document.getElementById('fr');
    var w = video.videoWidth, h = video.videoHeight;
    if (!frame || !frame.getContext || !w || !h) return;
    var k = Math.min(window.innerWidth / w, window.innerHeight / h);
    frame.width = window.innerWidth;
    frame.height = window.innerHeight;
    try {
      frame.getContext('2d').drawImage(video, (frame.width - w * k) / 2, (frame.height - h * k) / 2, w * k, h * k);
      frame.className = 'on';
    } catch (e) { /* The video's own picture is left. */ }
  }

  /* The "Paused" line of a film that plays on stays a moment where it was, and fades. The controls
     under it keep their place: the bar they sit in is held to the bottom. */
  function fadeOut(line) {
    var bot = document.getElementById('bt');
    if (!bot) return;
    line.removeAttribute('id');
    line.className = 'pz gone';
    bot.insertBefore(line, bot.firstChild);
    setTimeout(function () { if (line.parentNode) line.parentNode.removeChild(line); }, LEAVE_MS);
  }

  /* The countdown's bar runs down over what is left of it as it is drawn. */
  function drain() {
    var node = document.getElementById('ndr');
    if (node) node.style.webkitAnimationDuration = node.style.animationDuration = upLine.left + 's';
  }

  /* The page before fades out under the new one, which fades in. */
  function swap(moves) {
    // A page drawn again as it fades in goes on fading in.
    if (!moves || !shown) { if (!leftTimer) stage.className = ''; return; }
    clearTimeout(leftTimer);
    leaving.innerHTML = '';
    stage.className = 'in';
    // The nodes themselves, with what was set on them; their ids are the new page's.
    while (stage.firstChild) leaving.appendChild(stage.firstChild);
    var nodes = leaving.getElementsByTagName('*');
    for (var i = 0; i < nodes.length; i++) nodes[i].removeAttribute('id');
    leftTimer = setTimeout(function () { leftTimer = null; leaving.innerHTML = ''; }, LEAVE_MS);
  }

  function paint() {
    for (var i = 0; i < buttons.length; i++) {
      var node = document.getElementById('b' + i);
      if (!node) continue;
      node.className = i === focus ? 'b f' : 'b';
      // For what reads the page aloud.
      if (i === focus) node.focus();
    }
  }

  /* The overlay shows, and goes after a while unless the film is paused. */
  function wake() {
    hidden = false;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { hidden = true; hush(); }, OVERLAY_MS);
    hush();
  }
  function hush() {
    var node = document.getElementById('ov');
    if (node) node.className = hidden && shown === 'play' ? 'ov hide' : 'ov';
  }

  // What plays, waits for a film or counts down; the rest are still pictures.
  var BRIGHT = { play: 1, buf: 1, load: 1, soon: 1, next: 1, credits: 1, still: 1, resuming: 1 };
  // Where a press meant for the light would start a film, ask for a code, unpair or leave the page.
  var LIGHT_FIRST = { idle: 1, code: 1, unpair: 1, ended: 1, err: 1, out: 1, press: 1, old: 1, conn: 1 };
  var stirredAt = now();
  var litUntil = 0;          // a click until then belongs to the pointer's move that woke the page
  var litByDown = false;

  /* A page nobody does anything at dims, and its words move a little: a bright still picture marks some screens. */
  function rest() {
    var over = now() - stirredAt - (shown === 'pause' ? PAUSED_DIM_MS : IDLE_DIM_MS);
    var look = over < 0 || BRIGHT[shown] ? '' : 'dim d' + Math.floor(over / MOVE_MS) % 4;
    if (body.className !== look) body.className = look;
  }
  /* Whether the page was dimmed. */
  function rouse() {
    var dimmed = body.className !== '';
    stirredAt = now();
    rest();
    return dimmed;
  }

  /* ── full screen ── */

  // The first press of any kind asks, unseen, for the whole page full screen: a poll is not a press,
  // and a TV that hides its browser's bars itself loses nothing. Older browsers name it otherwise.
  var root = document.documentElement;
  var askFull = root && (root.requestFullscreen || root.webkitRequestFullscreen || root.webkitRequestFullScreen ||
    root.mozRequestFullScreen || root.msRequestFullscreen);
  var canFull = Boolean(askFull) && document.fullscreenEnabled !== false && document.webkitFullscreenEnabled !== false;
  var fullAsked = false;     // since the page last left full screen
  var RETRY_MS = 60000;
  var retryAt = Infinity;    // when a refused request may be made again

  function isFull() {
    return Boolean(document.fullscreenElement || document.webkitFullscreenElement || document.webkitCurrentFullScreenElement ||
      document.webkitIsFullScreen || document.mozFullScreenElement || document.msFullscreenElement);
  }
  function goFull() {
    if (!canFull || isFull() || (fullAsked && now() < retryAt)) return;
    fullAsked = true;
    retryAt = Infinity;
    try {
      var asking = askFull.call(root);
      if (asking && asking.then) asking.then(null, refusedFull);
    } catch (e) { refusedFull(); }
  }
  function refusedFull() { retryAt = now() + RETRY_MS; }
  function fullChanged() { if (!isFull()) fullAsked = false; }
  ['fullscreenchange', 'webkitfullscreenchange', 'mozfullscreenchange', 'MSFullscreenChange'].forEach(function (name) {
    document.addEventListener(name, fullChanged);
  });
  ['fullscreenerror', 'webkitfullscreenerror', 'mozfullscreenerror', 'MSFullscreenError'].forEach(function (name) {
    document.addEventListener(name, refusedFull);
  });

  /* ── keys ── */

  function act(what) {
    if (what === 'n') { codes = 0; mode = 'code'; newCode(false); }
    else if (what === 'a') { begin(); }
    else if (what === 'i') { mode = token ? 'idle' : mode; }
    else if (what === 'u') mode = 'unpair';
    else if (what === 'o') {
      var old = token;
      signedOut();
      loop += 1;
      pairings += 1;
      pending = null;
      send('unpair', old, {}, 15000, function (status) {
        // Where that was not heard, it is told again: this TV would stay paired on the server.
        if (status !== 200) setTimeout(function () { send('unpair', old, {}, 15000, function () {}); }, 5000);
      });
    }
    else if (what === 'k') { if (upCard) upCard.until = 0; ask('next'); }
    else if (what === 'w') watchCredits();
    else if (what === 'c') ask('cancel');
    else if (what === 'r') { tries = 0; ask('again'); }
    else if (what === 's') ask('stop');
    else if (what === 'x') { if (mayLetGo()) letItGo(); }
    else if (what === 'p') { if (wanted) hold(); else go(); tell(); }
    else if (what === 'b' || what === 'f') seekAsked(position() + (what === 'b' ? -10 : 10));
    show();
  }

  /* Whether the press was for the light alone. */
  function pressed() {
    active = true;
    if (codes > 1) codes = 1;
    return lights();
  }
  function lights() { return rouse() && Boolean(LIGHT_FIRST[shown]); }
  /* A pointer tells of its move before its press. */
  function pointed() {
    if (lights()) litUntil = now() + LIT_MS;
  }

  function back() {
    if (shown === 'play' && keepMark()) return true;
    if (shown === 'play' && !hidden) { hidden = true; hush(); return true; }
    if (shown === 'play') { wake(); return true; }
    if (shown === 'unpair' || (token && (shown === 'code' || shown === 'press' || shown === 'slow'))) { act('i'); return true; }
    if (shown === 'next') { act('c'); return true; }
    if (shown === 'credits') { act('w'); return true; }
    if (shown === 'err' || shown === 'ended') { act('s'); return true; }
    if (shown === 'buf' && mayLetGo()) { act('x'); return true; }
    return shown === 'pause' || shown === 'buf' || shown === 'load' || shown === 'soon' || shown === 'still';
  }

  var MEDIA = { 415: 'play', 19: 'pause', 10252: 'both', 179: 'both', 413: 'stop', 178: 'stop', 412: 'b', 417: 'f' };

  function media(key) {
    if (!play || !loaded || !ready || (shown !== 'play' && shown !== 'pause' && shown !== 'buf' && shown !== 'credits')) return;
    if (key === 'stop') act('s');
    else if (key === 'b' || key === 'f') act(key);
    else if (key === 'both' || (key === 'play') !== wanted) act('p');
    wake();
  }

  document.onkeydown = function (e) {
    var key = e.keyCode;
    var isBack = key === 10009 || key === 461 || key === 27 || key === 8;
    pressedKey(e, key, isBack);
    // After the press has done its work: a browser that wants a key before it plays plays first.
    if (!isBack && !e.repeat) goFull();
  };
  function pressedKey(e, key, isBack) {
    var used = true, light = pressed();
    if (light && (key === 13 || key === 32)) { e.preventDefault(); return; }
    if (shown === 'restarted') act('i');
    else if (MEDIA[key]) media(MEDIA[key]);
    else if (isBack) used = back();
    // With the controls up, OK is for the button it is on.
    else if (key === 13 && shown === 'play' && hidden && keepMark()) used = true;
    else if (shown === 'play' && hidden && (key === 9 || (key >= 13 && key <= 40))) wake();
    else if (key === 37 || key === 39) {
      if (buttons.length) focus = Math.max(0, Math.min(buttons.length - 1, focus + (key === 39 ? 1 : -1)));
      paint();
      if (shown === 'play') wake();
    } else if (key === 13 || key === 32) {
      if (shown === 'play') wake();
      if (buttons[focus] && !e.repeat) act(buttons[focus][1]);
    } else used = key === 38 || key === 40;
    if (used) e.preventDefault();
  }

  // Some browsers click at this, whatever became of the press.
  document.onkeyup = function (e) {
    if (e.keyCode === 32) e.preventDefault();
  };

  function buttonAt(node) {
    while (node && node.nodeType !== 9) {
      var id = /^b(\d)$/.exec(node.id || '');
      if (id && buttons[Number(id[1])]) return Number(id[1]);
      node = node.parentNode;
    }
    return -1;
  }
  function moveTo(n) {
    if (n < 0 || n === focus) return;
    focus = n;
    paint();
  }

  // A remote that points, or a mouse.
  document.onmousedown = function () {
    litByDown = lights() || now() < litUntil;
  };
  // A remote that points sends a click at OK, and no key.
  document.onclick = function (e) {
    clicked(e);
    goFull();
  };
  function clicked(e) {
    var light = pressed() || litByDown || now() < litUntil;
    litByDown = false;
    litUntil = 0;
    var n = buttonAt(e.target);
    var bare = n < 0 && !onControl(e.target);
    if (shown === 'play' && hidden) { wake(); if (bare) tapped(); return; }
    if (bare && tapped()) return;
    if (light) return;
    if (n < 0) return;
    moveTo(n);
    act(buttons[n][1]);
  }
  /* Whether a click landed on something that answers it by itself. */
  function onControl(node) {
    while (node && node.nodeType !== 9) {
      if (/^(A|BUTTON|INPUT|SELECT|TEXTAREA)$/.test(node.tagName || '') || node.id === 'sk') return true;
      node = node.parentNode;
    }
    return false;
  }
  /* A click on the bare picture of a film that plays or is paused does what the play button does. */
  function tapped() {
    if (!play || !loaded || !ready || (shown !== 'play' && shown !== 'pause')) return false;
    act('p');
    wake();
    return true;
  }
  var pointedAt = '';
  document.onmousemove = function (e) {
    // A page that changes under a pointer that lies still tells of a move too.
    var at = e.clientX + ' ' + e.clientY;
    if (at === pointedAt) return;
    pointedAt = at;
    pointed();
    if (shown === 'play') wake();
    moveTo(buttonAt(e.target));
  };
  document.onwheel = document.onmousewheel = pointed;
  // Tab.
  document.addEventListener('focus', function (e) {
    var n = buttonAt(e.target);
    if (n < 0 || n === focus) return;
    moveTo(n);
    if (shown === 'play') wake();
  }, true);

  function fit() {
    var scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    var move = 'translate(' + (window.innerWidth - 1920 * scale) / 2 + 'px,' + (window.innerHeight - 1080 * scale) / 2 + 'px) scale(' + scale + ')';
    stage.style.webkitTransform = move;
    stage.style.transform = move;
    leaving.style.webkitTransform = move;
    leaving.style.transform = move;
    if (notes) {
      notes.style.webkitTransform = move;
      notes.style.transform = move;
    }
  }
  window.onresize = fit;
  fit();

  function begin() {
    token = remembered(KEPT);
    mode = 'boot';
    show();
    if (token) poll();
    else newCode(false);
  }

  window.MMTv = { qr: qr };
  if (native() || sources()) begin();
  else { mode = 'old'; show(); }
})();
