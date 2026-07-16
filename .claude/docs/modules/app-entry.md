<!-- Claude-maintained; humans never edit. Registered in .claude/INDEX.md — an
unregistered KB file is a defect. Every path, command, and constant below must
be verified against the code before writing; on contradiction, fix here at
once. Unknown fact → omit the section, never guess.
NEVER cite a line number (no `file.sh:234`, no bare `(:234)`, no ranges): any edit
above it rots the citation, and re-numbering then eats the whole maintenance budget.
Cite the file plus a stable, greppable anchor — a function, variable, constant or
check name: `scripts/kb-gate.sh` (`git_push_in()`). Verify the anchor with grep. -->
↑ [INDEX](../../INDEX.md)

# Module: app-entry

## Purpose

The application's startup surface: turns environment variables into a validated
`Config`, assembles the FastAPI app, and orchestrates the bootstrap lifecycle
(DB → crypto canary → scheduler → crash-state reconciliation). Production entry
is the `mediaman` console script → `mediaman.main:cli_main`
(`pyproject.toml` `[project.scripts]`), which the Dockerfile runs via
`CMD ["mediaman"]` on `EXPOSE 8282`. `main.py` is a thin CLI/uvicorn wrapper;
`app_factory.py` owns the FastAPI-facing surface; `config.py` loads bootstrap-only
env config; the `bootstrap/` package holds the individual startup steps and the
pure operator-input validators.

## Key files

| File | Role |
|------|------|
| `src/mediaman/main.py` | CLI/uvicorn entry. `cli_main()` dispatches the `create-user` subcommand, calls `enforce_single_worker()`, loads+validates config (clean CLI exit on `ConfigError`), builds the app via `create_app()`, resolves the bind host (`_resolve_bind_host()`: `0.0.0.0` in-container else `127.0.0.1`), and runs uvicorn — `proxy_headers` enabled ONLY when a sanitised non-wildcard `MEDIAMAN_TRUSTED_PROXIES` exists. Module-level `app` is created only under `MEDIAMAN_EAGER_APP=1`. |
| `src/mediaman/app_factory.py` | FastAPI factory + lifecycle. `create_app()` builds `FastAPI` (docs/redoc/openapi disabled), registers security middleware, mounts `/static`, wires Jinja2 templates, and calls `_register_routers` (17 routers, imports deferred) + `_register_probes` (`/healthz` liveness, `/readyz` readiness). `lifespan()` runs the ordered startup and `_shutdown` on exit; fatal DB/crypto failures `sys.exit(1)`. |
| `src/mediaman/config.py` | Bootstrap config only. Frozen `Config` (`secret_key`, `port=8282`, `data_dir="/data"`, `bind_host=""`, `trusted_proxies=""`) plus `load_config()`/`ConfigError`. Validates `MEDIAMAN_SECRET_KEY` presence + entropy (via `crypto._aes_key._is_secret_key_strong`), `MEDIAMAN_PORT` (int 1–65535), and non-empty `MEDIAMAN_DATA_DIR`. All non-bootstrap settings (Plex/Sonarr/Radarr) live in the DB. |
| `src/mediaman/__init__.py` | Top-level package marker. Resolves `__version__` via `importlib.metadata.version("mediaman")`, falling back to `"0.1.0"` on `PackageNotFoundError`. Imports nothing beyond the stdlib; holds no application logic. |
| `src/mediaman/bootstrap/__init__.py` | Re-export shim. Surfaces `bootstrap_crypto`, `bootstrap_db`, `bootstrap_scheduling`, `shutdown_scheduling` from their canonical modules (`__all__`). |
| `src/mediaman/bootstrap/db.py` | Startup step 1. `bootstrap_db(app, config)` mkdirs the data dir (wrapping `OSError` into `DataDirNotWritableError`), runs `assert_data_dir_writable`, `init_db` + `set_connection`, then stashes `app.state.config`/`db`/`db_path` and pre-creates the `poster_cache` dir. |
| `src/mediaman/bootstrap/data_dir.py` | Data-dir writability probe. `assert_data_dir_writable()` writes a self-deleting temp file (not `os.access`, which reads the real uid and ignores RO mounts/ACLs); on `OSError` raises `DataDirNotWritableError` with errno-tailored `remediation_for()` advice (ENOSPC/EROFS/EDQUOT/EACCES/EPERM chown hint keyed to euid:egid). |
| `src/mediaman/bootstrap/crypto.py` | Startup step 2. `bootstrap_crypto(app, config)` runs the AES canary (`is_canary_valid`) against `MEDIAMAN_SECRET_KEY` and stashes fail-closed `app.state.canary_ok` (default `False`). Does NOT abort on mismatch (admin must still log in) but re-raises `ImportError`/`ModuleNotFoundError` (incident c089474). Canary failures are best-effort audit-logged. |
| `src/mediaman/bootstrap/scan_jobs.py` | Startup step 3 + shutdown. `bootstrap_scheduling(app, config)` refuses to start when `canary_ok` is false (`SchedulerStartupRefused`), recovers stuck deletions, reads+validates scheduler settings fresh from the DB, and starts APScheduler with scan + library-sync callbacks (closing over `db_path`/`secret_key` for worker threads); sets `app.state.scheduler_healthy`/`scheduler_error`. `shutdown_scheduling()` stops the scheduler on a worker thread with a bounded 30 s join. Holds `_run_scheduled_scan` and the `_stuck_deletion_failures` counter. |
| `src/mediaman/bootstrap/validators.py` | Pure, stdlib-only, side-effect-free validators. `validate_scan_time`/`validate_scan_day`/`validate_scan_timezone`/`validate_sync_interval` (used by `scan_jobs`), `enforce_single_worker` (raises `RuntimeError` on `WORKERS`/`UVICORN_WORKERS`/`MEDIAMAN_WORKERS` > 1), `sanitise_trusted_proxies` (drops the wildcard tokens in `_FORBIDDEN_TRUSTED_PROXY_TOKENS` with a CRITICAL log, drops non-CIDR entries with a WARNING, returns comma-joined survivors). |

