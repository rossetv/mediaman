"""init_db refuses a database last upgraded by a schema runner older than v36."""

from __future__ import annotations

import sqlite3

import pytest

from mediaman.db import connection

# A v34/v35 database needs one pass of the last image that still shipped the
# v36 runner; anything older needs the last pre-squash image first.
_V36_RUNNER_IMAGE = "rossetv/mediaman:sha-8414ba2fff2979d5da43c5d37c4652057e9eecb6"
_V34_RUNNER_IMAGE = "rossetv/mediaman:sha-7fc9dcbb787b841c02d3ded522f7fbf67f3e41f1"


def _stamped_db(path, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE admin_users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO admin_users (username) VALUES ('admin')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("version", "images"),
    [
        (1, [_V34_RUNNER_IMAGE, _V36_RUNNER_IMAGE]),
        (33, [_V34_RUNNER_IMAGE, _V36_RUNNER_IMAGE]),
        (34, [_V36_RUNNER_IMAGE]),
        (35, [_V36_RUNNER_IMAGE]),
    ],
)
def test_an_old_database_is_refused_and_left_untouched(db_path, version, images):
    _stamped_db(db_path, version)
    before = db_path.read_bytes()

    with pytest.raises(connection.SchemaTooOldError) as excinfo:
        connection.init_db(str(db_path))

    message = str(excinfo.value)
    assert f"version {version}" in message
    for image in images:
        assert image in message
    if version >= 34:
        assert _V34_RUNNER_IMAGE not in message
    assert db_path.read_bytes() == before
    assert sorted(p.name for p in db_path.parent.iterdir()) == ["mediaman.db"]


def test_an_empty_file_bootstraps(db_path):
    db_path.touch()

    conn = connection.init_db(str(db_path))

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"admin_users", "settings", "media_items"} <= tables


@pytest.mark.parametrize("version", [36, 37])
def test_a_database_at_or_past_v36_opens(db_path, version):
    _stamped_db(db_path, version)

    conn = connection.init_db(str(db_path))

    assert conn.execute("SELECT username FROM admin_users").fetchone()[0] == "admin"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version


def test_check_schema_version_ignores_a_missing_file_and_creates_nothing(db_path):
    connection.check_schema_version(str(db_path))

    assert not db_path.exists()


@pytest.mark.parametrize("version", [0, 36, 40])
def test_check_schema_version_passes_version_zero_and_36_plus(db_path, version):
    _stamped_db(db_path, version)

    connection.check_schema_version(str(db_path))


def test_check_schema_version_refuses_an_old_wal_database_without_changing_it(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("PRAGMA user_version=35")
    conn.commit()
    conn.close()
    before = db_path.read_bytes()

    with pytest.raises(connection.SchemaTooOldError, match="version 35"):
        connection.check_schema_version(str(db_path))

    # SQLite may leave empty -wal/-shm files beside a WAL database it reads.
    assert db_path.read_bytes() == before


def _hot_journal_db(path, version: int) -> None:
    """Leave ``path`` as a crash would: a rollback-mode file with a hot -journal."""
    src = path.parent / "src.db"
    conn = sqlite3.connect(src, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE admin_users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO admin_users (username) VALUES ('admin')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN")
    conn.execute("CREATE TABLE junk (x)")
    conn.executemany("INSERT INTO junk VALUES (?)", [("x" * 500,)] * 2000)
    conn.execute("UPDATE admin_users SET username='changed'")
    path.write_bytes(src.read_bytes())
    (path.parent / f"{path.name}-journal").write_bytes(
        (path.parent / "src.db-journal").read_bytes()
    )
    conn.close()


def test_a_version_zero_database_with_a_hot_journal_opens(db_path):
    _hot_journal_db(db_path, 0)
    assert (db_path.parent / f"{db_path.name}-journal").stat().st_size > 0

    conn = connection.init_db(str(db_path))

    assert conn.execute("SELECT username FROM admin_users").fetchone()[0] == "admin"


def test_a_version_35_database_with_a_hot_journal_is_still_refused(db_path):
    _hot_journal_db(db_path, 35)

    with pytest.raises(connection.SchemaTooOldError, match="version 35"):
        connection.init_db(str(db_path))
