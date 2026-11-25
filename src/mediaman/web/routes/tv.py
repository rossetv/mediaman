"""TVs paired with a code, the streams started on them, and the remote.

The TV's own routes are under ``/api/tv/device``. They read no cookie: a token in a header, which
no other site can send, is all that counts, and it opens those routes alone. Whatever a token is
refused for, the answer is the same 404 (429 once an address fails too often). A person's routes
are under ``/api/tvs``; a TV of someone else's is answered as one that never was.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import sqlite3
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, replace
from functools import partial
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.cast.tickets import TICKET_SECONDS
from mediaman.services.media_meta.plex_quality import TvQuality
from mediaman.services.media_meta.plex_stream import (
    StreamGone,
    StreamManager,
    StreamReplaced,
    get_stream_manager,
)
from mediaman.services.media_meta.plex_watch import PlexError, PlexItem, PlexWatch, is_in_libraries
from mediaman.services.rate_limit import ActionRateLimiter, RateLimiter, get_client_ip
from mediaman.services.tv.codes import CODE_SECONDS, get_codes
from mediaman.services.tv.players import (
    Choice,
    Left,
    NotPlaying,
    Play,
    Players,
    Report,
    Reported,
    Stale,
    TvOff,
    get_players,
)
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import tv as repo
from mediaman.web.repository.watch_progress import get_progress
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.cast import _CastRequest
from mediaman.web.routes.cast import _start as _start_cast
from mediaman.web.routes.watch.browse_api import api_watch_thumb
from mediaman.web.routes.watch.pages import resumable
from mediaman.web.routes.watch.stream_api import (
    _gone_answer,
    _guarded_start,
    _open_plex,
    _save,
    _scanned_library_ids,
    _StreamRoute,
    plex_failure,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=_StreamRoute)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CODE_RE = re.compile(r"[0-9]{6}")
_POLL_MS = {"pairing": 2000, "idle": 3000, "playing": 1000}

# What follows may take Plex long to say: apart from the server's worker threads, and from a
# start's answer.
_LOOKUPS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tv-next")
_LOOKUP_WAIT_S = 4.0

# The TVs of a home share an address.
_CODE_LIMITER = RateLimiter(max_attempts=30, window_seconds=600)
_POLL_LIMITER = RateLimiter(max_attempts=150, window_seconds=60)
_ASK_LIMITER = RateLimiter(max_attempts=30, window_seconds=60)
_FAILURE_LIMITER = RateLimiter(max_attempts=60, window_seconds=60)
# Only wrong codes count: there are a million, and a hundred may be open.
_WRONG_LIMITER = ActionRateLimiter(max_in_window=10, window_seconds=300)
_WRONG_ADDRESS_LIMITER = RateLimiter(max_attempts=30, window_seconds=300)
_ALL_WRONG_LIMITER = ActionRateLimiter(max_in_window=100, window_seconds=300)
_READ_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)
_COMMAND_LIMITER = ActionRateLimiter(max_in_window=240, window_seconds=60)
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)

_Username = Annotated[str, Depends(get_current_user)]
_Token = Annotated[str, Header(alias="X-TV-Token")]
_TvId = Annotated[int, Path(ge=1, le=2**53)]
_PlayId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
_Seconds = Annotated[float, Field(ge=0, le=1e6, allow_inf_nan=False)]
_Subtitle = Annotated[str, Field(pattern=r"^(off|[0-9]{1,18})$")]


def _clean_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or not name.isprintable():
        raise ValueError("not a name")
    return name


_Name = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_clean_name)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


_MAX_BODY_BYTES = 16 * 1024


class _ReportIn(_Body):
    play_id: _PlayId
    state: Reported
    position_s: _Seconds = 0
    played_s: _Seconds = 0
    volume: float = Field(default=1, ge=0, le=1)
    muted: bool = False
    can_volume: bool = False


class _Poll(_Body):
    ack: int = Field(default=0, ge=0, le=2**53)
    active: bool = False
    report: _ReportIn | None = None


class _Ask(_Body):
    play_id: _PlayId
    what: Literal["next", "auto", "again", "cancel", "stop"]


class _Pair(_Body):
    code: str = Field(max_length=20)
    confirm: bool = False
    name: _Name | None = None


class _Rename(_Body):
    name: _Name


class _PlayOn(_Body):
    rating_key: str = Field(pattern=r"^[0-9]{1,18}$")
    quality: TvQuality = "auto"
    subtitle: _Subtitle | None = None
    position_s: _Seconds | None = None
    autoplay: bool = True
    asked_s: _Seconds = 0


class _Command(_Body):
    kind: Literal[
        "play",
        "pause",
        "seek",
        "volume",
        "mute",
        "quality",
        "subtitles",
        "next",
        "again",
        "cancel",
        "stop",
    ]
    play_id: _PlayId | None = None
    position_s: _Seconds | None = None
    level: float | None = Field(default=None, ge=0, le=1)
    muted: bool | None = None
    quality: TvQuality | None = None
    subtitle: _Subtitle | None = None


def _read[B: _Body](model: type[B]) -> Callable[[Request], Coroutine[Any, Any, B]]:
    """A dependency that reads the body as *model*.

    FastAPI's own answer to a body it refuses repeats the body, and fails where that holds NaN.
    """

    async def read(request: Request) -> B:
        raw = await request.body()
        try:
            if len(raw) <= _MAX_BODY_BYTES:
                return model.model_validate_json(raw)
        except ValidationError:
            pass
        raise HTTPException(status_code=422, detail="Invalid body")

    return read


_PollBody = Annotated[_Poll, Depends(_read(_Poll))]
_AskBody = Annotated[_Ask, Depends(_read(_Ask))]
_PairBody = Annotated[_Pair, Depends(_read(_Pair))]
_NameBody = Annotated[_Rename, Depends(_read(_Rename))]
_PlayBody = Annotated[_PlayOn, Depends(_read(_PlayOn))]
_CommandBody = Annotated[_Command, Depends(_read(_Command))]


def _too_many(retry_after: int, error: str = "rate_limited") -> Response:
    response = respond_err(error, status=429)
    response.headers["Retry-After"] = str(retry_after)
    return response


def _refused(request: Request) -> Response:
    address = get_client_ip(request)
    if not _FAILURE_LIMITER.check(address):
        return _too_many(_FAILURE_LIMITER.retry_after(address))
    return respond_err("not_found", status=404)


def _carry_out(players: Players) -> None:
    """Save the places and end the streams that what plays on the TVs has left."""
    for left in players.drain():
        manager = get_stream_manager()
        try:
            _save_place(manager, left)
        except sqlite3.Error:
            # A place that cannot be saved must not leave a stream running.
            logger.exception("tv: could not save a place")
        if left.end:
            with contextlib.suppress(StreamGone):
                manager.end(left.stream_id, left.user)


def _save_place(manager: StreamManager, left: Left) -> None:
    if left.position_s is None:
        return
    with contextlib.suppress(StreamGone):
        try:
            stream = manager.get(left.stream_id, left.user)
        except StreamGone:
            stream = manager.reaped(left.stream_id, left.user)
        _save(stream, left.user, left.position_s, left.played_s)


def end_plays_of(username: str) -> None:
    """End what plays on *username*'s TVs, which stay paired."""
    players = get_players()
    players.end_plays_of(username)
    _carry_out(players)


