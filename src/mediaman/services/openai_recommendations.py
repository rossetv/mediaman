"""Generate media recommendations via OpenAI — trending and personalised."""
from __future__ import annotations


import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import quote as urlquote

import requests

if TYPE_CHECKING:
    from mediaman.services.plex import PlexClient

logger = logging.getLogger("mediaman")

# TV suggestions sometimes arrive with a season suffix (e.g. "The Boys:
# Season 5"). TMDB's /search/tv endpoint indexes the series title only,
# so the full string returns no match and the row ends up with no
# poster / description. We keep the display title as-is but search with
# the series title stripped of the trailing season marker.
_SEASON_SUFFIX_RE = re.compile(
    r"\s*[:\-–—]?\s*(?:season\s+\d+|s\d+)\s*$",
    re.IGNORECASE,
)


def _strip_season_suffix(title: str) -> str:
    """Return *title* with a trailing "Season N" / "SN" marker removed."""
    return _SEASON_SUFFIX_RE.sub("", title).strip()

# Default OpenAI model for the /v1/responses API.  Configurable via the
# ``openai_model`` setting in the DB; gpt-4.1 is a safe, long-lived choice
# for a project that may sit idle for a while between updates.
_DEFAULT_MODEL = "gpt-4.1"


def _get_openai_model(conn: sqlite3.Connection) -> str:
    """Return the OpenAI model to use, honouring the ``openai_model`` setting."""
    from mediaman.services.settings_reader import get_string_setting

    return get_string_setting(conn, "openai_model", default=_DEFAULT_MODEL) or _DEFAULT_MODEL

_RESPONSE_FORMAT = """Return ONLY a JSON array. Each object must have exactly these 3 fields:
- "title" (string): official English title
- "media_type" (string): "movie" or "tv"
- "reason" (string): one-sentence reason for the recommendation, max 120 characters

Example:
[
  {"title": "Inception", "media_type": "movie", "reason": "Mind-bending sci-fi thriller perfect for fans of complex narratives."},
  {"title": "Severance", "media_type": "tv", "reason": "Gripping workplace thriller with sci-fi elements and dark humour."}
]

JSON array only, no markdown, no explanation."""


def _get_openai_key(conn: sqlite3.Connection) -> str | None:
    """Read the OpenAI API key from settings, falling back to env var."""
    from mediaman.config import load_config
    from mediaman.services.settings_reader import get_string_setting

    val = get_string_setting(conn, "openai_api_key", secret_key=load_config().secret_key)
    return val or os.environ.get("OPENAI_API_KEY")


def _call_openai(prompt: str, conn: sqlite3.Connection | None, use_web_search: bool = True) -> list[dict]:
    """Send a prompt to OpenAI Responses API and parse the JSON array response.

    Always uses the Responses API (``/v1/responses``). When
    ``use_web_search`` is True (default), includes the ``web_search``
    tool so GPT can look up real-time data.
    """
    api_key = _get_openai_key(conn) if conn else os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("Recommendations skipped — OpenAI API key not configured")
        return []

    model = _get_openai_model(conn) if conn else _DEFAULT_MODEL
    try:
        body = {
            "model": model,
            "instructions": "You are a media recommendation engine. ALWAYS search the web to find current, real, accurate information. Do not rely on training data alone. Return only valid JSON.",
            "input": prompt,
        }
        if use_web_search:
            body["tools"] = [{"type": "web_search_preview"}]

        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()

        content = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content = part.get("text", "")
                        break

        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1]
            content = content.rsplit("```", 1)[0]

        items = json.loads(content)
        return items if isinstance(items, list) else []

    except Exception:
        logger.exception("OpenAI API call failed")
        return []


def _parse_recommendations(items: list[dict], category: str) -> list[dict]:
    """Normalise and validate raw GPT recommendations."""
    results = []
    for item in items:
        if not item.get("title"):
            continue

        title = str(item["title"])
        year = item.get("year")
        # YouTube search link — reliable, unlike hallucinated video IDs
        search_q = f"{title} {year} official trailer" if year else f"{title} official trailer"
        trailer_url = "https://www.youtube.com/results?search_query=" + urlquote(search_q)

        results.append({
            "title": title,
            "year": None,  # Populated from TMDB
            "media_type": "movie" if item.get("media_type") == "movie" else "tv",
            "category": category,
            "tmdb_id": None,  # Populated from TMDB
            "imdb_id": None,
            "description": None,  # Populated from TMDB
            "reason": str(item.get("reason", ""))[:200],
            "trailer_url": trailer_url,
            "poster_url": None,  # Populated from TMDB
        })
    return results


