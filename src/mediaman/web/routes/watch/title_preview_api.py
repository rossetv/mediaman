"""Watch JSON for the card a title grows into while the pointer rests on it: what that card
shows of the title's detail, under the detail's own field names.

Resting on cards asks for many titles that are never opened, so this answers from the
library, the caches and the person's own progress. TMDB is asked only for a title not cached,
within a limit of its own; OMDb never; Plex only for what nothing else knows (the words for
a film or show without TMDB, the next episode of a show not in progress), in a short time.
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import (
    SafeHTTPClient,
    allowed_outbound_hosts,
    disk_cache,
    get_string_setting,
)
from mediaman.services.media_meta.item_enrichment import tidy_summary
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai.recommendations.repository import (
    SuggestionDetail,
    fetch_suggestion_by_id,
)
from mediaman.services.openai.recommendations.user_picks import fetch_pick
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import get_progress, show_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.catalog_api import (
    REF_RE,
    _arr,
    _fill_fields,
    _OutsideLibraries,
    _payload,
    _title_of_fields,
    _tmdb_client,
    fill_show_leaving,
)
from mediaman.web.routes.watch.pages import (
    _primary_episode,
    leaving_info,
    play_href,
    progress_fields,
    resumable,
)

router = APIRouter()

_FIELDS = (
    "ref",
    "kind",
    "in_library",
    "library_ref",
    "title",
    "year",
    "year_end",
    "runtime_min",
    "season_count",
    "genres",
    "genre_refs",
    "certificate",
    "tagline",
    "overview",
    "backdrop_url",
    "ratings",
    "trailer_key",
    "reason",
    "play",
    "leaving",
    "leaving_seasons",
    "keep_allowed",
    "seasons",
    "download",
)
_SEASON_FIELDS = ("number", "in_library")
_DOWNLOAD_FIELDS = ("state", "pct", "request")

# Apart from the query limit, so resting on cards cannot use up anyone's searches.
_LIMITER = ActionRateLimiter(max_in_window=40, window_seconds=60, max_per_day=2000)

# The card waits for the answer, and a worker thread with it.
_PLEX_TIMEOUT = (1.0, 2.0)


class _NoTmdb(Exception):
    """Only TMDB has the title, and it is not set up."""


class _Ask:
    """One request's leave to ask TMDB or Plex, counted against the person's limit once
    however much it then asks. Raises disk_cache.Refused past the limit: a preview without
    its words or its next episode would be kept and shown as the title's."""

    def __init__(self, username: str) -> None:
        self._username = username
        self._counted = False

    def __call__(self) -> bool:
        if not self._counted and not _LIMITER.check(self._username):
            raise disk_cache.Refused(self._username)
        self._counted = True
        return True


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _open_plex(request: Request, conn: sqlite3.Connection) -> PlexWatch:
    allowed_hosts = allowed_outbound_hosts(conn)
    return PlexWatch(
        get_string_setting(conn, "plex_url"),
        get_string_setting(conn, "plex_token", secret_key=request.app.state.config.secret_key),
        http=SafeHTTPClient(
            default_timeout=_PLEX_TIMEOUT,
            default_max_bytes=4 * 1024 * 1024,
            allowed_hosts=allowed_hosts,
        ),
    )


def _fill_from_tmdb(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    kind: str,
    tmdb_id: int,
    ask: _Ask,
) -> None:
    """Fill *out* from TMDB and the ratings held; raises as ``catalog.details``."""
    fields = catalog.details(client, kind, tmdb_id, allow_fetch=ask, brief=True)
    _fill_fields(out, request, conn, kind, tmdb_id, fields)


def _library_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    is_admin: bool,
    ref: str,
    ask: _Ask,
) -> dict[str, Any] | None:
    """The preview of a library title ("movie-<key>", "show-<key>"), or None when the scanned
    libraries hold none. Raises _OutsideLibraries when Plex places it outside them."""
    kind, _, key = ref.partition("-")
    libraries = lib.scanned_library_ids(conn)
    movie = lib.find_movie(conn, libraries, key) if kind == "movie" else None
    show = lib.find_show(conn, libraries, key) if kind == "show" else None
    listed = movie or show
    if listed is None:
        return None
    client = _tmdb_client(request, conn)
    out = _payload(ref, "movie" if movie else "show", client is not None)
    out.update(in_library=True, library_ref=ref, title=listed.title)
    if show:
        out["season_count"] = show.season_count or None
    _leaving(out, conn, libraries, kind, key, is_admin)

    known = False
    tmdb_id = _tmdb_id(request, conn, client, libraries, ref, listed) if client else None
    if client is not None and tmdb_id is not None:
        # Without TMDB, Plex has the words.
        with contextlib.suppress(catalog.TmdbUnavailable):
            media_type = "movie" if movie else "tv"
            _fill_from_tmdb(out, request, conn, client, media_type, tmdb_id, ask)
            known = True
    if movie:
        _movie_play(out, conn, username, key)
        if not known:
            _movie_words(out, request, conn, libraries, key, ask)
    elif not _show_resume(out, conn, username, key) or not known:
        _show_from_plex(out, request, conn, username, libraries, key, ask)
    return out


