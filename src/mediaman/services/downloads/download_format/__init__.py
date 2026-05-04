"""download_format package — pure format, parse, and classification helpers.

All previously-public symbols are re-exported from this ``__init__`` so
existing imports such as ``from mediaman.services.downloads.download_format import
build_item`` continue to work without modification.
"""

from mediaman.services.downloads.download_format._classify import (
    classify_movie_upcoming,
    classify_series_upcoming,
    compute_movie_released_at,
    compute_series_released_at,
    extract_poster_url,
    map_arr_status,
    map_episode_state,
    map_state,
)
from mediaman.services.downloads.download_format._parsing import (
    format_episode_label,
    format_eta,
    format_relative_time,
    looks_like_series_nzb,
    normalise_for_match,
    parse_clean_title,
)
from mediaman.services.downloads.download_format._render import (
    build_episode_summary,
    build_item,
    select_hero,
)
from mediaman.services.downloads.download_format._types import DownloadItem

__all__ = [
    # Types
    "DownloadItem",
    "build_episode_summary",
    # Rendering
    "build_item",
    "classify_movie_upcoming",
    "classify_series_upcoming",
    "compute_movie_released_at",
    "compute_series_released_at",
    # Classification
    "extract_poster_url",
    "format_episode_label",
    "format_eta",
    "format_relative_time",
    "looks_like_series_nzb",
    "map_arr_status",
    "map_episode_state",
    "map_state",
    "normalise_for_match",
    # Parsing
    "parse_clean_title",
    "select_hero",
]
