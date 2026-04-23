/**
 * Settings page — client-side glue for the v2 redesign.
 *
 * All DOM building uses safe APIs (createElement / textContent);
 * we never assign to element.innerHTML. Every fetch is scoped to
 * existing backend endpoints. The file is self-contained (no
 * imports) so it can be served as a static asset.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------------
  // Bootstrap — values the template rendered server-side.
  // ---------------------------------------------------------------------
  var BOOT = {};
  try {
    var node = document.getElementById('setg-bootstrap');
    if (node && node.textContent) { BOOT = JSON.parse(node.textContent); }
  } catch (_err) { BOOT = {}; }

  var SELECTED_LIBS = (BOOT.selected_libraries || []).map(String);
  var DISK_THRESHOLDS = BOOT.disk_thresholds || {};
  var PLEX_LIBRARIES = [];

  // ---------------------------------------------------------------------
  // Savebar — lit up whenever any input changes.
  // ---------------------------------------------------------------------
  var savebar = document.getElementById('setg-savebar');
  function markDirty() { if (savebar) savebar.classList.add('on'); }
  function markClean() { if (savebar) savebar.classList.remove('on'); }

  // The send-newsletter panel contains ephemeral recipient checkboxes that
  // are not persisted settings, so changes inside it must not mark dirty.
  function isSettingsInput(el) {
    return el.closest('.setg-pg') && !el.closest('#newsletter-send-panel');
  }
  document.addEventListener('input', function (e) {
    if (isSettingsInput(e.target)) markDirty();
  }, true);
  document.addEventListener('change', function (e) {
    if (isSettingsInput(e.target)) markDirty();
  }, true);

  // ---------------------------------------------------------------------
  // Toggle switches — <span class="tog" data-toggle data-target="id">
  // mirrors its state to a hidden <input id="id">.
  // ---------------------------------------------------------------------
  function toggle(node) {
    var on = !node.classList.contains('on');
    node.classList.toggle('on', on);
    node.setAttribute('aria-checked', on ? 'true' : 'false');
    var target = node.getAttribute('data-target');
    if (target) {
      var input = document.getElementById(target);
      if (input) input.value = on ? 'true' : 'false';
    }
    markDirty();
  }
  document.querySelectorAll('.setg-pg [data-toggle]').forEach(function (n) {
    n.addEventListener('click', function () { toggle(n); });
    n.addEventListener('keydown', function (e) {
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(n); }
    });
  });

  // ---------------------------------------------------------------------
  // Integration cards — collapsible. Default collapsed; clicking the
  // header (or pressing Enter/Space with it focused) toggles the body.
  // Clicks on the Test button or the connection pill must NOT toggle.
  // ---------------------------------------------------------------------
  function toggleIntg(hd) {
    var card = hd.closest('.intg-card');
    if (!card) return;
    var body = document.getElementById(hd.getAttribute('aria-controls'));
    var expanded = card.classList.toggle('is-collapsed');
    // After toggle(), `expanded` is true when the class is NOW present
    // (i.e. card is collapsed). Flip the semantics for aria.
    var isOpen = !expanded;
    hd.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    if (body) {
      if (isOpen) { body.removeAttribute('hidden'); }
      else        { body.setAttribute('hidden', ''); }
    }
  }
  document.querySelectorAll('.setg-pg [data-intg-toggle]').forEach(function (hd) {
    hd.addEventListener('click', function (e) {
      // Don't toggle when the user clicked the Test button or an inner
      // control inside hd-actions. We still allow clicks on the chevron
      // itself (it's inert SVG).
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

  // ---------------------------------------------------------------------
  // Secret reveal — <button class="inp-reveal" data-reveal-target="id">
  // ---------------------------------------------------------------------
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

  // ---------------------------------------------------------------------
  // Rail scroll-spy — highlight the anchor of the section closest to the
  // top of the viewport.
  // ---------------------------------------------------------------------
  var rail = document.querySelector('.setg-rail');
  var railItems = document.querySelectorAll('.setg-rail-item');
  var blocks = document.querySelectorAll('.setg-block');
  var lastActiveHref = null;
  function syncRail() {
    if (!blocks.length) return;
    var pos = window.scrollY + 140;
    var current = blocks[0];
    blocks.forEach(function (b) { if (b.offsetTop <= pos) current = b; });
    // When the viewport has hit the bottom, the last section can never
    // reach offsetTop ≤ scrollY+140 on a short page — force-light it.
    var atBottom = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 4);
    if (atBottom) current = blocks[blocks.length - 1];
    var id = current ? '#' + current.id : '';
    var activeEl = null;
    railItems.forEach(function (r) {
      var on = r.getAttribute('href') === id;
      r.classList.toggle('on', on);
      if (on) activeEl = r;
    });
    // On mobile the rail is a horizontal scroller — keep the active chip
    // in view when the scroll-spy selection changes.
    if (activeEl && id !== lastActiveHref && rail && rail.scrollWidth > rail.clientWidth) {
      var target = activeEl.offsetLeft - (rail.clientWidth - activeEl.offsetWidth) / 2;
      rail.scrollTo({ left: Math.max(0, target), behavior: 'smooth' });
    }
    lastActiveHref = id;
  }
  window.addEventListener('scroll', syncRail, { passive: true });
  syncRail();

  // ---------------------------------------------------------------------
  // Connection status pills — shared updater for intg-card + ov-status.
  // ---------------------------------------------------------------------
  function setConnStatus(service, tone, label) {
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
  }

  // ---------------------------------------------------------------------
  // Test buttons — <button class="btn-test" data-test-service="plex">
  // ---------------------------------------------------------------------
  function testService(service, btn) {
    setConnStatus(service, 'untested', 'Testing…');
    if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
    fetch('/api/settings/test/' + encodeURIComponent(service), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          setConnStatus(service, 'ok', 'Connected');
          if (btn) btn.textContent = 'OK ✓';
        } else {
          setConnStatus(service, 'err', data.error || 'Error');
          if (btn) btn.textContent = 'Failed';
        }
      })
      .catch(function () {
        setConnStatus(service, 'err', 'Connection failed');
        if (btn) btn.textContent = 'Failed';
      })
      .finally(function () {
        if (!btn) return;
        setTimeout(function () { btn.textContent = 'Test'; btn.disabled = false; }, 1600);
      });
  }
  document.querySelectorAll('[data-test-service]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      testService(btn.getAttribute('data-test-service'), btn);
    });
  });

  // ---------------------------------------------------------------------
  // Auto-test configured services on page load.
  // ---------------------------------------------------------------------
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
      if (!configured) { setConnStatus(c.service, 'off', 'Not configured'); return; }
      setConnStatus(c.service, 'untested', 'Testing…');
      fetch('/api/settings/test/' + encodeURIComponent(c.service), { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          setConnStatus(c.service, data.ok ? 'ok' : 'err', data.ok ? 'Connected' : (data.error || 'Error'));
        })
        .catch(function () { setConnStatus(c.service, 'err', 'Connection failed'); });
    });
  }

  // ---------------------------------------------------------------------
  // Plex libraries — render pills + disk-threshold rows.
  // ---------------------------------------------------------------------
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
      pill.className = 'lib-pill' + (SELECTED_LIBS.indexOf(String(lib.id)) !== -1 ? ' on' : '');
      pill.textContent = lib.title;
      pill.dataset.libId = String(lib.id);
      pill.addEventListener('click', function () {
        var idx = SELECTED_LIBS.indexOf(this.dataset.libId);
        if (idx === -1) { SELECTED_LIBS.push(this.dataset.libId); this.classList.add('on'); }
        else { SELECTED_LIBS.splice(idx, 1); this.classList.remove('on'); }
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
    el.textContent = SELECTED_LIBS.length + ' of ' + PLEX_LIBRARIES.length + ' selected';
  }

  function renderDiskRows() {
    var container = document.getElementById('disk-threshold-rows');
    if (!container) return;
    container.replaceChildren();
    var selected = PLEX_LIBRARIES.filter(function (l) { return SELECTED_LIBS.indexOf(String(l.id)) !== -1; });
    if (!selected.length) {
      var hint = document.createElement('div');
      hint.className = 'fld-sub';
      hint.textContent = 'Select Plex libraries above to configure paths and thresholds.';
      container.appendChild(hint);
      return;
    }
    selected.forEach(function (lib) {
      var cfg = DISK_THRESHOLDS[String(lib.id)] || {};
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
      fetch('/api/settings/disk-usage?path=' + encodeURIComponent(path))
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r); })
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
    var pct = card.querySelector('[data-role="pct"]');
    var bytes = card.querySelector('[data-role="bytes"]');
    var fill = card.querySelector('[data-role="fill"]');
    var mark = card.querySelector('[data-role="mark"]');
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
    fetch('/api/plex/libraries')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        PLEX_LIBRARIES = data.libraries || [];
        renderLibraryPills();
        renderDiskRows();
      })
      .catch(function () { PLEX_LIBRARIES = []; renderLibraryPills(); });
  }

  // ---------------------------------------------------------------------
  // Save — PUT /api/settings.
  // ---------------------------------------------------------------------
  function collectSettings() {
    function v(id) { var el = document.getElementById(id); return el ? el.value : ''; }
    function n(id) { var x = parseInt(v(id), 10); return isFinite(x) ? x : 0; }
    return {
      plex_url:             v('plex_url'),
      plex_public_url:      v('plex_public_url'),
      plex_token:           v('plex_token'),
      plex_libraries:       SELECTED_LIBS,
      sonarr_url:           v('sonarr_url'),
      sonarr_public_url:    v('sonarr_public_url'),
      sonarr_api_key:       v('sonarr_api_key'),
      radarr_url:           v('radarr_url'),
      radarr_public_url:    v('radarr_public_url'),
      radarr_api_key:       v('radarr_api_key'),
      nzbget_url:           v('nzbget_url'),
      nzbget_public_url:    v('nzbget_public_url'),
      nzbget_username:      v('nzbget_username'),
      nzbget_password:      v('nzbget_password'),
      mailgun_domain:       v('mailgun_domain'),
      mailgun_from_address: v('mailgun_from_address'),
      mailgun_api_key:      v('mailgun_api_key'),
      openai_api_key:       v('openai_api_key'),
      tmdb_api_key:         v('tmdb_api_key'),
      tmdb_read_token:      v('tmdb_read_token'),
      omdb_api_key:         v('omdb_api_key'),
      base_url:             v('base_url'),
      scan_day:             v('scan_day'),
      scan_time:            v('scan_time'),
      scan_timezone:        v('scan_timezone'),
      library_sync_interval: v('library_sync_interval'),
      min_age_days:         n('min_age_days'),
      inactivity_days:      n('inactivity_days'),
      grace_days:           n('grace_days'),
      disk_thresholds:      collectDiskThresholds(),
      suggestions_enabled:  v('suggestions_enabled'),
    };
  }
  var saveBtn = document.getElementById('btn-save');
  var statusEl = document.getElementById('save-status');
  if (saveBtn) saveBtn.addEventListener('click', function () {
    saveBtn.disabled = true;
    var orig = saveBtn.textContent;
    saveBtn.textContent = 'Saving…';
    if (statusEl) statusEl.textContent = '';
    fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectSettings()),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var ok = data.status === 'saved';
        var ignored = (data.ignored || []).filter(function (k) { return k !== 'status'; });
        saveBtn.textContent = ok ? 'Saved ✓' : 'Error';
        if (statusEl) {
          if (ok && ignored.length) {
            statusEl.textContent = 'Saved · ignored unknown: ' + ignored.join(', ');
          } else if (ok) {
            statusEl.textContent = 'Settings saved';
          } else {
            statusEl.textContent = data.error || 'Save failed';
          }
        }
        if (ok) {
          setTimeout(markClean, 600);
          refreshOverviewHero();
        }
        setTimeout(function () {
          saveBtn.textContent = orig;
          saveBtn.disabled = false;
          if (ok && statusEl && !ignored.length) {
            statusEl.textContent = 'Edits apply to every section at once.';
          }
        }, 2200);
      })
      .catch(function () {
        saveBtn.textContent = 'Error';
        if (statusEl) statusEl.textContent = "Couldn't save settings. Try again.";
        setTimeout(function () { saveBtn.textContent = orig; saveBtn.disabled = false; }, 2200);
      });
  });

  var discardBtn = document.getElementById('btn-discard');
  if (discardBtn) discardBtn.addEventListener('click', function () {
    /* H73: use UIFeedback.confirm instead of window.confirm for consistency. */
    window.UIFeedback.confirm({
      title: 'Discard unsaved changes?',
      body: 'The page will reload and any unsaved settings will be lost.',
      confirmLabel: 'Discard',
      confirmVariant: 'danger',
    }).then(function (ok) {
      if (ok) window.location.reload();
    });
  });

  // ---------------------------------------------------------------------
  // Library sync.
  // ---------------------------------------------------------------------
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
    fetch('/api/library/sync', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        syncBtn.textContent = data.ok ? 'Synced ✓' : 'Failed';
      })
      .catch(function () { syncBtn.textContent = 'Failed'; })
      .finally(function () {
        setTimeout(function () {
          syncBtn.textContent = orig; syncBtn.disabled = false;
        }, 1800);
      });
  });

  // ---------------------------------------------------------------------
  // Subscribers.
  // ---------------------------------------------------------------------
  function loadSubscribers() {
    fetch('/api/subscribers')
      .then(function (r) { return r.json(); })
      .then(function (data) { renderSubscribers(data.subscribers || []); })
      .catch(function () {
        var list = document.getElementById('subscriber-list');
        if (list) { list.replaceChildren(); list.appendChild(makeMsg('Couldn\u2019t load subscribers.', 'err')); }
      });
  }
  function makeMsg(text, tone) {
    var el = document.createElement('div');
    el.className = 'fld-sub';
    if (tone === 'err') el.style.color = 'var(--danger)';
    el.textContent = text;
    return el;
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
    fetch('/api/subscribers/' + id, { method: 'DELETE' })
      .then(function () { loadSubscribers(); })
      .catch(function () { btn.disabled = false; });
  }
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
  }
  if (addSubBtn) addSubBtn.addEventListener('click', submitSubscriber);
  if (addSubInp) addSubInp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submitSubscriber(); }
  });

  // ---------------------------------------------------------------------
  // Newsletter send panel.
  // ---------------------------------------------------------------------
  var newsletterPanel = document.getElementById('newsletter-send-panel');
  var btnSendNL = document.getElementById('btn-send-newsletter');
  var btnCancelNL = document.getElementById('btn-cancel-newsletter');
  var btnConfirmNL = document.getElementById('btn-confirm-newsletter');
  var newsletterStatus = document.getElementById('newsletter-send-status');

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

  var _recipientFetchToken = 0;
  function renderRecipientCheckboxes() {
    var list = document.getElementById('newsletter-recipient-list');
    if (!list) return;
    list.replaceChildren();
    var token = ++_recipientFetchToken;
    fetch('/api/subscribers')
      .then(function (r) { return r.json(); })
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
    fetch('/api/newsletter/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recipients: recipients }),
    })
      .then(function (r) { return r.json(); })
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
      .catch(function () {
        btnConfirmNL.disabled = false;
        btnConfirmNL.textContent = 'Send newsletter';
        newsletterStatus.textContent = "Couldn't send. Try again.";
        newsletterStatus.className = 'inline-form-msg err';
      });
  });

  // ---------------------------------------------------------------------
  // Users.
  // ---------------------------------------------------------------------
  function loadUsers() {
    fetch('/api/users', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(renderUsers)
      .catch(function () {
        var list = document.getElementById('user-list');
        if (list) { list.replaceChildren(); list.appendChild(makeMsg("Couldn\u2019t load users.", 'err')); }
      });
  }
  function renderUsers(data) {
    var list = document.getElementById('user-list');
    if (!list) return;
    list.replaceChildren();
    if (!data.users || !data.users.length) { list.appendChild(makeMsg('No users yet.')); return; }
    data.users.forEach(function (user) {
      var row = document.createElement('div');
      row.className = 'usr-row';
      row.dataset.userId = user.id;

      var av = document.createElement('div');
      av.className = 'av';
      av.textContent = (user.username || '?').charAt(0).toUpperCase();
      row.appendChild(av);

      var meta = document.createElement('div');
      meta.className = 'usr-meta';
      var name = document.createElement('div');
      name.className = 'usr-name';
      name.textContent = user.username;
      if (user.username === data.current) {
        var you = document.createElement('span');
        you.className = 'you-pill';
        you.textContent = 'You';
        name.appendChild(document.createTextNode(' '));
        name.appendChild(you);
      }
      meta.appendChild(name);
      var sub = document.createElement('div');
      sub.className = 'usr-sub';
      sub.textContent = user.created_at ? ('Joined ' + String(user.created_at).slice(0, 10)) : '';
      meta.appendChild(sub);
      row.appendChild(meta);

      if (user.username !== data.current) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'link-danger';
        del.textContent = 'Delete';
        del.addEventListener('click', function () { openDeleteDrawer(user, row); });
        row.appendChild(del);
      }
      list.appendChild(row);
    });
  }

  function openDeleteDrawer(user, row) {
    document.querySelectorAll('.setg-pg .delete-drawer').forEach(function (n) { n.remove(); });
    var drawer = document.createElement('div');
    drawer.className = 'inline-form delete-drawer';

    var title = document.createElement('div');
    title.className = 'fld-lbl';
    title.style.textTransform = 'none';
    title.style.letterSpacing = '-.005em';
    title.style.fontSize = '14px';
    title.appendChild(document.createTextNode('Delete '));
    var strong = document.createElement('strong');
    strong.textContent = user.username;
    title.appendChild(strong);
    title.appendChild(document.createTextNode('?'));
    drawer.appendChild(title);

    var desc = document.createElement('div');
    desc.className = 'fld-sub';
    desc.style.marginTop = '6px';
    desc.textContent = 'Irreversible. All active sessions for this user will be terminated.';
    drawer.appendChild(desc);

    var lbl = document.createElement('label');
    lbl.className = 'fld';
    lbl.style.marginTop = '14px';
    var lblText = document.createElement('span');
    lblText.className = 'fld-lbl';
    lblText.textContent = 'Your password (re-authenticate)';
    lbl.appendChild(lblText);
    var input = document.createElement('input');
    input.className = 'inp';
    input.type = 'password';
    input.autocomplete = 'current-password';
    lbl.appendChild(input);
    drawer.appendChild(lbl);

    var actions = document.createElement('div');
    actions.className = 'inline-form-actions';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn--ghost btn--sm';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', function () { drawer.remove(); });
    actions.appendChild(cancel);
    var confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'btn btn--danger btn--sm';
    confirm.textContent = 'Delete user';
    actions.appendChild(confirm);
    var msg = document.createElement('span');
    msg.className = 'inline-form-msg';
    msg.style.margin = '0';
    actions.appendChild(msg);
    drawer.appendChild(actions);

    row.after(drawer);
    input.focus();

    function doDelete() {
      var pw = input.value;
      if (!pw) { msg.className = 'inline-form-msg err'; msg.textContent = 'Password required.'; return; }
      confirm.disabled = true;
      fetch('/api/users/' + user.id, {
        method: 'DELETE',
        credentials: 'same-origin',
        headers: { 'X-Confirm-Password': pw },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          confirm.disabled = false;
          if (data.ok) { drawer.remove(); loadUsers(); }
          else {
            msg.className = 'inline-form-msg err';
            msg.textContent = data.error || 'Delete failed';
          }
        })
        .catch(function () {
          confirm.disabled = false;
          msg.className = 'inline-form-msg err';
          msg.textContent = 'Network error. Try again.';
        });
    }
    confirm.addEventListener('click', doDelete);
    input.addEventListener('keydown', function (e) { if (e.key === 'Enter') doDelete(); });
  }

  // ---- Add user + self password + revoke sessions ----
  var addUserForm = document.getElementById('add-user-form');
  var btnAddUser = document.getElementById('btn-add-user');
  var btnCancelAddUser = document.getElementById('btn-cancel-add-user');
  var btnSubmitAddUser = document.getElementById('btn-submit-add-user');
  var createResult = document.getElementById('create-user-result');

  function toggleAddUser(open) {
    if (!addUserForm) return;
    var wantOpen = (typeof open === 'boolean') ? open : addUserForm.hidden;
    addUserForm.hidden = !wantOpen;
    if (wantOpen) { document.getElementById('new-username').focus(); }
    else {
      ['new-username', 'new-user-password'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
      });
      if (createResult) { createResult.textContent = ''; createResult.className = 'inline-form-msg'; }
    }
  }
  if (btnAddUser) btnAddUser.addEventListener('click', function () { toggleAddUser(); });
  if (btnCancelAddUser) btnCancelAddUser.addEventListener('click', function () { toggleAddUser(false); });
  if (btnSubmitAddUser) btnSubmitAddUser.addEventListener('click', function () {
    var username = document.getElementById('new-username').value.trim();
    var password = document.getElementById('new-user-password').value;
    if (username.length < 3) { createResult.className = 'inline-form-msg err'; createResult.textContent = 'Username must be at least 3 characters.'; return; }
    if (password.length < 12) { createResult.className = 'inline-form-msg err'; createResult.textContent = 'Password must be at least 12 characters.'; return; }
    btnSubmitAddUser.disabled = true;
    fetch('/api/users', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username, password: password }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        btnSubmitAddUser.disabled = false;
        if (data.ok) {
          createResult.className = 'inline-form-msg ok';
          createResult.textContent = 'User "' + username + '" created.';
          setTimeout(function () { toggleAddUser(false); }, 700);
          loadUsers();
        } else {
          createResult.className = 'inline-form-msg err';
          createResult.textContent = data.error || 'Failed';
          if (data.issues && data.issues.length) {
            var ul = document.createElement('ul');
            data.issues.forEach(function (it) {
              var li = document.createElement('li'); li.textContent = it; ul.appendChild(li);
            });
            createResult.appendChild(ul);
          }
        }
      });
  });

  var pwForm = document.getElementById('self-password-form');
  var btnChangePw = document.getElementById('btn-change-password');
  var btnCancelPw = document.getElementById('btn-cancel-password');
  var btnSubmitPw = document.getElementById('btn-submit-password');
  var pwResult = document.getElementById('password-result');

  function togglePwForm(open) {
    if (!pwForm) return;
    var wantOpen = (typeof open === 'boolean') ? open : pwForm.hidden;
    pwForm.hidden = !wantOpen;
    if (wantOpen) { document.getElementById('self-old-password').focus(); }
    else {
      ['self-old-password', 'self-new-password', 'self-confirm-password'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el) { el.value = ''; el.dispatchEvent(new Event('input')); }
      });
      if (pwResult) { pwResult.textContent = ''; pwResult.className = 'inline-form-msg'; }
    }
  }
  if (btnChangePw) btnChangePw.addEventListener('click', function () { togglePwForm(); });
  if (btnCancelPw) btnCancelPw.addEventListener('click', function () { togglePwForm(false); });
  if (btnSubmitPw) btnSubmitPw.addEventListener('click', function () {
    var oldPw = document.getElementById('self-old-password').value;
    var newPw = document.getElementById('self-new-password').value;
    var conf  = document.getElementById('self-confirm-password').value;
    if (!oldPw) { pwResult.className = 'inline-form-msg err'; pwResult.textContent = 'Enter your current password.'; return; }
    if (newPw.length < 12) { pwResult.className = 'inline-form-msg err'; pwResult.textContent = 'New password must be at least 12 characters.'; return; }
    if (newPw !== conf) { pwResult.className = 'inline-form-msg err'; pwResult.textContent = "Passwords don\u2019t match."; return; }
    btnSubmitPw.disabled = true;
    fetch('/api/users/change-password', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        btnSubmitPw.disabled = false;
        if (data.ok) {
          pwResult.className = 'inline-form-msg ok';
          pwResult.textContent = 'Password updated.';
          setTimeout(function () { togglePwForm(false); }, 900);
        } else {
          pwResult.className = 'inline-form-msg err';
          pwResult.textContent = data.error || 'Failed';
        }
      });
  });

  var btnRevokeOthers = document.getElementById('btn-revoke-others');
  if (btnRevokeOthers) btnRevokeOthers.addEventListener('click', function () {
    var orig = btnRevokeOthers.textContent;
    btnRevokeOthers.disabled = true;
    btnRevokeOthers.textContent = 'Signing out…';
    fetch('/api/users/sessions/revoke-others', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        btnRevokeOthers.disabled = false;
        btnRevokeOthers.textContent = data && data.ok
          ? 'Signed out ' + (data.revoked || 0)
          : orig;
        setTimeout(function () { btnRevokeOthers.textContent = orig; }, 2400);
      })
      .catch(function () { btnRevokeOthers.disabled = false; btnRevokeOthers.textContent = orig; });
  });

  // ---------------------------------------------------------------------
  // Password strength meters.
  // ---------------------------------------------------------------------
  function uniqueCount(s) { return new Set(s).size; }
  function classCount(s) {
    var n = 0;
    if (/[a-z]/.test(s)) n++;
    if (/[A-Z]/.test(s)) n++;
    if (/\d/.test(s)) n++;
    if (/[^A-Za-z0-9\s]/.test(s)) n++;
    return n;
  }
  function score(pw) {
    if (!pw) return { pct: 0, tone: '', label: '' };
    var len = pw.length, uniq = uniqueCount(pw), cls = classCount(pw);
    var passphrase = len >= 20 && uniq >= 12;
    var s = 0;
    if (len >= 12) s++;
    if (passphrase || cls >= 3) s++;
    if (uniq >= 6) s++;
    if (len >= 16) s++;
    if (len >= 20 || cls >= 4) s++;
    var tone = 'weak', label = 'Too weak';
    if (s >= 5)      { tone = 'strong'; label = 'Strong'; }
    else if (s >= 3) { tone = 'ok';     label = 'Getting there'; }
    return { pct: Math.min(100, (s / 5) * 100), tone: tone, label: label };
  }
  function wireStrength(inputId) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var key = input.getAttribute('data-strength');
    if (!key) return;
    var meter = document.getElementById(key + '-meter');
    var label = document.getElementById(key + '-label');
    function render() {
      var r = score(input.value);
      if (!input.value) { meter.style.width = '0%'; meter.className = 'fpc-meter-fill'; label.textContent = '\u00a0'; label.className = 'fpc-caption'; return; }
      meter.style.width = r.pct + '%';
      meter.className = 'fpc-meter-fill ' + r.tone;
      label.textContent = r.label;
      label.className = 'fpc-caption fpc-caption-' + r.tone;
    }
    input.addEventListener('input', render);
  }
  wireStrength('self-new-password');
  wireStrength('new-user-password');

  (function () {
    var pw = document.getElementById('self-new-password');
    var cp = document.getElementById('self-confirm-password');
    var lab = document.getElementById('self-match-label');
    if (!pw || !cp || !lab) return;
    function render() {
      if (!cp.value || !pw.value) { lab.textContent = '\u00a0'; lab.className = 'fpc-caption'; return; }
      if (cp.value === pw.value) { lab.textContent = 'Passwords match'; lab.className = 'fpc-caption fpc-caption-strong'; }
      else                       { lab.textContent = "Passwords don\u2019t match yet"; lab.className = 'fpc-caption fpc-caption-weak'; }
    }
    cp.addEventListener('input', render);
    pw.addEventListener('input', render);
  })();

  // ---------------------------------------------------------------------
  // Overview hero — schedule summary + storage placeholder.
  // `refreshOverviewHero` is called on initial load AND after each save,
  // so the Overview card never shows stale schedule info.
  // ---------------------------------------------------------------------
  function refreshOverviewHero() {
    var big = document.getElementById('ov-scan-big');
    var when = document.getElementById('ov-scan-when');
    if (!big || !when) return;
    var dayEl = document.getElementById('scan_day');
    var timeEl = document.getElementById('scan_time');
    var tzEl = document.getElementById('scan_timezone');
    var days = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' };
    var dayKey = dayEl ? dayEl.value : BOOT.scan_day;
    var day = days[dayKey] || 'Monday';
    var time = (timeEl && timeEl.value) || BOOT.scan_time || '09:00';
    var tz = (tzEl && tzEl.value) || BOOT.scan_timezone || 'UTC';
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
  refreshOverviewHero();

  (function () {
    var bar = document.getElementById('ov-storage-bar');
    var chips = document.getElementById('ov-storage-chips');
    var big = document.getElementById('ov-storage-big');
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

    var entries = Object.keys(DISK_THRESHOLDS || {});
    if (!entries.length) { renderUnconfigured(); return; }

    fetch('/api/dashboard/stats', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.storage) return;
        renderStorage(data.storage);
      })
      .catch(function () { /* leave placeholder — network/backend hiccup */ });
  })();

  // ---------------------------------------------------------------------
  // Boot.
  // ---------------------------------------------------------------------
  loadSubscribers();
  loadUsers();
  loadPlexLibraries();
  autoTest();
})();
