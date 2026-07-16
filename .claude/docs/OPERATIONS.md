<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Operations

<!-- One concern: running mediaman day to day — the container, the scheduler
jobs, the scan lifecycle, job-run heartbeats, the audit log, backoff/throttling,
disk usage, and how the app degrades when a dependency is down. Image build,
compose, and CI live in DEPLOYMENT.md; env-var/settings validation lives in
CONFIGURATION.md; auth/crypto/rate-limit facts live in SECURITY.md — this file
cross-references rather than repeating them. -->

## Facts — container & process

| Item | Value | Source |
|------|-------|--------|
| Entrypoint | `mediaman` console script → `mediaman.main:cli_main`; also dispatches a `create-user` subcommand before the single-worker check | `pyproject.toml` (`[project.scripts]`), `src/mediaman/main.py` (`cli_main`) |
| Admin user creation | `mediaman create-user` (subcommand) or the separate `mediaman-create-user` console script — both resolve to the same `create_user_cli` | `pyproject.toml` (`mediaman-create-user = "mediaman.web.auth.cli:create_user_cli"`), `src/mediaman/main.py`, `src/mediaman/web/auth/cli.py` (`create_user_cli`) |
| Process model | Single worker, always — `enforce_single_worker()` raises if `MEDIAMAN_WORKERS`/`UVICORN_WORKERS`/`WORKERS` > 1; APScheduler, rate limiters, arr throttle state, and client caches all assume one process | `src/mediaman/bootstrap/validators.py` (`enforce_single_worker`) |
| Startup order | `load_config` → `install_root_filter` → `bootstrap_db` → `bootstrap_crypto` → `bootstrap_scheduling` → startup reconciliation (delete intents, stranded download notifications); shutdown reverses it | `src/mediaman/app_factory.py` (`lifespan`) |
| Liveness probe | `GET /healthz` — no DB/Plex round-trip, only proves the ASGI loop is responsive | `src/mediaman/app_factory.py` (`_register_probes`, `healthz`) |
| Readiness probe | `GET /readyz` — 200 only when `scheduler_healthy AND canary_ok`; unauthenticated, body never discloses the failure reason (logged instead) | `src/mediaman/app_factory.py` (`_register_probes`, `readyz`) |
| Container healthcheck | Docker `HEALTHCHECK` polls `/healthz` — see DEPLOYMENT.md for the exact interval/timeout | `Dockerfile` (`HEALTHCHECK`) |
| Data on disk | `MEDIAMAN_DATA_DIR` (default `/data`): the SQLite DB (WAL mode → up to 3 files) and `poster_cache/`, pre-created at boot | `src/mediaman/bootstrap/db.py` (`bootstrap_db`) |
| Shutdown | SIGTERM → `lifespan` shutdown → `shutdown_scheduling()` stops APScheduler on a worker thread and joins for at most 30s so an in-flight job isn't abandoned mid-write; DB connections opened by scheduler jobs are closed explicitly to avoid FD leaks | `src/mediaman/bootstrap/scan_jobs.py` (`shutdown_scheduling`, `_SHUTDOWN_TIMEOUT_SECONDS`), `src/mediaman/scanner/scheduler.py` (`stop_scheduler`, `_close_tracked_connections`) |

## Facts — scheduler jobs

All jobs run in-process on a `BackgroundScheduler` with no persistent job store — a restart re-registers every job from scratch. Every job is registered with `max_instances=1`, `coalesce=True`, and `misfire_grace_time=3600s`: an outage longer than an hour drops the missed fire instead of stacking catch-up work; a routine restart still fires once the process returns.

