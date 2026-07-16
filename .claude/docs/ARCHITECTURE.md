<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../INDEX.md)

# Architecture

<!-- One concern: structure. Operational material belongs in OPERATIONS.md;
split, never grow, when a second concern appears. -->

## Module map

| Module | Responsibility | Entrypoint | Doc |
|--------|----------------|-----------|-----|
| app-entry | Env → `Config`, FastAPI app factory, ordered bootstrap lifecycle | `src/mediaman/main.py` (`cli_main`), `src/mediaman/app_factory.py` (`create_app`) | [→](modules/app-entry.md) |
| platform | Ring-0 stdlib helpers, AES-256-GCM + HMAC tokens, sole `sqlite3.connect` owner | `src/mediaman/core/`, `src/mediaman/crypto/`, `src/mediaman/db/` | [→](modules/platform.md) |
| scanner | Plex scan + deletion engine: fetch → evaluate → schedule → delete, orphan pruning | `src/mediaman/scanner/engine.py` (`ScanEngine.run_scan`) | [→](modules/scanner.md) |
| services-arr | Unified Sonarr/Radarr v3 client, queue fetch, throttled auto-search, completion detection | `src/mediaman/services/arr/base.py` (`ArrClient`) | [→](modules/services-arr.md) |
| services-downloads | NZBGet integration, download-queue merge, completion email, abandon chokepoint | `src/mediaman/services/downloads/` (4 public functions, no single entrypoint) | [→](modules/services-downloads.md) |
| services-infra | SSRF-safe outbound HTTP, settings decrypt reader, safe filesystem ops, rate limiters | `src/mediaman/services/infra/http/client/_core.py` (`SafeHTTPClient`) | [→](modules/services-infra.md) |
| services-mail | Mailgun transport + weekly newsletter assembly/render/send | `src/mediaman/services/mail/newsletter/__init__.py` (`send_newsletter`) | [→](modules/services-mail.md) |
| services-media-meta | Plex/TMDB/OMDb clients + OpenAI recommendations pipeline | `src/mediaman/services/media_meta/plex.py` (`PlexClient`), `src/mediaman/services/openai/recommendations/persist.py` (`refresh_recommendations`) | [→](modules/services-media-meta.md) |
| web-auth | Admin auth: bcrypt CRUD, sessions, DB-backed lockout, reauth tickets | `src/mediaman/web/auth/middleware.py` (`get_current_admin`) | [→](modules/web-auth.md) |
| web-data | Web-owned SQL repositories + Pydantic request-body models | `src/mediaman/web/repository/`, `src/mediaman/web/models/` | [→](modules/web-data.md) |
| web-frontend | Jinja2 templates + dependency-free vanilla-JS/CSS static bundle | `src/mediaman/web/templates/base.html` | [→](modules/web-frontend.md) |
| web-http | FastAPI route handlers, JSON envelope, session cookies | `src/mediaman/web/routes/` (17 routers, `app_factory._register_routers`) | [→](modules/web-http.md) |
| web-middleware | Security perimeter: CSRF, body-size cap, CSP/nonce headers, force-password-change | `src/mediaman/web/__init__.py` (`register_security_middleware`) | [→](modules/web-middleware.md) |

## Key flows

