"""download_format package — pure format, parse, and classification helpers.

All previously-public symbols are re-exported from this ``__init__`` so
existing imports such as ``from mediaman.services.download_format import
build_item`` continue to work without modification.
"""

from mediaman.services.download_format._classify import (
    classify_movie_upcoming,
    classify_series_upcoming,
    extract_poster_url,
    map_arr_status,
    map_episode_state,
    map_state,
)
from mediaman.services.download_format._parsing import (
    fmt_episode_label,
    fmt_eta,
    fmt_relative_time,
    looks_like_series_nzb,
    normalise_for_match,
    parse_clean_title,
)
from mediaman.services.download_format._render import (
    build_episode_summary,
    build_item,
    select_hero,
)
from mediaman.services.download_format._types import DownloadItem

__all__ = [
    # Types
    "DownloadItem",
    # Parsing
    "parse_clean_title",
    "normalise_for_match",
    "fmt_relative_time",
    "looks_like_series_nzb",
    "fmt_episode_label",
    "fmt_eta",
    # Classification
    "extract_poster_url",
    "classify_movie_upcoming",
    "classify_series_upcoming",
    "map_state",
    "map_arr_status",
    "map_episode_state",
    # Rendering
    "build_item",
    "build_episode_summary",
    "select_hero",
]
