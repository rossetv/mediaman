"""Which TMDB titles are already in the library.

A title matches on its TMDB id: the one Plex gave the library item, or for a show the one
its TVDB id maps to (through Sonarr, or through TMDB itself). Failing that it matches on
its title and year together. The library keeps no year of its own, so the year is read
from a "(2005)" in the title or in the folder Plex holds it in; without one, an exact
title counts only when Radarr or Sonarr reports the title downloaded, so a remake that
shares a title is left out of the library rather than hidden.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from mediaman.services.arr.state import ACTION_IN_LIBRARY, ACTION_PARTIAL

from ._arr import ArrView

_NOT_WORD = re.compile(r"[\W_]+")
_YEAR = re.compile(r"\((\d{4})\)")
_TRAILING_YEAR = re.compile(r"\s*\((\d{4})\)\s*$")


def normalise_title(title: str) -> str:
    return _NOT_WORD.sub(" ", title.casefold()).strip()


def _year_in(text: str | None) -> int | None:
    found = _YEAR.search(text or "")
    return int(found.group(1)) if found else None


def library_ids(conn: sqlite3.Connection) -> dict[str, tuple[int | None, int | None]]:
    """(TMDB id, year) of every library title by ref ("movie-<key>", "show-<key>"), either
    None when unknown. A show's TMDB id is the one any of its seasons carries; its year
    is read from the show's folder, the parent of a season's."""
    found: dict[str, tuple[int | None, int | None]] = {}
    for row in conn.execute(
        "SELECT id, tmdb_id, file_path FROM media_items WHERE media_type = 'movie'"
    ):
        parts = (row["file_path"] or "").replace("\\", "/").split("/")
        found[f"movie-{row['id']}"] = (row["tmdb_id"], _year_in("/".join(parts[-2:])))
    for row in conn.execute(
        "SELECT show_rating_key, MAX(tmdb_id) AS tmdb_id, MIN(file_path) AS file_path"
        " FROM media_items WHERE show_rating_key IS NOT NULL GROUP BY show_rating_key"
    ):
        parts = (row["file_path"] or "").replace("\\", "/").split("/")
        found[f"show-{row['show_rating_key']}"] = (row["tmdb_id"], _year_in("/".join(parts[-2:])))
    return found


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    """Library refs ("movie-<key>", "show-<key>") by TMDB id, TVDB id, title and year."""

    movies_by_tmdb: dict[int, str] = field(default_factory=dict)
    shows_by_tmdb: dict[int, str] = field(default_factory=dict)
    shows_by_tvdb: dict[int, str] = field(default_factory=dict)
    movies_by_title: dict[str, str] = field(default_factory=dict)
    shows_by_title: dict[str, str] = field(default_factory=dict)
    movies_by_title_year: dict[tuple[str, int], str] = field(default_factory=dict)
    shows_by_title_year: dict[tuple[str, int], str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        movies: Iterable[tuple[str, str, int | None]],
        shows: Iterable[tuple[str, str, int | None]],
        *,
        ids: Mapping[str, tuple[int | None, int | None]] | None = None,
        tmdb_for_tvdb: Mapping[int, int] | None = None,
    ) -> LibraryIndex:
        """From (key, title, TMDB id) per movie and (key, title, TVDB id) per show, with
        the (TMDB id, year) by ref of ``library_ids`` and the TMDB ids known for TVDB ids."""
        ids = ids or {}
        tmdb_for_tvdb = tmdb_for_tvdb or {}
        index = cls()
        for key, title, tmdb_id in movies:
            ref = f"movie-{key}"
            own_tmdb, year = ids.get(ref, (None, None))
            index._add(ref, title, tmdb_id or own_tmdb, year, index.movies_by_tmdb, True)
        for key, title, tvdb_id in shows:
            ref = f"show-{key}"
            own_tmdb, year = ids.get(ref, (None, None))
            if tvdb_id:
                index.shows_by_tvdb.setdefault(tvdb_id, ref)
            tmdb_id = own_tmdb or (tmdb_for_tvdb.get(tvdb_id) if tvdb_id else None)
            index._add(ref, title, tmdb_id, year, index.shows_by_tmdb, False)
        return index

    def _add(
        self,
        ref: str,
        title: str,
        tmdb_id: int | None,
        year: int | None,
        by_tmdb: dict[int, str],
        movie: bool,
    ) -> None:
        trailing = _TRAILING_YEAR.search(title)
        if trailing:
            title, year = title[: trailing.start()], int(trailing.group(1))
        name = normalise_title(title)
        if tmdb_id:
            by_tmdb.setdefault(tmdb_id, ref)
        (self.movies_by_title if movie else self.shows_by_title).setdefault(name, ref)
        if year:
            by_title_year = self.movies_by_title_year if movie else self.shows_by_title_year
            by_title_year.setdefault((name, year), ref)

    def library_ref(
        self,
        kind: str,
        tmdb_id: int,
        title: str,
        arr: ArrView,
        *,
        year: int | None = None,
        tvdb_id: int | None = None,
    ) -> str | None:
        """The library ref of a TMDB title ("movie" or "tv"), or None when it is not held.
        *year* is TMDB's year of the title and *tvdb_id* the TVDB id TMDB gives a show."""
        name = normalise_title(title)
        if kind == "movie":
            found = self.movies_by_tmdb.get(tmdb_id)
            by_title, by_title_year = self.movies_by_title, self.movies_by_title_year
        else:
            found = self.shows_by_tmdb.get(tmdb_id)
            by_title, by_title_year = self.shows_by_title, self.shows_by_title_year
            for tvdb in (arr.tvdb_id(tmdb_id), tvdb_id):
                if found is None and tvdb:
                    found = self.shows_by_tvdb.get(tvdb)
        if found is None and year:
            found = by_title_year.get((name, year))
        if found is not None:
            return found
        if arr.state(kind, tmdb_id)[0] in (ACTION_IN_LIBRARY, ACTION_PARTIAL):
            return by_title.get(name)
        return None
