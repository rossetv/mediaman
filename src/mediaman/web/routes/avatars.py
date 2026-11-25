"""People's avatars: choose a drawn one, upload a photo, remove it, and see it."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from mediaman.db import get_db
from mediaman.services.avatars import drawn, files, photo
from mediaman.services.rate_limit import ActionRateLimiter
from mediaman.web.auth.middleware import get_current_user
from mediaman.web.auth.password_hash import get_user_role
from mediaman.web.repository import avatars
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()

_CHANGE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60)
_UPLOAD_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=3600)

_MESSAGES = {
    "not_a_photo": "That file is not a photo Mediaman can use. Choose a JPEG, PNG or WebP.",
    "too_big": photo.TOO_BIG_MESSAGE,
    "too_small": f"That photo is too small. It needs at least {photo.MIN_SIDE} pixels each way.",
    "too_large": f"That photo is too large. The most is {photo.MAX_SIDE} pixels each way.",
    "too_many_requests": "Too many tries. Wait a while and try again.",
    "not_saved": "Could not save your picture. Try again.",
}
_STATUS = {"too_big": 413, "too_many_requests": 429, "not_saved": 500}


class _ChooseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: int = Field(strict=True, ge=1, le=drawn.COUNT)


def _refuse(reason: str) -> JSONResponse:
    return respond_err(reason, status=_STATUS.get(reason, 400), message=_MESSAGES[reason])


def _user_id(conn: sqlite3.Connection, username: str) -> int:
    user_id = avatars.find_user_id(conn, username)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


def _view(conn: sqlite3.Connection, user_id: int) -> JSONResponse:
    avatar = avatars.get_avatar(conn, user_id)
    kind = "photo" if avatar.file_name else "drawn" if avatar.choice else "default"
    return respond_ok(
        {
            "kind": kind,
            "choice": avatar.choice,
            "default": drawn.default_choice(user_id),
            "url": avatar.url,
            "drawn": [drawn.url(choice) for choice in range(1, drawn.COUNT + 1)],
        }
    )


def _tidy(conn: sqlite3.Connection, unused: str | None) -> None:
    where = files.folder(conn)
    files.remove(where, unused)
    files.sweep(where, avatars.file_names(conn))


@router.get("/api/account/avatar")
def api_my_avatar(username: str = Depends(get_current_user)) -> JSONResponse:
    conn = get_db()
    return _view(conn, _user_id(conn, username))


@router.put("/api/account/avatar")
def api_choose_avatar(body: _ChooseBody, username: str = Depends(get_current_user)) -> JSONResponse:
    conn = get_db()
    user_id = _user_id(conn, username)
    if not _CHANGE_LIMITER.check(username):
        return _refuse("too_many_requests")
    _tidy(conn, avatars.set_drawn(conn, user_id, body.choice))
    return _view(conn, user_id)


@router.delete("/api/account/avatar")
def api_remove_avatar(username: str = Depends(get_current_user)) -> JSONResponse:
    conn = get_db()
    user_id = _user_id(conn, username)
    if not _CHANGE_LIMITER.check(username):
        return _refuse("too_many_requests")
    _tidy(conn, avatars.clear(conn, user_id))
    return _view(conn, user_id)


@router.put("/api/account/avatar/photo")
async def api_upload_avatar(
    request: Request, username: str = Depends(get_current_user)
) -> JSONResponse:
    """Take a photo as the request's whole body; its declared type is not looked at."""
    if not _UPLOAD_LIMITER.check(username):
        return _refuse("too_many_requests")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > photo.MAX_BYTES:
            return _refuse("too_big")
        data += chunk
    return await run_in_threadpool(_save_photo, username, bytes(data))


def _save_photo(username: str, data: bytes) -> JSONResponse:
    try:
        cleaned = photo.clean(data)
    except photo.PhotoError as exc:
        return _refuse(exc.reason)
    conn = get_db()
    user_id = _user_id(conn, username)
    where = files.folder(conn)
    try:
        name = files.write(where, cleaned)
    except OSError:
        logger.exception("could not keep an avatar")
        return _refuse("not_saved")
    try:
        unused = avatars.set_upload(conn, user_id, name)
    except sqlite3.Error:
        files.remove(where, name)
        logger.exception("could not save an avatar")
        return _refuse("not_saved")
    _tidy(conn, unused)
    return _view(conn, user_id)


@router.get("/api/avatars/{user_id}")
def api_avatar(
    user_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    v: str = "",
    username: str = Depends(get_current_user),
) -> Response:
    """Serve a photo to its owner and to admins, who see it in the lists of people."""
    conn = get_db()
    if user_id != _user_id(conn, username) and get_user_role(conn, username) != "admin":
        raise HTTPException(status_code=404, detail="Not found")
    avatar = avatars.get_avatar(conn, user_id)
    data = files.read(files.folder(conn), avatar.file_name) if avatar.file_name else None
    kind = photo.content_type(data) if data else None
    if data is None or kind is None:
        choice = avatar.choice or drawn.default_choice(user_id)
        return RedirectResponse(
            drawn.url(choice), status_code=302, headers={"Cache-Control": "no-store, private"}
        )
    current = v == avatar.version
    return Response(
        data,
        media_type=kind,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable"
            if current
            else "private, no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
