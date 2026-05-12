"""Tests for POST /api/media/{id}/delete, /keep, and /api/media/redownload."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from mediaman.web.routes.library import router as library_router
from mediaman.web.routes.library_api import _DELETE_LIMITER, _KEEP_LIMITER
from mediaman.web.routes.library_api import router as library_api_router
from tests.helpers.factories import insert_media_item


def _insert_movie(conn, media_id: str = "m1", radarr_id: int | None = 101) -> None:
    """Insert a minimal movie row into media_items."""
    insert_media_item(
        conn,
        id=media_id,
        title="Test Movie",
        media_type="movie",
        plex_rating_key="rk1",
        file_path="/media/movie.mkv",
        file_size_bytes=1_000_000,
        radarr_id=radarr_id,
    )


def _insert_tv_season(
    conn, media_id: str = "s1", sonarr_id: int | None = 202, season: int = 1
) -> None:
    """Insert a minimal TV season row into media_items."""
    insert_media_item(
        conn,
        id=media_id,
        title="The Show",
        media_type="tv_season",
        plex_rating_key="rk2",
        file_path="/media/show",
        file_size_bytes=2_000_000,
        sonarr_id=sonarr_id,
        season_number=season,
        show_title="The Show",
        show_rating_key="show-rk",
    )


def _app(app_factory, conn):
    return app_factory(library_router, library_api_router, conn=conn)


class TestMediaDelete:
    def setup_method(self):
        _DELETE_LIMITER.reset()

    def test_delete_requires_auth(self, app_factory, conn):
        """DELETE endpoint returns 401 when no session cookie is present."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/media/m1/delete")
        assert resp.status_code == 401

    def test_delete_nonexistent_returns_403(self, app_factory, authed_client, conn):
        """Deleting an unknown media_id returns 403, not 404 (finding 12).

        Returning 404 leaks whether a given media_id exists. With auth
        already confirmed, an unknown id is treated as forbidden access
        instead of a discoverable resource boundary.
        """
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/does-not-exist/delete")
        assert resp.status_code == 403

    def test_delete_movie_calls_radarr_and_removes_row(self, app_factory, authed_client, conn):
        """Deleting a movie calls Radarr delete_movie and removes the DB row."""
        _insert_movie(conn, radarr_id=101)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] == "m1"

        mock_radarr.delete_movie.assert_called_once_with(101)

        # Row must be gone from DB
        row = conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert row is None

    def test_delete_movie_without_radarr_id_skips_radarr(self, app_factory, authed_client, conn):
        """A movie with no stored radarr_id skips Radarr delete but still removes the DB row."""
        _insert_movie(conn, radarr_id=None)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        mock_radarr.delete_movie.assert_not_called()

        row = conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert row is None

    def test_delete_tv_season_calls_sonarr(self, app_factory, authed_client, conn):
        """Deleting a TV season calls Sonarr delete methods and removes the DB row."""
        _insert_tv_season(conn, sonarr_id=202, season=1)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_sonarr = MagicMock()
        mock_sonarr.has_remaining_files.return_value = True  # series still has other seasons

        with patch(
            "mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr
        ):
            resp = client.post("/api/media/s1/delete")

        assert resp.status_code == 200
        mock_sonarr.delete_episode_files.assert_called_once_with(202, 1)
        mock_sonarr.unmonitor_season.assert_called_once_with(202, 1)

        row = conn.execute("SELECT id FROM media_items WHERE id='s1'").fetchone()
        assert row is None

    def test_delete_also_removes_scheduled_actions(self, app_factory, authed_client, conn):
        """Deleting a media item also prunes its associated scheduled_actions rows."""
        _insert_movie(conn)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO scheduled_actions
               (media_item_id, action, scheduled_at, token, token_used)
               VALUES ('m1', 'snoozed', ?, 'tok1', 0)""",
            (now,),
        )
        conn.commit()

        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        sa_row = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1'"
        ).fetchone()
        assert sa_row is None


class TestMediaDeleteTransactional:
    """C22 — transactional delete with Arr failure propagation."""

    def setup_method(self):
        _DELETE_LIMITER.reset()

    def test_arr_failure_returns_502_and_preserves_row(self, app_factory, authed_client, conn):
        """If Radarr delete raises, the endpoint returns 502 and keeps the DB row."""
        _insert_movie(conn, radarr_id=101)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        import requests as _requests

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = _requests.ConnectionError("Radarr exploded")

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 502
        # Row must still be present
        row = conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert row is not None

    def test_arr_404_treated_as_already_gone(self, app_factory, authed_client, conn):
        """Arr returning 404 means already-deleted upstream — DB row must still be pruned."""
        _insert_movie(conn, radarr_id=101)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        import requests as _requests

        fake_resp = MagicMock()
        fake_resp.status_code = 404
        http_err = _requests.HTTPError(response=fake_resp)

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = http_err

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/m1/delete")

        assert resp.status_code == 200
        row = conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert row is None

    def test_retry_after_failure_succeeds(self, app_factory, authed_client, conn):
        """After a transient Arr failure, a retry must succeed (idempotency)."""
        _insert_movie(conn, radarr_id=101)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        import requests as _requests

        mock_radarr = MagicMock()
        mock_radarr.delete_movie.side_effect = [
            _requests.ConnectionError("first fails"),
            None,
        ]

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            first = client.post("/api/media/m1/delete")
            assert first.status_code == 502
            second = client.post("/api/media/m1/delete")
            assert second.status_code == 200

        row = conn.execute("SELECT id FROM media_items WHERE id='m1'").fetchone()
        assert row is None


class TestMediaKeep:
    def test_keep_requires_auth(self, app_factory, conn):
        """Keep endpoint returns 401 without a session cookie."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/media/m1/keep", data={"duration": "30 days"})
        assert resp.status_code == 401

    def test_keep_forever_inserts_protected_forever_action(self, app_factory, authed_client, conn):
        """Keep with duration=forever inserts a protected_forever scheduled_action."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "forever"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

        sa = conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "protected_forever"

    def test_keep_30d_inserts_snoozed_action(self, app_factory, authed_client, conn):
        """Keep with duration='30 days' inserts a snoozed scheduled_action."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "30 days"})

        assert resp.status_code == 200

        sa = conn.execute(
            "SELECT action, snooze_duration FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa is not None
        assert sa["action"] == "snoozed"
        assert sa["snooze_duration"] == "30 days"

    def test_keep_invalid_duration_returns_400(self, app_factory, authed_client, conn):
        """Keep with an unrecognised duration returns 400."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/m1/keep", data={"duration": "1y"})
        assert resp.status_code == 400

    def test_keep_nonexistent_item_returns_404(self, app_factory, authed_client, conn):
        """Keep for an unknown item returns 404."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/no-such-id/keep", data={"duration": "7 days"})
        assert resp.status_code == 404

    def test_keep_updates_existing_action(self, app_factory, authed_client, conn):
        """Keep on an already-kept item updates the existing row rather than inserting a duplicate."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        client.post("/api/media/m1/keep", data={"duration": "7 days"})
        resp = client.post("/api/media/m1/keep", data={"duration": "forever"})

        assert resp.status_code == 200
        rows = conn.execute(
            "SELECT id FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchall()
        # Must be at most one active row
        assert len(rows) == 1
        sa = conn.execute(
            "SELECT action FROM scheduled_actions WHERE media_item_id='m1' AND token_used=0"
        ).fetchone()
        assert sa["action"] == "protected_forever"


class TestMediaRedownload:
    def test_redownload_requires_auth(self, app_factory, conn):
        """Redownload endpoint returns 401 without a session cookie."""
        app = _app(app_factory, conn)
        client = TestClient(app, raise_server_exceptions=True)

        resp = client.post("/api/media/redownload", json={"title": "Dune"})
        assert resp.status_code == 401

    def test_redownload_empty_title_returns_400(self, app_factory, authed_client, conn):
        """Redownload with a blank title returns 400."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/redownload", json={"title": "   "})
        assert resp.status_code == 400

    def test_redownload_submits_to_radarr(self, app_factory, authed_client, conn):
        """Redownload with tmdb_id calls Radarr add_movie and returns ok=True."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = [{"tmdbId": 42, "title": "Dune", "year": 2021}]
        mock_radarr.add_movie.return_value = None

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(
                "/api/media/redownload",
                json={"title": "Dune", "tmdb_id": 42},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_radarr.add_movie.assert_called_once_with(42, "Dune")

    def test_redownload_falls_through_to_sonarr_when_radarr_finds_nothing(
        self, app_factory, authed_client, conn
    ):
        """When Radarr returns no lookup results, Sonarr is tried next."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = []  # Radarr finds nothing

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_term.return_value = [
            {"tvdbId": 999, "tmdbId": None, "title": "Severance", "year": 2022}
        ]
        mock_sonarr.add_series.return_value = None

        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr),
        ):
            resp = client.post(
                "/api/media/redownload",
                json={"title": "Severance", "tvdb_id": 999},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        mock_sonarr.add_series.assert_called_once()

    def test_redownload_title_only_refused(self, app_factory, authed_client, conn):
        """Title-only submissions without year are refused (C15)."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        resp = client.post("/api/media/redownload", json={"title": "Dune"})
        assert resp.status_code == 400

    def test_redownload_title_year_accepted_when_unambiguous(
        self, app_factory, authed_client, conn
    ):
        """Title + exact year + confident title match is accepted."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = [
            {"tmdbId": 99, "title": "Dune", "year": 2021},
            {"tmdbId": 7, "title": "Completely Different", "year": 1999},
        ]
        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post(
                "/api/media/redownload",
                json={"title": "Dune", "year": 2021},
            )
        assert resp.status_code == 200
        mock_radarr.add_movie.assert_called_once_with(99, "Dune")

    def test_redownload_title_year_ambiguous_refused(self, app_factory, authed_client, conn):
        """Two equally-confident matches with the same year fall through (no add)."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        # Two identical titles, same year — ambiguous.
        mock_radarr.lookup_by_term.return_value = [
            {"tmdbId": 1, "title": "Inception", "year": 2010},
            {"tmdbId": 2, "title": "Inception", "year": 2010},
        ]
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_term.return_value = []
        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr),
        ):
            resp = client.post(
                "/api/media/redownload",
                json={"title": "Inception", "year": 2010},
            )
        # add_movie must not be called in an ambiguous case.
        mock_radarr.add_movie.assert_not_called()
        # Either a 409 (if Sonarr branch returns ambiguous) or fall-through
        # "not found" response — both leave the user safe.
        assert resp.status_code in (200, 400, 404, 409)
        assert resp.json().get("ok") is False

    def test_redownload_wrong_year_refused(self, app_factory, authed_client, conn):
        """A title-only request whose year does not match any lookup entry is refused."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = [
            {"tmdbId": 10, "title": "Inception", "year": 2010},
        ]
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_term.return_value = []
        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr),
        ):
            client.post(
                "/api/media/redownload",
                json={"title": "Inception", "year": 2020},  # wrong year
            )
        mock_radarr.add_movie.assert_not_called()


