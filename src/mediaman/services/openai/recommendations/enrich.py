"""TMDB + OMDb enrichment for recommendation dicts.

Takes a list of partially-populated recommendation dicts (from
prompts) and fills in ``year``, ``description``, ``rating``,
``poster_url``, and assorted detail fields by calling TMDB and OMDb.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, cast
from urllib.parse import quote as urlquote

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.item_enrichment import apply_tmdb_detail, tmdb_backdrop
from mediaman.services.openai.recommendations._types import RecommendationItem
from mediaman.services.openai.recommendations.prompts import strip_season_suffix
from mediaman.services.watch_catalog import normalise_title

if TYPE_CHECKING:
    from mediaman.services.media_meta._tmdb_shapes import TmdbSearchResult
    from mediaman.services.media_meta.tmdb import TmdbClient

logger = logging.getLogger(__name__)


#: Seconds looking up one batch may take.
ENRICH_SECONDS = 60.0
# After this many calls TMDB did not answer, it is not asked again for the batch.
_TMDB_FAILURES_MAX = 2

# Search results looked through for a pick's title.
_RESULTS_READ = 5

# Where a subtitle can begin. A hyphen needs its spaces: "Spider" is no "Spider-Man".
_SUBTITLE_RE = re.compile(r"\s*[:：–—(]|\s+-\s|\s+or\b", re.IGNORECASE)
_COMBINING_RE = re.compile(r"[\u0300-\u036f]")


class _Budget:
    """What is left of one batch's time, and of the failures TMDB is allowed."""

    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._ends_at = clock() + seconds
        self._failures = 0

    def has_time(self) -> bool:
        return self._clock() < self._ends_at

    def may_ask_tmdb(self) -> bool:
        return self._failures < _TMDB_FAILURES_MAX and self.has_time()

    def tmdb_failed(self) -> None:
        self._failures += 1


def _accept_all(_item: RecommendationItem) -> bool:
    return True


