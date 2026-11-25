"""Tickets that let a TV, which has no session, read one stream.

A ticket is a random token in the stream's address. Only its hash is kept, in memory, bound to
the stream, its user and the session that started it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

TICKET_SECONDS = 6 * 3600.0

_TICKET_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Ticket:
    stream_id: str
    user: str
    #: Hash of the session that started the stream, as the sessions table keys it, or of the
    #: token of the paired TV it was started for, as the TVs table does.
    session_hash: str
    expires: float


class TicketStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def issue(
        self, stream_id: str, user: str, session_hash: str, *, live: Callable[[Ticket], bool]
    ) -> str:
        """Return a new ticket for *stream_id*, forgetting those that ran out or are not *live*."""
        token = secrets.token_urlsafe(32)
        ticket = Ticket(stream_id, user, session_hash, self._clock() + TICKET_SECONDS)
        with self._lock:
            held = list(self._tickets.items())
        ended = [key for key, t in held if self.expired(t) or not live(t)]
        with self._lock:
            for key in ended:
                self._tickets.pop(key, None)
            self._tickets[_hash(token)] = ticket
        return token

    def read(self, token: str) -> Ticket | None:
        """The ticket *token* stands for, or None.

        One that ran out is handed back too, so that the caller can end its stream.
        """
        if not _TICKET_RE.fullmatch(token):
            return None
        with self._lock:
            return self._tickets.get(_hash(token))

    def expired(self, ticket: Ticket) -> bool:
        return self._clock() >= ticket.expires

    def move(self, session_hash: str, new_session_hash: str) -> None:
        """Hand the tickets of the session *session_hash* to the one that replaces it."""
        with self._lock:
            for key, ticket in self._tickets.items():
                if ticket.session_hash == session_hash:
                    self._tickets[key] = replace(ticket, session_hash=new_session_hash)

    def drop(self, token: str) -> None:
        with self._lock:
            self._tickets.pop(_hash(token), None)


_store = TicketStore()


def get_ticket_store() -> TicketStore:
    return _store