# ── the TV ──


def _paired(token: str) -> repo.Tv | None:
    if not _TOKEN_RE.fullmatch(token):
        return None
    return repo.find_by_token(get_db(), hash_token(token))


@router.post("/api/tv/device/code")
def device_code(request: Request, x_tv_token: _Token = "") -> Response:
    """Give the TV a code to show and the token it will hold once the code is entered."""
    if not x_tv_token:
        return _refused(request)
    address = get_client_ip(request)
    if not _CODE_LIMITER.check(address):
        return _too_many(_CODE_LIMITER.retry_after(address))
    held = _paired(x_tv_token)
    token = secrets.token_urlsafe(32)
    code = get_codes().issue(hash_token(token), held.id if held is not None else None, address)
    return respond_ok({"code": code, "token": token, "expires_in_s": int(CODE_SECONDS)})


@router.post("/api/tv/device/poll")
def device_poll(request: Request, body: _PollBody, x_tv_token: _Token = "") -> Response:
    held = _paired(x_tv_token)
    key = hash_token(x_tv_token)
    if held is None:
        left = get_codes().seconds_left(key)
        if left is None:
            return _refused(request)
        if not _POLL_LIMITER.check(key):
            return _too_many(_POLL_LIMITER.retry_after(key))
        return respond_ok(
            {"state": "pairing", "expires_in_s": left, "poll_ms": _POLL_MS["pairing"]}
        )
    if not _POLL_LIMITER.check(key):
        return _too_many(_POLL_LIMITER.retry_after(key))
    players = get_players()
    _keep_alive(players, key)
    if players.due_stamp(key):
        repo.mark_seen(get_db(), held.id, now_utc())
    report = Report(**body.report.model_dump()) if body.report is not None else None
    play, commands = players.poll(key, ack=body.ack, active=body.active, report=report)
    _carry_out(players)
    return respond_ok(
        {
            "state": "ready",
            "id": held.id,
            "name": held.name,
            "owner": held.owner,
            "play": play,
            "commands": commands,
            "poll_ms": _POLL_MS["idle" if play is None else "playing"],
        }
    )


