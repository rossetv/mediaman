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

  /* ── Motion ── */

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
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

  /* How long a kept card takes to leave its row: it fades and shrinks for
     --dur, and its slot starts closing part-way through that. */
  function cardExit() {
    var fade = tokenMs('--dur', 200);
    var closeAt = Math.round(fade * 0.6);
    return { fade: fade, closeAt: closeAt, total: fade + tokenMs('--dur-fast', 150) };
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

  /* Swap *oldNode* for *newNode* in place: both sit in one grid cell while
     the old fades out and the new fades in, so nothing around them moves.
     Returns a promise that settles once the old node is gone. */
  function swap(oldNode, newNode) {
    var parent = oldNode.parentNode;
    if (reduceMotion() || !oldNode.animate || !parent) {
      oldNode.replaceWith(newNode);
      return Promise.resolve();
    }
    var stack = el('div');
    stack.style.display = 'grid';
    oldNode.replaceWith(stack);
    stack.appendChild(oldNode);
    stack.appendChild(newNode);
    oldNode.style.gridArea = newNode.style.gridArea = '1 / 1';
    var opts = { duration: tokenMs('--dur', 200), easing: token('--ease-out', 'ease-out') };
    var out = oldNode.animate([{ opacity: 1 }, { opacity: 0 }], Object.assign({ fill: 'forwards' }, opts));
    newNode.animate([{ opacity: 0 }, { opacity: 1 }], opts);
    return new Promise(function (resolve) {
      out.onfinish = function () {
        oldNode.remove();
        newNode.style.gridArea = '';
        if (stack.parentNode) stack.replaceWith(newNode);
        resolve();
      };
    });
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

  window.MMWatch = window.MMWatch || {};
  window.MMWatch.core = {
    byId: byId,
    el: el,
    toArray: toArray,
    safePath: safePath,
    toast: toast,
    reduceMotion: reduceMotion,
    token: token,
    tokenMs: tokenMs,
    fadeIn: fadeIn,
    cardExit: cardExit,
    clearArea: clearArea,
    focusSoftly: focusSoftly,
    swap: swap,
    flip: flip
  };
})();
