"""Migration v8: add extended metadata columns to ``suggestions``.

Adds ``tagline``, ``runtime``, ``genres``, ``cast_json``, ``director``,
``trailer_key``, ``imdb_rating``, and ``metascore`` columns, each guarded by
an existence check so the migration is idempotent.
"""

from __future__ import annotations

import sqlite3

from mediaman.db.migrations._helpers import _column_exists


def apply(conn: sqlite3.Connection) -> None:
    """Add extended metadata columns to ``suggestions`` where absent."""
    for col, col_type in [
        ("tagline", "TEXT"),
        ("runtime", "INTEGER"),
        ("genres", "TEXT"),
        ("cast_json", "TEXT"),
        ("director", "TEXT"),
        ("trailer_key", "TEXT"),
        ("imdb_rating", "TEXT"),
        ("metascore", "TEXT"),
    ]:
        if not _column_exists(conn, "suggestions", col):
            conn.execute(f"ALTER TABLE suggestions ADD COLUMN {col} {col_type}")
