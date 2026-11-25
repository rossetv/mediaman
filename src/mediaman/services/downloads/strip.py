"""What is downloading, short enough for every page to ask for: the Watch page's row and the
header's ring.

One answer is shared by everybody and held for a few seconds, since making one asks Radarr,
Sonarr and NZBGet. What a service that does not answer had in hand stays as it last was.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from mediaman.core.time import parse_iso_utc
from mediaman.services.downloads.download_queue.items import DownloadsResponse

logger = logging.getLogger(__name__)

HOLD_S = 4.0
# An answer older than this is how things last were.
STALE_S = 30.0
# A caller with no better to fall back on waits this long for the answer being made.
WAIT_S = 3.0
READY_S = 60
MAX_ITEMS = 20
# The ninth search comes some nine hours after the first, about when Downloads offers an admin
# to abandon the title. Everybody sees how often it was searched for.
STUCK_AFTER_SEARCHES = 9

STALE_LINE = "Can't check downloads"

State = Literal["downloading", "almost_ready", "queued", "searching", "stuck", "ready"]

_ACTIVE: frozenset[State] = frozenset({"downloading", "almost_ready"})
_RANK: dict[State, int] = {
    "ready": 0,
    "downloading": 0,
    "almost_ready": 0,
    "queued": 1,
    "searching": 2,
    "stuck": 2,
}
_ETA = re.compile(r"~(?:(\d+) hr )?(\d+) (min|sec) remaining")


class Unavailable(Exception):
    """Nothing is known yet, and the services have not answered."""


@dataclass(frozen=True, slots=True)
class StripItem:
    key: str
    title: str
    kind: Literal["movie", "show"]
    state: State
    percent: int = 0
    minutes_left: int | None = None
    line: str = ""
    held: bool = False
    ready_seconds: int | None = None
    poster_url: str = ""
    art_url: str = ""
    tmdb_id: int | None = None
    #: The title it opens, None when not known.
    ref: str | None = None
    href: str = "/downloads"


@dataclass(frozen=True, slots=True)
class Strip:
    items: tuple[StripItem, ...] = ()
    outage: str = ""


@dataclass(frozen=True, slots=True)
class Answer:
    strip: Strip
    age_s: float


def _minutes_left(eta: object) -> int | None:
    found = _ETA.fullmatch(eta) if isinstance(eta, str) else None
    if found is None:
        return None
    if found[3] == "sec":
        return 1
    return int(found[1] or 0) * 60 + int(found[2])


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _state_of(item: Mapping[str, object]) -> State:
    state = item.get("state")
    if state == "downloading":
        return "downloading"
    if state == "almost_ready":
        return "almost_ready"
    # Only an item NZBGet does not hold names where it comes from.
    if state == "queued" or not item.get("arr_source"):
        return "queued"
    return "stuck" if _int(item.get("search_count")) >= STUCK_AFTER_SEARCHES else "searching"


def _kind_of(item: Mapping[str, object]) -> Literal["movie", "show"]:
    return "movie" if item.get("media_type") == "movie" else "show"


def _current(item: Mapping[str, object]) -> StripItem:
    state = _state_of(item)
    # "Searched 12x · next attempt in ~4h"
    hint = _text(item.get("search_hint")).partition(" · ")[2] if state == "stuck" else ""
    return StripItem(
        key=_text(item.get("id")),
        title=_text(item.get("title")),
        kind=_kind_of(item),
        state=state,
        percent=max(0, min(100, _int(item.get("progress")))) if state in _ACTIVE else 0,
        minutes_left=_minutes_left(item.get("eta")) if state == "downloading" else None,
        line=hint,
        poster_url=_text(item.get("poster_url")),
        tmdb_id=_int(item.get("tmdb_id")) or None,
    )


_READY_LINES = {"part": "Some episodes ready", "unknown": "Added"}


def _ready(item: Mapping[str, object], before: StripItem | None, now: datetime) -> StripItem | None:
    """The item of a download that is done, for as long as it says so."""
    done = parse_iso_utc(_text(item.get("completed_at")))
    if done is None:
        return None
    left = READY_S - max(0.0, (now - done).total_seconds())
    if left <= 0:
        return None
    return StripItem(
        key=_text(item.get("id")),
        title=_text(item.get("title")),
        kind=_kind_of(item),
        state="ready",
        percent=100,
        line=_READY_LINES.get(_text(item.get("readiness")), ""),
        ready_seconds=math.ceil(left),
        poster_url=_text(item.get("poster_url")) or (before.poster_url if before else ""),
        art_url=before.art_url if before else "",
        tmdb_id=before.tmdb_id if before else None,
    )


def _service_of(key: str) -> str:
    return {"radarr": "Radarr", "sonarr": "Sonarr"}.get(key.partition(":")[0], "NZBGet")


def collect(data: DownloadsResponse, before: Sequence[StripItem], now: datetime) -> list[StripItem]:
    """The items of *data*, in an order that moves nothing while its state stands: what is done
    or coming in, then what waits its turn, then what is looked for. *before* is the last
    answer's, for what a service that does not answer holds."""
    known = {item.key: item for item in before}
    down = data["unreachable"]
    found: dict[str, StripItem] = {}
    for raw in [*([data["hero"]] if data["hero"] else []), *data["queue"]]:
        item = _current(raw)
        # The ids are made of the title, which two films may share.
        if item.key in found:
            item = replace(item, key=f"{item.key}#{item.tmdb_id or len(found)}")
        was = known.get(item.key)
        lost = "NZBGet" in down and item.state in ("searching", "stuck")
        if lost and was and was.state in ("downloading", "almost_ready", "queued"):
            item = replace(was, held=True)
        found[item.key] = item
    done = {_text(raw.get("id")) for raw in data["recent"]}
    for raw in data["recent"]:
        ready = _ready(raw, known.get(_text(raw.get("id"))), now)
        if ready is not None:
            found.setdefault(ready.key, ready)
    for key, was in known.items():
        if was.state == "ready" or key in found or key in done:
            continue
        if _service_of(key) in down:
            found[key] = replace(was, held=True)
    place = {key: n for n, key in enumerate(known)}
    return sorted(found.values(), key=lambda i: (_RANK[i.state], place.get(i.key, len(place))))


