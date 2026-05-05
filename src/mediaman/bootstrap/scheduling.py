"""Back-compat shim — scheduling bootstrap logic now lives in :mod:`mediaman.bootstrap.scan_jobs`.

This module re-exports the public API so existing imports such as::

    from mediaman.bootstrap.scheduling import (
        bootstrap_scheduling,
        shutdown_scheduling,
        _validate_scan_time,
        _validate_scan_day,
        _validate_scan_timezone,
        _validate_sync_interval,
    )
    import mediaman.bootstrap.scheduling as _boot
    _boot._SHUTDOWN_TIMEOUT_SECONDS = 0.2  # test override

continue to work without change.

Validator functions originate in :mod:`mediaman.validators` (where they
can be tested in isolation) and are aliased here under their legacy
underscore-prefixed names so tests need no changes.

``shutdown_scheduling`` is defined here (not re-exported from
``scan_jobs``) so that test monkeypatches of
``_SHUTDOWN_TIMEOUT_SECONDS`` in this module's namespace are respected
by the function — Python function globals are bound to the module where
the function is *defined*, not where it is *imported*.
"""

from __future__ import annotations

import contextlib
import logging
import threading

from mediaman.bootstrap.scan_jobs import (
    _run_library_sync_job,
    _run_scheduled_scan,
    bootstrap_scheduling,
)
from mediaman.validators import (
    validate_scan_day as _validate_scan_day,
)
from mediaman.validators import (
    validate_scan_time as _validate_scan_time,
)
from mediaman.validators import (
    validate_scan_timezone as _validate_scan_timezone,
)
from mediaman.validators import (
    validate_sync_interval as _validate_sync_interval,
)

logger = logging.getLogger("mediaman")

# Bounded wait at shutdown — mirrored from scan_jobs so test
# monkeypatches on ``mediaman.bootstrap.scheduling._SHUTDOWN_TIMEOUT_SECONDS``
# are seen by the :func:`shutdown_scheduling` defined below.
_SHUTDOWN_TIMEOUT_SECONDS = 30

# Expose start_scheduler under this module's namespace so tests that
# monkeypatch ``mediaman.bootstrap.scheduling.start_scheduler`` find
# the name they expect.
with contextlib.suppress(Exception):
    from mediaman.scanner.scheduler import start_scheduler


def shutdown_scheduling() -> None:
    """Stop the APScheduler jobs with a bounded wait.

    Delegates to :mod:`mediaman.scanner.scheduler` and joins for at most
    :data:`_SHUTDOWN_TIMEOUT_SECONDS`. Defined here (not re-exported from
    ``scan_jobs``) so test overrides of ``_SHUTDOWN_TIMEOUT_SECONDS``
    in this module's namespace are respected.

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
    "_validate_scan_day",
    "_validate_scan_time",
    "_validate_scan_timezone",
    "_validate_sync_interval",
    "bootstrap_scheduling",
    "shutdown_scheduling",
    "start_scheduler",
]
