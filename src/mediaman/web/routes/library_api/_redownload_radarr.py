"""Radarr branch of the redownload flow: bring a movie back into Radarr."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    import sqlite3

from mediaman.services.arr.base import ArrClient
from mediaman.services.infra import SafeHTTPError
from mediaman.web.auth._password_hash_helpers import _sanitise_log_field
from mediaman.web.auth.password_hash import get_user_email
from mediaman.web.repository.library_api import record_redownload
from mediaman.web.routes.library_api._redownload_match import AddRefused, _redownload_audit_id

logger = logging.getLogger(__name__)


def _redownload_movie(
    client: ArrClient,
    *,
    conn: sqlite3.Connection,
    title: str,
    tmdb_id: int,
    username: str,
) -> JSONResponse:
    """Re-monitor and search a movie Radarr still holds, or add it when Radarr no longer has it.

    A Library Delete leaves the movie in Radarr unmonitored, so the lookup comes first: adding
    it again would only be refused as a duplicate. Radarr errors propagate to the caller.
    """
    existing = client.get_movie_by_tmdb(tmdb_id)
    movie_id = existing.get("id") if existing else None
    if movie_id is not None:
        client.remonitor_movie(int(movie_id))
        message = f"Re-monitored '{title}' in Radarr and started a search"
    else:
        try:
            client.add_movie(tmdb_id, title)
        except SafeHTTPError as exc:
            raise AddRefused(exc) from exc
        message = f"Added '{title}' to Radarr"
    email = get_user_email(conn, username)
    if email is None:
        logger.info("Re-download ready notice skipped: the requesting admin has no email set")
    record_redownload(
        conn,
        audit_id=_redownload_audit_id(media_type="movie", tmdb_id=tmdb_id, tvdb_id=None),
        audit_detail=f"Re-downloaded '{title}' by {username}",
        actor=username,
        email=email,
        title=title,
        media_type="movie",
        service="radarr",
        tmdb_id=tmdb_id,
    )
    logger.info(
        "Re-downloaded '%s' (tmdb=%s) via Radarr by %s",
        _sanitise_log_field(title, 256),
        tmdb_id,
        _sanitise_log_field(username),
    )
    return JSONResponse({"ok": True, "message": message})
