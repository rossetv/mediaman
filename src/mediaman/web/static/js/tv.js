/**
 * The TV's own page. Pairs this TV with a code, plays what its person sends it and answers the
 * remote's keys. Talks to /api/tv/device with the TV's token alone; no session, no account.
 *
 * ES5 and XMLHttpRequest only: the browsers of TVs from 2016 run it, so nothing of the app's
 * other scripts is used and nothing is loaded from elsewhere.
 */
(function () {
  'use strict';

  var API = '/api/tv/device/';
  var KEPT = 'mm-tv';
  var KEPT_PLACE = 'mm-tv-place';
  var MAX_CODES = 12;        // shown by themselves, with no key pressed
  var RESTARTED_MS = 8000;
  var LOST_AFTER = 8000;
  var OVERLAY_MS = 4000;
  var STALL_MS = 1500;
  var PUT_AGAIN_MS = 1500;
  var GIVE_UP_MS = 4000;
  var BROKEN_MS = 45000;
  var PAUSED_DIM_MS = 300000;
  var IDLE_DIM_MS = 600000;
  var MOVE_MS = 60000;
  var LIT_MS = 500;

  var body = document.body;
  var stage = document.getElementById('s');
  var leaving = document.getElementById('o');
  var hintNode = document.getElementById('f');
  var video = document.getElementById('v');
  var base = body.getAttribute('data-base') || (location.protocol + '//' + location.host);

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
      // A proxy's own page is not an answer.
      if (!answer || typeof answer !== 'object') { answer = {}; status = 0; }
      done(status, answer, status === 429 ? Number(xhr.getResponseHeader('Retry-After')) || 10 : 0);
    }
    // Some TVs have no xhr.timeout.
    var late = setTimeout(function () {
      if (over) return;
      over = true;
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
  var me = null;             // {name, owner}
  var play = null;           // as the last poll gave it
  var cutOff = null;         // whose film ended as this TV's token was refused
  var mode = 'boot';         // what the page shows while nothing plays
  var codes = 0;             // since the last key
  var waitUntil = 0;         // no new code before
  var heardAt = now();
  var away = false;

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
    var from = cutOff || (play && me.owner);
    var other = Boolean(from) && from !== got.owner;
    cutOff = null;
    pending = null;
    pairings += 1;
    token = mine.token;
    remember(KEPT, token);
    remember(KEPT_PLACE, null);
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
      heard();
      // First, so that nothing a TV's browser throws below ends the polling.
      pollSoon(again ? 500 : got.poll_ms || 3000);
      again = false;
      take(got);
    });
  }

  function refused() {
    token = null;
    loop += 1;
    cutOff = play && me.owner;
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

  function take(got) {
    me = { name: got.name, owner: got.owner };
    if (mode === 'boot') mode = 'idle';
    var was = play;
    playing(got.play);
    if (was && !got.play && boot !== got.boot) restarted(was);
    boot = got.boot;
    obey(got.commands || []);
    show();
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
    if (mode === 'conn' || mode === 'unpair' || mode === 'taken' || mode === 'code' || mode === 'restarted') mode = 'idle';
    if (!was || was.play_id !== next.play_id) {
      unload();
      rouse();
      asked = null;
      askNote = stopping = '';
      wanted = !next.paused;
      counted = next.played_s || 0;
      breaks = 0;
    }
    endsAt = next.upnext_in_s === null || next.upnext_in_s === undefined ? 0 : now() + next.upnext_in_s * 1000;
    if (next.url && loaded !== next.play_id) load(lastPlace(next));
    else if (!next.url && loaded) unload();
    else if (reloadDue) { reloadDue = false; load(position()); }
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
    askNote = '';
    if (stop) {
      stopping = 'Stopping';
      if (loaded) hold();
    }
    var wish = { play_id: id, what: what };
    if (what === 'again') wish.paused = Boolean(byItself) && !wanted;
    send('ask', token, wish, stop ? 10000 : 110000, function (status, got) {
      var same = play && play.play_id === id;
      if (asked === id) asked = null;
      // Until a poll says what came of it.
      if (what === 'auto') autoAt = now() + 5000;
      if (same && status !== 200 && got.error !== 'stale' && what !== 'auto') {
        askNote = WHY[got.error] || (status ? WHY.unreachable : WHY.silent);
        if (stopping) stopping = 'mediaman isn’t answering. Press Stop again.';
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
    else if (c.kind === 'seek') seek(c.position_s);
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
  var canVolume = null;

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
    return {
      play_id: play.play_id, state: stateNow(), position_s: position() || 0, played_s: counted,
      volume: video.volume, muted: Boolean(video.muted), can_volume: canVolume,
    };
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
    if (!loaded || !ready || video.ended || reloadDue || broken) return;
    wanted = false;
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
      if (there && ready && at - keptAt > 5000) { keptAt = at; remember(KEPT_PLACE, play.play_id + ' ' + Math.floor(time)); }
    }
    if (pending && mode === 'code') {
      if (now() >= pending.until) codeGone();
      else put('cd', codeLine());
    }
    if (play && play.state === 'upnext' && endsAt && asked !== play.play_id) {
      if (at >= endsAt && at >= autoAt) ask('auto');
      else put('in', nextLine());
    }
    if (play) bar();
    rest();
  }
  setInterval(tick, 500);

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
    not_found: 'Plex doesn’t have this one any more.<br>Choose something else.',
    too_many_streams: 'You’re watching on three screens, the most at once.<br>Stop one, then try again.',
    track_unavailable: 'The subtitles chosen aren’t there any more.<br>Choose others on your phone or computer.',
    rate_limited: 'Too many were started in a short time.<br>Wait a minute, then try again.',
    not_set_up: 'mediaman isn’t connected to Plex.<br>Ask an admin to check Settings.',
    superseded: 'Something else was started meanwhile.<br>Try again.',
  };
  var ICONS = {
    play: 'M7 4l13 8-13 8z', pause: 'M6 4h4v16H6zM14 4h4v16h-4z', back: 'M11 5v14l-9-7zM22 5v14l-9-7z',
    fwd: 'M2 5v14l9-7zM13 5v14l9-7z', stop: 'M5 5h14v14H5z', ok: 'M9 17l-5-5-2 2 7 7L22 8l-2-2z',
  };
  function glyph(name) { return '<svg class="i" viewBox="0 0 24 24"><path d="' + ICONS[name] + '"/></svg>'; }

  // The page holds the name and its first wait as drawn, until the first of these pages takes its place.
  var BRAND = document.querySelector('.brand').outerHTML;
  var BOOT = document.querySelector('.mid').outerHTML;
  var SPIN = '<div class="spin"></div>';
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
  // A TV that is paired need not pair anew.
  function leave() { return token ? [['Back', 'i']] : []; }
  function page(inner) { return '<div class="page">' + inner + '</div>'; }
  function art() {
    return '<div class="pic" id="pic"></div>';
  }
  function titles() {
    return '<div class="t1">' + esc(play.title) + '</div><div class="t2">' + under(play) + '</div>';
  }
  function codeLine() {
    var left = clock((pending.until - now()) / 1000);
    return pending.ranOut ? 'The last code ran out. This one works for ' + left + '.' : 'This code works for ' + left + '.';
  }
  function nextLine() {
    return endsAt ? 'Next episode in ' + Math.max(0, Math.ceil((endsAt - now()) / 1000)) + ' s' : 'Next episode';
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
    return '<div class="ov" id="ov"><div class="top">' + titles() + '</div><div class="bot">' +
      (paused ? '<div class="pz">' + (stopping ? glyph('stop') + stopping : blocked ? glyph('play') + 'Press OK to play' + (fullDue() ? ' in full screen' : '') : glyph('pause') + 'Paused') + '</div>' : '') +
      '<div class="bar"><div class="p" id="pr"></div>' + markNodes() + '</div><div class="tms"><span id="el"></span><span id="re"></span></div>' +
      offer([[glyph('back') + '10 s', 'b'], [glyph(paused ? 'play' : 'pause') + (paused ? 'Play' : 'Pause'), 'p'], [glyph('fwd') + '10 s', 'f'], [glyph('stop') + 'Stop', 's']]) +
      '</div></div>';
  }

  var PAGES = {
    boot: function () { return page(BOOT) + offer([]); },
    code: function () {
      if (!pending) return PAGES.boot();
      var address = base.replace(/^https?:\/\//, '') + '/pair';
      var picture = qrPicture(base + '/pair#' + pending.code);
      return page('<div class="code"><div class="lc">' + BRAND + '<div class="h">Watch on this TV</div>' +
        '<div class="n"><u>1</u><div>On your phone or computer, go to<div class="addr' + (address.length > 30 ? ' addr--long' : '') + '">' + esc(address) + '</div></div></div>' +
        '<div class="n"><u>2</u><div>Type this code<div class="big">' + esc(pending.code.substr(0, 3) + ' ' + pending.code.substr(3)) + '</div></div></div>' +
        '<div class="sm" id="cd"></div>' + offer([['New code', 'n']].concat(leave())) + '</div>' +
        '<div class="rc">' + (picture && picture + '<div class="cap">Or point your phone&rsquo;s camera here.<br>Check the code matches.</div>') + '</div></div>');
    },
    press: function () { return page(middle('Watch on this TV', 'The last code ran out.<br>Press OK for another.', [['New code', 'n']].concat(leave()))); },
    slow: function () { return page(middle('Too many codes', 'A new one comes by itself in ' + (waitUntil - now() > 90000 ? Math.ceil((waitUntil - now()) / 60000) + ' minutes' : 'a minute') + '.', leave(), SPIN)); },
    conn: function () {
      return page(middle('Connected to ' + esc(me.owner), 'This TV is now called ' + esc(me.name) + '.<br>Press OK on the TV&rsquo;s remote once, so films can start with sound.',
        [['OK', 'i']], '<div class="tick">' + glyph('ok') + '</div>'));
    },
    idle: function () {
      return page('<div class="idle">' + middle(esc(me.name) + ' is ready', 'On your phone or computer, open a film,<br>press the TV button and choose ' + esc(me.name) + '.',
        [['Pair another person', 'n'], ['Unpair this TV', 'u']]) + '</div>');
    },
    unpair: function () {
      return page(middle('Unpair this TV?', esc(me.owner) + ' won&rsquo;t be able to play here<br>until they pair it again.', [['Keep it', 'i'], ['Unpair', 'o']]));
    },
    out: function () {
      return page(middle('Signed out', 'This TV was unpaired. Nothing of yours is left on it.<br>Pair it again to watch.', [['Pair this TV', 'n']]));
    },
    taken: function () { return page(middle('Someone else has this TV now', 'The film stopped. Its place is kept.', [], SPIN)); },
    restarted: function () {
      var over = /^(ended|upnext|still)$/.test(endedWith.state);
      return page(middle('mediaman restarted', over ? 'The TV is ready for something else.' : 'Your place in ' + esc(endedWith.title) + ' is kept.', [['OK', 'i']]));
    },
    lost: function () {
      return page(middle('mediaman isn&rsquo;t answering', 'Trying again by itself. If it stays like this,<br>check the TV is on the internet.', [], SPIN));
    },
    old: function () {
      return page(middle('This TV&rsquo;s browser can&rsquo;t play films', 'It is missing what mediaman needs.<br>Ask an admin to add this TV by its address.', [['Try anyway', 'a']]));
    },
    load: function () {
      return art() + '<div class="shade"></div><div class="mid">' + SPIN + titles() + '<div class="sm">' + esc(me.owner) + ' is starting it</div></div>' + offer([]);
    },
    soon: function () { return art() + '<div class="shade"></div><div class="mid">' + SPIN + '<div class="l">Starting</div></div>' + offer([]); },
    play: function () { return overlay(false); },
    pause: function () { return overlay(true); },
    buf: function () {
      return '<div class="shade"></div><div class="mid">' + SPIN + '<div class="l">Waiting for the network</div></div>' + offer([]);
    },
    next: function () {
      var after = play.next || {};
      return art() + '<div class="up"><div class="t2" id="in"></div><div class="t1">' + (episode(after, after.title) || esc(play.title)) + '</div>' +
        (askNote && '<div class="t2">' + askNote + '</div>') + offer([['Play now', 'k'], ['Cancel', 'c']]) + '</div>';
    },
    still: function () {
      var minutes = Math.max(1, Math.ceil((play.still_in_s || 0) / 60));
      return art() + '<div class="shade"></div>' + middle('Are you still watching?', 'Press OK here, or answer on your phone.<br>It stops in ' + minutes + (minutes === 1 ? ' minute' : ' minutes') + ' and keeps your place.' + (askNote && '<br>' + askNote),
        [['Continue watching', 'k'], ['Stop', 's']]).replace(BRAND, '');
    },
    ended: function () {
      return art() + '<div class="shade"></div><div class="mid">' + titles() + '<div class="sm">' + (askNote || 'That&rsquo;s the end.') + '</div>' + offer([['Play again', 'r'], ['Close', 's']]) + '</div>';
    },
    err: function () {
      return page(middle('This can&rsquo;t play here', askNote || WHY[play.error] || WHY.tv, [['Try again', 'r'], ['Back', 's']]));
    },
  };
  var FOCUS = { play: 1, pause: 1 };

  function which() {
    if (play && token && mode !== 'taken') {
      var state = play.state;
      if (away && !(loaded && ready && !stalled)) return 'lost';
      if (asked === play.play_id) return 'soon';
      if (state === 'failed') return 'err';
      if (state === 'upnext') return 'next';
      if (state === 'still' || state === 'ended') return state;
      if (broken || overAt !== null) return 'soon';
      if (!loaded || !ready) return reloadDue ? 'buf' : 'load';
      if (!wanted) return 'pause';
      return stalled ? 'buf' : 'play';
    }
    if (away && mode !== 'old') return 'lost';
    if ((mode === 'idle' || mode === 'conn' || mode === 'unpair') && !me) return 'boot';
    return mode;
  }

  var leftTimer = null;
  var shown = '', drawn = '', dressed = '', focus = 0, hideTimer = null, hidden = false;

  function show() {
    var name = which();
    var html = PAGES[name]();
    if (html !== drawn) {
      var kept = name === shown ? focus : FOCUS[name] || 0;
      // The overlay of a film that pauses or plays on is the same place.
      if (FOCUS[name] && FOCUS[shown]) kept = focus;
      swap(name !== shown && !(FOCUS[name] && FOCUS[shown]));
      stage.innerHTML = html;
      drawn = html;
      dressed = '';
      focus = Math.min(kept, Math.max(0, buttons.length - 1));
      paint();
    }
    if (name !== shown) {
      if (FILM[name] && !FILM[shown]) hintFor();
      shown = name;
      rouse();
      if (name === 'play' || name === 'pause') wake();
    }
    if (pending) put('cd', codeLine());
    if (play) { put('in', nextLine()); bar(); dress(); }
    hush();
    hint();
  }

  /* The page before fades out under the new one, which fades in. */
  function swap(moves) {
    clearTimeout(leftTimer);
    leaving.innerHTML = '';
    stage.className = '';
    if (!moves || !shown) return;
    stage.className = 'in';
    // The nodes themselves, with what was set on them; their ids are the new page's.
    while (stage.firstChild) leaving.appendChild(stage.firstChild);
    var nodes = leaving.getElementsByTagName('*');
    for (var i = 0; i < nodes.length; i++) nodes[i].removeAttribute('id');
    leftTimer = setTimeout(function () { leaving.innerHTML = ''; }, 250);
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
  var BRIGHT = { play: 1, buf: 1, load: 1, soon: 1, next: 1, still: 1 };
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

  // The browser's own bars go only for the whole page, and only at a key's press: a poll is not one.
  var root = document.documentElement;
  var canFull = Boolean(root && (root.requestFullscreen || root.webkitRequestFullscreen) &&
    document.fullscreenEnabled !== false && document.webkitFullscreenEnabled !== false);
  var fullAsked = false;     // since the page last left full screen
  var FILM = { play: 1, pause: 1, buf: 1 };
  var hintTimer = null, hintGone = false;

  function isFull() {
    return Boolean(document.fullscreenElement || document.webkitFullscreenElement || document.webkitIsFullScreen);
  }
  function fullDue() { return canFull && !fullAsked && !isFull(); }
  /* Whether the press asked for full screen. */
  function goFull() {
    if (!fullDue()) return false;
    fullAsked = true;
    try {
      var asking = (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
      if (asking && asking.then) asking.then(null, refusedFull);
    } catch (e) { refusedFull(); return false; }
    return true;
  }
  function refusedFull() {
    canFull = false;
    show();
  }
  function fullChanged() {
    if (!isFull()) {
      fullAsked = false;
      if (FILM[shown]) hintFor();
    }
    show();
  }
  document.addEventListener('fullscreenchange', fullChanged);
  document.addEventListener('webkitfullscreenchange', fullChanged);
  document.addEventListener('fullscreenerror', refusedFull);
  document.addEventListener('webkitfullscreenerror', refusedFull);

  /* Over a film the hint goes with the controls; a page that waits keeps it. */
  function hintFor() {
    hintGone = false;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { hintGone = true; hint(); }, OVERLAY_MS);
  }
  function hint() {
    // A film the browser would not start says it in its own words.
    var on = fullDue() && !blocked && !(FILM[shown] && hintGone);
    if (hintNode) hintNode.className = on ? 'fs on' : 'fs';
  }

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
    else if (what === 'k') ask('next');
    else if (what === 'c') ask('cancel');
    else if (what === 'r') { tries = 0; ask('again'); }
    else if (what === 's') ask('stop');
    else if (what === 'p') { if (wanted) hold(); else go(); tell(); }
    else if (what === 'b' || what === 'f') seek(position() + (what === 'b' ? -10 : 10));
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
    if (shown === 'play' && !hidden) { hidden = true; hush(); return true; }
    if (shown === 'play') { wake(); return true; }
    if (shown === 'unpair' || (token && (shown === 'code' || shown === 'press' || shown === 'slow'))) { act('i'); return true; }
    if (shown === 'next') { act('c'); return true; }
    if (shown === 'err' || shown === 'ended') { act('s'); return true; }
    return shown === 'pause' || shown === 'buf' || shown === 'load' || shown === 'soon' || shown === 'still';
  }

  var MEDIA = { 415: 'play', 19: 'pause', 10252: 'both', 179: 'both', 413: 'stop', 178: 'stop', 412: 'b', 417: 'f' };

  function media(key) {
    if (!play || !loaded || !ready || (shown !== 'play' && shown !== 'pause' && shown !== 'buf')) return;
    if (key === 'stop') act('s');
    else if (key === 'b' || key === 'f') act(key);
    else if (key === 'both' || (key === 'play') !== wanted) act('p');
    wake();
  }

  document.onkeydown = function (e) {
    var key = e.keyCode, used = true, light = pressed();
    var isBack = key === 10009 || key === 461 || key === 27 || key === 8;
    var ok = key === 13 || key === 32;
    // The press that takes the page full screen does nothing else, but where it was asked to play too.
    var full = !isBack && !e.repeat && goFull() && !blocked;
    if ((light || full) && ok) { e.preventDefault(); return; }
    if (shown === 'restarted') act('i');
    else if (MEDIA[key]) media(MEDIA[key]);
    else if (isBack) used = back();
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
  };

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
  document.onclick = function (e) {
    var light = pressed() || litByDown || now() < litUntil;
    litByDown = false;
    litUntil = 0;
    if (shown === 'play' && hidden) { wake(); return; }
    if (light) return;
    var n = buttonAt(e.target);
    if (n < 0) return;
    moveTo(n);
    act(buttons[n][1]);
  };
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
    if (hintNode) { hintNode.style.webkitTransform = move; hintNode.style.transform = move; }
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
