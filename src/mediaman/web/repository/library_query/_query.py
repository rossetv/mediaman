"""Core library query pipeline — CTE SQL, pagination, protection maps.

Builds the library-page CTE SQL, executes the paginated count + SELECT, loads
protection maps, and assembles the final result via `._display`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from mediaman.core.library_selected import LIBRARY_SELECTED
from mediaman.core.time import now_iso, now_utc
from mediaman.scanner.repository import fetch_kept_show_keys, fetch_protected_media_ids
from mediaman.scanner.runner import get_day_setting
from mediaman.web.repository.library_query._display import Keep, _shape_rows, lapse_key
from mediaman.web.repository.library_query._match import title_distance

VALID_SORTS = {
    "added_desc",
    "added_asc",
    "name_asc",
    "name_desc",
    "size_desc",
    "size_asc",
    "watched_desc",
    "watched_asc",
    "deletion_asc",
    "deletion_desc",
}
VALID_TYPES = {"movie", "tv", "anime", "kept", "stale"}

# Hard cap on the user-supplied search term. Without this an attacker could
# submit a multi-megabyte string to be compared with every title.
# 200 chars is well above any realistic title length.
MAX_SEARCH_TERM_LEN = 200

# Joins the ids of a show row's seasons; a unit separator never occurs in an id.
_ITEM_ID_SEP = "\x1f"

# Canonical media_type values that represent a TV / anime *season* row.
TV_SEASON_TYPES: tuple[str, ...] = ("tv_season", "tv", "season")
ANIME_SEASON_TYPES: tuple[str, ...] = ("anime_season", "anime")
ALL_SEASON_TYPES: tuple[str, ...] = TV_SEASON_TYPES + ANIME_SEASON_TYPES


def _matching_ids(conn: sqlite3.Connection, q: str) -> list[str]:
    """The ids of the items whose own title, or whose show's, holds *q*."""
    return [
        r["id"]
        for r in conn.execute("SELECT id, title, show_title FROM media_items")
        if any(
            title and title_distance(title, q) is not None
            for title in (r["title"], r["show_title"])
        )
    ]


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
        where_clauses.append("id IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(_matching_ids(conn, q[:MAX_SEARCH_TERM_LEN])))

    kept_filter = False
    if media_type == "kept":
        kept_filter = True
    elif media_type == "stale":
        _min_age = get_day_setting(conn, "min_age_days", default=30)
        _inactivity = get_day_setting(conn, "inactivity_days", default=30)
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


# A keep in force by the scanner's rule (fetch_protected_media_ids and
# fetch_kept_show_keys); the Kept filter pages in SQL, so it cannot call them.
_ITEM_KEPT_SQL = """EXISTS(
                SELECT 1 FROM scheduled_actions sa
                WHERE sa.media_item_id = filtered.id
                AND (sa.action = 'protected_forever'
                     OR (sa.action = 'snoozed' AND sa.execute_at IS NOT NULL
                         AND sa.execute_at >= (SELECT now FROM clock)))
            )"""
_SHOW_KEPT_SQL = """EXISTS(
                SELECT 1 FROM kept_shows ks
                WHERE ks.show_rating_key = filtered.show_rating_key
                AND (ks.action = 'protected_forever'
                     OR (ks.execute_at IS NOT NULL AND ks.execute_at > (SELECT now FROM clock)))
            )"""


# The soonest pending deletion of a media item, by the rule of
# watch_library.pending_deletions; NULL when none is scheduled.
_ITEM_DELETION_SQL = f"""(
                SELECT MIN(da.execute_at) FROM scheduled_actions da
                JOIN media_items mi ON mi.id = da.media_item_id
                WHERE da.media_item_id = filtered.id
                AND da.action = 'scheduled_deletion'
                AND da.token_used = 0
                AND (da.delete_status IS NULL OR da.delete_status = 'pending')
                AND da.execute_at >= (SELECT now FROM clock)
                AND {LIBRARY_SELECTED}
            )"""


def _build_cte_sql(where_sql: str, sort: str) -> tuple[str, str]:
    """Build the CTE SQL block and the ORDER BY expression.

    Returns ``(cte_sql, order_expr)`` where ``cte_sql`` ends just before the
    final SELECT so callers can append their own SELECT clause. Its first
    bind parameter is the current time, ahead of the WHERE clause's.
    # rationale: the CTE SQL literal is inherently multi-line; further
    # splitting would produce helpers that are just string fragments.
    """
    _CTE_SORT = {
        "added_desc": "added_at DESC",
        "added_asc": "added_at ASC",
        "name_asc": "title COLLATE NOCASE ASC",
        "name_desc": "title COLLATE NOCASE DESC",
        "size_desc": "file_size_bytes DESC",
        "size_asc": "file_size_bytes ASC",
        "watched_desc": "COALESCE(last_watched_at, '1970-01-01') DESC",
        "watched_asc": "COALESCE(last_watched_at, '1970-01-01') ASC",
        # Rows with nothing scheduled go last in either direction.
        "deletion_asc": "deletion_at IS NULL, deletion_at ASC, title COLLATE NOCASE ASC",
        "deletion_desc": "deletion_at IS NULL, deletion_at DESC, title COLLATE NOCASE ASC",
    }
    order = _CTE_SORT.get(sort, _CTE_SORT["added_desc"])

    # rationale: where_sql contains only ?-placeholder fragments and constant
    # identifiers — no user values enter the SQL text; values go in `params`.
    # ``order`` is resolved from ``_CTE_SORT`` via .get() with a hardcoded
    # default — no user value enters the SQL text.
    cte_sql = f"""
    WITH clock(now) AS (SELECT ?),
    filtered AS (
        SELECT * FROM media_items {where_sql}
    ),
    display_items AS (
        SELECT
            id, title, 'movie' AS display_type,
            plex_rating_key, added_at, file_size_bytes, last_watched_at,
            show_rating_key, show_title, NULL AS season_count,
            0 AS has_rating_key, id AS item_ids,
            {_ITEM_KEPT_SQL} AS is_kept,
            {_ITEM_DELETION_SQL} AS deletion_at
        FROM filtered WHERE media_type = 'movie'

        UNION ALL

        SELECT
            MIN(id) AS id,
            COALESCE(show_title, title) AS title,
            CASE WHEN MAX(media_type) LIKE 'anime%' THEN 'anime' ELSE 'tv' END AS display_type,
            COALESCE(MAX(NULLIF(show_rating_key, '')), MIN(plex_rating_key)) AS plex_rating_key,
            MAX(added_at) AS added_at,
            SUM(file_size_bytes) AS file_size_bytes,
            MAX(last_watched_at) AS last_watched_at,
            COALESCE(show_rating_key, show_title) AS show_rating_key,
            COALESCE(show_title, title) AS show_title,
            COUNT(*) AS season_count,
            MAX(COALESCE(show_rating_key, '') != '') AS has_rating_key,
            GROUP_CONCAT(id, char(31)) AS item_ids,
            (MAX({_SHOW_KEPT_SQL}) OR MAX({_ITEM_KEPT_SQL})) AS is_kept,
            MIN({_ITEM_DELETION_SQL}) AS deletion_at
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
    # rationale: kept_where is a closed literal ternary (" WHERE is_kept = 1" or ""); order is
    # resolved from _CTE_SORT via .get() with a hardcoded default — no user value enters the SQL text
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
    now: str,
) -> tuple[dict[str, dict[str, Keep]], dict[str, Keep]]:
    """Load the keeps in force for *rows*, by the scanner's protection rule.

    Returns ``(sa_map, ks_map)``.  ``sa_map`` is keyed by the row id and holds
    the strongest ``(action, execute_at)`` of each kept media item in the row
    (one per season the row's filter let through), keyed by that item's id.
    ``ks_map`` is keyed by show_rating_key and holds only keeps in force.
    """
    row_items = {str(r["id"]): str(r["item_ids"]).split(_ITEM_ID_SEP) for r in rows}
    item_ids = sorted({i for ids in row_items.values() for i in ids})
    show_rkeys = sorted({r["show_rating_key"] for r in rows if r["show_rating_key"]})

    kept_ids = sorted(fetch_protected_media_ids(conn, item_ids, now))
    strongest: dict[str, Keep] = {}
    if kept_ids:
        # rationale: ph is purely "?" * len(kept_ids) — no user value ever enters the SQL text
        ph = ",".join("?" * len(kept_ids))
        for sa in conn.execute(
            f"SELECT media_item_id, action, execute_at "
            f"FROM scheduled_actions "
            f"WHERE media_item_id IN ({ph}) "
            f"AND action IN ('protected_forever', 'snoozed')",
            kept_ids,
        ).fetchall():
            keep = (sa["action"], sa["execute_at"])
            prev = strongest.get(sa["media_item_id"])
            if prev is None or lapse_key(keep) > lapse_key(prev):
                strongest[sa["media_item_id"]] = keep
    sa_map = {
        rid: {i: strongest[i] for i in ids if i in strongest} for rid, ids in row_items.items()
    }

    kept_keys = sorted(fetch_kept_show_keys(conn, show_rkeys, now))
    ks_map: dict[str, Keep] = {}
    if kept_keys:
        # rationale: ph is purely "?" * len(kept_keys) — no user value ever enters the SQL text
        ph = ",".join("?" * len(kept_keys))
        for ks in conn.execute(
            f"SELECT show_rating_key, action, execute_at "
            f"FROM kept_shows WHERE show_rating_key IN ({ph})",
            kept_keys,
        ).fetchall():
            ks_map[ks["show_rating_key"]] = (ks["action"], ks["execute_at"])

    return sa_map, ks_map


# rationale: a repository may return ``list[dict]`` at a documented
# template-feeding boundary. ``mediaman.web.routes.watch.list_page`` passes
# these display-ready dicts onto the Watch list view's ``_watch_list.html``
# template as ``items``, and ``/api/library`` answers them as JSON; the keys are
# display-shaped (``type_label``, ``protection_label``, ...) and a dataclass would
# add ceremony without removing the template's column coupling.
def fetch_library(
    conn: sqlite3.Connection,
    q: str = "",
    media_type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict[str, object]], int]:
    now = now_iso()
    where_sql, params, kept_filter = _build_where_clause(conn, q, media_type)
    cte_sql, order = _build_cte_sql(where_sql, sort)
    rows, total = _execute_paged_query(
        conn, cte_sql, order, [now, *params], kept_filter, page, per_page
    )
    sa_map, ks_map = _fetch_protection_maps(conn, rows, now)
    items = _shape_rows(rows, sa_map, ks_map)
    return items, total
