"""Older TVs (DLNA), added by address in Settings. Admins only.

Nothing is looked for by itself: mediaman asks only the address an admin typed, and those around
it for a TV that moved, within what :mod:`mediaman.services.dlna` allows.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from mediaman.core.time import now_utc
from mediaman.db import get_db
from mediaman.services.dlna.errors import TvError, TvFault
from mediaman.services.dlna.renderer import Renderer
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth.middleware import get_current_admin
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import older_tvs as repo
from mediaman.web.responses import respond_err, respond_ok
from mediaman.web.routes import older_play
from mediaman.web.routes import older_reach as reaching
from mediaman.web.routes.older_reach import Refusal
from mediaman.web.routes.older_stream import offer_picture, picture_media, withdraw_picture
from mediaman.web.routes.tv import _carry_out, _Name, _session
from mediaman.web.routes.users.rate_limits import too_many

router = APIRouter()

_CHECK_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60)
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=60, window_seconds=60)
# How long a TV that was shown the test picture may take to fetch it, and how long it stays.
_FETCH_SECONDS = 10.0
_SHOWN_SECONDS = 3.0
_SHOWS = threading.BoundedSemaphore(1)

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


def _not_found() -> Response:
    return respond_err("not_found", status=404, message="That TV is no longer in the list.")


def _already(tv: repo.OlderTv | None) -> Refusal:
    name = f"{tv.name} is" if tv is not None else "That TV is"
    return Refusal("already_added", 409, f"{name} already in the list.")


def _limited(admin: str) -> None:
    if not _CHECK_LIMITER.check(admin):
        raise Refusal("too_many_requests", 429, "Too many checks. Try again in a minute.")


def _look(admin: str, address: str) -> Renderer:
    """Ask the TV at *address* who it is."""
    _limited(admin)
    return reaching.find(get_db(), address, admin)


def _still_admin(request: Request, admin: str) -> None:
    """Refuse where the session has ended while the body, or the TV's answer, was on its way."""
    _session(request, admin)
    if get_user_role(get_db(), admin) != "admin":
        raise HTTPException(status_code=401, detail="Not authenticated")


def _shown(tv: repo.OlderTv) -> dict[str, object]:
    shown = asdict(tv)
    del shown["udn"]
    return shown


@router.get("/api/settings/older-tvs")
def list_older_tvs(admin: _Admin) -> Response:
    return respond_ok({"tvs": [_shown(tv) for tv in repo.every(get_db())]})


@router.post("/api/settings/older-tvs/check")
def check_address_of_tv(request: Request, body: _AddressBody, admin: _Admin) -> Response:
    """Look for a TV at an address; nothing is kept."""
    _still_admin(request, admin)
    try:
        found = _look(admin, body.address)
        same = repo.same(get_db(), found.address, found.udn)
        if same is not None:
            raise _already(same)
    except Refusal as exc:
        return exc.response
    return respond_ok({"name": found.name, "address": found.address})


@router.post("/api/settings/older-tvs/picture")
def show_test_picture(request: Request, body: _AddressBody, admin: _Admin) -> Response:
    """Show a picture on the TV at an address for a moment, to see that it can reach mediaman."""
    _still_admin(request, admin)
    conn = get_db()
    try:
        base = reaching.media_base(conn)
        same = repo.same(conn, body.address.strip(), "")
        if same is not None:
            raise _already(same)
        found = _look(admin, body.address)
        _still_admin(request, admin)
        if not _SHOWS.acquire(blocking=False):
            raise Refusal("busy", 503, "A test picture is on show. Try again in a moment.")
        try:
            fetched = _show_picture(found, base)
        finally:
            _SHOWS.release()
    except Refusal as exc:
        return exc.response
    if not fetched:
        return respond_err("no_fetch", status=502, message="The TV couldn’t reach mediaman.")
    return respond_ok({"from": base})


def _show_picture(tv: Renderer, base: str) -> bool:
    """Whether the TV fetched the picture; raises ``Refusal`` where it would not show one."""
    token, picture = offer_picture()
    try:
        try:
            tv.start(picture_media(f"{base}/api/cast/{token}/test.jpg"))
        except TvFault as exc:
            if not picture.fetched.is_set():
                raise Refusal("no_picture", 502, str(exc)) from exc
        except TvError:
            # It may have fetched it all the same.
            pass
        if not picture.fetched.wait(_FETCH_SECONDS):
            return False
        time.sleep(_SHOWN_SECONDS)
        return True
    finally:
        withdraw_picture(token)
        with contextlib.suppress(TvError):
            tv.stop()


@router.post("/api/settings/older-tvs")
def add_older_tv(request: Request, body: _AddBody, admin: _Admin) -> Response:
    """Add the TV at an address, which is asked again: only a TV that answers is added."""
    _still_admin(request, admin)
    conn = get_db()
    try:
        found = _look(admin, body.address)
        _still_admin(request, admin)
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
            raise Refusal(
                "too_many_tvs", 409, f"mediaman keeps at most {repo.MAX_OLDER_TVS} older TVs."
            ) from None
    except Refusal as exc:
        return exc.response
    tv = repo.find(conn, tv_id)
    return respond_ok({"tv": _shown(tv)}) if tv is not None else _not_found()


@router.post("/api/settings/older-tvs/{tv_id}/check")
def check_older_tv(request: Request, tv_id: _TvId, admin: _Admin) -> Response:
    """Ask whether the TV still answers at its address, and is still the same one."""
    conn = get_db()
    tv = repo.find(conn, tv_id)
    if tv is None:
        return _not_found()
    try:
        _limited(admin)
        seeker = reaching.Seeker(admin, get_client_ip(request), admin=True)
        reaching.reach(conn, tv_id, seeker=seeker)
    except Refusal as exc:
        return exc.response
    except TvError as exc:
        return reaching.told(exc).response
    reaching.note_seen(conn, tv_id)
    tv = repo.find(conn, tv_id)
    return respond_ok({"tv": _shown(tv)}) if tv is not None else _not_found()


@router.patch("/api/settings/older-tvs/{tv_id}")
def rename_older_tv(request: Request, tv_id: _TvId, body: _NameBody, admin: _Admin) -> Response:
    _still_admin(request, admin)
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
    older_play.end(tv_id, "unpaired", _carry_out)
    return respond_ok()
