"""Linking your own Plex account from the Profile page."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mediaman.core.audit import security_event
from mediaman.db import get_db
from mediaman.services.infra import get_string_setting
from mediaman.services.infra.settings_reader import plex_is_configured
from mediaman.services.media_meta.plex_watch import PlexError, PlexNotConfigured
from mediaman.services.plex_link import repository, sync
from mediaman.services.plex_link.attempts import (
    Attempt,
    AttemptStore,
    TooManyAttempts,
    advance,
    create_pin,
    discard,
)
from mediaman.services.plex_link.plex_tv import PlexTv, PlexTvUnavailable
from mediaman.services.rate_limit import ActionRateLimiter, get_client_ip
from mediaman.web.auth._token_hashing import hash_token
from mediaman.web.auth.middleware import get_current_admin, get_current_user
from mediaman.web.auth.reauth import has_recent_reauth
from mediaman.web.auth.user_crud import find_username_by_user_id
from mediaman.web.responses import respond_err, respond_ok

logger = logging.getLogger(__name__)

router = APIRouter()


class _RefundableLimiter(ActionRateLimiter):
    def give_back(self, actor: str) -> None:
        """Undo the last ``check`` that let *actor* through."""
        with self._lock:
            for log in (self._attempts.get(actor), self._daily.get(actor)):
                if log:
                    log.pop()


_START_LIMITER = _RefundableLimiter(max_in_window=5, window_seconds=3600, max_per_day=20)
# Taken before a start and given back unless it fails, since a failed start costs no start.
_FAILED_START_LIMITER = _RefundableLimiter(max_in_window=20, window_seconds=3600)
_FAILED_START_AUDIT_LIMITER = ActionRateLimiter(max_in_window=1, window_seconds=3600)
_CHECK_LIMITER = ActionRateLimiter(max_in_window=40, window_seconds=60)
_CHANGE_LIMITER = ActionRateLimiter(max_in_window=20, window_seconds=60)
# Shared by everyone: what this install may ask of plex.tv. Waiting sign-ins ask often, so
# starting one has a share of its own that they cannot use up.
_SHARED_START_LIMITER = ActionRateLimiter(max_in_window=30, window_seconds=60)
_SHARED_POLL_LIMITER = ActionRateLimiter(max_in_window=150, window_seconds=60)
_EVERYONE = "everyone"

_attempts = AttemptStore()

_HOST_RE = re.compile(r"[a-z0-9.-]{1,100}")

_MESSAGES = {
    "plex_not_set_up": "Plex is not set up on this Mediaman yet.",
    "plex_unavailable": "Plex is not answering. Try again in a few minutes.",
    "already_linked": "You already have a linked Plex account. Unlink it first.",
    "claimed": (
        "This Plex account was linked to another person here in the meantime. Start again."
    ),
    "not_on_server": "This Plex account has no access to this Plex server.",
    "unsupported_account": (
        "This Plex account cannot be linked: Mediaman cannot be sure which plays here are its own."
    ),
    "signin_rejected": "Plex did not accept the sign-in. Start again.",
    "expired": (
        "The sign-in ran out. Start again. If you approved it only now, remove Mediaman under"
        " Authorized Devices in your Plex account."
    ),
    "too_many_requests": "Too many tries. Wait a while and try again.",
}
_STATUS = {
    "plex_not_set_up": 409,
    "plex_unavailable": 503,
    "already_linked": 409,
    "claimed": 409,
    "expired": 409,
    "too_many_requests": 429,
}


class _CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: str = Field(min_length=1, max_length=64)


class _PlexLinkBody(BaseModel):
    """Empty to start signing in; the id of an attempt to say yes, that account is mine.

    ``move`` says the person was told the account is linked to someone else and moves to them.
    """

    model_config = ConfigDict(extra="forbid")
    attempt: str | None = Field(default=None, min_length=1, max_length=64)
    move: bool = False


def carry_attempt(old_token: str | None, new_token: str) -> None:
    """Keep a sign-in under way when its session's token is replaced."""
    _attempts.move(hash_token(old_token or ""), hash_token(new_token))


def _refuse(reason: str) -> JSONResponse:
    return respond_err(reason, status=_STATUS[reason], message=_MESSAGES[reason])


def _audit_failure(conn: sqlite3.Connection, request: Request, username: str, reason: str) -> None:
    security_event(
        conn,
        event="plex.link_failed",
        actor=username,
        ip=get_client_ip(request),
        detail={"reason": reason},
    )


