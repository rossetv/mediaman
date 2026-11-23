"""Which TMDB titles are already in the library.

A movie matches on the TMDB id Plex gave it. A show matches through Sonarr, which knows
both its TMDB and TVDB ids, against the TVDB id Plex gave it. Only when Radarr or Sonarr
reports the title downloaded does an exact title match count too: the library keeps no
year, so a remake that shares a title is left out of the library rather than hidden.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from mediaman.services.arr.state import ACTION_IN_LIBRARY, ACTION_PARTIAL

from ._arr import ArrView

_NOT_WORD = re.compile(r"[\W_]+")


def normalise_title(title: str) -> str:
    return _NOT_WORD.sub(" ", title.casefold()).strip()


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    """Library refs ("movie-<key>", "show-<key>") by TMDB id, TVDB id and title."""

    movies_by_tmdb: dict[int, str] = field(default_factory=dict)
    shows_by_tvdb: dict[int, str] = field(default_factory=dict)
    movies_by_title: dict[str, str] = field(default_factory=dict)
    shows_by_title: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        movies: Iterable[tuple[str, str, int | None]],
        shows: Iterable[tuple[str, str, int | None]],
    ) -> LibraryIndex:
        """From (key, title, TMDB id) per movie and (key, title, TVDB id) per show."""
        index = cls()
        for key, title, tmdb_id in movies:
            if tmdb_id:
                index.movies_by_tmdb.setdefault(tmdb_id, f"movie-{key}")
            index.movies_by_title.setdefault(normalise_title(title), f"movie-{key}")
        for key, title, tvdb_id in shows:
            if tvdb_id:
                index.shows_by_tvdb.setdefault(tvdb_id, f"show-{key}")
            index.shows_by_title.setdefault(normalise_title(title), f"show-{key}")
        return index

    def library_ref(self, kind: str, tmdb_id: int, title: str, arr: ArrView) -> str | None:
        """The library ref of a TMDB title ("movie" or "tv"), or None when it is not held."""
        if kind == "movie":
            found = self.movies_by_tmdb.get(tmdb_id)
            by_title = self.movies_by_title
        else:
            tvdb_id = arr.tvdb_id(tmdb_id)
            found = self.shows_by_tvdb.get(tvdb_id) if tvdb_id else None
            by_title = self.shows_by_title
        if found is not None:
            return found
        if arr.state(kind, tmdb_id)[0] in (ACTION_IN_LIBRARY, ACTION_PARTIAL):
            return by_title.get(normalise_title(title))
        return None
