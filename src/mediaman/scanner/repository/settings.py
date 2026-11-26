"""SQL operations for reading scanner settings from the `settings` table."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

# The roots are read several times each scan; that deletion is off is said once.
_said_deletion_off = False


def read_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the raw ``settings.value`` for *key*, or ``None`` if absent.

    The single repository entry point for
    ``SELECT value FROM settings WHERE key=?`` reads (the repository owns
    the SQL). The value is returned verbatim (no JSON / bool coercion);
    callers parse it for their own needs. A missing row and a row with a
    SQL ``NULL`` value both yield ``None``.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value = row["value"]
    return None if value is None else str(value)


def read_delete_allowed_roots_setting(
    conn: sqlite3.Connection,
    *,
    quiet: bool = False,
) -> list[str]:
    """Read the folders scheduled deletion may delete from.

    1. A ``delete_allowed_roots`` row in the ``settings`` table (a JSON
       array of absolute path strings) wins when present and non-empty.
       Nothing in mediaman writes that row, so on an install it is only
       there if someone put it into the database by hand.
    2. Otherwise the ``MEDIAMAN_DELETE_ROOTS`` environment variable,
       colon / semicolon separated paths, which is how a person sets them.
    3. If both are empty we return ``[]``: the caller must treat an empty
       list as fail-closed and refuse every deletion (path safety
       contract). Letting an unconfigured installation silently accept
       ``/`` would be catastrophic. With the variable unset, which is the
       default install, that scheduled deletion is off is logged at info,
       once per process;
       set with nothing valid in it, an error is logged.

    ``quiet=True`` silences every log line of this read (bad JSON, an
    unparseable env var, nothing configured) for read-only callers such as
    the storage panel; the returned list is the same.
    """
    row = conn.execute("SELECT value FROM settings WHERE key='delete_allowed_roots'").fetchone()
    roots: list[str] = []
    if row and row["value"]:
        try:
            parsed = json.loads(row["value"])
            if isinstance(parsed, list):
                roots = [str(r) for r in parsed if r]
        except (ValueError, TypeError):
            if not quiet:
                logger.warning(
                    "scanner.delete_roots.invalid_json — could not parse "
                    "delete_allowed_roots setting value %r; "
                    "falling through to env-var / fail-closed path.",
                    row["value"],
                    exc_info=True,
                )
    env_val = ""
    if not roots:
        env_val = os.environ.get("MEDIAMAN_DELETE_ROOTS", "")
        if env_val:
            # Single source of truth lives in path_safety.parse_delete_roots_env
            # so the deletion path and the disk-usage path always agree on
            # separator handling.
            from mediaman.services.infra import parse_delete_roots_env

            roots = parse_delete_roots_env(env_val, quiet=quiet)
            if not roots and not quiet:
                logger.error(
                    "MEDIAMAN_DELETE_ROOTS is set but no valid roots "
                    "parsed from %r — deletions will be refused.",
                    env_val,
                )
    global _said_deletion_off
    if not roots and not env_val and not quiet and not _said_deletion_off:
        _said_deletion_off = True
        logger.info(
            "Scheduled deletion is off until MEDIAMAN_DELETE_ROOTS is set "
            "(the folders it may delete from, separated by ':')."
        )
    return roots
