"""FastAPI application factory and startup lifecycle.

This module is the single place that owns the full application lifecycle:

- :func:`create_app` — assembles the ``FastAPI`` instance, registers
  middleware, mounts static files, and wires all routers.
- :func:`lifespan` — orchestrates startup and shutdown in the correct
  order (DB → crypto canary → scheduler → reconciliation).
- ``bootstrap_*`` helpers — the "runs once at startup" logic that was
  previously spread across the ``bootstrap/`` sub-package.  Keeping them
  here means ``lifespan`` can call them directly without the extra layer
  of indirection.  The ``bootstrap/`` package is now a thin back-compat
  shim so existing import paths (including test fixtures) continue to work.

Public names
------------
The following names are re-exported by ``mediaman.bootstrap`` for
backwards compatibility:

- :class:`DataDirNotWritableError`
- :func:`bootstrap_db`
- :func:`bootstrap_crypto`
- :func:`bootstrap_scheduling`
- :func:`shutdown_scheduling`
"""

from __future__ import annotations

import errno
import logging
import os
import sys
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediaman.config import Config, ConfigError, load_config
from mediaman.validators import (
    validate_scan_day,
    validate_scan_time,
    validate_scan_timezone,
    validate_sync_interval,
)

logger = logging.getLogger("mediaman")

_STATIC_DIR = Path(__file__).parent / "web" / "static"
_TEMPLATE_DIR = Path(__file__).parent / "web" / "templates"

# Bounded wait at shutdown so a SIGTERM can't be wedged forever by a
# long-running scan job. 30 s is comfortably longer than the
# inter-request work each scan loop iteration performs (DB write +
# Plex/Arr round-trips) but short enough that an orchestrator's normal
# 60–90 s grace window will never escalate to SIGKILL on a healthy
# scheduler.
_SHUTDOWN_TIMEOUT_SECONDS = 30

# Stuck-deletion recovery is best-effort at startup, but a recurring
# failure means deletions are leaking forever. We escalate to CRITICAL
# once the failure repeats so the noise is impossible to miss.
_stuck_deletion_failures = 0


# ---------------------------------------------------------------------------
# DB bootstrap
# ---------------------------------------------------------------------------


class DataDirNotWritableError(RuntimeError):
    """Raised when the data directory cannot be written by the current process.

    The Dockerfile pins the runtime identity to uid/gid 1000:1000. If an
    operator bind-mounts a host directory whose ownership doesn't match,
    SQLite eventually fails mid-migration with an opaque "attempt to write
    a readonly database" stack trace. We probe writability up-front so the
    operator sees one actionable line instead of a Python traceback.
    """


def _remediation_for(exc: OSError) -> str:
    """Return errno-tailored remediation advice for an OSError on the data dir."""
    proc_uid = os.geteuid()
    proc_gid = os.getegid()
    if exc.errno == errno.ENOSPC:
        return "disk is full — free space on the host filesystem backing /data"
    if exc.errno == errno.EROFS:
        return "filesystem is mounted read-only — remount rw or use a different path"
    if exc.errno == errno.EDQUOT:
        return "disk quota exceeded for the owning user — raise quota or free space"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"likely wrong ownership — on the host run: "
            f"chown -R {proc_uid}:{proc_gid} <your-bind-mount-for-/data>"
        )
    return (
        f"unexpected error (errno={exc.errno}) — most often this is wrong "
        f"ownership; on the host try: "
        f"chown -R {proc_uid}:{proc_gid} <your-bind-mount-for-/data>"
    )


