/**
 * History page — each row is a <details>: the summary line opens into the
 * full event details.
 *
 *  - Opening keeps `?event=<id>` in the address (replaceState, so Back does not
 *    step through every row) so a row can be linked to; closing drops it.
 *  - Closing plays the exit animation before the row folds; with reduced motion
 *    it folds at once. Opening is a CSS animation, which reduced motion shortens.
 *  - What lies below a row slides with it, over the same time as the row's own animation.
 *  - "Copy details" copies the labels and values the row shows.
 */
(function () {
  'use strict';

  var CLOSE_FALLBACK_MS = 220;

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function syncAddress(row) {
    var url = new URL(window.location.href);
    if (row.open) url.searchParams.set('event', row.dataset.event);
    else if (url.searchParams.get('event') === row.dataset.event) url.searchParams.delete('event');
    else return;
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  /* Slide what follows the row in the list, and the list on the page, along with its details. */
  function slideBelow(row, body, closing) {
    if (!body.animate || reduceMotion()) return [];
    var style = window.getComputedStyle(body);
    var away = 'translateY(' + -body.offsetHeight + 'px)';
    var below = [];
    [row, row.parentNode].forEach(function (from) {
      for (var n = from.nextElementSibling; n; n = n.nextElementSibling) below.push(n);
    });
    return below.map(function (n) {
      return n.animate({ transform: closing ? ['none', away] : [away, 'none'] }, {
        duration: parseFloat(style.animationDuration) * 1000,
        easing: style.animationTimingFunction,
        fill: closing ? 'forwards' : 'none',
        composite: 'add'
      });
    });
  }

  function fold(row) {
    var body = row.querySelector('.hist-body');
    var done = false;
    var slides = [];
    function finish() {
      if (done) return;
      done = true;
      row.classList.remove('is-closing');
      row.removeAttribute('open');
      slides.forEach(function (a) { a.cancel(); });
      syncAddress(row);
    }
    if (!body || reduceMotion()) return finish();
    row.classList.add('is-closing');
    slides = slideBelow(row, body, true);
    body.addEventListener('animationend', finish, { once: true });
    window.setTimeout(finish, CLOSE_FALLBACK_MS);
  }

  function detailsText(row) {
    return Array.prototype.map.call(row.querySelectorAll('.hist-field'), function (f) {
      return f.querySelector('dt').textContent + ': ' + f.querySelector('dd').textContent;
    }).join('\n');
  }

  /* The clipboard API needs a secure page; a Mediaman reached over plain http on a
     home network is not one, so fall back to a selected hidden textarea. */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.className = 'sr-only';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  function onCopy(btn) {
    var row = btn.closest('.hist');
    copyText(detailsText(row)).then(function () {
      btn.focus();
      if (window.UIFeedback) window.UIFeedback.toast('Details copied', { variant: 'success' });
    }, function () {
      btn.focus();
      if (window.UIFeedback) window.UIFeedback.error('Could not copy. Select the text and copy it by hand.');
    });
  }

  function init() {
    var list = document.querySelector('.card--hist');
    if (!list) return;

    list.addEventListener('click', function (e) {
      var copy = e.target.closest('[data-hist-copy]');
      if (copy) { onCopy(copy); return; }
      var sum = e.target.closest('.hist-sum');
      if (!sum) return;
      var row = sum.parentNode;
      if (row.open && !row.classList.contains('is-closing')) {
        e.preventDefault();
        fold(row);
      } else if (row.classList.contains('is-closing')) {
        e.preventDefault();
      } else {
        /* Open by the next frame, and not yet painted. */
        window.requestAnimationFrame(function () {
          if (row.open) slideBelow(row, row.querySelector('.hist-body'), false);
        });
      }
    });

    list.addEventListener('toggle', function (e) {
      if (e.target.open) syncAddress(e.target);
    }, true);

    var opened = document.querySelector('.hist[open]');
    if (opened && opened.scrollIntoView) {
      opened.scrollIntoView({ block: 'center', behavior: 'auto' });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
