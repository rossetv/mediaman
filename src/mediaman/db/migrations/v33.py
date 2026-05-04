"""Migration v33: add tamper-evidence triggers to ``audit_log``.

The audit log is the operator's primary forensic surface.  Two triggers raise
an SQLite error on any attempt to UPDATE or DELETE an existing row.  INSERT
remains unrestricted so the application code keeps writing new rows normally.

The triggers are not a security boundary — anyone with DB-file write access
can drop the trigger first.  They are a tamper-EVIDENCE measure: dropping the
trigger shows up in ``sqlite_master`` and is visible to any operator who runs
``.schema``.

Existing triggers (e.g. from a fresh-DB install that included them in
``_SCHEMA``) are dropped and recreated so the trigger body is always
up-to-date.

Guarded: returns immediately if ``audit_log`` does not exist.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _table_exists


def apply(conn: sqlite3.Connection) -> None:
    """Drop and recreate tamper-evidence triggers on ``audit_log``."""
    if not _table_exists(conn, "audit_log"):
        return
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    conn.execute(
        "CREATE TRIGGER audit_log_no_update "
        "BEFORE UPDATE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log rows are append-only'); END"
    )
    conn.execute(
        "CREATE TRIGGER audit_log_no_delete "
        "BEFORE DELETE ON audit_log "
        "BEGIN SELECT RAISE(ABORT, 'audit_log rows are append-only'); END"
    )
