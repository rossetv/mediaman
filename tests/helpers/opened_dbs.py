"""Close every database a test opens through ``init_db``.

Import ``close_opened_dbs`` into a test module that calls ``init_db`` inline:
it is an autouse fixture, so each connection is closed when its test ends.
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

import mediaman.db
import mediaman.db.connection


@pytest.fixture(autouse=True)
def close_opened_dbs(request, monkeypatch):
    opened: list[sqlite3.Connection] = []
    real = mediaman.db.connection.init_db

    def tracked(*args, **kwargs):
        conn = real(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(mediaman.db.connection, "init_db", tracked)
    monkeypatch.setattr(mediaman.db, "init_db", tracked)
    if hasattr(request.module, "init_db"):
        monkeypatch.setattr(request.module, "init_db", tracked)
    yield
    for conn in opened:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
