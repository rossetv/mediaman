/* Watch: the search bar, its sheet of categories, the suggestions and the
 * results, in two groups: titles in the library, then titles from TMDB that are
 * not. Cards built here mirror templates/_watch_card.html. Nothing is built with
 * innerHTML: titles come from Plex and TMDB. In the admin list view the same
 * field only announces what was typed (mm:watch-list-search) and the list
 * searches itself. A category's or person's page (watch/explore.js) stands in
 * the results' place, on MMWatch.search.stage.
 * Needs watch/core.js, watch/address.js and watch/keep.js; watch/explore.js when
 * it starts.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var byId = core.byId;
  var el = core.el;
  var safePath = core.safePath;

  var SEARCH_URL = '/api/watch/search?q=';
  var TMDB_URL = '/api/watch/search/tmdb?q=';
  var SEARCH_DELAY_MS = 200;
  var LIST_DELAY_MS = 350;
  var EXAMPLE_MS = 3200;
  var SKELETONS = 6;
  // TMDB answers nothing for less; do not ask.
  var TMDB_MIN_CHARS = 2;
  var SEARCH_FAILED = 'Couldn’t search just now.';
  var TMDB_FAILED = 'Couldn’t search for titles outside the library just now.';
  var LABEL = 'Search films and shows';
  var LIST_LABEL = 'Search your library';
  var EXAMPLES = ['popular action movies', 'an actor’s name', 'comedies to watch tonight', 'something scary'];
  var TRIES = ['popular action movies', 'popular comedy movies', 'top rated sci-fi'];
  var TRIES_EMPTY = ['popular action movies', 'popular comedy movies', 'something scary'];
  var LENS = 'M10.5 4a6.5 6.5 0 100 13 6.5 6.5 0 000-13M15.5 15.5L20 20';
  var PAGE_PARAMS = { genre: null, type: null, sort: null, where: null, decade: null, person: null, all: null };
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

  /* A library title's card; its link carries the ref: it is what takes focus
     back when the detail closes. */
  function libraryCard(item) {
    var card = el('article', 'watch-card');
    card.setAttribute('data-keep-id', item.keep_id || '');

    var link = el('a', 'watch-card__link');
    var at = /^\/watch\/(movie|show)\/([0-9]+)(\?|$)/.exec(item.href || '');
    var ref = item.ref || (at ? at[1] + '-' + at[2] : '');
    if (ref) link.setAttribute('data-title-ref', ref);
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
      var badge = el('span', 'watch-badge watch-badge--leaving' + (item.leaving_days <= 1 ? ' watch-badge--leaving-now' : ''));
      badge.appendChild(el('span', 'watch-badge__long', leavingText(item.leaving_days)));
      badge.appendChild(el('span', 'watch-badge__short', leavingShortText(item.leaving_days)));
      poster.appendChild(badge);
    }
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
    core.addMarks(card, poster, item);
    return card;
  }

  /* A search result also has its Keep. */
  function buildCard(item) {
    var card = libraryCard(item);
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

  /* A title TMDB knows, in the library or not; *plain*: its heading says which. */
  function buildTmdbCard(item, plain) {
    var card = el('article', 'watch-card');

    var link = el('a', 'watch-card__link');
    link.setAttribute('data-title-ref', item.ref || '');
    if (item.kind === 'movie' || item.kind === 'show') link.setAttribute('data-kind', item.kind);
    if (item.in_library) link.setAttribute('data-in-library', '');
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
    var badge = plain ? null : stateBadge(item);
    if (badge) poster.appendChild(badge);
    link.appendChild(poster);
    link.appendChild(el('span', 'watch-card__sub', kindLine(item)));
    card.appendChild(link);
    core.addMarks(card, poster, item);
    return card;
  }

  function kindLine(item) {
    return (item.kind === 'show' ? 'TV' : 'Movie') + (item.year ? ' · ' + item.year : '');
  }

  function stateBadge(item) {
    var state = item.download_state;
    if (item.in_library && state !== 'queued' && state !== 'downloading') state = 'in_library';
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

  /* ── Motion ── */

  function moves(node) { return !core.reduceMotion() && !!node.animate; }

  /* The spring of the open bar; a browser without linear() eases instead. */
  function spring() { return core.token('--ease-spring', core.token('--ease-out', 'ease-out')); }

  function play(node, frames, ms, opts) {
    return node.animate(frames, Object.assign({ duration: ms, easing: core.token('--ease-out', 'ease-out') }, opts));
  }

  function rise(node, delay) {
    if (!moves(node)) return;
    play(node, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], 220,
      { delay: delay || 0, fill: 'backwards' });
  }

  /* *node* starts from the box *from* on the screen and settles in its own place. */
  function grow(node, from) {
    if (!from || !from.height || !moves(node)) return;
    var to = node.getBoundingClientRect();
    if (!to.height) return;
    node.animate([
      { transform: 'translate(' + (from.left - to.left) + 'px, ' + (from.top - to.top) + 'px) scale(' + from.height / to.height + ')' },
      { transform: 'none' }
    ], { duration: 340, easing: spring() });
  }

  var KNOWN_BY = ['id', 'data-title-ref', 'data-watch-keep', 'data-keep-id', 'data-mark', 'data-mark-toggle'];

  /* *node*, off the page or a copy, set over *around* at *rect* to fade: a
     picture of what was there, in which nothing can be found or used. */
  function ghost(node, rect, around) {
    [node].concat(core.toArray(node.querySelectorAll('[id], [data-title-ref], [data-watch-keep], [data-keep-id], [data-mark], [data-mark-toggle]')))
      .forEach(function (n) { KNOWN_BY.forEach(function (name) { n.removeAttribute(name); }); });
    (node.getAnimations ? node.getAnimations() : []).forEach(function (a) { a.cancel(); });
    node.inert = true;
    node.setAttribute('aria-hidden', 'true');
    node.style.left = rect.left - around.left + 'px';
    node.style.top = rect.top - around.top + 'px';
    node.style.width = rect.width + 'px';
    return node;
  }

  function fadeAway(node, opacity, ms, shrink) {
    play(node, [{ opacity: opacity, transform: 'none' }, { opacity: 0, transform: shrink ? 'scale(.95)' : 'none' }], ms,
      { fill: 'forwards' }).onfinish = function () { node.remove(); };
  }

  var glides = new WeakMap();

  /* Make *root* hold *list*, [{ key, sig, make, update }], keeping the node of
     each key that stays. What stays glides from where it is on the screen this
     instant, even mid-flight; what is new rises; what is gone fades. A change of
     *sig* redraws a node where it stands. *first* changes the page around them,
     once it is known where they are. */
  function glide(root, list, first) {
    var g = glides.get(root);
    if (!g) {
      g = { nodes: new Map(), ghosts: el('div', 'results-ghosts') };
      g.ghosts.setAttribute('aria-hidden', 'true');
      g.ghosts.inert = true;
      root.parentNode.insertBefore(g.ghosts, root);
      glides.set(root, g);
    }
    var go = moves(root);
    var keep = new Set();
    list = list.filter(function (x) {
      if (keep.has(x.key)) return false;
      keep.add(x.key);
      return true;
    });

    if (go) {
      g.nodes.forEach(function (held) {
        held.rect = held.node.getBoundingClientRect();
        held.opacity = held.rising && held.rising.playState === 'running'
          ? parseFloat(getComputedStyle(held.node).opacity) : 1;
      });
    }
    if (first) first();
    var around = g.ghosts.getBoundingClientRect();
    g.nodes.forEach(function (held, key) {
      if (keep.has(key)) return;
      g.nodes.delete(key);
      held.node.remove();
      if (go && near(held.rect)) {
        g.ghosts.appendChild(ghost(held.node, held.rect, around));
        fadeAway(held.node, held.opacity * 0.9, 130, true);
      }
    });

    var fresh = [];
    list.forEach(function (x) {
      var held = g.nodes.get(x.key);
      if (!held) {
        held = { node: x.make() };
        g.nodes.set(x.key, held);
        fresh.push(held);
      } else if (held.sig !== x.sig) {
        stopMoving(held);
        var node = x.make();
        if (held.node.parentNode) held.node.replaceWith(node);
        held.node = node;
      }
      held.sig = x.sig;
      if (x.update) x.update(held.node);
    });
    var after = null;
    for (var i = list.length - 1; i >= 0; i--) {
      var node = g.nodes.get(list[i].key).node;
      if (node.parentNode !== root || node.nextSibling !== after) root.insertBefore(node, after);
      after = node;
    }
    if (!go) return;

    // All are measured before any moves, so the page is laid out once.
    var still = [];
    list.forEach(function (x) {
      var held = g.nodes.get(x.key);
      if (!held.rect || fresh.indexOf(held) > -1) return;
      stopMoving(held);
      still.push(held);
    });
    still.forEach(function (held) { held.to = held.node.getBoundingClientRect(); });
    fresh.forEach(function (held) { held.to = held.node.getBoundingClientRect(); });
    still.forEach(function (held) { slide(held, g.ghosts, around); });
    var n = 0;
    fresh.forEach(function (held) {
      if (!near(held.to)) return;
      held.rising = play(held.node, [{ opacity: 0, transform: 'translateY(14px) scale(.97)' }, { opacity: 1, transform: 'none' }], 240,
        { delay: Math.min(n++, 10) * 22, fill: 'backwards' });
    });
  }

  function near(rect) { return rect.bottom > -200 && rect.top < window.innerHeight + 200; }

  function stopMoving(held) {
    if (held.moving) held.moving.cancel();
    if (held.rising) held.rising.cancel();
    held.moving = held.rising = null;
  }

  function slide(held, ghosts, around) {
    var from = held.rect;
    var to = held.to;
    var opacity = held.opacity;
    var dx = from.left - to.left;
    var dy = from.top - to.top;
    if (Math.abs(dx) + Math.abs(dy) < 1 && opacity > 0.98) return;
    if (!near(from) && !near(to)) return;
    // A long trip would cross other posters: it leaves where it was and arrives in place.
    if (Math.abs(dx) > to.width * 2.1 || (Math.abs(dy) > 40 && Math.abs(dx) > to.width * 0.6) || Math.abs(dy) > 900) {
      var left = ghosts.appendChild(ghost(held.node.cloneNode(true), from, around));
      fadeAway(left, opacity, 160, false);
      opacity = 0;
      dx = Math.max(-20, Math.min(20, dx));
      dy = Math.max(-14, Math.min(14, dy));
    }
    held.moving = held.node.animate([
      { transform: 'translate(' + dx + 'px, ' + dy + 'px)', opacity: opacity },
      { transform: 'none', opacity: 1 }
    ], { duration: 300, easing: spring() });
  }

  /* ── Pieces of the results ── */

  function groupHead(title, count) {
    var head = el('div', 'results-group__head');
    var h = el('h2', 'results-group__title', title);
    // Takes focus after a retry has replaced the button that had it.
    h.tabIndex = -1;
    head.appendChild(h);
    head.appendChild(el('span', 'results-group__count'));
    setCount(head, count);
    return head;
  }

  function setCount(head, count) {
    head.querySelector('.results-group__count').textContent =
      typeof count === 'number' ? count + (count === 1 ? ' title' : ' titles') : '';
  }

  function skeleton() {
    var card = el('div', 'watch-card watch-card--skel');
    card.setAttribute('aria-hidden', 'true');
    card.appendChild(el('span', 'skel skel--poster'));
    card.appendChild(el('span', 'skel skel--line'));
    return card;
  }

  function failure(text, retry) {
    var line = el('p', 'results-fail');
    line.setAttribute('role', 'alert');
    line.appendChild(el('span', '', text));
    var again = el('button', 'watch-results__retry');
    again.type = 'button';
    again.appendChild(MM.dom.icon('i-rotate-right'));
    again.appendChild(document.createTextNode('Try again'));
    again.addEventListener('click', retry);
    line.appendChild(again);
    return line;
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

  function initials(name) {
    return String(name || '').split(/\s+/).map(function (w) { return w.charAt(0); }).slice(0, 2).join('');
  }

  /* A person's round photo, or their initials where there is none or it does not load. */
  function face(url, name) {
    var node = el('span', 'watch-face');
    node.setAttribute('aria-hidden', 'true');
    node.appendChild(name ? el('span', 'watch-face__letters', initials(name)) : MM.dom.icon('i-user'));
    var src = safePath(url);
    if (src) {
      var img = el('img');
      img.src = src;
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
      node.appendChild(img);
    }
    return node;
  }

  function small(url) {
    var node = el('span', 'watch-mini');
    var src = safePath(url);
    if (src) {
      var img = el('img');
      img.src = src;
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
      node.appendChild(img);
    }
    return node;
  }

  /* *text*, what was typed standing out in it where it begins a word. */
  function marked(text, typed) {
    var b = el('b');
    var at = typed ? text.toLowerCase().indexOf(typed) : -1;
    if (at === -1 || (at > 0 && !/[\s-]/.test(text.charAt(at - 1)))) {
      b.textContent = text;
      return b;
    }
    b.appendChild(document.createTextNode(text.slice(0, at)));
    b.appendChild(el('em', '', text.slice(at, at + typed.length)));
    b.appendChild(document.createTextNode(text.slice(at + typed.length)));
    return b;
  }

  function begins(text, typed) {
    return (' ' + String(text || '').toLowerCase().replace(/-/g, ' ')).indexOf(' ' + typed.replace(/-/g, ' ')) > -1;
  }

  /* A key of an input method. Safari sends the one that ends it after the end. */
  function composing(e) { return e.isComposing || e.keyCode === 229; }

  function readPageData() {
    var node = byId('watch-page-data');
    try { return JSON.parse(node ? node.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  function initSearch() {
    var input = byId('watch-search');
    var clear = byId('watch-search-clear');
    var browse = byId('watch-browse');
    var results = byId('watch-results');
    var status = byId('watch-search-status');
    var sheet = byId('watch-sheet');
    var veil = byId('watch-veil');
    var token = byId('watch-search-token');
    var hintText = byId('watch-search-hint');
    if (!input || !clear || !browse || !results || !status || !sheet || !veil || !token || !hintText) return;
    var box = input.closest('.watch-search');
    var page = byId('watch-page');
    var address = window.MMWatch.address;
    var explore = window.MMWatch.explore;
    var data = readPageData();

    var listView = data.is_admin === true && data.view === 'list';
    var view = results.hidden ? 'browse' : 'results'; // what stands under the bar: browse, results or page
    var timer = null;
    var hiding = null;
    var current = null; // the search being shown; a reply for any other is dropped
    var last = null; // the search shown last, for coming back to it
    var live = null; // what the results hold: { kind, node, ... }
    var tokened = false;
    var sheetOn = false;
    var sheetGenres = null; // the genres the sheet was drawn from
    var picked = null; // the option the arrow keys stand on
    var people = new Map(); // everybody a search has named, by id

    function canExplore() { return data.tmdb_configured === true && !listView; }

    /* ── The bar ── */

    function focused() { return document.activeElement === input; }

    function sync() {
      var on = focused();
      box.classList.toggle('is-focus', on);
      box.classList.toggle('has-value', !!input.value);
      box.classList.toggle('has-token', tokened);
      box.classList.toggle('is-open', on || !!input.value || tokened);
      // Another script may have hidden it; whether it shows is the bar's to say.
      clear.hidden = false;
    }

    function setPending(on) { box.classList.toggle('is-pending', on); }

    function label() { return listView ? LIST_LABEL : LABEL; }

    function setHint(text) {
      if (hintText.textContent === text) return;
      var old = box.querySelector('.watch-search__ph.is-old');
      if (old) old.remove();
      if (moves(hintText) && !input.value) {
        old = hintText.cloneNode(true);
        old.removeAttribute('id');
        old.classList.add('is-old');
        hintText.parentNode.insertBefore(old, hintText);
        play(old, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-12px)' }], 180,
          { fill: 'forwards' }).onfinish = old.remove.bind(old);
        play(hintText, [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'none' }], 240,
          { delay: 40, fill: 'backwards' });
      }
      hintText.textContent = text;
    }

    function restHint() { setHint(tokened ? 'Search again' : label()); }

    /* One round of examples once the page has settled, then it rests. */
    function showExamples() {
      if (!canExplore() || core.reduceMotion()) return;
      var n = 0;
      var tick = window.setInterval(function () {
        if (focused() || input.value || tokened || listView) { window.clearInterval(tick); return; }
        if (document.hidden) return;
        if (n === EXAMPLES.length) {
          window.clearInterval(tick);
          restHint();
          return;
        }
        setHint('Try “' + EXAMPLES[n++] + '”');
      }, EXAMPLE_MS);
    }

    function setToken(what) {
      tokened = !!what;
      token.hidden = !what;
      token.textContent = '';
      if (what) {
        var chip = el('span');
        chip.appendChild(what.face !== undefined ? face(what.face, what.person) : what.icon);
        chip.appendChild(el('span', 'watch-search__name', what.name));
        token.appendChild(chip);
        if (moves(chip)) chip.animate([{ opacity: 0, transform: 'scale(.7)' }, { opacity: 1, transform: 'none' }], { duration: 260, easing: spring() });
      }
      // The words it makes room for do not move a second time.
      hintText.textContent = tokened ? 'Search again' : label();
      sync();
    }

    /* ── The sheet ── */

    function option(node, key) {
      node.setAttribute('id', 'watch-option-' + key);
      node.classList.add('watch-option');
      node.setAttribute('role', 'option');
      node.setAttribute('aria-selected', 'false');
      node.setAttribute('tabindex', '-1');
      return node;
    }

    function staged(node, ms) {
      node.classList.add('is-staged');
      node.style.animationDelay = ms + 'ms';
      return node;
    }

    function tries(list, inSheet) {
      var row = el('div', 'watch-tries');
      list.forEach(function (text, i) {
        var b = el('button', 'watch-try');
        b.type = 'button';
        b.appendChild(MM.dom.icon('i-magnifying-glass'));
        b.appendChild(document.createTextNode(text));
        b.addEventListener('click', function () { fill(text); });
        if (inSheet) staged(option(b, 'try-' + i), 130 + i * 22);
        row.appendChild(b);
      });
      return row;
    }

    function drawSheet(genres) {
      if (sheetGenres === genres) return;
      sheetGenres = genres;
      sheet.textContent = '';
      if (genres.length) {
        var tiles = el('div', 'watch-tiles');
        tiles.setAttribute('role', 'group');
        tiles.setAttribute('aria-label', 'Browse by category');
        genres.forEach(function (genre, i) {
          var b = option(el('button', 'watch-tile'), 'genre-' + genre.id);
          b.type = 'button';
          var name = el('b', '', genre.name);
          b.appendChild(name);
          (genre.poster_urls || []).slice(0, 2).forEach(function (url) { b.appendChild(small(url)); });
          b.addEventListener('click', function () { explore.openGenre(genre.id, {}, name); });
          tiles.appendChild(staged(b, 40 + (i % 4 + Math.floor(i / 4)) * 24));
        });
        sheet.appendChild(el('p', 'watch-sheet__title', 'Browse by category'));
        sheet.appendChild(tiles);
      }
      sheet.appendChild(el('p', 'watch-sheet__title', 'Or try'));
      var row = tries(TRIES, true);
      row.setAttribute('role', 'group');
      row.setAttribute('aria-label', 'Or try');
      sheet.appendChild(row);
    }

    function openSheet(on) {
      on = on && canExplore() && !input.value && !tokened;
      if (on) explore.categories().then(function (all) { if (all.genres.length || !sheetGenres) drawSheet(all.genres); });
      if (on === sheetOn) return;
      sheetOn = on;
      if (on) {
        var preview = window.MMWatch.preview;
        if (preview && preview.close) preview.close();
        drawSheet(explore.genres());
        sheet.scrollTop = 0;
        setHint('Try “' + EXAMPLES[0] + '”');
      }
      page.classList.toggle('is-sheet-on', on);
      sheet.classList.toggle('is-on', on);
      sheet.inert = !on;
      [browse, byId('watch-all')].forEach(function (n) { if (n) n.inert = on; });
      sayList();
      pick(null);
    }

    /* The keyboard of a phone covers the bottom of the window, not of the page. */
    function fitSheet() {
      if (window.visualViewport) sheet.style.setProperty('--seen', Math.round(window.visualViewport.height) + 'px');
    }

    /* ── What stands under the bar ── */

    /* The rows fade and shrink behind what replaces them, and grow back. */
    function show(to) {
      if (view === to) return;
      var was = view;
      view = to;
      clearTimeout(hiding);
      if (to === 'browse') {
        leave();
        results.hidden = true;
        if (listView) return;
        if (browse.hidden && moves(browse)) browse.classList.add('is-off');
        browse.hidden = false;
        void browse.offsetWidth;
        browse.classList.remove('is-off');
        return;
      }
      results.hidden = false;
      if (was !== 'browse' || browse.hidden) return;
      function gone() {
        browse.hidden = true;
        browse.classList.remove('is-off');
      }
      if (moves(browse)) {
        browse.classList.add('is-off');
        hiding = setTimeout(gone, core.tokenMs('--dur-fast', 150));
      } else {
        gone();
      }
      toTop();
    }

    function toTop() {
      if (window.scrollY) window.scrollTo({ left: 0, top: 0, behavior: 'instant' });
    }

    /* What the results hold fades out behind what replaces it. */
    function leave() {
      if (!live) return;
      var node = live.node;
      live = null;
      sayList();
      if (!moves(node) || !node.getClientRects().length) { node.remove(); return; }
      var at = node.getBoundingClientRect();
      node.remove();
      node.classList.add('is-leaving');
      page.appendChild(ghost(node, at, page.getBoundingClientRect()));
      fadeAway(node, 1, 140, false);
    }

    function hold(kind, name, node) {
      if (live && live.kind === kind && !node) return false;
      leave();
      live = { kind: kind, node: node || el('div', 'results-page results-page--' + kind) };
      results.setAttribute('aria-label', name);
      results.appendChild(live.node);
      return true;
    }

    /* ── Suggestions ── */

    function holdResults() {
      if (!hold('results', 'Search results')) return;
      var side = el('div', 'results-side');
      var list = el('div', 'watch-suggestions');
      list.setAttribute('id', 'watch-suggestions');
      list.setAttribute('role', 'listbox');
      list.setAttribute('aria-label', 'Suggestions');
      var bar = el('div', 'watch-suggestions__bar');
      bar.setAttribute('aria-hidden', 'true');
      list.appendChild(bar);
      side.appendChild(list);
      var main = el('div', 'results-main');
      var grid = el('div', 'watch-grid results-grid');
      main.appendChild(grid);
      live.node.appendChild(side);
      live.node.appendChild(main);
      live.side = side;
      live.list = list;
      live.bar = bar;
      live.grid = grid;
      live.drawn = false; // whether the grid shows an answer
      live.rows = []; // what the grid shows
    }

    function suggestion(key, kind, o) {
      var tag = o.ref ? 'a' : 'button';
      return {
        key: key,
        make: function () {
          var b = option(el(tag, 'watch-suggestion watch-suggestion--' + kind), key);
          if (o.ref) {
            b.setAttribute('data-title-ref', o.ref);
            b.setAttribute('data-title', o.name);
            b.href = safePath(o.href) || '#title=' + encodeURIComponent(o.ref);
          } else {
            b.type = 'button';
          }
          var pic = el('span', 'watch-suggestion__pic');
          pic.appendChild(o.pic());
          b.appendChild(pic);
          var text = el('span', 'watch-suggestion__text');
          text.appendChild(el('b'));
          text.appendChild(el('small'));
          b.appendChild(text);
          b.appendChild(MM.dom.icon('i-chevron-right', 'watch-suggestion__go'));
          if (o.act) b.addEventListener('click', function () { b.act(b); });
          return b;
        },
        update: function (b) {
          b.act = o.act;
          b.querySelector('.watch-suggestion__text b').replaceWith(marked(o.name, o.typed));
          b.querySelector('.watch-suggestion__text small').textContent = o.sub;
        }
      };
    }

    function genreSuggestion(genre, asked, sort, typed) {
      var type = genre.types.indexOf(asked.type) > -1 ? asked.type : '';
      var facts = { type: type, sort: sort, decade: asked.decade };
      return suggestion('c-' + genre.id + (sort === asked.sort ? '' : '-' + sort), 'category', {
        name: genre.name + (type === 'movie' ? ' movies' : type === 'tv' ? ' shows' : ''),
        sub: ['Category', explore.sortName(sort)].concat(asked.decade || []).join(' · '),
        typed: typed,
        pic: function () {
          var tag = el('span', 'watch-suggestion__tag');
          tag.appendChild(explore.icon(genre.id));
          return tag;
        },
        act: function (b) { explore.openGenre(genre.id, facts, b.querySelector('b')); }
      });
    }

    function suggestionsFor(q, c) {
      var typed = q.toLowerCase();
      var asked = canExplore() ? explore.parse(q) : { genres: [], words: '' };
      var cats = asked.genres.slice(0, 2);
      var who = [];
      if (typed.length > 1) {
        people.forEach(function (p) { if (begins(p.name, typed)) who.push(p); });
        // The server's own answer may match more than the start of a name.
        if (c && c.tmdb.state === 'ok') {
          who = c.tmdb.people.concat(who.filter(function (p) {
            return !c.tmdb.people.some(function (o) { return o.person_id === p.person_id; });
          }));
        }
      }
      who = canExplore() ? who.slice(0, 3) : [];
      var list = cats.map(function (genre) { return genreSuggestion(genre, asked, asked.sort, asked.words); });
      if (cats.length === 1 && !who.length) {
        ['top_rated', 'newest'].forEach(function (sort) {
          if (sort !== asked.sort) list.push(genreSuggestion(cats[0], asked, sort, ''));
        });
      }
      who.forEach(function (p) {
        list.push(suggestion('p-' + p.person_id, 'person', {
          name: p.name,
          sub: [p.job].concat((p.known_for || []).slice(0, 2).join(', ')).filter(Boolean).join(' · '),
          typed: typed,
          pic: function () { return face(p.profile_url, p.name); },
          act: function (b) { explore.openPerson(p, b.querySelector('.watch-face')); }
        }));
      });
      titles(c).filter(function (t) { return t.ref && begins(t.title, typed); }).slice(0, 3).forEach(function (t) {
        list.push(suggestion('t-' + t.ref, 'title', {
          name: t.title, ref: t.ref, href: t.href, typed: typed,
          sub: t.subtitle || kindLine(t),
          pic: function () { return small(t.poster_url); }
        }));
      });
      return list;
    }

    /* The titles known while *c* runs: its own and those of the search before. */
    function titles(c) {
      var all = [];
      [c, last].forEach(function (s) {
        if (!s) return;
        [s.lib, s.tmdb].forEach(function (part) { all = all.concat((part && part.items) || []); });
      });
      return all;
    }

    /* The suggestions for *q*, and *rows* in the grid, which moves with them.
       Their column goes only once it is known to stay empty. */
    function draw(q, c, rows, settled) {
      live.rows = rows;
      glide(live.grid, rows, function () {
        var list = suggestionsFor(q, c);
        var at = picked && picked.id;
        if (list.length || settled) live.node.classList.toggle('has-no-suggestions', !list.length);
        live.side.classList.toggle('is-titles-only', list.every(function (x) { return x.key.charAt(0) === 't'; }));
        glide(live.list, list);
        sayList();
        pick(at ? options().filter(function (n) { return n.id === at; })[0] || null : null);
      });
    }

    /* ── The highlight ── */

    function options() {
      var root = sheetOn ? sheet : live && live.kind === 'results' ? live.list : null;
      return root ? core.toArray(root.querySelectorAll('.watch-option')).filter(function (n) { return n.getClientRects().length; }) : [];
    }

    /* Tell the field's readers of the list that shows, if one does. */
    function sayList() {
      var open = options().length > 0;
      input.setAttribute('aria-expanded', open ? 'true' : 'false');
      input.setAttribute('aria-controls', open && !sheetOn ? 'watch-suggestions' : 'watch-sheet');
    }

    function pick(node) {
      if (picked && picked !== node) {
        picked.classList.remove('is-picked');
        picked.setAttribute('aria-selected', 'false');
      }
      var bar = live && live.kind === 'results' ? live.bar : null;
      if (bar) {
        if (!sheetOn && node) {
          var at = 'translateY(' + node.offsetTop + 'px)';
          if (!bar.classList.contains('is-on')) {
            // It shows where it is wanted, and glides from there.
            bar.style.transition = 'none';
            bar.style.transform = at;
            void bar.offsetWidth;
            bar.style.transition = '';
          }
          bar.style.transform = at;
        }
        bar.classList.toggle('is-on', !sheetOn && !!node);
      }
      picked = node;
      if (!node) { input.removeAttribute('aria-activedescendant'); return; }
      node.classList.add('is-picked');
      node.setAttribute('aria-selected', 'true');
      input.setAttribute('aria-activedescendant', node.id);
      node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    /* The option beside *from* that way, nearest first. */
    function beside(all, from, way) {
      var a = from.getBoundingClientRect();
      var ax = a.left + a.width / 2;
      var ay = a.top + a.height / 2;
      var best = null;
      var least = Infinity;
      all.forEach(function (node) {
        if (node === from) return;
        var r = node.getBoundingClientRect();
        var x = r.left + r.width / 2 - ax;
        var y = r.top + r.height / 2 - ay;
        var across = way === 'ArrowLeft' || way === 'ArrowRight';
        var ok = across ? Math.abs(y) < 20 && (way === 'ArrowLeft' ? x < -8 : x > 8) : way === 'ArrowUp' ? y < -20 : y > 20;
        var far = across ? Math.abs(x) : Math.abs(y) * 4 + Math.abs(x);
        if (ok && far < least) {
          best = node;
          least = far;
        }
      });
      return best;
    }

    /* Whether the key moved the highlight. */
    function step(key) {
      var all = options();
      if (!all.length) return false;
      var across = key === 'ArrowLeft' || key === 'ArrowRight';
      if (!picked) {
        if (across) return false;
        pick(key === 'ArrowDown' ? all[0] : all[all.length - 1]);
        return true;
      }
      if (sheetOn) {
        var to = beside(all, picked, key);
        if (to || key === 'ArrowUp') pick(to);
        return true;
      }
      var strip = all.length > 1 && all[0].offsetTop === all[1].offsetTop;
      if (across && !strip) return false;
      var i = all.indexOf(picked) + (key === 'ArrowDown' || key === 'ArrowRight' ? 1 : -1);
      pick(all[i] || null);
      return true;
    }

    /* ── Results ── */

    function stop() {
      clearTimeout(timer);
      if (current) current.ctrl.abort();
      current = null;
      setPending(false);
      if (live && live.grid) live.grid.removeAttribute('aria-busy');
    }

    /* *lone*: no suggestion stands beside it, so it says what to try. */
    function nothing(q, lone) {
      var box = el('div', 'results-empty');
      var lens = explore.drawing(LENS, 'watch-line results-empty__lens');
      box.appendChild(lens);
      box.appendChild(el('p', 'watch-results__line', (lone ? 'No matches for “' : 'No title matches “') + q + '”'));
      if (!lone) return box;
      box.appendChild(el('p', 'watch-results__hint', 'Check the spelling, or try a shorter title.'));
      if (canExplore()) box.appendChild(tries(TRIES_EMPTY, false));
      box.look = function () {
        if (!moves(lens)) return;
        lens.animate([
          { transform: 'none' },
          { transform: 'translateX(-7px) rotate(-12deg)', offset: 0.3 },
          { transform: 'translateX(6px) rotate(9deg)', offset: 0.65 },
          { transform: 'none' }
        ], { duration: 350, delay: 60, easing: 'ease-in-out' });
      };
      return box;
    }

    /* With TMDB not set up, the line below says why nothing outside the library
       was searched. */
    function showNothing(c, notSetUp) {
      leave();
      hold('none', 'Search results');
      var block = nothing(c.q, true);
      live.node.appendChild(block);
      if (notSetUp) live.node.appendChild(hint(data));
      rise(live.node);
      block.look();
      status.textContent = 'No matches for “' + c.q + '”';
    }

    /* The whole search failed: one line, one way on that runs both searches again. */
    function showFailed() {
      leave();
      hold('failed', 'Search results');
      live.node.appendChild(failure(SEARCH_FAILED, function () {
        input.focus();
        run();
      }));
      rise(live.node);
      status.textContent = SEARCH_FAILED;
    }

    /* The two lists as they are shown. Titles of a category that was named in
       full join the one they belong to, after the matches. */
    function gather(c) {
      var lib = (c.lib.items || []).map(function (item) { return { item: item, key: item.ref || item.href, build: buildCard }; });
      var out = (c.tmdb.items || []).map(function (item) { return { item: item, key: item.ref, build: buildTmdbCard }; });
      var seen = new Set(lib.concat(out).map(function (x) { return x.key; }));
      ((c.cat && c.cat.items) || []).forEach(function (item) {
        var key = item.library_ref || item.ref;
        if (seen.has(key) || seen.has(item.ref)) return;
        seen.add(key);
        if (item.in_library) lib.push({ item: item, key: key, build: function (i) { return buildTmdbCard(i, true); } });
        else out.push({ item: item, key: key, build: buildTmdbCard });
      });
      return { lib: lib, out: out };
    }

    function readOut(c, all) {
      var parts = [];
      if (c.lib.state === 'ok' && all.lib.length) parts.push(all.lib.length + ' in your library');
      if (all.out.length) parts.push(all.out.length + ' available to download');
      status.textContent = parts.length
        ? parts.join(', ') + ' for “' + c.q + '”'
        : 'No matches in your library for “' + c.q + '”';
    }

    function headRow(key, title, count) {
      return {
        key: 'head-' + key,
        make: function () {
          var head = groupHead(title, count);
          head.setAttribute('data-results-head', key);
          return head;
        },
        update: function (node) { setCount(node, count); }
      };
    }

    function skeletonRows(key) {
      var rows = [];
      for (var n = 0; n < SKELETONS; n++) rows.push({ key: 'skel-' + key + n, make: skeleton });
      return rows;
    }

    function cardRows(list) {
      return list.map(function (x) {
        return { key: x.key, sig: JSON.stringify(x.item), make: function () { return x.build(x.item); } };
      });
    }

    function failRow(c, key, text, again) {
      return {
        key: 'fail-' + key,
        sig: text,
        make: function () {
          var line = failure(text, function () { c.refocus = key; again(); });
          line.setAttribute('data-results-fail', key);
          return line;
        }
      };
    }

    function plain(key, node) { return { key: key, sig: key, make: function () { return node(); } }; }

    /* Put what is known about search *c* on the page. The library group goes
       up the moment it arrives; TMDB fills its own place when it does. */
    function paint(c) {
      if (current !== c) return;
      var lib = c.lib;
      var tmdb = c.tmdb;
      var cat = c.cat ? c.cat.state : 'ok';
      var all = gather(c);
      var tmdbQuiet = tmdb.state === 'short' || tmdb.state === 'unconfigured' || (tmdb.state === 'ok' && !tmdb.items.length);
      var settled = lib.state !== 'loading' && tmdb.state !== 'loading' && cat !== 'loading';
      setPending(!settled);

      if (settled && !all.lib.length && !all.out.length) {
        if (lib.state === 'failed' && (tmdbQuiet || tmdb.state === 'failed')) { showFailed(); return; }
        if (lib.state === 'ok' && tmdbQuiet && cat === 'failed') { showFailed(); return; }
        if (lib.state === 'ok' && tmdbQuiet && !suggestionsFor(c.q, c).length) {
          showNothing(c, tmdb.state === 'unconfigured');
          last = c;
          return;
        }
      }
      holdResults();
      // Old results stay, dimmed, until the library answers.
      if (lib.state === 'loading' && live.drawn && !lib.items) {
        draw(c.q, c, live.rows, false);
        return;
      }

      var rows = [];
      if (lib.state === 'loading' && !lib.items) rows = rows.concat(headRow('lib', 'In your library'), skeletonRows('lib'));
      else if (lib.state === 'failed') {
        rows.push(headRow('lib', 'In your library'), failRow(c, 'lib', lib.text, function () { fetchLibrary(c); paint(c); }));
        rows = rows.concat(cardRows(all.lib));
      } else if (all.lib.length) {
        rows = rows.concat(headRow('lib', 'In your library', all.lib.length), cardRows(all.lib));
      } else if (tmdb.state === 'failed') {
        rows.push(plain('none', function () { return el('p', 'results-none', 'Nothing in your library matches'); }));
      }

      if (tmdb.state === 'loading' && !tmdb.items) {
        rows.push(headRow('tmdb', 'Available to download'));
        rows = rows.concat(c.before.length ? cardRows(c.before) : skeletonRows('tmdb'));
      } else if (tmdb.state === 'failed') {
        rows.push(headRow('tmdb', 'Available to download'), failRow(c, 'tmdb', TMDB_FAILED, function () { fetchTmdb(c); paint(c); }));
        rows = rows.concat(cardRows(all.out));
      } else if (all.out.length) {
        rows = rows.concat(headRow('tmdb', 'Available to download', all.out.length), cardRows(all.out));
      } else if (settled && !all.lib.length && lib.state === 'ok') {
        rows.push(plain('nothing-' + c.q, function () { return nothing(c.q, false); }));
      }
      // Said once there is a library result to put it under.
      if (tmdb.state === 'unconfigured' && (all.lib.length || (settled && lib.state === 'ok'))) {
        rows.push(plain('hint', function () { return hint(data); }));
      }
      live.grid.removeAttribute('aria-busy');
      live.drawn = lib.state !== 'loading';
      draw(c.q, c, rows, settled);
      refocus(c, 'lib', lib);
      refocus(c, 'tmdb', tmdb);
      if (settled) {
        last = c;
        readOut(c, all);
      }
    }

    /* After a retry the line it was in is gone: put focus on the group's
       heading, or on the new Try again if it failed again. */
    function refocus(c, which, part) {
      if (c.refocus !== which || part.state === 'loading') return;
      var a = document.activeElement;
      c.refocus = null;
      if (a && a !== document.body && document.body.contains(a)) return;
      var target = live.grid.querySelector('[data-results-fail="' + which + '"] .watch-results__retry') ||
        live.grid.querySelector('[data-results-head="' + which + '"] .results-group__title');
      if (target) target.focus();
    }

    /* Ask for one part of search *c*; what an earlier answer gave stays until this one is in. */
    function ask(c, part, url, read, failed) {
      c[part] = { state: 'loading', items: c[part] && c[part].items };
      MM.api.get(url, { signal: c.ctrl.signal }).then(function (reply) {
        if (current !== c) return;
        c[part] = read(reply || {});
        paint(c);
      }).catch(function (err) {
        if ((err && err.name === 'AbortError') || current !== c) return;
        c[part] = failed(err);
        paint(c);
      });
    }

    function fetchLibrary(c) {
      ask(c, 'lib', SEARCH_URL + encodeURIComponent(c.q), function (reply) {
        return { state: 'ok', items: reply.items || [] };
      }, function (err) {
        return { state: 'failed', text: MM.api.messageOf(err, SEARCH_FAILED) };
      });
    }

    function fetchTmdb(c) {
      ask(c, 'tmdb', TMDB_URL + encodeURIComponent(c.q), function (reply) {
        if (reply.tmdb_configured === false) return { state: 'unconfigured' };
        var found = reply.people || [];
        found.forEach(function (p) { people.set(p.person_id, p); });
        return { state: 'ok', items: reply.items || [], people: found };
      }, function () { return { state: 'failed' }; });
    }

    /* "popular action movies" is also the category's own list. Until the
       categories are in, it may name one. */
    function askCategory(c, before) {
      if (!explore.known()) {
        c.cat = { state: 'loading' };
        explore.categories().then(function () {
          if (current !== c) return;
          c.cat = null;
          if (explore.known()) askCategory(c, before);
          paint(c);
        });
        return;
      }
      var asked = explore.parse(c.q);
      if (!asked.exact) return;
      c.cat = before && { items: before.items };
      fetchCategory(c, asked);
    }

    function fetchCategory(c, asked) {
      ask(c, 'cat', explore.url(asked.exact, asked, 1), function (reply) {
        return { state: 'ok', items: reply.items || [] };
      }, function () { return { state: 'failed' }; });
    }

    /* ── Searching ── */

    /* Keep ?q= in the address, in the same entry so Back is not spammed; leaving
       a category, a person or a grid is a new one, so Back returns to it. */
    function syncUrl(q) {
      var away = q && (address.value('explore') !== null || address.value('grid') !== null);
      if (away) {
        var over = address.value('grid') !== null;
        address.push('search', address.url(Object.assign({}, PAGE_PARAMS, { q: q })));
        // Outlives a reload: emptied, the search gives way to the grid it was typed over.
        if (over) window.history.replaceState(Object.assign({}, window.history.state, { mmOverGrid: true }), '');
        return;
      }
      var to = address.url({ q: q || null });
      if (to !== window.location.pathname + window.location.search + window.location.hash) address.replace('search', to);
    }

    function run() {
      var q = input.value.trim();
      stop();
      if (q) show('results');
      syncUrl(q);
      if (!q) {
        status.textContent = '';
        show('browse');
        return;
      }
      var known = last && last.q === q ? last : null;
      var c = {
        q: q, ctrl: new AbortController(), refocus: null,
        lib: known && { items: known.lib.items }, tmdb: known && { items: known.tmdb.items }, cat: null,
        // What is on the page already and fits may stay while TMDB is asked.
        before: last && !known ? gather(last).out.filter(function (x) { return begins(x.item.title, q.toLowerCase()); }) : []
      };
      current = c;
      setPending(true);
      // Emptied now, so the same answer is read out again when it comes.
      status.textContent = '';
      fetchLibrary(c);
      if (data.tmdb_configured !== true) c.tmdb = { state: 'unconfigured' };
      else if (q.length < TMDB_MIN_CHARS) c.tmdb = { state: 'short' };
      else fetchTmdb(c);
      if (canExplore()) askCategory(c, known && known.cat);
      paint(c);
      if (live && live.grid && live.drawn && !c.lib.items) live.grid.setAttribute('aria-busy', 'true');
    }

    function addressed() {
      var params = new URLSearchParams(window.location.search);
      return ['q', 'genre', 'person', 'all'].some(function (key) { return params.get(key); });
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

    /* Every key: the bar and the suggestions answer at once, the server is asked
       when the person pauses. */
    function typed(now) {
      var q = input.value.trim();
      clearTimeout(timer);
      if (tokened) setToken(null);
      sync();
      if (listView) {
        if (!q) change();
        else timer = setTimeout(change, LIST_DELAY_MS);
        return;
      }
      stop();
      if (!q && address.value('search') !== null && (window.history.state || {}).mmOverGrid) {
        openSheet(false);
        // The grid takes the focus as it opens.
        window.addEventListener('popstate', function () { input.focus({ preventScroll: true }); }, { once: true });
        address.back('search');
        return;
      }
      if (!q || now) {
        openSheet(false);
        change();
        if (!input.value) openSheet(focused());
        return;
      }
      openSheet(false);
      show('results');
      if (address.value('explore') !== null || address.value('grid') !== null) syncUrl(q);
      holdResults();
      draw(q, null, live.rows, false);
      if (canExplore() && !explore.known()) {
        explore.categories().then(function () {
          if (!current && live && live.kind === 'results' && input.value.trim() === q) draw(q, null, live.rows, false);
        });
      }
      setPending(true);
      timer = setTimeout(change, SEARCH_DELAY_MS);
    }

    function fill(text) {
      input.value = text;
      input.focus({ preventScroll: true });
      typed(true);
    }

    /* Empty the bar: the text, or the category or person in it. */
    function clearSearch() {
      if (tokened) {
        setToken(null);
        address.push(null, address.url(PAGE_PARAMS));
      }
      input.value = '';
      input.focus({ preventScroll: true });
      typed(true);
      sync();
    }

    input.addEventListener('input', function () { typed(false); });
    input.addEventListener('keydown', function (e) {
      if (composing(e)) return;
      if (/^Arrow/.test(e.key) && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        if (e.key === 'ArrowDown' && !sheetOn && !options().length) {
          openSheet(true);
          if (sheetOn) e.preventDefault();
        } else if (step(e.key)) {
          e.preventDefault();
        }
        return;
      }
      if (e.key === 'Escape') {
        if (picked) pick(null);
        else if (input.value) clearSearch();
        else if (sheetOn) {
          openSheet(false);
          input.blur();
        } else return;
        e.preventDefault();
        return;
      }
      if (e.key === 'Backspace' && !input.value && tokened) {
        e.preventDefault();
        clearSearch();
        return;
      }
      // An Enter held down asks for nothing new.
      if (e.key !== 'Enter' || e.repeat) return;
      e.preventDefault();
      if (picked) {
        picked.click();
        return;
      }
      if (tokened || (!input.value && !listView)) return;
      clearTimeout(timer);
      change();
      // The keyboard on a phone covers the results.
      if (core.coarsePointer()) input.blur();
    });

    // Only someone's own press or Tab opens the sheet: scripts put focus here too.
    var tabbed = false;
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      tabbed = true;
      setTimeout(function () { tabbed = false; });
    }, true);
    input.addEventListener('focus', function () {
      sync();
      if (tabbed) openSheet(true);
    });
    input.addEventListener('blur', function () {
      sync();
      setTimeout(function () {
        if (focused() || sheet.contains(document.activeElement)) return;
        openSheet(false);
        pick(null);
        if (!input.value) restHint();
      });
    });
    box.addEventListener('mousedown', function (e) {
      if (clear.contains(e.target)) { e.preventDefault(); return; } // the caret stays
      if (e.target !== input) {
        e.preventDefault();
        input.focus({ preventScroll: true });
      }
      openSheet(true);
    });
    // The caret stays in the bar while an option is pressed.
    sheet.addEventListener('mousedown', function (e) { e.preventDefault(); });
    results.addEventListener('mousedown', function (e) {
      if (e.target.closest('.watch-suggestion')) e.preventDefault();
    });
    veil.addEventListener('click', function () {
      openSheet(false);
      input.blur();
    });
    clear.addEventListener('click', clearSearch);

    function modalOpen() {
      return core.toArray(document.querySelectorAll('[aria-modal="true"]')).some(function (n) { return n.getClientRects().length > 0; });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== '/' || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      var a = document.activeElement;
      if (a && (/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName) || a.isContentEditable || (a.closest && a.closest('[role="menu"]')))) return;
      if (modalOpen()) return;
      e.preventDefault();
      toTop();
      input.focus({ preventScroll: true });
      input.select();
      openSheet(true);
    });

    // The list empties the field itself.
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[data-list-clear]')) setTimeout(sync);
    });

    // The admin switch between the rows and the list moves the field with it.
    window.addEventListener('mm:watch-view', function (e) {
      var to = e && e.detail && e.detail.view;
      listView = data.is_admin === true && to === 'list';
      stop();
      clearTimeout(hiding);
      openSheet(false);
      browse.classList.remove('is-off');
      if (live) live.node.remove();
      live = null;
      sayList();
      view = 'browse';
      results.hidden = true;
      status.textContent = '';
      if (tokened) setToken(null);
      input.placeholder = label();
      input.setAttribute('aria-label', label());
      restHint();
      // Text goes along, unless Back led to something the address opens itself.
      if (input.value.trim() && (listView || !addressed())) change();
    });

    if (window.visualViewport) window.visualViewport.addEventListener('resize', fitSheet);
    fitSheet();
    // A narrow window shows no titles among the suggestions.
    window.addEventListener('resize', sayList);

    /* ── A page in the results' place ── */

    window.MMWatch.search.stage = {
      /* *node* takes the place of what stands under the bar; *what* names it there. */
      open: function (node, name, what) {
        stop();
        input.value = '';
        openSheet(false);
        pick(null);
        show('page');
        hold('page', name, node);
        status.textContent = '';
        setToken(what);
        input.blur();
        toTop();
      },
      token: setToken,
      close: function () {
        if (tokened) setToken(null);
        if (view === 'page') show('browse');
      },
      say: function (text) { status.textContent = text; },
      rise: rise,
      grow: grow,
      spring: spring,
      face: face,
      head: groupHead,
      data: data
    };

    address.layer({
      name: 'search',
      depth: 30,
      params: ['q'],
      read: function (here) {
        return here.params.get('view') === 'list' ? null : (here.params.get('q') || '').trim() || null;
      },
      open: function (q, how) {
        if (input.value.trim() === q && (how === 'start' || (current && current.q === q))) return;
        input.value = q;
        if (tokened) setToken(null);
        sync();
        run();
      },
      close: function (how) {
        // The list takes the text with it; whatever else was opened says what stands here now.
        if (how === 'push' || new URLSearchParams(window.location.search).get('view') === 'list') return;
        stop();
        input.value = '';
        sync();
        status.textContent = '';
        if (view === 'results') show('browse');
      }
    });

    sync();
    showExamples();
    if (canExplore()) explore.categories();
    // A value the browser put back (going back to the page) gets its results.
    if (input.value.trim() && !listView) run();
    // Nothing the address names after all: the rows.
    setTimeout(function () { if (view === 'results' && !live && !listView) show('browse'); });
  }

  window.MMWatch.search = { init: initSearch, card: libraryCard, tmdbCard: buildTmdbCard, composing: composing };
})();
