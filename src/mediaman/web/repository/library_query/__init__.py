"""Library display query — re-export barrel.

Three private modules, one concept each:

* `._query` — shared constants (``VALID_SORTS``, ``VALID_TYPES``,
  ``TV_SEASON_TYPES``, ``ANIME_SEASON_TYPES``, ``ALL_SEASON_TYPES``,
  ``MAX_SEARCH_TERM_LEN``) plus the core paginated query pipeline
  (``fetch_library`` and its private helpers).
* `._display` — display-formatting helpers (``days_ago``, ``type_css``,
  ``protection_label``, ``title_ref``, ``_shape_rows``) that convert raw DB values into
  Jinja-template-ready strings and dicts.
* `._stats` — simple ``COUNT`` / ``SUM`` queries for the library stats bar
  (``fetch_stats``, ``count_movies``, ``count_tv_shows``, ``count_anime_shows``, ``count_stale``,
  ``count_titles``, ``sum_total_size_bytes``).

This barrel re-exports the full public surface so callers import
from the package, ``from mediaman.web.repository.library_query import X``.
"""

from __future__ import annotations

from mediaman.web.repository.library_query._display import (
    days_ago,
    protection_label,
    title_ref,
    type_css,
)
from mediaman.web.repository.library_query._query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    MAX_SEARCH_TERM_LEN,
    TV_SEASON_TYPES,
    VALID_SORTS,
    VALID_TYPES,
    fetch_library,
)
from mediaman.web.repository.library_query._stats import (
    count_anime_shows,
    count_movies,
    count_stale,
    count_titles,
    count_tv_shows,
    fetch_stats,
    sum_total_size_bytes,
)

__all__ = [
    "ALL_SEASON_TYPES",
    "ANIME_SEASON_TYPES",
    "MAX_SEARCH_TERM_LEN",
    "TV_SEASON_TYPES",
    "VALID_SORTS",
    "VALID_TYPES",
    "count_anime_shows",
    "count_movies",
    "count_stale",
    "count_titles",
    "count_tv_shows",
    "days_ago",
    "fetch_library",
    "fetch_stats",
    "protection_label",
    "sum_total_size_bytes",
    "title_ref",
    "type_css",
]
