"""The role column and the watch_progress table exist on new and upgraded databases."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import init_db

_OLD_ADMIN_USERS = """
CREATE TABLE admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    email TEXT
);
"""


def test_users_of_an_old_database_become_admins(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(_OLD_ADMIN_USERS)
    old.execute(
        "INSERT INTO admin_users (username, password_hash, created_at) VALUES ('amy','h','2020')"
    )
    old.commit()
    old.close()

    conn = init_db(path)

    assert (
        conn.execute("SELECT role FROM admin_users WHERE username='amy'").fetchone()[0] == "admin"
    )
    conn.close()


def test_progress_rows_are_per_user_and_item_and_leave_with_the_user(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    for name in ("amy", "bo"):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'h', '2020')",
            (name,),
        )
    insert = (
        "INSERT INTO watch_progress (username, rating_key, updated_at) VALUES (?, '42', '2020')"
    )
    conn.execute(insert, ("amy",))
    conn.execute(insert, ("bo",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("amy",))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("nobody",))

    conn.execute("DELETE FROM admin_users WHERE username='amy'")

    rows = conn.execute("SELECT username FROM watch_progress").fetchall()
    assert [r[0] for r in rows] == ["bo"]
    conn.close()
