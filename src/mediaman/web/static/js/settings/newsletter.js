/**
 * settings/newsletter.js — Newsletter settings module.
 *
 * Responsibilities:
 *   - Subscriber list rendering, adds and confirmed removals applied to the
 *     rows in place (GET/POST/DELETE /api/subscribers)
 *   - Add subscriber form (#btn-add-subscriber, #new-subscriber-email)
 *   - Newsletter send panel (#newsletter-send-panel)
 *     - Open/close toggle (#btn-send-newsletter, #btn-cancel-newsletter)
 *     - Recipient checkbox list with Select all / Select none
 *     - Send confirmation (#btn-confirm-newsletter → POST /api/newsletter/send)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
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
        if (tone === 'err') el.style.color = 'var(--danger)';
        el.textContent = text;
        return el;
      }

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
        stat.className = 'sub-stat' + (s.active ? ' active' : ' bounced');
        stat.textContent = s.active ? 'Active' : 'Bounced';
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

      if (addSubBtn) addSubBtn.addEventListener('click', submitSubscriber);
      if (addSubInp) addSubInp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); submitSubscriber(); }
      });

      var newsletterPanel   = document.getElementById('newsletter-send-panel');
      var btnSendNL         = document.getElementById('btn-send-newsletter');
      var btnCancelNL       = document.getElementById('btn-cancel-newsletter');
      var btnConfirmNL      = document.getElementById('btn-confirm-newsletter');
      var newsletterStatus  = document.getElementById('newsletter-send-status');

      var rows = MM.settings.rows;

      function clearStatus() {
        if (!newsletterStatus) return;
        newsletterStatus.textContent = '';
        newsletterStatus.className = 'inline-form-msg';
      }

      function openNewsletter() {
        if (!newsletterPanel) return;
        rows.open(newsletterPanel);
        if (btnSendNL) btnSendNL.textContent = 'Close';
        renderRecipientCheckboxes();
      }
      function closeNewsletter() {
        if (!newsletterPanel) return;
        if (btnSendNL) btnSendNL.textContent = 'Select recipients';
        rows.close(newsletterPanel, clearStatus);
      }

      if (btnSendNL) btnSendNL.addEventListener('click', function () {
        rows.isOpen(newsletterPanel) ? closeNewsletter() : openNewsletter();
      });
      if (btnCancelNL) btnCancelNL.addEventListener('click', closeNewsletter);

      // Fetch-token guards against a race where the user closes and re-opens
      // the panel before the previous /api/subscribers response arrives.
      var _recipientFetchToken = 0;

      function renderRecipientCheckboxes() {
        var list = document.getElementById('newsletter-recipient-list');
        if (!list) return;
        list.replaceChildren();
        var token = ++_recipientFetchToken;
        MM.api.get('/api/subscribers')
          .then(function (data) {
            // Bail if the user has closed the panel or re-opened it (new fetch).
            if (token !== _recipientFetchToken) return;
            if (!newsletterPanel || !rows.isOpen(newsletterPanel)) return;
            var subs = (data.subscribers || []).filter(function (s) { return s.active; });
            if (!subs.length) {
              list.appendChild(makeMsg('No active subscribers.'));
              return;
            }
            var toggleRow = document.createElement('div');
            toggleRow.className = 'recipient-toggles';
            [['Select all', true], ['Select none', false]].forEach(function (pair) {
              var b = document.createElement('button');
              b.type = 'button';
              b.className = 'btn btn--ghost btn--sm';
              b.textContent = pair[0];
              b.addEventListener('click', function () {
                list.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = pair[1]; });
              });
              toggleRow.appendChild(b);
            });
            list.appendChild(toggleRow);
            subs.forEach(function (s) {
              var item = document.createElement('div');
              item.className = 'recipient-item';
              var cb = document.createElement('input');
              cb.type = 'checkbox';
              cb.id = 'recipient-' + s.id;
              cb.value = s.email;
              cb.checked = true;
              var lbl = document.createElement('label');
              lbl.htmlFor = 'recipient-' + s.id;
              lbl.textContent = s.email;
              item.appendChild(cb);
              item.appendChild(lbl);
              list.appendChild(item);
            });
          })
          .catch(function () {
            if (token !== _recipientFetchToken) return;
            var list2 = document.getElementById('newsletter-recipient-list');
            if (list2) { list2.replaceChildren(); list2.appendChild(makeMsg("Couldn't load recipients.", 'err')); }
          });
      }

      if (btnConfirmNL) btnConfirmNL.addEventListener('click', function () {
        var list = document.getElementById('newsletter-recipient-list');
        var recipients = [];
        list.querySelectorAll('input[type="checkbox"]:checked').forEach(function (cb) { recipients.push(cb.value); });
        if (!recipients.length) {
          newsletterStatus.textContent = 'Select at least one recipient';
          newsletterStatus.className = 'inline-form-msg err';
          return;
        }
        btnConfirmNL.disabled = true;
        btnConfirmNL.textContent = 'Sending…';
        newsletterStatus.textContent = '';
        MM.api.post('/api/newsletter/send', { recipients: recipients })
          .then(function (data) {
            btnConfirmNL.disabled = false;
            if (data.sent_to === 0) {
              btnConfirmNL.textContent = 'Send newsletter';
              newsletterStatus.textContent = data.message || 'Nothing to send';
              newsletterStatus.className = 'inline-form-msg ok';
            } else if (data.failed) {
              btnConfirmNL.textContent = 'Send newsletter';
              newsletterStatus.textContent = 'Sent to ' + data.sent_to + ' of ' + (data.sent_to + data.failed) + ' (' + data.failed + ' failed)';
              newsletterStatus.className = 'inline-form-msg err';
            } else {
              btnConfirmNL.textContent = 'Sent ✓';
              newsletterStatus.textContent = 'Sent to ' + data.sent_to + ' recipient' + (data.sent_to !== 1 ? 's' : '');
              newsletterStatus.className = 'inline-form-msg ok';
              setTimeout(function () { btnConfirmNL.textContent = 'Send newsletter'; }, 2400);
            }
          })
          .catch(function (err) {
            btnConfirmNL.disabled = false;
            btnConfirmNL.textContent = 'Send newsletter';
            newsletterStatus.textContent = MM.api.messageOf(err, "Couldn't send. Try again.");
            newsletterStatus.className = 'inline-form-msg err';
          });
      });

      loadSubscribers();
    },
  };

})();
