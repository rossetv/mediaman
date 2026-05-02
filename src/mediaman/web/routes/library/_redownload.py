"""Re-download flow and the ``/api/media/redownload`` handler.

Split out of :mod:`.api` to keep that module focused on orchestration.
This module owns the lookup-matching and audit-id helpers used by the
redownload handler — they are not used elsewhere.

Handler dependencies on Radarr/Sonarr client builders are looked up via
the sibling ``api`` module so that test patches on
``mediaman.web.routes.library.api.build_radarr_from_db`` / ``build_sonarr_from_db``
continue to take effect.
"""

from __future__ import annotations

import difflib
import logging
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.db import get_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http_client import SafeHTTPError

logger = logging.getLogger("mediaman")

router = APIRouter()


# Per-admin cap on redownload triggers (finding 8). Each call spawns an
# Arr lookup + add_movie/add_series round-trip, so a tighter burst cap
# than the search-query path: 20 per minute / 200 per day per actor.
_REDOWNLOAD_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)


# Minimum title similarity accepted for a title+year fuzzy match.
_REDOWNLOAD_TITLE_SIMILARITY = 0.9


class _RedownloadRequest(BaseModel):
    """Body schema for ``POST /api/media/redownload`` (finding 9).

    ``extra="forbid"`` rejects unknown keys with HTTP 422 instead of
    silently ignoring them. The title is bounded at 4096 chars so an
    over-length payload is refused at the wire layer; the handler
    further truncates to 256 chars (matching the historic behaviour)
    so existing clients that send slightly over-length titles continue
    to work. Sane integer bounds are applied on the ID fields so an
    attacker cannot smuggle in a negative or wildly large value.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=4096)
    year: int | None = Field(default=None, ge=1850, le=2200)
    tmdb_id: int | None = Field(default=None, ge=1)
    tvdb_id: int | None = Field(default=None, ge=1)
    imdb_id: str | None = Field(default=None, max_length=32)


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
                if got is None or wanted is None:
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

    def _norm(s: str) -> str:
        return s.strip().lower()

    target = _norm(title)
    scored: list[tuple[float, dict[str, object]]] = []
    for entry in lookup:
        cand_title = _norm(entry.get("title") or "")
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
    """Pick a stable audit-log ``media_item_id`` for a redownload (finding 10).

    The deletion path stores a UUID in this column; the historic
    redownload path stored the resolved title (free-text, locale-
    dependent, mutable). Mixing those breaks downstream consumers — the
    dashboard tries to cross-reference re-downloads against deletions
    by string-matching the column.

    Strategy: prefer the most stable identifier we have. ``tmdb:<id>``
    for movies and TV (because TMDB ids are universal across both
    Arrs); ``tvdb:<id>`` if Sonarr only knew the show by TVDB; finally
    ``imdb:<id>`` as a last resort. Each prefix makes the column
    self-describing instead of an opaque integer the dashboard might
    accidentally match against unrelated UUIDs.

    The ``actor`` column on the audit row carries the username; the
    ``detail`` field holds the human-readable title for display.
    """
    if tmdb_id is not None:
        return f"tmdb:{tmdb_id}"
    if tvdb_id is not None:
        return f"tvdb:{tvdb_id}"
    if imdb_id:
        return f"imdb:{imdb_id}"
    # Should never happen — caller guarantees at least one stable id is
    # present before we reach here. Fall back to a synthetic prefix so
    # the column never holds a raw user-controlled title.
    return f"redownload:{media_type}"


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: _RedownloadRequest,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item."""
    # Lazy import: handler accesses ``build_radarr_from_db`` /
    # ``build_sonarr_from_db`` via the parent ``api`` module so test
    # patches on those names take effect at call time.
    from . import api as _api

    if not _REDOWNLOAD_LIMITER.check(username):
        logger.warning("media.redownload_throttled user=%s", username)
        return JSONResponse(
            {"ok": False, "error": "Too many redownload requests — slow down"},
            status_code=429,
        )

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

    conn = get_db()
    config = request.app.state.config

    # Try Radarr first (movies)
    try:
        client = _api.build_radarr_from_db(conn, config.secret_key)
        if client:
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
            if entry is not None:
                resolved_tmdb = entry.get("tmdbId")
                if resolved_tmdb:
                    resolved_title = entry.get("title") or title
                    resolved_tmdb_int = int(resolved_tmdb) if resolved_tmdb is not None else None
                    client.add_movie(resolved_tmdb_int, resolved_title)
                    audit_id = _redownload_audit_id(
                        media_type="movie",
                        tmdb_id=resolved_tmdb_int,
                        tvdb_id=None,
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
                    return JSONResponse(
                        {"ok": True, "message": f"Added '{resolved_title}' to Radarr"}
                    )
    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Radarr"})
        # Fall through to try Sonarr

    # Try Sonarr (TV)
    try:
        client = _api.build_sonarr_from_db(conn, config.secret_key)
        if client:
            results = client.lookup_by_term(_url_quote(title), endpoint="/api/v3/series/lookup")
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
                resolved_tvdb = entry.get("tvdbId")
                if resolved_tvdb:
                    resolved_title = entry.get("title") or title
                    resolved_tvdb_int = int(resolved_tvdb) if resolved_tvdb is not None else None
                    client.add_series(resolved_tvdb_int, resolved_title)
                    resolved_tmdb_sonarr = entry.get("tmdbId")
                    resolved_tmdb_sonarr_int = (
                        int(resolved_tmdb_sonarr) if resolved_tmdb_sonarr is not None else None
                    )
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
                    logger.info(
                        "Re-downloaded '%s' (tvdb=%s) via Sonarr by %s",
                        resolved_title,
                        resolved_tvdb,
                        username,
                    )
                    return JSONResponse(
                        {"ok": True, "message": f"Added '{resolved_title}' to Sonarr"}
                    )
            if err in ("Ambiguous ID match", "Ambiguous title+year match"):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Ambiguous match for '{title}' — supply tmdb_id/tvdb_id/imdb_id",
                    },
                    status_code=409,
                )
    except SafeHTTPError as exc:
        if exc.status_code in (409, 422):
            return JSONResponse({"ok": False, "error": f"'{title}' already exists in Sonarr"})
        logger.warning(
            "Re-download via Sonarr failed for '%s': HTTP %s", title, exc.status_code, exc_info=True
        )
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"}
        )
    except Exception as exc:
        logger.warning("Re-download via Sonarr failed for '%s': %s", title, exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": "Download request failed — check service connectivity"}
        )

    return JSONResponse({"ok": False, "error": f"'{title}' not found in Radarr or Sonarr"})
