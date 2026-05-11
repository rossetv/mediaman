"""Redownload helpers: request schema, lookup matching, audit-ID generation,
and per-service redownload handlers.

The handlers here own the lookup → match → add → audit flow for each
Arr service.  ``__init__`` orchestrates the Radarr-then-Sonarr fall
through and provides the rate-limit gate / body validation.
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import quote as _url_quote

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.audit import log_audit
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http import SafeHTTPError

if TYPE_CHECKING:
    from mediaman.services.arr.base import ArrClient

logger = logging.getLogger(__name__)


class RedownloadParams(TypedDict):
    """Normalised parameters for a redownload request — see :func:`validate_redownload_body`."""

    title: str
    year: int | None
    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str | None


# Minimum title similarity accepted for a title+year fuzzy match.
_REDOWNLOAD_TITLE_SIMILARITY = 0.9


class _RedownloadRequest(BaseModel):
    """Body schema for ``POST /api/media/redownload``.

    ``extra="forbid"`` rejects unknown keys with HTTP 422 instead of
    silently ignoring them.  The title is bounded at 4096 chars so an
    over-length payload is refused at the wire layer; the handler further
    truncates to 256 chars (matching the historic behaviour) so existing
    clients that send slightly over-length titles continue to work.
    Sane integer bounds are applied on the ID fields so an attacker cannot
    smuggle in a negative or wildly large value.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=4096)
    year: int | None = Field(default=None, ge=1850, le=2200)
    tmdb_id: int | None = Field(default=None, ge=1)
    tvdb_id: int | None = Field(default=None, ge=1)
    imdb_id: str | None = Field(default=None, max_length=32)


def _match_by_id(
    lookup: list[dict[str, object]],
    wanted_ids: dict[str, object],
) -> tuple[dict[str, object] | None, str | None]:
    """Return the lookup entry whose ID equals one of *wanted_ids*."""
    hits = []
    for entry in lookup:
        for key, wanted in wanted_ids.items():
            got = entry.get(key)
            if got is None:
                continue
            if str(got).strip().lower() == str(wanted).strip().lower():
                hits.append(entry)
                break
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "Ambiguous ID match"
    return None, "Supplied ID did not match any lookup result"


def _match_by_title_year(
    lookup: list[dict[str, object]], title: str, year: int | None
) -> tuple[dict[str, object] | None, str | None]:
    """Fuzzy-match by title similarity + exact year.  Returns ``(entry, error)``."""
    if not title:
        return None, "No title for fuzzy match"
    target = title.strip().lower()
    scored: list[tuple[float, dict[str, object]]] = []
    for entry in lookup:
        cand_title = str(entry.get("title") or "").strip().lower()
        if not cand_title:
            continue
        ratio = difflib.SequenceMatcher(None, target, cand_title).ratio()
        scored.append((ratio, entry))
    if not scored:
        return None, "No titled lookup results"
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best = scored[0]
    if best_score < _REDOWNLOAD_TITLE_SIMILARITY:
        return None, "No confident title match"
    if year is None or best.get("year") != year:
        return None, "Year mismatch or missing"
    close = [
        entry
        for score, entry in scored
        if score >= _REDOWNLOAD_TITLE_SIMILARITY and entry.get("year") == year
    ]
    if len(close) > 1:
        return None, "Ambiguous title+year match"
    return best, None


def _pick_lookup_match(
    lookup: list[dict[str, object]],
    *,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    id_keys: tuple[str, ...],
) -> tuple[dict[str, object] | None, str | None]:
    """Return (entry, error) for a Radarr/Sonarr lookup response.

    Tries ID matching first (any of TMDB / TVDB / IMDb), then falls back
    to fuzzy title + exact-year matching when no IDs are supplied.
    """
    if not lookup:
        return None, "No lookup results"

    wanted_ids: dict[str, object] = {}
    if tmdb_id is not None:
        wanted_ids["tmdbId"] = tmdb_id
    if tvdb_id is not None:
        wanted_ids["tvdbId"] = tvdb_id
    if imdb_id:
        wanted_ids["imdbId"] = imdb_id
    if wanted_ids:
        return _match_by_id(lookup, wanted_ids)
    return _match_by_title_year(lookup, title, year)


