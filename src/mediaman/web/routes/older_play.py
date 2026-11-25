"""Playing on the older TVs, which are everyone's, for one person at a time.

A person's claim on such a TV stands in for a pairing, so that the remote's routes are the same.
It lasts until somebody takes the TV over, it is forgotten, older TVs are turned off, or the
session that made it ends.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse, Response

from mediaman.db import get_db
from mediaman.services.cast.tickets import Ticket
from mediaman.services.dlna.errors import AnotherTv, TvError, TvUnreachable
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Renderer
from mediaman.services.media_meta.plex_stream import StreamGone
from mediaman.services.media_meta.plex_watch import PlexError
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.services.tv import older
from mediaman.services.tv.older import CannotPlay, Claim, Driver
from mediaman.services.tv.players import NotPlaying, Players, get_players
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import tv as repo
from mediaman.web.repository.ratings import find_user_id
from mediaman.web.responses import respond_err
from mediaman.web.routes import older_reach as reaching
from mediaman.web.routes.cast import _may_still_watch
from mediaman.web.routes.older_reach import Refusal
from mediaman.web.routes.older_stream import film_media, hand_over, place_of, segments, sent

logger = logging.getLogger(__name__)

_CLAIM_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60)
_TAKING = threading.Lock()


@dataclass(frozen=True)
class Steering:
    """How the routes of the TVs go on from what plays; here so that they need not be imported."""

    carry_on: Callable[..., int | Response]
    carry_out: Callable[[Players], None]
    keep_alive: Callable[[Players, str], None]


def _active(key: str) -> bool:
    """Whether something plays that stopping would take from its person."""
    try:
        return get_players().current(key).state not in ("ended", "failed")
    except NotPlaying:
        return False


def held_by(conn: sqlite3.Connection, username: str, tv: repo.Tv) -> repo.Tv | None:
    """The older TV as *username*'s own, with a key where they have a claim on it."""
    user_id = find_user_id(conn, username)
    if user_id is None or not reaching.are_on(conn):
        return None
    claim = older.get_claims().of(tv.id)
    if claim is not None and (claim.user != username or claim.user_id != user_id):
        claim = None
    if claim is None and get_players().gone(user_id, tv.id) is not None:
        return None
    key = claim.key if claim is not None else None
    return replace(tv, owner=username, owner_id=user_id, token_hash=key)


def everyones(conn: sqlite3.Connection, username: str) -> list[repo.Tv]:
    """The older TVs as *username* sees them."""
    user_id = find_user_id(conn, username)
    if user_id is None or not reaching.are_on(conn):
        return []
    claims = older.get_claims()
    shown = []
    for tv in repo.every(conn):
        if tv.kind == "older":
            claim = claims.of(tv.id)
            own = claim is not None and (claim.user, claim.user_id) == (username, user_id)
            key = claim.key if claim is not None and own else None
            shown.append(replace(tv, owner=username, owner_id=user_id, token_hash=key))
    return shown


def key_of(tv_id: int) -> str:
    claim = older.get_claims().of(tv_id)
    return claim.key if claim is not None else ""


def told_of(tv: repo.Tv) -> dict[str, Any]:
    """What is told of an older TV beyond what is told of any."""
    claims = older.get_claims()
    claim = claims.of(tv.id)
    others = claim is not None and claim.key != tv.token_hash
    told: dict[str, Any] = {"busy": claim is not None and others and _active(claim.key)}
    if not get_players().is_playing(tv.token_hash or ""):
        # It is only asked while something plays on it.
        told["on"] = claims.answered(tv.id)
    return told


def ends(conn: sqlite3.Connection, claim: Claim) -> str | None:
    """Why the claim is no longer good, or None."""
    if older.get_claims().of(claim.tv_id) != claim:
        return "taken"
    tv = repo.find(conn, claim.tv_id)
    if tv is None or tv.kind != "older" or not reaching.are_on(conn):
        return "unpaired"
    try:
        reaching.media_base(conn)
    except Refusal:
        return "unpaired"
    watches = _may_still_watch(conn, Ticket("", claim.user, claim.session_hash, 0.0))
    return None if watches and find_user_id(conn, claim.user) == claim.user_id else "signed_out"


