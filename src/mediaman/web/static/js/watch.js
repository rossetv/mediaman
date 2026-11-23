/* Watch browse pages: Keep (with Undo), search, season tabs and the Plex
 * "Try again" button.
 *
 * Cards built here for search results mirror templates/_watch_card.html.
 * Nothing is built with innerHTML: titles come from Plex.
 */
(function () {
  'use strict';

  var KEEP_URL = '/api/watch/keep';
  var UNKEEP_URL = '/api/watch/unkeep';
  var SEARCH_URL = '/api/watch/search?q=';
  var SEARCH_DELAY_MS = 250;
  var EXIT_FALLBACK_MS = 400; // past the slowest motion token, for a transition that never runs

  function byId(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function toArray(list) { return Array.prototype.slice.call(list); }

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

  function toast(message, opts) {
    if (window.UIFeedback) window.UIFeedback.toast(message, opts);
  }

  /* ── Motion ── */

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function token(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function tokenMs(name, fallback) {
    var value = token(name, '');
    if (!value) return fallback;
    return /ms$/.test(value) ? parseFloat(value) : parseFloat(value) * 1000;
  }

  /* Ease a node that was just put on the page in (the shared .fade-in). */
  function fadeIn(node) {
    node.classList.remove('fade-in');
    void node.offsetWidth; // restart the animation when the node is reused
    node.classList.add('fade-in');
  }

  /* The frames that shrink a card out of its row (sideways) or a row out of
     the page (downwards), gap included, so nothing beside it jumps. */
  function shrinkFrames(node, sideways) {
    var parentStyle = getComputedStyle(node.parentNode);
    if (sideways) {
      var w = node.offsetWidth + 'px';
      var gap = parseFloat(parentStyle.columnGap) || 0;
      return [
        { flexBasis: w, width: w, marginRight: '0px', opacity: 1 },
        { flexBasis: '0px', width: '0px', marginRight: -gap + 'px', opacity: 0 }
      ];
    }
    var h = node.offsetHeight + 'px';
    var rowGap = parseFloat(parentStyle.rowGap) || 0;
    var side = node.previousElementSibling ? 'marginTop' : 'marginBottom';
    var from = { height: h, opacity: 1 };
    var to = { height: '0px', opacity: 0 };
    from[side] = '0px';
    to[side] = -rowGap + 'px';
    return [from, to];
  }

  /* Shrink *node* away, then take it off the page. Returns a cancel function
     that leaves it where it is (an Undo pressed mid-way). */
  function collapseAway(node, sideways) {
    if (reduceMotion() || !node.animate) {
      node.remove();
      return function () {};
    }
    node.style.overflow = 'hidden';
    var anim = node.animate(shrinkFrames(node, sideways), {
      duration: tokenMs('--dur-slow', 250),
      easing: token('--ease-out', 'ease-out'),
      fill: 'forwards'
    });
    anim.onfinish = function () {
      node.remove();
      anim.cancel();
      node.style.overflow = '';
    };
    return function () {
      anim.onfinish = null;
      anim.cancel();
      node.style.overflow = '';
    };
  }

  /* Grow a node that was just put back from nothing to its own size. */
  function growIn(node, sideways) {
    if (reduceMotion() || !node.animate) return;
    node.style.overflow = 'hidden';
    var anim = node.animate(shrinkFrames(node, sideways).reverse(), {
      duration: tokenMs('--dur-slow', 250),
      easing: token('--ease-out', 'ease-out')
    });
    anim.onfinish = anim.oncancel = function () { node.style.overflow = ''; };
  }

  /* Put a node back where it was taken from, or at the end when the node
     that followed it has gone too. */
  function putBack(spot) {
    var next = spot.next && spot.next.parentNode === spot.parent ? spot.next : null;
    spot.parent.insertBefore(spot.node, next);
  }

  function takeOut(node) {
    var spot = { node: node, parent: node.parentNode, next: node.nextSibling };
    node.remove();
    return spot;
  }

  /* ── Try again (Plex) ── */

  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-watch-retry]')) window.location.reload();
  });

  /* ── Keep menu ── */

  var menu = byId('watch-keep-menu');
  var openTrigger = null;
  var hideTimer = null;

  function menuItems() {
    return toArray(menu.querySelectorAll('[data-days]'));
  }

  /* Whether the trigger can still be seen: on the screen, and inside the
     visible part of its row. */
  function inView(trigger) {
    if (!document.body.contains(trigger)) return false;
    var r = trigger.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) {
      return false;
    }
    var row = trigger.closest('.watch-row__scroll');
    if (!row) return true;
    var box = row.getBoundingClientRect();
    return r.right > box.left && r.left < box.right;
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
    var above = top + h > bottom - edge && rect.top - gap - h > edge;
    if (above) top = rect.top - gap - h;
    var left = Math.max(edge, Math.min(rect.left, window.innerWidth - w - edge));
    menu.classList.toggle('is-above', above);
    menu.style.top = top + 'px';
    menu.style.left = left + 'px';
  }

  function closeMenu(refocus) {
    if (!openTrigger) return;
    var trigger = openTrigger;
    openTrigger = null;
    trigger.setAttribute('aria-expanded', 'false');
    if (refocus && document.body.contains(trigger)) trigger.focus();
    menu.classList.remove('is-open');
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () { menu.hidden = true; }, reduceMotion() ? 0 : EXIT_FALLBACK_MS);
  }

  function openMenu(trigger) {
    closeMenu(false);
    clearTimeout(hideTimer);
    openTrigger = trigger;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    placeMenu(trigger);
    void menu.offsetWidth; // start the entrance from the closed look
    menu.classList.add('is-open');
    menuItems()[0].focus({ preventScroll: true });
  }

  /* ── Keep ── */

  var busy = {};

  function triggersFor(id) {
    return toArray(document.querySelectorAll('[data-watch-keep]')).filter(function (t) {
      return t.getAttribute('data-watch-keep') === id;
    });
  }

  function cardsFor(id) {
    return toArray(document.querySelectorAll('.watch-card[data-keep-id]')).filter(function (c) {
      return c.getAttribute('data-keep-id') === id && !c.classList.contains('is-kept');
    });
  }

  /* One Keep or Undo at a time per title, whichever button it came from. The
     buttons stay focusable (aria-disabled, not disabled) so focus never drops. */
  function setBusy(id, on) {
    busy[id] = on;
    triggersFor(id).forEach(function (t) {
      if (on) t.setAttribute('aria-disabled', 'true'); else t.removeAttribute('aria-disabled');
      t.setAttribute('aria-busy', on ? 'true' : 'false');
    });
  }

  /* Where focus goes when a kept card leaves its row: the next card, else
     the one before; when the row goes, the next row, else the search box. */
  function focusAfterCard(card) {
    var sibling = card.nextElementSibling;
    while (sibling && sibling.classList.contains('is-kept')) sibling = sibling.nextElementSibling;
    if (!sibling) {
      sibling = card.previousElementSibling;
      while (sibling && sibling.classList.contains('is-kept')) sibling = sibling.previousElementSibling;
    }
    return sibling ? sibling.querySelector('.watch-card__link') : null;
  }

  function focusAfterRow(row) {
    var other = row.nextElementSibling || row.previousElementSibling;
    return (other && other.querySelector('.watch-row__scroll')) || byId('watch-search');
  }

  /* Take what a Keep ends off the page. Returns { undo, focus }: undo puts it
     all back (and returns the Keep button to focus), focus is where focus
     should go now. */
  function applyKept(id, days) {
    var undos = [];
    var focus = null;

    // A title page's "Leaving in N days · Keep" line says how long it is kept.
    triggersFor(id).forEach(function (trigger) {
      var line = trigger.closest('[data-watch-leaving]');
      if (!line) return;
      var parts = toArray(line.childNodes);
      parts.forEach(function (n) { n.remove(); });
      line.textContent = 'Kept for ' + days + ' days';
      line.tabIndex = -1;
      focus = line;
      undos.push(function () {
        line.textContent = '';
        parts.forEach(function (n) { line.appendChild(n); });
        line.removeAttribute('tabindex');
        return trigger;
      });
    });

    cardsFor(id).forEach(function (card) {
      var row = card.closest('[data-watch-leaving-row]');
      if (row) {
        // The Leaving soon card shrinks away; its row too when it was the last.
        var scroller = card.parentNode;
        var left = toArray(scroller.children).filter(function (c) {
          return c !== card && !c.classList.contains('is-kept');
        });
        var gone = left.length ? card : row;
        // Behind search results the row is hidden: it just goes, and focus
        // stays with what can be seen.
        var shown = card.getClientRects().length > 0;
        if (!focus && shown) focus = left.length ? focusAfterCard(card) : focusAfterRow(row);
        var spot = { node: gone, parent: gone.parentNode, next: gone.nextSibling };
        card.classList.add('is-kept');
        card.inert = true;
        var cancel = shown ? collapseAway(gone, gone === card) : (gone.remove(), function () {});
        undos.push(function () {
          cancel();
          card.classList.remove('is-kept');
          card.inert = false;
          if (!gone.parentNode) {
            putBack(spot);
            if (gone.getClientRects().length) growIn(gone, gone === card);
          }
          return card.querySelector('[data-watch-keep]');
        });
        return;
      }
      // Anywhere else the card stays, without its leaving badge or Keep.
      var spots = toArray(card.querySelectorAll('.watch-badge--leaving, [data-watch-keep]')).map(takeOut);
      if (!focus && spots.length) focus = card.querySelector('.watch-card__link');
      undos.push(function () {
        var back = null;
        spots.forEach(function (s) {
          putBack(s);
          if (s.node.hasAttribute('data-watch-keep')) back = s.node;
        });
        return back;
      });
    });

    return {
      focus: focus,
      undo: function () {
        var back = null;
        undos.forEach(function (fn) { back = fn() || back; });
        return back;
      }
    };
  }

  /* Move focus on only when it would otherwise be lost: it was on the Keep
     button or the menu, or has already fallen to the page. */
  function focusLost(trigger) {
    var active = document.activeElement;
    return !active || active === document.body || active === trigger ||
      (menu && menu.contains(active)) || !document.body.contains(active);
  }

  function keep(trigger, days) {
    var id = trigger.getAttribute('data-watch-keep');
    if (busy[id]) return;
    setBusy(id, true);
    MM.api.post(KEEP_URL, { media_item_id: id, days: days }).then(function () {
      setBusy(id, false);
      var moveFocus = focusLost(trigger);
      var done = applyKept(id, days);
      if (moveFocus && done.focus) done.focus.focus();
      toast('Kept for ' + days + ' days', {
        variant: 'success',
        action: { label: 'Undo', onClick: function () { undoKeep(id, days, done); } }
      });
    }).catch(function (err) {
      setBusy(id, false);
      toast(MM.api.messageOf(err, 'Could not keep this. Try again.'), { variant: 'error' });
    });
  }

  /* Undo puts the title back at once and asks the server after; if the
     server says no, the Keep stands and is taken off the page again. */
  function undoKeep(id, days, done) {
    if (busy[id]) return;
    var back = done.undo();
    setBusy(id, true);
    var active = document.activeElement;
    if (back && (!active || active === document.body || active === done.focus ||
                 (active.closest && active.closest('.ui-toast')))) {
      back.focus();
    }
    MM.api.post(UNKEEP_URL, { media_item_id: id }).then(function () {
      setBusy(id, false);
      toast('Keep undone', { variant: 'info' });
    }).catch(function (err) {
      setBusy(id, false);
      var trigger = back || triggersFor(id)[0];
      var moveFocus = trigger ? focusLost(trigger) : false;
      var again = applyKept(id, days);
      if (moveFocus && again.focus) again.focus.focus();
      toast(MM.api.messageOf(err, 'Could not undo. It stays kept for ' + days + ' days.'), { variant: 'error' });
    });
  }

  if (menu) {
    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target.closest) return;
      var trigger = target.closest('[data-watch-keep]');
      if (trigger) {
        if (busy[trigger.getAttribute('data-watch-keep')]) return;
        if (openTrigger === trigger) closeMenu(false); else openMenu(trigger);
        return;
      }
      var item = target.closest('#watch-keep-menu [data-days]');
      if (item && openTrigger) {
        var chosen = openTrigger;
        closeMenu(true);
        keep(chosen, parseInt(item.getAttribute('data-days'), 10));
        return;
      }
      closeMenu(false);
    });

    document.addEventListener('keydown', function (e) {
      if (!openTrigger) return;
      if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return; }
      // Back on the trigger first, so Tab carries on from there.
      if (e.key === 'Tab') { closeMenu(true); return; }
      var items = menuItems();
      var idx = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') items[(idx + 1) % items.length].focus();
      else if (e.key === 'ArrowUp') items[(idx - 1 + items.length) % items.length].focus();
      else if (e.key === 'Home') items[0].focus();
      else if (e.key === 'End') items[items.length - 1].focus();
      else return;
      e.preventDefault();
    });

    // The menu is fixed, so it follows its trigger through any scroll or
    // resize, and closes only once the trigger is out of sight.
    var queued = false;
    function follow() {
      if (!openTrigger || queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        if (!openTrigger) return;
        if (inView(openTrigger)) placeMenu(openTrigger); else closeMenu(false);
      });
    }
    window.addEventListener('resize', follow);
    document.addEventListener('scroll', follow, { capture: true, passive: true });
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
      if (busy[item.keep_id]) {
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

    function setPending(on) {
      box.classList.toggle('is-pending', on);
      results.setAttribute('aria-busy', on ? 'true' : 'false');
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
      msg.classList.remove('watch-results__msg--error');
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
      msg.classList.remove('watch-results__msg--error');
    }

    function showFound(q, items) {
      showResults();
      items.forEach(function (item) { grid.appendChild(buildCard(item)); });
      if (!items.length) msg.textContent = 'Nothing found for “' + q + '”.';
      status.textContent = items.length
        ? (items.length === 1 ? '1 result' : items.length + ' results') + ' for “' + q + '”'
        : msg.textContent;
    }

    function showFailed(text) {
      showResults();
      msg.classList.add('watch-results__msg--error');
      msg.appendChild(el('span', '', text));
      var retry = el('button', 'watch-results__retry', 'Try again');
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

  /* ── Season tabs (show page) ── */

  /* A season tab swaps the episode list in place: no reload, no jump to the
     top, focus stays on the tab. The address changes too, so Back and a
     shared link work; any trouble falls back to loading the page. */
  function initSeasons() {
    var nav = document.querySelector('.watch-seasons');
    var list = document.querySelector('.watch-episodes');
    if (!nav || !list || !window.fetch || !window.DOMParser || !window.history.pushState) return;
    var loading = null;
    var current = null;

    function pills() { return toArray(nav.querySelectorAll('a.watch-pill')); }

    function select(href) {
      current = href;
      pills().forEach(function (pill) {
        var on = pill.getAttribute('href') === href;
        pill.classList.toggle('on', on);
        if (on) pill.setAttribute('aria-current', 'page'); else pill.removeAttribute('aria-current');
      });
    }

    function swapLeaving(doc) {
      var oldLine = document.querySelector('.watch-hero__info [data-watch-leaving]');
      var newLine = doc.querySelector('.watch-hero__info [data-watch-leaving]');
      var fresh = newLine ? document.importNode(newLine, true) : null;
      if (oldLine && fresh) oldLine.replaceWith(fresh);
      else if (oldLine) oldLine.remove();
      else if (fresh) document.querySelector('.watch-hero__info .watch-actions').before(fresh);
    }

    function load(href, push) {
      if (loading) loading.abort();
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
          closeMenu(false);
          swapLeaving(doc);
          var fresh = document.importNode(newList, true);
          list.replaceWith(fresh);
          list = fresh;
          fadeIn(list);
          select(on.getAttribute('href'));
          if (push) window.history.pushState({ watchSeason: href }, '', href);
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return;
          window.location.assign(href);
        });
    }

    var first = nav.querySelector('a.watch-pill.on');
    current = first ? first.getAttribute('href') : null;

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

  initSearch();
  initSeasons();
})();
