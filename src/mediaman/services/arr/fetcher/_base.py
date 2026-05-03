"""Shared types and helpers for the arr_fetcher package."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TypedDict

import requests

from mediaman.services.infra.http_client import SafeHTTPError

logger = logging.getLogger("mediaman")


def _iter_still_searching[T](
    fetch_items: Callable[[], Iterable[T]],
    *,
    service_label: str,
) -> Iterable[T]:
    """Yield items from *fetch_items*, swallowing transient HTTP failures.

    The Radarr and Sonarr fetchers each tail their queue with a
    "monitored items still searching" pass that calls
    ``client.get_movies`` / ``client.get_series``. Both treat a network
    blip as recoverable — we'd rather show partial cards than wipe out
    the queue cards we already collected. This helper centralises that
    pattern so both fetchers use the same exception list
    (``RequestException`` AND ``SafeHTTPError``, since SafeHTTPClient
    raises the latter for non-2xx responses) and the same log format.

    The caller does its per-item filtering and card construction inside
    its own loop body — this helper only owns the outer try/except.
    """
    try:
        yield from fetch_items()
    except (requests.RequestException, SafeHTTPError):
        logger.warning("Failed to check %s for searching items", service_label, exc_info=True)


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
    * ``season_number`` — the raw season number from the Sonarr API payload;
      consumers should treat absent as 0. Avoids re-parsing the SxxExx label.
    """

    is_pack_episode: bool
    season_number: int


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
