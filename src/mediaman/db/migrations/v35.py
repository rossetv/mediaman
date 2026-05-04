"""Migration v35: schema version bump for v1 ciphertext sunset.

v1 AES ciphertexts (plain SHA-256 of the secret key, no prefix byte, no AAD)
are no longer supported by :func:`mediaman.crypto.decrypt_value`. This
migration bumps the schema version to 35 to signal that the database requires
the crypto-level migration to have run.

The actual re-encryption of legacy rows is performed by
:func:`mediaman.crypto.migrate_legacy_ciphertexts`, which is called from the
crypto bootstrap step after the AES canary check passes. That function
requires the ``MEDIAMAN_SECRET_KEY`` which is not available in
``apply_migrations``, so the two-step approach is intentional.

Databases that have not yet run this migration (version < 35) must call
:func:`mediaman.crypto.migrate_legacy_ciphertexts` before any code that
calls :func:`mediaman.crypto.decrypt_value` on stored settings rows.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """No-op schema step — crypto migration runs separately via bootstrap_crypto."""
    # The actual work (re-encrypting v1 and no-AAD v2 rows) is done by
    # migrate_legacy_ciphertexts() in bootstrap_crypto.py once the canary
    # check confirms the secret key is correct. Nothing to do at the pure
    # schema level.
    pass
