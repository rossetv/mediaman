"""APScheduler setup for periodic scanning and library sync.

Imports of :mod:`apscheduler`, :mod:`mediaman.db`, and the service
modules are deferred to :func:`start_scheduler` so that importing
this module does not drag in heavy dependencies or trigger DB access
at import time — unconditional top-level imports of heavy dependencies are deferred to call time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apscheduler.schedulers.background import BackgroundScheduler

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
    """Register *conn* for explicit close on scheduler shutdown."""
    with _scheduler_connections_lock:
        _scheduler_connections.append(conn)
    return conn


def _open_db_for_job() -> sqlite3.Connection:
    """Open a thread-local DB connection and remember it for shutdown."""
    from mediaman.db import get_db

    return _track_connection(get_db())


def _close_tracked_connections() -> None:
    """Close every connection registered by background jobs.

    Called from :func:`stop_scheduler`. Safe to call concurrently with
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
        except Exception:
            logger.warning("scheduler.shutdown.close_db_failed", exc_info=True)


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
            API keys; forwarded to :func:`trigger_pending_searches`.

    Returns:
        The running :class:`BackgroundScheduler` instance.

    Notes:
        Every job is registered with ``misfire_grace_time``. Without it
        APScheduler silently *drops* fires that are more than a few
        seconds late — combined with ``coalesce=True`` a long restart
        would erase an entire missed weekly scan. The
        :data:`_DEFAULT_MISFIRE_GRACE_SECONDS` window means routine
        restarts (deploys, host reboots) still get their fire after the
        process comes back, but a multi-hour outage is treated as
        "skip this tick" rather than letting catch-up work pile up.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from mediaman.services.arr._throttle_persistence import reconcile_stranded_throttle
    from mediaman.services.arr.completion import cleanup_recent_downloads
    from mediaman.services.arr.search_trigger import trigger_pending_searches

    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            logger.debug("start_scheduler: scheduler already running, skipping duplicate start")
            return _scheduler
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(
            scan_fn,
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=timezone,
            ),
            id="weekly_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
        )
        if sync_fn and sync_interval_minutes > 0:
            _scheduler.add_job(
                sync_fn,
                trigger=IntervalTrigger(minutes=sync_interval_minutes),
                id="library_sync",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
            )
        _scheduler.add_job(
            lambda: cleanup_recent_downloads(_open_db_for_job()),
            IntervalTrigger(hours=6),
            id="cleanup_recent_downloads",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
        )
        _scheduler.add_job(
            lambda: trigger_pending_searches(_open_db_for_job(), secret_key),
            IntervalTrigger(hours=1),
            id="trigger_pending_searches",
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
        _scheduler.add_job(
            lambda: reconcile_stranded_throttle(_open_db_for_job()),
            IntervalTrigger(hours=24),
            id="reconcile_stranded_throttle",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_DEFAULT_MISFIRE_GRACE_SECONDS,
        )
        _scheduler.start()
        return _scheduler


def stop_scheduler() -> None:
    """Shut down the running scheduler and close any tracked DB connections."""
    global _scheduler
    with _scheduler_lock:
        scheduler_to_stop = _scheduler
        _scheduler = None
    if scheduler_to_stop is not None:
        try:
            scheduler_to_stop.shutdown(wait=False)
        finally:
            # Close DB connections opened by job lambdas regardless of
            # whether shutdown raised — leaking the connections is the
            # finding we're fixing.
            _close_tracked_connections()
