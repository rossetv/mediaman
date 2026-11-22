"""Radarr branch of the redownload flow: bring a movie back into Radarr."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    import sqlite3

from mediaman.services.arr.base import ArrClient
from mediaman.web.repository.library_api import record_redownload
from mediaman.web.routes.library_api._redownload_match import _redownload_audit_id

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
        client.add_movie(tmdb_id, title)
        message = f"Added '{title}' to Radarr"
    record_redownload(
        conn,
        audit_id=_redownload_audit_id(
            media_type="movie", tmdb_id=tmdb_id, tvdb_id=None, imdb_id=None
        ),
        audit_detail=f"Re-downloaded '{title}' by {username}",
        actor=username,
        email=username,
        title=title,
        media_type="movie",
        service="radarr",
        tmdb_id=tmdb_id,
    )
    logger.info("Re-downloaded '%s' (tmdb=%s) via Radarr by %s", title, tmdb_id, username)
    return JSONResponse({"ok": True, "message": message})
