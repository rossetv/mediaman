"""The tables and columns of a person's account, on a new database and on an older one."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import init_db

_OLD_TABLES = """
CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'admin'
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES admin_users(username) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    token_hash TEXT,
    last_used_at TEXT,
    fingerprint TEXT,
    issued_ip TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    space_reclaimed_bytes INTEGER,
    created_at TEXT NOT NULL,
    actor TEXT
);
"""
_PER_PERSON = {
    "user_avatars": "(user_id, kind, choice, updated_at) VALUES (?, 'drawn', 3, '2026')",
    "user_ratings": "(user_id, media_type, tmdb_id, level, updated_at)"
    " VALUES (?, 'movie', 7, 2, '2026')",
    "user_seen_titles": "(user_id, media_type, tmdb_id, through_season, seen_at)"
    " VALUES (?, 'tv', 7, 2, '2026')",
    "user_rating_asks": "(user_id, media_type, tmdb_id, asked_at) VALUES (?, 'movie', 7, '2026')",
    "user_searches": "(user_id, phrase_key, phrase, searched_at) VALUES (?, 'a', 'A', '2026')",
    "user_ai_phrases": "(user_id, phrases, made_at) VALUES (?, '[]', '2026')",
    "user_ai_answers": "(user_id, kind, query_key, answer, made_at)"
    " VALUES (?, 'ask', 'a', '[]', '2026')",
    "user_ai_runs": "(user_id, kind, started_at) VALUES (?, 'ask', '2026')",
}


def _old_writes(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, must_change_password,"
        " email, role) VALUES (?, 'h', '2020', 0, NULL, 'viewer')",
        (name,),
    )
    conn.executemany(
        "INSERT INTO admin_sessions (token, token_hash, username, created_at, expires_at,"
        " last_used_at, fingerprint, issued_ip) VALUES (?, ?, ?, '2020', '2030', '2020', '', '')",
        [(f"{name}{n}", f"{name}{n}", name) for n in (1, 2)],
    )
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at, actor)"
        " VALUES ('m', 'kept', 'd', '2020', ?)",
        (name,),
    )
    conn.commit()


@pytest.fixture
def old_path(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_TABLES)
    _old_writes(conn, "amy")
    conn.close()
    return path


@pytest.fixture
def conn(tmp_path):
    conn = init_db(str(tmp_path / "new.db"))
    for name in ("amy", "bob"):
        conn.execute(
            "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, 'h', '2020')",
            (name,),
        )
    conn.commit()
    yield conn
    conn.close()


def _shape(conn: sqlite3.Connection) -> dict:
    """Columns in order, foreign keys and indexes of each table, and the code of triggers."""
    shape: dict = {}
    for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
        for pragma in ("table_xinfo", "foreign_key_list", "index_list"):
            rows = {tuple(r) for r in conn.execute(f"PRAGMA {pragma}({name})")}
            if pragma == "index_list":
                rows = {row[1:] for row in rows}
            shape[name, pragma] = rows
    for row in conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('index', 'trigger')"
    ):
        shape[row[0], row[1]] = row[2]
    return shape


def test_an_older_database_comes_out_the_same_as_a_new_one(old_path, tmp_path):
    fresh = init_db(str(tmp_path / "fresh.db"))
    upgraded = init_db(old_path)

    assert _shape(upgraded) == _shape(fresh)
    fresh.close()
    upgraded.close()


def test_a_second_start_changes_nothing(old_path):
    conn = init_db(old_path)
    before = _shape(conn), list(conn.iterdump())
    conn.close()

    conn = init_db(old_path)

    assert (_shape(conn), list(conn.iterdump())) == before
    conn.close()


def test_an_older_version_still_starts_and_writes_on_the_new_database(old_path):
    init_db(old_path).close()
    conn = sqlite3.connect(old_path)
    conn.executescript(_OLD_TABLES)

    _old_writes(conn, "bob")

    conn.close()
    conn = init_db(old_path)
    assert conn.execute("SELECT ai_history FROM admin_users").fetchall()[1][0] == 1
    assert [r[0] for r in conn.execute("SELECT actor_id FROM audit_log")] == [None, None]
    conn.close()


def test_every_session_an_older_version_stored_gets_its_own_id_unlike_its_token(old_path):
    """Before or after the column came: an older version may run again and store more."""
    conn = init_db(old_path)
    _old_writes(conn, "bob")
    conn.close()

    conn = init_db(old_path)

    rows = conn.execute("SELECT public_id, token FROM admin_sessions").fetchall()
    assert len({row["public_id"] for row in rows}) == 4
    assert all(len(row["public_id"]) == 32 for row in rows)
    conn.close()


def test_what_is_kept_for_a_person_goes_with_the_person_alone(conn):
    for table, values in _PER_PERSON.items():
        conn.executemany(f"INSERT INTO {table} {values}", [(1,), (2,)])
    conn.commit()

    conn.execute("DELETE FROM admin_users WHERE username = 'amy'")

    for table in _PER_PERSON:
        assert [r[0] for r in conn.execute(f"SELECT user_id FROM {table}")] == [2], table


def test_an_older_version_still_ends_a_person_on_the_new_database(old_path):
    conn = init_db(old_path)
    conn.execute(f"INSERT INTO user_seen_titles {_PER_PERSON['user_seen_titles']}", (1,))
    conn.commit()
    conn.close()
    conn = sqlite3.connect(old_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_OLD_TABLES)

    conn.execute("DELETE FROM admin_users WHERE username = 'amy'")

    assert conn.execute("SELECT count(*) FROM user_seen_titles").fetchone()[0] == 0
    conn.close()


def test_the_audit_log_keeps_the_rows_of_a_deleted_person(conn):
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, created_at, actor, actor_id)"
        " VALUES ('m', 'kept', '2026', 'amy', 1)"
    )

    conn.execute("DELETE FROM admin_users WHERE username = 'amy'")

    assert conn.execute("SELECT actor_id FROM audit_log").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("user_ratings", "VALUES (1, 'movie', 7, 0, NULL, NULL, '2026')"),
        ("user_ratings", "VALUES (1, 'movie', 7, 3, NULL, NULL, '2026')"),
        ("user_ratings", "VALUES (1, 'music', 7, 1, NULL, NULL, '2026')"),
        ("user_seen_titles", "VALUES (1, 'music', 7, NULL, NULL, 0, NULL, NULL, '2026')"),
        ("user_seen_titles", "VALUES (1, 'movie', 7, 2, NULL, 0, NULL, NULL, '2026')"),
        ("user_seen_titles", "VALUES (1, 'tv', 7, NULL, NULL, 0, NULL, NULL, '2026')"),
        ("user_avatars", "VALUES (1, 'drawn', NULL, NULL, '2026')"),
        ("user_avatars", "VALUES (1, 'upload', NULL, NULL, '2026')"),
        ("user_avatars", "VALUES (1, 'upload', 2, 'a.png', '2026')"),
        ("user_searches", "VALUES (1, '', '', '2026')"),
    ],
)
def test_a_value_that_means_nothing_is_refused(conn, table, values):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(f"INSERT INTO {table} {values}")


def test_a_person_has_one_rating_of_a_title(conn):
    insert = f"INSERT INTO user_ratings {_PER_PERSON['user_ratings']}"
    conn.executemany(insert, [(1,), (2,)])

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(insert, (1,))


def test_only_a_persons_newest_searches_and_answers_are_kept(conn):
    for table, bound in (("user_searches", 50), ("user_ai_answers", 100)):
        conn.execute(f"INSERT INTO {table} {_PER_PERSON[table]}", (2,))
        values = [(str(n), f"2026-{n:04}") for n in range(bound + 2)]
        # Newest first, so age decides what goes, not the order it was stored in.
        conn.executemany(
            f"INSERT INTO {table} "
            + (
                "VALUES (1, ?1, ?1, ?2)"
                if table == "user_searches"
                else "VALUES (1, 'ask', ?1, '[]', ?2)"
            ),
            values[::-1],
        )
        key = "phrase_key" if table == "user_searches" else "query_key"
        kept = {r[0] for r in conn.execute(f"SELECT {key} FROM {table} WHERE user_id = 1")}
        assert kept == {str(n) for n in range(2, bound + 2)}
        count = conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = 2").fetchone()[0]
        assert count == 1
