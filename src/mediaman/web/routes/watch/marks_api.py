"""Per-person Watched and Dismissed marks for Watch.

A mark lives only in mediaman, for the signed-in person: nothing is written to Plex,
and the cleanup scan does not read marks. Marking an episode, a season or a show reads
its episodes from Plex; everything else needs only the database. A film or show is marked as
a title too, which outlives its deletion.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from mediaman.core.time import now_utc
from mediaman.db import get_db, write_transaction
from mediaman.services.media_meta.plex_watch import (
    PlexEpisode,
    PlexError,
    PlexItemNotFound,
    PlexWatch,
    is_in_libraries,
)
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import ratings, seen_titles
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.ratings import RatedTitle
from mediaman.web.repository.seen_titles import SeenChange, ShowPart, TitleKey
from mediaman.web.repository.watch_progress import (
    MarkTarget,
    dismiss,
    mark_watched,
    progress_of,
    restore_marks,
    undismiss,
    unmark_watched,
)
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.ratings import UserId, Username
from mediaman.web.routes.watch import mark_undo
from mediaman.web.routes.watch.pages import is_valid_key, open_plex, plex_error_code

router = APIRouter()

# The numbers of episodes by season.
_Numbers = Mapping[int, Collection[int]]

_KINDS = frozenset({"movie", "episode", "season", "show"})
_DISMISS_REF_RE = re.compile(r"(movie|show)-([0-9]{1,18})")

# Per-person cap on marks and dismissals together; a show's mark costs a Plex call a season.
_MARK_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)


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
) -> tuple[list[MarkTarget], _Numbers]:
    """What a mark covers, and of a show its episodes' numbers by season; raises
    PlexItemNotFound for anything outside the scanned libraries."""
    if kind == "movie":
        if lib.find_movie(conn, libraries, key) is None:
            raise PlexItemNotFound("Not a movie of the library")
        return [MarkTarget(key, key, None, 0)], {}
    if kind == "show" and lib.find_show(conn, libraries, key) is None:
        raise PlexItemNotFound("Not a show of the library")
    plex = open_plex(request, conn)
    if kind == "episode":
        return _episode_targets(conn, plex, libraries, key), {}
    if kind == "season":
        episodes = plex.get_season_episodes(key)
        if not episodes:
            raise PlexItemNotFound("Not a season of the library")
        # A season does not say which show it is of; its episodes do.
        [first] = _episode_targets(conn, plex, libraries, episodes[0].rating_key)
        return _season_targets(episodes, key, first.show_rating_key or ""), {}
    show = plex.get_show(key)
    if not is_in_libraries(show, libraries):
        raise PlexItemNotFound("Not a show of the library")
    targets: list[MarkTarget] = []
    numbers: dict[int, set[int]] = {}
    for season in show.seasons:
        episodes = plex.get_season_episodes(season.rating_key)
        targets += _season_targets(episodes, season.rating_key, key)
        if season.index > 0 and episodes:
            numbers.setdefault(season.index, set()).update(e.index for e in episodes)
    return targets, numbers


@dataclass(frozen=True, slots=True)
class _Marked:
    """The title a mark is of, whole or a part of it."""

    title: RatedTitle
    part: ShowPart | None

    @property
    def key(self) -> TitleKey:
        return self.title.media_type, self.title.tmdb_id


def _marked(conn: sqlite3.Connection, libraries: list[str], kind: str, key: str) -> _Marked | None:
    """None for a special, and where the title has no TMDB id."""
    part = None
    ref = f"{kind}-{key}"
    if kind in ("season", "episode"):
        part = seen_titles.part_of(conn, libraries, kind, key)
        if part is None:
            return None
        ref = f"show-{part.show_key}"
    title = ratings.library_titles(conn, libraries, [ref]).get(ref)
    return _Marked(title, part) if title is not None else None


def _mark_title(
    conn: sqlite3.Connection,
    user_id: int,
    marked: _Marked,
    numbers: _Numbers,
    now: datetime,
) -> SeenChange | None:
    """Mark the title too, or the part of it that was taken back, if it has a mark."""
    if marked.part is not None:
        return seen_titles.change_part(conn, user_id, marked.key, marked.part, seen=True)
    show = marked.title.media_type == "tv"
    if show and not numbers:
        return None
    last_season = max(numbers) if show else None
    return seen_titles.mark(
        conn, user_id, marked.title, last_season, now, in_library=True, episodes=numbers
    )


def _unmark_title(conn: sqlite3.Connection, user_id: int, marked: _Marked) -> SeenChange | None:
    """Take the title's own mark back, or that part of it; None when it does not reach it."""
    if marked.part is not None:
        return seen_titles.change_part(conn, user_id, marked.key, marked.part, seen=False)
    if seen_titles.get_mark(conn, user_id, marked.key) is None:
        return None
    return seen_titles.unmark(conn, user_id, marked.title)


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


