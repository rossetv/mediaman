/**
 * settings/general.js — General settings module.
 *
 * Responsibilities:
 *   - Bootstrap payload parsing (setg-bootstrap JSON node)
 *   - Savebar dirty/clean tracking and save/discard logic
 *   - Toggle switches (<span data-toggle>)
 *   - Integration-card collapse/expand (<[data-intg-toggle]>)
 *   - Secret-reveal buttons (<button.inp-reveal>)
 *   - Rail scroll-spy (setg-rail-item active highlighting)
 *   - Plex library pills and disk-threshold card rendering
 *   - collectSettings() — gathers all form fields for PUT /api/settings
 *   - Overview hero refresh (schedule summary, storage bar)
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *   MM.dom  (core/dom.js)
 *
 * Exposes:
 *   MM.settings.general.init()
 *   MM.settings.general.markDirty()
 *   MM.settings.general.markClean()
 *   MM.settings.general.collectSettings()
 *   MM.settings.general.refreshOverviewHero()
 *   MM.settings.general.openReauthModal(opts)  — used by integrations + users
 *   MM.settings.general.BOOT                   — parsed bootstrap payload
 *   MM.settings.general.SELECTED_LIBS          — mutable array (mutated by integrations)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  MM.settings.general = {

    // ------------------------------------------------------------------
    // State — set during init(), readable by sibling modules.
    // ------------------------------------------------------------------
    BOOT: {},
    SELECTED_LIBS: [],
    DISK_THRESHOLDS: {},

    init: function () {
      var self = MM.settings.general;

      // ----------------------------------------------------------------
      // Bootstrap payload
      // ----------------------------------------------------------------
      try {
        var node = document.getElementById('setg-bootstrap');
        if (node && node.textContent) { self.BOOT = JSON.parse(node.textContent); }
      } catch (_err) { self.BOOT = {}; }

      self.SELECTED_LIBS = (self.BOOT.selected_libraries || []).map(String);
      self.DISK_THRESHOLDS = self.BOOT.disk_thresholds || {};

      // ----------------------------------------------------------------
      // Savebar
      // ----------------------------------------------------------------
      var savebar = document.getElementById('setg-savebar');
      function markDirty() { if (savebar) savebar.classList.add('on'); }
      function markClean() { if (savebar) savebar.classList.remove('on'); }
      self.markDirty = markDirty;
      self.markClean = markClean;

      // The send-newsletter panel contains ephemeral recipient checkboxes
      // that are not persisted settings, so changes inside it must not
      // mark dirty.
      function isSettingsInput(el) {
        return el.closest('.setg-pg') && !el.closest('#newsletter-send-panel');
      }
      document.addEventListener('input', function (e) {
        if (isSettingsInput(e.target)) markDirty();
      }, true);
      document.addEventListener('change', function (e) {
        if (isSettingsInput(e.target)) markDirty();
      }, true);

      // ----------------------------------------------------------------
      // Toggle switches — <span class="tog" data-toggle data-target="id">
      // mirrors state to a hidden <input id="id">.
      // ----------------------------------------------------------------
      function toggle(node) {
        var on = !node.classList.contains('on');
        node.classList.toggle('on', on);
        node.setAttribute('aria-checked', on ? 'true' : 'false');
        var target = node.getAttribute('data-target');
        if (target) {
          var input = document.getElementById(target);
          if (input) {
            var onVal = node.getAttribute('data-on-value') || 'true';
            var offVal = node.getAttribute('data-off-value') || 'false';
            input.value = on ? onVal : offVal;
          }
        }
        markDirty();
      }
      document.querySelectorAll('.setg-pg [data-toggle]').forEach(function (n) {
        n.addEventListener('click', function () { toggle(n); });
        n.addEventListener('keydown', function (e) {
          if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(n); }
        });
      });

      // ----------------------------------------------------------------
      // Integration cards — collapsible.
      // ----------------------------------------------------------------
      function toggleIntg(hd) {
        var card = hd.closest('.intg-card');
        if (!card) return;
        var body = document.getElementById(hd.getAttribute('aria-controls'));
        var expanded = card.classList.toggle('is-collapsed');
        var isOpen = !expanded;
        hd.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        if (body) {
          if (isOpen) { body.removeAttribute('hidden'); }
          else        { body.setAttribute('hidden', ''); }
        }
      }
      document.querySelectorAll('.setg-pg [data-intg-toggle]').forEach(function (hd) {
        hd.addEventListener('click', function (e) {
          if (e.target.closest('.btn-test')) return;
          if (e.target.closest('.conn'))     return;
          toggleIntg(hd);
        });
        hd.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleIntg(hd);
          }
        });
      });

      // ----------------------------------------------------------------
      // Secret reveal — <button class="inp-reveal" data-reveal-target="id">
      // ----------------------------------------------------------------
      document.querySelectorAll('.setg-pg .inp-reveal').forEach(function (btn) {
        btn.addEventListener('click', function (e) {
          e.preventDefault();
          var id = btn.getAttribute('data-reveal-target');
          var input = id ? document.getElementById(id) : btn.parentElement.querySelector('input');
          if (!input) return;
          var hidden = input.type === 'password';
          input.type = hidden ? 'text' : 'password';
          btn.textContent = hidden ? 'Hide' : 'Show';
        });
      });

      // ----------------------------------------------------------------
      // Rail scroll-spy
      // ----------------------------------------------------------------
      var rail = document.querySelector('.setg-rail');
      var railItems = document.querySelectorAll('.setg-rail-item');
      var blocks = document.querySelectorAll('.setg-block');
      var lastActiveHref = null;
      function syncRail() {
        if (!blocks.length) return;
        var pos = window.scrollY + 140;
        var current = blocks[0];
        blocks.forEach(function (b) { if (b.offsetTop <= pos) current = b; });
        var atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 4);
        if (atBottom) current = blocks[blocks.length - 1];
        var id = current ? '#' + current.id : '';
        var activeEl = null;
        railItems.forEach(function (r) {
          var on = r.getAttribute('href') === id;
          r.classList.toggle('on', on);
          if (on) activeEl = r;
        });
        if (activeEl && id !== lastActiveHref && rail && rail.scrollWidth > rail.clientWidth) {
          var target = activeEl.offsetLeft - (rail.clientWidth - activeEl.offsetWidth) / 2;
          rail.scrollTo({ left: Math.max(0, target), behavior: 'smooth' });
        }
        lastActiveHref = id;
      }
      window.addEventListener('scroll', syncRail, { passive: true });
      syncRail();

      // ----------------------------------------------------------------
      // Plex libraries — render pills + disk-threshold rows.
      // ----------------------------------------------------------------
      var PLEX_LIBRARIES = [];

      function renderLibraryPills() {
        var container = document.getElementById('plex-library-pills');
        if (!container) return;
        container.replaceChildren();
        if (!PLEX_LIBRARIES.length) {
          var msg = document.createElement('span');
          msg.className = 'fld-sub';
          msg.textContent = 'No libraries found — check Plex URL and token, then reload.';
          container.appendChild(msg);
          updateLibPillCount();
          return;
        }
        PLEX_LIBRARIES.forEach(function (lib) {
          var pill = document.createElement('span');
          pill.className = 'lib-pill' + (self.SELECTED_LIBS.indexOf(String(lib.id)) !== -1 ? ' on' : '');
          pill.textContent = lib.title;
          pill.dataset.libId = String(lib.id);
          pill.addEventListener('click', function () {
            var idx = self.SELECTED_LIBS.indexOf(this.dataset.libId);
            if (idx === -1) { self.SELECTED_LIBS.push(this.dataset.libId); this.classList.add('on'); }
            else { self.SELECTED_LIBS.splice(idx, 1); this.classList.remove('on'); }
            renderDiskRows();
            updateLibPillCount();
            markDirty();
          });
          container.appendChild(pill);
        });
        updateLibPillCount();
      }

      function updateLibPillCount() {
        var el = document.getElementById('lib-pill-count');
        if (!el) return;
        el.textContent = self.SELECTED_LIBS.length + ' of ' + PLEX_LIBRARIES.length + ' selected';
      }

      function renderDiskRows() {
        var container = document.getElementById('disk-threshold-rows');
        if (!container) return;
        container.replaceChildren();
        var selected = PLEX_LIBRARIES.filter(function (l) { return self.SELECTED_LIBS.indexOf(String(l.id)) !== -1; });
        if (!selected.length) {
          var hint = document.createElement('div');
          hint.className = 'fld-sub';
          hint.textContent = 'Select Plex libraries above to configure paths and thresholds.';
          container.appendChild(hint);
          return;
        }
        selected.forEach(function (lib) {
          var cfg = self.DISK_THRESHOLDS[String(lib.id)] || {};
          container.appendChild(buildLibPathCard(lib, cfg));
        });
      }

      function buildLibPathCard(lib, cfg) {
        var card = document.createElement('div');
        card.className = 'lp-card';
        card.dataset.libId = String(lib.id);

        var head = document.createElement('div');
        head.className = 'lp-head';
        var glyph = document.createElement('div');
        glyph.className = 'lp-glyph';
        glyph.textContent = (lib.title || '?').charAt(0).toUpperCase();
        head.appendChild(glyph);

        var title = document.createElement('div');
        title.className = 'lp-title';
        var nameTxt = document.createElement('div');
        nameTxt.className = 'lp-name-txt';
        nameTxt.textContent = lib.title;
        var kind = document.createElement('div');
        kind.className = 'lp-kind';
        kind.textContent = 'Plex library' + (lib.type ? ' · ' + lib.type : '');
        title.appendChild(nameTxt);
        title.appendChild(kind);
        head.appendChild(title);

        var usage = document.createElement('div');
        usage.className = 'lp-usage';
        var pct = document.createElement('span');
        pct.className = 'pct';
        pct.dataset.role = 'pct';
        pct.textContent = '—';
        var bytes = document.createElement('span');
        bytes.className = 'bytes';
        bytes.dataset.role = 'bytes';
        bytes.textContent = ' ';
        usage.appendChild(pct);
        usage.appendChild(bytes);
        head.appendChild(usage);
        card.appendChild(head);

        var bar = document.createElement('div');
        bar.className = 'lp-bar2';
        var fill = document.createElement('span');
        fill.className = 'fill';
        fill.dataset.role = 'fill';
        fill.style.width = '0%';
        bar.appendChild(fill);
        var mark = document.createElement('span');
        mark.className = 'thresh-mark';
        mark.dataset.role = 'mark';
        bar.appendChild(mark);
        card.appendChild(bar);

        var body = document.createElement('div');
        body.className = 'lp-body';

        var pathFld = document.createElement('div');
        pathFld.className = 'fld';
        var pathLbl = document.createElement('div');
        pathLbl.className = 'fld-lbl';
        pathLbl.textContent = 'Filesystem path';
        var pathInput = document.createElement('input');
        pathInput.className = 'inp inp--mono';
        pathInput.type = 'text';
        pathInput.placeholder = '/media/movies';
        pathInput.value = cfg.path || '';
        pathInput.dataset.field = 'path';
        pathFld.appendChild(pathLbl);
        pathFld.appendChild(pathInput);
        body.appendChild(pathFld);

        var thrFld = document.createElement('div');
        thrFld.className = 'fld';
        var thrLbl = document.createElement('div');
        thrLbl.className = 'fld-lbl';
        thrLbl.textContent = 'Scan above';
        var thrWrap = document.createElement('div');
        thrWrap.className = 'thresh-field';
        var thrInput = document.createElement('input');
        thrInput.className = 'inp inp--num';
        thrInput.type = 'number';
        thrInput.min = '0';
        thrInput.max = '100';
        thrInput.value = cfg.threshold || 0;
        thrInput.dataset.field = 'threshold';
        thrWrap.appendChild(thrInput);
        var unit = document.createElement('span');
        unit.className = 'unit';
        unit.textContent = '% used';
        thrWrap.appendChild(unit);
        thrFld.appendChild(thrLbl);
        thrFld.appendChild(thrWrap);
        body.appendChild(thrFld);

        card.appendChild(body);

        var foot = document.createElement('div');
        foot.className = 'lp-foot';
        var state = document.createElement('span');
        state.className = 'untested';
        state.dataset.role = 'state';
        state.textContent = cfg.threshold ? 'Checking…' : 'No threshold set — always scans';
        foot.appendChild(state);
        card.appendChild(foot);

        function refresh() {
          var path = pathInput.value.trim();
          var threshold = parseInt(thrInput.value, 10) || 0;
          if (!path || threshold <= 0) {
            paintState(card, null, threshold);
            return;
          }
          MM.api.get('/api/settings/disk-usage?path=' + encodeURIComponent(path))
            .then(function (data) {
              if (data.error) { paintState(card, 'err', threshold, data.error); return; }
              paintState(card, null, threshold, null, data.usage_pct);
            })
            .catch(function () { paintState(card, 'err', threshold, 'Fetch error'); });
        }
        pathInput.addEventListener('blur', refresh);
        thrInput.addEventListener('change', refresh);
        if (cfg.path && cfg.threshold) refresh();

        return card;
      }

      function paintState(card, force, threshold, errMsg, usagePct) {
        var pct   = card.querySelector('[data-role="pct"]');
        var bytes = card.querySelector('[data-role="bytes"]');
        var fill  = card.querySelector('[data-role="fill"]');
        var mark  = card.querySelector('[data-role="mark"]');
        var state = card.querySelector('[data-role="state"]');

        mark.style.left = threshold ? threshold + '%' : '-10px';

        if (force === 'err') {
          state.className = 'crit';
          state.textContent = errMsg || 'Path error';
          pct.textContent = '—';
          bytes.textContent = ' ';
          fill.style.width = '0%';
          return;
        }
        if (!threshold || threshold <= 0) {
          state.className = 'untested';
          state.textContent = 'No threshold set — always scans';
          pct.textContent = '—';
          bytes.textContent = ' ';
          fill.style.width = '0%';
          return;
        }
        if (usagePct === null || usagePct === undefined) {
          state.className = 'untested';
          state.textContent = 'Enter a path to check usage';
          pct.textContent = '—';
          bytes.textContent = ' ';
          fill.style.width = '0%';
          return;
        }
        pct.textContent = usagePct.toFixed(1) + '%';
        fill.style.width = Math.min(100, usagePct) + '%';
        if (usagePct >= threshold) {
          pct.className = 'pct crit';
          fill.className = 'fill crit';
          state.className = 'crit';
          state.textContent = 'Above threshold — will scan';
        } else if (usagePct >= threshold - 10) {
          pct.className = 'pct warn';
          fill.className = 'fill warn';
          state.className = 'warn';
          state.textContent = 'Approaching threshold';
        } else {
          pct.className = 'pct';
          fill.className = 'fill';
          state.className = 'ok';
          state.textContent = 'Below threshold — will skip';
        }
      }

      function collectDiskThresholds() {
        var out = {};
        document.querySelectorAll('#disk-threshold-rows .lp-card').forEach(function (card) {
          var libId = card.dataset.libId;
          var path = card.querySelector('[data-field="path"]').value.trim();
          var threshold = parseInt(card.querySelector('[data-field="threshold"]').value, 10) || 0;
          if (path || threshold) { out[libId] = { path: path, threshold: threshold }; }
        });
        return out;
      }

      function loadPlexLibraries() {
        MM.api.get('/api/plex/libraries')
          .then(function (data) {
            PLEX_LIBRARIES = data.libraries || [];
            renderLibraryPills();
            renderDiskRows();
          })
          .catch(function () { PLEX_LIBRARIES = []; renderLibraryPills(); });
      }
      self._loadPlexLibraries = loadPlexLibraries;

      // ----------------------------------------------------------------
      // collectSettings — called by save handler.
      // ----------------------------------------------------------------
      function collectSettings() {
        function v(id) { var el = document.getElementById(id); return el ? el.value : ''; }
        function n(id) { var x = parseInt(v(id), 10); return isFinite(x) ? x : 0; }
        return {
          plex_url:              v('plex_url'),
          plex_public_url:       v('plex_public_url'),
          plex_token:            v('plex_token'),
          plex_libraries:        self.SELECTED_LIBS,
          sonarr_url:            v('sonarr_url'),
          sonarr_public_url:     v('sonarr_public_url'),
          sonarr_api_key:        v('sonarr_api_key'),
          radarr_url:            v('radarr_url'),
          radarr_public_url:     v('radarr_public_url'),
          radarr_api_key:        v('radarr_api_key'),
          nzbget_url:            v('nzbget_url'),
          nzbget_public_url:     v('nzbget_public_url'),
          nzbget_username:       v('nzbget_username'),
          nzbget_password:       v('nzbget_password'),
          mailgun_domain:        v('mailgun_domain'),
          mailgun_from_address:  v('mailgun_from_address'),
          mailgun_api_key:       v('mailgun_api_key'),
          openai_api_key:        v('openai_api_key'),
          tmdb_api_key:          v('tmdb_api_key'),
          tmdb_read_token:       v('tmdb_read_token'),
          omdb_api_key:          v('omdb_api_key'),
          base_url:              v('base_url'),
          scan_day:              v('scan_day'),
          scan_time:             v('scan_time'),
          scan_timezone:         v('scan_timezone'),
          library_sync_interval: v('library_sync_interval'),
          min_age_days:          n('min_age_days'),
          inactivity_days:       n('inactivity_days'),
          grace_days:            n('grace_days'),
          disk_thresholds:       collectDiskThresholds(),
          suggestions_enabled:   v('suggestions_enabled'),
          auto_abandon_enabled:  v('auto_abandon_enabled'),
        };
      }
      self.collectSettings = collectSettings;

      // ----------------------------------------------------------------
      // Overview hero — schedule summary + storage bar.
      // ----------------------------------------------------------------
      function refreshOverviewHero() {
        var big = document.getElementById('ov-scan-big');
        var when = document.getElementById('ov-scan-when');
        if (!big || !when) return;
        var dayEl  = document.getElementById('scan_day');
        var timeEl = document.getElementById('scan_time');
        var tzEl   = document.getElementById('scan_timezone');
        var days   = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };
        var dayKey = dayEl ? dayEl.value : self.BOOT.scan_day;
        var day    = days[dayKey] || 'Monday';
        var time   = (timeEl && timeEl.value) || self.BOOT.scan_time || '09:00';
        var tz     = (tzEl && tzEl.value) || self.BOOT.scan_timezone || 'UTC';
        big.replaceChildren();
        big.appendChild(document.createTextNode(day + ' '));
        var small = document.createElement('small');
        small.textContent = time;
        big.appendChild(small);
        when.replaceChildren();
        when.appendChild(document.createTextNode('Timezone: '));
        var strong = document.createElement('b');
        strong.textContent = tz;
        when.appendChild(strong);
      }
      self.refreshOverviewHero = refreshOverviewHero;
      refreshOverviewHero();

      // Storage overview bar (IIFE — side-effect only, no public surface needed).
      (function () {
        var bar   = document.getElementById('ov-storage-bar');
        var chips = document.getElementById('ov-storage-chips');
        var big   = document.getElementById('ov-storage-big');
        if (!bar || !chips || !big) return;

        function renderUnconfigured() {
          big.textContent = 'Not configured';
          chips.replaceChildren();
          var note = document.createElement('span');
          note.style.color = 'var(--t3)';
          note.textContent = 'Set library paths under Libraries & Paths to populate this view.';
          chips.appendChild(note);
        }

        function renderStorage(s) {
          big.replaceChildren();
          big.appendChild(document.createTextNode(s.used + ' '));
          var small = document.createElement('small');
          small.textContent = 'of ' + s.total + ' used · ' + s.free + ' free';
          big.appendChild(small);

          bar.replaceChildren();
          var segs = [
            { pct: s.movies_pct, bg: 'var(--orange)' },
            { pct: s.tv_pct,     bg: 'var(--accent)' },
            { pct: s.anime_pct,  bg: 'var(--purple)' },
            { pct: s.other_pct,  bg: 'rgba(255,255,255,.2)' },
          ];
          segs.forEach(function (seg) {
            if (!seg.pct || seg.pct <= 0) return;
            var span = document.createElement('span');
            span.style.flex = '0 0 ' + seg.pct + '%';
            span.style.background = seg.bg;
            bar.appendChild(span);
          });

          chips.replaceChildren();
          var legend = [
            { bg: 'var(--orange)',        label: 'Movies',   val: s.movies_label, pct: s.movies_pct },
            { bg: 'var(--accent)',        label: 'TV Shows', val: s.tv_label,     pct: s.tv_pct     },
            { bg: 'var(--purple)',        label: 'Anime',    val: s.anime_label,  pct: s.anime_pct  },
            { bg: 'rgba(255,255,255,.3)', label: 'Other',    val: s.other_label,  pct: s.other_pct  },
          ];
          legend.forEach(function (item) {
            var row = document.createElement('span');
            var dot = document.createElement('span');
            dot.className = 'chip-dot';
            dot.style.background = item.bg;
            row.appendChild(dot);
            row.appendChild(document.createTextNode(item.label + ' '));
            var b = document.createElement('b');
            b.textContent = item.val + ' (' + item.pct + '%)';
            row.appendChild(b);
            chips.appendChild(row);
          });
        }

        var entries = Object.keys(self.DISK_THRESHOLDS || {});
        if (!entries.length) { renderUnconfigured(); return; }

        MM.api.get('/api/dashboard/stats')
          .then(function (data) {
            if (!data || !data.storage) return;
            renderStorage(data.storage);
          })
          .catch(function () { /* leave placeholder — network/backend hiccup */ });
      })();

      // ----------------------------------------------------------------
      // Save bar — PUT /api/settings.
      // ----------------------------------------------------------------
      var saveBtn    = document.getElementById('btn-save');
      var statusEl   = document.getElementById('save-status');
      var savebarEl  = document.getElementById('setg-savebar');

      // Turn FastAPI 422 bodies into a one-line human summary.
      function summariseSaveError(data, statusCode) {
        if (data && Array.isArray(data.detail) && data.detail.length) {
          return data.detail.map(function (d) {
            var loc = Array.isArray(d.loc) ? d.loc.filter(function (p) { return p !== 'body'; }) : [];
            var field = loc.length ? loc.join('.') : 'request';
            var msg = (d.msg || 'invalid').replace(/^Value error,\s*/, '');
            return field + ' — ' + msg;
          }).join(' · ');
        }
        if (data && typeof data.detail === 'string') return data.detail;
        if (data && data.error) return data.error;
        if (statusCode === 429) return 'Too many saves — slow down for a moment.';
        return 'Save failed (HTTP ' + (statusCode || '?') + ')';
      }

      function setSaveError(msg) {
        if (savebarEl) savebarEl.classList.add('is-error');
        if (statusEl) statusEl.textContent = msg;
        if (saveBtn) {
          saveBtn.classList.remove('btn--primary');
          saveBtn.classList.add('btn--danger');
          saveBtn.textContent = 'Try again';
        }
      }

      function clearSaveError() {
        if (savebarEl) savebarEl.classList.remove('is-error');
        if (saveBtn) {
          saveBtn.classList.remove('btn--danger');
          saveBtn.classList.add('btn--primary');
        }
      }

      // runSave is extracted so the reauth retry path can re-fire the same
      // payload without duplicating response-bookkeeping.
      function runSave(payload) {
        if (!saveBtn) return;
        saveBtn.disabled = true;
        clearSaveError();
        var orig = 'Save Settings';
        saveBtn.textContent = 'Saving…';
        if (statusEl) statusEl.textContent = '';

        var statusCode = 0;
        fetch('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
          .then(function (r) { statusCode = r.status; return r.json().catch(function () { return {}; }); })
          .then(function (data) {
            var ok = statusCode >= 200 && statusCode < 300 && data.status === 'saved';
            var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });

            if (ok) {
              saveBtn.textContent = 'Saved ✓';
              if (statusEl) {
                statusEl.textContent = ignored.length
                  ? 'Saved · ignored unknown: ' + ignored.join(', ')
                  : 'Settings saved';
              }
              setTimeout(markClean, 600);
              refreshOverviewHero();
              setTimeout(function () {
                saveBtn.textContent = orig;
                saveBtn.disabled = false;
                if (statusEl && !ignored.length) {
                  statusEl.textContent = 'Edits apply to every section at once.';
                }
              }, 2200);
              return;
            }

            // Sensitive-settings reauth gate. The backend returns 403 with
            // reauth_required: true when the session has no recent reauth
            // ticket. Open the reauth modal; on success, re-fire the same
            // payload so the user keeps their unsaved edits.
            if (statusCode === 403 && data && data.reauth_required) {
              saveBtn.textContent = orig;
              saveBtn.disabled = false;
              if (statusEl) statusEl.textContent = '';
              self.openReauthModal({
                onSubmit: function (pw) {
                  return fetch('/api/auth/reauth', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: pw }),
                  }).then(function (r) {
                    var sc = r.status;
                    return r.json().catch(function () { return {}; }).then(function (d) {
                      if (sc >= 200 && sc < 300 && d && d.ok) {
                        runSave(payload);
                        return { ok: true };
                      }
                      return { ok: false, error: (d && d.error) || ('Reauth failed (HTTP ' + sc + ')') };
                    });
                  });
                },
                onCancel: function () {
                  setSaveError('Save cancelled — re-authentication required.');
                },
              });
              return;
            }

            setSaveError(summariseSaveError(data, statusCode));
            saveBtn.disabled = false;
          })
          .catch(function () {
            setSaveError("Couldn't reach the server — check your connection and try again.");
            if (saveBtn) saveBtn.disabled = false;
          });
      }

      if (saveBtn) saveBtn.addEventListener('click', function () { runSave(collectSettings()); });

      // Clear error chrome whenever the user starts typing again.
      document.addEventListener('input', function () {
        if (savebarEl && savebarEl.classList.contains('is-error')) clearSaveError();
      }, true);

      var discardBtn = document.getElementById('btn-discard');
      if (discardBtn) discardBtn.addEventListener('click', function () {
        /* TODO(H73): use UIFeedback.confirm instead of window.confirm for consistency. */
        window.UIFeedback.confirm({
          title: 'Discard unsaved changes?',
          body: 'The page will reload and any unsaved settings will be lost.',
          confirmLabel: 'Discard',
          confirmVariant: 'danger',
        }).then(function (ok) {
          if (ok) window.location.reload();
        });
      });

      // ----------------------------------------------------------------
      // Reauth modal — centred dialog used by the savebar reauth gate.
      // Distinct from users.js openPasswordPrompt (an inline drawer).
      // ----------------------------------------------------------------
      function openReauthModal(opts) {
        document.querySelectorAll('.modal-backdrop.reauth-backdrop').forEach(function (n) { n.remove(); });

        var backdrop = document.createElement('div');
        backdrop.className = 'modal-backdrop reauth-backdrop';
        backdrop.style.alignItems = 'center';

        var sheet = document.createElement('div');
        sheet.className = 'modal-sheet reauth-sheet';
        sheet.setAttribute('role', 'dialog');
        sheet.setAttribute('aria-modal', 'true');
        sheet.setAttribute('aria-labelledby', 'reauth-title');

        var title = document.createElement('h2');
        title.className = 'reauth-title';
        title.id = 'reauth-title';
        title.textContent = 'Re-authenticate to save';
        sheet.appendChild(title);

        var desc = document.createElement('p');
        desc.className = 'reauth-desc';
        desc.textContent = 'mediaman requires a recent password confirmation before changing credentials or other sensitive options.';
        sheet.appendChild(desc);

        var field = document.createElement('label');
        field.className = 'reauth-field';
        var fieldLbl = document.createElement('span');
        fieldLbl.className = 'reauth-field-lbl';
        fieldLbl.textContent = 'Your password';
        field.appendChild(fieldLbl);
        var input = document.createElement('input');
        input.className = 'inp';
        input.type = 'password';
        input.autocomplete = 'current-password';
        field.appendChild(input);
        sheet.appendChild(field);

        var actions = document.createElement('div');
        actions.className = 'reauth-actions';
        var msg = document.createElement('span');
        msg.className = 'reauth-msg';
        actions.appendChild(msg);
        var cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn btn--ghost btn--sm';
        cancelBtn.textContent = 'Cancel';
        actions.appendChild(cancelBtn);
        var confirmBtn = document.createElement('button');
        confirmBtn.type = 'button';
        confirmBtn.className = 'btn btn--primary btn--sm';
        confirmBtn.textContent = 'Confirm & save';
        actions.appendChild(confirmBtn);
        sheet.appendChild(actions);

        backdrop.appendChild(sheet);
        document.body.appendChild(backdrop);
        setTimeout(function () { input.focus(); }, 0);

        var cancelled = false;
        function close(viaCancel) {
          if (cancelled) return;
          cancelled = true;
          backdrop.remove();
          document.removeEventListener('keydown', onKey);
          if (viaCancel && typeof opts.onCancel === 'function') opts.onCancel();
        }
        function onKey(e) { if (e.key === 'Escape') close(true); }
        document.addEventListener('keydown', onKey);
        cancelBtn.addEventListener('click', function () { close(true); });
        backdrop.addEventListener('click', function (e) { if (e.target === backdrop) close(true); });

        function submit() {
          var pw = input.value;
          if (!pw) { msg.textContent = 'Password required.'; return; }
          confirmBtn.disabled = true;
          msg.textContent = '';
          Promise.resolve(opts.onSubmit(pw)).then(function (res) {
            confirmBtn.disabled = false;
            if (res && res.ok) {
              cancelled = true; // suppress onCancel
              backdrop.remove();
              document.removeEventListener('keydown', onKey);
            } else {
              msg.textContent = (res && res.error) || 'Failed';
              input.focus();
              input.select();
            }
          }).catch(function () {
            confirmBtn.disabled = false;
            msg.textContent = 'Network error. Try again.';
          });
        }
        confirmBtn.addEventListener('click', submit);
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') submit();
        });
      }
      self.openReauthModal = openReauthModal;

      // ----------------------------------------------------------------
      // Boot.
      // ----------------------------------------------------------------
      loadPlexLibraries();
    },
  };

})();
