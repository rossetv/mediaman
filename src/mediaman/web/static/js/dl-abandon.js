// src/mediaman/web/static/js/dl-abandon.js
//
// Wires up the abandon-search icon buttons to the shared confirm modal.
// Server is the source of truth for abandon_visible;
// this file is purely click → fetch → refresh.
//
// All dynamic DOM is built with createElement + textContent. We never
// assign innerHTML — even though the only dynamic values are ints from
// our own server, treating them as untrusted in the DOM layer makes
// future additions safe by default.
//
// Requires: core/api.js (MM.api), core/dom.js (MM.dom), downloads/build_dom.js.

(function () {
  'use strict';

  var modal = document.getElementById('abandon-modal');
  if (!modal) return;

  var titleEl     = document.getElementById('abandon-modal-title');
  var copyEl      = document.getElementById('abandon-modal-copy');
  var listEl      = document.getElementById('abandon-season-list');
  var confirmBtn  = MM.dom.q('[data-abandon-confirm]', modal);
  var confirmLabel = MM.dom.q('[data-confirm-label]', confirmBtn);

  var inflight = {};   // dlId -> true while its abandon request runs
  var current = null;  // { dlId, kind, title, stuckSeasons, upcoming }
  var letGo = null;    // gives the page back once the dialog has closed

  /* setupDetail owns the display/aria-hidden lifecycle and registers with
     ModalA11y, which supplies the focus trap, ESC and focus return to the
     opener. */
  var _abandonModal = MM.modal.setupDetail(modal, {
    manageBodyOverflow: false, /* _ui_feedback.css holds the page */
    closeSelectors: ['[data-abandon-cancel]'],
    onClose: function () {
      current = null;
      if (letGo) { letGo(); letGo = null; }
      /* The focus goes back to the button that opened this; a poll may have taken it away. */
      setTimeout(function () {
        var active = document.activeElement;
        if (!current && (!active || active === document.body || modal.contains(active))) {
          MM.downloads.buildDom.focusHeading();
        }
      }, 0);
    },
  });

  function open(trigger) {
    var dlId = trigger.dataset.dlId;
    var kind = trigger.dataset.kind || 'movie';
    var title = trigger.dataset.title || '';
    var upcoming = trigger.dataset.abandonUpcoming === '1';
    var stuck = [];
    try {
      stuck = JSON.parse(trigger.dataset.stuckSeasons || '[]');
    } catch (e) { stuck = []; }

    current = { dlId: dlId, kind: kind, title: title, stuckSeasons: stuck, upcoming: upcoming };

    titleEl.textContent = upcoming
      ? 'Stop tracking ' + title + '?'
      : 'Abandon search for ' + title + '?';

    listEl.replaceChildren();

    if (!isPicker()) {
      copyEl.textContent = upcoming
        ? 'It hasn’t been released yet. Mediaman will unmonitor it in ' +
          'Radarr/Sonarr so it won’t be picked up when it lands. It stays ' +
          'in your library — re-monitor any time to start tracking again.'
        : 'Mediaman will stop poking Radarr/Sonarr and the item will be ' +
          'unmonitored. It stays in your library so you can re-monitor any ' +
          'time — nothing is deleted from disk.';
      listEl.hidden = true;
    } else {
      copyEl.textContent =
        'Pick which seasons to stop chasing. Already-downloaded seasons ' +
        'stay put — only the seasons you tick will be unmonitored in Sonarr.';
      listEl.hidden = false;
      stuck.forEach(function (s) { listEl.appendChild(buildSeasonRow(s)); });
    }
    showConfirmState();

    if (!letGo) letGo = MM.dom.holdPage();
    _abandonModal.open();
  }

  function buildSeasonRow(s) {
    var missing = s.missing_episodes || 0;
    /* Only what is being searched for in vain is ticked to begin with. */
    var on = missing > 0 && s.number !== 0;
    var row = document.createElement('div');
    row.className = 'setg-row';
    row.dataset.seasonRow = '';
    row.dataset.season = String(s.number);

    var left = document.createElement('div');
    var lbl = document.createElement('div');
    lbl.className = 'setg-row-lbl';
    lbl.id = 'abandon-season-lbl-' + s.number;
    lbl.textContent = s.number === 0 ? 'Specials' : 'Season ' + s.number;
    var sub = document.createElement('div');
    sub.className = 'setg-row-sub';
    sub.textContent = missing === 0 && s.aired === false
      ? 'Not aired yet'
      : (missing || 'No') + ' missing episode' + (missing === 1 ? '' : 's');
    left.appendChild(lbl);
    left.appendChild(sub);

    var right = document.createElement('div');
    var tog = document.createElement('span');
    tog.className = on ? 'tog on' : 'tog';
    tog.setAttribute('role', 'switch');
    tog.setAttribute('tabindex', '0');
    tog.setAttribute('aria-checked', on ? 'true' : 'false');
    tog.setAttribute('aria-labelledby', lbl.id);
    function flip() {
      if (isBusy()) return;
      tog.classList.toggle('on');
      tog.setAttribute(
        'aria-checked',
        tog.classList.contains('on') ? 'true' : 'false'
      );
      showConfirmState();
    }
    tog.addEventListener('click', flip);
    tog.addEventListener('keydown', function (e) {
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); }
    });
    right.appendChild(tog);

    row.appendChild(left);
    row.appendChild(right);
    return row;
  }

  function close() { _abandonModal.close(); }

  function selectedSeasons() {
    return Array.from(listEl.querySelectorAll('[data-season-row]'))
      .filter(function (row) {
        return row.querySelector('.tog').classList.contains('on');
      })
      .map(function (row) { return parseInt(row.dataset.season, 10); });
  }

  function isBusy() { return !!(current && inflight[current.dlId]); }

  /* One season still to air is not what is being searched for in vain: it is asked about,
     never sent as if it had been picked. */
  function needsPicker(stuck) {
    return stuck.length > 1 ||
      (stuck.length === 1 && !stuck[0].missing_episodes && stuck[0].aired === false);
  }

  function isPicker() {
    return !current.upcoming && current.kind === 'series' && needsPicker(current.stuckSeasons);
  }

  /* The button and the season switches follow the current selection and
     whether this item has a request running. */
  function showConfirmState() {
    if (!current) return;
    var busy = isBusy();
    Array.from(listEl.querySelectorAll('.tog')).forEach(function (tog) {
      tog.setAttribute('aria-disabled', busy ? 'true' : 'false');
      tog.setAttribute('tabindex', busy ? '-1' : '0');
    });
    if (busy) {
      confirmLabel.textContent = current.upcoming ? 'Stopping…' : 'Abandoning…';
      confirmBtn.setAttribute('aria-disabled', 'true');
    } else if (isPicker()) {
      var n = selectedSeasons().length;
      confirmLabel.textContent = n === 0 ? 'Abandon' :
        'Abandon ' + n + ' season' + (n === 1 ? '' : 's');
      confirmBtn.setAttribute('aria-disabled', n === 0 ? 'true' : 'false');
    } else {
      confirmLabel.textContent = current.upcoming ? 'Stop tracking' : 'Abandon';
      confirmBtn.setAttribute('aria-disabled', 'false');
    }
  }

  /* Safari gives a button that is clicked no focus: without it here and where the dialog
     is opened, the focus would stay or come back on the page behind. */
  function doConfirm() {
    confirmBtn.focus();
    if (!current || isBusy()) return;
    if (confirmBtn.getAttribute('aria-disabled') === 'true') return;

    var seasons = [];
    if (current.kind === 'series' && !current.upcoming) {
      seasons = isPicker() ? selectedSeasons()
        : current.stuckSeasons.map(function (s) { return s.number; });
    }

    var dlId = current.dlId;
    var title = current.title;
    var message = current.upcoming ? 'Stopped tracking ' + title
      : 'Stopped searching for ' + (isPicker() ? seasons.length + ' season' + (seasons.length === 1 ? '' : 's') + ' of ' : '') + title;
    inflight[dlId] = true;
    showConfirmState();

    /* The dialog may be closed or reopened for another item while the request
       runs, so only touch it when it still shows this item. */
    var showsThis = function () { return current && current.dlId === dlId; };
    var endpoint = '/api/downloads/' + encodeURIComponent(dlId) + '/abandon';
    function finish(text, variant) {
      if (showsThis()) close();
      document.dispatchEvent(new CustomEvent('mediaman:downloads:refresh'));
      window.UIFeedback.toast(text, { variant: variant });
    }
    MM.api.post(endpoint, { seasons: seasons })
      .then(function () {
        delete inflight[dlId];
        finish(message, 'success');
      })
      .catch(function (err) {
        delete inflight[dlId];
        if (err.status === 404) { finish('Already gone: ' + title, 'info'); return; }
        if (showsThis()) showConfirmState();
        window.UIFeedback.error(MM.api.messageOf(err, 'Couldn’t abandon. Try again.'));
      });
  }

  /* Backdrop click and the cancel button are owned by MM.modal.setupDetail. */
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-abandon-trigger]');
    if (!trigger) return;
    trigger.focus();
    open(trigger);
  });
  confirmBtn.addEventListener('click', doConfirm);

  /* Trigger an immediate poll when the abandon succeeds, so the page
     reflects the change without waiting for the next 2-second tick. */
  document.addEventListener('mediaman:downloads:refresh', function () {
    document.dispatchEvent(new CustomEvent('mediaman:poll:now'));
  });
}());