1. Browser request → `web/__init__.py` (`register_security_middleware`, runtime order `TrustedHost → SecurityHeaders → BodySizeLimit → Obscure405 → CSRFOrigin → ForcePasswordChange`) → `web/routes/*` handler (auth via `web/auth/middleware.py`) → `web/repository/*` or `services/*` → `db/connection.py` (`get_db`) — every JSON response goes through `web/responses.py` (`respond_ok`/`respond_err`).
2. `scanner/scheduler.py` weekly `CronTrigger` (job id `weekly_scan`) → `bootstrap/scan_jobs.py` (`_run_scheduled_scan`) → `scanner/runner.py` (`run_scan_from_db`) → `scanner/engine.py` (`ScanEngine.run_scan`: `_scan_all_libraries` → `_cleanup_orphans_per_library` → `_record_deletion_outcome` → `_run_post_scan_followups`) → `scanner/repository/*` (one commit per library) — the weekly scan.
3. `scanner/scheduler.py` `IntervalTrigger` (job id `library_sync`, default 30 min) → `scanner/runner.py` (`run_library_sync`) → `scanner/engine.py` (`ScanEngine.sync_library`, `dry_run=True`: upsert + orphan-prune only) → `services/downloads/notifications.py` (`check_download_notifications`) — the lightweight sync that also fires download-completion emails.
4. Recipient clicks a newsletter/dashboard keep link → `web/routes/keep.py` / `kept.py` / `kept_show.py` → `services/scheduled_actions` (`lookup_verified_action` → `apply_keep_snooze` / `apply_keep_forever`) → `scheduled_actions` table — cancels or defers a pending deletion; the route owns the transaction (the service helpers never commit).
5. Post-scan follow-up (or `POST /api/recommended/refresh`) → `services/openai/recommendations/persist.py` (`refresh_recommendations`) → `services/openai/client.py` (`call_openai`) → `services/media_meta/tmdb.py` / `omdb.py` (enrich) → `suggestions` table, DELETE+INSERT in one transaction (`_insert_recommendations`).
6. Post-scan follow-up (or `POST /api/newsletter/send`) → `services/mail/newsletter/__init__.py` (`send_newsletter`) → `services/mail/mailgun.py` (`MailgunClient.send`, EU/US region fallback) — the weekly digest; delivery failures are logged and swallowed so the scan never aborts on a mail failure.
7. `GET /api/downloads` poll → `services/downloads/download_queue/__init__.py` (`build_downloads_response`) → `services/arr/fetcher/` (Radarr/Sonarr queue cards) + `services/downloads/nzbget.py` (NZBGet queue) → completion diff → `services/arr/completion` (`record_verified_completions`) → `recent_downloads` table.
8. Maintenance jobs on fixed intervals (`scanner/scheduler.py` `_register_maintenance_jobs`): hourly `trigger_pending_searches`, 6-hourly `cleanup_recent_downloads`, daily `reconcile_stranded_throttle` — background upkeep for `services/arr`, independent of the scan/sync cadence.

## State & data

| State | Lives in | Written by | Read by |
|-------|----------|-----------|---------|
| `settings` (config + encrypted credentials) | SQLite `settings` table (`db/schema_definition.py`) | `web/repository/settings.py` (`write_settings`); `crypto/aes.py`/`crypto/_aes_key.py` (canary/salt rows, owned directly per CODE_GUIDELINES §2.2) | `services/infra/settings_reader.py`, every `build_*_from_db` factory (`services/arr/build.py`) |
| `media_items`, `scheduled_actions`, `kept_shows` | SQLite | `scanner/repository/*`; `web/repository/library_api.py` / `kept.py` | `scanner/engine.py`, `web/repository/dashboard.py`, `services/mail/newsletter/schedule.py` |
| `admin_users`, `admin_sessions`, `login_failures`, `reauth_tickets` | SQLite | `web/auth/*` — the only package permitted to touch these four tables | `web/auth/middleware.py` |
| `audit_log` (append-only, `BEFORE UPDATE`/`BEFORE DELETE` triggers `RAISE(ABORT)`) | SQLite | `core/audit.py` (`log_audit`, `security_event`/`security_event_or_raise`) | `web/repository/dashboard.py`, `scanner/repository/audit.py` (history page/API) |
| `suggestions` | SQLite | `services/openai/recommendations/persist.py` | `web/repository/recommended.py`, `services/mail/newsletter/summary.py` |
| `download_notifications`, `recent_downloads` | SQLite | `services/downloads/notifications.py`; `services/arr/completion/_verification.py` | `web/routes/downloads.py`, `services/mail` (completion email) |
| `subscribers`, `newsletter_deliveries` | SQLite | `web/repository/subscribers.py`; `services/mail/newsletter/subscribers.py` | `services/mail/newsletter/` |
| `arr_search_throttle` | SQLite (persisted, DDL reconciled at startup by `scanner/scheduler.py`) + in-process dicts | `services/arr/_throttle_persistence.py` | `services/arr/search_trigger.py` |
| Rate-limit buckets, arr throttle state, Plex/TMDB client caches | Process memory (module-level singletons) | `services/rate_limit/instances.py`, `services/arr/_throttle_state.py`, `scanner/runner.py` (Plex client cache), `services/media_meta/tmdb.py` (`_CLIENT_CACHE`) | Same module — discarded on restart; single-process assumption enforced by `bootstrap/validators.py` (`enforce_single_worker`) |
| Poster cache | Filesystem `<data_dir>/poster_cache/` (pre-created by `bootstrap/db.py`) | `web/routes/poster/cache.py` | `web/routes/poster/__init__.py` |

