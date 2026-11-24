"""The season a newsletter Re-Download link is for, from the link to the progress polling."""

from __future__ import annotations

import re
import sqlite3

from mediaman.core.deleted_media import DeletedMedia, fetch_deleted_media
from mediaman.crypto.tokens import DownloadTokenPayload

_SEASON_MEDIA_ID_RE = re.compile(r"sonarr-season:(\d{1,9}):")


def deleted_season(conn: sqlite3.Connection, payload: DownloadTokenPayload) -> DeletedMedia | None:
    """Return the deleted season *payload* asks for, or None when it names none: a movie, a
    show deleted without a season number, or a link from before seasons were named.

    Such a link carries the id of the deletion in ``sid``, where any other link has the
    recommendation it came from.
    """
    audit_id = payload.get("sid")
    if payload.get("act") != "redownload" or payload.get("mt") == "movie":
        return None
    if not isinstance(audit_id, int):
        return None
    stored = fetch_deleted_media(conn, [audit_id]).get(audit_id)
    if stored is None or stored.season_number is None or stored.tmdb_id != payload.get("tmdb"):
        return None
    return stored


def sonarr_media_id(title: str, season_number: int | None) -> str:
    """The media id of a poll token; :func:`season_of_media_id` reads the season back."""
    if season_number is None:
        return f"sonarr:{title}"
    return f"sonarr-season:{season_number}:{title}"


def season_of_media_id(media_item_id: str) -> int | None:
    found = _SEASON_MEDIA_ID_RE.match(media_item_id)
    return int(found.group(1)) if found else None


def season_name(season_number: int) -> str:
    return "Specials" if season_number == 0 else f"Season {season_number}"


def already_there(season_number: int, title: str) -> str:
    """The start of the answer for a season Sonarr has complete."""
    verb = "exist" if season_number == 0 else "exists"
    return f"{season_name(season_number)} of '{title}' already {verb}"
