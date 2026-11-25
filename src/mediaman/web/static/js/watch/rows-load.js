/* Watch: the one loader every poster row shares. Near the end of a row it
 * fetches the next page and adds it, one request at a time, until there are no
 * more; a title already in the row is never added again. Far-off posters are
 * unloaded, so a very long row stays smooth. Needs watch/core.js.
 *
 *   MMWatch.rowsLoad.start(viewport, { title, next, fetchPage })
 *
 * *viewport* is a .watch-row__viewport whose cards are already in it, *next*
 * where to go on from (null: the row is complete) and fetchPage(next) a promise
 * of { cards, next }: card nodes, each with its data-title-ref link, and where
 * to go on from after them. Returns { stop }, for a row that is taken away,
 * with next(), where it is to go on from, more(), which loads it now and says
 * whether a page is on its way, and check(), for a view the loader cannot see
 * moving. opts.took() is called after each page, opts.lost(told) when one could
 * not be had; told: the person was told.
 *
 * Cards laid out otherwise than in a sideways strip come with opts.view:
 *   scroller        the node that holds the cards
 *   near()          whether the last card is near the screen
 *   placeholders()  how many to show while a page loads
 *   range(cards)    the cards, [from, to), whose posters stay loaded
 *   failed(button)  show Try again; the placeholders stay
 *   took(fresh, done)  the cards just added, and whether they were the last
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;

  var AHEAD_VIEWS = 1.5; // the next page is asked for this near the last card
  var AWAKE_VIEWS = 3; // cards further off than this have their posters unloaded
  var PLACEHOLDERS = 6;
  var EMPTY_LIMIT = 8; // pages in a row with nothing new: the row is finished
  var BACKOFF_MS = [5000, 10000, 20000, 40000, 60000]; // after a 429; then Try again
  var SCROLL_MS = 600; // past any smooth scroll of a few cards

  var rows = new WeakMap();

  /* ── The titles a row holds ── */

  /* What a card is known by: its title's ref, but each season of a show (Leaving
     soon) is a card of its own. Null: it has neither. */
  function keyOf(card) {
    var link = card.querySelector('[data-title-ref]');
    var mark = card.getAttribute('data-mark');
    return /^season\//.test(mark) ? mark : link ? link.getAttribute('data-title-ref') : null;
  }

  /* What a ref is numbered by. Two refs of one source with different numbers
     are two titles, except picks, which are numbered anew in every batch. */
  function sourceOf(ref) {
    return /^tmdb-/.test(ref) ? 'tmdb' : /^(rec|pick)-/.test(ref) ? 'rec' : 'library';
  }

  function textOf(card, selector) {
    var node = card.querySelector(selector);
    return node ? node.textContent.trim().toLowerCase() : '';
  }

  /* The TMDB title a card says it is of, kind and number; null when it does not say. */
  function tmdbOf(link) {
    var ref = /^tmdb-(movie|tv)-([0-9]+)$/.exec(link.getAttribute('data-title-ref'));
    if (ref) return ref[1] + '-' + ref[2];
    var id = link.getAttribute('data-tmdb-id');
    return id ? (link.getAttribute('data-kind') === 'movie' ? 'movie-' : 'tv-') + id : null;
  }

  /* A filter that lets each card through once: the same key, the same TMDB
     title, or the same title, kind and year where neither ids nor refs can tell. */
  function onlyNew() {
    var keys = new Set();
    var ids = new Set();
    var names = new Map();
    return function isNew(card) {
      var link = card.querySelector('[data-title-ref]');
      if (!link) return true;
      var key = keyOf(card);
      var id = tmdbOf(link);
      if (keys.has(key) || (id && ids.has(id))) return false;
      var source = sourceOf(link.getAttribute('data-title-ref'));
      var name = textOf(card, '.watch-card__title') + '\n' + textOf(card, '.watch-card__sub');
      var named = names.get(name) || [];
      if (named.some(function (n) { return !(id && n.id) && (source === 'rec' || n.source !== source); })) return false;
      keys.add(key);
      if (id) ids.add(id);
      names.set(name, named.concat({ source: source, id: id }));
      return true;
    };
  }

  /* ── Posters ── */

  function unload(card) {
    card.setAttribute('data-far', '');
    var img = card.querySelector('.watch-card__poster img');
    if (!img || !img.hasAttribute('src') || img.hasAttribute('data-broken')) return;
    img.setAttribute('data-far-src', img.getAttribute('src'));
    img.removeAttribute('src');
  }

  function reload(card) {
    card.removeAttribute('data-far');
    var img = card.querySelector('.watch-card__poster img');
    if (!img || !img.hasAttribute('data-far-src')) return;
    img.setAttribute('src', img.getAttribute('data-far-src'));
    img.removeAttribute('data-far-src');
  }

  /* ── One row ── */

  function start(viewport, opts) {
    var view = opts.view || null;
    var scroller = view ? view.scroller : viewport.querySelector('.watch-row__scroll');
    var isNew = onlyNew();
    var cards = [];
    core.toArray(scroller.querySelectorAll('.watch-card')).forEach(function (card) {
      if (isNew(card)) cards.push(card);
      else card.remove();
    });
    var next = opts.next === undefined ? null : opts.next;
    var state = 'idle'; // idle, loading, waiting (after a 429), failed, done
    var tail = []; // what stands after the last card: placeholders, or Try again
    var retryButton = null;
    var empties = 0;
    var waits = 0;
    var timer = null;
    var onScreen = !('IntersectionObserver' in window);
    var stopped = false;
    var awake = new Set(cards);

    function present() { return cards.filter(function (card) { return card.parentNode === scroller; }); }

    function step() {
      var here = present();
      return here.length > 1 ? here[1].offsetLeft - here[0].offsetLeft : 0;
    }

    /* Whether the end of the cards, not of what stands after them, is near. */
    function near() {
      if (view) return view.near();
      var left = scroller.scrollWidth - scroller.clientWidth - scroller.scrollLeft - tail.length * step();
      return left < AHEAD_VIEWS * scroller.clientWidth;
    }

    function shown() {
      return onScreen && document.body.contains(viewport) && scroller.clientWidth > 0;
    }

    function check() {
      if (!stopped && state === 'idle' && shown() && near()) load();
    }

    /* ── What stands after the last card ── */

    function addPlaceholders() {
      var wanted = view ? view.placeholders() : PLACEHOLDERS;
      while (tail.length < wanted) {
        var skel = el('div', 'watch-card--skel');
        skel.setAttribute('aria-hidden', 'true');
        skel.appendChild(el('span', 'skel skel--poster'));
        skel.appendChild(el('span', 'skel skel--line'));
        scroller.appendChild(skel);
        tail.push(skel);
      }
    }

    /* Someone looking at what goes is slid back to the last card first, so the
       row does not jump under them. */
    function clearTail() {
      var nodes = tail;
      tail = [];
      if (!nodes.length) return;
      function remove() { nodes.forEach(function (n) { n.remove(); }); }
      if (view) { remove(); return; }
      var max = scroller.scrollWidth - nodes.length * step() - scroller.clientWidth;
      if (!(scroller.scrollLeft > max + 1) || core.reduceMotion()) { remove(); return; }
      scroller.scrollTo({ left: Math.max(0, max), behavior: 'smooth' });
      scroller.addEventListener('scrollend', remove, { once: true });
      setTimeout(remove, SCROLL_MS);
    }

    function showFailed() {
      retryButton = el('button', 'watch-row__retry', 'Try again');
      retryButton.type = 'button';
      retryButton.setAttribute('aria-label', 'Try again to load more of ' + opts.title);
      retryButton.addEventListener('click', load);
      if (view) { view.failed(retryButton); return; }
      var tile = el('div', 'watch-row__more');
      tile.appendChild(el('span', null, 'Couldn’t load more.'));
      tile.appendChild(retryButton);
      var first = tail.shift();
      if (first) first.replaceWith(tile);
      else scroller.appendChild(tile);
      clearTail();
      tail = [tile];
    }

    /* ── Loading ── */

    function load() {
      if (state === 'loading' || state === 'done') return;
      state = 'loading';
      scroller.setAttribute('aria-busy', 'true');
      if (retryButton) {
        retryButton.setAttribute('aria-disabled', 'true');
        retryButton.setAttribute('aria-busy', 'true');
      } else {
        addPlaceholders();
      }
      opts.fetchPage(next).then(function (page) { if (!stopped) took(page); }, function (err) {
        if (!stopped) failed(err);
      });
    }

    function settle(to) {
      state = to;
      scroller.removeAttribute('aria-busy');
      if (next === null) viewport.removeAttribute('data-row-more');
      else viewport.setAttribute('data-row-more', '');
    }

    function took(page) {
      var fresh = page.cards.filter(isNew);
      empties = fresh.length ? 0 : empties + 1;
      waits = 0;
      var after = page.next === undefined || page.next === next ? null : page.next;
      next = empties < EMPTY_LIMIT ? after : null;
      var tile = retryButton;
      var refocus = tile && document.activeElement === tile;
      retryButton = null;
      place(fresh, !!tile && !view);
      if (view) view.took(fresh, next === null);
      if (refocus) {
        var links = fresh.map(function (c) { return c.querySelector('.watch-card__link'); }).filter(Boolean);
        (links[0] || scroller).focus({ preventScroll: true });
      }
      settle(next === null ? 'done' : 'idle');
      if (next !== null && shown() && near()) load();
      else clearTail();
      if (opts.took) opts.took();
    }

    /* Each card takes the place of what stood after the last one, in order. */
    function place(fresh, hadTile) {
      if (hadTile && !fresh.length) tail.shift().remove();
      fresh.forEach(function (card) {
        if (!view) {
          card.classList.add('is-arriving');
          card.addEventListener('animationend', function () { card.classList.remove('is-arriving'); }, { once: true });
        }
        var held = tail.shift();
        if (held) held.replaceWith(card);
        else scroller.appendChild(card);
        cards.push(card);
        awake.add(card);
      });
      sweep();
    }

    function failed(err) {
      if (err && err.status === 429 && waits < BACKOFF_MS.length && !retryButton) {
        state = 'waiting';
        timer = setTimeout(function () {
          state = 'idle';
          if (shown() && near()) load();
          else { settle('idle'); clearTail(); }
        }, BACKOFF_MS[waits++]);
        if (opts.lost) opts.lost(false);
        return;
      }
      waits = 0;
      settle('failed');
      if (opts.lost) opts.lost(!!retryButton);
      if (!retryButton) { showFailed(); return; }
      retryButton.removeAttribute('aria-disabled');
      retryButton.setAttribute('aria-busy', 'false');
      core.toast('Still couldn’t load more. Try again in a moment.', { variant: 'error' });
    }

    /* ── Posters near and far ── */

    function inStrip(here) {
      var width = step();
      if (!width) return null;
      var seen = scroller.clientWidth;
      return [
        Math.max(0, Math.floor((scroller.scrollLeft - AWAKE_VIEWS * seen) / width)),
        Math.min(here.length, Math.ceil((scroller.scrollLeft + (AWAKE_VIEWS + 1) * seen) / width))
      ];
    }

    function sweep() {
      var here = present();
      var range = view ? view.range(here) : inStrip(here);
      if (!range) return;
      var keep = new Set(here.slice(range[0], range[1]));
      awake.forEach(function (card) { if (!keep.has(card)) unload(card); });
      keep.forEach(function (card) { if (!awake.has(card)) reload(card); });
      awake = keep;
    }

    /* ── Watching the row ── */

    var queued = false;
    function schedule() {
      if (queued || stopped) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        if (stopped) return;
        sweep();
        check();
      });
    }

    var observers = [];
    function watchWith(observer, node) {
      observer.observe(node);
      observers.push(observer);
    }
    // Only a row on the screen loads.
    if (!onScreen) {
      watchWith(new IntersectionObserver(function (entries) {
        onScreen = entries[entries.length - 1].isIntersecting;
        schedule();
      }, { rootMargin: '300px 0px' }), viewport);
    }
    if ('ResizeObserver' in window) watchWith(new ResizeObserver(schedule), scroller);
    else window.addEventListener('resize', schedule);
    scroller.addEventListener('scroll', schedule, { passive: true });

    var row = {
      stop: function () {
        stopped = true;
        clearTimeout(timer);
        observers.forEach(function (o) { o.disconnect(); });
        window.removeEventListener('resize', schedule);
        scroller.removeEventListener('scroll', schedule);
      },
      forward: function () {
        if (state === 'failed') load();
      },
      next: function () { return next; },
      more: function () {
        if (state === 'idle') load();
        return state === 'loading';
      },
      check: schedule
    };
    rows.set(viewport, row);
    settle(next === null ? 'done' : 'idle');
    schedule();
    return row;
  }

  /* The person asked for more past the last card: after a failure, try again. */
  function forward(viewport) {
    var row = rows.get(viewport);
    if (row) row.forward();
  }

  window.MMWatch.rowsLoad = { start: start, forward: forward, keyOf: keyOf };
})();