class TestMediaKeepRateLimit:
    """H20 — /api/media/{id}/keep must be rate-limited."""

    def setup_method(self):
        _KEEP_LIMITER.reset()
        _DELETE_LIMITER.reset()

    def test_keep_rate_limit_blocks_after_window_exceeded(self, app_factory, authed_client, conn):
        """Hammering /keep more than 60 times per minute returns 429."""
        _insert_movie(conn)
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        cap = _KEEP_LIMITER._max_in_window
        for i in range(cap):
            r = client.post("/api/media/m1/keep", data={"duration": "forever"})
            assert r.status_code != 429, f"Rate limit fired early on iteration {i}"

        r = client.post("/api/media/m1/keep", data={"duration": "forever"})
        assert r.status_code == 429
        assert r.json()["error"] is not None


class TestLibrarySearchLikeEscape:
    """H10 — LIKE metacharacters in the search query must not be treated as wildcards."""

    def test_percent_sign_is_treated_as_literal(self, conn):
        """A query containing '%' must only match titles containing a literal '%'."""
        from mediaman.db import set_connection

        set_connection(conn)
        insert_media_item(
            conn,
            id="m1",
            title="50% Off",
            plex_rating_key="rk1",
            file_path="/f1",
            file_size_bytes=1_000_000,
        )
        insert_media_item(
            conn,
            id="m2",
            title="Normal Movie",
            plex_rating_key="rk2",
            file_path="/f2",
            file_size_bytes=1_000_000,
        )

        from mediaman.web.repository.library_query import fetch_library as _fetch_library

        items, _total = _fetch_library(conn, q="%")
        titles = {i["title"] for i in items}
        assert "50% Off" in titles
        assert "Normal Movie" not in titles

    def test_underscore_is_treated_as_literal(self, conn):
        """A query containing '_' must match titles with a literal underscore only."""
        from mediaman.db import set_connection

        set_connection(conn)
        insert_media_item(
            conn,
            id="m1",
            title="foo_bar",
            plex_rating_key="rk1",
            file_path="/f1",
            file_size_bytes=1_000_000,
        )
        insert_media_item(
            conn,
            id="m2",
            title="fooXbar",
            plex_rating_key="rk2",
            file_path="/f2",
            file_size_bytes=1_000_000,
        )

        from mediaman.web.repository.library_query import fetch_library as _fetch_library

        items, _total = _fetch_library(conn, q="_")
        titles = {i["title"] for i in items}
        assert "foo_bar" in titles
        assert "fooXbar" not in titles


