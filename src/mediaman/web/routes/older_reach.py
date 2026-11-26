"""Reaching an older TV: whether mediaman may ask it at all, and where the film it plays is."""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from mediaman.core.time import now_utc
from mediaman.services.dlna import renderer
from mediaman.services.dlna.addresses import check_address
from mediaman.services.dlna.errors import AnotherTv, TvError, TvRefused, TvUnreachable
from mediaman.services.dlna.renderer import Renderer
from mediaman.services.dlna.wire import Wire
from mediaman.services.infra.settings_reader import get_bool_setting, get_string_setting
from mediaman.web import _parse_allowed_hosts
from mediaman.web.models.settings import _validate_tv_address
from mediaman.web.repository import older_tvs as repo
from mediaman.web.responses import respond_err

# Looking for a TV can hold a thread for over ten seconds.
_LOOKS = threading.BoundedSemaphore(2)
_looked: set[str] = set()
_looked_lock = threading.Lock()
# A new address of a TV is looked for around its old one, once at a time.
_SEARCH = threading.Lock()


class Refusal(Exception):
    def __init__(self, error: str, status: int, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.response = respond_err(error, status=status, message=message)


def are_on(conn: sqlite3.Connection) -> bool:
    return get_bool_setting(conn, "older_tvs_enabled", default=False)


def media_base(conn: sqlite3.Connection) -> str:
    """Where TVs fetch what they play, as an admin set it."""
    base = get_string_setting(conn, "tv_address")
    if not base:
        raise Refusal("no_tv_address", 409, "Set the address TVs use in Settings first.")
    try:
        base = _validate_tv_address(base)
    except ValueError as exc:
        raise Refusal(
            "bad_tv_address",
            409,
            "The address TVs use is not one a TV can use. Change it in Settings.",
        ) from exc
    if not base.startswith("https://") and not get_bool_setting(
        conn, "older_tvs_http", default=False
    ):
        raise Refusal(
            "http_off",
            409,
            "The address TVs use starts with http. Turn on Plain http for older TVs in Settings.",
        )
    host = urlsplit(base).hostname or ""
    if not _is_let_in(host):
        raise Refusal(
            "host_not_allowed",
            409,
            f"MEDIAMAN_ALLOWED_HOSTS does not hold {host}. Add it and restart mediaman.",
        )
    return base


def _is_let_in(host: str) -> bool:
    """Whether mediaman answers a request for *host*, as its host check does."""
    allowed = _parse_allowed_hosts(os.environ.get("MEDIAMAN_ALLOWED_HOSTS", ""))
    return any(
        host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:]))
        for pattern in allowed
    )


def own_addresses(conn: sqlite3.Connection) -> set[str]:
    found: set[str] = set()
    for key in ("tv_address", "base_url"):
        try:
            host = urlsplit(get_string_setting(conn, key)).hostname
            if host:
                found.add(host)
                found |= {
                    str(info[4][0]) for info in socket.getaddrinfo(host, None, socket.AF_INET)
                }
        # A name nobody knows is no address a TV can have.
        except (ValueError, OSError):
            continue
    return found


@contextmanager
def looking(*whose: str) -> Iterator[None]:
    """Hold one of the few looks, and the one look each of *whose* has."""
    with _looked_lock:
        if not _looked.isdisjoint(whose):
            raise Refusal("looking", 409, "mediaman is already looking for a TV. Wait a moment.")
        if not _LOOKS.acquire(blocking=False):
            raise Refusal("busy", 503, "Other TVs are being checked. Try again in a moment.")
        _looked.update(whose)
    try:
        yield
    finally:
        with _looked_lock:
            _looked.difference_update(whose)
        _LOOKS.release()


def may_ask(conn: sqlite3.Connection, typed: str, wire: Wire) -> str:
    """The address, as one mediaman may ask; raises ``Refusal``."""
    if not are_on(conn):
        raise Refusal("off", 409, "Older TVs are off. Turn them on below, save, then check again.")
    try:
        address = check_address(typed.strip(), [])
    except TvRefused as exc:
        raise Refusal("bad_address", 422, str(exc)) from exc
    if address in own_addresses(conn):
        raise Refusal("own_address", 422, "That is mediaman’s own address, not a TV’s.")
    try:
        return wire.check(address)
    except TvRefused as exc:
        raise Refusal("own_network", 409, str(exc)) from exc


def find(conn: sqlite3.Connection, typed: str, username: str) -> Renderer:
    """Ask the TV at an address who it is."""
    wire = Wire()
    address = may_ask(conn, typed, wire)
    with looking(f"person {username}", f"tv {address}"):
        try:
            return renderer.find(address, wire)
        except TvError as exc:
            raise told(exc) from exc


def told(exc: TvError) -> Refusal:
    if isinstance(exc, AnotherTv):
        return Refusal("another_tv", 409, str(exc))
    if isinstance(exc, TvRefused):
        return Refusal("refused", 502, str(exc))
    if isinstance(exc, TvUnreachable):
        return Refusal("unreachable", 502, str(exc))
    return Refusal("not_a_tv", 502, str(exc))


def same_tv(tv: repo.OlderTv, found: Renderer) -> Renderer:
    """*found*, if it is *tv* as far as either says."""
    if tv.udn and found.udn != tv.udn:
        raise AnotherTv(
            "Another device answers at this address now. If the TV’s address changed, "
            "unpair it here and add it again."
        )
    return found


def recognise(conn: sqlite3.Connection, tv_id: int, known: Renderer) -> Renderer:
    """The TV where it was found before, asked once; raises ``TvError``."""
    tv, wire = _kept(conn, tv_id), Wire()
    try:
        address = may_ask(conn, known.address, wire)
    except Refusal as exc:
        raise TvRefused(str(exc)) from exc
    return same_tv(tv, renderer.describe(address, known.location, wire))


@dataclass(frozen=True)
class Seeker:
    """Who asks for a TV, and from where."""

    username: str
    ip: str
    admin: bool


def reach(
    conn: sqlite3.Connection,
    tv_id: int,
    known: Renderer | None = None,
    *,
    seeker: Seeker | None = None,
) -> Renderer:
    """The kept TV; raises ``TvError`` or ``Refusal``.

    For an admin one that moved is looked for, told by its own id, and its new address kept.
    """
    if known is not None:
        try:
            return recognise(conn, tv_id, known)
        except TvError:
            pass
    tv, wire = _kept(conn, tv_id), Wire()
    address = may_ask(conn, tv.address, wire)
    whose = [f"tv {address}"] + ([f"person {seeker.username}"] if seeker is not None else [])
    with looking(*whose):
        try:
            return same_tv(tv, renderer.find(address, wire))
        except TvError as exc:
            if seeker is None or not seeker.admin or not tv.udn:
                raise
            if not _SEARCH.acquire(blocking=False):
                raise
            try:
                found = renderer.find_again(tv.udn, address, wire, skip=own_addresses(conn))
            except TvError:
                # Why it is not where it was says more.
                raise exc from None
            finally:
                _SEARCH.release()
    if not repo.move(conn, tv, found.address, actor=seeker.username, ip=seeker.ip):
        raise TvRefused("Another TV in the list has that address.")
    return found


def _kept(conn: sqlite3.Connection, tv_id: int) -> repo.OlderTv:
    tv = repo.find(conn, tv_id)
    if tv is None:
        raise TvRefused("That TV is no longer in the list.")
    return tv


def note_seen(conn: sqlite3.Connection, tv_id: int) -> None:
    repo.mark_seen(conn, tv_id, now_utc())
