"""Protected items page and API endpoints.

Handles items shielded from deletion: those marked protected_forever or
snoozed with a future execute_at. Provides page render, JSON listing,
and unprotect action.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from mediaman.auth.audit import log_audit
from mediaman.auth.middleware import get_current_admin
from mediaman.db import get_db
from mediaman.models import ACTION_PROTECTED_FOREVER, ACTION_SNOOZED, VALID_KEEP_DURATIONS
from mediaman.services.format import format_bytes as _format_bytes
from mediaman.services.format import media_type_badge
from mediaman.services.time import now_iso
from mediaman.web.routes._helpers import is_admin as _is_admin


def _resolve_show_rating_key(conn, supplied_key: str) -> tuple[str | None, str | None]:
    """Return ``(resolved_key, error)`` for a keep-show request.

    IDOR risk closed by this helper: the previous implementation fell
    back to matching seasons by ``show_title`` whenever the supplied
    rating key was missing on the stored rows. Two distinct shows
    sharing a title (a common case — remakes, international versions,
    generic one-word titles) collided in that branch so user A keeping
    ``Kingdom`` would also match user B's ``Kingdom`` rows.

    Resolution rules:
      (a) ``supplied_key`` is present and at least one media_items row
          carries that exact ``show_rating_key`` → use the supplied key.
      (b) ``supplied_key`` is missing (empty) but one, and only one,
          show exists matching by ``show_title`` across the whole
          library → use the rating key from that single row. The unique
          match guarantee is what keeps the IDOR shut.
      (c) anything else (no supplied key + zero or >1 title matches,
          or supplied key that doesn't map to any stored row) → return
          ``(None, error_message)`` so the caller can 409.

    ``supplied_key`` is the raw path parameter. Callers pass it through
    unchanged — never synthesised from ``show_title``.
    """
    key = (supplied_key or "").strip()
    if key:
        row = conn.execute(
            "SELECT 1 FROM media_items WHERE show_rating_key = ? LIMIT 1",
            (key,),
        ).fetchone()
        if row is not None:
            return key, None
        return None, "Unknown show_rating_key"
    return None, "show_rating_key required"


class _KeepShowBody(BaseModel):
    """Body shape for POST /api/show/{show_rating_key}/keep."""

    duration: str = "forever"
    season_ids: list[str] = []

logger = logging.getLogger("mediaman")

router = APIRouter()


def _format_expiry(action: str, execute_at: str | None) -> str:
    """Return a human-readable expiry string for a protected item."""
    if action == ACTION_PROTECTED_FOREVER:
        return "Forever"
    if not execute_at:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(execute_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (dt - now).days
        if delta <= 0:
            return "Expires today"
        if delta == 1:
            return "Expires tomorrow"
        return f"Expires in {delta} days"
    except (ValueError, TypeError):
        return "Unknown"


def _fetch_protected(conn) -> tuple[list[dict], list[dict]]:
    """Return (forever_items, snoozed_items) from scheduled_actions joined with media_items."""
    now = now_iso()

    rows = conn.execute("""
        SELECT
            sa.id          AS sa_id,
            sa.media_item_id,
            sa.action,
            sa.execute_at,
            sa.snooze_duration,
            mi.title,
            mi.media_type,
            mi.show_title,
            mi.season_number,
            mi.plex_rating_key,
            mi.file_size_bytes
        FROM scheduled_actions sa
        JOIN media_items mi ON mi.id = sa.media_item_id
        WHERE sa.action = 'protected_forever'
           OR (sa.action = 'snoozed' AND sa.execute_at > ?)
        ORDER BY sa.action DESC, sa.execute_at ASC
    """, (now,)).fetchall()

    forever = []
    snoozed = []
    for r in rows:
        media_type = r["media_type"] or "movie"
        badge_class, type_label = media_type_badge(media_type)
        if media_type in ("tv", "anime") and r["season_number"]:
            type_label = f"{type_label} · S{r['season_number']}"

        item = {
            "sa_id": r["sa_id"],
            "media_item_id": r["media_item_id"],
            "title": r["title"],
            "plex_rating_key": r["plex_rating_key"],
            "badge_class": badge_class,
            "type_label": type_label,
            "action": r["action"],
            "expiry": _format_expiry(r["action"], r["execute_at"]),
            "snooze_duration": r["snooze_duration"] or "",
            "file_size": _format_bytes(r["file_size_bytes"] or 0),
        }

        if r["action"] == ACTION_PROTECTED_FOREVER:
            forever.append(item)
        else:
            snoozed.append(item)

    return forever, snoozed


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------

@router.get("/kept")
def redirect_kept_to_library(request: Request) -> RedirectResponse:
    """Redirect /kept to the library Kept filter — auth-gated.

    Gated so an unauthenticated caller can't use the 301 target to
    enumerate internal URL structure; unauth callers just see the
    login redirect.
    """
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)


@router.get("/kept/page")
def redirect_kept_page(request: Request) -> RedirectResponse:
    """Redirect legacy /kept/page to /library?type=kept — auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)


