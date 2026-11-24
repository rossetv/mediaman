"""Protected items page and API endpoints.

Handles items shielded from deletion: protected_forever or snoozed with a future execute_at.
The show-level keep routes live in mediaman.web.routes.kept_show, whose sub-router is
mounted here; _resolve_show_rating_key is re-exported so tests can import it from here.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from mediaman.core.audit import log_audit
from mediaman.core.format import days_ago, media_type_badge, normalise_media_type
from mediaman.core.format import format_bytes as _format_bytes
from mediaman.core.time import now_iso
from mediaman.db import get_db
from mediaman.scanner.repository import is_show_kept_pure
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import format_expiry
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.middleware import is_admin as _is_admin
from mediaman.web.models import ACTION_PROTECTED_FOREVER
from mediaman.web.repository.kept import (
    delete_protections,
    fetch_protected_items,
    fetch_seasons_for_show,
    fetch_show_kept_status,
    fetch_show_title,
)
from mediaman.web.repository.kept import (
    resolve_show_rating_key as _resolve_show_rating_key,  # noqa: F401 — re-exported so tests can import it
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.kept_show import router as _kept_show_router

_UNPROTECT_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(_kept_show_router)


def _fetch_protected(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    now = now_iso()
    rows = fetch_protected_items(conn, now)

    forever = []
    snoozed = []
    for r in rows:
        media_type = r.media_type
        badge_class, type_label = media_type_badge(media_type)
        if normalise_media_type(media_type) != "movie" and r.season_number:
            type_label = f"{type_label} · S{r.season_number}"

        item = {
            "sa_id": r.sa_id,
            "media_item_id": r.media_item_id,
            "title": r.title,
            "plex_rating_key": r.plex_rating_key,
            "badge_class": badge_class,
            "type_label": type_label,
            "action": r.action,
            "expiry": format_expiry(r.action, r.execute_at),
            "snooze_duration": r.snooze_duration,
            "file_size": _format_bytes(r.file_size_bytes),
        }

        if r.action == ACTION_PROTECTED_FOREVER:
            forever.append(item)
        else:
            snoozed.append(item)

    return forever, snoozed


@router.get("/kept")
def redirect_kept_to_library(request: Request) -> RedirectResponse:
    """Redirect /kept to the Watch list's Kept filter, auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/watch?view=list&type=kept", status_code=301)


@router.get("/kept/page")
def redirect_kept_page(request: Request) -> RedirectResponse:
    """Redirect legacy /kept/page to the Watch list's Kept filter, auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/watch?view=list&type=kept", status_code=301)


@router.get("/api/kept")
def api_protected(username: str = Depends(get_current_admin)) -> JSONResponse:
    conn = get_db()
    forever, snoozed = _fetch_protected(conn)
    return JSONResponse({"forever": forever, "snoozed": snoozed})


@router.post("/api/media/{media_item_id}/unprotect")
def api_unprotect(media_item_id: str, username: str = Depends(get_current_admin)) -> JSONResponse:
    if not _UNPROTECT_LIMITER.check(username):
        logger.warning("media.unprotect_throttled user=%s", _sanitise_log_field(username))
        return respond_err(
            "too_many_requests", status=429, message="Too many unprotect operations -- slow down"
        )

    conn = get_db()
    with conn:
        removed = delete_protections(conn, media_item_id)
        if removed:
            log_audit(
                conn,
                media_item_id,
                "unprotected",
                f"Protection removed by {username} ({removed} keep{'s' if removed != 1 else ''})",
                actor=username,
            )

    if not removed:
        return respond_err("not_found", status=404, message="No active protection found")

    logger.info("Unprotected media_item_id=%s by %s", media_item_id, _sanitise_log_field(username))
    return respond_ok()


# The key may be a show title (shows with no rating key), and a "/" in it
# arrives decoded, so the parameter must be allowed to span segments.
@router.get("/api/show/{show_rating_key:path}/seasons")
def api_show_seasons(
    show_rating_key: str, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    conn = get_db()
    season_rows = fetch_seasons_for_show(conn, show_rating_key)

    show_title = ""
    if season_rows:
        # A show with no rating key is grouped, and so looked up, by its title.
        show_title = fetch_show_title(conn, show_rating_key) or show_rating_key

    seasons = []
    for r in season_rows:
        lw = r.last_watched_at
        last_watched = days_ago(lw) or None

        seasons.append(
            {
                "id": r.id,
                "season_number": r.season_number,
                "title": r.title,
                "kept": r.kept,
                "file_size": _format_bytes(r.file_size_bytes),
                "file_size_bytes": r.file_size_bytes,
                "last_watched": last_watched,
            }
        )

    # A lapsed show snooze is not a keep until the next scan sweeps its row.
    show_kept_row = (
        fetch_show_kept_status(conn, show_rating_key)
        if is_show_kept_pure(conn, show_rating_key)
        else None
    )

    return JSONResponse(
        {
            "show_title": show_title,
            "show_rating_key": show_rating_key,
            "show_kept": {
                "action": show_kept_row.action,
                "execute_at": show_kept_row.execute_at,
            }
            if show_kept_row
            else None,
            "seasons": seasons,
        }
    )


@router.get("/protected")
def redirect_protected_page(request: Request) -> RedirectResponse:
    """Redirect old /protected URL to the Watch list's Kept filter, auth-gated."""
    if not _is_admin(request):
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/watch?view=list&type=kept", status_code=301)
