"""Another language for a title: subtitles Plex finds and adds in seconds, or another copy of the
episode, season or movie with the audio, which Sonarr or Radarr finds and downloads.

Anyone signed in may do either, for a title they can watch; a new copy takes the limits of a new
request. What a search found is kept for the person who searched, so only that can be added or
downloaded. Adding subtitles leaves Plex's own selection of tracks as it was: which subtitles play
is each person's own choice, kept by mediaman.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import requests
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response
from urllib3.exceptions import NewConnectionError

from mediaman.core.audit import log_audit
from mediaman.db import get_db
from mediaman.services import watch_catalog
from mediaman.services.arr import ArrError
from mediaman.services.arr.base import ArrClient
from mediaman.services.arr.build import build_radarr_from_db, build_sonarr_from_db
from mediaman.services.arr.releases import Copy, copies_with
from mediaman.services.cast.tracks import LANGUAGES, NATIVE_NAMES, Track
from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta.plex_stream import get_stream_manager
from mediaman.services.media_meta.plex_watch import PlexError, PlexItem, SubtitleFound
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import language_copies, track_choices
from mediaman.web.repository.language_copies import Asked
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.search.download import _check_rate_limits
from mediaman.web.routes.watch.stream_api import _open_plex, plex_failure
from mediaman.web.routes.watch.tracks_api import _title, _valid_key

logger = logging.getLogger(__name__)

router = APIRouter()

# Per person, over these routes together: each asks Plex, Sonarr or Radarr.
_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)
# A search for copies asks every indexer.
_COPY_SEARCH_LIMITER = ActionRateLimiter(max_in_window=6, window_seconds=60, max_per_day=200)

#: Plex's on-demand search takes ISO 639-1 codes alone: it answers a regional tag such as pt-BR as
#: its language (pt), and a three-letter code with an error. A language added to ``LANGUAGES`` with
#: a two-letter code is offered here as it is.
SUBTITLE_LANGUAGES = [code for code in LANGUAGES if len(code) == 2]

#: Subtitle adds one person may have under way: each holds a server thread while Plex adds.
ADDS_AT_ONCE = 2
_adding: dict[str, int] = {}
_adding_lock = threading.Lock()

_FOUND_TTL_S = 30 * 60.0
_FOUND_MAX = 500
#: A copy asked for that has not come by then is no longer waited for.
_GIVEN_UP_AFTER = timedelta(days=7)

Scope = Literal["episode", "season", "movie"]


class _AddSubtitle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(pattern=r"^[a-z]{2}$")
    index: int = Field(ge=0, le=100)


class _GetCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(pattern=r"^[a-z]{2,3}$")
    scope: Scope
    index: int = Field(ge=0, le=100)


# What a search found, for the person who searched: (username, rating key, what, language,
# scope) -> (when it goes, what was found).
_found: dict[tuple[str, str, str, str, str], tuple[float, list[Any]]] = {}
_found_lock = threading.Lock()


def _keep_found(key: tuple[str, str, str, str, str], found: list[Any]) -> None:
    now = time.monotonic()
    with _found_lock:
        for old in [k for k, (until, _) in _found.items() if until <= now]:
            del _found[old]
        if len(_found) >= _FOUND_MAX:
            del _found[min(_found, key=lambda k: _found[k][0])]
        _found[key] = (now + _FOUND_TTL_S, found)


def _was_found(key: tuple[str, str, str, str, str], index: int) -> Any | None:
    with _found_lock:
        held = _found.get(key)
    if held is None or held[0] <= time.monotonic() or index >= len(held[1]):
        return None
    return held[1][index]


def _take_found(key: tuple[str, str, str, str, str], index: int) -> tuple[Any, list[Any]] | None:
    """What a search found at *index*, with all it found, taken so that it is downloaded once."""
    with _found_lock:
        held = _found.get(key)
        if held is None or held[0] <= time.monotonic() or index >= len(held[1]):
            return None
        del _found[key]
    return held[1][index], held[1]


def _parts(
    request: Request, conn: sqlite3.Connection, item: PlexItem, scope: str
) -> dict[str, str]:
    """The file in Plex of the item, or of each episode of its season, now."""
    if scope == "season" and item.parent_rating_key:
        return _open_plex(request, conn).episode_parts(item.parent_rating_key)
    part = item.media.part_id if item.media else None
    return {item.rating_key: part} if part else {}


def _rate_limited() -> Response:
    return respond_err("too_many_requests", status=429)


def _scope_of(item: PlexItem, scope: str) -> Scope | None:
    if item.type == "movie":
        return "movie" if scope == "movie" else None
    return scope if scope in ("episode", "season") else None  # type: ignore[return-value]


def _audio_languages(audio: list[Track]) -> list[str]:
    codes: list[str] = []
    for track in audio:
        code = track["language"]
        if code and not track["commentary"] and code not in codes:
            codes.append(code)
    return codes


def _have(item: PlexItem, audio: list[Track]) -> dict[str, object]:
    media = item.media
    size = int(media.part_size) if media and media.part_size.isdigit() else None
    resolution = media.resolution if media else ""
    quality = f"{resolution}p" if resolution.isdigit() else resolution.upper()
    return {"languages": _audio_languages(audio), "quality": quality, "size_bytes": size}


# ── Where a title is in Sonarr or Radarr ──


@dataclass(frozen=True)
class _Target:
    service: Literal["radarr", "sonarr"]
    arr_id: int
    season_number: int | None = None
    episode_id: int | None = None


def _client(request: Request, conn: sqlite3.Connection, item: PlexItem) -> ArrClient | None:
    secret = request.app.state.config.secret_key
    return (build_radarr_from_db if item.type == "movie" else build_sonarr_from_db)(conn, secret)


def _target(conn: sqlite3.Connection, client: ArrClient, item: PlexItem) -> _Target | None:
    """Where *item* is in the service; None where the service does not have it."""
    if item.type == "movie":
        row = conn.execute(
            "SELECT radarr_id FROM media_items WHERE id = ?", (item.rating_key,)
        ).fetchone()
        return _Target("radarr", row["radarr_id"]) if row and row["radarr_id"] else None
    row = conn.execute(
        "SELECT sonarr_id FROM media_items WHERE id = ?", (item.parent_rating_key,)
    ).fetchone()
    if not row or not row["sonarr_id"] or item.parent_index is None or item.index is None:
        return None
    episode = next(
        (
            e
            for e in client.get_episodes(row["sonarr_id"])
            if e.get("seasonNumber") == item.parent_index and e.get("episodeNumber") == item.index
        ),
        None,
    )
    if episode is None or not isinstance(episode.get("id"), int):
        return None
    return _Target("sonarr", row["sonarr_id"], item.parent_index, episode["id"])


def _search_ids(target: _Target, scope: Scope) -> dict[str, int]:
    if target.service == "radarr":
        return {"movieId": target.arr_id}
    if scope == "season":
        assert target.season_number is not None
        return {"seriesId": target.arr_id, "seasonNumber": target.season_number}
    assert target.episode_id is not None
    return {"episodeId": target.episode_id}


_ARR_FAILURES = (SafeHTTPError, requests.RequestException, ArrError, ValueError)


# ── How copies asked for are getting on ──


def _queue(client: ArrClient | None) -> list[dict[str, Any]] | None:
    """The service's whole queue; None where it could not be read."""
    if client is None:
        return None
    try:
        return [dict(entry) for entry in client.get_queue()]
    except _ARR_FAILURES:
        return None


