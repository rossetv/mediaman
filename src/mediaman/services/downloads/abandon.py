"""Abandon-search service.

Single chokepoint for unmonitoring stuck items in Radarr/Sonarr. Both the
manual API endpoint and the scheduler's auto-abandon hook delegate here so
the unmonitor + throttle-clear semantics live in exactly one place.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import cast

import requests

from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.search_trigger import clear_throttle
from mediaman.services.infra.http import SafeHTTPError

logger = logging.getLogger(__name__)


@dataclass
class AbandonResult:
    """Outcome of an abandon call.

    For movies, ``succeeded``/``failed`` carry the literal token ``0`` to
    signal "the movie itself" — there is no per-season concept.  For
    series, the lists carry the actual season numbers.

    A fully successful call has ``failed == []``; the throttle row is only
    cleared when nothing failed, so partial-failure callers can retry the
    rest without losing search-count context.
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
    """Unmonitor *arr_id* in Radarr and clear its throttle row.

    Returns an :class:`AbandonResult` whose ``failed`` list contains ``0``
    when the unmonitor call raises or the Radarr client cannot be built.
    The throttle is preserved on failure so retries still know how many
    times mediaman has been poking.
    """
    raw_client = build_radarr_from_db(conn, secret_key)
    if raw_client is None:
        logger.warning("abandon_movie: no radarr client available for %s", dl_id)
        return AbandonResult(kind="movie", failed=[0], dl_id=dl_id)
    client = cast(ArrClient, raw_client)
    try:
        client.unmonitor_movie(arr_id)
    except (SafeHTTPError, requests.RequestException, ArrError):
        logger.warning("abandon_movie: unmonitor_movie failed for %s", dl_id, exc_info=True)
        return AbandonResult(kind="movie", failed=[0], dl_id=dl_id)
    clear_throttle(conn, dl_id)
    logger.info("abandon_movie: unmonitored arr_id=%s dl_id=%s", arr_id, dl_id)
    return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)


def abandon_series(
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    series_id: int,
    dl_id: str,
) -> AbandonResult:
    """Unmonitor every monitored season of *series_id* in Sonarr.

    Used when the user stops tracking a not-yet-released series from the
    "Coming soon" list — there are no per-season stuck-search rows to
    pick from, so we unmonitor the whole show. Already-unmonitored
    seasons are skipped. If the series payload cannot be fetched the
    failure is signalled with the literal token ``0`` in ``failed``.
    """
    raw_client = build_sonarr_from_db(conn, secret_key)
    if raw_client is None:
        logger.warning("abandon_series: no sonarr client available for %s", dl_id)
        return AbandonResult(kind="series", failed=[0], dl_id=dl_id)
    client = cast(ArrClient, raw_client)
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
        clear_throttle(conn, dl_id)
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
    """Unmonitor each of *season_numbers* on *series_id* in Sonarr.

    Loops one ``unmonitor_season`` call per season; partial failures are
    surfaced via the result's ``failed`` list rather than raising.  The
    throttle row is cleared only when every season succeeded, so the next
    poke (if the user re-monitors the failed seasons) keeps its history.

    Raises :class:`ValueError` when *season_numbers* is empty — the
    endpoint should reject zero-season requests with a 400 rather than
    have this service silently no-op.
    """
    if not season_numbers:
        raise ValueError("abandon_seasons requires at least one season number")

    raw_client = build_sonarr_from_db(conn, secret_key)
    if raw_client is None:
        logger.warning("abandon_seasons: no sonarr client available for %s", dl_id)
        return AbandonResult(kind="series", failed=list(season_numbers), dl_id=dl_id)
    client = cast(ArrClient, raw_client)

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
        clear_throttle(conn, dl_id)
    logger.info(
        "abandon_seasons: dl_id=%s succeeded=%s failed=%s",
        dl_id,
        succeeded,
        failed,
    )
    return AbandonResult(kind="series", succeeded=succeeded, failed=failed, dl_id=dl_id)
