"""SQL queries for the library display view.

Shared between the browser-facing library page
(:mod:`mediaman.web.routes.library`) and the JSON API
(:mod:`mediaman.web.routes.library_api`) so neither route module needs to
import from the other (an inter-route import violation per §2.8.6).

This module is the canonical location for library-display SQL.  An
earlier revision lived under ``mediaman.scanner.repository`` despite
serving only web callers; it has been relocated here to match the
table-group ownership rule (§9.4).

Functions
---------
``fetch_library``
    Paginated query against ``media_items`` / ``scheduled_actions`` /
    ``kept_shows``, returning display-ready dicts plus a total-row count.

Constants
---------
``VALID_SORTS``  — accepted ``sort=`` parameter values.
``VALID_TYPES``  — accepted ``type=`` filter values.
``TV_SEASON_TYPES``, ``ANIME_SEASON_TYPES``, ``ALL_SEASON_TYPES``
    Canonical ``media_type`` values used in SQL ``IN`` clauses.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from mediaman.core.format import days_ago as _days_ago_fmt
from mediaman.core.format import format_bytes, relative_day_label
from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED
from mediaman.core.time import now_utc, parse_iso_strict_utc, parse_iso_utc
from mediaman.services.infra.settings_reader import get_int_setting

# ---------------------------------------------------------------------------
# Shared constants — used by both library.py and library_api/__init__.py.
# ---------------------------------------------------------------------------

VALID_SORTS = {
    "added_desc",
    "added_asc",
    "name_asc",
    "name_desc",
    "size_desc",
    "size_asc",
    "watched_desc",
    "watched_asc",
}
VALID_TYPES = {"movie", "tv", "anime", "kept", "stale"}

# Hard cap on the user-supplied search term applied to the LIKE filter.
# Without this an attacker could submit a multi-megabyte string and force
# SQLite to do a slow scan against every title and show_title row.
# 200 chars is well above any realistic title length.
MAX_SEARCH_TERM_LEN = 200

# Canonical media_type values that represent a TV / anime *season* row.
TV_SEASON_TYPES: tuple[str, ...] = ("tv_season", "tv", "season")
ANIME_SEASON_TYPES: tuple[str, ...] = ("anime_season", "anime")
ALL_SEASON_TYPES: tuple[str, ...] = TV_SEASON_TYPES + ANIME_SEASON_TYPES


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


def days_ago(dt_str: str | None) -> str:
    """Return 'N days ago' or '' given an ISO datetime string."""
    dt = parse_iso_utc(dt_str)
    if dt is None:
        return ""
    delta = (now_utc() - dt).days
    if delta > 3650:
        return ""
    return _days_ago_fmt(dt_str)


def type_css(media_type: str) -> str:
    """Return the CSS class for a type badge."""
    if media_type in ("tv_season", "season", "tv"):
        return "type-tv"
    if media_type in ("anime_season", "anime"):
        return "type-anime"
    return "type-mov"


def protection_label(sa_action: str | None, sa_execute_at: str | None) -> str | None:
    """Return a human-friendly protection label, or None if not protected."""
    if sa_action is None:
        return None
    if sa_action == ACTION_PROTECTED_FOREVER:
        return "Kept forever"
    if sa_action == ACTION_SNOOZED and sa_execute_at:
        execute_at = parse_iso_strict_utc(sa_execute_at)
        if execute_at is None:
            return None
        # ``protection_label`` returns None for today/past dates rather
        # than a string, so we pre-filter and only invoke
        # :func:`relative_day_label` on future deadlines.  The helper's
        # tomorrow case is the singular "1 more day"; its future arm
        # plural.
        now = now_utc()
        if (execute_at - now).days <= 0:
            return None
        return relative_day_label(
            execute_at,
            now=now,
            today="",  # unreachable: filtered above
            tomorrow="Kept for 1 more day",
            future=lambda days: f"Kept for {days} more days",
        )
    return None


def _build_where_clause(
    conn: sqlite3.Connection,
    q: str,
    media_type: str,
) -> tuple[str, list[object], bool]:
    """Build the SQL WHERE clause and bind params from search/filter inputs.

    Returns ``(where_sql, params, kept_filter)`` where ``kept_filter`` is
    True when the caller requested the "kept" virtual type (handled as a
    post-CTE filter rather than a media_type column match).
    """
    where_clauses: list[str] = []
    params: list[object] = []

    if q:
        # Cap the LIKE term before escaping — escaping before truncation
        # would let metacharacters at position MAX_SEARCH_TERM_LEN-1
        # split mid-escape and produce a malformed pattern.  Truncate raw,
        # then escape.
        q = q[:MAX_SEARCH_TERM_LEN]
        where_clauses.append("(title LIKE ? ESCAPE '\\' OR show_title LIKE ? ESCAPE '\\')")
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_escaped}%"
        params.extend([like, like])

    kept_filter = False
    if media_type == "kept":
        kept_filter = True
    elif media_type == "stale":
        _min_age = get_int_setting(conn, "min_age_days", default=30)
        _inactivity = get_int_setting(conn, "inactivity_days", default=30)
        _now = now_utc()
        age_cutoff = (_now - timedelta(days=_min_age)).isoformat()
        watch_cutoff = (_now - timedelta(days=_inactivity)).isoformat()
        where_clauses.append("added_at < ?")
        params.append(age_cutoff)
        where_clauses.append("(last_watched_at IS NULL OR last_watched_at < ?)")
        params.append(watch_cutoff)
    elif media_type and media_type in VALID_TYPES:
        _TYPE_MAP = {
            "movie": ("movie",),
            "tv": TV_SEASON_TYPES,
            "anime": ANIME_SEASON_TYPES,
        }
        db_types = _TYPE_MAP.get(media_type, (media_type,))
        placeholders = ",".join("?" * len(db_types))
        where_clauses.append(f"media_type IN ({placeholders})")
        params.extend(db_types)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    return where_sql, params, kept_filter


def _build_cte_sql(where_sql: str, sort: str) -> tuple[str, str]:
    """Build the CTE SQL block and the ORDER BY expression.

    Returns ``(cte_sql, order_expr)`` where ``cte_sql`` ends just before the
    final SELECT so callers can append their own SELECT clause.
    # rationale: the CTE SQL literal is inherently multi-line; further
    # splitting would produce helpers that are just string fragments.
    """
    _CTE_SORT = {
        "added_desc": "added_at DESC",
        "added_asc": "added_at ASC",
        "name_asc": "title ASC COLLATE NOCASE",
        "name_desc": "title DESC COLLATE NOCASE",
        "size_desc": "file_size_bytes DESC",
        "size_asc": "file_size_bytes ASC",
        "watched_desc": "COALESCE(last_watched_at, '1970-01-01') DESC",
        "watched_asc": "COALESCE(last_watched_at, '1970-01-01') ASC",
    }
    order = _CTE_SORT.get(sort, _CTE_SORT["added_desc"])

    cte_sql = f"""
    WITH filtered AS (
        SELECT * FROM media_items {where_sql}
    ),
    display_items AS (
        SELECT
            id, title, 'movie' AS display_type,
            plex_rating_key, added_at, file_size_bytes, last_watched_at,
            show_rating_key, show_title, NULL AS season_count,
            EXISTS(
                SELECT 1 FROM scheduled_actions sa
                WHERE sa.media_item_id = filtered.id AND sa.token_used = 0
                AND sa.action IN ('protected_forever', 'snoozed')
            ) AS is_kept
        FROM filtered WHERE media_type = 'movie'

        UNION ALL

        SELECT
            MIN(id) AS id,
            COALESCE(show_title, title) AS title,
            CASE WHEN MAX(media_type) LIKE 'anime%' THEN 'anime' ELSE 'tv' END AS display_type,
            MIN(plex_rating_key) AS plex_rating_key,
            MAX(added_at) AS added_at,
            SUM(file_size_bytes) AS file_size_bytes,
            MAX(last_watched_at) AS last_watched_at,
            COALESCE(show_rating_key, show_title) AS show_rating_key,
            COALESCE(show_title, title) AS show_title,
            COUNT(*) AS season_count,
            (
                EXISTS(
                    SELECT 1 FROM kept_shows ks
                    WHERE ks.show_rating_key = COALESCE(filtered.show_rating_key, filtered.show_title)
                ) OR EXISTS(
                    SELECT 1 FROM scheduled_actions sa
                    WHERE sa.media_item_id IN (
                        SELECT mi2.id FROM media_items mi2
                        WHERE COALESCE(mi2.show_rating_key, mi2.show_title) = COALESCE(filtered.show_rating_key, filtered.show_title)
                    ) AND sa.token_used = 0 AND sa.action IN ('protected_forever', 'snoozed')
                )
            ) AS is_kept
        FROM filtered
        WHERE media_type IN ('tv_season', 'anime_season', 'season', 'tv', 'anime')
        GROUP BY COALESCE(show_rating_key, show_title)
    )
    """
    return cte_sql, order


def _execute_paged_query(
    conn: sqlite3.Connection,
    cte_sql: str,
    order: str,
    params: list[object],
    kept_filter: bool,
    page: int,
    per_page: int,
) -> tuple[list[sqlite3.Row], int]:
    """Execute the count + paginated SELECT and return (rows, total)."""
    kept_where = " WHERE is_kept = 1" if kept_filter else ""

    count_row = conn.execute(
        cte_sql + f"SELECT COUNT(*) AS n FROM display_items{kept_where}",
        params,
    ).fetchone()
    total = count_row["n"]

    offset = (page - 1) * per_page
    offset = min(offset, 50_000)
    rows = conn.execute(
        cte_sql + f"SELECT * FROM display_items{kept_where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    return rows, total


def _fetch_protection_maps(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> tuple[dict[str, tuple[str, str | None]], dict[str, tuple[str, str | None]]]:
    """Load scheduled_actions and kept_shows protection data for *rows*.

    Returns ``(sa_map, ks_map)`` keyed by media_item_id and show_rating_key
    respectively.  Both maps hold ``(action, execute_at)`` tuples.
    """
    item_ids = [r["id"] for r in rows]
    show_rkeys = {r["show_rating_key"] for r in rows if r["show_rating_key"]}

    sa_map: dict[str, tuple[str, str | None]] = {}
    if item_ids:
        ph = ",".join("?" * len(item_ids))
        for sa in conn.execute(
            f"SELECT media_item_id, action, execute_at "
            f"FROM scheduled_actions "
            f"WHERE media_item_id IN ({ph}) AND token_used = 0 "
            f"AND action IN ('protected_forever', 'snoozed')",
            item_ids,
        ).fetchall():
            prev = sa_map.get(sa["media_item_id"])
            if prev is None or prev[0] != ACTION_PROTECTED_FOREVER:
                sa_map[sa["media_item_id"]] = (sa["action"], sa["execute_at"])

    ks_map: dict[str, tuple[str, str | None]] = {}
    if show_rkeys:
        ph = ",".join("?" * len(show_rkeys))
        for ks in conn.execute(
            f"SELECT show_rating_key, action, execute_at "
            f"FROM kept_shows WHERE show_rating_key IN ({ph})",
            list(show_rkeys),
        ).fetchall():
            ks_map[ks["show_rating_key"]] = (ks["action"], ks["execute_at"])

    return sa_map, ks_map


def _shape_rows(
    rows: list[sqlite3.Row],
    sa_map: dict[str, tuple[str, str | None]],
    ks_map: dict[str, tuple[str, str | None]],
) -> list[dict[str, object]]:
    """Convert raw DB rows into display-ready dicts.

    # rationale: single cohesive loop building one output dict per row;
    # the 18-key output dict is the natural seam and cannot be split further.
    """
    items = []
    for r in rows:
        display_type = r["display_type"]
        is_tv = display_type in ("tv", "anime")
        show_rk = r["show_rating_key"] or ""
        show_title = r["show_title"] or r["title"]

        protected = False
        prot_label: str | None = None
        if is_tv and show_rk:
            ks_entry = ks_map.get(show_rk)
            if ks_entry:
                prot_label = protection_label(ks_entry[0], ks_entry[1])
                protected = prot_label is not None
        if not protected:
            sa_entry = sa_map.get(str(r["id"]))
            if sa_entry:
                prot_label = protection_label(sa_entry[0], sa_entry[1])
                protected = prot_label is not None

        season_count = r["season_count"]
        if is_tv:
            if season_count and season_count > 1:
                type_label = f"{season_count} seasons"
            else:
                type_label = "1 season"
        else:
            type_label = "MOVIE"

        added_ago = days_ago(r["added_at"])
        subtitle_parts = []
        if added_ago:
            prefix = "Last added" if is_tv else "Added"
            subtitle_parts.append(f"{prefix} {added_ago}")

        items.append(
            {
                "id": r["id"],
                "title": r["title"],
                "subtitle": " · ".join(subtitle_parts),
                "media_type": display_type,
                "type_label": type_label,
                "type_css": type_css(display_type),
                "plex_rating_key": r["plex_rating_key"],
                "added_at": r["added_at"],
                "added_ago": added_ago,
                "file_size": format_bytes(r["file_size_bytes"] or 0),
                "file_size_bytes": r["file_size_bytes"] or 0,
                "last_watched": days_ago(r["last_watched_at"]),
                "show_rating_key": show_rk,
                "show_title_raw": show_title,
                "is_tv": is_tv,
                "protected": protected,
                "protection_label": prot_label,
            }
        )
    return items


def fetch_library(
    conn: sqlite3.Connection,
    q: str = "",
    media_type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict[str, object]], int]:
    """Query media_items and return (items, total_count).

    Pipeline:
    1. Build the WHERE clause from search/filter inputs.
    2. Build the display CTE SQL and ORDER BY expression.
    3. Execute the count + paginated SELECT.
    4. Load scheduled_actions / kept_shows protection data.
    5. Convert raw rows into display-ready dicts.
    """
    where_sql, params, kept_filter = _build_where_clause(conn, q, media_type)
    cte_sql, order = _build_cte_sql(where_sql, sort)
    rows, total = _execute_paged_query(conn, cte_sql, order, params, kept_filter, page, per_page)
    sa_map, ks_map = _fetch_protection_maps(conn, rows)
    items = _shape_rows(rows, sa_map, ks_map)
    return items, total


# ---------------------------------------------------------------------------
# Stats-bar counts — used by the library page render alongside fetch_library.
# Each helper does a single COUNT(*); the view-model assembler lives in the
# library route.
# ---------------------------------------------------------------------------


def count_movies(conn: sqlite3.Connection) -> int:
    """Return the count of movie rows in ``media_items``."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()
    return int(row["n"])


