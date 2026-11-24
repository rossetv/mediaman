/* Watch: finding something by what it is, on MMWatch.explore: the categories,
 * what was typed read as one ("popular action movies"), and the page of a
 * category (its order, kind and years, on the one grid) or of a person (all they
 * are known for). Both pages stand in the results' place, are in the address
 * (?genre= and ?person=) and close with Back, Esc or the search bar's cross.
 * Needs watch/core.js, watch/address.js, watch/search.js and watch/grid.js.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var address = watch.address;
  var el = core.el;

  var EXPLORE_URL = '/api/watch/explore';
  var PERSON_URL = '/api/watch/person/';
  var SORTS = [['popular', 'Popular'], ['top_rated', 'Top rated'], ['newest', 'Newest']];
  var TYPES = [['all', 'All'], ['movie', 'Movies'], ['tv', 'TV']];
  var WHERES = [['anywhere', 'Anywhere'], ['library', 'In library']];
  var TYPE_WORDS = { all: 'movies and TV', movie: 'movies', tv: 'TV' };
  var OWN = { q: null, view: null, all: null, page: null, per_page: null, genre: null, type: null, sort: null, where: null, decade: null, person: null };
  var NO_FIT = { title: 'Nothing fits these choices', text: 'Widen one of them to see more.' };
  var ALIAS_CHARS = 4;
  var FAILED = 'Couldn’t load the titles.';
  var TOO_MANY = 'Too many requests, trying again shortly.';
  var TOO_MANY_STILL = 'Too many requests. Try again in a while.';
  var WAITS_MS = [5000, 10000, 20000, 40000, 60000];
  var SVG = 'http://www.w3.org/2000/svg';

  /* Lines on a 24 by 24 square, by category. */
  var LINES = {
    action: 'M13 3L5 14h6l-1 7 8-11h-6z',
    comedy: 'M12 3a9 9 0 100 18 9 9 0 000-18M8 14c2 3.2 6 3.2 8 0M9 9.5h.1M15 9.5h.1',
    drama: 'M5 5c5 2 9 2 14 0v7c0 5-4 8-7 9-3-1-7-4-7-9zM9 10h.1M15 10h.1M9 16c2-2 4-2 6 0',
    'science-fiction': 'M12 6a6 6 0 100 12 6 6 0 000-12M3 16c-1.5 3 8 1 13-3.5S22.5 5 18 7',
    thriller: 'M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6M12 9.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5',
    horror: 'M6 20V11a6 6 0 0112 0v9l-3-2-3 2-3-2zM10 11h.1M14 11h.1',
    crime: 'M12 3l8 3v6c0 5-4 8-8 9-4-1-8-4-8-9V6z',
    adventure: 'M12 3a9 9 0 100 18 9 9 0 000-18M15.5 8.5l-2 5-5 2 2-5z',
    fantasy: 'M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',
    animation: 'M4 20l4-1L19 8l-3-3L5 16zM14 7l3 3',
    family: 'M4 11l8-7 8 7v9H4zM10 20v-5h4v5',
    kids: 'M12 3c-3.5 0-6 2.7-6 6.2C6 13 9 16 12 16s6-3 6-6.8C18 5.7 15.5 3 12 3M12 16v5M10 21h4',
    romance: 'M12 20S4 15 4 9.5A4 4 0 0112 8a4 4 0 018 1.5C20 15 12 20 12 20',
    mystery: 'M9 9a3 3 0 113.5 3c-.5.2-.5.8-.5 2M12 17.5h.1M12 3a9 9 0 100 18 9 9 0 000-18',
    documentary: 'M3 7h12v10H3zM15 11l6-3v8l-6-3',
    history: 'M7 3h10M7 21h10M8 3c0 5 8 6 8 9s-8 4-8 9M16 3c0 5-8 6-8 9s8 4 8 9',
    war: 'M12 5a7 7 0 100 14 7 7 0 000-14M12 2v6M12 16v6M2 12h6M16 12h6',
    western: 'M12 3l2.6 5.8 6.4.6-4.8 4.2 1.4 6.3L12 16.6 6.4 19.9l1.4-6.3L3 9.4l6.4-.6z',
    music: 'M9 18V5l11-2v13M6.5 15.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5M17.5 13.5a2.5 2.5 0 100 5 2.5 2.5 0 000-5',
    reality: 'M3 7h18v12H3zM8 3l4 4 4-4',
    talk: 'M4 5h16v11h-9l-4 4v-4H4z',
    news: 'M4 5h13v14H4zM17 9h3v8a2 2 0 01-3 2M7 9h7M7 12.5h7M7 16h4',
    soap: 'M12 3s6 6.5 6 11a6 6 0 01-12 0c0-4.5 6-11 6-11M9.5 14.5a2.5 2.5 0 002.5 2.5',
    'tv-movie': 'M3 5h18v12H3zM10 8.5l4.5 2.5-4.5 2.5zM8 21h8'
  };
  var OTHER = 'M4 5h16v14H4zM8 5v14M16 5v14M4 9.5h4M4 14.5h4M16 9.5h4M16 14.5h4';

  function icon(id) {
    return drawing(Object.prototype.hasOwnProperty.call(LINES, id) ? LINES[id] : OTHER, 'watch-line');
  }

  function drawing(lines, className) {
    var svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', lines);
    svg.appendChild(path);
    return svg;
  }

  /* ── The categories ── */

  var held = { genres: [], decades: [], people: [], library: null };
  var arrived = false; // whether they are in
  var asking = null;

  /* Asked for once a page; a failure is asked again by whoever needs them next. */
  function categories() {
    if (!asking) {
      asking = window.MM.api.get(EXPLORE_URL).then(function (reply) {
        reply = reply || {};
        held = { genres: reply.genres || [], decades: reply.decades || [], people: reply.people || [], library: reply.library || null };
        arrived = true;
        return held;
      }, function () {
        asking = null;
        return held;
      });
    }
    return asking;
  }

  function genre(id) {
    return held.genres.filter(function (g) { return g.id === id; })[0] || null;
  }

  /* A category's name before anything is known about it. */
  function nameOf(id) {
    var g = genre(id);
    return g ? g.name : id.split('-').map(function (w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(' ');
  }

  function sortName(sort) {
    return SORTS.filter(function (s) { return s[0] === sort; }).concat([SORTS[0]])[0][1];
  }

  /* ── What was typed ── */

  var FILLER = /\b(popular|best|top|rated|new|newest|latest|good|great|some|something|anything|to watch|tonight|movies?|films?|tv|shows?|series|from|in|of|an?|the)\b/g;
  var YEARS = /\b(?:the )?((?:19|20)?\d0)s\b/;

  function words(text) { return String(text).toLowerCase().replace(/[-\s]+/g, ' ').trim(); }

  /* Whether one of *genre*'s names is *text*, or begins with it; another word
     for it ("martial arts") must be well begun. */
  function called(g, text, whole) {
    return [g.name].concat(g.aliases || []).some(function (name, i) {
      var n = words(name);
      return whole ? n === text : n.indexOf(text) === 0 && (i === 0 || text.length >= ALIAS_CHARS);
    });
  }

  function matching(text, whole) {
    return text.length < 2 ? [] : held.genres.filter(function (g) { return called(g, text, whole); });
  }

  /* What *text* asks for: { genres, exact, words, type, sort, decade }. *genres*
     are those it begins to name, *exact* the one it names in full, *words* what
     is left without the words that only say which kind and in what order. */
  function parse(text) {
    var all = words(text);
    var years = YEARS.exec(all);
    var decade = null;
    if (years) {
      var d = years[1].length === 2 ? (years[1] < '30' ? '20' : '19') + years[1] : years[1];
      decade = held.decades.indexOf(d + 's') > -1 ? d + 's' : null;
    }
    var rest = words((decade ? all.replace(YEARS, ' ') : all).replace(FILLER, ' '));
    var found = matching(all, false);
    var whole = matching(all, true)[0] || matching(rest, true)[0] || null;
    matching(rest, false).forEach(function (g) { if (found.indexOf(g) === -1) found.push(g); });
    if (whole) found = [whole].concat(found.filter(function (g) { return g !== whole; }));
    var movie = /\b(movies?|films?)\b/.test(all);
    var tv = /\b(tv|shows?|series)\b/.test(all);
    var type = movie === tv ? 'all' : movie ? 'movie' : 'tv';
    return {
      genres: found,
      exact: whole ? whole.id : null,
      words: rest || all,
      type: whole && whole.types.indexOf(type) === -1 ? 'all' : type,
      sort: /\b(best|top|rated)\b/.test(all) ? 'top_rated' : /\b(new|newest|latest)\b/.test(all) ? 'newest' : 'popular',
      decade: decade
    };
  }

  function url(id, f, page) {
    var q = new URLSearchParams({ genre: id });
    if (f.type && f.type !== 'all') q.set('type', f.type);
    if (f.sort && f.sort !== 'popular') q.set('sort', f.sort);
    if (f.where === 'library') q.set('where', f.where);
    if (f.decade) q.set('decade', f.decade);
    if (page > 1) q.set('page', page);
    return EXPLORE_URL + '?' + q.toString();
  }

  /* ── People ── */

  var persons = new Map(); // who they are and what they are known for, by id

  /* Who somebody is: { person, known_for }. Their titles are never kept, whose
     cards say what is so now. */
  function person(id) {
    return persons.has(id) ? Promise.resolve(persons.get(id)) : personPage(id, 1);
  }

  function personPage(id, page) {
    return window.MM.api.get(PERSON_URL + encodeURIComponent(id) + (page > 1 ? '?page=' + page : '')).then(function (reply) {
      if (reply && reply.person) persons.set(id, { person: reply.person, known_for: reply.known_for });
      return reply;
    });
  }

  /* ── The page ── */

  var open = null; // what is open: { kind, id, host, list, turn, timer }

  function stage() { return watch.search.stage; }

  /* Says what the page is, and what a grid says in the same breath after it. */
  function speaker() {
    var said = '';
    return {
      page: function (text) {
        said = text + '. ';
        stage().say(text);
      },
      grid: function (text) {
        stage().say(said + text);
        said = '';
      },
      next: function () { said = ''; }
    };
  }

  function choice(value, list) {
    return list.some(function (o) { return o[0] === value; }) ? value : list[0][0];
  }

  /* The address' own part: { kind, id, type, sort, decade }, or null. */
  function readAddress(params) {
    if (params.get('view') === 'list' || (params.get('q') || '').trim()) return null;
    var id = params.get('genre') || '';
    if (/^[a-z0-9-]{1,40}$/.test(id)) {
      return {
        kind: 'genre', id: id,
        type: choice(params.get('type'), TYPES),
        sort: choice(params.get('sort'), SORTS),
        where: choice(params.get('where'), WHERES),
        decade: /^(19|20)\d0s$/.test(params.get('decade') || '') ? params.get('decade') : ''
      };
    }
    var who = params.get('person') || '';
    return /^[0-9]{1,18}$/.test(who) ? { kind: 'person', id: who } : null;
  }

  function query(f) {
    return {
      genre: f.id,
      type: f.type === 'all' ? null : f.type,
      sort: f.sort === 'popular' ? null : f.sort,
      where: f.where === 'library' ? f.where : null,
      decade: f.decade || null
    };
  }

  function drop() {
    if (open) {
      clearTimeout(open.timer);
      open.list.stop();
    }
    open = null;
  }

  /* A grid's *fetchPage*, for a first page that may be refused for now: the grid
     alone would show placeholders for minutes. Its failure line says why instead,
     and the page is asked for again by itself. *err*: what came of asking. */
  function patient(mine, fetchPage, err) {
    var waits = 0;

    function first() {
      clearTimeout(mine.timer);
      mine.turn++;
      var got = err ? Promise.reject(err) : fetchPage(1);
      err = null;
      return got;
    }

    function now(turn) { return open === mine && mine.turn === turn; }

    function say(text) {
      var line = mine.list.tail.querySelector('.grid-fail span');
      if (line) line.textContent = text;
    }

    function wait() {
      var ms = WAITS_MS[waits++];
      say(ms ? TOO_MANY : TOO_MANY_STILL);
      if (ms) mine.timer = setTimeout(again, ms);
    }

    function again() {
      var got = first();
      var turn = mine.turn;
      got.then(function (page) {
        if (!now(turn)) return;
        var held = mine.list.tail.contains(document.activeElement);
        mine.list.set({ cards: page.cards, next: page.next, fetchPage: fetchPage }, { move: false });
        var card = held && mine.host.querySelector('.watch-card__link');
        if (card) card.focus({ preventScroll: true });
      }, function (e) {
        if (!now(turn)) return;
        if (e && e.status === 429) wait();
        else say(FAILED);
      });
    }

    return function (page) {
      if (page !== 1) return fetchPage(page);
      var got = first();
      var turn = mine.turn;
      return got.catch(function (e) {
        if (!e || e.status !== 429) throw e;
        // Once the grid has its line up.
        setTimeout(function () { if (now(turn)) wait(); });
        throw new Error(TOO_MANY);
      });
    };
  }

  function frame() {
    var node = el('div', 'results-page explore');
    var back = el('button', 'explore-back');
    back.type = 'button';
    back.appendChild(window.MM.dom.icon('i-chevron-left'));
    back.appendChild(document.createTextNode('Back'));
    back.addEventListener('click', function () { address.back('explore'); });
    node.appendChild(back);
    var head = el('div', 'explore-head');
    var text = el('div', 'explore-head__text');
    var title = el('h1', 'explore-title');
    title.setAttribute('tabindex', '-1');
    var facts = el('p', 'explore-facts');
    text.appendChild(title);
    text.appendChild(facts);
    head.appendChild(text);
    node.appendChild(head);
    return { node: node, head: head, text: text, title: title, facts: facts, host: el('div', 'explore-grid') };
  }

  function plural(n) { return n === 1 ? '1 title' : n.toLocaleString() + ' titles'; }

  /* Without TMDB there is nothing to list: a line says so, with the way on for an admin. */
  function notSetUp(f) {
    var data = stage().data;
    var line = el('p', 'results-hint');
    line.appendChild(window.MM.dom.icon('i-circle-info'));
    var text = el('span', '', data.is_admin ? 'Connect TMDB to find titles this way. ' : 'This can’t be looked up right now.');
    var href = data.is_admin ? core.safePath(data.settings_tmdb_href) : '';
    if (href) {
      var a = el('a', '', 'Set up TMDB');
      a.href = href;
      text.appendChild(a);
    }
    line.appendChild(text);
    f.host.replaceWith(line);
  }

  /* There is no such category or person: no other try can find it. */
  function gone(f, list, title, text) {
    list.stop();
    f.node.classList.remove('is-unknown');
    f.title.textContent = title;
    f.facts.textContent = '';
    core.toArray(f.node.querySelectorAll('.explore-filters, .explore-gathering')).forEach(function (n) { n.remove(); });
    var box = el('div', 'grid-empty');
    box.appendChild(window.MM.dom.icon('i-film'));
    box.appendChild(el('p', 'grid-empty__title', text));
    var link = el('a', 'grid-empty__link', 'Back to all rows');
    link.href = '/watch';
    link.addEventListener('click', function (e) {
      if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      address.push(null, address.url(OWN));
      core.byId('watch-search').focus({ preventScroll: true });
    });
    box.appendChild(link);
    f.host.replaceWith(box);
    stage().rise(box);
    stage().say(title);
  }

  /* ── A category ── */

  function showGenre(want, from) {
    drop();
    var f = frame();
    var known = genre(want.id);
    var name = nameOf(want.id);
    var mine = { kind: 'genre', id: want.id, host: f.host, turn: 0, timer: null };
    var filter = { id: want.id, type: want.type, sort: want.sort, where: want.where, decade: want.decade };
    var bars = {};
    var say = speaker();
    var gathering = null; // the line that says so
    var drawn = false; // whether what may move is on the page
    f.title.textContent = name;

    function facts(total) {
      // The answer may say less than was made room for.
      if (typeof total !== 'number') f.facts.setAttribute('data-room', words(filter.where === 'library' ? 1000 : 10000));
      f.facts.textContent = '';
      f.facts.appendChild(el('span', '', words(total)));
    }

    function words(total) {
      var kind = known && known.types.length === 1 ? known.types[0] : filter.type;
      return sortName(filter.sort) + ' ' + TYPE_WORDS[kind] + (filter.decade ? ' of the ' + filter.decade : '') +
        (filter.where === 'library' ? ' in your library' : '') +
        (typeof total === 'number' ? ' · ' + plural(total) : '');
    }

    /* One order's pages; a filter changed meanwhile has its own. */
    function pages(of) {
      function ask(page) {
        say.next();
        if (arrived && of.decade && held.decades.indexOf(of.decade) === -1) dropDecade(of);
        return window.MM.api.get(url(want.id, of, page)).then(function (reply) {
          if (open === mine && page === 1 && url(want.id, of) === url(want.id, filter)) learn(reply);
          return { cards: ((reply && reply.items) || []).map(function (i) { return watch.search.tmdbCard(i); }), next: (reply && reply.next_page) || null };
        }, function (err) {
          if (err && err.status === 400 && of.decade) {
            dropDecade(of);
            return ask(page);
          }
          if (err && err.status === 404 && open === mine) {
            gone(f, mine.list, 'Category not found', 'There is no such category.');
            stage().token({ icon: icon(want.id), name: 'Category' });
          }
          // Its failure is shown; which filters are offered is still not known.
          if (open === mine) f.host.classList.remove('is-unknown');
          throw err;
        });
      }
      return function (page) {
        // A link may ask for years that are not listed.
        return of.decade && !arrived ? categories().then(function () { return ask(page); }) : ask(page);
      };
    }

    function dropDecade(of) {
      if (filter.decade === of.decade && (!open || open === mine)) {
        filter.decade = '';
        years.choose('');
        address.replace('explore', address.url(query(filter)));
        facts();
      }
      of.decade = '';
    }

    function learn(reply) {
      if (!reply) return;
      if (reply.tmdb_configured === false) { mine.list.stop(); notSetUp(f); reveal(); return; }
      if (reply.genre && reply.genre.name !== f.title.textContent) {
        f.title.textContent = reply.genre.name;
        stage().token({ icon: icon(want.id), name: reply.genre.name });
      }
      if (reply.genre) settleTypes(reply.genre);
      settleWhere(reply.library);
      reveal();
      facts(reply.total);
      say.page(f.title.textContent + ', ' + f.facts.textContent);
    }

    /* Only films or only TV: there is no kind to choose. */
    function settleTypes(g) {
      known = known || g;
      bars.type.hidden = g.types.length < 2;
      // A link may ask for a kind it has not.
      if (filter.type !== 'all' && g.types.indexOf(filter.type) === -1) change('type', 'all');
    }

    function reveal() {
      drawn = true;
      filters.classList.remove('is-unknown');
      f.host.classList.remove('is-unknown');
    }

    /* Offered once the library's own genres are known. A link may come before
       that. Once drawn, neither the choice nor the line above the grid comes or
       gives up its place. */
    function settleWhere(library) {
      if (!library) return;
      if (!drawn) bars.where.hidden = !library.ready;
      var early = !library.ready && filter.where === 'library';
      if (early && !gathering && !drawn) {
        gathering = el('p', 'results-hint explore-gathering');
        gathering.appendChild(window.MM.dom.icon('i-circle-info'));
        var text = el('span', '', 'What is in your library is still being gathered, so some of it may be missing. ');
        var button = el('button', 'explore-gathering__out', 'Look anywhere');
        button.type = 'button';
        button.addEventListener('click', function () {
          bars.where.querySelector('[data-value="anywhere"]').click();
          f.title.focus({ preventScroll: true });
        });
        text.appendChild(button);
        gathering.appendChild(text);
        f.host.before(gathering);
        stage().rise(gathering);
      }
      if (gathering) gathering.classList.toggle('is-unknown', !early);
    }

    /* All of it is in the library: its cards need not say so. */
    function heldOnly(of) { f.host.classList.toggle('is-held', of.where === 'library'); }

    function change(key, value) {
      filter[key] = value;
      address.replace('explore', address.url(query(filter)));
      facts();
      if (gathering && filter.where !== 'library') {
        gathering.remove();
        gathering = null;
      }
      load();
    }

    function load() {
      clearTimeout(mine.timer);
      var turn = ++mine.turn;
      var of = Object.assign({}, filter);
      var fetchPage = pages(of);
      mine.list.busy(true);
      fetchPage(1).then(function (got) {
        if (open !== mine || turn !== mine.turn) return;
        heldOnly(of);
        mine.list.set({ cards: got.cards, next: got.next, fetchPage: fetchPage }, { move: true });
      }, function (err) {
        if (open !== mine || turn !== mine.turn) return;
        heldOnly(of);
        // The grid is told when it asks, and says how to go on.
        mine.list.set({ cards: [], next: 1, fetchPage: patient(mine, fetchPage, err) }, { move: false });
      });
    }

    var filters = el('div', 'explore-filters');
    bars.type = segments('Type', TYPES, filter.type, function (v) { change('type', v); });
    bars.sort = segments('Order', SORTS, filter.sort, function (v) { change('sort', v); });
    bars.where = segments('Where', WHERES, filter.where, function (v) { change('where', v); });
    filters.appendChild(bars.type);
    filters.appendChild(bars.sort);
    filters.appendChild(bars.where);
    var years = yearsMenu(filter.decade, function (v) { change('decade', v); });
    filters.appendChild(years.node);
    // Their places are held until it is known which are offered, and whether a line pushes the grid down.
    filters.classList.add('is-unknown');
    f.host.classList.toggle('is-unknown', filter.where === 'library');
    f.node.appendChild(filters);
    f.node.appendChild(f.host);
    heldOnly(filter);
    facts();
    stage().open(f.node, name, { icon: icon(want.id), name: name });
    stage().grow(f.title, from);
    stage().rise(f.facts, 60);
    stage().rise(filters, 80);
    open = mine;
    mine.list = watch.grid.list(f.host, {
      title: name, cards: [], next: 1, fetchPage: patient(mine, pages(Object.assign({}, filter))), empty: NO_FIT, say: say.grid
    });
    f.title.focus({ preventScroll: true });
    if (arrived) settleWhere(held.library || { ready: false });
    if (known) settleTypes(known);
    if (arrived) reveal();
    if (!known) categories().then(function (all) {
      if (open !== mine) return;
      years.fill(all.decades);
      if (!arrived) return; // the first page will say
      settleWhere(all.library || { ready: false });
      if (genre(want.id)) settleTypes(genre(want.id));
      reveal();
    });
  }

  /* A row of choices with one thumb, which slides to the one pressed from
     wherever it is. */
  function segments(label, options, value, choose) {
    var box = el('div', 'explore-seg');
    box.setAttribute('role', 'group');
    box.setAttribute('aria-label', label);
    var thumb = el('span', 'explore-seg__thumb');
    options.forEach(function (o) {
      var b = el('button', 'explore-seg__item');
      b.type = 'button';
      b.setAttribute('data-value', o[0]);
      b.setAttribute('aria-pressed', o[0] === value ? 'true' : 'false');
      if (o[0] === value) b.appendChild(thumb);
      b.appendChild(el('span', 'explore-seg__label', o[1]));
      b.addEventListener('click', function () {
        if (b.contains(thumb)) return;
        var from = thumb.getBoundingClientRect();
        (thumb.getAnimations ? thumb.getAnimations() : []).forEach(function (a) { a.cancel(); });
        core.toArray(box.children).forEach(function (n) { n.setAttribute('aria-pressed', n === b ? 'true' : 'false'); });
        b.insertBefore(thumb, b.firstChild);
        var to = thumb.getBoundingClientRect();
        if (!core.reduceMotion() && thumb.animate && to.width) {
          thumb.animate([
            { transform: 'translateX(' + (from.left - to.left) + 'px) scaleX(' + from.width / to.width + ')' },
            { transform: 'none' }
          ], { duration: 260, easing: stage().spring() });
        }
        b.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: core.reduceMotion() ? 'auto' : 'smooth' });
        choose(o[0]);
      });
      box.appendChild(b);
    });
    return box;
  }

  function yearsMenu(value, choose) {
    var node = el('span', 'explore-years');
    var select = el('select', 'explore-years__select');
    select.setAttribute('aria-label', 'Years');
    node.appendChild(select);
    node.appendChild(window.MM.dom.icon('i-chevron-down'));
    function fill(decades) {
      select.textContent = '';
      [''].concat(decades, value && decades.indexOf(value) === -1 ? [value] : []).forEach(function (d) {
        var o = el('option', '', d || 'Any year');
        o.value = d;
        select.appendChild(o);
      });
      select.value = value;
    }
    fill(held.decades);
    select.addEventListener('change', function () {
      value = select.value;
      choose(value);
    });
    return { node: node, fill: fill, choose: function (to) { value = select.value = to; } };
  }

  /* ── A person ── */

  /* "Born 1960, died 2016, aged 55", or what of it is known. */
  function born(p) {
    var year = String(p.birthday || '').slice(0, 4);
    var end = String(p.deathday || '').slice(0, 4);
    var age = typeof p.age === 'number' ? p.age : null;
    return [
      year && 'Born ' + year,
      end && (year ? 'died ' : 'Died ') + end,
      age !== null && (end ? 'aged ' : 'age ') + age
    ].filter(Boolean).join(', ');
  }

  function line(width) {
    var s = el('span', 'skel skel--line');
    s.style.width = width;
    return s;
  }

  function knownPoster(t) {
    var a = el('a', 'cast-card__poster');
    a.setAttribute('data-title-ref', t.library_ref || t.ref);
    a.setAttribute('data-title', t.title);
    a.href = core.safePath(t.href) || '#title=' + encodeURIComponent(t.library_ref || t.ref);
    a.title = t.title;
    a.setAttribute('aria-label', t.title);
    a.appendChild(el('span', 'cast-card__poster-name', t.title));
    var src = core.safePath(t.poster_url);
    if (src) {
      var img = el('img');
      img.alt = '';
      img.setAttribute('data-broken-on-error', '');
      img.src = src;
      a.appendChild(img);
    }
    return a;
  }

  /* What is said of a person on page *f*, its room held until it is known. What
     stands below slides when that room changes. */
  function aboutPerson(f) {
    var years = el('p', 'explore-born');
    var story = el('div', 'explore-story');
    var bio = el('p', 'explore-bio');
    var more = el('button', 'explore-more', 'More');
    var known = el('div', 'explore-known');
    var posters = el('div', 'cast-card__posters');
    var drawn = null; // what it says, as JSON
    more.type = 'button';
    bio.id = 'explore-bio';
    more.setAttribute('aria-controls', bio.id);
    more.setAttribute('aria-expanded', 'false');
    story.appendChild(bio);
    story.appendChild(more);
    known.appendChild(el('p', 'cast-card__label', 'Known for'));
    known.appendChild(posters);
    f.text.appendChild(years);
    f.head.appendChild(story);
    f.head.appendChild(known);
    years.appendChild(line('130px'));
    ['100%', '96%', '64%'].forEach(function (w) { bio.appendChild(line(w)); });
    for (var i = 0; i < 4; i++) posters.appendChild(el('span', 'cast-card__poster skel'));
    more.classList.add('is-unknown');
    known.setAttribute('aria-hidden', 'true');

    var gliding = [];

    /* Make *change*; what it moved glides from where it is on the screen this instant. */
    function moving(change) {
      var nodes = [f.text, more, known, f.host];
      var was = nodes.map(function (n) { return n.getBoundingClientRect().top; });
      gliding.forEach(function (a) { a.cancel(); });
      gliding = [];
      bio.classList.remove('is-closing');
      change();
      if (core.reduceMotion() || !f.host.animate) return;
      nodes.forEach(function (n, at) {
        var by = was[at] - n.getBoundingClientRect().top;
        if (n.getClientRects().length && Math.abs(by) >= 1) glide(n, [{ transform: 'translateY(' + by + 'px)' }, { transform: 'none' }]);
      });
    }

    function glide(node, frames) {
      var a = node.animate(frames, { duration: core.tokenMs('--dur', 250), easing: core.token('--ease-out', 'ease-out') });
      gliding.push(a);
      return a;
    }

    /* The text's edge goes along with the button under it. */
    function turn(whole) {
      var gap = more.getBoundingClientRect().top - bio.getBoundingClientRect().top;
      moving(function () {
        gap -= more.getBoundingClientRect().top - bio.getBoundingClientRect().top;
        var resting = bio.offsetHeight;
        bio.classList.toggle('is-whole', whole);
        more.textContent = whole ? 'Less' : 'More';
        more.setAttribute('aria-expanded', whole ? 'true' : 'false');
        fit();
        if (core.reduceMotion() || !bio.animate) return;
        bio.classList.toggle('is-closing', !whole);
        var seen = resting + gap;
        glide(bio, [{ clipPath: 'inset(0 0 ' + (bio.offsetHeight - seen) + 'px)' }, { clipPath: 'inset(0)' }]).onfinish = function () {
          bio.classList.remove('is-closing');
        };
      });
    }

    /* Open, it keeps its way back. */
    function fit() {
      if (drawn && !bio.classList.contains('is-whole')) more.hidden = !(bio.scrollHeight > bio.clientHeight + 1);
    }

    more.addEventListener('click', function () {
      turn(!bio.classList.contains('is-whole'));
    });
    if (window.ResizeObserver) new window.ResizeObserver(fit).observe(bio);

    return {
      /* *of*: { person, known_for }, or null where nothing can be had now; *beside*
         writes the rest of the heading in the same breath. */
      draw: function (of, beside) {
        var key = of && JSON.stringify(of);
        moving(function () {
          if (beside) beside();
          if (drawn && (!of || key === drawn)) return;
          drawn = key;
          var p = (of && of.person) || {};
          years.textContent = born(p);
          years.hidden = !years.textContent;
          bio.textContent = p.biography || '';
          bio.classList.remove('is-whole');
          more.textContent = 'More';
          more.setAttribute('aria-expanded', 'false');
          more.classList.remove('is-unknown');
          story.hidden = !p.biography;
          posters.textContent = '';
          ((of && of.known_for) || []).slice(0, 4).forEach(function (t) { posters.appendChild(knownPoster(t)); });
          known.hidden = !posters.children.length;
          known.removeAttribute('aria-hidden');
          f.head.classList.add('is-told');
          fit();
        });
      }
    };
  }

  function showPerson(who, from) {
    drop();
    var f = frame();
    var id = String(who.person_id);
    var mine = { kind: 'person', id: id, host: f.host, turn: 0, timer: null };
    var photo = stage().face(who.profile_url, who.name);
    var heads = {};
    var say = speaker();
    var shown = new Set(); // the titles listed so far
    photo.classList.add('explore-photo');
    f.head.insertBefore(photo, f.text);
    f.node.classList.add('explore--person');
    f.title.textContent = who.name || '';
    f.facts.textContent = who.job || '';
    f.node.classList.toggle('is-unknown', !who.name);
    f.node.appendChild(f.host);
    var about = aboutPerson(f);

    function learn(reply) {
      if (reply.tmdb_configured === false || !reply.person) { about.draw(null); mine.list.stop(); notSetUp(f); return; }
      var p = reply.person;
      var more = Math.max(0, reply.total - reply.in_library);
      about.draw(persons.get(id), function () {
        f.node.classList.remove('is-unknown');
        f.title.textContent = p.name;
        f.facts.textContent = [p.job, reply.total && reply.in_library + ' in your library, ' + more + ' more'].filter(Boolean).join(' · ');
      });
      sharpen(p.photo_url || p.profile_url);
      if (!who.name) {
        stage().token({ face: p.profile_url, name: p.name, person: p.name });
        name(p.name);
      }
      say.page(p.name + ', ' + plural(reply.total));
    }

    /* Who it is, said where the outline of a person stood. */
    function name(text) {
      photo.children[0].replaceWith(stage().face(null, text).children[0]);
    }

    /* The larger photo takes the small one's place once it is here. */
    function sharpen(address) {
      var src = core.safePath(address);
      if (!src) return;
      var img = photo.querySelector('img');
      if (!img) {
        img = el('img');
        img.alt = '';
        img.setAttribute('data-broken-on-error', '');
        photo.appendChild(img);
        img.src = src;
        return;
      }
      var large = new window.Image();
      large.onload = function () { img.src = src; };
      large.src = src;
    }

    function head(key, title, count) {
      if (heads[key]) return [];
      heads[key] = true;
      var node = stage().head(title, count);
      node.setAttribute('data-group', key);
      return [node];
    }

    /* One page, or the next that has something new. What the library holds comes
       first; a title of it found later stands where it comes, with its badge. */
    function fetchPage(page) {
      say.next();
      return personPage(id, page).then(function (reply) {
        if (open === mine && page === 1) learn(reply);
        if (page === 1) {
          heads = {};
          shown.clear();
        }
        var cards = [];
        (reply.items || []).forEach(function (item) {
          if (shown.has(item.ref)) return;
          shown.add(item.ref);
          var held = item.in_library && !heads.tmdb;
          if (held) cards = cards.concat(head('lib', 'In your library', reply.in_library));
          else if (!item.in_library) cards = cards.concat(head('tmdb', 'Available to download', reply.total - reply.in_library));
          cards.push(watch.search.tmdbCard(item, held));
        });
        var next = reply.next_page || null;
        return cards.length || !next ? { cards: cards, next: next } : fetchPage(next);
      }, function (err) {
        if (open === mine && page === 1) {
          about.draw(null);
          if (err && err.status === 404) gone(f, mine.list, 'Person not found', 'There is nobody to show here.');
          else if (!f.title.textContent) {
            f.node.classList.remove('is-unknown');
            f.title.textContent = 'Couldn’t load this person';
          }
        }
        throw err;
      });
    }

    stage().open(f.node, who.name || 'Person', { face: who.profile_url || '', name: who.name || 'Person', person: who.name });
    if (persons.has(id)) about.draw(persons.get(id));
    stage().grow(photo, from);
    stage().rise(f.text, 60);
    open = mine;
    mine.list = watch.grid.list(f.host, {
      title: who.name || 'Person', cards: [], next: 1, fetchPage: patient(mine, fetchPage),
      empty: { title: 'Nothing to show yet' }, say: say.grid
    });
    f.title.focus({ preventScroll: true });
  }

  /* ── Opening ── */

  /* *from*: the node on the page it grows from. */
  function openGenre(id, facts, from) {
    var rect = from ? from.getBoundingClientRect() : null;
    var want = {
      kind: 'genre', id: id, type: choice(facts && facts.type, TYPES),
      sort: choice(facts && facts.sort, SORTS), where: 'anywhere', decade: (facts && facts.decade) || ''
    };
    address.push('explore', address.url(Object.assign({}, OWN, query(want)), ''));
    showGenre(want, rect);
  }

  function openPerson(who, from) {
    var rect = from ? from.getBoundingClientRect() : null;
    address.push('explore', address.url(Object.assign({}, OWN, { person: who.person_id }), ''));
    showPerson(who, rect);
  }

  address.layer({
    name: 'explore',
    depth: 20,
    params: ['genre', 'type', 'sort', 'where', 'decade', 'person'],
    read: function (here) {
      var at = readAddress(here.params);
      return at ? new URLSearchParams(Object.entries(at)).toString() : null;
    },
    open: function () {
      var at = readAddress(new URLSearchParams(window.location.search));
      if (at.kind === 'genre') showGenre(at, null);
      else showPerson(persons.has(at.id) ? persons.get(at.id).person : { person_id: at.id }, null);
    },
    close: function () {
      drop();
      stage().close();
    }
  });

  /* On window, so that a menu or a preview of the page's own has had its Esc first. */
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.defaultPrevented || watch.search.composing(e) || !open || address.top() !== 'explore') return;
    e.preventDefault();
    address.back('explore');
  });

  // The Browse half of the switch is the way out to the rows.
  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('#watch-viewtoggle [data-view="rows"]');
    if (!link || !open || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    address.push(null, address.url(OWN));
  });

  watch.explore = {
    categories: categories,
    all: function () { return held; },
    known: function () { return arrived; },
    parse: parse,
    url: url,
    icon: icon,
    drawing: drawing,
    sortName: sortName,
    person: person,
    born: born,
    openGenre: openGenre,
    openPerson: openPerson
  };
})();
