/* Watch: the thumb and the eye under every card's poster. A mouse that rests on the thumb,
 * or a press, has the four levels rise up the poster's edge; on a touch screen the poster
 * becomes their panel, with the card's other buttons in it, and a finger may press, slide
 * and let go in one movement. The levels and their state are ratings.js's, so a title says
 * the same wherever it is drawn; the eye and Dismiss are watch/marks.js's.
 *
 *   close(card)      fold what *card*, or any card, has open
 *   faceOf(button, ref)  draw on a thumb button how the title is rated
 *   SHOW_MS          how long a level just given is looked at before it folds
 *
 * Needs watch/core.js and ratings.js; on window.MMWatch.cardTools.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var ratings = window.MM.ratings;
  var el = core.el;
  var toArray = core.toArray;

  var DWELL_MS = 130;  // a mouse resting on the thumb
  var LEAVE_MS = 240;  // and off the card
  var HELD_MS = 220;   // a finger that stays on the thumb
  var SLIDE_PX = 10;
  var SHOW_MS = 560;
  var SHOW_STILL_MS = 350; // with nothing moving
  var OWN_CLICK_MS = 500;  // a press on a thumb that has just opened by itself
  var FOLD_MS = 260;

  if (!ratings) return;

  var open = null; // { card, thumb, over, set, picked, opened }
  var dwell = null;
  var leaving = null;
  var slid = false; // the click that ends a slide is not a press

  function mouse() { return window.matchMedia('(hover: hover) and (pointer: fine)').matches; }

  function titleOf(card) { return card.querySelector('.watch-card__title').textContent.trim(); }

  function refOf(card) { return card.getAttribute('data-tmdb-ref'); }

  function thumbOf(card) { return card.querySelector('[data-card-rate]'); }

  /* ── What the thumbs say ── */

  function nameOf(level) {
    var found = ratings.LEVELS.filter(function (l) { return l.level === level; })[0];
    return found ? found.name : '';
  }

  function faceOf(button, ref) {
    var level = ratings.get(ref) || null;
    if (button.getAttribute('data-face') === String(level)) return;
    button.setAttribute('data-face', String(level));
    button.classList.toggle('is-up', level > 0);
    button.classList.toggle('is-down', level < 0);
    button.textContent = '';
    button.appendChild(ratings.thumb(level || 1, !!level));
  }

  function paint(card) {
    var thumb = thumbOf(card);
    var level = ratings.get(refOf(card)) || null;
    if (level) card.setAttribute('data-level', String(level));
    else card.removeAttribute('data-level');
    if (!thumb) return;
    faceOf(thumb, refOf(card));
    thumb.hidden = ratings.canRate(refOf(card)) === false;
    thumb.setAttribute('aria-label', 'Rate ' + titleOf(card) + (level ? ': ' + nameOf(level) : ''));
  }

  /* A card that has come onto the page: what it was drawn with is taken as known, and it then
     says what is known now. */
  function adopt(card) {
    var ref = refOf(card);
    if (ref) {
      var link = card.querySelector('.watch-card__link');
      var refs = [card.getAttribute('data-dismiss-ref'), link && link.getAttribute('data-title-ref'), link && link.getAttribute('data-pick-ref')];
      ratings.know(ref, Number(card.getAttribute('data-level')) || null, refs.filter(Boolean));
      paint(card);
    }
    if (window.MMWatch.marks) window.MMWatch.marks.adopt(card);
  }

  function adoptAll(root) {
    if (root.matches && root.matches('.watch-card')) adopt(root);
    toArray(root.querySelectorAll('.watch-card')).forEach(adopt);
  }

  document.addEventListener('mm:rating', function (e) {
    var d = e.detail;
    var own = d.tmdbId ? 'tmdb-' + d.mediaType + '-' + d.tmdbId : '';
    toArray(document.querySelectorAll('.watch-card[data-tmdb-ref]')).forEach(function (card) {
      if (refOf(card) === own || d.refs.indexOf(refOf(card)) >= 0) paint(card);
    });
    if (open && d.from === open.set.node && d.phase === 'pending') picked(d.level);
  });

  // A page the browser kept comes back with the levels it left with.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    ratings.read(toArray(document.querySelectorAll('.watch-card[data-tmdb-ref]')).map(refOf)
      .filter(function (ref, i, all) { return all.indexOf(ref) === i; }));
  });

  new MutationObserver(function (changes) {
    changes.forEach(function (c) {
      toArray(c.addedNodes).forEach(function (n) { if (n.nodeType === 1 && n.isConnected) adoptAll(n); });
    });
    if (open && !open.card.isConnected) drop(open);
  }).observe(document.body, { childList: true, subtree: true });

  /* ── The levels, over the poster ── */

  function copyTool(button) {
    var b = el('button', 'card-over__b');
    b.type = 'button';
    b.setAttribute('tabindex', '-1');
    ['data-mark-toggle', 'aria-pressed', 'data-watch-dismiss', 'data-mark-name', 'data-watch-more', 'aria-haspopup', 'aria-label', 'aria-busy', 'aria-disabled'].forEach(function (name) {
      if (button.hasAttribute(name)) b.setAttribute(name, button.getAttribute(name));
    });
    if (b.hasAttribute('data-watch-more')) b.setAttribute('aria-expanded', 'false');
    b.appendChild(button.querySelector('svg').cloneNode(true));
    return b;
  }

  function openOn(card, focus) {
    clearTimeout(leaving);
    if (open && open.card === card) return;
    shut();
    var over = el('div', 'card-over');
    over.appendChild(el('span', 'card-over__scrim'));
    var column = over.appendChild(el('div', 'card-over__col'));
    toArray(card.querySelectorAll('.card-tool:not([data-card-rate])')).reverse().forEach(function (b) { column.appendChild(copyTool(b)); });
    var set = ratings.mount(refOf(card), { variant: 'lift', label: 'Your rating of ' + titleOf(card) });
    over.appendChild(set.node);
    card.appendChild(over);
    open = { card: card, thumb: thumbOf(card), over: over, set: set, opened: performance.now(), timer: null };
    void over.offsetWidth; // so that it rises
    card.classList.add('is-lifted');
    set.node.classList.add('is-open');
    open.thumb.setAttribute('aria-expanded', 'true');
    if (focus) set.enter();
    else if (!mouse()) {
      var given = set.node.querySelector('[aria-checked="true"]');
      if (given) set.pointAt(given);
    }
  }

  function remove(o) {
    o.set.destroy();
    o.over.remove();
  }

  /* Its card has gone from the page. */
  function drop(o) {
    open = null;
    clearTimeout(o.timer);
    o.card.classList.remove('is-lifted');
    o.thumb.setAttribute('aria-expanded', 'false');
    remove(o);
  }

  function shut(focus) {
    var o = open;
    if (!o) return;
    open = null;
    clearTimeout(o.timer);
    clearTimeout(leaving);
    var had = o.over.contains(document.activeElement);
    o.card.classList.remove('is-lifted');
    o.set.node.classList.remove('is-open');
    o.set.pointAt(null);
    o.over.setAttribute('inert', '');
    o.thumb.setAttribute('aria-expanded', 'false');
    if (focus || had) o.thumb.focus({ preventScroll: true });
    setTimeout(function () { remove(o); }, core.reduceMotion() ? 0 : FOLD_MS);
  }

  /* Until the mouse has left the card, its preview stays away. */
  function quiet(card) { card.classList.add('is-quiet'); }

  function picked(level) {
    var o = open;
    quiet(o.card);
    clearTimeout(o.timer);
    o.timer = setTimeout(function () {
      if (level !== null) comeDown(o);
      shut(o.over.contains(document.activeElement));
    }, core.reduceMotion() ? SHOW_STILL_MS : SHOW_MS);
  }

  /* The thumb given comes down to its place in the caption. */
  function comeDown(o) {
    var from = o.set.node.querySelector('[aria-checked="true"] .rate-th');
    var to = o.thumb.querySelector('.rate-th');
    if (!from || !to || core.reduceMotion() || !o.thumb.animate || !to.getClientRects().length) return;
    var a = from.getBoundingClientRect();
    var b = to.getBoundingClientRect();
    var c = o.card.getBoundingClientRect();
    var flier = el('span', 'card-flier ' + (o.thumb.classList.contains('is-up') ? 'is-up' : 'is-down'));
    flier.setAttribute('aria-hidden', 'true');
    flier.appendChild(from.cloneNode(true)).classList.add('is-on');
    flier.style.left = a.left - c.left + 'px';
    flier.style.top = a.top - c.top + 'px';
    flier.style.width = a.width + 'px';
    flier.style.height = a.height + 'px';
    o.card.appendChild(flier);
    to.style.opacity = '0';
    from.style.opacity = '0';
    var ms = core.tokenMs('--dur', 200) * 1.5;
    flier.animate([
      { transform: 'none' },
      { transform: 'translate(' + (b.left + b.width / 2 - a.left - a.width / 2) + 'px,' + (b.top + b.height / 2 - a.top - a.height / 2) + 'px) scale(' + b.width / a.width + ')' }
    ], { duration: ms, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards' });
    setTimeout(function () {
      flier.remove();
      to.style.opacity = '';
      to.animate([{ transform: 'scale(.7)' }, { transform: 'scale(1.25)' }, { transform: 'none' }], { duration: 260, easing: 'ease-out' });
    }, ms);
  }

  /* ── The mouse ── */

  function thumbAt(target) { return target && target.closest ? target.closest('[data-card-rate]') : null; }

  document.addEventListener('pointerover', function (e) {
    if (e.pointerType !== 'mouse' || !mouse()) return;
    var thumb = thumbAt(e.target);
    if (!thumb || (e.relatedTarget && thumb.contains(e.relatedTarget))) return;
    clearTimeout(dwell);
    var card = thumb.closest('.watch-card');
    dwell = setTimeout(function () {
      if (card.isConnected && !e.buttons && !document.documentElement.classList.contains('is-grid-moving')) openOn(card);
    }, DWELL_MS);
  });

  document.addEventListener('pointerout', function (e) {
    if (e.pointerType !== 'mouse' || !e.target.closest) return;
    var to = e.relatedTarget;
    var thumb = thumbAt(e.target);
    if (thumb && !(to && thumb.contains(to))) clearTimeout(dwell);
    var card = e.target.closest('.watch-card');
    if (!card || (to && card.contains(to))) return;
    card.classList.remove('is-quiet');
    if (!open || open.card !== card) return;
    clearTimeout(leaving);
    leaving = setTimeout(function () {
      if (open && open.card === card && !open.over.querySelector('.rate__b:focus-visible')) shut();
    }, LEAVE_MS);
  });

  document.addEventListener('pointerover', function (e) {
    if (open && e.target.closest && e.target.closest('.watch-card') === open.card) clearTimeout(leaving);
  });

  document.addEventListener('pointerdown', function (e) {
    clearTimeout(dwell);
    if (open && e.target.closest && !open.card.contains(e.target) && !e.target.closest('.watch-keep-menu')) shut();
  }, true);

  document.addEventListener('focusout', function (e) {
    // A press on what takes no focus has none to say; neither has a window left.
    if (open && e.relatedTarget && open.card.contains(e.target) && !open.card.contains(e.relatedTarget) &&
        !e.relatedTarget.closest('.watch-keep-menu')) shut();
  });

  document.addEventListener('click', function (e) {
    var thumb = thumbAt(e.target);
    if (thumb) {
      if (slid) { slid = false; return; }
      var card = thumb.closest('.watch-card');
      if (!open || open.card !== card) openOn(card, e.detail === 0);
      else if (e.detail === 0 || performance.now() - open.opened > OWN_CLICK_MS) shut(e.detail === 0);
    } else if (open && e.target.classList && e.target.classList.contains('card-over__scrim')) shut();
  });

  /* ── A finger ── */

  document.addEventListener('pointerdown', function (e) {
    var thumb = e.pointerType !== 'mouse' && e.isPrimary !== false && thumbAt(e.target);
    var card = thumb && thumb.closest('.watch-card');
    if (!card || (open && open.card === card)) return;
    var y = e.clientY;
    var sliding = false;
    var on = null;
    var held = setTimeout(start, HELD_MS);

    function start() {
      clearTimeout(held);
      sliding = true;
      openOn(card);
    }

    function move(ev) {
      if (ev.pointerId !== e.pointerId) return;
      if (!sliding && Math.abs(ev.clientY - y) <= SLIDE_PX) return;
      if (!sliding) start();
      if (!open || open.card !== card) return;
      var hit = toArray(open.set.node.querySelectorAll('.rate__b')).filter(function (b) {
        var r = b.getBoundingClientRect();
        return ev.clientY >= r.top && ev.clientY < r.bottom && ev.clientX > r.left - 44 && ev.clientX < r.right + 44;
      })[0] || null;
      if (hit !== on) open.set.pointAt(hit);
      on = hit;
    }

    function end(ev) {
      if (ev.pointerId !== e.pointerId) return;
      clearTimeout(held);
      ['pointermove', 'pointerup', 'pointercancel'].forEach(function (name) { window.removeEventListener(name, name === 'pointermove' ? move : end); });
      if (!sliding) return;
      slid = ev.type === 'pointerup';
      setTimeout(function () { slid = false; }, 60);
      if (slid && on && open && open.card === card) on.click();
    }

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  });

  document.addEventListener('contextmenu', function (e) { if (!mouse() && thumbAt(e.target)) e.preventDefault(); });

  /* ── Keys ──
     Left and right go between a card's buttons, up to the card and down to its Keep. Before the
     row's, the grid's and Details' own keys. */

  document.addEventListener('keydown', function (e) {
    if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey || !e.target || !e.target.closest) return;
    if (open && e.key === 'Escape') {
      e.preventDefault();
      e.stopImmediatePropagation();
      shut(open.card.contains(document.activeElement));
      return;
    }
    if (open && e.key === 'Tab' && open.over.contains(e.target)) {
      shut(true); // Tab goes on from the thumb
      return;
    }
    var tool = e.target.closest('.card-tool');
    if (!tool || e.shiftKey) return;
    var card = tool.closest('.watch-card');
    var tools = toArray(card.querySelectorAll('.card-tool')).filter(function (b) { return b.getClientRects().length > 0; });
    var i = tools.indexOf(tool);
    // They stand from the right to the left.
    var to = e.key === 'ArrowLeft' ? tools[(i + 1) % tools.length]
      : e.key === 'ArrowRight' ? tools[(i - 1 + tools.length) % tools.length]
      : e.key === 'ArrowUp' ? card.querySelector('.watch-card__link')
      : e.key === 'ArrowDown' ? card.querySelector('.watch-keep') : undefined;
    if (to === undefined || to === tool) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (to) to.focus({ preventScroll: true });
  }, true);

  adoptAll(document.body);

  window.MMWatch.cardTools = {
    close: function (card) { if (open && (!card || open.card === card)) shut(); },
    faceOf: faceOf,
    SHOW_MS: SHOW_MS
  };
})();
