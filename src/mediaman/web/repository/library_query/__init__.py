"""Library display query — re-export barrel.

Four private modules, one concept each:

* `._query` — shared constants (``VALID_SORTS``, ``VALID_TYPES``,
  ``TV_SEASON_TYPES``, ``ANIME_SEASON_TYPES``, ``ALL_SEASON_TYPES``)
  plus the core paginated query pipeline
  (``fetch_library`` and its private helpers).
* `._display` — display-formatting helpers (``days_ago``, ``type_css``,
  ``protection_label``, ``title_ref``, ``_shape_rows``) that convert raw DB values into
  Jinja-template-ready strings and dicts.
* `._match` — how a title is compared with what a person typed (``TitleSearch``,
  ``without_article``, ``MAX_SEARCH_TERM_LEN``).
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
from mediaman.web.repository.library_query._match import (
    MAX_SEARCH_TERM_LEN,
    TitleSearch,
    without_article,
)
from mediaman.web.repository.library_query._query import (
    ALL_SEASON_TYPES,
    ANIME_SEASON_TYPES,
    TV_SEASON_TYPES,
    VALID_SORTS,
    VALID_TYPES,
    display_key,
    fetch_display_rows,
    fetch_library,
    shape_display_rows,
)
from mediaman.web.repository.library_query._stats import (
    DUE_SOON_DAYS,
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
    "DUE_SOON_DAYS",
    "MAX_SEARCH_TERM_LEN",
    "TV_SEASON_TYPES",
    "VALID_SORTS",
    "VALID_TYPES",
    "TitleSearch",
    "count_anime_shows",
    "count_movies",
    "count_stale",
    "count_titles",
    "count_tv_shows",
    "days_ago",
    "display_key",
    "fetch_display_rows",
    "fetch_library",
    "fetch_stats",
    "protection_label",
    "shape_display_rows",
    "sum_total_size_bytes",
    "title_ref",
    "type_css",
    "without_article",
]
