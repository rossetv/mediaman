"""The ratings cache, filled in the background for the titles Watch's cards show, so that a
card has its Rotten Tomatoes and IMDb scores on its next load. A page notes what it showed
and never waits on OMDb; a scheduler job asks OMDb for a few of those titles at a time."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from typing import Any

from mediaman.core.time import now_utc
from mediaman.services.media_meta.omdb import get_omdb_key, is_key_refused, lookup_ratings
from mediaman.web.repository.search import fetch_ratings_cache, upsert_ratings_cache
from mediaman.web.repository.seen_titles import TitleKey

#: Titles asked of OMDb in a day, the asks of searches and title pages counted with them:
#: well under the 1000 a free key is allowed.
DAILY_BUDGET = 300
#: Titles asked in one run of the job.
TITLES_PER_RUN = 10
#: How long OMDb is left alone after it gave no answer (busy, down, a spent quota).
PAUSE_S = 3600.0
_SCORED_FRESH = timedelta(days=30)
# A title OMDb does not know is asked again, as it may have it by then, but not every day.
_UNSCORED_FRESH = timedelta(days=7)
# The titles noted beyond this many push out the ones noted first.
_WANTED_MAX = 1000

_clock = time.monotonic
_lock = threading.Lock()
# The titles cards showed, by when they were last noted: name and year.
_wanted: dict[TitleKey, tuple[str, int]] = {}
_resume_at = 0.0


def want(cards: Iterable[Mapping[str, Any]]) -> None:
    """Note the TMDB titles of *cards* for the job to ask OMDb of; cheap, and nothing waits.
    OMDb finds a title by its name and year, so one no card has named a year of is not noted:
    by its name alone OMDb may answer with another title of that name."""
    with _lock:
        for card in cards:
            ref, title, year = card.get("tmdb_ref"), card.get("title"), card.get("year")
            if not isinstance(ref, str) or not isinstance(title, str) or not title:
                continue
            _, media_type, tmdb_id = ref.split("-")
            key = (media_type, int(tmdb_id))
            known = _wanted.pop(key, None)
            if isinstance(year, int) and year > 0:
                _wanted[key] = (title, year)
            elif known is not None:
                _wanted[key] = known
        while len(_wanted) > _WANTED_MAX:
            del _wanted[next(iter(_wanted))]


def warm(conn: sqlite3.Connection, secret_key: str, *, stopped: Callable[[], bool]) -> None:
    """One run of the job: ask OMDb of the titles noted last that the cache has no fresh row
    of, within the run's and the day's budget. Nothing without a key, or while OMDb is left
    alone; a title OMDb gives no answer of ends the run and leaves OMDb alone for a while."""
    global _resume_at
    omdb_key = get_omdb_key(conn, secret_key)
    if not omdb_key or is_key_refused(omdb_key) or _clock() < _resume_at:
        return
    with _lock:
        noted = list(reversed(_wanted.items()))
    due = _due(conn, noted)
    room = min(TITLES_PER_RUN, DAILY_BUDGET - _asked_today(conn))
    for (media_type, tmdb_id), (title, year) in due[: max(room, 0)]:
        if stopped():
            break
        data = lookup_ratings(title, year, media_type, omdb_key=omdb_key)
        if data is None:
            _resume_at = _clock() + PAUSE_S
            break
        upsert_ratings_cache(
            conn,
            [
                (
                    tmdb_id,
                    media_type,
                    data.get("imdb"),
                    data.get("rt"),
                    data.get("metascore"),
                    now_utc().isoformat(),
                )
            ],
        )
        with _lock:
            _wanted.pop((media_type, tmdb_id), None)


def _due(
    conn: sqlite3.Connection, noted: list[tuple[TitleKey, tuple[str, int]]]
) -> list[tuple[TitleKey, tuple[str, int]]]:
    """The *noted* titles the cache holds no fresh row of; the fresh ones are forgotten."""
    now = now_utc()
    fresh: set[TitleKey] = set()
    # A few hundred at a time keeps each read well inside SQLite's limit on bound values.
    for start in range(0, len(noted), 400):
        chunk = [(tmdb_id, media_type) for (media_type, tmdb_id), _ in noted[start : start + 400]]
        for row in fetch_ratings_cache(conn, chunk):
            scored = row.imdb_rating or row.rt_rating or row.metascore
            if row.fetched_at >= (now - (_SCORED_FRESH if scored else _UNSCORED_FRESH)).isoformat():
                fresh.add((row.media_type, row.tmdb_id))
    with _lock:
        for key in fresh:
            _wanted.pop(key, None)
    return [entry for entry in noted if entry[0] not in fresh]


def _asked_today(conn: sqlite3.Connection) -> int:
    since = (now_utc() - timedelta(days=1)).isoformat()
    row = conn.execute("SELECT COUNT(*) FROM ratings_cache WHERE fetched_at >= ?", (since,))
    return int(row.fetchone()[0])


__all__ = ["DAILY_BUDGET", "PAUSE_S", "TITLES_PER_RUN", "want", "warm"]
