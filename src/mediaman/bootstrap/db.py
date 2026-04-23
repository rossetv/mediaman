"""DB bootstrap step — open the connection, stash on ``app.state`` (R23)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from mediaman.db import init_db, set_connection


def bootstrap_db(app: FastAPI, config) -> None:
    """Open the SQLite DB, run migrations, register the bootstrap connection.

    Side effects on ``app.state``:

    - ``app.state.config`` — the resolved config object.
    - ``app.state.db`` — the bootstrap :class:`sqlite3.Connection`.
    - ``app.state.db_path`` — absolute path of the DB file.
    """
    db_path = f"{config.data_dir}/mediaman.db"
    Path(config.data_dir).mkdir(parents=True, exist_ok=True)
    conn = init_db(db_path)
    set_connection(conn)
    app.state.config = config
    app.state.db = conn
    app.state.db_path = db_path

    # Ensure the poster cache directory exists at startup so the first
    # request doesn't race with the lazy mkdir inside the poster route.
    from mediaman.web.routes.poster import _get_cache_dir
    _get_cache_dir(config.data_dir)
