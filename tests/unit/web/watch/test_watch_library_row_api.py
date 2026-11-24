"""The paged library rows: the page's own order and cards, for the person asking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched, unmark_watched
from mediaman.web.routes.watch import library_order, library_row_api, pages
from tests.helpers.factories import insert_admin_user, insert_media_item, insert_settings

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
_TYPES = {"tv": "tv_season", "anime": "anime_season"}


def _movie(conn, key, title, added_at, library=1):
    insert_media_item(
        conn, id=key, plex_rating_key=key, title=title, added_at=added_at, plex_library_id=library
    )


def _season(conn, key, show, title, kind, library=1):
    insert_media_item(
        conn,
        id=key,
        plex_rating_key=key,
        title=title,
        show_title=title,
        media_type=_TYPES[kind],
        season_number=1,
        show_rating_key=show,
        plex_library_id=library,
    )


@pytest.fixture
def client(app_factory, conn, templates_stub):
    insert_settings(conn, plex_libraries='["1"]')
    for n in range(5):
        _movie(conn, f"10{n}", f"Film {n}", NOW - timedelta(days=n // 2))
    _movie(conn, "109", "Undated", "")
    _movie(conn, "900", "Hidden Film", NOW, library=9)
    _season(conn, "501", "500", "Coast", "tv")
    _season(conn, "601", "600", "Lanterns", "anime")
    _season(conn, "701", "700", "Hidden Show", "tv", library=9)
    for username in ("aoife", "cian"):
        insert_admin_user(conn, username=username, role="viewer")
    app = app_factory(
        pages.router,
        library_row_api.router,
        conn=conn,
        state_extras={"templates": templates_stub},
    )
    test_client = TestClient(app)
    test_client.cookies.set("session_token", create_session(conn, "aoife"))
    return test_client


def _page(client, kind, **params):
    resp = client.get(f"/api/watch/library/{kind}/row", params=params)
    assert resp.status_code == 200
    return resp.json()


def _all(client, kind, limit, **params):
    items, after = [], ""
    while after is not None:
        page = _page(client, kind, limit=limit, after=after, **params)
        items, after = items + page["items"], page["next"]
    return items


@pytest.mark.parametrize(
    ("kind", "title"), [("movies", "Movies"), ("tv", "TV"), ("anime", "Anime")]
)
def test_the_pages_of_a_row_are_the_watch_pages_row(client, kind, title):
    rows = {r["title"]: r["items"] for r in client.get("/watch").json()["rows"]}
    assert _all(client, kind, 2) == rows[title]
    assert _page(client, kind)["total"] == len(rows[title])


def test_the_watch_pages_cursor_goes_on_where_its_first_page_ends(client, monkeypatch):
    monkeypatch.setattr(pages, "ROW_PAGE_SIZE", 2)
    row = next(r for r in client.get("/watch").json()["rows"] if r["title"] == "Movies")
    assert _page(client, "movies", after=row["next"])["items"] == row["items"][2:]


def test_a_row_ends_at_its_last_title_with_no_empty_page_after(client):
    assert _page(client, "movies", limit=6)["next"] is None
    assert _page(client, "tv", limit=1)["next"] is None


_TITLES = ["The Amber Garden", "Amber Island", "Éclair", "eden", "An Apple", "The", "Zed"]


@pytest.fixture
def titled(client, conn):
    conn.execute("DELETE FROM media_items WHERE media_type = 'movie'")
    for n, title in enumerate(_TITLES):
        _movie(conn, f"20{n}", title, NOW - timedelta(days=n))
    conn.execute("UPDATE media_items SET file_path = '/m/film.mkv'")
    conn.commit()
    return client


def test_a_to_z_passes_over_an_article_accents_and_case(titled):
    want = ["The Amber Garden", "Amber Island", "An Apple", "Éclair", "eden", "The", "Zed"]
    assert [i["title"] for i in _page(titled, "movies", sort="az")["items"]] == want
    paged, after = [], ""
    while after is not None:
        page = _page(titled, "movies", sort="az", limit=2, after=after)
        paged, after = paged + page["items"], page["next"]
    assert [i["title"] for i in paged] == want


def test_the_shared_link_of_a_row_draws_its_first_page_in_the_order_it_asks_for(
    titled, monkeypatch
):
    monkeypatch.setattr(pages, "plex_is_configured", lambda _conn: True)
    monkeypatch.setattr(pages, "GRID_PAGE_SIZE", 3)
    grid = titled.get("/watch?all=movies&sort=az").json()["grid"]
    assert [i["title"] for i in grid["items"]] == ["The Amber Garden", "Amber Island", "An Apple"]
    assert (grid["sort"], grid["total"]) == ("az", 7)
    rest = _page(titled, "movies", sort="az", after=grid["next"])
    assert [i["title"] for i in rest["items"]] == ["Éclair", "eden", "The", "Zed"]

    newest = titled.get("/watch?all=movies&sort=size").json()["grid"]
    assert [i["title"] for i in newest["items"]] == _TITLES[:3]
    assert _page(titled, "movies", after=newest["next"])["items"][0]["title"] == "eden"


def test_titles_coming_and_going_while_paging_repeat_and_skip_nothing(client, conn):
    first = _page(client, "movies", limit=3)
    _movie(conn, "200", "Just Added", NOW + timedelta(hours=1))
    last_shown = first["items"][-1]["href"].rsplit("/", 1)[1]
    conn.execute("DELETE FROM media_items WHERE id = ?", (last_shown,))
    conn.commit()

    second = _page(client, "movies", limit=10, after=first["next"])

    assert [i["title"] for i in first["items"] + second["items"]] == [
        "Film 0",
        "Film 1",
        "Film 2",
        "Film 3",
        "Film 4",
        "Undated",
    ]


def test_marks_are_the_callers_own(client, conn):
    mark_watched(conn, "cian", [MarkTarget("100", "100", None, 0)], NOW)
    assert [i["watched"] for i in _page(client, "movies")["items"]] == [False] * 6


@pytest.mark.parametrize("after", ["nonsense", "WzEsMl0", "é", "W10", "e30", "NQ", "ImFiYyI"])
def test_a_malformed_cursor_is_refused(client, after):
    resp = client.get("/api/watch/library/movies/row", params={"after": after})
    assert (resp.status_code, resp.json()["error"]) == (400, "invalid_request")


def test_other_rows_and_absurd_limits_are_refused(client):
    assert client.get("/api/watch/library/films/row").status_code == 404
    for limit in ("0", "61", "x"):
        assert client.get(f"/api/watch/library/tv/row?limit={limit}").status_code == 422
    assert client.get("/api/watch/library/tv/row?sort=size").status_code == 422


def test_what_one_person_has_seen_of_a_show_says_nothing_to_another(client, conn):
    mark_watched(conn, "aoife", [MarkTarget("511", "501", "500", 0)], NOW)
    conn.execute("UPDATE media_items SET episode_count = 2 WHERE id = '501'")
    conn.commit()

    def tv():
        rows = {r["title"]: r["items"] for r in client.get("/watch").json()["rows"]}
        return [c["subtitle"] for c in rows["TV"]]

    mine = tv()
    client.cookies.set("session_token", create_session(conn, "cian"))

    assert (mine, tv()) == (["TV · 1 season · 1 left"], ["TV · 1 season"])


def _year(conn, key, year):
    conn.execute("UPDATE media_items SET year = ? WHERE id = ?", (year, key))
    conn.commit()


def _seen(conn, *keys, user="aoife"):
    mark_watched(conn, user, [MarkTarget(k, k, None, 0.0) for k in keys], NOW)


def _unseen(conn, key, user="aoife"):
    unmark_watched(conn, user, "movie", key)


def _names(items):
    return [i["title"] for i in items]


@pytest.mark.parametrize("limit", [1, 2, 60])
def test_newest_release_goes_by_year_then_a_to_z_and_a_title_of_no_year_comes_last(
    titled, conn, limit
):
    for key, year in (("200", 1983), ("201", 1983), ("202", 2024), ("205", None), ("206", 1977)):
        _year(conn, key, year)
    # Scanned before years were stored: the folder has it.
    conn.execute("UPDATE media_items SET file_path = '/m/eden (1999)/e.mkv' WHERE id = '203'")
    conn.commit()
    assert _names(_all(titled, "movies", limit, sort="year")) == [
        "Éclair",
        "eden",
        "The Amber Garden",
        "Amber Island",
        "Zed",
        "An Apple",
        "The",
    ]


def test_newest_release_of_a_show_is_the_year_it_began(client, conn):
    for key, show, title, year in (
        ("502", "500", "Coast", 2005),
        ("511", "510", "Moor", 2019),
        ("521", "520", "Dale", 1990),
    ):
        _season(conn, key, show, title, "tv")
        _year(conn, key, year)
    assert _names(_all(client, "tv", 1, sort="year")) == ["Moor", "Coast", "Dale"]


@pytest.mark.parametrize("limit", [1, 2, 60])
def test_not_watched_first_is_a_to_z_then_the_watched_ones(titled, conn, limit):
    _seen(conn, "202", "200")
    assert _names(_all(titled, "movies", limit, sort="unwatched")) == [
        "Amber Island",
        "An Apple",
        "eden",
        "The",
        "Zed",
        "The Amber Garden",
        "Éclair",
    ]


def test_not_watched_first_goes_by_the_asking_persons_own_marks(titled, conn):
    _seen(conn, "201", user="cian")
    plain = ["The Amber Garden", "Amber Island", "An Apple", "Éclair", "eden", "The", "Zed"]
    first = _page(titled, "movies", sort="unwatched", limit=2)
    assert _names(first["items"]) == plain[:2]
    theirs = TestClient(titled.app)
    theirs.cookies.set("session_token", create_session(conn, "cian"))
    assert _names(_all(theirs, "movies", 2, sort="unwatched")) == [
        "The Amber Garden",
        *plain[2:],
        "Amber Island",
    ]
    # Another person's place in the list says nothing of what that person has seen.
    _seen(conn, "202")
    mine = _page(titled, "movies", sort="unwatched", limit=3)
    rest = _page(theirs, "movies", sort="unwatched", after=mine["next"])
    assert _names(rest["items"]) == ["Éclair", "eden", "The", "Zed", "Amber Island"]


def test_not_watched_first_puts_a_show_seen_in_full_last(client, conn):
    _season(conn, "511", "510", "Moor", "tv")
    conn.execute("UPDATE media_items SET episode_count = 1 WHERE id IN ('501', '511')")
    conn.commit()
    mark_watched(conn, "aoife", [MarkTarget("5011", "501", "500", 0.0)], NOW)
    assert _names(_all(client, "tv", 1, sort="unwatched")) == ["Moor", "Coast"]


def test_marks_made_while_paging_repeat_and_skip_nothing(titled, conn):
    _seen(conn, "200", "206")  # The Amber Garden, Zed
    before = _names(_all(titled, "movies", 60, sort="unwatched"))
    page = _page(titled, "movies", sort="unwatched", limit=3)
    got = page["items"]
    _seen(conn, "201")  # shown already
    _unseen(conn, "200")  # not shown yet, and would stand before what is
    page = _page(titled, "movies", sort="unwatched", limit=3, after=page["next"])
    got += page["items"]
    _seen(conn, "203")
    _unseen(conn, "206")
    page = _page(titled, "movies", sort="unwatched", limit=3, after=page["next"])
    got += page["items"]
    assert page["next"] is None
    assert _names(got) == before
    # Asked for anew, the order is the new one; the badges were never held back.
    assert [i["watched"] for i in got if i["title"] in ("Zed", "The Amber Garden")] == [False] * 2
    assert _names(_all(titled, "movies", 60, sort="unwatched"))[-2:] == ["Amber Island", "eden"]


def test_a_place_whose_marks_are_no_longer_held_goes_on_by_the_marks_of_now(titled, conn):
    _seen(conn, "200")
    page = _page(titled, "movies", sort="unwatched", limit=3)
    library_order._held.clear()
    rest = _page(titled, "movies", sort="unwatched", after=page["next"])
    assert _names(page["items"] + rest["items"]) == [
        "Amber Island",
        "An Apple",
        "Éclair",
        "eden",
        "The",
        "Zed",
        "The Amber Garden",
    ]


def test_places_held_for_paging_stay_few(titled, conn):
    for _ in range(library_order.HELD_MAX + 5):
        page = _page(titled, "movies", sort="unwatched", limit=1)
    assert len(library_order._held) == library_order.HELD_MAX
    assert page["next"].partition(".")[2] in library_order._held


@pytest.mark.parametrize("sort", ["az", "added", "year", "unwatched"])
def test_the_shared_link_of_a_library_row_is_its_first_page_in_every_order(
    titled, conn, monkeypatch, sort
):
    monkeypatch.setattr(pages, "plex_is_configured", lambda _conn: True)
    monkeypatch.setattr(pages, "GRID_PAGE_SIZE", 3)
    _seen(conn, "200")
    _year(conn, "203", 2020)
    grid = titled.get(f"/watch?all=movies&sort={sort}").json()["grid"]
    rest = _page(titled, "movies", sort=sort, after=grid["next"])
    assert grid["items"] + rest["items"] == _page(titled, "movies", sort=sort)["items"]
    assert grid["sort"] == sort


@pytest.mark.parametrize("sort", ["year", "unwatched"])
def test_a_row_that_is_not_of_the_library_has_no_such_order(titled, monkeypatch, sort):
    monkeypatch.setattr(pages, "plex_is_configured", lambda _conn: True)
    grid = titled.get(f"/watch?all=recent&sort={sort}").json()["grid"]
    assert grid["sort"] == "added"


@pytest.mark.parametrize("sort", ["az", "added", "year", "unwatched"])
def test_a_place_that_is_none_is_refused_in_every_order(client, sort):
    for after in ("nonsense", "bm9uZQ", ".token"):
        resp = client.get("/api/watch/library/movies/row", params={"after": after, "sort": sort})
        assert resp.status_code == 400


@pytest.mark.parametrize("sort", ["az", "added", "year", "unwatched"])
def test_films_alike_in_everything_are_each_there_once_in_every_order(client, conn, sort):
    conn.execute("DELETE FROM media_items WHERE media_type = 'movie'")
    for n in (3, 1, 2):
        _movie(conn, f"30{n}", "The Same", NOW)
    _seen(conn, "302")
    hrefs = [i["href"][-1] for i in _all(client, "movies", 1, sort=sort)]
    assert hrefs == (["1", "3", "2"] if sort == "unwatched" else ["1", "2", "3"])
