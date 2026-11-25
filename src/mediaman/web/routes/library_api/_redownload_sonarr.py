"""Sonarr branch of the redownload flow: bring a season back into Sonarr."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    import sqlite3

from mediaman.services.arr.add_outcome import series_add_landed
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.arr.state import season_on_disk
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.repository.library_api import record_redownload
from mediaman.web.responses import respond_failure
from mediaman.web.routes.download._season import already_there, season_name
from mediaman.web.routes.library_api._redownload_match import AddRefused, _redownload_audit_id

logger = logging.getLogger(__name__)


def _redownload_season(
    client: ArrClient,
    *,
    conn: sqlite3.Connection,
    title: str,
    tvdb_id: int,
    season_number: int,
    username: str,
) -> JSONResponse:
    """Re-monitor and search a season of a series Sonarr still holds, or add the series with
    only that season monitored when Sonarr no longer has it.

    A Library Delete leaves the series in Sonarr, so the lookup comes first. A season Sonarr
    has complete is a 409. Sonarr errors propagate to the caller.
    """
    what = f"{season_name(season_number).lower()} of '{title}'"
    existing = client.get_series_by_tvdb(tvdb_id)
    if existing is not None:
        series_id = existing.get("id")
        if series_id is None:
            raise ArrError("Sonarr holds the series with no usable id")
        season = next(
            (s for s in existing.get("seasons") or [] if s.get("seasonNumber") == season_number),
            None,
        )
        if season is not None and season_on_disk(season):
            return respond_failure(f"{already_there(season_number, title)} in Sonarr", status=409)
        client.remonitor_season(int(series_id), season_number)
        message = f"Re-monitored {what} in Sonarr and started a search"
    else:
        try:
            client.add_series_with_seasons(tvdb_id, title, [season_number])
        except SafeHTTPError as exc:
            # A slow Sonarr can add the series after the request gave up waiting.
            if not series_add_landed(exc, client, tvdb_id):
                raise AddRefused(exc) from exc
        message = f"Added {what} to Sonarr"
    email = get_user_email(conn, username)
    if email is None:
        logger.info("Re-download ready notice skipped: the requesting admin has no email set")
    record_redownload(
        conn,
        audit_id=_redownload_audit_id(
            media_type="tv", tmdb_id=None, tvdb_id=tvdb_id, season_number=season_number
        ),
        audit_detail=f"Re-downloaded {what} by {username}",
        actor=username,
        email=email,
        title=title,
        media_type="tv",
        service="sonarr",
        tvdb_id=tvdb_id,
        season_number=season_number,
    )
    logger.info(
        "Re-downloaded '%s' season %s (tvdb=%s) via Sonarr by %s",
        _sanitise_log_field(title, 256),
        season_number,
        tvdb_id,
        _sanitise_log_field(username),
    )
    return JSONResponse({"ok": True, "message": message})
