"""Migration runner for the mediaman database — squashed baseline.

On 2026-05-04 we squashed migrations 1–35 into the baseline _SCHEMA constant
in :mod:`mediaman.db.schema_definition`. Pre-cutover databases (user_version
between 1 and 34 inclusive) must transit through release 1.9.0 — the last
release that still contained the per-version migration files v01–v35 — before
upgrading to this version.

Invariants
----------
* ``PRAGMA user_version`` is the authoritative version counter.
* Fresh databases (user_version == 0) receive the full _SCHEMA DDL and are
  stamped directly at CUTOVER_VERSION.
* Databases already at CUTOVER_VERSION are a no-op (already up to date).
* Databases at 0 < user_version < CUTOVER_VERSION raise
  :class:`SchemaTooOldError` — the required migration files no longer exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.schema_definition import _SCHEMA, CUTOVER_VERSION, DB_SCHEMA_VERSION


class SchemaTooOldError(RuntimeError):
    """Raised when the database predates the migration squash cutover.

    The caller must run release 1.9.0 to apply the missing migrations before
    upgrading to this version.
    """


class SchemaFromFutureError(RuntimeError):
    """Raised when the database was written by a newer build of mediaman.

    A version number above :data:`DB_SCHEMA_VERSION` means the database has
    been opened by a newer release that may have added columns, triggers or
    constraints this code does not know about. Refusing to start is safer
    than silently corrupting data on the first write.
    """


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance *conn* to :data:`~mediaman.db.schema_definition.DB_SCHEMA_VERSION`.

    Behaviour by current ``PRAGMA user_version``:

    * **0** — fresh database; apply the full _SCHEMA DDL and stamp at
      CUTOVER_VERSION.
    * **CUTOVER_VERSION .. DB_SCHEMA_VERSION** — already up to date; return.
    * **1 .. CUTOVER_VERSION - 1** — pre-squash database; raise
      :class:`SchemaTooOldError` with an actionable message.
    * **> DB_SCHEMA_VERSION** — database came from a newer build; raise
      :class:`SchemaFromFutureError` rather than silently accepting it.
    """
    current_version: int = conn.execute("PRAGMA user_version").fetchone()[0]

    if current_version == 0:
        # Brand-new database — apply the baseline schema and stamp it.
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={CUTOVER_VERSION}")
        conn.commit()
        return

    if 0 < current_version < CUTOVER_VERSION:
        raise SchemaTooOldError(
            f"Database is at version {current_version} which predates the "
            f"migration cutover ({CUTOVER_VERSION}). Upgrade through release "
            f"1.9.0 first, then this version."
        )

    if current_version > DB_SCHEMA_VERSION:
        raise SchemaFromFutureError(
            f"Database is at version {current_version}, newer than this "
            f"build supports ({DB_SCHEMA_VERSION}). The database was likely "
            f"opened by a newer release of mediaman; downgrading is not "
            f"supported. Restore a backup compatible with this version or "
            f"upgrade mediaman."
        )

    # CUTOVER_VERSION <= current_version <= DB_SCHEMA_VERSION — nothing to do.
