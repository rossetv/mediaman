"""An overall time limit for calls that a slow server could otherwise stretch without end.

A socket timeout starts again with every byte, so a server that drips its answer is never timed
out. The work runs on a thread of its own, which is left behind when its time is up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

# Left-behind threads that may be alive at once before no more work is started.
_MAX_LEFT = 4

_left: list[threading.Thread] = []
_left_lock = threading.Lock()


class TookTooLong(Exception):
    """The work was started and left behind, still running."""


class TooManyLeft(Exception):
    """Nothing was started: too much earlier work is still running."""


def within[T](seconds: float, work: Callable[[], T]) -> T:
    """The result of *work*, or what it raised. It must not touch a database connection."""
    with _left_lock:
        _left[:] = [t for t in _left if t.is_alive()]
        if len(_left) >= _MAX_LEFT:
            raise TooManyLeft
    results: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(work())
        except BaseException as exc:  # rationale: handed to the caller, whatever it is.
            errors.append(exc)

    thread = threading.Thread(target=run, name="plex-link-call", daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        with _left_lock:
            _left.append(thread)
        raise TookTooLong
    if errors:
        raise errors[0]
    return results[0]
