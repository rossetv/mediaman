"""Repository helpers for the admin_users.email column."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import init_db
from mediaman.web.auth.password_hash import (
    create_user,
    get_user_email,
    list_users,
    set_user_email,
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = init_db(str(tmp_path / "mediaman.db"))
    create_user(c, "rossetv", "TestPass!12345", enforce_policy=False)
    yield c
    c.close()


def test_get_user_email_returns_none_when_unset(conn: sqlite3.Connection) -> None:
    assert get_user_email(conn, "rossetv") is None


def test_get_user_email_returns_none_for_unknown_user(conn: sqlite3.Connection) -> None:
    assert get_user_email(conn, "ghost") is None


def test_set_user_email_round_trip(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "admin@example.com")
    assert get_user_email(conn, "rossetv") == "admin@example.com"


def test_set_user_email_normalises_whitespace(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "  admin@example.com  ")
    assert get_user_email(conn, "rossetv") == "admin@example.com"


def test_set_user_email_none_clears_value(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "admin@example.com")
    set_user_email(conn, "rossetv", None)
    assert get_user_email(conn, "rossetv") is None


def test_set_user_email_empty_string_clears_value(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "admin@example.com")
    set_user_email(conn, "rossetv", "")
    assert get_user_email(conn, "rossetv") is None


def test_set_user_email_rejects_invalid_address(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="Invalid email address"):
        set_user_email(conn, "rossetv", "rossetv")


def test_set_user_email_rejects_embedded_whitespace(conn: sqlite3.Connection) -> None:
    """Whitespace inside the address (after stripping) is rejected.

    Routes the validator's whitespace rule through ``set_user_email``
    so the function's stripping step cannot accidentally smuggle an
    invalid address into the column.
    """
    with pytest.raises(ValueError, match="Invalid email address"):
        set_user_email(conn, "rossetv", "  ad min@example.com  ")
    assert get_user_email(conn, "rossetv") is None


def test_set_user_email_unknown_user_is_noop(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "ghost", "ghost@example.com")
    assert get_user_email(conn, "ghost") is None


def test_list_users_includes_email_field(conn: sqlite3.Connection) -> None:
    set_user_email(conn, "rossetv", "admin@example.com")
    users = list_users(conn)
    assert len(users) == 1
    assert users[0]["username"] == "rossetv"
    assert users[0]["email"] == "admin@example.com"


def test_list_users_email_is_none_when_unset(conn: sqlite3.Connection) -> None:
    users = list_users(conn)
    assert users[0]["email"] is None


class TestSetUserEmailAuditInTransaction:
    """Verify the audit-in-transaction path introduced by the audit_actor kwarg."""

    def test_writes_audit_row_when_actor_supplied(self, conn: sqlite3.Connection) -> None:
        """set_user_email with audit_actor writes a sec:user.email_updated row."""
        set_user_email(
            conn, "rossetv", "ops@example.com", audit_actor="rossetv", audit_ip="1.2.3.4"
        )
        row = conn.execute(
            "SELECT action, actor, detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert row["actor"] == "rossetv"
        assert '"cleared":false' in row["detail"]
        assert get_user_email(conn, "rossetv") == "ops@example.com"

    def test_clears_audit_row_has_cleared_true(self, conn: sqlite3.Connection) -> None:
        """Clearing an email writes a detail with cleared:true."""
        set_user_email(conn, "rossetv", "ops@example.com")
        set_user_email(conn, "rossetv", None, audit_actor="rossetv", audit_ip="1.2.3.4")
        row = conn.execute(
            "SELECT detail FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is not None
        assert '"cleared":true' in row["detail"]

    def test_no_audit_row_without_actor(self, conn: sqlite3.Connection) -> None:
        """set_user_email without audit_actor writes no audit row."""
        set_user_email(conn, "rossetv", "ops@example.com")
        row = conn.execute(
            "SELECT * FROM audit_log WHERE action = 'sec:user.email_updated'"
        ).fetchone()
        assert row is None

    def test_audit_failure_rolls_back_email_change(
        self, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When security_event_or_raise raises, the UPDATE rolls back."""

        def _boom(c, *, event, actor, ip, detail):
            raise sqlite3.OperationalError("audit table gone")

        monkeypatch.setattr("mediaman.core.audit.security_event_or_raise", _boom)

        with pytest.raises(sqlite3.OperationalError, match="audit table gone"):
            set_user_email(
                conn,
                "rossetv",
                "attacker@evil.test",
                audit_actor="rossetv",
                audit_ip="1.2.3.4",
            )

        # Email must not have changed.
        assert get_user_email(conn, "rossetv") is None