def _assert_data_dir_writable(data_dir: Path) -> None:
    """Fail fast and loud if ``data_dir`` is not writable by this process.

    Uses a self-cleaning temp file rather than a fixed probe path so a
    partial failure can't leave a stray file behind. ``os.access`` is not
    used because it consults real (not effective) uid and ignores read-only
    filesystem mounts and ACLs.
    """
    try:
        with tempfile.NamedTemporaryFile(
            dir=data_dir, prefix=".mediaman-write-probe-", delete=True
        ):
            pass
    except OSError as exc:
        proc_uid = os.geteuid()
        proc_gid = os.getegid()
        try:
            st = data_dir.stat()
            owner = f"uid={st.st_uid} gid={st.st_gid}"
        except OSError:
            owner = "uid=? gid=? (stat failed)"
        raise DataDirNotWritableError(
            f"data dir {data_dir} is not writable by uid={proc_uid} "
            f"gid={proc_gid} (currently owned by {owner}); "
            f"{_remediation_for(exc)}; underlying error: {exc}"
        ) from exc


def bootstrap_db(app: FastAPI, config: Config) -> None:
    """Open the SQLite DB, run migrations, register the bootstrap connection.

    Side effects on ``app.state``:

    - ``app.state.config`` — the resolved config object.
    - ``app.state.db`` — the bootstrap :class:`sqlite3.Connection`.
    - ``app.state.db_path`` — absolute path of the DB file.
    """
    from mediaman.db import init_db, set_connection

    data_dir = Path(config.data_dir)
    # ``mkdir`` precedes the writability probe — without the wrapper the
    # OSError surfaces as an unhandled traceback (lost behind a wall of
    # ASGI frames) instead of the actionable single-line error the probe
    # produces.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        proc_uid = os.geteuid()
        proc_gid = os.getegid()
        raise DataDirNotWritableError(
            f"data dir {data_dir} could not be created by uid={proc_uid} "
            f"gid={proc_gid}; {_remediation_for(exc)}; underlying error: {exc}"
        ) from exc
    _assert_data_dir_writable(data_dir)
    db_path = str(Path(config.data_dir) / "mediaman.db")
    logger.info("DB initialised at %s", db_path)
    conn = init_db(db_path)
    set_connection(conn)
    app.state.config = config
    app.state.db = conn
    app.state.db_path = db_path

    # Ensure the poster cache directory exists at startup so the first
    # request doesn't race with the lazy mkdir inside the poster route.
    poster_cache_dir = Path(config.data_dir) / "poster_cache"
    poster_cache_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Crypto bootstrap
# ---------------------------------------------------------------------------


def bootstrap_crypto(app: FastAPI, config: Config) -> None:
    """Run the AES canary check and stash the result on ``app.state``.

    Does NOT refuse to start on a mismatch — the admin must still be
    able to log in to re-enter secrets. The downstream
    :func:`bootstrap_scheduling` reads the flag and refuses to start the
    scheduler when the canary failed.

    The canary state is initialised to ``False`` and only flipped to
    ``True`` after :func:`canary_check` returns a positive result. An
    import failure or any other exception leaves the flag at its
    fail-closed default — without this, a partial import (e.g. a missing
    ``cryptography`` extension) would slip through with the optimistic
    ``True`` and the scheduler would gleefully fire scans against
    settings it cannot decrypt.
    """
    canary_ok = False
    try:
        from mediaman.crypto import canary_check, migrate_legacy_ciphertexts

        canary_ok = bool(canary_check(app.state.db, config.secret_key))
        if canary_ok:
            # Migration v35: re-encrypt any legacy v1 or no-AAD v2 settings
            # ciphertexts to v2+AAD. Safe to call on every startup —
            # already-migrated rows are skipped. Errors are logged but do
            # not abort startup.
            try:
                n = migrate_legacy_ciphertexts(app.state.db, config.secret_key)
                if n:
                    logger.info("bootstrap_crypto: migrated %d legacy settings row(s) to v2+AAD", n)
            except Exception:
                logger.exception("bootstrap_crypto: migrate_legacy_ciphertexts failed (non-fatal)")
    except Exception:
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok


# ---------------------------------------------------------------------------
# Scheduling bootstrap
# ---------------------------------------------------------------------------


