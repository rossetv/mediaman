"""Older TVs (DLNA), added by address in Settings. Admins only.

Nothing is looked for by itself: mediaman asks only the address an admin typed, within what
:mod:`mediaman.services.dlna` allows.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.dlna import renderer
from mediaman.services.dlna.addresses import check_address
from mediaman.services.dlna.errors import TvError, TvRefused, TvUnreachable
from mediaman.services.dlna.wire import Wire
from mediaman.services.infra.settings_reader import get_bool_setting, get_setting
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.repository import older_tvs as repo
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes.tv import _Name
from mediaman.web.routes.users.rate_limits import too_many

router = APIRouter()

_CHECK_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60)
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)
# Looking for a TV can hold a thread for over ten seconds.
_LOOKS = threading.BoundedSemaphore(2)

_Admin = Annotated[str, Depends(get_current_admin)]
_TvId = Annotated[int, Path(ge=1, le=2**53)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _AddressBody(_Body):
    address: Annotated[str, Field(max_length=253)]


class _NameBody(_Body):
    name: _Name


class _AddBody(_AddressBody, _NameBody):
    pass


class _Refusal(Exception):
    def __init__(self, error: str, status: int, message: str) -> None:
        super().__init__(message)
        self.response = respond_err(error, status=status, message=message)


def _not_found() -> Response:
    return respond_err("not_found", status=404, message="That TV is no longer in the list.")


def _already(tv: repo.OlderTv | None) -> _Refusal:
    name = f"{tv.name} is" if tv is not None else "That TV is"
    return _Refusal("already_added", 409, f"{name} already in the list.")


def _is_own(conn: sqlite3.Connection, address: str) -> bool:
    for key in ("tv_address", "base_url"):
        try:
            if urlsplit(str(get_setting(conn, key) or "")).hostname == address:
                return True
        except ValueError:
            continue
    return False


@contextmanager
def _looking() -> Iterator[None]:
    if not _LOOKS.acquire(blocking=False):
        raise _Refusal("busy", 503, "Other TVs are being checked. Try again in a moment.")
    try:
        yield
    finally:
        _LOOKS.release()


def _look(admin: str, address: str) -> renderer.Renderer:
    """Ask the TV at *address* who it is."""
    if not _CHECK_LIMITER.check(admin):
        raise _Refusal("too_many_requests", 429, "Too many checks. Try again in a minute.")
    conn = get_db()
    if not get_bool_setting(conn, "older_tvs_enabled", default=False):
        raise _Refusal("off", 409, "Older TVs are off. Turn them on below, save, then check again.")
    wire = Wire()
    try:
        address = check_address(address.strip(), [])
        if _is_own(conn, address):
            raise _Refusal("own_address", 422, "That is mediaman’s own address, not a TV’s.")
    except TvRefused as exc:
        raise _Refusal("bad_address", 422, str(exc)) from exc
    try:
        wire.check(address)
    except TvRefused as exc:
        raise _Refusal("own_network", 409, str(exc)) from exc
    with _looking():
        try:
            return renderer.find(address, wire)
        except TvRefused as exc:
            raise _Refusal("refused", 502, str(exc)) from exc
        except TvUnreachable as exc:
            raise _Refusal("unreachable", 502, str(exc)) from exc
        except TvError as exc:
            raise _Refusal("not_a_tv", 502, str(exc)) from exc


def _shown(tv: repo.OlderTv) -> dict[str, object]:
    shown = asdict(tv)
    del shown["udn"]
    return shown


@router.get("/api/settings/older-tvs")
def list_older_tvs(admin: _Admin) -> Response:
    return respond_ok({"tvs": [_shown(tv) for tv in repo.every(get_db())]})


@router.post("/api/settings/older-tvs/check")
def check_address_of_tv(body: _AddressBody, admin: _Admin) -> Response:
    """Look for a TV at an address; nothing is kept."""
    try:
        found = _look(admin, body.address)
        same = repo.same(get_db(), found.address, found.udn)
        if same is not None:
            raise _already(same)
    except _Refusal as exc:
        return exc.response
    return respond_ok({"name": found.name, "address": found.address})


@router.post("/api/settings/older-tvs")
def add_older_tv(request: Request, body: _AddBody, admin: _Admin) -> Response:
    """Add the TV at an address, which is asked again: only a TV that answers is added."""
    conn = get_db()
    try:
        found = _look(admin, body.address)
        try:
            tv_id = repo.add(
                conn,
                name=body.name,
                address=found.address,
                udn=found.udn,
                actor=admin,
                ip=get_client_ip(request),
                now=now_utc(),
            )
        except repo.AlreadyAdded:
            raise _already(repo.same(conn, found.address, found.udn)) from None
        except repo.TooManyOlderTvs:
            raise _Refusal(
                "too_many_tvs", 409, f"mediaman keeps at most {repo.MAX_OLDER_TVS} older TVs."
            ) from None
    except _Refusal as exc:
        return exc.response
    tv = repo.find(conn, tv_id)
    return respond_ok({"tv": _shown(tv)}) if tv is not None else _not_found()


@router.post("/api/settings/older-tvs/{tv_id}/check")
def check_older_tv(tv_id: _TvId, admin: _Admin) -> Response:
    """Ask whether the TV still answers at its address, and is still the same one."""
    conn = get_db()
    tv = repo.find(conn, tv_id)
    if tv is None:
        return _not_found()
    try:
        found = _look(admin, tv.address)
    except _Refusal as exc:
        return exc.response
    if tv.udn and found.udn != tv.udn:
        return respond_err(
            "another_tv",
            status=409,
            message="Another device answers at this address now. If the TV’s address changed, "
            "forget it here and add it again.",
        )
    repo.mark_seen(conn, tv_id, now_utc())
    tv = repo.find(conn, tv_id)
    return respond_ok({"tv": _shown(tv)}) if tv is not None else _not_found()


@router.patch("/api/settings/older-tvs/{tv_id}")
def rename_older_tv(tv_id: _TvId, body: _NameBody, admin: _Admin) -> Response:
    if not _CHANGE_LIMITER.check(admin):
        return too_many(_CHANGE_LIMITER, admin)
    if not repo.rename(get_db(), tv_id, body.name):
        return _not_found()
    return respond_ok({"tv": {"id": tv_id, "name": body.name}})


@router.delete("/api/settings/older-tvs/{tv_id}")
def remove_older_tv(request: Request, tv_id: _TvId, admin: _Admin) -> Response:
    if not _CHANGE_LIMITER.check(admin):
        return too_many(_CHANGE_LIMITER, admin)
    if not repo.remove(get_db(), tv_id, actor=admin, ip=get_client_ip(request)):
        return _not_found()
    return respond_ok()
