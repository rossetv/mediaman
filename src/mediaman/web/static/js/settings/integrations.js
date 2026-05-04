/**
 * settings/integrations.js — Integrations settings module.
 *
 * Responsibilities:
 *   - Connection-status pills (shared updater for intg-card + ov-status)
 *   - Manual test buttons (<button data-test-service="…">)
 *   - Auto-test configured services on page load
 *   - Plex library sync button (#btn-sync-library)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.integrations.init()
 *   MM.settings.integrations.setConnStatus(service, tone, label)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  MM.settings.integrations = {

    // ------------------------------------------------------------------
    // setConnStatus — shared updater used by test + auto-test paths.
    // ------------------------------------------------------------------
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
          dot.style.background =
            tone === 'ok'   ? 'var(--success)' :
            tone === 'warn' ? 'var(--warning)' :
            tone === 'err'  ? 'var(--danger)'  : 'var(--t4)';
          dot.style.boxShadow =
            tone === 'ok'   ? '0 0 8px rgba(48,209,88,.6)' :
            tone === 'warn' ? '0 0 8px rgba(255,214,10,.6)' :
            tone === 'err'  ? '0 0 8px rgba(255,69,58,.6)'  : 'none';
        }
        var stEl = el.querySelector('[data-ov-status]');
        if (stEl) stEl.textContent = label;
      });
    },

    init: function () {
      var self = MM.settings.integrations;

      // ----------------------------------------------------------------
      // Manual test buttons
      // ----------------------------------------------------------------
      function testService(service, btn) {
        self.setConnStatus(service, 'untested', 'Testing…');
        if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
        MM.api.post('/api/settings/test/' + encodeURIComponent(service))
          .then(function (data) {
            if (data.ok) {
              self.setConnStatus(service, 'ok', 'Connected');
              if (btn) btn.textContent = 'OK ✓';
            } else {
              self.setConnStatus(service, 'err', data.error || 'Error');
              if (btn) btn.textContent = 'Failed';
            }
          })
          .catch(function (err) {
            // MM.api rejects on ok:false too; distinguish connection errors
            // from a well-formed failure response.
            var label = (err && err.error) || 'Connection failed';
            self.setConnStatus(service, 'err', label);
            if (btn) btn.textContent = 'Failed';
          })
          .then(function () {
            // .then used as finally (broad browser compat).
            if (!btn) return;
            setTimeout(function () { btn.textContent = 'Test'; btn.disabled = false; }, 1600);
          });
      }

      document.querySelectorAll('[data-test-service]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          testService(btn.getAttribute('data-test-service'), btn);
        });
      });

      // ----------------------------------------------------------------
      // Auto-test configured services on page load.
      // ----------------------------------------------------------------
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
        AUTO_CHECKS.forEach(function (c) {
          var configured = c.fields.every(fieldHasValue);
          if (!configured) { self.setConnStatus(c.service, 'off', 'Not configured'); return; }
          self.setConnStatus(c.service, 'untested', 'Testing…');
          MM.api.post('/api/settings/test/' + encodeURIComponent(c.service))
            .then(function (data) {
              self.setConnStatus(c.service, data.ok ? 'ok' : 'err', data.ok ? 'Connected' : (data.error || 'Error'));
            })
            .catch(function () { self.setConnStatus(c.service, 'err', 'Connection failed'); });
        });
      }
      autoTest();

      // ----------------------------------------------------------------
      // Library sync button
      // ----------------------------------------------------------------
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
          .then(function (data) {
            syncBtn.textContent = data.ok ? 'Synced ✓' : 'Failed';
          })
          .catch(function () { syncBtn.textContent = 'Failed'; })
          .then(function () {
            setTimeout(function () {
              syncBtn.textContent = orig; syncBtn.disabled = false;
            }, 1800);
          });
      });
    },
  };

})();