def _run_scheduled_scan(db_path: str | None, secret_key: str) -> None:
    """Execute a scheduled scan, reading all settings fresh from the DB.

    Lifted to module scope so it can be unit-tested without standing up
    an entire FastAPI app. The scheduler is in-process and the heartbeat
    thread opens its own DB connection, so the only dependencies passed
    in are the bootstrap DB path (for the heartbeat) and the AES secret
    (forwarded to the scan runner so settings can be decrypted).
    """
    from mediaman.db import (
        finish_scan_run,
        get_db,
        heartbeat_scan_run,
        open_thread_connection,
        start_scan_run,
    )
    from mediaman.scanner.runner import run_scan_from_db

    db_conn = get_db()
    run_id = start_scan_run(db_conn)
    if run_id is None:
        logger.info("Scheduled scan skipped — another scan is already running")
        return

    # Heartbeat worker keeps the lease alive while the scan itself is
    # busy with disk + Plex + Arr round-trips. Uses its own connection
    # so it never contends with the scan's writes for the SQLite write
    # lock.
    stop_heartbeat = threading.Event()

    def _heartbeat_loop() -> None:
        if not db_path:
            return
        try:
            hb_conn = open_thread_connection(db_path)
        except Exception:
            logger.warning("scan heartbeat thread could not open DB", exc_info=True)
            return
        try:
            while not stop_heartbeat.wait(60):
                heartbeat_scan_run(hb_conn, run_id)
        finally:
            try:
                hb_conn.close()
            except Exception:  # pragma: no cover — best-effort close
                logger.debug("scan heartbeat close failed", exc_info=True)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="scan-heartbeat", daemon=True)
    heartbeat_thread.start()
    try:
        run_scan_from_db(db_conn, secret_key)
        finish_scan_run(db_conn, run_id, "done")
    except Exception as exc:
        try:
            finish_scan_run(db_conn, run_id, "error", str(exc))
        except Exception:  # pragma: no cover — finish is best-effort here
            logger.debug("scan finish (error path) failed", exc_info=True)
        logger.exception("Scheduled scan failed")
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=5)


def _run_library_sync_job(secret_key: str) -> None:
    """Execute a lightweight library sync from Plex."""
    from mediaman.db import get_db
    from mediaman.scanner.runner import run_library_sync

    try:
        db_conn = get_db()
        run_library_sync(db_conn, secret_key)
    except Exception:
        logger.exception("Library sync failed")


def bootstrap_scheduling(app: FastAPI, config: Config) -> bool:
    """Start the APScheduler jobs. Returns True iff the scheduler actually started."""
    from mediaman.scanner.scheduler import start_scheduler
    from mediaman.services.infra.settings_reader import get_string_setting as _get_setting

    conn = app.state.db
    canary_ok = getattr(app.state, "canary_ok", True)

    # Default to "not ready" — only flipped to True at the end of the
    # successful path. /readyz reads this flag to decide its 200/503.
    app.state.scheduler_healthy = False
    # ``scheduler_error`` carries the *why* into /readyz so an
    # orchestrator log line can reach the operator without them having
    # to ssh into the container and tail the python logs.
    app.state.scheduler_error = None

    global _stuck_deletion_failures

    try:
        if not canary_ok:
            raise RuntimeError(
                "Refusing to start scheduler: AES canary check failed. "
                "Fix MEDIAMAN_SECRET_KEY (or re-enter encrypted settings) "
                "and restart. The web UI is still accessible so an admin "
                "can investigate."
            )

        # Reconcile any rows left in the 'deleting' state by a previous
        # crash — safe even if no scan runs this boot.
        try:
            from mediaman.scanner.engine import _recover_stuck_deletions

            _recover_stuck_deletions(conn)
            _stuck_deletion_failures = 0
        except Exception:
            _stuck_deletion_failures += 1
            if _stuck_deletion_failures > 1:
                logger.critical(
                    "Stuck-deletion recovery has failed %d consecutive boot(s); "
                    "deletions left in the 'deleting' state will accumulate "
                    "until the underlying error is resolved.",
                    _stuck_deletion_failures,
                    exc_info=True,
                )
            else:
                logger.exception("Stuck-deletion recovery failed at startup")

        scan_day = validate_scan_day(_get_setting(conn, "scan_day", default="mon"))
        scan_time = _get_setting(conn, "scan_time", default="09:00")
        scan_tz = validate_scan_timezone(_get_setting(conn, "scan_timezone", default="UTC"))
        hour, minute = validate_scan_time(scan_time)

        # Capture the secret key and the DB path now; the scheduler
        # callbacks are invoked from APScheduler worker threads where
        # ``app`` is not in scope.
        secret_key = config.secret_key
        db_path = getattr(app.state, "db_path", None)

        def scan_callback() -> None:
            _run_scheduled_scan(db_path, secret_key)

        def sync_callback() -> None:
            _run_library_sync_job(secret_key)

        sync_interval = validate_sync_interval(
            _get_setting(conn, "library_sync_interval", default="30")
        )

        start_scheduler(
            scan_fn=scan_callback,
            day_of_week=scan_day,
            hour=hour,
            minute=minute,
            timezone=scan_tz,
            sync_fn=sync_callback,
            sync_interval_minutes=sync_interval,
            secret_key=secret_key,
        )
        app.state.scheduler_healthy = True
        logger.info(
            "Scheduler started: scan every %s at %02d:%02d %s, library sync every %d min",
            scan_day,
            hour,
            minute,
            scan_tz,
            sync_interval,
        )
        return True
    except Exception as e:
        logger.exception("Could not start scheduler: %s", e)
        app.state.scheduler_error = str(e) or e.__class__.__name__
        return False


