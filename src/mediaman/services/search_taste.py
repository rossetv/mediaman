"""What the search sheet offers one person before anything is typed: every category in the order
of what they watch, the few they watch most, and searches made from their own plays and ratings.

Read from that person's rows alone, at each Watch page; no AI and no TMDB call (the library's
genres and cast are already held, from title_meta).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from mediaman.core.time import parse_iso_strict_utc
from mediaman.services import watch_catalog as catalog
from mediaman.services.openai.recommendations.plays import person_plays

# A play this many days old counts half as much as one today.
HALF_LIFE_DAYS = 60.0
FAVOURITES = 3
CHIPS = 4
# Played titles of the library it takes before the sheet speaks of a taste at all.
_ENOUGH = 3
_RATED = {2: 2.0, 1: 1.0, -1: -1.5, -2: -3.0}
# The order of the sheet for someone it knows nothing of.
_FIRST = (
    "action", "comedy", "thriller", "science-fiction", "drama", "horror",
    "family", "crime", "romance", "fantasy", "animation", "documentary",
)  # fmt: skip
DEFAULT_ORDER = (*_FIRST, *(g for g in catalog.GENRES if g not in _FIRST))
# How a category is said in a suggestion: (films, series, both).
_NOUN = {
    "comedy": ("feel-good comedy films", "comedy series to binge", "feel-good comedies"),
    "animation": ("animated films", "anime to binge", "anime and animation"),
    "romance": ("romantic films", "romance series", "love stories"),
    "drama": ("drama films", "drama series", "dramas"),
    "thriller": ("thrillers", "thriller series", "thrillers"),
    "mystery": ("mystery films", "mystery series", "mysteries"),
    "crime": ("crime films", "crime series", "crime stories"),
    "science-fiction": ("sci-fi films", "sci-fi series", "sci-fi"),
    "horror": ("horror films", "horror series", "horror"),
    "family": ("family films", "family series", "family films"),
    "documentary": ("documentaries", "docuseries", "documentaries"),
}
_KINDS = ("movie", "tv", "all")


def search_taste(
    conn: sqlite3.Connection, username: str, user_id: int, now: datetime
) -> dict[str, Any]:
    """``order``: every category id, the most watched first and the rated down last;
    ``favourites``: the few to mark; ``chips``: plain suggested searches; ``like``: the title
    liked most lately, for "More like". Someone with too few plays gets the usual order alone."""
    known = catalog.library_known(conn)
    weights, names, played = _weights(conn, username, user_id, now)
    weights = {key: w for key, w in weights.items() if key in known}
    if sum(1 for key in played if key in known) < _ENOUGH:
        return {
            "personal": False,
            "order": list(DEFAULT_ORDER),
            "favourites": [],
            "chips": [],
            "like": None,
        }

    score: dict[str, float] = defaultdict(float)
    titles: dict[str, int] = defaultdict(int)
    kinds: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for (kind, tmdb_id), weight in weights.items():
        genres = known[(kind, tmdb_id)][0]
        for genre in genres:
            # A title of many genres says less of each.
            score[genre] += weight / math.sqrt(len(genres))
            if weight > 0:
                titles[genre] += 1
                kinds[genre][kind] += weight
    liked = sorted((g for g in DEFAULT_ORDER if score[g] > 0), key=lambda g: -score[g])
    unplayed = [g for g in DEFAULT_ORDER if score[g] == 0]
    disliked = sorted((g for g in DEFAULT_ORDER if score[g] < 0), key=lambda g: -score[g])
    top = score[liked[0]] if liked else 0.0
    favourites = [g for g in liked if titles[g] >= 2 and score[g] >= top / 3][:FAVOURITES]
    return {
        "personal": True,
        "order": [*liked, *unplayed, *disliked],
        "favourites": favourites,
        "chips": _chips(liked, kinds, weights, known),
        "like": _like(weights, names),
    }


def _weights(
    conn: sqlite3.Connection, username: str, user_id: int, now: datetime
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], str], set[tuple[str, int]]]:
    """How much each title counts (its play, newer ones more, and the person's rating of it),
    the name of each, and which were played."""
    found: dict[tuple[str, int], float] = defaultdict(float)
    names: dict[tuple[str, int], str] = {}
    played: set[tuple[str, int]] = set()
    for play in person_plays(conn, username):
        if play.tmdb_id is None:
            continue
        key = (play.kind, play.tmdb_id)
        at = parse_iso_strict_utc(play.last_played)
        days = max(0.0, (now - at).total_seconds() / 86400) if at else HALF_LIFE_DAYS * 3
        weight = 0.5 ** (days / HALF_LIFE_DAYS)
        if play.note.startswith("left"):
            weight *= 0.3
        elif play.note == "watched again":
            weight *= 1.5
        found[key] += weight
        names.setdefault(key, play.title)
        played.add(key)
    for row in conn.execute(
        "SELECT media_type, tmdb_id, level, title FROM user_ratings WHERE user_id = ?", (user_id,)
    ):
        key = (row[0], row[1])
        found[key] += _RATED.get(row[2], 0.0)
        if row[3]:
            names.setdefault(key, row[3])
    return found, names, played


def _lean(kinds: dict[str, float]) -> str:
    films, shows = kinds.get("movie", 0.0), kinds.get("tv", 0.0)
    return "movie" if films > shows * 2 else "tv" if shows > films * 2 else "all"


def _genre_chip(genre: str, sort: str, kind: str) -> dict[str, Any]:
    plain = catalog.GENRES[genre].name.lower()
    nouns = _NOUN.get(genre)
    noun = (
        nouns[_KINDS.index(kind)]
        if nouns
        else {"movie": f"{plain} films", "tv": f"{plain} series", "all": plain}[kind]
    )
    text = {
        "popular": noun[0].upper() + noun[1:],
        "top_rated": f"Top-rated {noun}",
        "newest": f"New {plain} "
        + {"movie": "films", "tv": "series", "all": "films and series"}[kind],
    }[sort]
    return {"kind": "genre", "genre": genre, "type": kind, "sort": sort, "text": text}


def _chips(
    liked: list[str],
    kinds: dict[str, dict[str, float]],
    weights: dict[tuple[str, int], float],
    known: dict[tuple[str, int], tuple[list[str], list[Any]]],
) -> list[dict[str, Any]]:
    """Their top category, their second at its best rated, the actor in most of what they
    liked, and what is new in their top category."""
    chips: list[dict[str, Any]] = []
    if liked:
        chips.append(_genre_chip(liked[0], "popular", _lean(kinds[liked[0]])))
    if len(liked) > 1:
        chips.append(_genre_chip(liked[1], "top_rated", _lean(kinds[liked[1]])))
    star = _star(weights, known)
    if star is not None:
        person_id, name, path = star[:3]
        chips.append(
            {
                "kind": "person",
                "person_id": person_id,
                "name": name,
                "text": f"With {name}",
                "profile_url": catalog.tmdb_image_url(path, "w185"),
            }
        )
    if liked:
        chips.append(_genre_chip(liked[0], "newest", _lean(kinds[liked[0]])))
    return chips[:CHIPS]


def _star(
    weights: dict[tuple[str, int], float],
    known: dict[tuple[str, int], tuple[list[str], list[Any]]],
) -> list[Any] | None:
    """The billed actor in most of the titles they liked, two at least; the more liked wins a
    tie."""
    people: dict[int, list[Any]] = {}
    count: dict[int, int] = defaultdict(int)
    liking: dict[int, float] = defaultdict(float)
    for key, weight in weights.items():
        if weight <= 0:
            continue
        for person in known[key][1][:4]:
            people.setdefault(person[0], person)
            count[person[0]] += 1
            liking[person[0]] += weight
    best = max(count, key=lambda p: (count[p], liking[p]), default=None)
    return people[best] if best is not None and count[best] >= 2 else None


def _like(
    weights: dict[tuple[str, int], float], names: dict[tuple[str, int], str]
) -> dict[str, str] | None:
    best = max((kv for kv in weights.items() if kv[0] in names), key=lambda kv: kv[1], default=None)
    if best is None or best[1] < 1.0:
        return None
    (kind, tmdb_id), _ = best
    name = names[(kind, tmdb_id)]
    return {"ref": f"tmdb-{kind}-{tmdb_id}", "name": name, "text": f"More like {name}"}
