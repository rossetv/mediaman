"""Shared types, helpers, and card factory for the arr_fetcher package.

The :func:`make_arr_card` factory replaces the separate ``_make_sonarr_card``
and ``_make_radarr_card`` functions that used to live in the per-service
fetcher modules.  The old names remain as one-line shims in those modules so
existing callers continue to work.
"""

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
    from mediaman.core.format import format_bytes

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
    # Earliest known release-date epoch (movies) or latest past airing epoch
    # (series). 0.0 means "release date unknown" — auto-abandon treats that
    # as a hard skip so it never abandons something whose age it can't
    # reason about.
    released_at: float
    release_names: list[str]
    # Series-only fields
    episodes: list[ArrEpisodeEntry]
    episode_count: int
    downloading_count: int
    has_pack: bool


def make_arr_card(
    kind: str,
    title: str,
    *,
    source: str,
    year: int | None = None,
    poster_url: str = "",
    episodes: list[ArrEpisodeEntry] | None = None,
    episode_count: int = 0,
    downloading_count: int = 0,
    progress: int = 0,
    size: int = 0,
    sizeleft: int = 0,
    timeleft: str = "",
    status: str = "searching",
    is_upcoming: bool = False,
    release_label: str = "",
    arr_id: int = 0,
    title_slug: str = "",
    added_at: float = 0.0,
    released_at: float = 0.0,
    release_names: list[str] | None = None,
) -> ArrCard:
    """Build a download card for either a Radarr movie or a Sonarr series.

    This is the single card factory that replaces the former
    ``_make_radarr_card`` and ``_make_sonarr_card`` helpers.  Those
    functions remain as one-line shims in their respective modules.

    :param kind: ``"movie"`` or ``"series"``.
    :param title: Display title of the item.
    :param source: Human-readable service label, e.g. ``"Radarr"`` or
        ``"Sonarr"``.  Used in the ``source`` field and to prefix ``dl_id``.
    :param episodes: Episode entries — only meaningful for series cards.
    """
    size_str, done_str = _format_size_fields(size, sizeleft)
    dl_id = source.lower() + ":" + title
    card = ArrCard(
        kind=kind,
        dl_id=dl_id,
        title=title,
        source=source,
        poster_url=poster_url,
        year=year,
        progress=progress,
        size=size,
        sizeleft=sizeleft,
        size_str=size_str,
        done_str=done_str,
        timeleft=timeleft,
        status=status,
        is_upcoming=is_upcoming,
        release_label=release_label,
        arr_id=arr_id,
        title_slug=title_slug,
        added_at=added_at,
        released_at=released_at,
        release_names=release_names if release_names is not None else [],
    )
    if kind == "series":
        card["episodes"] = episodes if episodes is not None else []
        card["episode_count"] = episode_count
        card["downloading_count"] = downloading_count
    return card
