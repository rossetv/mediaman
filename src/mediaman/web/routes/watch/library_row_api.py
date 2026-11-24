"""The Watch page's library rows a page at a time, in any of their orders."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.library_order import is_place, library_page
from mediaman.web.routes.watch.pages import ROW_PAGE_SIZE, movie_cards, show_cards

router = APIRouter()


@router.get("/api/watch/library/{kind}/row")
def api_watch_library_row(
    kind: str,
    after: str = "",
    sort: lib.RowOrder = "added",
    limit: Annotated[int, Query(ge=1, le=60)] = ROW_PAGE_SIZE,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The cards of one library row (movies, tv or anime) past *after*, where a page of
    the same *sort* ended."""
    if kind not in ("movies", "tv", "anime"):
        return respond_err("not_found", status=404)
    if after and not is_place(after):
        return respond_err("invalid_request", status=400)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    if kind == "movies":
        movies = lib.library_movies(conn, libraries)
        total = len(movies)
        page, end = library_page(conn, username, libraries, movies, sort, limit, after)
        now = now_utc()
        items = movie_cards(conn, username, page, lib.pending_deletions(conn, libraries, now), now)
    else:
        shows = [s for s in lib.library_shows(conn, libraries) if s.anime == (kind == "anime")]
        total = len(shows)
        show_page, end = library_page(conn, username, libraries, shows, sort, limit, after)
        items = show_cards(conn, username, libraries, show_page)
    return respond_ok({"items": items, "next": end, "total": total})


__all__ = ["router"]
