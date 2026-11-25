"""The seasons a download request names, and adding them to a show Sonarr already tracks."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Literal

import requests
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.services import watch_catalog
from mediaman.services.arr import ArrError
from mediaman.services.arr._types import SonarrSeries
from mediaman.services.arr.base import ArrClient, SeasonsNotListed
from mediaman.services.arr.seasons import add_season, is_asked
from mediaman.services.arr.state import season_on_disk
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository.season_requests import (
    ShowIds,
    library_seasons,
    record_season,
    season_refusals,
)
from mediaman.web.responses import respond_failure

logger = logging.getLogger(__name__)

Result = Literal["asked", "already_asked", "held", "not_listed", "leaving", "removed", "failed"]


class SeasonPicks(BaseModel):
    """The seasons of a show to download. extra="forbid" turns unknown keys into a 422, and the
    lists are limited to 100 entries to refuse flood payloads."""

    model_config = ConfigDict(extra="forbid")

    monitored_seasons: list[int] | None = Field(default=None, max_length=100)
    search_seasons: list[int] | None = Field(default=None, max_length=100)

    def to_add(self) -> list[int] | None:
        """The seasons to add a show with; None for all of it.

        Sonarr searches every monitored season it is missing files for, so a season not
        asked to be searched is not monitored.
        """
        return self.monitored_seasons if self.search_seasons is None else self.search_seasons


def no_season() -> JSONResponse:
    return respond_failure("Pick at least one season", status=400)


def seasons_not_listed(exc: SeasonsNotListed, asked: Sequence[int]) -> JSONResponse:
    """The refusal of seasons Sonarr does not list: the person's own pick, not a fault."""
    message = str(exc)
    if set(asked) - set(exc.seasons):
        message += " Untick " + ("it" if len(exc.seasons) == 1 else "them") + " to add the rest."
    return respond_failure(message, status=422)


def add_to_tracked_show(
    conn: sqlite3.Connection,
    sonarr: ArrClient,
    *,
    tmdb_id: int,
    tvdb_id: int,
    title: str,
    numbers: Sequence[int],
    actor: str,
    email: str | None,
) -> JSONResponse:
    """Add the seasons *numbers* to a show Sonarr tracks, and answer for each of them.

    Only adds: a season held, or wanted already, is left as it is and not searched again, so the
    same request can be sent twice. Each season is recorded as it is added. Raises what Sonarr
    raises when the show cannot be read.
    """
    series = sonarr.get_series_by_tvdb(tvdb_id)
    series_id = series.get("id") if series else None
    if series is None or series_id is None:
        raise ArrError("Sonarr holds the series with no usable id")
    wanted = sorted(set(numbers))
    judge = _judge(conn, series, ShowIds(tmdb_id, tvdb_id, title), set(wanted), actor)
    results: dict[int, Result] = {}
    answering = True
    try:
        for number in wanted:
            result = judge(number)
            if result is None and answering:
                try:
                    result = "asked" if add_season(sonarr, series_id, number) else "failed"
                except (SafeHTTPError, requests.RequestException, ValueError, ArrError):
                    logger.exception("Sonarr does not say what became of season %s", number)
                    answering = False
            results[number] = result or "failed"
            if result in ("asked", "already_asked"):
                record_season(
                    conn,
                    title=title,
                    tmdb_id=tmdb_id,
                    tvdb_id=tvdb_id,
                    number=number,
                    actor=actor,
                    email=email,
                    added=result == "asked",
                )
    finally:
        # A failed season may have been added too.
        watch_catalog.invalidate()
    return _answer(title, results)


def _judge(
    conn: sqlite3.Connection, series: SonarrSeries, show: ShowIds, wanted: set[int], actor: str
) -> Callable[[int], Result | None]:
    """What each season of *wanted* is without being added; None for one to add."""
    listed = {s.get("seasonNumber"): s for s in series.get("seasons") or []}
    # The request's title is whatever the browser sent.
    show = replace(show, title=series.get("title") or show.title)
    refused = season_refusals(conn, show, wanted)
    in_plex = library_seasons(conn, show)
    is_admin = get_user_role(conn, actor) == "admin"

    def judge(number: int) -> Result | None:
        season = listed.get(number)
        refusal = refused.get(number)
        if season is None:
            return "not_listed"
        if season_on_disk(season):
            return "held"
        if refusal == "leaving":
            return "leaving"
        if refusal == "removed":
            # Sonarr may still be to be told of the deletion, and so want it, and Plex's last
            # scan may list it. Asked for by an admin, it is left monitored then.
            return None if is_admin else "removed"
        if is_asked(series, season):
            return "already_asked"
        in_sonarr = (season.get("statistics") or {}).get("episodeFileCount")
        return "held" if number in in_plex and not in_sonarr else None

    return judge


# What each result says of its seasons, after their names: for one, and for more.
_SAID: tuple[tuple[Result, str, str], ...] = (
    ("already_asked", "is already on its way", "are already on their way"),
    ("held", "is already here", "are already here"),
    ("not_listed", "isn't in Sonarr's listing", "aren't in Sonarr's listing"),
    (
        "leaving",
        "is about to be deleted: keep it first",
        "are about to be deleted: keep them first",
    ),
    (
        "removed",
        "was deleted here, so only an admin can bring it back",
        "were deleted here, so only an admin can bring them back",
    ),
    ("failed", "couldn't be added: try again", "couldn't be added: try again"),
)
# The answer when no season is here or on its way, by the first result there is.
_REFUSED: tuple[tuple[Result, int], ...] = (
    ("failed", 502),
    ("leaving", 409),
    ("removed", 403),
    ("not_listed", 422),
)


def _names(numbers: Sequence[int]) -> str:
    if len(numbers) == 1:
        return f"season {numbers[0]}"
    return "seasons " + ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _answer(title: str, results: dict[int, Result]) -> JSONResponse:
    found = set(results.values())
    said = []
    if "asked" in found:
        added = [n for n, result in results.items() if result == "asked"]
        said.append(f"Added {_names(added)} of '{title}' to Sonarr.")
    for result, one, more in _SAID:
        if numbers := [n for n, r in results.items() if r == result]:
            names = _names(numbers)
            verb = one if len(numbers) == 1 else more
            said.append(f"{names[0].upper()}{names[1:]} {verb}.")
    message = " ".join(said)
    seasons = [{"number": n, "result": result} for n, result in results.items()]
    if found & {"asked", "already_asked", "held"}:
        return JSONResponse({"ok": True, "message": message, "seasons": seasons})
    status = next(status for result, status in _REFUSED if result in found)
    return respond_failure(message, status=status, seasons=seasons)
