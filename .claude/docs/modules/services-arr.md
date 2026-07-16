<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: services-arr

## Purpose

Unified Sonarr/Radarr (*arr) v3 integration layer: one spec-driven HTTP client
(`ArrClient`) for both APIs, plus the queue-fetch, download-state, completion-detection
and throttled auto-search machinery built on top of it. Sits **below** web and scanner in
the dependency graph (both consume it; it must never import upward), and every outbound
call routes through `SafeHTTPClient` for SSRF re-validation, size caps and retry/backoff.
Entrypoint: `ArrClient` (`base.py`), constructed via `build_radarr_from_db` /
`build_sonarr_from_db` (`build.py`).

## Key files

| File | Role |
|------|------|
| `src/mediaman/services/arr/__init__.py` | Package facade; re-exports the public error surface (`ArrError`, `ArrConfigError`, `ArrKindMismatch`, `ArrUpstreamError`) from `_transport`. Docstring states allowed/forbidden deps (no imports from `mediaman.web` or `mediaman.scanner`). |
| `src/mediaman/services/arr/spec.py` | Frozen `ArrSpec` dataclass (`kind` + `exclusion_param`) and the two canonical instances `SONARR_SPEC` / `RADARR_SPEC`. Replaces per-service subclasses; single home for values that differ between Sonarr and Radarr. |
| `src/mediaman/services/arr/base.py` | `ArrClient` — the unified client composed from five mixins (`_TransportMixin`, `_LookupsMixin`, `_AddFlowMixin`, `_SonarrMixin`, `_RadarrMixin`). Holds `_require_series`/`_require_movie` kind guards and `get_queue()` (paginates with a 20-page hard cap, service-specific include params). |
| `src/mediaman/services/arr/_transport.py` | Raw HTTP layer: `_TransportMixin` with `_get`/`_put`/`_post`/`_delete` (each sets `self.last_error` and re-raises), `SafeHTTPClient` construction, the exception hierarchy (`ArrError` base), timeout/size constants (`_ARR_TIMEOUT_SECONDS`, `_ARR_MAX_RESPONSE_BYTES` = 64 MiB), and the shared read-modify-write `_unmonitor_with_retry` loop. |
| `src/mediaman/services/arr/_lookups.py` | `_LookupsMixin` — `lookup_by_tmdb_id`, `lookup_by_term` (URL-encodes term via `quote(safe='')`), `get_release` (404/network → `None`), and `is_reachable` (probes `/api/v3/system/status`). |
| `src/mediaman/services/arr/_add.py` | `_AddFlowMixin` — instance-cached `_choose_root_folder` and `_choose_quality_profile` (min-id default, logged at INFO). Both raise `ArrConfigError` rather than silently defaulting to `/tv`/`/movies` or profile id 4. |
| `src/mediaman/services/arr/_sonarr_methods.py` | `_SonarrMixin` (`kind="series"` ops): `delete_episode_files` (bulk endpoint w/ serial fallback on 404), `delete_series`, `get_series`/`get_series_by_id`/`get_episodes`/`get_episode_files`, `unmonitor_season`/`remonitor_season`, `search_series`, `get_missing_series` (paged, 100-page cap), `add_series`/`add_series_with_seasons`, `lookup_series_by_tmdb`. Each calls `_require_series` first. |
| `src/mediaman/services/arr/_radarr_methods.py` | `_RadarrMixin` (`kind="movie"` ops): `get_movies`/`get_movie_by_id`, `delete_movie`, `unmonitor_movie`/`remonitor_movie`, `search_movie`, `add_movie`, `get_movie_by_tmdb` (server-side `?tmdbId=` filter). Each calls `_require_movie` first. |
| `src/mediaman/services/arr/_types.py` | TypedDicts for the subset of *arr v3 response fields mediaman reads (`SonarrSeries`, `RadarrMovie`, `ArrQueueItem`, `ArrEpisode`, etc.). Every dict is `total=False` because *arr omits empty optional fields. |
| `src/mediaman/services/arr/build.py` | Factory helpers reading DB settings: `build_radarr_from_db` / `build_sonarr_from_db` / `build_plex_from_db` / `build_nzbget_from_db`. `_read_arr_credentials` reads `{service}_url`/`{service}_api_key` (key decrypted via `secret_key`); returns `None` when either is missing. |
| `src/mediaman/services/arr/state.py` | Download-state computation: `ACTION_*` constants, `compute_download_state` / `_compute_series_state` (in_library/partial/downloading/queued/`None`), `series_has_files`, `build_radarr_cache`/`build_sonarr_cache` (via shared `_build_arr_index`), `LazyArrClients`, `annotate_download_states`, and `attach_download_states` (recommendations annotation + stale `downloaded_at` self-heal). Over the 500-line ceiling with a documented rationale. |
| `src/mediaman/services/arr/search_trigger.py` | Throttled auto-search: `maybe_trigger_search` (3-phase reservation-token/TOCTOU lock protocol), `trigger_pending_searches` (scheduler sweep, two passes), `_trigger_sonarr_partial_missing`. Re-exports throttle state/persistence helpers as stable test-patch targets. |
| `src/mediaman/services/arr/_throttle_state.py` | Module-level in-memory throttle state (`_last_search_trigger`, `_search_count`, `_reservation_tokens`, `_last_search_trigger_by_arr`) + the `_state_lock` guarding them, the `ExponentialBackoff` config, `_search_backoff_seconds`, and `_arr_throttle_key`. Split out so state and persistence share one lock/dicts without a cycle. |
| `src/mediaman/services/arr/_throttle_persistence.py` | SQLite layer for `arr_search_throttle`: `_load_throttle_from_db`, `_save_trigger_to_db` (rate-limited `_warn_persist_failure`), `get_search_info` (in-memory then DB fallback, WAL cross-connection caveat), `reconcile_stranded_throttle` (90-day TTL reaper), `clear_throttle`, `reset_search_triggers`. |
| `src/mediaman/services/arr/auto_abandon.py` | Auto-abandon policy: `maybe_auto_abandon`, `_should_auto_abandon` (guard cascade: setting off / upcoming / release too fresh / `added_at` missing / not stalled 14 d), `_abandon_movie_with_audit` / `_abandon_series_with_audit` (emit `security_event` **before** the destructive abandon). Thresholds: 10 h button, 14 d auto, 30 d release grace. |
| `src/mediaman/services/arr/fetcher/__init__.py` | Queue-fetch facade: `fetch_arr_queue_result` (returns `FetchResult` with per-service errors) and `fetch_arr_queue` (back-compat list wrapper). Each service fetch is independently try/excepted so one down service still returns the other's cards. |
| `src/mediaman/services/arr/fetcher/_base.py` | Shared fetcher scaffolding: `make_arr_card` factory (`dl_id = source.lower()+":"+title`), `ArrCard`/`ArrEpisodeEntry`/`BaseArrCard`/`FetchResult` types, `clamp_progress`, `_format_size_fields`, and `_iter_still_searching` (shared outer try/except for the "still searching" pass). |
| `src/mediaman/services/arr/fetcher/_radarr.py` | `fetch_radarr_queue` — phase 1 one card per queue entry, phase 2 monitored movies still searching (deduped by `(title, year)`, skipping `hasFile`/unmonitored). |
| `src/mediaman/services/arr/fetcher/_sonarr.py` | `fetch_sonarr_queue` — groups queue episodes into one card per series and runs pack-detection/size aggregation (`_aggregate_pack_episodes` via three passes; cluster key = `downloadId` or NUL-joined `seriesId`+`title`+`label`). Phase 2 adds monitored series with zero `episodeFileCount`. |
| `src/mediaman/services/arr/completion/__init__.py` | Completion package facade re-exporting `detect_completed`, `fetch_and_sync_recent_downloads`, `record_verified_completions`, `cleanup_recent_downloads`, and the `CompletedItem`/`RecentDownloadItem` types. |
| `src/mediaman/services/arr/completion/_sync.py` | `detect_completed` (pure diff of two queue snapshots → `CompletedItem` list), `cleanup_recent_downloads` (7-day TTL), `_PosterLookup` (lazy title→poster map), `_sync_recent_row` (drops reappeared items, backfills posters), `fetch_and_sync_recent_downloads` (public read path). |
| `src/mediaman/services/arr/completion/_verification.py` | `record_verified_completions` + `_ArrLibraryIndex` (lazy tmdbId/title indexes), `_check_item_verified` (Radarr `hasFile` / Sonarr `series_has_files`, title-only fallback logs a WARNING), `_batch_insert_completions` (single batch + commit). Only verified completions land in `recent_downloads`. |
| `src/mediaman/services/arr/completion/_types.py` | `CompletedItem` and `RecentDownloadItem` TypedDicts, kept in their own module to break a circular import between `_sync` and `_verification`. |

