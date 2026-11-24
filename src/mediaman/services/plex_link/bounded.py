"""An overall time limit for calls that a slow server could otherwise stretch without end.

A socket timeout starts again with every byte, so a server that drips its answer is never timed
out. The work runs on a thread of its own, which is left behind when its time is up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class TookTooLong(Exception):
    """The work was started and left behind, still running."""


class TooBusy(Exception):
    """Nothing was started: too much other work is still running."""


class Pool:
    """At most *size* calls at once, those left behind included."""

    def __init__(self, size: int) -> None:
        self._free = threading.BoundedSemaphore(size)

    def within[T](self, seconds: float, work: Callable[[], T]) -> T:
        """The result of *work*, or what it raised. It must not touch a database connection."""
        if not self._free.acquire(blocking=False):
            raise TooBusy
        results: list[T] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                results.append(work())
            except BaseException as exc:  # rationale: handed to the caller, whatever it is.
                errors.append(exc)
            finally:
                self._free.release()

        thread = threading.Thread(target=run, name="plex-link-call", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._free.release()
            raise TooBusy from None
        thread.join(max(0.0, seconds))
        if thread.is_alive():
            raise TookTooLong
        if errors:
            raise errors[0]
        return results[0]


# Apart, so that a Plex server that hangs cannot stop anyone's sign-in at plex.tv.
PLEX_SERVER = Pool(4)
PLEX_TV = Pool(8)
