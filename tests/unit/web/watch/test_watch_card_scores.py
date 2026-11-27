"""Cards carry the Rotten Tomatoes score and IMDb rating the ratings cache holds of their title,
and draw them; a page or a row never asks OMDb for them."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import jinja2
import pytest

from mediaman.services.media_meta import omdb
from mediaman.web.routes.watch import catalog_api
from tests.helpers.factories import insert_suggestion
from tests.unit.web.watch.test_watch_card_marks import (
    _fresh_memo,
    arr,
    as_user,
    library,
    plex,
    tmdb,
)
from tests.unit.web.watch.test_watch_recommended_api import viewer as pick_viewer

__all__ = ["_fresh_memo", "arr", "as_user", "library", "pick_viewer", "plex", "tmdb"]

TEMPLATES = Path(__file__).resolve().parents[4] / "src/mediaman/web/templates"


@pytest.fixture
def client(as_user, library, monkeypatch):
    """The library film (TMDB 11) scored 85% and 7.8, its show (44) 59% alone, Brand New (12)
    held with OMDb's "N/A"; OMDb itself refuses to be asked."""
    library.executemany(
        "INSERT INTO ratings_cache (tmdb_id, media_type, imdb_rating, rt_rating, metascore,"
        " fetched_at) VALUES (?, ?, ?, ?, NULL, '2026-01-01T00:00:00+00:00')",
        [(11, "movie", "7.8", "85%"), (44, "tv", None, "59%"), (12, "movie", "N/A", "N/A")],
    )
    library.commit()

    def asked(*_a, **_k):
        raise AssertionError("OMDb was asked while cards were made")

    monkeypatch.setattr(omdb, "lookup_ratings", asked)
    monkeypatch.setattr(omdb, "fetch_ratings", asked)
    monkeypatch.setattr(catalog_api, "lookup_ratings", asked)
    return as_user("viewer")


def _scores(items):
    return {i["title"]: (i.get("rt"), i.get("imdb")) for i in items}


def test_library_rows_carry_what_the_cache_holds(client):
    rows = {r["title"]: _scores(r["items"]) for r in client.get("/watch").json()["rows"]}
    assert rows["Movies"] == {"The Glasshouse": (85, "7.8")}
    assert rows["TV"] == {"Glass Coast": (59, None)}
    [item] = client.get("/api/watch/library/movies/row").json()["items"]
    assert (item.get("rt"), item.get("imdb")) == (85, "7.8")


def test_discover_and_search_cards_carry_it_and_nothing_for_a_score_omdb_did_not_give(client):
    for row in client.get("/api/watch/discover").json()["rows"][:2]:
        assert _scores(row["items"])["Brand New"] == (None, None)
    found = client.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"]
    assert _scores(found)["Glass Onion"] == (None, None)
    held = client.get("/api/watch/search", params={"q": "glass"}).json()["items"]
    assert _scores(held) == {"The Glasshouse": (85, "7.8"), "Glass Coast": (59, None)}


def test_a_pick_carries_its_own_scores_where_the_cache_holds_none(pick_viewer, conn):
    conn.execute(
        "INSERT INTO ratings_cache (tmdb_id, media_type, imdb_rating, rt_rating, metascore,"
        " fetched_at) VALUES (2, 'movie', NULL, '91%', NULL, '2026-01-01T00:00:00+00:00')"
    )
    for tmdb_id, title in ((1, "Own"), (2, "Cached")):
        insert_suggestion(
            conn,
            title=title,
            tmdb_id=tmdb_id,
            rt_rating="40%",
            imdb_rating="6.4",
            batch_id="2099-01-02",
        )
    conn.commit()

    items = pick_viewer.get("/api/watch/recommended").json()["items"]

    assert _scores(items) == {"Own": (40, "6.4"), "Cached": (91, "6.4")}


class _Scores(HTMLParser):
    """The classes and words of a card's scores, in order."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.found: list[list[str]] = []
        self._depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if "watch-card__scores" in cls.split():
            self._depth = 1
        elif self._depth:
            self._depth += 1
            if "watch-score" in cls.split():
                self.found.append([cls, ""])

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth and self.found:
            self.found[-1][1] += data


def _card(**scores: object) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True)
    item = {
        "title": "Brightwater",
        "subtitle": "Movie",
        "href": "/watch/movie/1",
        "leaving_days": None,
    }
    item |= scores
    return str(env.get_template("_watch_card.html").module.card(item))


@pytest.mark.parametrize(
    ("scores", "want"),
    [
        (
            {"rt": 60, "imdb": "7.8"},
            [
                ["watch-score watch-score--rt is-fresh", "Rotten Tomatoes 60%"],
                ["watch-score", "IMDb 7.8"],
            ],
        ),
        ({"rt": 59}, [["watch-score watch-score--rt is-rotten", "Rotten Tomatoes 59%"]]),
        ({"imdb": "6.1"}, [["watch-score", "IMDb 6.1"]]),
    ],
)
def test_a_card_draws_its_scores_fresh_from_sixty(scores, want):
    assert _Scores(_card(**scores)).found == want


def test_a_card_with_no_score_draws_no_place_for_one():
    assert "watch-card__scores" not in _card(rt=None, imdb=None)