def _leaving(
    out: dict[str, Any],
    conn: sqlite3.Connection,
    libraries: list[str],
    kind: str,
    key: str,
    is_admin: bool,
) -> None:
    if kind == "show":
        fill_show_leaving(out, conn, libraries, key, is_admin)
        return
    now = now_utc()
    pending = lib.pending_deletion(conn, libraries, key)
    out["leaving"] = leaving_info(pending, now)
    out["keep_allowed"] = is_admin or pending is not None


def _tmdb_id(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    libraries: list[str],
    ref: str,
    listed: lib.LibraryMovie | lib.LibraryShow,
) -> int | None:
    """The TMDB id already known of a library title; TMDB is not asked for one."""
    own, _year = catalog.library_ids(conn).get(ref, (None, None))
    if isinstance(listed, lib.LibraryMovie):
        return listed.tmdb_id or own
    if own or not listed.tvdb_id:
        return own
    found = _arr(request, conn).tmdb_id_for_tvdb(listed.tvdb_id)
    if found is not None:
        return found
    index = catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)
    return next((tmdb_id for tmdb_id, held in index.shows_by_tmdb.items() if held == ref), None)


def _movie_play(out: dict[str, Any], conn: sqlite3.Connection, username: str, key: str) -> None:
    progress = get_progress(conn, username, [key]).get(key)
    resume = resumable(progress)
    pct, left = progress_fields(progress) if progress and resume else (None, None)
    out["play"] = {
        "label": "Resume" if resume else "Play",
        "href": play_href(key, resume=resume, origin=f"/watch#title={out['ref']}"),
        "progress_pct": pct,
        "minutes_left": left,
    }


def _movie_words(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    libraries: list[str],
    key: str,
    ask: _Ask,
) -> None:
    ask()
    try:
        item = _open_plex(request, conn).get_item(key)
    except PlexError:
        return
    if item.type != "movie" or not is_in_libraries(item, libraries):
        raise _OutsideLibraries(key)
    out.update(
        title=item.title or out["title"],
        year=item.year,
        overview=item.summary,
        runtime_min=round(item.duration_s / 60) or None,
    )


def _show_resume(out: dict[str, Any], conn: sqlite3.Connection, username: str, key: str) -> bool:
    """Play the episode last played where that is known to be unfinished; False otherwise."""
    played = [p for p in show_progress(conn, username, key).values() if p.played]
    last = max(played, key=lambda p: p.updated_at, default=None)
    if last is None or last.watched or not resumable(last):
        return False
    pct, left = progress_fields(last)
    out["play"] = {
        "label": "Resume",
        "href": play_href(last.rating_key, resume=True, origin=f"/watch#title={out['ref']}"),
        "progress_pct": pct,
        "minutes_left": left,
    }
    return True


def _show_from_plex(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    libraries: list[str],
    key: str,
    ask: _Ask,
) -> None:
    """The next episode to play, and the words *out* has none of yet."""
    ask()
    try:
        plex = _open_plex(request, conn)
        show = plex.get_show(key)
        if not is_in_libraries(show, libraries):
            raise _OutsideLibraries(key)
        out.update(
            year=out["year"] or show.year_first,
            overview=out["overview"] or show.summary,
            genres=out["genres"] or ([show.genre] if show.genre else []),
        )
        # As the title's page says them, from the same seasons.
        if out["year_end"] is None and out["year"] == show.year_first != show.year_last:
            out["year_end"] = show.year_last
        if out["play"] is not None:
            return
        episodes: dict[str, list[PlexEpisode]] = {}

        def episodes_of(season_key: str) -> list[PlexEpisode]:
            if season_key not in episodes:
                episodes[season_key] = plex.get_season_episodes(season_key)
            return episodes[season_key]

        counted = lib.show_seen(conn, username, libraries, show_key=key).get(key)
        origin = f"/watch#title={out['ref']}"
        progress = seen_titles.with_covered(
            conn, username, libraries, key, show_progress(conn, username, key)
        )
        primary, _season = _primary_episode(plex, show, progress, episodes_of, origin, counted)
    except PlexError:
        return
    out["play"] = {**primary, "progress_pct": None, "minutes_left": None}


