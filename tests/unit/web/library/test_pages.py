"""Tests for :mod:`mediaman.web.routes.library.pages`.

GET /library page route:
  - unauthenticated → redirect to /login
  - authenticated → 200 with rendered HTML context
  - sort/type/page params are clamped and sanitised
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mediaman.web.routes.library import router as pages_router
from tests.helpers.factories import insert_media_item


@pytest.fixture
def _app(app_factory, conn, templates_stub):
    return app_factory(pages_router, conn=conn, state_extras={"templates": templates_stub})


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


class TestLibraryPage:
    def test_unauthenticated_redirects_to_login(self, _app):
        """GET /library without a valid session redirects to /login."""
        client = TestClient(_app, raise_server_exceptions=True)
        resp = client.get("/library", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].endswith("/login")

    def test_authenticated_returns_200(self, _app, authed_client, conn):
        """GET /library with a valid session returns 200."""
        client = authed_client(_app, conn)
        resp = client.get("/library")
        assert resp.status_code == 200

    def test_context_includes_username(self, _app, authed_client, conn):
        """Rendered context must include the admin username."""
        client = authed_client(_app, conn)
        resp = client.get("/library")
        ctx = resp.json()
        assert ctx["username"] == "admin"

    def test_context_includes_items_and_stats(self, _app, authed_client, conn):
        """Rendered context includes items list and stats dict."""
        _insert_movie(conn, "m1", "Dune")
        client = authed_client(_app, conn)
        resp = client.get("/library")
        ctx = resp.json()
        assert "items" in ctx
        assert "stats" in ctx
        assert len(ctx["items"]) == 1

    def test_invalid_sort_is_sanitised_to_default(self, _app, authed_client, conn):
        """An unknown sort value is silently clamped to added_desc."""
        client = authed_client(_app, conn)
        resp = client.get("/library?sort=invalid_sort")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["current_sort"] == "added_desc"

    def test_invalid_type_is_sanitised_to_empty(self, _app, authed_client, conn):
        """An unknown type value is sanitised to an empty string (show all)."""
        client = authed_client(_app, conn)
        resp = client.get("/library?type=invalid_type")
        assert resp.status_code == 200
        ctx = resp.json()
        assert ctx["current_type"] == ""

    def test_page_below_minimum_rejected(self, _app, authed_client, conn):
        """A page value of 0 is rejected with 422 (finding 17).

        Pagination bounds are now enforced at the input layer — invalid
        values surface as a Pydantic validation error rather than being
        silently rewritten to 1.
        """
        client = authed_client(_app, conn)
        resp = client.get("/library?page=0")
        assert resp.status_code == 422

    def test_per_page_above_maximum_rejected(self, _app, authed_client, conn):
        """A per_page value above 100 is rejected with 422 (finding 17)."""
        client = authed_client(_app, conn)
        resp = client.get("/library?per_page=9999")
        assert resp.status_code == 422

    def test_pagination_metadata_correct(self, _app, authed_client, conn):
        """page_start, page_end, and total_pages are computed correctly."""
        for i in range(5):
            _insert_movie(conn, f"m{i}", f"Film {i}")
        client = authed_client(_app, conn)
        resp = client.get("/library?per_page=2&page=1")
        ctx = resp.json()
        assert ctx["total"] == 5
        assert ctx["total_pages"] == 3
        assert ctx["page_start"] == 1
        assert ctx["page_end"] == 2

    def test_search_query_propagated_to_context(self, _app, authed_client, conn):
        """The search query string is echoed back in the template context."""
        client = authed_client(_app, conn)
        resp = client.get("/library?q=Dune")
        ctx = resp.json()
        assert ctx["q"] == "Dune"
