/**
 * Dashboard page — client-side glue.
 *
 * Lives in an external file so the page-level CSP needs no
 * 'unsafe-inline'. The file is self-contained (no imports).
 *
 * Wires up:
 *   - Scan trigger button (data-action="trigger-scan")
 *   - Re-download buttons   (data-action="redownload")
 *   - Clear-scheduled button (data-action="clear-scheduled")
 *   - Per-tile keep buttons  (data-action="keep-tile" + data-duration)
 *
 * Nothing reloads the page: the [data-fragment] parts are re-read from the
 * server and swapped in place.
 *
 * All values flow in via data-* attributes parsed as JSON; nothing is
 * interpolated server-side into JavaScript.
 */
(function () {
  'use strict';

  var _scanElapsed = 0;
  var _scanStart = 0;

  /* Delays after a failed status poll, then every 10s; stop after 5 in a row. */
  var POLL_RETRY_MS = [2000, 4000, 8000, 10000];
  var POLL_MAX_FAILS = 5;
  var POLL_MAX_MS = 10 * 60 * 1000;
  /* A hung status request is aborted and counts as one failed poll. Polls give
     up after POLL_MAX_FAILS in a row or POLL_MAX_MS of unbroken failure. */
  var POLL_TIMEOUT_MS = 10000;

  function reduceMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function motionToken(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function motion() {
    var dur = motionToken('--dur-slow', '.25s');
    return {
      duration: /ms$/.test(dur) ? parseFloat(dur) : parseFloat(dur) * 1000,
      easing: motionToken('--ease-out', 'ease-out')
    };
  }

  function tileRects(scope) {
    var rects = {};
    MM.dom.qa('.tile', scope).forEach(function (t) {
      var w = t.querySelector('[data-id]');
      if (w) rects[w.dataset.id] = t.getBoundingClientRect();
    });
    return rects;
  }

  /* Slide each tile that was on the page from where it was to where it is
     now. Returns how many tiles were already there. */
  function slideTiles(scope, before, m) {
    var kept = 0;
    MM.dom.qa('.tile', scope).forEach(function (t) {
      var w = t.querySelector('[data-id]');
      var was = w && before[w.dataset.id];
      if (!was) {
        t.animate([{ opacity: 0, transform: 'scale(.96)' }, { opacity: 1, transform: 'none' }], m);
        return;
      }
      kept += 1;
      var now = t.getBoundingClientRect();
      var dx = was.left - now.left;
      var dy = was.top - now.top;
      if (dx || dy) t.animate([{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }], m);
    });
    return kept;
  }

  /* Put a fragment's fresh copy in place. Tiles still listed slide to their
     new place; otherwise the old copy fades out over the new one. The height
     follows either way, so what is below never jumps in one frame. */
  function swapFragment(cur, node) {
    if (reduceMotion() || !cur.animate) { cur.replaceWith(node); return; }
    var m = motion();
    var from = cur.getBoundingClientRect().height;
    var before = tileRects(cur);
    cur.before(node);
    cur.removeAttribute('data-fragment');
    cur.setAttribute('aria-hidden', 'true');
    cur.inert = true;
    cur.style.display = 'none';
    var to = node.getBoundingClientRect().height;
    if (slideTiles(node, before, m) > 0) {
      cur.remove();
    } else {
      /* The old copy fades out inside the new one, so it moves with the page
         and the shrinking height clips it. */
      Array.prototype.forEach.call(node.children, function (c) {
        c.animate([{ opacity: 0 }, { opacity: 1 }], m);
      });
      node.style.position = 'relative';
      cur.style.cssText = 'position:absolute;margin:0;pointer-events:none;top:0;left:0;right:0';
      node.appendChild(cur);
      var at = cur.getBoundingClientRect();
      var box = node.getBoundingClientRect();
      /* A first child's margin collapses through the fragment in the flow but
         not in the pinned copy, so line the first children up when they match. */
      var a = cur.firstElementChild;
      var b = node.firstElementChild;
      var dy = a && b && a.tagName === b.tagName
        ? b.getBoundingClientRect().top - a.getBoundingClientRect().top
        : box.top - at.top;
      cur.style.top = dy + 'px';
      cur.style.left = (box.left - at.left) + 'px';
      cur.style.right = (at.right - box.right) + 'px';
      var fade = cur.animate([{ opacity: 1 }, { opacity: 0 }], { duration: m.duration, easing: m.easing, fill: 'forwards' });
      fade.onfinish = fade.oncancel = function () { cur.remove(); node.style.position = ''; };
    }
    if (Math.abs(from - to) < 1) return;
    node.style.overflow = 'hidden';
    var grow = node.animate([{ height: from + 'px' }, { height: to + 'px' }], m);
    grow.onfinish = grow.oncancel = function () { node.style.overflow = ''; };
  }

  /* Where focus sits in a fragment, told by what it is rather than which node,
     so it can be found again in the fresh copy. */
  function focusPlace(cur) {
    var el = document.activeElement;
    if (!el || el === document.body || !cur.contains(el)) return null;
    var sel = null;
    if (el.dataset.action) {
      sel = '[data-action="' + el.dataset.action + '"]' +
        (el.dataset.duration ? '[data-duration="' + el.dataset.duration + '"]' : '');
    } else if (el.id) {
      sel = '#' + el.id;
    } else if (el.classList.contains('btn-sm-caret')) {
      sel = '.btn-sm-caret';
    }
    var item = el.closest('[data-id]');
    return {
      sel: sel,
      id: item && item.dataset.id,
      index: sel ? MM.dom.qa(sel, cur).indexOf(el) : -1
    };
  }

  /* The same control in the fresh copy; the one now in its place when its
     own item has gone; the heading only when the list is empty. */
  function restoreFocus(node, place) {
    var target = null;
    if (place.sel) {
      var all = MM.dom.qa(place.sel, node).filter(function (e) { return !e.closest('[inert]'); });
      if (place.id) {
        target = all.filter(function (e) {
          var item = e.closest('[data-id]');
          return item && item.dataset.id === place.id;
        })[0] || null;
      }
      if (!target && all.length) target = all[Math.min(place.index, all.length - 1)];
    }
    if (target) {
      target.focus({ preventScroll: true });
      if (document.activeElement === target) return;
    }
    var heading = node.querySelector('h2');
    if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  }

  /* A refresh that finds the login page instead of the dashboard: the session
     has ended, so send the user to sign in rather than leave the page stale. */
  function signIn() {
    window.location.href = '/login';
    return new Promise(function () {});
  }

  /* Keeps in flight hold a refresh back until the last one settles, so a tile
     whose request has not finished is not put back by an earlier answer.
     keepGen moves whenever a keep starts or ends, and refreshSeq with each
     refresh: an answer read before either moved is out of date. */
  var keepsInFlight = 0;
  var keepGen = 0;
  var refreshSeq = 0;
  var refreshWaiting = false;

  /* Re-read the page and swap in the parts that change with a scan, a clear or
     a keep, so nothing reloads and the scroll position stays put. Resolves true
     when the page now shows the server's latest. */
  function refreshFragments() {
    if (keepsInFlight > 0) { refreshWaiting = true; return Promise.resolve(false); }
    refreshWaiting = false;
    var seq = ++refreshSeq;
    var gen = keepGen;
    return fetch(window.location.pathname, {
      credentials: 'same-origin',
      headers: { Accept: 'text/html' }
    }).then(function (res) {
      if (!res.ok) throw new Error('refresh failed');
      if (res.redirected && /\/login(?:[?#]|$)/.test(res.url)) return signIn();
      return res.text();
    }).then(function (html) {
      if (seq !== refreshSeq) return false;
      if (gen !== keepGen) return refreshFragments();
      var doc = new DOMParser().parseFromString(html, 'text/html');
      if (!doc.querySelector('[data-fragment]')) return signIn();
      MM.dom.qa('[data-fragment]').forEach(function (cur) {
        var fresh = doc.querySelector('[data-fragment="' + cur.dataset.fragment + '"]');
        if (!fresh || cur.isEqualNode(fresh)) return;
        var place = focusPlace(cur);
        var node = document.importNode(fresh, true);
        swapFragment(cur, node);
        if (place) restoreFocus(node, place);
      });
      return true;
    });
  }

  function keepStarted() {
    keepsInFlight += 1;
    keepGen += 1;
  }

  /* A saved keep refreshes once its tile has gone; a failed one runs the
     refresh it held back, if any. */
  function keepSettled(saved) {
    keepsInFlight -= 1;
    keepGen += 1;
    if (!saved && keepsInFlight === 0 && refreshWaiting) refreshFragments().catch(function () {});
  }

  /* How long "Finished in …" stays before it fades away. */
  var STATUS_LINGER_MS = 5000;
  var statusFade = { timer: 0, anim: null };

  function stopStatusFade() {
    clearTimeout(statusFade.timer);
    if (statusFade.anim) statusFade.anim.cancel();
    statusFade = { timer: 0, anim: null };
  }

  function fadeStatusLater(el) {
    stopStatusFade();
    statusFade.timer = setTimeout(function () {
      var clear = function () {
        statusFade.anim = null;
        el.textContent = '';
        el.classList.remove('scan-status--success');
      };
      if (reduceMotion() || !el.animate) { clear(); return; }
      var m = motion();
      statusFade.anim = el.animate([{ opacity: 1 }, { opacity: 0 }], m);
      statusFade.anim.onfinish = clear;
    }, STATUS_LINGER_MS);
  }

  function updateScanStatus(msg) {
    stopStatusFade();
    var el = document.getElementById('scan-status');
    if (!el) return;
    el.textContent = '';
    el.classList.remove('scan-status--error', 'scan-status--success');
    if (!msg) return;
    el.appendChild(document.createTextNode(msg + ' '));
    var dots = document.createElement('span');
    dots.className = 'scan-dots';
    for (var i = 0; i < 3; i++) {
      var d = document.createElement('span');
      d.textContent = '·';
      dots.appendChild(d);
    }
    el.appendChild(dots);
  }

  function fmtElapsed(sec) {
    return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm ' + (sec % 60) + 's';
  }

  /* The button was disabled for the scan, so focus fell to the body; say why it
     stopped in a toast too, since #scan-status is not a live region (its
     running clock would be read out every few seconds). */
  function scanStopped(btn, reason, label) {
    btn.classList.remove('scan-running');
    btn.textContent = label || 'Scan now';
    btn.disabled = false;
    if (window.UIFeedback) window.UIFeedback.error(reason);
    if (MM.dom.focusStillIn(btn)) btn.focus();
  }

  function pollScanStatus(btn) {
    var fails = 0;
    var firstFailAt = 0;
    setTimeout(function check() {
      _scanElapsed = Math.round((Date.now() - _scanStart) / 1000);
      var ctrl = new AbortController();
      var abortTimer = setTimeout(function () { ctrl.abort(); }, POLL_TIMEOUT_MS);
      MM.api.get('/api/scan/status', { signal: ctrl.signal })
        .then(function (data) {
          fails = 0;
          firstFailAt = 0;
          if (data.running) {
            updateScanStatus('Scanning libraries — ' + fmtElapsed(_scanElapsed));
            setTimeout(check, 3000);
          } else if (data.status === 'skipped') {
            var skipReason = !data.reason ? 'Scan skipped' : 'Scan skipped: ' +
              (data.reason === 'Plex not configured' ? 'Plex is not configured' : data.reason);
            scanStopped(btn, skipReason);
            var skipEl = document.getElementById('scan-status');
            if (skipEl) {
              skipEl.classList.remove('scan-status--error', 'scan-status--success');
              skipEl.textContent = skipReason + '. ';
              var link = document.createElement('a');
              link.href = '/settings';
              link.textContent = 'Open Settings';
              skipEl.appendChild(link);
            }
          } else if (data.status === 'error') {
            var errReason = 'Scan failed: ' + (data.reason || 'unknown error');
            scanStopped(btn, errReason, 'Retry scan');
            var errEl = document.getElementById('scan-status');
            if (errEl) {
              errEl.classList.remove('scan-status--success');
              errEl.classList.add('scan-status--error');
              errEl.textContent = errReason;
            }
          } else {
            btn.classList.remove('scan-running');
            btn.classList.add('is-success');
            btn.textContent = 'Scan complete ✓';
            var statusEl = document.getElementById('scan-status');
            var liveEl = document.getElementById('scan-live');
            var took = 'Finished in ' + fmtElapsed(_scanElapsed);
            stopStatusFade();
            if (statusEl) {
              statusEl.textContent = took;
              statusEl.classList.add('scan-status--success');
            }
            if (liveEl) liveEl.textContent = 'Scan ' + took.toLowerCase();
            refreshFragments().then(function () {
              if (statusEl) fadeStatusLater(statusEl);
            }, function () {
              if (statusEl) statusEl.textContent = took + '. Refresh the page to see the results.';
            }).then(function () {
              setTimeout(function () {
                btn.classList.remove('is-success');
                btn.textContent = 'Scan now';
                btn.disabled = false;
                if (MM.dom.focusStillIn(btn)) btn.focus();
              }, 2000);
            });
          }
        })
        .catch(function () {
          fails += 1;
          if (!firstFailAt) firstFailAt = Date.now();
          if (fails >= POLL_MAX_FAILS || Date.now() - firstFailAt >= POLL_MAX_MS) {
            var lostReason = 'Lost track of the scan. Refresh to check its status.';
            scanStopped(btn, lostReason, 'Retry scan');
            var lostEl = document.getElementById('scan-status');
            if (lostEl) {
              lostEl.classList.remove('scan-status--success');
              lostEl.classList.add('scan-status--error');
              lostEl.textContent = lostReason;
            }
            return;
          }
          setTimeout(check, POLL_RETRY_MS[Math.min(fails, POLL_RETRY_MS.length) - 1]);
        })
        .finally(function () { clearTimeout(abortTimer); });
    }, 3000);
  }

  function triggerScan(btn) {
    btn.disabled = true;
    btn.classList.remove('is-error', 'is-success');
    btn.classList.add('scan-running');
    btn.textContent = 'Scanning';
    _scanElapsed = 0;
    _scanStart = Date.now();
    updateScanStatus('Starting scan…');
    var live = document.getElementById('scan-live');
    if (live) live.textContent = '';
    MM.api.post('/api/scan/trigger')
      .then(function (data) {
        if (data.status === 'already_running') updateScanStatus('Scan already in progress');
        pollScanStatus(btn);
      })
      .catch(function (err) {
        var reason = "Couldn't start the scan. " + MM.api.messageOf(err, 'Try again.');
        btn.textContent = 'Retry scan';
        btn.classList.remove('scan-running');
        btn.classList.add('is-error');
        btn.disabled = false;
        updateScanStatus('');
        var statusEl = document.getElementById('scan-status');
        if (statusEl) {
          statusEl.textContent = reason;
          statusEl.classList.add('scan-status--error');
        }
        if (window.UIFeedback) window.UIFeedback.error(reason);
        /* The button was disabled during the request, so focus fell to the body. */
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  /* Re-download a deleted item with the ids stored when it was deleted. */
  function redownload(btn) {
    var payload;
    try { payload = JSON.parse(btn.dataset.redownload); } catch (_) { payload = null; }

    if (!payload) {
      if (window.UIFeedback) {
        window.UIFeedback.error('No identifier available — cannot re-download this item.');
      }
      return;
    }

    if (btn.getAttribute('aria-disabled') === 'true') return;
    btn.disabled = true;
    btn.classList.remove('is-error', 'is-success');
    btn.textContent = 'Adding…';
    MM.api.post('/api/media/redownload', payload)
      .then(function () {
        /* aria-disabled, not disabled, so a focused button keeps focus. */
        btn.setAttribute('aria-disabled', 'true');
        btn.disabled = false;
        btn.textContent = 'Queued ✓';
        btn.classList.add('is-success');
        if (MM.dom.focusStillIn(btn)) btn.focus();
      })
      .catch(function (err) {
        var msg = MM.api.messageOf(err, '');
        btn.textContent = msg && msg.length < 30 ? msg : 'Failed';
        if (window.UIFeedback) {
          window.UIFeedback.error("Couldn't re-download. " + MM.api.messageOf(err, 'Try again.'));
        }
        btn.classList.add('is-error');
        btn.disabled = false;
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  async function clearScheduled(btn) {
    if (!window.UIFeedback) return;
    var proceed = await window.UIFeedback.confirm({
      title: 'Clear all scheduled deletions?',
      body: 'Nothing is deleted now. Items will be re-evaluated on the next scan and may be rescheduled.',
      confirmLabel: 'Clear all',
      confirmVariant: 'danger'
    });
    if (!proceed) return;
    /* The label stays as it is so the button does not change width. */
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    MM.api.post('/api/scan/clear-scheduled')
      .then(function (data) {
        var text = 'Cleared ' + data.cleared + ' scheduled deletion' + (data.cleared === 1 ? '' : 's');
        if (data.already_deleting) {
          text += '; ' + data.already_deleting +
            (data.already_deleting === 1 ? ' was' : ' were') + ' already being deleted';
        }
        window.UIFeedback.toast(text, { variant: 'success', duration: data.already_deleting ? 8000 : undefined });
        return refreshFragments().catch(function () {
          window.UIFeedback.error('Cleared, but the page could not update. Refresh to see the list.');
        });
      }, function (err) {
        window.UIFeedback.error("Couldn't clear scheduled deletions. " + MM.api.messageOf(err, 'Try again.'));
      })
      .then(function () {
        if (!btn.isConnected) return;
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        if (MM.dom.focusStillIn(btn)) btn.focus();
      });
  }

  /* Shrink a tile away, take it off the page and slide the rest into place. */
  function removeTile(tile) {
    return new Promise(function (resolve) {
      if (reduceMotion() || !tile.animate) { tile.remove(); resolve(); return; }
      var m = motion();
      tile.style.overflow = 'hidden';
      var anim = tile.animate([{ opacity: 1, transform: 'scale(1)' }, { opacity: 0, transform: 'scale(.96)' }],
        { duration: m.duration, easing: m.easing, fill: 'forwards' });
      anim.onfinish = function () {
        var grid = tile.parentNode;
        var before = grid ? tileRects(grid) : {};
        tile.remove();
        if (grid) slideTiles(grid, before, m);
        resolve();
      };
    });
  }

  /* Submit a keep straight from a tile with the chosen duration; the
     library's keep dialog is more than the dashboard needs. */
  function dashSubmitKeep(btn, duration) {
    var wrapper = btn.closest('.keep-wrapper');
    if (!wrapper) return;
    /* A second click while the request runs sends nothing. */
    if (wrapper.dataset.keepBusy === '1') return;
    wrapper.dataset.keepBusy = '1';
    wrapper.setAttribute('aria-busy', 'true');
    var tile = wrapper.closest('.tile');
    var menu = wrapper.querySelector('.snooze-dropdown');
    /* Read before the menu closes: closing it drops focus to the body. */
    var hadFocus = (tile || wrapper).contains(document.activeElement);
    var inMenu = !!menu && menu.contains(document.activeElement);
    if (tile) { tile.style.transition = 'opacity .15s ease-out'; tile.style.opacity = '.6'; }
    if (window.SnoozeMenu) window.SnoozeMenu.closeAll(null);
    keepStarted();
    var mediaId = wrapper.dataset.id;
    MM.api.postForm('/api/media/' + encodeURIComponent(mediaId) + '/keep',
      { duration: duration })
      .then(function (data) {
        keepSettled(true);
        /* Saved, but the item was already being deleted: say so. */
        if (data && data.deletion_running && data.message) {
          window.UIFeedback.toast(data.message, { duration: 8000 });
        } else {
          window.UIFeedback.success(duration === 'forever' ? 'Kept forever' : 'Kept for ' + duration);
        }
        var grid = tile && tile.parentNode;
        var next = tile && (tile.nextElementSibling || tile.previousElementSibling);
        var moveFocus = hadFocus && tile && MM.dom.focusStillIn(tile);
        return (tile ? removeTile(tile) : Promise.resolve()).then(function () {
          if (moveFocus) {
            var go = next && next.querySelector('[data-action="keep-tile"]');
            if (!go && grid) { go = grid.closest('[data-fragment]'); go = go && go.querySelector('h2'); }
            if (go) { if (!go.matches('button, a')) go.tabIndex = -1; go.focus(); }
          }
          return refreshFragments();
        }).catch(function () {});
      }, function (err) {
        keepSettled(false);
        delete wrapper.dataset.keepBusy;
        wrapper.removeAttribute('aria-busy');
        if (tile) tile.style.opacity = '';
        var msg = MM.api.messageOf(err, 'Try again.');
        window.UIFeedback.error("Couldn't save keep. " + msg);
        var trigger = wrapper.querySelector('.keep-trigger, .btn-sm-caret');
        if (inMenu && trigger && MM.dom.focusStillIn(wrapper)) trigger.focus();
      });
  }

  /* Single delegated click handler — every action is data-action driven. */
  document.addEventListener('click', function (e) {
    var scanBtn = e.target.closest('[data-action="trigger-scan"]');
    if (scanBtn) { triggerScan(scanBtn); return; }

    var redlBtn = e.target.closest('[data-action="redownload"]');
    if (redlBtn) { redownload(redlBtn); return; }

    var clearBtn = e.target.closest('[data-action="clear-scheduled"]');
    if (clearBtn) { clearScheduled(clearBtn); return; }

    var keepBtn = e.target.closest('[data-action="keep-tile"]');
    if (keepBtn) {
      var duration = keepBtn.dataset.duration || '30 days';
      dashSubmitKeep(keepBtn, duration);
      return;
    }
  });
})();
