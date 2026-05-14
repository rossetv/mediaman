"""Tests for :mod:`mediaman.web.routes.library.api`.

Covers GET /api/library and the _pick_lookup_match helper.
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
    _pick_lookup_match,
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


class TestPickLookupMatch:
    """Unit tests for the _pick_lookup_match helper."""

    def test_empty_lookup_returns_error(self):
        _, err = _pick_lookup_match(
            [], title="Dune", year=2021, tmdb_id=None, tvdb_id=None, imdb_id=None
        )
        assert err == "No lookup results"

    def test_tmdb_id_match_returns_entry(self):
        lookup = [{"tmdbId": 42, "title": "Dune", "year": 2021}]
        entry, err = _pick_lookup_match(
            lookup,
            title="Dune",
            year=2021,
            tmdb_id=42,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is None
        assert entry is not None
        assert entry["tmdbId"] == 42

    def test_ambiguous_tmdb_id_returns_error(self):
        """Two entries sharing the same tmdbId → ambiguous."""
        lookup = [
            {"tmdbId": 42, "title": "Dune", "year": 2021},
            {"tmdbId": 42, "title": "Dune Part Two", "year": 2024},
        ]
        _, err = _pick_lookup_match(
            lookup,
            title="Dune",
            year=2021,
            tmdb_id=42,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is not None
        assert "Ambiguous" in err

    def test_no_id_falls_back_to_title_year(self):
        """Without any ID, a high-confidence title+year match is returned."""
        lookup = [{"tmdbId": 10, "title": "Inception", "year": 2010}]
        entry, err = _pick_lookup_match(
            lookup,
            title="Inception",
            year=2010,
            tmdb_id=None,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is None
        assert entry is not None

    def test_low_confidence_title_match_rejected(self):
        """A fuzzy title score below 0.9 is rejected."""
        lookup = [{"tmdbId": 1, "title": "Completely Different Title", "year": 2020}]
        _, err = _pick_lookup_match(
            lookup,
            title="Inception",
            year=2020,
            tmdb_id=None,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is not None

    def test_year_mismatch_rejected(self):
        """A good title match with wrong year is rejected."""
        lookup = [{"tmdbId": 1, "title": "Inception", "year": 2010}]
        _, err = _pick_lookup_match(
            lookup,
            title="Inception",
            year=2020,
            tmdb_id=None,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is not None

    def test_id_not_found_in_lookup_returns_error(self):
        """A supplied tmdb_id that does not appear in the lookup results returns an error."""
        lookup = [{"tmdbId": 99, "title": "Dune", "year": 2021}]
        _, err = _pick_lookup_match(
            lookup,
            title="Dune",
            year=2021,
            tmdb_id=42,
            tvdb_id=None,
            imdb_id=None,
        )
        assert err is not None
        assert "did not match" in err


class TestApiLibraryList:
    """GET /api/library endpoint tests."""

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
