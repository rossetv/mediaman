/**
 * settings/newsletter.js — Newsletter settings module.
 *
 * Responsibilities:
 *   - Subscriber list rendering, adds and confirmed removals applied to the
 *     rows in place (GET/POST/DELETE /api/subscribers)
 *   - Add subscriber form (#btn-add-subscriber, #new-subscriber-email)
 *   - Newsletter send panel (#newsletter-send-panel)
 *     - Open/close toggle (#btn-send-newsletter, #btn-cancel-newsletter)
 *     - Recipient checkbox list with Select all / Select none, kept in step with
 *       the subscribers while the panel is open
 *     - Send (#btn-confirm-newsletter, a dialog naming how many people get it,
 *       then POST /api/newsletter/send)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *   window.UIFeedback (confirm dialog, toasts)
 *   MM.settings.rows (settings/rows.js)
 *
 * Exposes:
 *   MM.settings.newsletter.init()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  MM.settings.newsletter = {

    init: function () {

      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger-text)';
        el.textContent = text;
        return el;
      }

      var rows = MM.settings.rows;
      var subList = document.getElementById('subscriber-list');

      function showEmpty() {
        if (subList && !subList.querySelector('.sub-row')) {
          subList.replaceChildren(makeMsg('No subscribers yet.'));
        }
      }

      function loadSubscribers() {
        MM.api.get('/api/subscribers')
          .then(function (data) { renderSubscribers(data.subscribers || []); })
          .catch(function () {
            if (subList) { subList.replaceChildren(); subList.appendChild(makeMsg("Couldn't load subscribers.", 'err')); }
          });
      }

      function renderSubscribers(subs) {
        if (!subList) return;
        subList.replaceChildren();
        subs.forEach(function (s) { subList.appendChild(buildRow(s)); });
        showEmpty();
      }

      // A new subscriber joins the list as it stands, so nothing else moves.
      function appendNewSubscribers() {
        return MM.api.get('/api/subscribers').then(function (data) {
          if (!subList) return;
          // Each new row goes right after the row the server lists before it.
          var prev = null;
          (data.subscribers || []).forEach(function (s) {
            var existing = subList.querySelector('.sub-row[data-id="' + s.id + '"]');
            if (existing) { prev = existing; return; }
            Array.prototype.forEach.call(subList.querySelectorAll('.fld-sub'), function (m) { m.remove(); });
            var row = buildRow(s);
            subList.insertBefore(row, prev ? prev.nextSibling : subList.firstChild);
            prev = row;
            MM.settings.rows.enter(row);
          });
        });
      }

      function buildRow(s) {
        var row = document.createElement('div');
        row.className = 'sub-row';
        row.dataset.id = String(s.id);

        var av = document.createElement('div');
        av.className = 'av';
        av.textContent = (s.email || '?').charAt(0).toUpperCase();
        row.appendChild(av);

        var em = document.createElement('div');
        em.className = 'em';
        em.textContent = s.email;
        row.appendChild(em);

        var stat = document.createElement('span');
        stat.className = 'sub-stat' + (s.active ? ' active' : '');
        stat.textContent = s.active ? 'Active' : 'Unsubscribed';
        row.appendChild(stat);

        var rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'link-danger';
        rm.textContent = 'Remove';
        rm.setAttribute('aria-label', 'Remove: ' + s.email);
        rm.addEventListener('click', function () { removeSubscriber(s, row, rm); });
        row.appendChild(rm);
        return row;
      }

      function removeSubscriber(s, row, btn) {
        rows.holdFocus(btn);
        window.UIFeedback.confirm({
          title: 'Remove ' + s.email + '?',
          body: 'They stop getting the newsletter. You can add them again later.',
          confirmLabel: 'Remove',
          confirmVariant: 'danger',
        }).then(function (ok) {
          if (!ok) return;
          btn.disabled = true;
          btn.textContent = 'Removing…';
          btn.setAttribute('aria-label', 'Removing…: ' + s.email);
          MM.api.delete('/api/subscribers/' + s.id)
            .then(function () {
              MM.settings.rows.leave(row, {
                focusSelector: '.link-danger',
                fallback: addSubInp,
                done: showEmpty,
              });
              window.UIFeedback.success('Removed ' + s.email);
              refreshRecipients();
            })
            .catch(function (err) {
              var keepFocus = MM.dom.focusStillIn(btn);
              btn.disabled = false;
              btn.textContent = 'Remove';
              btn.setAttribute('aria-label', 'Remove: ' + s.email);
              if (keepFocus) btn.focus();
              window.UIFeedback.error(MM.api.messageOf(err, "Couldn't remove subscriber."));
            });
        });
      }

      var addSubBtn = document.getElementById('btn-add-subscriber');
      var addSubInp = document.getElementById('new-subscriber-email');

      var addSubLabel = addSubBtn ? addSubBtn.textContent : '';
      var addingSubscriber = false;
      function submitSubscriber() {
        if (!addSubInp || addingSubscriber) return;
        var email = addSubInp.value.trim();
        if (!email) return;
        addingSubscriber = true;
        if (addSubBtn) { addSubBtn.disabled = true; addSubBtn.textContent = 'Adding…'; }
        // The endpoint takes form-urlencoded (FastAPI ``Form(...)``) so
        // we use postForm rather than post (which sends JSON).
        MM.api.postForm('/api/subscribers', { email: email })
          .then(function () {
            addSubInp.value = '';
            refreshRecipients();
            return appendNewSubscribers().catch(loadSubscribers);
          })
          .catch(function (err) {
            window.UIFeedback.error(MM.api.messageOf(err, "Couldn't add subscriber."));
          })
          .then(function () {
            addingSubscriber = false;
            if (!addSubBtn) return;
            // Disabling the focused button drops focus to the body; give it back unless the person moved on.
            var keepFocus = MM.dom.focusStillIn(addSubBtn);
            addSubBtn.disabled = false; addSubBtn.textContent = addSubLabel;
            if (keepFocus) addSubBtn.focus();
          });
      }

      if (addSubBtn) addSubBtn.addEventListener('click', function () {
        rows.holdFocus(addSubBtn);
        submitSubscriber();
      });
      if (addSubInp) addSubInp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); submitSubscriber(); }
      });

      var newsletterPanel   = document.getElementById('newsletter-send-panel');
      var btnSendNL         = document.getElementById('btn-send-newsletter');
      var btnCancelNL       = document.getElementById('btn-cancel-newsletter');
      var btnConfirmNL      = document.getElementById('btn-confirm-newsletter');
      var newsletterStatus  = document.getElementById('newsletter-send-status');

      function clearStatus() {
        if (!newsletterStatus) return;
        newsletterStatus.textContent = '';
        newsletterStatus.className = 'inline-form-msg';
      }

      function paintToggle(open) {
        if (!btnSendNL) return;
        btnSendNL.textContent = open ? 'Close' : 'Select recipients';
        btnSendNL.setAttribute('aria-expanded', open ? 'true' : 'false');
      }
      if (btnSendNL && newsletterPanel) {
        btnSendNL.setAttribute('aria-controls', newsletterPanel.id);
        paintToggle(false);
      }

      function openNewsletter() {
        if (!newsletterPanel) return;
        rows.openInView(newsletterPanel);
        paintToggle(true);
        openRecipients();
      }
      function closeNewsletter() {
        if (!newsletterPanel) return;
        paintToggle(false);
        rows.close(newsletterPanel, clearStatus);
      }

      // A panel that is sending cannot be closed from its own buttons.
      function sending() { return !!(btnConfirmNL && btnConfirmNL.disabled); }
      if (btnSendNL) btnSendNL.addEventListener('click', function () {
        if (sending()) return;
        rows.holdFocus(btnSendNL);
        rows.isOpen(newsletterPanel) ? closeNewsletter() : openNewsletter();
      });
      if (btnCancelNL) btnCancelNL.addEventListener('click', function () {
        if (sending()) return;
        closeNewsletter();
        if (btnSendNL) btnSendNL.focus();
      });
      // A changed choice of recipients makes the last message stale, unless a send is under way.
      function clearUnlessSending() { if (!sending()) clearStatus(); }
      if (newsletterPanel) newsletterPanel.addEventListener('change', clearUnlessSending);

      // Fetch-token guards against a race where the user closes and re-opens
      // the panel before the previous /api/subscribers response arrives.
      var _recipientFetchToken = 0;

      function loadRecipients(first) {
        var list = document.getElementById('newsletter-recipient-list');
        if (!list) return;
        var token = ++_recipientFetchToken;
        MM.api.get('/api/subscribers')
          .then(function (data) {
            // Bail if the user has closed the panel or re-opened it (new fetch).
            if (token !== _recipientFetchToken) return;
            if (!newsletterPanel || !rows.isOpen(newsletterPanel)) return;
            var there = first && rows.inView(newsletterPanel) &&
              (document.activeElement === btnSendNL || MM.dom.focusStillIn(newsletterPanel));
            paintRecipients(list, (data.subscribers || []).filter(function (s) { return s.active; }));
            if (there) rows.bringIntoView(newsletterPanel);
          })
          .catch(function () {
            if (token !== _recipientFetchToken) return;
            list.replaceChildren(makeMsg("Couldn't load recipients.", 'err'));
          });
      }

      function openRecipients() {
        var list = document.getElementById('newsletter-recipient-list');
        if (list) list.replaceChildren();
        loadRecipients(true);
      }

      // The recipients follow the subscribers while the panel is open.
      function refreshRecipients() {
        if (newsletterPanel && rows.isOpen(newsletterPanel)) loadRecipients(false);
      }

      function buildToggles(list) {
        var toggleRow = document.createElement('div');
        toggleRow.className = 'recipient-toggles';
        [['Select all', true], ['Select none', false]].forEach(function (pair) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'btn btn--ghost btn--sm';
          b.textContent = pair[0];
          b.addEventListener('click', function () {
            list.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = pair[1]; });
            clearUnlessSending();
          });
          toggleRow.appendChild(b);
        });
        return toggleRow;
      }

      function buildRecipient(s, checked) {
        var item = document.createElement('div');
        item.className = 'recipient-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.id = 'recipient-' + s.id;
        cb.value = s.email;
        cb.checked = checked;
        var lbl = document.createElement('label');
        lbl.htmlFor = 'recipient-' + s.id;
        lbl.textContent = s.email;
        item.appendChild(cb);
        item.appendChild(lbl);
        return item;
      }

      // Those already listed keep their place, their tick and the focus.
      function paintRecipients(list, subs) {
        if (!subs.length) {
          list.replaceChildren(makeMsg('No active subscribers.'));
          return;
        }
        if (!list.querySelector('.recipient-toggles')) list.replaceChildren(buildToggles(list));
        var listed = {};
        var leftOut = false;
        Array.prototype.forEach.call(list.querySelectorAll('.recipient-item'), function (item) {
          var cb = item.querySelector('input');
          listed[cb.id] = item;
          if (!cb.checked) leftOut = true;
        });
        // Once someone is left out, nobody new is put in unasked.
        var before = list.querySelector('.recipient-toggles');
        subs.forEach(function (s) {
          var id = 'recipient-' + s.id;
          var item = listed[id];
          delete listed[id];
          if (!item) {
            item = buildRecipient(s, !leftOut);
            list.insertBefore(item, before.nextSibling);
          }
          before = item;
        });
        Object.keys(listed).forEach(function (id) { listed[id].remove(); });
        clearUnlessSending();
      }

      // Says how a send went, which the new words may make taller.
      function answer(text, tone) {
        var there = rows.stillAt(newsletterStatus, newsletterPanel);
        newsletterStatus.textContent = text;
        newsletterStatus.className = 'inline-form-msg ' + tone;
        if (there) rows.reveal(newsletterStatus);
      }

      function enableConfirm() {
        var keepFocus = MM.dom.focusStillIn(newsletterPanel);
        btnConfirmNL.disabled = false;
        if (keepFocus) btnConfirmNL.focus({ preventScroll: true });
      }

      var asking = false;
      if (btnConfirmNL) btnConfirmNL.addEventListener('click', function () {
        var list = document.getElementById('newsletter-recipient-list');
        var recipients = [];
        list.querySelectorAll('input[type="checkbox"]:checked').forEach(function (cb) { recipients.push(cb.value); });
        rows.holdFocus(btnConfirmNL, newsletterPanel);
        if (!recipients.length) {
          newsletterStatus.textContent = 'Select at least one recipient';
          newsletterStatus.className = 'inline-form-msg err';
          rows.reveal(newsletterStatus);
          return;
        }
        if (asking) return;
        asking = true;
        window.UIFeedback.confirm({
          title: 'Send the newsletter to ' + recipients.length + (recipients.length === 1 ? ' person?' : ' people?'),
          body: 'It goes out now. The scheduled newsletter is still sent as usual.',
          confirmLabel: 'Send newsletter',
        }).then(function (ok) {
          asking = false;
          if (ok) sendNewsletter(recipients);
        });
      });

      function sendNewsletter(recipients) {
        btnConfirmNL.disabled = true;
        btnConfirmNL.textContent = 'Sending…';
        newsletterStatus.textContent = '';
        rows.reveal(newsletterStatus);
        MM.api.post('/api/newsletter/send', { recipients: recipients })
          .then(function (data) {
            enableConfirm();
            if (data.sent_to === 0) {
              btnConfirmNL.textContent = 'Send newsletter';
              answer(data.message || 'Nothing to send', 'ok');
            } else if (data.failed) {
              btnConfirmNL.textContent = 'Send newsletter';
              answer('Sent to ' + data.sent_to + ' of ' + (data.sent_to + data.failed) + ' (' + data.failed + ' failed)', 'err');
            } else {
              btnConfirmNL.textContent = 'Sent ✓';
              answer('Sent to ' + data.sent_to + ' recipient' + (data.sent_to !== 1 ? 's' : ''), 'ok');
              setTimeout(function () { btnConfirmNL.textContent = 'Send newsletter'; }, 2400);
            }
          })
          .catch(function (err) {
            enableConfirm();
            btnConfirmNL.textContent = 'Send newsletter';
            answer(MM.api.messageOf(err, "Couldn't send. Try again."), 'err');
          });
      }

      loadSubscribers();
    },
  };

})();
