"""Repository functions for audit_log queries.

Encapsulates the paginated read patterns used by the history page and API.
The build_item / scrub helpers stay in the route module because they depend
on web-layer constants (ACTION_LABELS, ACTION_BADGE_CLASS) that the scanner
does not need.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Action names that target a show row rather than a media_items row.
# Pinning the JOIN to specific action names keeps a hypothetical Plex
# rating-key collision from surfacing a movie title against a show-action row.
SHOW_ACTIONS: tuple[str, ...] = ("kept_show", "removed_show_keep")

# Maps UI filter labels to the set of DB action names they match.
# ``kept`` and ``unkept`` each expand to multi-action IN clauses so the
# synthetic UI label matches both legacy and current DB action names.
_FILTER_MAP: dict[str, tuple[str, ...]] = {
    "kept": ("protected", "protected_forever", "kept", "kept_show"),
    "unkept": ("unprotected", "removed_show_keep"),
}


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One audit-log row joined with its media/show title columns.

    Returned by :func:`fetch_security_audit_rows` and
    :func:`fetch_media_audit_rows` so the history route consumes typed
    attributes instead of a raw :class:`sqlite3.Row`. Both queries
    project the same nine columns — the security path supplies ``NULL``
    for the three title columns so the shape is uniform.
    """

    id: int
    media_item_id: str
    action: str
    detail: str | None
    space_reclaimed_bytes: int | None
    created_at: str
    mi_title: str | None
    plex_rating_key: str | None
    ks_title: str | None


def _row_to_audit_row(row: sqlite3.Row) -> AuditRow:
    """Map a joined ``audit_log`` row to an :class:`AuditRow`."""
    return AuditRow(
        id=row["id"],
        media_item_id=row["media_item_id"],
        action=row["action"],
        detail=row["detail"],
        space_reclaimed_bytes=row["space_reclaimed_bytes"],
        created_at=row["created_at"],
        mi_title=row["mi_title"],
        plex_rating_key=row["plex_rating_key"],
        ks_title=row["ks_title"],
    )


def count_audit_rows(conn: sqlite3.Connection, action: str | None) -> int:
    """Return the total number of audit_log rows for the given filter.

    ``action="security"`` matches every ``sec:*`` event via a LIKE scan
    backed by ``idx_audit_log_action``.  Any other non-None value is an
    exact match.  None returns the global row count.
    """
    if action == "security":
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log al WHERE al.action LIKE ?",
            ("sec:%",),
        ).fetchone()
        return row["n"] if row else 0

    where_sql, where_params = _media_where_clause(action)
    # rationale: where_sql comes from a hard-coded filter map; no user input enters the SQL fragment
    row = conn.execute(  # nosec B608
        f"SELECT COUNT(*) AS n FROM audit_log al {where_sql}",
        where_params,
    ).fetchone()
    return row["n"] if row else 0


def fetch_security_audit_rows(
    conn: sqlite3.Connection, *, page: int, per_page: int
) -> list[AuditRow]:
    """Return a page of ``sec:*`` audit rows without joining any media tables.

    Security rows carry ``media_item_id='_security'`` which never matches
    a media_items.id or kept_shows.show_rating_key, so the JOINs are pure
    overhead for this path.  ``idx_audit_log_action`` covers the prefix
    LIKE so both the count and the page query are fast.
    """
    offset = (page - 1) * per_page
    rows = conn.execute(
        """
        SELECT
            al.id,
            al.media_item_id,
            al.action,
            al.detail,
            al.space_reclaimed_bytes,
            al.created_at,
            NULL AS mi_title,
            NULL AS plex_rating_key,
            NULL AS ks_title
        FROM audit_log al
        WHERE al.action LIKE ?
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
        """,
        ("sec:%", per_page, offset),
    ).fetchall()
    return [_row_to_audit_row(row) for row in rows]


def fetch_media_audit_rows(
    conn: sqlite3.Connection,
    *,
    action: str | None,
    page: int,
    per_page: int,
) -> list[AuditRow]:
    """Return a page of media-action audit rows.

    Security rows are NOT excluded by default so the unfiltered history view
    still surfaces them — the JOIN conditions skip ``_security`` rows so the
    JOINs are not wasted, and only the right table joins for
    show-vs-movie audit rows.
    """
    where_sql, where_params = _media_where_clause(action)
    offset = (page - 1) * per_page
    show_action_placeholders = ",".join("?" * len(SHOW_ACTIONS))
    params = (
        *SHOW_ACTIONS,  # for media_items NOT-IN
        *SHOW_ACTIONS,  # for kept_shows IN
        *where_params,
        per_page,
        offset,
    )
    # rationale: where_sql comes from a hard-coded filter map; no user input enters the SQL fragment
    rows = conn.execute(  # nosec B608
        f"""
        SELECT
            al.id,
            al.media_item_id,
            al.action,
            al.detail,
            al.space_reclaimed_bytes,
            al.created_at,
            mi.title AS mi_title,
            mi.plex_rating_key,
            ks.show_title AS ks_title
        FROM audit_log al
        LEFT JOIN media_items mi
          ON mi.id = al.media_item_id
            AND al.action NOT IN ({show_action_placeholders})
            AND al.media_item_id != '_security'
        LEFT JOIN kept_shows ks
          ON ks.show_rating_key = al.media_item_id
            AND al.action IN ({show_action_placeholders})
        {where_sql}
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [_row_to_audit_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _media_where_clause(action: str | None) -> tuple[str, tuple[str, ...]]:
    """Translate a UI filter name to a (WHERE SQL fragment, params) pair.

    The ``kept`` and ``unkept`` filters expand to multi-action IN clauses so
    the synthetic UI label matches both legacy and current DB action names.
    """
    if action and action in _FILTER_MAP:
        db_actions = _FILTER_MAP[action]
        placeholders = ",".join("?" * len(db_actions))
        return f"WHERE al.action IN ({placeholders})", db_actions
    if action:
        return "WHERE al.action = ?", (action,)
    return "", ()
