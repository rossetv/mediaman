/**
 * library.js — interactive behaviour for /library.
 *
 * Drives the row Keep menus and Keep dialog, Remove keep, and the Delete
 * confirmations (shows the server's message when a delete is refused). All
 * event wiring is delegated from the document so it survives DOM updates and
 * avoids inline handlers.
 *
 * Per-item data flows via data-* attributes on the markup; values that
 * could contain user-supplied strings (titles, IDs surfaced to the URL)
 * are JSON-encoded server-side and parsed here with JSON.parse.
 */
(function () {
  'use strict';

  var _keepDialogState = { showRk: '', hasRk: false, showKept: false, keptSeasonIds: [], showTitle: '', seasonId: '', duration: '30 days' };

  /* Modal handles assigned during wire() — both modals are CSS-class
     driven (no inline display:flex), so we pass activeClass to setupDetail.
     manageBodyOverflow stays at its default — neither modal locks body
     scroll today and we shouldn't introduce that as a side effect. */
  var _keepDialogModal = null;
  var _deleteModal = null;
  var _seasonDeleteModal = null;

  /* Each dialog's season fetch is numbered; a response that is not the
     latest belongs to a show no longer open and is dropped. */
  var _keepDialogRequest = 0;
  var _seasonDeleteRequest = 0;

  /* Keep chains still running, by show. A dialog opened for such a show keeps
     Keep disabled until its chain settles, so two chains never interleave. */
  var _keepChains = {};

  function keepChainKey() {
    return _keepDialogState.showRk || 'title:' + _keepDialogState.showTitle;
  }

  function trackKeepChain(key, chain) {
    var tracked = chain.then(function () {}, function () {});
    _keepChains[key] = tracked;
    tracked.then(function () { if (_keepChains[key] === tracked) delete _keepChains[key]; });
  }

  /* The note sits beside the confirm button, made on first use. */
  function keepChainNote() {
    var note = document.getElementById('keep-dialog-note');
    if (!note) {
      var confirmBtn = document.getElementById('keep-dialog-confirm');
      note = document.createElement('span');
      note.id = 'keep-dialog-note';
      note.className = 'keep-season-details';
      confirmBtn.parentNode.insertBefore(note, confirmBtn.parentNode.querySelector('.u-flex-1').nextSibling);
    }
    return note;
  }

  /* While a chain for this show runs, neither Keep/Update nor Remove all may
     start another. When it settles, a dialog still showing the show reads the
     seasons again, since the chain changed them. */
  function holdKeepWhileSaving(request) {
    var note = keepChainNote();
    var chain = _keepChains[keepChainKey()];
    var confirmBtn = document.getElementById('keep-dialog-confirm');
    var removeBtn = document.getElementById('keep-dialog-remove');
    note.textContent = chain ? 'Saving the previous change…' : '';
    removeBtn.disabled = !!chain;
    if (!chain) return;
    confirmBtn.disabled = true;
    chain.then(function () {
      if (request !== _keepDialogRequest) return;
      confirmBtn.disabled = false;
      removeBtn.disabled = false;
      note.textContent = '';
      loadKeepSeasons();
    });
  }

  /* A keep pressed while the item was already being deleted is saved, but its
     files may be gone: show the server's word on it before reloading. */
  function reloadAfterKeep(data) {
    if (data && data.deletion_running && data.message) {
      window.UIFeedback.toast(data.message, { duration: 8000 });
      setTimeout(function () { window.location.reload(); }, 8000);
      return;
    }
    window.location.reload();
  }

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
    var hasRk = wrapper.dataset.hasRk === 'true';

    if (!isTv) {
      MM.api.postForm('/api/media/' + encodeURIComponent(mediaId) + '/keep',
        { duration: duration })
        .then(reloadAfterKeep)
        .catch(function (err) {
          var msg = (err && err.message) || 'Try again.';
          window.UIFeedback.error("Couldn't save keep. " + msg);
        });
      return;
    }
    _keepDialogState.showRk = showRk;
    _keepDialogState.hasRk = hasRk;
    _keepDialogState.showTitle = showTitle;
    _keepDialogState.seasonId = mediaId;
    _keepDialogState.duration = duration || '30 days';
    openKeepDialog();
  }

  function openKeepDialog() {
    if (_keepDialogModal) _keepDialogModal.open();
    if (_keepDialogState.duration) selectDurationByValue(_keepDialogState.duration);
    loadKeepSeasons();
  }

  function seasonsUrl() {
    var rk = _keepDialogState.showRk || '_by_title';
    var url = '/api/show/' + encodeURIComponent(rk) + '/seasons';
    if (!_keepDialogState.showRk && _keepDialogState.showTitle) {
      url += '?title=' + encodeURIComponent(_keepDialogState.showTitle);
    }
    return url;
  }

  function loadKeepSeasons() {
    _keepDialogState.showKept = false;
    _keepDialogState.keptSeasonIds = [];
    var list = document.getElementById('keep-season-list');
    list.textContent = 'Loading…';
    var url = seasonsUrl();
    document.getElementById('keep-dialog-remove').classList.remove('is-visible');
    var confirmBtn = document.getElementById('keep-dialog-confirm');
    confirmBtn.textContent = 'Keep';
    /* A keep still running for the show opened before must not lock this one. */
    confirmBtn.disabled = false;
    var request = ++_keepDialogRequest;
    holdKeepWhileSaving(request);
    MM.api.get(url)
      .then(function (data) {
        if (request !== _keepDialogRequest) return;
        document.getElementById('keep-dialog-title').textContent = 'Keep ' + (data.show_title || _keepDialogState.showTitle || 'Show');
        _keepDialogState.showKept = !!data.show_kept;
        _keepDialogState.keptSeasonIds = data.seasons.filter(function (s) { return s.kept; }).map(function (s) { return s.id; });
        var hasKeeps = data.show_kept || data.seasons.some(function (s) { return s.kept; });
        document.getElementById('keep-dialog-remove').classList.toggle('is-visible', hasKeeps);
        if (hasKeeps) document.getElementById('keep-dialog-confirm').textContent = 'Update';
        renderSeasonPicker(data.seasons, data.show_kept);
      })
      .catch(function () {
        if (request === _keepDialogRequest) list.textContent = "Couldn't load seasons.";
      });
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

    /* A show keyed by its title has no rating key to keep the whole show
       under, so only per-season keeps are offered. */
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
    if (_keepDialogState.hasRk) list.appendChild(allRow);

    seasons.forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'keep-season-item';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'keep-season-' + s.id;
      cb.dataset.seasonId = s.id;
      if (s.kept) cb.dataset.kept = '1';
      cb.checked = !!showKept || s.kept || !anyKept;
      cb.addEventListener('change', function () {
        if (!cb.checked) allCb.checked = false;
      });
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
    var allCb = document.getElementById('keep-all-seasons');
    var allChecked = !!allCb && allCb.checked;
    var selectedIds = [];
    document.querySelectorAll('#keep-season-list input[data-season-id]:checked').forEach(function (cb) {
      selectedIds.push(cb.dataset.seasonId);
    });

    if (selectedIds.length === 0 && !allChecked) {
      window.UIFeedback.error('Select at least one season to continue.');
      return;
    }

    var btn = document.getElementById('keep-dialog-confirm');
    var btnLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Keeping…';
    var removeAllBtn = document.getElementById('keep-dialog-remove');
    if (removeAllBtn) removeAllBtn.disabled = true;

    /* The requests below outlive this click: the dialog may be closed and
       opened for another show meanwhile, so everything they need is read
       now, and the dialog's buttons are touched only while it is still the
       one that was open. */
    var rk = _keepDialogState.showRk;
    var showKept = _keepDialogState.showKept;
    var dur = _keepDialogState.duration || '30 days';
    var seasonsRequestUrl = seasonsUrl();
    var request = _keepDialogRequest;
    var chainKey = keepChainKey();
    var chain;
    var showTitle = _keepDialogState.showTitle;
    function dialogIsOpen() { return request === _keepDialogRequest; }
    /* A toast from a dialog no longer shown must say whose keep it is. */
    function say(msg) {
      return dialogIsOpen() || !showTitle ? msg : showTitle + ': ' + msg;
    }
    function restoreButton() {
      if (!dialogIsOpen()) return;
      btn.disabled = false;
      btn.textContent = btnLabel;
      if (removeAllBtn) removeAllBtn.disabled = false;
    }
    /* A keep that finished for a dialog no longer shown must not reload the
       page under the one that is. */
    function done(data) {
      if (dialogIsOpen()) reloadAfterKeep(data);
      else if (data && data.deletion_running && data.message) {
        window.UIFeedback.toast(say(data.message), { duration: 8000 });
      }
    }

    if (allChecked) {
      if (selectedIds.length === 0) {
        document.querySelectorAll('#keep-season-list input[data-season-id]').forEach(function (cb) {
          selectedIds.push(cb.dataset.seasonId);
        });
      }
      chain = MM.api.post(
        '/api/show/' + encodeURIComponent(rk) + '/keep',
        { duration: dur, season_ids: selectedIds }
      )
        .then(done)
        .catch(function (err) {
          var msg = (err && err.message) || 'Try again.';
          window.UIFeedback.error(say("Couldn't save keep. " + msg));
          restoreButton();
        });
    } else {
      /* Failures are reported by the season names the dialog shows. */
      var names = {};
      var unticked = [];
      document.querySelectorAll('#keep-season-list input[data-season-id]').forEach(function (cb) {
        names[cb.dataset.seasonId] = cb.labels[0].textContent;
        if (!cb.checked) unticked.push(cb.dataset.seasonId);
      });
      /* Sequential loop — accumulates failures, continues after each one. */
      var keepChain = Promise.resolve();
      var failed = [];
      var running = null;
      selectedIds.forEach(function (sid) {
        keepChain = keepChain.then(function () {
          return MM.api.postForm('/api/media/' + encodeURIComponent(sid) + '/keep', { duration: dur })
            .then(function (data) { if (data && data.deletion_running) running = data; })
            .catch(function () { failed.push(names[sid]); });
        });
      });
      /* Once every ticked season is kept, an unticked season loses its keep.
         A show keep covers every season, so it goes first: unticking one
         season ends it. The seasons are read again because ending the show
         keep leaves some with no keep of their own to remove. */
      keepChain = keepChain.then(function () {
        if (failed.length) return;
        var ended = Promise.resolve(true);
        if (showKept) {
          ended = MM.api.post('/api/show/' + encodeURIComponent(rk) + '/remove')
            .catch(function (err) {
              /* 404: no show keep any more (removed in another tab, or the
                 reply to an earlier try was lost), which is what was asked. */
              if (err && err.status === 404) return;
              throw err;
            })
            .then(function () {
              if (dialogIsOpen()) _keepDialogState.showKept = false;
              return true;
            })
            .catch(function () { failed.push('the show keep'); return false; });
        }
        return ended.then(function (ok) {
          if (!ok || !unticked.length) return;
          return MM.api.get(seasonsRequestUrl).then(function (data) {
            var keptNow = {};
            data.seasons.forEach(function (s) { if (s.kept) keptNow[s.id] = true; });
            var chain = Promise.resolve();
            unticked.forEach(function (sid) {
              if (!keptNow[sid]) return;
              chain = chain.then(function () {
                return MM.api.post('/api/media/' + encodeURIComponent(sid) + '/unprotect')
                  .catch(function () { failed.push(names[sid]); });
              });
            });
            return chain;
          }, function () { failed.push('the seasons to remove keeps from'); });
        });
      });
      chain = keepChain.then(function () {
        if (failed.length) {
          window.UIFeedback.error(say('Keep failed for: ' + failed.join(', ')));
          if (running && running.message) {
            window.UIFeedback.toast(say(running.message), { duration: 8000 });
          }
          restoreButton();
        } else {
          done(running);
        }
      });
    }
    trackKeepChain(chainKey, chain);
  }

  function removeAllKeeps() {
    /* Disabled until the requests settle, so a second click cannot resend them. */
    var removeBtn = document.getElementById('keep-dialog-remove');
    removeBtn.disabled = true;
    window.UIFeedback.confirm({
      title: 'Remove all keeps for this show?',
      body: 'Seasons may be scheduled for deletion on the next scan.',
      confirmLabel: 'Remove all',
      confirmVariant: 'danger'
    }).then(function (ok) {
      if (!ok) { removeBtn.disabled = false; return; }
      /* Like a keep, the requests outlive the dialog, so what they need is
         read now and the dialog is touched only while it is the same one. */
      var request = _keepDialogRequest;
      var chainKey = keepChainKey();
      var showTitle = _keepDialogState.showTitle;
      var rk = _keepDialogState.showRk;
      var allRequests = [];
      if (rk && _keepDialogState.showKept) {
        allRequests.push({
          url: '/api/show/' + encodeURIComponent(rk) + '/remove',
          name: 'the show keep',
          /* 404: no show keep any more, which is what was asked. */
          goneIsDone: true
        });
      }
      var names = {};
      document.querySelectorAll('#keep-season-list input[data-season-id]').forEach(function (cb) {
        names[cb.dataset.seasonId] = cb.labels[0].textContent;
      });
      _keepDialogState.keptSeasonIds.forEach(function (sid) {
        allRequests.push({ url: '/api/media/' + encodeURIComponent(sid) + '/unprotect', name: names[sid] || 'a season' });
      });

      var removeChain = Promise.resolve();
      var failed = [];
      allRequests.forEach(function (req) {
        removeChain = removeChain.then(function () {
          return MM.api.post(req.url)
            .catch(function (err) {
              if (req.goneIsDone && err && err.status === 404) return;
              throw err;
            })
            .catch(function () { failed.push(req.name); });
        });
      });
      var chain = removeChain.then(function () {
        if (failed.length) {
          var msg = 'Remove failed for: ' + failed.join(', ');
          window.UIFeedback.error(request === _keepDialogRequest || !showTitle ? msg : showTitle + ': ' + msg);
        } else if (request === _keepDialogRequest) {
          window.location.reload();
        }
      });
      trackKeepChain(chainKey, chain);
      /* Holds Keep and Remove all, then reads the seasons again, so a retry
         sends only what is still kept. */
      holdKeepWhileSaving(request);
    });
  }

  var _pendingDeleteId = null;

  function confirmDelete(mediaId, title) {
    _pendingDeleteId = mediaId;
    document.getElementById('delete-modal-body').textContent = 'Delete "' + title + '"? This cannot be undone.';
    if (_deleteModal) _deleteModal.open();
    // The confirm button is wired once in wire() and reads _pendingDeleteId
    // from closure state — no re-assignment here.
  }

  function closeDeleteModal() {
    _pendingDeleteId = null;
    if (_deleteModal) _deleteModal.close();
  }

  /**
   * Open the per-season Delete list for a grouped show row. Nothing is
   * deleted from here: each season's own Delete goes through confirmDelete.
   */
  function openSeasonDelete(showRk, showTitle) {
    var list = document.getElementById('season-delete-list');
    document.getElementById('season-delete-title').textContent = 'Delete from ' + (showTitle || 'show');
    if (_seasonDeleteModal) _seasonDeleteModal.open();
    var request = ++_seasonDeleteRequest;
    if (!showRk) { list.textContent = "Couldn't load seasons."; return; }
    list.textContent = 'Loading…';
    MM.api.get('/api/show/' + encodeURIComponent(showRk) + '/seasons')
      .then(function (data) {
        if (request === _seasonDeleteRequest) renderSeasonDeleteList(data, showTitle);
      })
      .catch(function () {
        if (request === _seasonDeleteRequest) list.textContent = "Couldn't load seasons.";
      });
  }

  function renderSeasonDeleteList(data, showTitle) {
    var list = document.getElementById('season-delete-list');
    list.replaceChildren();
    if (!data.seasons.length) { list.textContent = 'No seasons found.'; return; }
    var title = data.show_title || showTitle;

    data.seasons.forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'keep-season-item';
      var name = document.createElement('span');
      name.textContent = s.season_number != null ? 'Season ' + s.season_number : s.title;
      row.appendChild(name);

      var details = document.createElement('span');
      details.className = 'keep-season-details';
      var parts = [];
      if (s.file_size) parts.push(s.file_size);
      if (s.last_watched) parts.push('watched ' + s.last_watched);
      else parts.push('never watched');
      details.textContent = parts.join(' · ');
      row.appendChild(details);

      /* A kept season gets no Delete, as a kept row gets none on the page. */
      if (s.kept || data.show_kept) {
        var badge = document.createElement('span');
        badge.className = 'pill pill--kept';
        badge.textContent = 'Kept';
        row.appendChild(badge);
      } else {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn btn--danger btn--sm';
        del.textContent = 'Delete';
        del.addEventListener('click', function () {
          if (_seasonDeleteModal) _seasonDeleteModal.close();
          confirmDelete(String(s.id), title + ' — ' + name.textContent);
        });
        row.appendChild(del);
      }
      list.appendChild(row);
    });
  }

  function removeKeep(mediaId) {
    MM.api.post('/api/media/' + encodeURIComponent(mediaId) + '/unprotect')
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        var msg = (err && err.message) || 'Try again.';
        window.UIFeedback.error("Couldn't remove keep. " + msg);
      });
  }

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

  function wire() {
    /* Delete modal: CSS-class-driven (.is-visible owns the display
         rule), close-cancel button uses data-action, body scroll is not
         locked by this modal today. */
    var deleteModalEl = document.getElementById('delete-modal');
    if (deleteModalEl) {
      _deleteModal = MM.modal.setupDetail(deleteModalEl, {
        activeClass: 'is-visible',
        manageBodyOverflow: false,
        closeSelectors: ['[data-action="close-delete-modal"]'],
        onClose: function () { _pendingDeleteId = null; },
      });
    }
    /* Wire the confirm button once — reads _pendingDeleteId from module state
       rather than re-assigning .onclick on every confirmDelete() call. */
    var deleteConfirmBtn = document.getElementById('delete-confirm-btn');
    if (deleteConfirmBtn) {
      deleteConfirmBtn.addEventListener('click', function () {
        if (!_pendingDeleteId) return;
        MM.api.post('/api/media/' + encodeURIComponent(_pendingDeleteId) + '/delete')
          .then(function () { window.location.reload(); })
          .catch(function (err) {
            var msg = (err && err.message) || 'Try again.';
            window.UIFeedback.error("Couldn't delete item. " + msg);
          });
        closeDeleteModal();
      });
    }

    /* Keep dialog: CSS-class-driven (.active owns the display rule).
         The dialog card stops click propagation so a click on it doesn't
         bubble to the overlay's backdrop-close. */
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

    /* Season Delete dialog: same markup and display rule as the Keep dialog. */
    var seasonDeleteOverlay = document.getElementById('season-delete-overlay');
    if (seasonDeleteOverlay) {
      _seasonDeleteModal = MM.modal.setupDetail(seasonDeleteOverlay, {
        activeClass: 'active',
        manageBodyOverflow: false,
        closeSelectors: ['[data-action="close-season-delete"]'],
      });
      var seasonDeleteCard = seasonDeleteOverlay.querySelector('.keep-dialog');
      if (seasonDeleteCard) {
        seasonDeleteCard.addEventListener('click', function (e) { e.stopPropagation(); });
      }
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

  /* Delegate data-keep-dur and data-action="confirm-delete" clicks
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
    if (e.target.closest('[data-remove-keep-dialog]')) {
      e.preventDefault();
      /* The dialog's Remove all clears the show's and every season's keep. */
      submitKeep(e.target.closest('[data-remove-keep-dialog]'));
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
      return;
    }
    var showDeleteBtn = e.target.closest('[data-action="pick-season-delete"]');
    if (showDeleteBtn) {
      e.preventDefault();
      var showTitle;
      try { showTitle = JSON.parse(showDeleteBtn.dataset.title); } catch (_) { showTitle = ''; }
      openSeasonDelete(showDeleteBtn.dataset.showRk || '', showTitle);
    }
  });

  /* Delegated click handler for the .th-sort buttons.
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
