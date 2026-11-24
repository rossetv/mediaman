"""The paged library rows: the page's own order and cards, for the person asking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mediaman.web.auth.session_store import create_session
from mediaman.web.repository.watch_progress import MarkTarget, mark_watched
from mediaman.web.routes.watch import library_row_api, pages
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


def _all(client, kind, limit):
    items, after = [], ""
    while after is not None:
        page = _page(client, kind, limit=limit, after=after)
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
