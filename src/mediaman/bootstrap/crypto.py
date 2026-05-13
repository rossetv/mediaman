"""AES canary preflight.

Owns the second step of startup. After the DB is open, validate the
``MEDIAMAN_SECRET_KEY`` against the persisted canary so a key mismatch
refuses to spawn background jobs that would silently fail. The canary
verdict is stashed on ``app.state.canary_ok``; downstream
:func:`mediaman.bootstrap.scan_jobs.bootstrap_scheduling` reads the flag
and refuses to start the scheduler when it is false.

The web UI remains accessible regardless, so an admin can still log in
and re-enter the secret when a mismatch is detected.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from mediaman.config import Config

logger = logging.getLogger(__name__)


def bootstrap_crypto(app: FastAPI, config: Config) -> None:
    """Run the AES canary check and stash the result on ``app.state``.

    Does NOT refuse to start on a mismatch — the admin must still be
    able to log in to re-enter secrets. The downstream
    :func:`bootstrap_scheduling` reads the flag and refuses to start the
    scheduler when the canary failed.

    The canary state is initialised to ``False`` and only flipped to
    ``True`` after :func:`is_canary_valid` returns a positive result. A
    crypto or DB failure leaves the flag at its fail-closed default.
    ``ImportError`` / ``ModuleNotFoundError`` are intentionally NOT caught
    here — a missing module is a deployment bug that must crash bootstrap
    immediately rather than masquerade as a silent key-mismatch outage
    (see incident c089474: 13-day scheduler outage caused by a stale import
    that was swallowed as a false canary failure).
    """
    canary_ok = False
    try:
        from mediaman.core.audit import security_event
        from mediaman.crypto import is_canary_valid

        db = app.state.db

        def _on_canary_failure(reason: str) -> None:
            """Best-effort audit-log a canary failure.

            The canary fires before the audit table is guaranteed to exist on
            fresh-DB bootstrap, so any failure in the audit path is logged and
            swallowed — the security verdict (False) is what matters; the audit
            row is the cherry on top.
            """
            try:
                security_event(
                    db,
                    event="aes.canary_failed",
                    actor="",
                    ip="",
                    detail={"reason": reason},
                )
            except sqlite3.DatabaseError:  # pragma: no cover — best-effort audit write; DB errors must not override the security verdict
                logger.exception("aes.canary_failed audit write failed reason=%s", reason)

        canary_ok = bool(is_canary_valid(db, config.secret_key, on_failure=_on_canary_failure))
    # §6.4 site 4 (cold-start): surface crypto/DB failures as canary_ok=False
    # so the operator UI can signal the mismatch without crashing the web server.
    # ImportError / ModuleNotFoundError are deliberately re-raised — a missing
    # module is a deployment bug that must surface immediately (c089474).
    except Exception as exc:
        if isinstance(exc, ImportError):
            raise
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok


__all__ = ["bootstrap_crypto"]
