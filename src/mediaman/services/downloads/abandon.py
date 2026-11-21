"""Abandon-search service.

Single chokepoint for unmonitoring stuck items in Radarr/Sonarr. Both the
manual API endpoint and the scheduler's auto-abandon hook delegate here so
the unmonitor + throttle-clear semantics live in exactly one place.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

import requests

from mediaman.services.arr.base import ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.search_trigger import clear_throttle
from mediaman.services.infra import SafeHTTPError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AbandonResult:
    """Outcome of an abandon call.

    For movies, succeeded/failed carry the token 0 for the movie itself; for series they
    carry season numbers. The throttle row is cleared only when failed is empty, so
    partial failures can retry without losing search-count context.
    """

    kind: str  # "movie" or "series"
    succeeded: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    dl_id: str = ""


def abandon_movie(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    arr_id: int,
    dl_id: str,
) -> AbandonResult:
    """Unmonitor *arr_id* in Radarr and clear its throttle row. failed holds 0 when the
    unmonitor call raises or the client cannot be built; the throttle is kept so retries
    know the search count.
    """
    client = build_radarr_from_db(conn, secret_key)
    if client is None:
        logger.warning("abandon_movie: no radarr client available for %s", dl_id)
        return AbandonResult(kind="movie", failed=[0], dl_id=dl_id)
    try:
        client.unmonitor_movie(arr_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning("abandon_movie: unmonitor_movie failed for %s", dl_id, exc_info=True)
        return AbandonResult(kind="movie", failed=[0], dl_id=dl_id)
    clear_throttle(conn, dl_id, service="radarr")
    logger.info("abandon_movie: unmonitored arr_id=%s dl_id=%s", arr_id, dl_id)
    return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)


def abandon_series(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    series_id: int,
    dl_id: str,
) -> AbandonResult:
    """Unmonitor every monitored season of *series_id* in Sonarr, skipping
    already-unmonitored ones. Used for not-yet-released series, which have no per-season
    stuck-search rows. A failure to fetch the series is reported as 0 in failed.
    """
    client = build_sonarr_from_db(conn, secret_key)
    if client is None:
        logger.warning("abandon_series: no sonarr client available for %s", dl_id)
        return AbandonResult(kind="series", failed=[0], dl_id=dl_id)
    try:
        series = client.get_series_by_id(series_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning("abandon_series: get_series_by_id failed for %s", dl_id, exc_info=True)
        return AbandonResult(kind="series", failed=[0], dl_id=dl_id)

    seasons_raw = series.get("seasons") if isinstance(series, dict) else None
    monitored_seasons: list[int] = []
    if isinstance(seasons_raw, list):
        for s in seasons_raw:
            if (
                isinstance(s, dict)
                and s.get("monitored")
                and isinstance(s.get("seasonNumber"), int)
            ):
                monitored_seasons.append(s["seasonNumber"])

    if not monitored_seasons:
        # Nothing left to unmonitor — treat as success.
        clear_throttle(conn, dl_id, service="sonarr")
        logger.info(
            "abandon_series: no monitored seasons for series_id=%s dl_id=%s",
            series_id,
            dl_id,
        )
        return AbandonResult(kind="series", succeeded=[], dl_id=dl_id)

    return abandon_seasons(
        conn,
        secret_key,
        series_id=series_id,
        season_numbers=monitored_seasons,
        dl_id=dl_id,
    )


def abandon_seasons(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    series_id: int,
    season_numbers: list[int],
    dl_id: str,
) -> AbandonResult:
    """Unmonitor each of *season_numbers* in Sonarr, one call per season; partial failures
    land in failed instead of raising. The throttle row is cleared only if every season
    succeeded.

    Raises ValueError for empty *season_numbers* so the endpoint can return 400 instead
    of this silently doing nothing.
    """
    if not season_numbers:
        raise ValueError("abandon_seasons requires at least one season number")

    client = build_sonarr_from_db(conn, secret_key)
    if client is None:
        logger.warning("abandon_seasons: no sonarr client available for %s", dl_id)
        return AbandonResult(kind="series", failed=list(season_numbers), dl_id=dl_id)

    succeeded: list[int] = []
    failed: list[int] = []
    for season in season_numbers:
        try:
            client.unmonitor_season(series_id, season)
            succeeded.append(season)
        except (SafeHTTPError, requests.RequestException, ArrError):
            logger.warning(
                "abandon_seasons: unmonitor_season failed for %s season %s",
                dl_id,
                season,
                exc_info=True,
            )
            failed.append(season)

    if succeeded and not failed:
        clear_throttle(conn, dl_id, service="sonarr")
    logger.info(
        "abandon_seasons: dl_id=%s succeeded=%s failed=%s",
        dl_id,
        succeeded,
        failed,
    )
    return AbandonResult(kind="series", succeeded=succeeded, failed=failed, dl_id=dl_id)
