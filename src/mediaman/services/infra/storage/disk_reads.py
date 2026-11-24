"""Disk reads for request threads, which a mount that hangs must never hold.

Not for a deletion guard: there a disk that does not answer must keep refusing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

_DEADLINE_SECONDS = 2.0
_FAILURE_TTL_SECONDS = 3.0


class _Read[T]:
    def __init__(self, started: float) -> None:
        self.started = started
        self.done = False
        self.value: T | None = None


class DiskReads[T]:
    """At most one read in flight per path, and *slots* in all, each waited for 2 s at most.

    A path whose read failed is not read again for 3 s, and one whose read hangs is not
    read again, nor waited for, until it ends.
    """

    def __init__(self, slots: int) -> None:
        self._slots = slots
        self._changed = threading.Condition()
        self._running: dict[str, _Read[T]] = {}
        self._failed_until: dict[str, float] = {}

    def reset(self) -> None:
        """Forget every failure and leave the reads in flight to themselves."""
        with self._changed:
            self._running.clear()
            self._failed_until.clear()

    def read(self, path: str, reader: Callable[[str], T]) -> T | None:
        """Return ``reader(path)``, or None when it raises or is not back in time."""
        return self.read_all([path], reader)[path]

    def read_all(self, paths: Iterable[str], reader: Callable[[str], T]) -> dict[str, T | None]:
        """Return ``read`` of each of *paths*, read together under one deadline."""
        deadline = time.monotonic() + _DEADLINE_SECONDS
        reads = {path: self._start(path, reader, deadline) for path in paths}
        return {path: self._wait(read) if read else None for path, read in reads.items()}

    def _wait(self, read: _Read[T]) -> T | None:
        with self._changed:
            left = read.started + _DEADLINE_SECONDS - time.monotonic()
            self._changed.wait_for(lambda: read.done, max(0.0, left))
            return read.value

    def _start(self, path: str, reader: Callable[[str], T], deadline: float) -> _Read[T] | None:
        """Return the read in flight for *path*, a new one, or None when none may start."""
        with self._changed:
            while True:
                now = time.monotonic()
                if self._failed_until.get(path, 0.0) > now:
                    return None
                if running := self._running.get(path):
                    return running
                if len(self._running) < self._slots:
                    break
                # A slot can only come free soon from a read that is not overdue.
                newest = max(read.started for read in self._running.values())
                left = min(deadline, newest + _DEADLINE_SECONDS) - now
                if left <= 0:
                    return None
                self._changed.wait(left)
            read: _Read[T] = _Read(now)
            self._running[path] = read
            # A daemon thread: the interpreter waits for a pool's threads when it exits.
            thread = threading.Thread(
                target=self._run, args=(path, reader, read), name="disk-read", daemon=True
            )
            try:
                thread.start()
            except RuntimeError:
                del self._running[path]
                return None
            return read

    def _run(self, path: str, reader: Callable[[str], T], read: _Read[T]) -> None:
        value: T | None = None
        try:
            value = reader(path)
        except OSError:
            logger.debug("Disk read of %s failed", path)
        except Exception:  # rationale: whatever a read raises, its slot must come free
            logger.exception("Disk read of %s failed", path)
        with self._changed:
            now = time.monotonic()
            if self._running.get(path) is read:
                del self._running[path]
                # Too slow counts as failed, or every call would wait for it again.
                if value is None or now - read.started >= _DEADLINE_SECONDS:
                    self._failed_until = {
                        p: until for p, until in self._failed_until.items() if until > now
                    }
                    self._failed_until[path] = now + _FAILURE_TTL_SECONDS
                else:
                    read.value = value
            read.done = True
            self._changed.notify_all()


# Apart, so that the folders an admin types in Settings cannot take the panel's slots.
library_disk_reads: DiskReads[dict[str, int]] = DiskReads(16)
media_root_reads: DiskReads[dict[str, int]] = DiskReads(1)
settings_disk_reads: DiskReads[dict[str, float]] = DiskReads(4)
