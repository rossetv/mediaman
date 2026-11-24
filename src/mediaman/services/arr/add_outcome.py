"""Whether an add that Radarr or Sonarr never answered went through all the same.

A slow *arr can add the title after mediaman stopped waiting. Only adds may be
asked about: the title must not have been held before the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mediaman.services.arr.state import radarr_fetches_movie, sonarr_fetches_series
from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient


def _unanswered(exc: SafeHTTPError) -> bool:
    """A transport failure (status 0), or a 5xx such as a proxy's 504."""
    return exc.status_code == 0 or exc.status_code >= 500


def movie_add_landed(exc: SafeHTTPError, radarr_client: ArrClient, tmdb_id: int) -> bool:
    return _unanswered(exc) and radarr_fetches_movie(radarr_client, tmdb_id)


def series_add_landed(exc: SafeHTTPError, sonarr_client: ArrClient, tvdb_id: int) -> bool:
    return _unanswered(exc) and sonarr_fetches_series(sonarr_client, tvdb_id)
