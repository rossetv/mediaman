/* Watch: the search bar, its sheet of categories, the suggestions and the
 * results, in two groups: titles in the library, then titles from TMDB that are
 * not. Cards built here mirror templates/_watch_card.html. Nothing is built with
 * innerHTML: titles come from Plex and TMDB. A category's or person's page
 * (watch/explore.js) stands in the results' place, on MMWatch.search.stage. Where there is an AI, the sheet opens
 * on Tonight and the person's own phrases, and a sentence can be asked (watch/ask.js,
 * watch/tonight.js); the search itself never waits for it.
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
  var COMPOSING_MS = 400;
  var EXAMPLE_MS = 3200;
  var STEP_MS = 10000; // a step back this late was never made: a Back after it is the person's
  var SKELETONS = 6;
  // The categories the sheet opens on; the rest stand below what else it offers.
  var FIRST = ['action', 'comedy', 'thriller', 'science-fiction', 'drama', 'horror', 'family', 'crime', 'romance', 'fantasy', 'animation', 'documentary'];
  var PEOPLE = 6;
  var NO_PEOPLE = 'mm.watch.no-people'; // stored while none of the library are known, so no place is held for them
  var SEARCH_FAILED = 'Couldn’t search just now.';
  var TMDB_FAILED = 'Couldn’t search for titles outside the library just now.';
  var CATEGORIES_FAILED = 'Couldn’t load the categories just now.';
  var LABEL = 'Search films and shows';
  var SHORT_LABEL = 'Search';
  var ASK_HINT = 'Search, or ask for something';
  var EXAMPLES = ['popular action movies', 'an actor’s name', 'comedies to watch tonight', 'something scary'];
  var TRIES = ['popular action movies', 'popular comedy movies', 'top rated sci-fi'];
  var NO_TITLE = 'no-title'; // the line under people who were found alone
  var NOTHING = 'nothing-'; // what stands where nothing was found at all
  var TRIES_EMPTY = ['popular action movies', 'popular comedy movies', 'something scary'];
  var LENS = 'M10.5 4a6.5 6.5 0 100 13 6.5 6.5 0 000-13M15.5 15.5L20 20';
  var PAGE_PARAMS = { genre: null, type: null, sort: null, where: null, decade: null, person: null, all: null, like: null };
  var LIKE_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$/;
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
    if (days < 0) return 'Leaving any day';
    if (!days) return 'Leaving today';
    return days === 1 ? 'Leaving in 1 day' : 'Leaving in ' + days + ' days';
  }

  function leavingShortText(days) {
    if (days <= 0) return leavingText(days);
    return days === 1 ? '1 day left' : days + ' days left';
  }

  function leavingBadge(item) {
    if (typeof item.leaving_days !== 'number') return null;
    var badge = el('span', 'watch-badge watch-badge--leaving' + (item.leaving_days <= 1 ? ' watch-badge--leaving-now' : ''));
    var days = item.leaving_days;
    var words = leavingText(days);
    var short = leavingShortText(days);
    var tinyWords = days > 0 ? '' : days < 0 ? 'Any day' : 'Today';
    var least = '';
    // Of a show, the season that is leaving.
    if (typeof item.leaving_season === 'number') {
      var s = 'S' + item.leaving_season;
      words = words.replace('Leaving', 'Season ' + item.leaving_season + ' leaves');
      short = days > 0 ? s + ' · ' + short : short.replace('Leaving', s + ' leaves');
      least = s + (days > 0 ? ' · ' + days + 'd' : days ? ' · Due' : ' Today');
      tinyWords = s + ' · ' + (tinyWords || days + (days === 1 ? ' day' : ' days'));
    }
    badge.appendChild(el('span', 'watch-badge__long', words));
    if (tinyWords) {
      var tiny = el('span', 'watch-badge__tiny');
      tiny.setAttribute('data-words', tinyWords);
      tiny.setAttribute('aria-hidden', 'true');
      if (least) {
        tiny.appendChild(el('span', 'watch-badge__least'));
        tiny.firstChild.setAttribute('data-words', least);
      }
      badge.appendChild(tiny);
    }
    badge.appendChild(el('span', 'watch-badge__short', short));
    return badge;
  }

  /* Which of a show's seasons leave, and the one its badge is of. */
  function setLeaving(card, item) {
    card.setAttribute('data-leaving-id', item.leaving_id);
    card.setAttribute('data-leaving-seasons', JSON.stringify(item.leaving_seasons || []));
  }

  /* A library title's card; its link carries the ref: it is what takes focus
     back when the detail closes. */
  function libraryCard(item) {
    var card = el('article', 'watch-card');
    if (item.leaving_id) setLeaving(card, item);
    else card.setAttribute('data-keep-id', item.keep_id || '');

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
    var badge = leavingBadge(item);
    if (badge) poster.appendChild(badge);
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
    link.appendChild(core.caption(el('span', 'watch-card__sub'), sub));
    card.appendChild(link);
    core.addMarks(card, poster, item);
    return card;
  }

  /* A search result also has its Keep. */
  function buildCard(item) { return addKeep(libraryCard(item), item); }

  function addKeep(card, item) {
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

  /* One found by its category, in the library. */
  function buildHeldCard(item) {
    return addKeep(buildTmdbCard(item, true), item);
  }

  /* A title TMDB knows, in the library or not; *plain*: its heading says which. */
  function buildTmdbCard(item, plain) {
    var card = el('article', 'watch-card');
    if (item.keep_id) card.setAttribute('data-keep-id', item.keep_id);
    if (item.leaving_id) setLeaving(card, item);

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
    [leavingBadge(item), plain ? null : stateBadge(item)].forEach(function (badge) {
      if (badge) poster.appendChild(badge);
    });
    link.appendChild(poster);
    link.appendChild(el('span', 'watch-card__sub', kindLine(item)));
    card.appendChild(link);
    core.addMarks(card, poster, item);
    return card;
  }

  /* Whether there is nothing to download of it, though Plex may not have it yet. */
  function isHeld(item) { return !!item.in_library || /^(in_library|partial)$/.test(item.download_state || ''); }

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

  function groupHead(title, count, of) {
    var head = el('div', 'results-group__head');
    var h = el('h2', 'results-group__title', title);
    // Takes focus after a retry has replaced the button that had it.
    h.tabIndex = -1;
    head.appendChild(h);
    head.appendChild(el('span', 'results-group__count'));
    setCount(head, count, of);
    return head;
  }

  function titleCount(n) { return n + (n === 1 ? ' title' : ' titles'); }

  /* A category's name inside a sentence: "action", "TV movie". */
  function lower(name) { return name.replace(/\S*[a-z]\S*/g, function (word) { return word.toLowerCase(); }); }

  /* "3 titles", or "60 of 297 titles" where more were found than are shown. */
  function setCount(head, count, of) {
    head.querySelector('.results-group__count').textContent =
      typeof count !== 'number' ? '' : (of > count ? count + ' of ' : '') + titleCount(of > count ? of : count);
  }

  function times(n, make) {
    var all = [];
    while (all.length < n) all.push(make(all.length));
    return all;
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
    var src = safePath(url);
    if (src) {
      var img = el('img');
      img.src = src;
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
      node.appendChild(img);
    }
    node.appendChild(name ? el('span', 'watch-face__letters', initials(name)) : MM.dom.icon('i-user'));
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

  // As the server's own search reads them.
  var LETTERS = { 'æ': 'ae', 'œ': 'oe', 'ø': 'o', 'đ': 'd', 'ð': 'd', 'þ': 'th', 'ł': 'l', 'ħ': 'h', 'ı': 'i', 'ß': 'ss', '&': 'and' };
  var APOSTROPHE = /[\p{M}'’‘ʼ′`´]/u;

  /* The letters and digits of *text*, without case or accents; "&" is "and". */
  function folded(text) {
    return String(text || '').toLowerCase().replace(/[ʼ™℠]/g, '').normalize('NFKD')
      .replace(/[æœøđðþłħıß&]/g, function (char) { return LETTERS[char]; }).replace(/[^\p{L}\p{N}]/gu, '');
  }

  /* Where *typed* begins a word of *text*, as [from, to], or null; spaces, accents and
     punctuation are the same with or without. */
  function place(text, typed) {
    var want = folded(typed);
    // Nothing but punctuation is taken as it is.
    if (!want) return typed && text.indexOf(typed) === 0 ? [0, typed.length] : null;
    var plain = '';
    var from = []; // of each letter of plain, its place in text
    var to = [];
    var first = []; // whether it begins a word
    var inWord = false;
    text.replace(/[\s\S]/gu, function (char, at) {
      var letters = folded(char);
      for (var i = 0; i < letters.length; i++) {
        first.push(!i && (!inWord || char === '&'));
        from.push(at);
        to.push(at + char.length);
      }
      plain += letters;
      // An accent of its own and an apostrophe are inside their word.
      inWord = char !== '&' && (letters !== '' || (inWord && APOSTROPHE.test(char)));
      return char;
    });
    for (var at = plain.indexOf(want); at > -1; at = plain.indexOf(want, at + 1)) {
      if (first[at]) return [from[at], to[at + want.length - 1]];
    }
    return null;
  }

  /* *text*, what was typed standing out in it where it begins a word. */
  function marked(text, typed) {
    var b = el('b');
    var at = typed ? place(text, typed) : null;
    if (!at) {
      b.textContent = text;
      return b;
    }
    b.appendChild(document.createTextNode(text.slice(0, at[0])));
    b.appendChild(el('em', '', text.slice(at[0], at[1])));
    b.appendChild(document.createTextNode(text.slice(at[1])));
    return b;
  }

  function begins(text, typed) { return place(String(text || ''), typed) !== null; }

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
    var bar = box.closest('.wbar');
    var page = byId('watch-page');
    var address = window.MMWatch.address;
    var explore = window.MMWatch.explore;
    var data = readPageData();

    var view = results.hidden ? 'browse' : 'results'; // what stands under the bar: browse, results or page
    var timer = null;
    var hiding = null;
    var current = null; // the search being shown; a reply for any other is dropped
    var last = null; // the search shown last, for coming back to it
    var live = null; // what the results hold: { kind, node, ... }
    var tokened = false;
    var sheetOn = false;
    var sheetOf = null; // what the sheet was drawn from: { all, waiting }
    var outdated = false; // the library went empty, or has something now, since
    var emptied = false; // the step back to what an emptied search was typed over is on its way; a slow one lands at any time
    var emptiedTimer = null;
    var composed = ''; // what was typed last, where a word in it is still being composed
    var picked = null; // the option the arrow keys stand on
    var people = new Map(); // everybody a search has named, by id
    var ai = window.MMWatch.ask || null;
    var downloads = window.MMWatch.downloading || null;
    var shownDownloads = ''; // those of them the search was last drawn with
    var evening = null; // Tonight, once drawn
    var asking = null; // the place of a question over the results
    var ordinary = null; // what parts it from them
    var recorded = ''; // the search kept last
    var ownWords = ''; // what was typed or pressed last on this page: other words came with the address

    function canExplore() { return data.tmdb_configured === true; }

    /* ── The bar ── */

    function focused() { return document.activeElement === input; }

    function sync() {
      var on = focused();
      box.classList.toggle('is-focus', on);
      box.classList.toggle('has-value', !!input.value);
      box.classList.toggle('has-token', tokened);
      box.classList.toggle('is-open', on || ((!!input.value || tokened) && !asked()));
      // Another script may have hidden it; whether it shows is the bar's to say.
      clear.hidden = false;
      makeRoom();
    }

    /* ── Where it stands in the bar ── */

    var wide = window.matchMedia('(min-width: 1100px)');
    var phone = window.matchMedia('(max-width: 699.98px)');
    var meter = bar.querySelector('.space-meter');
    var profile = bar.querySelector('.profile-btn');

    /* The field stays between what is on either side, however large a person has text;
       where that leaves too little, or cuts the meter's figure, the meter drops the words
       beside its figure, and then that too, till the width or the words change. */
    function fitBetween() {
      if (short()) bar.classList.add('is-snug');
      if (short()) bar.classList.add('is-tight');
      short(); // the room that left
      makeRoom();
    }

    function short() {
      var brand = bar.querySelector('.brand');
      var l = brand.offsetLeft + brand.offsetWidth + 2;
      var r = (meter || profile).offsetLeft - 2;
      bar.style.setProperty('--l', l + 'px');
      bar.style.setProperty('--r', r + 'px');
      if (!meter) return false;
      var figure = meter.querySelector('.space-meter__free');
      return !phone.matches ? r - l < 200 : !!figure && figure.scrollWidth > figure.clientWidth + 1;
    }

    function fitAgain() {
      bar.classList.remove('is-snug', 'is-tight');
      fitBetween();
    }

    /* The open field would cover the meter. */
    function covers() {
      return !!meter && wide.matches && box.offsetLeft + box.offsetWidth + 8 > meter.offsetLeft;
    }

    function makeRoom() {
      bar.classList.toggle('is-wide-open', box.classList.contains('is-open') && covers());
    }

    /* The focus is on what the open field covers. */
    function asked() { return document.activeElement === meter && covers(); }

    /* The elements stand in the order they are read: on a phone the field is at the foot. */
    function place() {
      var active = document.activeElement;
      var after = phone.matches ? profile : meter || profile;
      [box, sheet].forEach(function (n) { bar.insertBefore(n, after); });
      // Moving a node takes the focus off it.
      if (active && bar.contains(active) && document.activeElement !== active) active.focus({ preventScroll: true });
    }

    function setPending(on) { box.classList.toggle('is-pending', on); }

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

    function example(i) { return 'Try “' + EXAMPLES[i] + '”'; }

    /* Whether the field shows all of *text*. */
    function fits(text) {
      var was = hintText.textContent;
      hintText.textContent = text;
      var ok = !(hintText.scrollWidth > hintText.clientWidth);
      hintText.textContent = was;
      return ok;
    }

    function rest() {
      if (tokened) return 'Search again';
      return fits(LABEL) ? LABEL : SHORT_LABEL;
    }

    function restHint() { setHint(rest()); }

    /* One round of examples once the page has settled, then it rests. */
    function showExamples() {
      if (!canExplore() || core.reduceMotion()) return;
      var n = 0;
      var tick = window.setInterval(function () {
        if (focused() || input.value || tokened) { window.clearInterval(tick); return; }
        if (document.hidden) return;
        while (n < EXAMPLES.length && !fits(example(n))) n++;
        if (n === EXAMPLES.length) {
          window.clearInterval(tick);
          restHint();
          return;
        }
        setHint(example(n++));
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
      sync();
      // The words it makes room for do not move a second time.
      hintText.textContent = rest();
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

    function pill(className, iconName, text, act) {
      var b = el('button', className);
      b.type = 'button';
      b.appendChild(MM.dom.icon(iconName));
      b.appendChild(el('span', '', text));
      b.addEventListener('click', function () { act(text); });
      return b;
    }

    function tryButton(text) { return pill('watch-try', 'i-magnifying-glass', text, fill); }

    function recentButton(text) { return pill('watch-try', 'i-clock-rotate-left', text, fill); }

    /* One made for the person, which is asked. */
    function phraseButton(text) { return pill('watch-try watch-try--ai', 'i-sparkles', text, fillAsk); }

    function personTile(p, i) {
      var b = option(el('button', 'watch-person'), 'person-' + p.person_id);
      b.type = 'button';
      var photo = face(p.profile_url, p.name);
      b.appendChild(photo);
      b.appendChild(el('span', 'watch-person__name', p.name));
      b.appendChild(el('span', 'watch-person__count', titleCount(p.titles)));
      b.addEventListener('click', function () { explore.openPerson(p, photo); });
      return staged(b, 130 + i * 22);
    }

    function genreTile(genre, i) {
      var b = option(el('button', 'watch-tile'), 'genre-' + genre.id);
      b.type = 'button';
      b.appendChild(explore.icon(genre.id));
      var name = el('b', '', genre.name);
      b.appendChild(name);
      b.addEventListener('click', function () { explore.openGenre(genre.id, {}, name); });
      return staged(b, 40 + (i % 4 + Math.floor(i / 4)) * 24);
    }

    /* Shuts the sheet and leads to all the library's titles, over what is open. */
    function openLibrary() {
      leaveSheet();
      window.MMWatch.expand.library('', '', { fromSearch: true });
    }

    function downloadsWords() {
      var state = downloads && window.MM.downloading.last();
      var n = state ? state.summary.total : 0;
      return 'Downloads' + (n ? ' · ' + n : '');
    }

    function sayDownloads() {
      var link = byId('watch-option-downloads');
      if (link) link.querySelector('span').textContent = downloadsWords();
    }

    function allWords(n) { return 'All ' + titleCount(n); }

    document.addEventListener('mm:watch-total', function (e) {
      data.library_total = e.detail.total;
      var link = byId('watch-option-library');
      if (link && data.library_total) link.querySelector('span').textContent = allWords(data.library_total);
      else if (link) link.remove();
      if (!link || !data.library_total) outdated = true;
    });

    function drawLibrary(again) {
      var nodes = [];
      var n = data.library_total;
      if (n && data.grids && data.grids.library) {
        nodes.push(option(pill('watch-try', 'i-table-cells-large', allWords(n), openLibrary), 'library'));
      }
      var link = option(el('a', 'watch-try'), 'downloads');
      link.setAttribute('href', '/downloads');
      link.appendChild(MM.dom.icon('i-download'));
      link.appendChild(el('span', '', downloadsWords()));
      nodes.push(link);
      drawPart('Your library', 'watch-tries', again ? nodes : nodes.map(function (b, i) { return staged(b, 130 + i * 22); }));
    }

    function outline(className, parts) {
      var node = el('div', className);
      node.setAttribute('aria-hidden', 'true');
      parts.forEach(function (part) { node.appendChild(el('span', part)); });
      return node;
    }

    function tileOutline() { return outline('watch-tile--skel skel', []); }

    function personOutline() {
      return outline('watch-person watch-person--skel', ['watch-face skel skel--circle', 'skel skel--line', 'skel skel--line']);
    }

    /* A heading of the sheet over *nodes*, in the row or grid *className*. *beside* stands
       at the heading's end. */
    function drawPart(title, className, nodes, beside) {
      if (!nodes.length) return null;
      var part = el('div', className);
      part.setAttribute('role', 'group');
      part.setAttribute('aria-label', title);
      nodes.forEach(function (node) { part.appendChild(node); });
      var head = el('p', 'watch-sheet__title', title);
      if (beside) {
        var line = el('div', 'watch-sheet__head');
        line.appendChild(head);
        line.appendChild(beside);
        sheet.appendChild(line);
      } else {
        sheet.appendChild(head);
      }
      sheet.appendChild(part);
      return part;
    }

    /* What the AI knows, as the sheet is drawn from it. */
    function assist() { return (ai && ai.state()) || null; }

    function drawTonight() {
      if (!evening) {
        evening = window.MMWatch.tonight.make({
          option: option,
          say: function (text) { status.textContent = text; },
          gone: function () { if (picked && !shows(picked)) pick(null); },
          open: function (item) {
            var detail = window.MMWatch.detail;
            if (!detail) return false;
            leaveSheet();
            // On a phone the focus would bring its keyboard back.
            detail.open(item.ref, { origin: core.coarsePointer() ? null : input, title: item.title, kind: item.kind });
            return true;
          }
        });
      }
      evening.refresh();
      sheet.appendChild(evening.node);
    }

    function shows(node) { return node.isConnected && node.getClientRects().length > 0; }

    /* The person's own searches, and how to have them forgotten. */
    function drawRecent(list, again) {
      var wipe = option(el('button', 'txt', 'Clear'), 'clear-recent');
      wipe.type = 'button';
      wipe.setAttribute('aria-label', 'Clear recent searches');
      var part = drawPart('Recent', 'watch-tries', list.map(function (text, i) {
        var b = option(recentButton(text), 'recent-' + i);
        return again ? b : staged(b, 20 + i * 22);
      }), wipe);
      if (!part) return;
      var wrap = el('div', 'watch-recent');
      var inner = wrap.appendChild(el('div'));
      sheet.insertBefore(wrap, part.previousElementSibling);
      inner.appendChild(part.previousElementSibling);
      inner.appendChild(part);
      wipe.addEventListener('click', function () { clearRecent(wrap, wipe); });
    }

    function clearRecent(wrap, wipe) {
      if (wipe.hasAttribute('aria-busy')) return;
      wipe.setAttribute('aria-busy', 'true');
      ai.forget().then(function () {
        recorded = '';
        status.textContent = 'Your recent searches are cleared';
        wrap.classList.add('is-gone');
        wrap.inert = true;
        pick(null);
        setTimeout(function () {
          // The phrases were made from them.
          if (wrap.isConnected) drawSheet(sheetOf.all, sheetOf.waiting);
        }, core.reduceMotion() ? 0 : core.tokenMs('--dur-slow', 250));
      }, function (err) {
        wipe.removeAttribute('aria-busy');
        core.toast(MM.api.messageOf(err, 'Couldn’t clear your searches. Try again.'), { variant: 'error' });
      });
    }

    /* *first*: they lead, with the person's own where there are any; else they close. */
    function drawTries(first, knows, again, delay) {
      var own = first && knows.phrases.length > 0;
      var texts = own ? knows.phrases : TRIES;
      drawPart(first && !knows.recent.length ? 'Try' : 'Or try', 'watch-tries', texts.map(function (text, i) {
        var b = option(own ? phraseButton(text) : tryButton(text), 'try-' + i);
        return again ? b : staged(b, delay + i * 22);
      }), own ? el('small', '', 'From what you’ve watched and searched') : null);
    }

    function peopleComing() {
      try { return !window.localStorage.getItem(NO_PEOPLE); } catch (e) { return true; }
    }

    function rememberPeople(all) {
      if (!all.library) return; // could not be had
      try {
        if (all.people.length) window.localStorage.removeItem(NO_PEOPLE);
        else window.localStorage.setItem(NO_PEOPLE, '1');
      } catch (e) { /* storage blocked */ }
    }

    /* Draw the sheet from the categories and people of *all*; *waiting*: they are
       on their way, and their place is held. */
    function drawSheet(all, waiting) {
      // Under the person's eyes the AI's part neither comes nor changes, save by their own hand.
      var knows = sheetOn && sheetOf && !sheet.querySelector('.watch-recent.is-gone') ? sheetOf.knows : assist();
      if (!outdated && sheetOf && sheetOf.all === all && sheetOf.waiting === waiting && sheetOf.knows === knows) return;
      // Drawn anew under the person's eyes, what stays does not come in again.
      var again = sheetOn && !!sheetOf;
      var at = picked && sheet.contains(picked) ? picked.id : null;
      sheetOf = { all: all, waiting: waiting, knows: knows };
      outdated = false;
      var first = FIRST.map(function (id) { return all.genres.filter(function (g) { return g.id === id; })[0]; }).filter(Boolean);
      var more = all.genres.filter(function (g) { return first.indexOf(g) === -1; });
      var folk = all.people.slice(0, PEOPLE);
      var held = waiting && peopleComing();
      if (!waiting) rememberPeople(all);
      var offers = !!knows && knows.available;
      sheet.textContent = '';
      if (offers) drawTonight();
      if (knows) drawRecent(knows.recent, again);
      if (offers) drawTries(true, knows, again, 60);
      drawPart('Browse by category', 'watch-tiles', waiting ? times(FIRST.length, tileOutline) : first.map(genreTile));
      drawLibrary(again);
      drawPart('People in your library', 'watch-people', held ? times(PEOPLE, personOutline) : folk.map(personTile));
      if (!offers) drawTries(false, knows, again, held || folk.length ? 190 : 130);
      drawPart('More categories', 'watch-tiles', more.map(genreTile));
      if (at) pick(byId(at));
    }

    function openSheet(on) {
      on = on && canExplore() && !input.value && !tokened;
      if (on) explore.categories().then(function (all) { drawSheet(all, false); });
      if (on === sheetOn) return;
      if (on) {
        var preview = window.MMWatch.preview;
        if (preview && preview.close) preview.close();
        drawSheet(explore.all(), !explore.known());
        sheet.scrollTop = 0;
        setHint(ai && ai.available() ? ASK_HINT : example(0));
        if (ai) ai.load();
      }
      sheetOn = on;
      page.classList.toggle('is-sheet-on', on);
      document.documentElement.classList.toggle('has-sheet', on);
      clear.setAttribute('aria-label', on ? 'Close search' : 'Clear search');
      sheet.classList.toggle('is-on', on);
      sheet.inert = !on;
      [browse, byId('watch-all')].forEach(function (n) { if (n) n.inert = on; });
      sayList();
      pick(null);
    }

    function leaveSheet() {
      openSheet(false);
      input.blur();
    }

    /* The keyboard of a phone covers the bottom of the window, not of the page, and can push
       what is seen of it down. */
    function fitSheet() {
      var seen = window.visualViewport;
      if (!seen) return;
      bar.style.setProperty('--seen', Math.round(seen.height) + 'px');
      bar.style.setProperty('--seen-top', Math.round(seen.offsetTop) + 'px');
    }

    /* ── What stands under the bar ── */

    /* The rows fade and shrink behind what replaces them, and grow back. */
    function show(to) {
      if (view === to) return;
      var was = view;
      view = to;
      if (to === 'browse') {
        clearTimeout(hiding);
        leave();
        hideAsk();
        results.hidden = true;
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

    /* ── Ask AI ── */

    function heldPage() { return [ordinary, live && live.node]; }

    /* As core.flip, of those of *nodes* that show. */
    function flip(nodes) {
      nodes = nodes.filter(function (n) { return n && n.getClientRects().length; });
      return nodes.length && moves(nodes[0]) ? core.flip(nodes) : function () {};
    }

    /* *change* makes what is asked taller or shorter: what stands below glides after it. */
    function around(change, nodes) {
      var move = flip(heldPage().concat(nodes || []));
      change();
      ordinary.hidden = !asking.item;
      move();
    }

    function askPlace() {
      var host = el('section', 'ask');
      host.setAttribute('aria-label', 'Ask AI');
      ordinary = el('div', 'ordinary');
      ordinary.appendChild(el('h2', 'ordinary__title', 'Search results'));
      ordinary.hidden = true;
      results.insertBefore(ordinary, results.firstChild);
      results.insertBefore(host, ordinary);
      asking = ai.view(host, { ask: fillAsk, stopped: function () { ai.keep({ q: null }); }, around: around });
    }

    function hideAsk() { if (asking && asking.item) asking.hide(); }

    /* A sentence is offered to the AI, or shows what came of it, while the search answers below. */
    function offerAsk(q) {
      if (!ai) return;
      if (!ai.fits(q)) { hideAsk(); return; }
      if (!asking) askPlace();
      asking.row(q);
      asking.pick(!picked);
    }

    function askNow(q) {
      var item = ai.question('ask', q);
      if (item.state !== 'new' && item.state !== 'refused') return;
      ai.keep({ q: item.value });
      ai.start(item);
      offerAsk(q);
    }

    function fillAsk(text) {
      if (!ai.fits(text)) { fill(text); return; }
      input.value = text;
      input.focus({ preventScroll: true });
      typed(true);
      record(text);
      askNow(text);
      if (core.coarsePointer()) input.blur();
      toTop();
    }

    /* The question that was on the page when it was left. */
    function askAgain() {
      var q = input.value.trim();
      if (!q || tokened || !ai || view !== 'results') return;
      offerAsk(q);
      if (ai.fits(q) && ai.kept().q === ai.question('ask', q).value) askNow(q);
    }

    /* A search the person meant: one pressed, entered or followed to a title. */
    function record(q) {
      if (!ai || !q || q === recorded) return;
      recorded = q;
      ai.record(q);
    }

    /* ── Titles like one ── */

    function likeFace(poster) {
      var node = el('span', 'watch-face watch-face--art');
      node.setAttribute('aria-hidden', 'true');
      node.appendChild(el('span'));
      var src = safePath(poster);
      if (src) {
        var img = node.appendChild(el('img'));
        img.src = src;
        img.alt = '';
        img.setAttribute('data-broken-on-error', '');
      }
      return node;
    }

    /* *asked*: it is asked at once, else offered. */
    function showLike(ref, name, poster, asked) {
      var node = el('div', 'results-page results-page--like');
      var host = node.appendChild(el('section', 'ask'));
      host.setAttribute('aria-label', 'More like ' + name);
      var place = ai.view(host, {
        ask: fillAsk,
        stopped: function () { ai.keepLike({ ref: ref, name: name, poster: poster, asked: false }); },
        around: function (change, nodes) {
          var move = flip(nodes || []);
          change();
          move();
        }
      });
      window.MMWatch.search.stage.open(node, 'More like ' + name, { icon: likeFace(poster), name: 'Like ' + name });
      var item = ai.question('like', ref, name);
      ai.keepLike({ ref: ref, name: name, poster: poster, asked: asked });
      if (asked && (item.state === 'new' || item.state === 'refused')) ai.start(item);
      place.show(item);
      host.querySelector('.ask__row').focus({ preventScroll: true });
    }

    function canLike(ref) { return !!ai && ai.available() && LIKE_RE.test(ref || ''); }

    /* Titles like *ref*, a page of their own; *name* and *poster* are the title's. */
    function like(ref, name, poster) {
      if (!canLike(ref)) return;
      address.push('like', address.url(Object.assign({}, PAGE_PARAMS, { q: null, like: ref }), ''));
      showLike(ref, name, poster || '', true);
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

    function personSuggestion(p, sub, typed) {
      return suggestion('p-' + p.person_id, 'person', {
        name: p.name, sub: sub, typed: typed,
        pic: function () { return face(p.profile_url, p.name); },
        act: function (b) { explore.openPerson(p, b.querySelector('.watch-face')); }
      });
    }

    /* The people *q* names. */
    function peopleFor(q, c) {
      var typed = q.toLowerCase();
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
      return canExplore() ? who.slice(0, 3) : [];
    }

    /* *lead*: the people come first. */
    function suggestionsFor(q, c, lead) {
      var typed = q.toLowerCase();
      var asked = canExplore() ? explore.parse(q) : { genres: [], words: '' };
      var cats = asked.genres.slice(0, 2);
      var who = peopleFor(q, c);
      var list = cats.map(function (genre) { return genreSuggestion(genre, asked, asked.sort, asked.words); });
      if (cats.length === 1 && !who.length) {
        ['top_rated', 'newest'].forEach(function (sort) {
          if (sort !== asked.sort) list.push(genreSuggestion(cats[0], asked, sort, ''));
        });
        (cats[0].actors || []).slice(0, 3).forEach(function (p) {
          list.push(personSuggestion(p, 'In ' + p.titles + ' ' + lower(cats[0].name) + ' titles', ''));
        });
      }
      var named = who.map(function (p) {
        return personSuggestion(p, [p.job].concat((p.known_for || []).slice(0, 2).join(', ')).filter(Boolean).join(' · '), typed);
      });
      list = lead ? named.concat(list) : list.concat(named);
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
        [s.lib, s.tmdb].forEach(function (part) { if (part) all = all.concat(part.held || [], part.items || []); });
      });
      return all;
    }

    /* What is downloading with *q* in its name, as the page knows it already: nobody is asked. */
    function downloadsFor(q) {
      var state = downloads && window.MM.downloading.last();
      var typed = folded(q);
      return state && typed ? state.items.filter(function (item) { return folded(item.title).indexOf(typed) !== -1; }) : [];
    }

    function downloadRows(list) {
      return [headRow('dl', 'Downloading', list.length)].concat(list.map(function (item) {
        var ref = downloads.ref(item);
        var poster = downloads.picture(item.poster_url);
        return {
          key: 'dl-' + item.key,
          sig: JSON.stringify([item.title, ref, downloads.href(item), poster]),
          make: function () {
            var link = el('a', 'results-dl');
            link.setAttribute('href', downloads.href(item));
            if (ref) {
              link.setAttribute('data-title-ref', ref);
              link.setAttribute('data-title', item.title);
              link.setAttribute('data-kind', item.kind === 'movie' ? 'movie' : 'show');
            }
            link.appendChild(small(poster));
            var text = link.appendChild(el('span', 'results-dl__text'));
            text.appendChild(el('b', null, item.title));
            text.appendChild(el('small'));
            return link;
          },
          update: function (node) { node.querySelector('small').textContent = downloads.line(item); }
        };
      }));
    }

    function withDownloads(q, rows) {
      var list = downloadsFor(q);
      if (!list.length) return rows;
      return downloadRows(list).concat(rows.filter(function (x) { return x.key !== NO_TITLE && x.key.indexOf(NOTHING) !== 0; }));
    }

    /* The suggestions for *q*, and *rows* in the grid, which moves with them.
       Their column goes only once it is known to stay empty. */
    function draw(q, c, rows, settled) {
      live.rows = rows;
      rows = withDownloads(q, rows);
      glide(live.grid, rows, function () {
        // With no title to show, the people are what was found.
        var lead = rows.some(function (x) { return x.key === NO_TITLE; }) && peopleFor(q, c).length > 0;
        var list = suggestionsFor(q, c, lead);
        var at = picked && picked.id;
        if (list.length || settled) live.node.classList.toggle('has-no-suggestions', !list.length);
        live.side.classList.toggle('is-titles-only', list.every(function (x) { return x.key.charAt(0) === 't'; }));
        resize(lead, function () { glide(live.list, list, function () { live.node.classList.toggle('is-people', lead); }); });
        sayList();
        // While the person types they are not read out.
        if (c && !settled && !status.textContent) status.textContent = suggestionCount();
        pick(at ? options().filter(function (n) { return n.id === at; })[0] || null : null);
      });
    }

    /* Where people come to lead, or no longer do, *change* gives their photos and
       their words another size: they grow or shrink to it. */
    function resize(lead, change) {
      var node = live.node;
      if (node.classList.contains('is-people') === lead || !moves(node)) { change(); return; }
      var faces = core.toArray(node.querySelectorAll('.watch-suggestion .watch-face'));
      var was = faces.map(function (n) { return n.getBoundingClientRect().height; });
      change();
      faces.forEach(function (n, i) {
        var to = n.getBoundingClientRect().height;
        if (!was[i] || !to || was[i] === to || !node.contains(n)) return;
        n.animate([{ transform: 'scale(' + was[i] / to + ')', transformOrigin: '0 0' }, { transform: 'none', transformOrigin: '0 0' }], { duration: 300, easing: spring() });
        play(n.closest('.watch-suggestion').querySelector('.watch-suggestion__text'), [{ opacity: 0 }, { opacity: 1 }], 220);
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
      if (asking) asking.pick(!node);
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
      if (picked && !shows(picked)) pick(null);
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
      if (canExplore()) {
        var row = el('div', 'watch-tries');
        TRIES_EMPTY.forEach(function (text) { row.appendChild(tryButton(text)); });
        box.appendChild(row);
      }
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
      if (asking && asking.item) {
        live.node.appendChild(el('p', 'results-none', 'No title has these words in its name.'));
        rise(live.node);
        status.textContent = ['No matches for “' + c.q + '”', askHint()].filter(Boolean).join('. ');
        return;
      }
      var block = nothing(c.q, true);
      live.node.appendChild(block);
      if (notSetUp) live.node.appendChild(hint(data));
      rise(live.node);
      block.look();
      status.textContent = 'No matches for “' + c.q + '”';
    }

    /* The whole search failed: one line, one way on that runs all of it again. */
    function showFailed(text) {
      leave();
      hold('failed', 'Search results');
      live.node.appendChild(failure(text, function () {
        input.focus();
        run();
      }));
      rise(live.node);
      status.textContent = text;
    }

    /* The two lists as they are shown. What TMDB knows joins the one it belongs
       to; titles of a category that was named in full come after the matches. */
    function gather(c) {
      var held = c.tmdb.held || []; // what the library's own search did not find
      var lib = (c.lib.items || []).concat(held).map(function (item) { return { item: item, key: item.ref || item.href, build: buildCard }; });
      var out = [];
      var total = (c.lib.total || 0) + held.length;
      var seen = new Set(lib.map(function (x) { return x.key; }));
      [c.tmdb.items, c.cat && c.cat.items].forEach(function (items) {
        (items || []).forEach(function (item) {
          var key = item.library_ref || item.ref;
          if (seen.has(key) || seen.has(item.ref)) return;
          seen.add(key).add(item.ref);
          if (isHeld(item)) {
            lib.push({ item: item, key: key, build: buildHeldCard });
            // One of the library's that matches by name too is among those found and not shown.
            if (!item.in_library || folded(item.title).indexOf(folded(c.q)) === -1) total++;
          } else out.push({ item: item, key: key, build: buildTmdbCard });
        });
      });
      return { lib: lib, out: out, total: Math.max(total, lib.length) };
    }

    function suggestionCount() {
      var n = sheetOn ? 0 : options().length;
      return n ? n + (n === 1 ? ' suggestion' : ' suggestions') : '';
    }

    function askHint() { return asking && asking.item && asking.offered() ? 'Press Return to ask AI' : ''; }

    function readOut(c, all, coming) {
      var parts = coming ? [coming + ' downloading'] : [];
      if (c.lib.state === 'ok' && all.lib.length) parts.push((all.total > all.lib.length ? all.lib.length + ' of ' : '') + all.total + ' in your library');
      if (all.out.length) parts.push(all.out.length + ' available to download');
      status.textContent = [parts.length
        ? parts.join(', ') + ' for “' + c.q + '”'
        : 'No matches in your library for “' + c.q + '”', suggestionCount(), askHint()].filter(Boolean).join('. ');
    }

    function headRow(key, title, count, of) {
      return {
        key: 'head-' + key,
        make: function () {
          var head = groupHead(title, count, of);
          head.setAttribute('data-results-head', key);
          return head;
        },
        update: function (node) { setCount(node, count, of); }
      };
    }

    function skeletonRows(key) {
      return times(SKELETONS, function (n) { return { key: 'skel-' + key + n, make: skeleton }; });
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
      if (downloads) shownDownloads = downloadsOf(c.q);
      var lib = c.lib;
      var tmdb = c.tmdb;
      var cat = c.cat ? c.cat.state : 'ok';
      var all = gather(c);
      var tmdbQuiet = tmdb.state === 'unconfigured' || (tmdb.state === 'ok' && !tmdb.items.length);
      var settled = lib.state !== 'loading' && tmdb.state !== 'loading' && cat !== 'loading';
      var coming = downloadsFor(c.q);
      all.out = all.out.filter(function (x) { return !coming.some(function (item) { return downloads.ref(item) === x.item.ref; }); });
      setPending(!settled);

      if (settled && !all.lib.length && !all.out.length && !coming.length) {
        if (lib.state === 'failed' && (tmdbQuiet || tmdb.state === 'failed')) { showFailed(SEARCH_FAILED); return; }
        if (lib.state === 'ok' && tmdbQuiet && cat === 'failed') { showFailed(SEARCH_FAILED); return; }
        if (lib.state === 'ok' && tmdbQuiet && !suggestionsFor(c.q, c).length) {
          if (cat === 'unknown') { showFailed(CATEGORIES_FAILED); return; }
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

      // People who lead are there until a title is.
      if (!settled && !all.lib.length && !all.out.length && live.node.classList.contains('is-people') && peopleFor(c.q, c).length) {
        live.grid.removeAttribute('aria-busy');
        draw(c.q, c, live.rows, false);
        return;
      }

      var rows = [];
      if (lib.state === 'loading' && !lib.items) rows = rows.concat(headRow('lib', 'In your library'), skeletonRows('lib'));
      else if (lib.state === 'failed') {
        rows.push(headRow('lib', 'In your library'), failRow(c, 'lib', lib.text, function () { fetchLibrary(c); paint(c); }));
        rows = rows.concat(cardRows(all.lib));
      } else if (all.lib.length) {
        rows = rows.concat(headRow('lib', 'In your library', all.lib.length, all.total), cardRows(all.lib));
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
        if (peopleFor(c.q, c).length) {
          rows.push({ key: NO_TITLE, sig: c.q, make: function () { return el('p', 'results-none', 'No title matches “' + c.q + '”'); } });
        } else {
          rows.push(plain(NOTHING + c.q, function () { return nothing(c.q, false); }));
        }
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
        readOut(c, all, coming.length);
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
      c[part] = Object.assign({}, c[part], { state: 'loading' });
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
        return { state: 'ok', items: reply.items || [], total: reply.total };
      }, function (err) {
        return { state: 'failed', text: MM.api.messageOf(err, SEARCH_FAILED) };
      });
    }

    function fetchTmdb(c) {
      ask(c, 'tmdb', TMDB_URL + encodeURIComponent(c.q), function (reply) {
        if (reply.tmdb_configured === false) return { state: 'unconfigured' };
        var found = reply.people || [];
        found.forEach(function (p) { people.set(p.person_id, p); });
        return { state: 'ok', items: reply.items || [], held: reply.held || [], people: found };
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
          else if (c.q.length > 1) c.cat = { state: 'unknown' };
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

    function setEmptied(on) {
      clearTimeout(emptiedTimer);
      emptied = on;
      if (on) emptiedTimer = setTimeout(function () { emptied = false; }, STEP_MS);
    }

    /* Keep ?q= in the address: a search is an entry, so Back returns to what it was
       typed over, and what is typed next stays in it. *inPlace*: nobody typed it. */
    function syncUrl(q, inPlace) {
      if (q && !inPlace && address.value('search') === null) {
        address.push('search', address.url(Object.assign({}, PAGE_PARAMS, { q: q })));
        // Outlives a reload: emptied, the search gives way to that.
        window.history.replaceState(Object.assign({}, window.history.state, { mmOver: true }), '');
        return;
      }
      var to = address.url({ q: q || null });
      if (to !== window.location.pathname + window.location.search + window.location.hash) address.replace('search', to);
    }

    function showResults(q, inPlace) {
      // Where the rows stood is kept with the entry they are left in.
      var rows = view === 'browse' && !browse.hidden;
      if (rows) syncUrl(q, inPlace);
      show('results');
      if (!rows) syncUrl(q, inPlace);
    }

    function run(inPlace) {
      var q = input.value.trim();
      stop();
      if (!q) {
        syncUrl(q);
        status.textContent = '';
        show('browse');
        return;
      }
      showResults(q, inPlace);
      offerAsk(q);
      var known = last && last.q === q ? last : null;
      var c = {
        q: q, ctrl: new AbortController(), refocus: null,
        lib: known && { items: known.lib.items, total: known.lib.total }, tmdb: known && { items: known.tmdb.items, held: known.tmdb.held }, cat: null,
        // What is on the page already and fits may stay while TMDB is asked.
        before: last && !known ? gather(last).out.filter(function (x) { return begins(x.item.title, q.toLowerCase()); }) : []
      };
      current = c;
      setPending(true);
      // Emptied now, so the same answer is read out again when it comes.
      status.textContent = '';
      fetchLibrary(c);
      if (data.tmdb_configured !== true) c.tmdb = { state: 'unconfigured' };
      else fetchTmdb(c);
      if (canExplore()) askCategory(c, known && known.cat);
      paint(c);
      if (live && live.grid && live.drawn && !c.lib.items) live.grid.setAttribute('aria-busy', 'true');
    }

    /* Every key: the bar and the suggestions answer at once, the server is asked
       when the person pauses, for longer inside a word that an input method is
       still composing. */
    function typed(now, inWord) {
      var q = ownWords = input.value.trim();
      clearTimeout(timer);
      composed = inWord ? input.value : '';
      if (tokened) setToken(null);
      sync();
      stop();
      if (!q && address.value('search') !== null && (window.history.state || {}).mmOver) {
        openSheet(false);
        if (emptied) return;
        setEmptied(true);
        address.back('search', function () {
          setEmptied(false);
          // What opens there takes the focus.
          input.focus({ preventScroll: true });
          openSheet(address.top() === null);
        });
        return;
      }
      if (!q || now) {
        openSheet(false);
        run();
        if (!input.value) openSheet(focused());
        return;
      }
      openSheet(false);
      if (address.value('search') === null) showResults(q);
      else show('results');
      offerAsk(q);
      holdResults();
      draw(q, null, live.rows, false);
      if (canExplore() && !explore.known()) {
        explore.categories().then(function () {
          if (!current && live && live.kind === 'results' && input.value.trim() === q) draw(q, null, live.rows, false);
        });
      }
      setPending(true);
      timer = setTimeout(run, inWord ? COMPOSING_MS : SEARCH_DELAY_MS);
    }

    function fill(text) {
      input.value = text;
      input.focus({ preventScroll: true });
      typed(true);
      record(text);
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

    input.addEventListener('input', function (e) { typed(false, e.isComposing === true); });
    // Not for a word that was searched for, nor one that a page has taken out of the field.
    input.addEventListener('compositionend', function () {
      if (composed && composed === input.value && !(current && current.q === composed.trim())) typed(false);
    });
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
        else if (sheetOn) leaveSheet();
        else return;
        e.preventDefault();
        return;
      }
      if (e.key === 'Tab') {
        // At once: the browser looks for the next stop as soon as this is over.
        openSheet(false);
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
      if (tokened || !input.value) return;
      clearTimeout(timer);
      run();
      var q = input.value.trim();
      record(q);
      if (ai && ai.fits(q)) askNow(q);
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
    results.addEventListener('click', function (e) {
      var q = input.value.trim();
      if (view === 'results' && q === ownWords && e.target.closest('.watch-suggestion, [data-title-ref]')) record(q);
    });
    veil.addEventListener('click', leaveSheet);
    clear.addEventListener('click', function () {
      if (sheetOn) leaveSheet();
      else clearSearch();
    });

    function modalOpen() {
      return core.toArray(document.querySelectorAll('[aria-modal="true"]')).some(function (n) { return n.getClientRects().length > 0; });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key !== '/' || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      var a = document.activeElement;
      if (a && (/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName) || a.isContentEditable || (a.closest && a.closest('[role="menu"]')))) return;
      // Inert: under the storage panel, which is no aria-modal.
      if (modalOpen() || input.closest('[inert]')) return;
      e.preventDefault();
      // On a screen of little height the bar goes with the page.
      if (box.getBoundingClientRect().top < 0) toTop();
      input.focus({ preventScroll: true });
      input.select();
      openSheet(true);
    });

    window.addEventListener('pageshow', function (e) { if (e.persisted) openSheet(false); });

    if (window.visualViewport) {
      ['resize', 'scroll'].forEach(function (type) { window.visualViewport.addEventListener(type, fitSheet); });
    }
    fitSheet();
    window.addEventListener('resize', function () {
      // A narrow window shows no titles among the suggestions.
      sayList();
      if (!focused()) hintText.textContent = rest();
      fitAgain();
    });
    // The meter's words arrive after the page, and change.
    if (window.ResizeObserver) {
      // In the next frame: it may change the meter's size itself.
      var sized = new window.ResizeObserver(function () { window.requestAnimationFrame(fitBetween); });
      [meter || profile, bar.querySelector('.brand')].forEach(function (n) { sized.observe(n); });
    }
    bar.addEventListener('focusin', sync);
    bar.addEventListener('focusout', function () { setTimeout(sync); });
    // WebKit still has the old width as it tells of a resize.
    phone.addEventListener('change', function () {
      place();
      window.requestAnimationFrame(fitAgain);
    });
    // Words that grow in a meter as wide as it can be do not change its size.
    if (meter && window.MutationObserver) {
      new window.MutationObserver(fitAgain).observe(meter, { childList: true, characterData: true, subtree: true });
    }
    place();

    /* ── A page in the results' place ── */

    window.MMWatch.search.stage = {
      /* *node* takes the place of what stands under the bar; *what* names it there. */
      open: function (node, name, what) {
        stop();
        input.value = '';
        openSheet(false);
        pick(null);
        show('page');
        hideAsk();
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

    function downloadsOf(q) { return JSON.stringify(downloadsFor(q)); }

    // They come in, or are there, while the search is on show.
    if (downloads) {
      window.MM.downloading.subscribe(function () {
        sayDownloads();
        var c = current;
        if (!c || view !== 'results' || shownDownloads === downloadsOf(c.q)) return;
        paint(c);
      });
    }

    address.layer({
      name: 'search',
      depth: 30,
      params: ['q'],
      read: function (here) {
        return (here.params.get('q') || '').trim() || null;
      },
      open: function (q, how) {
        if (how === 'pop') setEmptied(false);
        if (input.value.trim() === q && (how === 'start' || (current && current.q === q))) return;
        input.value = q;
        if (tokened) setToken(null);
        sync();
        run();
        askAgain();
      },
      close: function (how) {
        var again = emptied && input.value.trim();
        var text = input.value;
        setEmptied(false);
        // Whatever else was opened says what stands here now.
        if (how === 'push') return;
        // Typed since, it is a search of its own in the entry the step led to.
        if (again) {
          setTimeout(function () {
            // A page the step led to took the field.
            if (view === 'page' && !input.value) {
              input.value = text;
              typed(true);
            } else if (current && current.q === input.value.trim()) {
              syncUrl(current.q);
            }
          });
          return;
        }
        stop();
        input.value = '';
        sync();
        status.textContent = '';
        if (view === 'results') show('browse');
      }
    });

    address.layer({
      name: 'like',
      depth: 25,
      params: ['like'],
      read: function (here) {
        var ref = here.params.get('like');
        var other = ['q', 'genre', 'person', 'all'].some(function (key) { return here.params.get(key); });
        return ai && !other && LIKE_RE.test(ref || '') ? ref : null;
      },
      open: function (ref) {
        // Only what the person asked for here is asked for again.
        (ai.state() ? Promise.resolve() : ai.load()).then(function () {
          if (address.value('like') !== ref) return;
          var was = ai.keptLike(ref);
          if (was) showLike(ref, was.name, was.poster, was.asked);
          else address.replace('like', address.url({ like: null }));
        });
      },
      close: function () { if (view === 'page' && live && live.node.classList.contains('results-page--like')) window.MMWatch.search.stage.close(); }
    });

    window.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || e.defaultPrevented || composing(e) || address.top() !== 'like') return;
      e.preventDefault();
      address.back('like');
    });

    sync();
    hintText.textContent = rest();
    sayList();
    showExamples();
    if (canExplore()) explore.categories();
    if (ai && data.tmdb_configured === true) ai.load().then(askAgain);
    window.MMWatch.search.like = like;
    window.MMWatch.search.canLike = canLike;
    // A value the browser put back (going back to the page) gets its results.
    if (input.value.trim()) run(true);
    // Nothing the address names after all: the rows.
    setTimeout(function () { if (view === 'results' && !live) show('browse'); });
  }

  window.MMWatch.search = { init: initSearch, card: libraryCard, tmdbCard: buildTmdbCard, glide: glide, leavingBadge: leavingBadge, setLeaving: setLeaving, isHeld: isHeld, composing: composing };
})();
