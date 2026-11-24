"""Display-formatting helpers for the library view.

Converts raw DB values and row dicts into display-ready strings and dicts
consumed by the Jinja template.  These are pure transformations — no SQL,
no DB connection required.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from mediaman.core.format import days_ago as _days_ago_fmt
from mediaman.core.format import format_bytes
from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED
from mediaman.core.time import now_utc, parse_iso_strict_utc, parse_iso_utc
from mediaman.services.scheduled_actions.days_left import days_left


def days_ago(dt_str: str | None) -> str:
    dt = parse_iso_utc(dt_str)
    if dt is None:
        return ""
    delta = (now_utc() - dt).days
    if delta > 3650:
        return ""
    return _days_ago_fmt(dt_str)


def type_css(media_type: str) -> str:
    if media_type in ("tv_season", "season", "tv"):
        return "type-tv"
    if media_type in ("anime_season", "anime"):
        return "type-anime"
    return "type-mov"


def protection_label(sa_action: str | None, sa_execute_at: str | None) -> str | None:
    if sa_action is None:
        return None
    if sa_action == ACTION_PROTECTED_FOREVER:
        return "Kept forever"
    if sa_action == ACTION_SNOOZED and sa_execute_at:
        execute_at = parse_iso_strict_utc(sa_execute_at)
        if execute_at is None:
            return None
        now = now_utc()
        if execute_at < now:
            return None
        # Whole days of elapsed time left, not UTC calendar days: a keep ending in two
        # hours must not read "1 more day" just because midnight UTC falls in between.
        days = (execute_at - now).days
        if days < 1:
            return "Kept for less than a day"
        if days == 1:
            return "Kept for 1 more day"
        return f"Kept for {days} more days"
    return None


Keep = tuple[str, str | None]

_NEVER = datetime.min.replace(tzinfo=UTC)


def lapse_key(keep: Keep) -> tuple[bool, datetime]:
    """Order keeps by when they lapse: a snooze at its parsed time, forever last."""
    if keep[0] == ACTION_PROTECTED_FOREVER:
        return (True, _NEVER)
    return (False, parse_iso_strict_utc(keep[1]) or _NEVER)


def _row_protection(
    seasons: dict[str, Keep], show_keep: Keep | None, total: int
) -> tuple[bool, str | None]:
    """``(protected, label)`` for a row of *total* items.

    *seasons* and *show_keep* hold only keeps in force.  The row is protected
    only when every item in it is, and then reads the keep that lapses first.
    A row with only some items kept is not protected; it says how many are.
    """
    live = list(seasons.values())
    if show_keep is not None:
        lapses = [max(keep, show_keep, key=lapse_key) for keep in live]
        if len(live) < total:
            lapses.append(show_keep)
        return True, protection_label(*min(lapses, key=lapse_key))
    if not live:
        return False, None
    if len(live) < total:
        return False, f"{len(live)} of {total} seasons kept"
    return True, protection_label(*min(live, key=lapse_key))


def _shape_rows(
    rows: list[sqlite3.Row],
    sa_map: dict[str, dict[str, Keep]],
    ks_map: dict[str, Keep],
) -> list[dict[str, object]]:
    """Convert raw DB rows into display-ready dicts.

    # rationale: single cohesive loop building one output dict per row;
    # the 18-key output dict is the natural seam and cannot be split further.
    """
    items = []
    now = now_utc()
    for r in rows:
        display_type = r["display_type"]
        is_tv = display_type in ("tv", "anime")
        show_rk = r["show_rating_key"] or ""
        show_title = r["show_title"] or r["title"]

        # The scanner honours a show keep only under a real rating key.
        show_keep = ks_map.get(show_rk) if is_tv and r["has_rating_key"] else None
        show_kept = show_keep is not None
        protected, prot_label = _row_protection(
            sa_map.get(str(r["id"]), {}),
            show_keep,
            (r["season_count"] or 1) if is_tv else 1,
        )

        season_count = r["season_count"]
        if is_tv:
            if season_count and season_count > 1:
                type_label = f"{season_count} seasons"
            else:
                type_label = "1 season"
        else:
            type_label = "MOVIE"

        deletion_at = parse_iso_strict_utc(r["deletion_at"])

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
                "season_count": season_count if is_tv else None,
                "type_css": type_css(display_type),
                "plex_rating_key": r["plex_rating_key"],
                "added_at": r["added_at"],
                "added_ago": added_ago,
                "file_size": format_bytes(r["file_size_bytes"] or 0),
                "file_size_bytes": r["file_size_bytes"] or 0,
                "last_watched": days_ago(r["last_watched_at"]),
                "show_rating_key": show_rk,
                "has_rating_key": bool(r["has_rating_key"]),
                "show_title_raw": show_title,
                "is_tv": is_tv,
                "protected": protected,
                "protection_label": prot_label,
                "show_kept": show_kept,
                "deletion_at": r["deletion_at"] if deletion_at else None,
                "deletion_days": days_left(deletion_at, now) if deletion_at else None,
            }
        )
    return items
