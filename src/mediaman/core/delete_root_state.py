"""Which delete roots the last deletion pass found unavailable.

Stored as one ``settings`` row so the dashboard can show a banner without
probing a mount itself. Only the deletion pass writes it. Nothing here
commits: the caller owns the transaction.
"""

from __future__ import annotations

import json
import sqlite3

from mediaman.core.time import now_iso

STATE_KEY = "delete_root_unavailable"


def write_delete_root_state(conn: sqlite3.Connection, roots: dict[str, int]) -> None:
    """Record *roots* (root or folder -> items waiting); an empty dict removes the row."""
    if not roots:
        conn.execute("DELETE FROM settings WHERE key = ?", (STATE_KEY,))
        return
    now = now_iso()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (STATE_KEY, json.dumps({"roots": roots, "updated_at": now}), now),
    )


def read_delete_root_state(conn: sqlite3.Connection) -> dict[str, int]:
    """Return the recorded roots, or ``{}`` when the row is missing or malformed."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (STATE_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        roots = json.loads(row[0])["roots"]
        return {str(k): int(v) for k, v in roots.items()}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {}
