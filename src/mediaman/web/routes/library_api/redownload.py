"""Redownload route, request schema, and the rate-limiter singleton.

The ``POST /api/media/redownload`` handler lives here together with the
request schema and the per-admin rate limiter; the barrel module
re-exports the public names so existing test patch targets
(``mediaman.web.routes.library_api.foo``) keep working.

The flow is split across three private sibling modules to stay under the
size ceiling:

* :mod:`._redownload_match` — the lookup matcher and audit-ID picker
  shared by both Arr branches (re-exported here so
  ``...library_api.redownload._pick_lookup_match`` keeps working).
* :mod:`._redownload_radarr` — the Radarr add-and-record + try-redownload
  branch helpers.
* :mod:`._redownload_sonarr` — the Sonarr add-and-record + try-redownload
  branch helpers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.db import get_db
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.routes.library_api._redownload_match import (
    _REDOWNLOAD_TITLE_SIMILARITY as _REDOWNLOAD_TITLE_SIMILARITY,
)
from mediaman.web.routes.library_api._redownload_match import (
    _pick_lookup_match as _pick_lookup_match,
)
from mediaman.web.routes.library_api._redownload_match import (
    _redownload_audit_id as _redownload_audit_id,
)
from mediaman.web.routes.library_api._redownload_radarr import _try_radarr_redownload
from mediaman.web.routes.library_api._redownload_sonarr import _try_sonarr_redownload

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
