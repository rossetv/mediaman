"""Enrich re-download items with rich metadata for the cinematic layout.

These are pure functions — they mutate an ``item`` dict in place but have
no side effects beyond DB reads and outbound API calls.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import requests

from mediaman.services.infra import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.media_meta.tmdb import TmdbDetail

_BACKDROP_BASE = "https://image.tmdb.org/t/p/w1280"
_BACKDROP_RE = re.compile(
    r"https://image\.tmdb\.org/t/p/(?:w\d{3,4}|original)/[A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp)"
)
_BACKDROP_TTL_S = 600.0
_BACKDROP_CACHE_MAX = 256
_BACKDROP_TMDB_TIMEOUT_S = 3.0
# (media type, TMDB id) -> (when it was asked, backdrop or None): a title TMDB has no backdrop
# for, or does not answer for, is not asked again on every view of its page.
_backdrop_cache: dict[tuple[str, int], tuple[float, str | None]] = {}
_clock = time.monotonic

# A stored summary cut by a limit of this many characters ends mid-sentence.
_OLD_SUMMARY_CUT = 250
_SENTENCE_END = (".", "!", "?", "…", '"', "”", "'", "’", ")")


def tidy_summary(text: str | None) -> str | None:
    """*text* with a mid-sentence cut tidied: summaries stored before they were kept whole
    are 250 characters and end where the limit fell, so they end at the last full sentence,
    or at the last whole word with an ellipsis when there is none.
    """
    if not text or len(text) != _OLD_SUMMARY_CUT:
        return text
    text = text.rstrip()
    if text.endswith(_SENTENCE_END):
        return text
    ends = [m.end() for m in re.finditer(r"[.!?][\"”')]?(?=\s)", text)]
    if ends:
        return text[: ends[-1]]
    return text.rsplit(" ", 1)[0].rstrip(",;:- ") + "…" if " " in text else text


def tmdb_backdrop(details: object) -> str | None:
    """The image.tmdb.org address of the wide backdrop in a TMDB details or search payload;
    None when it has none or a path that is no image name.
    """
    path = details.get("backdrop_path") if isinstance(details, Mapping) else None
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    url = _BACKDROP_BASE + path
    return url if _BACKDROP_RE.fullmatch(url) else None


def safe_backdrop(value: object) -> str | None:
    """*value* when it is a TMDB image address, else None: a stored value is shown as a picture
    address, so nothing else is let through.
    """
    return value if isinstance(value, str) and _BACKDROP_RE.fullmatch(value) else None


def fill_backdrop(item: dict[str, object], conn: sqlite3.Connection, secret_key: str) -> None:
    """Give *item* a wide ``backdrop_url``: the one it has, else one stored with a suggestion
    of the same title, else TMDB's. The item is left without one when none is found; this
    never raises.
    """
    item["backdrop_url"] = safe_backdrop(item.get("backdrop_url"))
    tmdb_id = item.get("tmdb_id")
    if item["backdrop_url"] or not isinstance(tmdb_id, int) or not tmdb_id:
        return
    media_type = "movie" if item.get("media_type") == "movie" else "tv"
    row = conn.execute(
        "SELECT backdrop_url FROM suggestions WHERE media_type = ? AND tmdb_id = ? "
        "AND backdrop_url IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (media_type, tmdb_id),
    ).fetchone()
    item["backdrop_url"] = safe_backdrop(row["backdrop_url"]) if row else None
    if item["backdrop_url"]:
        return
    key = (media_type, tmdb_id)
    now = _clock()
    hit = _backdrop_cache.get(key)
    if hit and now - hit[0] < _BACKDROP_TTL_S:
        item["backdrop_url"] = hit[1]
        return
    from mediaman.services.media_meta.tmdb import TmdbClient

    backdrop: str | None = None
    with contextlib.suppress(requests.RequestException, SafeHTTPError, ValueError):
        client = TmdbClient.from_db(conn, secret_key, timeout=_BACKDROP_TMDB_TIMEOUT_S)
        if client is not None:
            backdrop = tmdb_backdrop(client.details(media_type, tmdb_id))
    if len(_backdrop_cache) >= _BACKDROP_CACHE_MAX:
        _backdrop_cache.clear()
    _backdrop_cache[key] = (now, backdrop)
    item["backdrop_url"] = backdrop


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
                backdrop = tmdb_backdrop(data)
                if backdrop:
                    item["backdrop_url"] = backdrop

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
        "imdb_rating, metascore, tmdb_id, backdrop_url "
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
                "backdrop_url": row["backdrop_url"],
            }
        )
        if row["tmdb_id"] and not tmdb_id:
            item["tmdb_id"] = row["tmdb_id"]
        return

    # 2. Fall back to TMDB + OMDB APIs
    enrich_item_with_tmdb(item, conn, secret_key)
