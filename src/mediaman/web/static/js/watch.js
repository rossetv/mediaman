/* Watch browse pages: Keep menu, search, and the Plex "Try again" button.
 *
 * Cards built here for search results mirror templates/_watch_card.html.
 * Nothing is built with innerHTML: titles come from Plex.
 */
(function () {
  'use strict';

  var KEEP_URL = '/api/watch/keep';
  var SEARCH_URL = '/api/watch/search?q=';
  var SEARCH_DELAY_MS = 250;

  function byId(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* Only same-site paths may become a link or image address. */
  function safePath(value) {
    return typeof value === 'string' && value.charAt(0) === '/' &&
      value.charAt(1) !== '/' && value.charAt(1) !== '\\' ? value : '';
  }

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

  function toast(message, variant) {
    if (window.UIFeedback) window.UIFeedback.toast(message, { variant: variant });
  }

  /* ── Try again ── */

  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-watch-retry]')) window.location.reload();
  });

  /* ── Keep ── */

  var menu = byId('watch-keep-menu');
  var openTrigger = null;

  function menuItems() {
    return Array.prototype.slice.call(menu.querySelectorAll('[data-days]'));
  }

  /* Open below the trigger, or above it when that is where the room is, and
     stay clear of the phone's bottom tab bar. */
  function placeMenu(trigger) {
    var gap = 6;
    var edge = 8;
    var rect = trigger.getBoundingClientRect();
    var bottom = window.innerHeight;
    var tabs = document.querySelector('.nav-tabs');
    if (tabs && getComputedStyle(tabs).display !== 'none') {
      bottom = Math.min(bottom, tabs.getBoundingClientRect().top);
    }
    var w = menu.offsetWidth;
    var h = menu.offsetHeight;
    var top = rect.bottom + gap;
    if (top + h > bottom - edge && rect.top - gap - h > edge) top = rect.top - gap - h;
    var left = Math.max(edge, Math.min(rect.left, window.innerWidth - w - edge));
    menu.style.top = top + 'px';
    menu.style.left = left + 'px';
  }

  function closeMenu(refocus) {
    if (!openTrigger) return;
    var trigger = openTrigger;
    openTrigger = null;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (refocus && document.body.contains(trigger)) trigger.focus();
  }

  function openMenu(trigger) {
    closeMenu(false);
    openTrigger = trigger;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    placeMenu(trigger);
    menuItems()[0].focus({ preventScroll: true });
  }

  /* After a Keep: a Leaving soon card goes; anywhere else the badge and
     button go. A detail page's leaving line says how long it was kept. */
  function applyKept(id, days) {
    var triggers = document.querySelectorAll('[data-watch-keep]');
    Array.prototype.forEach.call(triggers, function (trigger) {
      if (trigger.getAttribute('data-watch-keep') !== id) return;
      var line = trigger.closest('[data-watch-leaving]');
      var card = trigger.closest('.watch-card');
      if (line) {
        line.textContent = 'Kept for ' + days + ' days';
      } else if (card) {
        var row = card.closest('[data-watch-leaving-row]');
        if (row) {
          var neighbour = card.nextElementSibling || card.previousElementSibling;
          var scroller = card.parentNode;
          card.remove();
          if (!scroller.querySelector('.watch-card')) {
            row.remove();
          } else if (neighbour) {
            var link = neighbour.querySelector('a');
            if (link) link.focus();
          }
        } else {
          var badge = card.querySelector('.watch-badge--leaving');
          if (badge) badge.remove();
          trigger.remove();
        }
      }
    });
  }

  function keep(trigger, days) {
    var id = trigger.getAttribute('data-watch-keep');
    trigger.disabled = true;
    MM.api.post(KEEP_URL, { media_item_id: id, days: days }).then(function () {
      applyKept(id, days);
      toast('Kept for ' + days + ' days', 'success');
    }).catch(function (err) {
      trigger.disabled = false;
      toast(MM.api.messageOf(err, 'Could not keep this. Try again.'), 'error');
    });
  }

  if (menu) {
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target.closest) return;
      var trigger = target.closest('[data-watch-keep]');
      if (trigger) {
        if (openTrigger === trigger) closeMenu(false); else openMenu(trigger);
        return;
      }
      var item = target.closest('#watch-keep-menu [data-days]');
      if (item && openTrigger) {
        var chosen = openTrigger;
        closeMenu(false);
        keep(chosen, parseInt(item.getAttribute('data-days'), 10));
        return;
      }
      closeMenu(false);
    });

    document.addEventListener('keydown', function (e) {
      if (!openTrigger) return;
      if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return; }
      if (e.key === 'Tab') { closeMenu(false); return; }
      var items = menuItems();
      var idx = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') items[(idx + 1) % items.length].focus();
      else if (e.key === 'ArrowUp') items[(idx - 1 + items.length) % items.length].focus();
      else if (e.key === 'Home') items[0].focus();
      else if (e.key === 'End') items[items.length - 1].focus();
      else return;
      e.preventDefault();
    });

    // The menu is fixed, so it would be left behind by any scroll or resize.
    window.addEventListener('resize', function () { closeMenu(false); });
    document.addEventListener('scroll', function () { closeMenu(false); }, true);
  }

  /* ── Search ── */

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
      card.appendChild(btn);
    }
    return card;
  }

  function initSearch() {
    var input = byId('watch-search');
    var browse = byId('watch-browse');
    var results = byId('watch-results');
    var grid = byId('watch-grid');
    var msg = byId('watch-results-msg');
    if (!input || !browse || !results || !grid || !msg) return;

    var timer = null;
    var pending = null;

    function showBrowse() {
      results.hidden = true;
      browse.hidden = false;
      grid.textContent = '';
      msg.textContent = '';
    }

    function showResults(message, items) {
      browse.hidden = true;
      results.hidden = false;
      msg.textContent = message;
      grid.textContent = '';
      (items || []).forEach(function (item) { grid.appendChild(buildCard(item)); });
    }

    function run() {
      var q = input.value.trim();
      if (pending) pending.abort();
      if (!q) { showBrowse(); return; }
      pending = new AbortController();
      MM.api.get(SEARCH_URL + encodeURIComponent(q), { signal: pending.signal }).then(function (data) {
        var items = (data && data.items) || [];
        showResults(items.length ? '' : 'Nothing found for “' + q + '”.', items);
      }).catch(function (err) {
        if (err && err.name === 'AbortError') return;
        showResults(MM.api.messageOf(err, 'Search failed. Try again.'), []);
      });
    }

    input.addEventListener('input', function () {
      clearTimeout(timer);
      if (!input.value.trim()) { run(); return; }
      timer = setTimeout(run, SEARCH_DELAY_MS);
    });
  }

  initSearch();
})();
