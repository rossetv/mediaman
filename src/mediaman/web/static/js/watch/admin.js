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
 * show_title}) with `season_id`, the season the Keep dialog opens for ('' for
 * none; without one it opens for the season that leaves first).
 */
(function () {
  'use strict';

  var core = window.MMWatch.core;
  var el = core.el;
  var toArray = core.toArray;

  var KEEP_DAYS = { '7 days': 7, '30 days': 30, '90 days': 90 };
  var REMOVE_ALL_LABEL = 'Remove all keeps';

  /* The item ids with a keep, remove keep or delete in flight; a second
     press on the same item sends nothing. */
  var _busy = {};

  /* picked: the ids of the seasons to open with ticked beside the kept ones,
     or null for the one that leaves first. */
  var _keepDialogState = { showRk: '', hasRk: false, showKept: null, seasons: [], picked: null, showTitle: '', seasonId: '', duration: '30 days' };
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
        deletion: d.deletion === 'true',
        seasonId: null
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
      deletion: !!w.deletion,
      seasonId: w.season_id === undefined ? null : w.season_id
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
    return tracked.then(function () { if (_keepChains[key] === tracked) delete _keepChains[key]; });
  }

  function keptMessage(title, duration) {
    var name = title || 'it';
    return duration === 'forever' ? 'Kept ' + name + ' forever.' : 'Kept ' + name + ' for ' + duration + '.';
  }

  /* A keep as it was read, sent with a change to it: the server refuses the
     change (keep_changed) when the keep is no longer that one. */
  function readOf(keep) {
    return keep ? { action: keep.action, execute_at: keep.execute_at || null, kept_at: keep.kept_at || null } : null;
  }

  function keepUrl(id) { return '/api/media/' + encodeURIComponent(id) + '/keep'; }

  function keepAsRead(id, duration, keep) {
    var form = { duration: duration, read_action: keep ? keep.action : 'none' };
    if (keep) {
      form.read_execute_at = keep.execute_at || '';
      form.read_kept_at = keep.kept_at || '';
    }
    return MM.api.postForm(keepUrl(id), form);
  }

  function unprotect(id, keep) {
    return MM.api.post('/api/media/' + encodeURIComponent(id) + '/unprotect', keep ? { read: readOf(keep) } : undefined);
  }

  function changedMeanwhile(err) { return !!err && err.error === 'keep_changed'; }

  /* The Undo toast of each title's last keep, so a later change to that keep
     can take it away: its Undo would then undo the wrong thing. */
  var _undoToasts = {};

  /* *ids*: what the keep is of; *run* undoes it. */
  function offerUndo(ids, message, run) {
    ids.forEach(retireUndo);
    var entry = { spent: false };
    entry.node = window.UIFeedback.toast(message, {
      variant: 'success',
      dialog: document.documentElement.classList.contains('has-ttl') ? byId('ttl') : null,
      action: { label: 'Undo', onClick: function () {
        ids.forEach(function (id) { if (_undoToasts[id] === entry) delete _undoToasts[id]; });
        if (!entry.spent) run();
      } }
    });
    ids.forEach(function (id) { _undoToasts[id] = entry; });
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
    retireUndo(id);
    var detail = { action: 'keep', id: id, showRk: '' };
    emit('busy', detail);
    var days = KEEP_DAYS[duration];
    var viaWatch = !w.kept && w.deletion && !!days;
    var request = viaWatch
      ? MM.api.post('/api/watch/keep', { media_item_id: id, days: days })
      : !w.kept && !w.deletion
        ? keepAsRead(id, duration, null)
        : MM.api.postForm(keepUrl(id), { duration: duration });
    return request.then(function (data) {
      delete _busy[id];
      emit('done', detail);
      if (data && data.deletion_running && data.message) {
        /* Saved, but its files may already be gone: say so, with no undo. */
        window.UIFeedback.toast(data.message, { duration: 8000 });
        return true;
      }
      var undo = null;
      if (viaWatch) {
        undo = function () { return MM.api.post('/api/watch/unkeep', { media_item_id: id }); };
      } else if (!w.kept && !w.deletion && data && data.keep) {
        undo = function () { return unprotect(id, data.keep); };
      }
      if (undo) offerUndo([id], keptMessage(w.title, duration), function () { undoKeep(id, undo); });
      else window.UIFeedback.toast(keptMessage(w.title, duration), { variant: 'success' });
      return true;
    }, function (err) {
      delete _busy[id];
      emit(changedMeanwhile(err) ? 'done' : 'failed', detail);
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
      emit(changedMeanwhile(err) ? 'done' : 'failed', detail);
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
    retireUndo(id);
    backToTrigger(w);
    var detail = { action: 'unkeep', id: id, showRk: w.isTv ? w.showRk : '' };
    emit('busy', detail);
    return unprotect(id)
      .then(function () {
        delete _busy[id];
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

  /* An Undo that starts while the dialog is open for its show. */
  function holdOpenDialog() {
    if (!byId('keep-dialog-overlay').classList.contains('active')) return;
    holdKeepWhileSaving(_keepDialogRequest, function () { return byId('keep-season-list').querySelector('input'); });
  }

  function openKeepDialog(w, duration) {
    if (!_keepDialogModal) buildKeepDialog();
    _keepDialogState.showRk = w.showRk;
    _keepDialogState.hasRk = w.hasRk;
    _keepDialogState.showTitle = w.showTitle;
    _keepDialogState.seasonId = w.id;
    _keepDialogState.picked = w.seasonId === null ? null : w.seasonId ? [w.seasonId] : [];
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
    _keepDialogState.showKept = null;
    _keepDialogState.seasons = [];
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
        _keepDialogState.showKept = data.show_kept || null;
        _keepDialogState.seasons = data.seasons;
        var hasKeeps = data.show_kept || data.seasons.some(function (s) { return s.kept; });
        byId('keep-dialog-remove').classList.toggle('is-visible', hasKeeps);
        if (hasKeeps) confirmBtn.textContent = 'Update';
        renderSeasonPicker(data.seasons, _keepDialogState.showKept);
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
    document.querySelectorAll('#keep-season-list [data-keep-set]').forEach(labelSet);
  }

  function selectDurationByValue(dur) {
    document.querySelectorAll('.keep-dur-btn').forEach(function (b) { b.classList.toggle('active', b.dataset.dur === dur); });
  }

  function seasonName(s) { return s.season_number != null ? 'Season ' + s.season_number : s.title; }

  function leavesText(days) {
    return days <= 0 ? 'leaves today' : days === 1 ? 'leaves tomorrow' : 'leaves in ' + days + ' days';
  }

  function seasonDetails(s) {
    var parts = [];
    if (s.file_size) parts.push(s.file_size);
    parts.push(s.last_watched ? 'watched ' + s.last_watched : 'never watched');
    if (s.leaving) parts.push(leavesText(s.leaving.days));
    return el('span', 'keep-season-details', parts.join(' · '));
  }

  /* When a keep ends. */
  function endOf(keep) {
    if (!keep) return -Infinity;
    return keep.action === 'protected_forever' ? Infinity : Date.parse(keep.execute_at);
  }

  function lastsLonger(keep, duration) {
    if (!KEEP_DAYS[duration]) return false;
    return keep.action === 'protected_forever' || keep.days > KEEP_DAYS[duration];
  }

  /* Whether a season's keep was written by its show's keep. Two that are
     forever end alike, so they must have been made together too. */
  function isOfShow(keep, showKept) {
    return keep.action === showKept.action &&
      (keep.action === 'protected_forever' ? keep.kept_at === showKept.kept_at : keep.execute_at === showKept.execute_at);
  }

  /* The seasons the dialog opens for. */
  function pickedSeasons(seasons) {
    if (_keepDialogState.picked) return _keepDialogState.picked;
    var first = seasons.filter(function (s) { return s.leaving; })
      .sort(function (a, b) { return Date.parse(a.leaving.execute_at) - Date.parse(b.leaving.execute_at); })[0];
    return first ? [first.id] : [];
  }

  /* A keep there is changes only by this button, so that a tick alone never
     shortens one. */
  function setButton(id, name) {
    var b = button('btn btn--ghost btn--sm', '', { 'data-keep-set': id, 'data-name': name, 'aria-pressed': 'false' });
    labelSet(b);
    b.addEventListener('click', function () { pressSet(b, b.getAttribute('aria-pressed') !== 'true'); });
    return b;
  }

  function labelSet(b) {
    var dur = _keepDialogState.duration;
    b.textContent = 'Set to ' + dur;
    b.setAttribute('aria-label', 'Set to ' + dur + ', ' + b.dataset.name);
  }

  function pressSet(b, on) {
    b.setAttribute('aria-pressed', String(on));
    b.classList.toggle('btn--primary', on);
    b.classList.toggle('btn--ghost', !on);
  }

  function renderSeasonPicker(seasons, showKept) {
    var list = byId('keep-season-list');
    list.replaceChildren();
    var picked = pickedSeasons(seasons);

    /* A tick that goes takes the change to its keep with it. */
    function row(className, cb, labelText, set) {
      var item = el('div', className);
      var label = el('label', null, labelText);
      cb.type = 'checkbox';
      label.htmlFor = cb.id;
      item.appendChild(cb);
      item.appendChild(label);
      if (set) {
        cb.addEventListener('change', function () { holdSet(set, cb.checked); });
      }
      list.appendChild(item);
      return item;
    }
    function holdSet(set, on) {
      set.disabled = !on;
      if (!on) pressSet(set, false);
    }
    function keptPart(item, keep, set) {
      var part = el('span', 'keep-season-keep');
      part.appendChild(el('span', 'pill pill--kept', (keep && keep.label) || 'Kept'));
      if (set) part.appendChild(set);
      item.classList.add('keep-season-item--kept');
      item.appendChild(part);
    }

    /* A show keyed by its title has no rating key to keep the whole show
       under, so only per-season keeps are offered. */
    var allCb = el('input');
    allCb.id = 'keep-all-seasons';
    allCb.checked = !!showKept;
    var allSet = showKept ? setButton('all', 'All seasons') : null;
    if (_keepDialogState.hasRk) {
      var allRow = row('keep-season-item keep-all-row', allCb, 'All seasons', allSet);
      if (showKept) keptPart(allRow, showKept, allSet);
      allCb.addEventListener('change', function () {
        list.querySelectorAll('input[data-season-id]').forEach(function (cb) {
          cb.checked = allCb.checked;
          var set = cb.parentNode.querySelector('[data-keep-set]');
          if (set) holdSet(set, cb.checked);
        });
      });
    }

    /* A lone season is all a show row can mean. */
    var lone = seasons.length === 1 && !_keepDialogState.picked && !picked.length && !showKept && !seasons[0].kept;
    if (lone) allCb.checked = true;

    seasons.forEach(function (s) {
      var cb = el('input');
      cb.id = 'keep-season-' + s.id;
      cb.dataset.seasonId = s.id;
      cb.checked = !!showKept || s.kept || lone || picked.indexOf(s.id) >= 0;
      var set = s.kept && !showKept ? setButton(s.id, seasonName(s)) : null;
      var item = row('keep-season-item', cb, seasonName(s), set);
      cb.addEventListener('change', function () {
        if (cb.checked || !allCb.checked) return;
        allCb.checked = false;
        if (allSet) holdSet(allSet, false);
      });
      item.appendChild(seasonDetails(s));
      if (s.kept) keptPart(item, s.keep, set);
    });
  }

  /* The dialog's keep is saved: the dialog closes, if it is still the one
     that was open, and the show's row updates where it is. */
  function showKeepSaved(state, data, message, undo) {
    if (state.isOpen()) closeKeepDialog();
    emit('done', { action: 'show', id: state.seasonId, showRk: state.showRk });
    if (data && data.deletion_running && data.message) {
      window.UIFeedback.toast(state.say(data.message), { duration: 8000 });
    } else if (undo) {
      offerUndo(undo.ids, message, undo.run);
    } else {
      window.UIFeedback.toast(message, { variant: 'success' });
    }
  }

  function listNames(names) {
    return names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names[0];
  }

  /* What the ticks and the pressed buttons change of the keeps there are:
     all: the whole show is kept; keeps: the seasons kept for the duration
     chosen; endsShow: the show's own keep goes; drops: the seasons no longer
     kept; fresh: nothing but seasons that had no keep. */
  function chosenChanges() {
    var showKept = _keepDialogState.showKept;
    var list = byId('keep-season-list');
    var allCb = byId('keep-all-seasons');
    var all = !!allCb && allCb.checked;
    function isSet(id) {
      return toArray(list.querySelectorAll('[data-keep-set]')).some(function (b) {
        return b.getAttribute('data-keep-set') === id && b.getAttribute('aria-pressed') === 'true';
      });
    }
    var ticked = {};
    list.querySelectorAll('input[data-season-id]').forEach(function (cb) { ticked[cb.dataset.seasonId] = cb.checked; });
    var showSet = isSet('all');
    var duration = _keepDialogState.duration;
    var c = { all: all && (!showKept || showSet), keeps: [], endsShow: !!showKept && !all, drops: [], fresh: !showKept && !all, ticked: 0 };
    _keepDialogState.seasons.forEach(function (s) {
      if (!ticked[s.id]) {
        if (s.kept || showKept) c.drops.push(s);
        return;
      }
      c.ticked++;
      if (showKept) {
        /* A season's own keep that outlasts the show's is no part of it, nor
           is one that would be shortened. */
        if (showSet && (!s.keep || isOfShow(s.keep, showKept) ||
            (endOf(s.keep) <= endOf(showKept) && !lastsLonger(s.keep, duration)))) c.keeps.push(s);
      } else if (!s.kept || isSet(s.id)) {
        c.keeps.push(s);
        if (s.kept) c.fresh = false;
      }
    });
    if (c.drops.length) c.fresh = false;
    return c;
  }

  function nothingChosen(c) {
    if (!c.ticked) return 'Select at least one season to continue.';
    if (c.all && !c.keeps.length) {
      return _keepDialogState.showKept
        ? 'Every season has a longer keep of its own.'
        : 'Every season has a keep of its own. Press “Set to” on one to change it.';
    }
    if (!c.keeps.length && !c.drops.length && !c.endsShow) {
      return 'Nothing to change yet. Tick a season, or press “Set to” on a kept one.';
    }
    return '';
  }

  /* Says which seasons a save kept and which it let go. */
  function savedMessage(showTitle, c, duration) {
    var title = showTitle || 'the show';
    var names = c.keeps.map(seasonName).concat(c.all ? ['new seasons'] : []);
    var dropped = c.drops.map(seasonName);
    if (names.length) {
      return keptMessage(title + ', ' + listNames(names), duration) +
        (dropped.length ? ' Removed the keep on ' + listNames(dropped) + '.' : '');
    }
    return 'Removed the keep on ' + title + (dropped.length ? ', ' + listNames(dropped) : ' for new seasons') + '.';
  }

  function viaWatch(s, duration) { return !!s.leaving && !s.kept && !!KEEP_DAYS[duration]; }

  /* A season that leaves is kept for 7, 30 or 90 days through the Watch keep,
     whose undo brings its date back; see keep(). */
  function keepSeason(s, duration) {
    if (viaWatch(s, duration)) return MM.api.post('/api/watch/keep', { media_item_id: s.id, days: KEEP_DAYS[duration] });
    return keepAsRead(s.id, duration, s.keep);
  }

  /* Undo for seasons that had no keep before; null when one had its deletion
     replaced for good. made: the keeps this save wrote, by season. */
  function seasonsUndo(c, duration, state, chainKey, made) {
    if (!c.fresh) return null;
    if (!c.keeps.every(function (s) { return viaWatch(s, duration) || (!s.leaving && made[s.id]); })) return null;
    var detail = { action: 'unkeep', id: state.seasonId, showRk: state.showRk };
    function undoOne(s) {
      return s.leaving ? MM.api.post('/api/watch/unkeep', { media_item_id: s.id }) : unprotect(s.id, made[s.id]);
    }
    /* One at a time with every other change to this show. */
    function run() {
      var failed = [];
      var changed = false;
      var before = _keepChains[chainKey] || Promise.resolve();
      trackKeepChain(chainKey, before.then(function () {
        emit('busy', detail);
        return c.keeps.reduce(function (chain, s) {
          return chain.then(function () { return undoOne(s); }).catch(function (err) {
            changed = changed || changedMeanwhile(err);
            failed.push(seasonName(s) + ': ' + MM.api.messageOf(err, 'Try again.'));
          });
        }, Promise.resolve());
      }).then(function () {
        emit(changed || failed.length < c.keeps.length ? 'done' : 'failed', detail);
        if (failed.length) window.UIFeedback.error("Couldn't undo the keep. " + failed.join(' '));
        else window.UIFeedback.toast('Keep undone.');
      }));
      holdOpenDialog();
    }
    return { ids: c.keeps.map(function (s) { return s.id; }), run: run };
  }

  function idsOf(seasons) { return seasons.map(function (s) { return s.id; }); }

  /* Every keep of the show as the dialog read it. */
  function showAsRead() {
    var seasons = {};
    _keepDialogState.seasons.forEach(function (s) { seasons[s.id] = readOf(s.keep); });
    return { show: readOf(_keepDialogState.showKept), seasons: seasons };
  }

  var SHOW_CHANGED = 'The seasons or keeps of this show changed while this was open, so nothing was saved.';

  function changedMessage(names) {
    return listNames(names) + ' changed while this was open, so ' +
      (names.length > 1 ? 'they were left as they are now.' : 'it was left as it is now.');
  }

  function confirmKeepDialog() {
    var c = chosenChanges();
    var refusal = nothingChosen(c);
    if (refusal) {
      window.UIFeedback.error(refusal);
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
    var read = showAsRead();
    var dur = _keepDialogState.duration || '30 days';
    var request = _keepDialogRequest;
    var chainKey = keepChainKey();
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
    /* The Undo of an earlier keep this changes would undo the wrong thing. */
    (c.all || c.endsShow ? _keepDialogState.seasons : c.keeps.concat(c.drops)).forEach(function (s) { retireUndo(s.id); });
    emit('busy', rowDetail);
    function done(data, undo) {
      showKeepSaved(state, data, savedMessage(showTitle, c, dur), undo);
    }
    /* Read the seasons again, with the seasons *picked* ticked. */
    function readAgain(picked) {
      var refocus = document.activeElement === list || MM.dom.focusStillIn(btn);
      _keepDialogState.picked = picked;
      if (refocus) {
        list.tabIndex = -1;
        list.focus();
      }
      loadKeepSeasons(refocus ? function () { return btn; } : undefined);
    }

    /* What touches the show's own keep is one change on the server: all of it
       or none. Ending it, the server keeps the seasons left ticked at least
       as long as it lasted. */
    if (c.all || c.endsShow) {
      var failure = null;
      trackKeepChain(chainKey, (c.all
        ? MM.api.post('/api/show/' + encodeURIComponent(rk) + '/keep', { duration: dur, season_ids: idsOf(c.keeps), read: read })
        : MM.api.post('/api/show/' + encodeURIComponent(rk) + '/remove', { read: read, drops: idsOf(c.drops) })
      ).then(done).catch(function (err) {
        failure = err;
        window.UIFeedback.error(say(changedMeanwhile(err)
          ? SHOW_CHANGED
          : "Couldn't save keep. " + MM.api.messageOf(err, 'Try again.')));
        emit(changedMeanwhile(err) ? 'done' : 'failed', rowDetail);
      })).then(function () {
        if (!failure || !state.isOpen()) return;
        if (changedMeanwhile(failure)) {
          readAgain([]);
          return;
        }
        var refocus = document.activeElement === list || MM.dom.focusStillIn(btn);
        btn.disabled = false;
        btn.textContent = btnLabel;
        if (removeAllBtn) removeAllBtn.disabled = false;
        if (refocus) btn.focus();
      });
      return;
    }

    /* Sequential loop — accumulates failures, continues after each one. */
    var keepChain = Promise.resolve();
    var failed = [];
    var changed = [];
    var unsaved = [];
    var made = {};
    var saved = 0;
    var running = null;
    c.keeps.forEach(function (s) {
      keepChain = keepChain.then(function () {
        return keepSeason(s, dur).then(function (data) {
          saved++;
          made[s.id] = data && data.keep;
          if (data && data.deletion_running) running = data;
        }).catch(function (err) {
          /* The Watch keep refuses a season that no longer leaves as read. */
          if (changedMeanwhile(err) || (viaWatch(s, dur) && err && (err.status === 404 || err.status === 409))) {
            changed.push(seasonName(s));
            return;
          }
          failed.push(seasonName(s));
          unsaved.push(s.id);
        });
      });
    });
    /* Once every ticked season is kept, an unticked season loses its keep. */
    keepChain = keepChain.then(function () {
      if (failed.length || changed.length) return null;
      return c.drops.reduce(function (chain, s) {
        return chain.then(function () { return unprotect(s.id, s.keep); }).then(function () { saved++; }, function (err) {
          (changedMeanwhile(err) ? changed : failed).push(seasonName(s));
        });
      }, Promise.resolve());
    });
    var chain = keepChain.then(function () {
      if (!failed.length && !changed.length) {
        done(running, seasonsUndo(c, dur, state, chainKey, made));
        return;
      }
      var said = failed.length ? ['Keep failed for: ' + failed.join(', ') + '.'] : [];
      if (changed.length) said.push(changedMessage(changed));
      window.UIFeedback.error(say(said.join(' ')));
      if (running && running.message) {
        window.UIFeedback.toast(say(running.message), { duration: 8000 });
      }
      emit(saved || changed.length ? 'done' : 'failed', rowDetail);
    });
    trackKeepChain(chainKey, chain).then(function () {
      /* Some of it may be saved: show what is, with the rest still ticked. */
      if ((failed.length || changed.length) && state.isOpen()) readAgain(unsaved);
    });
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
      /* Like a keep, the requests outlive the dialog, so what they need is
         read now and the dialog is touched only while it is the same one. */
      var request = _keepDialogRequest;
      var chainKey = keepChainKey();
      var before = _keepChains[chainKey];
      if (!ok) { removeBtn.disabled = !!before; return; }
      var showTitle = _keepDialogState.showTitle;
      var rk = _keepDialogState.showRk;
      var state = {
        showRk: rk,
        seasonId: _keepDialogState.seasonId,
        isOpen: function () { return request === _keepDialogRequest; },
        say: function (msg) { return state.isOpen() || !showTitle ? msg : showTitle + ': ' + msg; }
      };
      var rowDetail = { action: 'unkeep', id: state.seasonId, showRk: rk };
      var seasons = _keepDialogState.seasons;
      seasons.forEach(function (s) { retireUndo(s.id); });
      emit('busy', rowDetail);
      var allRequests = [];
      var whole = !!rk && !!_keepDialogState.showKept;
      if (whole) {
        var body = { read: showAsRead(), drops: idsOf(seasons) };
        allRequests.push({
          send: function () { return MM.api.post('/api/show/' + encodeURIComponent(rk) + '/remove', body); },
          name: 'the show keep'
        });
      } else {
        seasons.forEach(function (s) {
          if (s.kept) allRequests.push({ send: function () { return unprotect(s.id, s.keep); }, name: seasonName(s) });
        });
      }

      var removeChain = before || Promise.resolve();
      var failed = [];
      var changed = [];
      allRequests.forEach(function (req) {
        removeChain = removeChain.then(req.send)
          .catch(function (err) { (changedMeanwhile(err) ? changed : failed).push(req.name); });
      });
      var chain = removeChain.then(function () {
        var missed = failed.length + changed.length;
        if (missed) {
          emit(changed.length || missed < allRequests.length ? 'done' : 'failed', rowDetail);
          var said = failed.length ? ['Remove failed for: ' + failed.join(', ') + '.'] : [];
          if (changed.length) said.push(whole ? SHOW_CHANGED : changedMessage(changed));
          window.UIFeedback.error(state.say(said.join(' ')));
        } else {
          showKeepSaved(state, null, 'Removed the keeps on ' + (showTitle || 'the show') + '.');
        }
      });
      trackKeepChain(chainKey, chain);
      /* Holds Keep and Remove all, then reads the seasons again, so a retry
         sends only what is still kept. Remove all was disabled through the
         requests, so focus fell to the page: after a failure it goes back. */
      holdKeepWhileSaving(request, function () {
        return failed.length || changed.length ? removeBtn : null;
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
      retireUndo(id);
      emit('busy', detail);
      return MM.api.post('/api/media/' + encodeURIComponent(id) + '/delete')
        .then(function () {
          delete _busy[id];
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
      var name = el('span', 'keep-season-name', seasonName(s));
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
