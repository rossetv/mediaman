/* Watch page: the Discover rows (Recommended for you, Trending this week,
 * Popular movies, Popular TV) that load after the page in place of their
 * skeleton, the badges on their posters, the menu of earlier picks, which
 * follows the row, and each person's Refresh of their own picks; the rest of the library rows as they are scrolled;
 * and what each of these rows holds when it is opened as a grid.
 * Needs watch/core.js, watch/address.js, watch/keep.js, watch/search.js, watch/rows-load.js, watch/making.js and watch/expand.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var address = watch.address;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var byId = core.byId;
  var toArray = core.toArray;
  var safePath = core.safePath;

  var DISCOVER_URL = '/api/watch/discover';
  var RECOMMENDED_URL = '/api/watch/recommended';
  var LIBRARY_URL = '/api/watch/library/';
  var OWN_URL = '/api/watch/picks/refresh';
  var SETTINGS_HREF = '/settings#setg-general';
  var SHARED_HREF = '/settings#setg-integrations';
  var NO_KEY = 'New ones need an OpenAI key, set in Settings.';
  var REC_TITLE = 'Recommended for you';
  var DISCOVER_ROWS = [
    ['trending', 'Trending this week'],
    ['popular_movies', 'Popular movies'],
    ['popular_tv', 'Popular TV']
  ];
  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$|^(rec|pick)-[0-9]{1,10}$/;
  var PICK_RE = /^(rec|pick)-[0-9]{1,10}$/;
  var GRID_PAGE = 60;
  var DAY_MS = 86400000;
  var POLL_MS = 3000;
  var POLL_MAX_MS = 30000;
  var MAX_FAILURES = 8;
  var LEFT_CARDS = 3; // at the row's left, whose batch is the one named
  var FADE_MS = 220; // past --dur
  var SWAP_FALLBACK_MS = 400; // past the slowest motion token, for an animation that never runs

  function toLogin() {
    var here = window.location;
    var place = here.pathname + here.search;
    here.href = '/login' + (place === '/watch' ? '' : '?next=' + encodeURIComponent(place)) + here.hash;
  }

  /* A 401: the session ran out, so the login page, not a "Couldn't load". */
  function loggedOut(err) {
    if (!(err && err.status === 401)) return false;
    toLogin();
    return true;
  }

  /* What a load gives while the browser leaves for the login page: nothing,
     so the skeleton stays until it does. */
  function never() { return new Promise(function () {}); }

  /* ── Cards ── */

  /* The download state on a poster, or null when the title can be downloaded. */
  function badge(state, pct, inLibrary) {
    var node;
    if (state === 'downloading') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--downloading');
      node.appendChild(icon('i-arrow-down'));
      if (typeof pct === 'number') {
        node.appendChild(el('span', 'sr-only', 'Downloading '));
        node.appendChild(document.createTextNode(pct + '%'));
      } else {
        node.appendChild(document.createTextNode('Downloading'));
      }
    } else if (state === 'queued') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--queued');
      node.appendChild(icon('i-clock'));
      node.appendChild(document.createTextNode('Queued'));
    } else if (inLibrary || state === 'in_library' || state === 'partial') {
      node = el('span', 'watch-badge watch-badge--state watch-badge--in');
      node.appendChild(icon('i-check'));
      node.appendChild(document.createTextNode('In library'));
    }
    return node || null;
  }

  /* One poster card, the markup of _watch_card.html's card(). The link
     keeps a real address for a new tab; a plain click opens the detail. */
  function card(item) {
    var ref = item && typeof item.ref === 'string' && REF_RE.test(item.ref) ? item.ref : '';
    if (!ref) return null;
    var article = el('article', 'watch-card');
    if (item.keep_id) article.setAttribute('data-keep-id', item.keep_id);
    if (item.leaving_id) watch.search.setLeaving(article, item);
    var link = el('a', 'watch-card__link');
    link.setAttribute('href', safePath(item.href) || '/watch#title=' + ref);
    link.setAttribute('data-title-ref', ref);
    if (item.kind === 'movie' || item.kind === 'show') link.setAttribute('data-kind', item.kind);
    if (item.in_library) link.setAttribute('data-in-library', '');
    if (item.tmdb_id > 0) link.setAttribute('data-tmdb-id', String(Number(item.tmdb_id)));
    if (PICK_RE.test(item.pick_ref || '')) link.setAttribute('data-pick-ref', item.pick_ref);
    var poster = el('span', 'watch-card__poster');
    var src = safePath(item.poster_url);
    if (src) {
      var img = el('img');
      img.setAttribute('src', src);
      img.setAttribute('alt', '');
      img.setAttribute('loading', 'lazy');
      img.setAttribute('data-broken-on-error', '');
      poster.appendChild(img);
    }
    poster.appendChild(el('span', 'watch-card__title', String(item.title || '')));
    [watch.search.leavingBadge(item), badge(item.download_state, item.download_pct, item.in_library)].forEach(function (b) {
      if (b) poster.appendChild(b);
    });
    link.appendChild(poster);
    var kind = item.kind === 'movie' ? 'Movie' : 'TV';
    var sub = el('span', 'watch-card__sub', typeof item.year === 'number' ? kind + ' · ' + item.year : kind);
    link.appendChild(item.ai_pick ? pickCaption(sub) : sub);
    article.appendChild(link);
    core.addMarks(article, poster, item);
    return article;
  }

  /* A pick's caption: the row's chip has the words of its icon. */
  function pickCaption(sub) {
    var caption = el('span', 'watch-card__cap');
    var mark = el('span', 'watch-card__ai');
    mark.appendChild(icon('i-sparkles'));
    mark.appendChild(el('span', 'sr-only', 'AI pick, '));
    caption.appendChild(mark);
    caption.appendChild(sub);
    return caption;
  }

  /* A card of the title under another ref (a pick, a result): it prints the
     same title, kind and year. */
  function sameTitle(link, detail) {
    if (!detail.title || !detail.kind) return false;
    var title = link.querySelector('.watch-card__title');
    var sub = link.querySelector('.watch-card__sub');
    if (!title || !sub || title.textContent !== detail.title) return false;
    return sub.textContent === (detail.kind === 'movie' ? 'Movie' : 'TV') + (detail.year ? ' · ' + detail.year : '');
  }

  /* A title's state changed (the detail queued it, or a download moved on):
     every card of it on the page shows the new badge. */
  function updateBadges(detail) {
    if (!detail || typeof detail.ref !== 'string') return;
    toArray(document.querySelectorAll('[data-title-ref]')).forEach(function (link) {
      if (link.getAttribute('data-title-ref') !== detail.ref && !sameTitle(link, detail)) return;
      var poster = link.querySelector('.watch-card__poster');
      if (!poster) return;
      var old = poster.querySelector('.watch-badge--state');
      var fresh = badge(detail.download_state, detail.download_pct, link.hasAttribute('data-in-library'));
      if (old && fresh) old.replaceWith(fresh);
      else if (old) old.remove();
      else if (fresh) poster.appendChild(fresh);
    });
  }

  /* ── Rows ── */

  function rowSection(id, title) {
    var section = el('section', 'watch-row');
    section.setAttribute('data-discover-row', id);
    section.setAttribute('data-row-id', id);
    section.appendChild(watch.expand.head(id, title));
    return section;
  }

  function cardsOf(items) {
    return (items || []).map(card).filter(Boolean);
  }

  /* The scrolling strip of cards, or null when none of *items* can be shown. */
  function strip(title, items) {
    var cards = cardsOf(items);
    if (!cards.length) return null;
    var viewport = el('div', 'watch-row__viewport');
    var scroller = el('div', 'watch-row__scroll');
    scroller.setAttribute('tabindex', '0');
    scroller.setAttribute('role', 'region');
    scroller.setAttribute('aria-label', title);
    cards.forEach(function (c) { scroller.appendChild(c); });
    viewport.appendChild(scroller);
    return viewport;
  }

  /* A quiet line in a row's place: nothing to show, or it did not load. */
  function note(text, extra) {
    var p = el('p', 'watch-row__note');
    p.appendChild(el('span', null, text));
    if (extra) p.appendChild(extra);
    return p;
  }

  function failedNote(title, retry) {
    var button = el('button', 'watch-row__retry', 'Try again');
    button.type = 'button';
    button.setAttribute('data-discover-retry', retry);
    var p = note('Couldn’t load ' + title + '.', button);
    p.classList.add('watch-row__note--failed');
    p.setAttribute('role', 'alert');
    return p;
  }

  function startRows(root) {
    // Until it loads; then watch-rows.js starts every row it finds.
    if (watch.rows) toArray(root.querySelectorAll('.watch-row__viewport')).forEach(watch.rows.init);
    var own = root.closest('[data-row-id]');
    (own ? [own] : toArray(root.querySelectorAll('[data-row-id]'))).forEach(watch.expand.row);
    address.putRowsBack(root);
  }

  /* Swap a row's body (its strip or note) for another in place. */
  function swapBody(oldBody, newBody) {
    var done = core.swap(oldBody, newBody);
    startRows(newBody.parentNode || newBody);
    return done;
  }

  /* ── Discover rows ── */

  var loaders = {}; // what loads the rest of each TMDB and library row, by id

  /* The later pages of one TMDB row; of one *kind* ("movie" or "tv") when given. */
  function discoverPages(id, kind) {
    return function (page) {
      return MM.api.get(DISCOVER_URL + '?row=' + id + '&page=' + page).then(function (data) {
        return { cards: ofKind(cardsOf(data.items), kind), next: data.next_page };
      });
    };
  }

  function ofKind(cards, kind) {
    if (kind !== 'movie' && kind !== 'tv') return cards;
    return cards.filter(function (c) {
      return (c.querySelector('.watch-card__link').getAttribute('data-kind') === 'movie') === (kind === 'movie');
    });
  }

  /* A TMDB row's grid goes on from where the row stands, or starts where the row is not here yet. */
  function discoverGrid(at) {
    var loader = at.row && loaders[at.id];
    function from(kind) {
      var pages = discoverPages(at.id, kind);
      if (loader) return { cards: ofKind(at.cards(), kind), next: loader.next(), fetchPage: pages };
      return { cards: [], next: 'first', fetchPage: function (page) { return page === 'first' ? firstOf(at.id, kind) : pages(page); } };
    }
    var made = from(at.sort);
    if (at.id === 'trending') {
      made.sort = { label: 'Show', choose: function (kind) { return from(kind); } };
    }
    return made;
  }

  function firstOf(id, kind) {
    return discover().then(function (data) {
      var row = (data.rows || []).filter(function (r) { return r.id === id; })[0];
      return { cards: row ? ofKind(cardsOf(row.items), kind) : [], next: row ? row.next_page : null };
    });
  }

  function discoverRow(id, title, items, next) {
    var body = strip(title, items);
    if (!body) return null;
    loaders[id] = watch.rowsLoad.start(body, { title: title, next: next, fetchPage: discoverPages(id) });
    var section = rowSection(id, title);
    section.appendChild(body);
    return section;
  }

  function discoverFailed(err) {
    if (loggedOut(err)) return never();
    return DISCOVER_ROWS.map(function (r) {
      var section = rowSection(r[0], r[1]);
      section.appendChild(failedNote(r[1], 'discover'));
      return section;
    });
  }

  var asking = null;

  /* Asked for once for the rows and a grid opened before they are here. */
  function discover() {
    if (!asking) {
      asking = MM.api.get(DISCOVER_URL);
      var done = function () { asking = null; };
      asking.then(done, done);
    }
    return asking;
  }

  /* The three TMDB rows, as sections; a failure gives each its Try again. */
  function loadDiscover() {
    return discover().then(function (data) {
      var titles = {};
      DISCOVER_ROWS.forEach(function (r) { titles[r[0]] = r[1]; });
      return (data.rows || []).map(function (row) {
        return titles[row.id] ? discoverRow(row.id, titles[row.id], row.items, row.next_page) : null;
      }).filter(Boolean);
    }, discoverFailed);
  }

  function retryDiscover(button) {
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
    var failed = toArray(document.querySelectorAll('[data-discover-row]')).filter(function (s) {
      return s.querySelector('[data-discover-retry="discover"]');
    });
    loadDiscover().then(function (fresh) {
      var stillFailed = fresh.some(function (s) { return s.querySelector('.watch-row__note--failed'); });
      if (stillFailed) {
        button.removeAttribute('aria-disabled');
        button.setAttribute('aria-busy', 'false');
        core.toast('Still couldn’t load them. Try again in a moment.', { variant: 'error' });
        return;
      }
      // Read before the swap: a row fading out is inert, so it loses the focus.
      var focused = document.activeElement === button;
      var focusNext = null;
      failed.forEach(function (old) {
        var match = fresh.filter(function (s) {
          return s.getAttribute('data-discover-row') === old.getAttribute('data-discover-row');
        })[0];
        if (!match) { old.remove(); return; }
        core.swap(old, match);
        startRows(match);
        if (!focusNext || old.contains(button)) focusNext = match.querySelector('.watch-row__scroll');
      });
      if (focusNext && focused) core.focusSoftly(focusNext);
    });
  }

  /* ── Your library ── */

  /* The rest of a library row, past the cards the page came with. *more*: the
     order and the size of a grid's page. */
  function libraryPages(kind, more) {
    return function (after) {
      return MM.api.get(LIBRARY_URL + kind + '/row?after=' + encodeURIComponent(after) + (more || '')).then(function (data) {
        return { cards: (data.items || []).map(watch.search.card), next: data.next, total: data.total };
      });
    };
  }

  function startLibrary() {
    toArray(document.querySelectorAll('.watch-row__viewport[data-row-next]')).forEach(function (viewport) {
      loaders[viewport.getAttribute('data-row-kind')] = watch.rowsLoad.start(viewport, {
        title: viewport.querySelector('.watch-row__scroll').getAttribute('aria-label'),
        next: viewport.getAttribute('data-row-next'),
        fetchPage: libraryPages(viewport.getAttribute('data-row-kind'))
      });
    });
  }

  /* A library row's grid: newest first, as the row, it goes on from where that stands. */
  function libraryGrid(at) {
    function pages(sort) { return libraryPages(at.id, '&limit=' + GRID_PAGE + (sort === 'added' ? '' : '&sort=' + sort)); }
    var made = {
      fetchPage: pages(at.sort),
      total: at.drawn ? at.drawn.total : at.row ? Number(at.row.getAttribute('data-row-total')) : 0,
      sort: {
        label: 'Sort',
        choose: function (sort) {
          return pages(sort)('').then(function (page) { return Object.assign(page, { fetchPage: pages(sort) }); });
        }
      }
    };
    if (at.drawn) made.next = at.drawn.next;
    else if (at.sort !== 'added') Object.assign(made, { cards: [], next: '' });
    else Object.assign(made, { cards: at.cards(), next: loaders[at.id] ? loaders[at.id].next() : null });
    return made;
  }

  watch.expand.source('recent', libraryGrid);
  DISCOVER_ROWS.forEach(function (r) { watch.expand.source(r[0], discoverGrid); });

  /* ── Recommended for you ── */

  var isAdmin = false;
  var rec = null; // { section, tools, body, payload }
  var shown = null; // the batch the picks button names
  var held = false; // it was chosen, and stays until the person moves the row
  var sought = null; // the batch chosen, while the row loads on to it
  var recLoader = null;
  var picksMenu = null;
  var picksMenuNode = null;
  var timer = null; // of the Refresh that counts down
  var ownBusy = false;
  var ownFailed = false; // the last refresh of the person's own picks made none
  var ownFresh = false; // the one just followed made new ones
  var making = null; // the words shown while they are made
  var ownRefs = ''; // the person's own picks in the latest batch

  /* "6 days", "14 h" or "25 min": how long until *iso*. */
  function waitText(iso) {
    var ms = new Date(iso).getTime() - Date.now();
    if (!(ms > 0)) return '';
    if (ms >= 2 * DAY_MS) return Math.ceil(ms / DAY_MS) + '\u00a0days';
    if (ms >= 3600000) return Math.ceil(ms / 3600000) + '\u00a0h';
    return Math.max(1, Math.ceil(ms / 60000)) + '\u00a0min';
  }

  function batchName(batch, first) {
    if (!batch.date_label) return first ? 'Latest' : '';
    return first ? 'Latest · ' + batch.date_label : batch.date_label;
  }

  function batchLabel(batch, first) {
    if (!batch.date_label) return first ? 'Latest picks' : batch.label;
    return first ? 'Latest picks · ' + batch.date_label : 'Picks of ' + batch.date_label;
  }

  /* A batch's name, and the fewer words of it a phone has room for. */
  function batchWords(batch, first) {
    var words = el('span');
    words.appendChild(el('span', 'row-tool__long', batchLabel(batch, first)));
    words.appendChild(el('span', 'picks-name__short', first ? 'Latest' : batch.date_label || batch.label));
    return words;
  }

  function placeOf(id) {
    return rec.payload.batches.map(function (b) { return b.id; }).indexOf(id);
  }

  function picksButton(payload) {
    var button = el('button', 'row-tool');
    button.type = 'button';
    button.setAttribute('data-picks-menu', '');
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'watch-picks-menu');
    var name = el('span', 'picks-name');
    // As wide as the widest name, so what is beside it stays put.
    payload.batches.forEach(function (b, i) {
      var sizer = batchWords(b, i === 0);
      sizer.className = 'picks-name__sizer';
      name.appendChild(sizer);
    });
    button.appendChild(name);
    button.appendChild(icon('i-chevron-down', 'row-tool__chev'));
    return button;
  }

  /* Name batch *id* on the picks button, the old name fading out under the new. */
  function showBatch(id) {
    var at = placeOf(id);
    var button = rec.tools.querySelector('[data-picks-menu]');
    if (at < 0 || !button) return;
    var name = button.querySelector('.picks-name');
    var old = toArray(name.children).filter(function (n) { return n.classList.contains('is-on'); });
    if (id === shown && old.length) return;
    shown = id;
    var batch = rec.payload.batches[at];
    button.setAttribute('aria-label', 'Picks: ' + batchLabel(batch, at === 0) + '. Choose another batch');
    var fresh = batchWords(batch, at === 0);
    fresh.className = 'picks-name__one';
    name.appendChild(fresh);
    if (old.length) void fresh.offsetWidth; // start the fade from nothing
    old.forEach(function (n) {
      n.classList.remove('is-on');
      if (core.reduceMotion()) n.remove();
      else setTimeout(function () { n.remove(); }, FADE_MS);
    });
    fresh.classList.add('is-on');
  }

  /* ── The picks button follows the row ── */

  function rowCards(scroller) {
    return toArray(scroller.children).filter(function (n) { return n.classList.contains('watch-card'); });
  }

  /* The batch that holds most of the row's first cards from the left, or null with no layout. */
  function leftBatch(scroller) {
    var cards = rowCards(scroller);
    if (!cards.length) return null;
    if (cards.length === 1) return cards[0].getAttribute('data-batch');
    var step = cards[1].offsetLeft - cards[0].offsetLeft;
    if (!(step > 0)) return null;
    var left = Math.max(0, scroller.scrollLeft);
    var right = left + LEFT_CARDS * step;
    var from = Math.floor(left / step);
    var widths = new Map();
    cards.slice(from, from + LEFT_CARDS + 1).forEach(function (card, i) {
      var start = (from + i) * step;
      var width = Math.min(right, start + card.offsetWidth) - Math.max(left, start);
      var id = card.getAttribute('data-batch');
      if (width > 0) widths.set(id, (widths.get(id) || 0) + width);
    });
    var most = null;
    widths.forEach(function (width, id) { if (most === null || width > widths.get(most)) most = id; });
    return most;
  }

  function followRow(viewport) {
    var scroller = viewport.querySelector('.watch-row__scroll');
    var queued = false;
    scroller.addEventListener('scroll', function () {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        if (held || !rec || rec.body !== viewport) return;
        var id = leftBatch(scroller);
        if (id !== null) showBatch(id);
      });
    }, { passive: true });
    ['wheel', 'pointerdown', 'touchstart', 'keydown'].forEach(function (type) {
      viewport.addEventListener(type, function () {
        held = false;
        stopSeeking();
      }, { passive: true, capture: true });
    });
  }

  function stopSeeking() {
    sought = null;
    if (rec) rec.body.removeAttribute('aria-busy');
  }

  /* Scroll the row to batch *id*, loading on to it. Its titles may all stand
     earlier in the row: then to the next older batch that has some. */
  function seek() {
    if (sought === null) return;
    var scroller = rec.body.querySelector('.watch-row__scroll');
    var cards = rowCards(scroller);
    var at = placeOf(sought);
    var card = cards.filter(function (c) { return placeOf(c.getAttribute('data-batch')) >= at; })[0];
    if (!card && recLoader.next() !== null) {
      if (!recLoader.more()) seekFailed(false);
      return;
    }
    if (!card) {
      var last = cards[cards.length - 1].getAttribute('data-batch');
      card = cards.filter(function (c) { return c.getAttribute('data-batch') === last; })[0];
    }
    stopSeeking();
    held = true;
    showBatch(card.getAttribute('data-batch'));
    scroller.scrollTo({ left: card.offsetLeft - cards[0].offsetLeft, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  function seekFailed(told) {
    if (sought === null) return;
    stopSeeking();
    if (!told) core.toast('Couldn’t load those picks. Try again.', { variant: 'error' });
  }

  /* The one menu of batches, the Keep menu's component and look. */
  function startPicksMenu() {
    if (picksMenu) return;
    picksMenuNode = el('div', 'watch-keep-menu rec-menu');
    picksMenuNode.setAttribute('id', 'watch-picks-menu');
    picksMenuNode.setAttribute('role', 'menu');
    picksMenuNode.setAttribute('aria-label', 'Batches of picks');
    picksMenuNode.hidden = true;
    (byId('watch-page') || document.body).appendChild(picksMenuNode);
    picksMenu = watch.keep.menu(picksMenuNode, {
      trigger: '[data-picks-menu]',
      fill: fillPicksMenu,
      choose: function (trigger, item) { chooseBatch(item.getAttribute('data-batch')); },
      spare: function () { return byId('watch-search'); }
    });
  }

  function fillPicksMenu() {
    picksMenuNode.textContent = '';
    rec.payload.batches.forEach(function (b, i) {
      var item = el('button', 'watch-keep-menu__item');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('tabindex', '-1');
      item.setAttribute('data-batch', b.id);
      item.setAttribute('aria-checked', b.id === shown ? 'true' : 'false');
      item.appendChild(icon('i-check'));
      item.appendChild(document.createTextNode(batchLabel(b, i === 0)));
      picksMenuNode.appendChild(item);
    });
  }

  /* Ready, busy (a turning reel) or cooling down ("Refresh in 6 days"). It is as wide as
     its widest label in every state, so what is beside it stays put. */
  function refreshButton() {
    var button = el('button', 'row-tool row-tool--refresh');
    button.type = 'button';
    button.setAttribute('data-own-refresh', '');
    button.setAttribute('data-widest', 'Refresh in 59 min');
    button.setAttribute('data-narrow', 'In 59 min');
    return button;
  }

  function noKey(payload) { return payload.my_refresh.reason === 'not_configured'; }

  function ownButton() { return rec && rec.section.querySelector('[data-own-refresh]'); }

  function setRefresh(state) {
    var button = ownButton();
    if (!button) return;
    clearTimeout(timer);
    var left = state === 'cooldown' ? waitText(rec.payload.my_refresh.next_available_at) : '';
    var face = { ready: ['i-rotate-right', 'Refresh my picks', 'My picks', 'Refresh my picks'],
      busy: [null, 'Refreshing…', 'Making…', 'Making your picks'],
      cooldown: ['i-clock-regular', 'Refresh in ' + left, 'In ' + left, 'Refresh my picks, available in ' + left] }[state];
    button.textContent = '';
    button.classList.toggle('is-busy', state === 'busy');
    if (state === 'ready') button.removeAttribute('aria-disabled');
    else button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-label', face[3]);
    var drawn = el('span', 'row-tool__face');
    drawn.appendChild(state === 'busy' ? watch.making.reel() : icon(face[0]));
    drawn.appendChild(el('span', 'row-tool__long', face[1]));
    drawn.setAttribute('data-narrow', face[2]);
    button.appendChild(drawn);
    // The clock counts down by itself; once it runs out Refresh is back.
    if (state === 'cooldown') timer = setTimeout(drawOwn, 60000);
  }

  /* A batch's cards, each with the batch it is of and its place in it. */
  function pickCards(payload) {
    return tagged(cardsOf(payload.items), payload.batch);
  }

  function tagged(cards, batch) {
    cards.forEach(function (c, i) {
      c.setAttribute('data-batch', batch);
      c.setAttribute('data-place', i);
    });
    return cards;
  }

  /* The batches older than the one the row of *payload* starts at. */
  function olderPicks(payload) {
    var ids = payload.batches.map(function (b) { return b.id; });
    var start = payload.batch;
    function gone(err) { if (!err || err.status !== 404) throw err; }
    function page(older) { return { cards: pickCards(older), next: older.next_batch }; }
    function skip(batch) {
      var after = ids.indexOf(batch) + 1;
      return after && after < ids.length ? load(ids[after]) : { cards: [], next: null };
    }
    function load(batch) {
      return fetchRec(batch, start).then(page, function (err) {
        gone(err);
        // With no start the second request would repeat the first.
        if (!start) return skip(batch);
        // Deleted meanwhile: the one the row starts at, so it starts at this one now, or this one.
        return fetchRec(batch).then(function (older) {
          start = batch;
          return page(older);
        }, function (err) {
          gone(err);
          return skip(batch);
        });
      });
    }
    return load;
  }

  function recBody(payload) {
    if (recLoader) recLoader.stop();
    recLoader = null;
    held = false;
    sought = null;
    var body = payload.enabled ? strip(REC_TITLE, payload.items) : null;
    if (body) {
      tagged(toArray(body.querySelectorAll('.watch-card')), payload.batch);
      recLoader = watch.rowsLoad.start(body, {
        title: REC_TITLE, next: payload.next_batch, fetchPage: olderPicks(payload), took: seek, lost: seekFailed
      });
      followRow(body);
      return body;
    }
    if (!payload.enabled) {
      var link = el('a', 'watch-row__link', 'Turn them on in Settings');
      link.setAttribute('href', SETTINGS_HREF);
      return note('Recommendations are switched off.', link);
    }
    var make = null;
    if (payload.refresh && !noKey(payload)) {
      make = el('a', 'watch-row__link', 'Refresh shared picks in Settings');
      make.setAttribute('href', SHARED_HREF);
    }
    return note('No picks yet. The first ones are for everyone, until your own are made.', make);
  }

  /* Recommended's grid: each batch under its date, from the one the row starts at. */
  function recGrid(at) {
    var shown = null; // the batch of the last card
    var had = new Set();
    /* A date stands over a title the grid does not have yet: the others are not added. */
    function isNew(card) {
      var link = card.querySelector('[data-title-ref]');
      var title = link.getAttribute('data-kind') + (link.getAttribute('data-tmdb-id') || link.textContent);
      return !had.has(title) && had.add(title);
    }
    function lines(cards, payload) {
      return cards.reduce(function (out, c) {
        var id = c.getAttribute('data-batch');
        if (isNew(c) && id !== shown) {
          out.push(batchLine(id, payload));
          shown = id;
        }
        return out.concat(c);
      }, []);
    }
    function pages(payload) {
      var older = olderPicks(payload);
      return function (batch) {
        return older(batch).then(function (page) { return { cards: lines(page.cards, payload), next: page.next }; });
      };
    }
    function jump(payload) {
      var from = payload.batches.map(function (b) { return b.id; }).indexOf(payload.batch);
      var options = payload.batches.map(function (b, i) { return { id: b.id, label: batchName(b, i === 0) }; }).slice(Math.max(0, from));
      return options.length > 1 ? { label: 'Jump to', value: payload.batch, options: options, jump: true } : null;
    }
    var empty = { title: 'No recommendations yet', link: 'Back to all rows', href: '/watch' };
    function line(card) { return batchLine(card.getAttribute('data-batch'), rec.payload); }
    function dates() { return rec.payload.batches.map(function (b) { return b.id; }); }
    if (at.row && rec && rec.payload.batch) {
      return { cards: lines(at.cards(), rec.payload), next: recLoader ? recLoader.next() : null,
        fetchPage: pages(rec.payload), sort: jump(rec.payload), empty: empty, line: line, dates: dates };
    }
    var rest = null;
    return { cards: [], next: 'first', empty: empty, line: line, dates: dates, fetchPage: function (batch) {
      if (batch !== 'first') return rest(batch);
      return fetchRec(null).then(function (payload) {
        if (!payload.batch) return { cards: [], next: null };
        rest = pages(payload);
        var sort = jump(payload);
        if (sort && address.value('grid') === at.id) watch.grid.setSort(sort);
        return { cards: lines(pickCards(payload), payload), next: payload.next_batch };
      });
    } };
  }

  function batchLine(id, payload) {
    var at = payload.batches.map(function (b) { return b.id; }).indexOf(id);
    var line = watch.grid.group(id, at < 0 ? 'Picks of ' : batchLabel(payload.batches[at], at === 0));
    line.insertBefore(core.aiLabel('AI picks'), line.firstChild);
    return line;
  }

  watch.expand.source('recommended', recGrid);

  function fillTools() {
    rec.tools.textContent = '';
    shown = null;
    if (!rec.payload.batches.length) return;
    rec.tools.appendChild(picksButton(rec.payload));
    startPicksMenu();
    showBatch(rec.payload.batch);
  }

  /* ── A person's own picks ── */

  function ownOf(payload) {
    return (payload.items || []).filter(function (i) { return i.own; }).map(function (i) { return i.pick_ref; }).join(' ');
  }

  function latestOf(payload) { return payload.batches.length ? payload.batches[0].id : null; }

  /* Whether the person may have their picks made: ready, busy, cooldown, or none
     (nothing they do here would change it). */
  function ownState(mine) {
    if (ownBusy || mine.running) return 'busy';
    if (mine.available) return 'ready';
    if (mine.reason !== 'wait' && mine.reason !== 'daily_limit') return 'none';
    return waitText(mine.next_available_at) ? 'cooldown' : 'ready';
  }

  function ownText(payload, state) {
    var mine = payload.my_refresh;
    if (state === 'busy') return 'Making your picks. It can take a minute or two.';
    if (mine.reason === 'daily_limit' && state === 'cooldown') return 'That’s all the refreshes for now.';
    if (mine.reason === 'not_enough_plays') return ((payload.items || []).length && !ownOf(payload) ? 'For everyone. ' : '') + 'Play five titles and these become yours.';
    if (mine.reason === 'not_configured' && isAdmin) return NO_KEY;
    if (ownFailed) return 'Couldn’t make new ones.' + (state === 'ready' ? ' Try again.' : '');
    var items = payload.items || [];
    if (ownOf(payload)) {
      return items.every(function (i) { return i.own; }) ? 'From your plays.' : 'From your plays, then new and popular.';
    }
    if (!items.length) return state === 'ready' ? 'Refresh for your own.' : '';
    if (state === 'ready') return 'For everyone, until you refresh.';
    return state === 'cooldown' ? 'For everyone, until yours are made.' : 'For everyone.';
  }

  /* The line under the heading: what these are, and the person's own Refresh. */
  function ownLine() {
    var line = el('div', 'rec-line');
    line.appendChild(core.aiLabel('AI picks'));
    var text = el('span', 'rec-line__text');
    text.setAttribute('role', 'status');
    line.appendChild(text);
    return line;
  }

  /* While picks are made the line's words are for a screen reader alone, and others change beside
     them; new picks end these with a word of their own. */
  function drawMaking(line, said, busy) {
    function hide() {
      making.stop();
      making.node.remove();
      making = null;
      said.classList.remove('sr-only');
    }
    line.classList.toggle('is-making', busy);
    if (busy) {
      ownFresh = false;
      if (making && !making.ending()) return;
      if (making) hide();
      making = watch.making.start({ few: rec.payload.few_ratings === true });
      said.classList.remove('fade-in');
      said.classList.add('sr-only');
      said.after(making.node);
    } else if (making && !ownFresh) {
      hide();
    } else if (making && !making.ending()) {
      making.end('Here they are.', function () {
        hide();
        said.classList.add('fade-in');
      });
    }
  }

  function drawOwn() {
    var line = rec && rec.section.querySelector('.rec-line');
    if (!line) return;
    var state = ownState(rec.payload.my_refresh);
    var button = ownButton();
    var said = line.querySelector('.rec-line__text');
    var says = ownText(rec.payload, state);
    if (said.textContent !== says) {
      // The toast tells of new picks.
      if (state !== 'busy' && making && ownFresh) said.setAttribute('aria-live', 'off');
      else said.removeAttribute('aria-live');
      said.textContent = says;
    }
    drawMaking(line, said, state === 'busy');
    if (state === 'none') {
      var focused = button && document.activeElement === button;
      if (button) button.remove();
      var row = focused && rec.section.querySelector('.watch-row__scroll');
      if (row) core.focusSoftly(row);
      return;
    }
    if (!button) line.appendChild(refreshButton());
    setRefresh(state);
    if (!ownBusy && rec.payload.my_refresh.running) followOwn(false);
  }

  function ownError(message) {
    ownBusy = false;
    rec.payload.my_refresh = { available: true, reason: null, next_available_at: null, running: false };
    drawOwn();
    core.toast(message, { variant: 'error' });
  }

  /* Have the person's own picks made again: once a week, and only when the server says so. */
  function refreshOwn(button) {
    if (ownBusy || button.getAttribute('aria-disabled') === 'true') {
      var left = !ownBusy && waitText(rec.payload.my_refresh.next_available_at);
      if (left) core.toast('Your picks can be refreshed again in ' + left + '.');
      return;
    }
    ownBusy = true;
    ownFailed = false;
    drawOwn();
    MM.api.post(OWN_URL).then(function () { followOwn(true); }, function (err) {
      if (loggedOut(err)) return;
      var data = (err && err.data) || {};
      if (data.error === 'running') { followOwn(false); return; }
      if (err && (err.status === 409 || (err.status === 429 && data.next_available_at))) {
        ownBusy = false;
        rec.payload.my_refresh = { available: false, reason: data.error, running: false, next_available_at: data.next_available_at };
        drawOwn();
        return;
      }
      ownError(MM.api.messageOf(err, 'Couldn’t refresh your picks. Try again.'));
    });
  }

  /* Follow the making of the person's picks to its end. *mine*: started here; a
     scan can be making them too, and then the status says nothing of how it went. */
  function followOwn(mine) {
    ownBusy = true;
    drawOwn();
    follow(OWN_URL, function (st) {
      if (st.status === 'running') return false;
      fetchRec(null).then(function (payload) {
        var own = ownOf(payload);
        var changed = own !== ownRefs || latestOf(payload) !== latestOf(rec.payload);
        var fresh = !!own && own !== ownRefs;
        ownFresh = fresh;
        ownBusy = false;
        ownFailed = !fresh && !payload.my_refresh.running && (st.status === 'failed' || (mine && st.status !== 'done'));
        if (changed) {
          updateRec(payload);
        } else {
          rec.payload.my_refresh = payload.my_refresh;
          drawOwn();
        }
        if (fresh) core.toast('Your new picks are in.', { variant: 'success' });
        else if (ownFailed) core.toast('Couldn’t make new picks for you.' + (payload.my_refresh.available ? ' Try again.' : ''), { variant: 'error' });
      }).catch(function (err) {
        if (!loggedOut(err)) ownError('Your picks may have changed, but they didn’t load. Reload the page to see them.');
      });
      return true;
    }, ownError);
  }

  /* The Recommended row for *payload*; null when a viewer has it switched off. */
  function recSection(payload) {
    if (!payload.enabled && !payload.refresh) return null;
    var section = rowSection('recommended', REC_TITLE);
    var tools = el('div', 'watch-row__tools');
    section.querySelector('.watch-row__head').appendChild(tools);
    if (payload.enabled) section.appendChild(ownLine());
    var body = recBody(payload);
    section.appendChild(body);
    rec = { section: section, tools: tools, body: body, payload: payload };
    if (!payload.enabled) return section;
    fillTools();
    ownRefs = ownOf(payload);
    drawOwn();
    return section;
  }

  function recFailed(err) {
    if (loggedOut(err)) return never();
    rec = null;
    var section = rowSection('recommended', REC_TITLE);
    section.appendChild(failedNote(REC_TITLE, 'recommended'));
    return section;
  }

  function fetchRec(batch, start) {
    return MM.api.get(RECOMMENDED_URL + (batch ? '?batch=' + encodeURIComponent(batch) : '') +
      (start ? '&start=' + encodeURIComponent(start) : ''));
  }

  function loadRecommended() {
    return fetchRec(null).then(recSection, recFailed);
  }

  /* Put a newer answer into the row that is there, without moving it. */
  function updateRec(payload) {
    if (picksMenu) picksMenu.close(false);
    var refocus = rec.tools.contains(document.activeElement) && document.activeElement.hasAttribute('data-picks-menu');
    rec.payload = payload;
    ownRefs = ownOf(payload);
    fillTools();
    drawOwn();
    if (refocus) {
      var pill = rec.tools.querySelector('[data-picks-menu]');
      if (pill) pill.focus({ preventScroll: true });
    }
    var body = recBody(payload);
    var old = rec.body;
    rec.body = body;
    old.removeAttribute('aria-busy');
    return swapBody(old, body);
  }

  function chooseBatch(id) {
    if (!recLoader || !id) return;
    stopSeeking();
    sought = id;
    rec.body.setAttribute('aria-busy', 'true');
    seek();
  }

  function retryRec(button) {
    var old = button.closest('[data-discover-row]');
    button.setAttribute('aria-disabled', 'true');
    button.setAttribute('aria-busy', 'true');
    fetchRec(null).then(function (payload) {
      var section = recSection(payload);
      if (!section) { old.remove(); return; }
      // Read before the swap: a row fading out is inert, so it loses the focus.
      var focused = document.activeElement === button;
      core.swap(old, section);
      startRows(section);
      var focus = section.querySelector('.watch-row__scroll, [data-picks-menu]');
      if (focus && focused) core.focusSoftly(focus);
    }, function (err) {
      if (loggedOut(err)) return;
      button.removeAttribute('aria-disabled');
      button.setAttribute('aria-busy', 'false');
      core.toast('Still couldn’t load your picks. Try again in a moment.', { variant: 'error' });
    });
  }

  /* Ask *url* how a refresh goes until take(status) says it is over, or
     lost(message) that nobody can tell. */
  function follow(url, take, lost) {
    var failures = 0;
    function later(ms) { setTimeout(tick, ms); }
    function tick() {
      if (document.hidden) { later(POLL_MS); return; }
      MM.api.get(url).then(function (st) {
        failures = 0;
        if (!take(st)) later(POLL_MS);
      }).catch(function (err) {
        if (err && (err.status === 401 || err.status === 403)) { toLogin(); return; }
        failures += 1;
        if (failures >= MAX_FAILURES) {
          lost('Couldn’t reach the server. Check your connection and try again.');
          return;
        }
        later(Math.min(POLL_MS * Math.pow(2, failures), POLL_MAX_MS));
      });
    }
    later(POLL_MS);
  }

  /* ── Landing on Recommended ── */

  /* /recommended sends people here with ?row=recommended: a query, since a
     #fragment of the redirect's own would drop an old #recommendation-<id>.
     The marker leaves the address at once; says whether it was there. */
  function takeLanding() {
    var params = (window.location.search || '').replace(/^\?/, '').split('&').filter(Boolean);
    var rest = params.filter(function (p) { return p !== 'row=recommended'; });
    if (rest.length === params.length) return false;
    address.replace(null, window.location.pathname + (rest.length ? '?' + rest.join('&') : '') + (window.location.hash || ''));
    return true;
  }

  /* Bring the Recommended row up under the top bar, unless there is none
     (picks off) or the person has scrolled while it loaded. */
  function showRecommended() {
    var row = document.querySelector('[data-discover-row="recommended"]');
    if (!row || window.scrollY > 0) return;
    var by = row.getBoundingClientRect().top - core.clearArea().top - 12;
    if (by > 0) window.scrollBy({ top: by, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* ── The slot ── */

  /* Put *content* where the skeleton is: both share one grid cell while the
     skeleton fades out and the rows fade in, so nothing below jumps. */
  function fill(slot, content) {
    slot.removeAttribute('aria-busy');
    var skel = slot.querySelector('.watch-discover-skel');
    function settle() {
      var lefts = content ? toArray(content.querySelectorAll('.watch-row__scroll')).map(function (n) {
        return { node: n, left: n.scrollLeft };
      }) : [];
      address.hold(slot);
      slot.textContent = '';
      if (content) {
        slot.appendChild(content);
        content.classList.remove('is-swapping-in');
        // Moved, they stand at their start.
        lefts.forEach(function (l) { if (l.left) l.node.scrollTo({ left: l.left, behavior: 'instant' }); });
      }
    }
    if (!content || !skel || core.reduceMotion()) {
      settle();
      if (content) startRows(content);
      return;
    }
    var layer = el('div', 'swap-layer watch-discover-layer');
    address.hold(slot);
    skel.replaceWith(layer);
    content.classList.add('is-swapping-in');
    layer.appendChild(content);
    layer.appendChild(skel);
    startRows(content);
    skel.classList.add('swap-out');
    var done = false;
    function finish() { if (!done) { done = true; settle(); } }
    skel.addEventListener('animationend', function (e) { if (e.target === skel) finish(); });
    setTimeout(finish, SWAP_FALLBACK_MS);
  }

  function start(slot, data) {
    isAdmin = !!data.is_admin;
    var discover = data.tmdb_configured ? loadDiscover() : Promise.resolve([]);
    Promise.all([loadRecommended(), discover]).then(function (got) {
      var rows = (got[0] ? [got[0]] : []).concat(got[1]);
      if (!rows.length) { fill(slot, null); return; }
      var content = el('div', 'watch-discover');
      content.appendChild(el('p', 'watch-group-label', 'Discover'));
      rows.forEach(function (r) { content.appendChild(r); });
      fill(slot, content);
      if (landing) showRecommended();
    });
  }

  function pageData() {
    var island = byId('watch-page-data');
    try { return JSON.parse(island ? island.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  document.addEventListener('mm:title-changed', function (e) { updateBadges(e.detail); });

  document.addEventListener('click', function (e) {
    var target = e.target;
    if (!target.closest) return;
    var retry = target.closest('[data-discover-retry]');
    if (retry && retry.getAttribute('aria-disabled') !== 'true') {
      if (retry.getAttribute('data-discover-retry') === 'recommended') retryRec(retry); else retryDiscover(retry);
      return;
    }
    var own = target.closest('[data-own-refresh]');
    if (own) refreshOwn(own);
  });

  watch.browse = { card: card, badge: badge };

  var landing = takeLanding();
  var slot = byId('watch-discover-slot');
  if (slot) start(slot, pageData());
  startLibrary();
})();
