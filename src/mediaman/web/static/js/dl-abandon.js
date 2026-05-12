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
// Requires: core/api.js (MM.api), core/dom.js (MM.dom).

(function () {
  'use strict';

  var modal = document.getElementById('abandon-modal');
  if (!modal) return;

  var titleEl     = document.getElementById('abandon-modal-title');
  var copyEl      = document.getElementById('abandon-modal-copy');
  var listEl      = document.getElementById('abandon-season-list');
  var confirmBtn  = MM.dom.q('[data-abandon-confirm]', modal);
  var confirmLabel = MM.dom.q('[data-confirm-label]', confirmBtn);

  var current = null;  // { dlId, kind, title, stuckSeasons, upcoming }

  /* This modal intentionally does NOT lock body scroll. We use
     MM.modal.setupDetail for the display:flex/none + aria-hidden
     lifecycle (so backdrop click, the cancel button, AND the ESC
     keypress are handled centrally). useModalA11y=false skips the
     ModalA11y focus-trap; setupDetail still attaches its own ESC
     fallback listener in that branch, so we don't need to add one
     here. */
  var _abandonModal = MM.modal.setupDetail(modal, {
    useModalA11y: false,
    manageBodyOverflow: false,
    closeSelectors: ['[data-abandon-cancel]'],
    onClose: function () {
      current = null;
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

    if (upcoming || kind === 'movie' || stuck.length <= 1) {
      copyEl.textContent = upcoming
        ? 'It hasn’t been released yet. Mediaman will unmonitor it in ' +
          'Radarr/Sonarr so it won’t be picked up when it lands. It stays ' +
          'in your library — re-monitor any time to start tracking again.'
        : 'Mediaman will stop poking Radarr/Sonarr and the item will be ' +
          'unmonitored. It stays in your library so you can re-monitor any ' +
          'time — nothing is deleted from disk.';
      listEl.style.display = 'none';
      confirmLabel.textContent = upcoming ? 'Stop tracking' : 'Abandon';
      confirmBtn.setAttribute('aria-disabled', 'false');
    } else {
      copyEl.textContent =
        'Pick which seasons to stop chasing. Already-downloaded seasons ' +
        'stay put — only the seasons you tick will be unmonitored in Sonarr.';
      listEl.style.display = '';
      stuck.forEach(function (s) { listEl.appendChild(buildSeasonRow(s)); });
      updateConfirmLabel();
    }

    _abandonModal.open();
  }

  function buildSeasonRow(s) {
    var row = document.createElement('div');
    row.className = 'setg-row';
    row.dataset.seasonRow = '';
    row.dataset.season = String(s.number);

    var left = document.createElement('div');
    var lbl = document.createElement('div');
    lbl.className = 'setg-row-lbl';
    lbl.textContent = 'Season ' + s.number;
    var sub = document.createElement('div');
    sub.className = 'setg-row-sub';
    sub.textContent = s.missing_episodes + ' missing episode'
      + (s.missing_episodes === 1 ? '' : 's');
    left.appendChild(lbl);
    left.appendChild(sub);

    var right = document.createElement('div');
    var tog = document.createElement('span');
    tog.className = 'tog on';
    tog.setAttribute('role', 'switch');
    tog.setAttribute('tabindex', '0');
    tog.setAttribute('aria-checked', 'true');
    tog.addEventListener('click', function () {
      tog.classList.toggle('on');
      tog.setAttribute(
        'aria-checked',
        tog.classList.contains('on') ? 'true' : 'false'
      );
      updateConfirmLabel();
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

  function updateConfirmLabel() {
    if (!current) return;
    if (current.kind !== 'series' || current.stuckSeasons.length <= 1) return;
    var n = selectedSeasons().length;
    confirmLabel.textContent = n === 0 ? 'Abandon' :
      'Abandon ' + n + ' season' + (n === 1 ? '' : 's');
    confirmBtn.setAttribute('aria-disabled', n === 0 ? 'true' : 'false');
  }

  function doConfirm() {
    if (!current) return;
    if (confirmBtn.getAttribute('aria-disabled') === 'true') return;

    var seasons = [];
    if (current.kind === 'series' && !current.upcoming) {
      if (current.stuckSeasons.length === 1) {
        seasons = [current.stuckSeasons[0].number];
      } else {
        seasons = selectedSeasons();
      }
    }

    confirmBtn.setAttribute('aria-disabled', 'true');

    var endpoint = '/api/downloads/' + encodeURIComponent(current.dlId) + '/abandon';
    MM.api.post(endpoint, { seasons: seasons })
      .then(function () {
        close();
        document.dispatchEvent(new CustomEvent('mediaman:downloads:refresh'));
      })
      .catch(function (err) {
        if (window.mediamanToast) {
          window.mediamanToast(
            'Couldn’t abandon: ' + (err.message || err), { kind: 'error' }
          );
        } else {
          console.error('abandon failed', err);
        }
        confirmBtn.setAttribute('aria-disabled', 'false');
      });
  }

  /* Backdrop click and the cancel button are owned by MM.modal.setupDetail. */
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-abandon-trigger]');
    if (trigger) open(trigger);
  });
  confirmBtn.addEventListener('click', doConfirm);

  /* Trigger an immediate poll when the abandon succeeds, so the page
     reflects the change without waiting for the next 2-second tick. */
  document.addEventListener('mediaman:downloads:refresh', function () {
    document.dispatchEvent(new CustomEvent('mediaman:poll:now'));
  });
}());
