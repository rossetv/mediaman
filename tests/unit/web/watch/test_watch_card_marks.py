"""Each reply that feeds cards says what this person rated and marked, and nothing of others."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.core.time import now_utc
from mediaman.services.watch_catalog import LibraryIndex
from mediaman.web.auth.session_store import create_session
from mediaman.web.repository import asked, ratings, seen_titles
from mediaman.web.repository.watch_progress import (
    MarkTarget,
    mark_watched,
    save_progress,
    unmark_watched,
)
from mediaman.web.routes.watch import browse_api, catalog_api, library_row_api, pages
from tests.helpers.factories import insert_admin_user, insert_scheduled_action
from tests.unit.web.watch.test_watch_catalog_api import (
    _fresh_memo,
    arr,
    library,
    plex,
    tmdb,
)

__all__ = ["_fresh_memo", "arr", "library", "plex", "tmdb"]

NEW = {"tmdb_ref": "tmdb-movie-12", "level": None, "watched": False}


@pytest.fixture
def as_user(app_factory, library, tmdb, arr, plex, templates_stub):
    library.execute("UPDATE media_items SET tmdb_id = 44 WHERE show_rating_key = '500'")
    for key, episodes in (("501", ["511", "512"]), ("502", ["521"])):
        listed = [{"key": k, "number": n, "path": k} for n, k in enumerate(episodes, 1)]
        library.execute(
            "UPDATE media_items SET episode_count = ?, episode_keys = ? WHERE id = ?",
            (len(episodes), json.dumps(listed), key),
        )
    library.commit()
    for name in ("viewer", "cian"):
        insert_admin_user(library, username=name, role="viewer")
    app = app_factory(
        browse_api.router,
        catalog_api.router,
        library_row_api.router,
        pages.router,
        conn=library,
        state_extras={"templates": templates_stub},
    )

    def _client(username):
        client = TestClient(app)
        client.cookies.set("session_token", create_session(library, username))
        return client

    return _client


def _mark(conn, media_type, tmdb_id, through=None, name="viewer"):
    title = ratings.RatedTitle(media_type, tmdb_id)
    user_id = ratings.find_user_id(conn, name)
    seen_titles.mark(conn, user_id, title, through, now_utc(), in_library=False)


def _rate(conn, media_type, tmdb_id, level, name="viewer"):
    title = ratings.RatedTitle(media_type, tmdb_id)
    ratings.set_level(conn, ratings.find_user_id(conn, name), title, level, now_utc())


def _own(card):
    return {name: card[name] for name in NEW}


def _marks(title):
    return title["tmdb_ref"], title["watched"]


def _by_title(items):
    return {i["title"]: _own(i) for i in items}


@pytest.fixture
def marked(as_user, library):
    """The library's film and show and a title outside it, marked and rated; cian says no."""
    _mark(library, "movie", 11)
    _mark(library, "movie", 12)
    _mark(library, "tv", 44, 2)
    _rate(library, "movie", 11, 2)
    _rate(library, "tv", 44, 1)
    for media_type, tmdb_id in (("movie", 11), ("movie", 12), ("tv", 44)):
        _rate(library, media_type, tmdb_id, -2, "cian")
    return as_user("viewer")


FILM = {"tmdb_ref": "tmdb-movie-11", "level": 2, "watched": True}
SHOW = {"tmdb_ref": "tmdb-tv-44", "level": 1, "watched": True}


def test_the_library_says_it_on_its_rows_and_in_its_search(marked):
    rows = {r["title"]: _by_title(r["items"]) for r in marked.get("/watch").json()["rows"]}
    assert rows["Movies"] == {"The Glasshouse": FILM}
    assert rows["TV"] == {"Glass Coast": SHOW}

    for kind, want in (("movies", FILM), ("tv", SHOW)):
        [item] = marked.get(f"/api/watch/library/{kind}/row").json()["items"]
        assert _own(item) == want
    found = marked.get("/api/watch/search", params={"q": "glass"}).json()["items"]
    assert _by_title(found) == {"The Glasshouse": FILM, "Glass Coast": SHOW}


def test_nothing_of_one_person_is_on_anothers_cards(as_user, library):
    _mark(library, "movie", 11, name="cian")
    _mark(library, "tv", 44, 2, name="cian")
    _rate(library, "movie", 11, 2, name="cian")
    client = as_user("viewer")

    for path in ("/api/watch/search?q=glass", "/api/watch/library/recent/row"):
        for item in client.get(path).json()["items"]:
            assert (item["level"], item["watched"]) == (None, False)
    for row in client.get("/api/watch/discover").json()["rows"]:
        for item in row["items"]:
            assert (item["level"], item["watched"]) == (None, False)


def test_titles_outside_the_library_say_it(marked, tmdb):
    mine = {**NEW, "watched": True}
    for row in marked.get("/api/watch/discover").json()["rows"][:2]:
        assert _by_title(row["items"])["Brand New"] == mine
    found = marked.get("/api/watch/search/tmdb", params={"q": "glass"}).json()["items"]
    assert _by_title(found) == {"Glass Onion": mine}
    title = marked.get("/api/watch/title/tmdb-movie-12").json()
    assert (_marks(title), title["mark_key"]) == (("tmdb-movie-12", True), None)


