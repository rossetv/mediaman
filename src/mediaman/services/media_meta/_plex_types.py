"""TypedDicts and converters for Plex API response shapes."""

from __future__ import annotations

import re
from collections.abc import Iterable
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


class PlexTrackedFile(TypedDict):
    path: str
    size: int


class PlexEpisodeFile(TypedDict):
    key: str
    number: int | None
    path: str


class PlexMovieItem(TypedDict):
    plex_rating_key: str
    title: str
    added_at: datetime | None
    updated_at: datetime | None
    file_path: str
    file_size_bytes: int
    poster_path: str
    tmdb_id: int | None
    tracked_files: list[PlexTrackedFile]


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
    episodes: list[PlexEpisodeFile]
    show_rating_key: str
    is_anime: bool
    tmdb_id: int | None  # the show's
    tvdb_id: int | None
    tracked_files: list[PlexTrackedFile]


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


_LEGACY_TMDB_PREFIX = "com.plexapp.agents.themoviedb://"
_LEGACY_TVDB_PREFIX = "com.plexapp.agents.thetvdb://"


def _leading_id(text: str) -> int | None:
    """Return the positive integer *text* starts with (``"123?lang=en"`` -> 123)."""
    match = re.match(r"[0-9]+", text)
    value = int(match.group()) if match else 0
    return value if value > 0 else None


def _external_id(item: Any, scheme: str, legacy_prefix: str) -> int | None:
    """Return the *scheme* id (``"tmdb"``/``"tvdb"``) Plex holds for *item*.

    New Plex agents list external ids as ``guids`` (``tmdb://N``); the
    legacy agents put a single id in ``guid`` instead.
    """
    guid = getattr(item, "guid", None)
    if not isinstance(guid, str):
        return None
    if guid.startswith(legacy_prefix):
        return _leading_id(guid[len(legacy_prefix) :])
    # Only the new agents (plex:// guids) carry ``guids``; reading an empty
    # ``guids`` on any other item makes plexapi reload it from the server.
    if not guid.startswith("plex://"):
        return None
    prefix = f"{scheme}://"
    for entry in item.guids or []:
        entry_id = getattr(entry, "id", None)
        if isinstance(entry_id, str) and entry_id.startswith(prefix):
            return _leading_id(entry_id[len(prefix) :])
    return None


def _part_files(media_lists: Iterable[Any]) -> list[PlexTrackedFile]:
    """Every part file of every media version, each path once.

    A file holding two episodes shows up under both, hence the dedupe.
    """
    files: list[PlexTrackedFile] = []
    seen: set[str] = set()
    for media in (m for media_list in media_lists for m in media_list or []):
        for part in media.parts or []:
            path = part.file
            if not isinstance(path, str) or not path or path in seen:
                continue
            seen.add(path)
            size = part.size
            files.append({"path": path, "size": size if isinstance(size, int) else 0})
    return files


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
        "tmdb_id": _external_id(movie, "tmdb", _LEGACY_TMDB_PREFIX),
        "tracked_files": _part_files([movie.media]),
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
        "episodes": [
            {
                "key": str(ep.ratingKey),
                "number": ep.index if isinstance(ep.index, int) else None,
                # Plex may list the versions of an episode in any order.
                "path": min((f["path"] for f in _part_files([ep.media])), default=""),
            }
            for ep in episodes
        ],
        "show_rating_key": str(show.ratingKey),
        "is_anime": _is_anime_show(show),
        "tmdb_id": _external_id(show, "tmdb", _LEGACY_TMDB_PREFIX),
        "tvdb_id": _external_id(show, "tvdb", _LEGACY_TVDB_PREFIX),
        "tracked_files": _part_files(ep.media for ep in episodes),
    }
