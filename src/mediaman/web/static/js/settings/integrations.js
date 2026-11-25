/**
 * settings/integrations.js — Integrations settings module.
 *
 * Responsibilities:
 *   - Connection-status pills (shared updater for intg-card + ov-status)
 *   - Manual test buttons (<button data-test-service="…">), which test the saved settings
 *   - Auto-test configured services on page load, and those a save has changed
 *   - Plex library sync button (#btn-sync-library), with the server's reason on failure
 *   - Refresh of the shared picks (#btn-refresh-picks), with how long until the next
 *   - Rail badge counting the services whose check failed
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *   MM.settings.savebar.isEdited (settings/savebar.js)
 *   window.UIFeedback (toasts)
 *
 * Exposes:
 *   MM.settings.integrations.init()
 *   MM.settings.integrations.setConnStatus(service, tone, label)
 *   MM.settings.integrations.paintRailCount()
 *   MM.settings.integrations.paintTests()   — called by savebar.js
 *   MM.settings.integrations.recheck(keys, values) — called by savebar.js: the settings just stored
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  /* `fields` are what a service must have to be checked, `also` what else its check reads. */
  var AUTO_CHECKS = [
    { service: 'plex',    fields: ['plex_url', 'plex_token'] },
    { service: 'sonarr',  fields: ['sonarr_url', 'sonarr_api_key'] },
    { service: 'radarr',  fields: ['radarr_url', 'radarr_api_key'] },
    { service: 'nzbget',  fields: ['nzbget_url'], also: ['nzbget_username', 'nzbget_password'] },
    { service: 'mailgun', fields: ['mailgun_domain', 'mailgun_api_key'], also: ['mailgun_from_address'] },
    { service: 'openai',  fields: ['openai_api_key'] },
    { service: 'tmdb',    fields: ['tmdb_read_token'] },
    { service: 'omdb',    fields: ['omdb_api_key'] },
  ];

  /* The newest check of each service: an older one answers for settings since replaced. */
  var newest = {};
  function begin(service) {
    newest[service] = (newest[service] || 0) + 1;
    return newest[service];
  }
  function isNewest(service, run) { return newest[service] === run; }

  function fieldValue(id) {
    var el = document.getElementById(id);
    return el ? el.value : '';
  }

  // A throttled service is not a disconnected one, so a 429 gets the
  // warning tone, which the rail badge does not count. Returns the label.
  function setFailedStatus(service, err) {
    var label = (err && err.status === 429)
      ? 'Rate limited'
      : MM.api.messageOf(err, 'Connection failed');
    MM.settings.integrations.setConnStatus(service, (err && err.status === 429) ? 'warn' : 'err', label);
    return label;
  }

  /* Stagger requests by 200 ms per service to avoid blowing the per-admin
     rate-limit burst cap when all 8 checks fire simultaneously on page load. */
  function autoTest(checks, valueOf) {
    checks.forEach(function (c, idx) {
      var run = begin(c.service);
      if (!c.fields.every(valueOf)) {
        MM.settings.integrations.setConnStatus(c.service, 'off', 'Not configured');
        return;
      }
      MM.settings.integrations.setConnStatus(c.service, 'untested', 'Testing…');
      setTimeout(function () {
        if (!isNewest(c.service, run)) return;
        MM.api.post('/api/settings/test/' + encodeURIComponent(c.service) + '?auto=1')
          .then(function () {
            if (isNewest(c.service, run)) MM.settings.integrations.setConnStatus(c.service, 'ok', 'Connected');
          })
          .catch(function (err) {
            // 429 is the common case here; staggering above reduces
            // occurrences, but it is still shown distinctly if it fires.
            if (isNewest(c.service, run)) setFailedStatus(c.service, err);
          });
      }, idx * 200);
    });
  }

  /* ── Shared picks ── */

  var PICKS_URL = '/api/recommended/refresh';
  var POLL_MS = 3000;
  var POLL_MAX_MS = 30000;
  var MAX_FAILURES = 8;
  var IDLE_LIMIT = 2; // "idle" twice running: the worker died without a result
  var picks = { known: false, running: false, nextAt: null, hasKey: false, on: false };
  var picksTimer = null;

  /* "14 h" or "25 min": how long until *iso*. */
  function waitText(iso) {
    var minutes = Math.ceil((new Date(iso).getTime() - Date.now()) / 60000);
    if (!(minutes > 0)) return '';
    return (minutes >= 60 ? Math.ceil(minutes / 60) + '\u00a0h' : minutes + '\u00a0min');
  }

  function picksButton() { return document.getElementById('btn-refresh-picks'); }

  /* Why the shared picks cannot be refreshed now, in words; empty when they can. */
  function picksWhy() {
    if (!picks.on) return 'AI recommendations are switched off.';
    if (!picks.hasKey) return 'Save an OpenAI key first.';
    var left = picks.nextAt && waitText(picks.nextAt);
    return left ? 'The shared picks can be refreshed again in ' + left + '.' : '';
  }

  function drawPicks() {
    var btn = picksButton();
    if (!btn) return;
    var name = 'Refresh shared picks for everyone';
    var left = picks.on && picks.hasKey && picks.nextAt ? waitText(picks.nextAt) : '';
    var why = picks.running ? '' : picksWhy();
    btn.textContent = '';
    if (picks.running) {
      var spinner = document.createElement('span');
      spinner.className = 'btn-spinner';
      spinner.setAttribute('aria-hidden', 'true');
      btn.appendChild(spinner);
    }
    btn.appendChild(document.createTextNode(picks.running ? 'Refreshing shared picks…' : left ? 'Refresh shared picks in ' + left : name));
    if (picks.running || !picks.known || why) btn.setAttribute('aria-disabled', 'true');
    else btn.removeAttribute('aria-disabled');
    btn.setAttribute('aria-label', picks.running ? 'Refreshing shared picks for everyone' : left ? name + ', available in ' + left : name);
    if (why) btn.setAttribute('title', why);
    else btn.removeAttribute('title');
    clearTimeout(picksTimer);
    if (left && !picks.running) picksTimer = setTimeout(drawPicks, 60000);
  }

  function takePicks(st) {
    picks.known = true;
    picks.nextAt = st.next_available_at || null;
  }

  function picksFailed(message) {
    picks.running = false;
    drawPicks();
    window.UIFeedback.error(message);
  }

  /* The session ended, or is no admin's any more: to sign-in, unless that would lose
     edits. A 401 gets here only then, and MM.api has told the person. */
  function picksLoggedOut(err) {
    if (!err || (err.status !== 401 && err.status !== 403)) return false;
    if (MM.api.hasUnsavedChanges()) {
      picks.running = false;
      drawPicks();
      if (err.status === 403) window.UIFeedback.error('You are no longer signed in as an admin.');
    } else {
      window.location.href = '/login?next=%2Fsettings' + (window.location.hash || '');
    }
    return true;
  }

  /* Follow a running refresh to its end. */
  function followPicks() {
    var failures = 0;
    var idle = 0;
    picks.running = true;
    drawPicks();
    function later(ms) { setTimeout(tick, ms); }
    function tick() {
      if (document.hidden) { later(POLL_MS); return; }
      MM.api.get(PICKS_URL + '/status').then(function (st) {
        failures = 0;
        idle = st.status === 'idle' ? idle + 1 : 0;
        if (st.status === 'running' || (st.status === 'idle' && idle < IDLE_LIMIT)) { later(POLL_MS); return; }
        takePicks(st);
        if (st.status === 'done' && st.result && st.result.ok) {
          picks.running = false;
          drawPicks();
          window.UIFeedback.success('New shared picks are on Watch.');
          return;
        }
        picksFailed((st.result && st.result.error) || 'The refresh stopped before it finished. Try again.');
      }).catch(function (err) {
        if (picksLoggedOut(err)) return;
        failures += 1;
        if (failures < MAX_FAILURES) { later(Math.min(POLL_MS * Math.pow(2, failures), POLL_MAX_MS)); return; }
        picksFailed('Couldn’t reach the server. Check your connection and try again.');
      });
    }
    later(POLL_MS);
  }

  function refreshPicks() {
    if (picks.running || !picks.known) return;
    var why = picksWhy();
    if (why) { window.UIFeedback.toast(why); return; }
    picks.running = true;
    drawPicks();
    MM.api.post(PICKS_URL).then(followPicks, function (err) {
      if (picksLoggedOut(err)) return;
      var next = err && err.status === 429 && err.data && err.data.next_available_at;
      if (next) {
        picks.running = false;
        picks.nextAt = next;
        drawPicks();
        return;
      }
      picksFailed(MM.api.messageOf(err, 'Couldn’t refresh the shared picks. Try again.'));
    });
  }

  /* What the refresh needs, as the settings are stored. */
  function readPicksSettings(valueOf) {
    picks.hasKey = !!valueOf('openai_api_key');
    picks.on = [false, 'false'].indexOf(valueOf('suggestions_enabled')) === -1;
  }

  function startPicks() {
    var btn = picksButton();
    if (!btn) return;
    readPicksSettings(fieldValue);
    drawPicks();
    btn.addEventListener('click', refreshPicks);
    MM.api.get(PICKS_URL + '/status').then(function (st) {
      takePicks(st);
      if (st.status === 'running') followPicks();
      else drawPicks();
    }, function () {
      // Not known: the server says so when it is pressed.
      picks.known = true;
      drawPicks();
    });
  }

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

    /* A test reads the saved settings, so its button says so while its card holds other values. */
    hasEdits: function (btn) {
      var card = btn.closest('.intg-card');
      return !!(card && MM.settings.savebar) &&
        Array.prototype.some.call(card.querySelectorAll('input[id], select[id]'), MM.settings.savebar.isEdited);
    },

    paintTests: function () {
      document.querySelectorAll('[data-test-service]').forEach(function (btn) {
        var label = MM.settings.integrations.hasEdits(btn) ? 'Test saved' : 'Test';
        // Leaving a field for this button repaints it mid-press, and Safari drops the click if the text is replaced.
        if (!btn.disabled && btn.textContent !== label) btn.textContent = label;
      });
    },

    /* The pill label changes on the page-load auto-check too, so only this
       separate region is announced, and only for a pressed Test. */
    announce: function (service, text) {
      document.querySelectorAll('#intg-' + service + ' > [data-conn-announce]').forEach(function (el) {
        el.textContent = text;
      });
    },

    /* Checks each service that reads one of `keys`. `values` are the settings as now stored:
       a field may hold a newer edit. */
    recheck: function (keys, values) {
      readPicksSettings(function (id) { return values[id]; });
      drawPicks();
      autoTest(AUTO_CHECKS.filter(function (c) {
        return c.fields.concat(c.also || []).some(function (key) { return keys.indexOf(key) !== -1; });
      }), function (id) { return values[id]; });
    },

    init: function () {
      var self = MM.settings.integrations;

      function serviceName(btn) {
        var h = btn.closest('.intg-card') && btn.closest('.intg-card').querySelector('h3');
        return h ? h.textContent.trim() : btn.getAttribute('data-test-service');
      }

      // An answer that already begins with the service's name is not given it twice.
      function said(btn, label) {
        var name = serviceName(btn);
        return label.indexOf(name) === 0 ? label : name + ': ' + label;
      }

      function testService(service, btn) {
        var run = begin(service);
        self.setConnStatus(service, 'untested', 'Testing…');
        if (btn) self.announce(service, '');
        if (btn) {
          if (self.hasEdits(btn)) window.UIFeedback.toast('Testing ' + serviceName(btn) + ' as last saved. Save to test your changes.');
          btn.disabled = true; btn.textContent = 'Testing…';
        }
        MM.api.post('/api/settings/test/' + encodeURIComponent(service))
          .then(function () {
            if (!isNewest(service, run)) return;
            self.setConnStatus(service, 'ok', 'Connected');
            if (btn) { btn.textContent = 'OK ✓'; self.announce(service, serviceName(btn) + ': Connected'); }
          })
          .catch(function (err) {
            if (!isNewest(service, run)) return;
            // MM.api rejects on ok:false too, so a failed test lands here.
            var label = setFailedStatus(service, err);
            // A throttled test did not fail; the button says to try again later.
            if (btn) { btn.textContent = (err && err.status === 429) ? 'Try later' : 'Failed'; self.announce(service, said(btn, label)); }
          })
          .then(function () {
            // .then used as finally (broad browser compat).
            if (!btn) return;
            setTimeout(function () {
              var keepFocus = MM.dom.focusStillIn(btn);
              btn.disabled = false;
              self.paintTests();
              if (keepFocus) btn.focus({ preventScroll: true });
            }, 1600);
          });
      }

      document.querySelectorAll('[data-test-service]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          testService(btn.getAttribute('data-test-service'), btn);
        });
      });

      autoTest(AUTO_CHECKS, fieldValue);
      startPicks();

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
              if (keepFocus) syncBtn.focus({ preventScroll: true });
            }, 1800);
          });
      });
    },
  };

})();
