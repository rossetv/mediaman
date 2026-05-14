"""Radarr branch of the redownload flow: add-and-record + try-redownload.

These two helpers are the Radarr half of ``POST /api/media/redownload``.
They are split out of :mod:`mediaman.web.routes.library_api.redownload` to
keep that module under the size ceiling; the redownload handler imports
them back so the registered route and its behaviour are unchanged. The
shared lookup matcher and audit-ID picker live in
:mod:`mediaman.web.routes.library_api._redownload_match` because the
Sonarr branch consumes them too.
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
