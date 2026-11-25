"""Playing on the older TVs, which are everyone's, for one person at a time.

A person's claim on such a TV stands in for a pairing, so that the remote's routes are the same.
It lasts until somebody takes the TV over, it is forgotten, older TVs are turned off, or the
session that made it ends.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from fastapi import Request
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.cast.tickets import Ticket
from mediaman.services.dlna.errors import TvError
from mediaman.services.dlna.media import Media
from mediaman.services.dlna.renderer import Renderer
from mediaman.services.media_meta.plex_stream import StreamGone
from mediaman.services.media_meta.plex_watch import PlexError
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.services.tv import older
from mediaman.services.tv.older import CannotPlay, Claim, Driver
from mediaman.services.tv.players import NotPlaying, Players, get_players
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.repository import tv as repo
from mediaman.web.repository.ratings import find_user_id
from mediaman.web.responses import respond_err
from mediaman.web.routes import older_reach as reaching
from mediaman.web.routes.cast import _may_still_watch
from mediaman.web.routes.older_reach import Refusal
from mediaman.web.routes.older_stream import film_media, place_of, segments, sent

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


def claim_tv(
    request: Request, tv: repo.Tv, username: str, *, confirm: bool, steering: Steering
) -> repo.Tv | Response:
    """Let *username* have the TV, which must answer; the answer to give where they cannot."""
    conn = get_db()
    user_id = find_user_id(conn, username)
    if user_id is None or not reaching.are_on(conn):
        return respond_err("not_found", status=404)
    if not _CLAIM_LIMITER.check(username):
        refused = respond_err("rate_limited", status=429)
        refused.headers["Retry-After"] = str(_CLAIM_LIMITER.retry_after(username))
        return refused
    claims = older.get_claims()
    in_use = respond_err("in_use", status=409, playing=True)

    def others_play() -> bool:
        held = claims.of(tv.id)
        return held is not None and held.user_id != user_id and _active(held.key)

    if others_play() and not confirm:
        return in_use
    try:
        reaching.media_base(conn)
        found = reaching.reach(conn, tv.id)
    except Refusal as exc:
        return exc.response
    except TvError as exc:
        claims.note(tv.id, answered=False)
        return reaching.told(exc).response
    claims.note(tv.id, answered=True)
    reaching.note_seen(conn, tv.id)
    claim = Claim(tv.id, username, user_id, hash_token(request.cookies.get("session_token", "")))
    players = get_players()
    with _TAKING:
        # Somebody may have begun while the TV was asked.
        if others_play() and not confirm:
            return in_use
        before = claims.take(claim)
    if before is not None:
        taken = before.user_id != user_id
        players.forget(before.key, "taken" if taken else "stopped", tv.id if taken else None)
        steering.carry_out(players)
    held = replace(tv, owner=username, owner_id=user_id, token_hash=claim.key)
    # What begins next asks whether the TV answers, and it just did.
    players.poll(claim.key, ack=0, active=True, report=None)
    _drive(
        Driver(
            claim, found, _server(request, held, claim, steering), claims=claims, players=players
        )
    )
    return held


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
        except (Refusal, PlexError, StreamGone) as exc:
            raise CannotPlay(type(exc).__name__) from exc
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
