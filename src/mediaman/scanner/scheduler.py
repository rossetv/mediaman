"""APScheduler setup for periodic scanning and library sync.

Imports of :mod:`apscheduler`, :mod:`mediaman.db`, and the service
modules are deferred to :func:`start_scheduler` so that importing
this module does not drag in heavy dependencies or trigger DB access
at import time (M-finding: unconditional top-level imports).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("mediaman")

_scheduler = None


def start_scheduler(
    *,
    scan_fn,
    day_of_week: str = "mon",
    hour: int = 9,
    minute: int = 0,
    timezone: str = "UTC",
    sync_fn=None,
    sync_interval_minutes: int = 30,
):
    """Start a background scheduler with weekly scan and optional library sync.

    Args:
        scan_fn: Zero-argument callable for the weekly scan.
        day_of_week: cron day-of-week string (default ``"mon"``).
        hour: Hour to run (default ``9``).
        minute: Minute to run (default ``0``).
        timezone: IANA timezone name (default ``"UTC"``).
        sync_fn: Optional zero-argument callable for library sync.
        sync_interval_minutes: Minutes between library syncs (default ``30``).

    Returns:
        The running :class:`BackgroundScheduler` instance.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from mediaman.db import get_db
    from mediaman.services.arr_completion import cleanup_recent_downloads
    from mediaman.services.arr_search_trigger import trigger_pending_searches

    global _scheduler
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
    )
    if sync_fn and sync_interval_minutes > 0:
        _scheduler.add_job(
            sync_fn,
            trigger=IntervalTrigger(minutes=sync_interval_minutes),
            id="library_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    _scheduler.add_job(
        lambda: cleanup_recent_downloads(get_db()),
        IntervalTrigger(hours=6),
        id="cleanup_recent_downloads",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        lambda: trigger_pending_searches(get_db()),
        IntervalTrigger(hours=1),
        id="trigger_pending_searches",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    """Shut down the running scheduler, if any."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
