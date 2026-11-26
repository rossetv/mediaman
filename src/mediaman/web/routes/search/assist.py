"""The AI in search, for one person at a time: their recent searches, phrases to try, titles
for a question or like a title, and what fits an evening.

Ordinary search is elsewhere and never waits for this. A question is answered in a
background thread and polled for. Without a key of OpenAI or TMDB, or with a daily limit of
0, nothing of the AI is offered; recent searches, Clear and the history switch work all the
same.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from mediaman.core.time import now_utc
from mediaman.db import get_db, open_thread_connection
from mediaman.services import watch_catalog as catalog
from mediaman.services.infra import disk_cache, get_bool_setting
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.openai import search_assist as ai
from mediaman.services.openai.client import OpenAIUnavailable, get_openai_key, key_refused
from mediaman.services.openai.recommendations.plays import person_plays
from mediaman.services.openai.search_assist import tonight
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.repository import search_assist as repo
from mediaman.web.repository import watch_library as lib
from mediaman.web.repository.recommended import fetch_recommendations, group_into_batches
from mediaman.web.repository.seen_titles import PersonMarks, person_marks
from mediaman.web.repository.watch_progress import continue_watching
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.watch.card_marks import CardMarks, card_marks, marked, tmdb_ref
from mediaman.web.routes.watch.catalog_api import _arr, held_fields
from mediaman.web.routes.watch.pages import (
    can_download,
    play_href,
    poster_url,
    progress_fields,
)

logger = logging.getLogger(__name__)

router = APIRouter()

MIN_WORDS = 3
_ACCOUNT_HEADER = "X-Assist-Account"
_OWNED_CHIP = "Only in your library"
# Plays worth making phrases from, when nothing was searched for.
_MIN_PLAYS = 3
_TMDB_TIMEOUT_S = 5.0
_LIKE_REF_RE = re.compile(r"^(movie|show)-([0-9]{1,18})$|^tmdb-(movie|tv)-([0-9]{1,10})$")

_WRITE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)
_ASK_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)
_TONIGHT_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)
_ELSEWHERE_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)


@dataclass(slots=True)
class _Asked:
    """What one run was asked, by digest, and what it answered. *base* is the answer before
    narrowing. In memory: a run dies with the process, and an answer of someone whose history
    is not used is kept nowhere else."""

    user_id: int
    kind: str
    key: str
    base: str
    at: datetime
    answer: dict[str, Any] | None = None
    key_refused: bool = False


_asked: dict[int, _Asked] = {}
_ASKED_FOR = timedelta(hours=1)
_ASKED_MAX = 512
_asked_lock = threading.Lock()


class _SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Annotated[str, Field(max_length=1000)]


class _AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Annotated[str, Field(max_length=repo.PHRASE_MAX)]
    refine: Annotated[str, Field(max_length=60)] = ""


class _LikeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: Annotated[str, Field(max_length=40)]
    refine: Annotated[str, Field(max_length=60)] = ""


class _HistoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def _secret(request: Request) -> str:
    return cast(str, request.app.state.config.secret_key)


def _has_openai_key(request: Request, conn: sqlite3.Connection) -> bool:
    try:
        get_openai_key(conn, _secret(request))
    except ValueError:
        return False
    return True


def _client(request: Request, conn: sqlite3.Connection) -> TmdbClient | None:
    """TMDB's client where the AI can be used: it answers only with titles TMDB knows. Not
    while OpenAI is known to refuse the key."""
    if (
        repo.switched_off(conn)
        or not _has_openai_key(request, conn)
        or key_refused(conn, _secret(request))
    ):
        return None
    return TmdbClient.from_db(conn, _secret(request), timeout=_TMDB_TIMEOUT_S, retry=False)


def _forget(user_id: int) -> None:
    with _asked_lock:
        for run_id in [r for r, asked in _asked.items() if asked.user_id == user_id]:
            del _asked[run_id]


def _sweep(conn: sqlite3.Connection) -> None:
    """Forget what is past its time, or of an account that is gone with its runs."""
    # Before the ids are read: a run begun meanwhile is in neither.
    with _asked_lock:
        held = dict(_asked)
    if not held:
        return
    there, since = repo.runs_there(conn), now_utc() - _ASKED_FOR
    with _asked_lock:
        for run_id, asked in held.items():
            if run_id not in there or asked.at < since:
                _asked.pop(run_id, None)


def _fetch(conn: sqlite3.Connection, user_id: int, kind: str, key: str) -> dict[str, Any] | None:
    stored = repo.fetch_answer(conn, user_id, kind, key, now_utc())
    if stored is not None:
        return cast(dict[str, Any], stored)
    with _asked_lock:
        found = [
            a.answer
            for a in _asked.values()
            if a.answer and (a.user_id, a.kind, a.key) == (user_id, kind, key)
        ]
    return found[-1] if found else None


def _digest(request: Request, *parts: str) -> str:
    """What a question is found again by; its words are not kept."""
    text = "\n".join(parts)
    return hmac.new(_secret(request).encode(), text.encode(), hashlib.sha256).hexdigest()


def _person(username: str) -> tuple[sqlite3.Connection, int | None]:
    conn = get_db()
    _sweep(conn)
    return conn, repo.user_id_of(conn, username)


def _account(request: Request, user_id: int, username: str) -> str:
    """Whose page this is, for what it remembers and sends: it says nothing of the account."""
    return _digest(request, "account", str(user_id), username)[:32]


def _of_another(request: Request, user_id: int, username: str) -> JSONResponse | None:
    """The refusal of what a page still open of someone else sends. A page that does not say
    whose it is is served."""
    said = request.headers.get(_ACCOUNT_HEADER)
    if not said or hmac.compare_digest(
        said.encode(), _account(request, user_id, username).encode()
    ):
        return None
    return respond_err("other_account", status=409)


def _unavailable() -> JSONResponse:
    return respond_err("unavailable", status=409)


def _not_found() -> JSONResponse:
    return respond_err("not_found", status=404)


def _too_many() -> JSONResponse:
    return respond_err("too_many_requests", status=429)


def _refused(refused: repo.Refused) -> JSONResponse:
    if refused.reason in ("running", "stopping"):
        return respond_err(refused.reason, status=409, run_id=refused.run_id)
    limit: dict[str, Any] = {} if refused.limit is None else {"limit": refused.limit}
    return respond_err(refused.reason, status=429, resets_at=refused.resets_at, **limit)


# --- Recent searches, phrases and the history switch -----------------------------


def _records(request: Request, conn: sqlite3.Connection, user_id: int) -> bool:
    """Whether the person's searches are kept: never where nothing could use them, since the
    switch that stops it is not shown there."""
    return _has_openai_key(request, conn) and repo.uses_history(conn, user_id)


@router.get("/api/search/assist")
def api_assist(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    """What the search panel shows at once: recent searches and the phrases made before. New
    phrases are made in the background when they are due."""
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    repo.drop_expired(conn, now_utc())
    available = _client(request, conn) is not None
    phrases = repo.stored_phrases(conn, user_id) if available else []
    if available:
        _make_phrases_later(request, conn, user_id, username)
    return respond_ok(
        {
            "account": _account(request, user_id, username),
            "available": available,
            "history": repo.uses_history(conn, user_id),
            "records": _records(request, conn, user_id),
            "recent": repo.recent_searches(conn, user_id, repo.RECENT_SHOWN),
            "personal": bool(phrases),
            "phrases": phrases or list(ai.FIXED_PHRASES),
            "min_words": MIN_WORDS,
        }
    )


@router.post("/api/search/assist/searches")
def api_record_search(
    body: _SearchBody, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    refused = _of_another(request, user_id, username)
    if refused is not None:
        return refused
    if not _WRITE_LIMITER.check(username):
        return _too_many()
    recorded = _records(request, conn, user_id) and repo.record_search(
        conn, user_id, body.q, now_utc()
    )
    return respond_ok({"recorded": recorded})


@router.delete("/api/search/assist/searches")
def api_clear_searches(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    refused = _of_another(request, user_id, username)
    if refused is not None:
        return refused
    repo.clear_searches(conn, user_id)
    _forget(user_id)
    return respond_ok()


@router.get("/api/search/assist/history")
def api_history(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    return respond_ok(
        {
            "enabled": repo.uses_history(conn, user_id),
            # Recommendations use it too, with neither TMDB nor the AI in search.
            "available": _has_openai_key(request, conn),
        }
    )


@router.put("/api/search/assist/history")
def api_set_history(body: _HistoryBody, username: str = Depends(get_current_user)) -> JSONResponse:
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    repo.set_uses_history(conn, user_id, body.enabled)
    if not body.enabled:
        _forget(user_id)
    return respond_ok({"enabled": body.enabled})


def _start_thread(
    request: Request,
    user_id: int,
    run_id: int,
    work: Callable[[sqlite3.Connection, Callable[[], bool]], None],
) -> None:
    """Do *work* of the run in the background; it is given its own connection, and what says
    whether the run was ended meanwhile. Not ended by it, the run failed."""
    db_path = request.app.state.db_path

    def run() -> None:
        try:
            conn = open_thread_connection(db_path)
        except sqlite3.Error:
            logger.warning("search assist: the database could not be opened", exc_info=True)
            return
        try:
            work(
                conn,
                lambda: repo.run_status(conn, user_id, run_id, now_utc()) != "running",
            )
        except Exception:  # rationale: background job runner; the run is recorded as failed
            logger.exception("search assist: a run failed")
        finally:
            try:
                repo.call_ended(conn, run_id, now_utc())
            except sqlite3.Error:  # released as stale later
                logger.warning("search assist: a run could not be ended", exc_info=True)
            finally:
                conn.close()

    threading.Thread(target=run, name="search-assist", daemon=True).start()


def _played(conn: sqlite3.Connection, username: str) -> list[str]:
    return [p.title for p in person_plays(conn, username)]


def _make_phrases_later(
    request: Request, conn: sqlite3.Connection, user_id: int, username: str
) -> None:
    if not repo.phrases_due(conn, user_id, now_utc()):
        return
    taste = repo.taste_of(conn, user_id, _played(conn, username))
    if not taste.searches and len(taste.played) < _MIN_PLAYS:
        return
    run_id = repo.start_run(conn, user_id, repo.PHRASES, now_utc())
    if isinstance(run_id, repo.Refused):
        return
    secret_key = _secret(request)

    def work(own: sqlite3.Connection, stopped: Callable[[], bool]) -> None:
        # Read only now: what is cleared from here on stops the run.
        fresh = repo.taste_of(own, user_id, _played(own, username))
        if fresh.is_empty or stopped():
            return
        phrases = ai.make_phrases(own, fresh, secret_key=secret_key)
        if phrases:
            repo.finish_with_phrases(own, user_id, run_id, phrases, now_utc())

    _start_thread(request, user_id, run_id, work)


# --- Questions -------------------------------------------------------------------


@router.post("/api/search/assist/ask")
def api_ask(
    body: _AskBody, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """Titles for a question of three words or more, or for it narrowed to a chip of its
    answer: at once when answered before, else a run to poll."""
    text = repo.tidy(body.q)
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    refused = _of_another(request, user_id, username)
    if refused is not None:
        return refused
    client = _client(request, conn)
    if client is None:
        return _unavailable()
    if ai.words(text) < MIN_WORDS:
        return respond_err("too_short", status=400)
    question = ai.Question(text=text)
    return _answer(
        request, conn, client, user_id, username, "ask", text.casefold(), question, body.refine
    )


@router.post("/api/search/assist/like")
def api_like(
    body: _LikeBody, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """As ``ask``, for titles like one of the library or of TMDB."""
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    refused = _of_another(request, user_id, username)
    if refused is not None:
        return refused
    client = _client(request, conn)
    if client is None:
        return _unavailable()
    try:
        # Only a title TMDB is asked for counts against the limit; a cached one is free.
        question = _like_question(conn, client, body.ref, lambda: _ASK_LIMITER.check(username))
    except disk_cache.Refused:
        return _too_many()
    except catalog.TmdbUnavailable:
        return respond_err("tmdb_request_failed", status=502)
    if question is None:
        return _not_found()
    return _answer(
        request, conn, client, user_id, username, "like", body.ref, question, body.refine
    )


def _index(conn: sqlite3.Connection, client: TmdbClient) -> catalog.LibraryIndex:
    libraries = lib.scanned_library_ids(conn)
    return catalog.library_index(conn, client, libraries, lib.library_movies, lib.library_shows)


def _tmdb_by_ref(index: catalog.LibraryIndex) -> dict[str, tuple[str, int]]:
    return {
        ref: (kind, tmdb_id)
        for kind, refs in (("movie", index.movies_by_tmdb), ("tv", index.shows_by_tmdb))
        for tmdb_id, ref in refs.items()
    }


def _like_question(
    conn: sqlite3.Connection, client: TmdbClient, ref: str, allow_fetch: Callable[[], bool]
) -> ai.Question | None:
    match = _LIKE_REF_RE.fullmatch(ref)
    if match is None:
        return None
    if match[1]:
        libraries = lib.scanned_library_ids(conn)
        find = lib.find_movie if match[1] == "movie" else lib.find_show
        held = find(conn, libraries, match[2])
        if held is None:
            return None
        title, year = held.title, held.year
        kind = "movie" if match[1] == "movie" else "tv"
        tmdb_id = _tmdb_by_ref(_index(conn, client)).get(ref, (kind, None))[1]
    else:
        kind, tmdb_id = match[3], int(match[4])
        try:
            found = catalog.details(client, kind, tmdb_id, allow_fetch=allow_fetch)
        except catalog.TitleNotFound:
            return None
        title, year = found["title"], found["year"]
    if not ai.words(title):
        return None
    return ai.Question(like=title, like_year=year, like_key=(kind, tmdb_id))


def _answer(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    user_id: int,
    username: str,
    kind: str,
    key: str,
    question: ai.Question,
    refine: str,
) -> JSONResponse:
    base = None
    asked_key = key
    key = base_key = _digest(request, kind, asked_key)
    if refine:
        # Only a chip that was offered: it is words of the server's own answer.
        base = _fetch(conn, user_id, kind, key)
        if base is None or refine not in [*base["chips"], _OWNED_CHIP]:
            return _not_found()
        if refine == _OWNED_CHIP:
            return _done(request, conn, client, user_id, username, base, base, owned_only=True)
        key = _digest(request, kind, asked_key, refine.casefold())
        question = ai.Question(
            text=question.text,
            like=question.like,
            like_year=question.like_year,
            like_key=question.like_key,
            refine=refine,
            narrowing=tuple(t["title"] for t in base["titles"]),
        )
    stored = _fetch(conn, user_id, kind, key)
    if stored is not None:
        return _done(request, conn, client, user_id, username, stored, base or stored)
    if not _ASK_LIMITER.check(username):
        return _too_many()
    run_id = repo.start_run(conn, user_id, kind, now_utc())
    if isinstance(run_id, repo.Refused):
        going = _asked.get(run_id.run_id) if run_id.run_id is not None else None
        if going is not None and (going.kind, going.key) == (kind, key):
            return respond_ok({"status": "running", "run_id": run_id.run_id}, status=202)
        return _refused(run_id)
    with _asked_lock:
        while len(_asked) >= _ASKED_MAX:
            del _asked[next(iter(_asked))]
        asked = _asked[run_id] = _Asked(user_id, kind, key, base_key, now_utc())
    secret_key = _secret(request)

    def work(own: sqlite3.Connection, stopped: Callable[[], bool]) -> None:
        def taste() -> ai.Taste:
            return repo.taste_of(own, user_id, _played(own, username))

        try:
            made = ai.make_answer(
                own, client, question, taste, secret_key=secret_key, stopped=stopped
            )
        except OpenAIUnavailable as exc:
            asked.key_refused = exc.refused
            return
        if made is None:
            return
        asked.answer = cast(dict[str, Any], made)
        if not repo.finish_with_answer(own, user_id, run_id, kind, key, made, now_utc()):
            asked.answer = None

    _start_thread(request, user_id, run_id, work)
    return respond_ok({"status": "running", "run_id": run_id}, status=202)


@router.get("/api/search/assist/runs/{run_id}")
def api_run(
    run_id: int, request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    client = _client(request, conn)
    if client is None:
        return _unavailable()
    status = repo.run_status(conn, user_id, run_id, now_utc())
    asked = _asked.get(run_id)
    if status is None or (status == "done" and asked is None):
        return _not_found()
    if status == "failed" and asked is not None and asked.key_refused:
        return respond_ok({"status": status, "run_id": run_id, "error": "unavailable"})
    if status != "done" or asked is None:
        return respond_ok({"status": status, "run_id": run_id})
    stored = _fetch(conn, user_id, asked.kind, asked.key)
    if stored is None:
        return _not_found()
    base = _fetch(conn, user_id, asked.kind, asked.base)
    return _done(request, conn, client, user_id, username, stored, base or stored)


@router.delete("/api/search/assist/runs/{run_id}")
def api_stop(run_id: int, username: str = Depends(get_current_user)) -> JSONResponse:
    """Stop a run: it keeps nothing, counts as a question asked, and holds its place until
    the call it made has ended."""
    conn, user_id = _person(username)
    if user_id is None or repo.run_status(conn, user_id, run_id, now_utc()) is None:
        return _not_found()
    repo.stop_run(conn, user_id, run_id)
    with _asked_lock:
        _asked.pop(run_id, None)
    return respond_ok()


def _done(
    request: Request,
    conn: sqlite3.Connection,
    client: TmdbClient,
    user_id: int,
    username: str,
    stored: dict[str, Any],
    base: dict[str, Any],
    *,
    owned_only: bool = False,
) -> JSONResponse:
    """The stored answer as it stands now: what the library holds may have changed since, and
    so may the person's ratings. The chips are those of *base*, the answer before narrowing."""
    libraries = lib.scanned_library_ids(conn)
    index = _index(conn, client)
    arr = _arr(request, conn)
    held = held_fields(conn, username, libraries)
    own = person_marks(conn, username)
    marks = card_marks(own, client)

    def cards(answer: dict[str, Any]) -> list[dict[str, object]]:
        kept = [
            (level < 0, t)
            for t in answer["titles"]
            if (level := own.levels.get((t["media_type"], t["tmdb_id"]), 0)) != -2
        ]
        found = [_card(t, index, arr, held, marks) for _, t in sorted(kept, key=lambda k: k[0])]
        return [c for c in found if c is not None]

    shown = cards(stored)
    before = shown if stored is base else cards(base)
    chips = list(base["chips"])
    if len({c["in_library"] for c in before}) == 2:
        chips.append(_OWNED_CHIP)
    return respond_ok(
        {
            "status": "done",
            "owned": [c for c in shown if c["in_library"]],
            "others": [c for c in shown if not (c["in_library"] or owned_only)],
            "chips": chips,
            "instead": stored["instead"],
        }
    )


