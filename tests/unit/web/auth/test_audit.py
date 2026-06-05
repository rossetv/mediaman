"""Unit tests for mediaman.core.audit.log_audit and security_event helpers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from mediaman.core.audit import log_audit, security_event, security_event_or_raise
from mediaman.db import init_db


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """Properly configured DB via init_db for audit helper tests."""
    c = init_db(str(tmp_path / "audit_test.db"))
    yield c
    c.close()


class TestLogAudit:
    def test_happy_path_insert(self, conn):
        log_audit(conn, "item-1", "deleted", "Deleted by admin")
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row is not None
        assert row["media_item_id"] == "item-1"
        assert row["action"] == "deleted"
        assert row["detail"] == "Deleted by admin"
        assert row["space_reclaimed_bytes"] is None

    def test_space_bytes_included(self, conn):
        log_audit(conn, "item-2", "deleted", "Deleted with size", space_bytes=1_234_567)
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["space_reclaimed_bytes"] == 1_234_567

    def test_space_bytes_none_omitted(self, conn):
        log_audit(conn, "item-3", "snoozed", "Kept for 7d", space_bytes=None)
        conn.commit()

        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["space_reclaimed_bytes"] is None

    def test_timestamp_is_utc_iso(self, conn):
        before = datetime.now(UTC).isoformat()
        log_audit(conn, "item-4", "test_action", "detail")
        conn.commit()
        after = datetime.now(UTC).isoformat()

        row = conn.execute("SELECT created_at FROM audit_log").fetchone()
        ts = row["created_at"]
        assert ts >= before
        assert ts <= after
        # Must be parseable as an ISO datetime
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_does_not_commit(self, conn):
        """log_audit must not commit — callers manage their own transactions."""
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


class TestSecurityEvent:
    def test_writes_sec_prefixed_action(self, conn):
        security_event(conn, event="login.success", actor="alice", ip="127.0.0.1")
        row = conn.execute("SELECT action, detail, media_item_id FROM audit_log").fetchone()
        assert row["action"] == "sec:login.success"
        assert "actor=alice" in row["detail"]
        assert "ip=127.0.0.1" in row["detail"]
        assert row["media_item_id"] == "_security"

    def test_swallows_db_failure(self, conn):
        """security_event must NEVER raise — best-effort writes."""
        conn.execute("DROP TABLE audit_log")
        # No exception even though the INSERT will blow up.
        security_event(conn, event="login.failed", actor="alice")

    def test_dict_detail_is_json_encoded(self, conn):
        security_event(
            conn,
            event="settings.write",
            actor="alice",
            ip="127.0.0.1",
            detail={"keys": ["plex_url", "base_url"]},
        )
        row = conn.execute("SELECT detail FROM audit_log").fetchone()
        assert "plex_url" in row["detail"]
        assert "base_url" in row["detail"]


class TestSecurityEventOrRaise:
    def test_writes_to_audit_log(self, conn):
        conn.execute("BEGIN")
        security_event_or_raise(
            conn,
            event="user.created",
            actor="admin",
            ip="127.0.0.1",
            detail={"new_username": "bob"},
        )
        conn.execute("COMMIT")
        row = conn.execute("SELECT action, detail FROM audit_log").fetchone()
        assert row["action"] == "sec:user.created"
        assert "bob" in row["detail"]

    def test_does_not_commit(self, conn):
        """The caller commits; this helper must NOT auto-commit."""
        conn.execute("BEGIN")
        security_event_or_raise(
            conn,
            event="user.deleted",
            actor="admin",
            ip="127.0.0.1",
        )
        # Roll back — the row must vanish.
        conn.rollback()
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row is None

    def test_raises_on_db_error(self, conn):
        """security_event_or_raise MUST propagate so the caller's wider
        transaction can be rolled back."""
        conn.execute("DROP TABLE audit_log")
        with pytest.raises(sqlite3.OperationalError):
            security_event_or_raise(conn, event="user.created", actor="admin")


class TestActorColumn:
    """Domain 05 HIGH: ``actor`` is a first-class queryable column.

    Previously the actor was only embedded in the security-event
    ``detail`` body via ``actor=alice`` and ``log_audit`` rows had no
    actor at all — every scanner-driven row read "scheduled by scan
    engine" with no link back to the session that triggered it.
    """

    def test_log_audit_writes_actor_column(self, conn):
        log_audit(conn, "item-1", "deleted", "Manually deleted", actor="alice")
        conn.commit()
        row = conn.execute("SELECT actor FROM audit_log").fetchone()
        assert row["actor"] == "alice"

    def test_log_audit_actor_defaults_to_null(self, conn):
        """No actor → autonomous (scanner) action → NULL in the column."""
        log_audit(conn, "item-1", "scheduled_deletion", "Auto-scheduled by scanner")
        conn.commit()
        row = conn.execute("SELECT actor FROM audit_log").fetchone()
        assert row["actor"] is None

    def test_log_audit_actor_with_space_bytes(self, conn):
        """Both kwargs together must land in the same row."""
        log_audit(
            conn,
            "item-7",
            "deleted",
            "Manual delete with size",
            space_bytes=999,
            actor="bob",
        )
        conn.commit()
        row = conn.execute("SELECT actor, space_reclaimed_bytes FROM audit_log").fetchone()
        assert row["actor"] == "bob"
        assert row["space_reclaimed_bytes"] == 999

    def test_security_event_writes_actor_column(self, conn):
        """``actor=alice`` lives in ``detail`` AND in the ``actor`` column."""
        security_event(conn, event="login.success", actor="alice", ip="1.2.3.4")
        row = conn.execute("SELECT actor, detail FROM audit_log").fetchone()
        assert row["actor"] == "alice"
        # Existing convention preserved: still grep-able in detail.
        assert "actor=alice" in row["detail"]

    def test_security_event_empty_actor_stored_as_empty_string(self, conn):
        """Empty-string default is preserved — distinct from NULL."""
        security_event(conn, event="login.failed", ip="1.2.3.4")
        row = conn.execute("SELECT actor FROM audit_log").fetchone()
        # The column gets the literal default value passed in: "".
        assert row["actor"] == ""

    def test_security_event_or_raise_writes_actor_column(self, conn):
        conn.execute("BEGIN")
        security_event_or_raise(
            conn,
            event="user.created",
            actor="admin",
            ip="127.0.0.1",
            detail={"new_username": "bob"},
        )
        conn.execute("COMMIT")
        row = conn.execute("SELECT actor, detail FROM audit_log").fetchone()
        assert row["actor"] == "admin"
        assert "actor=admin" in row["detail"]


class TestSecurityBodyInjectionStripped:
    """M6: CR/LF/NUL in free-form audit fields must be stripped so a caller
    cannot forge log-line boundaries or spoof ``actor=``/``ip=`` fields a
    later log-grep would attribute to the system.
    """

    def test_newline_in_actor_stripped(self):
        from mediaman.core.audit import _format_security_body

        body = _format_security_body("admin\nactor=root", "1.2.3.4", None)
        assert "\n" not in body
        assert "\r" not in body
        # The injected text is collapsed onto the same line; no second
        # forged record can be parsed out.
        assert body.count("\n") == 0

    def test_crlf_in_string_detail_stripped(self):
        from mediaman.core.audit import _format_security_body

        body = _format_security_body("alice", "1.2.3.4", "ok\r\nactor=root ip=9.9.9.9")
        assert "\r" not in body and "\n" not in body

    def test_nul_in_ip_stripped(self):
        from mediaman.core.audit import _format_security_body

        body = _format_security_body("alice", "1.2.3.4\x00evil", None)
        assert "\x00" not in body

    def test_clean_fields_unchanged(self):
        from mediaman.core.audit import _format_security_body

        body = _format_security_body("alice", "1.2.3.4", "all good")
        assert body == "actor=alice ip=1.2.3.4 all good"

    def test_injection_via_security_event_does_not_split_row(self, conn):
        security_event(
            conn,
            event="login.failed",
            actor="evil\nactor=admin",
            ip="1.2.3.4",
        )
        rows = conn.execute("SELECT detail FROM audit_log").fetchall()
        assert len(rows) == 1
        assert "\n" not in rows[0]["detail"]
