/**
 * watch/languages.js — another language for a title, as views pushed inside the Audio & subtitles
 * chooser (watch/tracks.js): subtitles Plex finds and adds in seconds, or another copy of the
 * episode, its season or the film with the audio, which Sonarr or Radarr finds and downloads.
 *
 * MM.languages.create(ctx) is made by the chooser for a title; ctx gives its state and its way of
 * pushing, going back and going home. It gives the rows under each of the menu's columns, the
 * title page's first view, and is told when the chooser opens and closes, so that copies on their
 * way are followed only while it is seen.
 *
 *   GET  /api/watch/languages/<key>                the languages, the household's, copies asked for
 *   GET  /api/watch/languages/<key>/subtitles      ?language: what Plex finds
 *   POST /api/watch/languages/<key>/subtitles      {language, index}: added, its track's id
 *   GET  /api/watch/languages/<key>/audio          ?language&scope: the copies Sonarr or Radarr find
 *   POST /api/watch/languages/<key>/audio          {language, scope, index}: downloaded
 */
(function (global) {
  'use strict';

  var MM = global.MM = global.MM || {};
  var FOLLOW_MS = 5000;      // how often a copy on its way is asked after, while it is seen
  var PICKED_MS = 170;

  var reduce = global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function ms(n) { return reduce && reduce.matches ? Math.min(n, 150) : n; }
  function phone() { return !!(global.matchMedia && global.matchMedia('(max-width: 699.98px)').matches); }

  function fold(s) { return String(s).normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }
  function andList(xs) { return xs.length < 2 ? xs.join('') : xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1]; }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function span(cls, text) { return el('span', cls, text == null ? null : text); }
  function button(cls, text) { var b = el('button', cls, text); b.type = 'button'; return b; }
  function icon(name, cls) { return MM.dom.icon(name, cls); }

  function fadeEnds(list) {
    list.classList.toggle('has-more-above', list.scrollTop > 1);
    list.classList.toggle('has-more-below', list.scrollTop + list.clientHeight < list.scrollHeight - 1);
  }

  /* The middle of a view, which scrolls where the glass is too short for it. */
  function scroller(v) {
    var body = el('div', 'lp-scroll');
    body.addEventListener('scroll', function () { fadeEnds(body); });
    v.appendChild(body);
    return body;
  }

  /* Scrolls *within* so that *what* is in sight, and nothing around it. */
  function reveal(what, within) {
    if (!what.getBoundingClientRect || !within.getBoundingClientRect) return;
    var r = what.getBoundingClientRect();
    var s = within.getBoundingClientRect();
    if (r.bottom > s.bottom) within.scrollTop += Math.min(r.bottom - s.bottom, r.top - s.top);
    else if (r.top < s.top) within.scrollTop -= s.top - r.top;
  }

  function sizeText(bytes) {
    if (!bytes) return '';
    if (bytes >= 1e9) return (bytes / 1e9).toFixed(bytes >= 1e10 ? 0 : 1) + ' GB';
    return Math.max(1, Math.round(bytes / 1e6)) + ' MB';
  }
  function ageText(days) {
    if (days == null) return '';
    if (days < 1) return 'new today';
    if (days < 60) return days + (days === 1 ? ' day old' : ' days old');
    if (days < 730) return Math.round(days / 30) + ' months old';
    return Math.round(days / 365) + ' years old';
  }

  /* Up and down within a list of options. */
  function arrows(list) {
    list.addEventListener('keydown', function (e) {
      var all = Array.prototype.slice.call(list.querySelectorAll('[role="option"]')).filter(function (x) { return !x.disabled; });
      var at = all.indexOf(document.activeElement);
      if (at < 0) return;
      var to = null;
      if (e.key === 'ArrowDown') to = Math.min(at + 1, all.length - 1);
      else if (e.key === 'ArrowUp') to = Math.max(at - 1, 0);
      else if (e.key === 'Home') to = 0;
      else if (e.key === 'End') to = all.length - 1;
      if (to == null) return;
      e.preventDefault();
      all.forEach(function (x) { x.setAttribute('tabindex', '-1'); });
      all[to].setAttribute('tabindex', '0');
      all[to].focus();
    });
  }

  function create(ctx) {
    var key = ctx.key;
    var base = '/api/watch/languages/' + encodeURIComponent(key);
    var info = null;           // what GET <base> said
    var asking = null;
    var follow = null;
    var BY = {};

    function st() { return ctx.state() || {}; }
    function episode() { return !!st().series; }
    function thing() { return episode() ? 'episode' : 'film'; }
    function seasonName() { return info && info.season != null ? 'Season ' + info.season : 'the season'; }

    function own(code) {
      return (BY[code] && BY[code].native) || (MM.tracks.nameIn(code, code)) || code;
    }
    function en(code) { return (BY[code] && BY[code].name) || MM.tracks.langWords(code) || code; }
    function all() { return (info && info.languages) || []; }

    function ask() {
      if (asking) return asking;
      asking = MM.api.get(base).then(function (data) {
        asking = null;
        take(data);
        return info;
      }, function (err) {
        asking = null;
        throw err;
      });
      return asking;
    }

    /* Each answer is drawn into what is shown, in place: the focus and the scroll stay where they
       are while a copy comes. */
    function take(data) {
      info = data;
      BY = {};
      all().forEach(function (l) { BY[l.code] = l; });
      placeRows();
      tick();
      keepFollowing();
    }

    function copies() { return (info && info.copies) || []; }
    function copyOf(id) { return copies().filter(function (c) { return c.id === id; })[0] || null; }
    /* One that will not come as it is: looked for again from its row. */
    function again(c) { return c.status === 'lacks' || c.status === 'failed'; }
    function onTheWay() { return copies().filter(function (c) { return !again(c); }); }

    /* The menu's rows of copies, one a copy, in the order given: a new one put in before Get
       another language, one gone taken out, the focus it had passed to the row after it. */
    function placeRows() {
      var get = MM.dom.qa('[data-lp-get]')[0];
      if (!get || !get.parentNode) return;
      var col = get.parentNode;
      var ids = copies().map(function (c) { return String(c.id); });
      MM.dom.qa('.lp-pending[data-copy]', col).forEach(function (r) {
        if (ids.indexOf(r.getAttribute('data-copy')) >= 0) return;
        if (r === document.activeElement) {
          var next = r.nextSibling;
          while (next && next.classList.contains('lp-pending') && ids.indexOf(next.getAttribute('data-copy')) < 0) next = next.nextSibling;
          (next || get).focus({ preventScroll: true });
        }
        r.remove();
      });
      copies().forEach(function (c) {
        var r = col.querySelector('.lp-pending[data-copy="' + c.id + '"]') || pendingRow(c);
        col.insertBefore(r, get);
      });
    }

    function keepFollowing() {
      clearTimeout(follow);
      follow = null;
      if (!ctx.isOpen() || !onTheWay().length) return;
      follow = setTimeout(function () { ask().catch(function () { keepFollowing(); }); }, FOLLOW_MS);
    }

    /* How each copy is getting on, wherever it is drawn: its row in the menu, its card. */
    function tick() {
      copies().forEach(function (c) {
        MM.dom.qa('[data-copy="' + c.id + '"]').forEach(function (n) {
          n.style.setProperty('--p', c.progress == null ? 0 : c.progress);
          MM.dom.qa('.lp-line', n).forEach(function (line) { writeLine(line, c); });
          if (n.classList.contains('lp-pending')) fillRow(n, c);
        });
      });
    }

    function showsPct(c) { return c.status === 'downloading' && c.progress != null; }

    /* The words of a copy's line: how it is, how far, how long it has left. */
    function writeLine(line, c) {
      line.querySelector('.lp-state').textContent = statusText(c) + (showsPct(c) ? ' · ' : '');
      var pct = line.querySelector('.lp-pct');
      pct.textContent = showsPct(c) ? c.progress + '%' : '';
      pct.hidden = !showsPct(c);
      line.querySelector('.lp-left').textContent = leftText(c);
    }

    function lineOf(cls, pctTag) {
      var line = span(cls + ' lp-line');
      line.appendChild(span('lp-state'));
      var pct = el(pctTag || 'span', 'lp-pct');
      pct.hidden = true;
      line.appendChild(pct);
      line.appendChild(span('lp-left'));
      return line;
    }

    function leftText(c) {
      if (c.status !== 'downloading' || c.minutes_left == null) return '';
      return c.minutes_left < 1 ? ' · under a minute left' : ' · about ' + c.minutes_left + ' min left';
    }

    function service() { return episode() ? 'Sonarr' : 'Radarr'; }

    function statusText(c) {
      if (c.status === 'downloading') return 'On its way';
      if (c.status === 'importing') return 'Almost in · being put in place';
      if (c.status === 'stuck') return 'Stuck · ' + service() + ' won’t put it in place';
      if (c.status === 'failed') return 'Didn’t download · look for another';
      if (c.status === 'lacks') return 'Came without it · look again';
      return 'Asked for · waiting to start';
    }

    function spoken(c) {
      if (c.status === 'lacks') return en(c.language) + ' audio: the new copy came without it. Look for another.';
      if (c.status === 'failed') return en(c.language) + ' audio: the new copy didn’t download. Look for another.';
      return en(c.language) + ' audio, ' + statusText(c).replace(' · ', ', ') + (showsPct(c) ? ', ' + c.progress + ' percent' : '') + '. Show progress.';
    }

    /* ── The menu's rows ── */

    function more(main, sub, iconName, onClick) {
      var b = button('lp-more');
      var dot = span('lp-more-dot');
      dot.appendChild(icon(iconName));
      b.appendChild(dot);
      var words = span('player-av-text');
      words.appendChild(span('player-av-main', main));
      words.appendChild(span('player-av-sub', sub));
      b.appendChild(words);
      b.appendChild(icon('i-chevron-right', 'lp-more-go'));
      b.addEventListener('click', function (e) { e.stopPropagation(); onClick(); });
      return b;
    }

    function pendingRow(c) {
      var b = button('lp-pending');
      var id = c.id;
      b.setAttribute('data-copy', id);
      var words = span('player-av-text');
      words.appendChild(span('player-av-main', own(c.language)));
      var line = lineOf('player-av-sub lp-pending-line');
      words.appendChild(line);
      var bar = span('lp-pending-bar');
      bar.appendChild(el('i'));
      words.appendChild(bar);
      b.appendChild(words);
      b.style.setProperty('--p', c.progress == null ? 0 : c.progress);
      writeLine(line, c);
      fillRow(b, c);
      /* What the copy is when it is pressed, not when the row was made. */
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        var now = copyOf(id) || c;
        if (again(now)) ctx.push(audioView(now.language, now.scope === 'season' ? 'season' : episode() ? 'episode' : 'movie'));
        else ctx.push(onwayView(now));
      });
      return b;
    }

    /* The row's look by how the copy is: one to look for again, or one stuck, says so in amber. */
    function fillRow(b, c) {
      b.classList.toggle('lp-pending--lacks', again(c));
      b.classList.toggle('lp-pending--stuck', c.status === 'stuck');
      var bar = b.querySelector('.lp-pending-bar');
      bar.hidden = again(c);
      bar.classList.toggle('is-waiting', c.progress == null && c.status !== 'stuck');
      var want = again(c) ? 'i-chevron-right' : 'i-arrow-down';
      var ico = b.querySelector('.lp-pending-ico');
      if (!ico || ico.getAttribute('data-ico') !== want) {
        var next = icon(want, 'lp-pending-ico');
        next.setAttribute('data-ico', want);
        b.insertBefore(next, ico);
        if (ico) ico.remove();
      }
      b.setAttribute('aria-label', spoken(c));
    }

    function audioRows() {
      var rows = copies().map(pendingRow);
      var get = more('Get another language…', 'A new copy · can take hours', 'i-plus', function () { ctx.push(langsView('audio')); });
      get.setAttribute('data-lp-get', '');
      rows.push(get);
      return rows;
    }

    function subtitleRows() {
      return [more('Add subtitles…', 'Any language · in seconds', 'i-plus', function () { ctx.push(langsView('subs')); })];
    }

    /* ── A view's head: back, what it is, and on a phone or over a page, Close ── */

    function head(title, backLabel, noBack) {
      var h = el('div', 'lp-head');
      if (!noBack) {
        var b = button('lp-back');
        b.setAttribute('aria-label', backLabel || 'Back');
        b.setAttribute('title', backLabel || 'Back');
        b.appendChild(icon('i-chevron-left'));
        b.addEventListener('click', function (e) { e.stopPropagation(); ctx.back(); });
        h.appendChild(b);
      }
      h.appendChild(el('h2', 'lp-title', title));
      var x = button('player-av-close lp-x');
      x.setAttribute('aria-label', 'Close');
      x.appendChild(icon('i-xmark'));
      x.addEventListener('click', function (e) { e.stopPropagation(); ctx.close(true); });
      h.appendChild(x);
      return h;
    }

    function status() {
      var s = el('p', 'sr-only');
      s.setAttribute('role', 'status');
      return s;
    }

    function emptyBox(ic, title, line, acts, cls) {
      var b = el('div', 'lp-empty' + (cls ? ' ' + cls : ''));
      var dot = span('lp-empty-dot');
      dot.appendChild(icon(ic));
      b.appendChild(dot);
      b.appendChild(el('p', 'lp-empty-title', title));
      b.appendChild(el('p', 'lp-empty-line', line));
      if (acts && acts.length) {
        var a = el('div', 'lp-acts');
        acts.forEach(function (x, i) {
          var p = button('player-pill lp-act' + (x[2] ? ' player-pill--primary' : ''), x[0]);
          if (i === 0) p.setAttribute('data-first', '');
          p.addEventListener('click', function (e) { e.stopPropagation(); x[1](); });
          a.appendChild(p);
        });
        b.appendChild(a);
      }
      return b;
    }

    /* ── The languages, for subtitles and for audio ── */

    function onTitle(mode, code) {
      var s = st();
      var list = mode === 'subs'
        ? (s.subtitle_tracks || []).filter(function (t) { return !t.forced; })
        : (s.audio_tracks || []).filter(function (t) { return !t.commentary; });
      return list.filter(function (t) { return t.language === code; })[0] || null;
    }

    function langsView(mode, opts) {
      opts = opts || {};
      var v = el('div', 'lp-langs-view');
      var subs = mode === 'subs';
      v.appendChild(head(subs ? 'Add subtitles' : 'Get another language', 'Back'));
      v.appendChild(el('p', 'lp-lede', subs
        ? 'Plex searches OpenSubtitles. They’re added to this ' + thing() + ' in a few seconds.'
        : 'Audio can’t be added to a file. mediaman looks for another copy of this ' + thing() + ' that has it.'));
      var field = el('label', 'lp-search');
      field.appendChild(icon('i-magnifying-glass'));
      var input = el('input', 'lp-input');
      input.type = 'search';
      input.setAttribute('aria-label', 'Search languages');
      input.setAttribute('autocomplete', 'off');
      input.setAttribute('spellcheck', 'false');
      input.setAttribute('aria-controls', 'lp-list-' + mode);
      field.appendChild(input);
      var clear = button('lp-clear');
      clear.setAttribute('aria-label', 'Clear the search');
      clear.appendChild(icon('i-xmark'));
      clear.hidden = true;
      field.appendChild(clear);
      v.appendChild(field);
      var list = el('div', 'lp-list');
      list.id = 'lp-list-' + mode;
      list.setAttribute('role', 'listbox');
      list.setAttribute('aria-label', subs ? 'Subtitle languages' : 'Audio languages');
      list.addEventListener('scroll', function () { fadeEnds(list); });
      v.appendChild(list);
      var count = status();
      v.appendChild(count);

      var top = [];
      var groups = [];
      var chosen = opts.chosen;
      var mineDefault = null;

      /* Who comes first. Subtitles: English, then what this person picked, then the household's.
         Audio: the household's languages, then what the title has, which only switches. */
      function order() {
        var taken = {};
        var known = function (c) { return !!BY[c]; };
        function take(codes) { return (codes || []).filter(function (c) { if (taken[c] || !known(c)) return false; taken[c] = true; return true; }); }
        top = [];
        groups = [];
        if (subs) {
          top = take(['en']);
          mineDefault = info && info.subtitle_default;
          var recent = take(info && info.recent);
          if (recent.length) groups.push(['Recent', recent]);
          var common = take(info && info.household_subtitles);
          if (common.length) groups.push(['Common in this household', common]);
        } else {
          var want = take(['en'].concat((info && info.household_audio) || []).filter(function (c) { return !onTitle('audio', c); }));
          if (want.length) groups.push(['For this household', want]);
          var have = take((st().audio_tracks || []).filter(function (t) { return !t.commentary; }).map(function (t) { return t.language; }));
          if (have.length) groups.push(['On this ' + thing(), have]);
        }
        if (!chosen) chosen = top[0] || (groups[0] && groups[0][1][0]);
      }

      function mark(into, text, q) {
        var at = q ? fold(text).indexOf(q) : -1;
        if (at < 0) { into.textContent = text; return; }
        into.appendChild(document.createTextNode(text.slice(0, at)));
        into.appendChild(el('mark', 'lp-hit', text.slice(at, at + q.length)));
        into.appendChild(document.createTextNode(text.slice(at + q.length)));
      }

      function option(code, q) {
        var o = button('player-av-row lp-opt');
        o.setAttribute('role', 'option');
        o.setAttribute('aria-selected', String(code === chosen));
        o.setAttribute('tabindex', '-1');
        o.setAttribute('data-code', code);
        o.setAttribute('lang', code);
        var words = span('player-av-text');
        var main = span('player-av-main');
        mark(main, own(code), q);
        words.appendChild(main);
        if (en(code) !== own(code)) {
          var sub = span('player-av-sub');
          sub.setAttribute('lang', 'en');
          mark(sub, en(code), q);
          words.appendChild(sub);
        }
        o.appendChild(words);
        var has = onTitle(mode, code);
        if (subs && code === mineDefault && !q) o.appendChild(span('player-av-tag', 'Your default'));
        if (has) o.appendChild(span('player-av-tag lp-here', !subs && has.id === st().audio ? 'Playing' : 'On this title'));
        o.appendChild(icon('i-check', 'player-menu-tick'));
        return o;
      }

      function groupTitle(t) { var p = el('p', 'player-menu-title lp-group', t); p.setAttribute('role', 'presentation'); return p; }

      function draw(q) {
        list.textContent = '';
        clear.hidden = !q;
        input.placeholder = 'Search ' + all().length + ' languages';
        if (!info) {
          list.setAttribute('aria-busy', 'true');
          for (var i = 0; i < 6; i++) list.appendChild(skelRow());
          return;
        }
        list.removeAttribute('aria-busy');
        var sorted = all().slice().sort(function (a, b) { return a.native.localeCompare(b.native, 'en', { sensitivity: 'base' }); });
        if (!q) {
          top.forEach(function (c) { list.appendChild(option(c)); });
          groups.forEach(function (g) {
            list.appendChild(groupTitle(g[0]));
            g[1].forEach(function (c) { list.appendChild(option(c)); });
          });
          list.appendChild(groupTitle('All languages'));
          sorted.forEach(function (l) { list.appendChild(option(l.code)); });
          count.textContent = '';
        } else {
          var hits = sorted.filter(function (l) { return fold(l.native).indexOf(q) >= 0 || fold(l.name).indexOf(q) >= 0 || l.code === q; });
          if (!hits.length) {
            var none = el('div', 'lp-nomatch');
            none.appendChild(el('p', 'lp-nomatch-title', 'No language called “' + input.value.trim() + '”'));
            none.appendChild(el('p', 'lp-nomatch-line', (subs ? 'Plex searches ' : 'There are ') + all().length + ' languages. Try its English name, like “Greek”, or its own, like “Ελληνικά”.'));
            list.appendChild(none);
            count.textContent = 'No languages match.';
          } else {
            hits.forEach(function (l) { list.appendChild(option(l.code, q)); });
            count.textContent = hits.length + (hits.length === 1 ? ' language matches.' : ' languages match.');
          }
        }
        var opts2 = list.querySelectorAll('.lp-opt');
        var sel = list.querySelector('.lp-opt[aria-selected="true"]') || opts2[0];
        if (sel) sel.setAttribute('tabindex', '0');
        list.scrollTop = 0;
        requestAnimationFrame(function () { fadeEnds(list); });
      }

      function pick(o) {
        if (ctx.busy()) return;
        var code = o.getAttribute('data-code');
        chosen = code;
        list.querySelectorAll('.lp-opt').forEach(function (x) { x.setAttribute('aria-selected', String(x === o)); });
        o.classList.add('is-picked');
        ctx.busy(true);
        setTimeout(function () {
          ctx.busy(false);
          var has = onTitle(mode, code);
          if (has) {
            ctx.switchTo(subs ? { subtitle: has.id } : { audio: has.id });
            return;
          }
          if (subs) ctx.push(subResultsView(code));
          else ctx.push(audioView(code, !episode() ? 'movie' : st().wholeSeason ? 'season' : 'episode'));
        }, ms(PICKED_MS));
      }

      input.addEventListener('input', function () { draw(fold(input.value.trim())); });
      clear.addEventListener('click', function (e) { e.stopPropagation(); input.value = ''; draw(''); input.focus(); });
      list.addEventListener('click', function (e) { var o = e.target.closest('.lp-opt'); if (o) { e.stopPropagation(); pick(o); } });
      list.addEventListener('keydown', function (e) {
        var o = e.target.closest && e.target.closest('.lp-opt');
        if (!o) return;
        var opts3 = Array.prototype.slice.call(list.querySelectorAll('.lp-opt'));
        var at = opts3.indexOf(o);
        var to = null;
        if (e.key === 'ArrowDown') to = Math.min(at + 1, opts3.length - 1);
        else if (e.key === 'ArrowUp') { if (at === 0) { e.preventDefault(); input.focus(); return; } to = at - 1; }
        else if (e.key === 'Home') to = 0;
        else if (e.key === 'End') to = opts3.length - 1;
        else if (e.key === 'PageDown') to = Math.min(at + 8, opts3.length - 1);
        else if (e.key === 'PageUp') to = Math.max(at - 8, 0);
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(o); return; }
        if (to == null) return;
        e.preventDefault();
        opts3.forEach(function (x) { x.setAttribute('tabindex', '-1'); });
        opts3[to].setAttribute('tabindex', '0');
        opts3[to].focus();
        if (opts3[to].scrollIntoView) opts3[to].scrollIntoView({ block: 'nearest' });
      });
      input.addEventListener('keydown', function (e) {
        var first = list.querySelector('.lp-opt');
        if (e.key === 'ArrowDown' && first) { e.preventDefault(); (list.querySelector('.lp-opt[tabindex="0"]') || first).focus(); }
        else if (e.key === 'Enter') { e.preventDefault(); var only = list.querySelector('.lp-opt[tabindex="0"]') || first; if (only) pick(only); }
        else if (e.key === 'Escape' && input.value) { e.preventDefault(); e.stopPropagation(); input.value = ''; draw(''); }
      });
      /* Typing anywhere in the view goes to the search. */
      v.addEventListener('keydown', function (e) {
        if (e.target === input || e.ctrlKey || e.metaKey || e.altKey) return;
        if ((e.key.length === 1 && e.key !== ' ') || (e.key === 'Backspace' && input.value)) input.focus();
      });
      if (opts.q) input.value = opts.q;
      function fill() {
        order();
        draw(fold((opts.q || input.value || '').trim()));
      }
      fill();
      if (!info) ask().then(function () { ctx.settle(fill); }, function () {
        list.textContent = '';
        list.removeAttribute('aria-busy');
        list.appendChild(emptyBox('i-triangle-exclamation', 'The languages didn’t load', 'Plex or mediaman didn’t answer. Try again in a moment.',
          [['Try again', function () { list.textContent = ''; draw(''); ask().then(function () { ctx.settle(fill); }, function () {}); }, true]], 'lp-empty--error'));
      });
      /* On a laptop the search takes the keyboard; on a phone the chosen language does, so no
         keyboard springs up over the list. */
      (phone() && !opts.q ? (list.querySelector('.lp-opt[tabindex="0"]') || input) : input).setAttribute('data-first', '');
      return {
        el: v, deep: true, label: subs ? 'Add subtitles' : 'Get another language',
        remake: function () { return langsView(mode, { chosen: chosen }); },
        after: function () { requestAnimationFrame(function () { fadeEnds(list); }); },
      };
    }

    function skelRow() {
      var s = el('div', 'lp-skel-row');
      s.appendChild(span('skel skel--line lp-skel-a'));
      s.appendChild(span('skel skel--line lp-skel-b'));
      return s;
    }

    /* ── Subtitles Plex found ── */

    function kindOf(r) {
      if (r.forced) return 'Signs & songs only';
      if (r.hearing_impaired) return 'SDH · sounds described too';
      return 'Full subtitles';
    }

    function subResultsView(code) {
      var v = el('div', 'lp-results-view');
      var alive = true;
      v.appendChild(head(en(code) + ' subtitles', 'Back to languages'));
      var lede = el('p', 'lp-lede');
      v.appendChild(lede);
      var list = el('div', 'lp-list lp-results');
      list.setAttribute('role', 'listbox');
      list.setAttribute('aria-label', en(code) + ' subtitles found');
      list.addEventListener('scroll', function () { fadeEnds(list); });
      v.appendChild(list);
      var said = status();
      v.appendChild(said);

      function loading() {
        v.classList.remove('lp-short');
        lede.textContent = 'Searching OpenSubtitles…';
        list.textContent = '';
        list.hidden = false;
        list.setAttribute('aria-busy', 'true');
        for (var i = 0; i < 3; i++) list.appendChild(skelRow());
        MM.dom.qa('.lp-empty', v).forEach(function (n) { n.remove(); });
      }
      function search() {
        loading();
        MM.api.get(base + '/subtitles?language=' + encodeURIComponent(code)).then(function (data) {
          if (alive) ctx.settle(function () { results(data.found || []); });
        }, function (err) {
          if (alive) ctx.settle(function () { failed(err); });
        });
      }
      function results(found) {
        list.textContent = '';
        list.removeAttribute('aria-busy');
        if (!found.length) { empty(); return; }
        lede.textContent = found.length + ' found on OpenSubtitles · best match first';
        said.textContent = found.length + ' ' + en(code) + ' subtitles found.';
        found.forEach(function (r, i) {
          var o = button('player-av-row lp-opt lp-res');
          o.setAttribute('role', 'option');
          o.setAttribute('aria-selected', 'false');
          o.setAttribute('tabindex', i === 0 ? '0' : '-1');
          if (i === 0) o.setAttribute('data-first', '');
          var words = span('player-av-text');
          words.appendChild(span('player-av-main', kindOf(r)));
          var sub = span('player-av-sub lp-file', r.file);
          sub.title = r.file;
          words.appendChild(sub);
          o.appendChild(words);
          if (i === 0) o.appendChild(span('player-av-tag lp-best', 'Best match'));
          var add = span('lp-add');
          add.appendChild(span('lp-add-word', 'Add'));
          o.appendChild(add);
          o.addEventListener('click', function (e) { e.stopPropagation(); adding(o, r, i); });
          list.appendChild(o);
        });
        arrows(list);
        requestAnimationFrame(function () { fadeEnds(list); });
        if (ctx.byKeys() && document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('player-av')) list.querySelector('[data-first]').focus({ preventScroll: true });
      }
      function empty() {
        lede.textContent = '';
        list.hidden = true;
        v.classList.add('lp-short');
        v.appendChild(emptyBox('i-captions', 'No ' + en(code) + ' subtitles for this ' + thing() + ' yet',
          'OpenSubtitles has none. People add them often, so look again in a few days.',
          [['Pick another language', function () { ctx.back(); }, true]]));
        said.textContent = 'No ' + en(code) + ' subtitles found.';
      }
      function failed(err) {
        lede.textContent = '';
        list.textContent = '';
        list.hidden = true;
        v.classList.add('lp-short');
        var busy = err && err.status === 429;
        var e = emptyBox('i-triangle-exclamation', busy ? 'Too many searches just now' : 'Couldn’t search for ' + en(code) + ' subtitles',
          busy ? 'Wait a minute, then look again.' : 'Plex didn’t manage the search this time. Nothing was added; try again in a moment.',
          [['Try again', search, true], ['Back', function () { ctx.back(); }]], 'lp-empty--error');
        e.setAttribute('role', 'alert');
        v.appendChild(e);
      }
      function adding(o, r, index) {
        if (ctx.busy() || list.classList.contains('is-adding')) return;
        list.classList.add('is-adding');
        o.classList.add('is-adding');
        o.setAttribute('aria-selected', 'true');
        list.querySelectorAll('.lp-res').forEach(function (x) { if (x !== o) x.disabled = true; });
        var add = o.querySelector('.lp-add');
        add.textContent = '';
        add.appendChild(span('player-btn-ring lp-ring'));
        add.appendChild(span('lp-add-word', 'Adding…'));
        lede.textContent = 'Adding to Plex. It takes a few seconds.';
        said.textContent = 'Adding ' + en(code) + ' subtitles. It takes a few seconds.';
        MM.api.post(base + '/subtitles', { language: code, index: index }).then(function (data) {
          if (!alive) return;
          if (!data.subtitle) {
            done();
            lede.textContent = 'Plex is still adding them. They’ll be in the menu next time it opens.';
            said.textContent = lede.textContent;
            return;
          }
          ctx.added({
            id: String(data.subtitle), language: code, label: en(code), language_name: en(code),
            forced: !!r.forced, hearing_impaired: !!r.hearing_impaired,
          });
        }, function (err) {
          if (!alive) return;
          done();
          lede.textContent = err && err.error === 'search_again'
            ? 'That search is too old. Go back and pick the language again.'
            : err && err.status === 429
              ? 'Subtitles you asked for are still being added. Try again once they’re in.'
              : 'Couldn’t add them: Plex didn’t answer. Try again.';
          said.textContent = lede.textContent;
        });
        function done() {
          list.classList.remove('is-adding');
          o.classList.remove('is-adding');
          o.setAttribute('aria-selected', 'false');
          list.querySelectorAll('.lp-res').forEach(function (x) { x.disabled = false; });
          add.textContent = '';
          add.appendChild(span('lp-add-word', 'Add'));
        }
      }
      search();
      return {
        el: v, label: en(code) + ' subtitles',
        gone: function () { alive = false; },
      };
    }

    /* ── Another copy, with that audio ── */

    function chip(code, cls) {
      var c = span('lp-chip' + (cls ? ' ' + cls : ''), own(code));
      c.setAttribute('lang', code);
      return c;
    }
    function chips(codes, want) {
      var row = span('lp-chips');
      codes.forEach(function (c) { row.appendChild(chip(c, c === want ? 'lp-chip--want' : '')); });
      return row;
    }
    function shortName(code) { return en(code).replace(/ \(.*\)$/, ''); }

    function copyLine(have) {
      var p = el('div', 'lp-have');
      p.appendChild(span('lp-have-label', 'You have'));
      p.appendChild(chips(have.languages || []));
      var meta = [have.quality, sizeText(have.size_bytes)].filter(Boolean).join(' · ');
      if (meta) p.appendChild(span('lp-have-meta', meta));
      return p;
    }

    function copyCard(ver, want, i, have, still) {
      var c = button('lp-ver');
      if (still) { c.disabled = true; c.classList.add('is-still'); c.setAttribute('role', 'presentation'); }
      else {
        c.setAttribute('role', 'option');
        c.setAttribute('aria-selected', 'false');
        c.setAttribute('tabindex', i === 0 ? '0' : '-1');
        if (i === 0) c.setAttribute('data-first', '');
      }
      var top = span('lp-ver-top');
      top.appendChild(chips(ver.languages, want));
      if (i === 0 && !still) top.appendChild(span('player-av-tag lp-best', 'Best match'));
      c.appendChild(top);
      var meta = [ver.season_pack ? seasonName() : '', ver.quality, sizeText(ver.size_bytes), ageText(ver.age_days), ver.indexer].filter(Boolean).join(' · ');
      c.appendChild(span('lp-ver-meta', meta));
      var line = span('lp-ver-line');
      line.appendChild(document.createTextNode(ver.season_pack ? 'Replaces the season’s episodes once it’s ready' : 'Replaces the copy you have once it’s ready'));
      var gone = (ver.loses || []).map(shortName);
      if (gone.length) {
        line.appendChild(document.createTextNode(' · '));
        line.appendChild(span('lp-lose', andList(gone) + ' audio goes'));
      } else if ((have.languages || []).length) {
        line.appendChild(document.createTextNode(' · keeps ' + andList(have.languages.map(shortName))));
      }
      c.appendChild(line);
      if (!still) c.appendChild(icon('i-chevron-right', 'lp-ver-go'));
      return c;
    }

    function seg(scope, onPick) {
      var s = el('div', 'lp-seg');
      s.setAttribute('role', 'radiogroup');
      s.setAttribute('aria-label', 'What to look for');
      var thumb = span('lp-seg-thumb');
      thumb.setAttribute('aria-hidden', 'true');
      s.appendChild(thumb);
      [['episode', 'This episode'], ['season', 'Whole season']].forEach(function (p) {
        var b = button('lp-seg-btn', p[1]);
        b.setAttribute('role', 'radio');
        b.setAttribute('data-scope', p[0]);
        b.addEventListener('click', function (e) { e.stopPropagation(); onPick(p[0]); });
        s.appendChild(b);
      });
      s.addEventListener('keydown', function (e) {
        if (!/^Arrow(Left|Right)$/.test(e.key)) return;
        e.preventDefault();
        var other = s.querySelector('[aria-checked="false"]');
        if (other) other.click();
      });
      function mark(to) {
        s.classList.toggle('is-second', to === 'season');
        s.querySelectorAll('.lp-seg-btn').forEach(function (b) {
          var on = b.getAttribute('data-scope') === to;
          b.setAttribute('aria-checked', String(on));
          b.setAttribute('tabindex', on ? '0' : '-1');
        });
      }
      mark(scope);
      s.mark = mark;
      return s;
    }

    function audioView(code, scope) {
      var v = el('div', 'lp-audio-view');
      var alive = true;
      var round = 0;
      var found = null;
      v.appendChild(head(shortName(code) + ' audio', 'Back to languages'));
      var segEl = null;
      if (scope !== 'movie') {
        segEl = seg(scope, function (to) {
          if (to === scope) return;
          scope = to;
          segEl.mark(to);
          var b = segEl.querySelector('[aria-checked="true"]');
          if (b) b.focus();
          ctx.settle(search);
        });
        v.appendChild(segEl);
      }
      var haveSlot = el('div', 'lp-have-slot');
      v.appendChild(haveSlot);
      if (info && info.have) haveSlot.appendChild(copyLine(info.have));
      var body = el('div', 'lp-audio-body');
      v.appendChild(body);
      var said = status();
      v.appendChild(said);

      function what() { return scope === 'season' ? seasonName() : scope === 'episode' ? 'this episode' : 'this film'; }

      function search() {
        var mine = ++round;
        v.classList.remove('lp-short');
        body.textContent = '';
        var line = el('p', 'lp-looking');
        line.appendChild(span('player-btn-ring lp-ring'));
        line.appendChild(document.createTextNode('Looking for copies of ' + what() + ' with ' + shortName(code) + ' audio…'));
        body.appendChild(line);
        for (var i = 0; i < 3; i++) {
          var s = el('div', 'lp-skel-ver');
          s.appendChild(span('skel skel--line lp-skel-c'));
          s.appendChild(span('skel skel--line lp-skel-a'));
          s.appendChild(span('skel skel--line lp-skel-b'));
          body.appendChild(s);
        }
        said.textContent = 'Looking for copies with ' + shortName(code) + ' audio.';
        MM.api.get(base + '/audio?language=' + encodeURIComponent(code) + '&scope=' + scope).then(function (data) {
          if (!alive || mine !== round) return;
          found = data;
          haveSlot.textContent = '';
          haveSlot.appendChild(copyLine(data.have || {}));
          ctx.settle(results);
        }, function (err) {
          if (!alive || mine !== round) return;
          ctx.settle(function () { failed(err); });
        });
      }

      function results() {
        var copies = found.found || [];
        body.textContent = '';
        if (!copies.length) { nothing(); return; }
        var list = el('div', 'lp-list lp-vers');
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', 'Copies with ' + shortName(code) + ' audio');
        list.addEventListener('scroll', function () { fadeEnds(list); });
        copies.forEach(function (ver, i) {
          var c = copyCard(ver, code, i, found.have || {});
          c.addEventListener('click', function (e) { e.stopPropagation(); ctx.push(confirmView(code, scope, ver, i, found)); });
          list.appendChild(c);
        });
        body.appendChild(el('p', 'lp-found', copies.length + (copies.length === 1 ? ' copy' : ' copies') + ' found · best match first'));
        body.appendChild(list);
        said.textContent = copies.length + (copies.length === 1 ? ' copy' : ' copies') + ' with ' + shortName(code) + ' audio found.';
        arrows(list);
        requestAnimationFrame(function () { fadeEnds(list); });
      }

      function nothing() {
        v.classList.add('lp-short');
        var acts = [];
        if (scope === 'episode') acts.push(['Look at the whole season', function () { segEl.querySelector('[data-scope="season"]').click(); }]);
        body.appendChild(emptyBox('i-magnifying-glass', 'No ' + shortName(code) + ' audio for ' + what(),
          scope === 'movie'
            ? 'No release of this film that ' + found.service + ' would take has ' + shortName(code) + ' audio.'
            : 'No release of ' + what() + ' that ' + found.service + ' would take has ' + shortName(code) + ' audio. mediaman can’t make a dub.', acts));
        said.textContent = 'No copies with ' + shortName(code) + ' audio.';
        if (code.length === 2) offerSubtitles();
      }

      /* Where no copy has the audio, subtitles in the language may. */
      function offerSubtitles() {
        MM.api.get(base + '/subtitles?language=' + encodeURIComponent(code)).then(function (data) {
          var n = (data.found || []).length;
          if (!alive || !n || !v.classList.contains('lp-short')) return;
          var f = el('div', 'lp-fallback');
          var dot = span('lp-more-dot');
          dot.appendChild(icon('i-captions'));
          f.appendChild(dot);
          var words = span('lp-fallback-text');
          words.appendChild(el('span', 'lp-fallback-title', own(code) + ' subtitles are there now'));
          words.appendChild(el('span', 'lp-fallback-line', n + ' on OpenSubtitles · added in a few seconds'));
          f.appendChild(words);
          var b = button('player-pill player-pill--primary lp-act', 'Add subtitles');
          b.setAttribute('aria-label', 'Add ' + en(code) + ' subtitles');
          b.addEventListener('click', function (e) { e.stopPropagation(); ctx.push(subResultsView(code)); });
          f.appendChild(b);
          ctx.settle(function () { body.appendChild(f); });
          said.textContent = 'No copies with ' + shortName(code) + ' audio. ' + en(code) + ' subtitles can be added instead.';
        }, function () { /* the audio's answer stands alone */ });
      }

      function failed(err) {
        v.classList.add('lp-short');
        body.textContent = '';
        var service = episode() ? 'Sonarr' : 'Radarr';
        var code2 = err && err.error;
        var title = service + ' didn’t answer';
        var line = 'Nothing was asked for and nothing changed. It may be busy; try again in a moment.';
        if (code2 === 'not_in_arr') { title = 'This ' + thing() + ' isn’t in ' + service; line = 'A copy can only be found for what ' + service + ' has.'; }
        else if (code2 === 'not_set_up') { title = service + ' isn’t set up'; line = 'Ask whoever runs mediaman to connect it in Settings.'; }
        else if (err && err.status === 429) { title = 'Too many searches just now'; line = 'Each one asks every indexer. Wait a minute, then look again.'; }
        var e = emptyBox('i-triangle-exclamation', title, line,
          [['Try again', function () { ctx.settle(search); }, true], ['Back', function () { ctx.back(); }]], 'lp-empty--error');
        e.setAttribute('role', 'alert');
        body.appendChild(e);
      }

      search();
      return {
        el: v, label: shortName(code) + ' audio',
        gone: function () { alive = false; },
      };
    }

    function confirmView(code, scope, ver, index, found) {
      var v = el('div', 'lp-confirm-view');
      var alive = true;
      v.appendChild(head('Get ' + shortName(code) + ' audio?', 'Back to the copies'));
      /* What it says scrolls, and the answer stays at the foot of the glass however short it is. */
      var body = scroller(v);
      body.appendChild(copyCard(ver, code, 0, found.have || {}, true));
      var ul = el('ul', 'lp-facts');
      function li(ic, text, cls) { var x = el('li', cls || ''); x.appendChild(icon(ic)); x.appendChild(span('', text)); ul.appendChild(x); }
      li('i-download', 'Downloads ' + (sizeText(ver.size_bytes) || 'it') + (ver.indexer ? ' from ' + ver.indexer : '') + '. That takes minutes to hours.');
      li('i-play', 'What you have keeps playing until the new copy is in. Then it takes its place, for everyone here.');
      var gone = (ver.loses || []).map(shortName);
      if (gone.length) li('i-triangle-exclamation', andList(gone) + ' audio goes with the old copy. ' + found.service + ' keeps one copy of each ' + thing() + '.', 'lp-warn');
      body.appendChild(ul);
      var note = el('p', 'lp-confirm-note');
      note.setAttribute('role', 'alert');
      note.hidden = true;
      body.appendChild(note);
      var acts = el('div', 'lp-acts lp-acts--end');
      var no = button('player-pill lp-act', 'Back');
      no.addEventListener('click', function (e) { e.stopPropagation(); ctx.back(); });
      var yes = button('player-pill player-pill--primary lp-act', ver.season_pack ? 'Get the season' : 'Get this copy');
      yes.setAttribute('data-first', '');
      yes.addEventListener('click', function (e) {
        e.stopPropagation();
        if (yes.disabled) return;
        yes.disabled = no.disabled = true;
        yes.textContent = 'Asking…';
        MM.api.post(base + '/audio', { language: code, scope: scope, index: index }).then(function (data) {
          if (!alive) return;
          take(Object.assign({}, info || {}, { copies: data.copies || [] }));
          var mine = (data.copies || []).filter(function (c) { return c.language === code; })[0];
          if (mine) ctx.ahead(onwayView(mine));
          else ctx.home();
        }, function (err) {
          if (!alive) return;
          yes.disabled = no.disabled = false;
          yes.textContent = ver.season_pack ? 'Get the season' : 'Get this copy';
          note.hidden = false;
          requestAnimationFrame(function () { reveal(note, body); });
          note.textContent = err && err.status === 429
            ? 'Too many downloads asked for just now. Try again in a minute.'
            : err && err.error === 'search_again'
              ? 'That search is too old. Go back and look again.'
              : err && err.error === 'already_asked'
                ? 'A copy with ' + shortName(code) + ' audio is already on its way for this ' + thing() + '.'
              : found.service + ' didn’t take it. Nothing was asked for; try again in a moment.';
        });
      });
      acts.appendChild(no);
      acts.appendChild(yes);
      v.appendChild(acts);
      return { el: v, label: 'Get ' + shortName(code) + ' audio?', gone: function () { alive = false; }, after: function () { fadeEnds(body); } };
    }

    function onwayView(c) {
      var s = st();
      var v = el('div', 'lp-onway-view');
      var page = ctx.onPage();
      v.appendChild(head(shortName(c.language) + ' audio', page ? 'Back to Add a language' : 'Back to Audio & subtitles'));
      var body = scroller(v);
      var dw = el('div', 'dw lp-dw');
      dw.setAttribute('data-copy', c.id);
      dw.style.setProperty('--p', c.progress || 0);
      var a = el('div', 'dw__link');
      if (s.art) {
        var art = el('img', 'dw__art');
        art.src = s.art;
        art.alt = '';
        var fill = el('img', 'dw__fill');
        fill.src = s.art;
        fill.alt = '';
        a.appendChild(art);
        a.appendChild(fill);
      }
      var text = span('dw__text');
      text.appendChild(span('dw__title', s.title + (c.scope === 'season' ? ' · ' + seasonName() : '')));
      var line = lineOf('dw__line', 'strong');
      writeLine(line, c);
      text.appendChild(line);
      text.appendChild(span('dw__fix', 'With ' + andList((c.release_languages || []).map(shortName)) + ' audio' + (c.quality ? ' · ' + c.quality : '')));
      a.appendChild(text);
      var bar = span('dw__bar');
      bar.appendChild(el('i'));
      a.appendChild(bar);
      dw.appendChild(a);
      body.appendChild(dw);
      var ul = el('ul', 'lp-facts');
      function li(ic, t) { var x = el('li'); x.appendChild(icon(ic)); x.appendChild(span('', t)); ul.appendChild(x); }
      /* Over the title's page nothing plays yet, and its menu of copies is the player's. */
      li('i-play', page ? 'What you have plays until the new copy is in.' : 'Keep watching. This copy plays until the new one is in.');
      li('i-download', page ? 'You’ll find it in Downloads too, and in the player under Audio until it’s done.' : 'You’ll find it in Downloads too, and here under Audio until it’s done.');
      body.appendChild(ul);
      var acts = el('div', 'lp-acts lp-acts--end');
      var done = button('player-pill player-pill--primary lp-act', 'Done');
      done.setAttribute('data-first', '');
      done.addEventListener('click', function (e) { e.stopPropagation(); ctx.home(); });
      acts.appendChild(done);
      v.appendChild(acts);
      return { el: v, label: shortName(c.language) + ' audio, on its way', after: function () { fadeEnds(body); } };
    }

    /* ── From the title page: which kind first ── */

    function kindView() {
      var v = el('div', 'lp-kind-view');
      v.appendChild(head('Add a language', 'Close', true));
      var list = el('div', 'lp-kind');
      var first = more('Subtitles', 'Any language · in seconds', 'i-captions', function () { ctx.push(langsView('subs')); });
      first.setAttribute('data-first', '');
      list.appendChild(first);
      list.appendChild(more('Audio', 'A new copy · can take hours', 'i-volume-high', function () { ctx.push(langsView('audio')); }));
      v.appendChild(list);
      var s = st();
      var audio = andList(uniq((s.audio_tracks || []).filter(function (t) { return !t.commentary; }).map(function (t) { return en(t.language); })));
      var subs = andList(uniq((s.subtitle_tracks || []).filter(function (t) { return !t.forced; }).map(function (t) { return en(t.language); })));
      var f = el('div', 'player-av-foot');
      f.appendChild(icon('i-circle-info'));
      f.appendChild(span('player-av-foot-text', (st().wholeSeason ? 'Its next episode has ' : 'This ' + thing() + ' has ') + (audio ? audio + ' audio' : 'audio in no known language') + (subs ? ', and ' + subs + ' subtitles.' : ', and no subtitles.')));
      v.appendChild(f);
      return { el: v, label: 'Add a language', sub: true, remake: kindView };
    }

    function uniq(xs) { return xs.filter(function (x, i) { return x && xs.indexOf(x) === i; }); }

    return {
      key: key,
      audioRows: audioRows,
      subtitleRows: subtitleRows,
      kindView: kindView,
      opened: function () { ask().catch(function () { /* the rows stand without it */ }); },
      closed: function () { clearTimeout(follow); follow = null; },
    };
  }

  MM.languages = { create: create, andList: andList };
})(window);