def _keep_alive(players: Players, key: str) -> None:
    """Count the poll as use of the stream, which a paused TV reads nothing of."""
    playing = players.stream(key)
    if playing is None:
        return
    play_id, stream_id, user = playing
    try:
        get_stream_manager().get(stream_id, user)
    except StreamGone as exc:
        players.stream_gone(key, play_id, moved=isinstance(exc, StreamReplaced))


@router.post("/api/tv/device/ask")
def device_ask(request: Request, body: _AskBody, x_tv_token: _Token = "") -> Response:
    """What the TV's own keys, and its countdown, ask for."""
    held = _paired(x_tv_token)
    if held is None:
        return _refused(request)
    key = hash_token(x_tv_token)
    if not _ASK_LIMITER.check(key):
        return _too_many(_ASK_LIMITER.retry_after(key))
    done = _carry_on(request, held, body.what, body.play_id, pressed=body.what != "again")
    return done if isinstance(done, Response) else respond_ok()


@router.post("/api/tv/device/unpair")
def device_unpair(request: Request, x_tv_token: _Token = "") -> Response:
    held = _paired(x_tv_token)
    if held is None:
        refused = _refused(request)
        # The TV is as unpaired as it asked to be.
        return refused if refused.status_code == 429 else respond_ok()
    _unpair(request, held, actor=held.owner or "")
    return respond_ok()


@router.get("/api/tv/device/art/{play_id}")
def device_art(request: Request, play_id: str) -> Response:
    """The picture of what plays, under a name only its TV and its person were told."""
    playing = get_players().art(play_id)
    if playing is None:
        return _refused(request)
    user, rating_key = playing
    return api_watch_thumb(request, rating_key, user)


# ── starting a stream on a TV ──


def _refusal(exc: Exception) -> Response:
    codes: dict[type[Exception], str] = {
        TvOff: "tv_off",
        NotPlaying: "not_playing",
        Stale: "stale",
    }
    return respond_err(codes[type(exc)], status=409)


