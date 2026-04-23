"""Library query helpers — fetch and shape media_items from SQLite."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from mediaman.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED
from mediaman.services.format import days_ago, format_bytes, parse_iso_utc
from mediaman.services.settings_reader import get_int_setting

_VALID_SORTS = {"added_desc", "added_asc", "name_asc", "name_desc", "size_desc", "size_asc", "watched_desc", "watched_asc"}
_VALID_TYPES = {"movie", "tv", "anime", "kept", "stale"}


def _days_ago(dt_str: str | None) -> str:
    """Return 'N days ago' or '' given an ISO datetime string."""
    dt = parse_iso_utc(dt_str)
    if dt is None:
        return ""
    delta = (datetime.now(timezone.utc) - dt).days
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
                execute_at = execute_at.replace(tzinfo=timezone.utc)
            delta = (execute_at - datetime.now(timezone.utc)).days
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
        where_clauses.append("(title LIKE ? ESCAPE \'\\\' OR show_title LIKE ? ESCAPE \'\\\')")
        q_escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{q_escaped}%"
        params.extend([like, like])

    kept_filter = False
    if media_type == "kept":
        kept_filter = True
    elif media_type == "stale":
        _min_age = get_int_setting(conn, "min_age_days", default=30)
        _inactivity = get_int_setting(conn, "inactivity_days", default=30)
        _now = datetime.now(timezone.utc)
        age_cutoff = (_now - timedelta(days=_min_age)).isoformat()
        watch_cutoff = (_now - timedelta(days=_inactivity)).isoformat()
        where_clauses.append("added_at < ?")
        params.append(age_cutoff)
        where_clauses.append("(last_watched_at IS NULL OR last_watched_at < ?)")
        params.append(watch_cutoff)
    elif media_type and media_type in _VALID_TYPES:
        _TYPE_MAP = {
            "movie": ("movie",),
            "tv": ("tv_season", "tv", "season"),
            "anime": ("anime_season", "anime"),
        }
        db_types = _TYPE_MAP.get(media_type, (media_type,))
        placeholders = ",".join("?" * len(db_types))
        where_clauses.append(f"media_type IN ({placeholders})")
        params.extend(db_types)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    _CTE_SORT = {
        "added_desc":   "added_at DESC",
        "added_asc":    "added_at ASC",
        "name_asc":     "title ASC COLLATE NOCASE",
        "name_desc":    "title DESC COLLATE NOCASE",
        "size_desc":    "file_size_bytes DESC",
        "size_asc":     "file_size_bytes ASC",
        "watched_desc": "COALESCE(last_watched_at, '1970-01-01') DESC",
        "watched_asc":  "COALESCE(last_watched_at, '1970-01-01') ASC",
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
        cte_sql + f"SELECT COUNT(*) AS n FROM display_items{kept_where}", params,
    ).fetchone()
    total = count_row["n"]

    offset = (page - 1) * per_page
    offset = min(offset, 50_000)
    rows = conn.execute(
        cte_sql + f"SELECT * FROM display_items{kept_where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    item_ids    = [r["id"] for r in rows]
    show_rkeys  = {r["show_rating_key"] for r in rows if r["show_rating_key"]}

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

        items.append({
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
        })

    return items, total


def fetch_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """Return counts and stale count for the library stats bar."""
    movies = conn.execute(
        "SELECT COUNT(*) AS n FROM media_items WHERE media_type = 'movie'"
    ).fetchone()["n"]

    tv = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('tv_season', 'tv', 'season')"
    ).fetchone()["n"]

    anime = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(show_rating_key, show_title)) AS n "
        "FROM media_items WHERE media_type IN ('anime_season', 'anime')"
    ).fetchone()["n"]

    min_age = get_int_setting(conn, "min_age_days", default=30)
    inactivity = get_int_setting(conn, "inactivity_days", default=30)

    now = datetime.now(timezone.utc)
    age_cutoff = (now - timedelta(days=min_age)).isoformat()
    watch_cutoff = (now - timedelta(days=inactivity)).isoformat()

    stale = conn.execute("""
        SELECT COUNT(*) AS n
        FROM media_items
        WHERE added_at < ?
          AND (last_watched_at IS NULL OR last_watched_at < ?)
    """, (age_cutoff, watch_cutoff)).fetchone()["n"]

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
