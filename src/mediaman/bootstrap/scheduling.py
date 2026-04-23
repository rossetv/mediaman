"""Scheduler bootstrap step (R23).

Extracts the APScheduler wiring — scan cron + periodic library sync —
out of ``main.lifespan``. ``bootstrap_scheduling`` returns True when the
scheduler started; the lifespan shutdown branch calls
:func:`shutdown_scheduling` only when it did.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import FastAPI

from mediaman.services.settings_reader import get_string_setting as _get_setting

logger = logging.getLogger("mediaman")

_SCAN_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _validate_scan_time(s: str) -> tuple[int, int]:
    """Parse and validate a scan time string in ``HH:MM`` 24-hour format.

    Returns ``(hour, minute)`` on success. Raises :class:`ValueError`
    with a descriptive message on any invalid input so the operator sees
    a clear startup error rather than a silent misconfiguration.

    Validation is two-stage: a regex confirms the shape, then
    :func:`datetime.strptime` confirms the value is a real time (e.g.
    ``"25:00"`` would pass the regex but fail strptime).
    """
    if not _SCAN_TIME_RE.match(s):
        raise ValueError(
            f"scan_time {s!r} is invalid — expected HH:MM in 24-hour format (e.g. '09:00')"
        )
    try:
        dt = datetime.strptime(s, "%H:%M")
    except ValueError:
        raise ValueError(
            f"scan_time {s!r} is not a valid time — expected HH:MM in 24-hour format"
        )
    return dt.hour, dt.minute


def bootstrap_scheduling(app: FastAPI, config) -> bool:
    """Start the APScheduler jobs. Returns True iff the scheduler actually started."""
    from mediaman.scanner.scheduler import start_scheduler

    conn = app.state.db
    canary_ok = getattr(app.state, "canary_ok", True)

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
        except Exception:
            logger.exception("Stuck-deletion recovery failed at startup")

        scan_day = _get_setting(conn, "scan_day", default="mon")
        scan_time = _get_setting(conn, "scan_time", default="09:00")
        scan_tz = _get_setting(conn, "scan_timezone", default="UTC")
        hour, minute = _validate_scan_time(scan_time)

        # Capture the secret key now; conn is accessed at scan time via get_db()
        _secret_key = config.secret_key

        def run_scheduled_scan() -> None:
            """Execute a scheduled scan, reading all settings fresh from the DB."""
            from mediaman.db import finish_scan_run, get_db, start_scan_run
            from mediaman.scanner.runner import run_scan_from_db

            db_conn = get_db()
            run_id = start_scan_run(db_conn)
            if run_id is None:
                logger.info("Scheduled scan skipped — another scan is already running")
                return
            try:
                run_scan_from_db(db_conn, _secret_key)
                finish_scan_run(db_conn, run_id, "done")
            except Exception as exc:
                try:
                    finish_scan_run(db_conn, run_id, "error", str(exc))
                except Exception:
                    pass
                logger.exception("Scheduled scan failed")

        def run_library_sync_job() -> None:
            """Execute a lightweight library sync from Plex."""
            from mediaman.db import get_db
            from mediaman.scanner.runner import run_library_sync

            try:
                db_conn = get_db()
                run_library_sync(db_conn, _secret_key)
            except Exception:
                logger.exception("Library sync failed")

        sync_interval = int(_get_setting(conn, "library_sync_interval", default="30"))

        start_scheduler(
            scan_fn=run_scheduled_scan,
            day_of_week=scan_day,
            hour=hour,
            minute=minute,
            timezone=scan_tz,
            sync_fn=run_library_sync_job,
            sync_interval_minutes=sync_interval,
        )
        app.state.scheduler_healthy = True
        logger.info(
            "Scheduler started: scan every %s at %02d:%02d %s, library sync every %d min",
            scan_day, hour, minute, scan_tz, sync_interval,
        )
        return True
    except Exception as e:
        logger.error("Could not start scheduler: %s", e)
        return False


def shutdown_scheduling() -> None:
    """Stop the APScheduler jobs. Safe to call even when never started."""
    from mediaman.scanner.scheduler import stop_scheduler
    stop_scheduler()