def summary(items: Sequence[StripItem]) -> dict[str, int]:
    active = [i.percent for i in items if i.state in _ACTIVE]
    ready = sum(i.state == "ready" for i in items)
    idle = 100 if ready else 0
    return {
        "total": len(items),
        "active": len(active),
        "waiting": len(items) - len(active) - ready,
        "ready": ready,
        "percent": round(sum(active) / len(active)) if active else idle,
    }


def _shown(item: StripItem) -> dict[str, object]:
    return {
        "key": item.key,
        "title": item.title,
        "kind": item.kind,
        "state": item.state,
        "percent": item.percent,
        "minutes_left": item.minutes_left,
        "line": item.line,
        "held": item.held,
        "ready_seconds": item.ready_seconds,
        "ref": item.ref,
        "href": item.href,
        "poster_url": item.poster_url,
        "art_url": item.art_url,
    }


def payload(answer: Answer) -> dict[str, object]:
    """What a page is told: the same for an admin and a viewer."""
    stale = answer.age_s > STALE_S
    items: list[StripItem] = []
    for item in answer.strip.items:
        if item.ready_seconds is not None:
            left = math.ceil(item.ready_seconds - answer.age_s)
            if left > 0:
                items.append(replace(item, ready_seconds=left))
        else:
            items.append(replace(item, held=True) if stale else item)
    return {
        "summary": summary(items),
        "outage": answer.strip.outage or (STALE_LINE if stale else ""),
        "items": [_shown(item) for item in items[:MAX_ITEMS]],
    }


class Shared:
    """The one answer. One caller at a time makes it; meanwhile the others get the last."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Condition()
        self._strip: Strip | None = None
        self._made_at = 0.0
        self._next_at = 0.0
        self._making = False

    def get(self, make: Callable[[Strip], Strip]) -> Answer:
        """The answer, made by *make* from the last one when that is past its time. Raises
        Unavailable when there is none and none can be made."""
        with self._lock:
            if self._making:
                if self._is_stale():
                    self._lock.wait_for(lambda: not self._making, timeout=WAIT_S)
                return self._answer()
            if self._strip is not None and self._clock() < self._next_at:
                return self._answer()
            self._making = True
            before = self._strip or Strip()
        made = None
        try:
            made = make(before)
        except Exception:
            logger.warning("Failed to read what is downloading", exc_info=True)
        finally:
            with self._lock:
                self._making = False
                self._next_at = self._clock() + HOLD_S
                if made is not None:
                    self._strip, self._made_at = made, self._clock()
                self._lock.notify_all()
        with self._lock:
            return self._answer()

    def last(self) -> Answer | None:
        """The last answer, while it is not how things once were; nobody is asked."""
        with self._lock:
            return None if self._is_stale() else self._answer()

    def clear(self) -> None:
        with self._lock:
            self._strip = None

    def _is_stale(self) -> bool:
        return self._strip is None or self._clock() - self._made_at > STALE_S

    def _answer(self) -> Answer:
        if self._strip is None:
            raise Unavailable
        return Answer(self._strip, self._clock() - self._made_at)


shared = Shared()
