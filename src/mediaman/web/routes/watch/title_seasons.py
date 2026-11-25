"""What each season of a show's page is, and whether the person may ask for it."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from mediaman.services import watch_catalog as catalog
from mediaman.services.arr.seasons import SeasonState
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository.season_requests import ShowIds, library_seasons, season_refusals

#: A library show's page is quick without Sonarr, so it waits this long for it at most.
SONARR_WAIT_S = 2.0


def add_missing_seasons(
    seasons: Sequence[dict[str, Any]], tmdb_seasons: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """*seasons* of a library show as Plex has them, and those only TMDB knows, by number."""
    held = {s["number"] for s in seasons}
    missing = [
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
    return sorted([*seasons, *missing], key=lambda s: int(s["number"]))


def fill_season_status(
    out: dict[str, Any],
    conn: sqlite3.Connection,
    arr: catalog.ArrView,
    username: str,
    *,
    tmdb_id: int | None,
    tvdb_id: int | None,
    download_url: str | None = None,
) -> None:
    """Give each season of the show *out* its status, and *out* what asks for more of them."""
    seasons: list[dict[str, Any]] = out["seasons"]
    tmdb_id = tmdb_id or (arr.tmdb_id_for_tvdb(tvdb_id) if tvdb_id else None)
    tvdb_id = tvdb_id or (arr.tvdb_id(tmdb_id) if tmdb_id else None)
    states = arr.season_states(tmdb_id, tvdb_id)
    show = ShowIds(tmdb_id, tvdb_id, out["title"])
    refused = season_refusals(conn, show, {s["number"] for s in seasons})
    # A season deleted lately may still be in the last scan.
    removed = {number for number, refusal in refused.items() if refusal == "removed"}
    in_plex = library_seasons(conn, show) - removed | {s["number"] for s in seasons if s["key"]}
    if states is None:
        # A show Sonarr does not track is added as any other.
        refused = {}
    is_admin = get_user_role(conn, username) == "admin"
    for season in seasons:
        number = season["number"]
        state = states.get(number) if states is not None else None
        season["status"] = _status(season, state, arr.answers_for("tv"), number in in_plex)
        season["asked"] = state is not None and state.asked
        refusal: str | None = refused.get(number)
        if refusal == "removed" and is_admin:
            refusal = None
        if states is not None and state is None and season["status"] != "held":
            refusal = "not_listed"
        wanted = season["status"] in ("partly_held", "not_asked", "not_out")
        season["askable"] = wanted and not season["asked"] and refusal is None
        season["refusal"] = refusal if wanted and not season["asked"] else None
    if tmdb_id is None:
        return
    body = {"media_type": "tv", "tmdb_id": tmdb_id, "title": out["title"]}
    out["seasons_request"] = (
        {"url": download_url, "body": {}}
        if download_url
        else {"url": "/api/search/download", "body": body}
    )


def _status(
    season: Mapping[str, Any], state: SeasonState | None, answered: bool, in_plex: bool
) -> str | None:
    if state is not None and state.status not in ("asked", "not_asked", "not_out"):
        return state.status
    if in_plex:
        return "held"
    if not answered:
        return None
    if state is not None:
        return state.status
    # TMDB lists an announced season with no episodes.
    return "not_out" if season["episode_count"] == 0 else "not_asked"
