/**
 * settings/toggles.js — page-wide widget wiring.
 *
 * Responsibilities:
 *   - Toggle switches (`<span data-toggle>` mirrors to a hidden `<input>`)
 *   - Integration-card collapse/expand (`<[data-intg-toggle]>` headers)
 *   - Secret-reveal buttons (`<button.inp-reveal>` flips type=password)
 *   - Rail scroll-spy (highlights the active section in `.setg-rail`)
 *
 * Cross-module dependencies:
 *   MM.settings.savebar.markDirty   — invoked when a toggle flips so the
 *                                     savebar appears.
 *
 * Exposes:
 *   MM.settings.toggles.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  function markDirty() {
    if (MM.settings.savebar && MM.settings.savebar.markDirty) {
      MM.settings.savebar.markDirty();
    }
  }

  function wireToggleSwitches() {
    function toggle(node) {
      var on = !node.classList.contains('on');
      node.classList.toggle('on', on);
      node.setAttribute('aria-checked', on ? 'true' : 'false');
      var target = node.getAttribute('data-target');
      if (target) {
        var input = document.getElementById(target);
        if (input) {
          var onVal = node.getAttribute('data-on-value') || 'true';
          var offVal = node.getAttribute('data-off-value') || 'false';
          input.value = on ? onVal : offVal;
        }
      }
      markDirty();
    }
    document.querySelectorAll('.setg-pg [data-toggle]').forEach(function (n) {
      n.addEventListener('click', function () { toggle(n); });
      n.addEventListener('keydown', function (e) {
        if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(n); }
      });
    });
  }

  function wireIntegrationCards() {
    function toggleIntg(hd) {
      var card = hd.closest('.intg-card');
      if (!card) return;
      var body = document.getElementById(hd.getAttribute('aria-controls'));
      var expanded = card.classList.toggle('is-collapsed');
      var isOpen = !expanded;
      hd.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      if (body) {
        if (isOpen) { body.removeAttribute('hidden'); }
        else        { body.setAttribute('hidden', ''); }
      }
    }
    document.querySelectorAll('.setg-pg [data-intg-toggle]').forEach(function (hd) {
      hd.addEventListener('click', function (e) {
        if (e.target.closest('.btn-test')) return;
        if (e.target.closest('.conn'))     return;
        toggleIntg(hd);
      });
      hd.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleIntg(hd);
        }
      });
    });
  }

  function wireSecretReveals() {
    document.querySelectorAll('.setg-pg .inp-reveal').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        var id = btn.getAttribute('data-reveal-target');
        var input = id ? document.getElementById(id) : btn.parentElement.querySelector('input');
        if (!input) return;
        var hidden = input.type === 'password';
        input.type = hidden ? 'text' : 'password';
        btn.textContent = hidden ? 'Hide' : 'Show';
      });
    });
  }

  function wireRailScrollSpy() {
    var rail = document.querySelector('.setg-rail');
    var railItems = document.querySelectorAll('.setg-rail-item');
    var blocks = document.querySelectorAll('.setg-block');
    var lastActiveHref = null;
    function syncRail() {
      if (!blocks.length) return;
      var pos = window.scrollY + 140;
      var current = blocks[0];
      blocks.forEach(function (b) { if (b.offsetTop <= pos) current = b; });
      var atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 4);
      if (atBottom) current = blocks[blocks.length - 1];
      var id = current ? '#' + current.id : '';
      var activeEl = null;
      railItems.forEach(function (r) {
        var on = r.getAttribute('href') === id;
        r.classList.toggle('on', on);
        if (on) activeEl = r;
      });
      if (activeEl && id !== lastActiveHref && rail && rail.scrollWidth > rail.clientWidth) {
        var target = activeEl.offsetLeft - (rail.clientWidth - activeEl.offsetWidth) / 2;
        rail.scrollTo({ left: Math.max(0, target), behavior: 'smooth' });
      }
      lastActiveHref = id;
    }
    window.addEventListener('scroll', syncRail, { passive: true });
    syncRail();
  }

  function init() {
    wireToggleSwitches();
    wireIntegrationCards();
    wireSecretReveals();
    wireRailScrollSpy();
  }

  MM.settings.toggles = {
    init: init,
  };
})();