def _card(
    title: dict[str, Any],
    index: catalog.LibraryIndex,
    arr: catalog.ArrView,
    held: Callable[[str | None], dict[str, object]],
    marks: CardMarks,
) -> dict[str, object] | None:
    card = catalog.tmdb_card(
        {
            "id": title["tmdb_id"],
            "media_type": title["media_type"],
            "title": title["title"],
            "release_date": str(title["year"] or ""),
            "poster_path": title["poster_path"],
            "vote_average": title["vote_average"],
        },
        index,
        arr,
    )
    if card is None:
        return None
    ref = cast(str | None, card["library_ref"])
    film = ref.removeprefix("movie-") if ref and ref.startswith("movie-") else None
    return {
        **marked({**card, **held(ref)}, marks),
        "minutes": title["minutes"],
        "season_count": title["season_count"],
        "genre": title["genre"],
        "reason": title["reason"],
        "play_href": play_href(film) if film else None,
    }


# --- Tonight -----------------------------------------------------------------------


@router.get("/api/search/assist/tonight")
def api_tonight(
    request: Request,
    minutes: Annotated[int, Query(ge=15, le=720)],
    skip: Annotated[int, Query(ge=0, le=3000)] = 0,
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """A set of what the person can play next of the library that fits in *minutes*: what they
    are partway through, then the next episode of a show they saw some of, then what they never
    began; with *skip*, the set after those."""
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    client = _client(request, conn)
    if client is None:
        return _unavailable()
    if not _TONIGHT_LIMITER.check(username):
        return _too_many()
    index = _index(conn, client)
    own = person_marks(conn, username)
    plays = person_plays(conn, username)
    found = tonight.evening(
        client,
        _candidates(conn, username, index, own, {(p.kind, p.tmdb_id) for p in plays}),
        tonight.known_titles(conn),
        _enjoyed(conn, user_id, plays, own.levels),
        minutes,
        skip=skip,
    )
    held = held_fields(conn, username, lib.scanned_library_ids(conn))
    return respond_ok(
        {
            "minutes": minutes,
            "items": [_tonight_card(c, held) for c in found.titles],
            "total": found.total,
            "more": found.total > skip + len(found.titles),
            "shortest": _tonight_card(found.shortest, held) if found.shortest else None,
        }
    )


@router.get("/api/search/assist/tonight/elsewhere")
def api_tonight_elsewhere(
    request: Request,
    minutes: Annotated[int, Query(ge=15, le=720)],
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """A set of titles the library does not hold that fit in *minutes*, a show by its
    episodes: of the picks made for the person, then of Trending and Popular. None they saw,
    played or rated a strong no. Each says whether it can be downloaded here."""
    conn, user_id = _person(username)
    if user_id is None:
        return _not_found()
    client = _client(request, conn)
    if client is None:
        return _unavailable()
    if not _ELSEWHERE_LIMITER.check(username):
        return _too_many()
    own = person_marks(conn, username)
    plays = person_plays(conn, username)
    done = {(p.kind, p.tmdb_id) for p in plays} | set(own.seen)
    done |= {key for key, level in own.levels.items() if level == -2}
    try:
        shelves = [item for _, _, items in catalog.discover_shelves(client) for item in items]
    except catalog.TmdbUnavailable:
        shelves = []
    index = _index(conn, client)
    arr = _arr(request, conn)
    cards: dict[tuple[str, int], dict[str, object]] = {}
    picks: list[dict[str, object]] = []
    listed: list[dict[str, object]] = []
    for item, into in [(i, picks) for i in _picks(conn, username, own)] + [
        (i, listed) for i in shelves
    ]:
        card = catalog.tmdb_card(item, index, arr)
        if card is None or card["in_library"]:
            continue
        key = ("movie" if card["kind"] == "movie" else "tv", cast(int, item["id"]))
        if key in done or key in cards:
            continue
        cards[key] = card
        into.append({"id": key[1], "media_type": key[0]})
    can = can_download(conn, _secret(request))
    marks = card_marks(own, client)
    found = tonight.elsewhere(
        client, picks, listed, _enjoyed(conn, user_id, plays, own.levels), minutes
    )
    items = []
    for title, run in found:
        card = cards[(str(title["media_type"]), cast(int, title["id"]))]
        items.append(
            {
                **marked({**card, **lib.mark_fields(None, set())}, marks),
                "minutes": run,
                "can_download": can[cast(str, card["kind"])],
            }
        )
    return respond_ok({"minutes": minutes, "items": items})


def _picks(conn: sqlite3.Connection, username: str, own: PersonMarks) -> list[dict[str, object]]:
    """The picks made for *username*, the newest first, as TMDB lists a title."""
    if not get_bool_setting(conn, "suggestions_enabled", default=True):
        return []
    batches = group_into_batches(
        fetch_recommendations(conn, username), now_utc().date(), own.levels, own.seen
    )
    return [
        {
            "id": pick["tmdb_id"],
            "media_type": pick["media_type"],
            "title": pick["title"],
            "release_date": str(pick["year"] or ""),
            "poster_path": pick["poster_url"],
            "vote_average": pick.get("rating"),
        }
        for batch in batches
        for pick in cast(list[dict[str, Any]], batch["items"])
        if isinstance(pick.get("tmdb_id"), int) and pick["media_type"] in ("movie", "tv")
    ]


def _candidates(
    conn: sqlite3.Connection,
    username: str,
    index: catalog.LibraryIndex,
    own: PersonMarks,
    played: set[tuple[str, int | None]],
) -> list[tonight.Candidate]:
    """What *username* can play next of the library: the latest they played first."""
    libraries = lib.scanned_library_ids(conn)
    resumed = continue_watching(conn, username)
    ahead = repo.episodes_ahead(conn, username, libraries, resumed)
    partway = {
        c.progress.media_item_id or c.progress.rating_key: c.progress
        for c in resumed
        if c.progress.show_rating_key is None
    }
    seen = lib.seen_films(conn, username, libraries) | repo.films_seen_in_plex(conn, username)
    keys = _tmdb_by_ref(index)
    found: list[tuple[str, tonight.Candidate]] = []
    for m in lib.library_movies(conn, libraries):
        ref = f"movie-{m.key}"
        key = keys.get(ref)
        if key is None or own.levels.get(key) == -2:
            continue
        level = own.levels.get(key, 0)
        p = partway.get(m.key)
        if p is not None:
            left = progress_fields(p)[1]
            found.append(
                (
                    p.updated_at,
                    tonight.Candidate(
                        ref, key, m.title, m.year, run=left, tier=0, left=bool(left), level=level
                    ),
                )
            )
        elif m.key not in seen and key not in own.seen and key not in played:
            found.append(("", tonight.Candidate(ref, key, m.title, m.year, level=level)))
    for s in lib.library_shows(conn, libraries):
        ref = f"show-{s.key}"
        key = keys.get(ref)
        next_up = ahead.get(s.key)
        if key is None or next_up is None or own.levels.get(key) == -2:
            continue
        found.append(
            (
                next_up.at,
                tonight.Candidate(
                    ref,
                    key,
                    s.title,
                    s.year,
                    episode=(next_up.season, next_up.number),
                    run=next_up.minutes,
                    tier=next_up.tier,
                    left=next_up.left,
                    level=own.levels.get(key, 0),
                ),
            )
        )
    found.sort(key=lambda f: f[0], reverse=True)
    return [c for _, c in found]


def _enjoyed(
    conn: sqlite3.Connection,
    user_id: int,
    plays: list[Any],
    levels: dict[tuple[str, int], int],
) -> dict[tuple[str, int], str]:
    """Nothing when the person's history is not to be used."""
    if not repo.uses_history(conn, user_id):
        return {}
    lately = [p for p in plays[:40] if p.tmdb_id and levels.get((p.kind, p.tmdb_id), 0) >= 0]
    lately.sort(key=lambda p: -levels.get((p.kind, p.tmdb_id), 0))
    return {(p.kind, p.tmdb_id): p.title for p in lately}


def _tonight_card(
    c: tonight.Candidate, held: Callable[[str | None], dict[str, object]]
) -> dict[str, object]:
    kind, _, key = c.ref.partition("-")
    href = catalog.library_href(c.ref) or "/"
    return {
        **held(c.ref),
        "ref": c.ref,
        "kind": kind,
        "title": c.title,
        "year": c.year,
        "minutes": c.minutes,
        "season": c.episode[0] if c.episode else None,
        "episode": c.episode[1] if c.episode else None,
        "partway": c.tier == 0,
        "left": c.left,
        "genre": next(iter(c.facts.genres), None) if c.facts else None,
        "reason": c.reason,
        "poster_url": poster_url(key),
        "href": f"{href}?season={c.episode[0]}" if c.episode else href,
        "in_library": True,
        "tmdb_ref": tmdb_ref(c.key),
        "level": c.level or None,
    }
