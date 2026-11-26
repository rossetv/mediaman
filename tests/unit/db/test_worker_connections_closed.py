"""reset_connection closes the connections that worker threads opened."""

from __future__ import annotations

import sqlite3
import threading

from mediaman.db import get_db, init_db, reset_connection, set_connection


def test_reset_closes_a_connection_a_worker_thread_still_holds(db_path):
    boot = init_db(str(db_path))
    set_connection(boot)
    opened, reset_done = threading.Event(), threading.Event()
    outcome: list[object] = []

    def worker() -> None:
        conn = get_db()
        opened.set()
        reset_done.wait(5)
        try:
            conn.execute("SELECT 1")
            outcome.append("still open")
        except sqlite3.ProgrammingError as exc:
            outcome.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    assert opened.wait(5)

    reset_connection()
    reset_done.set()
    thread.join(5)
    boot.close()

    assert outcome == ["Cannot operate on a closed database."]