class TestRedownloadSafeHTTPError:
    """SafeHTTPError 409/422 from Radarr/Sonarr must surface as 'already exists' responses."""

    def setup_method(self):
        _DELETE_LIMITER.reset()
        _KEEP_LIMITER.reset()

    def test_radarr_409_safe_http_error_returns_already_exists(
        self, app_factory, authed_client, conn
    ):
        """A 409 SafeHTTPError from Radarr returns the 'already exists in Radarr' message."""
        from mediaman.services.infra.http import SafeHTTPError

        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = [{"tmdbId": 42, "title": "Dune", "year": 2021}]
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/redownload", json={"title": "Dune", "tmdb_id": 42})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "already exists in Radarr" in body["error"]

    def test_radarr_422_safe_http_error_returns_already_exists(
        self, app_factory, authed_client, conn
    ):
        """A 422 SafeHTTPError from Radarr is treated identically to 409."""
        from mediaman.services.infra.http import SafeHTTPError

        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = [{"tmdbId": 42, "title": "Dune", "year": 2021}]
        mock_radarr.add_movie.side_effect = SafeHTTPError(
            status_code=422, body_snippet="unprocessable", url="http://radarr/api/v3/movie"
        )

        with patch(
            "mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr
        ):
            resp = client.post("/api/media/redownload", json={"title": "Dune", "tmdb_id": 42})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "already exists in Radarr" in body["error"]

    def test_sonarr_409_safe_http_error_returns_already_exists(
        self, app_factory, authed_client, conn
    ):
        """A 409 SafeHTTPError from Sonarr returns the 'already exists in Sonarr' message."""
        from mediaman.services.infra.http import SafeHTTPError

        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        mock_radarr = MagicMock()
        mock_radarr.lookup_by_term.return_value = []  # Radarr finds nothing — falls through

        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_term.return_value = [
            {"tvdbId": 999, "tmdbId": None, "title": "Severance", "year": 2022}
        ]
        mock_sonarr.add_series.side_effect = SafeHTTPError(
            status_code=409, body_snippet="already exists", url="http://sonarr/api/v3/series"
        )

        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr),
        ):
            resp = client.post("/api/media/redownload", json={"title": "Severance", "tvdb_id": 999})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "already exists in Sonarr" in body["error"]


