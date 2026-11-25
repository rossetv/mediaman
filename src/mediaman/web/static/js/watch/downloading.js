/* Watch page: the row of what is downloading, after Continue watching and only
 * while there is something in it. A card fills with colour as its title comes
 * in; one that is done says so for a minute, then its poster goes to the front
 * of Recently added.
 * Needs downloading.js and watch/core.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;
  var icon = window.MM.dom.icon;
  var toArray = core.toArray;

  var REF_RE = /^(movie|show)-[0-9]{1,18}$|^tmdb-(movie|tv)-[0-9]{1,10}$/;
  var HREF = '/downloads';
  var STATES = { queued: ['is-queued'], searching: ['is-looking'], stuck: ['is-looking', 'is-stuck'], ready: ['is-ready'] };
  var FLY_MS = 520;
  var SWEEP_MS = 1700; // past the ring the new card arrives with
  var FLY_EASE = 'cubic-bezier(.3,.7,.2,1)';

  var slot = core.byId('watch-downloading');
  if (!slot || !window.MM.downloading) return;

  var inner = null;
  var scroller = null;
  var words = null; // { name, count, link, note }
  var cards = {}; // by key: { node, item, timer }
  var landed = {}; // keys of what went to Recently added, until it downloads again
  var last = null; // the answer on show
  var starting = true;
  var closing = null;
  var opening = null;

  // Outside the row, which is not there to be read out when it has nothing.
  var said = el('p', 'sr-only');
  said.setAttribute('role', 'status');
  slot.parentNode.insertBefore(said, slot);

  /* ── What a card says ── */

  function left(minutes) {
    return minutes > 59 ? 'about ' + Math.round(minutes / 60) + ' h left' : minutes + ' min left';
  }

  /* [what stands out, the rest] */
  function says(item) {
    if (item.state === 'ready') return ['', item.line || 'Ready to watch'];
    if (item.state === 'almost_ready') return ['Almost ready', ''];
    if (item.state === 'queued') return ['', 'Queued'];
    if (item.state === 'stuck') return ['', 'Nothing found yet' + (item.line ? ' · ' + item.line : '')];
    if (item.state === 'searching') return ['', 'Looking for the best version'];
    return [item.percent + '%', typeof item.minutes_left === 'number' ? ' · ' + left(item.minutes_left) : ''];
  }

  /* TheTVDB's posters have no address of mediaman's own. */
  function picture(value) {
    return core.safePath(value) || (/^https:\/\//.test(value || '') ? value : '');
  }

  function image(className) {
    var img = el('img', className);
    img.setAttribute('alt', '');
    img.setAttribute('data-broken-on-error', '');
    return img;
  }

  /* The poster stands in where there is no wide picture. */
  function setPicture(img, item) {
    var poster = picture(item.poster_url);
    var art = img.classList.contains('dw__pic') ? '' : picture(item.art_url);
    var want = art || poster;
    if (want === img.getAttribute('data-of')) return;
    img.setAttribute('data-of', want);
    img.removeAttribute('data-broken');
    img.removeAttribute('data-next-src');
    if (art && poster) img.setAttribute('data-next-src', poster);
    if (want) img.setAttribute('src', want);
    else img.removeAttribute('src');
  }

  /* ── Cards ── */

  function build(item) {
    var card = el('article', 'watch-card dw');
    var link = el('a', 'dw__link');
    ['dw__art', 'dw__fill'].forEach(function (name) { link.appendChild(image(name)); });
    link.appendChild(el('span', 'dw__poster')).appendChild(image('dw__pic'));
    var text = el('span', 'dw__text');
    text.appendChild(el('span', 'dw__title', item.title));
    var line = text.appendChild(el('span', 'dw__line'));
    line.appendChild(el('strong'));
    line.appendChild(el('span'));
    link.appendChild(text);
    var go = el('span', 'dw__go');
    go.setAttribute('aria-hidden', 'true');
    go.appendChild(icon('i-play'));
    link.appendChild(go);
    ['dw__bar', 'dw__sweep'].forEach(function (name) {
      var part = el('span', name);
      part.setAttribute('aria-hidden', 'true');
      if (name === 'dw__bar') part.appendChild(el('i'));
      link.appendChild(part);
    });
    card.appendChild(link);
    return card;
  }

  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }

  function patch(card, item, lost) {
    var node = card.node;
    var link = node.querySelector('.dw__link');
    var wasReady = card.item && card.item.state === 'ready';
    card.item = item;
    toArray(link.querySelectorAll('img')).forEach(function (img) { setPicture(img, item); });
    var on = STATES[item.state] || [];
    ['is-queued', 'is-looking', 'is-stuck', 'is-ready'].forEach(function (name) {
      node.classList.toggle(name, on.indexOf(name) >= 0);
    });
    node.classList.toggle('is-held', !!(item.held || lost));
    node.style.setProperty('--p', String(item.percent));
    var ref = REF_RE.test(item.ref || '') ? item.ref : '';
    link.setAttribute('href', (ref && core.safePath(item.href)) || HREF);
    if (ref) {
      link.setAttribute('data-title-ref', ref);
      link.setAttribute('data-title', item.title);
      link.setAttribute('data-kind', item.kind === 'movie' ? 'movie' : 'show');
    } else ['data-title-ref', 'data-title', 'data-kind'].forEach(function (name) { link.removeAttribute(name); });
    var line = node.querySelector('.dw__line');
    var text = says(item);
    setText(line.children[0], text[0]);
    setText(line.children[1], text[1]);
    if (item.state === 'ready' && !wasReady) {
      card.timer = setTimeout(function () { land(item.key, Object.keys(cards).length === 1); draw(last); }, Math.max(1, item.ready_seconds || 1) * 1000);
    }
    if (item.state !== 'ready') clearTimeout(card.timer);
  }

  /* ── The row ── */

  function buildRow() {
    inner = el('div', 'dlx__in');
    var section = el('section', 'watch-row');
    var head = section.appendChild(el('div', 'watch-row__head'));
    var link = head.appendChild(el('h2', 'watch-row__title')).appendChild(el('a', 'row-open'));
    link.setAttribute('href', HREF);
    var name = link.appendChild(el('span'));
    var count = link.appendChild(el('span', 'dlx__count'));
    var cue = link.appendChild(el('span', 'row-open__cue'));
    cue.appendChild(el('span', 'row-open__word', 'See all'));
    cue.appendChild(icon('i-chevron-right'));
    var note = section.appendChild(el('p', 'dlx__note'));
    note.hidden = true;
    note.appendChild(icon('i-triangle-exclamation'));
    var noteText = note.appendChild(el('span'));
    var viewport = section.appendChild(el('div', 'watch-row__viewport'));
    scroller = viewport.appendChild(el('div', 'watch-row__scroll'));
    scroller.setAttribute('tabindex', '0');
    scroller.setAttribute('role', 'region');
    inner.appendChild(section);
    slot.appendChild(inner);
    words = { name: name, count: count, link: link, note: note, noteText: noteText };
    // Until it loads; then watch-rows.js starts every row it finds.
    if (window.MMWatch.rows) window.MMWatch.rows.init(viewport);
  }

  /* While it opens it cuts its cards off, and their shadows after. */
  function rest() {
    opening = setTimeout(function () { slot.classList.add('is-resting'); }, starting ? 0 : core.tokenMs('--dur-slow', 250));
  }

  function open() {
    if (slot.classList.contains('is-open')) return;
    slot.hidden = false;
    if (!starting) void slot.offsetWidth; // so that it opens from nothing
    slot.classList.add('is-open');
    rest();
  }

  function close() {
    if (!inner || closing) return;
    clearTimeout(opening);
    slot.classList.remove('is-resting');
    void slot.offsetWidth;
    slot.classList.remove('is-open');
    closing = setTimeout(function () {
      closing = null;
      inner.remove();
      inner = scroller = words = null;
      slot.hidden = true;
    }, core.reduceMotion() ? 0 : core.tokenMs('--dur-slow', 250));
  }

  function heading(state, list) {
    var busy = list.some(function (item) { return item.state !== 'ready'; });
    var gone = state.items.length - list.length;
    var total = Math.max(list.length, state.summary.total - gone);
    var name = busy ? 'Downloading' : 'Ready';
    setText(words.name, name);
    setText(words.count, String(total));
    words.link.setAttribute('aria-label', name + ', ' + total + ', see all in Downloads');
    scroller.setAttribute('aria-label', name);
    var outage = state.lost ? 'Can’t reach mediaman' : state.outage.replace("'", '’');
    var held = state.lost || list.some(function (item) { return item.held; });
    words.note.hidden = !outage;
    setText(words.noteText, outage ? outage + ' right now.' + (held ? ' This is how it last was.' : '') : '');
  }

  /* The cards in the order of *list*, moving only those out of place. */
  function order(list) {
    var active = document.activeElement;
    var next = scroller.firstElementChild;
    list.forEach(function (item) {
      var node = cards[item.key].node;
      while (next && next.classList.contains('is-leaving')) next = next.nextElementSibling;
      if (node === next) next = next.nextElementSibling;
      else scroller.insertBefore(node, next);
    });
    if (active && document.activeElement !== active && scroller.contains(active)) active.focus({ preventScroll: true });
  }

  function draw(state) {
    last = state;
    var list = state.items.filter(function (item) {
      if (item.state !== 'ready') delete landed[item.key];
      return !landed[item.key];
    });
    var there = {};
    list.forEach(function (item) { there[item.key] = true; });
    Object.keys(cards).forEach(function (key) {
      if (there[key]) return;
      // Its minute ran out between two answers.
      if (cards[key].item.state === 'ready') land(key, !list.length);
      else drop(key, !list.length);
    });
    if (!list.length) { close(); return; }
    if (closing) comeBack();
    if (!inner) buildRow();
    var isNew = !slot.classList.contains('is-open');
    var play = isNew ? null : core.flip(toArray(scroller.children));
    list.forEach(function (item) {
      var card = cards[item.key];
      var was = card && card.item.state;
      if (!card) {
        card = cards[item.key] = { node: build(item), item: null, timer: null };
        if (!isNew) core.fadeIn(card.node);
      }
      patch(card, item, state.lost);
      if (item.state === 'ready' && was && was !== 'ready') setText(said, item.title + ' is ready to watch');
    });
    order(list);
    sole();
    heading(state, list);
    if (play) play();
    open();
  }

  /* Something to show again before the row has closed: what it closed with is gone. */
  function comeBack() {
    clearTimeout(closing);
    closing = null;
    toArray(scroller.children).forEach(function (n) { n.remove(); });
    Object.keys(cards).forEach(function (key) { scroller.appendChild(cards[key].node); });
    slot.classList.add('is-open');
    rest();
  }

  function sole() {
    if (!scroller) return;
    var nodes = toArray(scroller.children).filter(function (n) { return !n.classList.contains('is-leaving'); });
    nodes.forEach(function (n) { n.classList.toggle('is-sole', nodes.length === 1); });
  }

  /* ── Leaving ── */

  /* Take a card out; with the whole row closing it goes with that. */
  function drop(key, withRow) {
    var card = cards[key];
    if (!card) return;
    delete cards[key];
    clearTimeout(card.timer);
    var node = card.node;
    if (withRow) return;
    var had = node.contains(document.activeElement);
    var others = toArray(scroller.children);
    var at = others.indexOf(node);
    var next = others[at + 1] || others[at - 1];
    function gone() {
      node.remove();
      sole();
    }
    if (had && next) core.focusSoftly(next.querySelector('.dw__link'));
    if (core.reduceMotion() || !node.animate || !onScreen(node)) { gone(); return; }
    node.classList.add('is-leaving');
    var gap = parseFloat(getComputedStyle(scroller).columnGap) || 0;
    var away = -(node.getBoundingClientRect().width + gap) + 'px';
    node.animate([{ opacity: 1, marginRight: 0 }, { opacity: 0, marginRight: away }],
      { duration: core.cardExit().total, easing: core.token('--ease-out', 'ease-out'), fill: 'forwards' }).onfinish = gone;
  }

  /* To be seen: not under a title, a grid or another tab. */
  function onScreen(node) {
    if (document.hidden || !node.getClientRects().length || document.documentElement.classList.contains('has-ttl')) return false;
    var box = node.getBoundingClientRect();
    return box.bottom > 0 && box.top < window.innerHeight;
  }

  function recentRow() {
    var row = document.querySelector('.watch-row[data-row-id="recent"]');
    return row && row.querySelector('.watch-row__scroll');
  }

  /* The markup of _watch_card.html's card(). */
  function recentCard(item) {
    var card = el('article', 'watch-card');
    var link = card.appendChild(el('a', 'watch-card__link'));
    link.setAttribute('href', core.safePath(item.href));
    link.setAttribute('data-title-ref', item.ref);
    link.setAttribute('data-kind', item.kind === 'movie' ? 'movie' : 'show');
    var poster = link.appendChild(el('span', 'watch-card__poster'));
    var src = picture(item.poster_url);
    if (src) {
      var img = poster.appendChild(el('img', 'is-loaded'));
      img.setAttribute('src', src);
      img.setAttribute('alt', '');
      img.setAttribute('data-broken-on-error', '');
    }
    poster.appendChild(el('span', 'watch-card__title', item.title));
    poster.appendChild(el('span', 'watch-badge watch-badge--new', 'New'));
    link.appendChild(el('span', 'watch-card__sub', item.kind === 'movie' ? 'Movie' : 'TV'));
    return card;
  }

  /* Put *item* at the front of Recently added and return its card, or null: the
     page has no such row, or what is done has no title. */
  function intoRecent(item) {
    var row = recentRow();
    if (!row || !REF_RE.test(item.ref || '')) return null;
    var there = toArray(row.children).filter(function (n) {
      return n.querySelector && n.querySelector('[data-title-ref="' + item.ref + '"]');
    })[0];
    if (there && there === row.firstElementChild) return there;
    var play = core.flip(toArray(row.children).slice(0, 12));
    var card = there || recentCard(item);
    var at = row.scrollLeft;
    var using = at > 2 || row.contains(document.activeElement);
    var snap = row.style.scrollSnapType;
    row.style.scrollSnapType = 'none';
    row.insertBefore(card, row.firstElementChild);
    // Whoever is further along the row stays where they are.
    if (!using) row.scrollLeft = 0;
    else if (!there) row.scrollLeft = at + card.offsetWidth + (parseFloat(getComputedStyle(row).columnGap) || 0);
    row.style.scrollSnapType = snap;
    if (!using) play();
    return card;
  }

  /* What is done goes to Recently added, its poster across the page. */
  function land(key, withRow) {
    var card = cards[key];
    if (!card || card.item.state !== 'ready') return;
    landed[key] = true;
    var item = card.item;
    var node = card.node;
    var poster = node.querySelector('.dw__pic');
    var from = onScreen(node) && poster.getAttribute('src') ? poster.getBoundingClientRect() : null;
    var had = node.contains(document.activeElement);
    var there = intoRecent(item);
    var to = there && there.querySelector('.watch-card__poster');
    if (had && withRow && there) core.focusSoftly(there.querySelector('.watch-card__link'));
    if (from && to && !core.reduceMotion() && node.animate) fly(poster, from, to, there);
    drop(key, withRow);
  }

  function fly(poster, from, to, card) {
    var end = to.getBoundingClientRect();
    var seen = end.top < window.innerHeight - 60 && end.bottom > 0 && end.right > 0 && end.left < window.innerWidth;
    var top = seen ? end.top : window.innerHeight + 20;
    var ghost = el('img', 'dw-fly');
    ghost.setAttribute('src', poster.getAttribute('src'));
    ghost.setAttribute('alt', '');
    [['left', end.left], ['top', top], ['width', end.width], ['height', end.height]].forEach(function (p) {
      ghost.style.setProperty(p[0], p[1] + 'px');
    });
    document.body.appendChild(ghost);
    to.style.setProperty('opacity', '0');
    function arrive() {
      if (!ghost.parentNode) return;
      ghost.remove();
      to.style.removeProperty('opacity');
      card.classList.add('is-new');
      setTimeout(function () { card.classList.remove('is-new'); }, SWEEP_MS);
    }
    ghost.animate([
      { transform: 'translate(' + (from.left - end.left) + 'px,' + (from.top - top) + 'px) scale(' + from.width / end.width + ',' + from.height / end.height + ')' },
      { transform: 'none' }
    ], { duration: FLY_MS, easing: FLY_EASE });
    setTimeout(arrive, FLY_MS);
  }

  window.MM.downloading.subscribe(draw);
  starting = false;
})();
