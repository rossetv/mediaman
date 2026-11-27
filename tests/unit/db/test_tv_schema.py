"""Tests for the table of TVs: what ends a pairing, whoever makes the change."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db.schema_definition import SCHEMA


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


def _people(conn) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT tvs.name, u.username FROM tv_people p JOIN tvs ON tvs.id = p.tv_id"
        " JOIN admin_users u ON u.id = p.user_id ORDER BY tvs.id, u.username"
    )
    return [tuple(r) for r in rows]


def test_a_tv_paired_before_several_people_could_be_is_paired_with_its_owner(paired):
    paired.execute("DELETE FROM tv_people")
    paired.executescript(SCHEMA)
    assert _people(paired) == [("amy", "amy"), ("bob", "bob")]


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM admin_users WHERE username = 'amy'",
        "UPDATE admin_users SET password_hash = 'other' WHERE username = 'amy'",
        "UPDATE admin_users SET must_change_password = 1 WHERE username = 'amy'",
    ],
)
def test_a_shared_tv_passes_to_the_others_where_one_persons_pairing_ends(paired, change):
    paired.execute(
        "INSERT INTO tv_people (tv_id, user_id, paired_at) SELECT tvs.id, u.id, '2021'"
        " FROM tvs, admin_users u WHERE tvs.name = 'amy' AND u.username = 'bob'"
    )
    paired.execute(change)
    owner = paired.execute(
        "SELECT u.username FROM tvs JOIN admin_users u ON u.id = tvs.owner_id WHERE tvs.name = 'amy'"
    ).fetchone()
    assert (tuple(owner), _people(paired)) == (("bob",), [("amy", "bob"), ("bob", "bob")])


def test_a_tv_an_older_image_hands_to_another_is_theirs_alone(paired):
    paired.execute(
        "UPDATE tvs SET owner_id = (SELECT id FROM admin_users WHERE username = 'bob'),"
        " token_hash = 'taken' WHERE name = 'amy'"
    )
    assert _people(paired) == [("amy", "bob"), ("bob", "bob")]
    paired.execute("UPDATE admin_users SET password_hash = 'other' WHERE username = 'bob'")
    assert _names(paired) == ["old"]
