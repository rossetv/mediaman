"""``cli_main`` refuses an old database before uvicorn starts."""

from __future__ import annotations

import os
import sqlite3

import pytest

from mediaman import main

_SECRET = "0123456789abcdef" * 4


def _stamp(path, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE admin_users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIAMAN_SECRET_KEY", _SECRET)
    monkeypatch.setenv("MEDIAMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["mediaman"])
    return tmp_path


@pytest.fixture
def uvicorn_run(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.append(kw))
    return calls


@pytest.mark.parametrize("version", [1, 35])
def test_an_old_database_stops_startup_before_uvicorn_with_no_traceback(
    data_dir, uvicorn_run, capsys, version
):
    db = data_dir / "mediaman.db"
    _stamp(db, version)
    before = db.read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        main.cli_main()

    assert excinfo.value.code == 1
    assert uvicorn_run == []
    err = capsys.readouterr().err
    assert f"at schema version {version}" in err
    assert "Traceback" not in err
    assert db.read_bytes() == before
    # SQLite may leave empty -wal/-shm files beside a WAL database it reads.
    assert {p.name for p in data_dir.iterdir()} <= {
        "mediaman.db",
        "mediaman.db-wal",
        "mediaman.db-shm",
    }


@pytest.mark.parametrize("version", [0, 36])
def test_a_current_database_reaches_uvicorn(data_dir, uvicorn_run, version):
    _stamp(data_dir / "mediaman.db", version)

    main.cli_main()

    assert len(uvicorn_run) == 1


def test_a_new_install_reaches_uvicorn(data_dir, uvicorn_run):
    main.cli_main()

    assert len(uvicorn_run) == 1
    assert list(data_dir.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_data_dir_reaches_the_app_for_its_own_error(data_dir, uvicorn_run):
    _stamp(data_dir / "mediaman.db", 0)
    for sidecar in ("-wal", "-shm"):
        (data_dir / f"mediaman.db{sidecar}").unlink(missing_ok=True)
    data_dir.chmod(0o500)
    try:
        main.cli_main()
    finally:
        data_dir.chmod(0o700)

    assert len(uvicorn_run) == 1


def test_a_corrupt_database_file_reaches_the_app_for_its_own_error(data_dir, uvicorn_run):
    (data_dir / "mediaman.db").write_bytes(b"this is not a database" * 100)

    main.cli_main()

    assert len(uvicorn_run) == 1