def _redownload_audit_id(
    *,
    media_type: str,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
) -> str:
    """Pick a stable audit-log ``media_item_id`` for a redownload.

    Prefer the most stable identifier available: ``tmdb:<id>`` for movies
    and TV (TMDB IDs are universal across both Arrs); ``tvdb:<id>`` if
    Sonarr only knew the show by TVDB; finally ``imdb:<id>`` as a last
    resort.  Each prefix makes the column self-describing instead of an
    opaque integer the dashboard might accidentally match against
    unrelated UUIDs.
    """
    if tmdb_id is not None:
        return f"tmdb:{tmdb_id}"
    if tvdb_id is not None:
        return f"tvdb:{tvdb_id}"
    if imdb_id:
        return f"imdb:{imdb_id}"
    # Should never happen — caller guarantees at least one stable id is
    # present before we reach here.
    return f"redownload:{media_type}"


def validate_redownload_body(
    body: _RedownloadRequest,
) -> tuple[RedownloadParams | None, JSONResponse | None]:
    """Normalise and validate the redownload request body.

    Returns ``(params_dict, None)`` on success or
    ``(None, error_response)`` when no identifier is supplied or the
    title is empty.  Strips whitespace and clamps the title to 256 chars
    so a hostile caller cannot blow up downstream Arr lookups.
    """
    title = body.title.strip()[:256]
    imdb_id = body.imdb_id.strip() if body.imdb_id else None
    if imdb_id == "":
        imdb_id = None
    tmdb_id = body.tmdb_id
    tvdb_id = body.tvdb_id
    year = body.year

    if tmdb_id is None and tvdb_id is None and not imdb_id and (not title or year is None):
        return None, JSONResponse(
            {
                "ok": False,
                "error": (
                    "Provide at least one of tmdb_id, tvdb_id, imdb_id; "
                    "title+year alone is only accepted with an exact "
                    "year and a confident title match"
                ),
            },
            status_code=400,
        )
    if not title:
        return None, JSONResponse({"ok": False, "error": "No title provided"}, status_code=400)
    return {
        "title": title,
        "year": year,
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "imdb_id": imdb_id,
    }, None


def handle_radarr_redownload(
    conn: sqlite3.Connection, client: ArrClient, params: RedownloadParams, username: str
) -> JSONResponse | None:
    """Run the Radarr redownload path.  Returns a JSONResponse on success, None on no-match."""
    title = params["title"]
    assert isinstance(title, str)
    lookup = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/movie/lookup")
    entry, _err = _pick_lookup_match(
        lookup or [],
        title=title,
        year=params["year"],
        tmdb_id=params["tmdb_id"],
        tvdb_id=None,
        imdb_id=params["imdb_id"],
        id_keys=("tmdbId", "imdbId"),
    )
    if entry is None:
        return None
    resolved_tmdb = entry.get("tmdbId")
    if not resolved_tmdb:
        return None
    resolved_title = str(entry.get("title") or title)
    resolved_tmdb_int = int(str(resolved_tmdb))
    client.add_movie(resolved_tmdb_int, resolved_title)
    audit_id = _redownload_audit_id(
        media_type="movie",
        tmdb_id=resolved_tmdb_int,
        tvdb_id=None,
        imdb_id=params["imdb_id"],
    )
    log_audit(
        conn,
        audit_id,
        "re_downloaded",
        f"Re-downloaded '{resolved_title}' by {username}",
        actor=username,
    )
    record_download_notification(
        conn,
        email=username,
        title=resolved_title,
        media_type="movie",
        tmdb_id=resolved_tmdb_int,
        service="radarr",
    )
    conn.commit()
    logger.info(
        "Re-downloaded '%s' (tmdb=%s) via Radarr by %s",
        resolved_title,
        resolved_tmdb,
        username,
    )
    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Radarr"})


def try_radarr_redownload(
    conn: sqlite3.Connection,
    client: ArrClient | None,
    params: RedownloadParams,
    username: str,
) -> JSONResponse | None:
    """Wrap :func:`handle_radarr_redownload` with the SafeHTTPError translator.

    Returns a JSONResponse on success / "already exists", or None when
    no Radarr client is configured or the lookup found no match (the
    caller falls through to Sonarr).
    """
    if not client:
        return None
    try:
        return handle_radarr_redownload(conn, client, params, username)
    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return JSONResponse(
                {"ok": False, "error": f"'{params['title']}' already exists in Radarr"}
            )
        return None  # Fall through to Sonarr