def _product(conn: sqlite3.Connection) -> str:
    """The name Plex shows on its approval page, with our host so a stranger's link stands out."""
    try:
        host = urlsplit(get_string_setting(conn, "base_url")).hostname or ""
    except ValueError:
        host = ""
    return f"mediaman ({host})" if _HOST_RE.fullmatch(host) else "mediaman"


def _attempt_view(
    conn: sqlite3.Connection, attempt: Attempt, *, signed_out: bool = True
) -> dict[str, object]:
    """*signed_out* is whether the sign-in before this one, if any, was signed out."""
    view: dict[str, object] = {
        "id": attempt.id,
        "state": attempt.state,
        "expires_in": attempt.expires_in(),
        "signed_out": attempt.signed_out and signed_out,
    }
    if attempt.state == "waiting":
        view["url"] = attempt.url
        view["app"] = attempt.plex_tv.product
    if attempt.identity is not None:
        view["name"] = attempt.identity.name
        view["moves"] = repository.is_claimed_by_other(conn, attempt.identity, attempt.username)
    if attempt.reason is not None:
        view["reason"] = attempt.reason
        view["message"] = _MESSAGES.get(attempt.reason, _MESSAGES["plex_unavailable"])
    return view


@router.get("/api/account/plex")
def api_plex_link(
    username: str = Depends(get_current_user), session_token: str | None = Cookie(default=None)
) -> JSONResponse:
    conn = get_db()
    link = repository.get_link(conn, username)
    attempt = _attempts.get(hash_token(session_token or ""), username)
    if attempt is not None and attempt.expires_in() == 0:
        attempt = None
    return respond_ok(
        {
            "configured": plex_is_configured(conn),
            "link": None
            if link is None
            else {
                "name": link.plex_name,
                "linked_at": link.linked_at,
                "last_read_at": link.last_read_at,
                "reading": sync.is_reading(username),
            },
            "moved_at": None if link else repository.moved_at(conn, username),
            "attempt": None if link or attempt is None else _attempt_view(conn, attempt),
        }
    )