def _enrich_recommendations(recommendations: list[dict], conn: sqlite3.Connection) -> None:
    """Enrich recommendations in place with TMDB data + OMDB ratings.

    Replaces the previous three-pass pipeline
    (``_fetch_tmdb_data`` → ``_fetch_tmdb_details`` → ``_fetch_omdb_ratings``)
    with a single loop that reuses the shared :class:`TmdbClient` and the
    canonical :func:`services.omdb.fetch_ratings` helper.

    Preserved quirks from the old implementation:

    * Description is truncated to 250 characters (suggestions table
      keeps rows compact).
    * When TMDB finds a release year we refresh ``trailer_url`` with
      ``"<title> <year> official trailer"`` so the YouTube search link
      matches the canonical year.
    * If TMDB yielded no ``rating`` but OMDb has an IMDb score, we fall
      back to that score (rounded to 1dp) as the card rating.
    """
    from mediaman.config import load_config
    from mediaman.services.omdb import fetch_ratings
    from mediaman.services.tmdb import TmdbClient

    secret_key = load_config().secret_key
    client = TmdbClient.from_db(conn, secret_key)

    for s in recommendations:
        # --- TMDB search + details -------------------------------------
        if client is not None:
            search_title = s["title"]
            if s["media_type"] == "tv":
                search_title = _strip_season_suffix(search_title) or s["title"]
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
                    s["trailer_url"] = (
                        "https://www.youtube.com/results?search_query="
                        + urlquote(search_q)
                    )

            tmdb_id = s.get("tmdb_id")
            if tmdb_id:
                data = client.details(s["media_type"], tmdb_id)
                if data:
                    detail = TmdbClient.shape_detail(
                        data, media_type=s["media_type"]
                    )
                    s["tagline"] = detail["tagline"]
                    s["runtime"] = detail["runtime"]
                    s["genres"] = detail["genres"]
                    if detail["director"]:
                        s["director"] = detail["director"]
                    if detail["cast_json"]:
                        s["cast_json"] = detail["cast_json"]
                    if detail["trailer_key"]:
                        s["trailer_key"] = detail["trailer_key"]

        # --- OMDb ratings ---------------------------------------------
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
        # Fill TMDB rating from IMDb score if TMDB lookup yielded nothing.
        if not s.get("rating") and "imdb" in ratings:
            try:
                s["rating"] = round(float(ratings["imdb"]), 1)
            except (TypeError, ValueError):
                pass


def _generate_trending(conn: sqlite3.Connection, previous_titles: list[str] | None = None) -> list[dict]:
    """Generate trending media recommendations using web search.

    Args:
        previous_titles: Titles recommended in the last 30 days — GPT is instructed
            not to repeat them.
    """
    now = datetime.now(timezone.utc)
    last_week_end = now - timedelta(days=now.weekday() + 1)  # Last Sunday
    last_week_start = last_week_end - timedelta(days=6)       # Last Monday
    week_str = f"{last_week_start.strftime('%-d')}–{last_week_end.strftime('%-d %B %Y')}"

    dedup_block = ""
    if previous_titles:
        dedup_block = "\nDo NOT recommend any of these previously suggested titles:\n"
        dedup_block += "\n".join(f"- {t}" for t in previous_titles[:100])
        dedup_block += "\n"

    prompt = f"""Search the web for the most popular and trending movies and TV shows for the week of {week_str}.

Include:
- New cinema/theatrical releases this week
- Popular new streaming releases (Netflix, Disney+, Apple TV+, Prime, HBO)
- Shows and movies everyone is talking about on social media right now

Do NOT include anything released more than 3 months ago unless it's having a major resurgence.
{dedup_block}
Return exactly 14 items (mix of movies and TV shows).
{_RESPONSE_FORMAT}"""

    items = _call_openai(prompt, conn, use_web_search=True)
    return _parse_recommendations(items, "trending")


