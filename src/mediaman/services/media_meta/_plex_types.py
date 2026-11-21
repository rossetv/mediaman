"""TypedDicts and converters for Plex API response shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, TypedDict

from mediaman.services.media_meta.anime_detect import is_anime as _is_anime_show

# Hard cap on a single /status/sessions/history response. Plex history
# XML is small; 4 MiB is orders of magnitude above normal and still
# cheap to hold in memory if a malicious or buggy server starts
# streaming indefinitely.
_HISTORY_MAX_BYTES = 4 * 1024 * 1024


class PlexLibrarySection(TypedDict):
    id: str
    type: str
    title: str


class PlexMovieItem(TypedDict):
    plex_rating_key: str
    title: str
    added_at: datetime | None
    updated_at: datetime | None
    file_path: str
    file_size_bytes: int
    poster_path: str


class PlexSeasonItem(TypedDict):
    plex_rating_key: str
    title: str  # show title, kept for DB compat
    show_title: str
    season_number: int
    added_at: datetime | None
    updated_at: datetime | None
    file_path: str
    file_size_bytes: int
    poster_path: str
    episode_count: int
    show_rating_key: str
    is_anime: bool


class PlexWatchEntry(TypedDict, total=False):
    viewed_at: datetime
    account_id: int
    episode_title: str  # only present for season watch history entries


class PlexRatedItem(TypedDict):
    title: str
    type: Literal["movie", "tv"]
    stars: float


class PlexAccount(TypedDict):
    id: int
    name: str


def _to_utc(dt: datetime | None) -> datetime | None:
    """Promote a plexapi datetime to a tz-aware UTC value.

    plexapi builds naive local-time datetimes from POSIX timestamps. ensure_tz treats
    naive values as UTC, so without this conversion stored timestamps would shift by the
    local UTC offset. Aware inputs are converted; None stays None.
    """
    return dt.astimezone(UTC) if dt is not None else None


def _movie_to_item(movie: Any) -> PlexMovieItem:
    file_path = ""
    file_size = 0
    if movie.media:
        for part in movie.media[0].parts:
            file_path = file_path or part.file
            file_size += part.size or 0
    return {
        "plex_rating_key": str(movie.ratingKey),
        "title": movie.title,
        "added_at": _to_utc(movie.addedAt),
        "updated_at": _to_utc(movie.updatedAt),
        "file_path": file_path,
        "file_size_bytes": file_size,
        "poster_path": movie.thumb,
    }


def _season_to_item(show: Any, season: Any) -> PlexSeasonItem:
    episodes = season.episodes()
    file_size = 0
    file_path = ""
    earliest_added = None
    for ep in episodes:
        if ep.media:
            for part in ep.media[0].parts:
                file_size += part.size or 0
                if not file_path and part.file:
                    file_path = str(part.file).rsplit("/", 1)[0]
        ep_added = ep.addedAt
        if ep_added is not None and (earliest_added is None or ep_added < earliest_added):
            earliest_added = ep_added
    added_at = season.addedAt or earliest_added
    return {
        "plex_rating_key": str(season.ratingKey),
        "title": show.title,
        "show_title": show.title,
        "season_number": season.index,
        "added_at": _to_utc(added_at),
        "updated_at": _to_utc(show.updatedAt),
        "file_path": file_path,
        "file_size_bytes": file_size,
        "poster_path": show.thumb,
        "episode_count": len(episodes),
        "show_rating_key": str(show.ratingKey),
        "is_anime": _is_anime_show(show),
    }
