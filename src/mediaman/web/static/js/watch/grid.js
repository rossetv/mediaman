/* Watch: the one grid of posters, on MMWatch.grid.
 *
 *   list(host, opts)   a grid that loads as it is scrolled (watch/rows-load.js)
 *   open(opts), close(opts), leave()   the layer a row grows into, which holds one
 *   take(card, control)   a card kept or dismissed in it leaves
 *   bring(card)   one that is back in its row is back in that row's grid
 *
 * Opening, each poster on the screen travels from the row to its place in the
 * grid; closing takes it back. Both layers are laid out while something moves:
 * the one arrived at is in the page's flow, the other is fixed where it stood,
 * so both ends of every trip can be measured. Each moving thing is a track with
 * two ends, 'rows' and 'grid'; turning round mid-way reads where each one is
 * and sends it to its other end. Needs watch/core.js, watch/keep.js and
 * watch/rows-load.js; MMWatch.address, when loaded, says whose Esc it is.
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch.core;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var toArray = core.toArray;

  var AHEAD_PX = 700; // the next page is asked for this near the last card
  var AWAKE_SCREENS = 3; // cards further off than this have their posters unloaded
  var HEARD_MS = 3000; // how long what a grid says of itself as it opens is left to be heard
  var JUMP_MS = 4000; // a jump whose next page takes longer is not awaited any more
  var SCROLL_KEYS = /^(Arrow(Up|Down)|Page(Up|Down)|Home|End| )$/;

  /* ── Cards ── */

  function linkOf(card) { return card.querySelector('.watch-card__link'); }

  /* What a card is known by in a row and in the grid. */
  function keyOf(card) {
    var link = linkOf(card);
    return watch.rowsLoad.keyOf(card) || (link ? link.getAttribute('href') : null);
  }

  function isCard(node) { return node.classList.contains('watch-card'); }

  /* A card as it is near the screen: its poster loaded, whatever a loader has done. */
  function wake(card) {
    card.removeAttribute('data-far');
    card.classList.remove('is-arriving');
    var img = card.querySelector('.watch-card__poster img');
    if (img && img.hasAttribute('data-far-src')) {
      img.setAttribute('src', img.getAttribute('data-far-src'));
      img.removeAttribute('data-far-src');
    }
    return card;
  }

  function copy(card) {
    var twin = wake(card.cloneNode(true));
    toArray(twin.querySelectorAll('.is-previewed')).forEach(function (n) { n.classList.remove('is-previewed'); });
    // A row has its own keys (watch-rows.js), the grid the Tab order.
    toArray(twin.querySelectorAll('[tabindex]')).forEach(function (n) { n.removeAttribute('tabindex'); });
    toArray(twin.querySelectorAll('[aria-expanded]')).forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    return twin;
  }

  /* A copied poster is drawn in the frame it appears in, not one later. */
  function drawNow(node) {
    toArray(node.querySelectorAll('img')).forEach(function (img) {
      img.removeAttribute('loading');
      img.decoding = 'sync';
    });
  }

  /* A dated line among the cards (a batch of picks). */
  function group(id, text) {
    var line = el('h2', 'grid-group', text);
    line.setAttribute('data-group', id);
    return line;
  }

  function plural(n) { return n === 1 ? '1 title' : n + ' titles'; }

  function plainClick(e) {
    return !(e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey);
  }

  function animate(node, frames, opts) {
    return node.animate(frames, Object.assign({ easing: core.token('--ease-out', 'ease-out') }, opts));
  }

  function scrollPage(y) { window.scrollTo({ left: 0, top: y, behavior: 'instant' }); }

  /* ── The grid ── */

  function list(host, opts) {
    var title = opts.title;
    var total = opts.total === undefined ? null : opts.total;
    var fetchPage = opts.fetchPage;
    var empty = opts.empty || { title: 'Nothing here yet' };
    var held = false; // nothing loads while the layer moves
    var wanted = null; // the group a jump is on its way to
    var wantedAt = 0; // when its next page was asked for
    var lead = null; // the card that leads the screen, kept there over a resize
    var width = window.innerWidth;
    var loader = null;
    var status = null;
    var marks = new Map(); // where each card and dated line taken out stood: a mark in its place, by it

    host.classList.add('watch-grid-host');
    var grid = host.querySelector('.watch-grid') || host.appendChild(el('div', 'watch-grid'));
    var tail = host.querySelector('.grid-tail') || host.appendChild(el('div', 'grid-tail'));
    grid.setAttribute('tabindex', '-1');

    function say(text) {
      if (opts.say) { opts.say(text); return; }
      if (!status) {
        status = host.appendChild(el('p', 'sr-only'));
        status.setAttribute('role', 'status');
      }
      status.textContent = text;
    }

    function cards() { return toArray(grid.children).filter(isCard); }

    function clearTop() { return opts.clearTop ? opts.clearTop() : core.clearArea().top; }

    function columns() {
      return Math.max(1, getComputedStyle(grid).gridTemplateColumns.split(' ').length);
    }

    /* The cards on the screen. They stand in document order, so the first one
       under it ends the search. */
    function onScreen() {
      var out = [];
      var all = cards();
      for (var i = 0; i < all.length; i++) {
        var r = all[i].getBoundingClientRect();
        if (r.top > window.innerHeight) break;
        if (r.bottom > 0) out.push(all[i]);
      }
      return out;
    }

    /* ── Loading ── */

    var view = {
      scroller: grid,
      near: function () {
        if (held) return false;
        var all = cards();
        var edge = all.length ? all[all.length - 1].getBoundingClientRect().bottom : grid.getBoundingClientRect().top;
        return edge < window.innerHeight + AHEAD_PX;
      },
      placeholders: function () {
        var n = columns();
        var last = 0; // cards since the last dated line
        toArray(grid.children).forEach(function (node) {
          if (isCard(node)) last++;
          else if (node.hasAttribute('data-group')) last = 0;
        });
        return (n - last % n) % n + 2 * n;
      },
      range: function (all) {
        var vh = window.innerHeight;
        var from = all.length;
        var to = 0;
        all.forEach(function (card, i) {
          if (!document.body.contains(card)) return;
          var r = card.getBoundingClientRect();
          if (r.bottom < -AWAKE_SCREENS * vh || r.top > (AWAKE_SCREENS + 1) * vh) return;
          from = Math.min(from, i);
          to = i + 1;
        });
        // Hidden, none of them has a place: as they were.
        return grid.getClientRects().length ? [from, to] : null;
      },
      failed: function (button) {
        wanted = null;
        grid.classList.add('is-static');
        var line = el('div', 'grid-fail');
        line.setAttribute('role', 'alert');
        line.appendChild(el('span', null, cards().length ? 'Couldn’t load more titles.' : 'Couldn’t load the titles.'));
        button.className = 'grid-fail__retry';
        line.appendChild(button);
        tail.replaceChildren(line);
      },
      took: function (fresh, done) {
        grid.classList.remove('is-static');
        tail.replaceChildren();
        if (done) ending();
        var added = fresh.filter(isCard);
        arrive(added);
        var more = added.length < cards().length ? ' more loaded.' : ' loaded.';
        if (added.length || done) say(added.length + more + (done ? ' That is everything.' : ''));
        if (opts.took) opts.took();
        if (Date.now() - wantedAt > JUMP_MS) wanted = null;
        // The loader settles after this; a jump goes on from there.
        if (wanted !== null) setTimeout(function () { if (wanted !== null) jump(wanted, done); });
      }
    };

    function arrive(fresh) {
      if (core.reduceMotion()) return;
      var left = grid.getBoundingClientRect().left;
      fresh.forEach(function (card) {
        var r = card.getBoundingClientRect();
        if (!card.animate || r.top > window.innerHeight || r.bottom < 0) return;
        animate(card, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], {
          duration: core.tokenMs('--dur-slow', 250),
          delay: Math.min(120, Math.round((r.left - left) / Math.max(1, r.width)) * 14),
          fill: 'backwards'
        });
      });
    }

    function ending() {
      var n = cards().length;
      if (n) {
        tail.replaceChildren(el('p', 'grid-end', total === null ? 'That’s everything for now.'
          : n === 1 ? 'That’s the only one.' : 'That’s all ' + n + '.'));
        return;
      }
      var box = el('div', 'grid-empty');
      box.appendChild(icon('i-film'));
      box.appendChild(el('p', 'grid-empty__title', empty.title));
      if (empty.text) box.appendChild(el('p', 'grid-empty__text', empty.text));
      if (empty.link) {
        var link = el('a', 'grid-empty__link', empty.link);
        link.setAttribute('href', core.safePath(empty.href) || '/watch');
        link.setAttribute('data-grid-back', '');
        box.appendChild(link);
      }
      tail.replaceChildren(box);
    }

    function begin(next) {
      if (next === undefined) next = null;
      loader = watch.rowsLoad.start(host, { title: title, next: next, fetchPage: fetchPage, view: view });
      if (next === null) ending();
    }

    /* ── Other contents ── */

    /* Put *content* ({ cards, next, fetchPage, total }) in place of what stands.
       how.move: what is on the screen before and after travels, from where it
       is if it was moving; the rest leaves and arrives. how.stay: the page does not
       scroll back to its start. A title that was here
       keeps its node, so its poster never blinks. */
    function set(content, how) {
      var moves = how && how.move && !core.reduceMotion();
      var before = new Map();
      var old = new Map();
      cards().forEach(function (card) { old.set(keyOf(card), card); });
      if (moves) onScreen().forEach(function (card) { before.set(keyOf(card), card.getBoundingClientRect()); });
      loader.stop();
      wanted = null;
      lead = null;
      if (content.total !== undefined) total = content.total;
      if (content.fetchPage) fetchPage = content.fetchPage;
      grid.classList.remove('is-static');
      grid.removeAttribute('aria-busy');
      busy(false);
      tail.replaceChildren();
      grid.replaceChildren();
      marks.clear();
      content.cards.forEach(function (node) {
        var kept = isCard(node) && old.get(keyOf(node));
        if (kept) {
          (kept.getAnimations ? kept.getAnimations() : []).forEach(function (a) { a.cancel(); });
          old.delete(keyOf(node));
        }
        grid.appendChild(wake(kept || node));
      });
      var by = host.getBoundingClientRect().top - clearTop();
      if (by < 0 && !(how && how.stay)) scrollPage(window.scrollY + by);
      begin(content.next);
      if (moves) travel(before, old);
    }

    function travel(before, gone) {
      before.forEach(function (from, key) {
        var node = gone.get(key);
        if (!node) return;
        node.classList.add('is-gone');
        node.setAttribute('inert', '');
        node.style.left = from.left + 'px';
        node.style.top = from.top + 'px';
        node.style.width = from.width + 'px';
        host.appendChild(node);
        var fade = animate(node, [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: isCard(node) ? 'scale(.96)' : 'none' }], {
          duration: core.tokenMs('--dur', 200), fill: 'forwards'
        });
        fade.onfinish = function () {
          if (!node.classList.contains('is-gone')) return;
          node.remove();
          fade.cancel(); // held, it would hide what an Undo put back
        };
      });
      var n = 0;
      onScreen().forEach(function (card) {
        var from = before.get(keyOf(card));
        var to = card.getBoundingClientRect();
        if (from) {
          card.classList.add('is-flyer');
          var a = animate(card, [
            { transform: 'translate(' + (from.left - to.left) + 'px, ' + (from.top - to.top) + 'px)' },
            { transform: 'none' }
          ], { duration: 380 });
          a.onfinish = a.oncancel = function () { card.classList.remove('is-flyer'); };
        } else {
          animate(card, [{ opacity: 0, transform: 'scale(.94)' }, { opacity: 1, transform: 'none' }], {
            duration: core.tokenMs('--dur-slow', 250), delay: Math.min(120, n++ * 6), fill: 'backwards'
          });
        }
      });
      // A line that comes to stand first loses the space over it: its words stand by its foot.
      linesOnScreen().forEach(function (line) {
        var from = before.get(line);
        var by = from && from.bottom - line.getBoundingClientRect().bottom;
        if (from) animate(line, [{ transform: 'translateY(' + by + 'px)' }, { transform: 'none' }], { duration: 380 });
        else animate(line, [{ opacity: 0 }, { opacity: 1 }], { duration: core.tokenMs('--dur-slow', 250) });
      });
    }

    function linesOnScreen() {
      return groups().filter(function (line) {
        var r = line.getBoundingClientRect();
        return r.bottom > 0 && r.top < window.innerHeight;
      });
    }

    /* Leave a mark in the place of *node*, which goes. */
    function markOut(node) {
      var mark = document.createTextNode('');
      marks.set(mark, node);
      node.replaceWith(mark);
      return mark;
    }

    function putIn(mark) {
      var node = marks.get(mark);
      marks.delete(mark);
      mark.replaceWith(still(node));
    }

    /* As it stood, whatever it was doing on its way out. */
    function still(node) {
      (node.getAnimations ? node.getAnimations() : []).forEach(function (a) { a.cancel(); });
      node.classList.remove('is-gone', 'is-flyer');
      node.removeAttribute('inert');
      ['left', 'top', 'width', 'paddingTop'].forEach(function (name) { node.style[name] = ''; });
      return node;
    }

    function isLine(node) { return node.nodeType === 1 && node.hasAttribute('data-group'); }

    /* The dated line over *node*, or the mark of one that went. */
    function lineOver(node) {
      var all = toArray(grid.childNodes);
      for (var i = all.indexOf(node) - 1; i >= 0; i--) {
        if (isLine(all[i]) || (marks.has(all[i]) && isLine(marks.get(all[i])))) return all[i];
      }
      return null;
    }

    function bare(line) {
      var next = line.nextElementSibling;
      return !next || !isCard(next);
    }

    /* Take *card* out, the others closing up, and the dated line it leaves with none under it.
       Returns what puts them back. */
    function take(card) {
      var mark;
      closeUp(function () {
        var line = lineOver(card);
        mark = markOut(card);
        if (!line || !isLine(line) || !bare(line)) return [];
        // Off the grid it would have the space over it that it does without when first.
        line.style.paddingTop = getComputedStyle(line).paddingTop;
        markOut(line);
        return [line];
      }, card);
      if (loader.next() === null) ending();
      return function () {
        closeUp(function () {
          wake(card);
          putIn(mark);
          lineBack(card);
        }, null);
        if (loader.next() === null) ending();
      };
    }

    function lineBack(card) {
      var line = lineOver(card);
      if (line && marks.has(line)) putIn(line);
    }

    /* The dated lines, each with the cards under it, those taken out by their marks. */
    function dated() {
      var out = [];
      toArray(grid.childNodes).forEach(function (spot) {
        var node = marks.get(spot) || spot;
        if (node.nodeType !== 1) return;
        if (isLine(node)) out.push({ id: idOf(node), spot: spot, cards: [] });
        else if (out.length && isCard(node)) out[out.length - 1].cards.push({ spot: spot, place: placeOf(node) });
      });
      return out;
    }

    function placeOf(card) { return Number(card.getAttribute('data-place')); }

    /* Put *card* at its place under the line of its date, made by *newLine()* where there is none.
       *dates*: the dates in their order. */
    function put(card, dates, newLine) {
      var date = card.getAttribute('data-batch');
      var all = dated();
      var own = all.filter(function (g) { return g.id === date; })[0];
      function endOf(g) { return (g.cards[g.cards.length - 1] || g).spot.nextSibling; }
      closeUp(function () {
        wake(card);
        if (own) {
          var next = own.cards.filter(function (c) { return c.place > placeOf(card); })[0];
          grid.insertBefore(card, next ? next.spot : endOf(own));
          if (marks.has(own.spot)) putIn(own.spot);
          return;
        }
        var later = all.filter(function (g) { return dates.indexOf(g.id) > dates.indexOf(date); })[0];
        grid.insertBefore(card, later ? later.spot : all.length ? endOf(all[all.length - 1]) : grid.firstChild);
        card.before(newLine());
      }, null);
      if (loader.next() === null) ending();
    }

    /* Make *change*, which returns the lines it takes out, the cards and lines left travelling to their places. */
    function closeUp(change, leaving) {
      var gone = new Map();
      var before = new Map();
      if (held || core.reduceMotion() || !grid.animate) { change(); return; }
      if (leaving) gone.set(keyOf(leaving), leaving);
      onScreen().forEach(function (card) { before.set(keyOf(card), card.getBoundingClientRect()); });
      linesOnScreen().forEach(function (line) { before.set(line, line.getBoundingClientRect()); });
      (change() || []).forEach(function (line) { gone.set(line, line); });
      travel(before, gone);
    }

    function busy(on) {
      if (on) host.setAttribute('aria-busy', 'true');
      else host.removeAttribute('aria-busy');
    }

    /* ── Dated lines ── */

    function groups() { return toArray(grid.querySelectorAll('.grid-group')); }

    function idOf(line) { return line.getAttribute('data-group'); }

    /* Of *ids*, in the order of their lines, those of a line that is there or may come yet. */
    function toCome(ids) {
      var there = groups().map(idOf);
      var gone = Array.from(marks.values()).filter(isLine).map(idOf);
      var last = ids.reduce(function (at, id, i) { return there.indexOf(id) < 0 ? at : i; }, -1);
      return ids.filter(function (id, i) {
        return there.indexOf(id) >= 0 || (i > last && loader.next() !== null && gone.indexOf(id) < 0);
      });
    }

    /* Scroll to the line *id*, loading until it is there; *done*: there is no more to load. */
    function jump(id, done) {
      var line = groups().filter(function (g) { return idOf(g) === id; })[0];
      wanted = line || done ? null : id;
      wantedAt = Date.now();
      if (line) {
        window.scrollBy({ top: line.getBoundingClientRect().top - clearTop() - 4, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
      } else if (!done) {
        loader.more();
      }
    }

    /* The line the screen stands under. */
    function groupNow() {
      var all = groups();
      var edge = clearTop() + 60;
      var under = all.filter(function (g) { return g.getBoundingClientRect().top < edge; });
      var line = under[under.length - 1] || all[0];
      return line ? idOf(line) : null;
    }

    /* ── Keyboard ── */

    /* The card above or below *card*, nearest its column; only its neighbours are measured. */
    function beside(all, i, down) {
      var from = all[i].getBoundingClientRect();
      var best = null;
      var line = null;
      for (var n = i + (down ? 1 : -1); all[n]; n += down ? 1 : -1) {
        var r = all[n].getBoundingClientRect();
        if (Math.abs(r.top - from.top) < 10) continue;
        if (line !== null && Math.abs(r.top - line) > 10) break;
        line = r.top;
        var off = Math.abs(r.left - from.left);
        if (!best || off < best.off) best = { card: all[n], off: off };
      }
      return best ? best.card : null;
    }

    function focusCard(card) {
      linkOf(card).focus({ preventScroll: true });
      var r = card.getBoundingClientRect();
      var high = clearTop() + 10;
      var low = core.clearArea().bottom - 10;
      var by = r.top < high ? r.top - high : r.bottom > low ? Math.min(r.bottom - low, r.top - high) : 0;
      if (by) window.scrollBy({ top: by, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
    }

    grid.addEventListener('keydown', function (e) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      var link = e.target.closest && e.target.closest('.watch-card__link');
      if (!link) return;
      var all = cards();
      var i = all.indexOf(link.closest('.watch-card'));
      var to = e.key === 'ArrowRight' ? all[i + 1] : e.key === 'ArrowLeft' ? all[i - 1]
        : e.key === 'ArrowDown' || e.key === 'ArrowUp' ? beside(all, i, e.key === 'ArrowDown') : undefined;
      if (to === undefined && !/^Arrow/.test(e.key)) return;
      e.preventDefault();
      if (to) focusCard(to);
    });

    /* Focus the first card that can be seen whole from its top. */
    function enter() {
      var edge = clearTop() - 10;
      var first = onScreen().filter(function (card) { return card.getBoundingClientRect().top >= edge; })[0];
      if (first) focusCard(first);
      return !!first;
    }

    /* ── Scrolling and resizing ── */

    function anchor() {
      lead = null;
      var edge = clearTop();
      if (held || host.getBoundingClientRect().top >= edge - 2) return;
      var card = onScreen().filter(function (c) { return c.getBoundingClientRect().top >= edge - 40; })[0];
      if (card) lead = { card: card, top: card.getBoundingClientRect().top };
    }

    var queued = false;
    function onScroll() {
      loader.check();
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () { queued = false; anchor(); });
    }

    /* A phone's address bar changes the height alone, which moves no card. */
    function onResize() {
      if (window.innerWidth !== width) {
        width = window.innerWidth;
        if (lead && document.body.contains(lead.card)) {
          window.scrollBy({ top: lead.card.getBoundingClientRect().top - lead.top, behavior: 'instant' });
        }
      }
      loader.check();
    }

    /* A jump still on its way gives in to the person's own scroll. */
    function onTake() { wanted = null; }
    function onKey(e) { if (SCROLL_KEYS.test(e.key)) wanted = null; }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('wheel', onTake, { passive: true });
    window.addEventListener('touchmove', onTake, { passive: true });
    window.addEventListener('keydown', onKey);
    window.addEventListener('resize', onResize);

    (opts.cards || []).forEach(function (node) { grid.appendChild(node); });
    begin(opts.next);

    return {
      node: grid,
      tail: tail,
      cards: cards,
      count: function () { return cards().length; },
      onScreen: onScreen,
      set: set,
      take: take,
      put: put,
      busy: busy,
      jump: function (id) { jump(id, false); },
      groupNow: groupNow,
      toCome: toCome,
      next: function () { return loader.next(); },
      enter: enter,
      hold: function (on) {
        held = on;
        if (!on) loader.check();
      },
      stop: function () {
        loader.stop();
        window.removeEventListener('scroll', onScroll);
        window.removeEventListener('wheel', onTake);
        window.removeEventListener('touchmove', onTake);
        window.removeEventListener('keydown', onKey);
        window.removeEventListener('resize', onResize);
      }
    };
  }

  /* ── The layer a row grows into ── */

  var page = document.getElementById('watch-page');
  var browse = document.getElementById('watch-browse');
  if (!page || !browse) {
    watch.grid = { list: list, copy: copy, group: group };
    return;
  }

  var BACK_MS = 360;
  var OUT_MS = 200;
  var IN_MS = 260;
  var NONE = 'translate(0px, 0px) scale(1)';
  var BLOCKS = '.watch-banner, .watch-notice, .watch-group-label, .watch-row, .watch-empty, [data-grid-block]';

  var html = document.documentElement;
  var dom = null; // the layer's own nodes
  var now = null; // what it holds: { opts, list, value }
  var move = null; // what moves: { to, gridY, tracks, token }
  var place = null; // the rows as they were: { y, lefts, focus }
  var lastY = 0;
  var saidAt = 0; // when the grid last said what it is

  function build() {
    var all = document.getElementById('watch-all') || el('section', 'watch-all');
    var fresh = !all.id;
    if (fresh) {
      all.id = 'watch-all';
      all.setAttribute('aria-labelledby', 'watch-all-title');
      all.hidden = true;
      var head = el('div', 'watch-all__head');
      head.appendChild(el('div', 'watch-all__glass'));
      var back = el('a', 'watch-all__back');
      back.setAttribute('href', '/watch');
      back.setAttribute('aria-label', 'Back to all rows');
      back.appendChild(icon('i-chevron-left'));
      var names = el('div', 'watch-all__names');
      var h1 = el('h1', 'watch-all__title');
      h1.id = 'watch-all-title';
      h1.setAttribute('tabindex', '-1');
      names.appendChild(h1);
      names.appendChild(el('span', 'watch-all__count'));
      head.appendChild(back);
      head.appendChild(names);
      head.appendChild(el('div', 'watch-all__tools'));
      all.appendChild(head);
      all.appendChild(el('div', 'watch-all__body'));
      browse.parentNode.insertBefore(all, browse.nextSibling);
    }
    var part = function (name) { return all.querySelector('.watch-all__' + name); };
    dom = {
      all: all, head: part('head'), back: part('back'), title: part('title'),
      count: part('count'), tools: part('tools'), body: part('body'),
      status: page.appendChild(el('p', 'sr-only')),
      menu: page.appendChild(el('div', 'watch-keep-menu rec-menu watch-all__menu'))
    };
    dom.status.setAttribute('role', 'status');
    dom.menu.id = 'watch-all-menu';
    dom.menu.setAttribute('role', 'menu');
    dom.menu.hidden = true;
    dom.sortMenu = watch.keep.menu(dom.menu, {
      trigger: '#watch-all-sort',
      fill: fillMenu,
      choose: function (trigger, item) { choose(item.getAttribute('data-value')); },
      spare: function () { return dom.title; }
    });
    all.addEventListener('click', function (e) {
      var link = e.target.closest && e.target.closest('.watch-all__back, [data-grid-back]');
      if (!link || !plainClick(e) || !now) return;
      e.preventDefault();
      now.opts.back();
    });
    dom.title.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' && now && now.list.enter()) e.preventDefault();
    });
  }

  function say(text) { dom.status.textContent = text; }

  function rowOf(opts) {
    var row = opts.row && opts.row();
    return row && document.body.contains(row) ? row : null;
  }

  function headBottom() { return core.clearArea().top + dom.head.offsetHeight; }

  /* ── The header ── */

  /* A date with nothing under it any more is not there to jump to. */
  function options() {
    var sort = now.opts.sort;
    if (!sort.jump) return sort.options;
    var left = now.list.toCome(sort.options.map(function (o) { return o.id; }));
    return sort.options.filter(function (o) { return left.indexOf(o.id) >= 0; });
  }

  function optionNow() {
    return now.opts.sort.options.filter(function (o) { return o.id === now.value; })[0] || now.opts.sort.options[0];
  }

  var SVG = 'http://www.w3.org/2000/svg';

  /* Two arrows, up and down; the sprite has none. */
  function sortIcon() {
    var svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'ico watch-all__sort-icon');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', 'M7 4v16M3 16l4 4 4-4M17 20V4M13 8l4-4 4 4');
    svg.appendChild(path);
    return svg;
  }

  function drawHead() {
    var o = now.opts;
    dom.title.textContent = o.title;
    setTotal(o.total);
    dom.tools.replaceChildren();
    if (!o.sort || o.total === 0) return;
    var button = el('button', 'row-tool');
    button.type = 'button';
    button.id = 'watch-all-sort';
    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', 'watch-all-menu');
    button.appendChild(o.sort.jump ? icon('i-clock-regular') : sortIcon());
    button.appendChild(el('span', 'watch-all__value'));
    button.appendChild(icon('i-chevron-down', 'row-tool__chev'));
    dom.tools.appendChild(button);
    labelSort();
  }

  /* What to choose from, once it is known. */
  function setSort(sort) {
    now.opts.sort = sort;
    now.value = sort.value;
    drawHead();
  }

  function labelSort() {
    var button = dom.tools.querySelector('#watch-all-sort');
    if (!button) return;
    button.querySelector('.watch-all__value').textContent = optionNow().short || optionNow().label;
    button.setAttribute('aria-label', now.opts.sort.label + ': ' + optionNow().label);
    fitSort();
  }

  /* With nothing to choose from there is no button; one that had the focus, or its menu, hands it on. */
  function fitSort() {
    var button = dom.tools.querySelector('#watch-all-sort');
    if (!button) return;
    var none = !options().length;
    if (none && !button.hidden) {
      dom.sortMenu.close(false);
      if (document.activeElement === button) dom.title.focus({ preventScroll: true });
    }
    button.hidden = none;
  }

  function setTotal(n) {
    dom.count.textContent = typeof n === 'number' && n > 0 ? plural(n) : '';
  }

  function fillMenu() {
    dom.menu.setAttribute('aria-label', now.opts.sort.label);
    dom.menu.replaceChildren();
    options().forEach(function (o) {
      var item = el('button', 'watch-keep-menu__item');
      item.type = 'button';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('tabindex', '-1');
      item.setAttribute('aria-checked', o.id === optionNow().id ? 'true' : 'false');
      item.setAttribute('data-value', o.id);
      item.appendChild(icon('i-check'));
      item.appendChild(document.createTextNode(o.label));
      dom.menu.appendChild(item);
    });
  }

  function choose(id) {
    var mine = now;
    if (!mine || move) return;
    var sort = mine.opts.sort;
    if (sort.jump) {
      mine.value = id;
      mine.jumped = true;
      labelSort();
      mine.list.jump(id);
      return;
    }
    if (id === mine.value) return;
    if (!mine.asked) mine.shown = mine.value;
    var asked = mine.asked = {};
    mine.value = id;
    labelSort();
    mine.list.busy(true);
    function undo() {
      mine.value = mine.shown;
      labelSort();
    }
    Promise.resolve(sort.choose(id)).then(function (content) {
      if (now !== mine || mine.asked !== asked) return;
      mine.asked = null;
      mine.list.busy(false);
      // Left meanwhile: the grid on its way out stays as it is.
      if (move) { undo(); return; }
      if (content.total !== undefined) setTotal(content.total);
      mine.fills++;
      mine.list.set(content, { move: true });
      stuck();
      if (sort.chosen) sort.chosen(id);
      say(mine.opts.title + ', ' + sort.label + ': ' + optionNow().label + '.');
    }, function () {
      if (now !== mine || mine.asked !== asked) return;
      mine.asked = null;
      mine.list.busy(false);
      undo();
      core.toast('Couldn’t change the order. Try again in a moment.', { variant: 'error' });
    });
  }

  /* Take *card* out of the open grid, as out of its row (watch/keep.js). Null: it is not in
     there. *control*: the button that was pressed, as the other cards have it. */
  function take(card, control) {
    var mine = now;
    if (!mine || !mine.list.node.contains(card)) return null;
    var all = mine.list.cards();
    var i = all.indexOf(card);
    var next = all[i + 1] || all[i - 1];
    var fills = mine.fills;
    function count(by) {
      if (now !== mine || typeof mine.opts.total !== 'number') return;
      mine.opts.total += by;
      setTotal(mine.opts.total);
    }
    var back = mine.list.take(card);
    count(-1);
    mine.jumped = false;
    nameLine();
    fitSort();
    return {
      focus: next ? next.querySelector(control) || linkOf(next) : dom.title,
      // Filled anew since, there is no place to go back to: bring() fills it again.
      undo: function () {
        if (now !== mine || mine.fills !== fills) return null;
        back();
        count(1);
        nameLine();
        fitSort();
        return card.querySelector(control);
      }
    };
  }

  /* *card* is back in its row, whose grid may be open without it. Returns its card
     in there when the grid was filled again for it, or has it put in. */
  function bring(card) {
    var row = now && (now.opts.contents || now.opts.line) && rowOf(now.opts);
    if (!row || !row.contains(card)) return null;
    function twin() { return now.list.cards().filter(function (c) { return keyOf(c) === keyOf(card); })[0] || null; }
    if (twin()) return null;
    settle();
    if (!now) return null; // it was closing
    if (now.opts.line) {
      putIn(card);
      return twin();
    }
    var content = now.opts.contents(now.value);
    var none = !now.opts.total;
    now.opts.total = content.total;
    if (none) drawHead(); else setTotal(content.total);
    now.fills++;
    now.list.set(content, { move: true, stay: true });
    return twin();
  }

  /* Put a copy of *card* under its date in the open grid, unless the page of that date is still to come. */
  function putIn(card) {
    var dates = now.opts.dates();
    var next = now.list.next();
    if (next !== null && dates.indexOf(card.getAttribute('data-batch')) >= dates.indexOf(next)) return;
    now.list.put(copy(card), dates, function () { return now.opts.line(card); });
    nameLine();
    fitSort();
  }

  /* ── The two layers ── */

  function pin(layer, top) {
    layer.classList.add('is-pinned');
    layer.style.top = top + 'px';
    layer.setAttribute('inert', '');
  }

  /* Fix *layer* where its content stands: in the flow, the margin of its first
     child is outside it, and fixed inside. */
  function pinInPlace(layer) {
    var first = toArray(layer.children).filter(function (n) { return n.getClientRects().length; })[0] || layer;
    var was = first.getBoundingClientRect().top;
    var top = layer.getBoundingClientRect().top;
    pin(layer, top);
    layer.style.top = top + was - first.getBoundingClientRect().top + 'px';
  }

  function unpin(layer) {
    layer.classList.remove('is-pinned');
    layer.style.top = '';
    layer.removeAttribute('inert');
  }

  function unhead() {
    dom.head.style.position = '';
    dom.head.style.top = '';
  }

  /* Put the layer *to* in the page's flow at the scroll *y* says, the other fixed
     where it stands. */
  function swapLayers(to, y) {
    // Too short for its scroll, even for a moment, Safari places what is fixed by the scroll it had.
    page.style.minHeight = page.offsetHeight + 'px';
    var from = to === 'grid' ? browse : dom.all;
    var headAt = to === 'rows' ? dom.head.getBoundingClientRect().top : 0;
    pinInPlace(from);
    unpin(to === 'grid' ? dom.all : browse);
    browse.hidden = false;
    dom.all.hidden = false;
    unhead();
    scrollPage(y());
    page.style.minHeight = '';
    lastY = window.scrollY;
    if (to === 'rows') {
      // Fixed, the layer no longer holds its header under the top bar.
      dom.head.classList.remove('is-stuck');
      dom.head.style.position = 'relative';
      dom.head.style.top = '0px';
      dom.head.style.top = headAt - dom.head.getBoundingClientRect().top + 'px';
    }
  }

  /* ── Where the rows were ── */

  function scrollers() { return toArray(browse.querySelectorAll('.watch-row__scroll')); }

  function savePlace() {
    var active = document.activeElement;
    place = {
      y: window.scrollY,
      lefts: scrollers().map(function (s) { return { node: s, left: s.scrollLeft }; }),
      focus: active && browse.contains(active) ? active : null
    };
  }

  /* The entry Back or Forward leads to, when where it was scrolled is known. */
  function arriving() {
    var to = watch.address && watch.address.arriving();
    return to && to.y !== undefined ? to : null;
  }

  /* Back to it, with the rows laid out. From a link there is none: the row in the middle of the screen. */
  function restoreLefts() {
    (place ? place.lefts : []).forEach(function (l) {
      if (!document.body.contains(l.node)) return;
      l.node.style.scrollSnapType = 'none';
      l.node.scrollLeft = l.left;
      setTimeout(function () { l.node.style.scrollSnapType = ''; }, 120);
    });
  }

  function placeFromLink(opts) {
    var back = arriving();
    if (back) {
      return { y: back.y, focus: null, lefts: scrollers().map(function (s) {
        var section = s.closest('[data-row-id]');
        return { node: s, left: (section && back.rows[section.getAttribute('data-row-id')]) || 0 };
      }) };
    }
    var row = rowOf(opts);
    if (!row) return { y: 0, lefts: [], focus: null };
    var keep = window.scrollY;
    dom.all.style.display = 'none';
    unpin(browse);
    scrollPage(0);
    var r = row.getBoundingClientRect();
    var y = Math.max(0, r.top - (window.innerHeight - r.height) / 2);
    dom.all.style.display = '';
    scrollPage(keep);
    return { y: y, lefts: [], focus: null };
  }

  /* ── Tracks ── */

  /* Where an element's words stand, whatever its box. */
  function wordsRect(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    return range.getBoundingClientRect();
  }

  function shownNow(node) {
    return node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
  }

  /* Everything that moves, with its two ends. Both layers are laid out. */
  function measure(opts) {
    var out = [];
    var row = rowOf(opts);
    var vh = window.innerHeight;
    var vw = html.clientWidth;
    var travel = core.tokenMs('--dur-move', 420);
    var flown = new Set();
    var byKey = new Map();
    function add(track) { out.push(track); }
    function fade(node, rows, grid, extra) {
      add(Object.assign({ node: node, rows: rows, grid: grid, open: OUT_MS, close: IN_MS, openAfter: 0, closeAfter: 0 }, extra));
    }

    var onGrid = now.list.onScreen();
    onGrid.forEach(function (card) { byKey.set(keyOf(card), card); });

    var scroller = row && row.querySelector('.watch-row__scroll');
    var inRow = scroller ? toArray(scroller.children) : [];
    var pad = scroller ? parseFloat(getComputedStyle(scroller).paddingLeft) || 0 : 0;
    var rects = new Map();
    inRow.forEach(function (node) { rects.set(node, node.getBoundingClientRect()); });
    var i = 0;
    inRow.forEach(function (node) {
      var r = rects.get(node);
      var twin = isCard(node) && !(r.right <= 4 || r.left >= vw - 4 || r.bottom <= 0 || r.top >= vh) && byKey.get(keyOf(node));
      if (!twin || flown.has(twin)) return;
      flown.add(twin);
      flown.add(node);
      var g = twin.getBoundingClientRect();
      var size = r.width / g.width;
      var edge = r.left < pad - 6 || r.right > vw - pad + 6;
      drawNow(twin);
      add({
        node: twin, partner: node, under: edge, origin: '0 0',
        rows: { transform: 'translate(' + (r.left - g.left) + 'px, ' + (r.top - g.top) + 'px) scale(' + size + ')', opacity: 1 },
        grid: { transform: NONE, opacity: 1 },
        open: travel, close: BACK_MS, openAfter: Math.min(30, i++ * 3), closeAfter: 0
      });
      var link = linkOf(twin);
      if (edge) {
        fade(link, { transform: NONE, opacity: 0.5 }, { transform: NONE, opacity: 1 }, { open: 120, close: 150, closeAfter: BACK_MS - 150 });
      }
      if (Math.abs(size - 1) <= 0.02) return;
      // The row's own words ride along and fade, so the type never changes size in a jump.
      var ride = linkOf(node).cloneNode(true);
      ['href', 'data-title-ref', 'data-title'].forEach(function (name) { ride.removeAttribute(name); });
      ride.classList.add('watch-ride');
      ride.setAttribute('inert', '');
      ride.setAttribute('aria-hidden', 'true');
      ride.style.width = r.width + 'px';
      drawNow(ride);
      twin.appendChild(ride);
      var upright = 'scale(' + 1 / size + ')';
      add({
        node: ride, temp: true, origin: '0 0',
        rows: { transform: upright, opacity: edge ? 0.5 : 1 }, grid: { transform: upright, opacity: 0 },
        open: 200, close: 200, openAfter: 30, closeAfter: BACK_MS - 200
      });
      var sub = link.querySelector('.watch-card__sub');
      if (sub) fade(sub, { transform: NONE, opacity: 0 }, { transform: NONE, opacity: 1 }, { openAfter: 30, close: 200, closeAfter: BACK_MS - 200 });
    });

    var first = onGrid.length ? onGrid[0].getBoundingClientRect() : null;
    onGrid.forEach(function (card) {
      if (flown.has(card)) return;
      var r = card.getBoundingClientRect();
      var line = (r.top - first.top) / Math.max(1, r.height);
      var column = (r.left - first.left) / Math.max(1, r.width);
      add({
        node: card, rows: { transform: 'translate(0px, 26px) scale(.96)', opacity: 0 }, grid: { transform: NONE, opacity: 1 },
        open: IN_MS, close: OUT_MS, openAfter: Math.max(0, Math.min(190, 40 + line * 28 + column * 7)), closeAfter: 0
      });
    });

    function side(node, x, y, after) {
      if (!node) return;
      add({
        node: node, rows: { transform: 'translate(' + x + 'px, ' + y + 'px)', opacity: 0 }, grid: { transform: NONE, opacity: 1 },
        open: IN_MS, close: OUT_MS, openAfter: after, closeAfter: 0
      });
    }
    toArray(now.list.node.querySelectorAll('.grid-group, .watch-card--skel')).forEach(function (n) {
      var r = n.getBoundingClientRect();
      if (r.top < vh && r.bottom > 0) side(n, 0, 0, 120);
    });
    side(dom.back, 10, 0, 100);
    side(dom.count, -8, 0, 140);
    side(dom.tools.firstElementChild, 10, 0, 140);
    side(now.list.tail.firstElementChild, 0, 12, 140);

    heading(row, add, travel);

    var mid = row ? row.getBoundingClientRect().top : vh / 2;
    toArray(browse.querySelectorAll(BLOCKS)).forEach(function (block) {
      var r = block.getBoundingClientRect();
      if (block === row || block.contains(row) || !r.height || r.bottom < -40 || r.top > vh + 40) return;
      fade(block, { transform: NONE, opacity: 1 },
        { transform: 'translate(0px, ' + (r.top < mid ? -28 : 36) + 'px) scale(1)', opacity: 0 }, { closeAfter: 40 });
    });
    if (!row) return out;
    toArray(row.querySelectorAll('.watch-row__head > :not(.watch-row__title), .rec-line, [data-grid-fade]')).forEach(function (node) {
      if (shownNow(node)) fade(node, { transform: NONE, opacity: 1 }, { transform: NONE, opacity: 0 }, { open: 150, closeAfter: 120 });
    });
    toArray(row.querySelectorAll('.watch-row__arrow')).forEach(function (node) {
      if (shownNow(node)) fade(node, { transform: NONE, opacity: 1 }, { transform: 'scale(.9)', opacity: 0 }, { open: 150, close: 200, closeAfter: 180 });
    });
    inRow.forEach(function (node) {
      var r = rects.get(node);
      if (flown.has(node) || r.right < 0 || r.left > vw) return;
      fade(node, { transform: NONE, opacity: 1 }, { transform: 'scale(.96)', opacity: 0 }, { closeAfter: 100 });
    });
    return out;
  }

  /* The row's heading becomes the header's title. */
  function heading(row, add, travel) {
    var from = row && (row.querySelector('[data-row-title]') || row.querySelector('.watch-row__title'));
    if (!from) return;
    var a = wordsRect(from);
    if (a.bottom <= 0 || a.top >= window.innerHeight) return;
    var b = wordsRect(dom.title);
    var box = dom.title.getBoundingClientRect();
    var size = parseFloat(getComputedStyle(from).fontSize) / parseFloat(getComputedStyle(dom.title).fontSize);
    add({
      node: dom.title, partner: from, origin: '0 50%',
      rows: {
        transform: 'translate(' + (a.left - box.left - (b.left - box.left) * size) + 'px, ' +
          (a.top + a.height / 2 - box.top - box.height / 2) + 'px) scale(' + size + ')',
        opacity: 1
      },
      grid: { transform: NONE, opacity: 1 },
      open: travel, close: BACK_MS, openAfter: 0, closeAfter: 0
    });
  }

  /* ── Playing them ── */

  function play(to) {
    var turned = move.tracks.some(function (t) { return t.anim; });
    var token = {};
    move.to = to;
    move.token = token;
    html.classList.add('is-grid-moving');
    now.list.hold(true);
    // All read before any is written, so one layout serves them all.
    move.tracks.forEach(function (t) {
      t.from = t[to === 'grid' ? 'rows' : 'grid'];
      if (!t.anim) return;
      var style = getComputedStyle(t.node);
      t.from = { transform: style.transform === 'none' ? NONE : style.transform, opacity: style.opacity };
    });
    var left = move.tracks.length;
    function ended() {
      left--;
      if (left === 0 && move && move.token === token) settle();
    }
    move.tracks.forEach(function (t) {
      if (t.anim) {
        t.anim.onfinish = null;
        t.anim.cancel();
      }
      if (t.origin) t.node.style.transformOrigin = t.origin;
      t.node.classList.add('is-track');
      if (t.partner) {
        t.partner.classList.add('is-flown');
        t.node.classList.add('is-flyer');
        if (t.under) t.node.classList.add('is-under');
      }
      var ms = to === 'grid' ? t.open : t.close;
      t.anim = animate(t.node, [t.from, t[to]], {
        duration: turned ? Math.max(200, Math.min(ms, 320)) : ms,
        delay: turned ? 0 : to === 'grid' ? t.openAfter : t.closeAfter,
        fill: 'both'
      });
      t.anim.onfinish = ended;
    });
    if (!left) settle();
  }

  function settle() {
    if (!move) return;
    var to = move.to;
    move.tracks.forEach(function (t) {
      if (t.anim) {
        t.anim.onfinish = null;
        t.anim.cancel();
      }
      if (t.origin) t.node.style.transformOrigin = '';
      t.node.classList.remove('is-track', 'is-flyer', 'is-under');
      if (t.partner) t.partner.classList.remove('is-flown');
      if (t.temp) t.node.remove();
    });
    move = null;
    html.classList.remove('is-grid-moving');
    unhead();
    unpin(browse);
    unpin(dom.all);
    if (to === 'grid') {
      browse.hidden = true;
      stuck();
      now.list.hold(false);
    } else {
      empty();
    }
  }

  function empty() {
    dom.all.hidden = true;
    if (now) now.list.stop();
    now = null;
    dom.sortMenu.close(false);
    dom.body.replaceChildren();
  }

  /* ── Opening and closing ── */

  /* From the middle of a row, the first card that was whole on the screen. */
  function firstSeen(row) {
    var scroller = row && row.querySelector('.watch-row__scroll');
    if (!scroller || !scroller.clientWidth) return null;
    var pad = parseFloat(getComputedStyle(scroller).paddingLeft) || 0;
    var cards = toArray(scroller.querySelectorAll('.watch-card'));
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].getBoundingClientRect().left >= pad - 20) return i ? keyOf(cards[i]) : null;
    }
    return null;
  }

  /* Scroll on to the line that holds the card *key*. */
  function aim(key) {
    if (key === null) return;
    var cards = now.list.cards();
    var card = cards.filter(function (c) { return keyOf(c) === key; })[0];
    if (!card || card.getBoundingClientRect().top - cards[0].getBoundingClientRect().top < 10) return;
    scrollPage(Math.max(window.scrollY, window.scrollY + card.getBoundingClientRect().top - headBottom() - 8));
  }

  /* The scroll at which the header just comes to rest under the top bar. */
  function headScroll() {
    return dom.all.getBoundingClientRect().top + window.scrollY - core.clearArea().top;
  }

  function fill(opts) {
    if (now) {
      now.list.stop();
      dom.body.replaceChildren();
    }
    var cards = opts.cards;
    var drawn = dom.body.querySelector('.watch-grid');
    if (!cards && !drawn) {
      var row = rowOf(opts);
      cards = row ? toArray(row.querySelectorAll('.watch-row__scroll .watch-card')).map(copy) : [];
    }
    // The server draws the first page of a shared link; anything later replaces it.
    if (cards) dom.body.replaceChildren();
    now = { opts: opts, value: opts.sort ? opts.sort.value : null, list: null, asked: null, shown: null, jumped: false, fills: 0 };
    now.list = list(dom.body, {
      title: opts.title, cards: cards, next: opts.next, fetchPage: opts.fetchPage,
      total: opts.total, empty: opts.empty || { title: 'Nothing in ' + opts.title + ' yet', text: 'What you add shows up here.', link: 'Back to all rows', href: '/watch' },
      clearTop: headBottom,
      say: function (text) { if (Date.now() - saidAt > HEARD_MS) say(text); },
      took: fitSort
    });
    drawHead();
  }

  function announce() {
    dom.title.focus({ preventScroll: true });
    var o = now.opts;
    saidAt = Date.now();
    say(o.title + (typeof o.total === 'number' ? ', ' + plural(o.total) : '') + ', as a grid. Press Escape to go back.');
  }

  /* The open grid is asked for in another order. A sort that jumps has none. */
  function resorted(opts) {
    return !!now.opts.sort && !!opts.sort && !opts.sort.jump && opts.sort.value !== now.value;
  }

  /* Says whether the page stands where it should: scrolled on into before, the
     grid grows back to there first. */
  function open(opts) {
    if (!dom) build();
    if (watch.preview && watch.preview.close) watch.preview.close();
    dom.sortMenu.close(false);
    if (move && now && now.opts.id === opts.id && !resorted(opts)) {
      if (move.to === 'grid') return true;
      swapLayers('grid', function () { return move.gridY; });
      play('grid');
      announce();
      return true;
    }
    settle();
    if (now && now.opts.id === opts.id) {
      if (resorted(opts)) choose(opts.sort.value);
      return true;
    }
    var fromRows = !browse.hidden && !now;
    if (fromRows && !opts.instant) savePlace();
    else if (!now && !opts.again) place = null;
    var back = opts.instant ? null : arriving();
    var deep = !!back && back.y > window.innerHeight / 2;
    var first = fromRows && !opts.instant && !back ? firstSeen(rowOf(opts)) : null;
    var y = window.scrollY;
    fill(opts);
    if (!fromRows || opts.instant || deep || core.reduceMotion() || !dom.all.animate) {
      browse.hidden = true;
      dom.all.hidden = false;
      scrollPage(opts.instant ? y : Math.min(y, headScroll()));
      aim(first);
      announce();
      stuck();
      return !back;
    }
    now.list.hold(true);
    swapLayers('grid', function () { return back ? back.y : Math.min(y, headScroll()); });
    aim(first);
    lastY = window.scrollY;
    dom.head.classList.remove('is-stuck');
    move = { to: 'grid', gridY: window.scrollY, tracks: null, token: null };
    move.tracks = measure(opts);
    play('grid');
    announce();
    return true;
  }

  function close(how) {
    if (!now) return;
    dom.sortMenu.close(false);
    if (move) {
      if (move.to === 'rows') return;
      move.gridY = window.scrollY;
      restoreLefts();
      swapLayers('rows', function () { return place.y; });
      play('rows');
      focusRows(now.opts, null);
      return;
    }
    var opts = now.opts;
    var active = document.activeElement;
    var held = active && now.list.node.contains(active) ? active.closest('.watch-card') : null;
    var key = held ? keyOf(held) : null;
    var y = window.scrollY;
    browse.hidden = false;
    if ((how && how.instant) || core.reduceMotion() || !dom.all.animate) {
      dom.all.hidden = true;
      if (!place) place = placeFromLink(opts);
      restoreLefts();
      scrollPage(place.y);
      empty();
      focusRows(opts, key);
      return;
    }
    pin(browse, 0); // laid out, and out of the way of the grid, while they are put back
    if (!place) place = placeFromLink(opts);
    restoreLefts();
    pin(browse, 0);
    scrollPage(y);
    swapLayers('rows', function () { return place.y; });
    move = { to: 'rows', gridY: y, tracks: null, token: null };
    move.tracks = measure(opts);
    play('rows');
    focusRows(opts, key);
  }

  /* The card the person was on, if the row shows it; else what had the focus
     when the grid opened; else the row's heading, or the search when the row is gone. */
  function focusRows(opts, key) {
    var row = rowOf(opts);
    say('Back to all rows.');
    var to = place && place.focus && document.body.contains(place.focus) ? place.focus : null;
    if (key !== null && row) {
      toArray(row.querySelectorAll('.watch-row__scroll .watch-card')).some(function (card) {
        if (keyOf(card) !== key) return false;
        var r = card.getBoundingClientRect();
        if (r.left >= 0 && r.right <= window.innerWidth) to = linkOf(card);
        return true;
      });
    }
    // Fewer cards than when it opened: a row that now shows whole has no link in its heading.
    if (row && watch.expand) watch.expand.row(row);
    if (to && (!shownNow(to) || to.matches('a:not([href])'))) to = null;
    if (!to && row) to = row.querySelector('.watch-row__title a[href]') || row.querySelector('.watch-row__scroll');
    if (!to) to = core.byId('watch-search');
    if (to) to.focus({ preventScroll: true });
  }

  /* Gone at once, the rows not shown: something else takes the page. Where the rows
     were is kept for a grid opened again (opts.again). */
  function leave() {
    if (!now) return;
    if (move) { move.to = 'rows'; settle(); } else empty();
    browse.hidden = true;
    scrollPage(0);
    lastY = window.scrollY;
  }

  /* ── Scroll, resize, Esc ── */

  function stuck() {
    if (!dom || move) return;
    dom.head.classList.toggle('is-stuck', !dom.all.hidden && dom.all.getBoundingClientRect().top < core.clearArea().top - 2);
  }

  window.addEventListener('scroll', function () {
    if (!now) return;
    var by = window.scrollY - lastY;
    lastY = window.scrollY;
    if (move) {
      // The layer on its way out goes with the page.
      if (move.to === 'rows' && by) dom.all.style.top = parseFloat(dom.all.style.top) - by + 'px';
      if (move.to === 'grid') move.gridY = window.scrollY;
      return;
    }
    stuck();
    nameLine();
  }, { passive: true });

  /* Name the date the screen stands under. */
  function nameLine() {
    if (!now.opts.sort || !now.opts.sort.jump || now.jumped) return;
    var id = now.list.groupNow();
    if (id !== null && id !== now.value) {
      now.value = id;
      labelSort();
    }
  }

  /* A batch jumped to stays named, even where the page is too short to bring it
     to the top, until the person scrolls on. */
  ['wheel', 'touchmove', 'keydown'].forEach(function (type) {
    window.addEventListener(type, function () { if (now) now.jumped = false; }, { passive: true });
  });

  var width = window.innerWidth;
  window.addEventListener('resize', function () {
    if (window.innerWidth === width) return;
    width = window.innerWidth;
    settle();
    stuck();
  });

  /* On window, so that a menu or a preview of the page's own has had its Esc first. */
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || e.defaultPrevented || !now || (move && move.to === 'rows')) return;
    var top = watch.address ? watch.address.top() : 'grid';
    if (top !== 'grid') return;
    e.preventDefault();
    now.opts.back();
  });

  watch.grid = {
    list: list,
    copy: copy,
    group: group,
    open: open,
    close: close,
    leave: leave,
    take: take,
    bring: bring,
    setTotal: function (n) { if (now) setTotal(n); },
    setSort: function (sort) { if (now) setSort(sort); },
    isOpen: function () { return !!now && !(move && move.to === 'rows'); },
    moving: function () { return !!move; }
  };
})();
