"""What fits an evening: what a person can play next of the library, by how long it runs, and
titles TMDB lists that are not in it.

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

PER_SET = 6
# Running times read from the cache for one request, and asked of TMDB for it.
_READS = 150
_ASKS = 3
# The same, of the titles not in the library, which are mostly new to the cache.
_ELSEWHERE_ASKS = 12
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
    """A library film, or the episode of a show a person would play next (*episode*: its season
    and number). *run* is what Plex says it runs, or has left to run, where it says; else it
    runs as TMDB says the film, or the show's episodes, do. *tier* orders it: 0 for what they
    are partway through, 1 for a show they saw some of, 2 for what they never began."""

    ref: str
    key: TitleKey
    title: str
    year: int | None
    episode: tuple[int, int] | None = None
    run: int | None = None
    tier: int = 2
    left: bool = False  # it is begun, and *run* is what it has left
    level: int = 0
    facts: Facts | None = None
    reason: str = ""

    @property
    def minutes(self) -> int | None:
        if self.run is not None:
            return self.run
        return None if self.facts is None else self.facts.minutes


@dataclass(frozen=True, slots=True)
class Evening:
    titles: list[Candidate]
    total: int
    shortest: Candidate | None
    # Whether every title's running time was known: where some were not looked up this time,
    # more may fit than *total* says.
    checked: bool = True


def known_titles(conn: sqlite3.Connection) -> dict[TitleKey, Known]:
    genres: dict[TitleKey, set[str]] = {}
    ratings: dict[TitleKey, float] = {}
    for genre in GENRES.values():
        for item in library_titles(conn, genre)[0]:
            key = (item["media_type"], item["id"])
            genres.setdefault(key, set()).add(genre.id)
            ratings[key] = float(item["vote_average"] or 0)
    return {key: Known(frozenset(found), ratings[key]) for key, found in genres.items()}


def _facts_reader(
    client: TmdbClient, most: int = _ASKS, unread: set[TitleKey] | None = None
) -> Callable[[TitleKey], Facts | None]:
    """*unread* gathers the titles whose facts were not looked up, for want of asks or of TMDB."""
    reads = asks = 0
    missed = set() if unread is None else unread

    def may_ask() -> bool:
        nonlocal asks
        asks += 1
        return asks <= most

    def read(key: TitleKey) -> Facts | None:
        nonlocal reads, asks
        with _lock:
            held = _facts.get(key)
        if held is not None:
            return held
        if reads >= _READS:
            missed.add(key)
            return None
        reads += 1
        try:
            found: dict[str, Any] = details(client, *key, allow_fetch=may_ask, brief=True)
        except TitleNotFound:
            return None
        except disk_cache.Refused:
            missed.add(key)
            return None
        except TmdbUnavailable:
            asks = most
            missed.add(key)
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
    unread: set[TitleKey] = set()
    read = _facts_reader(client, unread=unread)
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

    def order(c: Candidate) -> tuple[int, bool, int, float, str]:
        if c.tier < 2:  # the order they came in: the latest played first
            return (c.tier, False, 0, 0.0, "")
        held = known.get(c.key)
        shared = len(held.genres & telling) if held else 0
        return (2, c.level < 0, -shared, -(held.rating if held else 0.0), c.title.casefold())

    timed: list[Candidate] = []
    unknown = False
    for c in sorted(candidates, key=order):
        facts = read(c.key)
        if (facts is not None and facts.minutes) or c.run:
            timed.append(replace(c, facts=facts))
        elif c.key in unread:
            unknown = True
    fits = [c for c in timed if (c.minutes or 0) <= minutes]
    shortest = min(timed, key=lambda c: c.minutes or 0, default=None)
    chosen = fits[skip : skip + PER_SET]
    return Evening(
        [replace(c, reason=_reason(c, makers, liked_genres, shortest)) for c in chosen],
        len(fits),
        None if fits else shortest,
        not unknown,
    )


def elsewhere(
    client: TmdbClient,
    picks: Iterable[Mapping[str, Any]],
    listed: Iterable[Mapping[str, Any]],
    enjoyed: Mapping[TitleKey, str],
    minutes: int,
) -> list[tuple[Mapping[str, Any], int]]:
    """Up to a set of TMDB titles ("id" and "media_type" of each) that run no longer than
    *minutes*, a show by its episodes, with what they run: the *picks* made for the person
    first, then those *listed* of the genres they enjoyed most, else in the order given."""
    read = _facts_reader(client, _ELSEWHERE_ASKS)
    liked: Counter[str] = Counter()
    for key in list(enjoyed)[:_TELLING]:
        facts = read(key)
        if facts and facts.genres:
            liked[facts.genres[0]] += 1
    fits: list[tuple[bool, int, int, Mapping[str, Any], int]] = []
    for place, (picked, item) in enumerate(
        [(True, p) for p in picks] + [(False, i) for i in listed]
    ):
        facts = read((str(item["media_type"]), int(item["id"])))
        if facts is None or not facts.minutes or facts.minutes > minutes:
            continue
        liking = 0 if picked else max((liked[g] for g in facts.genres), default=0)
        fits.append((not picked, -liking, place, item, facts.minutes))
    fits.sort(key=lambda f: f[:3])
    return [(item, run) for *_, item, run in fits[:PER_SET]]


def _reason(
    c: Candidate,
    makers: Mapping[str, str],
    liked_genres: Mapping[str, str],
    shortest: Candidate | None,
) -> str:
    if c.episode is not None and c.tier < 2:
        return f"Next: S{c.episode[0]} · E{c.episode[1]}."
    if c.tier == 0:
        return "Pick up where you left off."
    if c.facts is None:
        return "New to you."
    if c.facts.director and c.facts.director in makers:
        return f"From {c.facts.director}, who also made {makers[c.facts.director]}."
    genre = next(iter(c.facts.genres), None)
    if genre and genre in liked_genres:
        return f"{genre}, like {liked_genres[genre]}."
    if shortest is not None and c.ref == shortest.ref:
        return "The shortest title here that you haven’t begun."
    return f"{genre}, and new to you." if genre else "New to you."
