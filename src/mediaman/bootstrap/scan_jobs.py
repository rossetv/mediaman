"""Scheduling lifecycle helpers: job callbacks and scheduler start/stop."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable

from fastapi import FastAPI

from mediaman.bootstrap.validators import (
    validate_scan_day,
    validate_scan_time,
    validate_scan_timezone,
    validate_sync_interval,
)
from mediaman.config import Config
from mediaman.scanner.scheduler import ScanSchedule

logger = logging.getLogger(__name__)


class SchedulerStartupRefused(Exception):
    """Raised when the scheduler cannot start due to a fatal pre-flight failure.

    Callers (the FastAPI lifespan) catch this to record the refusal reason on
    ``app.state.scheduler_error`` and continue serving the web UI — the
    operator can then investigate via ``/readyz`` without having to restart
    the container.
    """


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
# Single-process bootstrap-thread invariant — mutated only from
# bootstrap_scheduling; no lock required.
_stuck_deletion_failures = 0


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Drop any open write transaction so the thread's connection frees its lock."""
    try:
        conn.rollback()
    except sqlite3.Error:
        logger.warning("rollback after failed job also failed", exc_info=True)


def _run_scheduled_scan(db_path: str | None, secret_key: str) -> None:
    """Execute a scheduled scan, reading all settings fresh from the DB.

    Module-level so it can be unit-tested without standing up
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
    from mediaman.scanner.runner import run_scan_from_db, scan_end
    from mediaman.web.repository.delete_intents import reconcile_before_scan

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
        except sqlite3.Error:
            logger.warning("scan heartbeat thread could not open DB", exc_info=True)
            return
        try:
            while not stop_heartbeat.wait(60):
                heartbeat_scan_run(hb_conn, run_id)
        finally:
            try:
                hb_conn.close()
            except sqlite3.Error:  # pragma: no cover — best-effort close
                logger.debug("scan heartbeat close failed", exc_info=True)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="scan-heartbeat", daemon=True)
    heartbeat_thread.start()
    try:
        reconcile_before_scan(db_conn, secret_key)
        summary = run_scan_from_db(db_conn, secret_key)
        finish_scan_run(db_conn, run_id, *scan_end(summary))
    except Exception as exc:  # rationale: scheduler must survive a single bad row
        _rollback_quietly(db_conn)
        try:
            finish_scan_run(db_conn, run_id, "error", str(exc))
        except sqlite3.Error:  # pragma: no cover — finish is best-effort here
            logger.debug("scan finish (error path) failed", exc_info=True)
        logger.exception("Scheduled scan failed")
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=5)


def _run_library_sync_job(secret_key: str) -> None:
    from mediaman.db import get_db
    from mediaman.scanner.runner import run_library_sync

    db_conn = None
    try:
        db_conn = get_db()
        run_library_sync(db_conn, secret_key)
    except Exception:  # rationale: scheduler must survive a single bad row
        logger.exception("Library sync failed")
        if db_conn is not None:
            _rollback_quietly(db_conn)


def _recover_stuck_deletions_at_boot(conn: sqlite3.Connection) -> None:
    """Reconcile rows left in the 'deleting' state by a previous crash.

    Books nothing: a row whose removal finished goes back to pending, so the
    next live pass books it and unmonitors it in Radarr/Sonarr.

    Best-effort and safe even if no scan runs this boot. A recurring
    failure means deletions are leaking forever, so the
    ``_stuck_deletion_failures`` counter escalates the log to CRITICAL
    once the failure repeats.
    """
    global _stuck_deletion_failures
    try:
        from mediaman.scanner.deletions import recover_stuck_deletions

        recover_stuck_deletions(conn, book=False)
        _stuck_deletion_failures = 0
    except Exception:  # rationale: cold-start recovery
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


def _read_scheduler_config(conn: sqlite3.Connection) -> ScanSchedule:
    """Read and validate the scheduler settings from the DB.

    Each value is passed through its mediaman.bootstrap.validators
    check so a malformed setting raises here (and is caught by
    bootstrap_scheduling's cold-start handler) rather than
    reaching APScheduler.
    """
    from mediaman.services.infra import get_string_setting as _get_setting

    scan_day = validate_scan_day(_get_setting(conn, "scan_day", default="mon"))
    scan_time = _get_setting(conn, "scan_time", default="09:00")
    scan_tz = validate_scan_timezone(_get_setting(conn, "scan_timezone", default="UTC"))
    hour, minute = validate_scan_time(scan_time)
    raw_interval = _get_setting(conn, "library_sync_interval", default="30")
    # 0 means "disabled" in the settings UI; the validator refuses it so only
    # the scheduler config lets it through (the sync job is then not registered).
    sync_interval = 0 if raw_interval.strip() == "0" else validate_sync_interval(raw_interval)
    return ScanSchedule(
        scan_day=scan_day,
        hour=hour,
        minute=minute,
        scan_tz=scan_tz,
        sync_interval=sync_interval,
    )


def _build_scheduler_callbacks(
    db_path: str | None, secret_key: str
) -> tuple[Callable[[], None], Callable[[], None]]:
    """Build the ``(scan, sync)`` APScheduler callbacks.

    The callbacks close over *db_path* and *secret_key* because they are
    invoked from APScheduler worker threads where ``app`` is not in
    scope — the caller captures both off ``app.state``/``config`` while
    it still can and threads them in here.
    """

    def scan_callback() -> None:
        _run_scheduled_scan(db_path, secret_key)

    def sync_callback() -> None:
        _run_library_sync_job(secret_key)

    return scan_callback, sync_callback


SCHEDULE_SETTING_KEYS = frozenset(
    {"scan_day", "scan_time", "scan_timezone", "library_sync_interval"}
)


def reschedule_scan_jobs(conn: sqlite3.Connection, db_path: str | None, secret_key: str) -> bool:
    """Apply the saved scan schedule to the running scheduler.

    Only a job whose saved schedule differs from the live one is re-added, so a
    save that changes nothing leaves the library-sync countdown alone. Returns
    False when no scheduler is running; raises ``ValueError`` when a stored
    value is malformed, leaving the current schedule in place.
    """
    from mediaman.scanner import scheduler as sched

    scan_callback, sync_callback = _build_scheduler_callbacks(db_path, secret_key)
    return sched.reschedule_scan_jobs(
        lambda: _read_scheduler_config(conn), scan_fn=scan_callback, sync_fn=sync_callback
    )


def bootstrap_scheduling(app: FastAPI, config: Config) -> bool:
    from mediaman.scanner.scheduler import start_scheduler

    conn = app.state.db
    canary_ok = getattr(app.state, "canary_ok", False)  # fail-closed: unset → refuse to start

    # Default to "not ready" — only flipped to True at the end of the
    # successful path. /readyz reads this flag to decide its 200/503.
    app.state.scheduler_healthy = False
    # ``scheduler_error`` carries the *why* into /readyz so an
    # orchestrator log line can reach the operator without them having
    # to ssh into the container and tail the python logs.
    app.state.scheduler_error = None

    try:
        if not canary_ok:
            raise SchedulerStartupRefused(
                "Refusing to start scheduler: AES canary check failed. "
                "Put the previous MEDIAMAN_SECRET_KEY back, or re-enter or "
                "clear the encrypted settings the AES key mismatch warning "
                "names, then restart. The web UI is still accessible so an "
                "admin can investigate."
            )

        _recover_stuck_deletions_at_boot(conn)

        sched_config = _read_scheduler_config(conn)

        # Capture the secret key and the DB path now; the scheduler
        # callbacks are invoked from APScheduler worker threads where
        # ``app`` is not in scope.
        secret_key = config.secret_key
        db_path = getattr(app.state, "db_path", None)
        scan_callback, sync_callback = _build_scheduler_callbacks(db_path, secret_key)

        start_scheduler(
            scan_fn=scan_callback,
            day_of_week=sched_config.scan_day,
            hour=sched_config.hour,
            minute=sched_config.minute,
            timezone=sched_config.scan_tz,
            sync_fn=sync_callback,
            sync_interval_minutes=sched_config.sync_interval,
            secret_key=secret_key,
        )
        app.state.scheduler_healthy = True
        logger.info(
            "Scheduler started: scan every %s at %02d:%02d %s, library sync %s",
            sched_config.scan_day,
            sched_config.hour,
            sched_config.minute,
            sched_config.scan_tz,
            f"every {sched_config.sync_interval} min" if sched_config.sync_interval else "disabled",
        )
        return True
    # rationale: cold-start recovery; app stays up so the operator can investigate.
    except Exception as exc:
        logger.exception("bootstrap.scheduler_start_failed", extra={"error": str(exc)})
        app.state.scheduler_error = str(exc) or exc.__class__.__name__
        return False


def shutdown_scheduling() -> None:
    """Stop the APScheduler jobs with a bounded wait.

    Calls mediaman.scanner.scheduler.stop_scheduler from a worker
    thread and joins for at most _SHUTDOWN_TIMEOUT_SECONDS. The
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
        except Exception:  # rationale: log and proceed so SIGTERM completes  # pragma: no cover — best-effort shutdown
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
    "SchedulerStartupRefused",
    "bootstrap_scheduling",
    "shutdown_scheduling",
]
