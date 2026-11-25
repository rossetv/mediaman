"""The Watch page's rows of library titles a page at a time, in any of their orders."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch import all_titles
from mediaman.web.routes.watch.library_order import is_place, library_page
from mediaman.web.routes.watch.pages import (
    ROW_PAGE_SIZE,
    recently_added,
    title_cards,
)

router = APIRouter()


@router.get("/api/watch/library/{kind}/row")
def api_watch_library_row(
    kind: str,
    after: str = "",
    sort: lib.TitleOrder = "added",
    type_: Annotated[str, Query(alias="type")] = "",
    only: str = "",
    limit: Annotated[int, Query(ge=1, le=60)] = ROW_PAGE_SIZE,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """The cards of one row (movies, tv, anime or recent) past *after*, where a page of
    the same *sort* ended. Of library, which is All titles, those of *type*, or the one named *only*."""
    if kind not in ("movies", "tv", "anime", "recent", "library"):
        return respond_err("not_found", status=404)
    if kind != "library" and sort in all_titles.ADMIN_SORTS:
        return respond_err("invalid_request", status=400)
    if after and not is_place(after):
        return respond_err("invalid_request", status=400)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    now = now_utc()
    if kind == "library":
        return _library(conn, username, libraries, after, sort, type_, only, limit, now)
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
    order = cast(lib.RowOrder, sort)
    page, end = library_page(conn, username, libraries, titles, order, limit, after)
    items = title_cards(conn, username, libraries, page, now)
    return respond_ok({"items": items, "next": end, "total": len(titles)})


def _library(
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    after: str,
    sort: lib.TitleOrder,
    type_: str,
    only: str,
    limit: int,
    now: datetime,
) -> JSONResponse:
    admin = get_user_role(conn, username) == "admin"
    if not admin and (sort in all_titles.ADMIN_SORTS or type_ in all_titles.ADMIN_TYPES):
        return respond_err("forbidden", status=403)
    if all_titles.known_type(type_, admin) != type_:
        return respond_err("invalid_request", status=400)

    def read() -> dict[str, Any]:
        movies = lib.library_movies(conn, libraries)
        shows = lib.library_shows(conn, libraries)
        by_ref: dict[str, lib.LibraryMovie | lib.LibraryShow] = {
            **{f"movie-{m.key}": m for m in movies},
            **{f"show-{s.key}": s for s in shows},
        }

        def cards_of(refs: list[str]) -> dict[str, dict[str, Any]]:
            cards = title_cards(conn, username, libraries, [by_ref[r] for r in refs], now)
            return dict(zip(refs, cards, strict=True))

        library = all_titles.load(conn, username, libraries, movies, shows, admin=admin)
        if only:
            return all_titles.one_of(conn, library, cards_of, only, type_=type_, sort=sort, now=now)
        return all_titles.page_of(
            conn,
            username,
            libraries,
            library,
            cards_of,
            type_=type_,
            sort=sort,
            limit=limit,
            now=now,
            after=after,
        )

    return respond_ok(all_titles.in_one_read(conn, read))


__all__ = ["router"]
