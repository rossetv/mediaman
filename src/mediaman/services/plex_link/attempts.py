"""Sign-in attempts in progress, held in memory only and bound to one browser session.

An attempt is dropped when it fails, is cancelled, is used or runs out, and by a restart. The
browser is given an attempt's own id, never the PIN's id, and cannot name a PIN.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from mediaman.services.plex_link.plex_tv import (
    Pin,
    PinExpired,
    PlexIdentity,
    PlexTv,
    PlexTvUnavailable,
)

POLL_SECONDS = 2.0
_MAX_ATTEMPTS = 200

State = Literal["waiting", "confirm", "failed"]


class TooManyAttempts(Exception):
    """No room for another attempt."""


@dataclass
class Attempt:
    username: str
    server_id: str
    plex_tv: PlexTv = field(repr=False)
    pin: Pin = field(repr=False)
    expires_at: float
    id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    state: State = "waiting"
    reason: str | None = None
    identity: PlexIdentity | None = None
    signed_out: bool = True
    next_poll: float = 0.0
    polling: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def url(self) -> str:
        return self.plex_tv.approval_url(self.pin)

    def expires_in(self) -> int:
        return max(0, int(self.expires_at - time.monotonic()))

    def fail(self, reason: str) -> None:
        self.state, self.reason, self.identity = "failed", reason, None


class AttemptStore:
    """One attempt per session, keyed by the hash of its token."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, Attempt] = {}

    def add(self, session: str, attempt: Attempt) -> None:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, a in self._attempts.items() if a.expires_at <= now]:
                del self._attempts[key]
            if session not in self._attempts and len(self._attempts) >= _MAX_ATTEMPTS:
                raise TooManyAttempts
            self._attempts[session] = attempt

    def get(self, session: str, username: str) -> Attempt | None:
        with self._lock:
            attempt = self._attempts.get(session)
        return attempt if attempt is not None and attempt.username == username else None

    def drop(self, session: str, attempt: Attempt) -> bool:
        """Forget *attempt*; False when it was gone or replaced, so only one caller gets True."""
        with self._lock:
            if self._attempts.get(session) is not attempt:
                return False
            del self._attempts[session]
            return True

    def clear(self) -> None:
        with self._lock:
            self._attempts.clear()


def advance(attempt: Attempt, *, may_poll: Callable[[], bool]) -> None:
    """Ask plex.tv once, when it is time, whether the person has approved.

    plex.tv answering badly is not a refusal: the attempt waits on until it runs out.
    """
    if not attempt.polling.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if attempt.expires_at <= now:
            attempt.fail("expired")
            return
        if attempt.state != "waiting" or now < attempt.next_poll or not may_poll():
            return
        attempt.next_poll = now + POLL_SECONDS
        try:
            token = attempt.plex_tv.poll_pin(attempt.pin)
        except PlexTvUnavailable:
            return
        except PinExpired:
            attempt.fail("expired")
            return
        if token is None:
            return
        result = attempt.plex_tv.use_sign_in(token, attempt.server_id)
        attempt.signed_out = result.signed_out
        if result.identity is None:
            attempt.fail(result.reason or "plex_unavailable")
        else:
            attempt.state, attempt.identity = "confirm", result.identity
    finally:
        attempt.polling.release()


def discard(attempt: Attempt) -> None:
    """Give an attempt up. If the person has just approved it, sign that token out unused."""
    # A poll already running signs out whatever it gets.
    if attempt.state != "waiting" or not attempt.polling.acquire(blocking=False):
        return
    try:
        attempt.fail("cancelled")
        token = attempt.plex_tv.poll_pin(attempt.pin)
        if token is not None:
            attempt.plex_tv.sign_out(token)
    except (PinExpired, PlexTvUnavailable):
        return
    finally:
        attempt.polling.release()