def _title(item: PlexItem) -> dict[str, Any]:
    episode = item.type == "episode"
    return {
        "title": (item.grandparent_title or item.title) if episode else item.title,
        "year": item.year,
        "season": item.parent_index if episode else None,
        "episode": item.index if episode else None,
        "episode_title": item.title if episode else None,
        "duration_s": item.duration_s,
        "markers": [asdict(m) for m in item.markers],
        "chapters": [asdict(c) for c in item.chapters],
    }


def _following(plex: PlexWatch, rating_key: str) -> tuple[Any, dict[str, Any] | None]:
    """Whether an episode follows, and which."""
    try:
        episode = plex.next_episode(rating_key)
    except PlexError:
        return "unknown", None
    if episode is None:
        return "end", None
    return "next", {
        "rating_key": episode.rating_key,
        "season": episode.season_index,
        "episode": episode.index,
        "title": episode.title,
    }


def _look_up_next(plex: PlexWatch, key: str, play: Play) -> Future[None]:
    """Find what follows, for the play to learn."""

    def look() -> None:
        get_players().set_next(key, play.id, *_following(plex, play.rating_key))

    return _LOOKUPS.submit(look)


def _start_on(
    request: Request,
    held: repo.Tv,
    rating_key: str,
    choice: Choice,
    *,
    start_s: float | None = None,
    asked_s: float = 0,
    over: str | None = None,
    by_itself: bool = False,
) -> int | Response:
    """Play a title on the TV, as its owner's stream; the command's id, or the refusal.

    *start_s* None goes on from the saved place. With *over* it starts only while that plays.
    """
    user, key = held.owner, held.token_hash
    if user is None or key is None:
        return respond_err("not_found", status=404)
    players = get_players()
    body = _CastRequest(
        rating_key=rating_key,
        quality=choice.quality,  # type: ignore[arg-type]
        subtitle=choice.subtitle,
        page=f"tv-{held.id:06d}",
        asked_s=asked_s,
    )
    begun: list[Play] = []
    command: list[int] = []
    plex: list[PlexWatch] = []

    def go(manager: StreamManager, began: float) -> Response:
        conn = get_db()
        try:
            plex[:] = [_open_plex(request, conn)]
            item = plex[0].get_item(rating_key, marks=True)
        except PlexError as exc:
            return plex_failure(exc)
        if item.type not in ("movie", "episode") or not is_in_libraries(
            item, _scanned_library_ids(conn)
        ):
            return respond_err("not_found", status=404)
        place = start_s
        if place is None:
            saved = get_progress(conn, user, [rating_key]).get(rating_key)
            place = saved.position_s if saved is not None and resumable(saved) else 0
        if item.duration_s:
            place = min(place, item.duration_s)
        play = Play(
            user=user,
            rating_key=rating_key,
            about=_title(item),
            choice=choice,
            start_s=place,
            played_s=0,
            asked=players.now() - asked_s,
            next_state="end" if item.type == "movie" else "unknown",
        )
        command.append(players.begin(key, play, over=over, by_itself=by_itself))
        begun.append(play)
        # What played counts among the owner's streams until it has ended.
        _carry_out(players)
        answer = _start_cast(request, body, user, key, manager, began)
        if answer.status_code == 200:
            return _take_stream(answer, manager, held, play)
        return answer

    answer: Response | None = None
    try:
        answer = _guarded_start(body, user, go)
    except (TvOff, Stale) as exc:
        return _refusal(exc)
    finally:
        if begun and (answer is None or answer.status_code != 200):
            error = "unreachable" if answer is None else json.loads(bytes(answer.body))["error"]
            players.failed(key, begun[0].id, error)
            _carry_out(players)
    if answer.status_code != 200:
        return answer
    if begun[0].next_state == "unknown":
        _look_up_next(plex[0], key, begun[0])
    return command[0]


