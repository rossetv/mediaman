"""Download confirmation page."""

from __future__ import annotations

import json
import logging
import re

import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mediaman.auth.rate_limit import RateLimiter, get_client_ip
from mediaman.crypto import validate_download_token
from mediaman.db import get_db
from mediaman.services.arr_build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr_state import (
    build_radarr_cache,
    build_sonarr_cache,
    compute_download_state,
)
from mediaman.services.download_format import build_item
from mediaman.services.http_client import SafeHTTPError
from mediaman.services.item_enrichment import enrich_redownload_item

# YouTube video IDs are exactly 11 URL-safe base64 characters.
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

logger = logging.getLogger("mediaman")

router = APIRouter()

# Rate limiter for the public download GET endpoint.
_DOWNLOAD_LIMITER_GET = RateLimiter(max_attempts=30, window_seconds=60)


def validate_youtube_id(s: str | None) -> str | None:
    """Return *s* if it is a valid YouTube video ID, else None."""
    if not s:
        return None
    return s if _YOUTUBE_ID_RE.match(s) else None


# Backward-compat alias.
_validate_youtube_id = validate_youtube_id


def _base_download_item(payload: dict) -> dict:
    """Build the skeleton download item from a validated token payload."""
    return {
        "title":       payload.get("title", ""),
        "media_type":  payload.get("mt", ""),
        "tmdb_id":     payload.get("tmdb"),
        "email":       payload.get("email", ""),
        "action":      payload.get("act", "download"),
        "poster_url":  None,
        "year":        None,
        "description": None,
        "reason":      None,
        "rating":      None,
        "rt_rating":   None,
        "tagline":     None,
        "runtime":     None,
        "genres":      None,
        "cast_json":   None,
        "director":    None,
        "trailer_key": None,
        "imdb_rating": None,
        "metascore":   None,
        "genres_list": [],
        "cast_list":   [],
    }


def _build_item_from_suggestion(payload: dict, row) -> dict:
    """Build a fully-populated download item from a suggestions DB row."""
    item = _base_download_item(payload)
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
        "trailer_key": validate_youtube_id(row["trailer_key"]),
        "imdb_rating": row["imdb_rating"],
        "metascore":   row["metascore"],
    })
    return item


@router.get("/download/{token}", response_class=HTMLResponse)
def download_page(request: Request, token: str) -> HTMLResponse:
    """Render the download confirmation page."""
    config = request.app.state.config
    templates = request.app.state.templates
    conn = get_db()

    if not _DOWNLOAD_LIMITER_GET.check(get_client_ip(request)):
        return HTMLResponse("Too many requests. Try again later.", status_code=429)

    if len(token) > 4096:
        return templates.TemplateResponse(request, "download.html", {
            "state": "expired",
            "item": None,
        })

    payload = validate_download_token(token, config.secret_key)
    if payload is None:
        return templates.TemplateResponse(request, "download.html", {
            "state": "expired",
            "item": None,
        })

    sid = payload.get("sid")
    if sid:
        row = conn.execute(
            "SELECT poster_url, year, description, reason, rating, rt_rating, "
            "tagline, runtime, genres, cast_json, director, trailer_key, imdb_rating, metascore "
            "FROM suggestions WHERE id = ?",
            (sid,),
        ).fetchone()
        item = _build_item_from_suggestion(payload, row) if row else _base_download_item(payload)
    elif payload.get("act") == "redownload":
        item = _base_download_item(payload)
        enrich_redownload_item(item, conn, config.secret_key)
    else:
        item = _base_download_item(payload)

    item["trailer_key"] = validate_youtube_id(item.get("trailer_key"))

    if item.get("genres"):
        try:
            item["genres_list"] = json.loads(item["genres"])
        except (json.JSONDecodeError, TypeError):
            item["genres_list"] = []
    if item.get("cast_json"):
        try:
            item["cast_list"] = json.loads(item["cast_json"])
        except (json.JSONDecodeError, TypeError):
            item["cast_list"] = []

    item["download_state"] = None
    tmdb_id = payload.get("tmdb")
    if tmdb_id:
        try:
            radarr_client = build_radarr_from_db(conn, config.secret_key)
            sonarr_client = build_sonarr_from_db(conn, config.secret_key)
            radarr_cache = build_radarr_cache(radarr_client)
            sonarr_cache = build_sonarr_cache(sonarr_client)
            caches = {**radarr_cache, **sonarr_cache}
            mt = "movie" if payload.get("mt") == "movie" else "tv"
            state = compute_download_state(mt, tmdb_id, caches)
            if state is not None:
                item["download_state"] = state
        except (requests.RequestException, SafeHTTPError):
            logger.warning("Failed to check Arr library status for tmdb_id=%s", tmdb_id, exc_info=True)

    hero_item = None
    if item["download_state"] == "queued":
        service = "radarr" if item["media_type"] == "movie" else "sonarr"
        hero_item = build_item(
            dl_id=f"{service}:{item['title']}",
            title=item["title"],
            media_type=item["media_type"],
            poster_url=item.get("poster_url") or "",
            state="searching",
            progress=0,
            eta="",
            size_done="",
            size_total="",
        )

    return templates.TemplateResponse(request, "download.html", {
        "state": "confirm",
        "item":  item,
        "token": token,
        "hero_item": hero_item,
    })
