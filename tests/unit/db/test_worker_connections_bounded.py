"""A worker thread's connection does not outlive the thread."""

from __future__ import annotations

import os
import threading

import pytest

from mediaman.db import get_db, init_db, set_connection

pytestmark = pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="needs /dev/fd")


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_short_lived_threads_leave_no_connections_open(db_path):
    boot = init_db(str(db_path))
    set_connection(boot)

    def worker() -> None:
        get_db().execute("SELECT 1").fetchone()

    before = _open_fds()
    for _ in range(50):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
    leaked = _open_fds() - before
    boot.close()

    assert leaked < 5
