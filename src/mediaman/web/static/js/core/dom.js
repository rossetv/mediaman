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
 *   MM.dom.focusStillIn(el)
 *     True while focus is inside el or nowhere in particular. Used after a
 *     slow request so a user who has moved on keeps their place.
 *
 * No external dependencies. Safe to load in any order relative to other
 * core modules — no cross-module calls.
 */
(function (global) {
  'use strict';

  global.MM = global.MM || {};

  var SVG_NS = 'http://www.w3.org/2000/svg';

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

    /** True while focus is inside el, or on the body (nowhere in particular). */
    focusStillIn: function (el) {
      var active = document.activeElement;
      return !active || active === document.body || el.contains(active);
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