def _take_stream(answer: Response, manager: StreamManager, held: repo.Tv, play: Play) -> Response:
    """Hand the stream that started to the TV."""
    started = json.loads(bytes(answer.body))
    stream_id, user, key = started["stream_id"], play.user, held.token_hash or ""
    players = get_players()
    until = manager.now() + TICKET_SECONDS
    tracks = {name: started[name] for name in ("audio_tracks", "subtitle_tracks", "subtitle")}
    try:
        manager.allow(
            stream_id, user, partial(_may_go_on, key, user, play.id, lambda: manager.now() < until)
        )
        taken = players.loaded(key, play.id, stream_id=stream_id, url=started["url"], tracks=tracks)
    except StreamGone as exc:
        return _gone_answer(exc)
    if taken:
        return answer
    with contextlib.suppress(StreamGone):
        manager.end(stream_id, user)
    return respond_err("superseded", status=409)


def _may_go_on(key: str, user: str, play_id: str, in_time: Callable[[], bool]) -> bool:
    """Whether the reaper is to leave a TV's stream: its TV answers and is still the person's."""
    players = get_players()
    paired = repo.is_paired(get_db(), key, user)
    if not paired:
        players.forget(key, "unpaired")
    goes_on = paired and players.holds(key, play_id) and in_time()
    _carry_out(players)
    return goes_on


def _carry_on(
    request: Request,
    held: repo.Tv,
    what: str,
    play_id: str | None,
    *,
    pressed: bool,
    change: Callable[[Choice], Choice] = lambda choice: choice,
) -> int | Response:
    """Go on from what plays on the TV; the command's id, or the refusal."""
    players, key = get_players(), held.token_hash or ""
    try:
        if what == "stop":
            command = players.stop(key, play_id)
            _carry_out(players)
            return command
        if what == "cancel":
            players.cancel(key, play_id)
            return 0
        if what == "auto":
            play = players.due_next(key, play_id or "")
        else:
            play = players.current(key, play_id)
    except (NotPlaying, Stale) as exc:
        return _refusal(exc)
    if what in ("next", "auto"):
        return _start_next(request, held, play, by_itself=what == "auto")
    over = play.state in ("ended", "upnext", "still")
    if over and what == "restart":
        return _refusal(NotPlaying())
    return _start_on(
        request,
        held,
        play.rating_key,
        change(play.choice),
        start_s=0 if over else play.position_s,
        # What nobody pressed must not take the title from where it was asked for since.
        asked_s=0 if pressed else players.now() - play.asked,
        over=play.id,
    )


def _start_next(request: Request, held: repo.Tv, play: Play, *, by_itself: bool) -> int | Response:
    if play.next_state == "unknown":
        try:
            plex = _open_plex(request, get_db())
        except PlexError as exc:
            return plex_failure(exc)
        with contextlib.suppress(TimeoutError):
            _look_up_next(plex, held.token_hash or "", play).result(_LOOKUP_WAIT_S)
    item = play.next_item
    if item is None:
        if play.next_state == "unknown":
            return respond_err("unreachable", status=502)
        with contextlib.suppress(NotPlaying, Stale):
            get_players().cancel(held.token_hash or "", play.id)
        return respond_err("no_next", status=409)
    # Track ids are of one title; that subtitles are off is not.
    choice = play.choice
    if choice.subtitle != "off":
        choice = replace(choice, subtitle=None)
    return _start_on(request, held, item["rating_key"], choice, over=play.id, by_itself=by_itself)


# ── the person ──


def _not_found() -> Response:
    return respond_err("not_found", status=404)


def _own(username: str, tv_id: _TvId) -> repo.Tv | Response:
    """The person's paired TV; the answer to give where it is not theirs."""
    held = repo.find(get_db(), tv_id)
    if held is not None and held.owner == username and held.token_hash is not None:
        return held
    was = get_players().gone(username, tv_id)
    if was is None:
        return _not_found()
    reason, last = was
    return respond_err(reason, status=410, last=last)


def _limited(limiter: ActionRateLimiter, username: str) -> Response | None:
    return None if limiter.check(username) else _too_many(limiter.retry_after(username))