| Job id | Trigger | Cadence | Callback | What it does | Source |
|--------|---------|---------|----------|---------------|--------|
| `weekly_scan` | `CronTrigger(day_of_week, hour, minute, timezone)` — settings-driven | Weekly, default `mon 09:00 UTC` | `_run_scheduled_scan` | Full scan → deletion evaluation → deletions → recommendations refresh → newsletter | `src/mediaman/scanner/scheduler.py` (`_register_scan_jobs`), `src/mediaman/bootstrap/scan_jobs.py` (`_run_scheduled_scan`) |
| `library_sync` | `IntervalTrigger(minutes=…)` — only registered when `sync_interval_minutes > 0` | `library_sync_interval` setting, default 30 min | `_run_library_sync_job` → `run_library_sync` | Lightweight Plex upsert + orphan-prune + download-notification check; no deletion evaluation | `src/mediaman/scanner/scheduler.py` (`_register_scan_jobs`), `src/mediaman/scanner/runner.py` (`run_library_sync`) |
| `cleanup_recent_downloads` | `IntervalTrigger(hours=6)` | 6h | `cleanup_recent_downloads` | 7-day TTL reaper on `recent_downloads` | `src/mediaman/scanner/scheduler.py` (`_register_maintenance_jobs`), `src/mediaman/services/arr/completion/_sync.py` (`cleanup_recent_downloads`) |
| `trigger_pending_searches` | `IntervalTrigger(hours=1)` | 1h | `trigger_pending_searches` | Sweep of stuck *arr* searches — throttled re-search plus auto-abandon; wraps each item in try/except so one bad row can't abort the sweep | `src/mediaman/scanner/scheduler.py` (`_register_maintenance_jobs`), `src/mediaman/services/arr/search_trigger.py` (`trigger_pending_searches`) |
| `reconcile_stranded_throttle` | `IntervalTrigger(hours=24)` | 24h | `reconcile_stranded_throttle` | Reaps `arr_search_throttle` rows whose media item was deleted (ghost rows don't otherwise get cleaned up) | `src/mediaman/scanner/scheduler.py` (`_register_maintenance_jobs`), `src/mediaman/services/arr/_throttle_persistence.py` (`reconcile_stranded_throttle`) |

Manual triggers (operator-initiated, bypass the schedule; every one is admin-gated via `get_current_admin` and separately rate-limited so a leaked session cookie can't chain scans):

| Endpoint | Rate limit | Effect | Source |
|----------|-----------|--------|--------|
| `POST /api/scan/trigger` | `SCAN_TRIGGER_LIMITER` — 3/min, 20/day per actor | Runs the full scan now, in a background thread, with `skip_disk_check=True` (bypasses the `disk_thresholds` library filter — the admin explicitly asked); returns `{"status": "already_running"}` if a scan lease is already held; audited via `security_event(scan.triggered)` | `src/mediaman/web/routes/scan.py` (`trigger_scan`) |
| `GET /api/scan/status` | none | `{"running": is_scan_running(conn)}` | `src/mediaman/web/routes/scan.py` (`scan_status`) |
| `POST /api/scan/clear-scheduled` | in-module `_CLEAR_SCHEDULED_LIMITER` — 3/min, 20/day | Deletes every pending `scheduled_deletion` row, audited in the same transaction | `src/mediaman/web/routes/scan.py` (`clear_scheduled`) |
| `POST /api/library/sync` | in-module `_LIBRARY_SYNC_LIMITER` — 3/min, 20/day | Runs `run_library_sync` synchronously in the request thread, audited via `security_event(library.sync)` | `src/mediaman/web/routes/scan.py` (`api_library_sync`) |
| `POST /api/recommended/refresh` | cooldown-gated, not rate-limited — `RECOMMENDATION_REFRESH_COOLDOWN_HOURS=24` | Background recommendation refresh; DB-lease gated the same way as a scan (`start_refresh_run`/`is_refresh_running`) | `src/mediaman/web/routes/recommended/refresh.py`, `src/mediaman/services/openai/recommendations/throttle.py` |

## Facts — scan lifecycle

| Stage | What happens | Source |
|-------|--------------|--------|
| 1. Per-library scan | `_scan_all_libraries`: fetch → evaluate → upsert → schedule, one Plex-then-DB pass per library, committed after each library (a SIGKILL mid-scan rolls back only the in-flight library) | `src/mediaman/scanner/engine.py` (`ScanEngine.run_scan`, `_scan_all_libraries`) |
| 2. Orphan cleanup | `_cleanup_orphans_per_library` — per library, skipped entirely in `dry_run` | `src/mediaman/scanner/engine.py` (`_cleanup_orphans_per_library`) |
| 3. Deletions | `_record_deletion_outcome` → `DeletionExecutor.execute()` — two-phase on-disk delete for every pending row whose grace period has elapsed | `src/mediaman/scanner/engine.py` (`_record_deletion_outcome`), `src/mediaman/scanner/deletions.py` (`DeletionExecutor`) |
| 4. Follow-ups | `_run_post_scan_followups` — recommendations refresh runs **before** the newsletter so the digest reflects this week's picks; both wrapped in narrow except clauses so either failing never aborts the scan summary; skipped entirely in `dry_run` | `src/mediaman/scanner/engine.py` (`_run_post_scan_followups`) |
| `dry_run` (library-sync path) | Upserts still run (catalogue stays current) but no scheduling, no deletion-state writes, no on-disk `rm`, no newsletter, no recommendations refresh | `src/mediaman/scanner/engine.py` (`ScanEngine.__init__` docstring) |
| Disk-threshold library filtering | Scheduled scan only: `disk_thresholds` setting (`{lib_id: {"path", "threshold"}}`) filters a library out of the scan unless its disk usage is at/above the configured `threshold` percent; fails open on any parse error, missing threshold, or `OSError` from `shutil.disk_usage` — a broken disk check never blocks scanning | `src/mediaman/scanner/runner.py` (`_filter_libraries_by_disk`) |
| Manual scan bypasses the disk filter | `POST /api/scan/trigger` calls `run_scan_from_db(..., skip_disk_check=True)` | `src/mediaman/scanner/runner.py` (`run_scan_from_db`), `src/mediaman/web/routes/scan.py` (`trigger_scan`) |
| Two-phase on-disk delete | Row marked `'deleting'` and committed **before** the `rm`; `recover_stuck_deletions` reconciles any row still `'deleting'` at the next boot | `src/mediaman/scanner/deletions.py` (`DeletionExecutor`, `recover_stuck_deletions`) — full semantics in [GLOSSARY](GLOSSARY.md) |
| Stuck-deletion recovery at boot | Best-effort; a recurring failure escalates the log to CRITICAL after the first repeat so deletions leaking in the `'deleting'` state can't go unnoticed | `src/mediaman/bootstrap/scan_jobs.py` (`_recover_stuck_deletions_at_boot`, `_stuck_deletion_failures`) |

## Facts — job-run heartbeats

A heartbeat lease keeps two independent kinds of long-running job from double-firing across a restart, a manual trigger, or a slow round-trip.

| Item | Value | Source |
|------|-------|--------|
| Lease tables | `scan_runs`, `refresh_runs` — `id, started_at, finished_at, status, error, owner_id, heartbeat_at` | `src/mediaman/db/schema_definition.py` (`scan_runs`, `refresh_runs`), `src/mediaman/db/connection.py` (`_JOB_RUN_TABLES`) |
| Heartbeat interval | 60s — a dedicated thread with its own DB connection renews `heartbeat_at` on this cadence while the job runs | `src/mediaman/db/connection.py` (`_JOB_HEARTBEAT_INTERVAL_SECONDS`) |
| Stale threshold | 300s (5 min) — a row with `finished_at IS NULL` and a `heartbeat_at` older than this no longer blocks a new run (asserted `<` the interval at import time) | `src/mediaman/db/connection.py` (`_JOB_HEARTBEAT_STALE_SECONDS`) |
| Start semantics | `_start_job_run` opens `BEGIN IMMEDIATE` directly (reserved write lock up front) and returns `None` if a live-heartbeat row already exists — the caller must not already hold a transaction | `src/mediaman/db/connection.py` (`_start_job_run`) |
| `owner_id` | `hostname:pid` — informational only, never compared when deciding liveness (the heartbeat alone is the unforgeable signal) | `src/mediaman/db/connection.py` (`_get_job_owner_id`) |
| Scan: scheduled path | `_run_scheduled_scan` starts its own heartbeat thread (opens `open_thread_connection`, separate from the scan's own connection so it never contends for the write lock) | `src/mediaman/bootstrap/scan_jobs.py` (`_run_scheduled_scan`) |
| Scan: manual-trigger path | `POST /api/scan/trigger` runs its own independent heartbeat thread (`manual-scan-heartbeat`) alongside the scan worker | `src/mediaman/web/routes/scan.py` (`trigger_scan`, `_heartbeat_loop`) |
| Refresh: manual path | `POST /api/recommended/refresh` mirrors the same pattern — `_heartbeat_lease` on a 60s cadence, `is_refresh_running` as the DB-lease truth, an in-process `_refresh_thread_alive()` check as a same-process fallback | `src/mediaman/web/routes/recommended/refresh.py` (`_heartbeat_lease`, `_HEARTBEAT_INTERVAL_SECONDS`) |
| Public API | `is_scan_running`/`start_scan_run`/`finish_scan_run`/`heartbeat_scan_run` and the mirrored `*_refresh_run` set | `src/mediaman/db/connection.py` |

## Facts — audit log

The security/operational fact sheet lives in [SECURITY.md](SECURITY.md) (§"Audit log") — this table is the operational summary: what an operator can see and where.

| Item | Value | Source |
|------|-------|--------|
| Storage | `audit_log` table, append-only — `BEFORE UPDATE`/`BEFORE DELETE` triggers `RAISE(ABORT)`; INSERT is unrestricted | `src/mediaman/db/schema_definition.py` (`audit_log_no_update`, `audit_log_no_delete`) |
| Writers | `log_audit` (media actions); `security_event` (best-effort, self-commits — for events outside a wider transaction); `security_event_or_raise` (fail-closed, caller owns the transaction) | `src/mediaman/core/audit.py` |
| Operator page | `GET /history` — paginated, filterable by `ACTION_TYPES` (`scanned`, `scheduled`, `snoozed`, `kept`, `deleted`, `downloaded`, `re_downloaded`, …); default 25/page | `src/mediaman/web/routes/history.py` (`history_page`, `ACTION_TYPES`, `_PER_PAGE_DEFAULT`) |
| Operator API | `GET /api/history` (same filters, `per_page` capped at 100) and `GET /api/security-events` (the `sec:*` rows only) — both admin-gated | `src/mediaman/web/routes/history.py` (`api_history`, `api_security_events`, `_PER_PAGE_MAX`) |
| Scan-adjacent audit events | `scan.triggered` (manual trigger), `library.sync` / `library.sync.failed` (manual sync), plus every scheduled action written during a scan | `src/mediaman/web/routes/scan.py` |

## Facts — backoff & throttling

Per-actor / per-IP rate limiters (login, settings writes, subscriber writes, scan trigger, poster proxy, newsletter send, force-password-change) are fully catalogued in [SECURITY.md](SECURITY.md) (§"Rate limiting") — not repeated here. This table covers the retry/backoff mechanisms that pace **outbound** calls and background retries.

| Mechanism | Shape | Source |
|-----------|-------|--------|
| `SafeHTTPClient` retry | GET retries 429/5xx by default; POST/PUT/DELETE never retry unless `retry=True`; `Retry-After` respected (delta + HTTP-date, capped 60s); early-abort after consecutive 5xx | `src/mediaman/services/infra/http/retry.py` (`dispatch_loop`) |
| Mailgun send | Retries via `SafeHTTPClient` with a 500-also-retryable override, aborts after 2 consecutive 5xx | `src/mediaman/services/mail/mailgun.py` (`_RETRYABLE_POST_STATUSES`, `_CONSECUTIVE_5XX_ABORT`) |
| *arr per-item search backoff | `ExponentialBackoff(base=120s, max=86400s, jitter=0.1)` — deterministic jitter (blake2b-seeded, not `random`) so the gate is stable across repeated `/api/downloads` polls | `src/mediaman/services/arr/_throttle_state.py` (`_SEARCH_BACKOFF`, `_SEARCH_BACKOFF_BASE_SECONDS`, `_SEARCH_BACKOFF_MAX_SECONDS`, `_SEARCH_BACKOFF_JITTER`) |
| *arr per-instance fan-out cap | A **different** `dl_id` on the same Arr instance within 15 min (`_PER_ARR_THROTTLE_SECONDS`) is blocked; the same `dl_id` keeps advancing on its own per-item backoff | `src/mediaman/services/arr/search_trigger.py` (`_PER_ARR_THROTTLE_SECONDS`) |
| *arr throttle persistence | `arr_search_throttle` table backs the in-memory state across restarts; ghost rows (media item deleted) reaped daily by `reconcile_stranded_throttle` | `src/mediaman/db/schema_definition.py` (`arr_search_throttle`), `src/mediaman/services/arr/_throttle_persistence.py` |
| Download-notification arr-failure backoff | `ExponentialBackoff(base=60s, max=1800s)`, no jitter — genuine arr unreachability | `src/mediaman/services/downloads/_notification_backoff.py` (`_NOTIFY_BACKOFF`, `_BACKOFF_BASE_SECONDS`, `_BACKOFF_MAX_SECONDS`) |
| Download-notification poll throttle | Fixed 60s, does **not** accumulate — kept separate from arr-failure backoff so a movie that legitimately takes hours to download never accrues exponential delay | `src/mediaman/services/downloads/_notification_backoff.py` (`_POLL_INTERVAL_SECONDS`) |
| Stranded-notification claim grace | 3600s — a `notified=2` (claimed) row older than this is reclaimed by the startup sweep | `src/mediaman/services/downloads/_notification_claims.py` (`STRANDED_CLAIM_GRACE_SECONDS`) |
| Recommendation manual-refresh cooldown | 24h, keyed on the `last_manual_recommendation_refresh` setting | `src/mediaman/services/openai/recommendations/throttle.py` (`RECOMMENDATION_REFRESH_COOLDOWN_HOURS`) |
| Settings-page connection-test cache | 120s TTL per service, so a settings-page reload doesn't blow the tester rate limit; each tester call bounded to 15s | `src/mediaman/web/routes/settings/testers.py` (`TEST_CACHE_TTL_SECONDS`, `TESTER_TIMEOUT_SECONDS`) |
| Poster cache eviction | Soft cap 500 MiB on `<data_dir>/poster_cache/`; opportunistic LRU sweep (oldest-mtime first) back down to 90% of the cap, throttled to run at most once per 50 cache **writes** (cache hits don't count) | `src/mediaman/web/routes/poster/cache.py` (`_CACHE_DIR_MAX_BYTES`, `_CACHE_GC_RECHECK_EVERY`, `maybe_sweep_cache`) |

## Facts — disk usage

| Item | Value | Source |
|------|-------|--------|
| Aggregate usage (dashboard) | `get_aggregate_disk_usage` de-dups mounted disks by the `(total, used, free)` byte tuple — not `st_dev` — for correctness under container bind mounts | `src/mediaman/services/infra/storage/disk_usage.py` (`get_aggregate_disk_usage`, `get_disk_usage_for_paths`) |
| Directory usage | `get_directory_size` — `os.walk(followlinks=False)`, `lstat`, regular files only | `src/mediaman/services/infra/storage/disk_usage.py` (`get_directory_size`) |
| Operator API | `GET /api/settings/disk-usage?path=…` — path must resolve under `disk_usage_allowed_roots()` (the same allowlist as deletion, driven by `MEDIAMAN_DELETE_ROOTS` / the DB `delete_allowed_roots` row); read-only, admin-gated | `src/mediaman/web/routes/settings/api.py` (`api_disk_usage`) |
| Per-library scan gate | `disk_thresholds` setting drives `_filter_libraries_by_disk` (see Scan lifecycle above) — the only place disk usage changes scan *behaviour*, not just display | `src/mediaman/scanner/runner.py` (`_filter_libraries_by_disk`) |
| Setting validation | `disk_thresholds` shape/bounds validated in `web/models/settings.py` — see [CONFIGURATION.md](CONFIGURATION.md) for the full validator table | `src/mediaman/web/models/settings.py` (`validate_disk_thresholds`) |
| Newsletter storage summary | Reclaimed week/month/total totals computed from the same `get_aggregate_disk_usage` primitives | `src/mediaman/services/mail/newsletter/summary.py` (`_load_storage_stats`) |

## Procedures

1. **Create the first admin user**: `docker compose exec -it mediaman mediaman-create-user` (interactive), or non-interactively `mediaman-create-user --username admin --password-stdin`.
2. **Check liveness/readiness**: `curl localhost:8282/healthz` (process alive) and `curl localhost:8282/readyz` (scheduler + crypto canary both healthy; 503 body carries no detail — read container logs for the reason).
3. **Trigger a scan out of schedule**: `POST /api/scan/trigger` as an authenticated admin (3/min, 20/day per actor); poll `GET /api/scan/status` for completion.
4. **Trigger a lightweight sync**: `POST /api/library/sync` — runs synchronously in the request, no deletion evaluation.
5. **Inspect the audit trail**: `GET /history` (UI, paginated + filterable) or `GET /api/history` / `GET /api/security-events` (JSON, admin-gated).
6. **Check a mount's disk usage**: `GET /api/settings/disk-usage?path=<allowlisted path>`.
7. **Back up the database**: see README.md's Operations section (`sqlite3 …/mediaman.db ".backup …"` for a live, WAL-consistent snapshot).
8. **Upgrade**: `docker compose pull && docker compose up -d` — schema is applied idempotently on boot; delete-intent and stranded-notification reconciliation run automatically and are both idempotent (see DEPLOYMENT.md).

## Failure modes

| Symptom | Cause | Fix / behaviour |
|---------|-------|------------------|
| `/readyz` returns 503, web UI still reachable | AES canary mismatch or the scheduler failed to start (`SchedulerStartupRefused`) | Fix `MEDIAMAN_SECRET_KEY` and restart, or investigate via container logs (`bootstrap_scheduling`); the web UI stays up on purpose so an admin can log in — see SECURITY.md |
| A whole weekly scan silently never happened | Process was down/paused for >60 min (`misfire_grace_time=3600s`) — APScheduler drops a fire that late rather than stacking catch-up work | Expected; the next scheduled cadence fires normally. Trigger `POST /api/scan/trigger` to run one immediately |
| `POST /api/scan/trigger` returns `{"status": "already_running"}` | A scan (scheduled or manual) already holds a live `scan_runs` lease (heartbeat within the last 5 min) | Wait, or check `GET /api/scan/status`; if the lease looks stuck check the heartbeat thread/logs — a crashed run's stale heartbeat clears itself after 5 min |
| Scheduled scan silently skips a library | `disk_thresholds` setting has that library below its configured threshold percent | Expected (fail-open on the reverse — a broken disk check always scans). Use `POST /api/scan/trigger` (`skip_disk_check=True`) to force it regardless of threshold |
| Recommendations page (`GET /recommended`) renders without Radarr or Sonarr badges | `attach_download_states` degrades each service's cache independently on `SafeHTTPError`/`ArrError` (transport/domain failure) — logs a WARNING and returns an empty cache for that service only; a down Sonarr never hides Radarr state and vice versa; programming errors still propagate | Expected degrade — check the Arr service's own health; the page never 500s on this path |
| Downloads page (`GET /downloads`) shows an incomplete or empty queue with no on-page error | `fetch_arr_queue` (the wrapper `build_downloads_response` actually calls) drops the per-service error list that `fetch_arr_queue_result` collects — the failure is only a WARNING in the logs, not a user-visible banner | Check container logs for `Failed to fetch Radarr/Sonarr queue`; NZBGet failures on the same page are likewise logged and swallowed, not surfaced |
| NZBGet queue missing from the downloads page | `nzb_client.get_status()`/`get_queue()` raised `SafeHTTPError`/`RequestException`/`NzbgetError` | Logged WARNING, page renders with an empty NZBGet queue; check the Settings page's NZBGet connection test |
| Newsletter not sent this scan | All four Mailgun settings unset → silent DEBUG skip by design; a partial subset set → `NewsletterConfigError` (admin must fix; no auto-retry); a per-subscriber send failure is logged and swallowed | Check Settings → Mailgun connection test; see SECURITY.md/services-mail.md for the full config-error contract |
| Settings-page connection test looks stale right after fixing a credential | `TEST_CACHE` result is cached 120s per service | Any settings write that touches that service's keys invalidates its cache entry; otherwise wait out the TTL |
| Stuck-deletion recovery keeps failing at every boot | Underlying error in `recover_stuck_deletions` (e.g. a filesystem issue) repeats across restarts | `_stuck_deletion_failures` escalates the log to CRITICAL after the first repeat — deletions left in `'deleting'` accumulate until the root cause is fixed |
| Poster cache directory grows unexpectedly large | The opportunistic LRU sweep only runs on roughly 1-in-50 cache writes and only once the 500 MiB soft cap is exceeded | Expected — a low-traffic poster route can temporarily exceed the cap between sweeps; it self-corrects on the next triggered sweep |
| `arr_search_throttle` state resets after a restart | The fan-out cap (`_last_search_trigger_by_arr`) is deliberately in-process only, not persisted | Expected — the cap simply re-warms over the next 15-minute window; the per-item backoff (`arr_search_throttle` table) survives restarts independently |

## Related

- [ARCHITECTURE.md](ARCHITECTURE.md) — module map, key flows, state/data ownership, the six-layer dependency ring
- [DEPLOYMENT.md](DEPLOYMENT.md) — image build, compose hardening, CI release pipeline, backup/upgrade commands (README.md's Operations section)
- [CONFIGURATION.md](CONFIGURATION.md) — every env var and DB-backed setting, including `disk_thresholds` and the scheduler settings (`scan_day`/`scan_time`/`scan_timezone`/`library_sync_interval`)
- [SECURITY.md](SECURITY.md) — rate limiters, audit-log writers and required events, AES canary, SSRF defence
- [GLOSSARY.md](GLOSSARY.md) — scan phases, scheduled actions, two-phase delete, abandon/auto-abandon, recommendation refresh
- Modules: [scanner](modules/scanner.md), [app-entry](modules/app-entry.md), [services-arr](modules/services-arr.md), [services-downloads](modules/services-downloads.md), [services-infra](modules/services-infra.md), [services-mail](modules/services-mail.md), [platform](modules/platform.md) (job-run heartbeat helpers, `db/connection.py`)
