"""Shared types for the arr_fetcher package."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


@dataclass
class FetchResult:
    """Container returned by :func:`fetch_arr_queue_result`.

    ``cards`` is the list of download cards (same content as :func:`fetch_arr_queue`).
    ``errors`` is a list of human-readable error strings -- one per service that
    failed.  Empty when all fetches succeeded.  UI layers should surface these
    as a dismissible banner rather than hiding them silently.
    """

    cards: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class ArrEpisodeEntry(TypedDict, total=False):
    """A single episode entry within an :class:`ArrCard` (series cards only)."""

    label: str
    title: str
    progress: int
    size: int
    sizeleft: int
    size_str: str
    status: str
    download_id: str
    is_pack_episode: bool


class BaseArrCard(TypedDict):
    """Fields guaranteed present on every download card."""

    kind: str        # 'movie' or 'series'
    dl_id: str
    title: str
    source: str      # 'Radarr' or 'Sonarr'
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