def _record_sonarr_redownload(
    conn: sqlite3.Connection,
    *,
    resolved_title: str,
    resolved_tvdb_int: int,
    resolved_tmdb_sonarr_int: int | None,
    imdb_id: str | None,
    username: str,
) -> None:
    """Write the audit row + download-notification record for a Sonarr add."""
    audit_id = _redownload_audit_id(
        media_type="tv",
        tmdb_id=resolved_tmdb_sonarr_int,
        tvdb_id=resolved_tvdb_int,
        imdb_id=imdb_id,
    )
    log_audit(
        conn,
        audit_id,
        "re_downloaded",
        f"Re-downloaded '{resolved_title}' by {username}",
        actor=username,
    )
    record_download_notification(
        conn,
        email=username,
        title=resolved_title,
        media_type="tv",
        tmdb_id=resolved_tmdb_sonarr_int,
        tvdb_id=resolved_tvdb_int,
        service="sonarr",
    )
    conn.commit()


def handle_sonarr_redownload(
    conn: sqlite3.Connection,
    sonarr_client: ArrClient,
    params: RedownloadParams,
    username: str,
) -> tuple[JSONResponse | None, str | None]:
    """Run the Sonarr redownload path.

    Returns ``(response, err)`` where *err* is the matcher's
    "Ambiguous ID match" / "Ambiguous title+year match" marker so the
    caller can return a 409 even when no entry was picked.
    """
    title = params["title"]
    assert isinstance(title, str)
    results = sonarr_client.lookup_by_term(_url_quote(title), endpoint="/api/v3/series/lookup")
    entry, err = _pick_lookup_match(
        results or [],
        title=title,
        year=params["year"],
        tmdb_id=params["tmdb_id"],
        tvdb_id=params["tvdb_id"],
        imdb_id=params["imdb_id"],
        id_keys=("tvdbId", "tmdbId", "imdbId"),
    )
    if entry is None:
        return None, err
    resolved_tvdb = entry.get("tvdbId")
    if not resolved_tvdb:
        return None, err
    resolved_title = str(entry.get("title") or title)
    resolved_tvdb_int = int(str(resolved_tvdb))
    sonarr_client.add_series(resolved_tvdb_int, resolved_title)
    resolved_tmdb_sonarr = entry.get("tmdbId")
    resolved_tmdb_sonarr_int = (
        int(str(resolved_tmdb_sonarr)) if resolved_tmdb_sonarr is not None else None
    )
    _record_sonarr_redownload(
        conn,
        resolved_title=resolved_title,
        resolved_tvdb_int=resolved_tvdb_int,
        resolved_tmdb_sonarr_int=resolved_tmdb_sonarr_int,
        imdb_id=params["imdb_id"],
        username=username,
    )
    logger.info(
        "Re-downloaded '%s' (tvdb=%s) via Sonarr by %s",
        resolved_title,
        resolved_tvdb,
        username,
    )
    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Sonarr"}), err


def try_sonarr_redownload(
    conn: sqlite3.Connection,
    sonarr_client: ArrClient | None,
    params: RedownloadParams,
    username: str,
) -> JSONResponse | None:
    """Wrap :func:`handle_sonarr_redownload` with the Sonarr-side error translator.

    Returns a JSONResponse on success / "already exists" / "ambiguous
    match" / any handled HTTP error, or None when no Sonarr client is
    configured and we fall through to the final "not found" response.
    """
    title = params["title"]
    if not sonarr_client:
        return None
    try:
        response, err = handle_sonarr_redownload(conn, sonarr_client, params, username)
        if response is not None:
            return response
        if err in ("Ambiguous ID match", "Ambiguous title+year match"):
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Ambiguous match for '{title}' — supply tmdb_id/tvdb_id/imdb_id",
                },
                status_code=409,
            )
        return None
    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Sonarr"})
        logger.warning(
            "Re-download via Sonarr failed for '%s': HTTP %s",
            title,
            exc.status_code,
            exc_info=True,
        )
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"}
        )
    except Exception as exc:
        logger.warning("Re-download via Sonarr failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"}
        )
