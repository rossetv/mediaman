"""Sonarr branch of the redownload flow: bring a season back into Sonarr."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    import sqlite3

from mediaman.services.arr.add_outcome import series_add_landed
from mediaman.services.arr.base import ArrClient, ArrError
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.repository.library_api import record_redownload
from mediaman.web.routes.library_api._redownload_match import AddRefused, _redownload_audit_id

logger = logging.getLogger(__name__)


def _put_back_unmonitored(client: ArrClient, series_id: int, season_number: int) -> None:
    """Best effort: a failure here is logged, so the error being raised is not hidden."""
    try:
        client.unmonitor_season(series_id, season_number, max_retries=1)
    except (SafeHTTPError, requests.RequestException, ValueError, ArrError) as exc:
        logger.error(
            "Could not put sonarr series_id=%s season=%s back to unmonitored: %s",
            series_id,
            season_number,
            type(exc).__name__,
        )


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

    A Library Delete leaves the series in Sonarr, so the lookup comes first. Sonarr errors
    propagate to the caller.
    """
    existing = client.get_series_by_tvdb(tvdb_id)
    if existing is not None:
        series_id = existing.get("id")
        if series_id is None:
            raise ArrError("Sonarr holds the series with no usable id")
        was_unmonitored = any(
            s.get("seasonNumber") == season_number and not s.get("monitored")
            for s in existing.get("seasons") or []
        )
        try:
            client.remonitor_season(int(series_id), season_number)
        except (SafeHTTPError, requests.RequestException, ValueError, ArrError):
            # The monitor may have landed before the search failed. A season already
            # monitored before this request stays as it was.
            if was_unmonitored:
                _put_back_unmonitored(client, int(series_id), season_number)
            raise
        message = f"Re-monitored season {season_number} of '{title}' in Sonarr and started a search"
    else:
        try:
            client.add_series_with_seasons(tvdb_id, title, [season_number])
        except SafeHTTPError as exc:
            # A slow Sonarr can add the series after the request gave up waiting.
            if not series_add_landed(exc, client, tvdb_id):
                raise AddRefused(exc) from exc
        message = f"Added season {season_number} of '{title}' to Sonarr"
    email = get_user_email(conn, username)
    if email is None:
        logger.info("Re-download ready notice skipped: the requesting admin has no email set")
    record_redownload(
        conn,
        audit_id=_redownload_audit_id(
            media_type="tv", tmdb_id=None, tvdb_id=tvdb_id, season_number=season_number
        ),
        audit_detail=f"Re-downloaded season {season_number} of '{title}' by {username}",
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
