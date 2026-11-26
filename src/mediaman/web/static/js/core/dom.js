/**
 * core/dom.js — lightweight DOM query and mutation helpers.
 *
 * Public surface (under the global MM.dom namespace):
 *
 *   MM.dom.q(selector, ctx?)
 *     querySelector against ctx (default: document).
 *     Returns the first matching element or null.
 *
 *   MM.dom.qa(selector, ctx?)
 *     querySelectorAll against ctx (default: document).
 *     Always returns a plain Array (never a NodeList).
 *
 *   MM.dom.setText(el, text)
 *     Sets el.textContent only when it differs from the current value.
 *     Guards against unnecessary reflows on hot paths.
 *
 *   MM.dom.findByAttr(container, attr, value)
 *     Returns the first descendant of container where
 *     getAttribute(attr) === value, or null if none found.
 *
 *   MM.dom.on(el, event, handler)
 *     Convenience wrapper for addEventListener. Returns a cleanup
 *     function that removes the listener when called.
 *
 *   MM.dom.delegate(container, event, selector, handler)
 *     Wires a single listener on container that fires handler(e, target)
 *     when e.target.closest(selector) matches inside container.
 *     Returns a cleanup function.
 *
 *   MM.dom.icon(name, className?)
 *     A new icon, the markup of the icon macro in _components.html.
 *     name is a symbol id from _icons.html; a name is always written whole
 *     in the source, never put together, so tests can match it to its symbol.
 *
 *   MM.dom.setIcon(svg, name)
 *     Shows another symbol in an icon.
 *
 *   MM.dom.fold(details, toggle)
 *     Makes a <details> (class fold-more) open and shut smoothly when its
 *     summary, toggle, is pressed; with reduced motion the browser does it.
 *
 *   MM.dom.focusStillIn(el)
 *     True while focus is inside el or nowhere in particular. Used after a
 *     slow request so a user who has moved on keeps their place.
 *
 *   MM.dom.inView(el)
 *     Whether some of el shows between the bars at the top and bottom.
 *
 *   MM.dom.stillAt(status, scope)
 *     Whether the reader is still where they pressed, so a late answer in
 *     status does not pull the page back.
 *
 *   MM.dom.refuse(field, reason)
 *     Puts the cursor in the field to put right and brings both it and the
 *     element with its reason into view.
 *
 *   MM.dom.holdPage()
 *     For a dialog that stops the page scrolling: keeps the room of the page's
 *     scrollbar. Returns the function that lets go.
 *
 * No external dependencies. Safe to load in any order relative to other
 * core modules — no cross-module calls.
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  var SVG_NS = 'http://www.w3.org/2000/svg';

  var holders = 0;     // dialogs holding the page
  var barHeld = false; // whether the page had a scrollbar to keep the width of

  global.MM.dom = {

    /** querySelector with optional context element. */
    q: function (selector, ctx) {
      return (ctx || document).querySelector(selector);
    },

    icon: function (name, className) {
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('class', className ? 'ico ' + className : 'ico');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      var use = document.createElementNS(SVG_NS, 'use');
      use.setAttribute('href', '#' + name);
      svg.appendChild(use);
      return svg;
    },

    setIcon: function (svg, name) {
      svg.querySelector('use').setAttribute('href', '#' + name);
    },

    // By steps of its height, as what is below it moves with it.
    fold: function (details, toggle) {
      var sizing = null;
      toggle.addEventListener('click', function (e) {
        if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        e.preventDefault();
        var opens = !details.open || details.classList.contains('is-shutting');
        var from = details.offsetHeight;
        if (sizing) sizing.cancel();
        details.open = true;
        details.classList.toggle('is-shutting', !opens);
        var to = opens ? details.offsetHeight : toggle.offsetHeight;
        var tokens = getComputedStyle(document.documentElement);
        var mine = sizing = details.animate(
          [{ height: from + 'px', overflowY: 'clip' }, { height: to + 'px', overflowY: 'clip' }],
          { duration: parseFloat(tokens.getPropertyValue('--dur-slow')) * 1000, easing: tokens.getPropertyValue('--ease-out').trim() }
        );
        mine.onfinish = function () {
          if (sizing !== mine) return;
          sizing = null;
          details.open = opens;
          details.classList.remove('is-shutting');
        };
      });
    },

    /** True while focus is inside el, or on the body (nowhere in particular). */
    focusStillIn: function (el) {
      var active = document.activeElement;
      return !active || active === document.body || el.contains(active);
    },

    inView: function (el) {
      var root = document.documentElement;
      var bars = getComputedStyle(root);
      var box = el.getBoundingClientRect();
      return box.bottom > (parseFloat(bars.scrollPaddingTop) || 0) &&
        box.top < root.clientHeight - (parseFloat(bars.scrollPaddingBottom) || 0);
    },

    // A button clicked in Safari takes no focus, what is around it does: that is not somewhere else.
    stillAt: function (status, scope) {
      var at = document.activeElement || document.body;
      return (scope.contains(at) || at.contains(scope)) && global.MM.dom.inView(status);
    },

    // Safari scrolls to a focused field only later, which would take the reason out of view again.
    refuse: function (field, reason) {
      field.focus({ preventScroll: true });
      field.scrollIntoView({ block: 'nearest' });
      reason.scrollIntoView({ block: 'nearest' });
    },

    /**
     * The page stops scrolling behind an open dialog (the rule in
     * _ui_feedback.css), which takes its scrollbar away: a page that had one
     * is marked (html.page-bar, with the bar's width in --page-bar) so the
     * styles keep that room and nothing moves sideways. It is measured by
     * the first holder, before the bar goes, and stays until the last has
     * let go.
     */
    holdPage: function () {
      var root = document.documentElement;
      if (!holders) {
        var bar = global.innerWidth - root.clientWidth;
        barHeld = bar > 0;
        if (barHeld) {
          root.style.setProperty('--page-bar', bar + 'px');
          root.classList.add('page-bar');
        }
      }
      holders += 1;
      var holding = true;
      return function () {
        if (!holding) return;
        holding = false;
        holders -= 1;
        if (!holders && barHeld) {
          root.style.removeProperty('--page-bar');
          root.classList.remove('page-bar');
        }
      };
    },

    /** querySelectorAll — always returns a plain Array. */
    qa: function (selector, ctx) {
      return Array.prototype.slice.call(
        (ctx || document).querySelectorAll(selector)
      );
    },

    /**
     * Write textContent only when it has changed, avoiding needless
     * layout invalidation on elements updated on every poll tick.
     */
    setText: function (el, text) {
      if (!el) return;
      var s = text == null ? '' : String(text);
      if (el.textContent !== s) el.textContent = s;
    },

    /**
     * Walk all descendants of container looking for the first element
     * where getAttribute(attr) === value.  Useful when a CSS selector
     * would require escaping (e.g. numeric data-* values).
     */
    findByAttr: function (container, attr, value) {
      if (!container) return null;
      var all = container.querySelectorAll('[' + attr + ']');
      for (var i = 0; i < all.length; i++) {
        if (all[i].getAttribute(attr) === value) return all[i];
      }
      return null;
    },

    /**
     * Convenience wrapper for addEventListener.
     * Returns a cleanup function that removes the listener.
     */
    on: function (el, event, handler) {
      if (!el) return function () {};
      el.addEventListener(event, handler);
      return function () { el.removeEventListener(event, handler); };
    },

    /**
     * Event delegation: attach a single listener on container that
     * fires handler(event, matchedTarget) when the event source matches
     * selector inside container.  Returns a cleanup function.
     */
    delegate: function (container, event, selector, handler) {
      if (!container) return function () {};
      function listener(e) {
        var target = e.target && e.target.closest
          ? e.target.closest(selector)
          : null;
        if (target && container.contains(target)) {
          handler(e, target);
        }
      }
      container.addEventListener(event, listener);
      return function () { container.removeEventListener(event, listener); };
    },

  };

})(window);
