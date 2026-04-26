"""Security event audit log.

Writes structured records to the existing ``audit_log`` table with a
dedicated ``sec:`` prefix on the ``action`` column so they're easy to
query separately from media actions. Records carry actor username,
client IP, and a short detail string. These events are the trail an
operator uses to reconstruct what a compromised session did.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from mediaman.services.infra.time import now_iso

logger = logging.getLogger("mediaman")


def log_audit(
    conn: sqlite3.Connection,
    media_item_id: str,
    action: str,
    detail: str,
    *,
    space_bytes: int | None = None,
) -> None:
    """Insert a row into ``audit_log`` for a media action.

    Args:
        conn: Open SQLite connection.
        media_item_id: The media item ID (or a surrogate like a title string).
        action: Short action label, e.g. ``"deleted"``, ``"snoozed"``.
        detail: Human-readable detail string.
        space_bytes: Optional value for the ``space_reclaimed_bytes`` column;
            omit (or pass ``None``) for events where no space was reclaimed.

    Does **not** call ``conn.commit()`` — callers are responsible for
    committing in their own transaction so the audit row and the business
    row land in the same commit.
    """
    now = now_iso()
    if space_bytes is not None:
        conn.execute(
            "INSERT INTO audit_log "
            "(media_item_id, action, detail, space_reclaimed_bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (media_item_id, action, detail, space_bytes, now),
        )
    else:
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (media_item_id, action, detail, now),
        )


def security_event(
    conn: sqlite3.Connection,
    *,
    event: str,
    actor: str = "",
    ip: str = "",
    detail: dict | str | None = None,
) -> None:
    """Append a security event to ``audit_log``.

    - ``event``: short kebab-case tag (e.g. ``login.success``,
      ``settings.write``, ``user.delete``).
    - ``actor``: username, or empty string for unauthenticated events.
    - ``ip``: client IP (already extracted via ``get_client_ip``).
    - ``detail``: dict (JSON-encoded) or a short string.

    Writes to ``audit_log(media_item_id, action, detail, created_at)``
    with ``media_item_id='_security'`` so these events don't collide
    with real media row references and are easy to filter out of
    per-item queries.

    Failures to write are logged but not raised — audit-logging
    failures must not break the user-facing flow.
    """
    try:
        if isinstance(detail, dict):
            detail_str = json.dumps(detail, separators=(",", ":"))
        else:
            detail_str = str(detail or "")
        # Prefix with actor/ip so grep works even without JSON parsing
        prefix = f"actor={actor or '-'} ip={ip or '-'}"
        body = f"{prefix} {detail_str}" if detail_str else prefix
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            ("_security", f"sec:{event}", body, now_iso()),
        )
        conn.commit()
    except Exception:  # pragma: no cover — never break flow on log failure
        logger.exception("security_event write failed event=%s", event)
