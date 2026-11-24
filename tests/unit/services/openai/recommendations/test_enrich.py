"""Tests for mediaman.services.openai.recommendations.enrich.

Mocking strategy: enrich_recommendations does all imports inside the function
body (``from mediaman.services.media_meta.tmdb import TmdbClient``, etc.), so we patch
at the *canonical* module path where each name lives rather than at the
enrich module's namespace.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from mediaman.db import init_db
from mediaman.services.openai.recommendations.enrich import enrich_recommendations

_SECRET_KEY = "x" * 64


@pytest.fixture
def conn(db_path):
    return init_db(str(db_path))


def _rec(title="Dune", media_type="movie") -> dict:
    return {
        "title": title,
        "year": None,
        "media_type": media_type,
        "category": "trending",
        "tmdb_id": None,
        "description": None,
        "rating": None,
        "poster_url": None,
        "trailer_url": "https://www.youtube.com",
    }


def _shape_card(title="Dune", year=2021, tmdb_id=438631) -> dict:
    return {
        "tmdb_id": tmdb_id,
        "year": year,
        "description": "An epic sci-fi film.",
        "rating": 7.9,
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
    }


def _shape_detail_empty() -> dict:
    return {
        "tagline": "",
        "runtime": None,
        "genres": "",
        "director": "",
        "cast_json": "",
        "trailer_key": "",
    }


def _mock_tmdb(search_return=None, card_return=None, detail_return=None):
    """Build a TmdbClient mock. Returned via ``from_db``."""
    cls = MagicMock()
    inst = MagicMock()
    if search_return is None:
        inst.search_results_or_raise.side_effect = requests.ConnectionError("TMDB is down")
    else:
        inst.search_results_or_raise.return_value = [search_return]
    inst.shape_card.return_value = card_return or _shape_card()
    inst.shape_detail.return_value = _shape_detail_empty()
    inst.details.return_value = detail_return or {}
    cls.from_db.return_value = inst
    return cls, inst


class TestEnrichRecommendations:
    def test_tmdb_data_applied_to_rec(self, conn):
        cls, _inst = _mock_tmdb(
            search_return={"id": 438631, "title": "Dune"}, card_return=_shape_card()
        )
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0]["tmdb_id"] == 438631
        assert recs[0]["year"] == 2021
        assert recs[0]["rating"] == 7.9
        assert recs[0]["poster_url"]

    def test_a_long_description_is_kept_whole(self, conn):
        card = _shape_card()
        card["description"] = "A sentence. " * 60
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0]["description"] == card["description"]

    def test_the_wide_backdrop_of_the_details_is_kept_with_the_pick(self, conn):
        cls, _inst = _mock_tmdb(
            search_return={"id": 1, "title": "Dune"},
            detail_return={"id": 1, "backdrop_path": "/wide.jpg"},
        )
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0]["backdrop_url"] == "https://image.tmdb.org/t/p/w1280/wide.jpg"

    def test_omdb_ratings_applied(self, conn):
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch(
                "mediaman.services.media_meta.omdb.fetch_ratings",
                return_value={"rt": "92%", "imdb": "8.0", "metascore": "75"},
            ),
        ):
            recs = [_rec()]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert recs[0].get("rt_rating") == "92%"
        assert recs[0].get("imdb_rating") == "8.0"
        assert recs[0].get("metascore") == "75"

    def test_season_title_stripped_for_omdb_lookup(self, conn):
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}) as fr,
        ):
            recs = [_rec(title="Show — Season 2", media_type="tv")]
            enrich_recommendations(recs, conn, _SECRET_KEY)

        assert fr.call_args.args[0] == "Show"

    def test_imdb_score_used_as_fallback_rating(self, conn):
        """When TMDB has no rating, fall back to the IMDb score from OMDb."""
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={"imdb": "7.5"}),
        ):
            rec = _rec()
            rec["rating"] = None
            enrich_recommendations([rec], conn, _SECRET_KEY)

        assert rec["rating"] == 7.5

    def test_rec_without_title_skipped(self, conn):
        cls, _inst = _mock_tmdb(search_return=None)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            rec = _rec()
            rec["title"] = ""
            enrich_recommendations([rec], conn, _SECRET_KEY)  # must not raise

    def test_trailer_url_updated_with_year(self, conn):
        card = _shape_card(year=2021)
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}),
        ):
            rec = _rec()
            enrich_recommendations([rec], conn, _SECRET_KEY)

        assert "2021" in rec["trailer_url"]

    def test_zero_tmdb_rating_not_overwritten_by_imdb_fallback(self, conn):
        """A TMDB rating of 0.0 must not be replaced by the IMDb fallback.

        ``not 0.0`` is True in Python; the old guard ``if not s.get("rating")``
        would fire and overwrite a valid zero-score with the IMDb value.
        The correct guard is ``if s.get("rating") is None``.
        """
        card = _shape_card()
        card["rating"] = 0.0  # TMDB has a valid zero score
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch(
                "mediaman.services.media_meta.omdb.fetch_ratings",
                return_value={"imdb": "7.5"},
            ),
        ):
            rec = _rec()
            enrich_recommendations([rec], conn, _SECRET_KEY)

        # Rating must remain 0.0, not be replaced by the IMDb fallback 7.5.
        assert rec["rating"] == 0.0

    def test_none_tmdb_rating_uses_imdb_fallback(self, conn):
        card = _shape_card()
        card["rating"] = None
        cls, _inst = _mock_tmdb(search_return={"id": 1, "title": "Dune"}, card_return=card)
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch(
                "mediaman.services.media_meta.omdb.fetch_ratings",
                return_value={"imdb": "6.2"},
            ),
        ):
            rec = _rec()
            enrich_recommendations([rec], conn, _SECRET_KEY)

        assert rec["rating"] == 6.2


class _Tmdb:
    """Answers a title search from *titles*: (title, year) -> id; a missing one is no match."""

    def __init__(self, titles, down=False):
        self.titles, self.down, self.asked = titles, down, []

    def search_results_or_raise(self, title, *, year=None, media_type="movie"):
        self.asked.append((title, year))
        if self.down:
            raise requests.ConnectionError("down")
        found = self.titles.get((title, year))
        if not found:
            return []
        if isinstance(found, int):
            found = {"id": found, "title": title}
        return found if isinstance(found, list) else [found]

    def shape_card(self, item):
        return {**_shape_card(tmdb_id=item["id"]), "year": 2000}

    def details(self, media_type, tmdb_id):
        return None


@pytest.fixture
def enrich(conn, monkeypatch):
    def run(tmdb, recs, **kwargs):
        monkeypatch.setattr(
            "mediaman.services.media_meta.tmdb.TmdbClient.from_db", lambda *_a: tmdb
        )
        monkeypatch.setattr("mediaman.services.media_meta.omdb.fetch_ratings", lambda *_a, **_k: {})
        enrich_recommendations(recs, conn, _SECRET_KEY, **kwargs)
        return [(r["title"], r["tmdb_id"]) for r in recs]

    return run


class TestWhichPicksAreKept:
    def test_a_title_tmdb_does_not_know_is_dropped(self, enrich):
        tmdb = _Tmdb({("Real", None): 5})
        assert enrich(tmdb, [_rec("Made up"), _rec("Real")]) == [("Real", 5)]

    def test_an_outage_of_tmdb_drops_nothing_and_is_asked_about_twice_at_most(self, enrich):
        tmdb = _Tmdb({}, down=True)
        titles = ["A", "B", "C", "D"]
        assert enrich(tmdb, [_rec(t) for t in titles]) == [(t, None) for t in titles]
        assert [t for t, _ in tmdb.asked] == ["A", "B"]

    def test_details_tmdb_does_not_give_count_as_failures_too(self, enrich):
        tmdb = _Tmdb({(t, None): n for n, t in enumerate("ABCD", start=1)})
        asked = []
        tmdb.details = lambda _type, tmdb_id: asked.append(tmdb_id)
        enrich(tmdb, [_rec(t) for t in "ABCD"])
        assert asked == [1, 2]

    def test_when_the_time_is_up_the_rest_stays_as_it_is(self, enrich):
        tmdb = _Tmdb({(t, None): n for n, t in enumerate("ABC", start=1)})
        now = [0.0]
        real = tmdb.search_results_or_raise

        def slow(*args, **kwargs):
            now[0] += 40
            return real(*args, **kwargs)

        tmdb.search_results_or_raise = slow
        kept = enrich(tmdb, [_rec(t) for t in "ABC"], seconds=60, clock=lambda: now[0])
        assert kept == [("A", 1), ("B", 2), ("C", None)]

    def test_no_rating_is_asked_for_when_the_time_is_up(self, conn):
        cls, _inst = _mock_tmdb()
        with (
            patch("mediaman.services.media_meta.tmdb.TmdbClient", cls),
            patch("mediaman.services.media_meta.omdb.fetch_ratings", return_value={}) as ratings,
        ):
            enrich_recommendations([_rec()], conn, _SECRET_KEY, seconds=0)
        assert ratings.call_count == 0

    @pytest.mark.parametrize(
        "found",
        [
            {"title": "Amélie"},
            {"title": "The Fabulous Destiny", "original_title": "AMELIE"},
            {"name": "Amelie (2001)"},
        ],
    )
    def test_the_title_found_may_be_spelled_another_way(self, enrich, found):
        tmdb = _Tmdb({("Amelie", None): {"id": 4, **found}})
        assert enrich(tmdb, [_rec("Amelie")]) == [("Amelie", 4)]

    def test_ampersand_and_and_are_one_word(self, enrich):
        tmdb = _Tmdb({("Fire and Ash", None): {"id": 4, "title": "Fire & Ash"}})
        assert enrich(tmdb, [_rec("Fire and Ash")]) == [("Fire and Ash", 4)]

    @pytest.mark.parametrize(
        ("title", "found"),
        [
            ("Spiderman", {"title": "Spider-Man"}),
            ("Spider-Man", {"title": "Spiderman"}),
            ("Spider Man", {"title": "Spider-Man"}),
            ("Oceans Eleven", {"title": "Ocean's Eleven"}),
            ("Ocean's Eleven", {"title": "Ocean\u2019s Eleven"}),
            ("Birdman", {"title": "Birdman or (The Unexpected Virtue of Ignorance)"}),
            ("Star Wars", {"title": "Star Wars: Episode IV - A New Hope"}),
            ("Borat", {"title": "Borat (Cultural Learnings)"}),
            ("Dr Strange", {"title": "Dr. Strange, or Learning"}),
            ("Andor", {"name": "Andor \u2013 A Story"}),
        ],
    )
    def test_a_real_title_is_found_by_its_shorter_or_other_spelling(self, enrich, title, found):
        kind = "release_date" if "title" in found else "first_air_date"
        tmdb = _Tmdb({(title, 2001): {"id": 4, kind: "2001-05-01", **found}})
        assert enrich(tmdb, [{**_rec(title), "year": 2001}]) == [(title, 4)]

    @pytest.mark.parametrize(
        ("title", "year", "found"),
        [
            ("Alien", 1979, {"title": "Alien: Romulus", "release_date": "2024-08-13"}),
            ("Alien", None, {"title": "Alien: Romulus", "release_date": "2024-08-13"}),
            ("Spider", 2002, {"title": "Spider-Man", "release_date": "2002-05-01"}),
            ("Star", 1977, {"title": "Star Wars", "release_date": "1977-05-25"}),
            ("Star Wars: Rogue One", 2016, {"title": "Star", "release_date": "2016-01-01"}),
            ("Or", 2004, {"title": "Life or Death", "release_date": "2004-01-01"}),
            ("Birdman", 2014, {"title": "Birdman or (The)", "release_date": ""}),
            (
                "Borat",
                2006,
                {"title": "Borat or Cultural Learnings of America", "release_date": "2006-11-01"},
            ),
            ("Therapist", 2015, {"title": "The Rapist", "release_date": "2015-01-01"}),
            ("Nowhere", 2022, {"title": "Now Here", "release_date": "2022-01-01"}),
            ("Recycle", 2006, {"title": "Re-Cycle", "release_date": "1990-01-01"}),
            ("(2012)", 2009, {"title": "(500) Days", "release_date": "2009-07-17"}),
            ("(2012)", 2009, {"title": ": Days", "release_date": "2009-07-17"}),
            ("Life", 2002, {"title": "Life or Death", "release_date": "2002-01-01"}),
            ("Dead", 2006, {"title": "Dead or Alive Again", "release_date": "2006-01-01"}),
            ("Barbie: The Sequel", 2023, {"title": "Barbie", "release_date": "2023-07-01"}),
            ("Nowhere", 2022, {"title": "Now Here", "release_date": "1990-01-01"}),
            ("Nowhere", None, {"title": "Now Here", "release_date": "1990-01-01"}),
        ],
    )
    def test_a_film_that_only_begins_alike_is_refused(self, enrich, title, year, found):
        tmdb = _Tmdb({(title, year): {"id": 4, **found}, (title, None): {"id": 4, **found}})
        assert enrich(tmdb, [{**_rec(title), "year": year}]) == []

    def test_the_exact_title_wins_over_one_with_a_subtitle_before_it(self, enrich):
        results = [
            {"id": 1, "title": "Heat: The Making", "release_date": "1995-01-01"},
            {"id": 2, "title": "Heat", "release_date": "1995-12-15"},
        ]
        assert enrich(_Tmdb({("Heat", 1995): results}), [{**_rec("Heat"), "year": 1995}]) == [
            ("Heat", 2)
        ]

    @pytest.mark.parametrize(
        ("title", "year", "longer"),
        [
            ("Mad Max", 2015, "Mad Max: Fury Road"),
            ("Avatar", 2022, "Avatar: The Way of Water"),
            ("Star Trek", 2017, "Star Trek: Discovery"),
        ],
    )
    def test_an_exact_title_of_another_year_wins_over_a_subtitled_one_of_the_year(
        self, enrich, title, year, longer
    ):
        tmdb = _Tmdb(
            {
                (title, year): {"id": 1, "title": longer, "release_date": f"{year}-01-01"},
                (title, None): {"id": 2, "title": title, "release_date": "1979-01-01"},
            }
        )
        assert enrich(tmdb, [{**_rec(title), "year": year}]) == [(title, 2)]

    def test_a_subtitled_title_is_taken_when_neither_search_has_the_exact_one(self, enrich):
        found = {"id": 1, "name": "Monster: The Dahmer Story", "first_air_date": "2022-09-01"}
        other = {"id": 2, "name": "Monsters", "first_air_date": "2022-01-01"}
        tmdb = _Tmdb({("Monster", 2022): [other, found], ("Monster", None): [{**found}, other]})
        assert enrich(tmdb, [{**_rec("Monster", "tv"), "year": 2022}]) == [("Monster", 1)]

    def test_more_than_one_subtitled_title_of_the_year_is_refused(self, enrich):
        first = {"id": 1, "title": "Fear Street: 1994", "release_date": "2021-07-01"}
        second = {"id": 2, "title": "Fear Street: 1978", "release_date": "2021-07-08"}
        key = "Fear Street"
        for answers in (
            {(key, 2021): [first, second]},
            {(key, 2021): [first], (key, None): [second]},
        ):
            assert enrich(_Tmdb(answers), [{**_rec(key), "year": 2021}]) == []

    def test_of_exact_titles_the_one_of_the_year_then_of_the_same_hyphens_is_taken(self, enrich):
        old = {"id": 1, "title": "Halloween", "release_date": "1978-10-25"}
        new = {"id": 2, "title": "Halloween", "release_date": "2018-10-18"}
        recs = [{**_rec("Halloween"), "year": 2018}]
        assert enrich(_Tmdb({("Halloween", 2018): [old, new]}), recs) == [("Halloween", 2)]

        hyphen = {"id": 3, "title": "Re-Cycle", "release_date": "2022-01-01"}
        whole = {"id": 4, "title": "Recycle", "release_date": "2022-01-01"}
        recs = [{**_rec("Recycle"), "year": 2022}]
        assert enrich(_Tmdb({("Recycle", 2022): [hyphen, whole]}), recs) == [("Recycle", 4)]

    def test_the_exact_title_is_found_however_far_down_the_results(self, enrich):
        others = [{"id": n, "title": f"Other {n}"} for n in range(1, 20)]
        results = [*others, {"id": 99, "title": "Heat"}]
        assert enrich(_Tmdb({("Heat", None): results}), [_rec("Heat")]) == [("Heat", 99)]

    def test_an_apostrophe_is_no_space_so_the_year_need_not_fit(self, enrich):
        found = {"id": 4, "title": "Ocean's Eleven", "release_date": "2001-12-07"}
        assert enrich(_Tmdb({("Oceans Eleven", None): found}), [_rec("Oceans Eleven")]) == [
            ("Oceans Eleven", 4)
        ]

    def test_a_title_of_signs_alone_is_not_taken_for_another(self, enrich):
        assert enrich(_Tmdb({("???", None): {"id": 4, "title": "!!!"}}), [_rec("???")]) == []

    def test_another_title_found_by_the_search_is_not_taken_for_the_pick(self, enrich):
        tmdb = _Tmdb({("Made up", None): {"id": 4, "title": "Made", "original_title": "Up"}})
        assert enrich(tmdb, [_rec("Made up")]) == []

    def test_another_title_of_that_year_leaves_the_search_without_it(self, enrich):
        tmdb = _Tmdb({("Heat", 1990): {"id": 9, "title": "Heatwave"}, ("Heat", None): 3})
        assert enrich(tmdb, [{**_rec("Heat"), "year": 1990}]) == [("Heat", 3)]

    def test_the_year_picks_the_remake_and_a_wrong_year_still_finds_the_title(self, enrich):
        tmdb = _Tmdb({("Dune", 1984): 1, ("Dune", None): 2, ("Heat", None): 3})
        recs = [{**_rec("Dune"), "year": 1984}, {**_rec("Heat"), "year": 1990}]
        assert enrich(tmdb, recs) == [("Dune", 1), ("Heat", 3)]

    def test_a_refused_pick_makes_room_for_the_next(self, enrich):
        tmdb = _Tmdb({(t, None): n for n, t in enumerate("ABCD", start=1)})
        kept = enrich(tmdb, [_rec(t) for t in "ABCD"], accept=lambda r: r["tmdb_id"] != 1, limit=2)
        assert kept == [("B", 2), ("C", 3)]
        assert ("D", None) not in tmdb.asked
