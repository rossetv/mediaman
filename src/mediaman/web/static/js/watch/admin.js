/* Watch: the admin actions shared by the list view and the title detail, on
 * window.MMWatch.admin: Keep (and the TV Keep dialog), Remove keep, Delete
 * with its confirm, and the per-season Delete dialog. Needs watch/core.js.
 *
 * Nothing here reloads the page. Each action announces itself on the
 * document as an `mm:watch-admin` event, {phase: 'busy'|'done'|'failed',
 * action: 'keep'|'unkeep'|'show'|'delete', id, showRk}, and whatever shows
 * the title (a list row, the detail) updates itself in place.
 *
 * A wrapper is either a list row's .keep-wrapper (data-* attributes) or the
 * detail's `actions.keep_wrapper` object ({id, is_tv, show_rk, has_rk,
 * show_title}).
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;

  var KEEP_DAYS = { '7 days': 7, '30 days': 30, '90 days': 90 };
  var REMOVE_ALL_LABEL = 'Remove all keeps';

  /* The item ids with a keep, remove keep or delete in flight; a second
     press on the same item sends nothing. */
  var _busy = {};

  var _keepDialogState = { showRk: '', hasRk: false, showKept: false, keptSeasonIds: [], showTitle: '', seasonId: '', duration: '30 days' };
  var _keepDialogModal = null;
  var _seasonDeleteModal = null;

  /* Each dialog's season fetch is numbered; a response that is not the
     latest belongs to a show no longer open and is dropped. */
  var _keepDialogRequest = 0;
  var _seasonDeleteRequest = 0;

  /* Keep chains still running, by show. A dialog opened for such a show keeps
     Keep disabled until its chain settles, so two chains never interleave. */
  var _keepChains = {};

  function emit(phase, detail) {
    detail.phase = phase;
    document.dispatchEvent(new CustomEvent('mm:watch-admin', { detail: detail, bubbles: true }));
  }

  function parseJson(value, fallback) {
    try { return JSON.parse(value); } catch (_) { return fallback; }
  }

  function info(wrapper) {
    if (wrapper && wrapper.dataset) {
      var d = wrapper.dataset;
      return {
        el: wrapper,
        id: d.id || '',
        isTv: d.tv === 'true',
        showRk: d.showRk || '',
        hasRk: d.hasRk === 'true',
        showTitle: d.showTitle || '',
        title: parseJson(d.title, '') || d.showTitle || '',
        kept: d.kept === 'true',
        deletion: d.deletion === 'true'
      };
    }
    var w = wrapper || {};
    return {
      el: null,
      id: w.id == null ? '' : String(w.id),
      isTv: !!w.is_tv,
      showRk: w.show_rk || '',
      hasRk: !!w.has_rk,
      showTitle: w.show_title || '',
      title: w.title || w.show_title || '',
      kept: !!w.kept,
      deletion: !!w.deletion
    };
  }

  function triggerOf(w) {
    return w.el ? w.el.querySelector('.keep-trigger, .btn-sm-caret') : null;
  }

  function closeMenus() {
    if (window.SnoozeMenu) window.SnoozeMenu.closeAll(null);
  }

  /* After a failure the menu closes and focus goes back to its trigger, but
     only while the person is still there. */
  function backToTrigger(w) {
    closeMenus();
    var trigger = triggerOf(w);
    if (trigger && MM.dom.focusStillIn(w.el)) trigger.focus();
  }

  /* A press refused because another request is in flight: close the menu,
     say why, and put focus back on the trigger. */
  function explainBusy(w, text) {
    closeMenus();
    window.UIFeedback.toast(text);
    var trigger = triggerOf(w);
    if (trigger) trigger.focus();
  }

  function chainKeyFor(showRk, showTitle) {
    return showRk || 'title:' + showTitle;
  }

  function keepChainKey() {
    return chainKeyFor(_keepDialogState.showRk, _keepDialogState.showTitle);
  }

  function trackKeepChain(key, chain) {
    var tracked = chain.then(function () {}, function () {});
    _keepChains[key] = tracked;
    tracked.then(function () { if (_keepChains[key] === tracked) delete _keepChains[key]; });
  }

  function keptMessage(title, duration) {
    var name = title || 'it';
    return duration === 'forever' ? 'Kept ' + name + ' forever.' : 'Kept ' + name + ' for ' + duration + '.';
  }

  /* The Undo toast of each title's last keep, so a later change to that keep
     can take it away: its Undo would then undo the wrong thing. */
  var _undoToasts = {};

  function offerUndo(id, message, undo) {
    retireUndo(id);
    var entry = { spent: false };
    entry.node = window.UIFeedback.toast(message, {
      variant: 'success',
      action: { label: 'Undo', onClick: function () {
        if (_undoToasts[id] === entry) delete _undoToasts[id];
        if (!entry.spent) undoKeep(id, undo);
      } }
    });
    _undoToasts[id] = entry;
  }

  /* Dismiss the toast the way its own button would, without running its Undo. */
  function retireUndo(id) {
    var entry = _undoToasts[id];
    if (!entry) return;
    delete _undoToasts[id];
    entry.spent = true;
    var btn = entry.node && entry.node.querySelector && entry.node.querySelector('.ui-toast__action');
    if (btn) btn.click();
  }

  /* ── Keep ── */

  /* A movie (or a lone season) is kept at once. An item that was neither kept
     nor scheduled can be put back by removing the keep; one that was
     scheduled is kept for 7, 30 or 90 days through the Watch keep, whose
     undo brings its deletion date back. Keeping a scheduled item forever
     replaces its deletion and no route can bring the date back, so that,
     like changing an existing keep, has no undo. */
  function keep(wrapper, duration) {
    var w = info(wrapper);
    if (w.isTv) {
      if (_busy[w.id]) {
        explainBusy(w, 'Still removing the keep for this show. Try again in a moment.');
        return Promise.resolve(false);
      }
      openKeepDialog(w, duration);
      return Promise.resolve(false);
    }
    if (!w.id || _busy[w.id]) return Promise.resolve(false);
    _busy[w.id] = 'keep';
    backToTrigger(w);
    var id = w.id;
    var detail = { action: 'keep', id: id, showRk: '' };
    emit('busy', detail);
    var days = KEEP_DAYS[duration];
    var viaWatch = !w.kept && w.deletion && !!days;
    var request = viaWatch
      ? MM.api.post('/api/watch/keep', { media_item_id: id, days: days })
      : MM.api.postForm('/api/media/' + encodeURIComponent(id) + '/keep', { duration: duration });
    return request.then(function (data) {
      delete _busy[id];
      emit('done', detail);
      if (data && data.deletion_running && data.message) {
        /* Saved, but its files may already be gone: say so, with no undo. */
        retireUndo(id);
        window.UIFeedback.toast(data.message, { duration: 8000 });
        return true;
      }
      var undo = null;
      if (viaWatch) {
        undo = function () { return MM.api.post('/api/watch/unkeep', { media_item_id: id }); };
      } else if (!w.kept && !w.deletion) {
        undo = function () { return MM.api.post('/api/media/' + encodeURIComponent(id) + '/unprotect'); };
      }
      if (undo) offerUndo(id, keptMessage(w.title, duration), undo);
      else {
        retireUndo(id);
        window.UIFeedback.toast(keptMessage(w.title, duration), { variant: 'success' });
      }
      return true;
    }, function (err) {
      delete _busy[id];
      emit('failed', detail);
      window.UIFeedback.error("Couldn't save keep. " + MM.api.messageOf(err, 'Try again.'));
      backToTrigger(w);
      return false;
    });
  }

  function undoKeep(id, undo) {
    if (_busy[id]) return;
    _busy[id] = 'undo';
    var detail = { action: 'unkeep', id: id, showRk: '' };
    emit('busy', detail);
    undo().then(function () {
      delete _busy[id];
      emit('done', detail);
      window.UIFeedback.toast('Keep undone.');
    }, function (err) {
      delete _busy[id];
      emit('failed', detail);
      window.UIFeedback.error("Couldn't undo the keep. " + MM.api.messageOf(err, 'Try again.'));
    });
  }

  /* Guarded like a keep, on the same flag, so an item never has two requests
     in flight. */
  function removeKeep(wrapper, mediaId) {
    var w = info(wrapper);
    var id = mediaId == null ? w.id : String(mediaId);
    if (!id || _busy[id]) return Promise.resolve(false);
    /* Nor while a keep from the dialog is still saving for this show. */
    if (w.isTv && _keepChains[chainKeyFor(w.showRk, w.showTitle)]) {
      explainBusy(w, 'Still saving the keep for this show. Try again in a moment.');
      return Promise.resolve(false);
    }
    _busy[id] = 'unkeep';
    backToTrigger(w);
    var detail = { action: 'unkeep', id: id, showRk: w.isTv ? w.showRk : '' };
    emit('busy', detail);
    return MM.api.post('/api/media/' + encodeURIComponent(id) + '/unprotect')
      .then(function () {
        delete _busy[id];
        retireUndo(id);
        emit('done', detail);
        window.UIFeedback.toast('Removed the keep on ' + (w.title || 'it') + '.');
        return true;
      }, function (err) {
        delete _busy[id];
        emit('failed', detail);
        window.UIFeedback.error("Couldn't remove keep. " + MM.api.messageOf(err, 'Try again.'));
        backToTrigger(w);
        return false;
      });
  }

  /* ── The TV Keep dialog ── */

  function button(className, text, attrs) {
    var b = el('button', className, text);
    b.type = 'button';
    Object.keys(attrs || {}).forEach(function (k) { b.setAttribute(k, attrs[k]); });
    return b;
  }

  function overlay(id, titleId, title) {
    var root = el('div', 'keep-dialog-overlay wl-dialog');
    root.id = id;
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', titleId);
    root.setAttribute('aria-hidden', 'true');
    var card = el('div', 'keep-dialog');
    var heading = el('h3', null, title);
    heading.id = titleId;
    card.appendChild(heading);
    root.appendChild(card);
    /* A click inside the card must not reach the overlay's backdrop close. */
    card.addEventListener('click', function (e) { e.stopPropagation(); });
    return { root: root, card: card };
  }

  function buildKeepDialog() {
    var d = overlay('keep-dialog-overlay', 'keep-dialog-title', 'Keep Show');
    var bar = el('div', 'keep-duration-bar');
    bar.id = 'keep-duration-bar';
    ['7 days', '30 days', '90 days', 'forever'].forEach(function (dur) {
      var b = button('keep-dur-btn', dur === 'forever' ? 'Forever' : dur, { 'data-dur': dur });
      b.addEventListener('click', function () { selectDuration(b); });
      bar.appendChild(b);
    });
    var list = el('div', 'keep-season-list');
    list.id = 'keep-season-list';
    var actions = el('div', 'keep-dialog-actions');
    var remove = button('btn btn--danger btn--sm', REMOVE_ALL_LABEL, { id: 'keep-dialog-remove' });
    var confirm = button('btn btn--primary btn--sm', 'Update', { id: 'keep-dialog-confirm' });
    actions.appendChild(remove);
    actions.appendChild(el('div', 'u-flex-1'));
    actions.appendChild(button('btn btn--ghost btn--sm', 'Cancel', { 'data-action': 'close-keep-dialog' }));
    actions.appendChild(confirm);
    d.card.appendChild(bar);
    d.card.appendChild(list);
    d.card.appendChild(actions);
    document.body.appendChild(d.root);
    remove.addEventListener('click', removeAllKeeps);
    confirm.addEventListener('click', confirmKeepDialog);
    _keepDialogModal = MM.modal.setupDetail(d.root, {
      activeClass: 'active',
      manageBodyOverflow: false,
      closeSelectors: ['[data-action="close-keep-dialog"]']
    });
  }

  function buildSeasonDeleteDialog() {
    var d = overlay('season-delete-overlay', 'season-delete-title', 'Delete seasons');
    var list = el('div', 'keep-season-list');
    list.id = 'season-delete-list';
    var actions = el('div', 'keep-dialog-actions');
    actions.appendChild(el('div', 'u-flex-1'));
    actions.appendChild(button('btn btn--ghost btn--sm', 'Close', { 'data-action': 'close-season-delete' }));
    d.card.appendChild(list);
    d.card.appendChild(actions);
    document.body.appendChild(d.root);
    _seasonDeleteModal = MM.modal.setupDetail(d.root, {
      activeClass: 'active',
      manageBodyOverflow: false,
      closeSelectors: ['[data-action="close-season-delete"]']
    });
  }

  function byId(id) { return document.getElementById(id); }

  /* The note has a line of its own above the buttons, made on first use. */
  function keepChainNote() {
    var note = byId('keep-dialog-note');
    if (!note) {
      var actions = byId('keep-dialog-confirm').parentNode;
      note = el('p', 'keep-dialog-note');
      note.id = 'keep-dialog-note';
      note.setAttribute('role', 'status');
      actions.parentNode.insertBefore(note, actions);
    }
    return note;
  }

  /* While a chain for this show runs, neither Keep/Update nor Remove all may
     start another. When it settles, a dialog still showing the show reads the
     seasons again, since the chain changed them; focusAfter, if given, says
     what to focus once they are back. removing: the chain is this dialog's
     own Remove all, which says so on its button instead of in the note. */
  function holdKeepWhileSaving(request, focusAfter, removing) {
    var note = keepChainNote();
    var chain = _keepChains[keepChainKey()];
    var confirmBtn = byId('keep-dialog-confirm');
    var removeBtn = byId('keep-dialog-remove');
    note.textContent = chain && !removing ? 'Saving the previous change…' : '';
    removeBtn.textContent = chain && removing ? 'Removing…' : REMOVE_ALL_LABEL;
    removeBtn.disabled = !!chain;
    if (!chain) return;
    confirmBtn.disabled = true;
    chain.then(function () {
      if (request !== _keepDialogRequest) return;
      confirmBtn.disabled = false;
      removeBtn.disabled = false;
      removeBtn.textContent = REMOVE_ALL_LABEL;
      note.textContent = '';
      loadKeepSeasons(focusAfter);
    });
  }

  function openKeepDialog(w, duration) {
    if (!_keepDialogModal) buildKeepDialog();
    _keepDialogState.showRk = w.showRk;
    _keepDialogState.hasRk = w.hasRk;
    _keepDialogState.showTitle = w.showTitle;
    _keepDialogState.seasonId = w.id;
    _keepDialogState.duration = duration || '30 days';
    /* Close the menu this came from, and park focus on its trigger so
       closing the dialog returns there rather than to a hidden menu item. */
    closeMenus();
    var trigger = triggerOf(w);
    if (trigger) trigger.focus();
    _keepDialogModal.open();
    selectDurationByValue(_keepDialogState.duration);
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

  /* focusAfter returns the element to focus once the seasons are shown; it
     is used only while focus is still in the list or nowhere. */
  function loadKeepSeasons(focusAfter) {
    _keepDialogState.showKept = false;
    _keepDialogState.keptSeasonIds = [];
    var list = byId('keep-season-list');
    list.textContent = 'Loading…';
    byId('keep-dialog-remove').classList.remove('is-visible');
    var confirmBtn = byId('keep-dialog-confirm');
    confirmBtn.textContent = 'Keep';
    confirmBtn.disabled = true;
    var request = ++_keepDialogRequest;
    holdKeepWhileSaving(request);
    MM.api.get(seasonsUrl())
      .then(function (data) {
        if (request !== _keepDialogRequest) return;
        byId('keep-dialog-title').textContent = 'Keep ' + (data.show_title || _keepDialogState.showTitle || 'Show');
        _keepDialogState.showKept = !!data.show_kept;
        _keepDialogState.keptSeasonIds = data.seasons.filter(function (s) { return s.kept; }).map(function (s) { return s.id; });
        var hasKeeps = data.show_kept || data.seasons.some(function (s) { return s.kept; });
        byId('keep-dialog-remove').classList.toggle('is-visible', hasKeeps);
        if (hasKeeps) confirmBtn.textContent = 'Update';
        renderSeasonPicker(data.seasons, data.show_kept);
        /* A keep still running for this show holds Keep until it settles. */
        confirmBtn.disabled = !!_keepChains[keepChainKey()];
        if (focusAfter && MM.dom.focusStillIn(list)) {
          var target = focusAfter();
          if (target) target.focus();
        }
      })
      .catch(function () {
        if (request !== _keepDialogRequest) return;
        showSeasonsLoadFailure(list, confirmBtn);
      });
  }

  /* Keep stays off until the seasons load, so it can never report an empty
     pick; Retry reads them again. */
  function showSeasonsLoadFailure(list, confirmBtn) {
    confirmBtn.disabled = true;
    list.replaceChildren();
    var retry = button('btn btn--secondary btn--sm', 'Retry', { id: 'keep-seasons-retry' });
    retry.addEventListener('click', function () {
      /* The button goes with the message; park focus on the list meanwhile. */
      list.tabIndex = -1;
      list.focus();
      loadKeepSeasons(function () { return list.querySelector('input'); });
    });
    list.appendChild(el('span', null, "Couldn't load the seasons. "));
    list.appendChild(retry);
    retry.focus();
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

  function seasonDetails(s) {
    var parts = [];
    if (s.file_size) parts.push(s.file_size);
    parts.push(s.last_watched ? 'watched ' + s.last_watched : 'never watched');
    return el('span', 'keep-season-details', parts.join(' · '));
  }

  function renderSeasonPicker(seasons, showKept) {
    var list = byId('keep-season-list');
    list.replaceChildren();

    /* A show keyed by its title has no rating key to keep the whole show
       under, so only per-season keeps are offered. */
    var allRow = el('div', 'keep-season-item keep-all-row');
    var allCb = el('input');
    allCb.type = 'checkbox';
    allCb.id = 'keep-all-seasons';
    var anyKept = showKept || seasons.some(function (s) { return s.kept; });
    allCb.checked = !!showKept || !anyKept;
    allCb.addEventListener('change', function () {
      list.querySelectorAll('input[data-season-id]').forEach(function (cb) { cb.checked = allCb.checked; });
    });
    var allLabel = el('label', null, 'All seasons');
    allLabel.htmlFor = 'keep-all-seasons';
    allRow.appendChild(allCb);
    allRow.appendChild(allLabel);
    if (_keepDialogState.hasRk) list.appendChild(allRow);

    seasons.forEach(function (s) {
      var row = el('div', 'keep-season-item');
      var cb = el('input');
      cb.type = 'checkbox';
      cb.id = 'keep-season-' + s.id;
      cb.dataset.seasonId = s.id;
      if (s.kept) cb.dataset.kept = '1';
      cb.checked = !!showKept || s.kept || !anyKept;
      cb.addEventListener('change', function () {
        if (!cb.checked) allCb.checked = false;
      });
      var label = el('label', null, 'Season ' + s.season_number);
      label.htmlFor = 'keep-season-' + s.id;
      row.appendChild(cb);
      row.appendChild(label);
      row.appendChild(seasonDetails(s));
      if (s.kept) row.appendChild(el('span', 'pill pill--kept', 'Kept'));
      list.appendChild(row);
    });
  }

  /* The dialog's keep is saved: the dialog closes, if it is still the one
     that was open, and the show's row updates where it is. */
  function showKeepSaved(state, data, message) {
    if (state.isOpen()) closeKeepDialog();
    emit('done', { action: 'show', id: state.seasonId, showRk: state.showRk });
    if (data && data.deletion_running && data.message) {
      window.UIFeedback.toast(state.say(data.message), { duration: 8000 });
    } else {
      window.UIFeedback.toast(message, { variant: 'success' });
    }
  }

  function confirmKeepDialog() {
    var allCb = byId('keep-all-seasons');
    var allChecked = !!allCb && allCb.checked;
    var selectedIds = [];
    document.querySelectorAll('#keep-season-list input[data-season-id]:checked').forEach(function (cb) {
      selectedIds.push(cb.dataset.seasonId);
    });

    if (selectedIds.length === 0 && !allChecked) {
      window.UIFeedback.error('Select at least one season to continue.');
      return;
    }

    var btn = byId('keep-dialog-confirm');
    var btnLabel = btn.textContent;
    /* Disabling the focused button would drop focus out of the dialog; park
       it on the season list, as Retry does, until the button is back. */
    var list = byId('keep-season-list');
    if (document.activeElement === btn) {
      list.tabIndex = -1;
      list.focus();
    }
    btn.disabled = true;
    btn.textContent = 'Keeping…';
    var removeAllBtn = byId('keep-dialog-remove');
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
    var state = {
      showRk: rk,
      seasonId: _keepDialogState.seasonId,
      isOpen: function () { return request === _keepDialogRequest; },
      /* A toast from a dialog no longer shown must say whose keep it is. */
      say: function (msg) { return state.isOpen() || !showTitle ? msg : showTitle + ': ' + msg; }
    };
    var say = state.say;
    var rowDetail = { action: 'show', id: state.seasonId, showRk: rk };
    emit('busy', rowDetail);
    function restoreButton() {
      emit('failed', rowDetail);
      if (!state.isOpen()) return;
      var refocus = document.activeElement === list || MM.dom.focusStillIn(btn);
      btn.disabled = false;
      btn.textContent = btnLabel;
      if (removeAllBtn) removeAllBtn.disabled = false;
      if (refocus) btn.focus();
    }
    function done(data) {
      showKeepSaved(state, data, keptMessage(showTitle, dur));
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
          var msg = MM.api.messageOf(err, 'Try again.');
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
              if (state.isOpen()) _keepDialogState.showKept = false;
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
    var removeBtn = byId('keep-dialog-remove');
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
      var state = {
        showRk: rk,
        seasonId: _keepDialogState.seasonId,
        isOpen: function () { return request === _keepDialogRequest; },
        say: function (msg) { return state.isOpen() || !showTitle ? msg : showTitle + ': ' + msg; }
      };
      var rowDetail = { action: 'unkeep', id: state.seasonId, showRk: rk };
      emit('busy', rowDetail);
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
          emit('failed', rowDetail);
          window.UIFeedback.error(state.say('Remove failed for: ' + failed.join(', ')));
        } else {
          showKeepSaved(state, null, 'Removed the keeps on ' + (showTitle || 'the show') + '.');
        }
      });
      trackKeepChain(chainKey, chain);
      /* Holds Keep and Remove all, then reads the seasons again, so a retry
         sends only what is still kept. Remove all was disabled through the
         requests, so focus fell to the page: after a failure it goes back. */
      holdKeepWhileSaving(request, function () {
        return failed.length ? removeBtn : null;
      }, true);
    });
  }

  /* ── Delete ── */

  function deleteBody(opts) {
    var what = opts.kind === 'season' ? 'the season' : (opts.kind === 'movie' ? 'the film' : 'this title');
    var files = opts.size ? ' and its ' + opts.size + ' of files' : ' and its files';
    return 'This deletes ' + what + files + ". It can't be undone.";
  }

  /* opts: {kind: 'movie'|'season', size, showRk}. Resolves true once the
     item is gone. */
  function confirmDelete(mediaId, title, opts) {
    opts = opts || {};
    var id = mediaId == null ? '' : String(mediaId);
    if (!id || _busy[id]) return Promise.resolve(false);
    var detail = { action: 'delete', id: id, showRk: opts.showRk || '' };
    return window.UIFeedback.confirm({
      title: 'Delete ' + (title || 'this item') + '?',
      body: deleteBody(opts),
      confirmLabel: 'Delete',
      confirmVariant: 'danger'
    }).then(function (ok) {
      if (!ok || _busy[id]) return false;
      _busy[id] = 'delete';
      emit('busy', detail);
      return MM.api.post('/api/media/' + encodeURIComponent(id) + '/delete')
        .then(function () {
          delete _busy[id];
          retireUndo(id);
          emit('done', detail);
          window.UIFeedback.toast('Deleted ' + (title || 'the item') + '.', { variant: 'success' });
          return true;
        }, function (err) {
          delete _busy[id];
          emit('failed', detail);
          window.UIFeedback.error("Couldn't delete " + (title || 'the item') + '. ' + MM.api.messageOf(err, 'Try again.'));
          return false;
        });
    });
  }

  /* The per-season Delete list for a grouped show. Nothing is deleted from
     here: each season's own Delete goes through confirmDelete. */
  function pickSeasonDelete(showRk, showTitle) {
    if (!_seasonDeleteModal) buildSeasonDeleteDialog();
    var list = byId('season-delete-list');
    byId('season-delete-title').textContent = 'Delete from ' + (showTitle || 'show');
    _seasonDeleteModal.open();
    var request = ++_seasonDeleteRequest;
    if (!showRk) { list.textContent = "Couldn't load seasons."; return; }
    list.textContent = 'Loading…';
    MM.api.get('/api/show/' + encodeURIComponent(showRk) + '/seasons')
      .then(function (data) {
        if (request === _seasonDeleteRequest) renderSeasonDeleteList(data, showRk, showTitle);
      })
      .catch(function () {
        if (request === _seasonDeleteRequest) list.textContent = "Couldn't load seasons.";
      });
  }

  function renderSeasonDeleteList(data, showRk, showTitle) {
    var list = byId('season-delete-list');
    list.replaceChildren();
    if (!data.seasons.length) { list.textContent = 'No seasons found.'; return; }
    var title = data.show_title || showTitle;

    data.seasons.forEach(function (s) {
      var row = el('div', 'keep-season-item');
      var name = el('span', 'keep-season-name', s.season_number != null ? 'Season ' + s.season_number : s.title);
      row.appendChild(name);
      row.appendChild(seasonDetails(s));
      /* A kept season gets no Delete, as a kept row gets none in the list. */
      if (s.kept || data.show_kept) {
        row.appendChild(el('span', 'pill pill--kept', 'Kept'));
      } else {
        var del = button('btn btn--danger btn--sm', 'Delete');
        del.addEventListener('click', function () {
          _seasonDeleteModal.close();
          confirmDelete(String(s.id), title + ' — ' + name.textContent,
            { kind: 'season', size: s.file_size, showRk: showRk });
        });
        row.appendChild(del);
      }
      list.appendChild(row);
    });
  }

  window.MMWatch.admin = {
    keep: keep,
    removeKeep: removeKeep,
    confirmDelete: confirmDelete,
    pickSeasonDelete: pickSeasonDelete
  };
})();
