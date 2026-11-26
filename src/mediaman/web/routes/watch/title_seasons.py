"""What each season of a show's page is, and whether the person may ask for it."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from mediaman.services import watch_catalog as catalog
from mediaman.services.arr import build as arr_build
from mediaman.services.arr.seasons import ShowSeasons, is_refused_start, is_still_to_air
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository.season_requests import ShowIds, library_seasons, season_refusals

#: A title's page is quick without Radarr and Sonarr, so it waits no longer than this for them,
#: from before it asks Plex or TMDB.
ARR_WAIT_S = 1.0


def begin_arr_view(conn: sqlite3.Connection, secret_key: str) -> Callable[[], catalog.ArrView]:
    """Begin the Radarr and Sonarr view, and give what collects it."""
    due = time.monotonic() + ARR_WAIT_S
    catalog.arr_view(conn, secret_key, wait_s=0)

    def collect() -> catalog.ArrView:
        view = catalog.arr_view(conn, secret_key, wait_s=max(0.0, due - time.monotonic()))
        if view.generation is not None:
            return view
        # A view still being built knows nothing yet, not even what is not set up.
        builds = (
            ("radarr", arr_build.build_radarr_from_db),
            ("sonarr", arr_build.build_sonarr_from_db),
        )
        unset = frozenset(name for name, build in builds if build(conn, secret_key) is None)
        return replace(view, not_set_up=unset)

    return collect


def missing_seasons(
    seasons: Sequence[Mapping[str, Any]], tmdb_seasons: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The seasons of a show only TMDB knows, beside Plex's *seasons* of it."""
    held = {s["number"] for s in seasons}
    return [
        {
            **s,
            "in_library": False,
            "selected": False,
            "key": None,
            "watched": False,
            "summary": "",
            "year": None,
        }
        for s in tmdb_seasons
        if s["number"] not in held
    ]


def fill_season_status(
    out: dict[str, Any],
    conn: sqlite3.Connection,
    arr: catalog.ArrView,
    username: str,
    *,
    tmdb_id: int | None,
    tvdb_id: int | None,
    tmdb_seasons: Sequence[Mapping[str, Any]] = (),
    download_url: str | None = None,
) -> None:
    """Give each season of the show *out* its status, and *out* what asks for more of them."""
    seasons: list[dict[str, Any]] = [*out["seasons"], *out["more_seasons"]]
    air_dates = {s["number"]: s.get("air_date") for s in tmdb_seasons}
    tmdb_id = tmdb_id or (arr.tmdb_id_for_tvdb(tvdb_id) if tvdb_id else None)
    tvdb_id = tvdb_id or (arr.tvdb_id(tmdb_id) if tmdb_id else None)
    sonarr = arr.show_seasons(tmdb_id, tvdb_id, air_dates)
    show = ShowIds(tmdb_id, tvdb_id, out["title"])
    numbers = {s["number"] for s in seasons}
    refused = season_refusals(conn, show, numbers | (sonarr.wanted if sonarr else set()))
    # A season deleted lately may still be in the last scan.
    removed = {number for number, refusal in refused.items() if refusal == "removed"}
    in_plex = library_seasons(conn, show) - removed | {s["number"] for s in seasons if s["key"]}
    is_admin = get_user_role(conn, username) == "admin"
    answered = arr.answers_for("tv")
    for season in seasons:
        number = season["number"]
        state = sonarr.states.get(number) if sonarr else None
        if number in in_plex and (
            state is None or state.status in ("asked", "not_asked", "not_out")
        ):
            status: str | None = "held"
        elif state is not None:
            status = state.status
        elif not answered or (sonarr is not None and not sonarr.whole):
            status = None
        else:
            status = _tmdb_status(season, air_dates.get(number))
        asked = state is not None and state.asked
        refusal = None
        wanted = status in ("partly_held", "not_asked", "not_out") and not asked
        if wanted and sonarr is not None:
            refusal = _refusal(sonarr, refused, number, is_admin=is_admin)
        season.update(
            status=status, asked=asked, askable=wanted and refusal is None, refusal=refusal
        )
    # A view not built yet, unlike one Sonarr gave no answer for, is there when asked for again.
    out["seasons_pending"] = arr.generation is None
    # An admin is told before they ask; a viewer may not ask.
    if is_admin and sonarr is not None and not sonarr.monitored:
        out["also_starts"] = sorted(sonarr.wanted)
    if tmdb_id is None or "sonarr" in arr.not_set_up:
        return
    body = {"media_type": "tv", "tmdb_id": tmdb_id, "title": out["title"]}
    out["seasons_request"] = (
        {"url": download_url, "body": {}}
        if download_url
        else {"url": "/api/search/download", "body": body}
    )


def _tmdb_status(season: Mapping[str, Any], air_date: str | None) -> str:
    still_to_air = is_still_to_air(air_date)
    # TMDB lists an announced season with no date and no episodes.
    if still_to_air or (still_to_air is None and season["episode_count"] == 0):
        return "not_out"
    return "not_asked"


def _refusal(
    sonarr: ShowSeasons, refused: Mapping[int, str], number: int, *, is_admin: bool
) -> str | None:
    """Why a season of a show Sonarr tracks is not for this person to ask for, if it is not."""
    if number not in sonarr.states:
        return "not_listed"
    refusal = refused.get(number)
    if refusal == "leaving" or (refusal == "removed" and not is_admin):
        return refusal
    started = sonarr.started_with(number)
    if started and not sonarr.whole:
        return "show_unmonitored"
    if is_refused_start(started, refused, (), is_admin=is_admin):
        return "show_unmonitored"
    return None
