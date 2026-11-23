/**
 * Protected (kept items) page — client-side glue.
 *
 * Lives in an external file so the page-level CSP needs no
 * 'unsafe-inline'. The file is self-contained (no imports).
 *
 * Event delegation replaces inline onclick handlers. Values
 * are read from data-* attributes parsed via JSON to avoid any string
 * interpolation into JavaScript.
 */
(function () {
  'use strict';

  // Only an API error's message is meant for the reader; anything else is a bug.
  function userMessage(err) {
    return MM.api.messageOf(err, 'Try again.');
  }

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  /* Take the tile of a removed keep off the page: shrink it away, hand focus
     to the next tile (or the section heading), and say so when it was the
     last one. */
  function removeTile(btn) {
    var tile = btn.closest('.tile');
    if (!tile) return;
    var grid = tile.parentNode;
    var section = tile.closest('section');
    var hadFocus = tile.contains(document.activeElement);
    var next = tile.nextElementSibling || tile.previousElementSibling;
    var done = function () {
      tile.remove();
      if (hadFocus) {
        var go = next && next.querySelector('.btn');
        if (!go && section) { go = section.querySelector('h2'); if (go) go.tabIndex = -1; }
        if (go) go.focus();
      }
      if (grid && !grid.querySelector('.tile')) {
        var empty = document.createElement('div');
        empty.className = 'empty';
        var note = document.createElement('h3');
        note.textContent = 'Nothing left here';
        empty.appendChild(note);
        grid.replaceWith(empty);
      }
    };
    if (reduceMotion() || !tile.animate) { done(); return; }
    tile.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.96)' }],
      { duration: 200, easing: 'ease-out', fill: 'forwards' }).onfinish = done;
  }

  /* One request per button at a time; false when one is already running. */
  function begin(btn) {
    if (btn.getAttribute('aria-busy') === 'true') return false;
    btn.setAttribute('aria-busy', 'true');
    return true;
  }

  function end(btn) {
    btn.removeAttribute('aria-busy');
  }

  async function removeKeep(mediaItemId, btn) {
    if (!window.UIFeedback) return;
    var ok = await window.UIFeedback.confirm({
      title: 'Remove keep?',
      body: 'Without a keep, this item may be scheduled for deletion on the next scan.',
      confirmLabel: 'Remove keep',
      confirmVariant: 'danger'
    });
    if (!ok || !begin(btn)) return;
    MM.api.post('/api/media/' + encodeURIComponent(mediaItemId) + '/unprotect')
      .then(function () {
        window.UIFeedback.success('Keep removed');
        removeTile(btn);
      })
      .catch(function (err) {
        end(btn);
        window.UIFeedback.error("Couldn't remove keep. " + userMessage(err));
      });
  }

  async function removeShowKeep(showRatingKey, btn) {
    if (!window.UIFeedback) return;
    var ok = await window.UIFeedback.confirm({
      title: 'Stop keeping this show?',
      body: 'Season-level keeps stay in place. New seasons will no longer be automatically protected.',
      confirmLabel: 'Stop keeping show',
      confirmVariant: 'danger'
    });
    if (!ok || !begin(btn)) return;
    MM.api.post('/api/show/' + encodeURIComponent(showRatingKey) + '/remove')
      .then(function () {
        window.UIFeedback.success('Show no longer kept');
        removeTile(btn);
      })
      .catch(function (err) {
        end(btn);
        window.UIFeedback.error("Couldn't stop keeping show. " + userMessage(err));
      });
  }

  document.addEventListener('click', function (e) {
    var removeKeepBtn = e.target.closest('[data-action="remove-keep"]');
    if (removeKeepBtn) {
      var mid;
      try { mid = JSON.parse(removeKeepBtn.dataset.mediaId); } catch (_) { return; }
      removeKeep(mid, removeKeepBtn);
      return;
    }
    var removeShowBtn = e.target.closest('[data-action="remove-show-keep"]');
    if (removeShowBtn) {
      var rk;
      try { rk = JSON.parse(removeShowBtn.dataset.showRk); } catch (_) { return; }
      removeShowKeep(rk, removeShowBtn);
    }
  });
})();
