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
from datetime import datetime, timezone

logger = logging.getLogger("mediaman")


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
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("_security", f"sec:{event}", body,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception:  # pragma: no cover — never break flow on log failure
        logger.exception("security_event write failed event=%s", event)
