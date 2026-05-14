"""Persistence logic — fetch watch history, generate, enrich, and save recommendations."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import requests
from plexapi.exceptions import PlexApiException

from mediaman.core.time import now_utc
from mediaman.services.openai.recommendations._types import RecommendationItem
from mediaman.services.openai.recommendations.enrich import enrich_recommendations
from mediaman.services.openai.recommendations.prompts import (
    generate_personal,
    generate_trending,
)

if TYPE_CHECKING:
    from mediaman.services.media_meta.plex import PlexClient, PlexRatedItem

logger = logging.getLogger(__name__)


def _fetch_watch_history(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Query the DB for the 50 most recently watched items, normalising media_type."""
    rows = conn.execute("""
        SELECT DISTINCT mi.title, mi.media_type, mi.last_watched_at
        FROM media_items mi
        WHERE mi.last_watched_at IS NOT NULL
        ORDER BY mi.last_watched_at DESC
        LIMIT 50
    """).fetchall()

    watch_history = []
    for r in rows:
        media_type = r["media_type"] or "movie"
        if media_type in ("tv_season", "anime_season", "season"):
            media_type = "tv"
        elif media_type != "anime":
            media_type = "movie"
        watch_history.append({"title": r["title"], "type": media_type})
    return watch_history


def _fetch_plex_ratings(plex_client: PlexClient | None) -> list[PlexRatedItem]:
    """Return user ratings from Plex, or an empty list if unavailable."""
    if plex_client is None:
        return []
    try:
        ratings = plex_client.get_user_ratings()
        logger.info("Fetched %d user ratings from Plex", len(ratings))
        return ratings
    except (PlexApiException, requests.RequestException):
        logger.warning("Failed to fetch Plex user ratings — proceeding without them", exc_info=True)
        return []


def _fetch_previous_titles(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Return distinct suggestion titles from the last 30 days (deduplication input)."""
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    prev_rows = conn.execute(
        "SELECT DISTINCT title FROM suggestions WHERE batch_id >= ?", (cutoff_30d,)
    ).fetchall()
    return [r["title"] for r in prev_rows]


def _insert_recommendations(
    conn: sqlite3.Connection,
    all_recommendations: list[RecommendationItem],
    now: datetime,
    today: str,
    now_iso: str,
    manual: bool,
) -> int:
    """Delete the appropriate batch and INSERT all recommendations inside one transaction.

    The DELETE + INSERT loop is wrapped in ``with conn:`` so SQLite rolls back
    to the pre-DELETE state on any exception (§9.7). The caller must not wrap
    this in a second ``with conn:`` block.
    """
    inserted = 0
    with conn:  # rolls back DELETE + any partial INSERTs on exception (§9.7)
        if manual:
            conn.execute("DELETE FROM suggestions WHERE batch_id = ?", (today,))
        else:
            cutoff_90d_str = (now - timedelta(days=90)).strftime("%Y-%m-%d")
            conn.execute("DELETE FROM suggestions WHERE batch_id < ?", (cutoff_90d_str,))

        for s in all_recommendations:
            title = str(s.get("title") or "")
            reason = str(s.get("reason") or "")
            conn.execute(
                "INSERT INTO suggestions (title, year, media_type, category, tmdb_id, imdb_id, "
                "description, reason, poster_url, trailer_url, rating, rt_rating, "
                "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore, "
                "batch_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    s.get("year"),
                    s["media_type"],
                    s["category"],
                    s.get("tmdb_id"),
                    s.get("imdb_id"),
                    s.get("description"),
                    reason,
                    s.get("poster_url"),
                    s.get("trailer_url"),
                    s.get("rating"),
                    s.get("rt_rating"),
                    s.get("tagline"),
                    s.get("runtime"),
                    s.get("genres"),
                    s.get("cast_json"),
                    s.get("director"),
                    s.get("trailer_key"),
                    s.get("imdb_rating"),
                    s.get("metascore"),
                    today,
                    now_iso,
                ),
            )
            inserted += 1
    return inserted


def refresh_recommendations(
    conn: sqlite3.Connection,
    plex_client: PlexClient | None,
    manual: bool = False,
    *,
    secret_key: str,
) -> int:
    """Fetch watch history, generate both trending and personal recommendations.

    Args:
        conn: DB connection.
        plex_client: Plex client for watch history and ratings.
        manual: When True (user-triggered refresh), replace only today's batch.
            When False (scheduled), keep historical batches and prune rows older
            than 90 days.
        secret_key: Encryption key for reading API credentials from DB settings.

    Returns the total number of recommendations generated.
    """
    now = now_utc()
    today = now.strftime("%Y-%m-%d")

    watch_history = _fetch_watch_history(conn)
    user_ratings = _fetch_plex_ratings(plex_client)
    previous_titles = _fetch_previous_titles(conn, now)

    trending = generate_trending(conn, previous_titles, secret_key=secret_key)
    personal = (
        generate_personal(
            conn,
            watch_history,
            user_ratings,
            previous_titles,
            secret_key=secret_key,
        )
        if watch_history
        else []
    )
    all_recommendations = trending + personal

    if not all_recommendations:
        return 0

    enrich_recommendations(all_recommendations, conn, secret_key)

    inserted = _insert_recommendations(
        conn, all_recommendations, now, today, now.isoformat(), manual
    )

    logger.info(
        "Generated %d recommendations (%d trending, %d personal); inserted=%d skipped=%d",
        len(all_recommendations),
        len(trending),
        len(personal),
        inserted,
        len(all_recommendations) - inserted,
    )
    return inserted