@router.post("/api/account/plex")
def api_start_or_confirm(
    request: Request,
    body: _PlexLinkBody | None = None,
    username: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """Start a sign-in at Plex, or save the link once the person has said whose account it is.

    Starting needs a recent password, so a stolen session cookie cannot point this person's
    recommendations at another account. Confirming needs the attempt this session started.
    """
    conn = get_db()
    session = hash_token(session_token or "")
    if body is not None and body.attempt is not None:
        return _confirm(conn, request, username, session, body.attempt, move=body.move)

    if not has_recent_reauth(conn, session_token, username):
        return respond_err(
            "reauth_required",
            status=403,
            message="Recent password re-authentication required",
            reauth_required=True,
        )
    if repository.get_link(conn, username) is not None:
        return _refuse("already_linked")
    if not _FAILED_START_LIMITER.check(username):
        return _refuse("too_many_requests")
    if not _START_LIMITER.check(username):
        _FAILED_START_LIMITER.give_back(username)
        return _refuse("too_many_requests")
    if not _SHARED_START_LIMITER.check(_EVERYONE):
        _FAILED_START_LIMITER.give_back(username)
        _START_LIMITER.give_back(username)
        return _refuse("too_many_requests")
    return _start(conn, request, username, session)


def _start(conn: sqlite3.Connection, request: Request, username: str, session: str) -> JSONResponse:
    previous = _attempts.get(session, username)
    signed_out = True
    if previous is not None and _attempts.drop(session, previous):
        signed_out = discard(previous)
    try:
        server_id = sync.read_server_id(
            sync.open_history(conn, request.app.state.config.secret_key)
        )
        plex_tv = PlexTv(repository.client_id(conn), _product(conn))
        pin = create_pin(plex_tv)
        attempt = Attempt(
            username=username,
            server_id=server_id,
            plex_tv=plex_tv,
            pin=pin,
            expires_at=time.monotonic() + pin.expires_in_s,
        )
        _attempts.add(session, attempt)
    except PlexNotConfigured:
        _START_LIMITER.give_back(username)
        return _refuse("plex_not_set_up")
    except TooManyAttempts:
        _FAILED_START_LIMITER.give_back(username)
        return _refuse("too_many_requests")
    except (PlexError, PlexTvUnavailable):
        _START_LIMITER.give_back(username)
        if _FAILED_START_AUDIT_LIMITER.check(username):
            _audit_failure(conn, request, username, "plex_unavailable")
        return _refuse("plex_unavailable")
    _FAILED_START_LIMITER.give_back(username)
    security_event(conn, event="plex.link_started", actor=username, ip=get_client_ip(request))
    return respond_ok({"attempt": _attempt_view(conn, attempt, signed_out=signed_out)})


def _confirm(
    conn: sqlite3.Connection,
    request: Request,
    username: str,
    session: str,
    attempt_id: str,
    *,
    move: bool,
) -> JSONResponse:
    attempt = _attempts.get(session, username)
    if (
        attempt is None
        or attempt.id != attempt_id
        or attempt.identity is None
        or attempt.expires_in() == 0
    ):
        return _refuse("expired")
    if not move and repository.is_claimed_by_other(conn, attempt.identity, username):
        # They have not been told what linking would do; the page asks again, saying so.
        return respond_ok({"attempt": _attempt_view(conn, attempt)})
    if not _attempts.drop(session, attempt):
        return _refuse("expired")
    try:
        repository.save_link(
            conn,
            username,
            attempt.identity,
            attempt.server_id,
            ip=get_client_ip(request),
            move=move,
        )
    except repository.AlreadyLinked:
        return _refuse("already_linked")
    except repository.AccountClaimed:
        _audit_failure(conn, request, username, "claimed")
        return _refuse("claimed")
    sync.read_in_background(
        request.app.state.db_path, request.app.state.config.secret_key, username
    )
    return respond_ok({"name": attempt.identity.name})


@router.post("/api/account/plex/check")
def api_check(
    request: Request,
    username: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    """How this session's sign-in is going, asking plex.tv at most once every two seconds."""
    if not _CHECK_LIMITER.check(username):
        return _refuse("too_many_requests")
    session = hash_token(session_token or "")
    attempt = _attempts.get(session, username)
    if attempt is None:
        return respond_ok({"attempt": None, "message": _MESSAGES["expired"]})
    advance(attempt, may_poll=lambda: _SHARED_POLL_LIMITER.check(_EVERYONE))
    conn = get_db()
    view = _attempt_view(conn, attempt)
    if attempt.state == "failed":
        if not _attempts.drop(session, attempt):
            return respond_ok({"attempt": None, "message": _MESSAGES["expired"]})
        _audit_failure(conn, request, username, attempt.reason or "")
    elif _attempts.get(session, username) is not attempt:
        return respond_ok({"attempt": None, "message": _MESSAGES["expired"]})
    return respond_ok({"attempt": view})


@router.post("/api/account/plex/cancel")
def api_cancel(
    body: _CancelBody,
    username: str = Depends(get_current_user),
    session_token: str | None = Cookie(default=None),
) -> JSONResponse:
    session = hash_token(session_token or "")
    attempt = _attempts.get(session, username)
    signed_out = True
    if attempt is not None and attempt.id == body.attempt and _attempts.drop(session, attempt):
        signed_out = discard(attempt)
    return respond_ok({"signed_out": signed_out})


@router.delete("/api/account/plex")
def api_unlink(request: Request, username: str = Depends(get_current_user)) -> JSONResponse:
    if not _CHANGE_LIMITER.check(username):
        return _refuse("too_many_requests")
    repository.unlink(get_db(), username, actor=username, ip=get_client_ip(request), reason="self")
    return respond_ok()


@router.delete("/api/account/plex/notice")
def api_dismiss_notice(
    moved_at: str = Query(min_length=1, max_length=64),
    username: str = Depends(get_current_user),
) -> JSONResponse:
    """Dismiss the word that the link went to someone else, as it was shown."""
    if not _CHANGE_LIMITER.check(username):
        return _refuse("too_many_requests")
    conn = get_db()
    repository.dismiss_move(conn, username, moved_at)
    return respond_ok({"moved_at": repository.moved_at(conn, username)})


@router.get("/api/users/plex-links")
def api_list_links(admin: str = Depends(get_current_admin)) -> JSONResponse:
    """Who else has linked a Plex account, since when; never which account or what they played."""
    links = [link for link in repository.linked_users(get_db()) if link["username"] != admin]
    return respond_ok({"links": links})


@router.delete("/api/users/{user_id}/plex-link")
def api_admin_unlink(
    user_id: int, request: Request, admin: str = Depends(get_current_admin)
) -> JSONResponse:
    if not _CHANGE_LIMITER.check(admin):
        return _refuse("too_many_requests")
    conn = get_db()
    target = find_username_by_user_id(conn, user_id)
    if target is None or not repository.unlink(
        conn, target, actor=admin, ip=get_client_ip(request), reason="admin"
    ):
        return respond_err("not_found", status=404, message="No linked Plex account to remove")
    return respond_ok()
