"""Radarr's and Sonarr's library lists, held for the Downloads poll.

Every open Downloads page and header strip polls every few seconds, and each poll reads the
whole library of each service to find what is still searching. The lists are held for half
a minute, by service address. The queue itself is always read live. What is held is dropped as
soon as the queue's entries change, since a download that leaves the queue was just imported
and the library holds it now. It is also dropped as soon as mediaman writes to that service,
so a title added or abandoned here shows as such on the next poll.

Imports nothing of mediaman, so the client's transport can drop what is held on a write.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

HOLD_SECONDS = 30.0
_ENTRY_KEYS = ("id", "downloadId", "movieId", "seriesId", "episodeId")
#: The clock what is held is aged by.
clock: Callable[[], float] = time.monotonic


@dataclass(slots=True)
class _Held:
    at: float
    queue: frozenset[object] | None
    lists: dict[str, list[Any]] = field(default_factory=dict)


_lock = threading.Lock()
_held: dict[str, _Held] = {}
# Bumped by each write to a service, so a read begun before it is not held.
_writes: dict[str, int] = {}


def forget(url: str | None = None) -> None:
    """Drop what is held of the service at *url*, or of every service."""
    with _lock:
        if url is None:
            _held.clear()
            for key in _writes:
                _writes[key] += 1
            return
        _held.pop(url, None)
        _writes[url] = _writes.get(url, 0) + 1


def queue_of(entries: Iterable[Mapping[str, Any]]) -> frozenset[object]:
    """What tells one state of a queue from another: which download of which title each of
    its entries is."""
    return frozenset(tuple(entry.get(key) for key in _ENTRY_KEYS) for entry in entries)


def held_read[T](
    url: str,
    what: str,
    queue: frozenset[object] | None,
    read: Callable[[], list[T]],
) -> list[T]:
    """*what* of the service at *url* as held, or as *read* now and held, while its queue is
    *queue*. A read that fails is not held."""
    now = clock()
    with _lock:
        held = _held.get(url)
        if held is not None and (now - held.at >= HOLD_SECONDS or held.queue != queue):
            del _held[url]
            held = None
        if held is not None and what in held.lists:
            return list(held.lists[what])
        writes = _writes.get(url, 0)
    answer = read()
    with _lock:
        if _writes.get(url, 0) == writes:
            held = _held.get(url)
            if held is None or held.queue != queue:
                held = _held[url] = _Held(now, queue)
            held.lists[what] = list(answer)
    return answer


def held_list(url: str, what: str) -> list[Any] | None:
    """*what* of the service at *url* as held now, None when it is not: for what reads the
    library for what does not change, such as the TMDB id of each title."""
    now = clock()
    with _lock:
        held = _held.get(url)
        if held is None or now - held.at >= HOLD_SECONDS or what not in held.lists:
            return None
        return list(held.lists[what])