def count_tv_shows(conn: sqlite3.Connection) -> int:
    """Return the count of distinct TV shows (grouped by show_rating_key/show_title)."""
    placeholders = ",".join("?" * len(TV_SEASON_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        TV_SEASON_TYPES,
    ).fetchone()
    return int(row["n"])


def count_anime_shows(conn: sqlite3.Connection) -> int:
    """Return the count of distinct anime shows (grouped by show_rating_key/show_title)."""
    placeholders = ",".join("?" * len(ANIME_SEASON_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        ANIME_SEASON_TYPES,
    ).fetchone()
    return int(row["n"])


def count_stale(conn: sqlite3.Connection, *, age_cutoff: str, watch_cutoff: str) -> int:
    """Return the count of media items older than *age_cutoff* and unwatched since *watch_cutoff*."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
        """,
        (age_cutoff, watch_cutoff),
    ).fetchone()
    return int(row["n"])


def sum_total_size_bytes(conn: sqlite3.Connection) -> int:
    """Return the total file_size_bytes across all media items (0 when empty)."""
    row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    return int(row["n"] or 0)


__all__ = [
    "ALL_SEASON_TYPES",
    "ANIME_SEASON_TYPES",
    "MAX_SEARCH_TERM_LEN",
    "TV_SEASON_TYPES",
    "VALID_SORTS",
    "VALID_TYPES",
    "count_anime_shows",
    "count_movies",
    "count_stale",
    "count_tv_shows",
    "days_ago",
    "fetch_library",
    "protection_label",
    "sum_total_size_bytes",
    "type_css",
]
