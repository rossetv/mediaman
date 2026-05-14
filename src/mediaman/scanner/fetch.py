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

    def fetch_library_items(self, library_id: str) -> list[PlexItemFetch]:
        """Fetch items + watch history for a library from Plex.

        Pure network-read helper; touches no DB. Returns one
        :class:`PlexItemFetch` per movie or per season.

        **Fails closed on watch-history errors**. The
        previous code swallowed a per-item watch-history failure and
        substituted an empty list, which combined with
        ``check_inactivity([], …) == True`` to mean a transient Plex
        500 reclassified the item as "never watched" and made it
        eligible for deletion the next run. Now the offending item is
        excluded from the returned list and the failure is logged so
        an operator can spot it. A retry on the next scan picks up the
        item once Plex is healthy again.
        """
        lib_type = self._library_types.get(library_id, "movie")
        out: list[PlexItemFetch] = []
        if lib_type == "show":
            seasons = self._plex.get_show_seasons(library_id)
            lib_title = self._library_titles.get(library_id, "")
            default_anime = "anime" in lib_title
            for season in seasons:
                media_type = (
                    "anime_season" if season.get("is_anime", default_anime) else "tv_season"
                )
                try:
                    watch_history = self._plex.get_season_watch_history(season["plex_rating_key"])
                except (PlexApiException, requests.RequestException):
                    logger.warning(
                        "Failed to fetch watch history for season %s — "
                        "skipping season this scan to avoid misclassifying "
                        "as 'never watched'",
                        season.get("plex_rating_key"),
                        exc_info=True,
                    )
                    continue
                out.append(
                    PlexItemFetch(
                        item=season,
                        library_id=library_id,
                        media_type=media_type,
                        watch_history=watch_history,
                    )
                )
        else:
            items = self._plex.get_movie_items(library_id)
            for item in items:
                try:
                    watch_history = self._plex.get_watch_history(item["plex_rating_key"])
                except (PlexApiException, requests.RequestException):
                    logger.warning(
                        "Failed to fetch watch history for item %s — "
                        "skipping item this scan to avoid misclassifying "
                        "as 'never watched'",
                        item.get("plex_rating_key"),
                        exc_info=True,
                    )
                    continue
                out.append(
                    PlexItemFetch(
                        item=item,
                        library_id=library_id,
                        media_type="movie",
                        watch_history=watch_history,
                    )
                )
        return out
