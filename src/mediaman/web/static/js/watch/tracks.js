/**
 * watch/tracks.js — the Audio & subtitles chooser, of the player (player.js) and of the remote
 * of a TV (tv-remote.js).
 *
 * MM.tracks.create({wraps, root, opening, closed, pick}) puts a control in each of `wraps` that opens
 * the chooser: a popover on a laptop, a sheet from the foot on a phone, from the right edge on
 * a phone on its side (the stylesheet decides which). Its two groups are radios: the arrow keys,
 * Home and End move within one, Tab goes on to the next, Escape closes it and gives the focus
 * back. A pick ticks, the chooser goes, and then `pick({audio: id})`, `pick({subtitle: id or
 * 'off'})` or `pick({reset: true})` is called; what plays is the host's to say back with set().
 *
 * set(state) takes a title's tracks as the API gives them:
 *   {audio, subtitle, audio_tracks, subtitle_tracks, remembered, defaults, title, series, on}
 * where `on` is the name of the TV that plays it, or null for this device. With no tracks the
 * control is hidden.
 *
 * MM.tracks.said(...) words what changed, and a change that failed, alike for both.
 */
(function (global) {
  'use strict';

  var MM = global.MM = global.MM || {};
  var CHANNELS = { 1: 'Mono', 2: 'Stereo', 6: '5.1 surround', 7: '6.1 surround', 8: '7.1 surround' };
  var PICKED_MS = 170;       // the tick is seen to land before the chooser goes
  var CLOSED_MS = 120;       // and the chooser is on its way before the picture is held
  var LEAVE_MS = 260;
  var DEFAULTS_HREF = '/?you=profile/audio';
  var TV_ICON = 'play-on-tv'; // of _play_on.html

  var reduce = global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function ms(n) { return reduce && reduce.matches ? Math.min(n, 150) : n; }

  /* A language by its own name, and by its English one. */
  var names = {};
  function nameIn(code, lang) {
    var key = code + '|' + lang;
    if (!(key in names)) {
      var n = null;
      try { n = new Intl.DisplayNames([lang], { type: 'language' }).of(code); } catch (e) { n = null; }
      names[key] = n && n.toLowerCase() !== code.toLowerCase() ? n.charAt(0).toLocaleUpperCase(lang) + n.slice(1) : null;
    }
    return names[key];
  }
  function ownName(t) { return (t.language && nameIn(t.language, t.language)) || t.language_name || t.label; }
  function englishName(code) { return code ? nameIn(code, 'en') : null; }

  function audioText(t) {
    var main = ownName(t);
    var english = englishName(t.language);
    var parts = [];
    if (t.commentary) parts.push(t.title || 'Commentary');
    else if (english && english !== main) parts.push(english);
    if (CHANNELS[t.channels]) parts.push(CHANNELS[t.channels]);
    else if (t.channels) parts.push(t.channels + ' channels');
    return { main: main, sub: parts.join(' · '), short: t.commentary ? 'Commentary' : main, said: main + (t.commentary ? ' commentary' : ' audio') };
  }

  var OFF = { main: 'Off', sub: '', short: '', said: 'Subtitles off' };
  function subtitleText(t) {
    if (!t) return OFF;
    var main = ownName(t);
    var english = englishName(t.language);
    if (t.forced) {
      return { main: 'Signs & songs only', sub: [english || (t.language ? main : ''), 'Forced'].filter(Boolean).join(' · '),
        short: 'Signs & songs', said: 'Signs & songs only' };
    }
    var parts = [english && english !== main ? english : ''];
    if (t.hearing_impaired) parts.push('SDH · sounds described too');
    var short = main + (t.hearing_impaired ? ' SDH' : '');
    return { main: main, sub: parts.filter(Boolean).join(' · '), short: short, said: short + ' subtitles' };
  }

  function find(list, id) {
    return (list || []).filter(function (t) { return t.id === id; })[0] || null;
  }

  /* What plays, in words. */
  function describe(state) {
    var audio = find(state.audio_tracks, state.audio) || (state.audio_tracks || [])[0] || null;
    return {
      audio: audio ? audioText(audio) : null,
      subtitle: subtitleText(state.subtitle ? find(state.subtitle_tracks, state.subtitle) : null),
    };
  }

  function langWords(code) { return (code && (nameIn(code, 'en') || code)) || ''; }

  function defaultsLine(d) {
    d = d || {};
    var audio = d.audio_lang ? langWords(d.audio_lang) : 'each title as it comes';
    var subs = d.subtitle_lang === 'off' ? 'subtitles off'
      : d.subtitle_lang ? langWords(d.subtitle_lang) + ' subtitles'
        : 'subtitles when needed';
    return 'From your defaults: ' + audio + ', ' + subs + '.';
  }

  /* What a change says: `what` is 'audio', 'subtitle' or 'reset'; with `failed`, what stays. */
  function said(what, state, failed) {
    var now = describe(state);
    if (failed) {
      if (what === 'subtitle') return 'Couldn’t change the subtitles. Staying on ' + (now.subtitle === OFF ? 'none' : now.subtitle.short) + '.';
      return 'Couldn’t change the audio. Staying on ' + (now.audio ? now.audio.main : 'the one that plays') + '.';
    }
    if (what === 'reset') {
      return 'Back to your defaults: ' + (now.audio ? now.audio.main : 'as it comes') + (now.subtitle === OFF ? '.' : ', ' + now.subtitle.said + '.');
    }
    var text = what === 'audio' ? (now.audio ? now.audio.said : 'The audio') : now.subtitle.said;
    return state.on ? text + ' on ' + state.on + '.' : text + '. Kept for ' + state.title + '.';
  }

  function span(className, text) {
    var s = document.createElement('span');
    if (className) s.className = className;
    if (text) s.textContent = text;
    return s;
  }

  function create(opts) {
    var state = null;
    var box = null;
    var veil = null;
    var from = null;
    var busy = false;
    var seen;

    var triggers = opts.wraps.map(function (wrap) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'player-pill player-av-btn';
      b.setAttribute('data-av-btn', '');
      b.setAttribute('aria-haspopup', 'dialog');
      b.setAttribute('aria-expanded', 'false');
      b.setAttribute('title', 'Audio and subtitles');
      b.appendChild(MM.dom.icon('i-captions'));
      var label = span('player-av-state');
      label.appendChild(span('player-av-audio'));
      label.appendChild(span('player-av-more'));
      b.appendChild(label);
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        if (box) close(false);
        else open(b, e.detail === 0);
      });
      wrap.appendChild(b);
      wrap.hidden = true;
      return b;
    });

    function showState() {
      var known = !!state && ((state.audio_tracks || []).length > 0 || (state.subtitle_tracks || []).length > 0);
      opts.wraps.forEach(function (w) { w.hidden = !known; });
      if (!known) { close(false); return; }
      var now = describe(state);
      var audio = now.audio ? now.audio.short : 'Audio';
      var more = now.subtitle === OFF ? '' : ' · ' + (now.subtitle.short === 'Signs & songs' ? 'signs & songs' : now.subtitle.short + ' subtitles');
      var spoken = 'Audio and subtitles: ' + (now.audio ? now.audio.said : 'audio') + ', ' + (now.subtitle === OFF ? 'subtitles off' : now.subtitle.said);
      triggers.forEach(function (b) {
        MM.dom.setText(b.querySelector('.player-av-audio'), audio);
        MM.dom.setText(b.querySelector('.player-av-more'), more);
        b.setAttribute('aria-label', spoken);
      });
    }

    function row(group, id, text, checked, tag) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'player-av-row';
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(checked));
      b.setAttribute('tabindex', checked ? '0' : '-1');
      b.setAttribute('data-' + group, id);
      var words = span('player-av-text');
      words.appendChild(span('player-av-main', text.main));
      if (text.sub) words.appendChild(span('player-av-sub', text.sub));
      b.appendChild(words);
      if (tag) b.appendChild(span('player-av-tag', tag));
      b.appendChild(MM.dom.icon('i-check', 'player-menu-tick'));
      return b;
    }

    function fadeEnds(list) {
      list.classList.toggle('has-more-above', list.scrollTop > 1);
      list.classList.toggle('has-more-below', list.scrollTop + list.clientHeight < list.scrollHeight - 1);
    }

    function column(group, title, rows, none) {
      var col = document.createElement('div');
      col.className = 'player-av-col';
      var head = document.createElement('p');
      head.className = 'player-menu-title';
      head.id = 'player-av-' + group;
      head.textContent = title;
      col.appendChild(head);
      var list = document.createElement('div');
      list.className = 'player-av-list';
      list.setAttribute('role', 'radiogroup');
      list.setAttribute('aria-labelledby', head.id);
      rows.forEach(function (r) { list.appendChild(r); });
      list.addEventListener('scroll', function () { fadeEnds(list); });
      col.appendChild(list);
      if (none) {
        var p = document.createElement('p');
        p.className = 'player-av-none';
        p.textContent = none;
        col.appendChild(p);
      }
      return col;
    }

    function foot() {
      var f = document.createElement('div');
      f.className = 'player-av-foot';
      var words = span('player-av-foot-text');
      var link = null;
      if (state.on) {
        f.appendChild(MM.dom.icon(TV_ICON, 'ico--tv'));
        words.appendChild(document.createTextNode('Changes play on '));
        words.appendChild(document.createElement('b')).textContent = state.on;
        words.appendChild(document.createTextNode('. The picture restarts where it is; it takes a few seconds.'));
      } else if (state.remembered) {
        f.appendChild(MM.dom.icon('i-check'));
        words.appendChild(document.createTextNode(state.series ? 'Kept for every episode of ' : 'Kept for '));
        words.appendChild(document.createElement('b')).textContent = state.title;
        words.appendChild(document.createTextNode('.'));
        link = document.createElement('button');
        link.type = 'button';
        link.setAttribute('data-av-reset', '');
        link.textContent = 'Use my defaults';
      } else {
        f.appendChild(MM.dom.icon('i-circle-info'));
        words.textContent = defaultsLine(state.defaults);
        link = document.createElement('a');
        link.href = DEFAULTS_HREF;
        link.textContent = 'Change';
      }
      f.appendChild(words);
      if (link) {
        link.className = 'player-av-link';
        f.appendChild(link);
      }
      return f;
    }

    function build() {
      var b = document.createElement('div');
      b.className = 'player-av';
      b.setAttribute('role', 'dialog');
      b.setAttribute('aria-label', 'Audio and subtitles');
      b.tabIndex = -1;
      var head = document.createElement('div');
      head.className = 'player-av-head';
      head.appendChild(span('', 'Audio & subtitles'));
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'player-av-close';
      x.setAttribute('data-av-close', '');
      x.setAttribute('aria-label', 'Close');
      x.appendChild(MM.dom.icon('i-xmark'));
      head.appendChild(x);
      b.appendChild(head);
      var titles = document.createElement('div');
      titles.className = 'player-av-titles';
      titles.setAttribute('aria-hidden', 'true');
      ['Audio', 'Subtitles'].forEach(function (t) {
        var p = document.createElement('p');
        p.className = 'player-menu-title';
        p.textContent = t;
        titles.appendChild(p);
      });
      b.appendChild(titles);
      var cols = document.createElement('div');
      cols.className = 'player-av-cols';
      cols.addEventListener('scroll', function () { fadeEnds(cols); });

      var audio = state.audio_tracks || [];
      var playing = find(audio, state.audio) || audio[0];
      var mine = (state.defaults || {}).audio_lang;
      var tagged = mine && audio.filter(function (t) { return t.language === mine && !t.commentary; })[0];
      cols.appendChild(column('audio', 'Audio', audio.map(function (t) {
        return row('audio', t.id, audioText(t), t === playing, t === tagged ? 'Your default' : '');
      }), audio.length === 1 ? 'This title has one audio track.' : ''));

      var subs = state.subtitle_tracks || [];
      var shown = state.subtitle && find(subs, state.subtitle) ? state.subtitle : 'off';
      var rows = [row('subtitle', 'off', OFF, shown === 'off')].concat(subs.map(function (t) {
        return row('subtitle', t.id, subtitleText(t), t.id === shown);
      }));
      cols.appendChild(column('subtitle', 'Subtitles', rows, subs.length ? '' : 'This title has no subtitles.'));
      b.appendChild(cols);
      b.appendChild(foot());
      b.addEventListener('click', onClick);
      return b;
    }

    function open(trigger, byKey) {
      if (box || !state) return;
      if (opts.opening) opts.opening();
      from = trigger;
      veil = document.createElement('div');
      veil.className = 'player-av-veil';
      veil.setAttribute('data-av-close', '');
      veil.addEventListener('click', function () { close(true); });
      box = build();
      trigger.parentNode.appendChild(veil);
      trigger.parentNode.appendChild(box);
      box.querySelectorAll('.player-av-list, .player-av-cols').forEach(fadeEnds);
      trigger.setAttribute('aria-expanded', 'true');
      if (opts.root) opts.root.classList.add('has-av-open');
      /* From the keyboard to the track that plays; from a press, to the chooser itself. */
      var first = box.querySelector('[role="radio"][aria-checked="true"]');
      (byKey && first ? first : box).focus({ preventScroll: true });
    }

    function leave(el) {
      var gone = false;
      var end = function (e) {
        if (gone || (e && e.target !== el)) return;
        gone = true;
        el.remove();
      };
      el.classList.add('is-leaving');
      el.addEventListener('animationend', end);
      setTimeout(end, ms(LEAVE_MS));
    }

    function close(refocus) {
      if (!box) return;
      var b = box;
      var v = veil;
      var t = from;
      box = veil = from = null;
      t.setAttribute('aria-expanded', 'false');
      leave(b);
      leave(v);
      if (opts.root) opts.root.classList.remove('has-av-open');
      if (opts.closed) opts.closed();
      if (refocus) t.focus({ preventScroll: true });
    }

    function choose(row) {
      if (busy) return;
      if (row.getAttribute('aria-checked') === 'true') { close(true); return; }
      row.parentNode.querySelectorAll('[role="radio"]').forEach(function (r) {
        r.setAttribute('aria-checked', String(r === row));
        r.setAttribute('tabindex', r === row ? '0' : '-1');
      });
      row.classList.add('is-picked');
      var change = row.hasAttribute('data-audio') ? { audio: row.getAttribute('data-audio') } : { subtitle: row.getAttribute('data-subtitle') };
      then(change);
    }

    function then(change) {
      busy = true;
      setTimeout(function () {
        close(true);
        setTimeout(function () {
          busy = false;
          opts.pick(change);
        }, ms(CLOSED_MS));
      }, ms(PICKED_MS));
    }

    function onClick(e) {
      var t = e.target;
      if (t.closest('[data-av-close]')) { close(true); return; }
      if (t.closest('[data-av-reset]')) { if (!busy) then({ reset: true }); return; }
      var r = t.closest('.player-av-row');
      if (r) choose(r);
    }

    document.addEventListener('click', function (e) {
      if (!box || !e.target.closest) return;
      if (box.contains(e.target) || (veil && veil.contains(e.target)) || triggers.some(function (b) { return b.contains(e.target); })) return;
      close(false);
    });

    /* Keys while it is open; true where the key was its own. */
    function key(e) {
      if (!box) return false;
      if (e.key === 'Escape') {
        e.preventDefault();
        close(true);
        return true;
      }
      var r = e.target.closest && e.target.closest('.player-av-row');
      if (r && /^(ArrowDown|ArrowUp|Home|End)$/.test(e.key)) {
        e.preventDefault();
        var rows = Array.prototype.slice.call(r.parentNode.querySelectorAll('.player-av-row'));
        var at = rows.indexOf(r);
        var to = e.key === 'Home' ? 0 : e.key === 'End' ? rows.length - 1 : (at + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
        rows.forEach(function (x) { x.setAttribute('tabindex', '-1'); });
        rows[to].setAttribute('tabindex', '0');
        rows[to].focus();
        return true;
      }
      if (e.key === 'Tab') {
        /* Within the chooser: the close, the track that plays in each group, the link. */
        var stops = Array.prototype.slice.call(box.querySelectorAll('[data-av-close], .player-av-row[tabindex="0"], .player-av-link'))
          .filter(function (x) { return !x.getClientRects || x.getClientRects().length; });
        if (!stops.length) return false;
        e.preventDefault();
        var i = stops.indexOf(document.activeElement);
        var to = i < 0 ? (e.shiftKey ? stops.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + stops.length) % stops.length;
        stops[to].focus();
        return true;
      }
      /* The arrows seek, Space plays, and the rest is the page's, except within the chooser. */
      return box.contains(e.target);
    }

    return {
      set: function (next) {
        /* Told again of what it shows already, as a remote is on each poll, it is left as it is. */
        var said = JSON.stringify(next);
        if (said === seen) return;
        seen = said;
        state = next;
        showState();
        if (box) {
          /* What it offers changed under it: drawn again in place, the focus still in it. */
          var had = box.contains(document.activeElement);
          var old = box;
          box = build();
          box.classList.add('is-redrawn');
          old.parentNode.insertBefore(box, old);
          old.remove();
          box.querySelectorAll('.player-av-list, .player-av-cols').forEach(fadeEnds);
          if (had) (box.querySelector('[role="radio"][aria-checked="true"]') || box).focus({ preventScroll: true });
        }
      },
      state: function () { return state; },
      changing: function (on) {
        triggers.forEach(function (b) { b.classList.toggle('is-changing', on); });
      },
      isOpen: function () { return !!box; },
      close: close,
      key: key,
    };
  }

  MM.tracks = { create: create, said: said, describe: describe };
})(window);
