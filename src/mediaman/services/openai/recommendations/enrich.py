"""TMDB + OMDb enrichment for recommendation dicts.

Takes a list of partially-populated recommendation dicts (from
:mod:`.prompts`) and fills in ``year``, ``description``, ``rating``,
``poster_url``, and assorted detail fields by calling TMDB and OMDb.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote as urlquote

from mediaman.services.media_meta.item_enrichment import apply_tmdb_detail
from mediaman.services.openai.recommendations.prompts import strip_season_suffix


def enrich_recommendations(
    recommendations: list[dict[str, Any]],
    conn: sqlite3.Connection,
    secret_key: str,
) -> None:
    """Enrich recommendations in place with TMDB data + OMDB ratings.

    Replaces the previous three-pass pipeline
    (``_fetch_tmdb_data`` → ``_fetch_tmdb_details`` → ``_fetch_omdb_ratings``)
    with a single loop that reuses the shared :class:`TmdbClient` and the
    canonical :func:`services.omdb.fetch_ratings` helper.

    ``secret_key`` is required for decrypting API credentials stored in the DB.

    Preserved quirks from the old implementation:

    * Description is truncated to 250 characters (suggestions table
      keeps rows compact).
    * When TMDB finds a release year we refresh ``trailer_url`` with
      ``"<title> <year> official trailer"`` so the YouTube search link
      matches the canonical year.
    * If TMDB yielded no ``rating`` but OMDb has an IMDb score, we fall
      back to that score (rounded to 1dp) as the card rating.
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
                s["description"] = card["description"][:250]
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
                    apply_tmdb_detail(s, TmdbClient.shape_detail(data, media_type=s["media_type"]))

        title = s.get("title")
        if not title:
            continue
        ratings = fetch_ratings(
            title, s.get("year"), s["media_type"], conn=conn, secret_key=secret_key
        )
        if "rt" in ratings:
            s["rt_rating"] = ratings["rt"]
        if "imdb" in ratings:
            s["imdb_rating"] = ratings["imdb"]
        if "metascore" in ratings:
            s["metascore"] = ratings["metascore"]
        if not s.get("rating") and "imdb" in ratings:
            try:
                s["rating"] = round(float(ratings["imdb"]), 1)
            except (TypeError, ValueError):
                pass