## Invariants

- **Dependency direction.** The arr package sits below web and scanner and must not import from `mediaman.web` or `mediaman.scanner` (stated in the `__init__` docstring; enforced by keeping upward calls as late imports elsewhere).
- **Kind is asserted before any URL.** Every kind-specific method calls `_require_series`/`_require_movie` before issuing a request.
- **All service divergence lives in `ArrSpec`.** Endpoint spelling and `exclusion_param` differences live in `spec.py`; callers import `SONARR_SPEC`/`RADARR_SPEC` rather than building their own.
- **`_transport` verbs are the only HTTP path.** Each sets `self.last_error` (`None` on success, exception string on failure) and re-raises, so the UI can surface a banner instead of stale data. All traffic goes through `SafeHTTPClient`.
- **`_state_lock` guards all four throttle dicts;** the HTTP call and DB read are always outside the lock.
- **`_load_throttle_from_db` never returns "never fired" on error** — it returns `(0.0, 0)` "unknown", so the throttle re-warms rather than re-firing blindly.
- **`build.py` is the single source of truth for constructing clients** from DB settings (URL + decrypted API key); returns `None` when unconfigured, and callers guard on `None`.
- **Only Radarr/Sonarr-verified completions** (`hasFile` / `episodeFileCount > 0`) are written to `recent_downloads`; NZB-only items (no `radarr:`/`sonarr:` prefix) are verified-by-default.
- **Programming errors are never swallowed** (`KeyError`/`TypeError`/`AttributeError`, `sqlite3.InterfaceError`); only expected transport/domain exceptions (`SafeHTTPError`, `requests.RequestException`, `ArrError`, `sqlite3.DatabaseError`, `ConfigDecryptError`) are caught, so a real bug surfaces.
- **Caches are call- or request-scoped** (`_ArrLibraryIndex`, `_PosterLookup`, `LazyArrClients`, `RadarrCaches`/`SonarrCaches`) and fetch each service's library at most once; caches on `ArrClient` instances live on the instance (never the class) so two clients can't leak settings.
- **The sweep survives a single bad row.** `trigger_pending_searches` wraps each `maybe_auto_abandon` in a broad try/except so one malformed item can't abort the whole tick.

