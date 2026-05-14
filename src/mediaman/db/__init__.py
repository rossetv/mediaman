"""Database package — schema and connection management.

Owns the :mod:`sqlite3` connection lifecycle, WAL configuration, and full
schema DDL (applied idempotently via ``CREATE … IF NOT EXISTS`` in
:mod:`mediaman.db.schema_definition`).  This package holds the exclusive
``sqlite3.connect`` monopoly — no other package opens raw connections.

Allowed dependencies: stdlib :mod:`sqlite3`, :mod:`mediaman.core` (time and
audit helpers).  Forbidden: no business logic, no domain-table queries beyond
job-run bookkeeping, and :mod:`mediaman.db` must never import from
:mod:`mediaman.crypto` (crypto depends on db, not the reverse).
"""

from __future__ import annotations

from .connection import (
    close_db,
    finish_refresh_run,
    finish_scan_run,
    get_db,
    heartbeat_refresh_run,
    heartbeat_scan_run,
    init_db,
    is_refresh_running,
    is_scan_running,
    open_thread_connection,
    reset_connection,
    set_connection,
    start_refresh_run,
    start_scan_run,
)

__all__ = [
    "close_db",
    "finish_refresh_run",
    "finish_scan_run",
    "get_db",
    "heartbeat_refresh_run",
    "heartbeat_scan_run",
    "init_db",
    "is_refresh_running",
    "is_scan_running",
    "open_thread_connection",
    "reset_connection",
    "set_connection",
    "start_refresh_run",
    "start_scan_run",
]
