"""Show-level keep machinery: keep or remove a whole show's seasons.

Owns the two
show-keep routes, their body schema, the rating-key resolution that closes the IDOR risk,
the season-ownership check and the season action-row builder. The kept package mounts this
router and re-exports _resolve_show_rating_key so tests can import it from there.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mediaman.core.audit import log_audit
from mediaman.core.time import now_utc, parse_iso_strict_utc
from mediaman.db import get_db
from mediaman.scanner.repository import delete_show_scheduled_deletions
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.scheduled_actions import resolve_keep_decision
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.models import ACTION_PROTECTED_FOREVER, VALID_KEEP_DURATIONS
from mediaman.web.repository.kept import (
    KeepRow,
    SeasonRow,
    delete_kept_show,
    delete_protections,
    fetch_existing_actions_for_seasons,
    fetch_owned_season_ids,
    fetch_seasons_for_show,
    fetch_show_keep_row,
    fetch_show_kept_status,
    fetch_show_title,
    fetch_unkeyed_media_ids,
    resolve_show_rating_key,
    same_keep,
    set_protected_state,
    upsert_kept_show,
)
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()

_REMOVE_SHOW_KEEP_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)
_KEEP_SHOW_LIMITER = ActionRateLimiter(
    max_in_window=60,
    window_seconds=60,
    max_per_day=500,
)


_DELETION_RUNNING_MESSAGE = (
    "Kept, but a season of this show was already being deleted. Its files may "
    "already be gone; that season stays kept only if the deletion fails."
)


class KeepRead(BaseModel):
    """A keep as the person changing it read it."""

    action: str
    execute_at: str | None = None
    kept_at: str | None = None

    def row(self) -> KeepRow:
        return KeepRow(self.action, self.execute_at, self.kept_at)


class _ShowRead(BaseModel):
    """The keeps of a show and of every one of its seasons as the person read them."""

    show: KeepRead | None
    seasons: dict[str, KeepRead | None] = Field(max_length=500)


class _KeepShowBody(BaseModel):
    duration: str = "forever"
    season_ids: list[str] = Field(default_factory=list, max_length=50)
    read: _ShowRead | None = None


class _RemoveShowKeepBody(BaseModel):
    read: _ShowRead | None = None
    drops: list[str] | None = Field(default=None, max_length=500)


def keep_changed() -> JSONResponse:
    return respond_err(
        "keep_changed",
        status=409,
        message="This keep was changed after you opened it, so it was left as it is.",
    )


def _still_as_read(
    conn: sqlite3.Connection, show_rating_key: str, read: _ShowRead | None
) -> list[SeasonRow] | None:
    """The show's seasons; None when one, its keep or the show's keep is not as *read*."""
    seasons = fetch_seasons_for_show(conn, show_rating_key)
    if read is None:
        return seasons
    kept = {s.id: s.keep for s in seasons}
    if read.seasons.keys() != kept.keys():
        return None
    pairs = [(read.show, fetch_show_kept_status(conn, show_rating_key))]
    pairs += [(was, kept[season_id]) for season_id, was in read.seasons.items()]
    unchanged = all(same_keep(was.row() if was else None, keep) for was, keep in pairs)
    return seasons if unchanged else None


_NEVER = datetime.max.replace(tzinfo=UTC)
_ENDED = datetime.min.replace(tzinfo=UTC)


def _ends(keep: KeepRow | None, unreadable: datetime) -> datetime:
    """When a keep ends: *unreadable* when its date cannot be read."""
    if keep is None:
        return _ENDED
    if keep.action == ACTION_PROTECTED_FOREVER:
        return _NEVER
    return parse_iso_strict_utc(keep.execute_at) or unreadable


def _hold_duration(show_keep: KeepRow, now: datetime) -> str:
    """The shortest keep that lasts at least as long as *show_keep*."""
    ends = _ends(show_keep, _NEVER)
    for duration, days in VALID_KEEP_DURATIONS.items():
        if days is not None and ends <= now + timedelta(days=days):
            return duration
    return "forever"


def _hold_seasons(
    conn: sqlite3.Connection, held: list[str], show_keep: KeepRow, now: datetime, admin: str
) -> bool:
    """Keep the seasons of a show whose keep ends at least as long as it would have lasted.

    Returns True when the deletion pass is removing one of them right now.
    """
    duration = _hold_duration(show_keep, now)
    decision = resolve_keep_decision(duration, days=VALID_KEEP_DURATIONS[duration], now=now)
    to_update, to_insert, to_delete = _build_season_action_rows(
        held,
        fetch_existing_actions_for_seasons(conn, held),
        action=decision.action,
        execute_at=decision.execute_at,
        duration=duration,
        now_iso_str=now.isoformat(),
    )
    for season_id in held:
        log_audit(
            conn,
            season_id,
            "kept" if decision.action == ACTION_PROTECTED_FOREVER else "snoozed",
            f"Kept for {duration} by {admin} as the show keep ended",
            actor=admin,
        )
    return set_protected_state(conn, to_update=to_update, to_insert=to_insert, to_delete=to_delete)


def _validate_keep_show_input(
    conn: sqlite3.Connection,
    supplied_key: str,
    duration: str,
    season_ids: list[str],
    admin: str,
) -> tuple[str | None, JSONResponse | None]:
    if not season_ids:
        return None, respond_err("no_seasons", status=400, message="No seasons selected")
    if duration not in VALID_KEEP_DURATIONS:
        return None, respond_err("invalid_duration", status=400)
    resolved_key, err = resolve_show_rating_key(conn, supplied_key)
    if err or not resolved_key:
        logger.warning(
            "keep_show.rating_key_unresolved supplied=%s user=%s err=%s",
            _sanitise_log_field(supplied_key),
            _sanitise_log_field(admin),
            err,
        )
        return None, respond_err(err or "unknown_show", status=409, message=err)
    return resolved_key, None


def _check_season_ownership(
    conn: sqlite3.Connection,
    season_ids: list[str],
    resolved_key: str,
    admin: str,
) -> JSONResponse | None:
    """Confirm every season in season_ids belongs to resolved_key.

    Returns None on success or a 400 envelope when any season is unowned. Logs a warning
    when the unowned IDs lack a show_rating_key: seasons without one were once silently
    accepted, and the log flags regressions.
    """
    owned_ids = fetch_owned_season_ids(conn, season_ids, resolved_key)
    if owned_ids == set(season_ids):
        return None
    missing = set(season_ids) - owned_ids
    if missing:
        unkeyed_ids = fetch_unkeyed_media_ids(conn, missing)
        if unkeyed_ids:
            logger.warning(
                "keep_show.fallback_would_have_triggered user=%s show_rating_key=%s unkeyed_ids=%s",
                _sanitise_log_field(admin),
                _sanitise_log_field(resolved_key),
                unkeyed_ids,
            )
    return respond_err(
        "seasons_not_owned", status=400, message="Seasons do not belong to this show"
    )


def _build_season_action_rows(
    season_ids: list[str],
    existing_by_season: dict[str, list[int]],
    *,
    action: str,
    execute_at: str | None,
    duration: str,
    now_iso_str: str,
) -> tuple[
    list[tuple[str, str | None, str, str, int]],
    list[tuple[str, str, str, str | None, str, str, str]],
    list[int],
]:
    """Partition season IDs into (to_update, to_insert, to_delete) as set_protected_state
    expects: a season's first existing row becomes the keep and its other rows go.
    """
    to_update = [
        (action, execute_at, now_iso_str, duration, existing_by_season[sid][0])
        for sid in season_ids
        if sid in existing_by_season
    ]
    to_delete = [row_id for sid in season_ids for row_id in existing_by_season.get(sid, [])[1:]]
    to_insert = [
        (
            sid,
            action,
            now_iso_str,
            execute_at,
            secrets.token_urlsafe(32),
            now_iso_str,
            duration,
        )
        for sid in season_ids
        if sid not in existing_by_season
    ]
    return to_update, to_insert, to_delete


@router.post("/api/show/{show_rating_key:path}/keep")
def api_keep_show(
    show_rating_key: str,
    body: _KeepShowBody,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Keep an entire show: all listed seasons plus future seasons via the kept_shows rule.

    With *read*, only while every keep and season is as it was read.
    """
    if not _KEEP_SHOW_LIMITER.check(admin):
        logger.warning("kept_show.keep_throttled user=%s", _sanitise_log_field(admin))
        return respond_err(
            "too_many_requests",
            status=429,
            message="Too many show-keep requests; try again shortly.",
        )

    conn = get_db()
    duration = body.duration
    season_ids = body.season_ids

    resolved_key, err_response = _validate_keep_show_input(
        conn, show_rating_key, duration, season_ids, admin
    )
    if err_response is not None or resolved_key is None:
        return err_response or respond_err("unknown_show", status=409)

    now = now_utc()
    decision = resolve_keep_decision(duration, days=VALID_KEEP_DURATIONS.get(duration), now=now)
    show_title = fetch_show_title(conn, resolved_key) or "Unknown"
    now_iso_str = now.isoformat()

    # The show-keep upsert, seasons write, and audit row share one transaction
    # so a DB failure in any step rolls the whole thing back atomically. The
    # seasons' rows are read inside it, so a keep written meanwhile is replaced.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if _still_as_read(conn, resolved_key, body.read) is None:
            return keep_changed()
        ownership_err = _check_season_ownership(conn, season_ids, resolved_key, admin)
        if ownership_err is not None:
            return ownership_err
        existing_by_season = fetch_existing_actions_for_seasons(conn, season_ids)
        to_update, to_insert, to_delete = _build_season_action_rows(
            season_ids,
            existing_by_season,
            action=decision.action,
            execute_at=decision.execute_at,
            duration=duration,
            now_iso_str=now_iso_str,
        )
        upsert_kept_show(
            conn,
            show_rating_key=resolved_key,
            show_title=show_title,
            action=decision.action,
            execute_at=decision.execute_at,
            snooze_duration=duration,
            created_at=now_iso_str,
        )
        deletion_running = set_protected_state(
            conn, to_update=to_update, to_insert=to_insert, to_delete=to_delete
        )
        # The deletion pass cancels these when they are due; until then a kept show reads as leaving.
        deletion_running |= delete_show_scheduled_deletions(conn, resolved_key)
        log_audit(
            conn,
            resolved_key,
            "kept_show",
            f"Show '{show_title}' kept ({duration}) by {admin}",
            actor=admin,
        )

    logger.info(
        "Kept show %s (%s) -- %s by %s",
        _sanitise_log_field(resolved_key),
        _sanitise_log_field(show_title, 256),
        duration,
        _sanitise_log_field(admin),
    )
    reply: dict[str, object] = {"deletion_running": deletion_running}
    if deletion_running:
        reply["message"] = _DELETION_RUNNING_MESSAGE
    return respond_ok(reply)


