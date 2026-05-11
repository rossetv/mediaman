"""Database bootstrap — open the SQLite file, run migrations, stash on app state.

Owns the first step of startup: ensure the data directory is writable, open
the bootstrap connection, register it for repository-level lookups, and
attach the resolved config plus connection to ``app.state``.

The data-dir writability helpers live in :mod:`mediaman.bootstrap.data_dir`
and are re-exported here so test files can keep patching
``mediaman.bootstrap.db.tempfile`` without caring about the layout.
"""

from __future__ import annotations

import logging
import os

# Re-exported so ``patch.object(bootstrap_db_mod.tempfile, ...)`` in tests
# continues to land on the symbol the writability probe actually uses.
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from mediaman.bootstrap.data_dir import (
    DataDirNotWritableError,
    _assert_data_dir_writable,
    _remediation_for,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from mediaman.config import Config

logger = logging.getLogger(__name__)


def bootstrap_db(app: FastAPI, config: Config) -> None:
    """Open the SQLite DB, run migrations, register the bootstrap connection.

    Side effects on ``app.state``:

    - ``app.state.config`` — the resolved config object.
    - ``app.state.db`` — the bootstrap :class:`sqlite3.Connection`.
    - ``app.state.db_path`` — absolute path of the DB file.
    """
    from mediaman.db import init_db, set_connection

    data_dir = Path(config.data_dir)
    # ``mkdir`` precedes the writability probe — without the wrapper the
    # OSError surfaces as an unhandled traceback (lost behind a wall of
    # ASGI frames) instead of the actionable single-line error the probe
    # produces.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        proc_uid = os.geteuid()
        proc_gid = os.getegid()
        raise DataDirNotWritableError(
            f"data dir {data_dir} could not be created by uid={proc_uid} "
            f"gid={proc_gid}; {_remediation_for(exc)}; underlying error: {exc}"
        ) from exc
    _assert_data_dir_writable(data_dir)
    db_path = str(Path(config.data_dir) / "mediaman.db")
    logger.info("DB initialised at %s", db_path)
    conn = init_db(db_path)
    set_connection(conn)
    app.state.config = config
    app.state.db = conn
    app.state.db_path = db_path

    # Ensure the poster cache directory exists at startup so the first
    # request doesn't race with the lazy mkdir inside the poster route.
    poster_cache_dir = Path(config.data_dir) / "poster_cache"
    poster_cache_dir.mkdir(parents=True, exist_ok=True)


__all__ = [
    "DataDirNotWritableError",
    "_assert_data_dir_writable",
    "_remediation_for",
    "bootstrap_db",
    "tempfile",
]
