/** Profile › Watching: the audio language and the subtitles a person wants on everything they
 *  play, and what the hub's rows say of them. Requires: core/api.js (MM.api), core/dom.js (MM.dom), ui-feedback.js
 *  (UIFeedback), watch/you.js (MM.you).
 *
 *   GET /api/account/playback  → {audio_lang, subtitle_lang, auto_skip, languages: [{code, name, native}],
 *                                 library_languages: [code]}
 *   PUT /api/account/playback  {audio_lang} or {subtitle_lang}, null to clear, or {auto_skip};
 *                              answers the same
 *
 *  audio_lang null plays a title in the language it comes in. subtitle_lang null shows subtitles
 *  only where needed, "off" none, a code subtitles in it whenever the audio is in another.
 */
(function () {
  'use strict';

  var pane = document.querySelector('[data-you-group="profile"]');
  if (!pane || !window.MM || !MM.api) return;

  var URL_ = '/api/account/playback';
  var SAVED = 'Saved. It applies from the next thing you play.';
  var FAILED = 'Couldn’t save that. Try again.';
  var UNLOADED = 'Your languages didn’t load. Close this and try again.';
  var TITLE_LANG = 'As the title comes';
  var SAVED_MS = 300;  // picks in a row are told of once
  var BACK_MS = 260;   // the tick lands before the view steps back

  function q(sel) { return pane.querySelector(sel); }
  var views = {
    audio: q('[data-view="profile/audio"]'),
    subs: q('[data-view="profile/subtitles"]'),
    sub: q('[data-view="profile/subtitles/language"]'),
  };
  if (!views.audio || !views.subs || !views.sub) return;
  var rows = {
    audio: q('#account-row-audio [data-row-value]'),
    subs: q('#account-row-subs [data-row-value]'),
    sub: q('#account-row-sublang [data-row-value]'),
  };
  var fold = q('[data-sub-fold]');
  var alwaysName = q('[data-sub-lang-name]');

  var held = { audio_lang: null, subtitle_lang: null };
  var langs = [];      // [{code, name, own}] by English name
  var byCode = {};
  var library = [];    // codes of the languages the library's titles are in, as far as known
  var shownView = '';
  var savedTimer = null;

  /* The language's name in itself, where the server does not say it: as the browser knows it, or
     the English one. */
  function ownName(code, english) {
    var own = '';
    try { own = new Intl.DisplayNames([code], { type: 'language' }).of(code) || ''; } catch (e) { own = ''; }
    if (!own || own === code) return english;
    return own.charAt(0).toLocaleUpperCase(code) + own.slice(1);
  }

  function nameOf(code) { return code && byCode[code] ? byCode[code].own : code ? code : TITLE_LANG; }
  function subsMode(v) { return v === null || v === undefined ? 'needed' : v === 'off' ? 'off' : 'always'; }
  function subsValue(v) {
    var mode = subsMode(v);
    return mode === 'needed' ? 'When needed' : mode === 'off' ? 'Off' : 'Always · ' + nameOf(v);
  }

  function showValues() {
    rows.audio.textContent = nameOf(held.audio_lang);
    rows.subs.textContent = subsValue(held.subtitle_lang);
    var sub = subsMode(held.subtitle_lang) === 'always' ? held.subtitle_lang : subLangFallback();
    rows.sub.textContent = nameOf(sub);
    alwaysName.textContent = nameOf(sub);
  }

  /* The language Always would use where none is set yet. */
  function subLangFallback() { return held.audio_lang && byCode[held.audio_lang] ? held.audio_lang : 'en'; }

  /* ── Radios ── */

  function radio(attr, value, main, small) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'yo-pick-row';
    b.setAttribute('role', 'radio');
    b.setAttribute(attr, value);
    b.setAttribute('aria-checked', 'false');
    b.tabIndex = -1;
    var text = document.createElement('span');
    text.className = 'yo-pick-row__text';
    text.appendChild(document.createTextNode(main));
    if (small) {
      var s = document.createElement('small');
      s.textContent = small;
      text.appendChild(s);
    }
    b.appendChild(text);
    b.appendChild(MM.dom.icon('i-check', 'yo-pick-row__tick'));
    return b;
  }

  function langRow(attr, l) {
    return radio(attr, l.code, l.own, l.own.toLowerCase() === l.name.toLowerCase() ? '' : l.name);
  }

  function pickRows(view) { return Array.prototype.slice.call(view.querySelectorAll('.yo-pick-row')); }

  /* The one checked row of a view; a language listed twice is checked where it comes first. */
  function check(view, attr, value) {
    var found = false;
    pickRows(view).forEach(function (r) {
      var on = !found && r.getAttribute(attr) === value;
      if (on) found = true;
      r.setAttribute('aria-checked', String(on));
      r.tabIndex = on ? 0 : -1;
      r.classList.remove('is-picked');
    });
    if (!found) {
      var first = pickRows(view)[0];
      if (first) first.tabIndex = 0;
    }
  }

  function head(text) {
    var h = document.createElement('h2');
    h.className = 'yo-group__head';
    h.setAttribute('data-lang-head', '');
    h.textContent = text;
    return h;
  }

  function group(label, list) {
    var g = document.createElement('div');
    g.className = 'yo-group';
    g.setAttribute('role', 'radiogroup');
    g.setAttribute('aria-label', label);
    g.setAttribute('data-lang-group', '');
    list.forEach(function (r) { g.appendChild(r); });
    return g;
  }

  /* The languages first offered: the one set, then those of the library; before anything is known
     of the library, English and the browser's own. */
  function suggested(current) {
    var wanted = [current].concat(library.length ? library : ['en'].concat(Array.prototype.slice.call(navigator.languages || [navigator.language || ''])));
    var out = [];
    wanted.forEach(function (tag) {
      var code = String(tag || '').toLowerCase().split(/[-_]/)[0];
      if (byCode[code] && out.indexOf(code) === -1) out.push(code);
    });
    return out;
  }

  function fillList(view, attr, current, withTitle) {
    var box = view.querySelector('[data-lang-list]');
    box.textContent = '';
    var first = suggested(current).map(function (code) { return langRow(attr, byCode[code]); });
    if (withTitle) first.unshift(radio(attr, '', TITLE_LANG, 'Usually the language it was made in'));
    var title = library.length ? 'In your library' : 'Suggested';
    box.appendChild(head(title));
    box.appendChild(group(title, first));
    box.appendChild(head('All languages'));
    box.appendChild(group('All languages', langs.map(function (l) { return langRow(attr, l); })));
    check(view, attr, current || '');
  }

  function fill() {
    fillList(views.audio, 'data-audio-lang', held.audio_lang, true);
    var sub = subsMode(held.subtitle_lang) === 'always' ? held.subtitle_lang : subLangFallback();
    fillList(views.sub, 'data-sub-lang', sub, false);
    check(views.subs, 'data-subs', subsMode(held.subtitle_lang));
    fold.classList.toggle('is-open', subsMode(held.subtitle_lang) === 'always');
    showValues();
  }

  /* ── Search ── */

  function filter(input) {
    var view = input.closest('.you-view');
    var text = input.value.trim();
    var needle = text.toLowerCase();
    input.parentNode.classList.toggle('has-q', !!text);
    var seen = {};
    var shown = 0;
    var heads = view.querySelectorAll('[data-lang-head]');
    Array.prototype.forEach.call(view.querySelectorAll('[data-lang-group]'), function (g, i) {
      var any = 0;
      Array.prototype.forEach.call(g.querySelectorAll('.yo-pick-row'), function (r) {
        var code = r.getAttribute('data-audio-lang') !== null ? r.getAttribute('data-audio-lang') : r.getAttribute('data-sub-lang');
        var hit = !needle || r.textContent.toLowerCase().indexOf(needle) !== -1 || code === needle;
        // Searched for, a language shows once.
        if (needle && seen[code]) hit = false;
        if (hit && needle) seen[code] = true;
        r.hidden = !hit;
        if (hit) any += 1;
      });
      shown += any;
      g.hidden = !any;
      if (heads[i]) heads[i].hidden = !any || !!needle;
    });
    var none = view.querySelector('[data-lang-none]');
    none.hidden = !!shown;
    none.textContent = shown ? '' : 'No language called “' + text + '”.';
  }

  pane.addEventListener('input', function (e) {
    if (e.target.matches && e.target.matches('[data-lang-q]')) filter(e.target);
  });

  /* ── Saving ── */

  function toastSaved() {
    clearTimeout(savedTimer);
    savedTimer = setTimeout(function () { UIFeedback.success(SAVED); }, SAVED_MS);
  }

  function take(data) {
    held = { audio_lang: data.audio_lang || null, subtitle_lang: data.subtitle_lang || null };
  }

  /* Checks *row* at once; the answer keeps it, or puts back what was checked. */
  function save(view, attr, row, body, after) {
    var was = pickRows(view).filter(function (r) { return r.getAttribute('aria-checked') === 'true'; })[0];
    var wasValue = was ? was.getAttribute(attr) : null;
    check(view, attr, row.getAttribute(attr));
    row.classList.add('is-picked');
    if (after) after.now();
    return MM.api.put(URL_, body).then(function (data) {
      take(data);
      showValues();
      toastSaved();
      if (after && after.saved) after.saved();
    }, function (err) {
      if (wasValue !== null) check(view, attr, wasValue);
      if (after && after.undo) after.undo();
      UIFeedback.error(MM.api.messageOf ? MM.api.messageOf(err, FAILED) : FAILED);
    });
  }

  pane.addEventListener('click', function (e) {
    var t = e.target;
    var x = t.closest('[data-lang-x]');
    if (x) {
      var input = x.parentNode.querySelector('[data-lang-q]');
      input.value = '';
      filter(input);
      input.focus();
      return;
    }
    var row = t.closest('.yo-pick-row');
    if (!row || row.getAttribute('aria-checked') === 'true') return;
    if (row.hasAttribute('data-audio-lang')) {
      save(views.audio, 'data-audio-lang', row, { audio_lang: row.getAttribute('data-audio-lang') || null });
    } else if (row.hasAttribute('data-subs')) {
      var mode = row.getAttribute('data-subs');
      var before = fold.classList.contains('is-open');
      var lang = { needed: null, off: 'off', always: views.sub.querySelector('[aria-checked="true"]') ? views.sub.querySelector('[aria-checked="true"]').getAttribute('data-sub-lang') : subLangFallback() }[mode];
      save(views.subs, 'data-subs', row, { subtitle_lang: lang }, {
        now: function () { fold.classList.toggle('is-open', mode === 'always'); },
        undo: function () { fold.classList.toggle('is-open', before); },
      });
    } else if (row.hasAttribute('data-sub-lang')) {
      var code = row.getAttribute('data-sub-lang');
      save(views.sub, 'data-sub-lang', row, { subtitle_lang: code }, {
        now: function () {},
        saved: function () {
          check(views.subs, 'data-subs', 'always');
          fold.classList.add('is-open');
          setTimeout(function () {
            var back = document.querySelector('#you [data-you-back]');
            if (shownView === 'profile/subtitles/language' && back) back.click();
          }, BACK_MS);
        },
      });
    }
  });

  /* Arrows move among the rows a view shows, as among radios. */
  pane.addEventListener('keydown', function (e) {
    var row = e.target.closest && e.target.closest('.yo-pick-row');
    if (!row || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
    e.preventDefault();
    var list = pickRows(row.closest('.you-view')).filter(function (r) { return !r.hidden && !r.closest('[hidden]'); });
    var i = list.indexOf(row);
    var next = list[(i + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length];
    list.forEach(function (r) { r.tabIndex = r === next ? 0 : -1; });
    next.focus();
  });

  /* Skip intros and credits on the TV: the switch moves at once, and back where it was not saved. */
  var skip = q('#account-skip');
  function drawSkip(on) {
    skip.classList.toggle('on', on);
    skip.setAttribute('aria-checked', String(on));
  }
  if (skip) {
    skip.addEventListener('click', function () {
      if (skip.getAttribute('aria-disabled') === 'true') return;
      var was = skip.getAttribute('aria-checked') === 'true';
      skip.setAttribute('aria-disabled', 'true');
      drawSkip(!was);
      MM.api.put(URL_, { auto_skip: !was }).then(function (data) {
        drawSkip(data.auto_skip !== false);
        toastSaved();
      }, function (err) {
        drawSkip(was);
        UIFeedback.error(MM.api.messageOf ? MM.api.messageOf(err, FAILED) : FAILED);
      }).then(function () { skip.removeAttribute('aria-disabled'); });
    });
  }

  document.addEventListener('mm:you-shown', function (e) { shownView = e.detail && e.detail.view || ''; });
  document.addEventListener('mm:you-hidden', function () { shownView = ''; });

  MM.api.get(URL_).then(function (data) {
    take(data);
    // Fresher than the page, unless a press is on its way.
    if (skip && skip.getAttribute('aria-disabled') !== 'true') drawSkip(data.auto_skip !== false);
    langs = (data.languages || []).map(function (l) { return { code: l.code, name: l.name, own: l.native || ownName(l.code, l.name) }; });
    library = data.library_languages || [];
    byCode = {};
    langs.forEach(function (l) { byCode[l.code] = l; });
    fill();
  }, function () {
    [views.audio, views.sub].forEach(function (view) {
      var none = view.querySelector('[data-lang-none]');
      none.textContent = UNLOADED;
      none.hidden = false;
    });
  });
})();
