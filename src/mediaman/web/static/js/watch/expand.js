/* Watch: the rows grow into grids, on MMWatch.expand. A row's heading is a link
 * to its grid (?all=<row>); three screens along the same row it draws the eye.
 * Whatever opens one is in the address, so a shared link does too.
 * What a row's grid holds comes from its source:
 *
 *   source(id, function (at) { return what MMWatch.grid.open takes; })
 *     at: { id, sort, type (of All titles), ready (its first page, where it is in), row (its section, or null), drawn (the server drew the
 *           grid: { total, next }), cards() (copies of the row's) }
 *
 * Needs watch/core.js, watch/address.js and watch/grid.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var grid = watch.grid;
  var address = watch.address;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var toArray = core.toArray;

  var browse = core.byId('watch-browse');
  if (!browse || !grid.open) return;

  var LIBRARY = [['added', 'Recently added'], ['az', 'A to Z'], ['year', 'Newest release'], ['unwatched', 'Not watched first']];
  var SORTS = {
    'continue': [['added', 'Last watched'], ['az', 'A to Z']],
    recent: [['added', 'Recently added'], ['az', 'A to Z']],
    library: LIBRARY,
    trending: [['all', 'All'], ['movie', 'Films'], ['tv', 'TV']]
  };
  // What only an admin is told of a title: id, name and the name on the button.
  var ADMIN = [['size', 'Largest'], ['small', 'Smallest'], ['stalest', 'Longest unwatched by anyone', 'Longest unwatched'],
    ['seen', 'Last watched by anyone', 'Last watched'], ['leaving', 'Leaving soonest']];
  var IN_LIBRARY = ['movies', 'tv', 'anime']; // the rows whose grid is All titles with that filter
  var TYPES = IN_LIBRARY.concat('unseen');
  var ADMIN_TYPES = ['kept', 'stale'];
  var EMPTY = {
    'continue': { title: 'Nothing in Continue watching', text: 'What you start shows up here.' },
    leaving: { title: 'Nothing is leaving soon' }
  };
  var OTHERS = { all: null, view: null, q: null, genre: null, type: null, where: null, decade: null, person: null, page: null, per_page: null, sort: null };
  var OFFER_SCREENS = 2.6; // of the same row, less OFFER_LESS px, before its grid is offered
  var OFFER_LESS = 300;
  var EDGE = 2; // px; a row this little too wide is all there
  var CLEAR = 2; // px between a heading at its largest and what is beside it

  var titles = {}; // the rows that can be opened, by id
  var admin = false;
  var drawn = null; // the grid the server drew: { id, total, next }
  var sources = {};
  var away = false; // the grid was left for something else than its row

  function own(map, key) { return Object.prototype.hasOwnProperty.call(map, key); }

  function pageData() {
    var island = core.byId('watch-page-data');
    try { return JSON.parse(island ? island.textContent : '{}') || {}; } catch (e) { return {}; }
  }

  function rowOf(id) {
    return toArray(browse.querySelectorAll('[data-row-id]')).filter(function (s) {
      return s.getAttribute('data-row-id') === id;
    })[0] || null;
  }

  /* On its way out of *section*. A page put out of reach under a panel keeps its cards. */
  function leaves(node, section) {
    var inert = node.closest('[inert]');
    return !!inert && section.contains(inert);
  }

  /* A row being swapped for another has both for a moment; the one leaving is inert. */
  function scrollerOf(section) {
    return toArray(section.querySelectorAll('.watch-row__scroll')).filter(function (n) { return !leaves(n, section); })[0] || null;
  }

  function cardsIn(section) {
    return section ? toArray(section.querySelectorAll('.watch-row__scroll .watch-card')).filter(function (card) {
      return !card.classList.contains('is-kept') && !leaves(card, section);
    }) : [];
  }

  /* How many titles a row's grid has; null: nobody knows. */
  function totalOf(section) {
    var viewport = section.querySelector('.watch-row__viewport');
    if (section.hasAttribute('data-discover-row') || !viewport) return null;
    return viewport.hasAttribute('data-row-kind') ? Number(section.getAttribute('data-row-total')) : cardsIn(section).length;
  }

  /* ── The way in ── */

  /* A row's heading: the markup of _watch_all.html. */
  function head(id, title) {
    var box = el('div', 'watch-row__head');
    var h2 = el('h2', 'watch-row__title');
    var link = el('a', 'row-open');
    link.setAttribute('data-row-open', id);
    var words = el('span', null, title);
    words.setAttribute('data-row-title', '');
    var cue = el('span', 'row-open__cue');
    cue.setAttribute('data-grid-fade', '');
    cue.hidden = true;
    cue.appendChild(el('span', 'row-open__word', 'See all'));
    cue.appendChild(icon('i-chevron-right'));
    link.appendChild(words);
    link.appendChild(cue);
    h2.appendChild(link);
    box.appendChild(h2);
    return box;
  }

  /* How many there are, between See all's words and its arrow. */
  function counted(section) {
    var cue = section.querySelector('.row-open__cue');
    if (!cue || cue.querySelector('.row-open__end')) return;
    var words = el('span', 'row-open__words');
    var end = el('span', 'row-open__end');
    end.appendChild(el('span', 'row-open__total'));
    end.appendChild(cue.querySelector('.ico'));
    words.appendChild(cue.querySelector('.row-open__word'));
    words.appendChild(end);
    cue.appendChild(words);
  }

  /* See all is there only where something is off the screen. */
  function fit(section) {
    var scroller = scrollerOf(section);
    var link = section.querySelector('.row-open');
    if (!link || (scroller && !scroller.clientWidth)) return;
    var can = !!scroller && (scroller.scrollWidth - scroller.clientWidth > EDGE || scroller.parentNode.hasAttribute('data-row-more'));
    var id = link.getAttribute('data-row-open');
    var title = link.querySelector('[data-row-title]').textContent;
    var total = totalOf(section);
    section.toggleAttribute('data-row-whole', !can);
    link.querySelector('.row-open__cue').hidden = !can;
    if (can) {
      link.setAttribute('href', '/watch?all=' + id);
      link.setAttribute('aria-label', title + ', see all' + (total === null ? '' : ' ' + total));
    } else {
      link.removeAttribute('href');
      link.removeAttribute('aria-label');
      withdraw(section);
    }
    link.querySelector('.row-open__total').textContent = total === null ? '' : total;
  }

  var started = new WeakSet();

  /* Start *section*'s heading, or look again after its cards were swapped for others. */
  function row(section) {
    counted(section);
    var scroller = scrollerOf(section);
    if (scroller && !started.has(scroller)) {
      started.add(scroller);
      var again = function () { if (section.contains(scroller)) fit(section); };
      if ('ResizeObserver' in window) new ResizeObserver(again).observe(scroller);
      else window.addEventListener('resize', again);
      if ('MutationObserver' in window) {
        new MutationObserver(again).observe(scroller, { childList: true });
        new MutationObserver(again).observe(scroller.parentNode, { attributes: true, attributeFilter: ['data-row-more'] });
      }
    }
    fit(section);
  }

  /* ── The offer ── */

  var travelled = new WeakMap(); // how far each row was scrolled since another was
  var lefts = new WeakMap();
  var called = {}; // the rows whose heading has moved, by id

  /* A heading grows to its right: no further than what stands there, or the screen's edge. */
  function fitSwell(link) {
    var box = link.getBoundingClientRect();
    var style = getComputedStyle(link);
    var swell = parseFloat(style.getPropertyValue('--row-swell'));
    var chip = parseFloat(style.getPropertyValue('--row-chip')) * link.querySelector('.row-open__cue').getBoundingClientRect().width;
    var next = link.closest('.watch-row__title').nextElementSibling;
    var beside = next && next.getBoundingClientRect();
    var end = beside && beside.width && beside.top < box.bottom ? beside.left : document.documentElement.clientWidth;
    var full = swell * box.width + (1 + swell) * chip;
    var room = Math.max(0, end - CLEAR - box.right);
    if (full > room) link.style.setProperty('--row-room', room / full);
  }

  function offer(section) {
    var link = section.querySelector('.row-open');
    if (!link) return;
    link.classList.add('is-offered');
    var id = link.getAttribute('data-row-open');
    if (own(called, id)) return;
    called[id] = true;
    fitSwell(link);
    // With no animation to end, it would stay, and swell as soon as motion is allowed.
    if (!core.reduceMotion()) link.classList.add('is-calling');
  }

  /* A heading still moving comes to rest by itself. */
  function withdraw(section) {
    var link = section.querySelector('.row-open');
    if (link) link.classList.remove('is-offered');
    travelled.set(section, 0);
  }

  // Cancelled when the page is hidden, or it would move again on its return.
  ['animationend', 'animationcancel'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      e.target.classList.remove('is-calling');
    });
  });

  function rowScrolled(scroller) {
    var section = scroller.closest('[data-row-id]');
    if (!section || !browse.contains(section)) return;
    var by = Math.abs(scroller.scrollLeft - (lefts.get(scroller) || 0));
    lefts.set(scroller, scroller.scrollLeft);
    // Put back by the page itself, not scrolled by the person.
    if (grid.moving() || grid.isOpen()) return;
    if (by > 1) {
      toArray(browse.querySelectorAll('[data-row-id]')).forEach(function (other) { if (other !== section) withdraw(other); });
    }
    if (scroller.scrollLeft <= EDGE) {
      withdraw(section);
      return;
    }
    var sum = (travelled.get(section) || 0) + by;
    travelled.set(section, sum);
    if (sum < OFFER_SCREENS * scroller.clientWidth - OFFER_LESS || section.hasAttribute('data-row-whole')) return;
    offer(section);
  }

  document.addEventListener('scroll', function (e) {
    if (e.target.classList && e.target.classList.contains('watch-row__scroll')) rowScrolled(e.target);
  }, { capture: true, passive: true });

  /* ── What a grid holds ── */

  function source(id, make) { sources[id] = make; }

  /* A title as A to Z reads it, as the server does. */
  function azOf(card) {
    var name = card.querySelector('.watch-card__title').textContent.trim();
    return name.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/^(?:the|an?)\s+(?=\S)/, '');
  }

  function byTitle(cards) {
    return cards.map(function (card) { return { card: card, name: azOf(card) }; })
      .sort(function (a, b) { return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; })
      .map(function (x) { return x.card; });
  }

  /* A row that is all on the page. */
  function whole(at) {
    function ordered(sort) { return sort === 'az' ? byTitle(at.cards()) : at.cards(); }
    function contents(sort) {
      var cards = ordered(sort);
      return { cards: cards, next: null, total: cards.length };
    }
    var made = {
      total: at.drawn ? at.drawn.total : at.cards().length, next: null,
      empty: own(EMPTY, at.id) ? Object.assign({ link: 'Back to all rows', href: '/watch' }, EMPTY[at.id]) : undefined,
      contents: contents,
      sort: { label: 'Sort', choose: contents }
    };
    if (!at.drawn) made.cards = ordered(at.sort);
    return made;
  }

  function sortsOf(id) {
    if (id === 'library' && admin) return LIBRARY.concat(ADMIN);
    return own(SORTS, id) ? SORTS[id] : [];
  }

  function typeOf(given) { return TYPES.concat(admin ? ADMIN_TYPES : []).indexOf(given) < 0 ? '' : given; }

  /* *given*, or the order the row itself has. */
  function sortOf(id, given) {
    var list = sortsOf(id);
    function named(sort) { return list.filter(function (s) { return s[0] === sort; })[0]; }
    var known = named(given) || named('added') || list[0];
    return known ? known[0] : '';
  }

  function withSort(id, sort, value) {
    if (!sort || sort.jump) return sort;
    if (!own(SORTS, id)) return undefined;
    var first = sortOf(id, null);
    return Object.assign({
      value: value,
      options: sortsOf(id).map(function (s) { return { id: s[0], label: s[1], short: s[2], admin: ADMIN.indexOf(s) >= 0 }; }),
      chosen: function (to, type) { address.replace('grid', address.url(id === 'library' ? ofLibrary(to, type) : { sort: to === first ? null : to })); }
    }, sort);
  }

  /* The address of All titles, each part of it that is as usual left out. */
  function ofLibrary(sort, type) {
    return { all: 'library', type: type || null, sort: sort === sortOf('library', null) ? null : sort };
  }

  function show(id, sort, how) {
    var type = id === 'library' ? typeOf(how.type) : undefined;
    var from = id === 'library' && IN_LIBRARY.indexOf(type) >= 0 ? type : id;
    var section = rowOf(from);
    var link = section && section.querySelector('.row-open');
    if (link) link.classList.remove('is-calling'); // the grid measures its title at rest
    var made = (own(sources, id) ? sources[id] : whole)({
      id: id, sort: sort, type: type, ready: how.ready || null, row: section,
      drawn: how.drawn ? drawn : null,
      cards: function () { return cardsIn(rowOf(from)).map(grid.copy); }
    });
    var placed = grid.open(Object.assign({
      id: id, title: titles[id], total: null,
      row: function () { return rowOf(from); },
      back: function () { address.back('grid'); },
      instant: how.instant, again: how.again, still: how.still
    }, made, { sort: withSort(id, made.sort, sort) }));
    // A card kept or dismissed in there leaves it as it leaves the row.
    document.querySelector('.watch-all__body').toggleAttribute('data-watch-leaving-row', id === 'leaving');
    if (section) setTimeout(function () { withdraw(section); }, core.tokenMs('--dur-fast', 150));
    return placed;
  }

  /* ── The address ── */

  function readGrid(params) {
    var id = params.get('all') || '';
    if (!own(titles, id) || elsewhere(params)) return null;
    if (id === 'library' || (own(titles, 'library') && IN_LIBRARY.indexOf(id) >= 0)) {
      var type = typeOf(id === 'library' ? params.get('type') : id);
      return ['library', sortOf('library', params.get('sort'))].concat(type || []).join(' ');
    }
    var sort = sortOf(id, params.get('sort'));
    return sort && sort !== sortOf(id, null) ? id + ' ' + sort : id;
  }

  /* Something that takes the rows' place, and the grid's. */
  function elsewhere(params) {
    return !!(params.get('q') || '').trim() || !!params.get('genre') || !!params.get('person');
  }

  function start() {
    var data = pageData();
    titles = data.grids || {};
    away = elsewhere(new URLSearchParams(window.location.search));
    admin = data.is_admin === true;
    drawn = data.grid || null;
    toArray(browse.querySelectorAll('[data-row-id]')).forEach(row);
    address.layer({
      name: 'grid',
      depth: 10,
      params: ['all', 'sort', 'type'],
      read: function (here) { return readGrid(here.params); },
      open: function (value, how) {
        var at = value.split(' ');
        var first = how === 'start';
        var again = !first && away;
        away = false;
        var placed = show(at[0], sortOf(at[0], at[1]), { type: at[2], instant: first || again, again: again, drawn: first && !!drawn && drawn.id === at[0] });
        return !first && !again && placed;
      },
      close: function () {
        if (!grid.isOpen() && !grid.moving()) return false;
        if (elsewhere(new URLSearchParams(window.location.search))) {
          away = true;
          grid.leave();
          return false;
        }
        grid.close();
        return true;
      }
    });
  }

  function plainClick(e) {
    return !(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
  }

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var opener = e.target.closest('[data-row-open]');
    if (opener && plainClick(e) && (opener.tagName !== 'A' || opener.hasAttribute('href'))) {
      var id = opener.getAttribute('data-row-open');
      if (!own(titles, id)) return;
      e.preventDefault();
      opener.focus({ preventScroll: true }); // as Safari does not on a click, for the way back
      away = false;
      if (own(titles, 'library') && IN_LIBRARY.indexOf(id) >= 0) { library('', id, {}); return; }
      address.push('grid', address.url(Object.assign({}, OTHERS, { all: id }), ''));
      show(id, sortOf(id, null), {});
    }
  });

  /* Open All titles. how.ready: its first page, shown at once; how.still: nothing travels. */
  function library(sort, type, how) {
    away = false;
    sort = sortOf('library', sort);
    address.push('grid', address.url(Object.assign({}, OTHERS, ofLibrary(sort, typeOf(type))), ''));
    show('library', sort, Object.assign({ type: type }, how));
  }

  watch.expand = { head: head, row: row, source: source, library: library };

  start();
})();
