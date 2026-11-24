"""Which delete roots the last deletion pass found unavailable.

Stored as one ``settings`` row so Watch can show a notice without
probing a mount itself. Only the deletion pass writes it. Nothing here
commits: the caller owns the transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from mediaman.core.time import now_iso

logger = logging.getLogger(__name__)

STATE_KEY = "delete_root_unavailable"


def write_delete_root_state(
    conn: sqlite3.Connection, roots: dict[str, int], items: dict[str, list[int]] | None = None
) -> None:
    """Record *roots* (root or folder -> items waiting); an empty dict removes the row.

    *items* (folder -> ids of the waiting ``scheduled_actions`` rows) lets a
    reader count only those still waiting later.
    """
    if not roots:
        conn.execute("DELETE FROM settings WHERE key = ?", (STATE_KEY,))
        return
    now = now_iso()
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (STATE_KEY, json.dumps({"roots": roots, "items": items or {}, "updated_at": now}), now),
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


def read_delete_root_waiting(conn: sqlite3.Connection) -> dict[str, int]:
    """Return the recorded roots with each count cut to the rows still waiting.

    A keep or a clear removes a row at once, but the pass records the roots
    only when it runs. A row recorded without ids keeps its stored count. A
    folder with nothing left waiting is dropped. One indexed query, no disk.
    """
    roots = read_delete_root_state(conn)
    if not roots:
        return {}
    try:
        recorded = json.loads(
            conn.execute("SELECT value FROM settings WHERE key = ?", (STATE_KEY,)).fetchone()[0]
        ).get("items", {})
        ids = {str(k): v for k, v in recorded.items() if _is_id_list(v)}
    except (ValueError, TypeError, AttributeError):
        ids = {}
    wanted = sorted({i for folder in roots for i in ids.get(folder, [])})
    live: set[int] = set()
    if wanted:
        try:
            # One bound JSON value, so the number of ids never meets a bind-variable cap.
            live = {
                r[0]
                for r in conn.execute(
                    "SELECT id FROM scheduled_actions "
                    "WHERE id IN (SELECT value FROM json_each(?)) "
                    "AND action = 'scheduled_deletion' "
                    "AND (delete_status IS NULL OR delete_status = 'pending')",
                    (json.dumps(wanted),),
                )
            }
        except sqlite3.Error:
            logger.warning(
                "could not count waiting deletions; showing stored counts", exc_info=True
            )
            return roots
    result = {}
    for folder, count in roots.items():
        count = len([i for i in ids[folder] if i in live]) if folder in ids else count
        if count:
            result[folder] = count
    return result


def _is_id_list(value: object) -> bool:
    """True for a list of real ints (not bool) that fit SQLite's 64-bit integer."""
    return isinstance(value, list) and all(type(i) is int and -(2**63) <= i < 2**63 for i in value)