def _ours(queue: list[dict[str, Any]] | None, copy: Asked) -> list[dict[str, Any]] | None:
    """The copy's downloads in the queue; None where the queue could not be read."""
    if queue is None:
        return None
    out = []
    for entry in queue:
        if copy.service == "radarr":
            ours = entry.get("movieId") == copy.arr_id
        elif copy.scope == "season":
            episode = entry.get("episode") or {}
            ours = (
                entry.get("seriesId") == copy.arr_id
                and episode.get("seasonNumber") == copy.season_number
            )
        else:
            ours = entry.get("episodeId") == copy.episode_id
        if ours:
            out.append(entry)
    return out


#: A download the service gave up on, or one a person told it to leave: it will not come.
_FAILED_STATES = frozenset({"failed", "failedpending", "ignored"})


def _tracked(entry: dict[str, Any]) -> str:
    return str(entry.get("trackedDownloadState") or "").lower()


def _failed(entry: dict[str, Any]) -> bool:
    return _tracked(entry) in _FAILED_STATES


def _stuck(entry: dict[str, Any]) -> bool:
    """Downloaded, but the service will not put it in place by itself."""
    tracked = _tracked(entry)
    warned = str(entry.get("trackedDownloadStatus") or "").lower() in ("warning", "error")
    return tracked == "importblocked" or (tracked.startswith("import") and warned)


