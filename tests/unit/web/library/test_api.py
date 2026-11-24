"""Tests for mediaman.web.routes.library.api.

Covers GET /api/library.
The delete/keep/redownload endpoints are already covered in
tests/unit/web/test_library_mutations.py — this file targets what is
not already covered.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.library_api import (
    _DELETE_LIMITER,
    _KEEP_LIMITER,
)
from mediaman.web.routes.library_api import (
    router as api_router,
)
from tests.helpers.factories import insert_media_item


def _insert_movie(conn, media_id: str, title: str = "Test Movie") -> None:
    insert_media_item(
        conn,
        id=media_id,
        title=title,
        media_type="movie",
        plex_rating_key=f"rk-{media_id}",
        file_path="/f",
        file_size_bytes=1_000_000,
    )


class TestApiLibraryList:
    @pytest.fixture(autouse=True)
    def _reset_limiters(self):
        _DELETE_LIMITER.reset()
        _KEEP_LIMITER.reset()

    def test_requires_auth(self, app_factory, conn):
        app = app_factory(api_router, conn=conn)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/library")
        assert resp.status_code == 401

    def test_returns_paginated_response(self, app_factory, authed_client, conn):
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Dune")
        resp = client.get("/api/library")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert "page" in body
        assert "total_pages" in body

    def test_empty_library_returns_zero_total(self, app_factory, authed_client, conn):
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/library")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_search_query_filters_results(self, app_factory, authed_client, conn):
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Inception")
        _insert_movie(conn, "m2", "Dune")
        resp = client.get("/api/library?q=Inception")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Inception"

    def test_invalid_sort_falls_back_gracefully(self, app_factory, authed_client, conn):
        """An unrecognised sort value does not crash; defaults to added_desc."""
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Dune")
        resp = client.get("/api/library?sort=bogus_sort")
        assert resp.status_code == 200

    def test_pagination_respected(self, app_factory, authed_client, conn):
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        for i in range(5):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        resp = client.get("/api/library?per_page=2&page=2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

    def test_type_filter_movie(self, app_factory, authed_client, conn):
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Dune")
        resp = client.get("/api/library?type=movie")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_type_filter_invalid_returns_all(self, app_factory, authed_client, conn):
        """An unrecognised type value is silently ignored — all items returned."""
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Dune")
        resp = client.get("/api/library?type=nonsense")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_per_page_capped_at_100(self, app_factory, authed_client, conn):
        """per_page values above 100 are rejected with 422."""
        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        resp = client.get("/api/library?per_page=9999")
        assert resp.status_code == 422


class TestApiLibraryListDeletion:
    def test_rows_stats_and_sort_carry_the_deletion_data(self, app_factory, authed_client, conn):
        from datetime import UTC, datetime, timedelta

        from tests.helpers.factories import insert_scheduled_action

        app = app_factory(api_router, conn=conn)
        client = authed_client(app, conn)
        _insert_movie(conn, "m1", "Alpha")
        _insert_movie(conn, "m2", "Beta")
        insert_scheduled_action(
            conn,
            media_item_id="m2",
            action="scheduled_deletion",
            token="tok-m2",
            execute_at=(datetime.now(UTC) + timedelta(days=3)).isoformat(),
        )
        body = client.get("/api/library?sort=deletion_asc").json()
        assert [i["id"] for i in body["items"]] == ["m2", "m1"]
        assert body["items"][0]["deletion_days"] in (2, 3)
        assert body["items"][1]["deletion_at"] is None
        assert body["stats"]["due_soon"] == 1
        assert "movies_size" in body["stats"]


class TestApiLibraryTitles:
    @pytest.fixture
    def get(self, app_factory, authed_client, conn):
        client = authed_client(app_factory(api_router, conn=conn), conn)
        return lambda path: client.get(path).json()

    def test_an_item_carries_the_title_to_open(self, get, conn):
        insert_media_item(conn, id="10", title="B", file_size_bytes=3)
        # A show without its own key groups under its title, which may be a number.
        for item_id, key, size in (("20", "900", 2), ("30", None, 1)):
            insert_media_item(
                conn,
                id=item_id,
                media_type="tv_season",
                show_title="24",
                show_rating_key=key,
                file_size_bytes=size,
            )

        items = get("/api/library?sort=size_desc")["items"]

        assert [i["ref"] for i in items] == ["movie-10", "show-900", None]

    def test_titles_with_no_size_are_counted_and_a_show_with_one_sized_season_is_not(
        self, get, conn
    ):
        insert_media_item(conn, id="1", file_size_bytes=0)
        insert_media_item(conn, id="2", file_size_bytes=7)
        for item_id, kind, show, size in (
            ("3", "tv_season", "50", 0),
            ("4", "tv_season", "50", 9),
            ("5", "tv_season", "60", 0),
            ("6", "tv_season", "60", 0),
            ("7", "anime_season", "70", 0),
        ):
            insert_media_item(
                conn,
                id=item_id,
                media_type=kind,
                show_title=show,
                show_rating_key=show,
                file_size_bytes=size,
            )

        assert get("/api/library")["stats"]["no_size"] == {"movies": 1, "tv": 1, "anime": 1}
