"""The Watch page's rows of library titles a page at a time, in any of their orders."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.library_order import is_place, library_page
from mediaman.web.routes.watch.pages import (
    ROW_PAGE_SIZE,
    movie_cards,
    recently_added,
    show_cards,
)

router = APIRouter()


@router.get("/api/watch/library/{kind}/row")
def api_watch_library_row(
    kind: str,
    after: str = "",
    sort: lib.RowOrder = "added",
    limit: Annotated[int, Query(ge=1, le=60)] = ROW_PAGE_SIZE,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The cards of one row (movies, tv, anime or recent) past *after*, where a page of
    the same *sort* ended."""
    if kind not in ("movies", "tv", "anime", "recent"):
        return respond_err("not_found", status=404)
    if after and not is_place(after):
        return respond_err("invalid_request", status=400)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    titles: list[lib.LibraryMovie | lib.LibraryShow] = []
    if kind in ("movies", "recent"):
        titles += lib.library_movies(conn, libraries)
    if kind != "movies":
        titles += [
            s
            for s in lib.library_shows(conn, libraries)
            if kind == "recent" or s.anime == (kind == "anime")
        ]
    if kind == "recent":
        titles = recently_added(titles, now)
    page, end = library_page(conn, username, libraries, titles, sort, limit, after)
    items = _cards(conn, username, libraries, page, now)
    return respond_ok({"items": items, "next": end, "total": len(titles)})


def _cards(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    page: list[lib.LibraryMovie | lib.LibraryShow],
    now: datetime,
) -> list[dict[str, Any]]:
    """The cards of *page*, in its order."""
    movies = [t for t in page if isinstance(t, lib.LibraryMovie)]
    shows = [t for t in page if isinstance(t, lib.LibraryShow)]
    cards: dict[lib.LibraryMovie | lib.LibraryShow, dict[str, Any]] = {}
    if movies:
        pending = lib.pending_deletions(conn, libraries, now)
        films = movie_cards(conn, username, movies, pending, now)
        cards.update(zip(movies, films, strict=True))
    if shows:
        cards.update(zip(shows, show_cards(conn, username, libraries, shows), strict=True))
    return [cards[title] for title in page]


__all__ = ["router"]
