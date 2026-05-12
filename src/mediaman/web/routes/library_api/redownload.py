"""Redownload route, request schema, lookup matching, and audit-ID generation.

The ``POST /api/media/redownload`` handler lives here together with the
helpers it consumes; the barrel module re-exports the public names so
existing test patch targets (``mediaman.web.routes.library_api.foo``)
keep working.
"""

from __future__ import annotations

import difflib
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from urllib.parse import quote as _url_quote

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.db import get_db
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.infra.http import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository.library_api import record_redownload

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-admin cap on redownload triggers.  Each call spawns an Arr lookup +
# add_movie/add_series round-trip, so a tighter burst cap than the search
# path: 20 per minute / 200 per day per actor.
_REDOWNLOAD_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)

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


def _pick_lookup_match(
    lookup: Sequence[Mapping[str, object]],
    *,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    id_keys: tuple[str, ...],
) -> tuple[Mapping[str, object] | None, str | None]:
    """Return (entry, error) for a Radarr/Sonarr lookup response."""
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

    if not title:
        return None, "No title for fuzzy match"

    target = title.strip().lower()
    scored: list[tuple[float, Mapping[str, object]]] = []
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


def _radarr_add_and_record(
    client: ArrClient,
    entry: Mapping[str, object],
    *,
    conn: sqlite3.Connection,
    title: str,
    imdb_id: str | None,
    username: str,
) -> JSONResponse | None:
    """Add a movie via Radarr and persist the audit row.

    Returns ``None`` if the lookup entry has no usable ``tmdbId`` (in
    which case the caller should keep falling through).
    """
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
        imdb_id=imdb_id,
    )
    record_redownload(
        conn,
        audit_id=audit_id,
        audit_detail=f"Re-downloaded '{resolved_title}' by {username}",
        actor=username,
        email=username,
        title=resolved_title,
        media_type="movie",
        service="radarr",
        tmdb_id=resolved_tmdb_int,
    )
    logger.info(
        "Re-downloaded '%s' (tmdb=%s) via Radarr by %s",
        resolved_title,
        resolved_tmdb,
        username,
    )
    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Radarr"})


def _try_radarr_redownload(
    client: ArrClient,
    *,
    conn: sqlite3.Connection,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    imdb_id: str | None,
    username: str,
) -> JSONResponse | None:
    """Attempt a Radarr redownload; ``None`` means fall through to Sonarr.

    A successful add returns the "added to Radarr" response. A duplicate
    (HTTP 409/422) returns an "already exists" response. All other failures
    log and return ``None`` so the Sonarr fallback gets a chance.
    """
    try:
        lookup = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/movie/lookup")
        entry, _err = _pick_lookup_match(
            lookup or [],
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            tvdb_id=None,
            imdb_id=imdb_id,
            id_keys=("tmdbId", "imdbId"),
        )
        if entry is None:
            return None
        return _radarr_add_and_record(
            client, entry, conn=conn, title=title, imdb_id=imdb_id, username=username
        )
    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Radarr"})
        # Fall through to try Sonarr
        return None
    except (requests.RequestException, ArrError, ValueError, sqlite3.Error) as exc:
        # Match the Sonarr branch so a Radarr misconfiguration
        # (``ArrConfigError`` from missing root folder / quality profile,
        # ``ArrUpstreamError`` from a bad lookup response, ``ValueError``
        # from an invalid tmdb/tvdb id) doesn't abort the whole handler
        # before the Sonarr fallback gets a chance to run.
        logger.warning("Radarr redownload failed for '%s': %s", title, exc, exc_info=True)
        return None


def _sonarr_add_and_record(
    sonarr_client: ArrClient,
    entry: Mapping[str, object],
    *,
    conn: sqlite3.Connection,
    title: str,
    imdb_id: str | None,
    username: str,
) -> JSONResponse | None:
    """Add a series via Sonarr and persist the audit row.

    Returns ``None`` if the lookup entry has no usable ``tvdbId`` (in
    which case the caller treats the lookup as a miss).
    """
    resolved_tvdb = entry.get("tvdbId")
    if not resolved_tvdb:
        return None
    resolved_title = str(entry.get("title") or title)
    resolved_tvdb_int = int(str(resolved_tvdb))
    sonarr_client.add_series(resolved_tvdb_int, resolved_title)
    resolved_tmdb_sonarr = entry.get("tmdbId")
    resolved_tmdb_sonarr_int = (
        int(str(resolved_tmdb_sonarr)) if resolved_tmdb_sonarr is not None else None
    )
    audit_id = _redownload_audit_id(
        media_type="tv",
        tmdb_id=resolved_tmdb_sonarr_int,
        tvdb_id=resolved_tvdb_int,
        imdb_id=imdb_id,
    )
    record_redownload(
        conn,
        audit_id=audit_id,
        audit_detail=f"Re-downloaded '{resolved_title}' by {username}",
        actor=username,
        email=username,
        title=resolved_title,
        media_type="tv",
        service="sonarr",
        tmdb_id=resolved_tmdb_sonarr_int,
        tvdb_id=resolved_tvdb_int,
    )
    logger.info(
        "Re-downloaded '%s' (tvdb=%s) via Sonarr by %s",
        resolved_title,
        resolved_tvdb,
        username,
    )
    return JSONResponse({"ok": True, "message": f"Added '{resolved_title}' to Sonarr"})