## Invariants

| Invariant | Why / enforcement |
|-----------|-------------------|
| Single-worker only — `enforce_single_worker()` raises `RuntimeError` if `MEDIAMAN_WORKERS`/`UVICORN_WORKERS`/`WORKERS` > 1 | The APScheduler instance, in-memory rate limits and search-trigger throttle assume one process (token replay is now SQLite-backed, but the rest is not horizontally scalable). |
| `proxy_headers` is enabled ONLY when `sanitise_trusted_proxies(config.trusted_proxies)` returns a non-empty, non-wildcard value; otherwise uvicorn runs `proxy_headers=False` | uvicorn's `proxy_headers` rewrites `request.client.host` from `X-Forwarded-For`, a per-IP rate-limit-bypass footgun. |
| Fail-closed readiness: `app.state.canary_ok` and `app.state.scheduler_healthy` both default `False`; `/readyz` returns 503 unless BOTH are true | `bootstrap_crypto` only flips `canary_ok` to `True` after `is_canary_valid` returns positive. |
| Startup order in `lifespan()` is fixed and dependency-ordered: `load_config` → `install_root_filter` → `bootstrap_db` → `bootstrap_crypto` → `bootstrap_scheduling` → `_run_startup_reconciliation`; shutdown reverses it (scheduler only if started, then DB close) | Every later step needs the earlier one — e.g. the scheduler must not start before the canary verdict is known. |
| DB and crypto bootstrap failures are fatal (`sys.exit(1)` with a single clean log line, not an ASGI traceback); scheduler startup failure is NON-fatal | The web UI stays up so an operator can investigate the scheduler failure via `/readyz`. |
| `ImportError`/`ModuleNotFoundError` raised during the crypto canary is re-raised, never swallowed | A missing module must crash bootstrap immediately instead of masquerading as a key mismatch — regression guard for incident c089474, a 13-day scheduler outage. |
| `MEDIAMAN_SECRET_KEY` is the only mandatory env var and must pass the `_is_secret_key_strong` entropy check; every other value has a default | `Config` is a frozen dataclass loaded once. |
| `bind_host` default of `""` means "no operator override" → `_resolve_bind_host()` picks `0.0.0.0` inside a container (detected via `/.dockerenv` or `container=docker`) and `127.0.0.1` on bare metal; an explicit `MEDIAMAN_BIND_HOST` always wins | Avoids a container binding the unreachable loopback while keeping bare-metal deployments localhost-only by default. |
| The scheduler will not start when the AES canary failed: `bootstrap_scheduling` raises `SchedulerStartupRefused` before touching APScheduler if `app.state.canary_ok` is false | Background jobs would silently fail against a wrong key. |
| The uvicorn server always runs with `server_header=False` and `date_header=False` (both branches) | Suppresses server-fingerprinting response headers regardless of the proxy-headers path. |

