/* A person's rating of a title: the set of four thumbs, the same on every screen.
 *
 *   MM.ratings.mount(ref, { variant })   'plain' (default), 'grid' or 'lift'
 *     → { node, line, press(level), destroy() }. node is the set, line the words about it: inside
 *     node for plain and grid, for the caller to place for lift. Both are hidden while the
 *     title cannot be rated. 'lift' stands on end, best at the top, is called by the option
 *     `label` and seen while its node has .is-open; it has also pointAt(button or null), what
 *     the person is on, and enter(), which takes the focus in.
 *     `again`: in a grid, the question may be put again, so the words do not say it is the last.
 *   MM.ratings.get(ref)          -2, -1, 1, 2, null for none, undefined while never known
 *   MM.ratings.canRate(ref)      false, true, or undefined while not known
 *   MM.ratings.read(refs)        asks for those not known, in one request
 *   MM.ratings.know(ref, level, others)   what a page was drawn with: ref is tmdb-movie-<id> or
 *     tmdb-tv-<id>, others more refs of the same title. Where another level is known, the
 *     server is asked which is the newer.
 *   MM.ratings.set(ref, level)   rates, or with null takes the rating back; undefined does nothing
 *   MM.ratings.thumb(level, on)  a new thumb as an svg
 *   MM.ratings.LEVELS            [{ level, name, said }]
 *
 * Told to the document as `mm:rating`, with detail { phase, ref, refs, level, was, from,
 * mediaType, tmdbId }: phase is 'read', 'pending', 'saved' or 'failed' (level is then back
 * to was, what was last saved), was the level before, undefined while not known (there is
 * then nothing to undo to), refs every ref known to name the title, from the set's node.
 *
 * Requires: core/api.js.
 */
