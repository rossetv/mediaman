"""TMDB + OMDb enrichment for recommendation dicts.

Takes a list of partially-populated recommendation dicts (from
prompts) and fills in ``year``, ``description``, ``rating``,
``poster_url``, and assorted detail fields by calling TMDB and OMDb.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, cast
from urllib.parse import quote as urlquote

import requests

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.item_enrichment import apply_tmdb_detail, tmdb_backdrop
from mediaman.services.openai.recommendations._types import RecommendationItem
from mediaman.services.openai.recommendations.prompts import strip_season_suffix

if TYPE_CHECKING:
    from mediaman.services.media_meta.tmdb import TmdbClient

logger = logging.getLogger(__name__)


def _accept_all(_item: RecommendationItem) -> bool:
    return True


def enrich_recommendations(
    recommendations: list[RecommendationItem],
    conn: sqlite3.Connection,
    secret_key: str,
    *,
    accept: Callable[[RecommendationItem], bool] = _accept_all,
    limit: int | None = None,
) -> None:
    """Enrich recommendations in place with TMDB data and OMDb ratings, sharing one
    TmdbClient. *secret_key* decrypts the stored API credentials.

    A title TMDB answered it does not know is dropped, as is one *accept* refuses once its
    TMDB id and year are known, and any past *limit*; TMDB not answering drops nothing.

    Quirks kept from earlier versions: when TMDB finds a year, trailer_url is refreshed to
    "<title> <year> official trailer"; if TMDB has no rating but OMDb has an IMDb score, that
    score (1dp) is used. The description is kept whole, and the wide backdrop is stored with
    the pick.
    """
    from mediaman.services.media_meta.tmdb import TmdbClient

    client = TmdbClient.from_db(conn, secret_key)

    kept: list[RecommendationItem] = []
    for s in recommendations:
        if limit is not None and len(kept) >= limit:
            break
        if (client is None or _find_on_tmdb(client, s)) and accept(s):
            kept.append(s)
    recommendations[:] = kept
    for s in recommendations:
        _fill_details(client, s, conn, secret_key)


def _find_on_tmdb(client: TmdbClient, s: RecommendationItem) -> bool:
    """Give *s* its TMDB card; False when TMDB answered that it knows no such title."""
    search_title = s["title"]
    if not search_title:
        return True
    if s["media_type"] == "tv":
        search_title = strip_season_suffix(search_title) or s["title"]
    year = s.get("year")
    try:
        best = client.search_or_raise(search_title, year=year, media_type=s["media_type"])
        if best is None and year:
            best = client.search_or_raise(search_title, media_type=s["media_type"])
    except (SafeHTTPError, requests.RequestException, ValueError):
        logger.warning("recommendations: TMDB did not answer a title search", exc_info=True)
        return True
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
    client: TmdbClient | None, s: RecommendationItem, conn: sqlite3.Connection, secret_key: str
) -> None:
    from mediaman.services.media_meta.omdb import fetch_ratings

    tmdb_id = s.get("tmdb_id")
    if client is not None and tmdb_id:
        data = client.details(s["media_type"], tmdb_id)
        if data:
            apply_tmdb_detail(
                cast(dict[str, object], s),
                client.shape_detail(data, media_type=s["media_type"]),
            )
            s["backdrop_url"] = tmdb_backdrop(data)

    title = s.get("title")
    if not title:
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