def _shown(held: repo.Tv) -> dict[str, Any]:
    return {
        "id": held.id,
        "name": held.name,
        "paired_at": held.paired_at,
        "last_seen_at": held.last_seen_at,
        **get_players().view(held.token_hash or ""),
    }


@router.post("/api/tvs/pair")
def pair_tv(request: Request, body: _PairBody, username: _Username) -> Response:
    """Pair the TV that shows the code with the person."""
    address = get_client_ip(request)
    limits: tuple[tuple[RateLimiter | ActionRateLimiter, str], ...] = (
        (_WRONG_LIMITER, username),
        (_WRONG_ADDRESS_LIMITER, address),
        (_ALL_WRONG_LIMITER, "all"),
    )
    counted: list[tuple[RateLimiter | ActionRateLimiter, str, float]] = []

    def uncount() -> None:
        for limiter, who, at in counted:
            limiter.release(who, at)

    for limiter, who in limits:
        at = limiter.reserve(who)
        if at is None:
            uncount()
            return _too_many(limiter.retry_after(who), "too_many_tries")
        counted.append((limiter, who, at))
    wrong = respond_err("wrong_code", status=404)
    code = "".join(body.code.split())
    asked = get_codes().look(code) if _CODE_RE.fullmatch(code) else None
    if asked is None:
        return wrong
    uncount()
    conn, players = get_db(), get_players()
    again = repo.find(conn, asked.tv_id) if asked.tv_id is not None else None
    others = again is not None and again.owner != username
    if again is not None and others and not body.confirm:
        playing = players.is_playing(again.token_hash or "")
        return respond_err("in_use", status=409, playing=playing)
    too_many = respond_err("too_many_tvs", status=409)
    if (again is None or others) and len(repo.owned(conn, username)) >= repo.MAX_TVS_PER_PERSON:
        return too_many
    if get_codes().take(code) != asked:
        return wrong
    try:
        paired = repo.pair(
            conn,
            username=username,
            token_hash=asked.token_hash,
            name=body.name,
            again=again,
            ip=address,
            now=now_utc(),
        )
    except repo.TooManyTvs:
        return too_many
    if paired is None:
        return wrong
    if again is not None:
        key = again.token_hash or ""
        if others:
            players.forget(key, "taken", again.id)
        else:
            players.forget(key, "stopped")
        _carry_out(players)
    return respond_ok({"tv": {"id": paired.id, "name": paired.name}})


@router.get("/api/tvs")
def own_tvs(username: _Username) -> Response:
    limited = _limited(_READ_LIMITER, username)
    if limited is not None:
        return limited
    players = get_players()
    shown = [_shown(held) for held in repo.owned(get_db(), username)]
    _carry_out(players)
    return respond_ok({"tvs": shown})


@router.get("/api/tvs/all")
def all_tvs(admin: Annotated[str, Depends(get_current_admin)]) -> Response:
    """Every TV, for its name and owner; what plays on it is its owner's alone."""
    limited = _limited(_READ_LIMITER, admin)
    if limited is not None:
        return limited
    players = get_players()
    return respond_ok(
        {
            "tvs": [
                {
                    "id": held.id,
                    "kind": held.kind,
                    "name": held.name,
                    "owner": held.owner,
                    "paired_at": held.paired_at,
                    "last_seen_at": held.last_seen_at,
                    "on": players.is_on(held.token_hash or ""),
                    "playing": players.is_playing(held.token_hash or ""),
                }
                for held in repo.every(get_db())
            ]
        }
    )


@router.get("/api/tvs/{tv_id}")
def own_tv(tv_id: _TvId, username: _Username) -> Response:
    limited = _limited(_READ_LIMITER, username)
    if limited is not None:
        return limited
    held = _own(username, tv_id)
    if isinstance(held, Response):
        return held
    shown = _shown(held)
    _carry_out(get_players())
    return respond_ok({"tv": shown})


