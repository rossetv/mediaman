"""SQL operations for reading scanner settings from the `settings` table."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

logger = logging.getLogger("mediaman")


def read_delete_allowed_roots_setting(
    conn: sqlite3.Connection,
) -> list[str]:
    """Read ``delete_allowed_roots`` from settings / env.

    Precedence (intentionally inverse of the more common "env wins"
    convention):

    1. The ``delete_allowed_roots`` row in the ``settings`` table — a
       JSON array of absolute path strings — wins when present and
       non-empty. The settings table is the operator's UI-managed
       source of truth, and we want a value set in the admin UI to
       beat any leftover value in the container environment.
    2. The ``MEDIAMAN_DELETE_ROOTS`` environment variable — colon /
       semicolon separated paths — is consulted only when the DB row
       is missing or empty. This lets a fresh container start with a
       sensible bootstrap set before the operator has logged in to
       configure them in the UI.
    3. If both are empty we return ``[]`` and log a loud error: the
       caller must treat an empty list as fail-closed and refuse every
       deletion (path safety contract). Letting an unconfigured
       installation silently accept ``/`` would be catastrophic.

    Documenting the precedence inline so anyone reading the deletion
    path can confirm at a glance that the unusual order — DB beats env
    — is deliberate (Domain 05 finding).
    """
    row = conn.execute("SELECT value FROM settings WHERE key='delete_allowed_roots'").fetchone()
    roots: list[str] = []
    if row and row["value"]:
        try:
            parsed = json.loads(row["value"])
            if isinstance(parsed, list):
                roots = [str(r) for r in parsed if r]
        except (ValueError, TypeError):
            pass
    if not roots:
        env_val = os.environ.get("MEDIAMAN_DELETE_ROOTS", "")
        if env_val:
            # Single source of truth lives in path_safety.parse_delete_roots_env
            # so the deletion path and the disk-usage path always agree on
            # separator handling (finding 31).
            from mediaman.services.infra.path_safety import parse_delete_roots_env

            roots = parse_delete_roots_env(env_val)
            if not roots:
                logger.error(
                    "MEDIAMAN_DELETE_ROOTS is set but no valid roots "
                    "parsed from %r — deletions will be refused.",
                    env_val,
                )
    if not roots:
        logger.error(
            "delete_allowed_roots is not configured — all deletions "
            "will be refused. Set the delete_allowed_roots setting "
            "(JSON list) or the MEDIAMAN_DELETE_ROOTS env var "
            "(colon-separated) to re-enable deletions."
        )
    return roots