def test_a_further_page_of_a_row_says_it(marked, tmdb, monkeypatch):
    found = {"results": [{"id": 12, "title": "Brand New"}], "total_pages": 9}
    monkeypatch.setattr(tmdb, "_get", lambda *_a: found)
    row = {"row": "popular_movies", "page": 2}
    items = marked.get("/api/watch/discover", params=row).json()["items"]
    assert _by_title(items)["Brand New"] == {**NEW, "watched": True}


def test_a_show_outside_the_library_reads_as_unwatched_once_a_season_is_out(as_user, library, tmdb):
    client = as_user("viewer")
    _mark(library, "tv", 77, 2)
    assert client.get("/api/watch/title/tmdb-tv-77").json()["watched"] is True

    tmdb.detail[77]["seasons"].append({"season_number": 3, "episode_count": 1})

    assert client.get("/api/watch/title/tmdb-tv-77").json()["watched"] is False


def test_a_title_the_library_holds_says_it_and_so_do_its_episodes(marked):
    assert _marks(marked.get("/api/watch/title/movie-100").json()) == ("tmdb-movie-11", True)
    show = marked.get("/api/watch/title/show-500").json()
    assert _marks(show) == ("tmdb-tv-44", True)
    assert [s["watched"] for s in show["seasons"]] == [True, True]
    assert all(e["watched"] for e in show["episodes"])
    season = marked.get("/api/watch/title/show-500/season/2").json()
    assert (season["watched"], [e["watched"] for e in season["episodes"]]) == (True, [True])


def test_the_pages_without_scripts_say_it(marked):
    assert marked.get("/watch/movie/100").json()["movie"]["watched"] is True
    assert marked.get("/watch/show/500").json()["show"]["watched"] is True


def test_a_show_half_watched_stays_in_continue_watching_under_its_mark(marked, library):
    save_progress(
        library,
        username="viewer",
        rating_key="511",
        media_item_id="501",
        show_rating_key="500",
        position_s=600,
        duration_s=3000,
        played_s=600,
        now=now_utc(),
    )

    page = marked.get("/watch").json()

    [card] = next(r["items"] for r in page["rows"] if r["id"] == "continue")
    assert (card["mark_key"], card["tmdb_ref"], card["level"]) == ("511", "tmdb-tv-44", 1)
    assert page["banner"]["mark_key"] == "511"


def test_what_is_leaving_says_it(marked, library):
    for key in ("100", "501"):
        insert_scheduled_action(
            library,
            media_item_id=key,
            action="scheduled_deletion",
            token=f"tok-{key}",
            execute_at=(now_utc() + timedelta(days=3)).isoformat(),
            delete_status="pending",
        )
    library.commit()

    rows = marked.get("/watch").json()["rows"]

    leaving = next(r["items"] for r in rows if r["id"] == "leaving")
    assert sorted((_own(c) for c in leaving), key=lambda c: c["tmdb_ref"]) == [FILM, SHOW]


@pytest.mark.parametrize("path", ["/watch", "/api/watch/library/recent/row"])
def test_a_persons_marks_are_read_once_for_all_the_rows(marked, monkeypatch, path):
    read = []
    real = seen_titles.person_marks
    monkeypatch.setattr(seen_titles, "person_marks", lambda *a: read.append(1) or real(*a))
    marked.get(path)
    assert len(read) == 1


def test_a_film_unmarked_by_an_older_version_reads_so_everywhere(as_user, library):
    title = ratings.RatedTitle("movie", 11)
    user_id = ratings.find_user_id(library, "viewer")
    mark_watched(library, "viewer", [MarkTarget("100", "100", None, 0)], now_utc())
    seen_titles.mark(library, user_id, title, None, now_utc(), in_library=True)
    unmark_watched(library, "viewer", "movie", "100")
    client = as_user("viewer")

    [item] = client.get("/api/watch/library/movies/row").json()["items"]
    assert item["watched"] is False
    assert client.get("/api/watch/title/movie-100").json()["watched"] is False
    assert client.get("/watch/movie/100").json()["movie"]["watched"] is False
    assert client.get("/api/watch/title/tmdb-movie-11").json()["watched"] is False


def test_a_show_outside_the_library_with_a_part_taken_back_is_not_watched(as_user, library):
    _mark(library, "tv", 77, 2)
    part = seen_titles.ShowPart("900", 1, 2, {1: frozenset({1, 2})})
    user_id = ratings.find_user_id(library, "viewer")
    seen_titles.change_part(library, user_id, ("tv", 77), part, seen=False)

    assert as_user("viewer").get("/api/watch/title/tmdb-tv-77").json()["watched"] is False


def test_an_episode_played_in_plex_is_not_counted_twice_under_a_mark(marked, library):
    library.execute(
        "INSERT INTO plex_links (username, plex_account_id, server_account_id, plex_name,"
        " server_id, linked_at) VALUES ('viewer', 1, 1, 'v', 's', '2026')"
    )
    library.execute(
        "INSERT INTO plex_plays (username, rating_key, show_rating_key, media_type, title,"
        " viewed_at, history_key) VALUES ('viewer', '511', '500', 'tv', 'E1', '2026', 'h')"
    )
    library.commit()
    save_progress(
        library,
        username="viewer",
        rating_key="512",
        media_item_id="501",
        show_rating_key="500",
        position_s=600,
        duration_s=3000,
        played_s=600,
        now=now_utc(),
    )

    held = asked.held_library(library, "viewer", ["1"], LibraryIndex())

    assert held.is_watched("show-500", 1) is False
