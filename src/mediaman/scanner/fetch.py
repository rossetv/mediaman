"""Plex fetch layer — pulls library items and watch history.

Keeps all network I/O against Plex out of :mod:`mediaman.scanner.engine`
so the engine's write-path never overlaps an open Plex HTTP round-trip
with a SQLite write transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests
from plexapi.exceptions import PlexApiException

from mediaman.services.media_meta._plex_types import (
    PlexMovieItem,
    PlexSeasonItem,
    PlexWatchEntry,
)

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient

from mediaman.services.media_meta.plex import PlexResponseTooLarge

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlexItemFetch:
    """Network-read handoff between the scanner's fetch and write phases.

    The scanner fetches a library's full contents (items + watch history)
    from Plex into a list of these in phase 1, then phase 2 consumes the
    list with no further network calls. Keeps the SQLite write lock off
    the critical path of any HTTP round-trip.
    """

    item: PlexMovieItem | PlexSeasonItem
    library_id: str
    media_type: str
    watch_history: list[PlexWatchEntry]


@dataclass(frozen=True, slots=True)
class FetchedLibrary:
    """Result of a single library fetch — usable items plus skipped keys.

    *items* are the fully-fetched items (item + watch history) that the
    write phase may evaluate. *skipped_keys* are the ``plex_rating_key``
    values that exist in Plex this scan but whose watch history could NOT
    be fetched (a transient Plex history-fetch failure).

    The distinction is load-bearing for data integrity: an item whose
    history fetch failed is still *present* in Plex, so it must never be
    treated as an orphan and pruned. The engine unions *skipped_keys* into
    its ``seen_keys`` set before orphan removal so a history-fetch failure
    is "seen, just not evaluated this scan", never "gone from Plex".
    """

    items: list[PlexItemFetch]
    skipped_keys: frozenset[str]


class PlexFetcher:
    """Wraps the Plex client for the scanner's two-phase fetch.

    A thin adapter around the existing Plex client that knows how to
    produce :class:`PlexItemFetch` records for both movie and show
    libraries, swallowing watch-history errors so one flaky item can't
    derail an entire library sync.
    """

    def __init__(
        self,
        *,
        plex_client: PlexClient,
        library_types: dict[str, str],
        library_titles: dict[str, str] | None = None,
    ) -> None:
        self._plex = plex_client
        self._library_types = library_types
        self._library_titles = library_titles or {}

    def fetch_library_items(self, library_id: str) -> FetchedLibrary:
        """Fetch items + watch history for a library from Plex.

        Pure network-read helper; touches no DB. Returns a
        :class:`FetchedLibrary` carrying one :class:`PlexItemFetch` per
        successfully-fetched movie or season, plus the set of
        ``plex_rating_key`` values that were SKIPPED because their watch
        history could not be fetched.

        **Fails closed on watch-history errors**. The previous code
        swallowed a per-item watch-history failure and substituted an
        empty list, which combined with ``check_inactivity([], …) ==
        True`` to mean a transient Plex 500 reclassified the item as
        "never watched" and made it eligible for deletion the next run.
        The offending item is now excluded from ``items`` AND its key is
        recorded in ``skipped_keys`` so the engine can protect it from
        orphan pruning — a history-fetch failure means the item is still
        in Plex, just not evaluable this scan. A retry on the next scan
        picks up the item once Plex is healthy again.
        """
        lib_type = self._library_types.get(library_id, "movie")
        out: list[PlexItemFetch] = []
        skipped: set[str] = set()
        if lib_type == "show":
            self._fetch_show_seasons(library_id, out, skipped)
        else:
            self._fetch_movies(library_id, out, skipped)
        return FetchedLibrary(items=out, skipped_keys=frozenset(skipped))

    def _fetch_show_seasons(
        self,
        library_id: str,
        out: list[PlexItemFetch],
        skipped: set[str],
    ) -> None:
        """Fetch each season's watch history; record skips on failure."""
        seasons = self._plex.get_show_seasons(library_id)
        lib_title = self._library_titles.get(library_id, "")
        default_anime = "anime" in lib_title
        for season in seasons:
            media_type = "anime_season" if season.get("is_anime", default_anime) else "tv_season"
            rating_key = season["plex_rating_key"]
            try:
                watch_history = self._plex.get_season_watch_history(rating_key)
            except (PlexApiException, requests.RequestException, PlexResponseTooLarge):
                logger.warning(
                    "Failed to fetch watch history for season %s — "
                    "excluding from evaluation this scan but protecting "
                    "it from orphan removal (still present in Plex)",
                    rating_key,
                    exc_info=True,
                )
                skipped.add(rating_key)
                continue
            out.append(
                PlexItemFetch(
                    item=season,
                    library_id=library_id,
                    media_type=media_type,
                    watch_history=watch_history,
                )
            )

    def _fetch_movies(
        self,
        library_id: str,
        out: list[PlexItemFetch],
        skipped: set[str],
    ) -> None:
        """Fetch each movie's watch history; record skips on failure."""
        items = self._plex.get_movie_items(library_id)
        for item in items:
            rating_key = item["plex_rating_key"]
            try:
                watch_history = self._plex.get_watch_history(rating_key)
            except (PlexApiException, requests.RequestException, PlexResponseTooLarge):
                logger.warning(
                    "Failed to fetch watch history for item %s — "
                    "excluding from evaluation this scan but protecting "
                    "it from orphan removal (still present in Plex)",
                    rating_key,
                    exc_info=True,
                )
                skipped.add(rating_key)
                continue
            out.append(
                PlexItemFetch(
                    item=item,
                    library_id=library_id,
                    media_type="movie",
                    watch_history=watch_history,
                )
            )
