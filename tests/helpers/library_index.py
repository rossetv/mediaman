"""The held Watch library index, read the way the routes read it."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from mediaman.services import watch_catalog


def library_movies(conn: sqlite3.Connection) -> dict[int, str]:
    """The movies the library index holds now, by TMDB id (a held index is reused)."""

    def movies(conn: sqlite3.Connection, libraries: list[str]) -> list[SimpleNamespace]:
        rows = conn.execute(
            "SELECT id, title, tmdb_id FROM media_items WHERE media_type = 'movie'"
        ).fetchall()
        return [SimpleNamespace(key=r["id"], title=r["title"], tmdb_id=r["tmdb_id"]) for r in rows]

    index = watch_catalog.library_index(conn, None, ["1"], movies, lambda c, libs: [])
    return dict(index.movies_by_tmdb)
