"""Display-formatting helpers for the library view.

Converts raw DB values and row dicts into display-ready strings and dicts
consumed by the Jinja template.  These are pure transformations — no SQL,
no DB connection required.
"""

from __future__ import annotations

import sqlite3

from mediaman.core.format import days_ago as _days_ago_fmt
from mediaman.core.format import format_bytes, relative_day_label
from mediaman.core.scheduled_action_kinds import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED
from mediaman.core.time import now_utc, parse_iso_strict_utc, parse_iso_utc


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
        # ``protection_label`` returns None for today/past dates rather
        # than a string, so we pre-filter and only invoke
        # `relative_day_label` on future deadlines.  The helper's
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


def _scheduled_protection_label(
    seasons: dict[str, tuple[str, str | None]], total: int
) -> str | None:
    """Label for the keeps on a row's items, or None when none is live.

    Seasons are protected one by one, so a show with only some of them kept says
    how many.  When all are, the label is the keep that lapses first.
    """
    live = [
        (label, entry)
        for entry in seasons.values()
        if (label := protection_label(*entry)) is not None
    ]
    if not live:
        return None
    if len(live) < total:
        return f"{len(live)} of {total} seasons kept"
    snoozes = [(label, entry) for label, entry in live if entry[0] == ACTION_SNOOZED]
    if not snoozes:
        return live[0][0]
    # A live snooze label implies a parseable execute_at.
    return min(snoozes, key=lambda le: parse_iso_strict_utc(le[1][1]) or now_utc())[0]


def _shape_rows(
    rows: list[sqlite3.Row],
    sa_map: dict[str, dict[str, tuple[str, str | None]]],
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
            seasons = sa_map.get(str(r["id"]))
            if seasons:
                prot_label = _scheduled_protection_label(
                    seasons, (r["season_count"] or 1) if is_tv else 1
                )
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
            }
        )
    return items
