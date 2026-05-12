"""AES canary preflight + one-shot legacy-ciphertext migration.

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
    ``True`` after :func:`is_canary_valid` returns a positive result. An
    import failure or any other exception leaves the flag at its
    fail-closed default — without this, a partial import (e.g. a missing
    ``cryptography`` extension) would slip through with the optimistic
    ``True`` and the scheduler would gleefully fire scans against
    settings it cannot decrypt.
    """
    canary_ok = False
    try:
        from mediaman.core.audit import security_event
        from mediaman.crypto import is_canary_valid, migrate_legacy_ciphertexts

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
            except Exception:  # pragma: no cover
                logger.exception("aes.canary_failed audit write failed reason=%s", reason)

        def _on_migration_complete(migrated_count: int) -> None:
            """Best-effort audit-log after a successful v35 migration commit."""
            try:
                security_event(
                    db,
                    event="aes.v35_migration_complete",
                    actor="",
                    ip="",
                    detail={"migrated_count": migrated_count},
                )
            except Exception:  # pragma: no cover
                logger.exception("aes.v35_migration_complete audit write failed")

        canary_ok = bool(is_canary_valid(db, config.secret_key, on_failure=_on_canary_failure))
        if canary_ok:
            # Migration v35: re-encrypt any legacy v1 or no-AAD v2 settings
            # ciphertexts to v2+AAD. Safe to call on every startup —
            # already-migrated rows are skipped. Errors are logged but do
            # not abort startup.
            try:
                n = migrate_legacy_ciphertexts(
                    db, config.secret_key, on_complete=_on_migration_complete
                )
                if n:
                    logger.info("bootstrap_crypto: migrated %d legacy settings row(s) to v2+AAD", n)
            except Exception:
                logger.exception("bootstrap_crypto: migrate_legacy_ciphertexts failed (non-fatal)")
    except Exception:
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok


__all__ = ["bootstrap_crypto"]
