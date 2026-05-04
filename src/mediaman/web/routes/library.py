"""Library page routes.

Handles the browser-facing GET /library page.  All JSON API endpoints
(``/api/library``, ``/api/media/…``) live in the sibling module
:mod:`mediaman.web.routes.library_api`.

Query helpers (``fetch_library``, ``fetch_stats``, private helpers) and
the shared constants (``_VALID_SORTS``, ``_VALID_TYPES``,
``TV_SEASON_TYPES``, etc.) are defined inline here rather than in a
separate ``_query`` sub-module; they were split only because the old
package had five files.  The private names remain importable under their
original ``mediaman.web.routes.library._query`` path via the
``_query`` attribute shim at the bottom of this file so existing tests
that import directly from that path continue to work.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from mediaman.auth.middleware import resolve_page_session
from mediaman.services.infra.format import days_ago, format_bytes
from mediaman.services.infra.settings_reader import get_int_setting
from mediaman.services.infra.time import parse_iso_utc
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED

# ---------------------------------------------------------------------------
# Shared constants — used by both this module and library_api.
# ---------------------------------------------------------------------------

_VALID_SORTS = {
    "added_desc",
    "added_asc",
    "name_asc",
    "name_desc",
    "size_desc",
    "size_asc",
    "watched_desc",
    "watched_asc",
}
_VALID_TYPES = {"movie", "tv", "anime", "kept", "stale"}

# Hard cap on the user-supplied search term applied to the LIKE filter.
# Without this an attacker could submit a multi-megabyte string and force
# SQLite to do a slow scan against every title and show_title row.
# 200 chars is well above any realistic title length.
_MAX_SEARCH_TERM_LEN = 200

# Canonical media_type values that represent a TV / anime *season* row.
TV_SEASON_TYPES: tuple[str, ...] = ("tv_season", "tv", "season")
ANIME_SEASON_TYPES: tuple[str, ...] = ("anime_season", "anime")
ALL_SEASON_TYPES: tuple[str, ...] = TV_SEASON_TYPES + ANIME_SEASON_TYPES


# ---------------------------------------------------------------------------
# Private query helpers
# ---------------------------------------------------------------------------


def _days_ago(dt_str: str | None) -> str:
    """Return 'N days ago' or '' given an ISO datetime string."""
    dt = parse_iso_utc(dt_str)
    if dt is None:
        return ""
    delta = (datetime.now(UTC) - dt).days
    if delta > 3650:
        return ""
    return days_ago(dt_str)


def _type_css(media_type: str) -> str:
    """Return the CSS class for a type badge."""
    if media_type in ("tv_season", "season", "tv"):
        return "type-tv"
    if media_type in ("anime_season", "anime"):
        return "type-anime"
    return "type-mov"


def _protection_label(sa_action: str | None, sa_execute_at: str | None) -> str | None:
    """Return a human-friendly protection label, or None if not protected."""
    if sa_action is None:
        return None
    if sa_action == ACTION_PROTECTED_FOREVER:
        return "Kept forever"
    if sa_action == ACTION_SNOOZED and sa_execute_at:
        try:
            execute_at = datetime.fromisoformat(sa_execute_at)
            if execute_at.tzinfo is None:
                execute_at = execute_at.replace(tzinfo=UTC)
            delta = (execute_at - datetime.now(UTC)).days
            if delta <= 0:
                return None
            return f"Kept for {delta} more day{'s' if delta != 1 else ''}"
        except (ValueError, TypeError):
            return None
    return None


def fetch_library(
    conn: sqlite3.Connection,
    q: str = "",
    media_type: str = "",
    sort: str = "added_desc",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[dict[str, object]], int]:
    """Query media_items and return (items, total_count)."""
    where_clauses: list[str] = []
    params: list[object] = []

    if q:
        # Cap the LIKE term before escaping — escaping before truncation
        # would let metacharacters at position _MAX_SEARCH_TERM_LEN-1
        # split mid-escape and produce a malformed pattern.  Truncate raw,
        # then escape.
        q = q[:_MAX_SEARCH_TERM_LEN]
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
        _now = datetime.now(UTC)
        age_cutoff = (_now - timedelta(days=_min_age)).isoformat()
        watch_cutoff = (_now - timedelta(days=_inactivity)).isoformat()
        where_clauses.append("added_at < ?")
        params.append(age_cutoff)
        where_clauses.append("(last_watched_at IS NULL OR last_watched_at < ?)")
        params.append(watch_cutoff)
    elif media_type and media_type in _VALID_TYPES:
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

    items = []
    for r in rows:
        display_type = r["display_type"]
        is_tv = display_type in ("tv", "anime")
        show_rk = r["show_rating_key"] or ""
        show_title = r["show_title"] or r["title"]

        protected = False
        protection_label = None
        if is_tv and show_rk:
            ks_entry = ks_map.get(show_rk)
            if ks_entry:
                protection_label = _protection_label(ks_entry[0], ks_entry[1])
                protected = protection_label is not None
        if not protected:
            sa_entry = sa_map.get(str(r["id"]))
            if sa_entry:
                protection_label = _protection_label(sa_entry[0], sa_entry[1])
                protected = protection_label is not None

        season_count = r["season_count"]
        if is_tv:
            if season_count and season_count > 1:
                type_label = f"{season_count} seasons"
            else:
                type_label = "1 season"
        else:
            type_label = "MOVIE"

        added_ago = _days_ago(r["added_at"])
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
                "type_css": _type_css(display_type),
                "plex_rating_key": r["plex_rating_key"],
                "added_at": r["added_at"],
                "added_ago": added_ago,
                "file_size": format_bytes(r["file_size_bytes"] or 0),
                "file_size_bytes": r["file_size_bytes"] or 0,
                "last_watched": _days_ago(r["last_watched_at"]),
                "show_rating_key": show_rk,
                "show_title_raw": show_title,
                "is_tv": is_tv,
                "protected": protected,
                "protection_label": protection_label,
            }
        )

    return items, total


def fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts and stale count for the library stats bar.

    The TV/anime totals use the same grouping definition as
    :func:`fetch_library`'s display_items CTE —
    ``COALESCE(show_rating_key, show_title)``.  When a show has NULL in
    both columns SQLite's ``COUNT(DISTINCT NULL)`` returned 0 while the
    CTE's ``GROUP BY NULL`` collapsed every such row into a single group
    of 1, so the two queries reported different totals on the same data.

    Wrapping the count in a sub-select with ``GROUP BY`` (instead of
    ``COUNT(DISTINCT ...)``) makes the NULL behaviour match: NULL is a
    group in its own right, counted as 1.
    """
    movies = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()["n"]

    tv_placeholders = ",".join("?" * len(TV_SEASON_TYPES))
    tv = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({tv_placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        TV_SEASON_TYPES,
    ).fetchone()["n"]

    anime_placeholders = ",".join("?" * len(ANIME_SEASON_TYPES))
    anime = conn.execute(
        f"SELECT COUNT(*) AS n FROM ("
        f"  SELECT 1 FROM media_items "
        f"  WHERE media_type IN ({anime_placeholders}) "
        f"  GROUP BY COALESCE(show_rating_key, show_title)"
        f")",
        ANIME_SEASON_TYPES,
    ).fetchone()["n"]

    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = datetime.now(UTC)
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
    """,
        (age_cutoff, watch_cutoff),
    ).fetchone()["n"]

    total = movies + tv + anime
    total_size_row = conn.execute("SELECT SUM(file_size_bytes) AS n FROM media_items").fetchone()
    total_size = format_bytes(total_size_row["n"] or 0)

    return {
        "movies": movies,
        "tv": tv,
        "anime": anime,
        "stale": stale,
        "stale_min_age": min_age,
        "total": total,
        "total_size": total_size,
    }


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/library", response_class=HTMLResponse)
def library_page(
    request: Request,
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = Query(default=1, ge=1, le=100_000),
    per_page: int = Query(default=20, ge=1, le=100),
) -> Response:
    """Render the library page.  Redirects to /login if the session is invalid."""
    resolved = resolve_page_session(request)
    if isinstance(resolved, RedirectResponse):
        return resolved
    username, conn = resolved

    # Sort/type silently reset to defaults — these are vocabulary fields and an
    # unknown value is treated as "no filter" rather than an outright error.
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    stats = fetch_stats(conn)

    total_pages = max(1, (total + per_page - 1) // per_page)
    page_start = (page - 1) * per_page + 1 if total else 0
    page_end = min(page * per_page, total)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "username": username,
            "nav_active": "library",
            "items": items,
            "stats": stats,
            "q": q,
            "current_type": media_type,
            "current_sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "page_start": page_start,
            "page_end": page_end,
        },
    )


# ---------------------------------------------------------------------------
# Compatibility shim — tests that were written against the old package
# structure import from ``mediaman.web.routes.library._query``.  Expose a
# ``_query`` attribute on this module so those imports continue to work
# without needing to migrate every test.
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402

_query = _types.ModuleType("mediaman.web.routes.library._query")
_query._VALID_SORTS = _VALID_SORTS  # type: ignore[attr-defined]
_query._VALID_TYPES = _VALID_TYPES  # type: ignore[attr-defined]
_query._MAX_SEARCH_TERM_LEN = _MAX_SEARCH_TERM_LEN  # type: ignore[attr-defined]
_query.TV_SEASON_TYPES = TV_SEASON_TYPES  # type: ignore[attr-defined]
_query.ANIME_SEASON_TYPES = ANIME_SEASON_TYPES  # type: ignore[attr-defined]
_query.ALL_SEASON_TYPES = ALL_SEASON_TYPES  # type: ignore[attr-defined]
_query._days_ago = _days_ago  # type: ignore[attr-defined]
_query._type_css = _type_css  # type: ignore[attr-defined]
_query._protection_label = _protection_label  # type: ignore[attr-defined]
_query.fetch_library = fetch_library  # type: ignore[attr-defined]
_query.fetch_stats = fetch_stats  # type: ignore[attr-defined]

import sys as _sys  # noqa: E402

_sys.modules.setdefault("mediaman.web.routes.library._query", _query)
