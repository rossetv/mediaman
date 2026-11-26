"""A worker thread does not keep a connection from before a reset."""

from __future__ import annotations

import sqlite3
import threading

from mediaman.db import get_db, init_db, reset_connection, set_connection


def test_a_worker_thread_closes_its_connection_from_before_a_reset(tmp_path):
    first = init_db(str(tmp_path / "first.db"))
    set_connection(first)
    opened, reset_done = threading.Event(), threading.Event()
    outcome: list[object] = []

    def worker() -> None:
        old = get_db()
        opened.set()
        reset_done.wait(5)
        new = get_db()
        outcome.append(new.execute("PRAGMA database_list").fetchone()[2])
        try:
            old.execute("SELECT 1")
            outcome.append("still open")
        except sqlite3.ProgrammingError as exc:
            outcome.append(str(exc))
        new.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert opened.wait(5)

    reset_connection()
    first.close()
    second_path = str(tmp_path / "second.db")
    second = init_db(second_path)
    set_connection(second)
    reset_done.set()
    thread.join(5)
    second.close()

    assert outcome == [second_path, "Cannot operate on a closed database."]
