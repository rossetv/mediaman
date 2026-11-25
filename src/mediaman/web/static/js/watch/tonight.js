/* Watch: Tonight, at the top of the search panel: how long have you got, and three titles of
 * the library the person has not begun that fit. On window.MMWatch.tonight, which
 * watch/search.js works. Needs watch/core.js and watch/ask.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;
  var safePath = core.safePath;

  var TIMES = [
    { name: '1 h', minutes: 60, said: 'an hour' },
    { name: '1½ h', minutes: 90, said: 'an hour and a half' },
    { name: '2 h', minutes: 120, said: 'two hours' },
    { name: 'All evening', minutes: 400, said: 'an evening' }
  ];
  var SET = 3;

  function endsAt(minutes) {
    return new Date(Date.now() + minutes * 60000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }

  /* *opts*: option(node, key) makes a node one of the panel's options, say(text) reads out,
     open(item) opens a title, and gone() follows options that were taken away. */
  function make(opts) {
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

    var at = -1;
    var skip = 0;
    var turn = 0;
    var next = null; // how far the other titles are, or null: there are none
    var ends = []; // the lines that say when: { node, item }

    var times = TIMES.map(function (t, i) {
      var b = opts.option(button('', t.name), 'tonight-' + i);
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
      at = i;
      skip = from;
      times.forEach(function (b, n) { b.setAttribute('aria-checked', n === i ? 'true' : 'false'); });
      bar.style.opacity = 1;
      bar.style.transform = 'translateX(' + i * 100 + '%)';
      if (!node.classList.contains('is-on')) waiting();
      node.classList.add('is-on');
      list.classList.add('is-old');
      MM.api.get('/api/search/assist/tonight?minutes=' + TIMES[i].minutes + (from ? '&skip=' + from : '')).then(function (data) {
        if (mine === turn) draw(data);
      }).catch(function (err) {
        if (mine === turn) failed(err);
      });
    }

    /* The list holds *nodes*; *more* has its place either way. */
    function put(nodes, text, hasMore) {
      list.textContent = '';
      list.classList.remove('is-old');
      nodes.forEach(function (n) { list.appendChild(n); });
      told.textContent = text;
      more.hidden = !hasMore;
      foot.hidden = !text;
      opts.gone();
    }

    function waiting() {
      var box = el('div', 'tn__list');
      box.setAttribute('aria-hidden', 'true');
      for (var i = 0; i < SET; i++) box.appendChild(el('div', 'tn__card skel tn__card--skel'));
      ends = [];
      put([box], '', false);
    }

    function sub(item) {
      return (item.kind === 'show' ? 'TV · season ' + (item.season || 1) : [item.year, item.genre].filter(Boolean).join(' · ')) +
        (item.minutes ? ', ends ' + endsAt(item.minutes) : '');
    }

    function card(item, i) {
      var a = opts.option(el('a', 'tn__card'), 'tonight-title-' + i);
      a.href = safePath(item.href) || '/watch';
      a.style.animationDelay = i * 50 + 'ms';
      var art = a.appendChild(el('span', 'tn__art'));
      var poster = safePath(item.poster_url);
      if (poster) {
        var img = art.appendChild(el('img'));
        img.alt = '';
        // Its wide picture, else its poster.
        img.addEventListener('error', function () {
          if (img.src.indexOf('kind=art') < 0) return;
          img.setAttribute('data-broken-on-error', '');
          img.src = poster;
        });
        img.src = poster + '?kind=art';
      }
      if (item.minutes) art.appendChild(el('span', '', window.MMWatch.ask.dur(item.minutes)));
      var text = a.appendChild(el('div'));
      text.appendChild(el('b', '', item.title));
      ends.push({ node: text.appendChild(el('small', '', sub(item))), item: item });
      text.appendChild(el('p', '', item.reason));
      a.addEventListener('click', function (e) {
        if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        if (opts.open(item)) e.preventDefault();
      });
      return a;
    }

    function draw(data) {
      var items = data.items || [];
      var time = TIMES[at];
      ends = [];
      if (!items.length) { none(data.shortest, time); return; }
      next = data.more ? skip + SET : skip ? 0 : null;
      var box = el('div', 'tn__list');
      items.forEach(function (item, i) { box.appendChild(card(item, i)); });
      var few = data.total < SET;
      var text = few ? 'Only ' + data.total + ' unseen ' + (data.total === 1 ? 'title fits' : 'titles fit') + ' in ' + time.said
        : 'All in your library, none seen';
      more.textContent = next === 0 ? 'Back to the first' : data.total - next < SET ? 'The others' : 'Three others';
      put([box], text, next !== null);
      opts.say(items.map(function (item) { return item.title; }).join(', ') + '. ' + text);
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
      if (!shortest || !shortest.minutes) { line('Nothing here is unseen, or how long it runs isn’t known yet.'); return; }
      var i = TIMES.findIndex(function (t) { return t.minutes >= shortest.minutes; });
      var text = 'Nothing unseen here ' + (time === TIMES[TIMES.length - 1] ? 'fits in ' : 'is under ') + time.said +
        '. The shortest is ' + shortest.title + ', ' + window.MMWatch.ask.dur(shortest.minutes) + '.';
      line(text, i < 0 ? '' : 'Show ' + TIMES[i].name, function () { choose(i, 0); });
    }

    function failed(err) {
      next = null;
      line(err && err.status === 429 ? 'That was a lot at once. Wait a minute, then try again.' : 'Couldn’t look just now.', 'Try again',
        function () { choose(at, skip); });
    }

    return {
      node: node,
      /* The panel opens again, later. */
      refresh: function () { ends.forEach(function (e) { e.node.textContent = sub(e.item); }); }
    };
  }

  window.MMWatch.tonight = { make: make };
})();
