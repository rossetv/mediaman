"""Sign-in attempts in progress, held in memory only and bound to one browser session.

An attempt is dropped when it fails, is cancelled, is used or runs out, and by a restart. When it
runs out, plex.tv is asked one last time for a token to sign out, however the attempt ended. The
browser is given an attempt's own id, never the PIN's id, and cannot name a PIN.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from mediaman.services.plex_link.bounded import TookTooLong, TooManyLeft, within
from mediaman.services.plex_link.plex_tv import (
    Pin,
    PinExpired,
    PlexIdentity,
    PlexTv,
    PlexTvUnavailable,
    SignIn,
)

POLL_SECONDS = 2.0
_MAX_ATTEMPTS = 200
# For everything one poll leads to: who signed in, their servers and signing out.
_CHAIN_SECONDS = 25.0

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
    # Whether the token was fetched, or can no longer be: nothing is left to sign out later.
    collected: bool = False
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
        _call_later(attempt.expires_at - now, lambda: self._at_expiry(session, attempt))

    def _at_expiry(self, session: str, attempt: Attempt) -> None:
        self.drop(session, attempt)
        discard(attempt)

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


def _call_later(seconds: float, work: Callable[[], object]) -> None:
    timer = threading.Timer(seconds, work)
    timer.daemon = True
    timer.start()


def _poll(attempt: Attempt) -> SignIn | None:
    token = attempt.plex_tv.poll_pin(attempt.pin)
    if token is None:
        return None
    attempt.collected = True
    return attempt.plex_tv.use_sign_in(token, attempt.server_id)


def _give_back(attempt: Attempt) -> None:
    """Sign out the token of a sign-in nobody will use, when there is one."""
    token = attempt.plex_tv.poll_pin(attempt.pin)
    if token is not None:
        attempt.collected = True
        attempt.signed_out = False
        attempt.signed_out = attempt.plex_tv.sign_out(token)


def _return_unused(attempt: Attempt) -> None:
    if attempt.collected:
        return
    try:
        within(_CHAIN_SECONDS, lambda: _give_back(attempt))
    except PinExpired:
        attempt.collected = True
    except (PlexTvUnavailable, TookTooLong, TooManyLeft):
        return


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
            _return_unused(attempt)
            return
        if attempt.state != "waiting" or now < attempt.next_poll or not may_poll():
            return
        attempt.next_poll = now + POLL_SECONDS
        try:
            result = within(_CHAIN_SECONDS, lambda: _poll(attempt))
        except (PlexTvUnavailable, TooManyLeft):
            return
        except PinExpired:
            attempt.collected = True
            attempt.fail("expired")
            return
        except TookTooLong:
            # The call goes on and signs out what it gets, but nobody will hear whether it did.
            attempt.signed_out = not attempt.collected
            attempt.fail("plex_unavailable")
            return
        if result is None:
            return
        attempt.signed_out = result.signed_out
        if result.identity is None:
            attempt.fail(result.reason or "plex_unavailable")
        else:
            attempt.state, attempt.identity = "confirm", result.identity
    finally:
        attempt.polling.release()


def discard(attempt: Attempt) -> bool:
    """Give an attempt up, signing an approved but unused token out.

    False when Plex may still count this install as signed in. A sign-in approved after this is
    looked for once more when the attempt runs out.
    """
    # A poll that is running may be the one that gets the token.
    if not attempt.polling.acquire(timeout=_CHAIN_SECONDS + 5):
        return False
    try:
        if attempt.state == "waiting":
            attempt.fail("cancelled")
        _return_unused(attempt)
        return attempt.signed_out
    finally:
        attempt.polling.release()
