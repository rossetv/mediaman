"""Migration v16: intentionally skipped.

This version number was reserved and never used.  The runner advances
``PRAGMA user_version`` to 16 as part of normal sequential execution so
that the numbering of subsequent migrations is not disturbed.
"""

from __future__ import annotations

import sqlite3


def apply(conn: sqlite3.Connection) -> None:
    """No-op — this migration version was reserved but never implemented."""
