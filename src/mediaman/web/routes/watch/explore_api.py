"""Watch JSON for exploring TMDB: titles by genre, and a person with everything they
worked on, for admins and viewers alike."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import watch_library as lib
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.catalog_api import _arr, _discover_cards, _tmdb_client, held_fields

router = APIRouter()

_PERSON_ID_RE = re.compile(r"[0-9]{1,10}", re.ASCII)
_PERSON_PAGE = 60
_WHERE = ("anywhere", "library")

# Apart from the query limit, so looking around does not use up the day's searches. A list
# of films and TV together asks TMDB twice.
_EXPLORE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60, max_per_day=1000)
# Any person's id can be asked for, and a person's record is the largest thing cached: a day's
# must leave room for the rest of the cache.
_PERSON_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60, max_per_day=400)

_Page = Query(1, ge=1, le=catalog.MAX_PAGE)


@router.get("/api/watch/explore")
def api_watch_explore(
    request: Request,
    genre: str | None = None,
    type: str = "all",
    sort: str = "popular",
    decade: str | None = None,
    where: str = "anywhere",
    page: int = _Page,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """One page of a genre's titles, each card as on a discover row; without *genre*, the
    genres and decades there are, and the people the library has most of. *where* "library"
    lists only the titles it holds."""
    chosen = catalog.GENRES.get(genre) if genre is not None else None
    if genre is not None and chosen is None:
        return respond_err("not_found", status=404)
    if (
        type not in catalog.TYPES
        or sort not in catalog.SORTS
        or (decade is not None and decade not in catalog.decades())
        or where not in _WHERE
    ):
        return respond_err("invalid_request", status=400)
    conn = get_db()
    client = _tmdb_client(request, conn)
    if chosen is None:
        return _genres(conn, client)
    out: dict[str, object] = {
        "tmdb_configured": client is not None,
        "genre": {"id": chosen.id, "name": chosen.name, "types": chosen.types},
        "type": type,
        "sort": sort,
        "decade": decade,
        "where": where,
        "library": {"ready": False, "known": 0, "total": 0, "titles": 0},
        "items": [],
        "total": 0,
        "next_page": None,
    }
    if client is None:
        return respond_ok(out)
    held, covered = catalog.library_titles(conn, chosen, media_type=type, sort=sort, decade=decade)
    out["library"] = {**_coverage(covered), "titles": len(held)}
    if where == "library":
        end = page * catalog.LIBRARY_PAGE
        out["items"] = _held_cards(request, conn, username, held[end - catalog.LIBRARY_PAGE : end])
        out["total"] = len(held)
        out["next_page"] = page + 1 if end < len(held) and page < catalog.MAX_PAGE else None
        return respond_ok(out)
    try:
        # Only a page TMDB is asked for counts against the limit; a cached one is free.
        found, out["total"], out["next_page"] = catalog.explore_page(
            client,
            chosen,
            media_type=type,
            sort=sort,
            decade=decade,
            page=page,
            allow_fetch=lambda: _EXPLORE_LIMITER.check(username),
        )
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    [out["items"]] = _discover_cards(request, conn, client, username, [found])
    return respond_ok(out)


def _coverage(covered: catalog.Coverage) -> dict[str, object]:
    return {"ready": covered.ready, "known": covered.known, "total": covered.total}


def _held_cards(
    request: Request, conn: sqlite3.Connection, username: str, items: Sequence[Mapping[str, Any]]
) -> list[dict[str, object]]:
    """The cards of titles known to be held, and by which library title."""
    arr = _arr(request, conn)
    fields = held_fields(conn, username, lib.scanned_library_ids(conn))
    none_held = catalog.LibraryIndex()
    cards = []
    for item in items:
        card = catalog.tmdb_card(item, none_held, arr)
        if card is not None:
            ref = item["library_ref"]
            held = {"in_library": True, "library_ref": ref, "href": catalog.library_href(ref)}
            cards.append({**card, **held, **fields(ref)})
    return cards


def _genres(conn: sqlite3.Connection, client: TmdbClient | None) -> JSONResponse:
    posters = catalog.genre_posters(client) if client is not None else {}
    held = (
        catalog.library_people(conn)
        if client is not None
        else catalog.LibraryPeople(catalog.Coverage(0, 0))
    )
    return respond_ok(
        {
            "tmdb_configured": client is not None,
            "genres": [
                {
                    "id": g.id,
                    "name": g.name,
                    "aliases": list(g.aliases),
                    "types": g.types,
                    "poster_urls": posters.get(g.id, []),
                    "actors": held.actors.get(g.id, []),
                }
                for g in catalog.GENRES.values()
            ],
            "decades": catalog.decades(),
            "library": _coverage(held.coverage),
            "people": held.people,
        }
    )


@router.get("/api/watch/person/{person_id}")
def api_watch_person(
    person_id: str,
    request: Request,
    page: int = _Page,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """A person, what they are known for, and one page of all their films and shows. The
    first page has all the library holds before its own, so a later one may repeat a title."""
    if not _PERSON_ID_RE.fullmatch(person_id) or int(person_id) == 0:
        return respond_err("not_found", status=404)
    conn = get_db()
    client = _tmdb_client(request, conn)
    if client is None:
        return respond_ok(
            {
                "tmdb_configured": False,
                "person": None,
                "known_for": [],
                "items": [],
                "total": 0,
                "in_library": 0,
                "next_page": None,
            }
        )
    try:
        found = catalog.person(
            client, int(person_id), allow_fetch=lambda: _PERSON_LIMITER.check(username)
        )
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.PersonNotFound:
        return respond_err("not_found", status=404)
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    cards = _credit_cards(request, conn, client, username, found["credits"])
    known_for = [card for card, minor in cards if not minor] or [card for card, _ in cards]
    ordered = [card for card, _ in cards]
    held = [card for card in ordered if card["in_library"]]
    # Cut from the order of the credits: one that followed the library would lose a title
    # that came or went between two pages.
    end = page * _PERSON_PAGE
    items = ordered[end - _PERSON_PAGE : end]
    if page == 1:
        items = held + [card for card in items if not card["in_library"]]
    return respond_ok(
        {
            "tmdb_configured": True,
            "person": {
                "person_id": int(person_id),
                **{k: v for k, v in found.items() if k != "credits"},
                "age": catalog.age(found["birthday"], found["deathday"]),
            },
            "known_for": known_for[: catalog.KNOWN_FOR_LIMIT],
            "items": items,
            "total": len(ordered),
            "in_library": len(held),
            "next_page": page + 1 if end < len(ordered) else None,
        }
    )


def _credit_cards(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    username: str,
    credits: Sequence[Mapping[str, Any]],
) -> list[tuple[dict[str, object], bool]]:
    """(card, whether they only appear in it) of each credit, in order."""
    [cards] = _discover_cards(request, conn, client, username, [credits])
    by_ref = {f"tmdb-{c['media_type']}-{c['id']}": c for c in credits}
    return [
        ({**card, "role": by_ref[str(card["ref"])]["role"]}, by_ref[str(card["ref"])]["minor"])
        for card in cards
    ]


__all__ = ["router"]
