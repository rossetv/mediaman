"""Mark a film or a whole show watched when the library does not hold it.

The mark is the person's own, under the title's TMDB id. A title the library holds is marked
by its own route.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.repository import seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.ratings import RatedTitle
from mediaman.web.repository.seen_titles import SeenChange, TitleKey
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.ratings import (
    UserId,
    Username,
    named_titles,
    not_found,
    rate_limited,
    title_details,
)
from mediaman.web.routes.watch import mark_undo
from mediaman.web.routes.watch.card_marks import latest_season_out
from mediaman.web.routes.watch.marks_api import undone

router = APIRouter()

# Per-person cap on marks set and taken back.
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=1000)


def _named(username: str, ref: str) -> RatedTitle | JSONResponse:
    found = named_titles(get_db(), username, [ref])
    if ref not in found:
        return not_found()
    return found[ref] or respond_err(
        "no_tmdb_id", status=409, message="This title can't be marked: it has no TMDB match."
    )


def _held(title: RatedTitle) -> JSONResponse | None:
    """What says that the library holds the title, if it does: its own route marks it."""
    conn = get_db()
    refs = seen_titles.library_refs(conn, lib.scanned_library_ids(conn), _key(title))
    if not refs:
        return None
    kind, _, key = refs[0].partition("-")
    return respond_err("in_library", status=409, ref=refs[0], mark_kind=kind, mark_key=key)


def _key(title: RatedTitle) -> TitleKey:
    return title.media_type, title.tmdb_id


def _slot(user_id: int, title: RatedTitle) -> mark_undo.Slot:
    return user_id, title.media_type, f"tmdb-{title.tmdb_id}"


def _keep(change: SeenChange, title: RatedTitle, *, watched: bool) -> None:
    mark_undo.hold(
        _slot(change.user_id, title),
        change.key,
        watched=watched,
        changes=[],
        title=change,
        now=now_utc(),
    )


def _answer(title: RatedTitle, watched: bool, through_season: int | None) -> JSONResponse:
    return respond_ok(
        {
            "watched": watched,
            "media_type": title.media_type,
            "tmdb_id": title.tmdb_id,
            "tmdb_ref": f"tmdb-{title.media_type}-{title.tmdb_id}",
            "through_season": through_season,
        }
    )


@router.put("/api/watch/seen/{ref}")
def api_mark_seen(
    ref: str, request: Request, user_id: UserId, username: Username, undo: str = ""
) -> JSONResponse:
    """Mark the title watched, a show up to the latest season that is out; with undo=1, take
    back marking it not watched."""
    if not _CHANGE_LIMITER.check(username):
        return rate_limited()
    title = _named(username, ref)
    if isinstance(title, JSONResponse):
        return title
    if undo == "1":
        return undone(_slot(user_id, title), username, watched=True)
    held = _held(title)
    if held is not None:
        return held
    conn = get_db()
    show = title.media_type == "tv"
    fields = None
    if show or title.title is None:
        try:
            fields = title_details(request, conn, title, ask=True, anew=show)
        except catalog.TitleNotFound:
            return not_found()
    now = now_utc()
    through = latest_season_out(fields, now.date()) if fields and show else None
    if show and through is None:
        return respond_err(
            "seasons_unknown",
            status=503,
            message="This show can't be marked now: its seasons aren't known.",
        )
    if fields is not None and title.title is None:
        title = RatedTitle(title.media_type, title.tmdb_id, fields["title"] or None, fields["year"])
    change = seen_titles.mark(conn, user_id, title, through, now, in_library=False)
    _keep(change, title, watched=True)
    return _answer(title, True, change.after.through_season if change.after else None)


@router.delete("/api/watch/seen/{ref}")
def api_unmark_seen(ref: str, user_id: UserId, username: Username, undo: str = "") -> JSONResponse:
    """Mark the title not watched; with undo=1, take back marking it watched."""
    if not _CHANGE_LIMITER.check(username):
        return rate_limited()
    title = _named(username, ref)
    if isinstance(title, JSONResponse):
        return title
    if undo == "1":
        return undone(_slot(user_id, title), username, watched=False)
    held = _held(title)
    if held is not None:
        return held
    _keep(seen_titles.unmark(get_db(), user_id, title), title, watched=False)
    return _answer(title, False, None)


__all__ = ["router"]
