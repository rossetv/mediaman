"""Unit tests for mediaman.auth.audit.log_audit."""

import sqlite3
from datetime import datetime, timezone

from mediaman.auth.audit import log_audit


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_item_id TEXT,
            action TEXT,
            detail TEXT,
            space_reclaimed_bytes INTEGER,
            created_at TEXT
        )
    """)
    return conn


class TestLogAudit:
    def test_happy_path_insert(self):
        conn = _make_conn()
        log_audit(conn, "item-1", "deleted", "Deleted by admin")
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row is not None
        assert row["media_item_id"] == "item-1"
        assert row["action"] == "deleted"
        assert row["detail"] == "Deleted by admin"
        assert row["space_reclaimed_bytes"] is None

    def test_space_bytes_included(self):
        conn = _make_conn()
        log_audit(conn, "item-2", "deleted", "Deleted with size", space_bytes=1_234_567)
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["space_reclaimed_bytes"] == 1_234_567

    def test_space_bytes_none_omitted(self):
        conn = _make_conn()
        log_audit(conn, "item-3", "snoozed", "Kept for 7d", space_bytes=None)
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["space_reclaimed_bytes"] is None

    def test_timestamp_is_utc_iso(self):
        conn = _make_conn()
        before = datetime.now(timezone.utc).isoformat()
        log_audit(conn, "item-4", "test_action", "detail")
        conn.commit()
        after = datetime.now(timezone.utc).isoformat()

        row = conn.execute("SELECT created_at FROM audit_log").fetchone()
        ts = row["created_at"]
        assert ts >= before
        assert ts <= after
        # Must be parseable as an ISO datetime
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_does_not_commit(self):
        """log_audit must not commit — callers manage their own transactions."""
        conn = _make_conn()
        log_audit(conn, "item-5", "test", "no commit")
        # Without an explicit commit, another connection should not see the row.
        # We can't check across a shared in-memory DB easily, but we verify
        # the row IS visible within the same connection (write happened).
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row is not None
        # Rollback should revert it.
        conn.rollback()
        row_after = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row_after is None
