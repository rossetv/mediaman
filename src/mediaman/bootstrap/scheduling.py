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

from mediaman.config import Config
from mediaman.services.infra.settings_reader import get_string_setting as _get_setting

logger = logging.getLogger("mediaman")

_SCAN_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# APScheduler accepts either a single weekday token or a comma-separated
# list. We deliberately allow only canonical short names so a typo like
# "moon" trips early instead of silently producing a never-firing trigger.
_VALID_DAY_TOKENS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


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
        raise ValueError(f"scan_time {s!r} is not a valid time — expected HH:MM in 24-hour format")
    return dt.hour, dt.minute


def _validate_scan_day(s: str) -> str:
    """Reject scan_day values APScheduler can't parse.

    Accepts either a single token (``"mon"``) or a comma-separated list
    (``"mon,wed,fri"``). Tokens are normalised to lowercase short
    weekday names; anything else raises :class:`ValueError`.
    """
    raw = (s or "").strip().lower()
    if not raw:
        raise ValueError("scan_day must not be empty")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"scan_day {s!r} is invalid — expected one of {sorted(_VALID_DAY_TOKENS)}")
    bad = [p for p in parts if p not in _VALID_DAY_TOKENS]
    if bad:
        raise ValueError(
            f"scan_day {s!r} contains unknown weekday token(s): {bad!r} — "
            f"expected one or more of {sorted(_VALID_DAY_TOKENS)}"
        )
    return ",".join(parts)


def _validate_scan_timezone(s: str) -> str:
    """Reject scan_timezone values that aren't IANA timezones.

    Uses :class:`zoneinfo.ZoneInfo` to confirm the string resolves;
    raises :class:`ValueError` with a clear message otherwise so the
    operator sees the startup failure instead of an opaque
    APScheduler exception once the cron trigger fires.
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("scan_timezone must not be empty")
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except Exception:
        # Standard library should always provide it; defer if unavailable.
        return raw
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        raise ValueError(f"scan_timezone {raw!r} is not a known IANA timezone")
    except Exception as exc:
        raise ValueError(f"scan_timezone {raw!r} is invalid: {exc}")
    return raw


def _validate_sync_interval(s: str) -> int:
    """Parse and bound ``library_sync_interval`` (minutes).

    Returns the integer minute count. Refuses zero or negative values
    so the scheduler doesn't degenerate into a tight loop.
    """
    try:
        value = int(s)
    except (TypeError, ValueError):
        raise ValueError(
            f"library_sync_interval {s!r} is invalid — expected a positive integer (minutes)"
        )
    if value <= 0:
        raise ValueError(f"library_sync_interval must be a positive integer (got {value})")
    if value > 24 * 60:
        raise ValueError(f"library_sync_interval must be at most 1440 minutes (got {value})")
    return value


def bootstrap_scheduling(app: FastAPI, config: Config) -> bool:
    """Start the APScheduler jobs. Returns True iff the scheduler actually started."""
    from mediaman.scanner.scheduler import start_scheduler

    conn = app.state.db
    canary_ok = getattr(app.state, "canary_ok", True)

    # Default to "not ready" — only flipped to True at the end of the
    # successful path. /readyz reads this flag to decide its 200/503.
    app.state.scheduler_healthy = False

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

        scan_day = _validate_scan_day(_get_setting(conn, "scan_day", default="mon"))
        scan_time = _get_setting(conn, "scan_time", default="09:00")
        scan_tz = _validate_scan_timezone(_get_setting(conn, "scan_timezone", default="UTC"))
        hour, minute = _validate_scan_time(scan_time)

        # Capture the secret key now; conn is accessed at scan time via get_db()
        _secret_key = config.secret_key

        def run_scheduled_scan() -> None:
            """Execute a scheduled scan, reading all settings fresh from the DB."""
            import threading

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

            # Heartbeat worker keeps the lease alive while the scan
            # itself is busy with disk + Plex + Arr round-trips. Uses
            # its own connection so it never contends with the scan's
            # writes for the SQLite write lock (finding 9).
            stop_heartbeat = threading.Event()
            db_path = getattr(app.state, "db_path", None)

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

            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, name="scan-heartbeat", daemon=True
            )
            heartbeat_thread.start()
            try:
                run_scan_from_db(db_conn, _secret_key)
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

        def run_library_sync_job() -> None:
            """Execute a lightweight library sync from Plex."""
            from mediaman.db import get_db
            from mediaman.scanner.runner import run_library_sync

            try:
                db_conn = get_db()
                run_library_sync(db_conn, _secret_key)
            except Exception:
                logger.exception("Library sync failed")

        sync_interval = _validate_sync_interval(
            _get_setting(conn, "library_sync_interval", default="30")
        )

        start_scheduler(
            scan_fn=run_scheduled_scan,
            day_of_week=scan_day,
            hour=hour,
            minute=minute,
            timezone=scan_tz,
            sync_fn=run_library_sync_job,
            sync_interval_minutes=sync_interval,
            secret_key=_secret_key,
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
        logger.error("Could not start scheduler: %s", e)
        return False


def shutdown_scheduling() -> None:
    """Stop the APScheduler jobs. Safe to call even when never started."""
    from mediaman.scanner.scheduler import stop_scheduler

    stop_scheduler()
