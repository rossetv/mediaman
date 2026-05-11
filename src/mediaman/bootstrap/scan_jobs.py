"""Scheduling lifecycle helpers: job callbacks and scheduler start/stop."""

from __future__ import annotations

import logging
import threading

from fastapi import FastAPI

from mediaman.bootstrap.validators import (
    validate_scan_day,
    validate_scan_time,
    validate_scan_timezone,
    validate_sync_interval,
)
from mediaman.config import Config

logger = logging.getLogger(__name__)

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
            from mediaman.scanner.deletions import _recover_stuck_deletions

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
            """APScheduler callback: run the weekly scheduled scan in a worker thread."""
            _run_scheduled_scan(db_path, secret_key)

        def sync_callback() -> None:
            """APScheduler callback: run the periodic library sync in a worker thread."""
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


__all__ = [
    "_SHUTDOWN_TIMEOUT_SECONDS",
    "_run_library_sync_job",
    "_run_scheduled_scan",
    "_stuck_deletion_failures",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
