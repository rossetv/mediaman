/**
 * settings/newsletter.js — Newsletter settings module.
 *
 * Responsibilities:
 *   - Subscriber list rendering and removal (GET/DELETE /api/subscribers)
 *   - Add subscriber form (#btn-add-subscriber, #new-subscriber-email)
 *   - Newsletter send panel (#newsletter-send-panel)
 *     - Open/close toggle (#btn-send-newsletter, #btn-cancel-newsletter)
 *     - Recipient checkbox list with Select all / Select none
 *     - Send confirmation (#btn-confirm-newsletter → POST /api/newsletter/send)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
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

      // ----------------------------------------------------------------
      // Shared helpers
      // ----------------------------------------------------------------
      function makeMsg(text, tone) {
        var el = document.createElement('div');
        el.className = 'fld-sub';
        if (tone === 'err') el.style.color = 'var(--danger)';
        el.textContent = text;
        return el;
      }

      // ----------------------------------------------------------------
      // Subscriber list
      // ----------------------------------------------------------------
      function loadSubscribers() {
        MM.api.get('/api/subscribers')
          .then(function (data) { renderSubscribers(data.subscribers || []); })
          .catch(function () {
            var list = document.getElementById('subscriber-list');
            if (list) { list.replaceChildren(); list.appendChild(makeMsg("Couldn't load subscribers.", 'err')); }
          });
      }

      function renderSubscribers(subs) {
        var list = document.getElementById('subscriber-list');
        if (!list) return;
        list.replaceChildren();
        if (!subs.length) { list.appendChild(makeMsg('No subscribers yet.')); return; }
        subs.forEach(function (s) {
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
          rm.addEventListener('click', function () { removeSubscriber(s.id, rm); });
          row.appendChild(rm);

          list.appendChild(row);
        });
      }

      function removeSubscriber(id, btn) {
        btn.disabled = true;
        MM.api.delete('/api/subscribers/' + id)
          .then(function () { loadSubscribers(); })
          .catch(function () { btn.disabled = false; });
      }

      // ----------------------------------------------------------------
      // Add subscriber
      // ----------------------------------------------------------------
      var addSubBtn = document.getElementById('btn-add-subscriber');
      var addSubInp = document.getElementById('new-subscriber-email');

      function submitSubscriber() {
        if (!addSubInp) return;
        var email = addSubInp.value.trim();
        if (!email) return;
        var body = new URLSearchParams({ email: email });
        fetch('/api/subscribers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: body.toString(),
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (data.ok) { addSubInp.value = ''; loadSubscribers(); }
            else if (window.UIFeedback && window.UIFeedback.error) {
              window.UIFeedback.error(data.error || "Couldn't add subscriber.");
            } else { window.alert(data.error || "Couldn't add subscriber."); }
          });
        // NOTE: The add-subscriber endpoint sends form-urlencoded, not JSON,
        // so we use raw fetch rather than MM.api.post to avoid the
        // Content-Type being overridden to application/json.
      }

      if (addSubBtn) addSubBtn.addEventListener('click', submitSubscriber);
      if (addSubInp) addSubInp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); submitSubscriber(); }
      });

      // ----------------------------------------------------------------
      // Newsletter send panel
      // ----------------------------------------------------------------
      var newsletterPanel   = document.getElementById('newsletter-send-panel');
      var btnSendNL         = document.getElementById('btn-send-newsletter');
      var btnCancelNL       = document.getElementById('btn-cancel-newsletter');
      var btnConfirmNL      = document.getElementById('btn-confirm-newsletter');
      var newsletterStatus  = document.getElementById('newsletter-send-status');

      function openNewsletter() {
        if (!newsletterPanel) return;
        newsletterPanel.hidden = false;
        if (btnSendNL) btnSendNL.textContent = 'Close';
        renderRecipientCheckboxes();
      }
      function closeNewsletter() {
        if (!newsletterPanel) return;
        newsletterPanel.hidden = true;
        if (btnSendNL) btnSendNL.textContent = 'Select recipients';
        if (newsletterStatus) newsletterStatus.textContent = '';
      }

      if (btnSendNL) btnSendNL.addEventListener('click', function () {
        newsletterPanel.hidden ? openNewsletter() : closeNewsletter();
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
            if (!newsletterPanel || newsletterPanel.hidden) return;
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
            if (data.ok) {
              btnConfirmNL.textContent = 'Sent ✓';
              newsletterStatus.textContent = 'Sent to ' + data.sent_to + ' recipient' + (data.sent_to !== 1 ? 's' : '');
              newsletterStatus.className = 'inline-form-msg ok';
              setTimeout(function () { btnConfirmNL.textContent = 'Send newsletter'; }, 2400);
            } else {
              btnConfirmNL.textContent = 'Send newsletter';
              newsletterStatus.textContent = data.error || "Couldn't send";
              newsletterStatus.className = 'inline-form-msg err';
            }
          })
          .catch(function (err) {
            btnConfirmNL.disabled = false;
            btnConfirmNL.textContent = 'Send newsletter';
            newsletterStatus.textContent = (err && err.message) || "Couldn't send. Try again.";
            newsletterStatus.className = 'inline-form-msg err';
          });
      });

      // ----------------------------------------------------------------
      // Boot.
      // ----------------------------------------------------------------
      loadSubscribers();
    },
  };

})();
