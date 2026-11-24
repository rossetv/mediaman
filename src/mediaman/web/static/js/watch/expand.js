/* Watch: the rows grow into grids, on MMWatch.expand. A row's heading is a link
 * to its grid (?all=<row>); three screens along the same row a pill offers the
 * same. Whatever opens one is in the address, so a shared link does too.
 * What a row's grid holds comes from its source:
 *
 *   source(id, function (at) { return what MMWatch.grid.open takes; })
 *     at: { id, sort, row (its section, or null), drawn (the server drew the
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

  var LIBRARY = [['az', 'A to Z'], ['added', 'Recently added'], ['year', 'Newest release'], ['unwatched', 'Not watched first']];
  var SORTS = {
    'continue': [['added', 'Last watched'], ['az', 'A to Z']],
    recent: [['added', 'Recently added'], ['az', 'A to Z']],
    movies: LIBRARY, tv: LIBRARY, anime: LIBRARY,
    trending: [['all', 'All'], ['movie', 'Films'], ['tv', 'TV']]
  };
  var EMPTY = {
    'continue': { title: 'Nothing in Continue watching', text: 'What you start shows up here.' },
    leaving: { title: 'Nothing is leaving soon' }
  };
  var OTHERS = { view: null, q: null, genre: null, type: null, where: null, decade: null, person: null, page: null, per_page: null, sort: null };
  var OFFER_SCREENS = 2.6; // of the same row, less OFFER_LESS px, before its grid is offered
  var OFFER_LESS = 300;
  var EDGE = 2; // px; a row this little too wide is all there

  var titles = {}; // the rows that can be opened, by id
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

  function offer(section, id) {
    var button = el('button', 'row-offer');
    button.type = 'button';
    button.setAttribute('data-row-open', id);
    button.setAttribute('data-grid-fade', '');
    button.appendChild(icon('i-table-cells-large'));
    button.appendChild(el('span', 'row-offer__text'));
    var box = section.querySelector('.watch-row__head');
    box.insertBefore(button, box.querySelector('.watch-row__tools'));
    return button;
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
      hideOffer(section);
    }
    var text = section.querySelector('.row-offer__text');
    if (text) text.textContent = total === null ? 'Keep going in a grid' : 'See all ' + total + ' in a grid';
  }

  var started = new WeakSet();

  /* Start *section*'s heading, or look again after its cards were swapped for others. */
  function row(section) {
    if (!section.querySelector('.row-offer') && section.querySelector('.row-open')) offer(section, section.getAttribute('data-row-id'));
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

  function offerOf(section) { return section.querySelector('.row-offer'); }

  function hideOffer(section) {
    var button = offerOf(section);
    if (button) button.classList.remove('is-on');
    travelled.set(section, 0);
  }

  function rowScrolled(scroller) {
    var section = scroller.closest('[data-row-id]');
    if (!section || !browse.contains(section)) return;
    var by = Math.abs(scroller.scrollLeft - (lefts.get(scroller) || 0));
    lefts.set(scroller, scroller.scrollLeft);
    // Put back by the page itself, not scrolled by the person.
    if (grid.moving() || grid.isOpen()) return;
    if (by > 1) {
      toArray(browse.querySelectorAll('[data-row-id]')).forEach(function (other) { if (other !== section) hideOffer(other); });
    }
    var sum = (travelled.get(section) || 0) + by;
    travelled.set(section, sum);
    if (sum < OFFER_SCREENS * scroller.clientWidth - OFFER_LESS || section.hasAttribute('data-row-whole')) return;
    var button = offerOf(section);
    if (button) button.classList.add('is-on');
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

  /* *given*, or the order the row itself has. */
  function sortOf(id, given) {
    var list = own(SORTS, id) ? SORTS[id] : [];
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
      options: SORTS[id].map(function (s) { return { id: s[0], label: s[1] }; }),
      chosen: function (to) { address.replace('grid', address.url({ sort: to === first ? null : to })); }
    }, sort);
  }

  function show(id, sort, how) {
    var section = rowOf(id);
    var made = (own(sources, id) ? sources[id] : whole)({
      id: id, sort: sort, row: section,
      drawn: how.drawn ? drawn : null,
      cards: function () { return cardsIn(rowOf(id)).map(grid.copy); }
    });
    var placed = grid.open(Object.assign({
      id: id, title: titles[id], total: null,
      row: function () { return rowOf(id); },
      back: function () { address.back('grid'); },
      instant: how.instant, again: how.again
    }, made, { sort: withSort(id, made.sort, sort) }));
    // A card kept or dismissed in there leaves it as it leaves the row.
    document.querySelector('.watch-all__body').toggleAttribute('data-watch-leaving-row', id === 'leaving');
    if (section) setTimeout(function () { hideOffer(section); }, core.tokenMs('--dur-fast', 150));
    return placed;
  }

  /* ── The address ── */

  function readGrid(params) {
    var id = params.get('all') || '';
    if (!own(titles, id) || elsewhere(params)) return null;
    var sort = sortOf(id, params.get('sort'));
    return sort && sort !== sortOf(id, null) ? id + ' ' + sort : id;
  }

  /* Something that takes the rows' place, and the grid's. Only an admin's page has the List. */
  function elsewhere(params) {
    return (params.get('view') === 'list' && !!core.byId('watch-list')) || !!(params.get('q') || '').trim() || !!params.get('genre') || !!params.get('person');
  }

  function start() {
    var data = pageData();
    titles = data.grids || {};
    away = elsewhere(new URLSearchParams(window.location.search));
    drawn = data.grid || null;
    toArray(browse.querySelectorAll('[data-row-id]')).forEach(row);
    address.layer({
      name: 'grid',
      depth: 10,
      params: ['all', 'sort'],
      read: function (here) { return readGrid(here.params); },
      open: function (value, how) {
        var at = value.split(' ');
        var first = how === 'start';
        var again = !first && away;
        away = false;
        var placed = show(at[0], sortOf(at[0], at[1]), { instant: first || again, again: again, drawn: first && !!drawn && drawn.id === at[0] });
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
      away = false;
      address.push('grid', address.url(Object.assign({}, OTHERS, { all: id }), ''));
      show(id, sortOf(id, null), {});
      return;
    }
    // The Browse half of the switch is the way out to the rows.
    var rows = e.target.closest('#watch-viewtoggle [data-view="rows"]');
    if (rows && grid.isOpen() && !e.button && !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)) {
      e.preventDefault();
      address.close('grid');
    }
  });

  watch.expand = { head: head, row: row, source: source };

  start();
})();