def is_held(conn: sqlite3.Connection, key: str, username: str) -> bool:
    """Whether *key* is that of a good claim of *username*'s."""
    claim = older.get_claims().with_key(key)
    return claim is not None and claim.user == username and ends(conn, claim) is None


def session_of(key: str) -> str:
    """What the ticket of a stream for the holder of *key* is bound to."""
    claim = older.get_claims().with_key(key)
    return claim.session_hash if claim is not None else key


def end(tv_id: int, reason: str, carry_out: Callable[[Players], None]) -> None:
    """End what plays on an older TV, and its tickets."""
    claim = older.get_claims().of(tv_id)
    if claim is None:
        return
    older.get_claims().drop(claim)
    get_players().forget(claim.key, reason, tv_id)
    carry_out(get_players())


class Offer:
    """A TV that answered, for one person to take once their film has started.

    Until then whatever is on it goes on.
    """

    def __init__(
        self,
        request: Request,
        tv: repo.Tv,
        found: Renderer,
        claim: Claim,
        steering: Steering,
        *,
        confirm: bool,
    ) -> None:
        self._claim, self._confirm, self._steering = claim, confirm, steering
        self.held = replace(tv, owner=claim.user, owner_id=claim.user_id, token_hash=claim.key)
        self.session_hash = claim.session_hash
        self._found = found
        self._server = _server(request, self.held, claim, steering)
        self._taken = self._was_on = False

    def own_title(self) -> str | None:
        """The title of the person's own film on the TV, which makes room for the next."""
        claim = older.get_claims().of(self._claim.tv_id)
        if claim is None or claim.user_id != self._claim.user_id or not _active(claim.key):
            return None
        with contextlib.suppress(NotPlaying):
            return get_players().current(claim.key).rating_key
        return None

    def take(self) -> Response | None:
        """Take the TV; the refusal, where somebody plays on it whom nobody was asked about."""
        claims, players = older.get_claims(), get_players()
        with _TAKING:
            if not repo.has_session(get_db(), self._claim.user, self.session_hash):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            if not self._confirm and _others_play(self._claim.tv_id, self._claim.user_id):
                return _in_use()
            before = claims.take(self._claim)
            self._taken = True
        if before is not None:
            self._was_on = players.stream(before.key) is not None
            taken = before.user_id != self._claim.user_id
            players.forget(
                before.key,
                "taken" if taken else "stopped",
                self._claim.tv_id if taken else None,
            )
            self._steering.carry_out(players)
        # What begins next asks whether the TV answers, and it did.
        players.poll(self._claim.key, ack=0, active=True, report=None)
        return None

    def drive(self) -> None:
        """Let the TV, once taken, play what began, or stop what it played where nothing did."""
        if not self._taken:
            return
        older.get_claims().started(self._claim)
        _drive(
            Driver(
                self._claim,
                self._found,
                self._server,
                claims=older.get_claims(),
                players=get_players(),
                handed=self._was_on,
            )
        )


def _in_use() -> Response:
    return respond_err("in_use", status=409, playing=True)


def _others_play(tv_id: int, user_id: int) -> bool:
    held = older.get_claims().of(tv_id)
    if held is None or held.user_id == user_id:
        return False
    return _active(held.key) or older.get_claims().is_starting(held)