## Boundaries & invariants

- **Six-layer dependency ring, imports flow down only:** `web/` → `services/*` → `scanner/` → `db/` → `{crypto/, bootstrap/, core/}` (leaves). An upward import is a review-blocker (CODE_GUIDELINES §2, diagram). Enforcement is docstring convention + code review, **not** an automated import-linter — the sole machine-checked case is `tests/unit/services/rate_limit/test_ip_resolver_import.py`, an AST guard against one module importing `fastapi`.
- **`scanner/` and every `services/*` package must never import `mediaman.web`.** Both must run as a background job/script independent of the HTTP layer (stated in `scanner/__init__.py` and each `services/*/__init__.py` docstring). `services/arr`, `services/downloads`, and `services/media_meta` additionally forbid importing `mediaman.scanner`, since `scanner/` sits above them and consumes them, never the reverse.
- **`db/` owns the sole `sqlite3.connect` call in the production codebase** (CODE_GUIDELINES §2.4/§8.2); a bare `sqlite3.connect(...)` elsewhere is a review-blocker. `crypto/` reads/writes its own two `settings` rows (`aes_kdf_canary`, `aes_kdf_salt`) directly rather than through a `db/` repository, so `db/` never imports `crypto/` (§2.2/§2.8).
- **Every outbound HTTP call routes through `SafeHTTPClient` or `_SafePlexSession`** (`services/infra`), giving uniform SSRF re-validation, DNS-rebind pinning, redirect refusal, and a body-size cap across Plex, Sonarr/Radarr, NZBGet, Mailgun, TMDB, OMDb, and OpenAI.
- **Single-worker process.** `bootstrap/validators.py` (`enforce_single_worker`) raises `RuntimeError` if `MEDIAMAN_WORKERS`/`UVICORN_WORKERS`/`WORKERS` > 1; the in-process APScheduler, the rate-limit buckets, the arr throttle state, and the Plex/TMDB client caches all assume exactly one process holds them.
- **SQLite connection pragmas are set once**, in `db/connection.py` (`_configure_connection`): `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000` (ms), `foreign_keys=ON`. One connection per request (opened/closed by a FastAPI dependency) and one per scheduled job (opened by the runner, closed on scheduler shutdown); long-lived connections are forbidden (CODE_GUIDELINES §9.8).
- **Transactions are explicit** (§9.7): a multi-statement repository operation runs inside `with conn:`, never relying on SQLite's implicit transaction start. `db/connection.py` leaves `isolation_level` at the `sqlite3` default (legacy mode, not autocommit), so `with conn:` alone does not acquire a write lock up front — a plain `BEGIN` would only escalate to a reserved write lock on the first actual write.
- **Read-modify-write races issue `BEGIN IMMEDIATE` directly** to grab a reserved write lock before the read, closing the window a default `BEGIN DEFERRED` would leave open: job-run heartbeats (`db/connection.py` `_start_job_run`), login-lockout UPSERT (`web/auth/login_lockout.py`), atomic download-notification claims (`services/downloads/_notification_claims.py`), session/reauth writes (`web/auth/session_store/_writes.py`, `web/auth/password_hash/_change_password.py`), keep-mutation guards (`web/repository/library_api.py`), and the subscriber-insert race (`web/repository/subscribers.py` `try_add_subscriber`).
- **The schema is DDL-as-code** (`db/schema_definition.py`, one `SCHEMA` string of `CREATE ... IF NOT EXISTS` statements + indexes + the append-only `audit_log` triggers), applied idempotently by `init_db` on every boot; there is no migration runner (CODE_GUIDELINES §9.2).
- **APScheduler (`scanner/scheduler.py`, `BackgroundScheduler`) is in-process and in-memory** — no persistent job store is configured, so a restart re-registers every job from scratch. Every job carries `max_instances=1`, `coalesce=True`, and a `misfire_grace_time` of 3600s (`_DEFAULT_MISFIRE_GRACE_SECONDS`), so an outage longer than an hour drops the missed tick instead of stacking catch-up runs.
- **The scheduler refuses to start when the AES canary check failed** (`SchedulerStartupRefused`, raised in `bootstrap/scan_jobs.py` `bootstrap_scheduling` before touching APScheduler) — background jobs must never run against an unverified secret key.
