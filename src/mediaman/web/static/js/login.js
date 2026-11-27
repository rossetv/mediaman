/**
 * The sign-in page: makes room for a refusal's line, keeps a post that cannot go, and tells Watch
 * where the lights were, so its first frame is the one this page leaves.
 *
 * Loaded at the end of <body>, not deferred, so the room is made before the page is first drawn.
 */
(function () {
  'use strict';

  var ARRIVE = 'mm.arrive';
  var HOLD_MS = 1200; // a sign-in still unanswered this long says the library is on its way
  var d = document;
  var page = d.querySelector('.login-page--signin');
  if (!page) return;
  var card = page.querySelector('.login-card');
  var form = page.querySelector('.login-form');
  var down = d.getElementById('login-down');
  var pass = d.getElementById('password');
  var pbox = pass && pass.closest('.login-box');
  var said = d.getElementById('login-said');
  var hold = form.querySelector('.btn-hold');

  // A flag left by a sign-in that was refused, or that went elsewhere, would play Watch's arrival later.
  try { window.sessionStorage.removeItem(ARRIVE); } catch (e) { /* storage blocked */ }

  // A signed-out line is said once: the address goes back to the plain sign-in page.
  var query = new URLSearchParams(window.location.search);
  if (query.has('out')) {
    query.delete('out');
    var rest = query.toString();
    window.history.replaceState(window.history.state, '',
      window.location.pathname + (rest ? '?' + rest : '') + window.location.hash);
  }

  // Said once the page is up: words a live region is drawn with are not read out.
  var out = page.querySelector('.sub-out');
  if (out) {
    window.requestAnimationFrame(function () {
      out.appendChild(d.createTextNode(out.getAttribute('data-said')));
    });
  }

  // Lays the page out as *change* leaves it, and has the card and the form ease there from where they were.
  function makeRoom(change) {
    var c0 = card.getBoundingClientRect().top, f0 = form.getBoundingClientRect().top;
    change();
    var c1 = card.getBoundingClientRect().top, f1 = form.getBoundingClientRect().top;
    page.style.setProperty('--dc', (c0 - c1) + 'px');
    page.style.setProperty('--df', ((f0 - c0) - (f1 - c1)) + 'px');
    page.classList.remove('is-room');
    void page.offsetWidth; // restarts the motion when the line changes again
    page.classList.add('is-room');
  }

  var refused = page.querySelector('.login-msg:not(#login-down)');
  if (refused) {
    refused.hidden = true;
    makeRoom(function () { refused.hidden = false; });
  }

  if (pass && pbox) pass.addEventListener('input', function () { pbox.classList.remove('is-bad'); });

  // Only Watch, at the landing page, plays the arrival.
  function toWatch() {
    var next = query.get('next');
    return !next || next.split(/[?#]/)[0] === '/';
  }

  function leaveFlag() {
    var sky = Array.prototype.map.call(page.querySelectorAll('.mm-sky > i'), function (i) {
      return window.getComputedStyle(i).transform;
    });
    try {
      window.sessionStorage.setItem(ARRIVE, JSON.stringify({ t: Date.now(), sky: sky }));
    } catch (e) { /* storage blocked: Watch opens as on any load */ }
  }

  var holding = 0;
  // On the form itself, so it runs before submit-guard.js, which listens on the document.
  form.addEventListener('submit', function (e) {
    if (window.navigator.onLine === false) {
      // Nothing is sent, so the password stays in its field and the form is not marked busy.
      e.preventDefault();
      e.stopPropagation();
      var again = !down.hidden;
      makeRoom(function () {
        if (refused) refused.hidden = true;
        if (pbox) pbox.classList.remove('is-bad');
        down.hidden = false;
      });
      if (again) {
        // The same words, put in afresh, so the alert is read out again.
        var words = down.querySelector('span');
        window.requestAnimationFrame(function () { words.replaceWith(words.cloneNode(true)); });
      }
      if (pass) pass.focus();
      return;
    }
    if (form.hasAttribute('data-busy')) return;
    if (toWatch()) leaveFlag();
    form.removeAttribute('data-hold');
    if (said) said.textContent = '';
    window.clearTimeout(holding);
    holding = window.setTimeout(function () {
      if (!form.hasAttribute('data-busy') || !hold) return;
      form.setAttribute('data-hold', '');
      if (said) said.textContent = hold.textContent;
    }, HOLD_MS);
  });
})();
