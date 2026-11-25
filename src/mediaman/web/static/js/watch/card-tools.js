/* Watch: the thumb and the eye under every card's poster. A mouse that rests on the thumb,
 * or a press, has the four levels rise up the poster's edge; on a touch screen the poster
 * becomes their panel, with the card's other buttons in it, and a finger may press, slide
 * and let go in one movement. The levels and their state are ratings.js's, so a title says
 * the same wherever it is drawn; the eye and Dismiss are watch/marks.js's.
 *
 *   close(card)      fold what *card*, or any card, has open
 *   faceOf(button, ref)  draw on a thumb button how the title is rated
 *   moveStop(card)   bring its buttons' Tab stop to the first there is
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

  var nearing = null; // sees the cards to be read again
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
    button.classList.toggle('is-double', Math.abs(level) === 2);
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
    var gone = ratings.canRate(refOf(card)) === false;
    var active = document.activeElement;
    var had = gone && !thumb.hidden && (active === thumb || (open && open.card === card && open.over.contains(active)));
    thumb.hidden = gone;
    moveStop(card);
    if (had) {
      if (open && open.card === card) shut();
      var next = toArray(card.querySelectorAll('.card-tool')).filter(function (b) { return !b.hidden && b.getClientRects().length > 0; })[0];
      (next || card.querySelector('.watch-card__link')).focus({ preventScroll: true });
    }
    thumb.setAttribute('aria-label', 'Rate ' + titleOf(card) + (level ? ': ' + nameOf(level) : ''));
  }

  /* The buttons' one Tab stop, where the card has one, is the first of them there is. */
  function moveStop(card) {
    var tools = toArray(card.querySelectorAll('.card-tool'));
    var first = tools.filter(function (b) { return !b.hidden; })[0];
    var stop = tools.filter(function (b) { return b.getAttribute('tabindex') !== '-1'; })[0];
    if (!first || !stop || first === stop) return;
    stop.setAttribute('tabindex', '-1');
    first.removeAttribute('tabindex');
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
      if (nearing) nearing.observe(card);
    }
    if (inSight && !card.closest('.watch-row__scroll')) watchFar(card);
    if (window.MMWatch.marks) window.MMWatch.marks.adopt(card);
  }

  /* A grid's card a screen or more out of sight hides its buttons. Even faded to nothing, those of
     thousands of cards are drawn, and make every frame under the mouse slow in WebKit. A row, whose
     cards out of sight are beside it, has too few to matter. */
  var MANY = 500; // below this, hiding them gains a few ms and takes them from a screen reader's list
  var gridCards = new Set();
  var outOfSight = new Set();

  // One that holds the focus keeps them, as a hidden button would lose it.
  function drawFar(card) {
    card.classList.toggle('is-far', gridCards.size > MANY && outOfSight.has(card) && !card.contains(document.activeElement));
  }

  /* The grid has grown past MANY, or shrunk back under it. */
  function counted(was) {
    if ((was > MANY) !== (gridCards.size > MANY)) outOfSight.forEach(drawFar);
  }

  var inSight = window.IntersectionObserver ? new IntersectionObserver(function (entries) {
    entries.forEach(function (n) {
      if (n.isIntersecting) outOfSight.delete(n.target);
      else outOfSight.add(n.target);
      drawFar(n.target);
    });
  }, { rootMargin: '100% 0px' }) : null;

  function watchFar(card) {
    var was = gridCards.size;
    inSight.observe(card);
    gridCards.add(card);
    counted(was);
  }

  function letGo(card) {
    var was = gridCards.size;
    inSight.unobserve(card);
    gridCards.delete(card);
    outOfSight.delete(card);
    card.classList.remove('is-far');
    counted(was);
  }

  function forget(root) {
    if (root.matches && root.matches('.watch-card')) letGo(root);
    toArray(root.querySelectorAll('.watch-card')).forEach(letGo);
  }

  /* Of a poster's badges Leaving is the first and Watched the last, which is how CSS tells what
     stands under what: one that has come out of its place is put there. */
  function order(badge) {
    var poster = badge.closest('.watch-card__poster');
    var first = badge.matches('.watch-badge--leaving');
    if (!poster || !(first || badge.matches('.watch-badge--state, .watch-badge--new'))) return;
    var after = toArray(poster.querySelectorAll(first ? '.watch-badge' : '.watch-badge--watched')).filter(function (b) { return b !== badge; })[0];
    var all = toArray(poster.children);
    if (after && all.indexOf(after) < all.indexOf(badge)) poster.insertBefore(badge, after);
  }

  function adoptAll(root) {
    if (root.matches && root.matches('.watch-card')) adopt(root);
    toArray(root.querySelectorAll('.watch-card')).forEach(adopt);
  }

  function ratedCards() { return toArray(document.querySelectorAll('.watch-card[data-tmdb-ref]')); }

  function paintAll(refs) {
    ratedCards().forEach(function (card) { if (refs.has(refOf(card))) paint(card); });
  }

  var told = null; // the refs of one answer, whose cards are looked up once

  document.addEventListener('mm:rating', function (e) {
    var d = e.detail;
    if (d.phase !== 'read') paintAll(new Set(d.refs));
    else if (told) d.refs.forEach(function (ref) { told.add(ref); });
    else {
      told = new Set(d.refs);
      Promise.resolve().then(function () {
        var all = told;
        told = null;
        paintAll(all);
      });
    }
    if (open && d.from === open.set.node && d.phase === 'pending') picked(d.level);
  });

  // A page the browser kept comes back with the levels it left with: each card's is read again
  // as it nears the screen.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    if (nearing) nearing.disconnect();
    nearing = new IntersectionObserver(function (entries, seer) {
      var near = entries.filter(function (n) { return n.isIntersecting; }).map(function (n) { return n.target; });
      near.forEach(function (card) { seer.unobserve(card); });
      ratings.read(near.map(refOf));
    }, { rootMargin: '50%' });
    ratedCards().forEach(function (card) { nearing.observe(card); });
  });

  new MutationObserver(function (changes) {
    changes.forEach(function (c) {
      if (inSight) toArray(c.removedNodes).forEach(function (n) { if (n.nodeType === 1 && !n.isConnected) forget(n); });
      toArray(c.addedNodes).forEach(function (n) { if (n.nodeType === 1 && n.isConnected) { order(n); adoptAll(n); } });
    });
    if (open && !open.card.isConnected) drop(open);
  }).observe(document.body, { childList: true, subtree: true });

  /* ── What a card's CSS asks of it ──
     :has() could say it, and would be asked of every card at every change of the page. */

  var says = {}; // by its class: the card that has it

  function say(name, card) {
    if (says[name] === card) return;
    if (says[name]) says[name].classList.remove(name);
    if (card) card.classList.add(name);
    says[name] = card;
  }

  function cardAround(node, selector) {
    var at = node && node.closest && node.closest(selector || '.watch-card');
    return at ? at.closest('.watch-card') : null;
  }

  /* The keyboard's focus is in it. */
  function sayFocus() {
    var active = document.activeElement;
    say('has-focus', active && active.matches(':focus-visible') ? cardAround(active) : null);
  }

  document.addEventListener('focusin', sayFocus);
  document.addEventListener('focusout', function (e) {
    var card = cardAround(e.target);
    if (card && outOfSight.has(card) && !(e.relatedTarget && card.contains(e.relatedTarget))) drawFar(card);
  });
  document.addEventListener('focusout', function (e) { if (cardAround(e.relatedTarget) !== says['has-focus']) say('has-focus', null); });
  // A key pressed makes the keyboard's a focus the mouse gave.
  document.addEventListener('keyup', sayFocus);

  /* Its buttons are under the mouse. */
  document.addEventListener('pointerover', function (e) { say('is-on-tools', cardAround(e.target, '.card-tools')); });
  document.addEventListener('pointerout', function (e) { if (!e.relatedTarget) say('is-on-tools', null); });

  /* With a mouse as well, a finger's or a pen's press on a button not in sight is nobody's: it shows
     that card's buttons, which a touch may leave no hover to show, for the next press. A press
     elsewhere, or a scroll, hides them again. The browser leaves the card a finger pressed under
     :hover, so until the mouse moves again the page is by touch, and :hover shows no buttons. */
  var unseen = null;
  var byTouch = false;

  document.addEventListener('pointerdown', function (e) {
    var tool = e.pointerType !== 'mouse' && mouse() && e.target.closest && e.target.closest('.card-tool');
    var card = tool && tool.closest('.watch-card');
    unseen = tool && card !== says['is-touched'] && Number(getComputedStyle(tool).opacity) < 0.5 ? tool : null;
    if (tool) say('is-touched', card);
    else if (says['is-touched'] && !says['is-touched'].contains(e.target)) say('is-touched', null);
    if (!byTouch && e.pointerType === 'touch' && mouse()) {
      byTouch = true;
      document.documentElement.classList.add('is-by-touch');
    }
  }, true);

  document.addEventListener('pointermove', function (e) {
    if ((!byTouch && !says['is-touched']) || e.pointerType !== 'mouse') return;
    byTouch = false;
    document.documentElement.classList.remove('is-by-touch');
    say('is-touched', null);
  }, { passive: true });

  document.addEventListener('click', function (e) {
    if (unseen && unseen.contains(e.target)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
    unseen = null;
  }, true);

  // A drag the browser took for a scroll ends with no click, and a key then presses for itself.
  ['pointercancel', 'keydown'].forEach(function (name) {
    document.addEventListener(name, function () { unseen = null; }, true);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Tab') say('is-touched', null); }, true);

  document.addEventListener('scroll', function () { say('is-touched', null); }, true);

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
    var column = el('div', 'card-over__col');
    toArray(card.querySelectorAll('.card-tool:not([data-card-rate])')).reverse().forEach(function (b) { column.appendChild(copyTool(b)); });
    var set = ratings.mount(refOf(card), { variant: 'lift', label: 'Your rating of ' + titleOf(card) });
    over.appendChild(set.node);
    over.appendChild(column);
    card.appendChild(over);
    open = { card: card, thumb: thumbOf(card), over: over, set: set, opened: performance.now(), timer: null };
    void over.offsetWidth; // so that it rises
    lifted(card, true);
    set.node.classList.add('is-open');
    open.thumb.setAttribute('aria-expanded', 'true');
    if (focus) set.enter();
    else if (!mouse()) {
      var given = set.node.querySelector('[aria-checked="true"]');
      if (given) set.pointAt(given);
    }
  }

  /* Its row's heading takes no press meant for it. */
  function lifted(card, on) {
    var row = card.closest('.watch-row');
    card.classList.toggle('is-lifted', on);
    if (row) row.classList.toggle('has-lifted', on);
  }

  function remove(o) {
    o.set.destroy();
    o.over.remove();
  }

  /* Its card has gone from the page. */
  function drop(o) {
    open = null;
    clearTimeout(o.timer);
    lifted(o.card, false);
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
    // It folds from its own edge, not from where the sparks went.
    var group = o.set.node.querySelector('.rate__set');
    toArray(group.querySelectorAll('.pop')).forEach(function (b) { b.classList.remove('pop'); });
    group.style.transition = 'none';
    void group.offsetWidth;
    group.style.transition = '';
    lifted(o.card, false);
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
    var flier = el('span', 'card-flier');
    ['is-up', 'is-down', 'is-double'].forEach(function (c) { flier.classList.toggle(c, o.thumb.classList.contains(c)); });
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
    var thumb = e.pointerType !== 'mouse' && e.isPrimary !== false && !unseen && thumbAt(e.target);
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
     Left and right go between a card's buttons, up to the card and down to its Keep. In a touch
     screen's panel Tab and the arrows also go from the levels to the buttons beside them and back.
     Before the row's, the grid's and Details' own keys. */

  /* Where *e* takes the focus in the open panel: null to leave it, undefined for the levels' own keys. */
  function inPanel(e) {
    var column = toArray(open.over.querySelectorAll('.card-over__b')).filter(function (b) { return b.getClientRects().length > 0; });
    var i = column.indexOf(e.target);
    var levels = { focus: open.set.enter };
    if (e.key === 'Tab') return i === 0 && e.shiftKey ? levels : column[e.shiftKey ? i - 1 : i + 1] || null;
    if (e.shiftKey || !column.length) return undefined;
    if (i < 0) return e.key === 'ArrowLeft' ? column[column.length - 1] : undefined; // the eye
    return { ArrowUp: column[(i - 1 + column.length) % column.length], ArrowDown: column[(i + 1) % column.length], ArrowLeft: levels, ArrowRight: levels }[e.key];
  }

  document.addEventListener('keydown', function (e) {
    if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey || !e.target || !e.target.closest) return;
    if (open && e.key === 'Escape' && !e.target.closest('.watch-keep-menu')) {
      e.preventDefault();
      e.stopImmediatePropagation();
      shut(open.card.contains(document.activeElement));
      return;
    }
    if (open && open.over.contains(e.target)) {
      var next = inPanel(e);
      if (next === null) shut(true); // Tab goes on from the thumb
      else if (next) {
        e.preventDefault();
        e.stopImmediatePropagation();
        open.set.pointAt(null);
        next.focus({ preventScroll: true });
      }
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
    moveStop: moveStop,
    SHOW_MS: SHOW_MS
  };
})();
