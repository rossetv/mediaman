"""What Watch shows beyond the library: TMDB's discover rows and search, the TMDB side
of a title's detail, which TMDB titles the library already holds, and their Radarr and
Sonarr download state."""

from __future__ import annotations

from ._arr import ArrView, arr_view, invalidate
from ._dedupe import LibraryIndex, library_ids, normalise_title
from ._discover import (
    MAX_QUERY_LEN,
    MIN_QUERY_LEN,
    TmdbUnavailable,
    details,
    discover_shelves,
    search,
    tmdb_card,
)
from ._library import clear_library_index, library_index, tmdb_id_for_tvdb
from ._shape import discover_card, library_href, tmdb_image_url
from ._title import tmdb_fields

__all__ = [
    "MAX_QUERY_LEN",
    "MIN_QUERY_LEN",
    "ArrView",
    "LibraryIndex",
    "TmdbUnavailable",
    "arr_view",
    "clear_library_index",
    "details",
    "discover_card",
    "discover_shelves",
    "invalidate",
    "library_href",
    "library_ids",
    "library_index",
    "normalise_title",
    "search",
    "tmdb_card",
    "tmdb_fields",
    "tmdb_id_for_tvdb",
    "tmdb_image_url",
]
