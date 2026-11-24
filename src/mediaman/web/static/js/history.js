/**
 * History page — each row is a <details>: the summary line opens into the
 * full event details.
 *
 *  - Opening keeps `?event=<id>` in the address (replaceState, so Back does not
 *    step through every row) so a row can be linked to; closing drops it.
 *  - Closing plays the exit animation before the row folds; with reduced motion
 *    it folds at once. Opening is a CSS animation, which reduced motion shortens.
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

  function fold(row) {
    var body = row.querySelector('.hist-body');
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      row.classList.remove('is-closing');
      row.removeAttribute('open');
      syncAddress(row);
    }
    if (!body || reduceMotion()) return finish();
    row.classList.add('is-closing');
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
