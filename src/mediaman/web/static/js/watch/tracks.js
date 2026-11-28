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
 * Where the state names the title's `key` and watch/languages.js is there, a row under each column
 * leads on to another language: views pushed inside the same glass, which slide (or, with less
 * motion, only fade) and grow it by a clip of its edge. Escape goes back a view, then closes.
 * openLanguages(trigger) opens it from a page instead, over that page's `host`, at its first view.
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
  var FADE_MS = 150;         // the control's words, from what played to what plays
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
        short: 'Signs & songs', said: 'signs & songs only' };
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
    text = text.charAt(0).toUpperCase() + text.slice(1);
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
    var stack = [];            // the views in the chooser, the one shown last
    var busy = false;
    var held = false;          // a change is under way: the control goes on saying what played
    var seen;
    var fresh = null;          // subtitles just added: {id, language}, lit in the menu
    var byKeys = false;

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
      if (!held) showLabel();
    }

    function spokenOf(now) {
      return 'Audio and subtitles: ' + (now.audio ? now.audio.said : 'audio') + ', ' + (now.subtitle === OFF ? 'subtitles off' : now.subtitle.said);
    }

    function showLabel() {
      var now = describe(state);
      var audio = now.audio ? now.audio.short : 'Audio';
      var more = now.subtitle === OFF ? '' : ' · ' + (now.subtitle.short === 'Signs & songs' ? 'signs & songs' : now.subtitle.short + ' subtitles');
      var spoken = spokenOf(now);
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

    function column(group, title, rows, none, extra) {
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
      (extra || []).forEach(function (x) { col.appendChild(x); });
      return col;
    }

    function foot() {
      var f = document.createElement('div');
      f.className = 'player-av-foot';
      var words = span('player-av-foot-text');
      var link = null;
      if (fresh && !state.on) {
        f.appendChild(MM.dom.icon('i-circle-check', 'lp-ok'));
        words.appendChild(document.createTextNode(langWords(fresh.language) + ' subtitles added from OpenSubtitles, for everyone who watches '));
        words.appendChild(document.createElement('b')).textContent = state.title;
        words.appendChild(document.createTextNode('.'));
      } else if (state.on) {
        f.appendChild(MM.dom.icon(TV_ICON, 'ico--tv'));
        words.appendChild(document.createTextNode('Changes play on '));
        words.appendChild(document.createElement('b')).textContent = state.on;
        words.appendChild(document.createTextNode('. The picture restarts where it is; it takes a few seconds.'));
      } else if (state.remembered) {
        f.appendChild(MM.dom.icon('i-check'));
        words.appendChild(document.createTextNode(state.series ? 'Kept for every episode of ' : 'Kept for '));
        words.appendChild(document.createElement('b')).textContent = state.title;
        words.appendChild(document.createTextNode('.'));
      } else {
        f.appendChild(MM.dom.icon('i-circle-info'));
        words.textContent = defaultsLine(state.defaults);
        /* In a tab of its own, so that what plays here goes on playing. */
        link = document.createElement('a');
        link.href = DEFAULTS_HREF;
        link.target = '_blank';
        link.rel = 'noopener';
        link.setAttribute('aria-label', 'Change your defaults, in a new tab');
        link.textContent = 'Change';
      }
      if (state.remembered && !fresh) {
        link = document.createElement('button');
        link.type = 'button';
        link.setAttribute('data-av-reset', '');
        link.textContent = 'Use my defaults';
      }
      f.appendChild(words);
      if (link) {
        link.className = 'player-av-link';
        f.appendChild(link);
      }
      return f;
    }

    /* Another language, where the title can be asked about: watch/languages.js. */
    var langs = null;
    function languages() {
      if (!state || !state.key || !MM.languages) return null;
      if (!langs || langs.key !== state.key) langs = MM.languages.create(context());
      return langs;
    }

    function context() {
      return {
        key: state.key,
        state: function () { return state; },
        push: push,
        back: back,
        home: function () { fresh = null; home(menuView()); },
        /* On from where it is, with only the menu to go back to. */
        ahead: function (v) {
          if (!box) return;
          stack.slice(1).forEach(function (x) { if (x.gone) x.gone(); });
          stack = [menuView(), v];
          show(v, 'fwd');
          focusIn(v);
        },
        close: close,
        settle: settle,
        busy: function (on) { if (on === undefined) return busy; busy = on; return busy; },
        byKeys: function () { return byKeys; },
        isOpen: function () { return !!box; },
        menuChanged: function () { if (box && stack.length === 1 && stack[0].menu) redrawMenu(); },
        /* A track already on the title: it only switches, as a pick in the menu does. */
        switchTo: function (change) { then(change); },
        /* Subtitles just added: they play, and the menu shows them lit. */
        added: function (track) {
          fresh = { id: track.id, language: track.language, track: track };
          state = withFresh(state);
          seen = null;
          home(menuView());
          if (opts.pick) opts.pick({ subtitle: track.id, added: true });
        },
      };
    }

    /* Until the host's word on the title has the subtitles just added, they are drawn as on. */
    function withFresh(s) {
      if (!s || find(s.subtitle_tracks, fresh.id)) return s;
      return Object.assign({}, s, { subtitle_tracks: (s.subtitle_tracks || []).concat([fresh.track]), subtitle: fresh.id });
    }

    function menuView() {
      var v = document.createElement('div');
      v.className = 'lp-menu';
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
      v.appendChild(head);
      var titles = document.createElement('div');
      titles.className = 'player-av-titles';
      titles.setAttribute('aria-hidden', 'true');
      ['Audio', 'Subtitles'].forEach(function (t) {
        var p = document.createElement('p');
        p.className = 'player-menu-title';
        p.textContent = t;
        titles.appendChild(p);
      });
      v.appendChild(titles);
      var cols = document.createElement('div');
      cols.className = 'player-av-cols';
      cols.addEventListener('scroll', function () { fadeEnds(cols); });

      var more = languages();
      var audio = state.audio_tracks || [];
      var playing = find(audio, state.audio) || audio[0];
      var mine = (state.defaults || {}).audio_lang;
      var tagged = mine && audio.filter(function (t) { return t.language === mine && !t.commentary; })[0];
      cols.appendChild(column('audio', 'Audio', audio.map(function (t) {
        return row('audio', t.id, audioText(t), t === playing, t === tagged ? 'Your default' : '');
      }), audio.length === 1 ? 'This title has one audio track.' : '', more ? more.audioRows() : []));

      var subs = state.subtitle_tracks || [];
      var shown = state.subtitle && find(subs, state.subtitle) ? state.subtitle : 'off';
      var rows = [row('subtitle', 'off', OFF, shown === 'off')].concat(subs.map(function (t) {
        var lit = fresh && fresh.id === t.id;
        var r = row('subtitle', t.id, subtitleText(t), t.id === shown, lit ? 'Just added' : '');
        if (lit) r.classList.add('lp-fresh');
        return r;
      }));
      cols.appendChild(column('subtitle', 'Subtitles', rows, subs.length ? '' : 'This title has no subtitles.', more ? more.subtitleRows() : []));
      v.appendChild(cols);
      v.appendChild(foot());
      return {
        el: v, menu: true, label: 'Audio and subtitles', remake: menuView,
        after: function () { MM.dom.qa('.player-av-list, .player-av-cols', v).forEach(fadeEnds); },
      };
    }

    function redrawMenu() {
      var old = stack[0].el;
      var had = box.contains(document.activeElement);
      var v = menuView();
      v.el.classList.add('lp-view');
      stack[0] = v;
      old.parentNode.insertBefore(v.el, old);
      old.remove();
      v.after();
      if (had) (v.el.querySelector('[role="radio"][aria-checked="true"]') || box).focus({ preventScroll: true });
    }

    function open(trigger, byKey, first) {
      if (box || !state) return;
      if (opts.opening) opts.opening();
      from = trigger;
      byKeys = !!byKey;
      veil = document.createElement('div');
      veil.className = 'player-av-veil';
      veil.setAttribute('data-av-close', '');
      veil.addEventListener('click', function () { close(true); });
      box = document.createElement('div');
      box.className = 'player-av';
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-label', 'Audio and subtitles');
      box.tabIndex = -1;
      box.addEventListener('click', onClick);
      /* From a page, over all of it; from the player, up from its control. */
      var host = first ? opts.host || document.body : trigger.parentNode;
      if (first) {
        box.classList.add('lp-dialog');
        veil.classList.add('lp-dialog-veil');
        /* No player hands it the keys here: it takes its own, before the page under it. */
        box.addEventListener('keydown', function (e) { if (key(e)) e.stopPropagation(); });
      }
      host.appendChild(veil);
      host.appendChild(box);
      var v = first ? first(context()) : menuView();
      stack = [v];
      show(v, 'none');
      trigger.setAttribute('aria-expanded', 'true');
      if (opts.root) opts.root.classList.add('has-av-open');
      var more = languages();
      if (more) more.opened();
      /* From the keyboard to the track that plays; from a press, to the chooser itself. */
      var to = box.querySelector('[data-first]') || box.querySelector('[role="radio"][aria-checked="true"]');
      (byKey && to ? to : box).focus({ preventScroll: true });
    }

    function leave(el) {
      var gone = false;
      var end = function (e) {
        if (gone || (e && e.target !== el)) return;
        gone = true;
        el.remove();
      };
      el.classList.add('is-leaving');
      /* Out of reach at once, though it is still seen going. */
      el.inert = true;
      el.addEventListener('animationend', end);
      setTimeout(end, ms(LEAVE_MS));
    }

    function close(refocus) {
      if (!box) return;
      var b = box;
      var v = veil;
      var t = from;
      box = veil = from = null;
      stack = [];
      fresh = null;
      if (langs) langs.closed();
      t.setAttribute('aria-expanded', 'false');
      leave(b);
      leave(v);
      if (opts.root) opts.root.classList.remove('has-av-open');
      if (opts.closed) opts.closed();
      if (refocus && t.isConnected !== false) t.focus({ preventScroll: true });
    }

    function phone() { return !!(global.matchMedia && global.matchMedia('(max-width: 699.98px)').matches); }
    function moving() { return !(reduce && reduce.matches) && typeof box.animate === 'function'; }
    function edge() { return box.classList.contains('lp-dialog') && !phone(); }

    /* A view in, the last one out: forward slides left, back slides right. The glass grows or
       shrinks to the new view by a clip of its edge, never by a change of its size mid-flight;
       with less motion the new view only fades in. */
    function show(view, dir) {
      var old = stack.length > 1 || dir !== 'none' ? box.querySelector('.lp-view:not(.is-out)') : null;
      var top = edge();
      var h0 = box.offsetHeight;
      var oldTop = old ? old.offsetTop : 0;
      box.classList.toggle('lp-deep', !!view.deep);
      box.classList.toggle('lp-sub', stack.length > 1 || !!view.sub);
      box.setAttribute('aria-label', view.label || 'Audio and subtitles');
      /* A view shown again, as one gone back to is, comes back as it was before it went. */
      if (view.el.getAnimations) view.el.getAnimations().forEach(function (a) { a.cancel(); });
      view.el.classList.remove('is-out');
      view.el.inert = false;
      if (view.el.style) view.el.style.top = '';
      view.el.classList.add('lp-view');
      if (!old || dir === 'none' || !moving()) {
        if (old) old.remove();
        box.appendChild(view.el);
        if (old && typeof view.el.animate === 'function') view.el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 150, easing: 'ease-out' });
        if (view.after) view.after();
        return;
      }
      old.classList.add('is-out');
      old.inert = true;
      box.appendChild(view.el);
      var h1 = box.offsetHeight;
      var dx = dir === 'back' ? -1 : 1;
      var r = global.getComputedStyle(box).borderTopLeftRadius;
      var d = 320;
      var ease = 'cubic-bezier(.32,.72,0,1)';
      old.style.top = (top ? oldTop : oldTop + (h1 - h0)) + 'px';
      if (h1 >= h0) {
        var grow = h1 - h0;
        box.animate([{ clipPath: top ? 'inset(0 0 ' + grow + 'px 0 round ' + r + ')' : 'inset(' + grow + 'px 0 0 0 round ' + r + ')' },
          { clipPath: 'inset(0 0 0 0 round ' + r + ')' }], { duration: d, easing: ease });
      } else {
        var shrink = h0 - h1;
        var b = box;
        b.style.height = h0 + 'px';
        b.classList.add(top ? 'lp-pin-top' : 'lp-pin-foot');
        old.style.top = oldTop + 'px';
        b.animate([{ clipPath: 'inset(0 0 0 0 round ' + r + ')' },
          { clipPath: top ? 'inset(0 0 ' + shrink + 'px 0 round ' + r + ')' : 'inset(' + shrink + 'px 0 0 0 round ' + r + ')' }],
          { duration: d, easing: ease }).onfinish = function () { b.style.height = ''; b.classList.remove('lp-pin-top', 'lp-pin-foot'); };
      }
      old.animate([{ transform: 'none', opacity: 1 }, { transform: 'translateX(' + (-20 * dx) + 'px)', opacity: 0, offset: 0.6 }, { transform: 'translateX(' + (-28 * dx) + 'px)', opacity: 0 }],
        { duration: 220, easing: 'cubic-bezier(.4,0,1,1)', fill: 'forwards' }).onfinish = function () { old.remove(); };
      view.el.animate([{ transform: 'translateX(' + (28 * dx) + 'px)', opacity: 0 }, { transform: 'none', opacity: 1 }],
        { duration: d, easing: ease, delay: 70, fill: 'backwards' });
      if (view.after) view.after();
    }

    /* What changes inside a view, such as a search's answer, grows the glass by the same clip. */
    function settle(change) {
      if (!box || !moving()) { change(); return; }
      var h0 = box.offsetHeight;
      change();
      if (!box) return;
      var h1 = box.offsetHeight;
      if (h1 > h0 + 1) {
        var r = global.getComputedStyle(box).borderTopLeftRadius;
        var g = h1 - h0;
        box.animate([{ clipPath: edge() ? 'inset(0 0 ' + g + 'px 0 round ' + r + ')' : 'inset(' + g + 'px 0 0 0 round ' + r + ')' },
          { clipPath: 'inset(0 0 0 0 round ' + r + ')' }], { duration: 300, easing: 'cubic-bezier(.32,.72,0,1)' });
      }
    }

    function push(v) {
      if (!box) return;
      stack.push(v);
      show(v, 'fwd');
      focusIn(v);
    }
    function back() {
      if (!box) return;
      if (stack.length < 2) { close(true); return; }
      if (stack[stack.length - 1].gone) stack[stack.length - 1].gone();
      stack.pop();
      var prev = stack[stack.length - 1];
      var v = prev.remake ? prev.remake() : prev;
      stack[stack.length - 1] = v;
      show(v, 'back');
      focusIn(v);
    }
    function home(v) {
      if (!box) return;
      stack.slice(1).forEach(function (x) { if (x.gone) x.gone(); });
      stack = [v];
      show(v, 'back');
      focusIn(v);
    }
    /* On a laptop a search takes the keyboard; elsewhere the focus goes in only from the keys. */
    function focusIn(v) {
      var f = v.el.querySelector('[data-first]');
      if (!byKeys && !(f && f.tagName === 'INPUT' && !phone())) f = null;
      setTimeout(function () {
        if (!box) return;
        if (f && box.contains(f)) f.focus({ preventScroll: true });
        else box.focus({ preventScroll: true });
      }, 0);
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
      var r = t.closest('.player-av-row[role="radio"]');
      if (r) choose(r);
    }

    document.addEventListener('click', function (e) {
      if (!box || !e.target.closest) return;
      /* A press that changed the view took its own target away. */
      if (e.target.isConnected === false) return;
      if (box.contains(e.target) || (veil && veil.contains(e.target)) || (from && from.contains(e.target)) || triggers.some(function (b) { return b.contains(e.target); })) return;
      close(false);
    });

    var STOPS = '[data-av-close], .player-av-row[tabindex="0"], .player-av-link, .lp-more, .lp-pending, .lp-back, .lp-x, .lp-input, .lp-clear, .lp-seg-btn[tabindex="0"], .lp-opt[tabindex="0"], .lp-ver[tabindex="0"], .lp-acts > .player-pill, .lp-fallback > .player-pill';

    /* Keys while it is open; true where the key was its own. */
    function key(e) {
      if (!box) return false;
      if (e.key === 'Escape') {
        e.preventDefault();
        back();
        return true;
      }
      var r = e.target.closest && e.target.closest('.player-av-row[role="radio"]');
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
        /* Within the view shown: its close, the track that plays in each group, its ways on. */
        var view = stack.length ? stack[stack.length - 1].el : box;
        var stops = Array.prototype.slice.call(view.querySelectorAll(STOPS))
          .filter(function (x) { return !x.disabled && !x.hidden && (!x.getClientRects || x.getClientRects().length); });
        if (!stops.length) return false;
        e.preventDefault();
        var i = stops.indexOf(document.activeElement);
        var next = i < 0 ? (e.shiftKey ? stops.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + stops.length) % stops.length;
        stops[next].focus();
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
        state = fresh ? withFresh(next) : next;
        showState();
        /* What it offers changed under it: the menu is drawn again in place, the focus still in it. */
        if (box && stack.length && stack[0].menu) {
          if (stack.length === 1) redrawMenu();
          else stack[0] = menuView();
        }
      },
      state: function () { return state; },
      /* While it changes the control says what played; then it fades over to what plays. */
      changing: function (on) {
        if (on === held) return;
        held = on;
        if (on || !state || !triggers.length || triggers[0].getAttribute('aria-label') === spokenOf(describe(state))) return;
        triggers.forEach(function (b) { b.classList.add('is-changing'); });
        setTimeout(function () {
          if (held || !state) return;
          showLabel();
          triggers.forEach(function (b) { b.classList.remove('is-changing'); });
        }, ms(FADE_MS));
      },
      isOpen: function () { return !!box; },
      /* From a title's page: Add a language, which kind first, over the page. */
      openLanguages: function (trigger, byKey) {
        if (box || !languages()) return;
        open(trigger, byKey, function () { return languages().kindView(); });
      },
      close: close,
      key: key,
    };
  }

  MM.tracks = { create: create, said: said, describe: describe, nameIn: nameIn, langWords: langWords };
})(window);
