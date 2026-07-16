<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](INDEX.md)

# mediaman — Overview

## What & why

mediaman is a self-hosted media lifecycle manager for a Plex library: one FastAPI
process that scans configured Plex libraries on a schedule, schedules stale
(old + unwatched) movies/seasons for deletion behind a grace period with a
per-recipient HMAC "keep" link, emails a weekly newsletter, and serves an admin
web UI for browsing the library, searching Sonarr/Radarr, watching the NZBGet
queue, and reviewing OpenAI-generated recommendations.

It exists to automate the "should this ever-growing library be trimmed?"
decision instead of leaving it to manual disk-space triage: every deletion is
age/inactivity-gated, reversible during its grace period via a signed link, and
audited. All integration credentials (Plex, Sonarr, Radarr, NZBGet, Mailgun,
TMDB, OMDb, OpenAI) are configured through the web UI and stored encrypted at
rest; `MEDIAMAN_SECRET_KEY` is the only secret an operator manages directly.

## Domain concepts

| Term | Meaning |
|------|---------|
| Scan | Weekly full pass: fetch every configured Plex library, upsert `media_items`, evaluate eligibility, schedule/execute deletions, prune orphans, run post-scan follow-ups (`scanner/engine.py` `ScanEngine.run_scan`). |
| Library sync | Lightweight ~30-min pass (default `sync_interval_minutes=30`): upsert + orphan-prune only, never evaluates for deletion; also fires download-completion notifications (`scanner/engine.py` `sync_library`). |
| Scheduled action | A pending deletion, snooze, or keep-forever row in `scheduled_actions`, keyed by an HMAC keep-token stored only as its SHA-256 hash. |
| Grace period | Days between an item being scheduled for deletion and the on-disk `rm` actually running; a keep link cancels it before expiry. |
| Keep / snooze / keep-forever | Recipient actions on a newsletter link: anonymous 7/30/90-day snooze, or admin "protected forever" (`services/scheduled_actions`, `core/scheduled_action_kinds.py`). |
| Orphan | A `media_items` row whose file Plex no longer reports; pruned only after two consecutive suspicious scans of that library (fail-closed guard). |
| dry_run | Scan mode that still upserts the catalogue but skips every deletion-state write (scheduling, `rm`, newsletter, recommendation refresh); used by library sync. |
| *arr | Sonarr (TV) / Radarr (movies), unified behind one `ArrClient` (`services/arr/base.py`). |
| Auto-abandon | Automatic Sonarr/Radarr search cancellation for a stalled download past a staleness threshold (`services/arr/auto_abandon.py`). |
| Reauth ticket | Short-lived, password-reverified privilege ticket required before a sensitive settings write (`web/auth/reauth.py`). |
| Suggestion | An OpenAI-generated, TMDB/OMDb-enriched recommendation row in the `suggestions` table. |
| Canary | A stored ciphertext used at boot to prove `MEDIAMAN_SECRET_KEY` can still decrypt existing data (`crypto/aes.py` `is_canary_valid`). |
| Delete allowed roots | `MEDIAMAN_DELETE_ROOTS` / the DB settings equivalent — the mandatory allowlist of filesystem roots mediaman may delete from; deletion fails closed if unset. |

## System boundaries

| External system | Direction | Via |
|-----------------|-----------|-----|
| Plex | read | `services/media_meta/plex.py` (`PlexClient`, via hardened `_SafePlexSession`) |
| Sonarr / Radarr | read/write | `services/arr` (`ArrClient`, built by `services/arr/build.py`) |
| NZBGet | read | `services/downloads/nzbget.py` (`NzbgetClient`, JSON-RPC) |
| Mailgun | write | `services/mail/mailgun.py` (`MailgunClient`) |
| TMDB | read | `services/media_meta/tmdb.py` (`TmdbClient`) |
| OMDb | read | `services/media_meta/omdb.py` (`fetch_ratings`) |
| OpenAI | read | `services/openai/client.py` (`call_openai`) |
| SQLite (`mediaman.db`) | read/write | `db/connection.py` — sole `sqlite3.connect` owner |
| Media filesystem | read/delete | `services/infra/storage` (`delete_path`, allowlist-gated) |
| Browser / email client | inbound HTTP | FastAPI app (`app_factory.create_app`) |

Every outbound HTTP call (Plex/Arr/NZBGet/Mailgun/TMDB/OMDb/OpenAI) routes
through `SafeHTTPClient` or `_SafePlexSession` (`services/infra`), giving SSRF
re-validation, DNS-rebind pinning, redirect refusal, a size cap, and identity
encoding uniformly across every integration.

## Process model

1. Production entry: the `mediaman` console script → `main.cli_main`
   (`pyproject.toml` `[project.scripts]`) → `enforce_single_worker()` →
   `create_app()` → uvicorn, single ASGI process/worker (multi-worker raises
   `RuntimeError` at startup — `bootstrap/validators.py`).
