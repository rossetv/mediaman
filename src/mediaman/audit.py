"""Security event audit log.

Writes structured records to the existing ``audit_log`` table with a
dedicated ``sec:`` prefix on the ``action`` column so they're easy to
query separately from media actions. Records carry actor username,
client IP, and a short detail string. These events are the trail an
operator uses to reconstruct what a compromised session did.

Two write paths exist:

* :func:`security_event` — best-effort write that swallows exceptions.
  Suitable for events whose ABSENCE from the log is not by itself a
  security incident (login.success, session.destroy, etc.).
* :func:`security_event_or_raise` — fail-closed write that re-raises
  on failure and intentionally does NOT call ``conn.commit()`` so the
  caller can hold the write inside a wider ``BEGIN ... COMMIT``
  transaction. Privilege-establishing mutations (admin create / delete,
  password change, sensitive settings, lockout / unlock) MUST use this
  variant — if the audit row cannot be persisted the mutation must be
  rolled back so we never have a "the change happened but no one knows"
  situation.
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


def _format_security_body(actor: str, ip: str, detail: dict | str | None) -> str:
    """Render the ``detail`` column for a security event.

    Prefixes ``actor=`` and ``ip=`` so a human grepping the log can find
    everything for a user without parsing JSON, and appends a
    JSON-encoded body when *detail* is a dict.
    """
    if isinstance(detail, dict):
        detail_str = json.dumps(detail, separators=(",", ":"))
    else:
        detail_str = str(detail or "")
    prefix = f"actor={actor or '-'} ip={ip or '-'}"
    return f"{prefix} {detail_str}" if detail_str else prefix


def security_event(
    conn: sqlite3.Connection,
    *,
    event: str,
    actor: str = "",
    ip: str = "",
    detail: dict | str | None = None,
) -> None:
    """Append a security event to ``audit_log`` (best-effort).

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
    failures must not break the user-facing flow. Use
    :func:`security_event_or_raise` when you need fail-closed
    behaviour for high-impact mutations.
    """
    try:
        body = _format_security_body(actor, ip, detail)
        conn.execute(
            "INSERT INTO audit_log (media_item_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            ("_security", f"sec:{event}", body, now_iso()),
        )
        conn.commit()
    except Exception:  # pragma: no cover — never break flow on log failure
        logger.exception("security_event write failed event=%s", event)


def security_event_or_raise(
    conn: sqlite3.Connection,
    *,
    event: str,
    actor: str = "",
    ip: str = "",
    detail: dict | str | None = None,
) -> None:
    """Insert a security audit row inside the caller's transaction.

    Differences vs. :func:`security_event`:

    * Does NOT swallow exceptions — any DB error propagates so the
      caller's wider transaction can be rolled back.
    * Does NOT call ``conn.commit()``. The caller owns the transaction
      and commits after both the business mutation AND this audit row
      are queued, so the two land atomically.

    Use this for privilege-establishing changes — admin user create /
    delete, password change, sensitive settings writes, account
    lockout / unlock — where a "the mutation succeeded but no audit
    trail exists" outcome is itself a security incident.
    """
    body = _format_security_body(actor, ip, detail)
    conn.execute(
        "INSERT INTO audit_log (media_item_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
        ("_security", f"sec:{event}", body, now_iso()),
    )
