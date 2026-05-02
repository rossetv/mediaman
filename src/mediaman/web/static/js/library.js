/**
 * library.js — interactive behaviour for /library.
 *
 * Extracted from library.html (Finding 65: move inline scripts external so
 * a strict CSP nonce can be applied). All event wiring is delegated from
 * the document so it survives DOM updates and avoids inline handlers.
 *
 * Per-item data flows via data-* attributes on the markup; values that
 * could contain user-supplied strings (titles, IDs surfaced to the URL)
 * are JSON-encoded server-side and parsed here with JSON.parse.
 */
(function () {
  'use strict';

  /* ----------------------------------------------------------------------
   * Keep dialog state — module-private; not exposed on window.
   * ---------------------------------------------------------------------- */
  var _keepDialogState = { showRk: '', showTitle: '', seasonId: '', duration: '30 days' };

  /**
   * Submit a keep/snooze request for a media item.
   * For TV seasons (showRk truthy), opens the season-picker dialog.
   * For movies, submits directly and reloads.
   */
  function submitKeep(btn, duration) {
    var wrapper = btn.closest('.keep-wrapper');
    if (!wrapper) return;
    var mediaId = wrapper.dataset.id;
    var isTv = wrapper.dataset.tv === 'true';
    var showRk = wrapper.dataset.showRk || '';
    var showTitle = wrapper.dataset.showTitle || '';

    if (!isTv) {
      var body = new URLSearchParams({ duration: duration });
      fetch('/api/media/' + encodeURIComponent(mediaId) + '/keep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
      }).then(function (r) {
        if (r.ok) window.location.reload();
        else r.json().then(function (d) { window.UIFeedback.error("Couldn't save keep. " + (d.error || r.status)); });
      });
      return;
    }
    var durMap = { '7d': '7 days', '30d': '30 days', '90d': '90 days', 'forever': 'forever', 'current': null };
    _keepDialogState.showRk = showRk;
    _keepDialogState.showTitle = showTitle;
    _keepDialogState.seasonId = mediaId;
    _keepDialogState.duration = durMap[duration] || '30 days';
    openKeepDialog();
  }

  function openKeepDialog() {
    var overlay = document.getElementById('keep-dialog-overlay');
    overlay.classList.add('active');
    overlay.setAttribute('aria-hidden', 'false');
    if (window.ModalA11y) window.ModalA11y.onOpened('keep-dialog-overlay');
    if (_keepDialogState.duration) selectDurationByValue(_keepDialogState.duration);

    var list = document.getElementById('keep-season-list');
    list.textContent = 'Loading…';
    var rk = _keepDialogState.showRk || '_by_title';
    var url = '/api/show/' + encodeURIComponent(rk) + '/seasons';
    if (!_keepDialogState.showRk && _keepDialogState.showTitle) {
      url += '?title=' + encodeURIComponent(_keepDialogState.showTitle);
    }
    document.getElementById('keep-dialog-remove').classList.remove('is-visible');
    document.getElementById('keep-dialog-confirm').textContent = 'Keep';
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        document.getElementById('keep-dialog-title').textContent = 'Keep ' + (data.show_title || _keepDialogState.showTitle || 'Show');
        var hasKeeps = data.show_kept || data.seasons.some(function (s) { return s.kept; });
        document.getElementById('keep-dialog-remove').classList.toggle('is-visible', hasKeeps);
        if (hasKeeps) document.getElementById('keep-dialog-confirm').textContent = 'Update';
        if (!_keepDialogState.duration && data.show_kept && data.show_kept.snooze_duration) {
          _keepDialogState.duration = data.show_kept.snooze_duration;
          selectDurationByValue(_keepDialogState.duration);
        } else if (!_keepDialogState.duration) {
          _keepDialogState.duration = 'forever';
          selectDurationByValue('forever');
        }
        renderSeasonPicker(data.seasons, data.show_kept);
      })
      .catch(function () { list.textContent = "Couldn't load seasons."; });
  }

  function closeKeepDialog() {
    var overlay = document.getElementById('keep-dialog-overlay');
    overlay.classList.remove('active');
    overlay.setAttribute('aria-hidden', 'true');
    if (window.ModalA11y) window.ModalA11y.onClosed('keep-dialog-overlay');
  }

  function selectDuration(btn) {
    document.querySelectorAll('.keep-dur-btn').forEach(function (b) { b.classList.remove('active'); });
    btn.classList.add('active');
    _keepDialogState.duration = btn.dataset.dur;
  }

  function selectDurationByValue(dur) {
    document.querySelectorAll('.keep-dur-btn').forEach(function (b) { b.classList.toggle('active', b.dataset.dur === dur); });
  }

  function renderSeasonPicker(seasons, showKept) {
    var list = document.getElementById('keep-season-list');
    while (list.firstChild) list.removeChild(list.firstChild);

    var allRow = document.createElement('div');
    allRow.className = 'keep-season-item keep-all-row';
    var allCb = document.createElement('input');
    allCb.type = 'checkbox'; allCb.id = 'keep-all-seasons';
    var anyKept = showKept || seasons.some(function (s) { return s.kept; });
    allCb.checked = !!showKept || !anyKept;
    allCb.addEventListener('change', function () {
      list.querySelectorAll('input[data-season-id]').forEach(function (cb) { cb.checked = allCb.checked; });
    });
    var allLabel = document.createElement('label');
    allLabel.htmlFor = 'keep-all-seasons';
    allLabel.textContent = 'All seasons';
    allRow.appendChild(allCb);
    allRow.appendChild(allLabel);
    list.appendChild(allRow);

    seasons.forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'keep-season-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'keep-season-' + s.id;
      cb.dataset.seasonId = s.id;
      cb.checked = s.kept || !anyKept;
      var label = document.createElement('label');
      label.htmlFor = 'keep-season-' + s.id;
      label.textContent = 'Season ' + s.season_number;
      row.appendChild(cb);
      row.appendChild(label);

      var details = document.createElement('span');
      details.className = 'keep-season-details';
      var parts = [];
      if (s.file_size) parts.push(s.file_size);
      if (s.last_watched) parts.push('watched ' + s.last_watched);
      else parts.push('never watched');
      details.textContent = parts.join(' · ');
      row.appendChild(details);

      if (s.kept) {
        var badge = document.createElement('span');
        badge.className = 'pill pill--kept';
        badge.textContent = 'Kept';
        row.appendChild(badge);
      }
      list.appendChild(row);
    });
  }

  function confirmKeepDialog() {
    var allChecked = document.getElementById('keep-all-seasons').checked;
    var selectedIds = [];
    document.querySelectorAll('#keep-season-list input[data-season-id]:checked').forEach(function (cb) {
      selectedIds.push(cb.dataset.seasonId);
    });

    if (selectedIds.length === 0 && !allChecked) {
      window.UIFeedback.error('Select at least one season to continue.');
      return;
    }

    var btn = document.getElementById('keep-dialog-confirm');
    btn.disabled = true;
    btn.textContent = 'Keeping…';

    if (allChecked) {
      if (selectedIds.length === 0) {
        document.querySelectorAll('#keep-season-list input[data-season-id]').forEach(function (cb) {
          selectedIds.push(cb.dataset.seasonId);
        });
      }
      fetch('/api/show/' + encodeURIComponent(_keepDialogState.showRk) + '/keep', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ duration: _keepDialogState.duration, season_ids: selectedIds }),
      }).then(function (r) {
        if (r.ok) window.location.reload();
        else r.json().then(function (d) { window.UIFeedback.error("Couldn't save keep. " + (d.error || 'Try again.')); btn.disabled = false; btn.textContent = 'Keep'; });
      });
    } else {
      /* Finding 35: check response.ok for each request; report failed season IDs. */
      var durMap = { '7 days': '7d', '30 days': '30d', '90 days': '90d', 'forever': 'forever' };
      var dur = durMap[_keepDialogState.duration] || '30d';
      var failed = [];
      /* Run sequentially so we can short-circuit on first failure for user clarity. */
      (function runNext(i) {
        if (i >= selectedIds.length) {
          if (failed.length) {
            window.UIFeedback.error('Keep failed for ' + failed.length + ' season(s): ' + failed.join(', '));
            btn.disabled = false;
            btn.textContent = 'Keep';
          } else {
            window.location.reload();
          }
          return;
        }
        var sid = selectedIds[i];
        fetch('/api/media/' + encodeURIComponent(sid) + '/keep', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ duration: dur }).toString(),
        }).then(function (r) {
          if (!r.ok) failed.push(sid);
          runNext(i + 1);
        }).catch(function () {
          failed.push(sid);
          runNext(i + 1);
        });
      })(0);
    }
  }

  function removeAllKeeps() {
    window.UIFeedback.confirm({
      title: 'Remove all keeps for this show?',
      body: 'Seasons may be scheduled for deletion on the next scan.',
      confirmLabel: 'Remove all',
      confirmVariant: 'danger'
    }).then(function (ok) {
      if (!ok) return;
      /* Finding 35: collect season IDs to remove; check response.ok per request. */
      var seasonIds = [];
      var rk = _keepDialogState.showRk;
      document.querySelectorAll('#keep-season-list input[data-season-id]').forEach(function (cb) {
        seasonIds.push(cb.dataset.seasonId);
      });

      var failed = [];
      var allRequests = [];
      if (rk) allRequests.push({ url: '/api/show/' + encodeURIComponent(rk) + '/remove', id: rk });
      seasonIds.forEach(function (sid) {
        allRequests.push({ url: '/api/media/' + encodeURIComponent(sid) + '/unprotect', id: sid });
      });

      (function runNext(i) {
        if (i >= allRequests.length) {
          if (failed.length) {
            window.UIFeedback.error('Remove failed for: ' + failed.join(', '));
          } else {
            window.location.reload();
          }
          return;
        }
        var req = allRequests[i];
        fetch(req.url, { method: 'POST' }).then(function (r) {
          if (!r.ok) failed.push(req.id);
          runNext(i + 1);
        }).catch(function () {
          failed.push(req.id);
          runNext(i + 1);
        });
      })(0);
    });
  }

  /* ----------------------------------------------------------------------
   * Delete confirmation modal
   * ---------------------------------------------------------------------- */
  var _pendingDeleteId = null;

  function confirmDelete(mediaId, title) {
    _pendingDeleteId = mediaId;
    document.getElementById('delete-modal-body').textContent = 'Delete "' + title + '"? This cannot be undone.';
    var modal = document.getElementById('delete-modal');
    modal.classList.add('is-visible');
    modal.setAttribute('aria-hidden', 'false');
    if (window.ModalA11y) window.ModalA11y.onOpened('delete-modal');
    document.getElementById('delete-confirm-btn').onclick = function () {
      if (!_pendingDeleteId) return;
      fetch('/api/media/' + encodeURIComponent(_pendingDeleteId) + '/delete', { method: 'POST' })
        .then(function (r) {
          if (r.ok) window.location.reload();
          else r.json().then(function (d) { window.UIFeedback.error("Couldn't delete item. " + (d.error || r.status)); });
        });
      closeDeleteModal();
    };
  }

  function closeDeleteModal() {
    _pendingDeleteId = null;
    var modal = document.getElementById('delete-modal');
    modal.classList.remove('is-visible');
    modal.setAttribute('aria-hidden', 'true');
    if (window.ModalA11y) window.ModalA11y.onClosed('delete-modal');
  }

  function removeKeep(mediaId) {
    fetch('/api/media/' + encodeURIComponent(mediaId) + '/unprotect', { method: 'POST' })
      .then(function (r) { if (r.ok) window.location.reload(); });
  }

  /* ----------------------------------------------------------------------
   * Sort + search-debounce helpers (toolbar wiring)
   * ---------------------------------------------------------------------- */
  function setSort(value) {
    document.getElementById('sort-input').value = value;
    document.getElementById('toolbar-form').submit();
  }

  function toggleSort(col) {
    var input = document.getElementById('sort-input');
    var current = input.value;
    var sortMap = {
      name: { default: 'name_asc', asc: 'name_asc', desc: 'name_desc' },
      size: { default: 'size_desc', asc: 'size_asc', desc: 'size_desc' },
      watched: { default: 'watched_desc', asc: 'watched_asc', desc: 'watched_desc' },
    };
    var m = sortMap[col];
    if (!m) return;
    if (current === m.asc) input.value = m.desc;
    else if (current === m.desc) input.value = m.asc;
    else input.value = m.default;
    document.getElementById('toolbar-form').submit();
  }

  var _searchTimer = null;
  function debounceSubmit() {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function () { document.getElementById('toolbar-form').submit(); }, 400);
  }

  /* ----------------------------------------------------------------------
   * Wiring — runs on script load (deferred, so DOM is parsed).
   * ---------------------------------------------------------------------- */
  function wire() {
    /* Modal-a11y registration. Safe to call once at startup; both modals
       must exist on the library page when items are present (delete) and
       always (keep). */
    if (window.ModalA11y) {
      window.ModalA11y.register('keep-dialog-overlay', closeKeepDialog);
      window.ModalA11y.register('delete-modal', closeDeleteModal);
    }

    /* Delete-modal backdrop click closes. */
    var deleteModal = document.getElementById('delete-modal');
    if (deleteModal) {
      deleteModal.addEventListener('click', function (e) {
        if (e.target === deleteModal) closeDeleteModal();
      });
      /* Cancel button in delete modal. */
      var deleteCancel = deleteModal.querySelector('[data-action="close-delete-modal"]');
      if (deleteCancel) deleteCancel.addEventListener('click', closeDeleteModal);
    }

    /* Keep-dialog wiring: backdrop click closes; inner card stops propagation. */
    var keepOverlay = document.getElementById('keep-dialog-overlay');
    if (keepOverlay) {
      keepOverlay.addEventListener('click', function (e) {
        if (e.target === keepOverlay) closeKeepDialog();
      });
      var keepCard = keepOverlay.querySelector('.keep-dialog');
      if (keepCard) {
        keepCard.addEventListener('click', function (e) { e.stopPropagation(); });
      }
      /* Duration buttons */
      keepOverlay.querySelectorAll('.keep-dur-btn').forEach(function (b) {
        b.addEventListener('click', function () { selectDuration(b); });
      });
      /* Remove-all / Cancel / Confirm buttons (data-action delegation). */
      var removeBtn = document.getElementById('keep-dialog-remove');
      if (removeBtn) removeBtn.addEventListener('click', removeAllKeeps);
      var confirmBtn = document.getElementById('keep-dialog-confirm');
      if (confirmBtn) confirmBtn.addEventListener('click', confirmKeepDialog);
      keepOverlay.querySelectorAll('[data-action="close-keep-dialog"]').forEach(function (b) {
        b.addEventListener('click', closeKeepDialog);
      });
    }

    /* Mobile sort <select> — uses change event rather than inline onchange. */
    var mobileSort = document.getElementById('lib-mobile-sort-select');
    if (mobileSort) {
      mobileSort.addEventListener('change', function () { setSort(mobileSort.value); });
    }

    /* Debounced search input — uses input event rather than inline oninput. */
    var searchInput = document.querySelector('#toolbar-form input[name="q"]');
    if (searchInput) {
      searchInput.addEventListener('input', debounceSubmit);
    }
  }

  /* Finding 17: delegate data-keep-dur and data-action="confirm-delete" clicks
     instead of using inline onclick handlers. Values come from data-* attributes
     so they are never interpolated into JS strings. */
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var keepBtn = e.target.closest('[data-keep-dur]');
    if (keepBtn) {
      e.preventDefault();
      submitKeep(keepBtn, keepBtn.dataset.keepDur);
      return;
    }
    var removeBtn = e.target.closest('[data-remove-keep-id]');
    if (removeBtn) {
      e.preventDefault();
      /* dataset value is JSON-encoded by the template — parse it safely. */
      var mid;
      try { mid = JSON.parse(removeBtn.dataset.removeKeepId); } catch (_) { return; }
      removeKeep(mid);
      return;
    }
    var deleteBtn = e.target.closest('[data-action="confirm-delete"]');
    if (deleteBtn) {
      e.preventDefault();
      var id = deleteBtn.dataset.id;
      var title;
      try { title = JSON.parse(deleteBtn.dataset.title); } catch (_) { title = ''; }
      confirmDelete(id, title);
    }
  });

  /* Finding 10: delegated click handler for the .th-sort buttons.
     Replaces the inline onclick attribute on <th> (which was illegal
     a11y — <th> isn't keyboard-activatable). */
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var sortBtn = e.target.closest('.th-sort');
    if (sortBtn && sortBtn.dataset.sortCol) {
      e.preventDefault();
      toggleSort(sortBtn.dataset.sortCol);
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wire);
  } else {
    wire();
  }
}());
