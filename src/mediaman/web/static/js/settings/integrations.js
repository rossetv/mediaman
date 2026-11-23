/**
 * settings/integrations.js — Integrations settings module.
 *
 * Responsibilities:
 *   - Connection-status pills (shared updater for intg-card + ov-status)
 *   - Manual test buttons (<button data-test-service="…">)
 *   - Auto-test configured services on page load
 *   - Plex library sync button (#btn-sync-library), with the server's reason on failure
 *   - Rail badge counting the services whose check failed
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.integrations.init()
 *   MM.settings.integrations.setConnStatus(service, tone, label)
 *   MM.settings.integrations.paintRailCount()
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  MM.settings.integrations = {

    setConnStatus: function (service, tone, label) {
      document.querySelectorAll('[data-conn="' + service + '"]').forEach(function (el) {
        el.classList.remove('ok', 'warn', 'err', 'off', 'untested');
        el.classList.add(tone);
        var lbl = el.querySelector('[data-conn-label]');
        if (lbl) lbl.textContent = label;
      });
      document.querySelectorAll('[data-ov-service="' + service + '"]').forEach(function (el) {
        var dot = el.querySelector('.conn-dot');
        if (dot) {
          /* Toggle CSS modifier classes instead of writing inline styles.
             Classes .ok/.err/.warn are defined in _base.css; .off is already there. */
          dot.classList.remove('ok', 'err', 'warn', 'off');
          if (tone === 'ok' || tone === 'err' || tone === 'warn' || tone === 'off') {
            dot.classList.add(tone);
          }
        }
        var stEl = el.querySelector('[data-ov-status]');
        if (stEl) stEl.textContent = label;
      });
      MM.settings.integrations.paintRailCount();
    },

    paintRailCount: function () {
      var item = document.querySelector('.setg-rail-item[href="#setg-integrations"]');
      if (!item) return;
      var failed = document.querySelectorAll('[data-conn].err').length;
      var badge = item.querySelector('.rail-count');
      if (!failed) {
        if (badge) badge.remove();
        item.removeAttribute('aria-label');
        return;
      }
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'rail-count err';
        item.appendChild(badge);
      }
      badge.textContent = String(failed);
      item.setAttribute('aria-label', 'Integrations, ' + failed + ' not connected');
    },

    /* The pill label changes on the page-load auto-check too, so only this
       separate region is announced, and only for a pressed Test. */
    announce: function (service, text) {
      document.querySelectorAll('#intg-' + service + ' > [data-conn-announce]').forEach(function (el) {
        el.textContent = text;
      });
    },

    init: function () {
      var self = MM.settings.integrations;

      function serviceName(btn) {
        var h = btn.closest('.intg-card') && btn.closest('.intg-card').querySelector('h3');
        return h ? h.textContent.trim() : btn.getAttribute('data-test-service');
      }

      function testService(service, btn) {
        self.setConnStatus(service, 'untested', 'Testing…');
        if (btn) self.announce(service, '');
        if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
        MM.api.post('/api/settings/test/' + encodeURIComponent(service))
          .then(function () {
            self.setConnStatus(service, 'ok', 'Connected');
            if (btn) { btn.textContent = 'OK ✓'; self.announce(service, serviceName(btn) + ': Connected'); }
          })
          .catch(function (err) {
            // MM.api rejects on ok:false too, so a failed test lands here.
            var label = MM.api.messageOf(err, 'Connection failed');
            self.setConnStatus(service, 'err', label);
            if (btn) { btn.textContent = 'Failed'; self.announce(service, serviceName(btn) + ': ' + label); }
          })
          .then(function () {
            // .then used as finally (broad browser compat).
            if (!btn) return;
            setTimeout(function () {
              var keepFocus = MM.dom.focusStillIn(btn);
              btn.textContent = 'Test'; btn.disabled = false;
              if (keepFocus) btn.focus();
            }, 1600);
          });
      }

      document.querySelectorAll('[data-test-service]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          testService(btn.getAttribute('data-test-service'), btn);
        });
      });

      var AUTO_CHECKS = [
        { service: 'plex',    fields: ['plex_url', 'plex_token'] },
        { service: 'sonarr',  fields: ['sonarr_url', 'sonarr_api_key'] },
        { service: 'radarr',  fields: ['radarr_url', 'radarr_api_key'] },
        { service: 'nzbget',  fields: ['nzbget_url'] },
        { service: 'mailgun', fields: ['mailgun_domain', 'mailgun_api_key'] },
        { service: 'openai',  fields: ['openai_api_key'] },
        { service: 'tmdb',    fields: ['tmdb_read_token'] },
        { service: 'omdb',    fields: ['omdb_api_key'] },
      ];

      function fieldHasValue(id) {
        var el = document.getElementById(id);
        return !!(el && el.value && el.value !== '');
      }

      function autoTest() {
        /* Stagger requests by 200 ms per service to avoid blowing the per-admin
           rate-limit burst cap when all 8 checks fire simultaneously on page load. */
        AUTO_CHECKS.forEach(function (c, idx) {
          var configured = c.fields.every(fieldHasValue);
          if (!configured) { self.setConnStatus(c.service, 'off', 'Not configured'); return; }
          self.setConnStatus(c.service, 'untested', 'Testing…');
          setTimeout(function () {
            MM.api.post('/api/settings/test/' + encodeURIComponent(c.service))
              .then(function () {
                self.setConnStatus(c.service, 'ok', 'Connected');
              })
              .catch(function (err) {
                // err is an APIError with .status and .message. 429 is the
                // common case here; staggering above reduces occurrences, but
                // show it distinctly if it still fires.
                // A throttled service is not a disconnected one, so it gets the
                // warning tone, which the rail badge does not count.
                if (err && err.status === 429) self.setConnStatus(c.service, 'warn', 'Rate limited');
                else self.setConnStatus(c.service, 'err', MM.api.messageOf(err, 'Connection failed'));
              });
          }, idx * 200);
        });
      }
      autoTest();

      var syncBtn = document.getElementById('btn-sync-library');
      if (syncBtn) syncBtn.addEventListener('click', function () {
        var orig = syncBtn.textContent;
        // Build the in-flight label as a spinner + text node so the spinner
        // animates alongside the label. Avoid innerHTML for safety.
        while (syncBtn.firstChild) syncBtn.removeChild(syncBtn.firstChild);
        var spinner = document.createElement('span');
        spinner.className = 'btn-spinner';
        spinner.setAttribute('aria-hidden', 'true');
        syncBtn.appendChild(spinner);
        syncBtn.appendChild(document.createTextNode('Syncing…'));
        syncBtn.disabled = true;
        MM.api.post('/api/library/sync')
          .then(function () { syncBtn.textContent = 'Synced ✓'; })
          .catch(function (err) {
            syncBtn.textContent = 'Failed';
            window.UIFeedback.error(MM.api.messageOf(err, 'Library sync failed'));
          })
          .then(function () {
            setTimeout(function () {
              var keepFocus = MM.dom.focusStillIn(syncBtn);
              syncBtn.textContent = orig; syncBtn.disabled = false;
              if (keepFocus) syncBtn.focus();
            }, 1800);
          });
      });
    },
  };

})();
