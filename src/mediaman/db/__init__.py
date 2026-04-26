"""Database package — schema, migrations, and connection management.

Split from the original monolithic ``db.py`` (R5). Callers continue to
import every symbol from :mod:`mediaman.db`.
"""
# ruff: noqa: F401 — this module is a deliberate re-export facade; the
# "unused" private imports are part of the module's public surface.

from .connection import (
    close_db,
    finish_refresh_run,
    finish_scan_run,
    get_db,
    init_db,
    is_refresh_running,
    is_scan_running,
    open_thread_connection,
    set_connection,
    start_refresh_run,
    start_scan_run,
)
from .schema import DB_SCHEMA_VERSION, apply_migrations

__all__ = [
    "init_db",
    "get_db",
    "open_thread_connection",
    "set_connection",
    "close_db",
    "is_scan_running",
    "start_scan_run",
    "finish_scan_run",
    "is_refresh_running",
    "start_refresh_run",
    "finish_refresh_run",
    "DB_SCHEMA_VERSION",
    "apply_migrations",
]