def enrich_recommendations(
    recommendations: list[RecommendationItem],
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    accept: Callable[[RecommendationItem], bool] = _accept_all,
    limit: int | None = None,
    seconds: float = ENRICH_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Enrich recommendations in place with TMDB data and OMDb ratings, sharing one
    TmdbClient. *secret_key* decrypts the stored API credentials.

    A title TMDB answered it does not know is dropped, as is one *accept* refuses once its
    TMDB id and year are known, and any past *limit*; TMDB not answering drops nothing.
    After *seconds*, or two calls TMDB did not answer, what is left stays as it is.

    Quirks kept from earlier versions: when TMDB finds a year, trailer_url is refreshed to
    "<title> <year> official trailer"; if TMDB has no rating but OMDb has an IMDb score, that
    score (1dp) is used. The description is kept whole, and the wide backdrop is stored with
    the pick.
    """
    from mediaman.services.media_meta.tmdb import TmdbClient

    client = TmdbClient.from_db(conn, secret_key)
    budget = _Budget(seconds, clock)

    kept: list[RecommendationItem] = []
    for s in recommendations:
        if limit is not None and len(kept) >= limit:
            break
        if (client is None or _find_on_tmdb(client, s, budget)) and accept(s):
            kept.append(s)
    recommendations[:] = kept
    for s in recommendations:
        _fill_details(client, s, conn, secret_key, budget)


def _spelling(title: object) -> str:
    """*title* with what two spellings of one title differ in removed: case, accents,
    punctuation, spaces, "&" for "and" and a year in brackets at its end."""
    text = re.sub(r"\s*\(\d{4}\)\s*$", "", title) if isinstance(title, str) else ""
    text = unicodedata.normalize("NFKD", text.replace("&", " and "))
    squeezed = normalise_title(_COMBINING_RE.sub("", text)).replace(" ", "")
    return squeezed or text.strip()


def _main_titles(title: str) -> set[str]:
    """The spellings *title* has when it is cut where a subtitle can begin."""
    return {_spelling(title[: m.start()]) for m in _SUBTITLE_RE.finditer(title) if m.start()}


def _pick_result(
    results: Sequence[TmdbSearchResult], title: str, year: int | None
) -> TmdbSearchResult | None:
    """The first of *results* that is *title*, or else that is it but for a subtitle one of
    the two leaves out, which only a title of the same *year* may: "Alien" is no "Alien:
    Romulus"."""
    wanted = _spelling(title)
    but_for_subtitle = None
    for found in results[:_RESULTS_READ]:
        names = [
            name
            for k in ("title", "name", "original_title", "original_name")
            if isinstance(name := found.get(k), str) and name
        ]
        if any(_spelling(name) == wanted for name in names):
            return found
        released = str(found.get("release_date") or found.get("first_air_date") or "")
        if (
            but_for_subtitle is None
            and year
            and released[:4] == str(year)
            and any(
                wanted in _main_titles(name) or _spelling(name) in _main_titles(title)
                for name in names
            )
        ):
            but_for_subtitle = found
    return but_for_subtitle


def _find_on_tmdb(client: TmdbClient, s: RecommendationItem, budget: _Budget) -> bool:
    """Give *s* its TMDB card; False when TMDB answered that it knows no title of that name."""
    search_title = s["title"]
    if not search_title:
        return True
    if s["media_type"] == "tv":
        search_title = strip_season_suffix(search_title) or s["title"]
    best = None
    # The year first, then without it: a model's year can be wrong.
    for year in dict.fromkeys((s.get("year"), None)):
        if not budget.may_ask_tmdb():
            return True
        try:
            results = client.search_results_or_raise(
                search_title, year=year, media_type=s["media_type"]
            )
        except (SafeHTTPError, requests.RequestException, ValueError):
            logger.warning("recommendations: TMDB did not answer a title search", exc_info=True)
            budget.tmdb_failed()
            return True
        best = _pick_result(results, search_title, s.get("year"))
        if best is not None:
            break
    if best is None:
        return False
    card = client.shape_card(best)
    s["tmdb_id"] = card["tmdb_id"]
    s["year"] = card["year"]
    s["description"] = card["description"]
    s["rating"] = card["rating"]
    if card["poster_url"]:
        s["poster_url"] = card["poster_url"]
    if s.get("year"):
        search_q = f"{s['title']} {s['year']} official trailer"
        s["trailer_url"] = "https://www.youtube.com/results?search_query=" + urlquote(search_q)
    return True


def _fill_details(
    client: TmdbClient | None,
    s: RecommendationItem,
    conn: sqlite3.Connection,
    secret_key: str,
    budget: _Budget,
) -> None:
    from mediaman.services.media_meta.omdb import fetch_ratings

    tmdb_id = s.get("tmdb_id")
    if client is not None and tmdb_id and budget.may_ask_tmdb():
        data = client.details(s["media_type"], tmdb_id)
        if data is None:
            budget.tmdb_failed()
        if data:
            apply_tmdb_detail(
                cast(dict[str, object], s),
                client.shape_detail(data, media_type=s["media_type"]),
            )
            s["backdrop_url"] = tmdb_backdrop(data)

    title = s.get("title")
    if not title or not budget.has_time():
        return
    omdb_title = strip_season_suffix(title) or title if s["media_type"] == "tv" else title
    ratings = fetch_ratings(
        omdb_title, s.get("year"), s["media_type"], conn=conn, secret_key=secret_key
    )
    if "rt" in ratings:
        s["rt_rating"] = ratings["rt"]
    if "imdb" in ratings:
        s["imdb_rating"] = ratings["imdb"]
    if "metascore" in ratings:
        s["metascore"] = ratings["metascore"]
    if s.get("rating") is None and "imdb" in ratings:
        with contextlib.suppress(TypeError, ValueError):
            s["rating"] = round(float(ratings["imdb"]), 1)