# ---------------------------------------------------------------------------
# JSON API endpoints
# ---------------------------------------------------------------------------

@router.get("/api/kept")
def api_protected(username: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all actively kept items as JSON."""
    conn = get_db()
    forever, snoozed = _fetch_protected(conn)
    return JSONResponse({"forever": forever, "snoozed": snoozed})


@router.post("/api/media/{media_item_id}/unprotect")
def api_unprotect(media_item_id: str, username: str = Depends(get_current_admin)) -> JSONResponse:
    """Remove protection from a media item.

    Deletes the scheduled_actions entry (protected_forever or snoozed) and
    logs the action to audit_log.
    """
    conn = get_db()

    # Pick the most-recent protection row — avoids targeting a stale
    # snooze when a newer protect/snooze has already been applied.
    row = conn.execute(
        "SELECT id FROM scheduled_actions "
        "WHERE media_item_id = ? AND action IN ('protected_forever', 'snoozed') "
        "ORDER BY id DESC LIMIT 1",
        (media_item_id,),
    ).fetchone()

    if row is None:
        return JSONResponse({"error": "No active protection found"}, status_code=404)

    conn.execute("DELETE FROM scheduled_actions WHERE id = ?", (row["id"],))
    log_audit(conn, media_item_id, "unprotected", "Protection removed by admin")
    conn.commit()

    logger.info("Unprotected media_item_id=%s by %s", media_item_id, username)
    return JSONResponse({"ok": True})


@router.get("/api/show/{show_rating_key}/seasons")
def api_show_seasons(show_rating_key: str, request: Request, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Return all seasons of a show for the keep dialog season picker.

    Looks up by show_rating_key first. If that yields no results (column
    not yet populated by a scan), falls back to matching by show_title
    via the ``title`` query parameter.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, show_title, season_number, file_size_bytes, last_watched_at "
        "FROM media_items "
        "WHERE show_rating_key = ? ORDER BY season_number ASC",
        (show_rating_key,),
    ).fetchall()

    # Fallback: match by show_title if show_rating_key isn't populated yet
    if not rows:
        fallback_title = request.query_params.get("title", "")
        if fallback_title:
            rows = conn.execute(
                "SELECT id, title, show_title, season_number, file_size_bytes, last_watched_at "
                "FROM media_items "
                "WHERE show_title = ? AND media_type IN ('tv_season', 'anime_season', 'season') "
                "ORDER BY season_number ASC",
                (fallback_title,),
            ).fetchall()

    show_title = rows[0]["show_title"] if rows else ""

    seasons = []
    for r in rows:
        kept_row = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? "
            "AND action IN ('protected_forever', 'snoozed') AND token_used = 0",
            (r["id"],),
        ).fetchone()

        # Format last watched for display
        lw = r["last_watched_at"]
        last_watched = None
        if lw:
            try:
                lw_dt = datetime.fromisoformat(str(lw))
                if lw_dt.tzinfo is None:
                    lw_dt = lw_dt.replace(tzinfo=timezone.utc)
                delta = (datetime.now(timezone.utc) - lw_dt).days
                if delta == 0:
                    last_watched = "today"
                elif delta == 1:
                    last_watched = "yesterday"
                else:
                    last_watched = f"{delta} days ago"
            except (ValueError, TypeError):
                pass

        size_bytes = r["file_size_bytes"] or 0
        seasons.append({
            "id": r["id"],
            "season_number": r["season_number"],
            "title": r["title"],
            "kept": kept_row is not None,
            "file_size": _format_bytes(size_bytes),
            "file_size_bytes": size_bytes,
            "last_watched": last_watched,
        })

    show_kept = conn.execute(
        "SELECT action, execute_at FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()

    return JSONResponse({
        "show_title": show_title,
        "show_rating_key": show_rating_key,
        "show_kept": dict(show_kept) if show_kept else None,
        "seasons": seasons,
    })


@router.post("/api/show/{show_rating_key}/keep")
def api_keep_show(
    show_rating_key: str,
    body: _KeepShowBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Keep an entire show (all listed seasons + future seasons via kept_shows rule)."""
    conn = get_db()
    duration = body.duration
    season_ids = body.season_ids

    if not season_ids:
        return JSONResponse({"ok": False, "error": "No seasons selected"}, status_code=400)

    if duration not in VALID_KEEP_DURATIONS:
        return JSONResponse({"ok": False, "error": "Invalid duration"}, status_code=400)

    # Guard against IDOR — every season_id must actually belong to this
    # show. We used to fall back to matching by ``show_title`` when the
    # stored ``show_rating_key`` was NULL. That opened a collision path
    # for two distinct shows sharing a title. Now we strictly match on
    # ``show_rating_key`` and log any attempt that would have needed
    # the old fallback, so operators can spot unscanned rows in the
    # wild and backfill them.
    resolved_key, err = _resolve_show_rating_key(conn, show_rating_key)
    if err or not resolved_key:
        logger.warning(
            "keep_show.rating_key_unresolved supplied=%r user=%s err=%s",
            show_rating_key, admin, err,
        )
        return JSONResponse({"ok": False, "error": err or "Unknown show"}, status_code=409)

    placeholders = ",".join("?" * len(season_ids))
    owned = conn.execute(
        f"SELECT id FROM media_items WHERE id IN ({placeholders}) "  # noqa: S608 — placeholders are '?' only, not user input
        f"AND show_rating_key = ?",
        tuple(season_ids) + (resolved_key,),
    ).fetchall()
    owned_ids = {r["id"] for r in owned}
    if owned_ids != set(season_ids):
        # Note any season whose show_rating_key is unpopulated — if
        # that's the cause, a scan backfill is the correct remedy.
        missing = set(season_ids) - owned_ids
        if missing:
            unkeyed = conn.execute(
                f"SELECT id FROM media_items WHERE id IN ({','.join('?' * len(missing))}) "
                f"AND (show_rating_key IS NULL OR show_rating_key = '')",
                tuple(missing),
            ).fetchall()
            if unkeyed:
                logger.warning(
                    "keep_show.fallback_would_have_triggered user=%s "
                    "show_rating_key=%s unkeyed_ids=%s",
                    admin, resolved_key, [r["id"] for r in unkeyed],
                )
        return JSONResponse({"ok": False, "error": "Seasons do not belong to this show"}, status_code=400)

    days = VALID_KEEP_DURATIONS.get(duration)

    now = datetime.now(timezone.utc)
    if duration == "forever":
        action = ACTION_PROTECTED_FOREVER
        execute_at = None
    else:
        action = ACTION_SNOOZED
        execute_at = (now + timedelta(days=days)).isoformat() if days else None

    title_row = conn.execute(
        "SELECT show_title FROM media_items WHERE show_rating_key = ? LIMIT 1",
        (show_rating_key,),
    ).fetchone()
    show_title = title_row["show_title"] if title_row else "Unknown"

    conn.execute(
        "INSERT INTO kept_shows (show_rating_key, show_title, action, execute_at, snooze_duration, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(show_rating_key) DO UPDATE SET action=excluded.action, "
        "execute_at=excluded.execute_at, snooze_duration=excluded.snooze_duration",
        (show_rating_key, show_title, action, execute_at, duration, now.isoformat()),
    )

    for sid in season_ids:
        existing = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id = ? AND token_used = 0",
            (sid,),
        ).fetchone()
        token = secrets.token_urlsafe(32)
        if existing:
            conn.execute(
                "UPDATE scheduled_actions SET action = ?, execute_at = ?, "
                "snoozed_at = ?, snooze_duration = ? WHERE id = ?",
                (action, execute_at, now.isoformat(), duration, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO scheduled_actions "
                "(media_item_id, action, scheduled_at, execute_at, token, token_used, snoozed_at, snooze_duration) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (sid, action, now.isoformat(), execute_at, token, now.isoformat(), duration),
            )

    log_audit(conn, show_rating_key, "kept_show", f"Show '{show_title}' kept ({duration}) by {admin}")
    conn.commit()

    logger.info("Kept show %s (%s) — %s by %s", show_rating_key, show_title, duration, admin)
    return JSONResponse({"ok": True})


@router.post("/api/show/{show_rating_key}/remove")
def api_remove_show_keep(show_rating_key: str, admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Remove a show-level keep rule. Individual season keeps are not affected."""
    conn = get_db()
    row = conn.execute(
        "SELECT id, show_title FROM kept_shows WHERE show_rating_key = ?",
        (show_rating_key,),
    ).fetchone()

    if row is None:
        return JSONResponse({"ok": False, "error": "No show-level keep found"}, status_code=404)

    conn.execute("DELETE FROM kept_shows WHERE id = ?", (row["id"],))
    log_audit(conn, show_rating_key, "removed_show_keep", f"Show keep removed for '{row['show_title']}' by {admin}")
    conn.commit()

    logger.info("Removed show keep for %s by %s", show_rating_key, admin)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Legacy redirect — bookmarks and external links land on the library view.
# ---------------------------------------------------------------------------

@router.get("/protected")
def redirect_protected_page(request: Request) -> RedirectResponse:
    """Redirect old /protected URL to library kept filter — auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/library?type=kept", status_code=301)
