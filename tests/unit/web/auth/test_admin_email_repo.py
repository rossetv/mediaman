"""Repository helpers for the admin_users.email column."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db.migrations import apply_migrations
from mediaman.web.auth.password_hash import (
    create_user,
    get_user_email,
    list_users,
    set_user_email,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_migrations(c)
    create_user(c, "rossetv", "TestPass!12345", enforce_policy=False)
    return c


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