## Gotchas

- **One class, two services.** Calling a Sonarr method on a Radarr client (or vice versa) raises `ArrKindMismatch` via `_require_series`/`_require_movie`, not a confusing upstream 404.
- **`_unmonitor_with_retry` covers transport failures only.** Sonarr/Radarr expose no ETag/version, so a PUT clobbered by a concurrent writer is indistinguishable from success — the loop returns after the first successful PUT without re-reading. Correctness relies on the single-worker model.
- **The throttle subsystem is best-effort in-process state.** `_last_search_trigger_by_arr` is **not** persisted and resets on restart; the persist-warning counters reset on restart. The fan-out cap is a soft rate-limit, not a correctness invariant.
- **`maybe_trigger_search` is a 3-phase reservation-token protocol.** The DB read and the HTTP call both run **outside** `_state_lock` (so a slow disk/upstream can't starve sibling throttle reads); only the in-memory reserve (phase 1) and commit/rollback (phase 3) hold the lock. Rollback compares a per-attempt uuid token, **not** float-equality on the timestamp, so a sibling's fresh reservation is never silently nuked.
- **The per-arr-instance fan-out cap stores `(epoch, dl_id)`.** A **different** `dl_id` within `_PER_ARR_THROTTLE_SECONDS` (15 min) is blocked (caps fan-out + closes the title-rename bypass since a renamed item mints a fresh `dl_id`), but the **same** `dl_id` passes so it keeps advancing on its own per-item backoff.
- **`get_search_info`'s DB-fallback branch calls `mediaman.db.get_db()`** (request-local connection), a **different** SQLite connection from the scheduler thread that writes throttle rows — cross-connection visibility relies on WAL mode; the caller's transaction is not in scope there.
- **Exception policy is deliberately narrow.** DB helpers catch `sqlite3.DatabaseError` (covers `OperationalError`) but **not** `sqlite3.InterfaceError` (programmer error must propagate). A previous broad `except Exception` had silently disabled the throttle by reporting zeros for every `dl_id`.
- **"Absent == 0" for *arr integer counts.** `series_has_files` and `_season_stats` rely on it — the TypedDicts are `total=False` and *arr omits only zero-valued counts. Do **not** turn `.get(..., 0)` into strict subscripts (would `KeyError` on the legitimate zero case).
- **`_compute_series_state` requires `episodeCount > 0` in the `have_all` check on purpose.** A freshly-aired season briefly reports `episodeCount == 0`/`episodeFileCount == 0`, and `0 >= 0` would falsely flip a show to `in_library` for one poll cycle. `partial` is the intended lesser evil (pinned by a regression test).
- **`compute_download_state` returns `None` (not `"queued"`) for an unmonitored Radarr movie** — that residue of a prior abandon would otherwise render a disabled "Queued" button that wedges the user; the click path re-monitors.
- **Auto-abandon audits before the destructive abandon** (`security_event` first) so a compromised-settings attack stays discoverable even if Radarr/Sonarr is down. `actor=""` marks it system-driven.
- **Sonarr pack clustering uses NUL (`\x00`) as the synthesised cluster-key separator** — the old `":"` separator collided on titles like `Star Trek: Picard`, silently merging two rows and double-counting pack totals.
- **`dl_id` format is `source.lower()+":"+title`** (e.g. `radarr:Inception`) — completion verification, throttle keys, and the sonarr partial-missing pass all depend on this exact format across passes.
- **`get_queue` / `get_missing_series` page with hard caps** (20 and 100 pages); hitting the cap logs a WARNING because records beyond it are silently truncated/orphaned.
- **`attach_download_states` clears a stale `downloaded_at` flag only** when the relevant Arr client is configured **and** reports the item untracked — an Arr-down/unconfigured render must never wipe a freshly-optimistic flag; the DB UPDATE runs in an explicit transaction.

## Extension points

- **New per-service divergence** → add a field to `ArrSpec` (`spec.py`) and set it on `SONARR_SPEC`/`RADARR_SPEC`; never reintroduce per-service subclasses.
- **New Sonarr/Radarr operation** → add a method to `_SonarrMixin`/`_RadarrMixin`, calling `_require_series`/`_require_movie` first.
- **New *arr response field mediaman reads** → add it to the relevant `total=False` TypedDict in `_types.py`.
- **New client type built from DB settings** → add a `build_*_from_db` factory to `build.py` (the arr package hosts the shared factory for Plex/NZBGet too).
- **Throttle-state test seams** → patch the helpers re-exported from `search_trigger.py` (backed by `_throttle_state.py` / `_throttle_persistence.py`).

## Related

- Modules: [services-infra](services-infra.md) (`SafeHTTPClient`, settings, config decryption — the layer below), [services-downloads](services-downloads.md) (`download_format`, `abandon`, `nzbget` — late-imported to break cycles).
- Consumers (must not import back): `mediaman.scanner` (engine, runner, scheduler, arr_dates, deletions), `mediaman.services.downloads.*`, `mediaman.services.mail.newsletter.enrich`, many `mediaman.web.routes.*`.
- DB tables: `arr_search_throttle` (DDL in `mediaman/db/schema_definition.py`, reconciled on startup by `mediaman/scanner/scheduler.py`), `recent_downloads`.
