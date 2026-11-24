"""The Watch page's library rows a page at a time, newest added first or A to Z."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
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
    """The cards of one library row (movies, tv or anime) past the *after* cursor, which is of the same
    *sort*."""
    if kind not in ("movies", "tv", "anime"):
        return respond_err("not_found", status=404)
    cursor = lib.parse_cursor(after) if after else None
    if after and cursor is None:
        return respond_err("invalid_request", status=400)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    items: list[dict[str, Any]]
    if kind == "movies":
        movies = lib.in_order(lib.library_movies(conn, libraries), sort)
        total = len(movies)
        page, next_cursor = lib.row_page(movies, cursor, limit, sort)
        now = now_utc()
        items = movie_cards(conn, username, page, lib.pending_deletions(conn, libraries, now), now)
    else:
        shows = lib.in_order(
            [s for s in lib.library_shows(conn, libraries) if s.anime == (kind == "anime")], sort
        )
        total = len(shows)
        show_page, next_cursor = lib.row_page(shows, cursor, limit, sort)
        items = show_cards(conn, username, libraries, show_page)
    return respond_ok(
        {
            "items": items,
            "next": lib.cursor_text(next_cursor) if next_cursor else None,
            "total": total,
        }
    )


__all__ = ["router"]