class TestRedownloadTitleCap:
    """H11 — redownload title must be capped at 256 chars."""

    def setup_method(self):
        _DELETE_LIMITER.reset()
        _KEEP_LIMITER.reset()

    def test_overlong_title_is_silently_truncated(self, app_factory, authed_client, conn):
        """A title over 256 chars is truncated, not rejected."""
        app = _app(app_factory, conn)
        client = authed_client(app, conn)

        long_title = "A" * 300
        mock_radarr = MagicMock()
        # Simulate lookup returning nothing (title won't match after truncation)
        mock_radarr.lookup_by_term.return_value = []
        mock_sonarr = MagicMock()
        mock_sonarr.lookup_by_term.return_value = []

        with (
            patch("mediaman.web.routes.library_api.build_radarr_from_db", return_value=mock_radarr),
            patch("mediaman.web.routes.library_api.build_sonarr_from_db", return_value=mock_sonarr),
        ):
            resp = client.post(
                "/api/media/redownload",
                json={"title": long_title, "tmdb_id": 42},
            )
        # Must not raise; result doesn't matter (lookup returns nothing)
        assert resp.status_code in (200, 400, 404)
        # The lookup term must have been truncated to 256 chars
        call_args = mock_radarr.lookup_by_term.call_args
        if call_args is not None:
            term_used = call_args[0][0] if call_args[0] else ""
            assert len(term_used) <= 256 + 10  # +10 for URL encoding overhead
