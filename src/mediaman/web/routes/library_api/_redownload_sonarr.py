"""Sonarr branch of the redownload flow: add-and-record + try-redownload.

These helpers are the Sonarr half of ``POST /api/media/redownload``.
They are split out of :mod:`mediaman.web.routes.library_api.redownload` to
keep that module under the size ceiling; the redownload handler imports
them back so the registered route and its behaviour are unchanged. The
shared lookup matcher and audit-ID picker live in
:mod:`mediaman.web.routes.library_api._redownload_match` because the
Radarr branch consumes them too.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from urllib.parse import quote as _url_quote

import requests
from fastapi.responses import JSONResponse

from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.infra import SafeHTTPError
from mediaman.web.repository.library_api import record_redownload
from mediaman.web.routes.library_api._redownload_match import (
    _pick_lookup_match,
    _redownload_audit_id,
)

logger = logging.getLogger(__name__)


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