2. `app_factory.lifespan()` runs a fixed, dependency-ordered bootstrap:
   `load_config` → `install_root_filter` → `bootstrap_db` → `bootstrap_crypto`
   → `bootstrap_scheduling` → `_run_startup_reconciliation`; shutdown reverses
   it. DB/crypto failures are fatal (`sys.exit(1)`); scheduler failure is not
   (`/readyz` reports it, the web UI stays up).
3. An in-process APScheduler (`scanner/scheduler.py` `start_scheduler`) drives
   all background work: weekly scan (`ScanEngine.run_scan`), ~30-min library
   sync (`sync_library`), and maintenance jobs (`cleanup_recent_downloads`,
   `trigger_pending_searches`, `reconcile_stranded_throttle`).
4. The uvicorn event loop serves FastAPI request handlers (17 routers,
   `app_factory._register_routers`) for the web UI and JSON API; handlers
   parse/validate, enforce auth (`web/auth`) + rate limits
   (`services/rate_limit`), then delegate to `services/*` and
   `web/repository/*`.
5. `mediaman-create-user` (`web/auth/cli.py`) is a separate CLI entrypoint that
   short-circuits `cli_main` before the single-worker check or server startup,
   for bootstrapping the first admin.

## Repo layout

```
src/mediaman/
├── main.py, app_factory.py, config.py   # CLI/uvicorn entry, FastAPI factory, bootstrap-only Config
├── bootstrap/                            # ordered startup steps: db, crypto, scan_jobs, validators
├── core/                                 # Ring 0: pure stdlib helpers (time, format, audit, backoff, scrub, email)
├── crypto/                                # AES-256-GCM at-rest encryption + HMAC-signed tokens
├── db/                                    # sole sqlite3.connect owner; schema_definition.py = the one DDL
├── scanner/                               # Plex scan + deletion engine, repository, deletions, scheduler
├── services/
│   ├── arr/                              # unified Sonarr/Radarr client, queue-fetch, throttle, completion
│   ├── downloads/                        # NZBGet client, download-queue merge, completion email, abandon
│   ├── infra/                            # SSRF-safe HTTP, settings reader, safe filesystem ops
│   ├── rate_limit/                       # IP-bucketed + per-actor sliding-window rate limiters
│   ├── mail/                             # Mailgun transport + newsletter assembly/render/send
│   ├── media_meta/                       # Plex/TMDB/OMDb clients + item enrichment
│   ├── openai/                           # LLM call wrapper + recommendations pipeline
│   └── scheduled_actions/                # keep/snooze/keep-forever mutations behind the web keep routes
└── web/
    ├── auth/                              # admin users, bcrypt, sessions, lockout, reauth (sole DB owner of these 4 tables)
    ├── middleware/                        # CSRF, body-size cap, CSP/headers+nonce, force-password-change, rate_limit decorator
    ├── models/                            # Pydantic request-body validation
    ├── repository/                        # web-owned SQL; returns dataclasses (or sanctioned template dicts)
    ├── routes/                            # FastAPI route handlers, one sub-package per feature area
    ├── static/                            # dependency-free vanilla JS/CSS bundle (window.MM namespace)
    └── templates/                         # Jinja2 HTML, base.html inheritance root
tests/
├── unit/                                  # auth, bootstrap, core, crypto, db, scanner, services, web
└── integration/                           # cross-module flows
```

## Key constants

| Constant | Default | Source |
|----------|---------|--------|
| `MEDIAMAN_PORT` | `8282` | `config.py` (`Config.port`) |
| Web server workers | `1` (enforced) | `bootstrap/validators.py` (`enforce_single_worker`) |
| `SESSION_COOKIE_MAX_AGE` | `86400` (24h) | `web/cookies.py` |
| `BCRYPT_ROUNDS` | `12` | `web/auth/_password_hash_helpers.py` |
| Weekly-scan misfire grace | `3600`s | `scanner/scheduler.py` (`_DEFAULT_MISFIRE_GRACE_SECONDS`) |
| Library-sync interval | `30` min (settings-driven) | `scanner/scheduler.py` (`sync_interval_minutes` default) |
| `MEDIAMAN_MAX_REQUEST_BYTES` | `8388608` (8 MiB) | `web/middleware/body_size.py` (`_DEFAULT_MAX_REQUEST_BYTES`) |
| `SafeHTTPClient` default cap | 8 MiB | `services/infra/http/client/_request.py` (`_DEFAULT_MAX_BYTES`) |
| Arr response cap | 64 MiB | `services/arr/_transport.py` (`_ARR_MAX_RESPONSE_BYTES`) |
| Plex response cap | 16 MiB | `services/media_meta/_plex_session.py` (`_PLEX_MAX_BYTES`) |
