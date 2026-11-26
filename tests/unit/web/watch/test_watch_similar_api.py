"""TMDB's side of a search forgives a plural, and "similar to" finds a title and its like."""

# ruff: noqa: F811

from __future__ import annotations

import pytest

from tests.unit.web.watch.test_watch_catalog_api import (  # noqa: F401  (fixtures)
    _fresh_memo,
    arr,
    library,
    no_tmdb,
    tmdb,
    viewer,
)

FIANCE = {"id": 70, "media_type": "tv", "name": "90 Day Fiancé", "first_air_date": "2014-01-12"}


@pytest.fixture
def paths(tmdb):
    """TMDB answering by path and query: *answers* maps (path, query) to results."""
    answers: dict[tuple[str, str], list[dict]] = {}
    asked: list[tuple[str, str]] = []

    def get(path, params):
        key = (path, str(params.get("query", "")))
        asked.append(key)
        return {"results": answers.get(key, [])}

    tmdb._get = get
    tmdb.answers = answers
    tmdb.asked = asked
    return tmdb


def test_a_plural_tmdb_does_not_forgive_is_searched_for_again_without_it(viewer, paths, arr):
    paths.answers["/search/multi", "90 day fiance"] = [FIANCE]

    items = viewer.get("/api/watch/search/tmdb", params={"q": "90 days fiance"}).json()["items"]

    assert [i["title"] for i in items] == ["90 Day Fiancé"]


def test_tmdb_is_not_asked_twice_when_its_answer_holds_the_words(viewer, paths, arr):
    paths.answers["/search/multi", "90 days"] = [{**FIANCE, "name": "90 Days"}]

    viewer.get("/api/watch/search/tmdb", params={"q": "90 days"})

    assert paths.asked == [("/search/multi", "90 days")]


def test_similar_names_a_tmdb_title_and_gives_what_tmdb_finds_like_it(viewer, paths, arr):
    paths.answers["/search/multi", "90 day fiance"] = [FIANCE]
    paths.answers["/tv/70/recommendations", ""] = [
        {"id": 71, "name": "Love Is Blind", "media_type": "tv"},
        {"id": 70, "name": "90 Day Fiancé", "media_type": "tv"},
        {"id": 72, "name": "Married at First Sight"},
    ]

    body = viewer.get("/api/watch/similar", params={"q": "90 days fiance"}).json()

    assert (body["title"]["title"], body["title"]["ref"]) == ("90 Day Fiancé", "tmdb-tv-70")
    assert [(i["ref"], i["title"]) for i in body["items"]] == [
        ("tmdb-tv-71", "Love Is Blind"),
        ("tmdb-tv-72", "Married at First Sight"),
    ]


def test_similar_names_the_librarys_own_title_first(viewer, library, paths, arr):
    paths.answers["/movie/11/recommendations", ""] = [{"id": 12, "title": "Glass Onion"}]

    body = viewer.get("/api/watch/similar", params={"q": "the glasshouse"}).json()

    assert (body["title"]["ref"], body["title"]["kind"]) == ("movie-100", "movie")
    assert [i["ref"] for i in body["items"]] == ["tmdb-movie-12"]
    assert not any(path == "/search/multi" for path, _ in paths.asked)


def test_a_library_show_like_it_says_its_seasons(viewer, library, paths, arr):
    library.execute("UPDATE media_items SET tmdb_id = 55 WHERE show_rating_key = '500'")
    library.commit()
    paths.answers["/movie/11/recommendations", ""] = [
        {"id": 55, "media_type": "tv", "name": "Glass Coast"},
        {"id": 12, "title": "Glass Onion"},
    ]

    items = viewer.get("/api/watch/similar", params={"q": "the glasshouse"}).json()["items"]

    assert [(i["ref"], i["library_ref"], i.get("season_count")) for i in items] == [
        ("tmdb-tv-55", "show-500", 2),
        ("tmdb-movie-12", None, None),
    ]


def test_a_library_title_that_only_holds_the_words_gives_way_to_tmdbs_own(
    viewer, library, paths, arr
):
    paths.answers["/search/multi", "house"] = [{"id": 9, "media_type": "movie", "title": "House"}]

    body = viewer.get("/api/watch/similar", params={"q": "house"}).json()

    assert body["title"]["ref"] == "tmdb-movie-9"


def test_the_tmdb_title_closest_to_the_words_is_named_tmdbs_order_on_ties(viewer, paths, arr):
    paths.answers["/search/multi", "mike"] = [
        {"id": 1, "media_type": "movie", "title": "Magic Mike"},
        {"id": 2, "media_type": "movie", "title": "Mike"},
        {"id": 3, "media_type": "tv", "name": "Mike"},
    ]

    body = viewer.get("/api/watch/similar", params={"q": "mike"}).json()

    assert body["title"]["ref"] == "tmdb-movie-2"


def test_a_library_title_forgiven_the_words_is_named_where_tmdb_has_none(
    viewer, library, paths, arr
):
    body = viewer.get("/api/watch/similar", params={"q": "the glasshouses"}).json()

    assert body["title"]["ref"] == "movie-100"
    assert ("/search/multi", "the glasshouses") in paths.asked


def test_a_title_with_no_recommendations_gives_its_similar_ones(viewer, paths, arr):
    paths.answers["/search/multi", "orbit seven"] = [
        {"id": 5, "media_type": "movie", "title": "Orbit Seven"}
    ]
    paths.answers["/movie/5/similar", ""] = [{"id": 6, "title": "Orbit Eight"}]

    body = viewer.get("/api/watch/similar", params={"q": "orbit seven"}).json()

    assert [i["title"] for i in body["items"]] == ["Orbit Eight"]


def test_nothing_is_named_by_words_no_title_holds(viewer, paths, arr):
    paths.answers["/search/multi", "zzz nothing"] = [
        {"id": 5, "media_type": "movie", "title": "Orbit"}
    ]

    body = viewer.get("/api/watch/similar", params={"q": "zzz nothing"}).json()

    assert (body["title"], body["items"]) == (None, [])


def test_similar_without_tmdb_says_so(viewer, no_tmdb, arr):
    body = viewer.get("/api/watch/similar", params={"q": "anything"}).json()

    assert (body["tmdb_configured"], body["title"]) == (False, None)