@router.post("/api/show/{show_rating_key:path}/remove")
def api_remove_show_keep(
    show_rating_key: str,
    body: _RemoveShowKeepBody | None = None,
    admin: str = Depends(get_current_admin),
) -> JSONResponse:
    """Remove a show-level keep rule. Individual season keeps are not affected.

    With *read*, only while every keep and season is as it was read. With *drops* too, those
    seasons lose their own keeps, and every other season is kept for at least as long as the
    show keep would have kept it.
    """
    if not _REMOVE_SHOW_KEEP_LIMITER.check(admin):
        return respond_err(
            "too_many_requests",
            status=429,
            message="Too many remove-show-keep requests; try again shortly.",
        )
    read = body.read if body else None
    drops = body.drops if body else None
    if drops is not None and read is None:
        return respond_err("read_required", status=400)
    conn = get_db()
    now = now_utc()
    deletion_running = False
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        seasons = _still_as_read(conn, show_rating_key, read)
        if seasons is None:
            return keep_changed()
        keep_row = fetch_show_keep_row(conn, show_rating_key)
        if keep_row is None:
            return respond_err("not_found", status=404, message="No show-level keep found")
        if drops:
            ownership_err = _check_season_ownership(conn, drops, show_rating_key, admin)
            if ownership_err is not None:
                return ownership_err
        kept_id, show_title = keep_row
        show_keep = fetch_show_kept_status(conn, show_rating_key)
        if drops is not None and show_keep is not None:
            until = _ends(show_keep, _NEVER)
            held = [s.id for s in seasons if s.id not in drops and _ends(s.keep, _ENDED) < until]
            deletion_running = _hold_seasons(conn, held, show_keep, now, admin)
        delete_kept_show(conn, kept_id)
        log_audit(
            conn,
            show_rating_key,
            "removed_show_keep",
            f"Show keep removed for '{show_title}' by {admin}",
            actor=admin,
        )
        for season_id in dict.fromkeys(drops or ()):
            removed = delete_protections(conn, season_id)
            if removed:
                log_audit(
                    conn,
                    season_id,
                    "unprotected",
                    f"Protection removed by {admin} ({removed} keep{'s' if removed != 1 else ''})",
                    actor=admin,
                )

    logger.info(
        "Removed show keep for %s by %s",
        _sanitise_log_field(show_rating_key),
        _sanitise_log_field(admin),
    )
    reply: dict[str, object] = {"deletion_running": deletion_running}
    if deletion_running:
        reply["message"] = _DELETION_RUNNING_MESSAGE
    return respond_ok(reply)
