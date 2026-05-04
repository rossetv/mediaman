"""Security event and media-action audit log.

Writes structured records to the ``audit_log`` table. Two distinct write
families exist:

**Media-action auditing** (:func:`log_audit`)
    Records deletions, snoozes, and other media-item mutations.  The
    ``space_reclaimed_bytes`` column is ``NULL`` when no space was freed.

**Security-event auditing** (:func:`security_event`, :func:`security_event_or_raise`)
    Records authentication and privilege events with a ``sec:`` prefix on
    the ``action`` column so they are easy to filter independently of media
    actions.

Transaction-ownership semantics
--------------------------------
None of the functions in this module call ``conn.commit()``.  This is
deliberate: the audit row must land in the *same* transaction as the
business mutation it records.  Callers are responsible for committing
(or rolling back) after both the business row and the audit row have been
written.  If the audit INSERT fails and the caller is using
:func:`security_event_or_raise`, the exception propagates so the caller's
transaction is aborted — ensuring we never have a "the mutation happened
but no one knows" situation.

The sole exception is :func:`security_event` (best-effort path), which
*does* call ``conn.commit()`` after the INSERT because it is used for
low-stakes events (login.success, session.destroy) that are not part of a
wider transaction.
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
    actor: str | None = None,
) -> None:
    """Insert a row into ``audit_log`` for a media action.

    Args:
        conn: Open SQLite connection.
        media_item_id: The media item ID (or a surrogate like a title string).
            By convention, ``"_security"`` is used for security events
            written via :func:`security_event` / :func:`security_event_or_raise`.
        action: Short action label, e.g. ``"deleted"``, ``"snoozed"``.
        detail: Human-readable detail string.
        space_bytes: Optional value for the ``space_reclaimed_bytes`` column;
            omit (or pass ``None``) for events where no space was reclaimed.
        actor: Username of the admin who triggered the action, or ``None``
            for scanner-driven (autonomous) events. Stored in the
            dedicated ``actor`` column so operators can run
            ``WHERE actor = 'alice'`` instead of grepping ``detail``.

    Does **not** call ``conn.commit()`` — callers are responsible for
    committing in their own transaction so the audit row and the business
    row land in the same commit.
    """
    conn.execute(
        "INSERT INTO audit_log "
        "(media_item_id, action, detail, space_reclaimed_bytes, created_at, actor) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (media_item_id, action, detail, space_bytes, now_iso(), actor),
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


def _insert_security_event(
    conn: sqlite3.Connection,
    *,
    event: str,
    actor: str,
    ip: str,
    detail: dict | str | None,
) -> None:
    """Execute the ``audit_log`` INSERT for a security event.

    Shared by :func:`security_event` and :func:`security_event_or_raise`.
    Does not commit and does not catch exceptions — both of those
    concerns belong to the caller.
    """
    body = _format_security_body(actor, ip, detail)
    conn.execute(
        "INSERT INTO audit_log "
        "(media_item_id, action, detail, created_at, actor) "
        "VALUES (?, ?, ?, ?, ?)",
        ("_security", f"sec:{event}", body, now_iso(), actor),
    )


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
        _insert_security_event(conn, event=event, actor=actor, ip=ip, detail=detail)
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
    _insert_security_event(conn, event=event, actor=actor, ip=ip, detail=detail)
