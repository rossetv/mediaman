"""AES-canary bootstrap step (R23).

Runs the key-mismatch canary against the live DB. Sets
``app.state.canary_ok`` so the scheduling step can refuse to start when
the canary fails (every scheduled scan would otherwise silently fail
under the wrong key).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from mediaman.config import Config

logger = logging.getLogger("mediaman")


def bootstrap_crypto(app: FastAPI, config: Config) -> None:
    """Run the AES canary check and stash the result on ``app.state``.

    Does NOT refuse to start on a mismatch — the admin must still be
    able to log in to re-enter secrets. The downstream
    :func:`bootstrap_scheduling` reads the flag and refuses to start the
    scheduler when the canary failed.

    The canary state is initialised to ``False`` and only flipped to
    ``True`` after :func:`canary_check` returns a positive result. An
    import failure or any other exception leaves the flag at its
    fail-closed default — without this, a partial import (e.g. a missing
    ``cryptography`` extension) would slip through with the optimistic
    ``True`` and the scheduler would gleefully fire scans against
    settings it cannot decrypt.
    """
    canary_ok = False
    try:
        from mediaman.crypto import canary_check, migrate_legacy_ciphertexts

        canary_ok = bool(canary_check(app.state.db, config.secret_key))
        if canary_ok:
            # Migration v35: re-encrypt any legacy v1 or no-AAD v2 settings
            # ciphertexts to v2+AAD. Safe to call on every startup — already-
            # migrated rows are skipped. Errors are logged but do not abort
            # startup: a failed re-encrypt leaves the row readable via the
            # no-AAD fallback in decrypt_value until the next boot.
            try:
                n = migrate_legacy_ciphertexts(app.state.db, config.secret_key)
                if n:
                    logger.info("bootstrap_crypto: migrated %d legacy settings row(s) to v2+AAD", n)
            except Exception:
                logger.exception("bootstrap_crypto: migrate_legacy_ciphertexts failed (non-fatal)")
    except Exception:
        logger.exception("AES canary check failed unexpectedly")
        canary_ok = False
    app.state.canary_ok = canary_ok
