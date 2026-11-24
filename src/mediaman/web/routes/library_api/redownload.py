"""Redownload route (POST /api/media/redownload), its request schema and rate-limiter
singleton.

The flow is split across ._redownload_match (audit-ID picker), ._redownload_radarr
(movies) and ._redownload_sonarr (seasons).
"""

from __future__ import annotations

import logging

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.db import get_db
from mediaman.services.arr.base import ArrError, SeasonsNotListed
from mediaman.services.infra import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.responses import respond_err
from mediaman.web.routes.download import drop_arr_cache
from mediaman.web.routes.library_api._redownload_match import AddRefused
from mediaman.web.routes.library_api._redownload_radarr import _redownload_movie
from mediaman.web.routes.library_api._redownload_sonarr import _redownload_season

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-admin cap on redownload triggers.  Each call makes an Arr lookup +
# remonitor/add round-trip, so a tighter burst cap than the search
# path: 20 per minute / 200 per day per actor.
_REDOWNLOAD_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)


class _RedownloadRequest(BaseModel):
    """Body schema for POST /api/media/redownload.

    A movie carries tmdb_id; a season carries tvdb_id and season_number. extra="forbid"
    rejects unknown keys with a 422. The title is bounded at 4096 chars so oversize payloads
    are refused at the wire layer, and the handler truncates to 256 so clients sending
    slightly long titles keep working. ID fields have integer bounds so a negative or huge
    value cannot get in.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=4096)
    tmdb_id: int | None = Field(default=None, ge=1)
    tvdb_id: int | None = Field(default=None, ge=1)
    season_number: int | None = Field(default=None, ge=0, le=10000)


def _bad_request(error: str) -> JSONResponse:
    return respond_err(error, message=error)


def _is_already_added(exc: SafeHTTPError) -> bool:
    """True when an add was refused because the entry exists.

    Servarr answers 409 or 422 for a duplicate, and some versions a 400 whose body says so.
    """
    if exc.status_code in (409, 422):
        return True
    return exc.status_code == 400 and "already" in exc.body_snippet.lower()


_ARR_FAILURES = (AddRefused, SafeHTTPError, requests.RequestException, ArrError, ValueError)


def _not_configured(service: str) -> JSONResponse:
    text = f"{service} is not configured"
    return respond_err(text, status=409, message=text)


def _arr_failure(exc: Exception, service: str, title: str) -> JSONResponse:
    if isinstance(exc, AddRefused):
        if _is_already_added(exc.cause):
            text = f"'{title}' already exists in {service}"
            return respond_err(text, status=200, message=text)
        exc = exc.cause
    if isinstance(exc, SeasonsNotListed):
        return respond_err(str(exc), status=200, message=str(exc))
    # No exc_info: the traceback's last line would print the client's text raw.
    logger.error(
        "Re-download via %s failed for '%s': %s: %s",
        service,
        _sanitise_log_field(title, 256),
        type(exc).__name__,
        _sanitise_log_field(str(exc), 512),
    )
    text = "Download request failed — check service connectivity"
    return respond_err(text, status=200, message=text)


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: _RedownloadRequest,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    # Late import: the barrel re-exports build_radarr_from_db /
    # build_sonarr_from_db, and tests patch those names at the barrel
    # module path. A top-level ``from ...library_api import …`` here would
    # create a circular import (barrel → redownload → barrel), so we look
    # them up at call time via the fully-loaded barrel.
    from mediaman.web.routes.library_api import build_radarr_from_db, build_sonarr_from_db

    if not _REDOWNLOAD_LIMITER.check(username):
        logger.warning("media.redownload_throttled user=%s", _sanitise_log_field(username))
        text = "Too many redownload requests — slow down"
        return respond_err(text, status=429, message=text)

    title = body.title.strip()[:256]
    if not title:
        return _bad_request("No title provided")
    tmdb_id, tvdb_id, season_number = body.tmdb_id, body.tvdb_id, body.season_number
    conn = get_db()
    secret_key = request.app.state.config.secret_key

    if tmdb_id is not None and tvdb_id is None:
        radarr = build_radarr_from_db(conn, secret_key)
        if radarr is None:
            return _not_configured("Radarr")
        try:
            return _redownload_movie(
                radarr, conn=conn, title=title, tmdb_id=tmdb_id, username=username
            )
        except _ARR_FAILURES as exc:
            return _arr_failure(exc, "Radarr", title)
        finally:
            # A failed request may have added the title too.
            drop_arr_cache()

    if tvdb_id is not None and tmdb_id is None and season_number is not None:
        sonarr = build_sonarr_from_db(conn, secret_key)
        if sonarr is None:
            return _not_configured("Sonarr")
        try:
            return _redownload_season(
                sonarr,
                conn=conn,
                title=title,
                tvdb_id=tvdb_id,
                season_number=season_number,
                username=username,
            )
        except _ARR_FAILURES as exc:
            return _arr_failure(exc, "Sonarr", title)
        finally:
            drop_arr_cache()

    return _bad_request("Provide tmdb_id for a movie, or tvdb_id and season_number for a season")
