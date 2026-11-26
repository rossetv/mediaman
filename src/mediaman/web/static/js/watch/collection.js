/* Watch: the other films of a film's collection, above the cast in its detail, and the dialog
 * that asks for those that are missing, on MMWatch.collection. The row comes after the title
 * is on show. The films are asked for one after another, also once the dialog is closed.
 * Needs watch/core.js and watch/keep.js; uses watch/browse.js for the cards, watch-rows.js for the arrows and
 * watch/detail.js, which holds the overlay and calls into().
 */
(function () {
  'use strict';

  var watch = window.MMWatch;
  var core = watch && watch.core;
  var root = document.getElementById('ttl');
  if (!core || !root) return;

  var el = core.el;
  var toArray = core.toArray;
  var icon = window.MM.dom.icon;

  /* How long after a film was asked for Radarr may not list it yet. */
  var GRACE_MS = 30000;
  /* How long what is known of a row on show is not asked for again. */
  var FRESH_MS = 60000;
  /* How long after the dialog opened, or a run ended, the rest of a press can still come. */
  var DOUBLE_CLICK_MS = 600;
  var OUT = [['held', 'Already in the library'], ['requested', 'Already asked for'], ['unreleased', 'Not out yet'],
    ['undated', 'No release date yet'], ['unavailable', 'Can’t be checked right now'],
    ['not_set_up', 'Can’t be downloaded here']];

  /* A limit of a minute or of the day: the server does not say which. */
  var WHEN = 'try again in a minute, or tomorrow if the day’s limit is reached.';

  var known = {};   // by the ref of a title: its collection, { name, films }
  var without = {}; // the refs of the films known to be in none
  var lefts = {};   // by the ref of a title: where its row was left
  var asked = {};   // by the ref of a film: when it was asked for here
  var view = null;  // the row last drawn: { ref, coll, holder, node, cards, scroller, stale, at, gen, waits }
  var line = Promise.resolve(); // the films asked for, one after another

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function day(iso) {
    return new Date(iso + 'T12:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function isOf(film, ref) { return film.ref === ref || film.library_ref === ref; }

  function hereIn(coll, ref) {
    return coll.films.filter(function (f) { return isOf(f, ref); })[0] || coll.films.filter(function (f) { return f.here; })[0];
  }

  /* What is known of the collection of *ref*, also from another film of it. */
  function collOf(ref) {
    if (known[ref]) return known[ref];
    var key = Object.keys(known).filter(function (k) {
      return known[k].films.some(function (f) { return isOf(f, ref); });
    })[0];
    return key ? known[key] : null;
  }

  function missing(coll) { return coll.films.filter(function (f) { return f.can_request; }); }

  /* ── The row ── */

  /* Put the row of the film *ref* at the end of *body*: at once where its collection is
     known, else once it is, its place held meanwhile when it is *expected* to be in one.
     *kind*: what the title is. */
  function into(body, ref, kind, expected) {
    if (kind !== 'movie') return;
    var holder = el('div', 'ttl__collsec');
    holder.hidden = true;
    body.appendChild(holder);
    var same = view && view.ref === ref;
    if (!same) view = { ref: ref, coll: collOf(ref), holder: null, node: null, cards: [], scroller: null, stale: true, at: 0, gen: 0, waits: !!expected && !without[ref] };
    view.holder = holder;
    if (view.coll) show();
    else if (view.waits) holdPlace(holder);
    if (view.stale || Date.now() - view.at > FRESH_MS) ask(view);
  }

  function holdPlace(holder) {
    var skel = el('div', 'coll-skel');
    skel.setAttribute('aria-hidden', 'true');
    var head = el('div', 'coll__head');
    var words = el('div', 'coll__words');
    words.appendChild(el('span', 'skel skel--line coll-skel__title'));
    words.appendChild(el('span', 'skel skel--line coll-skel__name'));
    head.appendChild(words);
    skel.appendChild(head);
    var row = el('div', 'watch-row__scroll');
    for (var i = 0; i < 8; i++) {
      var card = el('div', 'watch-card--skel');
      card.appendChild(el('span', 'skel skel--poster'));
      card.appendChild(el('span', 'skel skel--line'));
      row.appendChild(card);
    }
    var viewport = el('div', 'watch-row__viewport');
    viewport.appendChild(row);
    skel.appendChild(viewport);
    holder.hidden = false;
    holder.appendChild(skel);
  }

  /* What holds the row's place while the film *ref* loads; null for one not *expected* or known to be in a collection. */
  function skeleton(ref, expected) {
    if (without[ref] || !(expected || collOf(ref))) return null;
    var holder = el('div', 'ttl__collsec');
    holdPlace(holder);
    return holder;
  }

  /* Ease *holder* from the height *from* it had, so what is under it does not jump. */
  function ease(holder, from) {
    if (!holder.animate || core.reduceMotion()) return;
    if (!from) {
      watch.keep.growIn(holder, false);
      return;
    }
    var to = holder.offsetHeight;
    if (to === from) return;
    var frame = function (h) { return { height: h + 'px', overflow: 'clip' }; };
    holder.animate([frame(from), frame(to)], { duration: core.tokenMs('--dur-slow', 250), easing: core.token('--ease-out', 'ease-out') });
  }

  function ask(v) {
    v.stale = false;
    v.at = Date.now();
    var gen = ++v.gen;
    window.MM.api.get('/api/watch/title/' + encodeURIComponent(v.ref) + '/collection').then(function (coll) {
      if (gen === v.gen) take(v, coll && Array.isArray(coll.films) && coll.films.length > 1 ? coll : null);
    }, function () {
      if (gen !== v.gen) return;
      v.stale = true; // what is known stays
      letGo(v);
    });
  }

  /* No films came to the place held for them. */
  function letGo(v) {
    if (v.waits && !v.coll && v.holder.isConnected) watch.keep.foldAway(v.holder);
    v.waits = false;
  }

  function take(v, coll) {
    if (coll) {
      coll.films = coll.films.filter(function (f) { return f && watch.browse.card(f); });
      coll.films.forEach(inGrace);
      known[v.ref] = coll;
      delete without[v.ref];
    } else {
      delete known[v.ref];
      without[v.ref] = true;
    }
    var was = v.coll;
    v.coll = coll;
    if (v !== view || !v.holder.isConnected) {
      v.node = null;
      v.waits = false;
      return;
    }
    if (!coll && v.waits) {
      letGo(v);
      return;
    }
    v.waits = false;
    if (!coll) {
      if (v.holder.contains(document.activeElement)) title().focus({ preventScroll: true });
      v.node = null;
      v.holder.textContent = '';
      v.holder.hidden = true;
      return;
    }
    if (v.node && was && coll.films.map(refOf).join() === was.films.map(refOf).join()) {
      redraw(v, was);
      return;
    }
    v.node = null;
    var height = v.holder.offsetHeight;
    v.holder.textContent = '';
    show();
    core.fadeIn(v.holder);
    ease(v.holder, height);
  }

  function refOf(film) { return film.ref; }

  function title() { return document.getElementById('ttl-title'); }

  /* Asked for a moment ago, and Radarr does not say so yet. */
  function inGrace(film) {
    if (film.download_state || film.in_library || !(Date.now() - asked[film.ref] < GRACE_MS)) return;
    changed(film, 'queued', null);
  }

  function changed(film, state, pct) {
    film.download_state = state;
    film.download_pct = pct;
    film.can_request = false;
    film.why_not = state === 'queued' || state === 'downloading' ? 'requested' : 'held';
  }

  /* The row of the view, in its place and scrolled to where it was. */
  function show() {
    var v = view;
    var fresh = !v.node;
    if (fresh) draw(v);
    v.holder.hidden = false;
    v.holder.appendChild(v.node);
    if (v.ref in lefts) v.scroller.scrollLeft = lefts[v.ref];
    else toHere(v);
    if (fresh && watch.rows) watch.rows.init(v.scroller.parentNode);
  }

  function draw(v) {
    var node = v.node = el('section', 'coll');
    node.setAttribute('aria-labelledby', 'coll-title');
    var head = el('div', 'coll__head');
    var words = el('div', 'coll__words');
    v.title = el('h3', 'ttl__sec-title', 'Collection');
    v.title.id = 'coll-title';
    v.title.tabIndex = -1;
    words.appendChild(v.title);
    v.name = el('p', 'coll__name');
    words.appendChild(v.name);
    head.appendChild(words);
    v.button = el('button', 'btn watch-btn--grey coll__get');
    v.button.type = 'button';
    v.button.setAttribute('data-ttl-act', 'collection');
    v.button.setAttribute('aria-haspopup', 'dialog');
    v.button.appendChild(icon('i-download'));
    v.button.appendChild(el('span', 'coll__get-full'));
    v.button.appendChild(el('span', 'coll__get-brief'));
    v.button.addEventListener('click', function () {
      v.button.focus({ preventScroll: true }); // Safari leaves a pressed button without it
      openDialog(v);
    });
    head.appendChild(v.button);
    node.appendChild(head);

    var viewport = el('div', 'watch-row__viewport');
    v.scroller = el('div', 'watch-row__scroll coll__row');
    v.scroller.setAttribute('role', 'list');
    v.scroller.setAttribute('aria-labelledby', 'coll-title');
    v.cards = v.coll.films.map(function (f) { return cardOf(v, f); });
    v.cards.forEach(function (c) { v.scroller.appendChild(c); });
    viewport.appendChild(v.scroller);
    node.appendChild(viewport);
    v.scroller.addEventListener('scroll', function () {
      if (v.scroller.isConnected) lefts[v.ref] = v.scroller.scrollLeft;
    }, { passive: true });
    v.scroller.addEventListener('keydown', function (e) { alongRow(v, e); });
    v.scroller.addEventListener('focusin', function (e) { stopAt(v, e.target.closest('.watch-card__link')); });
    stopAt(v, linkOf(cardHere(v)));
    label(v);
  }

  function cardOf(v, film) {
    var card = watch.browse.card(film);
    card.setAttribute('role', 'listitem');
    core.toArray(card.querySelectorAll('.card-tool')).forEach(function (b) { b.tabIndex = -1; });
    var link = linkOf(card);
    link.setAttribute('data-ttl-act', 'film-' + film.ref);
    link.tabIndex = -1;
    if (film === hereIn(v.coll, v.ref)) {
      card.classList.add('is-here');
      link.setAttribute('aria-current', 'true');
      link.appendChild(el('span', 'coll__here', 'This film'));
    }
    link.addEventListener('click', function (e) {
      if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      link.focus({ preventScroll: true }); // what Back finds the focus on
      if (card.classList.contains('is-here')) {
        title().focus({ preventScroll: true });
        root.scrollTo({ top: 0, behavior: core.reduceMotion() ? 'auto' : 'smooth' });
      } else {
        openFilm(card, film);
      }
    });
    return card;
  }

  function linkOf(card) { return card.querySelector('.watch-card__link'); }

  function cardHere(v) {
    return v.cards.filter(function (c) { return c.classList.contains('is-here'); })[0] || v.cards[0];
  }

  /* The film's page grows out of its card, as it does from a card of the page. */
  function openFilm(card, film) {
    var from = card.querySelector('.watch-card__poster').getBoundingClientRect();
    var sheet = root.querySelector('.ttl__sheet');
    watch.detail.open(film.library_ref || film.ref, { title: film.title, kind: film.kind });
    if (!sheet || !sheet.animate || core.reduceMotion()) return;
    var wide = window.matchMedia('(min-width: 700px)').matches;
    if (wide) {
      var to = sheet.getBoundingClientRect();
      sheet.style.transformOrigin = Math.round(from.left + from.width / 2 - to.left) + 'px ' +
        Math.round(from.top + from.height / 2 - to.top) + 'px';
    }
    sheet.animate([wide ? { opacity: 0, transform: 'translateY(16px) scale(.96)' } : { transform: 'translateY(100%)' }, {}],
      { duration: core.tokenMs('--dur-slow', 250), easing: core.token('--ease-out', 'ease-out') });
  }

  /* The film of the title in the middle of the row, which alone scrolls. */
  function toHere(v) { v.scroller.scrollLeft = middleFor(v, cardHere(v)); }

  /* Where the row has *card* in its middle; its start where that shows the whole card. */
  function middleFor(v, card) {
    var end = v.scroller.clientWidth - parseFloat(getComputedStyle(v.scroller).paddingRight);
    if (card.offsetLeft + card.offsetWidth <= end) return 0;
    return card.offsetLeft - (v.scroller.clientWidth - card.offsetWidth) / 2;
  }

  /* The row is one Tab stop, which the arrow keys move. */
  function stopAt(v, link) {
    if (!link) return;
    v.cards.forEach(function (c) { linkOf(c).tabIndex = linkOf(c) === link ? 0 : -1; });
  }

  function alongRow(v, e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    var i = v.cards.findIndex(function (c) { return c.contains(e.target); });
    if (i >= 0 && e.key === 'ArrowDown' && e.target === linkOf(v.cards[i])) {
      var tool = core.controlIn(v.cards[i], '.card-tool');
      if (tool && tool !== e.target) {
        e.preventDefault();
        tool.focus({ preventScroll: true });
      }
      return;
    }
    var to = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1
      : e.key === 'Home' ? 0 : e.key === 'End' ? v.cards.length - 1 : -2;
    if (i < 0 || to === -2) return;
    e.preventDefault();
    if (!v.cards[to]) return;
    linkOf(v.cards[to]).focus();
    v.scroller.scrollTo({ left: middleFor(v, v.cards[to]), behavior: core.reduceMotion() ? 'auto' : 'smooth' });
  }

  /* All that a card says of *film*. */
  function said(film) { return JSON.stringify(Object.assign({}, film, { here: null })); }

  /* The same films, some changed: those cards are put in their places. */
  function redraw(v, was) {
    v.coll.films.forEach(function (film, i) {
      var old = v.cards[i];
      if (said(film) === said(was.films[i])) return;
      var focused = old.contains(document.activeElement);
      var stop = linkOf(old).tabIndex;
      var card = v.cards[i] = cardOf(v, film);
      linkOf(card).tabIndex = stop;
      old.replaceWith(card);
      if (focused) linkOf(card).focus({ preventScroll: true });
    });
    label(v);
  }

  function label(v) {
    var films = v.coll.films;
    var held = films.filter(function (f) { return f.why_not === 'held'; }).length;
    v.name.textContent = v.coll.name + ' · ' + plural(films.length, 'film', 'films') +
      (held ? ' · ' + (held === films.length ? 'all' : held) + ' in the library' : '');
    var n = missing(v.coll).length;
    var busy = job && job.busy && job.coll.name === v.coll.name;
    var text = busy ? 'Adding ' + Math.min(job.count, job.at + 1) + ' of ' + job.count + '…'
      : n === 1 ? 'Download the missing film' : 'Download the missing ' + n;
    var gone = !n && !busy;
    if (gone && v.button === document.activeElement) v.title.focus({ preventScroll: true });
    v.button.hidden = gone;
    v.button.querySelector('.coll__get-full').textContent = text;
    v.button.querySelector('.coll__get-brief').textContent = busy ? text : 'Get ' + n + ' missing';
  }

  /* ── The dialog ── */

  var jobs = {};     // by the name of a collection: the last job of it
  var job = null;    // the one under way or shown: { coll, items: [{ film, on, state, note }], busy, count, at, run, text, endedAt }
  var dialog = null; // its parts, once built
  var openedAt = 0;

  function button(cls, text) {
    var b = el('button', cls, text);
    b.type = 'button';
    return b;
  }

  function build() {
    var d = { root: el('div', 'keep-dialog-overlay coll-dialog') };
    d.root.id = 'coll-dialog';
    d.root.setAttribute('role', 'dialog');
    d.root.setAttribute('aria-modal', 'true');
    d.root.setAttribute('aria-labelledby', 'coll-dialog-title');
    d.root.setAttribute('aria-describedby', 'coll-dialog-lead');
    d.root.setAttribute('aria-hidden', 'true');
    var card = el('div', 'keep-dialog');
    d.title = el('h3');
    d.title.id = 'coll-dialog-title';
    d.title.tabIndex = -1;
    d.title.setAttribute('data-modal-focus', '');
    d.lead = el('p', 'coll-dialog__lead');
    d.lead.id = 'coll-dialog-lead';
    d.all = button('season-pick__all coll-dialog__all');
    d.all.addEventListener('click', function () {
      if (job.busy) return;
      var on = ticked(job).length < left(job).length;
      left(job).forEach(function (i) { i.on = on; });
      sync();
    });
    var under = el('div', 'coll-dialog__under');
    under.appendChild(d.lead);
    under.appendChild(d.all);
    d.body = el('div', 'coll-dialog__body');
    d.body.addEventListener('scroll', edges, { passive: true });
    window.addEventListener('resize', edges);
    d.list = el('ul', 'coll-list');
    d.out = el('div', 'coll-out');
    d.said = el('p', 'coll-dialog__said');
    d.said.setAttribute('role', 'status');
    var actions = el('div', 'keep-dialog-actions');
    d.cancel = button('btn btn--ghost', 'Cancel');
    d.go = button('btn btn--primary');
    actions.appendChild(d.cancel);
    actions.appendChild(d.go);
    d.body.appendChild(d.list);
    d.body.appendChild(d.out);
    [d.title, under, d.body, d.said, actions].forEach(function (n) { card.appendChild(n); });
    d.root.appendChild(card);
    // The rest of a double click on what opened it neither closes it nor answers it.
    d.root.addEventListener('click', function (e) {
      if (e.detail > 1 && Date.now() - openedAt < DOUBLE_CLICK_MS) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    document.body.appendChild(d.root);
    d.modal = window.MM.modal.setupDetail(d.root, {
      activeClass: 'active',
      manageBodyOverflow: false, // the title's page holds it
      onClose: function () {
        d.open = false;
        Promise.resolve().then(function () {
          // The button that opened it may have gone with the last film.
          if (!root.hidden && !root.matches('.is-leaving, .is-aside') && view && view.node && view.button.hidden && view.node.isConnected &&
              !(document.activeElement && root.contains(document.activeElement))) view.title.focus({ preventScroll: true });
        });
      }
    });
    d.cancel.addEventListener('click', function () { d.modal.close(); });
    // Held down, the key that pressed it does not press what it says next.
    d.go.addEventListener('keydown', function (e) { if (e.repeat && (e.key === 'Enter' || e.key === ' ')) e.preventDefault(); });
    d.go.addEventListener('click', function (e) {
      if (e.detail > 1 || d.go.getAttribute('aria-disabled') === 'true' || Date.now() - job.endedAt < DOUBLE_CLICK_MS) return;
      if (left(job).length) start(job);
      else d.modal.close();
    });
    return d;
  }

  /* Those of *j* still to ask for. */
  function left(j) { return j.items.filter(function (i) { return i.state !== 'done'; }); }

  function ticked(j) { return left(j).filter(function (i) { return i.on; }); }

  function fresh(film) { return { film: film, on: true, state: '', note: '' }; }

  /* What there is to do for *coll*, with the failures of the job before; null for nothing. */
  function jobOf(coll) {
    var films = missing(coll);
    if (!films.length) return null;
    var before = jobs[coll.name];
    var old = {};
    if (before) before.items.forEach(function (i) { old[i.film.ref] = i; });
    function unsettled(f) {
      var i = old[f.ref];
      return !!i && !i.had && (i.state === 'failed' || i.state === 'unasked');
    }
    if (!films.some(unsettled)) return { coll: coll, busy: false, count: 0, at: 0, items: films.map(fresh) };
    before.coll = coll;
    before.items = coll.films.map(function (f) {
      var i = old[f.ref];
      if (f.can_request) {
        if (i && i.state !== 'done') {
          i.film = f;
          i.had = null;
          return i;
        }
        return fresh(f);
      }
      return i && i.state === 'done' && !i.had ? i : null;
    }).filter(Boolean);
    return before;
  }

  function openDialog(v) {
    if (job && job.busy && job.coll.name !== v.coll.name) {
      core.toast('Still adding the films of ' + job.coll.name + '. Try again in a moment.');
      return;
    }
    if (!(job && job.busy)) {
      var next = jobOf(v.coll);
      if (!next) return;
      job = jobs[v.coll.name] = next;
    }
    dialog = dialog || build();
    fill();
    dialog.open = true;
    openedAt = Date.now();
    dialog.modal.open();
    dialog.body.scrollTop = 0;
    edges();
  }

  function fill() {
    var d = dialog;
    d.title.textContent = job.items.length === 1 ? 'Download the missing film' : 'Download the missing films';
    d.lead.textContent = job.coll.name;
    d.said.textContent = job.text || '';
    d.list.textContent = '';
    d.list.setAttribute('aria-label', 'Films to download');
    job.items.forEach(function (item) { d.list.appendChild(rowOf(item)); });
    d.out.textContent = '';
    var wanted = job.items.map(function (i) { return i.film.ref; });
    var rest = job.coll.films.filter(function (f) { return wanted.indexOf(f.ref) === -1; });
    if (rest.length) {
      d.out.appendChild(el('h4', '', 'Left out'));
      OUT.forEach(function (why) {
        var films = rest.filter(function (f) { return f.why_not === why[0]; });
        if (!films.length) return;
        var p = el('p');
        p.appendChild(el('b', '', why[1] + ': '));
        p.appendChild(document.createTextNode(films.map(function (f) {
          return f.title + (why[0] === 'unreleased' && f.release_date ? ' (' + day(f.release_date) + ')' : '');
        }).join(', ')));
        d.out.appendChild(p);
      });
    }
    d.out.hidden = !d.out.children.length;
    sync();
  }

  function rowOf(item) {
    var f = item.film;
    var li = el('li');
    var row = item.row = el('label', 'coll-item');
    var mark = el('span', 'coll-item__mark');
    item.box = el('input');
    item.box.type = 'checkbox';
    item.box.checked = item.on;
    item.box.addEventListener('change', function () {
      item.on = item.box.checked;
      sync();
    });
    mark.appendChild(item.box);
    item.sign = el('span', 'coll-item__sign');
    item.sign.setAttribute('aria-hidden', 'true');
    item.shown = '';
    mark.appendChild(item.sign);
    row.appendChild(mark);
    var poster = el('span', 'coll-item__poster');
    poster.setAttribute('aria-hidden', 'true');
    var src = core.safePath(f.poster_url);
    if (src) {
      var img = el('img');
      img.alt = '';
      img.loading = 'lazy';
      img.setAttribute('data-broken-on-error', '');
      img.setAttribute('src', src);
      poster.appendChild(img);
    }
    row.appendChild(poster);
    var words = el('span', 'coll-item__words');
    words.appendChild(el('span', 'coll-item__name', f.title));
    item.sub = el('span', 'coll-item__sub');
    words.appendChild(item.sub);
    row.appendChild(words);
    li.appendChild(row);
    return li;
  }

  var SIGNS = { done: 'i-circle-check', failed: 'i-circle-exclamation', wait: 'i-clock' };

  /* What the open dialog says of its films and offers. */
  function sync() {
    if (!dialog || !job.items[0].row) return;
    var d = dialog;
    job.items.forEach(function (item) {
      var locked = job.busy || item.state === 'done';
      var sign = locked ? item.state : '';
      item.box.checked = item.on;
      item.box.disabled = locked;
      item.row.classList.toggle('is-off', !item.on);
      ['wait', 'asking', 'done', 'failed'].forEach(function (s) { item.row.classList.toggle('is-' + s, item.state === s); });
      if (item.shown !== sign) {
        item.sign.textContent = '';
        if (sign === 'asking') item.sign.appendChild(el('span', 'btn-spinner'));
        else if (SIGNS[sign]) item.sign.appendChild(icon(SIGNS[sign]));
        item.shown = sign;
      }
      item.row.classList.toggle('is-locked', locked && (!!SIGNS[sign] || sign === 'asking'));
      item.sub.textContent = [item.film.year, item.note].filter(Boolean).join(' · ');
    });
    var n = ticked(job).length;
    var rest = left(job).length;
    var tried = job.items.some(function (i) { return i.state; });
    d.go.textContent = job.busy ? 'Adding ' + Math.min(job.count, job.at + 1) + ' of ' + job.count + '…'
      : !rest ? 'Done'
      : !n ? 'Pick films' : (tried ? 'Try again with ' : 'Download ') + n;
    if (job.busy || (rest && !n)) d.go.setAttribute('aria-disabled', 'true');
    else d.go.removeAttribute('aria-disabled');
    d.all.textContent = n < rest ? 'Select all' : 'Select none';
    d.all.disabled = job.busy || rest < 2;
    d.cancel.textContent = tried ? 'Close' : 'Cancel';
    d.cancel.hidden = !rest;
    d.root.classList.toggle('is-busy', job.busy);
  }

  /* Where the list goes on out of sight, it fades. */
  function edges() {
    var b = dialog && dialog.body;
    if (!b) return;
    b.classList.toggle('has-above', b.scrollTop > 1);
    b.classList.toggle('has-below', b.scrollTop < b.scrollHeight - b.clientHeight - 1);
  }

  /* The film being added, in the dialog's list. */
  function inSight(item) {
    if (!dialog || !dialog.open || !item.row) return;
    var box = dialog.body.getBoundingClientRect();
    var row = item.row.getBoundingClientRect();
    if (row.top < box.top) dialog.body.scrollTop += row.top - box.top;
    else if (row.bottom > box.bottom) dialog.body.scrollTop += row.bottom - box.bottom;
  }

  function say(j, text) {
    j.text = text;
    if (dialog && job === j) dialog.said.textContent = text;
  }

  /* ── Asking ── */

  function start(j) {
    var items = ticked(j);
    if (j.busy || !items.length) return;
    j.busy = true;
    j.count = items.length;
    j.at = 0;
    var run = j.run = { limited: false };
    items.forEach(function (item) {
      item.state = 'wait';
      item.note = 'Waiting';
    });
    say(j, items.length > 1 ? 'Keep this page open until all ' + items.length + ' are added.' : '');
    update();
    line = items.reduce(function (before, item) {
      return before.then(function () { return askFor(j, item, run); });
    }, line).then(function () { if (j.run === run) finish(j, items); });
  }

  function waits() { return !!job && job.busy && job.items.some(function (i) { return i.state === 'wait'; }); }

  /* Asked as a request finds the session over and goes to sign in: no more can be added, and
     the page is not to be held back. */
  var hasUnsaved = window.MM.api.hasUnsavedChanges;
  window.MM.api.hasUnsavedChanges = function () {
    stop();
    return hasUnsaved();
  };

  function stop() {
    var j = job;
    if (!j || !j.busy) return;
    j.run = null; // an answer still to come starts nothing more
    j.busy = false;
    j.items.forEach(function (i) { if (i.state === 'wait' || i.state === 'asking') unaskedAgain(i); });
    line = Promise.resolve();
    say(j, 'You were signed out. Sign in to add the rest.');
    update();
  }

  function askToStay(e) {
    if (!waits()) return;
    e.preventDefault();
    e.returnValue = 'yes'; // older browsers ask by this
  }

  function update() {
    // Only while it is needed: Firefox does not keep a page that listens for it.
    window.removeEventListener('beforeunload', askToStay);
    if (waits()) window.addEventListener('beforeunload', askToStay);
    sync();
    if (view && view.node && view.coll) label(view);
  }

  function unaskedAgain(item) {
    item.state = 'unasked';
    item.note = 'Not asked yet';
  }

  function askFor(j, item, run) {
    if (j.run !== run) return null;
    var f = item.film;
    if (item.had) {
      item.state = 'done';
      item.note = item.had;
      j.at += 1;
      update();
      return null;
    }
    if (run.limited) {
      unaskedAgain(item);
      update();
      return null;
    }
    item.state = 'asking';
    item.note = 'Adding…';
    update();
    inSight(item);
    return window.MM.api.post('/api/search/download', {
      media_type: 'movie', tmdb_id: Number(f.ref.replace('tmdb-movie-', '')), title: f.title
    }).then(function () { done(item, 'Queued'); }, function (err) {
      if (j.run !== run) return;
      if (err && err.status === 409) {
        done(item, 'Already added');
        return;
      }
      run.limited = !!err && err.status === 429 && /^too many/i.test(err.message || '');
      if (run.limited) {
        unaskedAgain(item);
        return;
      }
      item.state = 'failed';
      item.note = window.MM.api.messageOf(err, 'Something went wrong. Try again.');
    }).then(function () {
      if (j.run === run) j.at += 1;
      update();
    });
  }

  function done(item, note) {
    var f = item.film;
    item.state = 'done';
    item.note = note;
    asked[f.ref] = Date.now();
    var refs = [f.ref];
    // The title on show may go by another ref, such as a pick's.
    if (view && view.coll && hereIn(view.coll, view.ref) && hereIn(view.coll, view.ref).ref === f.ref && view.ref !== f.ref) refs.push(view.ref);
    refs.forEach(function (ref) {
      document.dispatchEvent(new CustomEvent('mm:title-changed', {
        detail: { ref: ref, download_state: 'queued', download_pct: null, title: f.title, kind: 'movie', year: f.year }
      }));
    });
  }

  /* *film* came to be queued or held by another way while it was still to be asked for; *gone*:
     it was taken out again. */
  function gotElsewhere(film, gone) {
    if (!job) return;
    job.items.forEach(function (i) {
      if (i.film.ref !== film.ref || i.state === 'asking' || i.state === 'done') return;
      i.had = gone ? null : OUT.filter(function (why) { return why[0] === film.why_not; })[0][1];
    });
  }

  function finish(j, items) {
    var got = items.filter(function (i) { return i.state === 'done'; }).length;
    var unasked = items.filter(function (i) { return i.state === 'unasked'; }).length;
    var failed = items.length - got - unasked;
    j.busy = false;
    var queued = items.some(function (i) { return i.had; }) ? 'queued or in the library.' : 'queued.';
    var name = items[0].film.title;
    var words = [];
    if (got) words.push((items.length === 1 ? name + ' is ' : got === items.length ? got + ' films are '
      : got + ' of ' + items.length + (got === 1 ? ' films is ' : ' films are ')) + queued);
    if (failed) words.push(got ? plural(failed, 'was', 'were') + ' not.'
      : items.length === 1 ? name + ' was not queued.'
      : failed === items.length ? 'None of the ' + failed + ' films was queued.'
      : plural(failed, 'film was', 'films were') + ' not queued.');
    if (unasked) words.push((items.length === 1 ? name + ' is' : got || failed ? plural(unasked, 'is', 'are')
      : unasked + ' films are') + ' not asked for yet: ' + WHEN);
    var text = words.join(' ');
    say(j, text);
    j.endedAt = Date.now();
    if (!(dialog && dialog.open && job === j)) core.toast(text, { variant: failed ? 'error' : unasked ? 'info' : 'success' });
    update();
  }

  /* ── What changes elsewhere ── */

  document.addEventListener('mm:title-changed', function (e) {
    var d = e.detail || {};
    var all = Object.keys(known).map(function (k) { return known[k]; });
    all.concat((view && view.coll) || [], (job && job.coll) || []).forEach(function (coll) {
      coll.films.forEach(function (f) {
        if (!isOf(f, d.ref) && !(d.kind === 'movie' && f.title === d.title && f.year === d.year)) return;
        if (d.download_state) {
          changed(f, d.download_state, d.download_pct);
          gotElsewhere(f);
        } else {
          if (view) view.stale = true;
          gotElsewhere(f, true);
        }
      });
    });
    // What the page changes on its cards it does not change on those off it.
    if (view && view.node && !view.node.isConnected) view.node = null;
    if (view && view.node && view.coll) label(view);
  });

  ['mm:watch-marked', 'mm:watch-kept', 'mm:watch-admin', 'mm:watch-scheduled'].forEach(function (name) {
    document.addEventListener(name, function () { if (view) view.stale = true; });
  });

  watch.collection = { into: into, skeleton: skeleton };
})();
