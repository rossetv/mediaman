"""Database package — schema, migrations, and connection management.

Split from the original monolithic ``db.py`` (R5). Callers continue to
import every symbol from :mod:`mediaman.db`.
"""

# "unused" private imports are part of the module's public surface.

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
from .migrations import SchemaFromFutureError, SchemaTooOldError, apply_migrations
from .schema_definition import CUTOVER_VERSION, DB_SCHEMA_VERSION

__all__ = [
    "CUTOVER_VERSION",
    "DB_SCHEMA_VERSION",
    "SchemaFromFutureError",
    "SchemaTooOldError",
    "apply_migrations",
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