def _status(entries: list[dict[str, Any]]) -> str:
    """How the copy's downloads are getting on, the worst that is still coming first: "failed"
    only where none of them is still coming."""
    coming = [e for e in entries if not _failed(e)]
    if not coming:
        return "failed"
    if any(_stuck(e) for e in coming):
        return "stuck"
    if any(_tracked(e).startswith("import") for e in coming):
        return "importing"
    return "downloading"


def _progress(entries: list[dict[str, Any]]) -> int | None:
    size = sum(e.get("size") or 0 for e in entries)
    left = sum(e.get("sizeleft") or 0 for e in entries)
    if size <= 0:
        return None
    return max(0, min(99, round(100 * (size - left) / size)))


def _minutes_left(entries: list[dict[str, Any]]) -> int | None:
    """The longest time left the queue gives, in whole minutes; its form is "[d.]hh:mm:ss"."""
    longest = None
    for entry in entries:
        left = entry.get("timeleft")
        if not isinstance(left, str):
            continue
        days, _, clock = (
            left.rpartition(".")
            if left.count(":") == 2 and "." in left.split(":")[0]
            else ("", "", left)
        )
        try:
            h, m, sec = (int(x) for x in clock.split(":"))
            total = int(days or 0) * 1440 + h * 60 + m + sec / 60
        except ValueError:
            continue
        longest = total if longest is None else max(longest, total)
    return None if longest is None else round(longest)


def _asked_long_ago(copy: Asked) -> bool:
    try:
        asked = datetime.fromisoformat(copy.asked_at)
    except ValueError:
        return True
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=UTC)
    return datetime.now(UTC) - asked > _GIVEN_UP_AFTER


def _copies(
    request: Request,
    conn: sqlite3.Connection,
    username: str,
    item: PlexItem,
    audio: list[Track],
) -> list[dict[str, object]]:
    """The copies asked for of the item, as they are getting on; each settled as it is found
    in Plex with its language, or without it."""
    asked = language_copies.for_item(conn, item.rating_key, item.parent_rating_key)
    if not asked:
        return []
    have = _audio_languages(audio)
    part = item.media.part_id if item.media else None
    minutes: dict[int, int | None] = {}
    # One item, one service: its queue is read once for all the copies asked for.
    queue = _queue(_client(request, conn, item)) if any(c.state == "asked" for c in asked) else None
    shown: list[dict[str, object]] = []
    for copy in asked:
        status, progress = "lacks", None
        if copy.state == "asked":
            entries = _ours(queue, copy)
            # Only a file of this item's that changed since the ask tells how the copy came.
            changed = part is not None and copy.parts.get(item.rating_key) not in (None, part)
            if entries:
                status = _status(entries)
                if status != "failed":
                    coming = [e for e in entries if not _failed(e)]
                    progress = _progress(coming)
                    minutes[copy.id] = _minutes_left(coming) if status == "downloading" else None
            elif copy.language in have and (changed or item.rating_key in copy.lacked):
                language_copies.settle(conn, copy.id, "ready")
                continue
            elif changed:
                language_copies.settle(conn, copy.id, "lacks")
            elif _asked_long_ago(copy):
                language_copies.settle(conn, copy.id, "gone")
                continue
            else:
                status = "waiting"
        elif copy.language in have:
            continue
        shown.append(
            {
                "id": copy.id,
                "language": copy.language,
                "scope": copy.scope,
                "status": status,
                "progress": progress,
                "minutes_left": minutes.get(copy.id),
                "release_languages": list(copy.release_languages),
                "quality": copy.quality,
                "size_bytes": copy.size_bytes,
                "mine": copy.username == username,
            }
        )
    # One row a language: the latest.
    latest: dict[object, dict[str, object]] = {}
    for copy_shown in shown:
        latest.setdefault(copy_shown["language"], copy_shown)
    return list(latest.values())


# ── Routes ──


def _language_list() -> list[dict[str, str]]:
    return [
        {"code": code, "name": LANGUAGES[code], "native": NATIVE_NAMES[code]}
        for code in SUBTITLE_LANGUAGES
    ]


