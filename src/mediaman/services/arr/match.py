"""Decide whether a Plex item is the same thing as a Radarr/Sonarr entry.

Pure functions, no I/O.  Two strengths of check live here:

* The scan index (:func:`build_arr_index`, :func:`index_match_movie`,
  :func:`index_match_season`) matches on the TMDB/TVDB id plus the folder
  name.  It is only used to remember which Radarr/Sonarr entry an item
  belongs to.
* The strict checks (:func:`verify_movie`, :func:`verify_season`) run
  against a freshly fetched entry before any Radarr/Sonarr write.  On top
  of the id and folder they require the arr's own files to be files Plex
  reported for the item, by name and size: a second library (a 4K copy,
  say) can share the id and the folder name but never the exact file.

Paths are compared by their last components only, because Radarr, Sonarr
and mediaman may mount the same storage at different places.  Names are
compared case-sensitively.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from mediaman.services.arr._types import ArrEpisodeFile, RadarrMovie, SonarrSeries


class MovieRef(NamedTuple):
    radarr_id: int
    folder: str


class SeriesRef(NamedTuple):
    sonarr_id: int
    folder: str
    seasons: frozenset[int]


class ArrIndex(NamedTuple):
    """Radarr movies by TMDB id and Sonarr series by TVDB id."""

    movies: dict[int, MovieRef]
    series: dict[int, SeriesRef]


class MatchResult(NamedTuple):
    """``ok`` and, when not ok, a reason a person can read."""

    ok: bool
    reason: str


_OK = MatchResult(True, "")


def _parts(path: object) -> list[str]:
    """Split *path* into its non-empty components, on either separator."""
    if not isinstance(path, str):
        return []
    return [p for p in re.split(r"[\\/]+", path) if p]


def _name(path: object) -> str:
    parts = _parts(path)
    return parts[-1] if parts else ""


def _parent(path: str) -> str:
    return "/".join(_parts(path)[:-1])


def _parent_name(path: object) -> str:
    parts = _parts(path)
    return parts[-2] if len(parts) >= 2 else ""


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _size(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _drop_duplicates[K, V](pairs: Iterable[tuple[K, V]]) -> dict[K, V]:
    out: dict[K, V] = {}
    seen_twice: set[K] = set()
    for key, value in pairs:
        if key in out or key in seen_twice:
            out.pop(key, None)
            seen_twice.add(key)
        else:
            out[key] = value
    return out


def _season_numbers(series: SonarrSeries) -> frozenset[int]:
    seasons = series.get("seasons")
    if not isinstance(seasons, list):
        return frozenset()
    return frozenset(
        s["seasonNumber"]
        for s in seasons
        if isinstance(s, Mapping)
        and isinstance(s.get("seasonNumber"), int)
        and not isinstance(s.get("seasonNumber"), bool)
    )


def build_arr_index(movies: Iterable[RadarrMovie], series: Iterable[SonarrSeries]) -> ArrIndex:
    """Index Radarr movies by TMDB id and Sonarr series by TVDB id.

    Entries without an arr id or a TMDB/TVDB id are skipped.  An id carried
    by two entries is left out altogether, since either could be the one
    Plex sees.
    """
    movie_pairs = []
    for movie in movies:
        radarr_id = _positive_int(movie.get("id"))
        tmdb_id = _positive_int(movie.get("tmdbId"))
        if radarr_id is not None and tmdb_id is not None:
            movie_pairs.append((tmdb_id, MovieRef(radarr_id, _name(movie.get("path")))))
    series_pairs = []
    for show in series:
        sonarr_id = _positive_int(show.get("id"))
        tvdb_id = _positive_int(show.get("tvdbId"))
        if sonarr_id is not None and tvdb_id is not None:
            ref = SeriesRef(sonarr_id, _name(show.get("path")), _season_numbers(show))
            series_pairs.append((tvdb_id, ref))
    return ArrIndex(_drop_duplicates(movie_pairs), _drop_duplicates(series_pairs))


def _series_folder_matches(series_folder: str, plex_folder: str) -> bool:
    """True when *plex_folder* is the series folder or a season folder in it."""
    if not series_folder:
        return False
    return series_folder in (_name(plex_folder), _parent_name(plex_folder))


def _series_root(series_folder: str, path: str) -> str | None:
    """Return the series folder holding the episode file *path*, or ``None``.

    The file sits in the series folder itself or in a season folder in it.
    """
    parent = _parent(path)
    if not series_folder:
        return None
    if _name(parent) == series_folder:
        return parent
    if _parent_name(parent) == series_folder:
        return _parent(parent)
    return None


def index_match_movie(index: ArrIndex, *, tmdb_id: int | None, plex_file_path: str) -> int | None:
    """Return the Radarr id for a Plex movie file, or ``None``.

    Matches when *tmdb_id* is indexed and the Radarr folder's name equals
    the name of the folder holding *plex_file_path*.
    """
    key = _positive_int(tmdb_id)
    ref = index.movies.get(key) if key is not None else None
    if ref is None or not ref.folder or ref.folder != _parent_name(plex_file_path):
        return None
    return ref.radarr_id


def index_match_season(
    index: ArrIndex, *, tvdb_id: int | None, season_number: int, plex_file_path: str
) -> int | None:
    """Return the Sonarr series id for a Plex season, or ``None``.

    *plex_file_path* is the folder Plex holds the season's episodes in (a
    season folder, or the show folder when the show has no season
    folders).  Matches when *tvdb_id* is indexed, Sonarr knows the season
    and the Sonarr series folder's name is that folder or its parent.
    """
    key = _positive_int(tvdb_id)
    ref = index.series.get(key) if key is not None else None
    if ref is None or season_number not in ref.seasons:
        return None
    if not _series_folder_matches(ref.folder, plex_file_path):
        return None
    return ref.sonarr_id


def _tracked(tracked_files: object) -> list[tuple[str, int, str]]:
    """Return ``(name, size, path)`` for each well-formed tracked file."""
    if not isinstance(tracked_files, Sequence) or isinstance(tracked_files, str):
        return []
    out = []
    for entry in tracked_files:
        if not isinstance(entry, Mapping):
            continue
        path = entry.get("path")
        size = _size(entry.get("size"))
        name = _name(path)
        if name and size is not None and isinstance(path, str):
            out.append((name, size, path))
    return out


def verify_movie(
    entry: RadarrMovie, *, tmdb_id: int | None, tracked_files: Sequence[Mapping[str, object]] | None
) -> MatchResult:
    """Strict check that Radarr's *entry* is the movie Plex tracks.

    Passes only when the entry has *tmdb_id*, its folder name is the folder
    of a tracked file, and its ``movieFile`` has the same name and size as
    a tracked file in that folder.
    """
    expected = _positive_int(tmdb_id)
    if expected is None:
        return MatchResult(False, "Plex has no TMDB id for this movie; nothing was deleted")
    if _positive_int(entry.get("tmdbId")) != expected:
        return MatchResult(
            False, "Radarr's entry is a different movie (TMDB id differs); nothing was deleted"
        )
    tracked = _tracked(tracked_files)
    if not tracked:
        return MatchResult(
            False, "mediaman does not know this movie's files yet; nothing was deleted"
        )
    folder = _name(entry.get("path"))
    in_folder = [t for t in tracked if folder and _parent_name(t[2]) == folder]
    if not in_folder:
        return MatchResult(
            False, "Radarr's folder for this movie is not the one Plex uses; nothing was deleted"
        )
    movie_file = entry.get("movieFile")
    if not isinstance(movie_file, Mapping):
        return MatchResult(False, "Radarr has no file for this movie; nothing was deleted")
    arr_name = _name(movie_file.get("relativePath") or movie_file.get("path"))
    arr_size = _size(movie_file.get("size"))
    if (
        arr_name
        and arr_size is not None
        and (arr_name, arr_size) in {(name, size) for name, size, _ in in_folder}
    ):
        return _OK
    return MatchResult(
        False,
        "Radarr's file for this movie is a different file (size or name differ); "
        "nothing was deleted",
    )


def verify_season(
    series: SonarrSeries,
    episode_files: Iterable[ArrEpisodeFile],
    *,
    tvdb_id: int | None,
    season_number: int,
    tracked_files: Sequence[Mapping[str, object]] | None,
) -> MatchResult:
    """Strict check that Sonarr's season is the season Plex tracks.

    Passes only when *series* has *tvdb_id* and the season, the season has
    at least one episode file in *episode_files*, and those files and the
    tracked files are the same set by name and size, all tracked files in
    one series folder named as Sonarr's.  Sonarr deletes only its own
    files, so a tracked file it does not manage would be left on disk.
    """
    expected = _positive_int(tvdb_id)
    if expected is None:
        return MatchResult(False, "Plex has no TVDB id for this show; nothing was deleted")
    if _positive_int(series.get("tvdbId")) != expected:
        return MatchResult(
            False, "Sonarr's entry is a different show (TVDB id differs); nothing was deleted"
        )
    if season_number not in _season_numbers(series):
        return MatchResult(False, "Sonarr does not have this season; nothing was deleted")
    tracked = _tracked(tracked_files)
    if not tracked:
        return MatchResult(
            False, "mediaman does not know this season's files yet; nothing was deleted"
        )
    folder = _name(series.get("path"))
    roots = {_series_root(folder, path) for _, _, path in tracked}
    in_folder = {
        (name, size) for name, size, path in tracked if _series_root(folder, path) is not None
    }
    if not in_folder:
        return MatchResult(
            False, "Sonarr's folder for this show is not the one Plex uses; nothing was deleted"
        )
    season_files = [
        ef
        for ef in episode_files
        if isinstance(ef, Mapping) and ef.get("seasonNumber") == season_number
    ]
    if not season_files:
        return MatchResult(False, "Sonarr has no files for this season; nothing was deleted")
    arr_files = set()
    for ef in season_files:
        arr_name = _name(ef.get("relativePath") or ef.get("path"))
        arr_size = _size(ef.get("size"))
        if not arr_name or arr_size is None or (arr_name, arr_size) not in in_folder:
            return MatchResult(
                False,
                "Sonarr has a file for this season that Plex does not show "
                "(size or name differ); nothing was deleted",
            )
        arr_files.add((arr_name, arr_size))
    if len(roots) != 1 or any((name, size) not in arr_files for name, size, _ in tracked):
        return MatchResult(
            False,
            "Plex has files for this season that Sonarr does not manage. Delete them by hand "
            "or let a scheduled deletion handle it; nothing was deleted",
        )
    return _OK
