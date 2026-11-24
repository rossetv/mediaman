"""What Watch shows beyond the library: TMDB's discover rows, genres, people and search,
the TMDB side of a title's detail, which TMDB titles the library already holds, and their
Radarr and Sonarr download state."""

from __future__ import annotations

from ._arr import ArrView, arr_view, invalidate
from ._dedupe import LibraryIndex, SeenTitles, library_ids, normalise_title
from ._discover import (
    MAX_PAGE,
    MAX_QUERY_LEN,
    MIN_QUERY_LEN,
    SHELVES,
    TitleNotFound,
    details,
    discover_page,
    discover_shelves,
    search,
    tmdb_card,
)
from ._explore import (
    KNOWN_FOR_LIMIT,
    SORTS,
    TYPES,
    PersonNotFound,
    age,
    decades,
    explore_page,
    genre_posters,
    people,
    person,
)
from ._gate import TmdbBusy, TmdbDown, TmdbUnavailable
from ._genres import GENRES, Genre
from ._library import clear_library_index, library_index, tmdb_id_for_tvdb
from ._shape import discover_card, library_href, tmdb_image_url
from ._title import tmdb_fields
from ._title_meta import (
    LIBRARY_PAGE,
    Coverage,
    LibraryPeople,
    fill_title_meta,
    library_people,
    library_titles,
)

__all__ = [
    "GENRES",
    "KNOWN_FOR_LIMIT",
    "LIBRARY_PAGE",
    "MAX_PAGE",
    "MAX_QUERY_LEN",
    "MIN_QUERY_LEN",
    "SHELVES",
    "SORTS",
    "TYPES",
    "ArrView",
    "Coverage",
    "Genre",
    "LibraryIndex",
    "LibraryPeople",
    "PersonNotFound",
    "SeenTitles",
    "TitleNotFound",
    "TmdbBusy",
    "TmdbDown",
    "TmdbUnavailable",
    "age",
    "arr_view",
    "clear_library_index",
    "decades",
    "details",
    "discover_card",
    "discover_page",
    "discover_shelves",
    "explore_page",
    "fill_title_meta",
    "genre_posters",
    "invalidate",
    "library_href",
    "library_ids",
    "library_index",
    "library_people",
    "library_titles",
    "normalise_title",
    "people",
    "person",
    "search",
    "tmdb_card",
    "tmdb_fields",
    "tmdb_id_for_tvdb",
    "tmdb_image_url",
]
