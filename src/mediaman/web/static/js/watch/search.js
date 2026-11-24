/* Watch: the search box and its results, in two groups: titles in the
 * library, then titles from TMDB that are not. Cards built here mirror
 * templates/_watch_card.html. Nothing is built with innerHTML: titles come
 * from Plex and TMDB. In the admin list view the same field only announces
 * what was typed (mm:watch-list-search) and the list searches itself.
 * Needs watch/core.js and watch/keep.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var byId = core.byId;
  var el = core.el;
  var safePath = core.safePath;
  var fadeIn = core.fadeIn;

  var SEARCH_URL = '/api/watch/search?q=';
  var TMDB_URL = '/api/watch/search/tmdb?q=';
  var SEARCH_DELAY_MS = 350;
  // TMDB answers nothing for less; do not ask.
  var TMDB_MIN_CHARS = 2;
  var SEARCH_FAILED = 'Couldn’t search just now.';
  var TMDB_FAILED = 'Couldn’t search for titles outside the library just now.';
  var STATE_BADGES = {
    queued: ['i-clock', 'Queued'],
    in_library: ['i-check', 'In library'],
    partial: ['i-check', 'In library']
  };

  function dur(minutes) {
    if (minutes >= 60) {
      var rest = minutes % 60;
      return Math.floor(minutes / 60) + ' h' + (rest ? ' ' + rest + ' min' : '');
    }
    return minutes + ' min';
  }

  function leavingText(days) {
    if (days <= 0) return 'Leaving today';
    return days === 1 ? 'Leaving in 1 day' : 'Leaving in ' + days + ' days';
  }

  function leavingShortText(days) {
    if (days <= 0) return 'Leaving today';
    return days === 1 ? '1 day left' : days + ' days left';
  }

  function buildCard(item) {
    var card = el('article', 'watch-card');
    card.setAttribute('data-keep-id', item.keep_id || '');

    // The link carries the ref: it is what takes focus back when the detail closes.
    var link = el('a', 'watch-card__link');
    if (item.ref) link.setAttribute('data-title-ref', item.ref);
    link.href = safePath(item.href) || '/watch';
    var poster = el('span', 'watch-card__poster');
    var posterUrl = safePath(item.poster_url);
    if (posterUrl) {
      var img = el('img');
      img.src = posterUrl;
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('data-broken-on-error', '');
      poster.appendChild(img);
    }
    poster.appendChild(el('span', 'watch-card__title', item.title));
    if (typeof item.leaving_days === 'number') {
      var badge = el('span', 'watch-badge watch-badge--leaving');
      badge.appendChild(el('span', 'watch-badge__long', leavingText(item.leaving_days)));
      badge.appendChild(el('span', 'watch-badge__short', leavingShortText(item.leaving_days)));
      poster.appendChild(badge);
    }
    if (item.watched) poster.appendChild(el('span', 'watch-badge watch-badge--watched', 'Watched'));
    if (item.progress_pct) {
      var bar = el('progress', 'watch-bar');
      bar.max = 100;
      bar.value = item.progress_pct;
      bar.setAttribute('aria-hidden', 'true');
      poster.appendChild(bar);
    }
    link.appendChild(poster);

    var sub = item.subtitle || '';
    if (item.minutes_left && !/ left$/.test(sub)) {
      sub += ' · ' + dur(item.minutes_left) + ' left';
    }
    link.appendChild(el('span', 'watch-card__sub', sub));
    card.appendChild(link);

    // Only a title that is leaving comes with a keep id, and the server checks it again.
    if (item.keep_id) {
      var btn = el('button', 'watch-keep');
      btn.type = 'button';
      btn.setAttribute('data-watch-keep', item.keep_id);
      btn.setAttribute('aria-haspopup', 'menu');
      btn.setAttribute('aria-expanded', 'false');
      btn.setAttribute('aria-label', 'Keep ' + item.title);
      btn.appendChild(el('span', '', 'Keep'));
      btn.appendChild(MM.dom.icon('i-chevron-down', 'watch-keep__chev'));
      if (window.MMWatch.keep.isBusy(item.keep_id)) {
        btn.setAttribute('aria-disabled', 'true');
        btn.setAttribute('aria-busy', 'true');
      }
      card.appendChild(btn);
    }
    return card;
  }

  /* A title from TMDB that is not in the library. */
  function buildTmdbCard(item) {
    var card = el('article', 'watch-card');

    var link = el('a', 'watch-card__link');
    link.setAttribute('data-title-ref', item.ref || '');
    link.href = safePath(item.href) || '#title=' + encodeURIComponent(item.ref || '');
    var poster = el('span', 'watch-card__poster');
    var posterUrl = safePath(item.poster_url);
    if (posterUrl) {
      var img = el('img');
      img.src = posterUrl;
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('data-broken-on-error', '');
      poster.appendChild(img);
    }
    poster.appendChild(el('span', 'watch-card__title', item.title));
    var badge = stateBadge(item);
    if (badge) poster.appendChild(badge);
    link.appendChild(poster);
    var sub = (item.kind === 'show' ? 'TV' : 'Movie') + (item.year ? ' · ' + item.year : '');
    link.appendChild(el('span', 'watch-card__sub', sub));
    card.appendChild(link);
    return card;
  }

  function stateBadge(item) {
    var state = item.download_state;
    var icon;
    var text;
    var said = '';
    var kind = state;
    if (state === 'downloading') {
      icon = 'i-arrow-down';
      if (typeof item.download_pct === 'number') {
        said = 'Downloading ';
        text = item.download_pct + '%';
      } else {
        text = 'Downloading';
      }
    } else if (STATE_BADGES[state]) {
      icon = STATE_BADGES[state][0];
      text = STATE_BADGES[state][1];
      if (state !== 'queued') kind = 'in';
    } else {
      return null;
    }
    var badge = el('span', 'watch-badge watch-badge--state watch-badge--' + kind);
    badge.appendChild(MM.dom.icon(icon));
    if (said) badge.appendChild(el('span', 'sr-only', said));
    badge.appendChild(document.createTextNode(text));
    return badge;
  }

  /* ── Groups ── */

  function groupHead(title, count) {
    var head = el('div', 'results-group__head');
    var h = el('h2', 'results-group__title', title);
    // Takes focus after a retry has replaced the button that had it.
    h.tabIndex = -1;
    head.appendChild(h);
    if (typeof count === 'number') {
      head.appendChild(el('span', 'results-group__count', count + (count === 1 ? ' title' : ' titles')));
    }
    return head;
  }

  function group(title, count) {
    var box = el('div', 'results-group');
    box.appendChild(groupHead(title, count));
    return box;
  }

  function cardGrid(items, build) {
    var grid = el('div', 'watch-grid');
    items.forEach(function (item) { grid.appendChild(build(item)); });
    return grid;
  }

  function loadingGroup(title) {
    var box = group(title);
    box.setAttribute('aria-busy', 'true');
    var grid = el('div', 'watch-grid');
    grid.setAttribute('aria-hidden', 'true');
    for (var n = 0; n < 6; n++) {
      var skel = el('div', 'watch-card watch-card--skel');
      skel.appendChild(el('span', 'skel skel--poster'));
      skel.appendChild(el('span', 'skel skel--line'));
      grid.appendChild(skel);
    }
    box.appendChild(grid);
    return box;
  }

  function failedGroup(title, text, retry) {
    var box = group(title);
    var line = el('p', 'watch-results__msg watch-results__msg--error');
    line.setAttribute('role', 'alert');
    line.appendChild(el('span', '', text));
    var again = el('button', 'watch-results__retry', 'Try again');
    again.type = 'button';
    again.addEventListener('click', retry);
    line.appendChild(again);
    box.appendChild(line);
    return box;
  }

  /* The line under the library group when TMDB is not set up. */
  function hint(data) {
    var line = el('p', 'results-hint');
    line.appendChild(MM.dom.icon('i-circle-info'));
    var text = el('span');
    var link = data.is_admin ? safePath(data.settings_tmdb_href) : '';
    if (link) {
      text.appendChild(document.createTextNode(
        'Only your library is searched. Connect TMDB to find titles you don’t have yet. '));
      var a = el('a', '', 'Set up TMDB');
      a.href = link;
      text.appendChild(a);
    } else {
      text.textContent = data.is_admin
        ? 'Only your library is searched. Connect TMDB to find titles you don’t have yet.'
        : 'Only titles already in the library can be found right now.';
    }
    line.appendChild(text);
    return line;
  }

  function readPageData() {
    var node = byId('watch-page-data');
    try { return JSON.parse(node ? node.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  function initSearch() {
    var input = byId('watch-search');
    var clear = byId('watch-search-clear');
    var browse = byId('watch-browse');
    var results = byId('watch-results');
    var msg = byId('watch-results-msg');
    var status = byId('watch-search-status');
    if (!input || !clear || !browse || !results || !msg || !status) return;
    var box = input.closest('.watch-search');
    var data = readPageData();
    // The browse grid of the page shell is not used: each group brings its own.
    var spare = byId('watch-grid');
    if (spare) spare.hidden = true;

    var listView = data.is_admin === true && data.view === 'list';
    var timer = null;
    // The search being shown; a reply for any other is dropped.
    var current = null;
    var shown = null;
    // The view change in progress: { to, anim }.
    var fade = null;
    // What each group slot holds now.
    var filled = new WeakMap();

    function setPending(on) {
      box.classList.toggle('is-pending', on);
    }

    function clearMsg() {
      msg.textContent = '';
      msg.classList.remove('watch-results__msg--error', 'watch-results__msg--empty');
      msg.removeAttribute('role');
    }

    /* The view goes at once, with whatever was still fading out behind it. */
    function dropGroups() {
      core.endSwap(shown);
      if (shown && shown.parentNode) shown.remove();
      shown = null;
    }

    function stop() {
      clearTimeout(timer);
      if (current) current.ctrl.abort();
      current = null;
      setPending(false);
      results.setAttribute('aria-busy', 'false');
    }

    /* Show the results or the rows: the one on view fades out over --dur-fast,
       then the other is shown and eases in over --dur. Asking for the view that
       is on its way is a no-op; asking for the one still on view stops the fade
       and leaves it as it was. */
    function showView(toResults) {
      var to = toResults ? results : browse;
      var from = toResults ? browse : results;
      if (fade) {
        var busy = fade;
        if (busy.to === to) return;
        fade = null;
        busy.anim.cancel();
        return;
      }
      if (!to.hidden) return;
      if (core.reduceMotion() || !from.animate || from.hidden) {
        from.hidden = true;
        to.hidden = false;
        fadeIn(to);
        return;
      }
      var anim = from.animate([{ opacity: 1 }, { opacity: 0 }], {
        duration: core.tokenMs('--dur-fast', 150),
        easing: core.token('--ease-out', 'ease-out'),
        fill: 'forwards'
      });
      fade = { to: to, anim: anim };
      anim.onfinish = function () {
        if (!fade || fade.anim !== anim) return;
        fade = null;
        from.hidden = true;
        anim.cancel();
        to.hidden = false;
        fadeIn(to);
      };
    }

    function showBrowse() {
      status.textContent = '';
      showView(false);
      dropGroups();
      clearMsg();
    }

    /* Reveal the results in place of the rows. Says whether they were not yet on view. */
    function reveal() {
      var was = results.hidden;
      showView(true);
      return was;
    }

    function mount(node) {
      var first = reveal();
      var hadMsg = msg.textContent !== '';
      clearMsg();
      if (shown && shown.parentNode) {
        core.swap(shown, node);
      } else {
        results.appendChild(node);
        if (!first && hadMsg) fadeIn(node);
      }
      shown = node;
    }

    function showLine(kind, iconName) {
      reveal();
      dropGroups();
      clearMsg();
      if (current) current.view = current.libDrawn = current.tmdbDrawn = null;
      msg.classList.add('watch-results__msg--' + kind);
      if (iconName) {
        msg.appendChild(MM.dom.icon(iconName, 'watch-results__icon'));
      }
      fadeIn(msg);
    }

    /* Nothing found. With TMDB not set up, the line below says why nothing
       outside the library was searched. */
    function showNothing(q, notSetUp) {
      showLine('empty', 'i-magnifying-glass');
      var line = 'No matches for “' + q + '”';
      msg.appendChild(el('span', 'watch-results__line', line));
      msg.appendChild(el('span', 'watch-results__hint', 'Check the spelling, or try a shorter title.'));
      status.textContent = line;
      if (notSetUp) {
        shown = hint(data);
        results.appendChild(shown);
      }
    }

    /* The whole search failed: one line, one way on that runs both searches again. */
    function showFailed() {
      showLine('error');
      msg.setAttribute('role', 'alert');
      msg.appendChild(el('span', '', SEARCH_FAILED));
      var retry = el('button', 'watch-results__retry', 'Try again');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        input.focus();
        run();
      });
      msg.appendChild(retry);
      status.textContent = SEARCH_FAILED;
    }

    function readOut(c) {
      var parts = [];
      if (c.lib.state === 'ok' && c.lib.items.length) parts.push(c.lib.items.length + ' in your library');
      if (c.tmdb.state === 'ok' && c.tmdb.items.length) parts.push(c.tmdb.items.length + ' available to download');
      status.textContent = parts.length
        ? parts.join(', ') + ' for “' + c.q + '”'
        : 'No matches in your library for “' + c.q + '”';
    }

    /* After a retry the group it was in is rebuilt: put focus back in the
       new one, on its Try again if it failed again. */
    function refocus(c, which, node) {
      if (c.refocus !== which || !node) return;
      var a = document.activeElement;
      c.refocus = null;
      if (a && a !== document.body && document.body.contains(a)) return;
      var target = node.querySelector('.watch-results__retry') || node.querySelector('.results-group__title');
      if (target) target.focus();
    }

    /* Put what is known about search *c* on the page. The library group goes
       up the moment it arrives; TMDB fills its own place when it does. */
    function paint(c) {
      if (current !== c || (c.lib.state === 'loading' && !c.view)) return;
      var tmdb = c.tmdb;
      var tmdbQuiet = tmdb.state === 'short' || tmdb.state === 'unconfigured' ||
        (tmdb.state === 'ok' && !tmdb.items.length);
      var settled = tmdb.state !== 'loading' && c.lib.state !== 'loading';
      results.setAttribute('aria-busy', 'false');
      setPending(!settled);

      if (c.lib.state === 'ok' && !c.lib.items.length && tmdbQuiet) {
        showNothing(c.q, tmdb.state === 'unconfigured');
        return;
      }
      if (c.lib.state === 'failed' && (tmdbQuiet || tmdb.state === 'failed')) { showFailed(); return; }

      if (!c.view) {
        c.view = el('div', 'results-groups');
        c.libSlot = el('div');
        c.tmdbSlot = el('div');
        c.view.appendChild(c.libSlot);
        c.view.appendChild(c.tmdbSlot);
        mount(c.view);
      }
      // An empty library says so above a TMDB error, so it is drawn again then.
      var tmdbNew = c.tmdbDrawn !== tmdb;
      if (c.libDrawn !== c.lib || (tmdbNew && c.lib.state === 'ok' && !c.lib.items.length)) {
        c.libDrawn = c.lib;
        fillLibrary(c);
      }
      if (tmdbNew) {
        c.tmdbDrawn = tmdb;
        fillTmdb(c);
      }
      if (settled) readOut(c);
    }

    /* What a slot holds changes by a cross-fade, so a group that arrives
       does not pop in over its skeleton. */
    function fillSlot(slot, node) {
      var old = filled.get(slot);
      filled.set(slot, node);
      if (old && old.parentNode && node) {
        core.swap(old, node);
        return;
      }
      core.endSwap(old);
      slot.textContent = '';
      if (node) {
        slot.appendChild(node);
        fadeIn(node);
      }
    }

    function fillLibrary(c) {
      var lib = c.lib;
      var node = null;
      if (lib.state === 'loading') node = loadingGroup('In your library');
      else if (lib.state === 'failed') {
        node = failedGroup('In your library', lib.text, function () { c.refocus = 'lib'; fetchLibrary(c); paint(c); });
      } else if (lib.items.length) {
        node = group('In your library', lib.items.length);
        node.appendChild(cardGrid(lib.items, buildCard));
      } else if (c.tmdb.state === 'failed') {
        node = el('p', 'results-none', 'Nothing in your library matches');
      }
      fillSlot(c.libSlot, node);
      if (lib.state !== 'loading') refocus(c, 'lib', node);
    }

    function fillTmdb(c) {
      var tmdb = c.tmdb;
      var node = null;
      if (tmdb.state === 'loading') node = loadingGroup('Available to download');
      else if (tmdb.state === 'failed') {
        node = failedGroup('Available to download', TMDB_FAILED, function () { c.refocus = 'tmdb'; fetchTmdb(c); paint(c); });
      } else if (tmdb.state === 'unconfigured') {
        // Said once there is a library result to put it under.
        if (c.lib.state === 'ok' && c.lib.items.length) node = hint(data);
      } else if (tmdb.state === 'ok' && tmdb.items.length) {
        node = group('Available to download', tmdb.items.length);
        node.appendChild(cardGrid(tmdb.items, buildTmdbCard));
      }
      fillSlot(c.tmdbSlot, node);
      if (tmdb.state !== 'loading') refocus(c, 'tmdb', node);
    }

    function fetchLibrary(c) {
      c.lib = { state: 'loading' };
      MM.api.get(SEARCH_URL + encodeURIComponent(c.q), { signal: c.ctrl.signal }).then(function (reply) {
        if (current !== c) return;
        c.lib = { state: 'ok', items: (reply && reply.items) || [] };
        paint(c);
      }).catch(function (err) {
        if ((err && err.name === 'AbortError') || current !== c) return;
        c.lib = { state: 'failed', text: MM.api.messageOf(err, 'Couldn’t search just now.') };
        paint(c);
      });
    }

    function fetchTmdb(c) {
      c.tmdb = { state: 'loading' };
      MM.api.get(TMDB_URL + encodeURIComponent(c.q), { signal: c.ctrl.signal }).then(function (reply) {
        if (current !== c) return;
        if (reply && reply.tmdb_configured === false) c.tmdb = { state: 'unconfigured' };
        else c.tmdb = { state: 'ok', items: (reply && reply.items) || [] };
        paint(c);
      }).catch(function (err) {
        if ((err && err.name === 'AbortError') || current !== c) return;
        c.tmdb = { state: 'failed' };
        paint(c);
      });
    }

    /* Keep ?q= in the address, replacing the entry so Back is not spammed. */
    function syncUrl(q) {
      if (!window.history || !window.history.replaceState || !window.URLSearchParams) return;
      var params = new URLSearchParams(window.location.search);
      if (q) params.set('q', q); else params.delete('q');
      var query = params.toString();
      window.history.replaceState(window.history.state, '',
        window.location.pathname + (query ? '?' + query : '') + (window.location.hash || ''));
    }

    function run() {
      var q = input.value.trim();
      stop();
      syncUrl(q);
      if (!q) { showBrowse(); return; }
      var c = { q: q, ctrl: new AbortController(), lib: null, tmdb: null, view: null };
      current = c;
      setPending(true);
      // Old results stay, dimmed, until the library answers.
      results.setAttribute('aria-busy', 'true');
      // Emptied now, so the same answer is read out again when it comes.
      status.textContent = '';
      if (data.tmdb_configured !== true) c.tmdb = { state: 'unconfigured' };
      else if (q.length < TMDB_MIN_CHARS) c.tmdb = { state: 'short' };
      fetchLibrary(c);
      if (!c.tmdb) fetchTmdb(c);
    }

    function announceList() {
      document.dispatchEvent(new CustomEvent('mm:watch-list-search', {
        bubbles: true,
        detail: { q: input.value.trim() }
      }));
    }

    function change() {
      if (listView) announceList(); else run();
    }

    function clearSearch() {
      input.value = '';
      clear.hidden = true;
      change();
      input.focus();
    }

    input.addEventListener('input', function () {
      clear.hidden = !input.value;
      clearTimeout(timer);
      if (!input.value.trim()) { change(); return; }
      timer = setTimeout(change, SEARCH_DELAY_MS);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && input.value) { e.preventDefault(); clearSearch(); return; }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      clearTimeout(timer);
      change();
      // The keyboard on a phone covers the results.
      if (core.coarsePointer()) input.blur();
    });
    clear.addEventListener('click', clearSearch);

    // The admin switch between the rows and the list moves the field with it.
    window.addEventListener('mm:watch-view', function (e) {
      var view = e && e.detail && e.detail.view;
      listView = data.is_admin === true && view === 'list';
      stop();
      if (fade) { fade.anim.cancel(); fade = null; }
      results.hidden = true;
      dropGroups();
      clearMsg();
      status.textContent = '';
      input.placeholder = listView ? 'Search your library' : 'Search films and shows';
      input.setAttribute('aria-label', input.placeholder);
      if (input.value.trim()) change();
    });

    // A value the browser put back (going back to the page) gets its results.
    if (input.value) {
      clear.hidden = false;
      if (!listView) {
        // The page opened on the results (a search in the address): hold their place until they arrive.
        if (!results.hidden) {
          shown = loadingGroup('In your library');
          results.appendChild(shown);
        }
        run();
      }
    }
  }

  window.MMWatch.search = { init: initSearch };
})();
