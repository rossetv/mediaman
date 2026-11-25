"""What is downloading, short enough for every page to ask for: the Watch page's row and the
header's ring.

One answer is shared by everybody and held for a few seconds, since making one asks Radarr,
Sonarr and NZBGet. What a service that does not answer had in hand stays as it last was.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import blake2b
from typing import Any, Literal

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
# For a release whose name says nothing that a person could read.
UNNAMED = "A download"

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
_KEY_SALT = secrets.token_bytes(16)
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
    #: What the queue knows it by: for what only NZBGet holds, the release's own name, which
    #: is told to nobody.
    dl_id: str = ""
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


def _is_arr(dl_id: str) -> bool:
    return dl_id.startswith(("radarr:", "sonarr:"))


def _key_of(dl_id: str, tmdb_id: int | None) -> str:
    """What a page tells the item by. By its TMDB id where that is known, since two films may
    share the title an id is made of."""
    if not _is_arr(dl_id):
        name = dl_id.encode(errors="replace")
        return "nzb-" + blake2b(name, key=_KEY_SALT, digest_size=12).hexdigest()
    return f"{dl_id.partition(':')[0]}::{tmdb_id}" if tmdb_id else dl_id


def _title_of(item: Mapping[str, object]) -> str:
    """Its title; of a release whose name has nothing to strip, which may be no title at all,
    a plain word."""
    dl_id, title = _text(item.get("id")), _text(item.get("title"))
    if _is_arr(dl_id):
        return title
    whole = dl_id.replace(".", " ").replace("_", " ").strip().rstrip("- ")
    return UNNAMED if title in ("", whole) else title


def for_viewer(item: Mapping[str, object]) -> dict[str, object]:
    """The id and title a viewer is told of *item*: for what only NZBGet holds they are made of
    the release's name, which is told to nobody but an admin."""
    dl_id = _text(item.get("id"))
    if _is_arr(dl_id):
        return {}
    return {"id": _key_of(dl_id, None), "title": _title_of(item)}


def _current(item: Mapping[str, object]) -> StripItem:
    state = _state_of(item)
    dl_id, tmdb_id = _text(item.get("id")), _int(item.get("tmdb_id")) or None
    # "Searched 12x · next attempt in ~4h"
    hint = _text(item.get("search_hint")).partition(" · ")[2] if state == "stuck" else ""
    return StripItem(
        key=_key_of(dl_id, tmdb_id),
        dl_id=dl_id,
        title=_title_of(item),
        kind=_kind_of(item),
        state=state,
        percent=max(0, min(100, _int(item.get("progress")))) if state in _ACTIVE else 0,
        minutes_left=_minutes_left(item.get("eta")) if state == "downloading" else None,
        line=hint,
        poster_url=_text(item.get("poster_url")),
        tmdb_id=tmdb_id,
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
    dl_id, tmdb_id = _text(item.get("id")), before.tmdb_id if before else None
    return StripItem(
        key=_key_of(dl_id, tmdb_id),
        dl_id=dl_id,
        title=_title_of(item),
        kind=_kind_of(item),
        state="ready",
        percent=100,
        line=_READY_LINES.get(_text(item.get("readiness")), ""),
        ready_seconds=math.ceil(left),
        poster_url=_text(item.get("poster_url")) or (before.poster_url if before else ""),
        art_url=before.art_url if before else "",
        tmdb_id=tmdb_id,
    )


def _service_of(dl_id: str) -> str:
    return dl_id.partition(":")[0].capitalize() if _is_arr(dl_id) else "NZBGet"


def _was(
    dl_id: str, before: Sequence[StripItem], found: Mapping[str, StripItem]
) -> StripItem | None:
    """The one of *before* that is done; None when two of its title could be it."""
    gone = [i for i in before if i.dl_id == dl_id and i.key not in found]
    ready = [i for i in gone if i.state == "ready"] or gone
    return ready[0] if len(ready) == 1 else None


def collect(data: DownloadsResponse, before: Sequence[StripItem], now: datetime) -> list[StripItem]:
    """The items of *data*, in an order that moves nothing while its state stands: what is done
    or coming in, then what waits its turn, then what is looked for. *before* is the last
    answer's, for what a service that does not answer holds."""
    known = {item.key: item for item in before}
    down = data["unreachable"]
    found: dict[str, StripItem] = {}
    for raw in [*([data["hero"]] if data["hero"] else []), *data["queue"]]:
        item = _current(raw)
        if item.key in found:
            item = replace(item, key=f"{item.key}#{len(found)}")
        was = known.get(item.key)
        lost = "NZBGet" in down and item.state in ("searching", "stuck")
        if lost and was and was.state in ("downloading", "almost_ready", "queued"):
            item = replace(was, held=True)
        found[item.key] = item
    live = {item.dl_id for item in found.values()}
    done = {_text(raw.get("id")) for raw in data["recent"]}
    for raw in data["recent"]:
        ready = _ready(raw, _was(_text(raw.get("id")), before, found), now)
        if ready is not None:
            found.setdefault(ready.key, ready)
    for key, was in known.items():
        if was.state == "ready" or key in found or was.dl_id in done:
            continue
        # Listed now that its TMDB id is known.
        if was.tmdb_id is None and was.dl_id in live:
            continue
        if _service_of(was.dl_id) in down:
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


def _tmdb_ref(item: StripItem) -> str | None:
    kind = "movie" if item.kind == "movie" else "tv"
    return f"tmdb-{kind}-{item.tmdb_id}" if item.ref and item.tmdb_id else None


def _shown(item: StripItem) -> dict[str, Any]:
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
        "tmdb_ref": _tmdb_ref(item),
        "href": item.href,
        "poster_url": item.poster_url,
        "art_url": item.art_url,
    }


def payload(answer: Answer) -> dict[str, Any]:
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
        "age_ms": round(answer.age_s * 1000),
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
        return self._make(make)

    def offer(self, make: Callable[[Strip], Strip], asked_at: float) -> Answer | None:
        """What stands once *make* has made it of what was read from *asked_at* on, unless it is
        being made or was since. Nobody is waited for; None when nothing is known."""
        with self._lock:
            mine = not self._making and (self._strip is None or asked_at >= self._made_at)
            if not mine:
                return None if self._strip is None else self._answer()
            if asked_at - self._made_at > STALE_S:
                self._strip = None  # nobody looked meanwhile: what it held may be long gone
            self._making = True
        try:
            return self._make(make)
        except Unavailable:
            return None

    def now(self) -> float:
        return self._clock()

    def _make(self, make: Callable[[Strip], Strip]) -> Answer:
        with self._lock:
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
#: The same of what the Downloads page reads, with no titles looked up for it.
on_page = Shared()
