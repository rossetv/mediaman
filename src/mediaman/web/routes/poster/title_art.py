"""The pictures of a library title: its wide backdrop and its 2:3 poster, never one in the
place of the other."""

from __future__ import annotations

import sqlite3
import time

from mediaman.services import watch_catalog as catalog
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.web.repository.watch_library import find_movie, find_show, scanned_library_ids

_clock = time.monotonic
_TMDB_TIMEOUT_S = 3.0
_UNANSWERED_S = 300.0
# Library key -> when TMDB may be asked again for a title it gave no answer for.
_unanswered: dict[str, float] = {}


def title_art(key: str) -> dict[str, str]:
    """``backdrop_url`` and ``poster_url`` of the library title *key*. Either may answer 404,
    and then the title has no such picture."""
    return {
        "backdrop_url": f"/api/poster/{key}?kind=art",
        "poster_url": f"/api/poster/{key}",
    }


def tmdb_backdrop_url(conn: sqlite3.Connection, secret_key: str, key: str) -> str | None:
    """The image-proxy address of TMDB's backdrop for the library title *key*; None when TMDB
    is not set up, does not know the title or does not answer. A title without an answer is
    not asked about again for a few minutes, so a slow TMDB does not hold up each page."""
    if _unanswered.get(key, 0.0) > _clock():
        return None
    client = TmdbClient.from_db(conn, secret_key, timeout=_TMDB_TIMEOUT_S, retry=False)
    if client is None:
        return None
    libraries = scanned_library_ids(conn)
    movie = find_movie(conn, libraries, key)
    if movie is not None:
        kind, tmdb_id = "movie", movie.tmdb_id
    else:
        show = find_show(conn, libraries, key)
        if show is None:
            return None
        kind, tmdb_id = "tv", catalog.library_ids(conn)[f"show-{key}"][0]
        if tmdb_id is None and show.tvdb_id is not None:
            tmdb_id = catalog.tmdb_id_for_tvdb(client, show.tvdb_id)
            if tmdb_id is None:
                _unanswered[key] = _clock() + _UNANSWERED_S
    if tmdb_id is None:
        return None
    try:
        backdrop: str | None = catalog.details(client, kind, tmdb_id)["backdrop_url"]
    except catalog.TmdbUnavailable:
        _unanswered[key] = _clock() + _UNANSWERED_S
        return None
    return backdrop