def _generate_personal(conn: sqlite3.Connection, watch_history: list[dict], user_ratings: list[dict] | None = None, previous_titles: list[str] | None = None) -> list[dict]:
    """Generate personalised recommendations based on watch history and user ratings.

    Args:
        watch_history: Recently watched titles from Plex.
        user_ratings: User star ratings from Plex.
        previous_titles: Titles recommended in the last 30 days — GPT is instructed
            not to repeat them.
    """
    history_text = "\n".join(
        f"- {h['title']} ({h['type']})"
        for h in watch_history[:50]
    )

    ratings_block = ""
    if user_ratings:
        ratings_text = "\n".join(
            f"- {r['title']} ({r['type']}): {r['stars']}/5 stars"
            for r in user_ratings[:80]
        )
        ratings_block = f"""
The household has rated these titles (1-5 stars). Use these ratings to understand their preferences — recommend more of what they rate highly and avoid genres/styles they rate poorly:

{ratings_text}
"""

    dedup_block = ""
    if previous_titles:
        dedup_block = "\nDo NOT recommend any of these previously suggested titles:\n"
        dedup_block += "\n".join(f"- {t}" for t in previous_titles[:100])
        dedup_block += "\n"

    prompt = f"""Based on this household's recent watch history and their ratings, suggest 14 movies and TV shows they would enjoy.
Consider all viewers' tastes. Do NOT suggest anything already in the watch history or ratings below.

Recent watch history:
{history_text}
{ratings_block}{dedup_block}
Return exactly 14 items (mix of movies and TV shows).
{_RESPONSE_FORMAT}"""

    items = _call_openai(prompt, conn, use_web_search=True)
    return _parse_recommendations(items, "personal")


def refresh_recommendations(conn: sqlite3.Connection, plex_client: PlexClient | None, manual: bool = False) -> int:
    """Fetch watch history, generate both trending and personal recommendations.

    Args:
        conn: DB connection.
        plex_client: Plex client for watch history and ratings.
        manual: When True (user-triggered refresh), replace only today's batch.
            When False (scheduled), keep historical batches and prune rows older
            than 90 days.

    Returns the total number of recommendations generated.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

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
        elif media_type == "anime":
            media_type = "anime"
        else:
            media_type = "movie"
        watch_history.append({"title": r["title"], "type": media_type})

    # Fetch user ratings from Plex (1-5 stars)
    user_ratings = []
    try:
        user_ratings = plex_client.get_user_ratings()
        logger.info("Fetched %d user ratings from Plex", len(user_ratings))
    except Exception:
        logger.warning("Failed to fetch Plex user ratings — proceeding without them", exc_info=True)

    # Gather previous recommendation titles for deduplication (last 30 days)
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    prev_rows = conn.execute(
        "SELECT DISTINCT title FROM suggestions WHERE batch_id >= ?", (cutoff_30d,)
    ).fetchall()
    previous_titles = [r["title"] for r in prev_rows]

    trending = _generate_trending(conn, previous_titles)
    personal = _generate_personal(conn, watch_history, user_ratings, previous_titles) if watch_history else []
    all_recommendations = trending + personal

    if not all_recommendations:
        return 0

    # Enrich with TMDB search + details and OMDb ratings in one pass
    _enrich_recommendations(all_recommendations, conn)

    if manual:
        # Manual refresh: replace today's batch only
        conn.execute("DELETE FROM suggestions WHERE batch_id = ?", (today,))
    else:
        # Auto refresh: keep historical batches, prune anything older than 90 days
        cutoff_90d = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM suggestions WHERE batch_id < ?", (cutoff_90d,))

    now_iso = now.isoformat()
    for s in all_recommendations:
        conn.execute(
            "INSERT INTO suggestions (title, year, media_type, category, tmdb_id, imdb_id, "
            "description, reason, poster_url, trailer_url, rating, rt_rating, "
            "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore, "
            "batch_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s["title"], s.get("year"), s["media_type"], s["category"], s.get("tmdb_id"),
             s.get("imdb_id"), s.get("description"), s["reason"], s.get("poster_url"),
             s.get("trailer_url"), s.get("rating"), s.get("rt_rating"),
             s.get("tagline"), s.get("runtime"), s.get("genres"), s.get("cast_json"),
             s.get("director"), s.get("trailer_key"), s.get("imdb_rating"), s.get("metascore"),
             today, now_iso),
        )
    conn.commit()

    logger.info("Generated %d recommendations (%d trending, %d personal)",
                len(all_recommendations), len(trending), len(personal))
    return len(all_recommendations)