def ask_for(
    request: Request, tv: repo.Tv, username: str, *, confirm: bool, steering: Steering
) -> Offer | Response:
    """Ask the TV whether *username* can have it; the answer to give where they cannot."""
    conn = get_db()
    user_id = find_user_id(conn, username)
    if user_id is None or not reaching.are_on(conn):
        return respond_err("not_found", status=404)
    if not _CLAIM_LIMITER.check(username):
        refused = respond_err("rate_limited", status=429)
        refused.headers["Retry-After"] = str(_CLAIM_LIMITER.retry_after(username))
        return refused
    if not confirm and _others_play(tv.id, user_id):
        return _in_use()
    claims = older.get_claims()
    admin = get_user_role(conn, username) == "admin"
    seeker = reaching.Seeker(username, get_client_ip(request), admin)
    try:
        reaching.media_base(conn)
        found = reaching.reach(conn, tv.id, seeker=seeker)
    except Refusal as exc:
        if exc.error == "host_not_allowed" and not admin:
            return respond_err(
                exc.error,
                status=409,
                message="mediaman is not set up for this TV. An admin can see why in Settings.",
            )
        return exc.response
    except TvError as exc:
        claims.note(tv.id, answered=False)
        refusal = reaching.told(exc)
        if not admin and isinstance(exc, (TvUnreachable, AnotherTv)):
            # Only an admin's look goes further than the kept address.
            return respond_err(
                refusal.error,
                status=refusal.response.status_code,
                message=f"{tv.name} does not answer. If it is on, its address may have changed: "
                "an admin can Check it in Settings.",
            )
        return refusal.response
    claims.note(tv.id, answered=True)
    reaching.note_seen(conn, tv.id)
    claim = Claim(tv.id, username, user_id, hash_token(request.cookies.get("session_token", "")))
    return Offer(request, tv, found, claim, steering, confirm=confirm)


def _drive(driver: Driver) -> None:
    threading.Thread(target=driver.run, name="older-tv", daemon=True).start()


def _server(request: Request, held: repo.Tv, claim: Claim, steering: Steering) -> older.Server:
    # The request that made the claim is over; what outlives it is the app alone.
    later = Request({"type": "http", "app": request.app, "headers": []})
    players = get_players()

    def reach(known: Renderer) -> Renderer:
        try:
            found = reaching.reach(get_db(), claim.tv_id, known)
        except Refusal as exc:
            raise CannotPlay(str(exc)) from exc
        return found

    def film(url: str, position_s: float, about: dict[str, Any]) -> tuple[Media, float]:
        playing = players.stream(claim.key)
        if playing is None:
            raise CannotPlay("nothing plays")
        _, stream_id, user = playing
        try:
            base = reaching.media_base(get_db())
            number, at = place_of(segments(stream_id, user), position_s)
        except (Refusal, PlexError, StreamGone, ValueError) as exc:
            raise CannotPlay(type(exc).__name__) from exc
        hand_over(stream_id, number)
        token = url.split("/")[3]
        duration = about.get("duration_s")
        seconds = max(0, round(duration - at)) if duration else None
        return film_media(f"{base}/api/cast/{token}/from/{number}.ts", _title(about), seconds), at

    def keep_alive() -> None:
        steering.keep_alive(players, claim.key)
        if players.due_stamp(claim.key):
            reaching.note_seen(get_db(), claim.tv_id)

    def carry_on(what: str, play_id: str, paused: bool) -> None:
        answer = steering.carry_on(
            later, held, what, play_id, pressed=what != "again", paused=paused
        )
        if isinstance(answer, Response):
            logger.info("older tv: could not go on (HTTP %d)", answer.status_code)

    return older.Server(
        ends=lambda: ends(get_db(), claim),
        reach=reach,
        recognise=lambda known: reaching.recognise(get_db(), claim.tv_id, known),
        film=film,
        sent=sent,
        keep_alive=keep_alive,
        carry_on=carry_on,
        carry_out=lambda: steering.carry_out(players),
    )


def _title(about: dict[str, Any]) -> str:
    title = str(about.get("title") or "")
    if about.get("episode") is None:
        return title
    season = about.get("season")
    code = f"S{season} E{about['episode']}" if season is not None else f"E{about['episode']}"
    return " · ".join(part for part in (title, code, about.get("episode_title")) if part)
