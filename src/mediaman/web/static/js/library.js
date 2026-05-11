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

  /* Modal handles assigned during wire() — both modals are CSS-class
     driven (no inline display:flex), so we pass activeClass to setupDetail.
     manageBodyOverflow stays at its default — neither modal locks body
     scroll today and we shouldn't introduce that as a side effect. */
  var _keepDialogModal = null;
  var _deleteModal = null;

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
      MM.api.postForm('/api/media/' + encodeURIComponent(mediaId) + '/keep',
        { duration: duration })
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          var msg = (err && err.message) || 'Try again.';
          window.UIFeedback.error("Couldn't save keep. " + msg);
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
    if (_keepDialogModal) _keepDialogModal.open();
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
    MM.api.get(url)
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
    if (_keepDialogModal) _keepDialogModal.close();
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
    list.replaceChildren();

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
      MM.api.post(
        '/api/show/' + encodeURIComponent(_keepDialogState.showRk) + '/keep',
        { duration: _keepDialogState.duration, season_ids: selectedIds }
      )
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          var msg = (err && err.message) || 'Try again.';
          window.UIFeedback.error("Couldn't save keep. " + msg);
          btn.disabled = false;
          btn.textContent = 'Keep';
        });
    } else {
      /* Finding 35: check each request; report failed season IDs. */
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
        MM.api.postForm('/api/media/' + encodeURIComponent(sid) + '/keep', { duration: dur })
          .then(function () { runNext(i + 1); })
          .catch(function () { failed.push(sid); runNext(i + 1); });
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
        MM.api.post(req.url)
          .then(function () { runNext(i + 1); })
          .catch(function () { failed.push(req.id); runNext(i + 1); });
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
    if (_deleteModal) _deleteModal.open();
    document.getElementById('delete-confirm-btn').onclick = function () {
      if (!_pendingDeleteId) return;
      MM.api.post('/api/media/' + encodeURIComponent(_pendingDeleteId) + '/delete')
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          var msg = (err && err.message) || 'Try again.';
          window.UIFeedback.error("Couldn't delete item. " + msg);
        });
      closeDeleteModal();
    };
  }

  function closeDeleteModal() {
    _pendingDeleteId = null;
    if (_deleteModal) _deleteModal.close();
  }

  function removeKeep(mediaId) {
    MM.api.post('/api/media/' + encodeURIComponent(mediaId) + '/unprotect')
      .then(function () { window.location.reload(); })
      .catch(function () { /* original failed silently — preserve that. */ });
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
    /* ── Delete modal — CSS-class-driven (.is-visible owns the display
         rule), close-cancel button uses data-action, body scroll is not
         locked by this modal today. ── */
    var deleteModalEl = document.getElementById('delete-modal');
    if (deleteModalEl) {
      _deleteModal = MM.modal.setupDetail(deleteModalEl, {
        activeClass: 'is-visible',
        manageBodyOverflow: false,
        closeSelectors: ['[data-action="close-delete-modal"]'],
        onClose: function () { _pendingDeleteId = null; },
      });
    }

    /* ── Keep dialog — CSS-class-driven (.active owns the display rule).
         The dialog card stops click propagation so a click on it doesn't
         bubble to the overlay's backdrop-close. ── */
    var keepOverlay = document.getElementById('keep-dialog-overlay');
    if (keepOverlay) {
      _keepDialogModal = MM.modal.setupDetail(keepOverlay, {
        activeClass: 'active',
        manageBodyOverflow: false,
        closeSelectors: ['[data-action="close-keep-dialog"]'],
      });
      var keepCard = keepOverlay.querySelector('.keep-dialog');
      if (keepCard) {
        keepCard.addEventListener('click', function (e) { e.stopPropagation(); });
      }
      /* Duration buttons */
      keepOverlay.querySelectorAll('.keep-dur-btn').forEach(function (b) {
        b.addEventListener('click', function () { selectDuration(b); });
      });
      /* Remove-all / Confirm buttons */
      var removeBtn = document.getElementById('keep-dialog-remove');
      if (removeBtn) removeBtn.addEventListener('click', removeAllKeeps);
      var confirmBtn = document.getElementById('keep-dialog-confirm');
      if (confirmBtn) confirmBtn.addEventListener('click', confirmKeepDialog);
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
