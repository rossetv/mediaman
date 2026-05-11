/**
 * settings/general.js — General settings entry point and wiring.
 *
 * Parses the server-rendered bootstrap payload, exposes `collectSettings`
 * (the JSON body for PUT /api/settings), and calls each sibling module's
 * `init()` in dependency order. The page bootstrap (`settings.js`)
 * invokes `MM.settings.general.init()` on DOMContentLoaded.
 *
 * After Phase 8B the heavy lifting lives in:
 *   - settings/savebar.js          — dirty tracking, save submit, reauth modal
 *   - settings/disk_thresholds.js  — Plex pills + per-library disk cards
 *   - settings/overview.js         — schedule summary + storage bar
 *   - settings/toggles.js          — toggle switches, collapse, reveal,
 *                                    rail scroll-spy
 *
 * Cross-module dependencies:
 *   MM.api  (core/api.js)
 *
 * Exposes:
 *   MM.settings.general.init()
 *   MM.settings.general.collectSettings()        — read by savebar
 *   MM.settings.general.BOOT                     — parsed bootstrap payload
 *   MM.settings.general.SELECTED_LIBS            — live view of selected libs
 *   MM.settings.general.markDirty()              — kept for callers reaching
 *   MM.settings.general.markClean()              into the legacy surface
 *   MM.settings.general.refreshOverviewHero()
 *   MM.settings.general.openReauthModal(opts)
 */
(function () {
  'use strict';

  window.MM = window.MM || {};
  MM.settings = MM.settings || {};

  var BOOT = {};

  function parseBootstrap() {
    try {
      var node = document.getElementById('setg-bootstrap');
      if (node && node.textContent) { return JSON.parse(node.textContent); }
    } catch (_err) { /* fall through to default */ }
    return {};
  }

  function v(id) { var el = document.getElementById(id); return el ? el.value : ''; }
  function n(id) { var x = parseInt(v(id), 10); return isFinite(x) ? x : 0; }

  function collectSettings() {
    var diskThresholds = MM.settings.diskThresholds;
    var selectedLibs   = diskThresholds ? diskThresholds.getSelectedLibs() : [];
    var diskMap        = diskThresholds ? diskThresholds.collect() : {};
    return {
      plex_url:              v('plex_url'),
      plex_public_url:       v('plex_public_url'),
      plex_token:            v('plex_token'),
      plex_libraries:        selectedLibs,
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
      disk_thresholds:       diskMap,
      suggestions_enabled:   v('suggestions_enabled'),
      auto_abandon_enabled:  v('auto_abandon_enabled'),
    };
  }

  function refreshOverviewHero() {
    if (MM.settings.overview && MM.settings.overview.refreshOverviewHero) {
      MM.settings.overview.refreshOverviewHero();
    }
  }

  function init() {
    BOOT = parseBootstrap();
    MM.settings.general.BOOT = BOOT;

    // Order matters: overview reads boot, savebar reads collectSettings, the
    // toggles/disk_thresholds modules call into savebar.markDirty.
    if (MM.settings.overview)        MM.settings.overview.init({
      boot: BOOT,
      diskThresholds: BOOT.disk_thresholds || {},
    });
    if (MM.settings.diskThresholds)  MM.settings.diskThresholds.init({
      selectedLibs: BOOT.selected_libraries || [],
      diskThresholds: BOOT.disk_thresholds || {},
    });
    if (MM.settings.toggles)         MM.settings.toggles.init();
    if (MM.settings.savebar)         MM.settings.savebar.init({
      getPayload: collectSettings,
      refreshOverviewHero: refreshOverviewHero,
    });
  }

  MM.settings.general = {
    init: init,
    collectSettings: collectSettings,
    refreshOverviewHero: refreshOverviewHero,
    BOOT: BOOT,
    // The legacy surface — callers (and our own docstring) used to mutate
    // these directly. Forward to the new modules so any external code that
    // still pokes at them keeps working.
    get SELECTED_LIBS() {
      return MM.settings.diskThresholds
        ? MM.settings.diskThresholds.getSelectedLibs()
        : [];
    },
    markDirty: function () {
      if (MM.settings.savebar) MM.settings.savebar.markDirty();
    },
    markClean: function () {
      if (MM.settings.savebar) MM.settings.savebar.markClean();
    },
    openReauthModal: function (opts) {
      if (MM.settings.savebar) MM.settings.savebar.openReauthModal(opts);
    },
  };
})();
