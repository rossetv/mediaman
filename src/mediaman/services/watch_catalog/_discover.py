"""TMDB lists, search and details for Watch, through the disk cache.

Only TMDB's own answers are cached; who is asking, what they watched, kept or asked to
download never is. A failed fetch raises ``TmdbUnavailable`` and is not cached. The
fetches capture only the TMDB client, never a database connection, because the cache may
run them on a worker thread.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import requests

from mediaman.services.infra import SafeHTTPError, disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient, year_of

from ._arr import ArrView
from ._dedupe import LibraryIndex
from ._gate import TmdbBusy, TmdbDown, TmdbUnavailable, gate
from ._shape import discover_card, tmdb_id_of, tmdb_image_url, tmdb_kind, tmdb_rating
from ._title import tmdb_fields

logger = logging.getLogger(__name__)

# TMDB answers no page of a list past this one.
MAX_PAGE = 500

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

# A failure someone waited for is not held longer: they may try again.
_HOLD_S = 5.0

MAX_QUERY_LEN = 100

# (row id, row title, TmdbClient method, media_type to stamp: the popular lists omit it)
SHELVES: tuple[tuple[str, str, str, str | None], ...] = (
    ("trending", "Trending this week", "trending", None),
    ("popular_movies", "Popular movies", "popular_movies", "movie"),
    ("popular_tv", "Popular TV", "popular_tv", "tv"),
)

# The TMDB list behind each row, by row id, and the media_type to stamp on its items.
_LISTS: dict[str, tuple[str, str | None]] = {
    "trending": ("/trending/all/week", None),
    "popular_movies": ("/movie/popular", "movie"),
    "popular_tv": ("/tv/popular", "tv"),
}

# What ``tmdb_card`` reads of a list item.
_CARD_FIELDS = (
    "id",
    "media_type",
    "title",
    "name",
    "release_date",
    "first_air_date",
    "poster_path",
    "vote_average",
)


class TitleNotFound(TmdbUnavailable):
    """TMDB has no such title, or marks it adult."""


def _failure(exc: Exception, what: str) -> TmdbUnavailable:
    if isinstance(exc, SafeHTTPError):
        if exc.status_code == 429:
            return TmdbBusy(what)
        # 0 is no answer at all.
        if exc.status_code == 0 or exc.status_code >= 500:
            return TmdbDown(what)
    elif not isinstance(exc, ValueError):
        return TmdbDown(what)
    return TmdbUnavailable(what)


def _is_gone(exc: Exception) -> bool:
    return isinstance(exc, SafeHTTPError) and exc.status_code == 404


def _cache() -> disk_cache.Store[Any]:
    return disk_cache.meta()


def _cached(
    client: TmdbClient,
    key: str,
    fresh_s: float,
    stale_s: float,
    fetch: Callable[[], Any],
    allow_fetch: Callable[[], bool] | None = None,
    brief: bool = False,
    background: bool = False,
) -> Any:
    """As the disk cache's ``get_or_fetch``, for a request: TMDB is asked, or waited for,
    only as ``gate`` lets it, which *allow_fetch* comes after. *fetch* must raise TmdbDown
    or TmdbBusy when TMDB gives no answer. A caller in the *background* does not pass the
    gate."""
    waits = False
    with contextlib.ExitStack() as held:

        def may_ask() -> bool:
            nonlocal waits
            waits = True
            if not background:
                held.enter_context(gate.asking(client.token, probe=not brief))
            return allow_fetch is None or allow_fetch()

        def noted() -> Any:
            if background:
                return _shaped(fetch)()
            if not waits:  # refreshed for nobody who waits
                gate.check(client.token)
            return _noted(client, fetch)

        try:
            return _cache().get_or_fetch(
                key, fresh_s, stale_s, noted, allow_fetch=may_ask, brief=brief, hold_s=_HOLD_S
            )
        except TimeoutError as exc:
            raise TmdbUnavailable("still being asked") from exc


def _noted(client: TmdbClient, fetch: Callable[[], Any]) -> Any:
    try:
        value = _shaped(fetch)()
    except TmdbUnavailable as exc:
        gate.note(client.token, exc)
        raise
    gate.note(client.token, None)
    return value


def _shaped(fetch: Callable[[], Any]) -> Callable[[], Any]:
    """*fetch*, whose answer TMDB shaped in a way that cannot be read is a failure too."""

    def shaped() -> Any:
        try:
            return fetch()
        except (TypeError, ValueError, AttributeError, LookupError, ArithmeticError) as exc:
            logger.warning("watch: an answer of TMDB could not be read", exc_info=True)
            raise TmdbUnavailable("malformed") from exc

    return shaped


def _shelf_fetch(
    client: TmdbClient, method: str, media_type: str | None
) -> Callable[[], list[dict[str, object]]]:
    def fetch() -> list[dict[str, object]]:
        items: list[dict[str, object]] = getattr(client, method)(1)
        # The client answers [] on a failure; a real list is never empty.
        if not items:
            raise TmdbDown(method)
        return [{**i, "media_type": media_type} if media_type else i for i in items]

    return fetch


def discover_shelves(client: TmdbClient) -> list[tuple[str, str, list[dict[str, object]]]]:
    """(row id, title, TMDB items) for each discover row TMDB answered; raises
    TmdbUnavailable only when none did."""
    shelves = []
    for shelf_id, title, method, media_type in SHELVES:
        try:
            items = _cached(
                client,
                f"tmdb:discover:{shelf_id}:1",
                6 * _HOUR,
                7 * _DAY,
                _shelf_fetch(client, method, media_type),
            )
        except TmdbUnavailable:
            continue
        shelves.append((shelf_id, title, [i for i in items if not i.get("adult")]))
    if not shelves:
        raise TmdbUnavailable("discover")
    return shelves


def discover_page(
    client: TmdbClient, row_id: str, page: int, *, allow_fetch: Callable[[], bool] | None = None
) -> tuple[list[dict[str, object]], int | None]:
    """The TMDB items of a later page (2 to ``MAX_PAGE``) of a discover row, and the page
    after it or None at the end of the list; KeyError for an unknown row. Only the fields
    a card shows are cached, so hundreds of pages stay small. Raises TmdbUnavailable, and
    TmdbBusy when TMDB answered 429. *allow_fetch* is asked as in ``search``."""
    path, media_type = _LISTS[row_id]

    def fetch() -> dict[str, Any]:
        try:
            # The client's list methods answer [] on a failure too; asked directly so a
            # failure is not taken for the end of the list.
            payload = client._get(path, {"page": page})
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            raise _failure(exc, row_id) from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise TmdbUnavailable(row_id)
        items = [
            {f: r[f] for f in _CARD_FIELDS if f in r}
            for r in results
            if isinstance(r, dict) and not r.get("adult")
        ]
        if media_type:
            items = [{**i, "media_type": media_type} for i in items]
        total = payload.get("total_pages")
        return {
            "items": items,
            "pages": total if isinstance(total, int) else MAX_PAGE,
            "empty": not results,
        }

    # "v2": entries before it hold adult titles.
    found: dict[str, Any] = _cached(
        client, f"tmdb:discover-page:v2:{row_id}:{page}", 6 * _HOUR, 7 * _DAY, fetch, allow_fetch
    )
    more = not found["empty"] and page < min(found["pages"], MAX_PAGE)
    return found["items"], page + 1 if more else None


def search(
    client: TmdbClient, query: str, *, allow_fetch: Callable[[], bool] | None = None
) -> list[dict[str, object]]:
    """The first page of TMDB's multi search for *query*, adult titles and people left out. *allow_fetch* is asked before
    TMDB is called for an answer not cached; when it says no this raises
    ``disk_cache.Refused``."""
    q = query[:MAX_QUERY_LEN]

    def fetch() -> list[dict[str, object]]:
        try:
            # The client's paged search answers [] on a failure too; asked directly so a
            # failure is not cached, or shown, as "no results".
            payload = client._get(
                "/search/multi", {"query": q, "include_adult": "false", "page": 1}
            )
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            raise _failure(exc, "search") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise TmdbUnavailable("search")
        return [r for r in results if isinstance(r, dict)]

    result: list[dict[str, object]] = _cached(
        client, f"tmdb:search:{q.casefold()}:1", 15 * _MINUTE, _HOUR, fetch, allow_fetch
    )
    return [r for r in result if not r.get("adult")]


def _details_key(media_type: str, tmdb_id: int) -> str:
    # "v5": entries before it lack the director's id, the date and the popularity.
    return f"tmdb:details:v5:{media_type}:{tmdb_id}"


def held_details(kind: str, tmdb_id: int) -> dict[str, Any] | None:
    """What ``details`` answered for a title in the last 90 days, or None. TMDB is not asked."""

    def fetch() -> dict[str, Any]:
        raise TmdbUnavailable("not held")

    key = _details_key("movie" if kind == "movie" else "tv", tmdb_id)
    try:
        found: dict[str, Any] = _cache().get_or_fetch(
            key, 90 * _DAY, 90 * _DAY, fetch, allow_fetch=lambda: False
        )
    except (TmdbUnavailable, disk_cache.Refused):
        return None
    return None if found.get("adult") or found.get("gone") else found


def details(
    client: TmdbClient,
    kind: str,
    tmdb_id: int,
    *,
    allow_fetch: Callable[[], bool] | None = None,
    brief: bool = False,
    fresh: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """The ``tmdb_fields`` of a movie or show ("movie" or "tv"). Only these are cached:
    TMDB's own payload, with its full crew and every country's release dates, is many
    times larger. Raises TitleNotFound, TmdbUnavailable and TmdbBusy; *allow_fetch* is
    asked as in ``search``, and *brief* says that *client* gives up early, so that its
    failure is not held against the next caller. *fresh* takes nothing TMDB answered more
    than a week ago, and *background* is as in ``_cached``."""
    media_type = "movie" if kind == "movie" else "tv"

    def fetch() -> dict[str, Any]:
        try:
            found = client.details_or_raise(media_type, tmdb_id)
        except (SafeHTTPError, requests.RequestException, ValueError) as exc:
            if _is_gone(exc):
                return {"gone": True, "at": time.time()}
            raise _failure(exc, "details") from exc
        # Stored, so that a title held before TMDB marked or dropped it is not served from
        # then on.
        if found.get("adult"):
            return {"adult": True}
        return tmdb_fields(found, media_type)

    key = _details_key(media_type, tmdb_id)
    result: dict[str, Any] = _cached(
        client,
        key,
        7 * _DAY,
        7 * _DAY if fresh else 90 * _DAY,
        fetch,
        allow_fetch,
        brief,
        background,
    )
    # A title TMDB lacks may be added to it, so that is held for a day and no longer.
    if result.get("gone") and time.time() - result.get("at", 0) >= _DAY:
        with contextlib.suppress(TmdbUnavailable):
            result = _cached(client, key, 0, 0, fetch, allow_fetch, brief, background)
    if result.get("adult") or result.get("gone"):
        raise TitleNotFound("details")
    return result


def tmdb_card(
    item: Mapping[str, Any], index: LibraryIndex, arr: ArrView
) -> dict[str, object] | None:
    """The card of one TMDB list or search item; None for a person or a malformed item."""
    kind = tmdb_kind(item)
    tmdb_id = tmdb_id_of(item)
    if kind is None or tmdb_id is None:
        return None
    title = str(item.get("title") or item.get("name") or "")
    year = year_of(item.get("release_date") or item.get("first_air_date"))
    state, pct = arr.state(kind, tmdb_id)
    return discover_card(
        ref=f"tmdb-{kind}-{tmdb_id}",
        kind="movie" if kind == "movie" else "show",
        title=title,
        year=year,
        poster_url=tmdb_image_url(item.get("poster_path"), "w342"),
        rating=tmdb_rating(item.get("vote_average")),
        library_ref=index.library_ref(kind, tmdb_id, title, arr, year=year),
        download_state=state,
        download_pct=pct,
    )
