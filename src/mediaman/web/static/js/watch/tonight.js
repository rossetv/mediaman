/* Watch: Tonight, at the top of the search panel: how long have you got, and what fits. First
 * what the person can play now of the library: what they are partway through, the next episode
 * of a show they watch, then what they never began; under it, when it comes, titles the library
 * does not have. On window.MMWatch.tonight, which watch/search.js works. Needs watch/core.js,
 * watch/search.js and watch/ask.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;

  var TIMES = [
    { name: '1 h', minutes: 60, said: 'an hour' },
    { name: '1½ h', minutes: 90, said: 'an hour and a half' },
    { name: '2 h', minutes: 120, said: 'two hours' },
    // The most the server takes: no limit.
    { name: 'All evening', minutes: 720, said: 'an evening', short: 'Evening' }
  ];
  var SET = 6; // outlines while the first set comes: the server says how many a set holds
  var COUNTS = ['One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight'];

  function dur(minutes) { return window.MMWatch.ask.dur(minutes); }

  /* What a card says under its poster: which episode, and how long it runs or has left. */
  function sub(item) {
    var length = item.minutes ? dur(item.minutes) + (item.left ? ' left' : '') : '';
    var what = item.kind === 'show' ? (item.episode ? 'S' + item.season + ' · E' + item.episode : 'TV') : item.year || 'Movie';
    return [what, length].filter(Boolean).join(' · ');
  }

  function awaySub(item) {
    var kind = item.kind === 'show' ? 'TV' : 'Movie';
    return item.minutes ? kind + ' · ' + dur(item.minutes) + (item.kind === 'show' ? ' episodes' : '') : kind;
  }

  /* *opts*: option(node, key) makes a node one of the panel's options, say(text) reads out,
     open(item) opens a title, and gone() follows options that were taken away. */
  function make(opts) {
    var search = window.MMWatch.search;
    var node = el('section', 'tn');
    var head = node.appendChild(el('div', 'tn__head'));
    var name = head.appendChild(el('b', '', 'How long have you got tonight?'));
    name.id = 'watch-tonight-title';
    node.setAttribute('role', 'group');
    node.setAttribute('aria-labelledby', name.id);
    var seg = head.appendChild(el('div', 'tn__seg'));
    var bar = seg.appendChild(el('i'));
    bar.setAttribute('aria-hidden', 'true');
    var out = node.appendChild(el('div', 'tn__out')).appendChild(el('div'));
    var list = out.appendChild(el('div'));
    var foot = out.appendChild(el('div', 'tn__foot'));
    var told = foot.appendChild(el('span'));
    var more = opts.option(button('txt', ''), 'tonight-more');
    foot.appendChild(more);
    foot.hidden = true;
    var away = out.appendChild(el('div')); // the titles the library does not have

    var at = -1;
    var skip = 0;
    var turn = 0;
    var awayFor = 0; // the minutes the titles not in the library were asked for
    var next = null; // how far the other titles are, or null: there are none

    var times = TIMES.map(function (t, i) {
      var b = opts.option(button('', t.name), 'tonight-' + i);
      if (t.short) b.setAttribute('data-short', t.short);
      b.setAttribute('aria-checked', 'false');
      b.addEventListener('click', function () { choose(i, 0); });
      return seg.appendChild(b);
    });
    more.addEventListener('click', function () { if (next !== null) choose(at, next); });

    function button(className, text) {
      var b = el('button', className, text);
      b.type = 'button';
      return b;
    }

    function choose(i, from) {
      var mine = ++turn;
      var minutes = TIMES[i].minutes;
      if (minutes !== awayFor) elsewhere(minutes);
      at = i;
      skip = from;
      times.forEach(function (b, n) { b.setAttribute('aria-checked', n === i ? 'true' : 'false'); });
      bar.style.opacity = 1;
      bar.style.transform = 'translateX(' + i * 100 + '%)';
      if (!node.classList.contains('is-on')) waiting();
      node.classList.add('is-on');
      list.classList.add('is-old');
      MM.api.get('/api/search/assist/tonight?minutes=' + minutes + (from ? '&skip=' + from : '')).then(function (data) {
        if (mine === turn) draw(data);
      }).catch(function (err) {
        if (mine === turn) failed(err);
      });
    }

    /* The titles the library does not have, which come when they come, and are left out
       quietly when they do not. Those of the time before stay, dimmed, till then: the panel
       keeps its height. */
    function elsewhere(minutes) {
      awayFor = minutes;
      away.classList.add('is-old');
      away.setAttribute('aria-busy', 'true');
      MM.api.get('/api/search/assist/tonight/elsewhere?minutes=' + minutes).then(function (data) {
        if (minutes === awayFor) drawAway(data.items || []);
      }, function () {
        // The library's own are what count.
        if (minutes === awayFor) drawAway([]);
      });
    }

    /* The list holds *nodes*; *more* has its place either way. */
    function put(nodes, text, hasMore) {
      list.textContent = '';
      list.classList.remove('is-old');
      nodes.forEach(function (n) { list.appendChild(n); });
      told.textContent = text;
      more.hidden = !hasMore;
      foot.hidden = !text && !hasMore;
      opts.gone();
    }

    function waiting() {
      var box = el('div', 'tn__list');
      box.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < SET; i++) {
        var card = box.appendChild(el('div', 'watch-card watch-card--skel'));
        card.appendChild(el('span', 'skel skel--poster'));
        card.appendChild(el('span', 'skel skel--line'));
      }
      put([box], '', false);
    }

    function group(title, note, cards) {
      var part = el('div', 'tn__part');
      var line = part.appendChild(el('p', 'tn__group', title));
      if (note) line.appendChild(el('small', '', note));
      var box = part.appendChild(el('div', 'tn__list'));
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', title);
      cards.forEach(function (c) { box.appendChild(c); });
      return part;
    }

    /* A poster card as the rows have, one of the panel's options; it opens the title. */
    function option(card, item, key, i) {
      var link = opts.option(card.querySelector('.watch-card__link'), key);
      card.style.animationDelay = i * 40 + 'ms';
      link.addEventListener('click', function (e) {
        if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (opts.open(item)) e.preventDefault();
      });
      return card;
    }

    // Its rating and mark are on its title's page: buttons under a poster would stand among the options.
    var NO_TOOLS = { tmdb_ref: null, mark_kind: null, mark_key: null };

    function readyCard(item, i) {
      var card = search.card(Object.assign({}, item, NO_TOOLS, { subtitle: sub(item) }));
      card.classList.add('tn__card');
      return option(card, item, 'tonight-title-' + i, i);
    }

    function awayCard(item, i) {
      var card = search.tmdbCard(Object.assign({}, item, NO_TOOLS));
      core.caption(card.querySelector('.watch-card__sub'), awaySub(item));
      card.classList.add('tn__card');
      return option(card, item, 'tonight-away-' + i, i);
    }

    function drawAway(items) {
      away.textContent = '';
      away.classList.remove('is-old');
      away.removeAttribute('aria-busy');
      if (!items.length) { opts.gone(); return; }
      var can = items.some(function (item) { return item.can_download; });
      away.appendChild(group('Not in your library', can ? 'Open one to download it' : '', items.map(awayCard)));
      opts.gone();
    }

    function draw(data) {
      var items = data.items || [];
      var time = TIMES[at];
      if (!items.length) { none(data.shortest, time); return; }
      next = data.more ? data.next : skip ? 0 : null;
      // Only where every running time was known is the count all that fits.
      var text = data.checked && !data.more && !skip && items.length < SET
        ? 'Only ' + data.total + (data.total === 1 ? ' title fits' : ' titles fit') + ' in ' + time.said : '';
      more.textContent = next === 0 ? 'Back to the first'
        : data.total - next <= items.length ? 'The others' : (COUNTS[items.length - 1] || items.length) + ' others';
      put([group('Ready to play', '', items.map(readyCard))], text, next !== null);
      opts.say(items.map(function (item) { return item.title; }).join(', ') + (text ? '. ' + text : ''));
    }

    function line(text, label, act) {
      var p = el('p', 'tn__none', text);
      if (label) {
        p.appendChild(el('br'));
        var b = opts.option(button('chip', label), 'tonight-way');
        b.addEventListener('click', act);
        p.appendChild(b);
      }
      put([p], '', false);
      opts.say(text);
    }

    function none(shortest, time) {
      next = null;
      if (!shortest || !shortest.minutes) { line('Nothing here is left to watch, or how long it runs isn’t known yet.'); return; }
      var i = TIMES.findIndex(function (t) { return t.minutes >= shortest.minutes; });
      var text = 'Nothing you can play here ' + (time === TIMES[TIMES.length - 1] ? 'fits in ' : 'is under ') + time.said +
        '. The shortest is ' + shortest.title + ', ' + dur(shortest.minutes) + '.';
      line(text, i < 0 ? '' : 'Show ' + TIMES[i].name, function () { choose(i, 0); });
    }

    function failed(err) {
      next = null;
      line(err && err.status === 429 ? 'That was a lot at once. Wait a minute, then try again.' : 'Couldn’t look just now.', 'Try again',
        function () { choose(at, skip); });
    }

    return { node: node };
  }

  window.MMWatch.tonight = { make: make };
})();
