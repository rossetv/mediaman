"""Migration v35 — AES v1 ciphertext sunset marker.

A no-op at the SQL level. The actual work — re-encrypting any
``v1`` (no-AAD) ciphertext rows in ``settings`` — is done by
:func:`mediaman.crypto.aes.migrate_legacy_ciphertexts`, which runs
from :mod:`mediaman.bootstrap.crypto` once the canary check confirms
the secret key is correct.

Bumping ``user_version`` to 35 here is the schema-level signal that
the database has been opened by code which expects the crypto-level
migration to have run.  Two-phase split is intentional: the SQL
migration must run before the connection serves any other request,
but ``MEDIAMAN_SECRET_KEY`` is not in scope inside :func:`apply_migrations`.

Pre-cutover databases at v34 (the squash baseline) advance through
this migration on first boot of the post-squash release.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """No-op DDL step; see module docstring for the rationale."""
    # No SQL changes — the version bump itself is the signal. The
    # ``conn`` argument exists for symmetry with future post-cutover
    # migrations that may need to alter the schema.
    _ = conn  # silence unused-arg lint without renaming the parameter
