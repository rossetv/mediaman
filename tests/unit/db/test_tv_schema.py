"""Tests for the table of TVs: what ends a pairing, whoever makes the change."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def paired(conn):
    for name in ("amy", "bob"):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'h', '2020')",
            (name,),
        )
        conn.execute(
            "INSERT INTO tvs (owner_id, kind, name, token_hash, created_at)"
            " SELECT id, 'page', username, 'hash-' || username, '2020' FROM admin_users"
            " WHERE username = ?",
            (name,),
        )
    conn.execute(
        "INSERT INTO tvs (kind, name, address, created_at)"
        " VALUES ('older', 'old', '192.0.2.7', '2020')"
    )
    conn.commit()
    return conn


def _names(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT name FROM tvs ORDER BY id")]


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM admin_users WHERE username = 'amy'",
        "UPDATE admin_users SET password_hash = 'other' WHERE username = 'amy'",
        "UPDATE admin_users SET must_change_password = 1 WHERE username = 'amy'",
    ],
)
def test_a_persons_tvs_alone_end_with_the_person_and_with_the_password(paired, change):
    paired.execute(change)
    assert _names(paired) == ["bob", "old"]


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE admin_users SET email = 'amy@example.test' WHERE username = 'amy'",
        "UPDATE admin_users SET must_change_password = 0 WHERE username = 'amy'",
        "UPDATE admin_users SET password_hash = password_hash WHERE username = 'amy'",
    ],
)
def test_other_changes_of_the_person_keep_the_tvs(paired, change):
    paired.execute(change)
    assert _names(paired) == ["amy", "bob", "old"]


@pytest.mark.parametrize(
    "columns",
    [
        "(kind, name, token_hash, created_at) VALUES ('page', 'n', 'h', '2020')",
        "(kind, name, owner_id, created_at) VALUES ('page', 'n', 1, '2020')",
        "(kind, name, owner_id, token_hash, created_at) VALUES ('page', 'n', 2, 'hash-amy', '2020')",
        "(kind, name, created_at) VALUES ('older', 'n', '2020')",
        "(kind, name, owner_id, address, created_at) VALUES ('older', 'n', 1, 'a', '2020')",
        "(kind, name, token_hash, address, created_at) VALUES ('older', 'n', 'h', 'a', '2020')",
    ],
)
def test_a_tv_that_would_be_nobodys_or_two_peoples_is_refused(paired, columns):
    with pytest.raises(sqlite3.IntegrityError):
        paired.execute(f"INSERT INTO tvs {columns}")
