/* Watch: All titles, the whole library as one grid with filters and orders, on
 * MMWatch.library. For an admin its cards carry Keep and Delete in their More
 * menu (watch/admin.js does them), and each change brings its card up to date
 * where it is.
 *
 *   durations(card, node, item), menu(card, node, item), act(card, act, item)
 *                    what watch/marks.js puts in a card's menu, and does of it
 *   open(href, hooks), show(href)   go to it from what stands over the page
 *
 * Needs watch/core.js, watch/grid.js, watch/expand.js and watch/search.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var grid = watch.grid;
  var el = core.el;
  var toArray = core.toArray;

  if (!watch.expand) return;

  var URL = '/api/watch/library/library/row';
  var PAGE = 60;
  var SLOW_MS = 400; // how long what leads here waits for the titles before it is left without them
  var FILTERS = [['', 'All'], ['movies', 'Movies'], ['tv', 'TV'], ['anime', 'Anime'], ['unseen', 'Not watched'],
    ['kept', 'Kept', true], ['stale', 'Stale', true]];
  var DURATIONS = [['7 days', 'Keep 7 days'], ['30 days', 'Keep 30 days'], ['90 days', 'Keep 90 days'], ['forever', 'Keep forever']];
  var WORKING = { keep: 'Keeping…', show: 'Keeping…', unkeep: 'Removing…', 'delete': 'Deleting…' };

  function pageData() {
    var island = core.byId('watch-page-data');
    try { return JSON.parse(island ? island.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  var data = pageData();
  var isAdmin = data.is_admin === true;

  /* ── Cards ── */

  function build(item) {
    var card = watch.search.card(item);
    var link = card.querySelector('.watch-card__link');
    card.setAttribute('data-key', item.key);
    if (item.tone) card.setAttribute('data-tone', item.tone);
    if (item.admin) card.setAttribute('data-admin', JSON.stringify(item.admin));
    if (!item.href) {
      // Watch cannot open it; its menu has what can be done of it.
      link.removeAttribute('href');
      link.tabIndex = 0;
      if (item.admin) card.appendChild(more(item.title));
    }
    showWorking(card);
    return card;
  }

  function more(title) {
    var b = el('button', 'watch-more');
    b.type = 'button';
    b.setAttribute('data-watch-more', '');
    b.setAttribute('aria-haspopup', 'menu');
    b.setAttribute('aria-expanded', 'false');
    b.setAttribute('aria-label', 'More for ' + title);
    b.appendChild(window.MM.dom.icon('i-ellipsis'));
    return b;
  }

  /* Have *card* say what *fresh*, the same title as it is now, says. */
  function refresh(card, fresh) {
    ['data-key', 'data-tone', 'data-admin', 'data-keep-id', 'data-leaving-id', 'data-leaving-seasons'].forEach(function (name) {
      if (fresh.hasAttribute(name)) card.setAttribute(name, fresh.getAttribute(name));
      else card.removeAttribute(name);
    });
    var poster = card.querySelector('.watch-card__poster');
    var now = fresh.querySelector('.watch-card__poster');
    ['.watch-badge--leaving', '.watch-badge--watched', '.watch-bar'].forEach(function (name) {
      var old = poster.querySelector(name);
      var next = now.querySelector(name);
      if (old && next) old.replaceWith(next);
      else if (old) old.remove();
      else if (next) poster.appendChild(next);
    });
    labels.delete(card);
    card.querySelector('.watch-card__title').textContent = fresh.querySelector('.watch-card__title').textContent;
    card.querySelector('.watch-card__sub').replaceWith(fresh.querySelector('.watch-card__sub'));
    showWorking(card);
  }

  function adminOf(card) {
    try { return JSON.parse(card.getAttribute('data-admin')); } catch (e) { return null; }
  }

  /* The cards of *key* in the open grid, none on its way out. */
  function cardsOf(key) {
    return toArray(document.querySelectorAll('.watch-all .watch-card')).filter(function (card) {
      return card.getAttribute('data-key') === key && !card.classList.contains('is-gone');
    });
  }

  function moreOf(card) { return card.querySelector('[data-watch-more]'); }

  /* ── Pages ── */

  var version = 0; // every change that is saved
  var changed = {}; // at which of them each title was changed last

  function ask(sort, type, more) {
    var asked = version;
    return window.MM.api.get(URL + '?limit=' + PAGE + (type ? '&type=' + type : '') + (sort === 'added' ? '' : '&sort=' + sort) + (more || ''))
      .then(function (got) { return Object.assign(got, { asked: asked }); });
  }

  function isOld(got, key) { return changed[key] > got.asked; }

  /* Read again each title of *got*, which stands now, that was read before a change to it was saved. */
  function readChanged(got) {
    got.items.forEach(function (item) { if (isOld(got, item.key)) setTimeout(function () { readAgain(item.key, {}); }); });
  }

  function barOf(got) { return { type: got.type, counts: got.counts, fact: got.fact, total: got.total }; }

  /* *empty*, of the grid the pages are for: what it says with nothing in it is told by the first of them. */
  function pages(sort, type, empty) {
    return function (after) {
      return ask(sort, type, after ? '&after=' + encodeURIComponent(after) : '').then(function (got) {
        var now = grid.shows('library');
        if (now && now.sort === sort) grid.setBar(barOf(got));
        if (empty) Object.assign(empty, got.empty);
        readChanged(got);
        return { cards: got.items.map(build), next: got.next, total: got.total };
      });
    };
  }

  function content(sort, type) {
    var page = pages(sort, type);
    return ask(sort, type).then(function (got) { return made(got, page); });
  }

  function made(got, page) {
    return { cards: got.items.map(build), next: got.next, total: got.total, bar: barOf(got), fetchPage: page, empty: got.empty, placed: function () { readChanged(got); } };
  }

  function href(type, sort) {
    return '/watch?all=library' + (type ? '&type=' + type : '') + (sort === 'added' ? '' : '&sort=' + sort);
  }

  watch.expand.source('library', function (at) {
    var got = at.drawn || at.ready;
    var empty = Object.assign({ title: 'Nothing here' }, got && got.empty);
    var opts = {
      fetchPage: pages(at.sort, at.type, empty),
      empty: empty,
      refresh: refresh,
      total: got ? got.total : null,
      filter: {
        label: 'Show', value: at.type, href: href,
        options: FILTERS.filter(function (f) { return isAdmin || !f[2]; }).map(function (f) { return { id: f[0], label: f[1], admin: !!f[2] }; }),
        counts: got ? got.counts : null, // Its place is held, so that nothing moves as it comes.
        fact: got ? got.fact : isAdmin ? '\u00a0' : null
      },
      sort: { label: 'Sort', choose: content }
    };
    if (at.drawn) opts.next = at.drawn.next;
    else if (at.ready) {
      Object.assign(opts, { cards: at.ready.items.map(build), next: at.ready.next });
      readChanged(at.ready);
    } else if (at.row && at.sort === 'added') {
      // The row's own cards travel; the rest of the first page, and its facts, come after them.
      opts.cards = at.cards();
      opts.next = null;
      opts.waiting = true;
      opts.total = Number(at.row.getAttribute('data-row-total'));
      opts.later = function () { return content(at.sort, at.type); };
    } else Object.assign(opts, { cards: [], next: '' });
    return opts;
  });

  /* ── The menu of a card ── */

  function line(node, icon, text, className) {
    var p = node.appendChild(el('p', className));
    if (icon) p.appendChild(window.MM.dom.icon(icon));
    p.appendChild(document.createTextNode(text));
  }

  /* How long to keep a film, in the same menu. Whether it was that. */
  function durations(card, node, item) {
    var a = node && adminOf(card);
    if (!a) return false;
    node.setAttribute('aria-label', 'Keep ' + a.title + ' for');
    DURATIONS.forEach(function (d) {
      var choice = item('admin-keep', 'i-clock-regular', d[1]);
      choice.setAttribute('data-keep-dur', d[0]);
      node.appendChild(choice);
    });
    return true;
  }

  function menu(card, node, item) {
    var a = adminOf(card);
    if (!a) return;
    line(node, 'i-lock', 'Admins only', 'watch-menu-label');
    line(node, null, a.facts, 'watch-menu-facts');
    // A show's dialog has how long, and which seasons.
    node.appendChild(item(a.is_tv ? 'admin-keep' : 'keep-for', 'i-check', a.kept ? 'Change keep…' : 'Keep…'));
    if (a.unkeep) node.appendChild(item('admin-unkeep', 'i-xmark', 'Remove keep' + (a.unkeep === 'dialog' ? '…' : '')));
    if (a['delete']) {
      var del = item('admin-delete', 'i-trash', 'Delete…');
      del.classList.add('is-danger');
      node.appendChild(del);
    }
  }

  function act(card, what, item) {
    var a = adminOf(card);
    var admin = watch.admin;
    if (!a || !admin) return;
    if (what === 'admin-keep') admin.keep(a, item.getAttribute('data-keep-dur') || '30 days');
    else if (what === 'admin-unkeep') {
      /* The dialog's Remove all clears the show's and every season's keep. */
      if (a.unkeep === 'dialog') admin.keep(a);
      else admin.removeKeep(a, a.id);
    } else if (!a['delete']) {
      // Kept while its menu was open.
      window.UIFeedback.toast(a.title + ' is kept now, so it can’t be deleted.');
    } else if (a['delete'].id === undefined) {
      admin.pickSeasonDelete(a['delete'].show_rk, a.title, a.seasons || 0);
    } else {
      admin.confirmDelete(a['delete'].id, a.title, { kind: a['delete'].kind, size: a['delete'].size, showRk: a.is_tv ? a.show_rk : '' });
    }
  }

  /* ── While a change is made, and after it ── */

  var working = Object.create(null); // the admin actions still running, by the key of their title
  var labels = new WeakMap(); // what a caption says while nothing runs

  function focusIsFree() {
    var active = document.activeElement;
    if (document.documentElement.classList.contains('has-ttl') || document.querySelector('.wl-dialog.active')) return false;
    return !active || active === document.body || !!active.closest('.ui-toast.is-leaving');
  }

  /* Draw the card for the actions still running on it: its caption says what the first of them is doing. */
  function showWorking(card) {
    var running = working[card.getAttribute('data-key')];
    var sub = card.querySelector('.watch-card__sub');
    var button = moreOf(card);
    card.classList.toggle('is-working', !!running);
    if (running) {
      if (!labels.has(card)) labels.set(card, toArray(sub.childNodes));
      var spin = el('span', 'btn-spinner');
      spin.setAttribute('aria-hidden', 'true');
      sub.replaceChildren(spin, document.createTextNode(WORKING[running[0]] || 'Saving…'));
    } else if (labels.has(card)) {
      sub.replaceChildren.apply(sub, labels.get(card));
      labels.delete(card);
    }
    if (!button) return;
    button.setAttribute('aria-busy', running ? 'true' : 'false');
    if (running) button.setAttribute('aria-disabled', 'true');
    else button.removeAttribute('aria-disabled');
  }

  /* Note that an action on a title starts or ends. */
  function note(key, d) {
    var running = working[key] || [];
    if (d.phase === 'busy') running.push(d.action);
    else running.splice(Math.max(0, running.indexOf(d.action)), 1);
    if (running.length) working[key] = running;
    else delete working[key];
  }

  /* The focus of an Undo, which its toast takes along: to the title it is of. */
  function focusOn(card) {
    if (card && focusIsFree()) moreOf(card).focus({ preventScroll: true });
  }

  function leave(card) {
    var had = card.contains(document.activeElement) || focusIsFree();
    var taken = grid.take(card, '[data-watch-more]');
    if (taken && had && taken.focus) core.focusSoftly(taken.focus);
    // A dialog opened from it has the focus to give back.
    if (taken && window.ModalA11y) window.ModalA11y.handOn(function (from) { return card.contains(from) ? taken.focus : null; });
  }

  /* Read *key* again as it stands after the action *d* and bring its card up to date where it is. One
     the filter no longer holds stays until the grid is filled anew, so its Undo still has a card. */
  function readAgain(key, d) {
    var now = grid.shows('library');
    if (!now) return Promise.resolve();
    return ask(now.sort, now.filter, '&only=' + encodeURIComponent(key)).then(function (got) {
      var still = grid.shows('library');
      if (!still || isOld(got, key)) return;
      if (still.sort === now.sort && still.filter === now.filter) grid.setBar(barOf(got));
      cardsOf(key).forEach(function (card) {
        if (got.items.length) refresh(card, build(got.items[0]));
        else leave(card);
        focusOn(got.items.length ? card : null);
      });
    }, function () {
      cardsOf(key).forEach(function (card) { letGo(card, d); });
      window.UIFeedback.error('Couldn’t read the title again. Reload to see the change.');
    });
  }

  /* The change is saved but the title as it now is could not be read: a saved keep shows as kept. */
  function letGo(card, d) {
    var a = adminOf(card);
    if (a) {
      // A film's; of a show's seasons (show) only the server can tell.
      if (d.action === 'keep') Object.assign(a, { kept: true, unkeep: 'item', 'delete': null });
      if (d.keep !== undefined) a.keep = d.keep;
      card.setAttribute('data-admin', JSON.stringify(a));
    }
    showWorking(card);
  }

  document.addEventListener('mm:watch-admin', function (e) {
    var d = e.detail || {};
    var key = d.showRk ? 'show:' + d.showRk : 'id:' + d.id;
    if (d.phase === 'done') {
      version++;
      changed[key] = version;
    }
    note(key, d);
    var cards = cardsOf(key);
    if (d.phase !== 'done') {
      cards.forEach(showWorking);
      if (d.phase === 'busy' && d.action === 'unkeep') Promise.resolve().then(function () { focusOn(cards[0]); });
      return;
    }
    if (d.action === 'delete' && !d.showRk) {
      // Details, where it is open, takes it out itself.
      Promise.resolve().then(function () { cardsOf(key).forEach(leave); });
    }
    readAgain(key, d);
  });

  /* What is scheduled has changed: every card that stands here is read again. */
  document.addEventListener('mm:watch-scheduled', function () {
    var now = grid.shows('library');
    if (!now) return;
    var left = document.querySelectorAll('.watch-all .watch-card').length;
    version++;
    (function next(after) {
      ask(now.sort, now.filter, after ? '&after=' + encodeURIComponent(after) : '').then(function (got) {
        var still = grid.shows('library');
        if (!still || still.sort !== now.sort || still.filter !== now.filter) return;
        grid.setBar(barOf(got));
        // One changed since has been read again by itself.
        got.items.forEach(function (item) { if (!isOld(got, item.key)) cardsOf(item.key).forEach(function (card) { refresh(card, build(item)); }); });
        left -= got.items.length;
        if (left > 0 && got.next) next(got.next);
      }, function () {});
    })('');
  });

  /* A mark changes how many are not watched; marks.js draws the card. */
  document.addEventListener('mm:watch-marked', function () {
    var now = grid.shows('library');
    if (!now) return;
    ask(now.sort, now.filter, '&only=none').then(function (got) {
      var still = grid.shows('library');
      if (still && still.filter === now.filter) grid.setBar(barOf(got));
    }, function () {});
  });

  /* ── Going to it from what stands over the page ── */

  function stateOf(address) {
    var params = new URLSearchParams(address.slice(address.indexOf('?') + 1));
    return { sort: params.get('sort') || 'added', type: params.get('type') || '' };
  }

  function show(address) {
    var to = stateOf(address);
    watch.expand.library(to.sort, to.type, { still: true });
  }

  /* Go to All titles at *address* once its titles are in or are slow to come. hooks.leave(): as the
     page is about to change; hooks.arrive(): they are on view; hooks.fail(): they could not be had,
     and nothing has changed. Returns what calls it off. */
  function open(address, hooks) {
    var to = stateOf(address);
    var settled = false;
    var timer = setTimeout(function () { go(null); }, SLOW_MS);

    function go(got) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      var before = grid.shows('library');
      hooks.leave();
      watch.expand.library(to.sort, to.type, { still: true, ready: got });
      if (got && !before) hooks.arrive();
    }

    ask(to.sort, to.type).then(go, function () {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      hooks.fail();
    });
    return function () {
      settled = true;
      clearTimeout(timer);
    };
  }

  watch.library = { durations: durations, menu: menu, act: act, open: open, show: show };
})();