def shutdown_scheduling() -> None:
    """Stop the APScheduler jobs with a bounded wait.

    Calls :func:`mediaman.scanner.scheduler.stop_scheduler` from a worker
    thread and joins for at most :data:`_SHUTDOWN_TIMEOUT_SECONDS`. The
    underlying call passes ``wait=False`` to APScheduler for fast
    shutdown of the scheduler thread itself, but jobs already executing
    are allowed to complete within the timeout window so a SIGTERM mid-
    scan does not abandon a half-written DB row. If the timeout expires
    we log and return — the alternative is a pod that ignores SIGTERM
    forever and gets SIGKILL'd by the orchestrator.

    Safe to call even when the scheduler was never started.
    """
    from mediaman.scanner.scheduler import stop_scheduler

    done = threading.Event()

    def _drain() -> None:
        try:
            stop_scheduler()
        except Exception:  # pragma: no cover — best-effort shutdown
            logger.exception("scheduler shutdown raised — abandoning in-flight jobs")
        finally:
            done.set()

    worker = threading.Thread(target=_drain, name="scheduler-shutdown", daemon=True)
    worker.start()
    if not done.wait(_SHUTDOWN_TIMEOUT_SECONDS):
        logger.warning(
            "Scheduler shutdown still draining after %ds — abandoning "
            "in-flight jobs to allow process exit.",
            _SHUTDOWN_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown lifecycle.

    Orchestrates the bootstrap steps in dependency order:

    1. DB first — every later step needs an open connection.
    2. Crypto canary — must run before the scheduler so a key mismatch
       refuses to spawn background jobs that would silently fail.
    3. Scheduling — opt-in; failures here are logged but do NOT take
       the web UI down.

    Fatal startup errors are converted into a single clean log line
    followed by ``sys.exit(1)`` rather than allowed to surface as an
    ASGI traceback. The traceback path buries the actionable line under
    fifteen frames of uvicorn/FastAPI internals; the single-line path
    fits in an orchestrator's restart-loop log without truncation.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        logger.critical("Configuration error at startup: %s", exc)
        sys.exit(1)

    try:
        bootstrap_db(app, config)
    except DataDirNotWritableError as exc:
        logger.critical("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.critical("Database bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    try:
        bootstrap_crypto(app, config)
    except Exception as exc:  # pragma: no cover — bootstrap_crypto already swallows
        logger.critical("Crypto bootstrap failed at startup: %s", exc, exc_info=True)
        sys.exit(1)

    scheduler_started = bootstrap_scheduling(app, config)

    # Reconcile any in-flight manual delete operations that crashed
    # between the external Radarr/Sonarr call and the local DB cleanup.
    # Idempotent — safe to run on every cold start.
    try:
        from mediaman.web.routes.library.api import reconcile_pending_delete_intents

        reconciled = reconcile_pending_delete_intents()
        if reconciled:
            logger.info("Reconciled %d pending delete intent(s) at startup", reconciled)
    except Exception:
        logger.exception("delete-intent reconciliation failed at startup; continuing")

    # Reconcile download_notifications rows stranded at notified=2 by a
    # crashed worker (H-5 — finding 22 follow-up).
    try:
        from mediaman.services.downloads.notifications import (
            reconcile_stranded_notifications,
        )

        reset = reconcile_stranded_notifications(app.state.db)
        if reset:
            logger.info("Reconciled %d stranded download notification(s) at startup", reset)
    except Exception:
        logger.exception("download-notification reconciliation failed at startup; continuing")

    logger.info("Mediaman started on port %s", config.port)
    yield

    if scheduler_started:
        shutdown_scheduling()

    app.state.db.close()
    logger.info("Mediaman shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Mediaman",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from mediaman.web import register_security_middleware

    register_security_middleware(app)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    from mediaman.web.routes.auth import router as auth_router
    from mediaman.web.routes.dashboard import router as dashboard_router
    from mediaman.web.routes.download import router as download_router
    from mediaman.web.routes.downloads import router as downloads_router
    from mediaman.web.routes.force_password_change import router as force_pw_router
    from mediaman.web.routes.history import router as history_router
    from mediaman.web.routes.keep import router as keep_router
    from mediaman.web.routes.kept import router as kept_router
    from mediaman.web.routes.library import router as library_router
    from mediaman.web.routes.poster import router as poster_router
    from mediaman.web.routes.recommended import router as recommended_router
    from mediaman.web.routes.scan import router as scan_router
    from mediaman.web.routes.search import router as search_router
    from mediaman.web.routes.settings import router as settings_router
    from mediaman.web.routes.subscribers import router as subscribers_router
    from mediaman.web.routes.users import router as users_router

    app.include_router(auth_router)
    app.include_router(force_pw_router)
    app.include_router(dashboard_router)
    app.include_router(download_router)
    app.include_router(downloads_router)
    app.include_router(history_router)
    app.include_router(keep_router)
    app.include_router(library_router)
    app.include_router(poster_router)
    app.include_router(kept_router)
    app.include_router(scan_router)
    app.include_router(settings_router)
    app.include_router(subscribers_router)
    app.include_router(search_router)
    app.include_router(recommended_router)
    app.include_router(users_router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness probe for the container HEALTHCHECK.

        Returns 200 with a tiny JSON body whenever the ASGI event loop
        is responsive. Deliberately no DB or Plex round-trip — those are
        failure modes the healthcheck should not conflate with the
        process being alive.
        """
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness probe — ``/healthz`` says "alive", this says "alive AND configured".

        The scheduler is the most consequential background service; if
        it did not come up the deletion executor and library sync will
        never run, so the app is technically responsive but
        operationally broken. Returning 503 here gives orchestrators a
        signal they can switch their healthcheck to without confusing
        liveness and readiness.

        When the probe is failing the body now carries the *reason* —
        the last scheduler bootstrap error stashed on
        ``app.state.scheduler_error`` — so an operator looking at the
        orchestrator status doesn't have to ssh into the container and
        tail the python logs to discover *why*.
        """
        scheduler_healthy = bool(getattr(app.state, "scheduler_healthy", False))
        canary_ok = bool(getattr(app.state, "canary_ok", True))
        ready = scheduler_healthy and canary_ok
        body: dict[str, str] = {
            "status": "ready" if ready else "not_ready",
            "scheduler": "ok" if scheduler_healthy else "down",
            "crypto": "ok" if canary_ok else "down",
        }
        if not ready:
            scheduler_error = getattr(app.state, "scheduler_error", None)
            if scheduler_error:
                body["scheduler_error"] = str(scheduler_error)
        return JSONResponse(body, status_code=200 if ready else 503)

    return app