## Gotchas

| Gotcha | Detail |
|--------|--------|
| `MEDIAMAN_DELETE_ROOTS` is intentionally NOT a `Config` field | Read on demand at deletion time in `mediaman.scanner.repository`. Adding it here would imply a single source of truth that does not exist and risk divergence from the live env var. |
| `load_config()` is called twice per boot | Once in `cli_main()` (to fail fast with a clean CLI message before uvicorn is imported) and again inside `lifespan()` (belt-and-braces; the lifespan copy is the one stashed on `app.state`). |
| `data_dir` defaults to `/data` (the container `VOLUME`) | On bare-metal installs this default will fail on most distros — operators must set `MEDIAMAN_DATA_DIR` to a writable path. |
| `create-user` short-circuits | `cli_main` mutates `sys.argv` and returns before `enforce_single_worker()` and the uvicorn import, so the subcommand runs without any single-worker check or server startup. |
| `/readyz` is UNAUTHENTICATED and reachable on the public vhost | Its body is only `{status: ready\|not_ready}`; the failure reason (scheduler/crypto state, raw bootstrap exception text that can leak paths/exception classes) is written to logs via `logger.warning`, never returned to the caller. |
| `enforce_single_worker` only RAISES on an integer value > 1 | An unparseable value (e.g. `WORKERS=auto`) logs a WARNING and is treated as unset, so a typo cannot silently disable the guard but also does not block startup. |
| `assert_data_dir_writable` deliberately avoids `os.access` | `os.access` consults real not effective uid and ignores read-only mounts/ACLs, so it writes a self-deleting `NamedTemporaryFile` instead; `bootstrap_db` additionally wraps the `mkdir` `OSError` into `DataDirNotWritableError` so a mkdir failure is actionable too. |
| `/healthz` performs no DB or Plex round-trip | It only proves the ASGI loop is responsive; `/readyz` is the endpoint that asserts "alive AND configured". |
| The 17 route-module imports live inside `_register_routers`, not at module top | Purely so importing `mediaman.app_factory` does not transitively import every route and its dependencies — an intentional import-cost optimisation. |
| Both startup reconciliation sweeps (pending delete intents, stranded download notifications) are best-effort | Any exception is logged via `logger.exception` and swallowed so a bad reconciliation never blocks the web UI from coming up. |

## Extension points

- **New startup/shutdown step** → add it to `lifespan()` in dependency order, with its reverse teardown in `_shutdown()`.
- **New route module** → import it and `include_router` inside `_register_routers` (keep the import deferred to preserve the import-cost optimisation).
- **New operator-input validator** → add it to `bootstrap/validators.py` (pure, stdlib-only, side-effect-free); wire scheduler settings through `_read_scheduler_config` in `scan_jobs.py`.
- **New bootstrap-only env setting** → add a field to the frozen `Config` and validate it in `load_config()`. Runtime/service settings (Plex/Sonarr/Radarr) belong in the DB, not here.
- **New container probe** → register it inside `_register_probes` so it can close over `app.state`.
