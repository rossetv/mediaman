"""The ratings cache is filled in the background for the titles Watch's cards show, within a
daily budget, and never while a page waits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mediaman.services.infra import SafeHTTPError
from mediaman.services.media_meta import omdb
from mediaman.web.routes.watch import ratings_warm
from mediaman.web.routes.watch.card_marks import scored
from tests.unit.web.watch.test_watch_card_marks import (
    _fresh_memo,
    arr,
    as_user,
    library,
    plex,
    tmdb,
)

__all__ = ["_fresh_memo", "arr", "as_user", "library", "plex", "tmdb"]

NOON = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    tick = Clock()
    monkeypatch.setattr(ratings_warm, "_clock", tick)
    monkeypatch.setattr(ratings_warm, "now_utc", lambda: NOON)
    monkeypatch.setattr(ratings_warm, "_wanted", {})
    monkeypatch.setattr(ratings_warm, "_resume_at", 0.0)
    monkeypatch.setattr(omdb, "_refused", None)
    return tick


@pytest.fixture
def asked(monkeypatch, clock):
    """OMDb, stubbed: every title scores 85% and 7.8; the titles asked, in order."""
    titles: list[str] = []
    answers: dict[str, dict[str, str] | None] = {}

    def lookup(title, year, media_type, *, omdb_key=None, **_k):
        assert omdb_key == "key"
        titles.append(title)
        return answers.get(title, {"rt": "85%", "imdb": "7.8"})

    monkeypatch.setattr(ratings_warm, "get_omdb_key", lambda _c, _s: "key")
    monkeypatch.setattr(ratings_warm, "lookup_ratings", lookup)
    return titles, answers


def _card(n: int, kind: str = "movie") -> dict[str, object]:
    return {"tmdb_ref": f"tmdb-{kind}-{n}", "title": f"Title {n}", "year": 2000 + n}


def _cache(conn, n: int, at: datetime, rt: str | None = "70%", kind: str = "movie") -> None:
    conn.execute(
        "INSERT INTO ratings_cache (tmdb_id, media_type, imdb_rating, rt_rating, metascore,"
        " fetched_at) VALUES (?, ?, NULL, ?, NULL, ?)",
        (n, kind, rt, at.isoformat()),
    )
    conn.commit()


def _warm(conn) -> None:
    ratings_warm.warm(conn, "secret", stopped=lambda: False)


def test_a_card_shown_gets_its_scores_on_its_next_load(conn, asked):
    titles, _ = asked
    assert "rt" not in scored(conn, [_card(1, "tv")])[0]
    assert titles == []

    _warm(conn)

    assert titles == ["Title 1"]
    card = scored(conn, [_card(1, "tv")])[0]
    assert (card["rt"], card["imdb"]) == (85, "7.8")


def test_nothing_is_asked_without_an_omdb_key(conn, asked, monkeypatch):
    titles, _ = asked
    monkeypatch.setattr(ratings_warm, "get_omdb_key", lambda _c, _s: None)
    scored(conn, [_card(1)])

    _warm(conn)

    assert titles == []


def test_a_title_cached_lately_is_not_asked_again(conn, asked):
    titles, _ = asked
    _cache(conn, 1, NOON - timedelta(days=20))
    _cache(conn, 2, NOON - timedelta(days=3), rt=None)
    _cache(conn, 3, NOON - timedelta(days=40))
    _cache(conn, 4, NOON - timedelta(days=10), rt=None)
    scored(conn, [_card(n) for n in (1, 2, 3, 4)])

    _warm(conn)

    assert sorted(titles) == ["Title 3", "Title 4"]


def test_a_title_omdb_does_not_know_is_kept_as_known_and_not_asked_again(conn, asked):
    titles, answers = asked
    answers["Title 1"] = {}
    scored(conn, [_card(1)])
    _warm(conn)
    scored(conn, [_card(1)])

    _warm(conn)

    assert titles == ["Title 1"]
    assert conn.execute("SELECT COUNT(*) FROM ratings_cache").fetchone()[0] == 1


def test_one_run_asks_a_few_titles_and_the_next_run_the_rest(conn, asked):
    titles, _ = asked
    scored(conn, [_card(n) for n in range(1, 16)])

    _warm(conn)
    first = len(titles)
    _warm(conn)

    assert (first, len(titles)) == (ratings_warm.TITLES_PER_RUN, 15)


def test_no_title_is_asked_past_the_day_s_budget(conn, asked):
    titles, _ = asked
    for n in range(ratings_warm.DAILY_BUDGET - 3):
        _cache(conn, 1000 + n, NOON - timedelta(hours=20))
    _cache(conn, 5000, NOON - timedelta(hours=30))
    scored(conn, [_card(n) for n in range(1, 10)])

    _warm(conn)
    _warm(conn)

    assert len(titles) == 3


def test_a_busy_omdb_ends_the_run_and_is_left_alone_for_a_while(conn, clock, monkeypatch):
    calls: list[object] = []

    def busy(*_a, **k):
        calls.append(k["params"]["t"])
        raise SafeHTTPError(429, "Too many requests", "https://www.omdbapi.com/")

    monkeypatch.setattr(ratings_warm, "get_omdb_key", lambda _c, _s: "key")
    monkeypatch.setattr(omdb._OMDB_CLIENT, "get", busy)
    scored(conn, [_card(n) for n in range(1, 6)])

    _warm(conn)
    after_first = len(calls)
    clock.now += 60
    _warm(conn)
    paused = len(calls)
    clock.now += ratings_warm.PAUSE_S
    _warm(conn)

    assert (after_first, paused, len(calls)) == (1, 1, 2)
    assert conn.execute("SELECT COUNT(*) FROM ratings_cache").fetchone()[0] == 0


def test_a_run_ends_when_the_scheduler_stops(conn, asked):
    titles, _ = asked
    scored(conn, [_card(n) for n in range(1, 4)])

    ratings_warm.warm(conn, "secret", stopped=lambda: bool(titles))

    assert titles == ["Title 3"]


def test_a_library_title_is_asked_by_its_year(as_user, library, clock, monkeypatch):
    library.execute("UPDATE media_items SET year = 2019 WHERE id = '100'")
    library.commit()
    asks: dict[str, int | None] = {}
    monkeypatch.setattr(ratings_warm, "get_omdb_key", lambda _c, _s: "key")
    monkeypatch.setattr(
        ratings_warm, "lookup_ratings", lambda t, y, _m, **_k: (asks.setdefault(t, y), {})[1]
    )
    monkeypatch.setattr(ratings_warm, "TITLES_PER_RUN", 1000)
    as_user("viewer").get("/watch")

    _warm(library)

    assert asks["The Glasshouse"] == 2019


def test_a_title_no_card_named_a_year_of_is_not_asked(conn, asked):
    titles, _ = asked
    scored(conn, [{**_card(1), "year": None}, {**_card(2), "year": None}])
    scored(conn, [_card(2)])
    scored(conn, [{**_card(2), "year": None}])

    _warm(conn)

    assert titles == ["Title 2"]
