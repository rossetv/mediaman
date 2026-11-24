/* Watch: season tabs on the show page. Needs watch/core.js and watch/keep.js. */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;
  var toArray = core.toArray;
  var fadeIn = core.fadeIn;

  /* ── Season tabs (show page) ── */

  /* A season tab swaps the episode list in place: no reload, no jump to the
     top, focus stays on the tab. The address changes too, so Back and a
     shared link work; when a season will not load, the one showing stays and an error offers a retry. */
  function initSeasons() {
    var nav = document.querySelector('.watch-seasons');
    var list = document.querySelector('.watch-episodes');
    if (!nav || !list || !window.fetch || !window.DOMParser || !window.history.pushState) return;
    var loading = null;
    var current = null;
    var shown = null; // the season whose episodes are on the page
    var failure = null;

    function pills() { return toArray(nav.querySelectorAll('a.watch-pill')); }

    function select(href) {
      current = href;
      pills().forEach(function (pill) {
        var on = pill.getAttribute('href') === href;
        pill.classList.toggle('on', on);
        if (on) pill.setAttribute('aria-current', 'page'); else pill.removeAttribute('aria-current');
      });
      showPicked(core.reduceMotion() ? 'auto' : 'smooth');
    }

    /* The tabs stand on one line that scrolls, with the rows' arrows and
       fades (watch-rows.js starts it). */
    function makeLine() {
      var viewport = el('div', 'watch-row__viewport watch-seasons__line');
      nav.before(viewport);
      viewport.appendChild(nav);
      nav.classList.add('watch-row__scroll');
      showPicked('auto');
    }

    function showPicked(behavior) {
      var pill = nav.querySelector('.watch-pill.on');
      if (!pill) return;
      var left = pill.offsetLeft - nav.scrollLeft;
      var cutOff = left < 0 || left + pill.offsetWidth > nav.clientWidth;
      if (cutOff) nav.scrollTo({ left: pill.offsetLeft - (nav.clientWidth - pill.offsetWidth) / 2, behavior: behavior });
    }

    /* The line's slot keeps its place through a switch, so Resume and the
       season tabs stay where the next tap expects them: the new season's
       own line (on one that is not leaving, the quiet one naming the season
       that is), else an empty slot. The slot is a new node: an Undo still
       pending refills the old one. */
    function swapLeaving(doc) {
      var oldLine = document.querySelector('.watch-hero__info [data-watch-leaving]');
      var newLine = doc.querySelector('.watch-hero__info [data-watch-leaving]');
      var fresh = null;
      if (newLine) {
        fresh = document.importNode(newLine, true);
        fadeIn(fresh);
      } else if (oldLine) {
        fresh = el('p', 'watch-leaving');
        fresh.setAttribute('data-watch-leaving', '');
      }
      if (oldLine && fresh) oldLine.replaceWith(fresh);
      else if (fresh) document.querySelector('.watch-hero__info .watch-actions').before(fresh);
    }

    function clearFailure() {
      if (failure) failure.remove();
      failure = null;
    }

    /* The season stays as it was; the user can try the one they asked for again. */
    function showFailed(href, push) {
      select(shown);
      list.setAttribute('aria-busy', 'false');
      clearFailure();
      failure = el('p', 'watch-results__msg watch-results__msg--error watch-results__msg--season');
      failure.setAttribute('role', 'alert');
      var asked = pills().filter(function (pill) { return pill.getAttribute('href') === href; })[0];
      failure.appendChild(el('span', '', 'Could not load ' + (asked ? asked.textContent.trim() : 'that season') + '.'));
      var retry = el('button', 'watch-results__retry', 'Try again');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        select(href);
        load(href, push);
      });
      failure.appendChild(retry);
      list.before(failure);
    }

    function load(href, push) {
      if (loading) loading.abort();
      clearFailure();
      var ctrl = new AbortController();
      loading = ctrl;
      list.setAttribute('aria-busy', 'true');
      fetch(href, { credentials: 'same-origin', signal: ctrl.signal, headers: { Accept: 'text/html' } })
        .then(function (resp) {
          if (!resp.ok || resp.redirected) throw new Error('season page');
          return resp.text();
        })
        .then(function (html) {
          if (loading !== ctrl) return;
          loading = null;
          var doc = new DOMParser().parseFromString(html, 'text/html');
          var newList = doc.querySelector('.watch-episodes');
          var on = doc.querySelector('.watch-seasons a.watch-pill.on');
          if (!newList || !on) throw new Error('season page');
          window.MMWatch.keep.closeMenu(false);
          swapLeaving(doc);
          var fresh = document.importNode(newList, true);
          list.replaceWith(fresh);
          list = fresh;
          fadeIn(list);
          select(on.getAttribute('href'));
          shown = current;
          if (push) window.history.pushState({ watchSeason: href }, '', href);
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return;
          if (loading && loading !== ctrl) return;
          loading = null;
          showFailed(href, push);
        });
    }

    makeLine();
    var first = nav.querySelector('a.watch-pill.on');
    current = shown = first ? first.getAttribute('href') : null;

    nav.addEventListener('click', function (e) {
      var pill = e.target.closest && e.target.closest('a.watch-pill');
      if (!pill || e.defaultPrevented || e.button !== 0 ||
          e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      var href = pill.getAttribute('href');
      if (href === current && !loading) return;
      select(href);
      load(href, true);
    });

    window.addEventListener('popstate', function () {
      var href = window.location.pathname + window.location.search;
      if (href !== current || loading) load(href, false);
    });
  }

  window.MMWatch.seasons = { init: initSeasons };
})();