@router.get("/api/watch/languages/{rating_key}")
def get_languages(
    request: Request, rating_key: str, username: str = Depends(get_current_user)
) -> Response:
    """The languages to offer for the title, the household's first, and the copies of it asked
    for with another audio language."""
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    item, audio, subtitles = title
    defaults = track_choices.get_defaults(conn, username)
    return respond_ok(
        {
            "kind": item.type,
            "season": item.parent_index,
            "languages": _language_list(),
            "subtitle_default": defaults.subtitle_lang,
            "recent": track_choices.recent_subtitle_languages(conn, username),
            "household_subtitles": track_choices.household_languages(conn, "subtitle"),
            "household_audio": track_choices.household_languages(conn, "audio"),
            "have": _have(item, audio),
            "copies": _copies(request, conn, username, item, audio),
        }
    )


def _found_subtitle(found: SubtitleFound) -> dict[str, object]:
    return {
        "file": found.file,
        "provider": found.provider,
        "hearing_impaired": found.hearing_impaired,
        "forced": found.forced,
    }


@router.get("/api/watch/languages/{rating_key}/subtitles")
def search_subtitles(
    request: Request, rating_key: str, language: str, username: str = Depends(get_current_user)
) -> Response:
    """The subtitles in *language* Plex can add to the title, best first."""
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    if language not in SUBTITLE_LANGUAGES:
        return respond_err("unknown_language", status=422)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    try:
        found = _open_plex(request, conn).search_subtitles(rating_key, language)
    except PlexError as exc:
        return plex_failure(exc)
    _keep_found((username, rating_key, "subtitle", language, ""), found)
    return respond_ok({"found": [_found_subtitle(f) for f in found]})


@router.post("/api/watch/languages/{rating_key}/subtitles")
def add_subtitle(
    request: Request, rating_key: str, body: _AddSubtitle, username: str = Depends(get_current_user)
) -> Response:
    """Add to the title the subtitle at *index* of what this person's last search in the
    language found; the id of its track, or null while Plex is still adding it."""
    if not _LIMITER.check(username):
        return _rate_limited()
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    conn = get_db()
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    found = _was_found((username, rating_key, "subtitle", body.language, ""), body.index)
    if not isinstance(found, SubtitleFound):
        return respond_err("search_again", status=409)
    with _adding_lock:
        if _adding.get(username, 0) >= ADDS_AT_ONCE:
            return respond_err("adding_already", status=429)
        _adding[username] = _adding.get(username, 0) + 1
    try:
        subtitle = get_stream_manager().add_subtitle(rating_key, found.key)
    except PlexError as exc:
        return plex_failure(exc)
    finally:
        with _adding_lock:
            _adding[username] -= 1
            if not _adding[username]:
                del _adding[username]
    return respond_ok({"subtitle": subtitle})


def _copy(copy: Copy, have: list[str]) -> dict[str, object]:
    return {
        "languages": list(copy.languages),
        "quality": copy.quality,
        "size_bytes": copy.size_bytes,
        "age_days": copy.age_days,
        "indexer": copy.indexer,
        "season_pack": copy.season_pack,
        "loses": [code for code in have if code not in copy.languages],
    }


def _arr_title(
    request: Request, conn: sqlite3.Connection, rating_key: str, language: str, scope: str
) -> tuple[PlexItem, list[Track], ArrClient, _Target, Scope] | Response:
    if not _valid_key(rating_key):
        return respond_err("not_found", status=404)
    if language not in LANGUAGES:
        return respond_err("unknown_language", status=422)
    title = _title(request, conn, rating_key)
    if isinstance(title, Response):
        return title
    item, audio, _ = title
    wanted = _scope_of(item, scope)
    if wanted is None:
        return respond_err("bad_scope", status=422)
    client = _client(request, conn, item)
    if client is None:
        return respond_err("not_set_up", status=503)
    try:
        target = _target(conn, client, item)
    except _ARR_FAILURES:
        logger.warning("watch.languages: the service could not be read", exc_info=True)
        return respond_err("unreachable", status=502)
    if target is None:
        return respond_err("not_in_arr", status=404)
    return item, audio, client, target, wanted


