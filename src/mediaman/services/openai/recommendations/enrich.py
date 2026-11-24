"""TMDB + OMDb enrichment for recommendation dicts.

Takes a list of partially-populated recommendation dicts (from
prompts) and fills in ``year``, ``description``, ``rating``,
``poster_url``, and assorted detail fields by calling TMDB and OMDb.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import cast
from urllib.parse import quote as urlquote

from mediaman.services.media_meta.item_enrichment import apply_tmdb_detail, tmdb_backdrop
from mediaman.services.openai.recommendations._types import RecommendationItem
from mediaman.services.openai.recommendations.prompts import strip_season_suffix


def enrich_recommendations(
    recommendations: list[RecommendationItem],
    conn: sqlite3.Connection,
    secret_key: str,
) -> None:
    """Enrich recommendations in place with TMDB data and OMDb ratings, sharing one
    TmdbClient. *secret_key* decrypts the stored API credentials.

    Quirks kept from earlier versions: when TMDB finds a year, trailer_url is refreshed to
    "<title> <year> official trailer"; if TMDB has no rating but OMDb has an IMDb score, that
    score (1dp) is used. The description is kept whole, and the wide backdrop is stored with
    the pick.
    """
    from mediaman.services.media_meta.omdb import fetch_ratings
    from mediaman.services.media_meta.tmdb import TmdbClient

    client = TmdbClient.from_db(conn, secret_key)

    for s in recommendations:
        if client is not None:
            search_title = s["title"]
            if s["media_type"] == "tv":
                search_title = strip_season_suffix(search_title) or s["title"]
            best = client.search(
                search_title,
                year=s.get("year"),
                media_type=s["media_type"],
            )
            if best:
                card = TmdbClient.shape_card(best)
                s["tmdb_id"] = card["tmdb_id"]
                s["year"] = card["year"]
                s["description"] = card["description"]
                s["rating"] = card["rating"]
                if card["poster_url"]:
                    s["poster_url"] = card["poster_url"]
                if s.get("year"):
                    search_q = f"{s['title']} {s['year']} official trailer"
                    s["trailer_url"] = "https://www.youtube.com/results?search_query=" + urlquote(
                        search_q
                    )

            tmdb_id = s.get("tmdb_id")
            if tmdb_id:
                data = client.details(s["media_type"], tmdb_id)
                if data:
                    item = cast(dict[str, object], s)
                    apply_tmdb_detail(
                        item, TmdbClient.shape_detail(data, media_type=s["media_type"])
                    )
                    item["backdrop_url"] = tmdb_backdrop(data)

        title = s.get("title")
        if not title:
            continue
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
