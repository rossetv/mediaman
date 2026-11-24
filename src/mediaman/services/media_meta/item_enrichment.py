"""Enrich re-download items with rich metadata for the cinematic layout.

These are pure functions — they mutate an ``item`` dict in place but have
no side effects beyond DB reads and outbound API calls.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mediaman.services.media_meta.tmdb import TmdbDetail


def apply_tmdb_detail(item: dict[str, object], detail: TmdbDetail) -> None:
    """Merge a shape_detail result into *item* in place, overwriting only where the detail
    has a non-falsy value.
    """
    item["tagline"] = detail["tagline"]
    item["runtime"] = detail["runtime"]
    if detail["genres"]:
        item["genres"] = detail["genres"]
    if detail["director"]:
        item["director"] = detail["director"]
    if detail["cast_json"]:
        item["cast_json"] = detail["cast_json"]
    if detail["trailer_key"]:
        item["trailer_key"] = detail["trailer_key"]


def enrich_item_with_tmdb(
    item: dict[str, object], conn: sqlite3.Connection, secret_key: str
) -> None:
    """Fetch TMDB search, details and OMDb ratings into *item* in place; the item is left
    unchanged on any client-level failure.
    """
    from mediaman.services.media_meta.omdb import fetch_ratings
    from mediaman.services.media_meta.tmdb import TmdbClient

    # Preserve the 5s timeout this code path used before consolidation —
    # the guest download page is interactive and should stay snappy.
    client = TmdbClient.from_db(conn, secret_key, timeout=5.0)
    title = cast(str, item["title"])
    media_type = cast(str, item.get("media_type", "movie"))

    if client is not None:
        # Search → populate tmdb_id / year / description / rating / poster
        tmdb_id = cast("int | None", item.get("tmdb_id"))
        if not tmdb_id:
            best = client.search(title, media_type=media_type)
            if best:
                card = TmdbClient.shape_card(best)
                tmdb_id = card["tmdb_id"]
                item["tmdb_id"] = tmdb_id
                if card["year"]:
                    item["year"] = card["year"]
                item["description"] = card["description"]
                if card["rating"]:
                    item["rating"] = card["rating"]
                if card["poster_url"]:
                    item["poster_url"] = card["poster_url"]

        # Details → tagline/runtime/genres/cast/director/trailer + fill gaps
        if tmdb_id:
            data = client.details(media_type, tmdb_id)
            if data:
                apply_tmdb_detail(item, TmdbClient.shape_detail(data, media_type=media_type))

                # Fill gaps from details payload (search may have returned
                # nothing, or the caller supplied only a tmdb_id)
                card = TmdbClient.shape_card(data)
                if not item.get("poster_url") and card["poster_url"]:
                    item["poster_url"] = card["poster_url"]
                if not item.get("year") and card["year"]:
                    item["year"] = card["year"]
                if not item.get("description"):
                    item["description"] = card["description"]
                if not item.get("rating") and card["rating"]:
                    item["rating"] = card["rating"]

    # OMDB ratings
    year = cast("int | None", item.get("year"))
    ratings = fetch_ratings(title, year, media_type, conn=conn, secret_key=secret_key)
    if "rt" in ratings:
        item["rt_rating"] = ratings["rt"]
    if "imdb" in ratings:
        item["imdb_rating"] = ratings["imdb"]
    if "metascore" in ratings:
        item["metascore"] = ratings["metascore"]


def enrich_redownload_item(
    item: dict[str, object], conn: sqlite3.Connection, secret_key: str
) -> None:
    """Enrich *item* from the recommendations cache first (no API calls), falling back to
    TMDB and OMDb.
    """
    media_type = item.get("media_type", "movie")
    tmdb_id = item.get("tmdb_id") or None

    # 1. Try the recommendations cache — item was likely a recommendation originally.
    # A title alone can be a namesake's, so it is matched only when there is no id.
    row = conn.execute(
        "SELECT poster_url, year, description, reason, rating, rt_rating, "
        "tagline, runtime, genres, cast_json, director, trailer_key, "
        "imdb_rating, metascore, tmdb_id "
        "FROM suggestions WHERE media_type = ? AND (tmdb_id = ? OR (? IS NULL AND title = ?)) "
        "ORDER BY created_at DESC LIMIT 1",
        (media_type, tmdb_id, tmdb_id, item.get("title", "")),
    ).fetchone()

    if row and row["poster_url"]:
        item.update(
            {
                "poster_url": row["poster_url"],
                "year": row["year"],
                "description": row["description"],
                "reason": row["reason"],
                "rating": row["rating"],
                "rt_rating": row["rt_rating"],
                "tagline": row["tagline"],
                "runtime": row["runtime"],
                "genres": row["genres"],
                "cast_json": row["cast_json"],
                "director": row["director"],
                "trailer_key": row["trailer_key"],
                "imdb_rating": row["imdb_rating"],
                "metascore": row["metascore"],
            }
        )
        if row["tmdb_id"] and not tmdb_id:
            item["tmdb_id"] = row["tmdb_id"]
        return

    # 2. Fall back to TMDB + OMDB APIs
    enrich_item_with_tmdb(item, conn, secret_key)
