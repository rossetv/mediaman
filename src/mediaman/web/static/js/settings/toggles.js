/**
 * settings/toggles.js — page-wide widget wiring.
 *
 * Responsibilities:
 *   - Toggle switches (`<span data-toggle>` mirrors to a hidden `<input>`)
 *   - Integration-card collapse/expand (`<[data-intg-toggle]>` headers, `.intg-fold` buttons)
 *   - Secret-reveal buttons (`<button.inp-reveal>` flips type=password)
 *   - The fold of what the AI recommendations send to OpenAI
 *
 * Cross-module dependencies:
 *   MM.dom.fold                     — opens and shuts that fold
 *   MM.settings.rows                — folds an integration card's body open and shut
 *   MM.settings.savebar.markDirty   — invoked when a toggle flips so the
 *                                     savebar appears.
 *
 * Exposes:
 *   MM.settings.toggles.init()
 *   MM.settings.toggles.sync()   — redraws switches from their inputs
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

  // Safari puts an old value back in a hidden input on the way back to the page, but not the switch drawn from it.
  function readSwitches() {
    document.querySelectorAll('.setg-pg [data-toggle]').forEach(function (node) {
      var input = document.getElementById(node.getAttribute('data-target'));
      if (!input) return;
      input.value = node.classList.contains('on')
        ? node.getAttribute('data-on-value') || 'true'
        : node.getAttribute('data-off-value') || 'false';
    });
  }

  // Redraw each switch from its hidden input, after Discard puts the values back.
  function sync() {
    document.querySelectorAll('.setg-pg [data-toggle]').forEach(function (node) {
      var input = document.getElementById(node.getAttribute('data-target'));
      if (!input) return;
      var on = input.value === (node.getAttribute('data-on-value') || 'true');
      node.classList.toggle('on', on);
      node.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  function wireIntegrationCards() {
    function toggleIntg(hd) {
      var card = hd.closest('.intg-card');
      var btn = hd.querySelector('.intg-fold');
      if (!card || !btn) return;
      var body = document.getElementById(btn.getAttribute('aria-controls'));
      var expanded = card.classList.toggle('is-collapsed');
      var isOpen = !expanded;
      btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      if (body) {
        if (isOpen) MM.settings.rows.open(body); else MM.settings.rows.close(body);
      }
    }
    // Each body folds like the inline forms, which need an inner wrapper to fold into.
    document.querySelectorAll('.setg-pg .intg-card-bd').forEach(function (body) {
      var inner = document.createElement('div');
      inner.className = 'setg-fold-inner';
      while (body.firstChild) inner.appendChild(body.firstChild);
      // The list's rows carry only the answer; Test sits at the foot of the open card.
      var card = body.closest('.intg-card');
      var test = card && card.querySelector('.intg-card-hd [data-test-service]');
      if (test) {
        var acts = document.createElement('div');
        acts.className = 'intg-acts';
        acts.appendChild(test);
        inner.appendChild(acts);
      }
      body.appendChild(inner);
      body.classList.add('setg-fold');
    });
    // The fold's button takes Enter and Space itself; its click comes up through the header.
    document.querySelectorAll('.setg-pg [data-intg-toggle]').forEach(function (hd) {
      hd.addEventListener('click', function (e) {
        if (e.target.closest('.btn-test')) return;
        if (e.target.closest('.conn'))     return;
        toggleIntg(hd);
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
        var word = hidden ? 'Hide' : 'Show';
        var name = input.getAttribute('aria-label');
        input.type = hidden ? 'text' : 'password';
        btn.textContent = word;
        if (name) btn.setAttribute('aria-label', word + ' ' + name);
      });
    });
  }

  function init() {
    wireToggleSwitches();
    readSwitches();
    window.addEventListener('pageshow', function () { if (!MM.you || MM.you.shown('settings')) readSwitches(); });
    wireIntegrationCards();
    wireSecretReveals();
    var more = document.getElementById('setg-ai-more');
    if (more) MM.dom.fold(more, document.getElementById('setg-ai-more-toggle'));
  }

  MM.settings.toggles = {
    init: init,
    sync: sync,
  };
})();