def _outside_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    ref: str,
    client: TmdbClient,
    media_type: str,
    tmdb_id: int,
    download_url: str | None,
    ask: _Ask,
) -> dict[str, Any]:
    """The preview of a TMDB title; raises as ``catalog.details``."""
    fields = catalog.details(client, media_type, tmdb_id, allow_fetch=ask, brief=True)
    return _title_of_fields(
        request,
        conn,
        username,
        client,
        ref,
        media_type,
        tmdb_id,
        download_url,
        fields,
    )


def _stored_pick(ref: str, rec: SuggestionDetail) -> dict[str, Any]:
    out = _payload(ref, "movie" if rec.media_type == "movie" else "show", True)
    out.update(title=rec.title, year=rec.year, overview=tidy_summary(rec.description) or "")
    out["ratings"].update(tmdb=rec.rating, rt=rec.rt_rating)
    return out


def _trimmed(out: dict[str, Any]) -> dict[str, Any]:
    """Only what the preview shows."""
    trimmed = {f: out[f] for f in _FIELDS}
    trimmed["seasons"] = [{f: s[f] for f in _SEASON_FIELDS} for s in out["seasons"]]
    if out["download"] is not None:
        trimmed["download"] = {f: out["download"][f] for f in _DOWNLOAD_FIELDS}
    if "sonarr_tracked" in out:
        trimmed["sonarr_tracked"] = out["sonarr_tracked"]
    return trimmed


@router.get("/api/watch/preview/{ref}")
def api_watch_preview(
    ref: str, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """The preview of the title *ref* names. One the library holds is its library title's,
    whatever *ref* named it, with a pick's own reason."""
    if not REF_RE.fullmatch(ref):
        return _not_found()
    conn = get_db()
    ask = _Ask(username)
    kind, _, rest = ref.partition("-")
    try:
        if kind in ("movie", "show"):
            out = _library_title(
                request, conn, username, get_user_role(conn, username) == "admin", ref, ask
            )
        else:
            out = _other_title(request, conn, username, ref, kind, rest, ask)
    except _NoTmdb:
        return respond_err("tmdb_not_configured", status=503)
    except (disk_cache.Refused, catalog.TmdbBusy):
        return respond_err("too_many_requests", status=429)
    except catalog.TitleNotFound:
        return _not_found()
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    except _OutsideLibraries:
        return _not_found()
    return respond_ok(_trimmed(out)) if out is not None else _not_found()


def _other_title(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    ref: str,
    kind: str,
    rest: str,
    ask: _Ask,
) -> dict[str, Any] | None:
    """The preview of a TMDB title or a pick; None for a pick that is not there, or not
    this person's. Raises as ``catalog.details``."""
    client = _tmdb_client(request, conn)
    if kind == "tmdb":
        if client is None:
            raise _NoTmdb
        media_type, _, tmdb_id = rest.partition("-")
        out = _outside_title(
            request, conn, username, ref, client, media_type, int(tmdb_id), None, ask
        )
        return _as_held(out, request, conn, username, ask)
    if kind == "pick":
        rec = fetch_pick(conn, username, int(rest))
        download_url = f"/api/watch/picks/{rest}/download"
    else:
        rec = fetch_suggestion_by_id(conn, int(rest))
        download_url = f"/api/recommended/{rest}/download"
    if rec is None or rec.media_type not in ("movie", "tv"):
        return None
    out = _stored_pick(ref, rec)
    if client is not None and rec.tmdb_id:
        try:
            out = _outside_title(
                request,
                conn,
                username,
                ref,
                client,
                rec.media_type,
                rec.tmdb_id,
                download_url,
                ask,
            )
            out = _as_held(out, request, conn, username, ask)
        except catalog.TitleNotFound:
            raise
        except catalog.TmdbUnavailable:
            pass  # The pick's own text is better than none.
    out["reason"] = rec.reason or None
    return out


def _as_held(
    out: dict[str, Any],
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    ask: _Ask,
) -> dict[str, Any]:
    """*out*, or its library title where the library holds it."""
    library_ref = out["library_ref"]
    if library_ref is None:
        return out
    is_admin = get_user_role(conn, username) == "admin"
    return _library_title(request, conn, username, is_admin, library_ref, ask) or out


__all__ = ["router"]
