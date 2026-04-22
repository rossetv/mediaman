"""Enrich re-download items with rich metadata for the cinematic layout.

These are pure functions — they mutate an ``item`` dict in place but have
no side effects beyond DB reads and outbound API calls.
"""

from __future__ import annotations


def _fetch_tmdb_for_item(item: dict, conn, secret_key: str) -> None:
    """Fetch TMDB search + details + OMDB ratings for a single item.

    Thin wrapper around :class:`services.tmdb.TmdbClient` — fills gaps on
    the item dict in place. On any client-level failure (missing token,
    network error, no results) the item is left unchanged.
    """
    from mediaman.services.omdb import fetch_ratings
    from mediaman.services.tmdb import TmdbClient

    # Preserve the 5s timeout this code path used before consolidation —
    # the guest download page is interactive and should stay snappy.
    client = TmdbClient.from_db(conn, secret_key, timeout=5.0)
    title = item["title"]
    media_type = item.get("media_type", "movie")

    if client is not None:
        # Search → populate tmdb_id / year / description / rating / poster
        tmdb_id = item.get("tmdb_id")
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
                detail = TmdbClient.shape_detail(data, media_type=media_type)
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
    ratings = fetch_ratings(title, item.get("year"), media_type, conn=conn, secret_key=secret_key)
    if "rt" in ratings:
        item["rt_rating"] = ratings["rt"]
    if "imdb" in ratings:
        item["imdb_rating"] = ratings["imdb"]
    if "metascore" in ratings:
        item["metascore"] = ratings["metascore"]


def _enrich_redownload_item(item: dict, conn, secret_key: str) -> None:
    """Enrich a re-download item with rich metadata for the cinematic layout.

    First checks the recommendations cache (fast, no API calls).
    Falls back to TMDB + OMDB APIs if no recommendation record exists.
    """
    title = item.get("title", "")
    media_type = item.get("media_type", "movie")

    # 1. Try the recommendations cache — item was likely a recommendation originally
    row = conn.execute(
        "SELECT poster_url, year, description, reason, rating, rt_rating, "
        "tagline, runtime, genres, cast_json, director, trailer_key, "
        "imdb_rating, metascore, tmdb_id "
        "FROM suggestions WHERE title = ? AND media_type = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (title, media_type),
    ).fetchone()

    if row and row["poster_url"]:
        item.update({
            "poster_url":  row["poster_url"],
            "year":        row["year"],
            "description": row["description"],
            "reason":      row["reason"],
            "rating":      row["rating"],
            "rt_rating":   row["rt_rating"],
            "tagline":     row["tagline"],
            "runtime":     row["runtime"],
            "genres":      row["genres"],
            "cast_json":   row["cast_json"],
            "director":    row["director"],
            "trailer_key": row["trailer_key"],
            "imdb_rating": row["imdb_rating"],
            "metascore":   row["metascore"],
        })
        if row["tmdb_id"] and not item.get("tmdb_id"):
            item["tmdb_id"] = row["tmdb_id"]
        return

    # 2. Fall back to TMDB + OMDB APIs
    _fetch_tmdb_for_item(item, conn, secret_key)
