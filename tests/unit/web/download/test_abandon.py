"""Tests for POST /api/downloads/{dl_id}/abandon.

Covers :mod:`mediaman.web.routes.downloads`:
- movie happy path → abandon_movie called, 200
- series happy path with seasons → abandon_seasons called, 200
- upcoming series → abandon_series called (no seasons body), 200
- unknown dl_id → 404
- empty seasons list on series → 400
- unauthenticated request → 401/403
- _lookup_dl_item uses the 'id' field from build_item (integration)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mediaman.config import Config
from mediaman.db import init_db, set_connection
from mediaman.web.auth.password_hash import create_user
from mediaman.web.auth.session_store import create_session
from mediaman.web.routes.downloads import router as downloads_router


def _make_downloads_app(conn, secret_key: str) -> FastAPI:
    app = FastAPI()
    app.include_router(downloads_router)
    app.state.config = Config(secret_key=secret_key)
    app.state.db = conn
    set_connection(conn)
    return app


class TestAbandonEndpoint:
    """POST /api/downloads/{dl_id}/abandon"""

    def _make_client(self, db_path, secret_key):
        conn = init_db(str(db_path))
        app = _make_downloads_app(conn, secret_key)
        create_user(conn, "admin", "password1234", enforce_policy=False)
        token = create_session(conn, "admin")
        client = TestClient(app)
        client.cookies.set("session_token", token)
        return client

    def test_movie_happy_path(self, db_path, secret_key, monkeypatch):
        """POST with no seasons on a movie item → 200, abandon_movie called once."""
        called = {}

        def fake_abandon_movie(conn, sk, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_movie", fake_abandon_movie)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "movie", "arr_id": 42, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )
        assert resp.status_code == 200
        assert called == {"arr_id": 42, "dl_id": "radarr:Tenet"}
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "movie"

    def test_series_happy_path(self, db_path, secret_key, monkeypatch):
        """POST with seasons on a series item → 200, abandon_seasons called with correct numbers."""
        called = {}

        def fake_abandon_seasons(conn, sk, *, series_id, season_numbers, dl_id):
            called["series_id"] = series_id
            called["season_numbers"] = season_numbers
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="series", succeeded=season_numbers, dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_seasons", fake_abandon_seasons)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "series", "arr_id": 7, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3ASeverance/abandon",
            json={"seasons": [21, 22]},
        )
        assert resp.status_code == 200
        assert called["series_id"] == 7
        assert called["season_numbers"] == [21, 22]
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "series"

    def test_upcoming_series_dispatches_to_abandon_series(self, db_path, secret_key, monkeypatch):
        """Upcoming series in the "Coming soon" list → abandon_series, no seasons body needed."""
        called = {}

        def fake_abandon_series(conn, sk, *, series_id, dl_id):
            called["series_id"] = series_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="series", succeeded=[1, 2], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_series", fake_abandon_series)
        monkeypatch.setattr(
            "mediaman.web.routes.downloads.build_downloads_response",
            lambda c, sk: {"queue": [], "hero": None, "upcoming": [], "recent": []},
        )
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {
                "kind": "series",
                "arr_id": 7,
                "state": "upcoming",
                "dl_id": dl_id,
            },
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3AFutureShow/abandon",
            json={},  # no seasons body
        )
        assert resp.status_code == 200
        assert called == {"series_id": 7, "dl_id": "sonarr:FutureShow"}
        body = resp.json()
        assert body["ok"] is True
        assert body["abandoned"]["kind"] == "series"

    def test_unknown_dl_id_returns_404(self, db_path, secret_key, monkeypatch):
        """POST for a dl_id not in the queue → 404."""
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: None,
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ADoesNotExist/abandon",
            json={},
        )
        assert resp.status_code == 404

    def test_empty_seasons_on_series_returns_400(self, db_path, secret_key, monkeypatch):
        """POST with empty seasons list on a series item → 400."""
        monkeypatch.setattr(
            "mediaman.web.routes.downloads._lookup_dl_item",
            lambda c, sk, dl_id: {"kind": "series", "arr_id": 7, "dl_id": dl_id},
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/sonarr%3ASeverance/abandon",
            json={"seasons": []},
        )
        assert resp.status_code == 400

    def test_unauthenticated_request_is_rejected(self, db_path, secret_key):
        """POST without a session cookie → 401 or 403."""
        conn = init_db(str(db_path))
        app = _make_downloads_app(conn, secret_key)
        client = TestClient(app)  # no cookie set

        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )
        assert resp.status_code in (401, 403)

    def test_lookup_uses_real_payload_id_field(self, db_path, secret_key, monkeypatch):
        """Integration: _lookup_dl_item must find items via the canonical 'id'
        field produced by build_item, not the missing 'dl_id' key.

        Does NOT monkey-patch _lookup_dl_item — verifies the whole stack from
        POST through lookup, payload key matching, and abandon dispatch.
        """
        called = {}

        def fake_abandon_movie(conn, sk, *, arr_id, dl_id):
            called["arr_id"] = arr_id
            called["dl_id"] = dl_id
            from mediaman.services.downloads.abandon import AbandonResult

            return AbandonResult(kind="movie", succeeded=[0], dl_id=dl_id)

        monkeypatch.setattr("mediaman.web.routes.downloads.abandon_movie", fake_abandon_movie)

        # A searching movie item — matches what fetch_arr_queue returns for a
        # monitored Radarr title that has no NZBGet match.
        searching_item = {
            "kind": "movie",
            "dl_id": "radarr:Tenet",
            "title": "Tenet",
            "source": "Radarr",
            "poster_url": "",
            "progress": 0,
            "size": 0,
            "sizeleft": 0,
            "size_str": "0 B",
            "done_str": "0 B",
            "timeleft": "",
            "status": "searching",
            "arr_id": 42,
            "added_at": 0.0,
            "is_upcoming": False,
            "release_label": "",
        }

        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.fetch_arr_queue",
            lambda c, _sk: [searching_item],
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.build_nzbget_from_db",
            lambda c, _sk: None,
        )
        monkeypatch.setattr(
            "mediaman.services.downloads.download_queue.maybe_trigger_search",
            lambda *a, **kw: None,
        )

        client = self._make_client(db_path, secret_key)
        resp = client.post(
            "/api/downloads/radarr%3ATenet/abandon",
            json={},
        )

        assert resp.status_code == 200, (
            f"Expected 200 but got {resp.status_code} — "
            "likely _lookup_dl_item is still comparing against 'dl_id' instead of 'id'"
        )
        assert called.get("arr_id") == 42
        assert called.get("dl_id") == "radarr:Tenet"
