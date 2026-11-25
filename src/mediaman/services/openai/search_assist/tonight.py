"""What fits an evening: library titles a person has not begun, by how long they run.

The model has no part in this, so it answers at once.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from mediaman.services.infra import disk_cache
from mediaman.services.media_meta.tmdb import TmdbClient
from mediaman.services.watch_catalog import (
    GENRES,
    TitleNotFound,
    TmdbUnavailable,
    details,
    library_titles,
)

PER_SET = 3
# Running times read from the cache for one request, and asked of TMDB for it.
_READS = 150
_ASKS = 3
# Enjoyed titles a reason may name.
_TELLING = 20

TitleKey = tuple[str, int]

_lock = threading.Lock()
# What TMDB holds of a title that bears on an evening, and does not change.
_facts: dict[TitleKey, Facts] = {}
_FACTS_MAX = 20_000


@dataclass(frozen=True, slots=True)
class Facts:
    minutes: int | None
    genres: tuple[str, ...]
    director: str | None


@dataclass(frozen=True, slots=True)
class Known:
    """What the library's own table holds of a title."""

    genres: frozenset[str]
    rating: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """A library title not begun. *episodes* is of a show's first season; None for a film."""

    ref: str
    key: TitleKey
    title: str
    year: int | None
    episodes: int | None = None
    level: int = 0
    facts: Facts | None = None
    reason: str = ""

    @property
    def minutes(self) -> int | None:
        if self.facts is None or self.facts.minutes is None:
            return None
        return self.facts.minutes * (1 if self.episodes is None else self.episodes)


@dataclass(frozen=True, slots=True)
class Evening:
    titles: list[Candidate]
    total: int
    shortest: Candidate | None


def known_titles(conn: sqlite3.Connection) -> dict[TitleKey, Known]:
    genres: dict[TitleKey, set[str]] = {}
    ratings: dict[TitleKey, float] = {}
    for genre in GENRES.values():
        for item in library_titles(conn, genre)[0]:
            key = (item["media_type"], item["id"])
            genres.setdefault(key, set()).add(genre.id)
            ratings[key] = float(item["vote_average"] or 0)
    return {key: Known(frozenset(found), ratings[key]) for key, found in genres.items()}


def _facts_reader(client: TmdbClient) -> Callable[[TitleKey], Facts | None]:
    reads = asks = 0

    def may_ask() -> bool:
        nonlocal asks
        asks += 1
        return asks <= _ASKS

    def read(key: TitleKey) -> Facts | None:
        nonlocal reads, asks
        with _lock:
            held = _facts.get(key)
        if held is not None:
            return held
        if reads >= _READS:
            return None
        reads += 1
        try:
            found: dict[str, Any] = details(client, *key, allow_fetch=may_ask, brief=True)
        except TmdbUnavailable:
            asks = _ASKS
            return None
        except (TitleNotFound, disk_cache.Refused):
            return None
        facts = Facts(found["runtime_min"], tuple(found["genres"]), found["director"])
        with _lock:
            if len(_facts) >= _FACTS_MAX:
                _facts.clear()
            _facts[key] = facts
        return facts

    return read


def evening(
    client: TmdbClient,
    candidates: Iterable[Candidate],
    known: Mapping[TitleKey, Known],
    enjoyed: Mapping[TitleKey, str],
    minutes: int,
    *,
    skip: int = 0,
) -> Evening:
    """Three of *candidates* that run no longer than *minutes*, after the first *skip* of
    them: the most like what the person enjoyed first, what they rated down last.
    *enjoyed* is the titles they liked or lately played, the most telling first."""
    read = _facts_reader(client)
    makers: dict[str, str] = {}
    liked_genres: dict[str, str] = {}
    for key, title in list(enjoyed.items())[:_TELLING]:
        facts = read(key)
        if facts and facts.director:
            makers.setdefault(facts.director, title)
        if facts and facts.genres:
            liked_genres.setdefault(facts.genres[0], title)
    taste: Counter[str] = Counter()
    for key in enjoyed:
        if key in known:
            taste.update(known[key].genres)
    telling = {g for g, n in taste.most_common(3)}

    def order(c: Candidate) -> tuple[bool, int, float, str]:
        held = known.get(c.key)
        shared = len(held.genres & telling) if held else 0
        return (c.level < 0, -shared, -(held.rating if held else 0.0), c.title.casefold())

    timed = [
        replace(c, facts=facts)
        for c in sorted(candidates, key=order)
        if (facts := read(c.key)) is not None and facts.minutes
    ]
    fits = [c for c in timed if (c.minutes or 0) <= minutes]
    shortest = min(timed, key=lambda c: c.minutes or 0, default=None)
    chosen = fits[skip : skip + PER_SET]
    return Evening(
        [replace(c, reason=_reason(c, makers, liked_genres, shortest)) for c in chosen],
        len(fits),
        None if fits else shortest,
    )


def _reason(
    c: Candidate,
    makers: Mapping[str, str],
    liked_genres: Mapping[str, str],
    shortest: Candidate | None,
) -> str:
    assert c.facts is not None
    if c.facts.director and c.facts.director in makers:
        return f"From {c.facts.director}, who also made {makers[c.facts.director]}."
    genre = next(iter(c.facts.genres), None)
    if genre and genre in liked_genres:
        return f"{genre}, like {liked_genres[genre]}."
    if shortest is not None and c.ref == shortest.ref:
        return "The shortest title here that you haven’t begun."
    return f"{genre}, and new to you." if genre else "New to you."
