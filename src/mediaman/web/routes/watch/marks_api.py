"""Per-person Watched and Dismissed marks for Watch.

A mark lives only in mediaman, for the signed-in person: nothing is written to Plex,
and the cleanup scan does not read marks. Marking an episode, a season or a show reads
its episodes from Plex; everything else needs only the database.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItemNotFound,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.watch_progress import (
    MarkChange,
    MarkTarget,
    dismiss,
    mark_watched,
    progress_of,
    restore_marks,
    undismiss,
    unmark_watched,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.pages import is_valid_key, open_plex, plex_error_code

router = APIRouter()

_KINDS = frozenset({"movie", "episode", "season", "show"})
_DISMISS_REF_RE = re.compile(r"(movie|show)-([0-9]{1,18})")

# Per-person cap on marks and dismissals together; a show's mark costs a Plex call a season.
_MARK_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)

# How long a mark or unmark can be undone. What it changed is held here, in memory, for that
# long; after a restart the change simply stays.
UNDO_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class _Undo:
    watched: bool  # what the change it takes back did
    changes: list[MarkChange]
    expires: datetime


_UNDO: dict[tuple[str, str, str], _Undo] = {}
_UNDO_LOCK = threading.Lock()


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _rate_limited() -> JSONResponse:
    return respond_err(
        "too_many_requests", status=429, message="Too many changes at once — slow down"
    )


def _episode_targets(
    conn: sqlite3.Connection, plex: PlexWatch, libraries: list[str], key: str
) -> list[MarkTarget]:
    item = plex.get_item(key)
    show_key = item.grandparent_rating_key or ""
    if (
        item.type != "episode"
        or not item.parent_rating_key
        or not is_in_libraries(item, libraries)
        or lib.find_show(conn, libraries, show_key) is None
    ):
        raise PlexItemNotFound("Not an episode of the library")
    return [MarkTarget(key, item.parent_rating_key, show_key, item.duration_s)]


def _season_targets(
    episodes: list[PlexEpisode], season_key: str, show_key: str
) -> list[MarkTarget]:
    return [MarkTarget(e.rating_key, season_key, show_key, e.duration_s) for e in episodes]


def _targets(
    request: Request, conn: sqlite3.Connection, libraries: list[str], kind: str, key: str
) -> list[MarkTarget]:
    """What a mark covers; raises PlexItemNotFound for anything outside the scanned libraries."""
    if kind == "movie":
        if lib.find_movie(conn, libraries, key) is None:
            raise PlexItemNotFound("Not a movie of the library")
        return [MarkTarget(key, key, None, 0)]
    if kind == "show" and lib.find_show(conn, libraries, key) is None:
        raise PlexItemNotFound("Not a show of the library")
    plex = open_plex(request, conn)
    if kind == "episode":
        return _episode_targets(conn, plex, libraries, key)
    if kind == "season":
        episodes = plex.get_season_episodes(key)
        if not episodes:
            raise PlexItemNotFound("Not a season of the library")
        # A season does not say which show it is of; its episodes do.
        [first] = _episode_targets(conn, plex, libraries, episodes[0].rating_key)
        return _season_targets(episodes, key, first.show_rating_key or "")
    show = plex.get_show(key)
    if not is_in_libraries(show, libraries):
        raise PlexItemNotFound("Not a show of the library")
    return [
        target
        for season in show.seasons
        for target in _season_targets(
            plex.get_season_episodes(season.rating_key), season.rating_key, key
        )
    ]


def _may_unmark(
    conn: sqlite3.Connection, username: str, libraries: list[str], kind: str, key: str
) -> bool:
    """Whether *key* may be unmarked: of the scanned libraries by what the library sync stored,
    or an episode or season this person has nothing on (there is nothing to change)."""
    if kind == "movie":
        return lib.find_movie(conn, libraries, key) is not None
    if kind == "show":
        return lib.find_show(conn, libraries, key) is not None
    rows = progress_of(conn, username, kind, key)
    if not rows:
        return True
    show_key = rows[0].show_rating_key
    return show_key is not None and lib.find_show(conn, libraries, show_key) is not None


def _remember(
    username: str, kind: str, key: str, *, watched: bool, changes: list[MarkChange], now: datetime
) -> None:
    with _UNDO_LOCK:
        for old in [k for k, v in _UNDO.items() if v.expires <= now]:
            del _UNDO[old]
        if changes:
            _UNDO[(username, kind, key)] = _Undo(watched, changes, now + UNDO_WINDOW)


def _undo(username: str, kind: str, key: str, *, watched: bool) -> JSONResponse:
    """Take back this person's last change of *key*, when it set the opposite of *watched*."""
    slot = (username, kind, key)
    with _UNDO_LOCK:
        held = _UNDO.get(slot)
        if held is None or held.watched is watched or held.expires <= now_utc():
            held = None
        else:
            del _UNDO[slot]
    if held is None:
        return respond_err("too_late", status=409, message="This can no longer be undone.")
    restore_marks(get_db(), username, held.changes)
    return respond_ok({"watched": watched, "changed": len(held.changes)})


@router.post("/api/watch/watched/{kind}/{key}")
def api_watch_mark(
    request: Request,
    kind: str,
    key: str,
    undo: str = "",
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Mark a movie, episode, season or show watched; with undo=1, take back marking it not
    watched."""
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    if kind not in _KINDS or not is_valid_key(key):
        return _not_found()
    if undo == "1":
        return _undo(username, kind, key, watched=True)
    conn = get_db()
    try:
        targets = _targets(request, conn, lib.scanned_library_ids(conn), kind, key)
    except PlexItemNotFound:
        return _not_found()
    except PlexError as exc:
        return respond_err(plex_error_code(exc), status=503)
    now = now_utc()
    changes = mark_watched(conn, username, targets, now)
    _remember(username, kind, key, watched=True, changes=changes, now=now)
    return respond_ok({"watched": True, "changed": len(changes)})


@router.delete("/api/watch/watched/{kind}/{key}")
def api_watch_unmark(
    kind: str,
    key: str,
    undo: str = "",
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Mark a movie, episode, season or show not watched; with undo=1, take back marking it
    watched."""
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    if kind not in _KINDS or not is_valid_key(key):
        return _not_found()
    if undo == "1":
        return _undo(username, kind, key, watched=False)
    conn = get_db()
    if not _may_unmark(conn, username, lib.scanned_library_ids(conn), kind, key):
        return _not_found()
    changes = unmark_watched(conn, username, kind, key)
    _remember(username, kind, key, watched=False, changes=changes, now=now_utc())
    return respond_ok({"watched": False, "changed": len(changes)})


def _dismissable(ref: str) -> str | None:
    """The key of the library movie or show *ref* names; None when it names none."""
    match = _DISMISS_REF_RE.fullmatch(ref)
    if match is None:
        return None
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    kind, key = match.groups()
    found = (lib.find_movie if kind == "movie" else lib.find_show)(conn, libraries, key)
    return key if found is not None else None


@router.post("/api/watch/dismissed/{ref}")
def api_watch_dismiss(ref: str, username: str = Depends(get_current_user)) -> JSONResponse:
    """Hide a title from this person's Continue watching until they play it again."""
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    key = _dismissable(ref)
    if key is None:
        return _not_found()
    dismiss(get_db(), username, key, now_utc())
    return respond_ok()


@router.delete("/api/watch/dismissed/{ref}")
def api_watch_undismiss(ref: str, username: str = Depends(get_current_user)) -> JSONResponse:
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    key = _dismissable(ref)
    if key is None:
        return _not_found()
    undismiss(get_db(), username, key)
    return respond_ok()


__all__ = ["router"]
