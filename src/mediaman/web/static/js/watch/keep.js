/* Watch: the Keep menu, Keep and Undo, and the motion of a kept card leaving
 * its row. Needs watch/core.js.
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var byId = core.byId;
  var toArray = core.toArray;
  var reduceMotion = core.reduceMotion;
  var token = core.token;
  var tokenMs = core.tokenMs;
  var cardExit = core.cardExit;
  var clearArea = core.clearArea;
  var focusSoftly = core.focusSoftly;
  var toast = core.toast;

  var KEEP_URL = '/api/watch/keep';
  var FOREVER = 'forever';
  var UNKEEP_URL = '/api/watch/unkeep';
  var EXIT_FALLBACK_MS = 400; // past the slowest motion token, for a transition that never runs

  /* How a card leaves its row (sideways) or a row leaves the page
     (downwards), gap included, so nothing beside it jumps. A card fades and
     shrinks in place while its slot closes behind it. */
  function shrinkMotion(node, sideways) {
    var parentStyle = getComputedStyle(node.parentNode);
    var ease = token('--ease-out', 'ease-out');
    if (sideways) {
      var w = node.offsetWidth + 'px';
      var gap = parseFloat(parentStyle.columnGap) || 0;
      var t = cardExit();
      var open = { flexBasis: w, width: w, marginRight: '0px' };
      // Each property eases only between the keyframes that name it, so the
      // fade and the close overlap.
      return {
        duration: t.total,
        frames: [
          Object.assign({ offset: 0, opacity: 1, transform: 'none', easing: ease }, open),
          Object.assign({ offset: t.closeAt / t.total, easing: ease }, open),
          { offset: t.fade / t.total, opacity: 0, transform: 'scale(.92)', easing: ease },
          { offset: 1, opacity: 0, transform: 'scale(.92)', flexBasis: '0px', width: '0px', marginRight: -gap + 'px', easing: ease }
        ]
      };
    }
    var h = node.offsetHeight + 'px';
    var rowGap = parseFloat(parentStyle.rowGap) || 0;
    var side = node.previousElementSibling ? 'marginTop' : 'marginBottom';
    var from = { offset: 0, height: h, opacity: 1, easing: ease };
    var to = { offset: 1, height: '0px', opacity: 0, easing: ease };
    from[side] = '0px';
    to[side] = -rowGap + 'px';
    return { duration: tokenMs('--dur-slow', 250), frames: [from, to] };
  }

  /* While a card's slot opens or closes, its poster, title and Keep keep
     their own widths and are clipped, never squeezed. Returns the undo. */
  function pinContent(node, sideways) {
    if (!sideways) return noop;
    var kids = toArray(node.children);
    kids.forEach(function (kid) { kid.style.minWidth = kid.offsetWidth + 'px'; });
    return function () { kids.forEach(function (kid) { kid.style.minWidth = ''; }); };
  }

  /* Shrink *node* away, then take it off the page. Returns a cancel function
     that leaves it where it is (an Undo pressed mid-way). */
  function collapseAway(node, sideways) {
    if (reduceMotion() || !node.animate) {
      node.remove();
      return function () {};
    }
    var motion = shrinkMotion(node, sideways);
    var unpin = pinContent(node, sideways);
    node.style.overflow = 'hidden';
    var anim = node.animate(motion.frames, { duration: motion.duration, fill: 'forwards' });
    anim.onfinish = function () {
      node.remove();
      anim.cancel();
      node.style.overflow = '';
      unpin();
    };
    return function () {
      anim.onfinish = null;
      anim.cancel();
      node.style.overflow = '';
      unpin();
    };
  }

  /* Grow a node that was just put back from nothing to its own size: the
     same motion played backwards. *done*: called once it has its size. */
  function growIn(node, sideways, done) {
    if (reduceMotion() || !node.animate) return;
    var motion = shrinkMotion(node, sideways);
    var frames = motion.frames.reverse().map(function (f) {
      return Object.assign({}, f, { offset: 1 - f.offset });
    });
    var unpin = pinContent(node, sideways);
    node.style.overflow = 'hidden';
    var anim = node.animate(frames, { duration: motion.duration });
    anim.onfinish = anim.oncancel = function () {
      node.style.overflow = '';
      unpin();
      if (done) done();
    };
  }

  /* The same for a card of *row*: its See all is fitted to the cards' width,
     which the new one has only at the end. */
  function growCard(card, row) {
    growIn(card, true, function () {
      if (window.MMWatch.expand) window.MMWatch.expand.row(row);
    });
  }

  /* Where a node is: its parent and every node after it, so it can go back
     before the first of those still there, whatever else has gone since. */
  function spotOf(node) {
    var after = [];
    for (var n = node.nextSibling; n; n = n.nextSibling) after.push(n);
    return { node: node, parent: node.parentNode, after: after };
  }

  function putBack(spot) {
    var next = null;
    for (var i = 0; i < spot.after.length && !next; i++) {
      if (spot.after[i].parentNode === spot.parent) next = spot.after[i];
    }
    spot.parent.insertBefore(spot.node, next);
  }

  function takeOut(node) {
    var spot = spotOf(node);
    node.remove();
    return spot;
  }

  function noop() {}

  /* ── Menus ──
     The one small menu of the page: Keep for N days, and the batch of picks
     on Recommended. Fixed under its trigger, so a scrolling row never clips
     it; arrow keys, Home, End, Esc and Tab; it follows its trigger through
     any scroll and closes once the trigger is out of sight. */

  /* Whether the trigger can still be seen: on the screen, and inside the
     visible part of its row or list. */
  function inView(trigger) {
    if (!document.body.contains(trigger)) return false;
    var r = trigger.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= window.innerHeight || r.right <= 0 || r.left >= window.innerWidth) {
      return false;
    }
    var box = trigger.closest('.watch-row__scroll, .space-pane');
    if (!box) return true;
    var b = box.getBoundingClientRect();
    return r.right > b.left && r.left < b.right && r.bottom > b.top && r.top < b.bottom;
  }

  /* *opts*: trigger (selector), choose(trigger, item), canOpen(trigger),
     fill(trigger) (what the menu holds for it), spare() (where focus goes when the trigger has left the page). */
  function makeMenu(node, opts) {
    var openTrigger = null;
    var hideTimer = null;

    function items() {
      return toArray(node.querySelectorAll('.watch-keep-menu__item'));
    }

    /* Open below the trigger, or above it when that is where the room is,
       and stay clear of the sticky top bar and the phone's bottom tab bar.
       A menu taller than the room scrolls inside it. Right-aligned to a
       trigger at the right of the screen. */
    function place(trigger) {
      var gap = 6;
      var edge = 8;
      var rect = trigger.getBoundingClientRect();
      var area = clearArea();
      var scrolled = node.scrollTop;
      node.style.maxHeight = '';
      var w = node.offsetWidth;
      var h = node.offsetHeight;
      var below = area.bottom - edge - (rect.bottom + gap);
      var room = rect.top - gap - (area.top + edge);
      var above = h > below && room > below;
      var fits = Math.max(0, Math.floor(above ? room : below));
      if (h > fits) {
        h = fits;
        node.style.maxHeight = fits + 'px';
        node.scrollTop = scrolled;
      }
      var top = above ? rect.top - gap - h : rect.bottom + gap;
      var at = trigger.closest('.space-pane') ? rect.right - w : rect.left;
      var left = Math.max(edge, Math.min(at, window.innerWidth - w - edge));
      node.classList.toggle('is-above', above);
      node.style.top = top + 'px';
      node.style.left = left + 'px';
    }

    /* Focus *item* and scroll the menu, not the page, just enough to show it. */
    function focusItem(item) {
      item.focus({ preventScroll: true });
      var box = node.getBoundingClientRect();
      var inset = (parseFloat(getComputedStyle(node).paddingTop) + node.clientTop) || 0;
      var r = item.getBoundingClientRect();
      if (r.top < box.top + inset) node.scrollTop -= box.top + inset - r.top;
      else if (r.bottom > box.bottom - inset) node.scrollTop += r.bottom - (box.bottom - inset);
    }

    function close(refocus) {
      if (!openTrigger) return;
      var trigger = openTrigger;
      openTrigger = null;
      trigger.setAttribute('aria-expanded', 'false');
      if (refocus && document.body.contains(trigger)) {
        trigger.focus();
      } else if (node.contains(document.activeElement)) {
        // Closed without a press (scrolled away, swapped out): focus must not stay on a hidden item.
        var spare = document.body.contains(trigger) ? trigger : (opts.spare && opts.spare());
        if (spare) spare.focus({ preventScroll: true }); else document.activeElement.blur();
      }
      node.classList.remove('is-open');
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () { node.hidden = true; }, reduceMotion() ? 0 : EXIT_FALLBACK_MS);
    }

    function open(trigger) {
      close(false);
      clearTimeout(hideTimer);
      openTrigger = trigger;
      if (opts.fill) opts.fill(trigger);
      node.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
      place(trigger);
      void node.offsetWidth; // start the entrance from the closed look
      node.classList.add('is-open');
      var list = items();
      var first = list.filter(function (i) { return i.getAttribute('aria-checked') === 'true'; })[0] || list[0];
      if (first) focusItem(first);
    }

    document.addEventListener('click', function (e) {
      var target = e.target;
      if (!target.closest) return;
      var trigger = target.closest(opts.trigger);
      if (trigger) {
        if (opts.canOpen && !opts.canOpen(trigger)) return;
        if (openTrigger === trigger) close(false); else open(trigger);
        return;
      }
      var item = target.closest('.watch-keep-menu__item');
      if (item && openTrigger && node.contains(item)) {
        var chosen = openTrigger;
        close(true);
        opts.choose(chosen, item);
        return;
      }
      close(false);
    });

    document.addEventListener('keydown', function (e) {
      if (!openTrigger) return;
      if (e.key === 'Escape') { e.preventDefault(); close(true); return; }
      // Back on the trigger first, so Tab carries on from there.
      if (e.key === 'Tab') { close(true); return; }
      var list = items();
      var idx = list.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') focusItem(list[(idx + 1) % list.length]);
      else if (e.key === 'ArrowUp') focusItem(list[(idx - 1 + list.length) % list.length]);
      else if (e.key === 'Home') focusItem(list[0]);
      else if (e.key === 'End') focusItem(list[list.length - 1]);
      else return;
      e.preventDefault();
    });

    var queued = false;
    function follow(e) {
      if (!openTrigger || queued || (e && e.target === node)) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        if (!openTrigger) return;
        if (inView(openTrigger)) place(openTrigger); else close(false);
      });
    }
    window.addEventListener('resize', follow);
    document.addEventListener('scroll', follow, { capture: true, passive: true });

    return {
      node: node,
      open: open,
      close: close,
      trigger: function () { return openTrigger; }
    };
  }

  var menu = byId('watch-keep-menu');

  /* ── Keep ── */

  var busy = {};
  var afterBusy = {}; // an Undo pressed while its title was busy, run once it is not

  function triggersFor(id) {
    return toArray(document.querySelectorAll('[data-watch-keep]')).filter(function (t) {
      return t.getAttribute('data-watch-keep') === id;
    });
  }

  function cardsFor(id) {
    return toArray(document.querySelectorAll('.watch-card[data-keep-id]')).filter(function (c) {
      return c.getAttribute('data-keep-id') === id && !c.classList.contains('is-kept');
    });
  }

  /* One Keep or Undo at a time per title, whichever button it came from. The
     buttons stay focusable (aria-disabled, not disabled) so focus never drops. */
  function setBusy(id, on) {
    busy[id] = on;
    triggersFor(id).forEach(function (t) {
      if (on) t.setAttribute('aria-disabled', 'true'); else t.removeAttribute('aria-disabled');
      t.setAttribute('aria-busy', on ? 'true' : 'false');
    });
    var next = !on && afterBusy[id];
    if (next) {
      delete afterBusy[id];
      next();
    }
  }

  /* Where focus goes when a kept card leaves its row: the next card's Keep,
     else the one before's, so a run of Keeps needs no Tab between them; when
     the row goes, the next row, else the search box. *control*: the button
     that was pressed, as the other cards have it. */
  function focusAfterCard(card, control) {
    var sibling = card.nextElementSibling;
    while (sibling && sibling.classList.contains('is-kept')) sibling = sibling.nextElementSibling;
    if (!sibling) {
      sibling = card.previousElementSibling;
      while (sibling && sibling.classList.contains('is-kept')) sibling = sibling.previousElementSibling;
    }
    return sibling ? sibling.querySelector(control) || sibling.querySelector('.watch-card__link') : null;
  }

  function focusAfterRow(row) {
    var other = row.nextElementSibling || row.previousElementSibling;
    return (other && other.querySelector('.watch-row__scroll')) || byId('watch-search');
  }

  /* A kept Leaving soon card waits on the cancel of its own collapse. The one
     that emptied its row stays in it (holdsRow), and the row waits on its
     spot and the cancel of its collapse, until an Undo brings it back. */
  var keptCards = new WeakMap();
  var goneRows = new WeakMap();

  function away(node, sideways, shown) {
    if (shown) return collapseAway(node, sideways);
    node.remove();
    return noop;
  }

  /* Shrink a kept (or dismissed) card out of its row, or the row out of the
     page when it was the last. Behind search results the row is hidden: it
     just goes. Returns { undo, focus } as applyKept does. */
  function keepFromRow(card, row, shown, control) {
    // In a row opened as a grid, the grid closes up around it.
    var grid = window.MMWatch.grid;
    var taken = grid && grid.take && grid.take(card, control);
    if (taken) return taken;
    var scroller = card.parentNode;
    var left = toArray(scroller.children).filter(function (c) {
      return c !== card && !c.classList.contains('is-kept');
    });
    var focus = left.length ? focusAfterCard(card, control) : focusAfterRow(row);
    var spot = spotOf(card);
    var state = { cancel: noop, holdsRow: !left.length };
    card.classList.add('is-kept');
    card.inert = true;
    keptCards.set(card, state);
    if (left.length) {
      state.cancel = away(card, true, shown);
    } else {
      goneRows.set(row, { spot: spotOf(row), cancel: away(row, false, shown) });
    }
    return {
      focus: focus,
      undo: function () {
        var own = keptCards.get(card);
        if (own) own.cancel();
        keptCards.delete(card);
        card.classList.remove('is-kept');
        card.inert = false;
        var rowBack = false;
        var gone = goneRows.get(row);
        if (gone) {
          goneRows.delete(row);
          gone.cancel();
          if (!row.parentNode) { putBack(gone.spot); rowBack = true; }
          // A kept card that emptied the row is still in it: it goes now,
          // at once when the row is only now coming back.
          toArray(scroller.children).forEach(function (c) {
            var other = keptCards.get(c);
            if (c === card || !other || !other.holdsRow) return;
            other.holdsRow = false;
            other.cancel = away(c, true, !rowBack && c.getClientRects().length > 0);
          });
        }
        var cardBack = card.parentNode !== scroller;
        if (cardBack) putBack(spot);
        if (rowBack) {
          if (row.getClientRects().length) growIn(row, false);
        } else if (cardBack && card.getClientRects().length) {
          growCard(card, row);
        }
        var twin = grid && grid.bring && grid.bring(card);
        return (twin || card).querySelector(control);
      }
    };
  }

  /* Shrink *node* out of the page. Returns what puts it back. */
  function foldAway(node) {
    var spot = spotOf(node);
    var cancel = away(node, false, node.getClientRects().length > 0);
    return function () {
      cancel();
      if (node.parentNode) return;
      putBack(spot);
      growIn(node, false);
    };
  }

  function seasonShown() {
    var on = document.querySelector('.watch-seasons a.watch-pill.on');
    return on ? on.getAttribute('href') : null;
  }

  /* The title page's line, put back in the hero when the season tabs took
     it off and its season is the one shown again. */
  function putLineBack(line, season) {
    if (document.body.contains(line) || seasonShown() !== season) return;
    var info = document.querySelector('.watch-hero__info');
    if (!info) return;
    var current = info.querySelector('[data-watch-leaving]');
    var actions = info.querySelector('.watch-actions');
    if (current) current.replaceWith(line);
    else if (actions) actions.before(line);
    else info.appendChild(line);
  }

  function span(days) { return days === FOREVER ? FOREVER : 'for ' + days + ' days'; }

  /* The storage panel, which lists what is scheduled:
     kept(id, trigger) takes a title off its list, as { undo, focus } or null;
     started() and settled(saved) go round each request. */
  var panel = null;

  /* Take what a Keep ends off the page. Returns { undo, focus }: undo puts it
     all back (and returns the Keep button to focus, one that is shown), focus
     is where focus should go now. */
  function applyKept(id, days, trigger) {
    var undos = [];
    var focus = null;

    var listed = panel && panel.kept(id, trigger);
    if (listed) {
      undos.push(listed.undo);
      focus = listed.focus;
    }

    // A title page's "Leaving in N days · Keep" line says how long it is kept.
    triggersFor(id).forEach(function (button) {
      var line = button.closest('[data-watch-leaving]');
      if (!line) return;
      var parts = toArray(line.childNodes);
      var season = seasonShown();
      parts.forEach(function (n) { n.remove(); });
      line.textContent = 'Kept ' + span(days);
      line.classList.add('watch-leaving--kept');
      line.tabIndex = -1;
      focus = line;
      undos.push(function () {
        line.textContent = '';
        line.classList.remove('watch-leaving--kept');
        parts.forEach(function (n) { line.appendChild(n); });
        line.removeAttribute('tabindex');
        putLineBack(line, season);
        return button;
      });
    });

    // The card the Keep was pressed in comes first, so focus stays there; an open
    // grid's, the rows hidden behind it, last, so an Undo has it back before the row's.
    var own = trigger && trigger.closest('.watch-card');
    function rank(card) { return card.closest('.watch-grid') ? 1 : card === own ? -1 : 0; }
    cardsFor(id).sort(function (a, b) { return rank(a) - rank(b); }).forEach(function (card) {
      var shown = card.getClientRects().length > 0;
      var row = card.closest('[data-watch-leaving-row]');
      if (row) {
        var taken = keepFromRow(card, row, shown, '[data-watch-keep]');
        undos.push(taken.undo);
        if (!focus && shown) focus = taken.focus;
        return;
      }
      // Anywhere else the card stays, without its leaving badge or Keep.
      var spots = toArray(card.querySelectorAll('.watch-badge--leaving, [data-watch-keep]')).map(takeOut);
      if (!focus && shown && spots.length) focus = card.querySelector('.watch-card__link');
      undos.push(function () {
        var back = null;
        spots.forEach(function (s) {
          putBack(s);
          if (s.node.hasAttribute('data-watch-keep')) back = s.node;
        });
        return back;
      });
    });

    return {
      focus: focus,
      undo: function () {
        var backs = undos.slice().reverse().map(function (fn) { return fn(); }).reverse().filter(Boolean);
        return backs.filter(function (b) { return b.getClientRects().length && !b.closest('[inert]'); })[0] || backs[0] || null;
      }
    };
  }

  /* Move focus on only when it would otherwise be lost: it was on the Keep
     button or the menu, or has already fallen to the page. */
  function focusLost(trigger) {
    var active = document.activeElement;
    return !active || active === document.body || active === trigger ||
      (menu && menu.contains(active)) || !document.body.contains(active);
  }

  /* The title a Keep button belongs to, for the toasts: its label reads "Keep <title>", with
     the season of a season's. The title page's line adds the season of the tab shown. */
  function titleOf(button) {
    var label = button.getAttribute('aria-label');
    var title = label ? label.replace(/^Keep /, '') : '';
    if (!title) return '';
    var season = '';
    if (button.closest('[data-watch-leaving]')) {
      var tab = document.querySelector('.watch-seasons a.watch-pill.on');
      if (tab) season = tab.textContent.trim();
    }
    return season ? title + ', ' + season : title;
  }

  /* Watch's own Keep can be undone. Forever, and a title whose day has come
     (data-keep-late), go by the admins' route, which has no Undo. */
  function canUndo(trigger, days) {
    return days !== FOREVER && !trigger.hasAttribute('data-keep-late');
  }

  function send(id, days, undoable) {
    if (undoable) return MM.api.post(KEEP_URL, { media_item_id: id, days: days });
    return MM.api.postForm('/api/media/' + encodeURIComponent(id) + '/keep',
      { duration: days === FOREVER ? FOREVER : days + ' days' });
  }

  function started(id) {
    setBusy(id, true);
    if (panel) panel.started();
  }

  function settled(id, saved) {
    setBusy(id, false);
    if (panel) panel.settled(saved);
    if (saved) document.dispatchEvent(new CustomEvent('mm:watch-kept', { detail: { id: id } }));
  }

  function moveTo(node) {
    if (!node.closest('.space-pane')) { focusSoftly(node); return; }
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: 'nearest', behavior: reduceMotion() ? 'auto' : 'smooth' });
  }

  function keep(trigger, days) {
    var id = trigger.getAttribute('data-watch-keep');
    if (busy[id]) return;
    var name = titleOf(trigger);
    var undoable = canUndo(trigger, days);
    var dialog = trigger.closest('.space__panel');
    started(id);
    send(id, days, undoable).then(function (data) {
      var moveFocus = focusLost(trigger);
      var done = applyKept(id, days, trigger);
      if (moveFocus && done.focus) moveTo(done.focus);
      if (data && data.deletion_running && data.message) {
        toast(data.message, { duration: 8000 });
      } else {
        toast('Kept ' + (name || 'this') + ' ' + span(days) + '.', {
          variant: 'success',
          dialog: dialog,
          action: undoable ? { label: 'Undo', onClick: function () { undoKeep(id, days, done, name); } } : null
        });
      }
      settled(id, true);
    }).catch(function (err) {
      toast(MM.api.messageOf(err, 'Could not keep ' + (name || 'this') + '. Try again.'), { variant: 'error' });
      settled(id, false);
    });
  }

  /* Undo puts the title back at once and asks the server after; if the
     server says no, the Keep stands and is taken off the page again. */
  function undoKeep(id, days, done, name) {
    // The toast is gone already, so an Undo pressed mid-way waits its turn.
    if (busy[id]) {
      afterBusy[id] = function () { undoKeep(id, days, done, name); };
      return;
    }
    var back = done.undo();
    started(id);
    var active = document.activeElement;
    if (back && (!active || active === document.body || active === done.focus ||
                 (active.closest && active.closest('.ui-toast')))) {
      back.focus();
    }
    MM.api.post(UNKEEP_URL, { media_item_id: id }).then(function () {
      toast('Keep undone' + (name ? ' for ' + name : ''), { variant: 'info' });
      settled(id, true);
    }).catch(function (err) {
      var trigger = back || triggersFor(id)[0];
      var moveFocus = trigger ? focusLost(trigger) : false;
      var again = applyKept(id, days, trigger);
      if (moveFocus && again.focus) moveTo(again.focus);
      toast(MM.api.messageOf(err, 'Could not undo' + (name ? ' for ' + name : '') + '. It stays kept for ' + days + ' days.'), { variant: 'error' });
      settled(id, false);
    });
  }

  var keepMenu = menu ? makeMenu(menu, {
    trigger: '[data-watch-keep]',
    canOpen: function (trigger) { return !busy[trigger.getAttribute('data-watch-keep')]; },
    choose: function (trigger, item) {
      var days = item.getAttribute('data-days');
      keep(trigger, days === FOREVER ? FOREVER : parseInt(days, 10));
    },
    spare: function () {
      return document.querySelector('.watch-seasons a.watch-pill.on, .watch-search__input, #watch-search');
    }
  }) : null;

  window.MMWatch.keep = {
    menu: makeMenu,
    fromRow: keepFromRow,
    foldAway: foldAway,
    growIn: growIn,
    growCard: growCard,
    closeMenu: function (refocus) { if (keepMenu) keepMenu.close(refocus); },
    isBusy: function (id) { return !!busy[id]; },
    joinPanel: function (joined) { panel = joined; }
  };
})();