def _own_or_admins(username: str, tv_id: _TvId) -> repo.Tv | None:
    held = repo.find(get_db(), tv_id)
    if held is None:
        return None
    if held.owner == username or get_user_role(get_db(), username) == "admin":
        return held
    return None


@router.patch("/api/tvs/{tv_id}")
def rename_tv(tv_id: _TvId, body: _NameBody, username: _Username) -> Response:
    limited = _limited(_CHANGE_LIMITER, username)
    if limited is not None:
        return limited
    held = _own_or_admins(username, tv_id)
    if held is None:
        return _not_found()
    repo.rename(get_db(), tv_id, body.name)
    return respond_ok({"tv": {"id": tv_id, "name": body.name}})


@router.delete("/api/tvs/{tv_id}")
def remove_tv(request: Request, tv_id: _TvId, username: _Username) -> Response:
    limited = _limited(_CHANGE_LIMITER, username)
    if limited is not None:
        return limited
    held = _own_or_admins(username, tv_id)
    if held is None:
        return _not_found()
    _unpair(request, held, actor=username)
    return respond_ok()


def _unpair(request: Request, held: repo.Tv, *, actor: str) -> None:
    if not repo.remove(get_db(), held, actor=actor, ip=get_client_ip(request)):
        return
    players = get_players()
    players.forget(held.token_hash or "", "unpaired", held.id)
    _carry_out(players)


@router.post("/api/tvs/{tv_id}/play")
def play_on_tv(request: Request, tv_id: _TvId, body: _PlayBody, username: _Username) -> Response:
    limited = _limited(_COMMAND_LIMITER, username)
    if limited is not None:
        return limited
    held = _own(username, tv_id)
    if isinstance(held, Response):
        return held
    choice = Choice(quality=body.quality, subtitle=body.subtitle, autoplay=body.autoplay)
    done = _start_on(
        request,
        held,
        body.rating_key,
        choice,
        start_s=body.position_s,
        asked_s=body.asked_s,
    )
    if isinstance(done, Response):
        return done
    return respond_ok({"tv": _shown(held)})


@router.post("/api/tvs/{tv_id}/commands")
def command_tv(request: Request, tv_id: _TvId, body: _CommandBody, username: _Username) -> Response:
    limited = _limited(_COMMAND_LIMITER, username)
    if limited is not None:
        return limited
    held = _own(username, tv_id)
    if isinstance(held, Response):
        return held
    done = _run(request, held, body)
    if isinstance(done, Response):
        return done
    return respond_ok({"id": done, "tv": _shown(held)})


def _run(request: Request, held: repo.Tv, body: _Command) -> int | Response:
    missing = respond_err("invalid", status=422)
    if body.kind == "quality":
        quality = body.quality
        if quality is None:
            return missing
        return _carry_on(
            request,
            held,
            "restart",
            body.play_id,
            pressed=True,
            change=lambda c: replace(c, quality=quality),
        )
    if body.kind == "subtitles":
        if body.subtitle is None:
            return missing
        return _carry_on(
            request,
            held,
            "restart",
            body.play_id,
            pressed=True,
            change=lambda c: replace(c, subtitle=body.subtitle),
        )
    if body.kind in ("next", "again", "cancel", "stop"):
        return _carry_on(request, held, body.kind, body.play_id, pressed=True)
    args: dict[str, Any] = {}
    if body.kind == "seek":
        args = {"position_s": body.position_s}
    elif body.kind == "volume":
        args = {"level": body.level}
    elif body.kind == "mute":
        args = {"muted": body.muted}
    if None in args.values():
        return missing
    try:
        return get_players().send(
            held.token_hash or "",
            body.kind,  # type: ignore[arg-type]
            body.play_id,
            **args,
        )
    except (NotPlaying, Stale, TvOff) as exc:
        return _refusal(exc)
