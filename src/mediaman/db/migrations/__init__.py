"""Incremental migration runner for the mediaman database.

This module owns :func:`apply_migrations`, which advances a database from
any prior version up to :data:`~mediaman.db.schema_definition.DB_SCHEMA_VERSION`
one step at a time, never skipping a version.

Invariants
----------
* ``PRAGMA user_version`` is the authoritative version counter.
* Each migration runs inside its own ``BEGIN`` / ``COMMIT`` block.  A
  ``ROLLBACK`` is issued on any exception so the database is never left in a
  half-migrated state.
* The version is updated *inside* the same transaction as the migration body,
  so a crash mid-migration leaves ``user_version`` at the previous value and
  the migration will be retried on the next startup.
* Version 1 is special: the full ``_SCHEMA`` DDL is applied via
  ``executescript`` (which handles multi-statement SQL) and then committed
  immediately outside the normal ``_run_migration`` helper.
* ``current_version`` is read once at the start of the function.  Every
  ``if current_version < N`` guard therefore reflects the state at startup,
  not after any individual migration step.  This is intentional: a fresh
  database (version 0) will have all guards evaluate to ``True`` and all
  migrations will run in order.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations import (
    v02,
    v03,
    v04,
    v05,
    v06,
    v07,
    v08,
    v09,
    v10,
    v11,
    v12,
    v13,
    v14,
    v15,
    v16,
    v17,
    v18,
    v19,
    v20,
    v21,
    v22,
    v23,
    v24,
    v25,
    v26,
    v27,
    v28,
    v29,
    v30,
    v31,
    v32,
    v33,
    v34,
    v35,
)
from mediaman.db.schema_definition import _SCHEMA


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Run every migration block against *conn* up to :data:`DB_SCHEMA_VERSION`."""

    current_version = conn.execute("PRAGMA user_version").fetchone()[0]

    def _run_migration(target_version: int, module) -> None:
        conn.execute("BEGIN")
        try:
            module.apply(conn)
            conn.execute(f"PRAGMA user_version={target_version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if current_version < 1:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=1")
        conn.commit()

    if current_version < 2:
        _run_migration(2, v02)

    if current_version < 3:
        _run_migration(3, v03)

    if current_version < 4:
        _run_migration(4, v04)

    if current_version < 5:
        _run_migration(5, v05)

    if current_version < 6:
        _run_migration(6, v06)

    if current_version < 7:
        _run_migration(7, v07)

    if current_version < 8:
        _run_migration(8, v08)

    if current_version < 9:
        _run_migration(9, v09)

    if current_version < 10:
        _run_migration(10, v10)

    if current_version < 11:
        _run_migration(11, v11)

    if current_version < 12:
        _run_migration(12, v12)

    if current_version < 13:
        _run_migration(13, v13)

    if current_version < 14:
        _run_migration(14, v14)

    if current_version < 15:
        _run_migration(15, v15)

    if current_version < 16:
        _run_migration(16, v16)

    if current_version < 17:
        _run_migration(17, v17)

    if current_version < 18:
        _run_migration(18, v18)

    if current_version < 19:
        _run_migration(19, v19)

    if current_version < 20:
        _run_migration(20, v20)

    if current_version < 21:
        _run_migration(21, v21)

    if current_version < 22:
        _run_migration(22, v22)

    if current_version < 23:
        _run_migration(23, v23)

    if current_version < 24:
        _run_migration(24, v24)

    if current_version < 25:
        _run_migration(25, v25)

    if current_version < 26:
        _run_migration(26, v26)

    if current_version < 27:
        _run_migration(27, v27)

    if current_version < 28:
        _run_migration(28, v28)

    if current_version < 29:
        _run_migration(29, v29)

    if current_version < 30:
        _run_migration(30, v30)

    if current_version < 31:
        _run_migration(31, v31)

    if current_version < 32:
        _run_migration(32, v32)

    if current_version < 33:
        _run_migration(33, v33)

    if current_version < 34:
        _run_migration(34, v34)

    if current_version < 35:
        _run_migration(35, v35)
