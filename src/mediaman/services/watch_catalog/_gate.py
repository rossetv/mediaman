"""How many requests may wait on TMDB at once, and for how long none asks it once it gives
no answer: a TMDB that hangs must leave worker threads to everything else.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

#: Requests waiting on TMDB at once, of the server's 40 worker threads.
SLOTS = 10
#: After this many failures in a row, nobody asks TMDB for ``PAUSE_S``, but for one request
#: every ``PROBE_S`` that finds out whether it is back.
FAILURES_MAX = 2
PAUSE_S = 15.0
PROBE_S = 5.0

_clock = time.monotonic


class TmdbUnavailable(Exception):
    """TMDB failed and nothing usable was cached."""


class TmdbBusy(TmdbUnavailable):
    """TMDB answered 429, or too many wait on it, and nothing usable was cached."""


class TmdbDown(TmdbUnavailable):
    """TMDB could not be reached, did not answer in time, or answered with a 5xx."""


class Gate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._free = SLOTS
            self._token: str | None = None
            self._answered()

    def _answered(self) -> None:
        self._failures = 0
        self._paused_until = self._probe_at = 0.0
        self._error: type[TmdbUnavailable] = TmdbDown

    def _of(self, token: str) -> None:
        # What one token was refused says nothing of the one that replaced it.
        if token != self._token:
            self._token = token
            self._answered()

    @contextlib.contextmanager
    def asking(self, token: str, *, probe: bool = True) -> Iterator[None]:
        """Held while one request waits on TMDB. Raises at once TmdbBusy when too many do,
        and what TMDB last failed with while it is paused, unless *probe* and it is time to
        try again."""
        with self._lock:
            self._of(token)
            now = _clock()
            paused = now < self._paused_until
            if paused and not (probe and now >= self._probe_at):
                raise self._error("paused")
            if not self._free:
                raise TmdbBusy("too many wait on TMDB")
            if paused:
                self._probe_at = now + PROBE_S
            self._free -= 1
        try:
            yield
        finally:
            with self._lock:
                self._free += 1

    def check(self, token: str) -> None:
        """Raises as ``asking`` while TMDB is paused."""
        with self._lock:
            if token == self._token and _clock() < self._paused_until:
                raise self._error("paused")

    def note(self, token: str, error: TmdbUnavailable | None) -> None:
        """Take in how an ask ended. What is wrong with one title is still an answer."""
        with self._lock:
            if token != self._token:
                return
            if not isinstance(error, TmdbDown | TmdbBusy):
                self._answered()
                return
            self._failures += 1
            if self._failures >= FAILURES_MAX:
                now = _clock()
                self._paused_until, self._probe_at = now + PAUSE_S, now + PROBE_S
                self._error = type(error)


gate = Gate()
