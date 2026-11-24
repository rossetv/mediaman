"""APScheduler setup for periodic scanning and library sync.

Imports of apscheduler, mediaman.db, and the service
modules are deferred to start_scheduler so that importing
this module does not drag in heavy dependencies or trigger DB access
at import time — unconditional top-level imports of heavy dependencies are deferred to call time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Module-level scheduler reference. Mutation is guarded by ``_scheduler_lock``;
# in practice ``start_scheduler`` and ``stop_scheduler`` are only ever called
# from the FastAPI lifespan (single-threaded startup / shutdown), but the
# lock makes the contract explicit and protects against a future caller
# that drives the scheduler from a worker thread.  ``BackgroundScheduler``
# is imported under ``TYPE_CHECKING`` so the lazy-import contract in the
# module docstring is preserved — annotations are strings at runtime.
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()

# Track every DB connection opened by background scheduler jobs so they
# can be closed deterministically on shutdown. Without this list each
# ``get_db()`` call from inside an APScheduler worker thread allocates a
# new thread-local SQLite connection that is never closed when the
# scheduler stops, leaking file descriptors and write transactions on
# every reload.
_scheduler_connections: list[sqlite3.Connection] = []
_scheduler_connections_lock = threading.Lock()


# Misfire grace window for every job (in seconds). If the process is
# paused (deploy, host reboot, long GC) for longer than this window the
# job is *dropped* by APScheduler — we'd rather skip a stale fire than
# stack up an hour of catch-up work. One hour matches the cadence of
# ``trigger_pending_searches`` and is comfortably longer than any
# routine restart, so a planned deploy never silently loses work; the
# operator has to be down for >60 minutes before a fire is dropped.
_DEFAULT_MISFIRE_GRACE_SECONDS = 3600


def _track_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    with _scheduler_connections_lock:
        # A worker thread has one connection for all the jobs it runs.
        if conn not in _scheduler_connections:
            _scheduler_connections.append(conn)
    return conn


def _open_db_for_job() -> sqlite3.Connection:
    """Open a thread-local DB connection and remember it for shutdown."""
    from mediaman.db import get_db

    return _track_connection(get_db())


def _close_tracked_connections() -> None:
    """Close every connection registered by background jobs.

    Called from stop_scheduler. Safe to call concurrently with
    in-flight jobs because APScheduler waits for each job to settle
    before signalling completion (we pass ``wait=False`` for fast
    shutdown but the worker threads still drain their current tick).
    Errors closing one connection don't prevent the rest from being
    cleaned up.
    """
    with _scheduler_connections_lock:
        connections = list(_scheduler_connections)
        _scheduler_connections.clear()
    for conn in connections:
        # rationale: shutdown path — close every connection regardless of individual failures.
        try:
            conn.close()
        except sqlite3.ProgrammingError:
            # A connection only its own thread may close, which is when that thread ends.
            logger.debug("scheduler.shutdown.close_db_left_to_its_thread")
        except Exception:
            logger.exception("scheduler.shutdown.close_db_failed")


@dataclass(frozen=True, slots=True)
class ScanSchedule:
    """The validated scan settings: weekly scan time and library-sync interval (0 = off)."""

    scan_day: str
    hour: int
    minute: int
    scan_tz: str
    sync_interval: int


def _weekly_scan_trigger(day_of_week: str, hour: int, minute: int, timezone: str) -> CronTrigger:
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=timezone)


def _add_weekly_scan_job(
    scheduler: BackgroundScheduler, scan_fn: Callable[[], None], trigger: CronTrigger
) -> None:
    scheduler.add_job(
        scan_fn,
        trigger=trigger,
        id="weekly_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )


def _add_library_sync_job(
    scheduler: BackgroundScheduler, sync_fn: Callable[[], None], interval_minutes: int
) -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        sync_fn,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="library_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )


def _register_scan_jobs(
    scheduler: BackgroundScheduler,
    *,
    scan_fn: Callable[[], None],
    day_of_week: str,
    hour: int,
    minute: int,
    timezone: str,
    sync_fn: Callable[[], None] | None,
    sync_interval_minutes: int,
) -> None:
    """Register the weekly scan and the optional library-sync job.

    Called by start_scheduler *while ``_scheduler_lock`` is still
    held* — extracting it does not release the lock, so the
    all-jobs-or-no-jobs registration window is preserved.
    """
    _add_weekly_scan_job(
        scheduler, scan_fn, _weekly_scan_trigger(day_of_week, hour, minute, timezone)
    )
    if sync_fn and sync_interval_minutes > 0:
        _add_library_sync_job(scheduler, sync_fn, sync_interval_minutes)


def reschedule_scan_jobs(
    read_schedule: Callable[[], ScanSchedule],
    *,
    scan_fn: Callable[[], None],
    sync_fn: Callable[[], None],
) -> bool:
    """Bring the running scan jobs in line with the saved schedule.

    A job is re-added only when the live one differs from *read_schedule*'s,
    since re-adding an interval job restarts its countdown; the sync job is
    removed when its interval is 0. Comparing with the live jobs under the lock,
    rather than with what one save changed, means overlapping saves converge on
    the last committed schedule. Returns False and changes nothing when no
    scheduler is running; whatever *read_schedule* raises (``ValueError`` for a
    malformed value) propagates and leaves the current schedule in place.
    """
    with _scheduler_lock:
        running = _scheduler
        if running is None:
            return False
        schedule = read_schedule()
        trigger = _weekly_scan_trigger(
            schedule.scan_day, schedule.hour, schedule.minute, schedule.scan_tz
        )
        scan_job = running.get_job("weekly_scan")
        # CronTrigger has no __eq__; its repr names every field and the timezone.
        if scan_job is None or repr(scan_job.trigger) != repr(trigger):
            _add_weekly_scan_job(running, scan_fn, trigger)
        sync_job = running.get_job("library_sync")
        if schedule.sync_interval == 0:
            if sync_job is not None:
                running.remove_job("library_sync")
        elif sync_job is None or sync_job.trigger.interval != timedelta(
            minutes=schedule.sync_interval
        ):
            _add_library_sync_job(running, sync_fn, schedule.sync_interval)
    return True


def _register_maintenance_jobs(
    scheduler: BackgroundScheduler,
    *,
    secret_key: str,
) -> None:
    """Register the fixed-cadence background maintenance jobs.

    Called by start_scheduler *while ``_scheduler_lock`` is still
    held* — extracting it does not release the lock, so the
    all-jobs-or-no-jobs registration window is preserved.
    """
    from apscheduler.triggers.interval import IntervalTrigger

    from mediaman.services.arr._throttle_persistence import reconcile_stranded_throttle
    from mediaman.services.arr.completion import cleanup_recent_downloads
    from mediaman.services.arr.search_trigger import trigger_pending_searches
    from mediaman.services.watch_catalog import fill_title_meta

    scheduler.add_job(
        lambda: cleanup_recent_downloads(_open_db_for_job()),
        IntervalTrigger(hours=6),
        id="cleanup_recent_downloads",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        lambda: trigger_pending_searches(_open_db_for_job(), secret_key),
        IntervalTrigger(hours=1),
        id="trigger_pending_searches",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )
    scheduler.add_job(
        lambda: fill_title_meta(
            _open_db_for_job(), secret_key, stopped=lambda: _scheduler is not scheduler
        ),
        IntervalTrigger(minutes=1),
        id="fill_title_meta",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )
    # Daily reaper for arr_search_throttle rows whose item has been
    # deleted. arr_search_throttle rows are keyed by media_item_id; when
    # the item is deleted the throttle row becomes a ghost — periodic
    # reaping prevents monotonic table growth because individual deletions
    # don't propagate to the throttle DB.
    scheduler.add_job(
        lambda: reconcile_stranded_throttle(_open_db_for_job()),
        IntervalTrigger(hours=24),
        id="reconcile_stranded_throttle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
    )


def start_scheduler(
    *,
    scan_fn: Callable[[], None],
    day_of_week: str = "mon",
    hour: int = 9,
    minute: int = 0,
    timezone: str = "UTC",
    sync_fn: Callable[[], None] | None = None,
    sync_interval_minutes: int = 30,
    secret_key: str,
) -> BackgroundScheduler:
    """Start a background scheduler with weekly scan and optional library sync.

    Args:
        scan_fn: Zero-argument callable for the weekly scan.
        day_of_week: cron day-of-week string (default ``"mon"``).
        hour: Hour to run (default ``9``).
        minute: Minute to run (default ``0``).
        timezone: IANA timezone name (default ``"UTC"``).
        sync_fn: Optional zero-argument callable for library sync.
        sync_interval_minutes: Minutes between library syncs (default ``30``).
        secret_key: Application secret used to decrypt stored Radarr/Sonarr
            API keys; forwarded to trigger_pending_searches.

    Returns:
        The running BackgroundScheduler instance.

    Notes:
        Every job is registered with ``misfire_grace_time``. Without it
        APScheduler silently *drops* fires that are more than a few
        seconds late — combined with ``coalesce=True`` a long restart
        would erase an entire missed weekly scan. The
        _DEFAULT_MISFIRE_GRACE_SECONDS window means routine
        restarts (deploys, host reboots) still get their fire after the
        process comes back, but a multi-hour outage is treated as
        "skip this tick" rather than letting catch-up work pile up.
    """
    # rationale: every job must be registered inside one ``_scheduler_lock``
    # acquisition — the ``_register_*`` helpers are invoked while the lock is
    # still held so there is never a window where only some jobs exist.
    from apscheduler.schedulers.background import BackgroundScheduler

    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            logger.debug("start_scheduler: scheduler already running, skipping duplicate start")
            return _scheduler
        _scheduler = BackgroundScheduler()
        _register_scan_jobs(
            _scheduler,
            scan_fn=scan_fn,
            day_of_week=day_of_week,
            hour=hour,
            minute=minute,
            timezone=timezone,
            sync_fn=sync_fn,
            sync_interval_minutes=sync_interval_minutes,
        )
        _register_maintenance_jobs(_scheduler, secret_key=secret_key)
        _scheduler.start()
        return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    with _scheduler_lock:
        scheduler_to_stop = _scheduler
        _scheduler = None
    if scheduler_to_stop is not None:
        try:
            scheduler_to_stop.shutdown(wait=False)
        finally:
            # Close DB connections opened by job lambdas regardless of
            # whether shutdown raised, so the connections are never leaked.
            _close_tracked_connections()
