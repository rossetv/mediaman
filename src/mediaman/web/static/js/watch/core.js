/* Watch: helpers the other modules under watch/ share, on window.MMWatch.core.
 * Load this first.
 */
(function () {
  'use strict';

  function byId(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function toArray(list) { return Array.prototype.slice.call(list); }

  /* Only same-site paths may become a link or image address. Browsers drop
     tabs and newlines from a URL, so "/\t/host" would become "//host". */
  function safePath(value) {
    return typeof value === 'string' && value.charAt(0) === '/' &&
      value.charAt(1) !== '/' && value.charAt(1) !== '\\' && !/[\t\n\r]/.test(value) ? value : '';
  }

  function toast(message, opts) {
    if (window.UIFeedback) window.UIFeedback.toast(message, opts);
  }

  /* The mark of something an AI chose: the icon and the words. */
  function aiLabel(text) {
    var label = el('span', 'ai-label');
    label.appendChild(window.MM.dom.icon('i-sparkles'));
    label.appendChild(document.createTextNode(text));
    return label;
  }

  /* ── Motion ── */

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function coarsePointer() {
    return window.matchMedia('(pointer: coarse)').matches;
  }

  function token(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function tokenMs(name, fallback) {
    var value = token(name, '');
    if (!value) return fallback;
    return /ms$/.test(value) ? parseFloat(value) : parseFloat(value) * 1000;
  }

  /* Ease a node that was just put on the page in (the shared .fade-in). */
  function fadeIn(node) {
    node.classList.remove('fade-in');
    void node.offsetWidth; // restart the animation when the node is reused
    node.classList.add('fade-in');
  }

  /* How long a kept card takes to leave its row: all of it within
     --dur-slow. It fades and shrinks for --dur, and its slot starts closing
     at 0.6 of the whole, so the cards after it have settled by the end. */
  function cardExit() {
    var total = tokenMs('--dur-slow', 250);
    var fade = Math.min(tokenMs('--dur', 200), total);
    return { fade: fade, closeAt: Math.round(total * 0.6), total: total };
  }

  /* The part of the window no sticky top bar or phone tab bar covers. */
  function clearArea() {
    var area = { top: 0, bottom: window.innerHeight };
    toArray(document.querySelectorAll('.nav-glass, .nav-topbar, .nav-tabs')).forEach(function (bar) {
      if (getComputedStyle(bar).display === 'none') return;
      var r = bar.getBoundingClientRect();
      if (bar.classList.contains('nav-tabs')) area.bottom = Math.min(area.bottom, r.top);
      else area.top = Math.max(area.top, r.bottom);
    });
    return area;
  }

  /* Focus a node without the instant jump focus() makes to show it; once
     whatever is collapsing has settled, scroll to it only if it is out of
     sight. */
  function focusSoftly(node) {
    node.focus({ preventScroll: true });
    var reduce = reduceMotion();
    setTimeout(function () {
      if (document.activeElement !== node) return;
      var edge = 8;
      var area = clearArea();
      var r = node.getBoundingClientRect();
      var by = 0;
      if (r.top < area.top) by = r.top - area.top - edge;
      else if (r.bottom > area.bottom) by = Math.min(r.bottom - area.bottom + edge, r.top - area.top - edge);
      if (by) window.scrollBy({ top: by, behavior: reduce ? 'auto' : 'smooth' });
    }, reduce ? 0 : Math.max(cardExit().total, tokenMs('--dur-slow', 250)));
  }

  /* A node put in another place starts its CSS animations again, so a view
     that has eased in must not carry .fade-in when a swap moves it: it would
     blink back to nothing. */
  function settle(node) {
    node.classList.remove('fade-in');
    toArray(node.querySelectorAll('.fade-in')).forEach(function (n) { n.classList.remove('fade-in'); });
  }

  /* The cross-fades still running, by the stack that holds them: the node
     fading in, the fade-outs, and who waits for the end. */
  var _fades = new WeakMap();

  /* End the cross-fade *stack* holds at once: every other node goes and
     *keep* (by default the node fading in, unless it was dropped) stands
     alone where the stack was. */
  function endFade(stack, keep) {
    var fade = _fades.get(stack);
    if (!fade) return;
    _fades.delete(stack);
    if (keep === undefined) keep = fade.live.parentNode === stack ? fade.live : null;
    toArray(stack.children).forEach(function (n) {
      if (n === keep) return;
      n.remove();
      n.removeAttribute('inert');
    });
    fade.anims.forEach(function (a) { a.cancel(); });
    if (keep) {
      keep.style.gridArea = stack.style.gridArea;
      settle(keep);
    }
    if (stack.parentNode) {
      // Moving a node takes the focus off what is in it.
      var active = document.activeElement;
      if (keep) stack.replaceWith(keep);
      else stack.remove();
      if (keep && keep.contains(active)) active.focus({ preventScroll: true });
    }
    fade.waiting.forEach(function (resolve) { resolve(); });
  }

  /* End at once the cross-fade *node* stands in, if any. */
  function endSwap(node) {
    if (node && node.parentNode) endFade(node.parentNode);
  }

  /* Swap *oldNode* for *newNode* in place: both sit in one grid cell while
     the old fades out and the new fades in, so nothing around them moves.
     The node fading out is inert, so nothing that looks for the view finds
     it. A swap of any node of a cross-fade still running joins it: all but
     the newest node fade out from where they are, and the end leaves the
     newest alone (or nothing, if it was dropped meanwhile). Returns a promise
     that settles once the old node is gone. */
  function swap(oldNode, newNode) {
    var stack = oldNode.parentNode;
    if (!stack) return Promise.resolve();
    var fade = _fades.get(stack);
    newNode.removeAttribute('inert');
    if (reduceMotion() || !oldNode.animate) {
      if (fade) endFade(stack, newNode);
      else oldNode.replaceWith(newNode);
      return Promise.resolve();
    }
    if (!fade) {
      settle(oldNode);
      stack = el('div');
      stack.style.display = 'grid';
      stack.style.gridArea = oldNode.style.gridArea;
      oldNode.replaceWith(stack);
      stack.appendChild(oldNode);
      oldNode.style.gridArea = '1 / 1';
      fade = { gen: 0, live: null, anims: [], waiting: [] };
      _fades.set(stack, fade);
    }
    stack.appendChild(newNode);
    newNode.style.gridArea = '1 / 1';
    var gen = ++fade.gen;
    fade.live = newNode;
    var opts = { duration: tokenMs('--dur', 200), easing: token('--ease-out', 'ease-out') };
    toArray(stack.children).forEach(function (n) {
      if (n === newNode) return;
      n.setAttribute('inert', '');
      var from = parseFloat(getComputedStyle(n).opacity);
      var out = n.animate([{ opacity: isNaN(from) ? 1 : from }, { opacity: 0 }], Object.assign({ fill: 'forwards' }, opts));
      fade.anims.push(out);
      out.onfinish = function () { if (fade.gen === gen) endFade(stack); };
    });
    newNode.animate([{ opacity: 0 }, { opacity: 1 }], opts);
    return new Promise(function (resolve) { fade.waiting.push(resolve); });
  }

  /* Take the places of *nodes* now; the function returned, called after the
     page has been rearranged, slides each from where it was to where it is. */
  function flip(nodes) {
    var first = toArray(nodes).map(function (node) {
      return { node: node, rect: node.getBoundingClientRect() };
    });
    return function play() {
      if (reduceMotion()) return;
      var opts = { duration: tokenMs('--dur', 200), easing: token('--ease-out', 'ease-out') };
      first.forEach(function (f) {
        if (!f.node.animate) return;
        var now = f.node.getBoundingClientRect();
        var dx = f.rect.left - now.left;
        var dy = f.rect.top - now.top;
        if (!dx && !dy) return;
        f.node.animate([
          { transform: 'translate(' + dx + 'px, ' + dy + 'px)' },
          { transform: 'none' }
        ], opts);
      });
    };
  }

  /* ── Watched marks ──
     The markup of _watch_card.html's mark macros; watch/marks.js works them.
     A mark is "<kind>/<key>": movie, episode, season or show, and its key. */

  var MARK_RE = /^(movie|episode|season|show)\/[0-9]{1,18}$/;

  function markOf(item) {
    var mark = item && item.mark_kind + '/' + item.mark_key;
    return mark && MARK_RE.test(mark) ? mark : '';
  }

  /* The mark on a poster. */
  function watchedBadge() {
    var badge = el('span', 'watch-badge watch-badge--watched');
    badge.appendChild(window.MM.dom.icon('i-eye'));
    badge.appendChild(el('span', 'sr-only', 'Watched'));
    return badge;
  }

  /* Give a card its mark and its More button; a title the library does not
     hold has neither. */
  function addMarks(card, poster, item) {
    var mark = markOf(item);
    if (!mark) return;
    card.setAttribute('data-mark', mark);
    if (item.watched) poster.appendChild(watchedBadge());
    var more = el('button', 'watch-more');
    more.type = 'button';
    more.setAttribute('data-watch-more', '');
    more.setAttribute('aria-haspopup', 'menu');
    more.setAttribute('aria-expanded', 'false');
    more.setAttribute('aria-label', 'More for ' + item.title);
    more.appendChild(window.MM.dom.icon('i-ellipsis'));
    card.appendChild(more);
  }

  function markAction(watched) { return watched ? 'Mark as not watched' : 'Mark as watched'; }

  function toggle(className, mark, watched, label, name) {
    var b = el('button', className);
    b.type = 'button';
    b.setAttribute('data-mark-toggle', mark);
    b.setAttribute('data-mark-name', name);
    b.setAttribute('aria-pressed', watched ? 'true' : 'false');
    b.setAttribute('aria-label', label);
    b.appendChild(window.MM.dom.icon('i-eye'));
    return b;
  }

  /* An episode's toggle: the eye, and the word once watched. */
  function markToggle(mark, watched, name) {
    var b = toggle('watch-mark', mark, watched, 'Mark ' + name + ' as watched', name);
    b.setAttribute('title', markAction(watched));
    b.appendChild(el('span', 'watch-mark__word', 'Watched'));
    return b;
  }

  /* A button toggle: both its labels are laid out, so its width never changes. */
  function markButton(className, mark, watched, name, label) {
    var b = toggle(className + ' watch-mark-btn', mark, watched, label, name);
    var faces = el('span', 'watch-mark-btn__faces');
    faces.appendChild(el('span', 'watch-mark-btn__off', label));
    faces.appendChild(el('span', 'watch-mark-btn__on', 'Watched'));
    b.appendChild(faces);
    return b;
  }

  window.MMWatch = window.MMWatch || {};
  window.MMWatch.core = {
    markOf: markOf,
    watchedBadge: watchedBadge,
    addMarks: addMarks,
    markAction: markAction,
    markToggle: markToggle,
    markButton: markButton,
    byId: byId,
    el: el,
    aiLabel: aiLabel,
    toArray: toArray,
    safePath: safePath,
    toast: toast,
    reduceMotion: reduceMotion,
    coarsePointer: coarsePointer,
    token: token,
    tokenMs: tokenMs,
    fadeIn: fadeIn,
    cardExit: cardExit,
    clearArea: clearArea,
    focusSoftly: focusSoftly,
    swap: swap,
    endSwap: endSwap,
    flip: flip
  };
})();
