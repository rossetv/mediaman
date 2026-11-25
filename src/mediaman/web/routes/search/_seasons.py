"""The seasons a download request names, and adding them to a show Sonarr already tracks."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import requests
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.services import watch_catalog
from mediaman.services.arr import ArrError
from mediaman.services.arr._sonarr_methods import SeriesChange
from mediaman.services.arr._types import SonarrSeries
from mediaman.services.arr.base import ArrClient, SeasonsNotListed
from mediaman.services.arr.seasons import (
    SeasonRefused,
    add_season,
    is_asked,
    is_refused_start,
    readable_seasons,
    show_seasons,
)
from mediaman.services.arr.state import _season_stats, season_on_disk
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository.season_requests import (
    ShowIds,
    library_seasons,
    record_season,
    record_started,
    season_refusals,
)
from mediaman.web.responses import respond_failure

logger = logging.getLogger(__name__)

#: How long adding seasons to a show may take; those not come to by then are answered as failed.
REQUEST_SECONDS = 40.0

Result = Literal[
    "asked",
    "already_asked",
    "held",
    "not_listed",
    "leaving",
    "removed",
    "show_unmonitored",
    "failed",
    # Asked for in Sonarr, with no history line and no mail.
    "unrecorded",
]


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


@dataclass(frozen=True)
class _Request:
    """The seasons *named* of a show, and whether an admin asks."""

    conn: sqlite3.Connection
    show: ShowIds
    named: list[int]
    is_admin: bool

    def judge(self, series: SonarrSeries, number: int) -> Result | None:
        """What season *number* is without being added to *series*; None for one to add."""
        seasons, whole = readable_seasons(series)
        season = next((s for s in seasons if s["seasonNumber"] == number), None)
        if season is None:
            return "not_listed" if whole else "failed"
        if season_on_disk(season):
            return "held"
        started = self.started(series, number)
        refused = season_refusals(self.conn, self.show, {number} | started)
        refusal = refused.get(number)
        if refusal == "leaving":
            return "leaving"
        if refusal == "removed":
            # Sonarr may still be to be told of the deletion, and so want it, and Plex's last
            # scan may list it. Asked for by an admin, it is left monitored then.
            if not self.is_admin:
                return "removed"
        elif is_asked(series, season):
            return "already_asked"
        elif not _season_stats(season).get("episodeFileCount") and (
            number in library_seasons(self.conn, self.show)
        ):
            return "held"
        bring_back = self.named if self.is_admin else ()
        barred = {n for n, why in refused.items() if why == "leaving" or n not in bring_back}
        # One whose state cannot be read could be started as well.
        if started and not whole:
            return "show_unmonitored"
        if is_refused_start(started, barred, self.named, is_admin=self.is_admin):
            return "show_unmonitored"
        return None

    def started(self, series: SonarrSeries, number: int) -> set[int]:
        return show_seasons(series, {}, {}).started_with(number)

    def was_removed(self, number: int) -> bool:
        return season_refusals(self.conn, self.show, {number}).get(number) == "removed"


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
    """Add the seasons *numbers* to a show Sonarr tracks, and answer for each of them, in a
    limited time.

    Only adds: a season held, or wanted already, is left as it is and not searched again, so the
    same request can be sent twice. Each season is recorded as it is added. Raises what Sonarr
    raises when the show cannot be read.
    """
    with sonarr.within(REQUEST_SECONDS):
        return _add_all(
            conn,
            sonarr,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            title=title,
            numbers=numbers,
            actor=actor,
            email=email,
        )


def _add_all(
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
    series = sonarr.get_series_by_tvdb(tvdb_id)
    series_id = series.get("id") if series else None
    if series is None or not isinstance(series_id, int):
        raise ArrError("Sonarr holds the series with no usable id")
    wanted = sorted(set(numbers))
    # The request's title is whatever the browser sent.
    show = ShowIds(tmdb_id, tvdb_id, str(series.get("title") or title))
    request = _Request(conn, show, wanted, get_user_role(conn, actor) == "admin")
    results: dict[int, Result] = {}
    started: set[int] = set()
    answering = True
    try:
        for number in wanted:
            result = request.judge(series, number)
            was_removed = request.was_removed(number)
            change = SeriesChange()
            also: set[int] = set()
            if result is None and answering:
                result, answering = _add(sonarr, series_id, request, number, change, also)
            result = result or "failed"
            if result in ("asked", "already_asked"):
                try:
                    refusal = record_season(
                        conn,
                        show,
                        title=title,
                        number=number,
                        actor=actor,
                        email=email,
                        added=result == "asked",
                        was_removed=was_removed,
                        started=also if change.series else (),
                    )
                except sqlite3.Error:
                    logger.exception("Season %s of %s could not be recorded", number, show)
                    result, refusal = "unrecorded", None
                if refusal is not None:
                    if result == "asked":
                        sonarr.take_back_season(series_id, number, change)
                    result = refusal
            if also and result not in ("asked", "unrecorded") and change.left_on:
                try:
                    record_started(
                        conn, show, title=title, number=number, actor=actor, started=also
                    )
                except sqlite3.Error:
                    logger.exception("Started seasons %s could not be recorded", sorted(also))
            if change.left_on or (result in ("asked", "unrecorded") and change.series):
                started |= also
            results[number] = result
    finally:
        # A failed season may have been added too.
        watch_catalog.invalidate()
    return _answer(title, results, sorted(started))


def _add(
    sonarr: ArrClient,
    series_id: int,
    request: _Request,
    number: int,
    change: SeriesChange,
    also: set[int],
) -> tuple[Result | None, bool]:
    """What became of adding the season, and whether Sonarr is worth asking for more. Tells
    *change* what was changed, and *also* which seasons nobody named that starts."""

    def judge(series: SonarrSeries) -> Result | None:
        result = request.judge(series, number)
        if result is None:
            also.update(request.started(series, number) - set(request.named))
        return result

    try:
        add_season(sonarr, series_id, number, judge, change)
    except SeasonRefused as exc:
        return cast(Result, exc.result), True
    except (SafeHTTPError, requests.RequestException, ValueError, ArrError) as exc:
        logger.exception("Season %s of Sonarr series %s could not be added", number, series_id)
        return "failed", not _gave_no_answer(exc)
    return "asked", True


def _gave_no_answer(exc: Exception) -> bool:
    if isinstance(exc, SafeHTTPError):
        # 0 is a transport error; a proxy answers 502-504 for a service that is down.
        return exc.status_code in (0, 502, 503, 504)
    return isinstance(exc, requests.RequestException)


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
    (
        "show_unmonitored",
        "can't be added: the show is off in Sonarr, and turning it on would start other seasons",
        "can't be added: the show is off in Sonarr, and turning it on would start other seasons",
    ),
    ("failed", "couldn't be added: try again", "couldn't be added: try again"),
    (
        "unrecorded",
        "is asked for, but mediaman couldn't note it: try again",
        "are asked for, but mediaman couldn't note it: try again",
    ),
)
# The answer when no season is here or on its way, by the first result there is.
_REFUSED: tuple[tuple[Result, int], ...] = (
    ("failed", 502),
    ("unrecorded", 502),
    ("leaving", 409),
    ("show_unmonitored", 409),
    ("removed", 403),
    ("not_listed", 422),
)


def _names(numbers: Sequence[int]) -> str:
    if len(numbers) == 1:
        return f"season {numbers[0]}"
    return "seasons " + ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"


def _answer(title: str, results: dict[int, Result], started: Sequence[int]) -> JSONResponse:
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
    if started:
        said.append(f"The show was off in Sonarr, so {_names(started)} started too.")
    message = " ".join(said)
    told = {
        "seasons": [{"number": n, "result": result} for n, result in results.items()],
        "also_started": list(started),
    }
    if found & {"asked", "already_asked", "held"}:
        return JSONResponse({"ok": True, "message": message, **told})
    status = next(status for result, status in _REFUSED if result in found)
    return respond_failure(message, status=status, **told)