def _too_late() -> JSONResponse:
    return respond_err("too_late", status=409, message="This can no longer be undone.")


def undone(slot: mark_undo.Slot, username: str, *, watched: bool) -> JSONResponse:
    """Take back this person's last change of *slot*, when it set the opposite of *watched*."""
    held = mark_undo.take(slot, watched=watched, now=now_utc())
    if held is None:
        return _too_late()
    conn = get_db()
    with write_transaction(conn):
        changed = restore_marks(conn, username, held.changes)
        if held.title is not None and seen_titles.restore(conn, held.title):
            changed = changed or 1
    if not changed:
        return _too_late()
    return respond_ok({"watched": watched, "changed": changed})


@router.post("/api/watch/watched/{kind}/{key}")
def api_watch_mark(
    request: Request,
    kind: str,
    key: str,
    user_id: UserId,
    username: Username,
    undo: str = "",
) -> JSONResponse:
    """Mark a movie, episode, season or show watched; with undo=1, take back marking it not
    watched."""
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    if kind not in _KINDS or not is_valid_key(key):
        return _not_found()
    if undo == "1":
        return undone((user_id, kind, key), username, watched=True)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    try:
        targets, numbers = _targets(request, conn, libraries, kind, key)
    except PlexItemNotFound:
        return _not_found()
    except PlexError as exc:
        return respond_err(plex_error_code(exc), status=503)
    now = now_utc()
    marked = _marked(conn, libraries, kind, key)
    with write_transaction(conn):
        changes = mark_watched(conn, username, targets, now)
        title = _mark_title(conn, user_id, marked, numbers, now) if marked else None
    mark_undo.hold(
        (user_id, kind, key),
        marked.key if marked else None,
        watched=True,
        changes=changes,
        title=title,
        now=now,
    )
    return respond_ok({"watched": True, "changed": len(changes)})


@router.delete("/api/watch/watched/{kind}/{key}")
def api_watch_unmark(
    kind: str,
    key: str,
    user_id: UserId,
    username: Username,
    undo: str = "",
) -> JSONResponse:
    """Mark a movie, episode, season or show not watched; with undo=1, take back marking it
    watched."""
    if not _MARK_LIMITER.check(username):
        return _rate_limited()
    if kind not in _KINDS or not is_valid_key(key):
        return _not_found()
    if undo == "1":
        return undone((user_id, kind, key), username, watched=False)
    conn = get_db()
    libraries = lib.scanned_library_ids(conn)
    if not _may_unmark(conn, username, libraries, kind, key):
        return _not_found()
    marked = _marked(conn, libraries, kind, key)
    with write_transaction(conn):
        title = _unmark_title(conn, user_id, marked) if marked else None
        keys = [key]
        if marked and title and not marked.part:
            # The mark was of every copy, and one left marked would keep the title watched.
            refs = seen_titles.library_refs(conn, libraries, marked.key)
            keys = [ref.partition("-")[2] for ref in refs]
        changes = [c for k in keys for c in unmark_watched(conn, username, kind, k)]
    mark_undo.hold(
        (user_id, kind, key),
        marked.key if marked else None,
        watched=False,
        changes=changes,
        title=title,
        now=now_utc(),
    )
    return respond_ok({"watched": False, "changed": len(changes) or int(title is not None)})


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


__all__ = ["router", "undone"]
