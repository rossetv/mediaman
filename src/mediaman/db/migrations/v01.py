"""Migration v1: initial schema.

The full DDL is applied by the runner directly via ``executescript(_SCHEMA)``
before invoking migration functions, so this module exists as a placeholder
to keep the numbering consistent.  ``apply`` is intentionally a no-op.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """No-op — the initial schema is applied by the runner before migrations."""
