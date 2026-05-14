"""Library display query — re-export barrel.

Promoted from a single ``library_query.py`` module when it crossed the
300-line target.  Three private modules, one concept each:

* :mod:`._query` — shared constants (``VALID_SORTS``, ``VALID_TYPES``,
  ``TV_SEASON_TYPES``, ``ANIME_SEASON_TYPES``, ``ALL_SEASON_TYPES``,
  ``MAX_SEARCH_TERM_LEN``) plus the core paginated query pipeline
  (``fetch_library`` and its private helpers).
* :mod:`._display` — display-formatting helpers (``days_ago``, ``type_css``,
  ``protection_label``, ``_shape_rows``) that convert raw DB values into
  Jinja-template-ready strings and dicts.
* :mod:`._stats` — simple ``COUNT`` / ``SUM`` queries for the library stats bar
  (``count_movies``, ``count_tv_shows``, ``count_anime_shows``, ``count_stale``,
  ``sum_total_size_bytes``).

This barrel re-exports the full public surface unchanged so every
``from mediaman.web.repository.library_query import X`` keeps working.
"""

from __future__ import annotations

from mediaman.web.repository.library_query._display import (
    days_ago,
    protection_label,
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
    count_tv_shows,
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
    "count_tv_shows",
    "days_ago",
    "fetch_library",
    "protection_label",
    "sum_total_size_bytes",
    "type_css",
]