def _sonarr_fallback_fail_response() -> JSONResponse:
    """Return the generic 200-body failure used when Sonarr is unreachable."""
    return JSONResponse(
        {"ok": False, "error": "Download request failed — check service connectivity"}
    )


def _try_sonarr_redownload(
    sonarr_client: ArrClient,
    *,
    conn: sqlite3.Connection,
    title: str,
    year: int | None,
    tmdb_id: int | None,
    tvdb_id: int | None,
    imdb_id: str | None,
    username: str,
) -> JSONResponse | None:
    """Attempt a Sonarr redownload; ``None`` means fall through to "not found".

    Unlike the Radarr branch, all Sonarr exceptions surface as a 200-body
    failure response: by the time we reach Sonarr there is no further
    fallback, so a transport error must be reported rather than silently
    swallowed.
    """
    try:
        results = sonarr_client.lookup_by_term(_url_quote(title), endpoint="/api/v3/series/lookup")
        entry, err = _pick_lookup_match(
            results or [],
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            imdb_id=imdb_id,
            id_keys=("tvdbId", "tmdbId", "imdbId"),
        )
        if entry is not None:
            resp = _sonarr_add_and_record(
                sonarr_client, entry, conn=conn, title=title, imdb_id=imdb_id, username=username
            )
            if resp is not None:
                return resp
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
        logger.exception("Re-download via Sonarr failed for '%s': HTTP %s", title, exc.status_code)
        return _sonarr_fallback_fail_response()
    except (requests.RequestException, ArrError, ValueError, sqlite3.Error):
        logger.exception("Re-download via Sonarr failed for '%s'", title)
        return _sonarr_fallback_fail_response()


def _validate_redownload_request(
    body: _RedownloadRequest,
) -> tuple[str, int | None, int | None, int | None, str | None] | JSONResponse:
    """Normalise + validate a redownload payload.

    Returns the tuple ``(title, year, tmdb_id, tvdb_id, imdb_id)`` on
    success, or a 400 ``JSONResponse`` describing what is missing.
    """
    title = body.title.strip()[:256]
    year = body.year
    tmdb_id = body.tmdb_id
    tvdb_id = body.tvdb_id
    imdb_id = body.imdb_id.strip() if body.imdb_id else None
    if imdb_id == "":
        imdb_id = None

    if tmdb_id is None and tvdb_id is None and not imdb_id and (not title or year is None):
        return JSONResponse(
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
        return JSONResponse({"ok": False, "error": "No title provided"}, status_code=400)

    return title, year, tmdb_id, tvdb_id, imdb_id


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: _RedownloadRequest,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item via Radarr or Sonarr."""
    # Late import: the barrel re-exports build_radarr_from_db /
    # build_sonarr_from_db, and tests patch those names at the barrel
    # module path. A top-level ``from ...library_api import …`` here would
    # create a circular import (barrel → redownload → barrel), so we look
    # them up at call time via the fully-loaded barrel.
    from mediaman.web.routes.library_api import build_radarr_from_db, build_sonarr_from_db

    if not _REDOWNLOAD_LIMITER.check(username):
        logger.warning("media.redownload_throttled user=%s", username)
        return JSONResponse(
            {"ok": False, "error": "Too many redownload requests — slow down"},
            status_code=429,
        )

    validated = _validate_redownload_request(body)
    if isinstance(validated, JSONResponse):
        return validated
    title, year, tmdb_id, tvdb_id, imdb_id = validated

    conn = get_db()
    secret_key = request.app.state.config.secret_key

    radarr = build_radarr_from_db(conn, secret_key)
    if radarr is not None:
        resp = _try_radarr_redownload(
            radarr,
            conn=conn,
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            username=username,
        )
        if resp is not None:
            return resp

    sonarr = build_sonarr_from_db(conn, secret_key)
    if sonarr is not None:
        resp = _try_sonarr_redownload(
            sonarr,
            conn=conn,
            title=title,
            year=year,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            imdb_id=imdb_id,
            username=username,
        )
        if resp is not None:
            return resp

    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr or Sonarr"})
