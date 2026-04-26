"""Shared types for the arr_fetcher package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


def _format_size_fields(size: int, sizeleft: int) -> tuple[str, str]:
    """Return ``(size_str, done_str)`` formatted via :func:`~mediaman.services.infra.format.format_bytes`.

    Both Radarr and Sonarr cards compute these fields identically; this helper
    keeps the formatting in one place so a future unit change touches one line.

    Args:
        size: Total download size in bytes.
        sizeleft: Remaining bytes to download.

    Returns:
        A ``(size_str, done_str)`` tuple, each a human-readable byte string.
    """
    from mediaman.services.infra.format import format_bytes

    return format_bytes(size), format_bytes(size - sizeleft)


@dataclass
class FetchResult:
    """Container returned by :func:`fetch_arr_queue_result`.

    ``cards`` is the list of download cards (same content as :func:`fetch_arr_queue`).
    ``errors`` is a list of human-readable error strings -- one per service that
    failed.  Empty when all fetches succeeded.  UI layers should surface these
    as a dismissible banner rather than hiding them silently.
    """

    cards: list[ArrCard] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _ArrEpisodeRequired(TypedDict):
    """Fields that are always present on an episode entry at construction time."""

    size: int
    sizeleft: int
    label: str
    title: str
    progress: int
    size_str: str
    status: str
    download_id: str


class ArrEpisodeEntry(_ArrEpisodeRequired, total=False):
    """A single episode entry within an :class:`ArrCard` (series cards only).

    The required fields in :class:`_ArrEpisodeRequired` are always populated
    when the entry is first built.  Optional fields are added later during
    aggregation:

    * ``is_pack_episode`` — set by ``_aggregate_pack_episodes`` once all
      episodes in a series are known.
    """

    is_pack_episode: bool


class BaseArrCard(TypedDict):
    """Fields guaranteed present on every download card."""

    kind: str  # 'movie' or 'series'
    dl_id: str
    title: str
    source: str  # 'Radarr' or 'Sonarr'
    poster_url: str


class ArrCard(BaseArrCard, total=False):
    """A download card produced by :func:`fetch_arr_queue`.

    Both movie and series cards share this shape; series cards additionally
    carry an ``episodes`` list.  ``total=False`` allows partial construction
    as cards are built up incrementally. The five fields in :class:`BaseArrCard`
    are always present; all others are optional.
    """

    year: int | None
    progress: int
    size: int
    sizeleft: int
    size_str: str
    done_str: str
    timeleft: str
    status: str
    is_upcoming: bool
    release_label: str
    arr_id: int
    title_slug: str
    added_at: float
    release_names: list[str]
    # Series-only fields
    episodes: list[ArrEpisodeEntry]
    episode_count: int
    downloading_count: int
    has_pack: bool