(function (global) {
  'use strict';

  var MM = global.MM;
  var URL = '/api/watch/ratings';
  var MAX_REFS = 100; // the server's limit for one read
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var LEVELS = [
    { level: -2, name: 'Strong no', said: 'Gone from your picks.' },
    { level: -1, name: 'Not for me', said: 'Fewer like this.' },
    { level: 1, name: 'I like it', said: 'More like this.' },
    { level: 2, name: 'I love it', said: 'Counts most in your picks.' },
  ];
  var IDLE = {
    plain: 'Only you see this.',
    grid: 'Only you see this. You won’t be asked again.',
    lift: 'Only you see your rating.',
  };
  var NOT_SAVED = 'Your rating wasn’t saved. Try again.';
  var HAND = 'M3 11.6c0-.8.6-1.4 1.4-1.4s1.4.6 1.4 1.4v7.2c0 .8-.6 1.4-1.4 1.4S3 19.6 3 18.8Z' +
    'M8.4 11.2l3.2-6.8C12 3.5 12.6 3 13.4 3c1.3 0 2.2 1 2.2 2.4 0 1.4-.5 2.7-.9 3.8h3.9c1.6 0 2.7 1.4 2.4 2.9' +
    'l-1.1 5.6c-.3 1.5-1.5 2.5-3 2.5h-5.7c-1 0-1.9-.3-2.8-.8Z';

  function el(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text) n.textContent = text;
    return n;
  }

  function svg(tag, attrs, children) {
    var n = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    (children || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }

  /* Its outline, and the fill drawn into it while it is on. */
  function hand() { return [svg('path', { d: HAND }), svg('path', { 'class': 'ink', d: HAND })]; }

  function thumb(level, on) {
    var two = Math.abs(level) === 2;
    return svg('svg', {
      'class': 'rate-th' + (two ? ' rate-th--2' : '') + (level < 0 ? ' rate-th--down' : '') + (on ? ' is-on' : ''),
      viewBox: two ? '0 0 32 24' : '-1 0 26 24',
      'aria-hidden': 'true',
      focusable: 'false',
    }, two ? [
      svg('g', { transform: 'translate(10.5 -2.8) scale(.92)' }, [svg('g', { 'class': 'b' }, hand())]),
      svg('g', { transform: 'translate(0 2)' }, [
        svg('g', { 'class': 'f' }, [svg('g', { 'class': 'halo' }, [svg('path', { d: HAND })])].concat(hand())),
      ]),
    ] : [svg('g', { 'class': 'f' }, hand())]);
  }

  /* ── The levels ──
     A title has one state however many refs name it: `want` is shown, `saved` is what
     the server holds, as far as is known. One request of a title is out at a time.
     `before` is the level before the first press not settled yet, undefined while not
     known; `sure` is whether the server said it, `tried` what it may have stored unheard. */

  var states = {}; // by ref
  var byKey = {}; // by title, more than one only while a change of it is out
  var sets = [];

  function stateOf(ref) {
    var s = states[ref];
    if (s) return s;
    var m = /^tmdb-(movie|tv)-0*([1-9][0-9]*)$/.exec(ref);
    var key = m ? m[1] + '-' + m[2] : '';
    s = (key && withKey(key)) || {
      key: '', refs: [], want: null, saved: null, rateable: undefined, known: false, askAgain: false,
      busy: false, asking: false, lost: false, rev: 0,
      before: undefined, sure: false, tried: [], pressed: null, from: null,
    };
    if (key) setKey(s, key);
    s.refs.push(ref);
    states[ref] = s;
    return s;
  }

  function withKey(key, except) {
    return (byKey[key] || []).filter(function (s) { return s !== except; })[0];
  }

  function leaveKey(s) {
    if (s.key) byKey[s.key].splice(byKey[s.key].indexOf(s), 1);
  }

  function setKey(s, key) {
    if (s.key === key) return;
    leaveKey(s);
    s.key = key;
    (byKey[key] = byKey[key] || []).push(s);
  }

  function setsOf(s) {
    return sets.filter(function (set) { return stateOf(set.ref) === s; });
  }

  /* Only the sets that were there for the change say how it failed. */
  function setError(s, words) {
    setsOf(s).forEach(function (set) { set.error = words; });
  }

  function tell(s, phase, was, ref) {
    var title = /^(movie|tv)-(\d+)$/.exec(s.key);
    setsOf(s).forEach(function (set) { set.paint(); });
    document.dispatchEvent(new CustomEvent('mm:rating', {
      detail: {
        phase: phase, ref: ref || s.refs[0], refs: s.refs.slice(), level: s.want, was: was, from: s.from,
        mediaType: title ? title[1] : null, tmdbId: title ? Number(title[2]) : null,
      },
    }));
  }

  /* The refs of *other* now name *s*. */
  function join(s, other) {
    leaveKey(other);
    other.refs.forEach(function (ref) { states[ref] = s; });
    s.refs = s.refs.concat(other.refs);
  }

  function know(ref, level, others) {
    var s = stateOf(ref);
    if (!s.known && !s.busy) {
      s.want = s.saved = level;
      s.known = s.rateable = true;
    } else if (!s.busy && s.saved !== level) {
      // Drawn from a newer answer, or from an older one.
      doubt(s);
      read([ref]);
    }
    others.forEach(function (other) {
      var o = states[other];
      if (o === s) return;
      if (!o) {
        states[other] = s;
        s.refs.push(other);
      } else if (!o.busy && !o.key) {
        join(s, o);
        setsOf(s).forEach(function (set) { set.paint(); });
      }
    });
  }

  function doubt(s) {
    s.askAgain = true;
    s.rev += 1; // an answer on its way may be from before
  }

  /* Only a save says which title a ref names, so any other ref may name the changed one:
     those on show are asked about again, the rest when they next are. */
  function doubtOthers(s) {
    Object.keys(states).forEach(function (ref) {
      if (states[ref] !== s && !states[ref].key) doubt(states[ref]);
    });
    read(sets.map(function (set) { return set.ref; }));
  }

  function change(ref, level, from) {
    var s = stateOf(ref);
    if (level === s.want || level === undefined) return;
    if (!s.busy && !s.tried.length) {
      s.before = s.known && !s.askAgain ? s.saved : undefined;
      s.sure = false;
    }
    s.want = level;
    setError(s, '');
    s.pressed = ref;
    s.from = from || null;
    tell(s, 'pending', s.before, ref);
    if (!s.busy) send(s);
  }

  function send(s) {
    var level = s.want;
    var ref = s.pressed;
    var url = URL + '/' + encodeURIComponent(ref);
    s.busy = true;
    (level === null ? MM.api.delete(url) : MM.api.put(url, { level: level })).then(function (data) {
      // A level it stored unheard is not the one before.
      if (!s.sure && data.was !== undefined && s.tried.indexOf(data.was) < 0) s.before = data.was;
      s.sure = true;
      s.tried = [];
      s.saved = level;
      s.known = true;
      s.askAgain = false;
      s.rateable = true;
      s.rev += 1;
      s.lost = false;
      setKey(s, data.media_type + '-' + data.tmdb_id);
      var other = withKey(s.key, s);
      // One whose own change is out joins when that has settled.
      if (other && !other.busy) join(s, other);
      if (s.want !== level) { send(s); return; }
      s.busy = false;
      setError(s, '');
      tell(s, 'saved', s.before, ref);
      doubtOthers(s);
    }, function (err) {
      // Without the server's word, it may have saved it.
      if (!(err.status >= 400 && err.status < 500)) {
        s.lost = true;
        s.tried.push(level);
      }
      if (s.want !== level) { send(s); return; }
      failed(s, err, ref);
    });
  }

  function failed(s, err, ref) {
    var other = s.key && withKey(s.key, s);
    var again = s.lost ? [ref] : [];
    s.busy = false;
    if (s.sure) s.before = s.saved;
    s.sure = false;
    if (s.lost) doubt(s);
    s.lost = false;
    if (other && other.busy) {
      doubt(s);
      doubt(other);
      again = s.refs.slice(0, 1).concat(other.refs.slice(0, 1));
    } else if (other) {
      s.saved = other.saved;
      s.rateable = other.rateable;
      s.rev += 1; // an answer on its way may be from before
      join(s, other);
    }
    s.want = s.saved;
    setError(s, err.error === 'no_tmdb_id' ? MM.api.messageOf(err, NOT_SAVED) : NOT_SAVED);
    tell(s, 'failed', s.known ? s.saved : undefined, ref);
    read(again);
  }

  /* ── Reading ── */

  var toAsk = [];

  function read(refs) {
    refs.forEach(function (ref) {
      var s = stateOf(ref);
      if ((s.known && !s.askAgain) || s.asking) return;
      s.asking = true;
      if (!toAsk.length) setTimeout(flush, 0);
      toAsk.push(ref);
    });
  }

  function flush() {
    var refs = toAsk;
    toAsk = [];
    for (var i = 0; i < refs.length; i += MAX_REFS) ask(refs.slice(i, i + MAX_REFS));
  }

  function ask(refs) {
    var asked = refs.map(function (ref) { return { ref: ref, s: stateOf(ref), rev: stateOf(ref).rev }; });
    MM.api.get(URL + '?refs=' + encodeURIComponent(refs.join(','))).then(function (data) {
      asked.forEach(function (a) {
        var s = a.s;
        s.asking = false;
        if (states[a.ref] !== s) return;
        if (s.rev !== a.rev) { read([a.ref]); return; }
        var got = data.ratings[a.ref];
        var was = s.known ? s.want : undefined;
        s.known = true;
        s.askAgain = false;
        s.rateable = !!got && got.rateable;
        s.saved = got ? got.level : null;
        if (s.busy) return;
        var shown = s.want;
        s.want = s.saved;
        s.tried = [];
        s.from = null;
        if (s.want !== shown) setError(s, ''); // saved after all, or changed elsewhere
        tell(s, 'read', was, a.ref);
      });
    }, function () {
      asked.forEach(function (a) { a.s.asking = false; });
    });
  }

  // A page kept by the browser comes back with the levels it left with.
  global.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    Object.keys(states).forEach(function (ref) { doubt(states[ref]); });
    read(sets.map(function (set) { return set.ref; }));
  });

  /* ── The set ── */

  function levelOf(level) {
    return LEVELS.filter(function (l) { return l.level === level; })[0];
  }

  function mount(ref, opts) {
    var variant = (opts && opts.variant) || 'plain';
    var inLift = variant === 'lift';
    var stateAttr = inLift ? 'aria-checked' : 'aria-pressed';
    var node = el('div', 'rate rate--' + variant);
    var group = el('div', 'rate__set');
    group.setAttribute('role', inLift ? 'radiogroup' : 'group');
    group.setAttribute('aria-label', (opts && opts.label) || 'Your rating');
    node.appendChild(group);
    var line = el('p', 'rate__line');
    line.setAttribute('role', 'status');
    // What is read or pressed elsewhere is not announced here.
    line.setAttribute('aria-live', 'off');
    if (variant === 'plain' || variant === 'grid') node.appendChild(line);

    var touched = false;
    var lineKey = null;
    var at = -1; // the thumb shown as on

    var buttons = LEVELS.map(function (l) {
      var b = el('button', 'rate__b ' + (l.level > 0 ? 'is-up' : 'is-down') + (Math.abs(l.level) === 2 ? ' is-double' : ''));
      b.type = 'button';
      b.setAttribute('aria-label', l.name);
      if (inLift) {
        b.setAttribute('role', 'radio');
        b.setAttribute('tabindex', '-1');
      }
      b.appendChild(el('span', 'rate__hl'));
      b.appendChild(thumb(l.level));
      if (!inLift) {
        var name = el('span', variant === 'grid' ? 'rate__lab' : 'rate__tip', l.name);
        name.setAttribute('aria-hidden', 'true');
        b.appendChild(name);
      }
      b.addEventListener('click', function () { press(l.level); });
      group.insertBefore(b, inLift ? group.firstChild : null);
      return b;
    });
    var lift = inLift ? lifted(node, group) : null;

    function press(level) {
      var s = stateOf(ref);
      var b = buttons[LEVELS.indexOf(levelOf(level))];
      var next = s.want === level ? null : level;
      touched = true;
      line.setAttribute('aria-live', 'polite');
      buttons.forEach(function (o) { o.classList.remove('pop', 'back'); });
      change(ref, next, node);
      void b.offsetWidth; // so that the same thumb moves again
      b.classList.add(next === null ? 'back' : 'pop');
      if (lift) lift.say(b, next === null ? 'Rating removed' : levelOf(level).name);
    }

    /* The highlight slides from the thumb it was on; from none, or to none, it comes or goes
       where it is. */
    function moveTo(now) {
      if (now === at) return;
      if (at < 0 || now < 0) node.removeAttribute('data-from');
      else node.setAttribute('data-from', String(at));
      at = now;
    }

    function sayLine(s) {
      var l = levelOf(s.want);
      var bold = '';
      var text = opts && opts.again ? IDLE.plain : IDLE[variant];
      if (set.error) text = set.error;
      else if (inLift && !touched) text = IDLE.lift;
      else if (l) { bold = l.name + '.'; text = ' ' + l.said; }
      else if (inLift) text = 'Rating removed.';
      line.classList.toggle('is-bad', !!set.error);
      if (lineKey === bold + text) return;
      var first = lineKey === null;
      lineKey = bold + text;
      var words = el('span', first ? '' : 'is-new');
      if (bold) words.appendChild(el('b', '', bold));
      words.appendChild(document.createTextNode(text));
      line.textContent = '';
      line.appendChild(words);
    }

    function paint() {
      var s = stateOf(ref);
      node.hidden = line.hidden = s.rateable === false;
      node.classList.toggle('has', s.want !== null);
      moveTo(LEVELS.indexOf(levelOf(s.want)));
      buttons.forEach(function (b, i) {
        var on = LEVELS[i].level === s.want;
        b.classList.toggle('is-on', on);
        b.setAttribute(stateAttr, String(on));
      });
      if (lift) lift.paint();
      sayLine(s);
    }

    var set = { ref: ref, paint: paint, error: '' };
    sets.push(set);
    paint();
    read([ref]);

    return {
      node: node,
      line: line,
      press: press,
      pointAt: lift && lift.pointAt,
      enter: lift && lift.enter,
      destroy: function () { sets = sets.filter(function (o) { return o !== set; }); },
    };
  }

  /* The set on end: a place that goes along with the mouse, the finger or the focus, and the name
     of the level it is on. */
  function lifted(node, group) {
    var buttons = Array.prototype.slice.call(group.children); // best first
    var puck = group.insertBefore(el('span', 'rate__puck'), group.firstChild);
    var name = node.appendChild(el('span', 'rate__name'));
    var words = name.appendChild(el('span'));
    var said = null; // the button pressed last, which keeps its own words
    name.setAttribute('aria-hidden', 'true');

    function isOn(b) { return b.getAttribute('aria-checked') === 'true'; }

    function place(b) {
      var i = buttons.indexOf(b);
      node.classList.toggle('has-at', i >= 0);
      if (i >= 0) node.style.setProperty('--at', String(i));
      ['is-up', 'is-down', 'is-double'].forEach(function (c) { puck.classList.toggle(c, i >= 0 && isOn(b) && b.classList.contains(c)); });
    }

    function pointAt(b) {
      if (b !== said) said = null;
      buttons.forEach(function (o) { o.classList.toggle('is-at', o === b); });
      place(b || buttons.filter(isOn)[0]);
      name.classList.toggle('is-on', !!b);
      if (b && b !== said) words.textContent = isOn(b) ? 'Take back' : b.getAttribute('aria-label');
    }

    function focusOn(b) {
      b.focus({ preventScroll: true });
    }

    buttons.forEach(function (b) {
      b.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') pointAt(b); });
      b.addEventListener('focus', function () { if (b.matches(':focus-visible')) pointAt(b); });
    });
    group.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse' && !said) pointAt(null); });
    group.addEventListener('keydown', function (e) {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var i = buttons.indexOf(e.target);
      var to = { ArrowUp: i - 1, ArrowLeft: i - 1, ArrowDown: i + 1, ArrowRight: i + 1, Home: 0, End: 3 }[e.key];
      if (i < 0 || to === undefined) return;
      e.preventDefault();
      e.stopPropagation();
      focusOn(buttons[(to + buttons.length) % buttons.length]);
    });

    return {
      pointAt: pointAt,
      say: function (b, text) {
        pointAt(b);
        said = b;
        words.textContent = text;
      },
      paint: function () {
        var at = buttons.filter(function (b) { return b.classList.contains('is-at'); })[0];
        place(at || buttons.filter(isOn)[0]);
      },
      enter: function () { focusOn(buttons.filter(isOn)[0] || buttons[1]); },
    };
  }

  MM.ratings = {
    LEVELS: LEVELS,
    know: know,
    thumb: thumb,
    mount: mount,
    read: read,
    get: function (ref) {
      var s = stateOf(ref);
      return s.known || s.busy ? s.want : undefined;
    },
    canRate: function (ref) { return stateOf(ref).rateable; },
    set: function (ref, level) { change(ref, level); },
  };
})(window);
