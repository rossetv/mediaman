"""Library JSON API endpoints."""

from __future__ import annotations

import difflib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Body, Depends, Form, Query, Request
from fastapi.responses import JSONResponse

from mediaman.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.auth.rate_limit import ActionRateLimiter
from mediaman.db import get_db
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.downloads.notifications import record_download_notification
from mediaman.services.infra.http_client import SafeHTTPError
from mediaman.web.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS

from ._query import _VALID_SORTS, _VALID_TYPES, fetch_library

logger = logging.getLogger("mediaman")

router = APIRouter()

# Per-admin cap on media deletes.
_DELETE_LIMITER = ActionRateLimiter(
    max_in_window=20,
    window_seconds=60,
    max_per_day=300,
)

# Per-admin cap on keep/snooze actions.
_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


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


@router.post("/api/media/{media_id}/delete")
def api_media_delete(
    media_id: str,
    request: Request,
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Delete a media item via Radarr/Sonarr."""
    if not _DELETE_LIMITER.check(username):
        logger.warning("media.delete_throttled user=%s", username)
        return JSONResponse(
            {"error": "Too many delete operations — slow down"},
            status_code=429,
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
            return JSONResponse({"error": "Not found"}, status_code=404)
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
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    title = snapshot["title"]
    config = request.app.state.config
    is_movie = snapshot["media_type"] == "movie"

    def _is_already_gone(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
        return status == 404

    if is_movie:
        client = build_radarr_from_db(conn, config.secret_key)
        if client:
            radarr_id = snapshot["radarr_id"]
            if radarr_id:
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
                        logger.warning(
                            "Radarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "Upstream Radarr delete failed — DB row preserved",
                            },
                            status_code=502,
                        )
            else:
                logger.info(
                    "No stored radarr_id for '%s' — skipping Radarr-level delete.",
                    title,
                )
    else:
        client = build_sonarr_from_db(conn, config.secret_key)
        if client:
            sid = snapshot["sonarr_id"]
            season_num = snapshot["season_number"]
            if sid and season_num is not None:
                try:
                    client.delete_episode_files(sid, season_num)
                    client.unmonitor_season(sid, season_num)
                    logger.info("Deleted season files for '%s' S%s via Sonarr", title, season_num)
                    if not client.has_remaining_files(sid):
                        client.delete_series(sid)
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
                        logger.warning(
                            "Sonarr delete failed for '%s': %s", title, exc, exc_info=True
                        )
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": "Upstream Sonarr delete failed — DB row preserved",
                            },
                            status_code=502,
                        )

    rk = snapshot["plex_rating_key"] or ""
    detail = f"Deleted '{title}' by {username}"
    if rk:
        detail += f" [rk:{rk}]"
    try:
        conn.execute("BEGIN IMMEDIATE")
        log_audit(conn, media_id, "deleted", detail, space_bytes=snapshot["file_size_bytes"])
        conn.execute("DELETE FROM scheduled_actions WHERE media_item_id = ?", (media_id,))
        conn.execute("DELETE FROM media_items WHERE id = ?", (media_id,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    logger.info("Deleted %s (%s) — %s by %s", media_id, title, snapshot["file_path"], username)
    return JSONResponse({"ok": True, "id": media_id})


@router.post("/api/media/{media_id}/keep")
def api_media_keep(
    media_id: str,
    duration: str = Form(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Apply protection to a media item."""
    if not _KEEP_LIMITER.check(username):
        logger.warning("media.keep_throttled user=%s", username)
        return JSONResponse(
            {"error": "Too many keep operations — slow down"},
            status_code=429,
        )

    conn = get_db()

    if duration not in VALID_KEEP_DURATIONS:
        return JSONResponse({"error": "Invalid duration"}, status_code=400)

    row = conn.execute("SELECT id FROM media_items WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    now = datetime.now(timezone.utc)

    if duration == "forever":
        action = ACTION_PROTECTED_FOREVER
        execute_at = None
        snooze_label = "forever"
    else:
        days = VALID_KEEP_DURATIONS[duration]
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

    log_audit(conn, media_id, "snoozed", f"Kept for {snooze_label} by admin ({username})")

    conn.commit()
    logger.info("Media item %s protected for %s by %s", media_id, snooze_label, username)

    return JSONResponse({"ok": True, "id": media_id, "duration": snooze_label})


# Minimum title similarity accepted for a title+year fuzzy match.
_REDOWNLOAD_TITLE_SIMILARITY = 0.9


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


@router.post("/api/media/redownload")
def api_media_redownload(
    request: Request,
    body: dict = Body(...),
    username: str = Depends(get_current_admin),
) -> JSONResponse:
    """Re-download a deleted media item."""
    title = str(body.get("title") or "").strip()[:256]
    year_raw = body.get("year")
    try:
        year = int(year_raw) if year_raw not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    tmdb_id = body.get("tmdb_id")
    tvdb_id = body.get("tvdb_id")
    imdb_id = body.get("imdb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_id = None
    try:
        tvdb_id = int(tvdb_id) if tvdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tvdb_id = None
    if imdb_id is not None:
        imdb_id = str(imdb_id).strip() or None

    if tmdb_id is None and tvdb_id is None and not imdb_id:
        if not title or year is None:
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
                    resolved_title = entry.get("title") or title
                    client.add_movie(resolved_tmdb, resolved_title)
                    log_audit(conn, resolved_title, "re_downloaded", f"Re-downloaded by {username}")
                    record_download_notification(
                        conn,
                        email=username,
                        title=resolved_title,
                        media_type="movie",
                        tmdb_id=resolved_tmdb,
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
        client = build_sonarr_from_db(conn, config.secret_key)
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
                    client.add_series(resolved_tvdb, resolved_title)
                    resolved_tmdb_sonarr = entry.get("tmdbId")
                    log_audit(conn, resolved_title, "re_downloaded", f"Re-downloaded by {username}")
                    record_download_notification(
                        conn,
                        email=username,
                        title=resolved_title,
                        media_type="tv",
                        tmdb_id=resolved_tmdb_sonarr,
                        tvdb_id=resolved_tvdb,
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