@router.get("/api/watch/languages/{rating_key}/audio")
def search_copies(
    request: Request,
    rating_key: str,
    language: str,
    scope: Scope,
    username: str = Depends(get_current_user),
) -> Response:
    """The copies of the episode, its season or the movie that Sonarr or Radarr find with
    *language* among their audio, best first, and what each would lose of what plays now."""
    if not _COPY_SEARCH_LIMITER.check(username):
        return _rate_limited()
    conn = get_db()
    title = _arr_title(request, conn, rating_key, language, scope)
    if isinstance(title, Response):
        return title
    item, audio, client, target, wanted = title
    try:
        releases = client.find_releases(**_search_ids(target, wanted))
    except _ARR_FAILURES:
        logger.warning("watch.languages: the release search failed", exc_info=True)
        return respond_err("unreachable", status=502)
    found = copies_with(releases, language)
    _keep_found((username, rating_key, "audio", language, wanted), found)
    have = _have(item, audio)
    languages = have["languages"]
    assert isinstance(languages, list)
    return respond_ok(
        {
            "service": "Radarr" if target.service == "radarr" else "Sonarr",
            "have": have,
            "found": [_copy(c, languages) for c in found],
        }
    )


def _refused(exc: Exception) -> bool:
    """Whether the grab surely did not happen: the service said no, or was never reached. A
    late or broken answer may follow a grab the service did."""
    if isinstance(exc, SafeHTTPError):
        return 0 < exc.status_code < 500
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if isinstance(exc, requests.ConnectionError):
        reason = getattr(exc.args[0], "reason", None) if exc.args else None
        return isinstance(reason, NewConnectionError)
    return False


def _forget_failed(
    conn: sqlite3.Connection, client: ArrClient, item: PlexItem, language: str
) -> None:
    """A copy in *language* whose downloads all failed no longer stands in the way of another."""
    asked = [
        c
        for c in language_copies.for_item(conn, item.rating_key, item.parent_rating_key)
        if c.state == "asked" and c.language == language
    ]
    if not asked:
        return
    queue = _queue(client)
    for copy in asked:
        entries = _ours(queue, copy)
        if entries and _status(entries) == "failed":
            language_copies.settle(conn, copy.id, "gone")


@router.post("/api/watch/languages/{rating_key}/audio")
def get_copy(
    request: Request, rating_key: str, body: _GetCopy, username: str = Depends(get_current_user)
) -> Response:
    """Download the copy at *index* of what this person's last search found, as a new request
    is: it replaces the file of the episode, season or movie once it is in."""
    limited = _check_rate_limits(username, request)
    if limited is not None:
        return limited
    conn = get_db()
    title = _arr_title(request, conn, rating_key, body.language, body.scope)
    if isinstance(title, Response):
        return title
    item, audio, client, target, wanted = title
    _forget_failed(conn, client, item, body.language)
    if language_copies.asked_already(conn, item.rating_key, item.parent_rating_key, body.language):
        return respond_err("already_asked", status=409)
    key = (username, rating_key, "audio", body.language, wanted)
    taken = _take_found(key, body.index)
    if taken is None or not isinstance(taken[0], Copy):
        return respond_err("search_again", status=409)
    found, everything = taken
    try:
        parts = _parts(request, conn, item, wanted)
    except PlexError:
        _keep_found(key, everything)
        logger.warning("watch.languages: Plex could not be read for the copy", exc_info=True)
        return respond_err("unreachable", status=502)
    try:
        client.grab_release(found.guid, found.indexer_id)
    except _ARR_FAILURES as exc:
        if _refused(exc):
            _keep_found(key, everything)
            logger.warning("watch.languages: the copy could not be asked for", exc_info=True)
            return respond_err("unreachable", status=502)
        # The service had the ask but no answer came back in time, as when it is still fetching
        # the release from the indexer: kept as asked, so it is followed and not asked for twice.
        logger.warning("watch.languages: the copy was asked for without an answer", exc_info=True)
    language_copies.add(
        conn,
        username=username,
        rating_key=item.rating_key,
        season_key=item.parent_rating_key,
        scope=wanted,
        language=body.language,
        service=target.service,
        arr_id=target.arr_id,
        season_number=target.season_number,
        episode_id=target.episode_id,
        parts=parts,
        # Known of the item it was asked from alone: the others' tracks are not read.
        lacked=[] if body.language in _audio_languages(audio) else [item.rating_key],
        release_languages=found.languages,
        quality=found.quality,
        size_bytes=found.size_bytes,
    )
    name = item.grandparent_title or item.title
    with conn:
        log_audit(
            conn,
            item.rating_key if item.type == "movie" else item.parent_rating_key or item.rating_key,
            "downloaded",
            f"Asked for a copy of '{name}' with {LANGUAGES[body.language]} audio by {username}",
            actor=username,
        )
    watch_catalog.invalidate()
    return respond_ok({"copies": _copies(request, conn, username, item, audio)})
