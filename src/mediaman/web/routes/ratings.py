"""What each person thinks of a title: four levels, theirs alone.

A title is named by the ref Watch uses for it, and its TMDB id and name are read here, never
taken from the page. The cleanup scan, deletions and keeps read nothing of this.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations.repository import fetch_suggestion_by_id
from mediaman.services.openai.recommendations.user_picks import fetch_pick
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import ratings
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.ratings import RatedTitle
from mediaman.web.repository.watch_progress import get_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.catalog_api import REF_RE
from mediaman.web.routes.watch.pages import is_valid_key

router = APIRouter()

# Per-person cap on ratings set and taken back, and on questions at a title's end.
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=1000)
_READ_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)
# Titles of one read.
_MAX_REFS = 100
# A worker thread waits for the answer.
_TMDB_TIMEOUT_S = 3.0


class _RatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal[-2, -1, 1, 2]


def _user_id(username: Annotated[str, Depends(get_current_user)]) -> int:
    user_id = ratings.find_user_id(get_db(), username)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


_UserId = Annotated[int, Depends(_user_id)]
_Username = Annotated[str, Depends(get_current_user)]


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _rate_limited() -> JSONResponse:
    return respond_err(
        "too_many_requests", status=429, message="Too many changes at once — slow down"
    )


def _no_tmdb_id() -> JSONResponse:
    return respond_err(
        "no_tmdb_id", status=409, message="This title can't be rated: it has no TMDB match."
    )


def _titles(
    conn: sqlite3.Connection, username: str, refs: Iterable[str]
) -> dict[str, RatedTitle | None]:
    """The titles *refs* name, from what is stored here; None for one with no TMDB id. A ref that
    is malformed or names nothing this person may see is left out."""
    refs = [ref for ref in dict.fromkeys(refs) if REF_RE.fullmatch(ref)]
    found = ratings.library_titles(conn, lib.scanned_library_ids(conn), refs)
    for ref in refs:
        kind, _, rest = ref.partition("-")
        if kind == "tmdb":
            media_type, _, tmdb_id = rest.partition("-")
            if int(tmdb_id) > 0:
                found[ref] = RatedTitle(media_type, int(tmdb_id))
        elif kind in ("pick", "rec"):
            rec = (
                fetch_pick(conn, username, int(rest))
                if kind == "pick"
                else fetch_suggestion_by_id(conn, int(rest))
            )
            if rec is not None and rec.media_type in ("movie", "tv"):
                found[ref] = (
                    RatedTitle(rec.media_type, rec.tmdb_id, rec.title, rec.year)
                    if rec.tmdb_id and rec.tmdb_id > 0
                    else None
                )
    return found


def _details(
    request: Request, conn: sqlite3.Connection, title: RatedTitle, *, ask: bool
) -> dict[str, Any] | None:
    """TMDB's fields of *title*, asking it only with *ask*; None when there is no answer. Raises
    TitleNotFound."""
    client = TmdbClient.from_db(
        conn, request.app.state.config.secret_key, timeout=_TMDB_TIMEOUT_S, retry=False
    )
    if client is None:
        return None
    try:
        return catalog.details(
            client, title.media_type, title.tmdb_id, allow_fetch=lambda: ask, brief=True
        )
    except catalog.TitleNotFound:
        raise
    except (catalog.TmdbUnavailable, disk_cache.Refused):
        return None


def _answer(title: RatedTitle, level: int | None, was: int | None) -> JSONResponse:
    return respond_ok(
        {
            "media_type": title.media_type,
            "tmdb_id": title.tmdb_id,
            "level": level,
            "was": was,
        }
    )


@router.get("/api/watch/ratings")
def api_ratings(user_id: _UserId, username: _Username, refs: str = "") -> JSONResponse:
    """This person's rating of each title of *refs*, separated by commas."""
    if not _READ_LIMITER.check(username):
        return _rate_limited()
    asked = refs.split(",")
    if len(asked) > _MAX_REFS:
        return respond_err("too_many_titles", status=400)
    conn = get_db()
    levels = ratings.levels_for(conn, user_id)
    return respond_ok(
        {
            "ratings": {
                ref: {
                    "level": levels.get((title.media_type, title.tmdb_id)) if title else None,
                    "rateable": title is not None,
                }
                for ref, title in _titles(conn, username, asked).items()
            }
        }
    )


@router.put("/api/watch/ratings/{ref}")
def api_rate(
    ref: str, body: _RatingRequest, request: Request, user_id: _UserId, username: _Username
) -> JSONResponse:
    if not _CHANGE_LIMITER.check(username):
        return _rate_limited()
    conn = get_db()
    found = _titles(conn, username, [ref])
    if ref not in found:
        return _not_found()
    title = found[ref]
    if title is None:
        return _no_tmdb_id()
    if title.title is None:
        try:
            fields = _details(request, conn, title, ask=True)
        except catalog.TitleNotFound:
            return _not_found()
        if fields is not None:
            title = RatedTitle(
                title.media_type, title.tmdb_id, fields["title"] or None, fields["year"]
            )
    was = ratings.set_level(conn, user_id, title, body.level, now_utc())
    return _answer(title, body.level, was)


@router.delete("/api/watch/ratings/{ref}")
def api_unrate(ref: str, user_id: _UserId, username: _Username) -> JSONResponse:
    if not _CHANGE_LIMITER.check(username):
        return _rate_limited()
    conn = get_db()
    found = _titles(conn, username, [ref])
    if ref not in found:
        return _not_found()
    title = found[ref]
    if title is None:
        return _no_tmdb_id()
    was = ratings.clear_level(conn, user_id, title)
    return _answer(title, None, was)


@router.post("/api/watch/ratings/ask/{rating_key}")
def api_rating_ask(
    rating_key: str, request: Request, user_id: _UserId, username: _Username
) -> JSONResponse:
    """Whether to ask this person, who just finished the movie or episode, what they thought.

    Said once of a film and of a season; a question that is ignored is not put again.
    """
    if not _CHANGE_LIMITER.check(username):
        return _rate_limited()
    if not is_valid_key(rating_key):
        return _not_found()
    conn = get_db()
    nothing = respond_ok({"ask": False})
    played = get_progress(conn, username, [rating_key]).get(rating_key)
    if played is None or not played.watched or not played.played:
        return nothing
    show_key = played.show_rating_key
    ref = f"show-{show_key}" if show_key else f"movie-{rating_key}"
    title = _titles(conn, username, [ref]).get(ref)
    if title is None:
        return nothing
    season = 0
    if show_key:
        end = ratings.season_end(conn, rating_key, show_key)
        if end is None or not end.last:
            return nothing
        season = end.number
        try:
            fields = _details(request, conn, title, ask=False)
        except catalog.TitleNotFound:
            fields = None
        # The last episode held may not be the season's: not while it is still on air.
        total = next(
            (
                s["episode_count"]
                for s in (fields or {}).get("tmdb_seasons", [])
                if s["number"] == season
            ),
            None,
        )
        if not total or end.episode < total:
            return nothing
    if not ratings.claim_ask(conn, user_id, title, season, now_utc()):
        return nothing
    return respond_ok(
        {
            "ask": True,
            "ref": ref,
            "media_type": title.media_type,
            "tmdb_id": title.tmdb_id,
            "title": title.title,
            "season": season or None,
        }
    )


__all__ = ["router"]
