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

  /* The Undo toast of each title's last keep, so a later change to that keep
     can take it away: its Undo would then undo the wrong thing. */
  var _undoToasts = {};

  /* *ids*: what the keep is of; *run* undoes it. */
  function offerUndo(ids, message, run) {
    ids.forEach(retireUndo);
    var entry = { spent: false };
    entry.node = window.UIFeedback.toast(message, {
      variant: 'success',
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
      if (undo) offerUndo([id], keptMessage(w.title, duration), function () { undoKeep(id, undo); });
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

  /* The shortest keep that lasts at least as long as *keep*. */
  function atLeast(keep) {
    if (keep.action === 'protected_forever') return 'forever';
    return Object.keys(KEEP_DAYS).filter(function (dur) { return KEEP_DAYS[dur] >= keep.days; })[0] || 'forever';
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
     chosen; endsShow: the show's own keep goes, and held are the seasons kept
     at least as long as it lasted; drops: the seasons no longer kept; fresh:
     nothing but seasons that had no keep. */
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
    var endsShow = !!showKept && !all;
    var c = { all: all && (!showKept || showSet), keeps: [], endsShow: endsShow, held: [], drops: [], fresh: !showKept && !all, ticked: 0 };
    _keepDialogState.seasons.forEach(function (s) {
      if (!ticked[s.id]) {
        if (s.kept || showKept) c.drops.push(s);
        return;
      }
      c.ticked++;
      if (showKept) {
        /* A season's own keep that outlasts the show's is no part of it. */
        if (showSet && endOf(s.keep) <= endOf(showKept)) c.keeps.push(s);
        else if (endsShow && endOf(s.keep) < endOf(showKept)) c.held.push(s);
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
    if (c.all && !c.keeps.length) return 'Every season has a keep of its own. Press “Set to” on one to change it.';
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

  /* A season that leaves is kept for 7, 30 or 90 days through the Watch keep,
     whose undo brings its date back; see keep(). */
  function keepSeason(s, duration) {
    var days = KEEP_DAYS[duration];
    if (s.leaving && !s.kept && days) return MM.api.post('/api/watch/keep', { media_item_id: s.id, days: days });
    return MM.api.postForm('/api/media/' + encodeURIComponent(s.id) + '/keep', { duration: duration });
  }

  /* Undo for seasons that had no keep before; null when one had its deletion
     replaced for good. */
  function seasonsUndo(c, duration, state, chainKey) {
    if (!c.fresh || (!KEEP_DAYS[duration] && c.keeps.some(function (s) { return s.leaving; }))) return null;
    var detail = { action: 'unkeep', id: state.seasonId, showRk: state.showRk };
    function run() {
      var failed = [];
      emit('busy', detail);
      trackKeepChain(chainKey, c.keeps.reduce(function (chain, s) {
        return chain.then(function () {
          return s.leaving
            ? MM.api.post('/api/watch/unkeep', { media_item_id: s.id })
            : MM.api.post('/api/media/' + encodeURIComponent(s.id) + '/unprotect');
        }).catch(function (err) { failed.push(seasonName(s) + ': ' + MM.api.messageOf(err, 'Try again.')); });
      }, Promise.resolve()).then(function () {
        emit(failed.length < c.keeps.length ? 'done' : 'failed', detail);
        if (failed.length) window.UIFeedback.error("Couldn't undo the keep. " + failed.join(' '));
        else window.UIFeedback.toast('Keep undone.');
      }));
    }
    return { ids: c.keeps.map(function (s) { return s.id; }), run: run };
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
    var showKept = _keepDialogState.showKept;
    var dur = _keepDialogState.duration || '30 days';
    var seasonsRequestUrl = seasonsUrl();
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
    emit('busy', rowDetail);
    /* The Undo of an earlier keep of these seasons would undo the wrong thing. */
    function done(data, undo) {
      c.keeps.concat(c.held, c.drops).forEach(function (s) { retireUndo(s.id); });
      showKeepSaved(state, data, savedMessage(showTitle, c, dur), undo);
    }

    if (c.all) {
      trackKeepChain(chainKey, MM.api.post(
        '/api/show/' + encodeURIComponent(rk) + '/keep',
        { duration: dur, season_ids: c.keeps.map(function (s) { return s.id; }) }
      )
        .then(done)
        .catch(function (err) {
          var msg = MM.api.messageOf(err, 'Try again.');
          window.UIFeedback.error(say("Couldn't save keep. " + msg));
          emit('failed', rowDetail);
          if (!state.isOpen()) return;
          var refocus = document.activeElement === list || MM.dom.focusStillIn(btn);
          btn.disabled = false;
          btn.textContent = btnLabel;
          if (removeAllBtn) removeAllBtn.disabled = false;
          if (refocus) btn.focus();
        }));
      return;
    }

    /* Sequential loop — accumulates failures, continues after each one. */
    var keepChain = Promise.resolve();
    var failed = [];
    var unsaved = [];
    var running = null;
    function save(s, duration) {
      keepChain = keepChain.then(function () {
        return keepSeason(s, duration)
          .then(function (data) { if (data && data.deletion_running) running = data; })
          .catch(function () { failed.push(seasonName(s)); unsaved.push(s.id); });
      });
    }
    c.keeps.forEach(function (s) { save(s, dur); });
    c.held.forEach(function (s) { save(s, atLeast(showKept)); });
    /* Once every ticked season is kept, an unticked season loses its keep.
       A show keep covers every season, so it goes first: unticking one
       season ends it. The seasons are read again because ending the show
       keep leaves some with no keep of their own to remove. */
    keepChain = keepChain.then(function () {
      if (failed.length) return;
      var ended = Promise.resolve(true);
      if (c.endsShow) {
        ended = MM.api.post('/api/show/' + encodeURIComponent(rk) + '/remove')
          .catch(function (err) {
            /* 404: no show keep any more (removed in another tab, or the
               reply to an earlier try was lost), which is what was asked. */
            if (err && err.status === 404) return;
            throw err;
          })
          .then(function () { return true; })
          .catch(function () { failed.push('the show keep'); return false; });
      }
      return ended.then(function (ok) {
        if (!ok || !c.drops.length) return;
        return MM.api.get(seasonsRequestUrl).then(function (data) {
          var keptNow = {};
          data.seasons.forEach(function (s) { if (s.kept) keptNow[s.id] = true; });
          var chain = Promise.resolve();
          c.drops.forEach(function (s) {
            if (!keptNow[s.id]) return;
            chain = chain.then(function () {
              return MM.api.post('/api/media/' + encodeURIComponent(s.id) + '/unprotect')
                .catch(function () { failed.push(seasonName(s)); });
            });
          });
          return chain;
        }, function () { failed.push('the seasons to remove keeps from'); });
      });
    });
    var chain = keepChain.then(function () {
      if (!failed.length) {
        done(running, seasonsUndo(c, dur, state, chainKey));
        return;
      }
      window.UIFeedback.error(say('Keep failed for: ' + failed.join(', ')));
      if (running && running.message) {
        window.UIFeedback.toast(say(running.message), { duration: 8000 });
      }
      emit('failed', rowDetail);
    });
    trackKeepChain(chainKey, chain).then(function () {
      if (!failed.length || !state.isOpen()) return;
      /* Some of it may be saved: show what is, with the rest still ticked. */
      var refocus = document.activeElement === list || MM.dom.focusStillIn(btn);
      _keepDialogState.picked = unsaved;
      if (refocus) {
        list.tabIndex = -1;
        list.focus();
      }
      loadKeepSeasons(refocus ? function () { return btn; } : undefined);
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
      _keepDialogState.seasons.forEach(function (s) {
        if (!s.kept) return;
        retireUndo(s.id);
        allRequests.push({ url: '/api/media/' + encodeURIComponent(s.id) + '/unprotect', name: seasonName(s) });
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
