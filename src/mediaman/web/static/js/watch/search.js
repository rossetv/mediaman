/* Watch: the search box and its result cards. Cards built here mirror
 * templates/_watch_card.html. Nothing is built with innerHTML: titles come
 * from Plex. Needs watch/core.js and watch/keep.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var byId = core.byId;
  var el = core.el;
  var safePath = core.safePath;
  var fadeIn = core.fadeIn;

  var SEARCH_URL = '/api/watch/search?q=';
  var SEARCH_DELAY_MS = 250;

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

    var link = el('a', 'watch-card__link');
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

    if (item.keep_id) {
      var btn = el('button', 'watch-keep');
      btn.type = 'button';
      btn.setAttribute('data-watch-keep', item.keep_id);
      btn.setAttribute('aria-haspopup', 'menu');
      btn.setAttribute('aria-expanded', 'false');
      btn.setAttribute('aria-label', 'Keep ' + item.title);
      btn.appendChild(el('span', '', 'Keep'));
      var chev = el('i', 'fa-solid fa-chevron-down watch-keep__chev');
      chev.setAttribute('aria-hidden', 'true');
      btn.appendChild(chev);
      if (window.MMWatch.keep.isBusy(item.keep_id)) {
        btn.setAttribute('aria-disabled', 'true');
        btn.setAttribute('aria-busy', 'true');
      }
      card.appendChild(btn);
    }
    return card;
  }

  function initSearch() {
    var input = byId('watch-search');
    var clear = byId('watch-search-clear');
    var browse = byId('watch-browse');
    var results = byId('watch-results');
    var grid = byId('watch-grid');
    var msg = byId('watch-results-msg');
    var status = byId('watch-search-status');
    if (!input || !clear || !browse || !results || !grid || !msg || !status) return;
    var box = input.closest('.watch-search');

    var timer = null;
    var pending = null;
    // The message dims with the old results while a search runs.
    msg.style.transition = 'opacity var(--dur) var(--ease-out)';

    function setPending(on) {
      box.classList.toggle('is-pending', on);
      results.setAttribute('aria-busy', on ? 'true' : 'false');
      msg.style.opacity = on ? '.5' : '';
      msg.style.transitionDelay = on ? '.1s' : '';
    }

    function showBrowse() {
      setPending(false);
      status.textContent = '';
      if (!results.hidden) {
        results.hidden = true;
        browse.hidden = false;
        fadeIn(browse);
      }
      grid.textContent = '';
      msg.textContent = '';
      msg.classList.remove('watch-results__msg--failed', 'watch-results__msg--empty');
    }

    function showResults() {
      setPending(false);
      if (results.hidden) {
        browse.hidden = true;
        results.hidden = false;
        fadeIn(results);
      } else {
        fadeIn(grid);
      }
      grid.textContent = '';
      msg.textContent = '';
      msg.classList.remove('watch-results__msg--failed', 'watch-results__msg--empty');
    }

    function showFound(q, items) {
      showResults();
      items.forEach(function (item) { grid.appendChild(buildCard(item)); });
      if (items.length) {
        status.textContent = (items.length === 1 ? '1 result' : items.length + ' results') + ' for “' + q + '”';
        return;
      }
      var line = 'Nothing found for “' + q + '”.';
      var icon = el('i', 'fa-solid fa-magnifying-glass watch-results__icon');
      icon.setAttribute('aria-hidden', 'true');
      msg.classList.add('watch-results__msg--empty');
      msg.appendChild(icon);
      msg.appendChild(el('span', 'watch-results__line', line));
      msg.appendChild(el('span', 'watch-results__hint', 'Check the spelling, or try a shorter title.'));
      status.textContent = line;
    }

    /* Laid out like the empty state: an icon, the line, and a way on. */
    function showFailed(text) {
      showResults();
      var icon = el('i', 'fa-solid fa-triangle-exclamation watch-results__icon');
      icon.setAttribute('aria-hidden', 'true');
      msg.classList.add('watch-results__msg--failed');
      msg.appendChild(icon);
      msg.appendChild(el('span', 'watch-results__line', text));
      var retry = el('button', 'btn watch-btn--grey watch-results__again', 'Try again');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        input.focus();
        run();
      });
      msg.appendChild(retry);
      status.textContent = text;
    }

    function run() {
      var q = input.value.trim();
      clearTimeout(timer);
      if (pending) pending.abort();
      pending = null;
      if (!q) { showBrowse(); return; }
      var ctrl = new AbortController();
      pending = ctrl;
      setPending(true);
      // Emptied now, so the same answer is read out again when it comes.
      status.textContent = '';
      MM.api.get(SEARCH_URL + encodeURIComponent(q), { signal: ctrl.signal }).then(function (data) {
        if (pending !== ctrl) return;
        pending = null;
        showFound(q, (data && data.items) || []);
      }).catch(function (err) {
        if ((err && err.name === 'AbortError') || pending !== ctrl) return;
        pending = null;
        showFailed(MM.api.messageOf(err, 'Search failed.'));
      });
    }

    function clearSearch() {
      input.value = '';
      clear.hidden = true;
      run();
      input.focus();
    }

    input.addEventListener('input', function () {
      clear.hidden = !input.value;
      clearTimeout(timer);
      if (!input.value.trim()) { run(); return; }
      timer = setTimeout(run, SEARCH_DELAY_MS);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && input.value) { e.preventDefault(); clearSearch(); }
    });
    clear.addEventListener('click', clearSearch);
    // A value the browser put back (going back to the page) gets its results.
    if (input.value) { clear.hidden = false; run(); }
  }

  window.MMWatch.search = { init: initSearch };
})();
