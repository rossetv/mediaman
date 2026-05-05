"""Library JSON API endpoints — package root.

Package layout:

* :mod:`~mediaman.web.routes.library_api.delete_intents` — delete-intent
  durability helpers (record / complete / fail / reconcile).
* :mod:`~mediaman.web.routes.library_api.redownload` — redownload request
  schema, lookup matching, and audit-ID generation.
* This module (``__init__``) — rate-limiter constants, route handlers, and
  re-exports of the above for backwards-compatible imports.

Handles all ``/api/library`` and ``/api/media/…`` JSON routes:

* GET  /api/library                — paginated library list
* POST /api/media/{id}/keep        — protect a media item
* POST /api/media/{id}/delete      — delete via Radarr/Sonarr (two-phase)
* POST /api/media/redownload       — trigger a re-download via Radarr/Sonarr

The browser-facing GET /library page lives in the sibling module
:mod:`mediaman.web.routes.library`.

The ``build_radarr_from_db`` and ``build_sonarr_from_db`` names are
imported at the top of this module and used directly.  Tests that were
previously patching ``mediaman.web.routes.library.api.build_radarr_from_db``
have been updated to patch ``mediaman.web.routes.library_api.build_radarr_from_db``.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse

from mediaman.audit import log_audit
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.library import _VALID_SORTS, _VALID_TYPES, fetch_library

# Re-exports for backwards-compatible imports
from mediaman.web.routes.library_api.delete_intents import (
    _complete_delete_intent,
    _fail_delete_intent,
    _record_delete_intent,
    reconcile_pending_delete_intents,
)
from mediaman.web.routes.library_api.redownload import (
    _REDOWNLOAD_TITLE_SIMILARITY,
    _RedownloadRequest,
    _pick_lookup_match,
    _redownload_audit_id,
)

logger = logging.getLogger("mediaman")

router = APIRouter()


# ---------------------------------------------------------------------------
# Rate limiters
# ---------------------------------------------------------------------------

# Per-admin cap on keep/unkeep toggles.  These are cheap DB writes with no
# downstream Arr round-trip, so a generous burst cap is fine: 60 per minute
# / 500 per day per actor.
_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)

# Per-admin cap on delete triggers.  Each call initiates an Arr delete which
# may trigger a rename/move on disk; tighter than keep: 20 per minute /
# 300 per day per actor.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=300,
)

# Per-admin cap on redownload triggers.  Each call spawns an Arr lookup +
# add_movie/add_series round-trip, so a tighter burst cap than the search
# path: 20 per minute / 200 per day per actor.
_REDOWNLOAD_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=200,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/library")
def api_library(
    q: str = "",
    type: str = "",
    sort: str = "added_desc",
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Return paginated library items as JSON."""
    conn = get_db()
    sort = sort if sort in _VALID_SORTS else "added_desc"
    media_type = type if type in _VALID_TYPES else ""

    items, total = fetch_library(
        conn, q=q, media_type=media_type, sort=sort, page=page, per_page=per_page
    )
    total_pages = max(1, (total + per_page - 1) // per_page)

    return JSONResponse(
        {
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    )


@router.post("/api/media/{media_id}/keep")
def api_media_keep(
    media_id: str,
    duration: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply protection to a media item."""
    if not _KEEP_LIMITER.check(username):
        logger.warning("media.keep_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many keep operations — slow down"
        )

    conn = get_db()

    if duration not in VALID_KEEP_DURATIONS:
        return respond_err("invalid_duration", status=400)

    row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return respond_err("not_found", status=404)

    now = datetime.now(UTC)

    if duration == "forever":
        action = ACTION_PROTECTED_FOREVER
        execute_at = None
        snooze_label = "forever"
    else:
        days = VALID_KEEP_DURATIONS[duration]
        assert days is not None  # only "forever" maps to None and is handled above
        action = ACTION_SNOOZED
        execute_at = (now + timedelta(days=int(days))).isoformat()
        snooze_label = duration

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
            (media_id,),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE scheduled_actions "
                "SET action=?, execute_at=?, snoozed_at=?, snooze_duration=?, token_used=0 "
                "WHERE id=?",
                (action, execute_at, now.isoformat(), snooze_label, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, execute_at, token, token_used, "
                "snoozed_at, snooze_duration) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    media_id,
                    action,
                    now.isoformat(),
                    execute_at,
                    secrets.token_urlsafe(32),
                    now.isoformat(),
                    snooze_label,
                ),
            )
    except Exception:
        conn.execute("ROLLBACK")
        raise

    log_audit(
        conn,
        media_id,
        "snoozed",
        f"Kept for {snooze_label} by admin ({username})",
        actor=username,
    )

    conn.commit()
    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return respond_ok({"id": media_id, "duration": snooze_label})


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr.

    Two-phase, three-transaction layout — intentionally split:

    1. **Snapshot transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — read the media row, capture identifiers, release the lock.
    2. **External Arr call** (no DB transaction held)
       — Radarr / Sonarr round-trip can take seconds; holding a SQLite
       write lock that long would block every other writer in the process.
       A delete-intent row is persisted *before* this step so a crash
       between the Arr call returning and the DB cleanup landing can be
       reconciled by :func:`reconcile_pending_delete_intents` at startup.
    3. **Cleanup transaction** (``BEGIN IMMEDIATE`` … ``COMMIT``)
       — write the audit row, prune ``scheduled_actions``, drop the
       ``media_items`` row, then mark the delete-intent complete.
    """
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return respond_err(
            "too_many_requests", status=429, message="Too many delete operations — slow down"
        )
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, title, media_type, file_path, file_size_bytes, radarr_id, sonarr_id, season_number, plex_rating_key "
            "FROM media_items WHERE id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            # Do NOT leak existence/non-existence: returning 404 tells an
            # attacker which media IDs are valid.  With auth already confirmed,
            # an unknown id is treated as forbidden access.
            return respond_err("forbidden", status=403)
        snapshot = {
            "title": row["title"],
            "media_type": row["media_type"],
            "file_path": row["file_path"],
            "file_size_bytes": row["file_size_bytes"],
            "radarr_id": row["radarr_id"],
            "sonarr_id": row["sonarr_id"],
            "season_number": row["season_number"],
            "plex_rating_key": row["plex_rating_key"],
        }
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise

    title = snapshot["title"]
    config = request.app.state.config
    is_movie = snapshot["media_type"] == "movie"

    def _is_already_gone(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status == 404

    # Tracks the row id of the delete-intent persisted before any external
    # call.  None means there is no intent to finalise (the no-Arr-id path
    # skips writing one).
    intent_id: int | None = None

    if is_movie:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            radarr_id = snapshot["radarr_id"]
            if radarr_id:
                intent_id = _record_delete_intent(conn, media_id, "radarr", radarr_id)
                try:
                    client.delete_movie(radarr_id)
                    logger.info(
                        "Deleted '%s' via Radarr (id %s, with files + exclusion)", title, radarr_id
                    )
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Radarr reports id %s already gone for '%s' — idempotent delete",
                            radarr_id,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.warning(
                            "Radarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return respond_err(
                            "upstream_delete_failed",
                            status=502,
                            message="Upstream Radarr delete failed — DB row preserved",
                        )
            else:
                logger.info("No stored radarr_id for '%s' — skipping Radarr-level delete.", title)
    else:
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            sid = snapshot["sonarr_id"]
            season_num = snapshot["season_number"]
            if sid and season_num is not None:
                intent_id = _record_delete_intent(conn, media_id, "sonarr", sid)
                try:
                    sonarr_client.delete_episode_files(sid, season_num)
                    sonarr_client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    if not sonarr_client.has_remaining_files(sid):
                        sonarr_client.delete_series(sid)
                        logger.info(
                            "No files remain for '%s' — deleted series from Sonarr with exclusion",
                            title,
                        )
                except Exception as exc:
                    if _is_already_gone(exc):
                        logger.info(
                            "Sonarr reports id %s already gone for '%s' — idempotent delete",
                            sid,
                            title,
                        )
                    else:
                        _fail_delete_intent(conn, intent_id, str(exc))
                        logger.warning(
                            "Sonarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return respond_err(
                            "upstream_delete_failed",
                            status=502,
                            message="Upstream Sonarr delete failed — DB row preserved",
                        )

    rk = snapshot["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(
            conn,
            media_id,
            "deleted",
            detail,
            space_bytes=snapshot["file_size_bytes"],
            actor=username,
        )
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise

    if intent_id is not None:
        _complete_delete_intent(conn, intent_id)
    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot["file_path"], username)
    return respond_ok({"id": media_id})


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: _RedownloadRequest,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item via Radarr or Sonarr."""
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
        client = build_radarr_from_db(conn, config.secret_key)
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
                    resolved_title = str(entry.get("title") or title)
                    resolved_tmdb_int = int(str(resolved_tmdb))
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
        sonarr_client = build_sonarr_from_db(conn, config.secret_key)
        if sonarr_client:
            results = sonarr_client.lookup_by_term(
                _url_quote(title), endpoint="/api/v3/series/lookup"
            )
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


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

__all__ = [
    "_DELETE_LIMITER",
    "_KEEP_LIMITER",
    "_REDOWNLOAD_LIMITER",
    "_REDOWNLOAD_TITLE_SIMILARITY",
    "_RedownloadRequest",
    "_complete_delete_intent",
    "_fail_delete_intent",
    "_pick_lookup_match",
    "_record_delete_intent",
    "_redownload_audit_id",
    "api_library",
    "api_media_delete",
    "api_media_keep",
    "api_media_redownload",
    "build_radarr_from_db",
    "build_sonarr_from_db",
    "reconcile_pending_delete_intents",
    "router",
]
